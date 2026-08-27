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
        # Always rebuild the client when called — simpler than tracking token changes.
        # PyGitHub construction is cheap (no network call), and this is only called
        # a few times per run (fork check, PR creation, merge).
        self._gh = Github(tok) if tok else Github()
        return self._gh

    def ensure_fork(self, owner: str, repo: str) -> tuple[str, str]:
        """Fork the repo if we don't have push access, return (owner, repo) to use.

        If the authenticated user owns the repo or is a collaborator with push
        rights, returns the original (owner, repo). Otherwise forks to the user's
        account and returns (user.login, repo). Creates the fork if it doesn't
        exist yet; reuses it if it already exists.

        Returns the original owner/repo unchanged if no token is configured.
        """
        tok = _token()
        if not tok:
            # No token — can't check permissions or fork. Caller will fail at push.
            return owner, repo

        gh = self._github()
        try:
            repository = gh.get_repo(f"{owner}/{repo}")
            user = gh.get_user()

            # Do we already have push access?
            if repository.owner.login == user.login:
                return owner, repo  # we own it
            if repository.permissions and repository.permissions.push:
                return owner, repo  # we're a collaborator with push

            # Need a fork. Check if we already forked it.
            try:
                fork = user.get_repo(repo)
                if fork.fork and fork.parent and fork.parent.full_name == f"{owner}/{repo}":
                    # Fork exists and points at the right upstream.
                    return user.login, repo
            except Exception:
                pass  # fork doesn't exist yet

            # Create the fork (async on GitHub's side; usually ready in <5s).
            fork = user.create_fork(repository)
            return fork.owner.login, fork.name

        except Exception:
            # API call failed (rate limit, network, etc.) — return original and let
            # the push attempt surface the real error.
            return owner, repo

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

    def push_branch(self, repo_path: Path, branch_name: str, upstream_owner: str = "", upstream_repo: str = "") -> tuple[str, str]:
        """Push branch to remote, forking first if we lack push access.

        Returns (fork_owner, fork_repo) — the owner/repo the branch was pushed to.
        When we have push access to the original repo, returns the original owner/repo.
        When we forked, returns the fork's owner/repo so the caller can create a
        cross-repo PR with the correct head format (fork_owner:branch → base_owner:base).

        Re-injects the token into the remote URL at push time so that the push
        is authenticated even when the repo was cloned without credentials (e.g.
        a public repo cloned without a token).  Without this, git push on a
        shallow clone of someone else's repo returns exit code 128 (remote:
        Permission to <owner>/<repo>.git denied).
        """
        token = _token()

        # Read current remote to extract owner/repo if not provided
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=repo_path, capture_output=True, text=True,
        )
        remote_url = result.stdout.strip()

        if not upstream_owner and remote_url:
            # Extract from remote: https://github.com/owner/repo.git
            parts = remote_url.replace("https://", "").replace("http://", "").split("/")
            if len(parts) >= 3 and "github.com" in parts[0]:
                upstream_owner = parts[1]
                upstream_repo = parts[2].replace(".git", "")

        # Check if we need to fork
        fork_owner, fork_repo = upstream_owner, upstream_repo
        if token and upstream_owner and upstream_repo:
            fork_owner, fork_repo = self.ensure_fork(upstream_owner, upstream_repo)

        # If we forked, update the remote URL to point at the fork
        if fork_owner != upstream_owner and token:
            fork_url = f"https://{token}@github.com/{fork_owner}/{fork_repo}.git"
            subprocess.run(
                ["git", "remote", "set-url", "origin", fork_url],
                cwd=repo_path, check=True, capture_output=True,
            )
        elif token and remote_url and "github.com" in remote_url and f"{token}@" not in remote_url:
            # No fork, but embed token in URL for auth
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

        return fork_owner, fork_repo

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

    def merge_pull_request(
        self,
        owner: str,
        repo: str,
        pr_url: str,
        commit_message: str = "",
    ) -> bool:
        """Merge an open PR by its URL. Returns True on success.

        Only merges if the PR is open and not a draft. Uses squash merge
        so the fix lands as a single clean commit on the base branch.
        """
        if not _token() or not pr_url:
            return False
        try:
            repository = self._github().get_repo(f"{owner}/{repo}")
            # Extract PR number from URL (last path segment)
            pr_number = int(pr_url.rstrip("/").split("/")[-1])
            pr = repository.get_pull(pr_number)
            if pr.state != "open" or pr.draft:
                return False
            # Wait briefly for GitHub to register the PR as mergeable
            import time
            for _ in range(5):
                pr = repository.get_pull(pr_number)
                if pr.mergeable is not None:
                    break
                time.sleep(2)
            if not pr.mergeable:
                return False
            msg = commit_message or pr.title
            pr.merge(
                commit_title=msg,
                merge_method="squash",
            )
            return True
        except Exception:
            return False

    def get_repo_info(self, repo_url: str) -> tuple[str, str]:
        """Extract owner and repo name from URL."""
        parts = repo_url.rstrip("/").split("/")
        return parts[-2], parts[-1].replace(".git", "")
