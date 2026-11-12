import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import asyncssh
import httpx

from .config import Config, MachineConfig
from .home_assistant import HomeAssistant, HomeAssistantError


@dataclass
class CommandResult:
    exit_status: int
    stdout: str
    stderr: str


class AdapterError(Exception):
    def __init__(self, kind: str, message: str):
        self.kind = kind
        super().__init__(message)


class Adapters(Protocol):
    async def observe(self, machine_id: str) -> None: ...

    async def login(self, machine: MachineConfig) -> None: ...

    async def run(
        self,
        machine_id: str,
        command: str,
        *,
        timeout: float,
        stdin: str | None = None,
    ) -> CommandResult: ...

    async def stream(
        self, machine_id: str, command: str, *, timeout: float, on_line: Callable[[str, str], None]
    ) -> int: ...

    async def wake(self, machine_id: str) -> None: ...

    async def shutdown(self, machine_id: str) -> CommandResult: ...

    async def ready(self, deployment_id: str) -> None: ...


def redact(text: str, config: Config) -> str:
    secrets = [config.hub.api.admin_token, config.hub.home_assistant.token]
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
        self.home_assistant = HomeAssistant(config, transport)

    def _error(self, kind: str, error: object) -> AdapterError:
        # Timeouts often stringify to "" (e.g. httpx.ConnectTimeout); fall back to the class name.
        message = str(error) or type(error).__name__
        return AdapterError(kind, redact(message, self.config))

    # Trust on first use, like `ssh -o StrictHostKeyChecking=accept-new`: an unknown host's key
    # is saved; a host that is already listed must keep presenting a listed key.
    async def _trust_new_host(self, host: str, port: int) -> None:
        path = self.config.hub.ssh.known_hosts_path
        if any(asyncssh.match_known_hosts(str(path), host, "", None if port == 22 else port)[:2]):
            return
        timeout = self.config.hub.timeouts.ssh_connect_seconds
        key = await asyncio.wait_for(asyncssh.get_server_host_key(host, port), timeout)
        if key:
            entry = host if port == 22 else f"[{host}]:{port}"
            with path.open("a") as file:
                file.write(f"{entry} {key.export_public_key().decode()}")

    async def _connect(self, machine: MachineConfig):
        authentication = (
            {"password": machine.password.get_secret_value()}
            if machine.password
            else {"client_keys": [machine.key_path]}
        )
        try:
            await self._trust_new_host(machine.host, machine.port)
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
        except TimeoutError as error:  # an OSError, but its message is empty
            timeout = self.config.hub.timeouts.ssh_connect_seconds
            raise self._error("connect", f"timed out after {timeout:g}s") from error
        except (OSError, asyncssh.DisconnectError) as error:
            raise self._error("connect", error) from error

    async def login(self, machine: MachineConfig) -> None:
        connection = await self._connect(machine)
        connection.close()
        await connection.wait_closed()

    async def observe(self, machine_id: str) -> None:
        await self.login(self.config.machines[machine_id])

    async def run(
        self,
        machine_id: str,
        command: str,
        *,
        timeout: float,
        stdin: str | None = None,
    ) -> CommandResult:
        connection = await self._connect(self.config.machines[machine_id])
        try:
            result = await connection.run(command, input=stdin, timeout=timeout, check=False)
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

    async def stream(
        self, machine_id: str, command: str, *, timeout: float, on_line: Callable[[str, str], None]
    ) -> int:
        connection = await self._connect(self.config.machines[machine_id])

        async def pump(source, reader):
            async for line in reader:
                on_line(source, redact(line.rstrip("\r\n"), self.config))

        async def work():
            process = await connection.create_process(command, encoding="utf-8", errors="replace")
            process.stdin.write_eof()
            await asyncio.gather(pump("stdout", process.stdout), pump("stderr", process.stderr))
            await process.wait_closed()
            return -1 if process.exit_status is None else process.exit_status

        try:
            return await asyncio.wait_for(work(), timeout)
        except asyncio.TimeoutError as error:
            raise self._error("timeout", "command timed out") from error
        except asyncssh.DisconnectError as error:
            on_line("stderr", redact(str(error), self.config))
            return -1
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

    async def ready(self, deployment_id: str) -> None:
        deployment = self.config.deployments[deployment_id]
        try:
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.config.hub.timeouts.http_seconds
            ) as client:
                response = await client.get(str(deployment.endpoint))
        except httpx.RequestError as error:
            raise self._error("http", error) from error
        if not response.is_success:
            raise AdapterError("http", f"endpoint returned HTTP {response.status_code}")

    async def wake(self, machine_id: str) -> None:
        try:
            await self.home_assistant.power_cycle(self.config.machines[machine_id].plug_entity_id)
        except HomeAssistantError as error:
            raise AdapterError(error.kind, str(error)) from error


def make_adapters(
    config: Config, transport: httpx.AsyncBaseTransport | None = None
) -> Adapters:
    return RealAdapters(config, transport)
