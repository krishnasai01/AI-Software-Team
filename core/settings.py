"""Process configuration, read from the environment (SOPS-decrypted .env on the servers)."""

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agent:agent@localhost:5432/agent_company"
    temporal_address: str = "localhost:7233"
    temporal_namespace: str = "default"

    # GitHub: one App per role. Keys are PEM files on the server, never inside a sandbox.
    github_api_url: str = "https://api.github.com"
    github_webhook_secret: str = ""
    github_app_ids: dict[str, int] = Field(default_factory=dict)  # {"engineer": 123, ...}
    github_app_key_dir: Path = Path("/run/secrets/github-apps")  # <role>.pem
    # Logins allowed to make an issue agent-ready: you, plus the PM and tech lead bots.
    trusted_labelers: list[str] = Field(default_factory=list)

    # Model proxy. The master key mints per-run keys; it never leaves the worker server.
    litellm_url: str = "http://localhost:4000"
    litellm_master_key: str = ""
    # How sandboxes reach the proxy (on the sandbox network).
    sandbox_litellm_url: str = "http://litellm:4000"
    langfuse_url: str = ""

    # Sandboxes
    sandbox_runtime: str = "runsc"  # gVisor; "runc" only for local development
    sandbox_network: str = "sandbox-net"
    sandbox_egress_proxy: str = "http://egress:3128"
    sandbox_cpus: float = 2
    sandbox_memory: str = "4g"
    sandbox_timeout_s: int = 90 * 60

    # Chat
    slack_bot_token: str = ""
    slack_channel: str = ""

    # Team-wide ceiling in USD per day (raise to 500 in Phase 3).
    team_daily_ceiling_usd: float = 250
    timezone: str = "America/Chicago"


@lru_cache
def get_settings() -> Settings:
    return Settings()
