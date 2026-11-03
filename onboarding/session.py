"""Where an onboarding stands, so that running it again resumes. Holds no secrets.

Progress itself is never stored: the engine re-detects every step each time. The session keeps only
what cannot be detected: the operator's answers, the targets they added, what they confirmed or
skipped, and a log of what ran.
"""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from onboarding.playbook import Target

LOG_LIMIT = 300


def default_dir(playbook: str) -> Path:
    return Path.home() / ".agent-company" / "onboarding" / playbook


class SessionError(Exception):
    pass


class Session:
    def __init__(self, path: Path | None, playbook: str):
        self.path = path
        self.playbook = playbook
        self.answers: dict[str, str] = {}
        self.targets: dict[str, dict[str, dict[str, str]]] = {}  # kind -> name -> values
        self.acks: set[str] = set()
        self.skipped: set[str] = set()
        self.log: list[dict] = []

    @classmethod
    def load(cls, path: Path | None, playbook: str) -> "Session":
        session = cls(path, playbook)
        if path is not None and path.exists():
            try:
                data = json.loads(path.read_text())
            except json.JSONDecodeError as e:
                raise SessionError(f"{path} is damaged ({e}). Move it aside to start over.") from e
            if data.get("playbook") != playbook:
                raise SessionError(f"{path} belongs to the {data.get('playbook')!r} playbook")
            session.answers = data.get("answers", {})
            session.targets = data.get("targets", {})
            session.acks = set(data.get("acks", []))
            session.skipped = set(data.get("skipped", []))
            session.log = data.get("log", [])
        return session

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        data = {
            "playbook": self.playbook,
            "answers": self.answers,
            "targets": self.targets,
            "acks": sorted(self.acks),
            "skipped": sorted(self.skipped),
            "log": self.log[-LOG_LIMIT:],
        }
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
        tmp.replace(self.path)

    def record(self, event: str, **fields: str) -> None:
        self.log.append({"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event, **fields})
        self.save()

    def all_targets(self) -> list[Target]:
        return [Target(kind, name, values) for kind, named in self.targets.items() for name, values in named.items()]

    def target_names(self) -> set[str]:
        return {name for named in self.targets.values() for name in named}
