"""Secret values: kept apart from the session, and scrubbed from everything the model or a log sees."""

import json
import os
import secrets as _secrets
from pathlib import Path

MIN_REDACT = 6  # shorter values would blank out ordinary words


class SecretStore:
    """Persisted secrets live in a 0600 file; ephemeral ones (a GitHub token) only in memory."""

    def __init__(self, path: Path | None = None):
        self.path = path
        self._kept: dict[str, str] = {}
        self._memory: dict[str, str] = {}
        if path is not None and path.exists():
            self._kept = json.loads(path.read_text())

    def get(self, name: str) -> str | None:
        return self._memory.get(name) or self._kept.get(name)

    def has(self, name: str) -> bool:
        return bool(self.get(name))

    def names(self) -> list[str]:
        return sorted(set(self._kept) | set(self._memory))

    def set(self, name: str, value: str, persist: bool = True) -> None:
        if persist:
            self._kept[name] = value
            self._memory.pop(name, None)
            self._write()
        else:
            self._memory[name] = value

    def generate(self, name: str, persist: bool = True) -> str:
        value = _secrets.token_urlsafe(32)
        self.set(name, value, persist)
        return value

    def redact(self, text: str) -> str:
        values = {**self._kept, **self._memory}
        for name, value in sorted(values.items(), key=lambda kv: -len(kv[1])):
            if len(value) >= MIN_REDACT:
                text = text.replace(value, f"<hidden:{name}>")
        return text

    def wipe(self) -> None:
        self._kept.clear()
        if self.path is not None and self.path.exists():
            self.path.unlink()

    def _write(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self._kept, f)
        tmp.replace(self.path)
