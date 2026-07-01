"""Agent 9: PR Author - creates one structured pull request per file."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import settings
from models import ConfidenceScore, Finding, PatchResult, PullRequest, PipelineEvent
from utils.critical_path import CriticalPathDetector
from utils.github_client import GitHubClient
from utils.llm_client import llm, LLMError


class PRAuthorAgent:
    """Creates structured PRs (one per file) with confidence scores and gates."""

    def __init__(self):
        self.name = "PR Author"
        self.phase = "phase_5_publication"
        self.github = GitHubClient()
        self.critical_detector = CriticalPathDetector()

    async def create_pr(
        self,
        file_path: str,
        findings: list[Finding],
        patch: PatchResult,
        confidence: ConfidenceScore,
        repo_path: str,
        repo_url: str,
        repo_owner: str,
        repo_name: str,
        base_branch: str = "main",
        event_emitter: Optional[Any] = None,
    ) -> PullRequest:
        """Create a single pull request fixing all findings in one file."""
        primary = max(findings, key=lambda f: f.severity_rank)

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message=f"Creating PR for {file_path} ({len(findings)} issue(s))...",
                details={"file": file_path},
            ))

        # Critical-path / approval gating.
        modified_funcs: list[str] = []
        for f in findings:
            modified_funcs.extend(f.affected_modules)
        is_critical = self.critical_detector.analyze_fix(
            file_path=file_path,
            finding_severity=primary.severity.value,
            modified_functions=modified_funcs,
        )
        confidence.is_critical_path = is_critical
        requires_approval = (
            confidence.total_score < settings.confidence_threshold_auto_merge or is_critical
        )

        # Title summarises the file's fixes.
        if len(findings) == 1:
            title = f"[Fix] {primary.title}"
        else:
            title = f"[Fix] {file_path}: {len(findings)} issues ({primary.severity.value}+)"

        description = await self._generate_description(file_path, findings, confidence, patch)
        blast_summary = self._blast_summary(primary)

        # Branch (from base) → write the validated patch into the working tree →
        # commit → push → open PR. Writing the patch here is essential: validation
        # only touched sandbox copies, so the real working tree is still unchanged.
        branch_name = f"fix/{primary.id}-{datetime.utcnow():%Y%m%d-%H%M%S}"
        pr_url = ""
        wrote = False
        try:
            self.github.create_branch(repo_path, branch_name, base_branch)
            wrote = self._apply_patch_to_worktree(patch, repo_path)
            if not wrote:
                raise RuntimeError("patch produced no file content to commit")
            self.github.commit_changes(repo_path, title)
            self.github.push_branch(repo_path, branch_name)
            pr_url = self.github.create_pull_request(
                repo_owner, repo_name, title, description,
                branch_name, base_branch=base_branch, draft=requires_approval,
            )
        except Exception as e:
            if event_emitter:
                await event_emitter(PipelineEvent(
                    event_type="agent_warning", agent_name=self.name,
                    phase=self.phase, message=f"GitHub operation failed: {str(e)[:160]}",
                ))

        files_changed = [file_path]
        for f in findings:
            for af in f.affected_files[:3]:
                if af not in files_changed:
                    files_changed.append(af)

        pr = PullRequest(
            finding_id=primary.id,
            finding_ids=[f.id for f in findings],
            title=title,
            description=description,
            branch_name=branch_name,
            files_changed=files_changed[:6],
            diff_content=patch.unified_diff,
            confidence_score=confidence,
            blast_radius_summary=blast_summary,
            root_cause_explanation=primary.root_cause or "",
            requires_approval=requires_approval,
            github_pr_url=pr_url,
        )

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="pr_created", agent_name=self.name,
                phase=self.phase,
                message=f"PR {'created' if pr_url else 'prepared'}: {title} (confidence: {confidence.total_score:.0%})",
                details={"file": file_path, "pr_url": pr_url,
                         "confidence": confidence.total_score, "needs_approval": requires_approval},
            ))

        return pr

    def _apply_patch_to_worktree(self, patch: PatchResult, repo_path: str) -> bool:
        """Write the validated patch content into the real working tree to commit."""
        if not patch.ok or patch.new_content is None:
            return False
        target = Path(repo_path) / patch.file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patch.new_content, encoding="utf-8")
        return True

    async def _generate_description(
        self, file_path: str, findings: list[Finding], confidence: ConfidenceScore, patch: PatchResult,
    ) -> str:
        critical_badge = "⚠️ CRITICAL PATH" if confidence.is_critical_path else ""
        approval_note = (
            "🔒 Human approval required"
            if (confidence.total_score < settings.confidence_threshold_auto_merge or confidence.is_critical_path)
            else "✅ Auto-merge eligible"
        )

        issue_lines = "\n".join(
            f"- **{f.title}** ({f.issue_class.value}/{f.severity.value}"
            + (f", line {f.line_number}" if f.line_number else "") + ")"
            + (f" — {f.root_cause}" if f.root_cause else "")
            for f in findings
        )

        summary_prompt = f"""Write a concise, professional PR summary (2-3 sentences) for fixes in `{file_path}`.
Issues fixed:
{issue_lines}

Describe what was wrong and how the change fixes it at the source. Be specific and technical."""
        try:
            body = await llm.triage(summary_prompt, max_tokens=512)
        except LLMError:
            body = f"Fixes {len(findings)} issue(s) in `{file_path}`."

        signals = (
            f"| Tests Passed | +40% | {'✅' if confidence.tests_signal > 0 else '❌'} |\n"
            f"| Security Clean | +25% | {'✅' if confidence.security_clean_signal > 0 else '❌'} |\n"
            f"| AST Valid | +10% | {'✅' if confidence.ast_valid_signal > 0 else '❌'} |\n"
            f"| Cache Hit | +15% | {'✅' if confidence.cache_hit_signal > 0 else '❌'} |\n"
            f"| Fix Order | +10% | {'✅' if confidence.fix_order_signal > 0 else '❌'} |"
        )

        diff_block = ""
        if patch.unified_diff:
            diff = patch.unified_diff
            if len(diff) > 6000:
                diff = diff[:6000] + "\n... (diff truncated)"
            diff_block = f"\n\n### Diff\n```diff\n{diff}\n```"

        return f"""## Fixes in `{file_path}`

{body}

### Issues addressed
{issue_lines}

---

### Confidence Score: {confidence.total_score:.0%}

| Signal | Weight | Status |
|--------|--------|--------|
{signals}

**{approval_note}** {critical_badge}
{diff_block}
"""

    def _blast_summary(self, finding: Finding) -> str:
        n = finding.blast_radius
        mods = len(finding.affected_modules)
        if n == 0:
            return "Isolated change — no other files affected"
        if n <= 2:
            return f"Narrow blast radius: {n} file(s) affected"
        if n <= 8:
            return f"Moderate blast radius: {n} files across {mods} module(s)"
        return f"Wide blast radius: {n} files across {mods} module(s) — careful review advised"
