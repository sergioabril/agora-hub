import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


log = logging.getLogger(__name__)


def target_at(schedule, now: datetime) -> str:
    local_time = now.astimezone(ZoneInfo(schedule.timezone)).time()
    wake = time.fromisoformat(schedule.wake_at)
    shutdown = time.fromisoformat(schedule.shutdown_at)
    if wake < shutdown:
        up = wake <= local_time < shutdown
    else:
        up = local_time >= wake or local_time < shutdown
    return "up" if up else "down"


def next_transition(schedule, now: datetime) -> tuple[datetime, str]:
    zone = ZoneInfo(schedule.timezone)
    today = now.astimezone(zone).date()
    transitions = ((schedule.wake_at, "up"), (schedule.shutdown_at, "down"))
    candidates = []
    for offset in range(3):
        day = today + timedelta(days=offset)
        for wall_time, target in transitions:
            local = datetime.combine(day, time.fromisoformat(wall_time), zone)
            instant = local.astimezone(timezone.utc)
            if instant > now:
                candidates.append((instant, target))
    return min(candidates, key=lambda candidate: candidate[0])


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _apply(controller, target: str) -> None:
    try:
        action = controller.schedule_wake if target == "up" else controller.schedule_shutdown
        await action()
    except Exception:
        log.exception("scheduled %s action failed", target)


async def run_schedule(config, controller, now=_utc_now, sleep=asyncio.sleep) -> None:
    schedule = config.hub.schedule
    if not schedule.enabled:
        await controller.machines_status()
        await controller.deployments_status()
        return
    current = now()
    await _apply(controller, target_at(schedule, current))
    while True:
        transition, target = next_transition(schedule, current)
        await sleep(max(0, (transition - now()).total_seconds()))
        current = transition
        await _apply(controller, target)
