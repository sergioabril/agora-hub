import base64
import hashlib
import json
import os
import re
from pathlib import Path

FILES_PATH = "/files/"  # how the harness refers to a stored file in text; no route serves it
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
FILE_NAME = re.compile(r"^[0-9a-f]{32}\.(png|jpg|webp)$")


class FileStore:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)

    def save(self, data: bytes, stem: str | None = None) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            ext = "png"
        elif data.startswith(b"\xff\xd8\xff"):
            ext = "jpg"
        elif data[:4] == b"RIFF" and data[8:12] == b"WEBP":
            ext = "webp"
        else:
            raise ValueError("unsupported image type")
        name = f"{stem or hashlib.sha256(data).hexdigest()[:32]}.{ext}"
        if not (self.folder / name).exists():
            self._write(name, data)
        return name

    def _write(self, name: str, data: bytes):
        tmp = self.folder / f"{name}.tmp"
        with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as file:
            file.write(data)
        os.replace(tmp, self.folder / name)

    def save_call(self, stem: str, tool_call: dict):
        """Keeps the tool call that made the image <stem>, next to it, so a later request can replay it."""
        self._write(f"{stem}.json", json.dumps(tool_call).encode())

    def call(self, stem: str) -> dict | None:
        path = self.folder / f"{stem}.json"
        return json.loads(path.read_bytes()) if re.fullmatch("[0-9a-f]{32}", stem) and path.is_file() else None

    def path(self, name: str) -> Path | None:
        path = self.folder / name
        return path if FILE_NAME.match(name) and path.is_file() else None

    def find(self, stem: str) -> str | None:
        return next((f"{stem}.{ext}" for ext in MEDIA_TYPES if self.path(f"{stem}.{ext}")), None)

    def data_url(self, name: str) -> str:
        data = base64.b64encode(self.path(name).read_bytes()).decode()
        return f"data:{MEDIA_TYPES[name.split('.')[1]]};base64,{data}"

    def save_metadata(self, stem: str, metadata: dict):
        if not re.fullmatch("[0-9a-f]{32}", stem):
            raise ValueError("invalid file stem")
        self._write(f"{stem}.metadata.json", json.dumps(metadata).encode())

    def metadata(self, stem: str) -> dict | None:
        if not re.fullmatch("[0-9a-f]{32}", stem):
            return None
        path = self.folder / f"{stem}.metadata.json"
        return json.loads(path.read_bytes()) if path.is_file() else None
