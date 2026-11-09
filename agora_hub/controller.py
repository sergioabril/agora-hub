import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .adapters import AdapterError, Adapters, redact
from .config import Config

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


class _ActionError(RuntimeError):
    def __init__(self, message: str, output: str = ""):
        self.output = output
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

    async def _run_operation(self, operation: Operation, coroutine) -> bool:
        try:
            output = await coroutine
            if output:
                operation.output = redact(str(output), self.config)[-4000:]
            operation.status = "succeeded"
            operation.error = None
            return True
        except asyncio.CancelledError:
            operation.status = "cancelled"
            raise
        except Exception as error:
            operation.status = "failed"
            operation.error = self._error(error)
            if output := getattr(error, "output", ""):
                operation.output = redact(output, self.config)[-4000:]
            return False
        finally:
            operation.finished_at = _now()

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
            "shutdown_requested": self.shutdown_requested[machine_id],
        }
        self.machine_observations[machine_id] = observation
        return observation

    async def _observe_deployment(self, deployment_id: str) -> dict:
        deployment = self.config.deployments[deployment_id]
        errors = []
        script_state = None
        calls = [self.adapters.ready(deployment_id)]
        if deployment.status_command:
            calls.insert(0, self.adapters.deployment_status(deployment_id))
        results = await asyncio.gather(*calls, return_exceptions=True)
        ready_result = results[-1]
        ready = not isinstance(ready_result, BaseException)
        if not ready:
            errors.append(self._error(ready_result))
        if deployment.status_command:
            status_result = results[0]
            if isinstance(status_result, BaseException):
                errors.insert(0, self._error(status_result))
                script_state = "unknown"
            else:
                script_state = status_result
        observed_state = script_state if script_state is not None else ("running" if ready else "unknown")
        discrepancy = script_state is not None and (script_state == "running") != ready
        observation = {
            "observed_state": observed_state,
            "script_state": script_state,
            "ready": ready,
            "discrepancy": discrepancy,
            "errors": errors,
            "checked_at": _now(),
        }
        self.deployment_observations[deployment_id] = observation
        return observation

    def _deployment_active(self, observation: dict) -> bool:
        return observation["script_state"] in ("starting", "running", "stopping") or observation["ready"]

    async def machines_status(self) -> dict:
        await asyncio.gather(*(self._observe_machine(key) for key in self.config.machines))
        machines = {
            key: {**self.machine_observations[key], "operation": self._operation(self.machine_operations.get(key))}
            for key in self.config.machines
        }
        return {"machines": machines, "active": self.active}

    async def deployments_status(self) -> dict:
        await asyncio.gather(*(self._observe_deployment(key) for key in self.config.deployments))
        deployments = {}
        for key, deployment in self.config.deployments.items():
            deployments[key] = {
                "name": deployment.name,
                "endpoint": str(deployment.endpoint),
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

    async def _wake(self, machine_id: str) -> None:
        observation = await self._observe_machine(machine_id)
        if observation["reachable"] and not observation["error"]:
            return
        operation = self.machine_operations[machine_id] = Operation("on")
        work = self._fail(observation["error"]) if observation["reachable"] else self._power_on(machine_id)
        await self._run_operation(operation, work)

    async def _start(self, deployment_id: str, wake: bool = True) -> None:
        deployment = self.config.deployments[deployment_id]
        if wake:
            await asyncio.gather(*(self._wake(machine_id) for machine_id in deployment.machines))
        unavailable = []
        for machine_id in deployment.machines:
            observation = self.machine_observations[machine_id]
            if not observation["reachable"] or observation["error"]:
                unavailable.append(machine_id)
        if unavailable:
            raise RuntimeError("unavailable machines: " + ", ".join(unavailable))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.config.hub.timeouts.deployment_start_seconds
        result = await self.adapters.run(
            deployment.head,
            deployment.start_command,
            cwd=deployment.working_directory,
            timeout=self.config.hub.timeouts.deployment_start_seconds,
        )
        output = result.stdout + result.stderr
        if result.exit_status:
            raise _ActionError(f"start command exited {result.exit_status}: {output}", output)
        while True:
            observation = await self._observe_deployment(deployment_id)
            if observation["ready"]:
                return output
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise _ActionError("deployment readiness timed out; outcome unknown", output)
            await asyncio.sleep(min(self.config.hub.timeouts.readiness_poll_seconds, remaining))

    async def _stop(self, deployment_id: str) -> str:
        deployment = self.config.deployments[deployment_id]
        result = await self.adapters.run(
            deployment.head,
            deployment.stop_command,
            cwd=deployment.working_directory,
            timeout=self.config.hub.timeouts.deployment_stop_seconds,
        )
        output = result.stdout + result.stderr
        if result.exit_status:
            raise _ActionError(f"stop command exited {result.exit_status}: {output}", output)
        return output

    def _launch(self, body) -> None:
        async def guarded():
            try:
                await body
            finally:
                self._release()

        self.task = asyncio.create_task(guarded())

    async def machine_action(self, machine_id: str, action: str) -> dict:
        self._reserve(action, machine_id, [machine_id])
        try:
            if action == "on":
                observation = await self._observe_machine(machine_id)
                if observation["reachable"]:
                    raise Conflict("machine_on", "machine is already on", {"machine": machine_id})
            else:
                for deployment_id, deployment in self.config.deployments.items():
                    if machine_id not in deployment.machines:
                        continue
                    observation = await self._observe_deployment(deployment_id)
                    if self._deployment_active(observation):
                        raise Conflict("deployment_active", "deployment must be stopped first", {"deployment": deployment_id})
                    stopped = not deployment.status_command and (
                        (operation := self.deployment_operations.get(deployment_id)) is not None
                        and operation.action == "stop" and operation.status == "succeeded"
                    )
                    if observation["observed_state"] == "unknown" and not stopped:
                        raise Conflict("deployment_state_unknown", "deployment state is unknown", {"deployment": deployment_id})
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
            if action == "start" and self._deployment_active(observation):
                raise Conflict("deployment_active", "deployment is already active", {"deployment": deployment_id})
            operation = self.deployment_operations[deployment_id] = Operation(action)
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
            observation = await self._observe_deployment(deployment_id)
            if self._deployment_active(observation):
                return
            operation = self.deployment_operations[deployment_id] = Operation("start")
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
                operation = self.deployment_operations[deployment_id] = Operation("stop")
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
