"""Trusted git operations on the worker. Agent code never runs in these repositories.

The sandbox gets the repository as a git bundle and returns its commits as a bundle. Everything
that needs a GitHub token or must be trustworthy (what the base is, what changed, whether it may
be pushed) happens here, in a fresh bare repository with hooks and fsmonitor disabled and no user
or system git config, so nothing the agent wrote can run or influence the result.
"""

import base64
import os
import subprocess
from pathlib import Path

from agents._shared.guards import protected_paths_touched
from core.settings import get_settings


class GitError(RuntimeError):
    pass


class RejectedResult(Exception):
    """The sandbox's commits may not be pushed (rewritten history, protected paths, too large)."""


class GitWorkspace:
    def __init__(self, root: Path, repo: str, token: str | None = None):
        self.root = root
        self.url = get_settings().github_git_url.format(repo=repo)
        self._auth: list[str] = []
        if token:
            header = base64.b64encode(f"x-access-token:{token}".encode()).decode()
            self._auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {header}"]
        self._env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root.parent),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
        for k in ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy"):
            if k in os.environ:
                self._env[k] = os.environ[k]

    def git(self, *args: str, auth: bool = False) -> str:
        cmd = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            *(self._auth if auth else []),
            *args,
        ]
        p = subprocess.run(cmd, cwd=self.root, env=self._env, capture_output=True, text=True)
        if p.returncode != 0:
            # Never echo the command line: it may hold the auth header.
            raise GitError(f"git {args[0]} failed: {p.stderr.strip()[-500:]}")
        return p.stdout.strip()

    def init(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.git("init", "--quiet", "--bare")

    def fetch(self, remote_ref: str, local_ref: str) -> str:
        """Fetch one ref from the product repo; return its sha."""
        self.git("fetch", "--quiet", "--no-tags", self.url, f"+{remote_ref}:{local_ref}", auth=True)
        return self.rev(local_ref)

    def remote_has(self, ref: str) -> bool:
        return bool(self.git("ls-remote", self.url, ref, auth=True))

    def rev(self, ref: str) -> str:
        return self.git("rev-parse", "--verify", f"{ref}^{{commit}}")

    def show(self, ref: str, path: str) -> str | None:
        try:
            return self.git("show", f"{ref}:{path}")
        except GitError:
            return None

    def list_dir(self, ref: str, path: str) -> list[str]:
        try:
            out = self.git("ls-tree", "--name-only", f"{ref}:{path}")
        except GitError:
            return []
        return sorted(out.splitlines())

    def project_instructions(self, ref: str) -> str:
        """CLAUDE.md and docs/brain/*.md as of `ref` (the base branch, never the PR under work)."""
        parts = []
        claude_md = self.show(ref, "CLAUDE.md")
        if claude_md:
            parts.append(f"# Repository instructions (CLAUDE.md)\n\n{claude_md}")
        brain = [(n, self.show(ref, f"docs/brain/{n}")) for n in self.list_dir(ref, "docs/brain") if n.endswith(".md")]
        if brain:
            parts.append("# Product brain\n\n" + "\n\n".join(f"## {n}\n\n{t}" for n, t in brain if t))
        return "\n\n".join(parts)

    def input_bundle(self, path: Path, base_sha: str, work_sha: str) -> None:
        """A bundle with `base` and `work` branches: everything the sandbox gets of the repo."""
        self.git("update-ref", "refs/heads/base", base_sha)
        self.git("update-ref", "refs/heads/work", work_sha)
        self.git("bundle", "create", "--quiet", str(path), "refs/heads/base", "refs/heads/work")

    def import_result(self, bundle: Path, start_sha: str) -> str:
        """Bring in the sandbox's commits. Returns the candidate head sha."""
        if bundle.stat().st_size > get_settings().max_bundle_mb * 1024 * 1024:
            raise RejectedResult(f"result bundle is larger than {get_settings().max_bundle_mb} MB")
        try:
            self.git("bundle", "verify", "--quiet", str(bundle))
            self.git("fetch", "--quiet", "--no-tags", str(bundle), "+refs/heads/agent-result:refs/candidate/head")
        except GitError as e:
            raise RejectedResult(f"unusable result bundle: {e}") from e
        head = self.rev("refs/candidate/head")
        try:
            self.git("merge-base", "--is-ancestor", start_sha, head)
        except GitError as e:
            raise RejectedResult("the commits rewrite history instead of building on the branch") from e
        return head

    def changed_files(self, base_sha: str, head_sha: str) -> list[str]:
        return self.git("diff", "--name-only", "--no-renames", f"{base_sha}...{head_sha}").splitlines()

    def check_pushable(self, base_sha: str, head_sha: str) -> list[str]:
        """Changed files, or RejectedResult if any is protected."""
        changed = self.changed_files(base_sha, head_sha)
        touched = protected_paths_touched(changed)
        if touched:
            raise RejectedResult("the change touches protected paths: " + ", ".join(touched))
        return changed

    def push(self, sha: str, branch: str) -> None:
        """Fast-forward only: if the branch moved, the push fails and the job is retried."""
        self.git("push", "--quiet", self.url, f"{sha}:refs/heads/{branch}", auth=True)
