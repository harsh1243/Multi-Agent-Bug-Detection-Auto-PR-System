"""Agent 3: Static Analysis - runs Bandit and ESLint.

(Semgrep was removed: it does not run on Windows and the LLM Bug Hunter is now the
primary cross-language detector. Bandit covers Python locally; ESLint covers JS/TS.)
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from models import Finding, IssueClass, FindingSeverity, PipelineEvent


class StaticAnalysisAgent:
    """Runs static analysis tools concurrently."""

    SEVERITY_MAP = {
        "ERROR": FindingSeverity.HIGH, "WARNING": FindingSeverity.MEDIUM,
        "INFO": FindingSeverity.LOW, "critical": FindingSeverity.CRITICAL,
        "high": FindingSeverity.HIGH, "medium": FindingSeverity.MEDIUM,
        "low": FindingSeverity.LOW,
    }

    def __init__(self):
        self.name = "Static Analysis"
        self.phase = "phase_1_discovery"

    async def run(self, repo_path: str, event_emitter: Optional[Any] = None) -> list[Finding]:
        """Run all scanners and aggregate findings."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message="Running static analysis scanners...",
            ))

        findings = []
        findings.extend(await self._run_bandit(repo_path))
        findings.extend(await self._run_eslint(repo_path))

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_complete", agent_name=self.name,
                phase=self.phase, message=f"Static analysis complete: {len(findings)} findings.",
                details={"findings": len(findings)},
            ))

        return findings

    async def _run_bandit(self, repo_path: str) -> list[Finding]:
        findings = []
        try:
            result = subprocess.run(
                ["bandit", "-r", "-f", "json", repo_path],
                capture_output=True, text=True, timeout=120,
            )
            if result.stdout:
                data = json.loads(result.stdout)
                for r in data.get("results", []):
                    sev_map = {"HIGH": FindingSeverity.HIGH, "MEDIUM": FindingSeverity.MEDIUM, "LOW": FindingSeverity.LOW}
                    findings.append(Finding(
                        issue_class=IssueClass.SECURITY_VULNERABILITY,
                        severity=sev_map.get(r.get("issue_severity"), FindingSeverity.MEDIUM),
                        title=f"Bandit: {r.get('test_name', 'Issue')}",
                        description=r.get("issue_text", ""),
                        file_path=r.get("filename", ""),
                        line_number=r.get("line_number"),
                        code_snippet=r.get("code", ""),
                        tool_source="bandit",
                        rule_id=r.get("test_id"),
                    ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return findings

    async def _run_eslint(self, repo_path: str) -> list[Finding]:
        findings = []
        try:
            result = subprocess.run(
                ["npx", "eslint", "--format", "json", "--ext", ".js,.jsx,.ts,.tsx", repo_path],
                capture_output=True, text=True, timeout=120, cwd=repo_path,
            )
            if result.stdout:
                data = json.loads(result.stdout)
                for file_entry in data:
                    for msg in file_entry.get("messages", []):
                        if msg.get("severity", 0) >= 2:  # Error level
                            findings.append(Finding(
                                issue_class=IssueClass.CODE_QUALITY,
                                severity=FindingSeverity.MEDIUM,
                                title=f"ESLint: {msg.get('ruleId', 'error')}",
                                description=msg.get("message", ""),
                                file_path=file_entry.get("filePath", ""),
                                line_number=msg.get("line"),
                                column=msg.get("column"),
                                tool_source="eslint",
                                rule_id=msg.get("ruleId"),
                            ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return findings
