"""An in-memory GitHub with the same interface as core.github.GitHub, plus a recording chat."""

from datetime import UTC, datetime

from core.github import POLICY_CHECK, CIState

COMPLIANT_RULES = [
    {
        "type": "pull_request",
        "parameters": {
            "require_code_owner_review": True,
            "dismiss_stale_reviews_on_push": True,
            "require_last_push_approval": True,
        },
    },
    {
        "type": "required_status_checks",
        "parameters": {
            "strict_required_status_checks_policy": True,
            "required_status_checks": [{"context": POLICY_CHECK, "integration_id": 1}],
        },
    },
    {"type": "non_fast_forward"},
    {"type": "deletion"},
]


class FakeChat:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def post(self, text: str, thread: str | None = None) -> str:
        self.posts.append(text)
        return ""


class FakeGitHub:
    def __init__(self, repo: str = "acme/shop"):
        self.repo = repo
        self.issues: dict[int, dict] = {}
        self.prs: dict[int, dict] = {}
        self.ci: dict[str, CIState] = {}
        self.reviews: dict[int, list[dict]] = {}
        self.checks: list[tuple[str, str | None, str]] = []  # (sha, conclusion, title)
        self.auto_merge: set[str] = set()
        self.reruns: list[int] = []
        self.rules = list(COMPLIANT_RULES)
        self.files_at_base: dict[str, str] = {}
        self.label_actors: dict[tuple[int, str], str] = {}
        self.posted_reviews: list[tuple[int, str, str]] = []
        self.parents: dict[str, list[str]] = {}
        self._next = 1

    def _n(self) -> int:
        n, self._next = self._next, self._next + 1
        return n

    # Test helpers

    def add_issue(self, title="Fix the cart", labels=("agent-ready",), actor="owner", association="OWNER", body=""):
        n = self._n()
        self.issues[n] = {
            "number": n,
            "title": title,
            "body": body,
            "state": "open",
            "labels": [{"name": label} for label in labels],
            "author_association": association,
            "comments": [],
        }
        for label in labels:
            self.label_actors[(n, label)] = actor
        return n

    def add_pr(self, branch, sha, files=(("src/app.py", 10),), repo=None, labels=()):
        n = self._n()
        self.prs[n] = {
            "number": n,
            "title": f"PR {n}",
            "body": "",
            "state": "open",
            "merged_at": None,
            "draft": False,
            "mergeable_state": "clean",
            "node_id": f"PR_{n}",
            "html_url": f"https://github.com/{self.repo}/pull/{n}",
            "head": {"ref": branch, "sha": sha, "repo": {"full_name": repo or self.repo}},
            "labels": [{"name": label} for label in labels],
            "merge_commit_sha": None,
            "merged_by": None,
            "files": [{"filename": f, "additions": lines, "deletions": 0} for f, lines in files],
        }
        return n

    def merge(self, n: int, by: str = "owner") -> str:
        pr = self.prs[n]
        pr.update(
            state="closed",
            merged_at=datetime.now(UTC).isoformat(),
            merge_commit_sha=f"merge-{n}",
            merged_by={"login": by},
        )
        return pr["merge_commit_sha"]

    def labels_of(self, n: int) -> set[str]:
        return {lbl["name"] for lbl in self.issues[n]["labels"]}

    # The GitHub interface

    async def get_issue(self, repo, number):
        return self.issues[number]

    async def list_open_issues(self, repo, label):
        return [i for i in self.issues.values() if i["state"] == "open" and label in {x["name"] for x in i["labels"]}]

    async def label_actor(self, repo, number, label):
        return self.label_actors.get((number, label))

    async def issue_comments(self, repo, number, since=None):
        return list(self.issues[number]["comments"])

    async def create_issue(self, repo, title, body, labels):
        n = self.add_issue(title, labels, actor="agent-company[bot]", body=body)
        return n

    async def add_labels(self, repo, number, *labels):
        target = self.issues.get(number) or self.prs[number]
        for label in labels:
            if label not in {x["name"] for x in target["labels"]}:
                target["labels"].append({"name": label})

    async def remove_label(self, repo, number, label):
        target = self.issues.get(number) or self.prs[number]
        target["labels"] = [x for x in target["labels"] if x["name"] != label]

    async def comment(self, repo, number, body):
        (self.issues.get(number) or self.prs[number]).setdefault("comments", []).append(
            {"user": {"login": "agent-company[bot]"}, "body": body}
        )

    async def set_issue_state(self, repo, number, state):
        self.issues[number]["state"] = state

    async def get_pr(self, repo, number):
        return self.prs[number]

    async def list_prs(self, repo, state="open", **params):
        prs = [p for p in self.prs.values() if p["state"] == state]
        if "head" in params:
            prs = [p for p in prs if f"{repo.split('/')[0]}:{p['head']['ref']}" == params["head"]]
        return prs

    async def recently_closed_prs(self, repo, limit=50):
        return [p for p in self.prs.values() if p["state"] == "closed"]

    async def find_open_pr(self, repo, branch):
        return next(
            (
                p
                for p in self.prs.values()
                if p["state"] == "open" and p["head"]["ref"] == branch and p["head"]["repo"]["full_name"] == repo
            ),
            None,
        )

    async def open_or_update_pr(self, repo, branch, base, title, body):
        existing = await self.find_open_pr(repo, branch)
        if existing:
            existing.update(title=title, body=body)
            return existing
        n = self.add_pr(branch, "unknown")
        self.prs[n].update(title=title, body=body)
        return self.prs[n]

    async def pr_files(self, repo, number):
        return self.prs[number]["files"]

    async def pr_reviews(self, repo, number):
        return self.reviews.get(number, [])

    async def post_review(self, repo, number, commit_sha, event, body, comments=None):
        self.posted_reviews.append((number, commit_sha, event))

    async def update_branch(self, repo, number, expected_head_sha):
        # Like GitHub: a merge commit of the base into the head becomes the new head.
        new = f"{expected_head_sha}+base"
        self.prs[number]["head"]["sha"] = new
        self.prs[number]["mergeable_state"] = "clean"
        self.parents[new] = [expected_head_sha, "base-tip"]
        self.ci[new] = self.ci.get(expected_head_sha, CIState("none"))

    async def enable_auto_merge(self, repo, pr_node_id, method="SQUASH"):
        self.auto_merge.add(pr_node_id)

    async def revert_pr(self, repo, pr_node_id, title, body):
        n = self.add_pr(f"revert-{pr_node_id}", f"revert-sha-{pr_node_id}")
        self.prs[n].update(title=title, body=body)
        return n

    async def ci_state(self, repo, sha):
        return self.ci.get(sha, CIState("none"))

    async def commit_parents(self, repo, sha):
        return self.parents.get(sha, [])

    async def rerun_failed_jobs(self, repo, run_id):
        self.reruns.append(run_id)

    async def post_check(self, repo, sha, name, conclusion, title, summary):
        self.checks.append((sha, conclusion, title))

    async def branch_rules(self, repo, branch):
        return self.rules

    async def file_at(self, repo, path, ref):
        return self.files_at_base.get(path)
