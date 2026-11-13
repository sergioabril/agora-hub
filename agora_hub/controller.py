import asyncio
import logging
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .adapters import AdapterError, Adapters, redact
from .config import Config, MachineConfig

log = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Operation:
    action: str
    status: str = "in_progress"
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    error: str | None = None
    output: str = ""


class Conflict(Exception):
    def __init__(self, code: str, message: str, blocking: dict | None = None):
        self.code = code
        self.message = message
        self.blocking = blocking
        super().__init__(message)


class Controller:
    def __init__(self, config: Config, adapters: Adapters):
        self.config = config
        self.adapters = adapters
        self.active: dict | None = None
        self.task: asyncio.Task | None = None
        self.machine_operations: dict[str, Operation] = {}
        self.deployment_operations: dict[str, Operation] = {}
        self.machine_observations: dict[str, dict] = {}
        self.deployment_observations: dict[str, dict] = {}
        self.logs: dict[str, dict] = {}  # deployment_id -> {"entries": deque, "count": total appended}
        self.changed = asyncio.Event()  # replaced on every new line or finished operation
        self.shutdown_requested = {machine_id: False for machine_id in config.machines}

    def _operation(self, operation: Operation | None) -> dict | None:
        return asdict(operation) if operation else None

    def _reserve(self, action: str, target: str, machines: list[str]) -> None:
        if self.active:
            raise Conflict("busy", "another operation is active", dict(self.active))
        self.active = {"action": action, "target": target, "machines": list(machines)}

    def _release(self) -> None:
        self.active = None

    def _error(self, error: BaseException) -> str:
        prefix = f"{error.kind}: " if isinstance(error, AdapterError) else ""
        return redact(prefix + str(error), self.config)

    def _notify(self) -> None:
        self.changed.set()
        self.changed = asyncio.Event()

    def _begin(self, deployment_id: str, action: str) -> Operation:
        self.logs[deployment_id] = {"entries": deque(maxlen=2000), "count": 0}
        self.deployment_operations[deployment_id] = Operation(action)
        return self.deployment_operations[deployment_id]

    def _log(self, deployment_id: str, source: str, text: str) -> str:
        text = redact(text, self.config).rstrip("\r\n")[:2000]
        log = self.logs[deployment_id]
        log["entries"].append({"source": source, "text": text, "at": _now()})
        log["count"] += 1
        self._notify()
        return text

    def follow(self, deployment_id: str):
        operation = self.deployment_operations[deployment_id]
        log = self.logs[deployment_id]
        first = {"type": "operation", "operation": asdict(operation)}

        async def events():
            yield first
            sent = 0
            while True:
                changed = self.changed
                entries, count, finished = list(log["entries"]), log["count"], operation.finished_at
                for entry in entries[max(len(entries) - (count - sent), 0):]:
                    yield {"type": "line", **entry}
                sent = count
                if finished:
                    yield {"type": "end", "operation": asdict(operation)}
                    return
                await changed.wait()

        return events()

    async def _run_operation(self, operation: Operation, coroutine) -> bool:
        try:
            await coroutine
            operation.status = "succeeded"
            operation.error = None
            return True
        except asyncio.CancelledError:
            operation.status = "cancelled"
            raise
        except Exception as error:
            operation.status = "failed"
            operation.error = self._error(error)
            return False
        finally:
            operation.finished_at = _now()
            self._notify()

    async def test_login(self, machine: MachineConfig) -> dict:
        try:
            await self.adapters.login(machine)
        except AdapterError as caught:
            return {"reachable": caught.kind in ("auth", "host_key"), "error": self._error(caught)}
        return {"reachable": True, "error": None}

    async def _observe_machine(self, machine_id: str) -> dict:
        reachable = False
        error = None
        try:
            await self.adapters.observe(machine_id)
            reachable = True
        except AdapterError as caught:
            reachable = caught.kind in ("auth", "host_key")
            error = self._error(caught)
        observation = {
            "reachable": reachable,
            "error": error,
            "checked_at": _now(),
            "shutdown_requested": self.shutdown_requested.get(machine_id, False),
        }
        self.machine_observations[machine_id] = observation
        return observation

    async def _observe_deployment(self, deployment_id: str) -> dict:
        try:
            models = await self.adapters.ready(deployment_id)
            ready, error = True, None
        except AdapterError as caught:
            models, ready, error = [], False, self._error(caught)
        observation = {"ready": ready, "models": models, "error": error, "checked_at": _now()}
        self.deployment_observations[deployment_id] = observation
        return observation

    def _in_progress(self, deployment_id: str) -> Operation | None:
        operation = self.deployment_operations.get(deployment_id)
        return operation if operation and operation.status == "in_progress" else None

    def _observed_state(self, deployment_id: str) -> str:
        if operation := self._in_progress(deployment_id):
            return "starting" if operation.action == "start" else "stopping"
        return "running" if self.deployment_observations[deployment_id]["ready"] else "stopped"

    async def machines_status(self) -> dict:
        await asyncio.gather(*(self._observe_machine(key) for key in self.config.machines))
        machines = {
            key: {
                "name": machine.name,
                "host": machine.host,
                "user": machine.user,
                "port": machine.port,
                "key_path": self._key_path(machine),
                "plug_entity_id": machine.plug_entity_id,
                "shutdown_command": machine.shutdown_command,
                "device": machine.device,
                **self.machine_observations[key],
                "operation": self._operation(self.machine_operations.get(key)),
            }
            for key, machine in self.config.machines.items()
        }
        return {"machines": machines, "active": self.active}

    def _key_path(self, machine: MachineConfig) -> str | None:
        if machine.key_path is None:
            return None
        path, config_dir = machine.key_path.resolve(), self.config.config_dir
        return str(path.relative_to(config_dir) if path.is_relative_to(config_dir) else path)

    async def deployments_status(self) -> dict:
        keys = list(self.config.deployments)
        await asyncio.gather(*(self._observe_deployment(key) for key in keys))
        deployments = {}
        for key in keys:
            deployment = self.config.deployments[key]
            deployments[key] = {
                "name": deployment.name,
                "machines": list(deployment.machines),
                "head": deployment.head,
                "start_command": deployment.start_command,
                "stop_command": deployment.stop_command,
                "endpoint": str(deployment.endpoint),
                "observed_state": self._observed_state(key),
                **self.deployment_observations[key],
                "operation": self._operation(self.deployment_operations.get(key)),
            }
        return {"deployments": deployments, "active": self.active}

    async def _poll_machine(self, machine_id: str, reachable: bool, timeout: float) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            observation = await self._observe_machine(machine_id)
            if observation["reachable"] is reachable:
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                state = "reachable" if reachable else "unreachable"
                raise RuntimeError(f"machine did not become {state} before timeout")
            await asyncio.sleep(min(self.config.hub.timeouts.readiness_poll_seconds, remaining))

    async def _power_on(self, machine_id: str) -> None:
        await self.adapters.wake(machine_id)
        self.shutdown_requested[machine_id] = False
        await self._poll_machine(machine_id, True, self.config.hub.timeouts.machine_boot_seconds)

    async def _power_off(self, machine_id: str) -> None:
        await self.adapters.shutdown(machine_id)
        self.shutdown_requested[machine_id] = True
        await self._poll_machine(machine_id, False, self.config.hub.timeouts.machine_shutdown_seconds)

    async def _fail(self, message: str) -> None:
        raise RuntimeError(message)

    async def _wake(self, machine_id: str, deployment_id: str | None = None) -> None:
        observation = await self._observe_machine(machine_id)
        if observation["reachable"] and not observation["error"]:
            return
        operation = self.machine_operations[machine_id] = Operation("on")
        work = self._fail(observation["error"]) if observation["reachable"] else self._power_on(machine_id)
        if deployment_id and not observation["reachable"]:
            self._log(deployment_id, "hub", f"waking {machine_id}")
        if await self._run_operation(operation, work) and deployment_id:
            self._log(deployment_id, "hub", f"{machine_id} is up")

    async def _script(self, deployment_id: str, command: str, timeout: float) -> int:
        head = self.config.deployments[deployment_id].head
        self._log(deployment_id, "hub", f"running {command} on {head}")
        lines = deque(maxlen=4000)  # enough lines for the last 4,000 characters

        def on_line(source: str, text: str) -> None:
            lines.append(self._log(deployment_id, source, text))

        try:
            status = await self.adapters.stream(head, command, timeout=timeout, on_line=on_line)
        finally:
            self.deployment_operations[deployment_id].output = "\n".join(lines)[-4000:]
        self._log(deployment_id, "hub", f"exited {status}")
        return status

    async def _start(self, deployment_id: str, wake: bool = True) -> None:
        deployment = self.config.deployments[deployment_id]
        if wake:
            await asyncio.gather(*(self._wake(machine_id, deployment_id) for machine_id in deployment.machines))
        unavailable = []
        for machine_id in deployment.machines:
            observation = self.machine_observations[machine_id]
            if not observation["reachable"] or observation["error"]:
                unavailable.append(machine_id)
        if unavailable:
            raise RuntimeError("unavailable machines: " + ", ".join(unavailable))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.hub.timeouts.deployment_start_seconds
        status = await self._script(deployment_id, deployment.start_command, max(deadline - loop.time(), 0))
        if status:
            raise RuntimeError(f"start command exited {status}")
        self._log(deployment_id, "hub", f"waiting for {deployment.models_url}")
        while True:
            observation = await self._observe_deployment(deployment_id)
            if observation["ready"]:
                self._log(deployment_id, "hub", "ready")
                return
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise RuntimeError("endpoint did not answer 2xx before timeout; outcome unknown")
            await asyncio.sleep(min(self.config.hub.timeouts.readiness_poll_seconds, remaining))

    async def _stop(self, deployment_id: str) -> None:
        command = self.config.deployments[deployment_id].stop_command
        status = await self._script(deployment_id, command, self.config.hub.timeouts.deployment_stop_seconds)
        if status:
            raise RuntimeError(f"stop command exited {status}")

    def _launch(self, body) -> None:
        async def guarded():
            try:
                await body
            finally:
                self._release()

        self.task = asyncio.create_task(guarded())

    def _running_conflict(self, deployment_id: str) -> Conflict:
        name = self.config.deployments[deployment_id].name
        message = f"machine is running deployment {name}; stop it first"
        return Conflict("deployment_active", message, {"deployment": deployment_id})

    async def machine_action(self, machine_id: str, action: str) -> dict:
        members = [key for key, deployment in self.config.deployments.items() if machine_id in deployment.machines]
        if action == "off":
            for deployment_id in members:
                if self._in_progress(deployment_id):
                    raise self._running_conflict(deployment_id)
        self._reserve(action, machine_id, [machine_id])
        try:
            if action == "on":
                observation = await self._observe_machine(machine_id)
                if observation["reachable"]:
                    raise Conflict("machine_on", "machine is already on", {"machine": machine_id})
            else:
                for deployment_id in members:
                    if (await self._observe_deployment(deployment_id))["ready"]:
                        raise self._running_conflict(deployment_id)
            operation = self.machine_operations[machine_id] = Operation(action)
            work = self._power_on(machine_id) if action == "on" else self._power_off(machine_id)
            self._launch(self._run_operation(operation, work))
            return self._operation(operation)
        except BaseException:
            self._release()
            raise

    async def deployment_action(self, deployment_id: str, action: str) -> dict:
        deployment = self.config.deployments[deployment_id]
        self._reserve(action, deployment_id, deployment.machines)
        try:
            observation = await self._observe_deployment(deployment_id)
            if action == "start" and observation["ready"]:
                raise Conflict("deployment_active", "deployment is already active", {"deployment": deployment_id})
            operation = self._begin(deployment_id, action)
            work = self._start(deployment_id) if action == "start" else self._stop(deployment_id)
            self._launch(self._run_operation(operation, work))
            return self._operation(operation)
        except BaseException:
            self._release()
            raise

    async def schedule_wake(self) -> None:
        schedule = self.config.hub.schedule
        try:
            self._reserve("wake", "schedule", schedule.machines)
        except Conflict as conflict:
            log.info("scheduled wake skipped while %s is active", conflict.blocking["action"])
            return

        async def work():
            await asyncio.gather(*(self._wake(machine_id) for machine_id in schedule.machines))
            deployment_id = schedule.startup_deployment
            if not deployment_id:
                return
            if (await self._observe_deployment(deployment_id))["ready"]:
                return
            operation = self._begin(deployment_id, "start")
            await self._run_operation(operation, self._start(deployment_id, wake=False))

        self._launch(work())

    async def schedule_shutdown(self) -> None:
        await self.close()
        schedule = self.config.hub.schedule
        self._reserve("shutdown", "schedule", schedule.machines)

        async def work():
            for deployment_id, deployment in self.config.deployments.items():
                if not set(deployment.machines) & set(schedule.machines):
                    continue
                operation = self._begin(deployment_id, "stop")
                await self._run_operation(operation, self._stop(deployment_id))

            async def shut(machine_id: str):
                operation = self.machine_operations[machine_id] = Operation("off")
                await self._run_operation(operation, self._power_off(machine_id))

            await asyncio.gather(*(shut(machine_id) for machine_id in schedule.machines))

        self._launch(work())

    async def close(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
