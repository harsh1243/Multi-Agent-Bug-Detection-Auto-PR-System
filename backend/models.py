"""Pydantic models for the bug detection system."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field


class IssueClass(str, Enum):
    """Four classes of issues the system detects."""
    FUNCTIONAL_BUG = "functional_bug"
    SECURITY_VULNERABILITY = "security_vulnerability"
    CODE_QUALITY = "code_quality"
    PERFORMANCE = "performance"


class FindingSeverity(str, Enum):
    """Severity levels for findings."""
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class JobStatus(str, Enum):
    """Pipeline execution status."""
    PENDING = "pending"
    CLONING = "cloning"
    PHASE_1_DISCOVERY = "phase_1_discovery"
    PHASE_2_INVESTIGATION = "phase_2_investigation"
    PHASE_3_PLANNING = "phase_3_planning"
    PHASE_4_FIX_VALIDATE = "phase_4_fix_validate"
    PHASE_5_PUBLICATION = "phase_5_publication"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class PipelineEvent(BaseModel):
    """SSE event for pipeline updates."""
    event_type: str  # agent_start, agent_complete, finding_discovered, fix_generated, etc.
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    agent_name: Optional[str] = None
    phase: Optional[str] = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


class Finding(BaseModel):
    """A discovered issue in the repository."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    issue_class: IssueClass
    severity: FindingSeverity
    title: str
    description: str
    file_path: str
    line_number: Optional[int] = None
    column: Optional[int] = None
    code_snippet: Optional[str] = None
    tool_source: str  # llm, bandit, eslint, pip-audit, repo_mapper, etc.
    rule_id: Optional[str] = None
    confidence: float = 0.0

    # Discovery extras (populated by the LLM Bug Hunter)
    suggested_fix: Optional[str] = None   # concrete fix instruction from confirmation pass
    evidence: Optional[str] = None        # why this is a real bug (cited reasoning)

    # Investigation results (populated by Bug Investigation Agent)
    root_cause: Optional[str] = None
    affected_files: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    blast_radius: int = 0
    similar_past_fixes: list[dict] = Field(default_factory=list)

    @property
    def severity_rank(self) -> int:
        """Numeric rank for severity comparisons (higher = more severe)."""
        return {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}.get(
            self.severity.value, 1
        )


class RepairPlanItem(BaseModel):
    """A single item in the repair plan — one epicenter file, all its findings."""
    finding_id: str  # primary/highest-severity finding (back-compat)
    finding_ids: list[str] = Field(default_factory=list)  # all findings in this file
    epicenter_file: str
    files_to_modify: list[str]
    fix_strategy: str
    confidence_pre_score: float = 0.0
    dependencies: list[str] = Field(default_factory=list)  # finding_ids that must be fixed first


class RepairPlan(BaseModel):
    """The complete repair plan."""
    items: list[RepairPlanItem]
    total_confidence_pre_score: float = 0.0
    estimated_fix_time_seconds: int = 0


class FilePatch(BaseModel):
    """A single file changed by a patch (a patch may span several files)."""
    file_path: str
    new_content: str                       # full new file content (after applying edits)
    unified_diff: str = ""                 # minimal diff for this file
    edits_applied: int = 0
    edits_total: int = 0


class PatchResult(BaseModel):
    """Result of generating a surgical patch spanning one or more files.

    A fix can now touch the symptom file *and* the root-cause file(s). ``files``
    holds every changed file; ``file_path`` is the epicenter/primary file.
    """
    file_path: str                         # primary/epicenter file (back-compat)
    ok: bool = False                       # at least one file edited cleanly + sane
    files: list[FilePatch] = Field(default_factory=list)  # every changed file
    unified_diff: str = ""                 # combined diff across all files (for the PR)
    edits_applied: int = 0
    edits_total: int = 0
    errors: list[str] = Field(default_factory=list)
    raw_response: str = ""                  # raw LLM output (for retry context)

    @property
    def new_content(self) -> Optional[str]:
        """Back-compat: content of the primary file if changed, else the first
        changed file (or None if nothing was changed)."""
        for fp in self.files:
            if fp.file_path == self.file_path:
                return fp.new_content
        return self.files[0].new_content if self.files else None

    @property
    def changed_files(self) -> list[str]:
        """Paths of every file this patch changes."""
        return [fp.file_path for fp in self.files]


class ValidationResult(BaseModel):
    """Result from the validation pipeline."""
    gate_1_ast_valid: bool = False
    gate_2_tests_passed: bool = False
    gate_3_no_regressions: bool = False
    gate_4_security_clean: bool = False
    tests_available: bool = True           # False => repo has no runnable test suite
    pytest_output: Optional[str] = None
    bandit_output: Optional[str] = None
    test_failures: list[str] = Field(default_factory=list)
    new_security_findings: list[dict] = Field(default_factory=list)
    retry_count: int = 0
    passed: bool = False


class ConfidenceScore(BaseModel):
    """Confidence score breakdown."""
    tests_signal: float = 0.0  # +40%
    security_clean_signal: float = 0.0  # +25%
    ast_valid_signal: float = 0.0  # +10%
    cache_hit_signal: float = 0.0  # +15%
    fix_order_signal: float = 0.0  # +10%
    total_score: float = 0.0
    is_critical_path: bool = False


class PullRequest(BaseModel):
    """Generated pull request."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    finding_id: str
    finding_ids: list[str] = Field(default_factory=list)  # all findings bundled in this PR
    title: str
    description: str
    branch_name: str
    files_changed: list[str]
    diff_content: str
    confidence_score: ConfidenceScore
    blast_radius_summary: str
    root_cause_explanation: str
    requires_approval: bool = False
    approval_status: str = "pending"  # pending, approved, rejected, stale
    github_pr_url: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class PipelineJob(BaseModel):
    """A pipeline execution job."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    repo_url: str
    repo_owner: str
    repo_name: str
    branch: str = "main"
    status: JobStatus = JobStatus.PENDING
    created_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    events: list[PipelineEvent] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)             # fixable findings (→ PRs)
    report_only_findings: list[Finding] = Field(default_factory=list)  # informational (no PRs)
    unresolved_findings: list[Finding] = Field(default_factory=list)   # fixable but patch failed
    repair_plan: Optional[RepairPlan] = None
    validation_results: list[ValidationResult] = Field(default_factory=list)
    pull_requests: list[PullRequest] = Field(default_factory=list)
    error_message: Optional[str] = None
    cost_estimate_usd: float = 0.0


class RunRequest(BaseModel):
    """Request body for POST /run."""
    repo_url: str
    branch: str = "main"
    max_findings: int = 50


class JobResponse(BaseModel):
    """Response for job creation."""
    job_id: str
    status: str
    message: str
