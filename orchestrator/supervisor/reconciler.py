"""ReconcilerWorkflow: every 5 minutes, compare GitHub, claims and running workflows.

- Claims whose workflow is gone are released, so the ticket is picked up again.
- `agent-ready` issues with no running workflow are started (catches missed webhooks).
- Agent PRs whose head commit has no review get a review.
- Inbox events that failed to route are replayed.
"""

from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.common import RetryPolicy
from temporalio.service import RPCError

with workflow.unsafe.imports_passed_through():
    from core.chat import get_chat
    from core.tickets import AGENT_READY, NEEDS_HUMAN, TicketRef, Tickets, issue_number_from_branch
    from orchestrator import state
    from orchestrator.types import ticket_workflow_id
    from orchestrator.webhooks.router import route, start_review, start_ticket


class ReconcileActivities:
    def __init__(self, client: Client):
        self.client = client

    async def _running(self, workflow_id: str) -> bool:
        try:
            desc = await self.client.get_workflow_handle(workflow_id).describe()
        except RPCError:
            return False
        return desc.status == WorkflowExecutionStatus.RUNNING

    @activity.defn
    async def reconcile(self) -> dict:
        stats = {"leases_released": 0, "tickets_started": 0, "reviews_started": 0, "events_replayed": 0}

        # Any live claim whose workflow is gone: a lapsed lease after a crash, or a parked claim left
        # behind by a workflow that was terminated by hand. Running workflows own their claims, and
        # Temporal's heartbeat timeout re-runs a dead agent on another worker.
        for c in await state.live_claims():
            if await self._running(c["workflow_id"]):
                continue
            await state.release(c["ticket"], c["workflow_id"], status="expired")
            await state.record_incident(
                "orphaned_claim", "released claim; ticket returns to Ready", agent=c["agent"], ticket=c["ticket"]
            )
            stats["leases_released"] += 1

        tickets = Tickets("orchestrator")
        for product in await state.enabled_products():
            repo = product["repo"]
            for issue in await tickets.list_open_issues(repo, AGENT_READY):
                labels = {lbl["name"] for lbl in issue["labels"]}
                if NEEDS_HUMAN in labels or await self._running(ticket_workflow_id(repo, issue["number"])):
                    continue
                # Re-read right before starting: a workflow may have just finished and moved the label.
                fresh = await tickets.get_issue(TicketRef(repo, issue["number"]))
                fresh_labels = {lbl["name"] for lbl in fresh["labels"]}
                if fresh["state"] == "open" and AGENT_READY in fresh_labels and NEEDS_HUMAN not in fresh_labels:
                    stats["tickets_started"] += await start_ticket(self.client, repo, issue["number"])

            for pr in await tickets.list_open_prs(repo):
                number = issue_number_from_branch(pr["head"]["ref"])
                if number is None or pr.get("draft"):
                    continue
                sha = pr["head"]["sha"]
                reviews = await tickets.pr_reviews(repo, pr["number"])
                if any(r.get("commit_id") == sha and r["user"]["type"] == "Bot" for r in reviews):
                    continue
                stats["reviews_started"] += await start_review(self.client, repo, pr["number"], sha, number)

        for ev in await state.unprocessed_events():
            try:
                await route(self.client, ev["event"], ev["payload"])
                await state.mark_event(ev["delivery_id"])
                stats["events_replayed"] += 1
            except Exception as e:  # noqa: BLE001
                await state.mark_event(ev["delivery_id"], error=f"{type(e).__name__}: {e}")

        if stats["leases_released"]:
            await get_chat().post(f":warning: Reconciler released {stats['leases_released']} orphaned claim(s).")
        return stats


@workflow.defn
class ReconcilerWorkflow:
    @workflow.run
    async def run(self) -> dict:
        return await workflow.execute_activity(
            "reconcile",
            start_to_close_timeout=timedelta(minutes=4),
            retry_policy=RetryPolicy(maximum_attempts=2),
            result_type=dict,
        )
