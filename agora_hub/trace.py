import json
from datetime import datetime, timezone


def _short(value, key=None):
    if isinstance(value, dict):
        return {k: _short(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_short(v) for v in value]
    if isinstance(value, str):
        if value.startswith("data:") and ";base64," in value:
            return f"{value[:value.index(',') + 1]}<{len(value)} chars>"
        if key in ("authorization", "cookie", "set-cookie"):
            return "<redacted>"
        if key in ("content", "arguments", "text") and len(value) > 2000:
            return value[:2000] + f"… <{len(value)} chars>"
        if key in ("result", "b64_json") and len(value) > 100:
            return f"<{len(value)} chars>"
    return value


def trace(request_id: str | None, event: str, **fields):
    """Prints one JSON line to stdout: the conversation log."""
    time = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    line = {"time": time, "request": request_id, "event": event, **_short(fields)}
    print(json.dumps(line, ensure_ascii=False), flush=True)
