"""Agent 10: Orchestrator - controls the entire pipeline workflow."""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Coroutine

from config import settings
from models import (
    JobStatus, PipelineEvent, PipelineJob, Finding, IssueClass,
    PatchResult, ValidationResult, PullRequest,
)
from utils.chroma_memory import RepositoryMemory
from utils.confidence_scorer import ConfidenceScorer
from utils.github_client import GitHubClient

from agents.repo_mapper import RepoMapperAgent
from agents.dependency_analyzer import DependencyAnalyzerAgent
from agents.static_analysis import StaticAnalysisAgent
from agents.llm_bug_hunter import LLMBugHunterAgent
from agents.bug_investigation import BugInvestigationAgent
from agents.repair_planner import RepairPlannerAgent
from agents.code_generation import CodeGenerationAgent
from agents.validation_agent import ValidationAgent
from agents.security_verification import SecurityVerificationAgent
from agents.pr_author import PRAuthorAgent


_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


class OrchestratorAgent:
    """LLM-based controller for the entire pipeline."""

    def __init__(self):
        self.name = "Orchestrator"
        self.memory = RepositoryMemory()
        self.confidence_scorer = ConfidenceScorer()
        self.github = GitHubClient()

        # Sub-agents
        self.repo_mapper = RepoMapperAgent()
        self.dep_analyzer = DependencyAnalyzerAgent()
        self.static_analysis = StaticAnalysisAgent()
        self.bug_hunter = LLMBugHunterAgent()
        self.bug_investigation = BugInvestigationAgent()
        self.repair_planner = RepairPlannerAgent()
        self.code_gen = CodeGenerationAgent()
        self.validator = ValidationAgent()
        self.security_verify = SecurityVerificationAgent()
        self.pr_author = PRAuthorAgent()

    async def run_pipeline(
        self,
        job: PipelineJob,
        event_callback: Callable[[PipelineEvent], Coroutine[Any, Any, None]],
    ) -> PipelineJob:
        """Execute the full 5-phase pipeline."""
        repo_owner, repo_name = self.github.get_repo_info(job.repo_url)
        job.repo_owner = repo_owner
        job.repo_name = repo_name

        # Phase 0: Clone
        job.status = JobStatus.CLONING
        await self._emit(event_callback, "Cloning repository...", agent_name=self.name)

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = self.github.clone_repo(job.repo_url, Path(tmpdir) / "repo", job.branch)

            # ── Phase 1: Parallel Discovery ──
            job.status = JobStatus.PHASE_1_DISCOVERY
            await self._emit(event_callback, "Phase 1: Discovery (graph + scanners + LLM bug hunt)...", agent_name=self.name)

            kg, findings_kg = await self.repo_mapper.run(str(repo_path), repo_name, event_callback)
            findings_dep = await self.dep_analyzer.run(str(repo_path), event_callback)
            findings_static = await self.static_analysis.run(str(repo_path), event_callback)
            findings_llm = await self.bug_hunter.run(str(repo_path), kg, event_callback)

            all_findings = findings_kg + findings_dep + findings_static + findings_llm
            deduped = self._dedupe(all_findings)

            # ── Gating: split fixable vs report-only ──
            fixable = [f for f in deduped if self._is_fixable(f)]
            report_only = [f for f in deduped if not self._is_fixable(f)]
            job.report_only_findings = report_only

            await self._emit(
                event_callback,
                f"Phase 1 complete: {len(deduped)} findings — {len(fixable)} fixable, "
                f"{len(report_only)} report-only.",
                agent_name=self.name,
                details={"total": len(deduped), "fixable": len(fixable), "report_only": len(report_only)},
            )

            if not fixable:
                job.findings = []
                job.status = JobStatus.COMPLETED
                job.completed_at = datetime.utcnow()
                await self._emit(event_callback, "No fixable issues found. Pipeline complete.", agent_name=self.name)
                return job

            # ── Phase 2: Investigation (fixable only) ──
            job.status = JobStatus.PHASE_2_INVESTIGATION
            job.findings = await self.bug_investigation.run(fixable, kg, event_callback)

            # ── Phase 3: Planning (file-grouped) ──
            job.status = JobStatus.PHASE_3_PLANNING
            job.repair_plan = await self.repair_planner.run(job.findings, kg, event_callback)

            # ── Phase 4 + 5: Fix-Validate loop, one PR per file ──
            job.status = JobStatus.PHASE_4_FIX_VALIDATE
            id_to_finding = {f.id: f for f in job.findings}

            fixed_files = 0
            for i, item in enumerate(job.repair_plan.items):
                if settings.max_files_to_fix and fixed_files >= settings.max_files_to_fix:
                    await self._emit(event_callback, "Reached max_files_to_fix cap; stopping.", agent_name=self.name)
                    break

                group = [id_to_finding[fid] for fid in item.finding_ids if fid in id_to_finding]
                if not group:
                    continue
                primary = max(group, key=lambda f: f.severity_rank)

                await self._emit(
                    event_callback,
                    f"Fixing {item.epicenter_file} ({len(group)} issue(s)) [{i+1}/{len(job.repair_plan.items)}]...",
                    agent_name=self.name,
                    details={"file": item.epicenter_file, "issues": len(group)},
                )

                patch, validation, ok = await self._fix_validate_loop(
                    item.epicenter_file, group, primary, str(repo_path), item.fix_strategy, kg, event_callback,
                )

                if not ok or patch is None or not patch.ok:
                    job.unresolved_findings.extend(group)
                    await self._emit(
                        event_callback,
                        f"Could not produce a validated fix for {item.epicenter_file} — marked unresolved.",
                        agent_name=self.name,
                        details={"file": item.epicenter_file},
                    )
                    continue

                cache_hit = any(f.similar_past_fixes for f in group)
                confidence = self.confidence_scorer.compute(validation, cache_hit, i == 0)
                job.validation_results.append(validation)

                # ── Phase 5: Publish (one PR per file) ──
                job.status = JobStatus.PHASE_5_PUBLICATION
                pr = await self.pr_author.create_pr(
                    file_path=item.epicenter_file,
                    findings=group,
                    patch=patch,
                    confidence=confidence,
                    repo_path=str(repo_path),
                    repo_url=job.repo_url,
                    repo_owner=repo_owner,
                    repo_name=repo_name,
                    base_branch=job.branch,
                    event_emitter=event_callback,
                )
                job.pull_requests.append(pr)
                fixed_files += 1

                # Store each fix in memory (best-effort).
                for f in group:
                    self._remember(f, item.fix_strategy, confidence.total_score)

                job.status = JobStatus.PHASE_4_FIX_VALIDATE  # back to loop

            job.status = JobStatus.COMPLETED
            job.completed_at = datetime.utcnow()
            await self._emit(
                event_callback,
                f"Pipeline complete: {len(job.pull_requests)} PR(s) from {len(job.findings)} fixable "
                f"finding(s); {len(job.unresolved_findings)} unresolved; "
                f"{len(job.report_only_findings)} report-only.",
                agent_name=self.name,
                details={"prs": len(job.pull_requests), "unresolved": len(job.unresolved_findings)},
            )

        return job

    # ── Fix-validate inner loop ───────────────────────────────────────
    async def _fix_validate_loop(
        self, file_path, group, primary, repo_path, fix_strategy, kg, event_callback,
    ) -> tuple[PatchResult | None, ValidationResult, bool]:
        """Generate → validate → security-verify, retrying on failure."""
        patch: PatchResult | None = None
        validation = ValidationResult()

        # Immediate graph neighbours are candidate locations for a cross-file
        # root-cause fix — offer them to the code generator as editable context.
        related = self._related_files(file_path, kg)

        for attempt in range(settings.validation_max_retries):
            if attempt == 0:
                patch = await self.code_gen.generate_file_patch(
                    file_path, group, repo_path, fix_strategy, related, event_callback,
                )
            else:
                error_out = "\n".join(validation.test_failures) if validation else ""
                patch = await self.code_gen.regenerate_with_error(
                    file_path, group, repo_path, patch, error_out, related, event_callback,
                )

            if patch is None or not patch.ok:
                validation = ValidationResult(test_failures=(patch.errors if patch else ["No patch produced"]))
                continue

            validation = await self.validator.validate(primary, patch, repo_path, kg, event_callback)
            security = await self.security_verify.verify(primary, patch, repo_path, event_callback)
            validation.gate_4_security_clean = security.gate_4_security_clean
            validation.new_security_findings = security.new_security_findings
            validation.retry_count = attempt

            if validation.passed and validation.gate_4_security_clean:
                return patch, validation, True

        return patch, validation, False

    # ── Helpers ───────────────────────────────────────────────────────
    def _related_files(self, file_path: str, kg, limit: int = 3) -> list[str]:
        """Editable source files adjacent to ``file_path`` in the knowledge graph
        (its dependencies/dependents) — candidate sites for a cross-file fix."""
        try:
            neighbours = kg.get_neighbours(file_path, hop=1)
        except Exception:
            return []
        out: list[str] = []
        for n in neighbours:
            if n == file_path:
                continue
            attrs = kg.graph.nodes.get(n, {})
            if attrs.get("type") == "File" and str(n).endswith(".py"):
                out.append(n)
        return out[:limit]

    def _is_fixable(self, f: Finding) -> bool:
        """Decide whether a finding should produce a PR (vs report-only)."""
        cls = f.issue_class.value
        if cls == IssueClass.CODE_QUALITY.value and not settings.fix_code_quality:
            return False
        if cls not in settings.auto_fix_issue_classes and not (
            cls == IssueClass.CODE_QUALITY.value and settings.fix_code_quality
        ):
            return False
        min_rank = _SEVERITY_RANK.get(settings.min_severity_to_fix, 1)
        return f.severity_rank >= min_rank

    def _dedupe(self, findings: list[Finding]) -> list[Finding]:
        """Collapse duplicate findings (same file, ~line, normalized title)."""
        seen: set[tuple] = set()
        out: list[Finding] = []
        for f in findings:
            key = (f.file_path, f.line_number, f.issue_class.value, f.title.strip().lower())
            if key in seen:
                continue
            seen.add(key)
            out.append(f)
        return out

    def _remember(self, finding: Finding, fix_strategy: str, score: float) -> None:
        try:
            self.memory.store_fix(
                bug_type=finding.issue_class.value,
                root_cause=finding.root_cause or finding.title,
                fix_strategy=fix_strategy,
                affected_file=finding.file_path,
                confidence_score=score,
            )
        except Exception:
            pass  # memory is best-effort; never break the pipeline

    async def _emit(
        self,
        callback: Callable[[PipelineEvent], Coroutine[Any, Any, None]],
        message: str,
        agent_name: str = "Orchestrator",
        details: dict | None = None,
    ) -> None:
        event = PipelineEvent(
            event_type="orchestrator_update",
            agent_name=agent_name,
            message=message,
            details=details or {},
        )
        await callback(event)
