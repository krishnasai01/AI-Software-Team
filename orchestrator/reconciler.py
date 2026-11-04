"""The reconciler: the only place ticket state advances.

Every few seconds it takes webhook nudges and reconciles the tickets they mention; every few
minutes it sweeps every product. Each ticket is reconciled the same way either way: snapshot,
`decide()`, apply. Run: `python -m orchestrator.reconciler`.
"""

import asyncio
import logging
from zoneinfo import ZoneInfo

from core import budget
from core.github import AGENT_READY, NEEDS_BREAKDOWN, agent_branch, issue_number_from_branch
from orchestrator import jobs, snapshot, state, sweeps
from orchestrator.actions import apply, notify
from orchestrator.context import Ctx, default_ctx
from orchestrator.decide import Action, decide
from orchestrator.digest import build_digest

log = logging.getLogger(__name__)


def ticket_from_event(event: str, payload: dict) -> tuple[str | None, int | None]:
    """Which (repo, ticket) a webhook is about; ticket None means "sweep the product"."""
    repo = (payload.get("repository") or {}).get("full_name")
    if event in ("issues", "issue_comment"):
        issue = payload.get("issue") or {}
        return repo, None if "pull_request" in issue else issue.get("number")
    branch = None
    if event in ("pull_request", "pull_request_review"):
        pr = payload.get("pull_request") or {}
        if (pr.get("head", {}).get("repo") or {}).get("full_name") == repo:
            branch = pr["head"]["ref"]
    elif event == "check_suite":
        branch = (payload.get("check_suite") or {}).get("head_branch")
    elif event == "check_run":
        branch = ((payload.get("check_run") or {}).get("check_suite") or {}).get("head_branch")
    elif event == "status":
        branch = next((b["name"] for b in payload.get("branches") or []), None)
    return repo, issue_number_from_branch(branch) if branch else None


class Reconciler:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.last_full = 0.0

    async def global_pause(self) -> str | None:
        if await budget.spent_today() >= await budget.ceiling():
            return "the team's daily spending ceiling is reached; work resumes tomorrow"
        return None

    async def reconcile_ticket(
        self, product: dict, number: int, issue: dict | None = None, open_pr=snapshot._LOOKUP, paused: str | None = None
    ) -> Action:
        snap = await snapshot.build(self.ctx, product, number, issue=issue, open_pr=open_pr, paused=paused)
        action = decide(snap)
        await apply(self.ctx, product, snap, action)
        log.info("%s#%s -> %s", product["repo"], number, action)
        return action

    async def sweep_product(self, product: dict) -> None:
        ctx, repo = self.ctx, product["repo"]
        paused = await self.global_pause()
        open_prs = await ctx.gh.list_prs(repo)
        by_branch = {p["head"]["ref"]: p for p in open_prs if (p["head"].get("repo") or {}).get("full_name") == repo}

        await sweeps.sweep_merges(ctx, product)
        await sweeps.sweep_reverts(ctx, product, open_prs)
        await sweeps.sweep_answers(ctx, product)
        await sweeps.sweep_epics(ctx, product)
        await sweeps.sweep_outsider_reviews(ctx, product, open_prs)

        issues = {
            i["number"]: i
            for label in (AGENT_READY, NEEDS_BREAKDOWN)
            for i in await ctx.gh.list_open_issues(repo, label)
        }
        numbers = set(issues) | set(await jobs.tickets_with_live_jobs(product["name"]))
        for number in sorted(numbers):
            try:
                await self.reconcile_ticket(
                    product,
                    number,
                    issue=issues.get(number),
                    open_pr=by_branch.get(agent_branch(number)),
                    paused=paused,
                )
            except Exception:  # noqa: BLE001 - one bad ticket must not stop the others
                log.exception("reconciling %s#%s failed", repo, number)

    async def handle_nudges(self) -> set[str]:
        """Reconcile tickets named in new webhooks. Returns products that need a full sweep."""
        sweep: set[str] = set()
        targets: set[tuple[str, int]] = set()
        for ev in await state.take_events():
            repo, number = ticket_from_event(ev["event"], ev["payload"])
            product = await state.product_for_repo(repo) if repo else None
            if product is None:
                continue
            if number is None:
                sweep.add(product["name"])
            else:
                targets.add((product["name"], number))
        paused = await self.global_pause() if targets else None
        for name, number in sorted(targets):
            if name in sweep:
                continue
            try:
                await self.reconcile_ticket(await state.product(name), number, paused=paused)
            except Exception:  # noqa: BLE001
                log.exception("reconciling %s#%s failed", name, number)
        return sweep

    async def daily(self) -> None:
        """Once a day after the digest hour: digest, promotions, proxy budgets."""
        s = self.ctx.settings
        local = self.ctx.now().astimezone(ZoneInfo(s.timezone))
        if local.hour < s.digest_hour or await state.control("daily_done") == str(local.date()):
            return
        await state.set_control("daily_done", str(local.date()), "reconciler")
        for product in await state.enabled_products():
            await sweeps.propose_promotions(self.ctx, product)
        try:
            await budget.sync_proxy_teams()
        except Exception:  # noqa: BLE001
            log.exception("syncing LiteLLM team budgets failed")
        await notify(self.ctx, await build_digest())

    async def tick(self, full: bool) -> None:
        for row in await jobs.requeue_expired():
            log.warning("job %s lease expired -> %s", row["id"], row["status"])
        sweep = await self.handle_nudges()
        products = await state.enabled_products()
        for product in products:
            if full or product["name"] in sweep:
                try:
                    await self.sweep_product(product)
                except Exception:  # noqa: BLE001
                    log.exception("sweeping %s failed", product["name"])
        if full:
            await sweeps.watchdog(self.ctx)
            await self.daily()

    async def run_forever(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            full = loop.time() - self.last_full >= self.ctx.settings.full_sweep_s
            if full:
                self.last_full = loop.time()
            try:
                await self.tick(full)
            except Exception:  # noqa: BLE001 - the loop itself must never die
                log.exception("reconciler tick failed")
            await asyncio.sleep(self.ctx.settings.reconcile_tick_s)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await Reconciler(default_ctx()).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
