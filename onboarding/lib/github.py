"""Probes and an action for GitHub repos, acting as the operator (their own token).

The Apps do not exist yet while a team is being onboarded, so this uses the operator's token. It is
asked for with hidden input (or taken from `gh` or $GITHUB_TOKEN) and kept in memory only.
"""

import httpx

from core.github import GitHub, GitHubPermissionError, UserToken
from onboarding.registry import ActionResult, Context, Registry, Status, done, missing, partial


async def user_github(ctx: Context) -> GitHub:
    service = ctx.env.services.get("github")
    if service is not None:
        return service
    return GitHub("operator", app=UserToken(await ctx.secret("github_token")))


def problem(e: Exception) -> str:
    """What GitHub said, in a line: its own message is usually the useful part."""
    if isinstance(e, GitHubPermissionError):
        return str(e)[:300]
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 401:
            return "GitHub rejected the token"
        if code in (403, 404):
            return f"GitHub says {code}: the token lacks access, or the repo does not exist"
        return f"GitHub answered {code}: {e.response.text[:200]}"
    return f"{type(e).__name__}: {e}"


def register(reg: Registry) -> None:
    @reg.probe("repo_access")
    async def repo_access(ctx: Context, repo: str) -> Status:
        try:
            info = await (await user_github(ctx)).get_repo(repo)
        except httpx.HTTPStatusError as e:
            return missing(f"{repo}: {problem(e)}")
        if not (info.get("permissions") or {}).get("admin"):
            return partial(f"{repo}: your token can see it but is not an admin, and settings and rulesets need admin")
        return done(f"{repo} (default branch {info['default_branch']})")

    @reg.probe("repo_settings")
    async def repo_settings(ctx: Context, repo: str, want: dict) -> Status:
        info = await (await user_github(ctx)).get_repo(repo)
        wrong = [k for k, v in want.items() if info.get(k) != v]
        return done() if not wrong else missing(f"{repo} needs: " + ", ".join(f"{k}={want[k]}" for k in wrong))

    @reg.probe("file_on_branch")
    async def file_on_branch(ctx: Context, repo: str, path: str) -> Status:
        gh = await user_github(ctx)
        branch = (await gh.get_repo(repo))["default_branch"]
        if await gh.file_at(repo, path, branch) is None:
            return missing(f"{path} is not on {repo}'s {branch} branch")
        return done(f"{path} is on {branch}")

    def settings_preview(ctx: Context, repo: str, settings: dict) -> str:
        return f"Change settings on {repo}: " + ", ".join(f"{k} = {v}" for k, v in settings.items())

    @reg.action("set_repo_settings", preview=settings_preview)
    async def set_repo_settings(ctx: Context, repo: str, settings: dict) -> ActionResult:
        try:
            await (await user_github(ctx)).update_repo(repo, **settings)
        except (httpx.HTTPStatusError, GitHubPermissionError) as e:
            return ActionResult(False, problem(e))
        return ActionResult(True, f"updated {repo}")
