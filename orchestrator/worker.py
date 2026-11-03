"""Job runner on the worker server: `python -m orchestrator.worker`.

For each job: check the budget, prepare the repo bundle in a clean workspace, run the sandbox
(heartbeating the job's lease), then verify what came back *outside* the sandbox and act on it:
push and open the PR, post the review, or create sub-tickets and questions. The sandbox never
holds a GitHub token; the worker never runs agent code.
"""

import asyncio
import json
import logging
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path

from agents.runner import load_role
from core import budget
from core.github import AGENT_READY, EPIC, NEEDS_BREAKDOWN, SPLIT, GitHubPermissionError, agent_branch, app_for
from core.gitops import GitWorkspace, RejectedResult
from core.model import LiteLLM
from core.sandbox import DockerSandbox, Sandbox, SandboxSpec
from orchestrator import jobs, state
from orchestrator.actions import ask_human, notify
from orchestrator.context import Ctx, default_ctx
from orchestrator.snapshot import CREATED_BY_US

log = logging.getLogger(__name__)

MAX_RESULT_BYTES = 5 * 1024 * 1024
CAP_SUBTYPES = {"error_max_turns", "error_max_budget_usd"}
CRASH_SUBTYPES = {"runner_error", "no_result", "error_during_execution"}
# The App whose token the worker uses to fetch (and, for the engineer, push) the repo.
GIT_IDENTITY = {"engineer": "engineer", "reviewer": "reviewer", "techlead": "orchestrator"}


class JobContext:
    """Everything about one job that the worker learns while preparing it."""

    def __init__(self, job: jobs.Job, product: dict, workspace: GitWorkspace, base_sha: str, work_sha: str):
        self.job, self.product, self.ws = job, product, workspace
        self.base_sha, self.work_sha = base_sha, work_sha


class Worker:
    def __init__(self, ctx: Ctx, sandbox: Sandbox | None = None, proxy: LiteLLM | None = None, git_token=None):
        self.ctx = ctx
        self.sandbox = sandbox or DockerSandbox()
        self.proxy = proxy or LiteLLM()
        self.worker_id = ctx.settings.worker_id
        self._git_token = git_token  # tests pass a stub; production mints App installation tokens

    async def git_token(self, kind: str, repo: str) -> str | None:
        if self._git_token is not None:
            return self._git_token(kind, repo)
        perms = {"contents": "write"} if kind == "engineer" else {"contents": "read"}
        return await app_for(GIT_IDENTITY[kind]).token(repo, perms)

    # Loop

    async def run_forever(self) -> None:
        if isinstance(self.sandbox, DockerSandbox):
            removed = await self.sandbox.cleanup_orphans()
            if removed:
                log.warning("removed %d sandboxes left over from a previous run", removed)
        try:
            await budget.sync_proxy_teams()  # runs are minted inside their role's budgeted team
        except Exception:  # noqa: BLE001 - the reconciler retries daily; the global proxy ceiling still applies
            log.exception("syncing LiteLLM team budgets failed")
        slots = asyncio.Semaphore(self.ctx.settings.max_sandboxes)
        while True:
            await slots.acquire()
            job = await jobs.claim(self.worker_id)
            if job is None:
                slots.release()
                await asyncio.sleep(2)
                continue
            asyncio.create_task(self._guarded(job, slots))

    async def _guarded(self, job: jobs.Job, slots: asyncio.Semaphore) -> None:
        try:
            await self.run_job(job)
        except GitHubPermissionError as e:
            await jobs.fail(job, self.worker_id, f"GitHub permission error: {e}")
        except Exception as e:  # noqa: BLE001 - a crash is retried with backoff
            log.exception("job %s crashed", job.id)
            await jobs.retry_or_fail(job, self.worker_id, f"{type(e).__name__}: {e}")
        finally:
            slots.release()

    # One job

    async def run_job(self, job: jobs.Job) -> None:
        product = await state.product(job.product)
        try:
            remaining = await budget.check(job.kind)
        except budget.BudgetExceeded as e:
            await jobs.defer(job, self.worker_id, timedelta(minutes=30), str(e))
            return
        config, _ = load_role(job.kind)
        per_run = float(min(config["max_budget_usd_per_run"], remaining))
        run_id = f"{job.kind}-{uuid.uuid4().hex[:16]}"

        with tempfile.TemporaryDirectory(prefix=f"job-{job.id}-") as tmp_name:
            tmp = Path(tmp_name)
            jc = await self.prepare(job, product, tmp)
            if jc is None:
                return
            task_dir, out_dir = tmp / "task", tmp / "out"
            task_dir.mkdir()
            jc.ws.input_bundle(task_dir / "repo.bundle", jc.base_sha, jc.work_sha)
            task = await self.build_task(jc, run_id, per_run)

            team = await state.q1("SELECT litellm_team_id FROM agents WHERE role = %s", (job.kind,))
            key = await self.proxy.issue_key(
                run_id,
                team["litellm_team_id"] if team else None,
                per_run,
                hours=config["run_timeout_minutes"] // 60 + 2,
            )
            task["model"] = {"base_url": self.ctx.settings.sandbox_model_base_url, "api_key": key}
            (task_dir / "task.json").write_text(json.dumps(task))
            trace = (
                f"{self.ctx.settings.langfuse_url.rstrip('/')}/trace/{run_id}"
                if self.ctx.settings.langfuse_url
                else None
            )
            await state.start_run(
                run_id, job.kind, f"{product['repo']}#{job.ticket}" if job.ticket else None, job.id, trace
            )
            spend = None
            try:
                res = await self.sandbox.run(
                    SandboxSpec(
                        image=product["sandbox_image"],
                        name=f"sbx-{job.id}-{uuid.uuid4().hex[:6]}",
                        task_dir=task_dir,
                        out_dir=out_dir,
                        timeout_s=config["run_timeout_minutes"] * 60,
                    ),
                    heartbeat=lambda: jobs.heartbeat(job.id, self.worker_id),
                )
            finally:
                try:
                    spend = await self.proxy.key_spend(key)
                finally:
                    await self.proxy.revoke(key)

            result = read_result(out_dir / "result.json")
            cost = max(spend or 0.0, float(result.get("cost_usd") or 0.0))
            subtype = "timeout" if res.timed_out else result.get("subtype", "no_result")
            await state.finish_run(
                run_id,
                subtype,
                result.get("turns") or 0,
                result.get("tokens_in") or 0,
                result.get("tokens_out") or 0,
                cost,
            )
            await budget.record(job.kind, cost)
            if res.lost_lease:
                log.warning("job %s lost its lease (cancelled or taken over); result discarded", job.id)
                return

            base = {"run_id": run_id, "cost_usd": cost, "denials": result.get("denials", [])[:20]}
            if res.timed_out or subtype in CAP_SUBTYPES:
                await jobs.succeed(job.id, self.worker_id, {**base, "outcome": "cap", "error": subtype})
                return
            if subtype in CRASH_SUBTYPES or not result:
                errors = "; ".join(result.get("errors") or []) or res.log_tail[-1500:] or subtype
                await jobs.retry_or_fail(job, self.worker_id, f"run {run_id} crashed: {errors}")
                return
            handler = {
                "engineer": self.finish_engineer,
                "reviewer": self.finish_reviewer,
                "techlead": self.finish_techlead,
            }[job.kind]
            outcome = await handler(jc, result, out_dir)
            await jobs.succeed(job.id, self.worker_id, {**base, **outcome})

    async def prepare(self, job: jobs.Job, product: dict, tmp: Path) -> JobContext | None:
        repo, base = product["repo"], product["default_branch"]
        ws = GitWorkspace(tmp / "repo", repo, token=await self.git_token(job.kind, repo))
        ws.init()
        base_sha = ws.fetch(f"refs/heads/{base}", "refs/remotes/base")
        work_sha = base_sha
        if job.kind == "reviewer" or (job.kind == "techlead" and job.mode == "arbitrate"):
            work_sha = ws.fetch(f"refs/pull/{job.pr}/head", "refs/remotes/pr")
            if job.head_sha and work_sha != job.head_sha:
                await jobs.succeed(job.id, self.worker_id, {"outcome": "stale", "error": "the PR moved on"})
                return None
        elif job.ticket and ws.remote_has(f"refs/heads/{agent_branch(job.ticket)}"):
            work_sha = ws.fetch(f"refs/heads/{agent_branch(job.ticket)}", "refs/remotes/work")
        return JobContext(job, product, ws, base_sha, work_sha)

    async def build_task(self, jc: JobContext, run_id: str, per_run: float) -> dict:
        job, product, gh = jc.job, jc.product, self.ctx.gh
        repo = product["repo"]
        if job.ticket:
            issue = await gh.get_issue(repo, job.ticket)
            ticket = {"number": issue["number"], "title": issue["title"], "body": issue.get("body") or ""}
        else:
            pr = await gh.get_pr(repo, job.pr)
            ticket = {"number": pr["number"], "title": pr["title"], "body": pr.get("body") or ""}
        context = dict(job.input)
        task = {
            "run_id": run_id,
            "role": job.kind,
            "mode": job.mode,
            "repo": repo,
            "ticket": ticket,
            "instructions": jc.ws.project_instructions(jc.base_sha),
            "context": context,
            "memories": await state.recent_memories(product["name"]),
            "budget_usd": per_run,
            "git_author": {"name": f"agent-company-{job.kind}[bot]", "email": f"{job.kind}@agents.noreply.local"},
        }
        reviews = (
            await state.q(
                """SELECT result, head_sha FROM jobs WHERE product = %s AND pr = %s AND kind = 'reviewer'
               AND status = 'succeeded' ORDER BY id DESC LIMIT 5""",
                (product["name"], job.pr),
            )
            if job.pr
            else []
        )
        if job.kind == "engineer" and job.mode == "rework" and reviews:
            r = reviews[0]["result"]
            lines = [r.get("summary", "")] + [f"- {c['path']}:{c['line']}: {c['body']}" for c in r.get("comments", [])]
            context["feedback"] = "\n".join(lines)
        if job.kind == "reviewer":
            pr = await gh.get_pr(repo, job.pr)
            ci = await gh.ci_state(repo, jc.work_sha)
            task["pr"] = {"number": job.pr, "body": pr.get("body") or ""}
            context["ci"] = ci.state + (f": failing {', '.join(ci.failed)}" if ci.failed else "")
        if job.kind == "techlead" and job.mode == "arbitrate":
            context["review_history"] = [
                {"verdict": r["result"].get("verdict"), "summary": r["result"].get("summary")} for r in reviews
            ]
            context["reason"] = "The engineer and reviewer disagree after several rounds."
        return task

    # Results: everything from the sandbox is untrusted and verified here

    async def finish_engineer(self, jc: JobContext, result: dict, out_dir: Path) -> dict:
        out = result.get("output") or {}
        questions = [str(q) for q in out.get("open_questions") or []][:10]
        common = {"summary": str(out.get("summary", ""))[:2000], "questions": questions}
        if out.get("status") == "blocked":
            return {**common, "outcome": "blocked"}
        bundle = out_dir / "result.bundle"
        if not result.get("committed") or not bundle.exists():
            return {**common, "outcome": "no_changes"}
        try:
            head = jc.ws.import_result(bundle, start_sha=jc.work_sha)
            changed = jc.ws.check_pushable(jc.base_sha, head)
        except RejectedResult as e:
            return {**common, "outcome": "rejected", "reason": str(e)}
        repo, number = jc.product["repo"], jc.job.ticket
        jc.ws.push(head, agent_branch(number))
        pr = await self.ctx.as_role("engineer").open_or_update_pr(
            repo,
            agent_branch(number),
            jc.product["default_branch"],
            str(out.get("pr_title") or f"Fix #{number}")[:200],
            pr_body(out, number, result),
        )
        return {
            **common,
            "outcome": "completed",
            "pushed": True,
            "head_sha": head,
            "pr": pr["number"],
            "changed_files": changed[:200],
        }

    async def finish_reviewer(self, jc: JobContext, result: dict, out_dir: Path) -> dict:
        out = result.get("output") or {}
        verdict = out.get("verdict")
        if verdict not in ("approve", "request_changes"):
            raise ValueError("reviewer returned no verdict")
        comments = [
            c
            for c in out.get("comments") or []
            if isinstance(c, dict) and {"path", "line", "body"} <= c.keys() and isinstance(c["line"], int)
        ]
        summary = str(out.get("summary", ""))[:6000]
        await self.ctx.as_role("reviewer").post_review(
            jc.product["repo"],
            jc.job.pr,
            jc.work_sha,
            "APPROVE" if verdict == "approve" else "REQUEST_CHANGES",
            summary,
            comments[:30],
        )
        if out.get("injection_suspected"):
            reason = f"the reviewer suspects a prompt injection in PR #{jc.job.pr}"
            await state.pause(jc.product["name"], reason, "reviewer")
            await state.record_incident(
                "security", reason, agent="reviewer", ticket=f"{jc.product['repo']}#{jc.job.ticket}"
            )
            await notify(
                self.ctx,
                f":octagonal_sign: {jc.product['name']} paused: {reason}. Look at it, then "
                f"`orchestrator.admin resume --product {jc.product['name']}`.",
            )
        return {
            "outcome": "completed",
            "verdict": verdict,
            "summary": summary,
            "comments": comments[:30],
            "risk": out.get("risk") if out.get("risk") in ("low", "medium", "high") else "high",
            "injection_suspected": bool(out.get("injection_suspected")),
            "pr": jc.job.pr,
        }

    async def finish_techlead(self, jc: JobContext, result: dict, out_dir: Path) -> dict:
        out = result.get("output") or {}
        job, product = jc.job, jc.product
        name, repo = product["name"], product["repo"]
        decision = out.get("decision")
        question = out.get("question") or {}
        if out.get("risk_flag") in ("product", "security") and decision != "ask_human":
            decision = "ask_human"
            question = {
                "question": question.get("question")
                or f"The tech lead flagged a {out['risk_flag']} decision: {out.get('guidance', '')}",
                "options": question.get("options") or [],
                "default": "",
                "reversible": False,
            }
        guidance = str(out.get("guidance", ""))[:4000]
        outcome = {"outcome": "completed", "decision": decision, "guidance": guidance, "question": question}

        if decision == "split" or job.mode == "breakdown":
            subs = [s for s in out.get("sub_tickets") or [] if isinstance(s, dict) and s.get("title")][:15]
            if not subs:
                decision, outcome["decision"] = "ask_human", "ask_human"
                question = {
                    "question": "The tech lead could not split this ticket into smaller ones. How should it be scoped?",
                    "options": [],
                    "default": "",
                    "reversible": False,
                }
                outcome["question"] = question
            else:
                parent = await state.ticket(name, job.ticket) or {}
                numbers = []
                for sub in subs:
                    n = await self.ctx.gh.create_issue(
                        repo, str(sub["title"])[:200], f"{sub.get('body', '')}\n\nPart of #{job.ticket}", [AGENT_READY]
                    )
                    await state.upsert_ticket(
                        name,
                        n,
                        labeled_by=CREATED_BY_US,
                        parent=job.ticket,
                        external_origin=bool(parent.get("external_origin")),
                    )
                    numbers.append(n)
                await self.ctx.gh.add_labels(repo, job.ticket, EPIC if job.mode == "breakdown" else SPLIT)
                if job.mode == "breakdown":
                    await self.ctx.gh.remove_label(repo, job.ticket, NEEDS_BREAKDOWN)
                await self.ctx.gh.comment(
                    repo, job.ticket, "Split into " + ", ".join(f"#{n}" for n in numbers) + f".\n\n{guidance}"
                )
                outcome.update(decision="split", sub_tickets=numbers)
                return outcome

        if decision == "ask_human":
            await ask_human(
                self.ctx,
                product,
                job.ticket,
                str(question.get("question") or guidance)[:2000],
                [str(o) for o in question.get("options") or []][:6],
                question.get("default") or None,
                bool(question.get("reversible")),
            )
        elif decision in ("answer", "side_with_reviewer", "side_with_engineer"):
            await state.add_memory(
                name,
                "decision",
                f"Tech lead on #{job.ticket} ({job.mode}): {guidance[:1000]}",
                "agent",
                job.ticket,
                result.get("run_id"),
            )
        else:
            raise ValueError(f"tech lead returned an unknown decision {decision!r}")
        return outcome


def read_result(path: Path) -> dict:
    if not path.exists() or path.stat().st_size > MAX_RESULT_BYTES:
        return {}
    try:
        data = json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def pr_body(out: dict, number: int, result: dict) -> str:
    body = str(out.get("pr_body") or out.get("summary") or "")[:20000]
    if f"#{number}" not in body:
        body += f"\n\nCloses #{number}"
    tests = "\n".join(
        f"- `{t.get('command', '')}`: {'pass' if t.get('passed') else 'FAIL'}"
        for t in out.get("tests_run") or []
        if isinstance(t, dict)
    )
    body += f"\n\n---\n**Tests run by the engineer**\n{tests or '- none reported'}"
    questions = "\n".join(f"- {q}" for q in out.get("open_questions") or [])
    if questions:
        body += f"\n\n**Open questions**\n{questions}"
    return body + f"\n\n<sub>Run `{result.get('run_id')}` · ${float(result.get('cost_usd') or 0):.2f}</sub>"


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    await Worker(default_ctx()).run_forever()


if __name__ == "__main__":
    asyncio.run(main())
