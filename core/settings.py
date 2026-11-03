"""Process configuration, read from the environment (SOPS-decrypted on the servers)."""

import socket
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://agent:agent@localhost:5432/agent_company"

    # GitHub: one App per role. Keys are PEM files on the servers, never inside a sandbox.
    github_api_url: str = "https://api.github.com"
    github_git_url: str = "https://github.com/{repo}.git"  # tests point this at local bare repos
    github_webhook_secret: str = ""
    github_app_ids: dict[str, int] = Field(default_factory=dict)  # orchestrator, engineer, reviewer
    github_app_key_dir: Path = Path("/run/secrets/github-apps")  # <role>.pem
    # Logins allowed to make an issue agent-ready (you, plus any PM / tech lead bots).
    trusted_labelers: list[str] = Field(default_factory=list)
    # People whose approvals and /answer comments count. Usually just you.
    human_approvers: list[str] = Field(default_factory=list)

    # Model proxy. The master key mints per-run keys; it never leaves the servers.
    litellm_url: str = "http://localhost:4000"
    litellm_master_key: str = ""
    # How sandboxes reach the proxy: Anthropic pass-through, so Claude Code's requests arrive intact.
    sandbox_model_base_url: str = "http://litellm:4000/anthropic"
    langfuse_url: str = ""

    # Sandboxes (worker server)
    worker_id: str = Field(default_factory=socket.gethostname)
    max_sandboxes: int = 4
    sandbox_runtime: str = "runsc"  # gVisor; "runc" only for local development
    # "per-job": a fresh internal network per job with only the proxies attached. Or a network name.
    sandbox_network: str = "per-job"
    sandbox_proxy_containers: dict[str, str] = Field(
        default_factory=lambda: {"litellm": "litellm", "egress": "egress"}  # alias -> container
    )
    sandbox_egress_proxy: str = "http://egress:3128"
    sandbox_cpus: float = 2
    sandbox_memory: str = "4g"
    sandbox_disk_quota: str = ""  # e.g. "20G"; needs overlay2 on xfs with pquota
    max_bundle_mb: int = 200

    # Reconciler
    reconcile_tick_s: float = 5  # webhook nudges are handled within one tick
    full_sweep_s: float = 300  # safety-net sweep of every product
    ci_grace_minutes: int = 10  # CI checks may not exist yet right after a push
    ci_stuck_hours: int = 3
    question_default_hours: int = 24  # reversible questions only
    max_jobs_per_ticket: int = 12  # loop breaker: escalate after this many agent runs
    merge_watch_hours: int = 2  # how long main CI is watched after a merge

    # Chat and digest
    slack_bot_token: str = ""
    slack_channel: str = ""
    digest_hour: int = 8

    # Spend: team-wide hard ceiling in USD per day (raise with `admin set-ceiling`, never by an agent).
    team_daily_ceiling_usd: float = 250
    timezone: str = "America/Chicago"


@lru_cache
def get_settings() -> Settings:
    return Settings()
