import json
from datetime import datetime, timezone


def timestamp() -> str:
    """Now in UTC, to the millisecond: 2026-11-26T11:41:05.123Z."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def trace(request_id: str | None, event: str, **fields):
    """Prints one short JSON line to stdout: who called and which tools ran. Conversations live in sessions/."""
    line = {"time": timestamp(), "request": request_id, "event": event, **fields}
    print(json.dumps(line, ensure_ascii=False), flush=True)
