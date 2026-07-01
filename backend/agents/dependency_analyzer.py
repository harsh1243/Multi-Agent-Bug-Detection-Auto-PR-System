"""Agent 2: Dependency Analyzer - scans for CVEs and outdated packages."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional

from models import Finding, IssueClass, FindingSeverity, PipelineEvent


class DependencyAnalyzerAgent:
    """Scans dependencies for CVEs and outdated packages."""

    def __init__(self):
        self.name = "Dependency Analyzer"
        self.phase = "phase_1_discovery"

    async def run(self, repo_path: str, event_emitter: Optional[Any] = None) -> list[Finding]:
        """Run pip-audit, npm audit, and safety checks."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message="Scanning dependencies for CVEs...",
            ))

        findings = []
        repo = Path(repo_path)

        # Python: pip-audit
        if (repo / "requirements.txt").exists():
            findings.extend(await self._run_pip_audit(repo))

        # Python: safety check
        if (repo / "requirements.txt").exists() or (repo / "pyproject.toml").exists():
            findings.extend(await self._run_safety(repo))

        # Node.js: npm audit
        if (repo / "package.json").exists():
            findings.extend(await self._run_npm_audit(repo))

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_complete", agent_name=self.name,
                phase=self.phase, message=f"Dependency scan complete: {len(findings)} issues found.",
                details={"findings": len(findings)},
            ))

        return findings

    async def _run_pip_audit(self, repo: Path) -> list[Finding]:
        findings = []
        try:
            result = subprocess.run(
                ["pip-audit", "--requirement", str(repo / "requirements.txt"),
                 "--format=json", "--desc"],
                capture_output=True, text=True, timeout=60, cwd=str(repo),
            )
            if result.returncode in (0, 1) and result.stdout:
                data = json.loads(result.stdout)
                for dep in data.get("dependencies", []):
                    for vuln in dep.get("vulns", []):
                        findings.append(Finding(
                            issue_class=IssueClass.SECURITY_VULNERABILITY,
                            severity=FindingSeverity.HIGH,
                            title=f"CVE in {dep.get('name', 'unknown')}: {vuln.get('id', 'CVE-?')}",
                            description=vuln.get("description", "Known vulnerability in dependency"),
                            file_path="requirements.txt",
                            tool_source="pip-audit",
                            rule_id=vuln.get("id"),
                        ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return findings

    async def _run_safety(self, repo: Path) -> list[Finding]:
        findings = []
        try:
            result = subprocess.run(
                ["safety", "check", "--json"],
                capture_output=True, text=True, timeout=60, cwd=str(repo),
            )
            if result.stdout:
                data = json.loads(result.stdout)
                for vuln in data if isinstance(data, list) else data.get("vulnerabilities", []):
                    findings.append(Finding(
                        issue_class=IssueClass.SECURITY_VULNERABILITY,
                        severity=FindingSeverity.HIGH,
                        title=f"Safety: {vuln.get('package_name', 'unknown')} vulnerability",
                        description=vuln.get("vulnerability_spec", ""),
                        file_path="requirements.txt",
                        tool_source="safety",
                    ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return findings

    async def _run_npm_audit(self, repo: Path) -> list[Finding]:
        findings = []
        try:
            result = subprocess.run(
                ["npm", "audit", "--json"],
                capture_output=True, text=True, timeout=60, cwd=str(repo),
            )
            if result.stdout:
                data = json.loads(result.stdout)
                advisories = data.get("advisories", {})
                for adv_id, adv in advisories.items():
                    sev_map = {"critical": FindingSeverity.CRITICAL, "high": FindingSeverity.HIGH,
                               "moderate": FindingSeverity.MEDIUM, "low": FindingSeverity.LOW}
                    findings.append(Finding(
                        issue_class=IssueClass.SECURITY_VULNERABILITY,
                        severity=sev_map.get(adv.get("severity"), FindingSeverity.MEDIUM),
                        title=f"npm: {adv.get('module_name', 'unknown')} - {adv.get('title', '')}",
                        description=adv.get("overview", ""),
                        file_path="package.json",
                        tool_source="npm-audit",
                        rule_id=str(adv_id),
                    ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return findings
