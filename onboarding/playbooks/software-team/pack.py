"""The software team's own probes and actions: GitHub Apps, sops secrets, the two stacks, rulesets, rehearsal.

Everything here is idempotent and says exactly what it will do before it does it (the engine shows
the preview and waits for the operator's yes). Secrets are only ever passed on stdin, never in argv.
"""

import json
import re
import shlex
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jwt

from core.github import AGENT_READY, POLICY_CHECK, AppAdmin, GitHubPermissionError, agent_branch
from onboarding.lib.github import problem, user_github
from onboarding.lib.hosts import where
from onboarding.registry import ActionResult, Context, Registry, Status, done, missing, partial
from onboarding.shell import LOCAL
from orchestrator.sweeps import config_problems

KEY_DIR = "/etc/agent-company/github-apps"
SECRETS = "infra/secrets"
RULESET_NAME = "agent-company"
BRAIN_BRANCH = "onboarding/agent-company-brain"
REHEARSAL_TITLE = "Rehearsal: add a line to docs/rehearsal.md"

EVENTS = ["issues", "issue_comment", "pull_request", "pull_request_review", "check_suite", "check_run", "status"]
ROLES = {  # least privilege per App: what docs/setup.md asks for
    "orchestrator": {
        "permissions": {
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
            "checks": "write",
            "actions": "write",
            "metadata": "read",
        },
        "events": EVENTS,
    },
    "engineer": {"permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"}, "events": []},
    "reviewer": {"permissions": {"contents": "read", "pull_requests": "write", "metadata": "read"}, "events": []},
}
LEVEL = {"read": 1, "write": 2, "admin": 3}

# What each stack's compose file interpolates (infra/*/docker-compose.yml). Only the worker gets the model key.
ENV_KEYS = {
    "control": [
        "PUBLIC_HOSTNAME",
        "PRIVATE_IP",
        "WORKER_PRIVATE_IP",
        "POSTGRES_PASSWORD",
        "GITHUB_WEBHOOK_SECRET",
        "GITHUB_APP_IDS",
        "TRUSTED_LABELERS",
        "HUMAN_APPROVERS",
        "LITELLM_MASTER_KEY",
        "SLACK_BOT_TOKEN",
        "SLACK_CHANNEL",
        "TEAM_DAILY_CEILING_USD",
        "TIMEZONE",
    ],
    "worker": [
        "PRIVATE_IP",
        "CONTROL_PRIVATE_IP",
        "POSTGRES_PASSWORD",
        "ANTHROPIC_API_KEY",
        "LITELLM_MASTER_KEY",
        "GITHUB_APP_IDS",
        "SLACK_BOT_TOKEN",
        "SLACK_CHANNEL",
        "SANDBOX_RUNTIME",
        "MAX_SANDBOXES",
        "WORKER_ID",
        "TEAM_DAILY_CEILING_USD",
        "TIMEZONE",
    ],
}

AGE_PUBLIC = re.compile(r"age1[a-z0-9]{58}")
AGE_SHOW = 'age-keygen -y "$HOME/.config/sops/age/keys.txt"'
AGE_MAKE = (
    'f="$HOME/.config/sops/age/keys.txt"; if [ ! -s "$f" ]; then mkdir -p "$(dirname "$f")"; '
    '(umask 077; age-keygen -o "$f" 2>/dev/null); fi; ' + AGE_SHOW
)
AGE_RECIPIENTS = re.compile(r"(?m)^(?P<ind>[ \t]*)age:[^\n]*(?:\n(?P=ind)[ \t]+[^\n]*)*")
SUDO = 'S=""; [ "$(id -u)" = 0 ] || S="sudo -n"; '
POSTGRES = (
    "cid=$(docker ps -q --filter label=com.docker.compose.project=agent-company-control "
    '--filter label=com.docker.compose.service=postgres | head -n1); test -n "$cid" || exit 3; '
)


def distinct(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def read_pem(path: str) -> str:
    return Path(path).expanduser().read_text()


def app_admin(ctx: Context, app_id: str | int, pem: str) -> AppAdmin:
    factory = ctx.env.services.get("app_admin")
    key = read_pem(pem)
    return factory(int(app_id), key) if factory else AppAdmin(int(app_id), key)


def parse_checks(text: str) -> list[str]:
    return list(dict.fromkeys(c.strip() for c in re.split(r"[,\n]", text or "") if c.strip()))


def update_sops_recipients(text: str, keys: list[str]) -> str:
    """Put `keys` in the age recipients of a .sops.yaml, keeping real keys that are already there."""
    match = AGE_RECIPIENTS.search(text)
    if match is None:
        raise ValueError(".sops.yaml has no `age:` recipients to update")
    merged = list(dict.fromkeys([*AGE_PUBLIC.findall(match.group(0)), *keys]))
    ind = match.group("ind")
    block = f"{ind}age: >-\n" + ",\n".join(f"{ind}  {key}" for key in merged)
    return text[: match.start()] + block + text[match.end() :]


def ruleset_payload(orchestrator_app_id: int, ci_checks: list[str]) -> dict:
    """The ruleset docs/setup.md asks for. orchestrator.sweeps.config_problems is the judge of it."""
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": True,
                    "require_code_owner_review": True,
                    "require_last_push_approval": True,
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "required_status_checks": [
                        {"context": POLICY_CHECK, "integration_id": orchestrator_app_id},
                        *({"context": c} for c in ci_checks),
                    ],
                },
            },
        ],
    }


def template_files(root: Path, login: str) -> dict[str, str]:
    """The product-brain template as files to commit, with the owner filled in. Its README is about the template."""
    base = root / "templates" / "product-brain"
    files = {}
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        relative = path.relative_to(base).as_posix()
        if relative != "README.md":
            files[relative] = path.read_text().replace("@REPLACE_WITH_YOUR_LOGIN", f"@{login}")
    return files


async def build_env(which: str, ctx: Context, app_ids: dict[str, int], bot: str) -> dict[str, str]:
    v = ctx.values
    login = ctx.value("operator_login")
    approvers = [login, *(a.strip() for a in v.get("extra_approvers", "").split(",") if a.strip())]
    env = {
        "PUBLIC_HOSTNAME": v.get("public_hostname", ""),
        "PRIVATE_IP": ctx.value("control_private_ip" if which == "control" else "worker_private_ip"),
        "CONTROL_PRIVATE_IP": ctx.value("control_private_ip"),
        "WORKER_PRIVATE_IP": ctx.value("worker_private_ip"),
        "POSTGRES_PASSWORD": await ctx.secret("postgres_password"),
        "GITHUB_WEBHOOK_SECRET": await ctx.secret("webhook_secret"),
        "GITHUB_APP_IDS": json.dumps(app_ids),
        "TRUSTED_LABELERS": json.dumps([login, bot]),
        "HUMAN_APPROVERS": json.dumps(approvers),
        "ANTHROPIC_API_KEY": await ctx.secret("anthropic_api_key") if which == "worker" else "",
        "LITELLM_MASTER_KEY": await ctx.secret("litellm_master_key"),
        "SLACK_BOT_TOKEN": ctx.secrets.get("slack_bot_token") or "",
        "SLACK_CHANNEL": v.get("slack_channel", ""),
        "SANDBOX_RUNTIME": v.get("sandbox_runtime", "runsc"),
        "MAX_SANDBOXES": v.get("max_sandboxes", "4"),
        "WORKER_ID": v.get("worker_id", "worker-1"),
        "TEAM_DAILY_CEILING_USD": "250",
        "TIMEZONE": v.get("timezone", "America/Chicago"),
    }
    out = {key: env[key] for key in ENV_KEYS[which]}
    for key, value in out.items():
        if "\n" in value:
            raise ValueError(f"{key} must be a single line")
    return out


def register(reg: Registry) -> None:
    # Servers

    def clone_preview(ctx: Context, hosts: list[str], dir: str, url: str, ref: str) -> str:
        on = ", ".join(where(h) for h in distinct(hosts))
        return f"On {on}: git clone --branch {ref} {url} {dir} (uses sudo -n to create {dir} if needed)."

    @reg.action("clone_platform", preview=clone_preview)
    async def clone_platform(ctx: Context, hosts: list[str], dir: str, url: str, ref: str) -> ActionResult:
        q = shlex.quote
        for host in distinct(hosts):
            script = (
                f"set -e; {SUDO}if [ -d {q(dir)}/.git ]; then echo already cloned; exit 0; fi; "
                f'$S mkdir -p {q(dir)}; $S chown "$(id -un)" {q(dir)}; git clone --branch {q(ref)} {q(url)} {q(dir)}'
            )
            result = await ctx.shell(host).script(script, timeout=300, on_line=ctx.say)
            if not result.ok:
                return ActionResult(False, f"clone failed on {where(host)}: {result.problem()}")
        return ActionResult(True, f"cloned {url} ({ref}) to {dir}")

    # Encryption keys and secrets

    @reg.probe("age_keys")
    async def age_keys(ctx: Context, hosts: list[str]) -> Status:
        found: dict[str, str] = {}
        problems = []
        for host in distinct(hosts):
            result = await ctx.shell(host).script(AGE_SHOW, timeout=30)
            key = result.out.strip().splitlines()[-1] if result.ok and result.out.strip() else ""
            if AGE_PUBLIC.fullmatch(key):
                found[host] = key
            else:
                problems.append(f"no age key on {where(host)}")
        config = ctx.root / ".sops.yaml"
        text = config.read_text() if config.exists() else ""
        if "REPLACE_WITH" in text:
            problems.append(".sops.yaml still has placeholder recipients")
        unlisted = [where(h) for h, k in found.items() if k not in text]
        if unlisted:
            problems.append(f".sops.yaml does not list the key of {', '.join(unlisted)}")
        if not problems:
            return done(f"{len(found)} age keys, all listed in .sops.yaml")
        return (partial if found else missing)("; ".join(problems))

    def age_preview(ctx: Context, hosts: list[str]) -> str:
        on = ", ".join(where(h) for h in distinct(hosts))
        return (
            f"On {on}: create an age key at ~/.config/sops/age/keys.txt where there is none "
            "(mode 0600; existing keys are never touched).\n"
            "Then add their public keys to the recipients in .sops.yaml here, keeping any real keys already listed. "
            "Commit that file afterwards."
        )

    @reg.action("setup_age_keys", preview=age_preview)
    async def setup_age_keys(ctx: Context, hosts: list[str]) -> ActionResult:
        keys = []
        for host in distinct(hosts):
            result = await ctx.shell(host).script(AGE_MAKE, timeout=60)
            key = result.out.strip().splitlines()[-1] if result.ok and result.out.strip() else ""
            if not AGE_PUBLIC.fullmatch(key):
                return ActionResult(False, f"could not create or read an age key on {where(host)}: {result.problem()}")
            keys.append(key)
        config = ctx.root / ".sops.yaml"
        config.write_text(update_sops_recipients(config.read_text(), keys))
        return ActionResult(True, f"{len(keys)} public keys are now recipients in .sops.yaml (commit it)")

    @reg.probe("sops_keys")
    async def sops_keys(ctx: Context, name: str) -> Status:
        path = ctx.root / SECRETS / f"{name}.enc.env"
        if not path.exists():
            return missing(f"{SECRETS}/{name}.enc.env does not exist yet")
        result = await ctx.shell(LOCAL).run(["sops", "--decrypt", "--output-type", "json", str(path)], timeout=60)
        if not result.ok:
            return missing(f"cannot decrypt {name}.enc.env here: {result.problem()}")
        try:
            have = set(json.loads(result.out))
        except json.JSONDecodeError:
            return missing(f"{name}.enc.env did not decrypt to a list of variables")
        absent = [k for k in ENV_KEYS[name] if k not in have]
        return (
            done(f"{name}.enc.env has all {len(have)} variables")
            if not absent
            else partial(f"{name}.enc.env lacks: {', '.join(absent)}")
        )

    def secrets_preview(ctx: Context, control_host: str, worker_host: str, deploy_dir: str) -> str:
        lines = [
            "Write and encrypt (sops, with the recipients in .sops.yaml) these files, then copy them to the servers:"
        ]
        for name, host in (("control", control_host), ("worker", worker_host)):
            lines.append(
                f"  {SECRETS}/{name}.enc.env -> {where(host)}:{deploy_dir}/{SECRETS}/  "
                f"({len(ENV_KEYS[name])} variables: {', '.join(ENV_KEYS[name])})"
            )
        lines.append(
            "Values come from your answers, the generated secrets and your App IDs. Plaintext never touches disk."
        )
        return "\n".join(lines)

    @reg.action("write_secrets", preview=secrets_preview)
    async def write_secrets(ctx: Context, control_host: str, worker_host: str, deploy_dir: str) -> ActionResult:
        app_ids = {role: int(ctx.value(f"{role}_app_id")) for role in ROLES}
        slug = (await app_admin(ctx, app_ids["orchestrator"], ctx.value("orchestrator_pem")).info())["slug"]
        for name, host in (("control", control_host), ("worker", worker_host)):
            env = await build_env(name, ctx, app_ids, f"{slug}[bot]")
            plain = "".join(f"{k}={v}\n" for k, v in env.items())
            encrypt = await ctx.shell(LOCAL).run(
                [
                    "sops",
                    "--config",
                    str(ctx.root / ".sops.yaml"),
                    "--encrypt",
                    "--input-type",
                    "dotenv",
                    "--output-type",
                    "dotenv",
                    "--filename-override",
                    str(ctx.root / SECRETS / f"{name}.enc.env"),
                    "/dev/stdin",
                ],
                stdin=plain,
                timeout=60,
            )
            if not encrypt.ok:
                return ActionResult(False, f"sops could not encrypt {name}.enc.env: {encrypt.problem()}")
            target = ctx.root / SECRETS / f"{name}.enc.env"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(encrypt.out)
            target.chmod(0o600)
            if host == LOCAL and Path(deploy_dir).resolve() == ctx.root.resolve():
                continue
            q = shlex.quote(deploy_dir)
            copy = await ctx.shell(host).script(
                f"mkdir -p {q}/{SECRETS} && cat > {q}/{SECRETS}/{name}.enc.env", stdin=encrypt.out, timeout=60
            )
            if not copy.ok:
                return ActionResult(False, f"could not copy {name}.enc.env to {where(host)}: {copy.problem()}")
        return ActionResult(True, "control.enc.env and worker.enc.env written, encrypted and copied")

    # GitHub Apps and the webhook

    @reg.probe("github_app")
    async def github_app(ctx: Context, role: str, app_id: str, pem: str) -> Status:
        try:
            info = await app_admin(ctx, app_id, pem).info()
        except OSError:
            return missing(f"cannot read the private key at {pem}")
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as e:
            return missing(f"GitHub did not accept App {app_id} with that key ({type(e).__name__})")
        want = ROLES[role]["permissions"]
        granted = info.get("permissions") or {}
        short = [f"{k}:{v}" for k, v in want.items() if LEVEL.get(granted.get(k), 0) < LEVEL[v]]
        too_much = [f"{k}:{v}" for k, v in granted.items() if LEVEL.get(v, 0) > LEVEL.get(want.get(k), 0)]
        no_events = [e for e in ROLES[role]["events"] if e not in (info.get("events") or [])]
        problems = []
        if short:
            problems.append("needs permissions " + ", ".join(short))
        if too_much:
            problems.append("has more access than its role allows (least privilege): " + ", ".join(too_much))
        if no_events:
            problems.append("is not subscribed to events " + ", ".join(no_events))
        if problems:
            return partial(
                f"{info['slug']}: " + "; ".join(problems) + f". Fix at github.com/settings/apps/{info['slug']}"
            )
        return done(f"{info['slug']} has the right permissions for {role}")

    @reg.probe("app_key_installed")
    async def app_key_installed(ctx: Context, role: str, hosts: list[str]) -> Status:
        absent = []
        for host in distinct(hosts):
            if not (await ctx.shell(host).run(["test", "-s", f"{KEY_DIR}/{role}.pem"], timeout=20)).ok:
                absent.append(where(host))
        return done(f"{role}.pem is installed") if not absent else missing(f"{role}.pem is not on {', '.join(absent)}")

    def key_preview(ctx: Context, role: str, pem: str, hosts: list[str]) -> str:
        return (
            f"Copy the {role} App's private key ({pem}) to {', '.join(where(h) for h in distinct(hosts))} as "
            f"{KEY_DIR}/{role}.pem, owned by root with mode 0400 (uses sudo -n). The key is sent over ssh, not printed."
        )

    @reg.action("install_app_key", preview=key_preview)
    async def install_app_key(ctx: Context, role: str, pem: str, hosts: list[str]) -> ActionResult:
        key = read_pem(pem)
        target = f"{KEY_DIR}/{role}.pem"
        script = f"set -e; {SUDO}$S install -d -m 0755 {KEY_DIR}; $S tee {target} >/dev/null; $S chmod 0400 {target}"
        for host in distinct(hosts):
            result = await ctx.shell(host).script(script, stdin=key, timeout=60)
            if not result.ok:
                return ActionResult(False, f"could not install the key on {where(host)}: {result.problem()}")
        return ActionResult(True, f"{role}.pem installed on {', '.join(where(h) for h in distinct(hosts))}")

    @reg.probe("webhook_config")
    async def webhook_config(ctx: Context, app_id: str, pem: str, url: str) -> Status:
        try:
            config = await app_admin(ctx, app_id, pem).hook_config()
        except OSError:
            return missing(f"cannot read the private key at {pem}")
        except (httpx.HTTPError, ValueError, jwt.PyJWTError) as e:
            return missing(f"could not read the App's webhook settings ({type(e).__name__})")
        if config.get("url") != url:
            return missing(f"the webhook URL is {config.get('url') or 'not set'}, it should be {url}")
        if config.get("content_type") != "json":
            return missing("the webhook content type must be application/json")
        return done(f"deliveries go to {url}")

    @reg.action(
        "set_webhook",
        preview=lambda ctx, app_id, pem, url: (
            f"Set the orchestrator App's webhook to {url} (JSON, with the generated secret) through GitHub's API."
        ),
    )
    async def set_webhook(ctx: Context, app_id: str, pem: str, url: str) -> ActionResult:
        try:
            await app_admin(ctx, app_id, pem).set_hook_config(url, await ctx.secret("webhook_secret"))
        except (httpx.HTTPError, GitHubPermissionError) as e:
            return ActionResult(False, f"GitHub refused: {problem(e)}")
        return ActionResult(True, f"webhook set to {url}")

    # The stacks

    def stacks_preview(ctx: Context, control_host: str, worker_host: str, dir: str) -> str:
        return (
            f"In {dir}, with sops decrypting each stack's secrets in memory:\n"
            f"  on {where(control_host)}: docker compose -f infra/control/docker-compose.yml up -d --build\n"
            f"  on {where(worker_host)}: docker compose -f infra/worker/docker-compose.yml up -d --build\n"
            "The first build takes several minutes."
        )

    @reg.action("compose_up", preview=stacks_preview)
    async def compose_up(ctx: Context, control_host: str, worker_host: str, dir: str) -> ActionResult:
        for name, host in (("control", control_host), ("worker", worker_host)):
            inner = f"docker compose -f infra/{name}/docker-compose.yml up -d --build"
            script = f"cd {shlex.quote(dir)} && sops exec-env {SECRETS}/{name}.enc.env {shlex.quote(inner)}"
            ctx.say(f"--- {name} stack on {where(host)}")
            result = await ctx.shell(host).script(script, timeout=1800, on_line=ctx.say)
            if not result.ok:
                return ActionResult(False, f"the {name} stack did not start on {where(host)}: {result.problem()}")
        return ActionResult(True, "both stacks started")

    async def query(ctx: Context, host: str, sql: str):
        return await ctx.shell(host).script(
            POSTGRES + f'docker exec "$cid" psql -U agent -d agent_company -tAc {shlex.quote(sql)}', timeout=60
        )

    @reg.probe("db_migrated")
    async def db_migrated(ctx: Context, host: str) -> Status:
        expected = len(list((ctx.root / "db" / "migrations").glob("*.sql")))
        result = await query(ctx, host, "select count(*) from schema_migrations")
        if not result.ok or not result.out.strip().isdigit():
            return missing(f"cannot read schema_migrations on {where(host)}: {result.problem()}")
        applied = int(result.out.strip())
        if applied >= expected:
            return done(f"{applied} migrations applied")
        return partial(f"{applied} of {expected} migrations applied")

    def migrate_preview(ctx: Context, host: str, dir: str) -> str:
        return f"On {where(host)}, in {dir}: run the migrate service of the control stack (python -m db.migrate)."

    @reg.action("run_migrations", preview=migrate_preview)
    async def run_migrations(ctx: Context, host: str, dir: str) -> ActionResult:
        inner = "docker compose -f infra/control/docker-compose.yml run --rm migrate"
        script = f"cd {shlex.quote(dir)} && sops exec-env {SECRETS}/control.enc.env {shlex.quote(inner)}"
        result = await ctx.shell(host).script(script, timeout=300, on_line=ctx.say)
        return ActionResult(result.ok, "migrations applied" if result.ok else f"failed: {result.problem()}")

    # Products

    @reg.probe("product_registered")
    async def product_registered(ctx: Context, host: str, name: str, repo: str, image: str) -> Status:
        if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            return missing(f"{name!r} is not a valid product name")
        result = await query(ctx, host, f"select repo || ' ' || sandbox_image from products where name = '{name}'")
        if not result.ok:
            return missing(f"cannot read the products table on {where(host)}: {result.problem()}")
        row = result.out.strip()
        if not row:
            return missing(f"{name} is not registered yet")
        if row != f"{repo} {image}":
            return partial(f"{name} is registered as {row!r}, not {repo} {image}")
        return done(f"{name} is registered (autonomy level 0 until promoted)")

    def register_preview(ctx: Context, host: str, dir: str, name: str, repo: str, image: str) -> str:
        return (
            f"On {where(host)}: orchestrator.admin add-product {name} {repo} {image}\n"
            "It starts at autonomy level 0 (shadow mode): you approve every merge until it earns more."
        )

    @reg.action("add_product", preview=register_preview)
    async def add_product(ctx: Context, host: str, dir: str, name: str, repo: str, image: str) -> ActionResult:
        admin = shlex.join(["python", "-m", "orchestrator.admin", "add-product", name, repo, image])
        inner = f"docker compose -f infra/control/docker-compose.yml run --rm migrate {admin}"
        script = f"cd {shlex.quote(dir)} && sops exec-env {SECRETS}/control.enc.env {shlex.quote(inner)}"
        result = await ctx.shell(host).script(script, timeout=300, on_line=ctx.say)
        return ActionResult(result.ok, f"{name} registered" if result.ok else f"failed: {result.problem()}")

    @reg.probe("apps_installed")
    async def apps_installed(ctx: Context, repo: str, apps: list[dict]) -> Status:
        absent = []
        for app in apps:
            try:
                admin = app_admin(ctx, app["app_id"], app["pem"])
                if not await admin.installed_on(repo):
                    absent.append(
                        f"{app['role']}: https://github.com/apps/{(await admin.info())['slug']}/installations/new"
                    )
            except (OSError, httpx.HTTPError, ValueError, jwt.PyJWTError) as e:
                absent.append(f"{app['role']}: could not check ({type(e).__name__})")
        if not absent:
            return done(f"all {len(apps)} Apps are installed on {repo}")
        return (partial if len(absent) < len(apps) else missing)(
            f"install on {repo} (all repositories or just this one): " + "; ".join(absent)
        )

    def brain_preview(ctx: Context, repo: str, login: str) -> str:
        names = ", ".join(template_files(ctx.root, login))
        return (
            f"Open a pull request on {repo} adding the product brain and merge policy ({names}), "
            f"with @{login} as the code owner. Files that already exist are left alone. Nothing merges until you do."
        )

    @reg.action("open_brain_pr", preview=brain_preview)
    async def open_brain_pr(ctx: Context, repo: str, login: str) -> ActionResult:
        gh = await user_github(ctx)
        try:
            base = (await gh.get_repo(repo))["default_branch"]
            for pr in await gh.list_prs(repo, "open"):
                if pr["head"]["ref"].startswith(BRAIN_BRANCH):
                    return ActionResult(True, f"a pull request is already open: {pr['html_url']}")
            files = template_files(ctx.root, login)
            new = {path: text for path, text in files.items() if await gh.file_at(repo, path, base) is None}
            if not new:
                return ActionResult(True, f"every template file already exists on {base}")
            branch = f"{BRAIN_BRANCH}-{datetime.now(UTC):%Y%m%d-%H%M%S}"
            await gh.put_files(repo, base, branch, new, "Add the agent-company product brain and merge policy")
            body = (
                "Adds what the agent team reads and obeys:\n\n"
                + "\n".join(f"- `{p}`" for p in new)
                + "\n\nBefore merging, review `.agent-company/policy.yaml` (what may ever merge without you) and "
                "`.github/CODEOWNERS` (only the sensitive paths, no catch-all), and fill in `docs/brain/*`."
            )
            pr = await gh.open_or_update_pr(
                repo, branch, base, "Add agent-company product brain and merge policy", body
            )
        except (httpx.HTTPStatusError, GitHubPermissionError) as e:
            return ActionResult(False, problem(e))
        return ActionResult(True, f"opened {pr['html_url']} with {len(new)} files. Review it, then merge it")

    @reg.probe("ruleset_ok")
    async def ruleset_ok(ctx: Context, repo: str, app_id: str) -> Status:
        gh = await user_github(ctx)
        try:
            branch = (await gh.get_repo(repo))["default_branch"]
            rules = await gh.branch_rules(repo, branch)
        except httpx.HTTPStatusError as e:
            return missing(problem(e))
        problems = config_problems(rules, int(app_id))
        if not problems:
            return done(f"{repo}'s {branch} branch is protected the way the platform needs")
        return (partial if rules else missing)("; ".join(problems))

    def ruleset_preview(ctx: Context, repo: str, app_id: str, ci_checks: str) -> str:
        checks = parse_checks(ci_checks)
        return (
            f"On {repo}, create (or replace) the ruleset {RULESET_NAME!r} on the default branch, no bypass list:\n"
            "  - pull requests required: 0 approvals, code-owner review, stale approvals dismissed, "
            "last push approved\n"
            f"  - required checks: {POLICY_CHECK} (pinned to your orchestrator App)"
            + "".join(f", {c}" for c in checks)
            + "\n  - branches must be up to date; force pushes and deletion blocked"
            + (
                ""
                if checks
                else "\nNote: no CI checks listed, so CI is not required by GitHub (the policy still wants it green)."
            )
        )

    @reg.action("create_ruleset", preview=ruleset_preview)
    async def create_ruleset(ctx: Context, repo: str, app_id: str, ci_checks: str) -> ActionResult:
        gh = await user_github(ctx)
        payload = ruleset_payload(int(app_id), parse_checks(ci_checks))
        try:
            existing = next((r for r in await gh.list_rulesets(repo) if r["name"] == RULESET_NAME), None)
            if existing:
                await gh.update_ruleset(repo, existing["id"], payload)
                return ActionResult(True, f"updated the {RULESET_NAME} ruleset on {repo}")
            await gh.create_ruleset(repo, payload)
        except (httpx.HTTPStatusError, GitHubPermissionError) as e:
            return ActionResult(False, f"GitHub refused the ruleset: {problem(e)}")
        return ActionResult(True, f"created the {RULESET_NAME} ruleset on {repo}")

    # Rehearsal

    @reg.probe("rehearsal")
    async def rehearsal(ctx: Context, repo: str) -> Status:
        gh = await user_github(ctx)
        issue = next((i for i in await gh.list_issues(repo) if i["title"] == REHEARSAL_TITLE), None)
        if issue is None:
            return missing("no rehearsal issue yet")
        number = issue["number"]
        owner = repo.split("/", 1)[0]
        prs = await gh.list_prs(repo, "all", head=f"{owner}:{agent_branch(number)}")
        if not prs:
            return partial(
                f"issue #{number} is waiting for the engineer: no pull request yet (are the stacks healthy?)"
            )
        pr = prs[0]
        if pr.get("merged_at"):
            return done(f"PR #{pr['number']} merged: the whole path works")
        if pr["state"] == "closed":
            return partial(f"PR #{pr['number']} was closed without merging")
        reviews = await gh.pr_reviews(repo, pr["number"])
        approved = any(
            r.get("state") == "APPROVED" and (r.get("user") or {}).get("login", "").endswith("[bot]") for r in reviews
        )
        check = await gh.check_run(repo, pr["head"]["sha"], POLICY_CHECK)
        parts = [
            f"PR #{pr['number']} opened by the engineer",
            "approved by the reviewer" if approved else "waiting for the reviewer",
            f"policy check {check['status']}" if check else "no policy check yet",
        ]
        advice = ". Approve and merge it yourself: new products start at level 0" if approved and check else ""
        return partial(", ".join(parts) + advice)

    @reg.action(
        "create_rehearsal_issue",
        preview=lambda ctx, repo: (
            f"Create the issue {REHEARSAL_TITLE!r} on {repo}, labeled {AGENT_READY}, as you. "
            "The scripted agent then opens a PR; no model calls, no cost."
        ),
    )
    async def create_rehearsal_issue(ctx: Context, repo: str) -> ActionResult:
        gh = await user_github(ctx)
        try:
            if any(i["title"] == REHEARSAL_TITLE for i in await gh.list_issues(repo)):
                return ActionResult(True, "the rehearsal issue already exists")
            number = await gh.create_issue(
                repo,
                REHEARSAL_TITLE,
                "Created by the onboarding assistant to rehearse the whole path with a scripted agent.",
                [AGENT_READY],
            )
        except (httpx.HTTPStatusError, GitHubPermissionError) as e:
            return ActionResult(False, problem(e))
        return ActionResult(True, f"created issue #{number}; watch for the engineer's pull request")
