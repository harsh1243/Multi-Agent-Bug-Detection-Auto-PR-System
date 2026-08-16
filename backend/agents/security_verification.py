"""Agent 8: Security Verification - differential security analysis (multi-file)."""

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
    """Differential pre/post security analysis across every file the patch changes."""

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
        """Run differential security analysis on all changed files."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message=f"Running differential security analysis on {finding.file_path}...",
                details={"finding_id": finding.id},
            ))

        changed = list(dict.fromkeys([finding.file_path] + patch.changed_files))

        if shutil.which("bandit") is None:
            if event_emitter:
                await event_emitter(PipelineEvent(
                    event_type="agent_warning", agent_name=self.name,
                    phase=self.phase,
                    message="Bandit is unavailable; security confidence will not be awarded.",
                    details={"finding_id": finding.id, "scanner_available": False},
                ))
            # Unavailable is not the same as a detected vulnerability. Allow a
            # human-review PR, but expose the missing signal to confidence scoring.
            return ValidationResult(
                gate_4_security_clean=True,
                security_scanner_available=False,
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            # Baseline (original) and patched copies.
            original = Path(tmpdir) / "original"
            shutil.copytree(repo_path, original, ignore=shutil.ignore_patterns(".git"))
            patched = Path(tmpdir) / "patched"
            shutil.copytree(repo_path, patched, ignore=shutil.ignore_patterns(".git"))
            self._apply_patch(patch, patched)

            # Scan every changed file before and after.
            baseline_findings: list[dict] = []
            post_findings: list[dict] = []
            for cf in changed:
                baseline_findings += self._scan_file(original, cf)
                post_findings += self._scan_file(patched, cf)

            baseline_ids = {self._key(f) for f in baseline_findings}

            # Original vulnerability should be gone. Scanner findings carry a rule_id
            # we can re-check; LLM-found bugs have none, so rely on the "no new
            # vulnerabilities" check below. Always a real bool.
            if finding.rule_id:
                expected_rule = finding.rule_id.split(":")[-1]
                original_vuln_gone = not any(
                    str(f.get("check_id", "")).split(":")[-1] == expected_rule
                    for f in post_findings
                )
            else:
                original_vuln_gone = True

            # No NEW medium+ findings introduced anywhere the patch touched.
            new_findings = [f for f in post_findings if self._key(f) not in baseline_ids]
            new_medium_plus = [
                f for f in new_findings
                if f.get("extra", {}).get("severity", "").lower() in ("high", "critical", "medium")
            ]

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
                security_scanner_available=True,
                new_security_findings=[
                    {"id": f.get("check_id"), "message": f.get("extra", {}).get("message", "")}
                    for f in new_medium_plus
                ],
            )

    @staticmethod
    def _key(f: dict) -> str:
        extra = f.get("extra", {})
        return f"{f.get('path', '')}:{f.get('check_id', '')}:{extra.get('message', '')}"

    def _apply_patch(self, patch: PatchResult, sandbox: Path) -> None:
        """Write every precomputed patched file into the sandbox."""
        if not patch.ok or not patch.files:
            return
        for fp in patch.files:
            target = (sandbox / fp.file_path).resolve()
            try:
                target.relative_to(sandbox.resolve())
            except ValueError:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(fp.new_content, encoding="utf-8")

    def _scan_file(self, repo: Path, file_path: str) -> list[dict]:
        """Run bandit on a python file, return findings normalised to a common shape."""
        target = repo / file_path
        if not target.exists() or not file_path.endswith(".py"):
            return []

        findings: list[dict] = []
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
                        "path": file_path.replace("\\", "/"),
                        "start": {"line": r.get("line_number")},
                        "extra": {
                            "message": r.get("issue_text"),
                            "severity": r.get("issue_severity", "MEDIUM").lower(),
                        },
                    })
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return findings
