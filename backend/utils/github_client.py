"""GitHub API client for PR creation and repo operations."""

from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

from github import Github
from github.PullRequest import PullRequest as GHPR

from config import settings


class GitHubClient:
    """GitHub operations: clone, branch, commit, PR."""

    def __init__(self):
        self.github = Github(settings.github_token or "")

    def clone_repo(self, repo_url: str, target_dir: Path, branch: str = "main") -> Path:
        """Clone a repository to target directory."""
        # Extract owner/repo from URL
        parts = repo_url.rstrip("/").split("/")
        owner, repo = parts[-2], parts[-1].replace(".git", "")

        token_part = f"{settings.github_token}@" if settings.github_token else ""
        auth_url = f"https://{token_part}github.com/{owner}/{repo}.git"

        target_dir.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--branch", branch, auth_url, str(target_dir)],
            check=True, capture_output=True, timeout=120,
        )
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
        """Push branch to remote."""
        subprocess.run(
            ["git", "push", "-u", "origin", branch_name],
            cwd=repo_path, check=True, capture_output=True,
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
        if not settings.github_token:
            return ""
        repository = self.github.get_repo(f"{owner}/{repo}")
        pr = repository.create_pull(
            title=title, body=body, head=head_branch,
            base=base_branch, draft=draft,
        )
        return pr.html_url

    def get_repo_info(self, repo_url: str) -> tuple[str, str]:
        """Extract owner and repo name from URL."""
        parts = repo_url.rstrip("/").split("/")
        return parts[-2], parts[-1].replace(".git", "")
