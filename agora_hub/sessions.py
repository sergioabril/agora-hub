"""One folder per chat: <config>/sessions/<user id>/<session id>/ holds session.json, conversation.jsonl and files/."""
import base64
import json
import os
import re
from pathlib import Path

from .tools import FileStore
from .trace import timestamp

DATA_URL = re.compile(r"data:image/[\w.+-]+;base64,(.*)", re.S)
IMAGE_ITEMS = {"image_generation_call": "ig_", "web_image_call": "wi_"}  # item type -> id prefix before the file stem


def owner(root: Path, session_id: str) -> str | None:
    """The id of the user whose folder holds this session, or None while nobody has it.
    session_id is a canonical UUID, so it holds no glob patterns."""
    return next((folder.parent.name for folder in root.glob(f"*/{session_id}")), None)


class Session:
    def __init__(self, root: Path, user_id: str, session_id: str, store: FileStore):
        self.id, self.user_id, self.store = session_id, user_id, store
        self.folder = root / user_id / session_id
        self.files = self.folder / "files"

    def log(self, request_id: str, kind: str, payload: dict):
        """Appends one line to conversation.jsonl and moves session.json's updated_at to its time."""
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if kind == "turn_started":
            payload = {**payload, "input": [self._item(item) for item in payload["input"]]}
        if kind == "response_item":
            payload = self._item(payload)
        if kind == "tool_output":
            for name in payload["files"]:
                self.link(name)
        time = timestamp()
        line = {"timestamp": time, "request": request_id, "type": kind, "payload": payload}
        with open(self.folder / "conversation.jsonl", "a", encoding="utf-8") as file:
            file.write(json.dumps(line, ensure_ascii=False) + "\n")
        header = self.folder / "session.json"
        created_at = json.loads(header.read_text())["created_at"] if header.is_file() else time
        tmp = self.folder / "session.json.tmp"
        tmp.write_text(json.dumps({"id": self.id, "user_id": self.user_id, "created_at": created_at, "updated_at": time}))
        os.replace(tmp, header)

    def turns(self, running: str | None = None) -> list[dict]:
        """The log read back turn by turn: what was sent, what came back and how it ended.
        A turn with no end line that isn't running was cut short by the hub stopping: it is interrupted."""
        path = self.folder / "conversation.jsonl"
        if not path.is_file():
            return []
        turns = []
        for text in path.read_text().splitlines():
            line = json.loads(text)
            payload = line["payload"]
            if line["type"] == "turn_started":
                turns.append({"id": line["request"], "started_at": line["timestamp"], "status": "interrupted",
                              "error": None, "input": payload["input"], "output": []})
            elif line["type"] == "response_item":
                turns[-1]["output"].append(payload)
            elif line["type"] == "turn_ended":
                turns[-1].update(status=payload["status"], error=payload["error"])
        if turns and turns[-1]["id"] == running:
            turns[-1]["status"] = "in_progress"
        return turns

    def history(self) -> list[dict]:
        """The chat so far, as the model's input items: every turn's input, then its output, photos restored."""
        items = []
        for turn in self.turns():
            items += turn["input"] + turn["output"]
        return [self._restored(item) for item in items]

    def _restored(self, item: dict) -> dict:
        """The item with each photo's file swapped back for the photo itself, as the client first sent it."""
        if not isinstance(item.get("content"), list):
            return item
        parts = []
        for part in item["content"]:
            if part.get("file") and self.store.path(part["file"]):
                part = {**part, "image_url": self.store.data_url(part["file"])}
            parts.append(part)
        return {**item, "content": parts}

    def _item(self, item: dict) -> dict:
        """The item with its type, and each base64 image swapped for null plus the name of its link in files/."""
        if "type" not in item:  # a bare {"role", "content"} is a message
            item = {"type": "message", **item}
        if item["type"] in IMAGE_ITEMS:
            stem = str(item.get("id") or "").removeprefix(IMAGE_ITEMS[item["type"]])
            return {**item, "result": None, "file": self.link(self.store.find(stem))}
        if isinstance(item.get("content"), list):
            return {**item, "content": [self._part(part) for part in item["content"]]}
        return item

    def _part(self, part: dict) -> dict:
        url = part.get("image_url") if part.get("type") == "input_image" else None
        match = DATA_URL.match(url) if isinstance(url, str) else None
        if not match:
            return part
        try:
            name = self.store.save(base64.b64decode(match[1], validate=True))
        except ValueError:
            name = None
        return {**part, "image_url": None, "file": self.link(name)}

    def link(self, name: str | None) -> str | None:
        """Links a file of the shared store into files/, relatively, so the link works in the container and on the host."""
        if not name or not self.store.path(name):
            return None
        self.files.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            (self.files / name).symlink_to(os.path.relpath(self.store.folder / name, self.files))
        except FileExistsError:
            pass  # linked by an earlier turn
        return name
