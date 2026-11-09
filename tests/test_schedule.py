import asyncio
from datetime import datetime, timezone

import pytest

from agora_hub.adapters import AdapterError
from agora_hub.config import load_config
from agora_hub.controller import Controller
from agora_hub.schedule import next_transition, run_schedule, target_at
from tests.fakes import FakeAdapters


UTC = timezone.utc


class StopClock(Exception):
    pass


class Clock:
    def __init__(self, current, transitions):
        self.current = current
        self.transitions = transitions
        self.sleeps = 0

    def now(self):
        return self.current

    async def sleep(self, seconds):
        if self.sleeps == self.transitions:
            raise StopClock
        self.sleeps += 1
        self.current = self.current.fromtimestamp(self.current.timestamp() + seconds, UTC)


class RecordingController:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail

    async def schedule_wake(self):
        self.calls.append("up")
        if self.fail == "up":
            raise RuntimeError("wake failed")

    async def schedule_shutdown(self):
        self.calls.append("down")
        if self.fail == "down":
            raise RuntimeError("shutdown failed")

    async def machines_status(self):
        self.calls.append("machines_status")

    async def deployments_status(self):
        self.calls.append("deployments_status")


@pytest.fixture
def config(config_dir):
    value = load_config(config_dir)
    timeouts = value.hub.timeouts
    timeouts.machine_boot_seconds = 0.02
    timeouts.machine_shutdown_seconds = 0.02
    timeouts.deployment_start_seconds = 0.02
    timeouts.readiness_poll_seconds = 0.001
    return value


@pytest.mark.asyncio
async def test_daytime_restart_wakes_once_then_shuts_down(config):
    clock = Clock(datetime(2026, 1, 15, 10, tzinfo=UTC), 1)
    controller = RecordingController()
    with pytest.raises(StopClock):
        await run_schedule(config, controller, clock.now, clock.sleep)
    assert controller.calls == ["up", "down"]
    assert clock.current == datetime(2026, 1, 15, 22, tzinfo=UTC)


@pytest.mark.asyncio
async def test_nighttime_restart_shuts_down_once(config):
    clock = Clock(datetime(2026, 1, 15, 23, tzinfo=UTC), 0)
    controller = RecordingController()
    with pytest.raises(StopClock):
        await run_schedule(config, controller, clock.now, clock.sleep)
    assert controller.calls == ["down"]


@pytest.mark.asyncio
async def test_action_exception_does_not_stop_timer(config):
    clock = Clock(datetime(2026, 1, 15, 10, tzinfo=UTC), 1)
    controller = RecordingController(fail="up")
    with pytest.raises(StopClock):
        await run_schedule(config, controller, clock.now, clock.sleep)
    assert controller.calls == ["up", "down"]


def test_overnight_window(config):
    schedule = config.hub.schedule
    schedule.wake_at = "22:00"
    schedule.shutdown_at = "06:00"
    assert target_at(schedule, datetime(2026, 1, 15, 22, tzinfo=UTC)) == "up"
    assert target_at(schedule, datetime(2026, 1, 15, 11, tzinfo=UTC)) == "down"
    assert next_transition(schedule, datetime(2026, 1, 15, 22, tzinfo=UTC)) == (
        datetime(2026, 1, 16, 5, tzinfo=UTC), "down"
    )


@pytest.mark.asyncio
async def test_disabled_schedule_only_reads_status(config):
    config.hub.schedule.enabled = False
    adapters = FakeAdapters()
    controller = Controller(config, adapters)
    slept = False

    async def sleep(_):
        nonlocal slept
        slept = True

    await run_schedule(config, controller, lambda: datetime(2026, 1, 15, tzinfo=UTC), sleep)
    assert not slept
    assert {call[0] for call in adapters.calls} == {"observe", "deployment_status", "ready"}


@pytest.mark.asyncio
async def test_null_startup_deployment_only_wakes_machines(config):
    config.hub.schedule.startup_deployment = None
    adapters = FakeAdapters()
    controller = Controller(config, adapters)

    async def stop(_):
        raise StopClock

    with pytest.raises(StopClock):
        await run_schedule(config, controller, lambda: datetime(2026, 1, 15, 10, tzinfo=UTC), stop)
    await controller.task
    assert {call[1] for call in adapters.calls if call[0] == "wake"} == set(config.hub.schedule.machines)
    assert not [call for call in adapters.calls if call[0] == "run"]


@pytest.mark.parametrize(
    ("day", "wake_utc", "shutdown_utc"),
    [
        (29, datetime(2026, 3, 29, 1, 30, tzinfo=UTC), datetime(2026, 3, 29, 2, tzinfo=UTC)),
        (25, datetime(2026, 10, 25, 0, 30, tzinfo=UTC), datetime(2026, 10, 25, 2, 30, tzinfo=UTC)),
    ],
)
@pytest.mark.asyncio
async def test_dst_transitions_fire_once(config, day, wake_utc, shutdown_utc):
    schedule = config.hub.schedule
    schedule.wake_at = "02:30"
    schedule.shutdown_at = "04:00" if day == 29 else "03:30"
    clock = Clock(datetime(2026, 3 if day == 29 else 10, day, tzinfo=UTC), 2)
    controller = RecordingController()
    with pytest.raises(StopClock):
        await run_schedule(config, controller, clock.now, clock.sleep)
    assert controller.calls == ["down", "up", "down"]
    assert next_transition(schedule, clock.now()) > (shutdown_utc, "")
    first = next_transition(schedule, datetime(2026, 3 if day == 29 else 10, day, tzinfo=UTC))
    assert first == (wake_utc, "up")


@pytest.mark.parametrize("wake_fails", [False, True])
@pytest.mark.asyncio
async def test_action_settles_then_no_calls_or_reconciliation_until_transition(config, wake_fails):
    adapters = FakeAdapters()
    if wake_fails:
        adapters.failures[("wake", "spark-2")] = AdapterError("http", "plug failed")
    else:
        adapters.run_hooks[("spark-1", "bash start.sh")] = lambda: adapters.readiness.update(pro=True)
    controller = Controller(config, adapters)
    clock = Clock(datetime(2026, 1, 15, 10, tzinfo=UTC), 1)
    first_count = None

    async def sleep(seconds):
        nonlocal first_count
        if clock.sleeps == 0:
            await controller.task
            first_count = len(adapters.calls)
            adapters.reachable["spark-1"] = False
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert len(adapters.calls) == first_count
            assert adapters.reachable["spark-1"] is False
        await clock.sleep(seconds)

    with pytest.raises(StopClock):
        await run_schedule(config, controller, clock.now, sleep)
    await controller.task
    assert len(adapters.calls) > first_count
