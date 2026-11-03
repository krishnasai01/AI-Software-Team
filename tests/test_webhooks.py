import hashlib
import hmac

import pytest
from temporalio.exceptions import WorkflowAlreadyStartedError

from orchestrator.webhooks import router
from orchestrator.webhooks.app import valid_signature

REPO = "acme/shop"


def test_signature():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert valid_signature("s3cret", body, sig)
    assert not valid_signature("s3cret", body + b" ", sig)
    assert not valid_signature("other", body, sig)
    assert not valid_signature("s3cret", body, None)
    assert not valid_signature("", body, sig)  # an unset secret never accepts anything


class FakeHandle:
    def __init__(self, client, wf_id):
        self.client, self.wf_id = client, wf_id

    async def signal(self, sig, *args):
        self.client.signals.append((self.wf_id, sig.__name__, *args))

    async def cancel(self):
        self.client.cancelled.append(self.wf_id)


class FakeClient:
    def __init__(self):
        self.started, self.signals, self.cancelled = [], [], []

    async def start_workflow(self, wf, arg, id, task_queue, id_reuse_policy):
        if id in {s[1] for s in self.started}:
            raise WorkflowAlreadyStartedError(id, "x")
        self.started.append((wf.__qualname__, id, arg))

    def get_workflow_handle(self, wf_id):
        return FakeHandle(self, wf_id)


@pytest.fixture
def client(monkeypatch):
    from core.settings import get_settings

    async def product(repo):
        return {"repo": repo} if repo == REPO else None

    monkeypatch.setattr(router.state, "product_for_repo", product)
    monkeypatch.setenv("TRUSTED_LABELERS", '["krishna", "techlead-agent[bot]"]')
    get_settings.cache_clear()
    yield FakeClient()
    get_settings.cache_clear()


def issue_event(action, label, sender="krishna", labels=("agent-ready",), state="open", repo=REPO):
    return {
        "action": action,
        "label": {"name": label},
        "sender": {"login": sender},
        "repository": {"full_name": repo},
        "issue": {"number": 42, "state": state, "labels": [{"name": n} for n in labels]},
    }


def pr_event(action, ref="agent/issue-42", merged=False, draft=False):
    return {
        "action": action,
        "sender": {"login": "engineer-agent[bot]"},
        "repository": {"full_name": REPO},
        "pull_request": {
            "number": 7,
            "draft": draft,
            "merged": merged,
            "head": {"ref": ref, "sha": "abc123def4567890"},
        },
    }


async def test_trusted_label_starts_ticket_once(client):
    assert await router.route(client, "issues", issue_event("labeled", "agent-ready")) == "started ticket"
    assert await router.route(client, "issues", issue_event("labeled", "agent-ready")) == "ticket already running"
    assert client.started[0][:2] == ("TicketWorkflow.run", "ticket-acme-shop-42")


async def test_untrusted_label_is_ignored(client):
    out = await router.route(client, "issues", issue_event("labeled", "agent-ready", sender="random-user"))
    assert out.startswith("ignored") and not client.started


async def test_unknown_repo_is_ignored(client):
    out = await router.route(client, "issues", issue_event("labeled", "agent-ready", repo="evil/repo"))
    assert out.startswith("ignored") and not client.started


async def test_removing_needs_human_requeues(client):
    out = await router.route(client, "issues", issue_event("unlabeled", "needs-human", labels=("agent-ready",)))
    assert out == "started ticket"


async def test_needs_human_issue_does_not_start(client):
    ev = issue_event("labeled", "agent-ready", labels=("agent-ready", "needs-human"))
    assert (await router.route(client, "issues", ev)).startswith("ignored")


async def test_closed_issue_signals_instead_of_cancelling(client):
    assert await router.route(client, "issues", issue_event("closed", None)) == "signalled ticket closed"
    assert client.signals == [("ticket-acme-shop-42", "issue_closed_signal")]
    assert client.cancelled == []


async def test_pr_push_starts_review_keyed_by_commit(client):
    assert await router.route(client, "pull_request", pr_event("synchronize")) == "started review"
    name, wf_id, inp = client.started[0]
    assert name == "ReviewWorkflow.run" and wf_id == "review-acme-shop-7-abc123def456"
    assert inp.ticket_number == 42


async def test_human_pr_is_reviewed_without_a_ticket(client):
    await router.route(client, "pull_request", pr_event("opened", ref="feature/login"))
    assert client.started[0][2].ticket_number is None


async def test_draft_pr_is_not_reviewed(client):
    assert (await router.route(client, "pull_request", pr_event("opened", draft=True))).startswith("ignored")


async def test_merge_signals_ticket(client):
    await router.route(client, "pull_request", pr_event("closed", merged=True))
    assert client.signals == [("ticket-acme-shop-42", "pr_closed_signal", True)]
