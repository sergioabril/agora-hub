import hashlib
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id         TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at TEXT NOT NULL,
  disabled   INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS user_invites (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users,
  code_hash  TEXT NOT NULL UNIQUE,
  consumed   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_tokens (
  token_hash TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users,
  invite_id  TEXT NOT NULL UNIQUE REFERENCES user_invites,
  revoked    INTEGER NOT NULL DEFAULT 0);
"""


def now(minutes: int = 0) -> str:
    # One format everywhere, so times compare as text.
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class Accounts:
    def __init__(self, path: Path):
        path.touch(mode=0o600, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def _invite(self, user_id: str) -> dict:
        code = secrets.token_hex(16)
        expires_at = now(10)
        self.db.execute(
            "INSERT INTO user_invites (id, user_id, code_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, digest(code), now(), expires_at),
        )
        return {"id": user_id, "invite_code": code, "expires_at": expires_at}

    def add(self, name: str) -> dict:
        user_id = str(uuid.uuid4())
        with self.db:
            self.db.execute("INSERT INTO users (id, name, created_at) VALUES (?, ?, ?)", (user_id, name, now()))
            return self._invite(user_id)

    def get(self, user_id: str) -> dict | None:
        row = self.db.execute("SELECT id, name, disabled FROM users WHERE id = ?", (user_id,)).fetchone()
        return {"id": row[0], "name": row[1], "disabled": bool(row[2])} if row else None

    def users(self) -> list[dict]:
        rows = self.db.execute("SELECT id, name, disabled FROM users ORDER BY created_at, rowid")
        return [{"id": id_, "name": name, "disabled": bool(disabled)} for id_, name, disabled in rows]

    def disable(self, user_id: str, disabled: bool) -> dict:
        with self.db:
            self.db.execute("UPDATE users SET disabled = ? WHERE id = ?", (int(disabled), user_id))
        return self.get(user_id)

    def invite(self, user_id: str) -> dict:
        with self.db:
            return self._invite(user_id)

    def revoke(self, user_id: str) -> int:
        with self.db:
            revoked = self.db.execute(
                "UPDATE user_tokens SET revoked = 1 WHERE user_id = ? AND revoked = 0", (user_id,)
            ).rowcount
            self.db.execute("UPDATE user_invites SET consumed = 1 WHERE user_id = ? AND consumed = 0", (user_id,))
        return revoked

    def link(self, code: str) -> dict | None:
        with self.db:
            row = self.db.execute(
                "SELECT i.id, u.id, u.name FROM user_invites i JOIN users u ON u.id = i.user_id"
                " WHERE i.code_hash = ? AND i.consumed = 0 AND i.expires_at > ? AND u.disabled = 0",
                (digest(code.strip().lower()), now()),
            ).fetchone()
            if row is None:
                return None
            invite_id, user_id, name = row
            claimed = self.db.execute(
                "UPDATE user_invites SET consumed = 1 WHERE id = ? AND consumed = 0", (invite_id,)
            ).rowcount
            if claimed != 1:
                return None
            token = secrets.token_hex(32)
            self.db.execute(
                "INSERT INTO user_tokens (token_hash, user_id, invite_id) VALUES (?, ?, ?)",
                (digest(token), user_id, invite_id),
            )
        return {"token": token, "user": {"id": user_id, "name": name}}

    def user(self, token: str) -> dict | None:
        row = self.db.execute(
            "SELECT u.id, u.name FROM user_tokens t JOIN users u ON u.id = t.user_id"
            " WHERE t.token_hash = ? AND t.revoked = 0 AND u.disabled = 0",
            (digest(token),),
        ).fetchone()
        return {"id": row[0], "name": row[1]} if row else None
