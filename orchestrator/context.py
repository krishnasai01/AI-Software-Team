"""What the reconciler and worker need from the outside world, injectable for tests."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from core.chat import Chat, get_chat
from core.settings import Settings, get_settings


@dataclass
class Ctx:
    gh: Any  # core.github.GitHub("orchestrator"), or a fake in tests
    chat: Chat
    settings: Settings
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    gh_for: Callable[[str], Any] | None = None  # role -> client (engineer opens PRs, reviewer reviews)
    cache: dict = field(default_factory=dict)

    def as_role(self, role: str) -> Any:
        return self.gh_for(role) if self.gh_for else self.gh


def default_ctx() -> Ctx:
    from core.github import GitHub

    clients: dict[str, GitHub] = {}

    def gh_for(role: str) -> GitHub:
        if role not in clients:
            clients[role] = GitHub(role)
        return clients[role]

    return Ctx(gh=gh_for("orchestrator"), chat=get_chat(), settings=get_settings(), gh_for=gh_for)
