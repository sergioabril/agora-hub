import asyncio
import shlex
from dataclasses import dataclass
from typing import Protocol

import asyncssh
import httpx

from .config import Config


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


class AdapterError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


STATES = ("stopped", "starting", "running", "stopping", "failed", "unknown")


class Adapters(Protocol):
    async def observe(self, machine_id: str) -> None: ...

    async def run(
        self,
        machine_id: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float,
        stdin: str | None = None,
    ) -> CommandResult: ...

    async def wake(self, machine_id: str) -> None: ...

    async def shutdown(self, machine_id: str) -> CommandResult: ...

    async def deployment_status(self, deployment_id: str) -> str: ...

    async def ready(self, deployment_id: str) -> None: ...


def redact(text: str, config: Config) -> str:
    secrets = [config.admin_token, config.ha_token]
    for machine in config.machines.values():
        secrets.extend([machine.password, machine.sudo_password])
    for secret in secrets:
        if secret is not None:
            value = secret.get_secret_value()
            if value:
                text = text.replace(value, "***")
    return text


class RealAdapters:
    def __init__(self, config: Config, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    def _error(self, kind: str, error: object) -> AdapterError:
        return AdapterError(kind, redact(str(error), self.config))

    async def _connect(self, machine_id: str):
        machine = self.config.machines[machine_id]
        authentication = (
            {"password": machine.password.get_secret_value()}
            if machine.password
            else {"client_keys": [machine.key_path]}
        )
        try:
            return await asyncssh.connect(
                machine.host,
                port=machine.port,
                username=machine.user,
                known_hosts=str(self.config.hub.ssh.known_hosts_path),
                connect_timeout=self.config.hub.timeouts.ssh_connect_seconds,
                **authentication,
            )
        except asyncssh.PermissionDenied as error:
            raise self._error("auth", error) from error
        except asyncssh.HostKeyNotVerifiable as error:
            raise self._error("host_key", error) from error
        except (OSError, asyncssh.DisconnectError) as error:
            raise self._error("connect", error) from error

    async def observe(self, machine_id: str) -> None:
        connection = await self._connect(machine_id)
        connection.close()
        await connection.wait_closed()

    async def run(
        self,
        machine_id: str,
        command: str,
        *,
        cwd: str | None = None,
        timeout: float,
        stdin: str | None = None,
    ) -> CommandResult:
        connection = await self._connect(machine_id)
        remote_command = f"cd {shlex.quote(cwd)} && {command}" if cwd else command
        try:
            result = await connection.run(
                remote_command, input=stdin, timeout=timeout, check=False
            )
            return CommandResult(
                -1 if result.exit_status is None else result.exit_status,
                redact(result.stdout or "", self.config),
                redact(result.stderr or "", self.config),
            )
        except asyncio.TimeoutError as error:
            raise self._error("timeout", "command timed out") from error
        except asyncssh.DisconnectError as error:
            return CommandResult(-1, "", redact(str(error), self.config))
        except OSError as error:
            raise self._error("connect", error) from error
        finally:
            connection.close()
            await connection.wait_closed()

    async def shutdown(self, machine_id: str) -> CommandResult:
        machine = self.config.machines[machine_id]
        stdin = (
            machine.sudo_password.get_secret_value() + "\n"
            if machine.sudo_password
            else None
        )
        result = await self.run(
            machine_id,
            machine.shutdown_command,
            timeout=self.config.hub.timeouts.machine_shutdown_seconds,
            stdin=stdin,
        )
        if result.exit_status not in (0, -1):
            raise AdapterError(
                "command",
                redact(f"shutdown exited {result.exit_status}: {result.stderr}", self.config),
            )
        return result

    async def deployment_status(self, deployment_id: str) -> str:
        deployment = self.config.deployments[deployment_id]
        if not deployment.status_command:
            raise AdapterError("command", "status command is not configured")
        result = await self.run(
            deployment.head,
            deployment.status_command,
            cwd=deployment.working_directory,
            timeout=self.config.hub.timeouts.status_command_seconds,
        )
        state = result.stdout.strip()
        if result.exit_status or state not in STATES:
            detail = result.stderr or result.stdout
            raise AdapterError(
                "command",
                redact(f"invalid status result ({result.exit_status}): {detail}", self.config),
            )
        return state

    async def ready(self, deployment_id: str) -> None:
        deployment = self.config.deployments[deployment_id]
        url = str(deployment.endpoint).rstrip("/") + deployment.readiness.path
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.config.hub.timeouts.http_seconds
            ) as client:
                response = await client.get(url)
        except httpx.RequestError as error:
            raise self._error("http", error) from error
        if response.status_code != deployment.readiness.expected_status:
            raise AdapterError("http", f"readiness returned HTTP {response.status_code}")

    async def wake(self, machine_id: str) -> None:
        machine = self.config.machines[machine_id]
        home_assistant = self.config.hub.home_assistant
        base_url = str(home_assistant.base_url).rstrip("/")
        headers = {"Authorization": f"Bearer {self.config.ha_token.get_secret_value()}"}
        body = {"entity_id": machine.plug_entity_id}
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                headers=headers,
                timeout=self.config.hub.timeouts.http_seconds,
            ) as client:
                paths = (home_assistant.turn_off_path, home_assistant.turn_on_path)
                for index, path in enumerate(paths):
                    response = await client.post(base_url + path, json=body)
                    response.raise_for_status()
                    if index == 0:
                        await asyncio.sleep(home_assistant.power_cycle_delay_seconds)
        except httpx.HTTPError as error:
            raise self._error("http", error) from error


def make_adapters(
    config: Config, transport: httpx.AsyncBaseTransport | None = None
) -> Adapters:
    return RealAdapters(config, transport)
