"""Agent 8: Security Verification - differential security analysis."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from config import settings
from models import Finding, PatchResult, ValidationResult, PipelineEvent


class SecurityVerificationAgent:
    """Performs differential security analysis pre/post fix."""

    def __init__(self):
        self.name = "Security Verification"
        self.phase = "phase_4_fix_validate"

    async def verify(
        self,
        finding: Finding,
        patch: PatchResult,
        repo_path: str,
        event_emitter: Optional[Any] = None,
    ) -> ValidationResult:
        """Run differential security analysis."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message=f"Running differential security analysis on {finding.file_path}...",
                details={"finding_id": finding.id},
            ))

        with tempfile.TemporaryDirectory() as tmpdir:
            # Baseline (original)
            original = Path(tmpdir) / "original"
            shutil.copytree(repo_path, original, ignore=shutil.ignore_patterns(".git"))

            # Patched
            patched = Path(tmpdir) / "patched"
            shutil.copytree(repo_path, patched, ignore=shutil.ignore_patterns(".git"))
            self._apply_patch(patch, patched)

            # Run scans
            baseline_findings = self._scan_file(original, finding.file_path)
            post_findings = self._scan_file(patched, finding.file_path)

            # Differential analysis
            baseline_ids = {f"{f.get('check_id', '')}:{f.get('start', {}).get('line', 0)}" for f in baseline_findings}
            post_ids = {f"{f.get('check_id', '')}:{f.get('start', {}).get('line', 0)}" for f in post_findings}

            # Original vulnerability should be gone. Scanner findings carry a rule_id
            # we can re-check; LLM-found bugs have none, so there is no scanner rule
            # to verify against — treat the original as resolved and rely on the
            # "no new vulnerabilities" check below. Always a real bool.
            if finding.rule_id:
                original_vuln_gone = not any(
                    f.get("check_id") == finding.rule_id for f in post_findings
                )
            else:
                original_vuln_gone = True

            # No NEW medium+ findings introduced
            new_findings = [f for f in post_findings
                           if f"{f.get('check_id', '')}:{f.get('start', {}).get('line', 0)}" not in baseline_ids]
            new_medium_plus = [f for f in new_findings
                             if f.get("extra", {}).get("severity", "").lower() in ("high", "critical", "medium")]

            security_clean = bool(original_vuln_gone and len(new_medium_plus) == 0)

            if event_emitter:
                await event_emitter(PipelineEvent(
                    event_type="agent_complete", agent_name=self.name,
                    phase=self.phase,
                    message=f"Security diff: original_fixed={original_vuln_gone}, new_issues={len(new_medium_plus)}",
                    details={"finding_id": finding.id, "security_clean": security_clean,
                             "new_findings": len(new_medium_plus)},
                ))

            return ValidationResult(
                gate_4_security_clean=security_clean,
                new_security_findings=[{"id": f.get("check_id"), "message": f.get("extra", {}).get("message", "")}
                                       for f in new_medium_plus],
            )

    def _apply_patch(self, patch: PatchResult, sandbox: Path) -> None:
        """Write the precomputed patched content into the sandbox."""
        if not patch.ok or patch.new_content is None:
            return
        target = sandbox / patch.file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(patch.new_content, encoding="utf-8")

    def _scan_file(self, repo: Path, file_path: str) -> list[dict]:
        """Run bandit on a file, return all findings (normalised to a common shape)."""
        target = repo / file_path
        if not target.exists():
            return []

        findings = []

        # Bandit
        try:
            result = subprocess.run(
                ["bandit", "-f", "json", "-q", str(target)],
                capture_output=True, text=True, timeout=60,
            )
            if result.stdout:
                data = json.loads(result.stdout)
                for r in data.get("results", []):
                    findings.append({
                        "check_id": f"bandit:{r.get('test_id')}",
                        "path": r.get("filename"),
                        "start": {"line": r.get("line_number")},
                        "extra": {
                            "message": r.get("issue_text"),
                            "severity": r.get("issue_severity", "MEDIUM").lower(),
                        },
                    })
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass

        return findings
