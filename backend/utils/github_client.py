"""GitHub API client for PR creation and repo operations."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from github import Github
from github.PullRequest import PullRequest as GHPR

from config import settings


def _token() -> str | None:
    """Read the GitHub token at call time — not at import time.

    settings.github_token is frozen at module-import time (Pydantic reads env
    vars once in Settings.__init__).  The Streamlit sidebar sets
    os.environ["GITHUB_TOKEN"] *after* the backend module tree is imported, so
    settings.github_token is always None in that path.  Reading directly from
    os.environ here picks up the live value set by the sidebar.
    """
    return (
        os.environ.get("GITHUB_TOKEN")
        or settings.github_token
        or None
    )


class GitHubClient:
    """GitHub operations: clone, branch, commit, PR."""

    def __init__(self):
        # PyGitHub client is rebuilt on first use via _github() so it always
        # picks up the token even when it is set after module import.
        self._gh: Github | None = None

    def _github(self) -> Github:
        """Return an authenticated PyGitHub instance (lazy, token-aware)."""
        tok = _token()
        if self._gh is None or (tok and not isinstance(self._gh._Github__requester._Requester__authorizationHeader, str)):
            self._gh = Github(tok) if tok else Github()
        return self._gh

    def clone_repo(self, repo_url: str, target_dir: Path, branch: str = "main") -> Path:
        """Clone a repository to target directory."""
        # Extract owner/repo from URL
        parts = repo_url.rstrip("/").split("/")
        owner, repo = parts[-2], parts[-1].replace(".git", "")

        token_part = f"{_token()}@" if _token() else ""
        auth_url = f"https://{token_part}github.com/{owner}/{repo}.git"

        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                ["git", "clone", "--depth", "1", "--branch", branch, auth_url, str(target_dir)],
                check=True, capture_output=True, timeout=120,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
            # The token is embedded in ``auth_url``, and both exceptions put the
            # full command in their message — which the UI prints. Re-raise a
            # clean error; ``from None`` drops the original so nothing leaks.
            raise RuntimeError(
                f"git clone failed for {owner}/{repo} (branch '{branch}')."
            ) from None
        return target_dir

    def create_branch(self, repo_path: Path, branch_name: str, base_branch: str | None = None) -> None:
        """Create and checkout a new branch, cut from ``base_branch`` when given.

        Branching from the base each time keeps per-file PRs independent instead of
        stacking each fix on top of the previous one.
        """
        args = ["git", "checkout", "-b", branch_name]
        if base_branch:
            args.append(base_branch)
        try:
            subprocess.run(args, cwd=repo_path, check=True, capture_output=True)
        except subprocess.CalledProcessError:
            # Base ref not available (e.g. shallow/detached) — branch from current HEAD.
            subprocess.run(
                ["git", "checkout", "-B", branch_name],
                cwd=repo_path, check=True, capture_output=True,
            )

    def commit_changes(self, repo_path: Path, message: str) -> None:
        """Stage and commit all changes."""
        subprocess.run(
            ["git", "add", "-A"], cwd=repo_path, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=agent@bugdetector.dev",
             "-c", "user.name=Bug Detection Agent",
             "commit", "-m", message],
            cwd=repo_path, check=True, capture_output=True,
        )

    def push_branch(self, repo_path: Path, branch_name: str) -> None:
        """Push branch to remote.

        Re-injects the token into the remote URL at push time so that the push
        is authenticated even when the repo was cloned without credentials (e.g.
        a public repo cloned without a token).  Without this, git push on a
        shallow clone of someone else's repo returns exit code 128 (remote:
        Permission to <owner>/<repo>.git denied).
        """
        token = _token()
        if token:
            # Read the current remote URL and embed the token if not already there.
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=repo_path, capture_output=True, text=True,
            )
            remote_url = result.stdout.strip()
            if remote_url and "github.com" in remote_url and f"{token}@" not in remote_url:
                # https://github.com/... → https://TOKEN@github.com/...
                auth_url = remote_url.replace("https://", f"https://{token}@", 1)
                subprocess.run(
                    ["git", "remote", "set-url", "origin", auth_url],
                    cwd=repo_path, check=True, capture_output=True,
                )
        result = subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=repo_path, capture_output=True, timeout=120,
        )
        if result.returncode != 0:
            stderr = result.stderr.decode(errors="ignore").strip()
            raise subprocess.CalledProcessError(
                result.returncode,
                ["git", "push"],
                stderr=stderr.encode(),
            )

    def restore_worktree(self, repo_path: Path, base_branch: str | None = None) -> None:
        """Discard uncommitted changes and return to the base branch.

        Called after each PR attempt (success or failure) so leftover writes from
        a failed attempt can never leak into the next PR's ``git add -A``.
        """
        subprocess.run(
            ["git", "reset", "--hard"], cwd=repo_path, capture_output=True,
        )
        subprocess.run(
            ["git", "clean", "-fd"], cwd=repo_path, capture_output=True,
        )
        if base_branch:
            subprocess.run(
                ["git", "checkout", base_branch], cwd=repo_path, capture_output=True,
            )

    def create_pull_request(
        self,
        owner: str,
        repo: str,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str = "main",
        draft: bool = False,
    ) -> str:
        """Create a GitHub PR and return the URL."""
        if not _token():
            return ""
        repository = self._github().get_repo(f"{owner}/{repo}")
        pr = repository.create_pull(
            title=title, body=body, head=head_branch,
            base=base_branch, draft=draft,
        )
        return pr.html_url

    def get_repo_info(self, repo_url: str) -> tuple[str, str]:
        """Extract owner and repo name from URL."""
        parts = repo_url.rstrip("/").split("/")
        return parts[-2], parts[-1].replace(".git", "")
