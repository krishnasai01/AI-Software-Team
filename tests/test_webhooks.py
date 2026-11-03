import hashlib
import hmac

import pytest

from orchestrator.reconciler import ticket_from_event
from orchestrator.webhooks.app import HANDLED_EVENTS, valid_signature


def test_signature():
    body = b'{"a":1}'
    sig = "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert valid_signature("s3cret", body, sig)
    assert not valid_signature("s3cret", body + b" ", sig)
    assert not valid_signature("other", body, sig)
    assert not valid_signature("s3cret", body, None)
    assert not valid_signature("", body, sig)  # an unset secret never accepts anything


REPO = {"full_name": "acme/shop"}


def pr(ref, repo="acme/shop"):
    return {"head": {"ref": ref, "repo": {"full_name": repo}}}


@pytest.mark.parametrize(
    "event, payload, expected",
    [
        ("issues", {"repository": REPO, "issue": {"number": 4}}, ("acme/shop", 4)),
        ("issue_comment", {"repository": REPO, "issue": {"number": 4}}, ("acme/shop", 4)),
        ("issue_comment", {"repository": REPO, "issue": {"number": 9, "pull_request": {}}}, ("acme/shop", None)),
        ("pull_request", {"repository": REPO, "pull_request": pr("agent/issue-42")}, ("acme/shop", 42)),
        ("pull_request_review", {"repository": REPO, "pull_request": pr("agent/issue-42")}, ("acme/shop", 42)),
        # A fork can name its branch anything; it never maps to a ticket.
        ("pull_request", {"repository": REPO, "pull_request": pr("agent/issue-42", "evil/shop")}, ("acme/shop", None)),
        ("pull_request", {"repository": REPO, "pull_request": pr("feature/x")}, ("acme/shop", None)),
        ("check_suite", {"repository": REPO, "check_suite": {"head_branch": "agent/issue-7"}}, ("acme/shop", 7)),
        (
            "check_run",
            {"repository": REPO, "check_run": {"check_suite": {"head_branch": "agent/issue-7"}}},
            ("acme/shop", 7),
        ),
        ("status", {"repository": REPO, "branches": [{"name": "agent/issue-7"}]}, ("acme/shop", 7)),
    ],
)
def test_events_map_to_tickets(event, payload, expected):
    assert event in HANDLED_EVENTS
    assert ticket_from_event(event, payload) == expected
