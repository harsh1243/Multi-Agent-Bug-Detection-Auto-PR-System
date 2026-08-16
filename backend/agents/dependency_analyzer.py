"""Agent 2: Dependency Analyzer - scans for CVEs and outdated packages."""

from __future__ import annotations

import asyncio
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

        repo = Path(repo_path)

        # Independent scanners — run them concurrently.
        scanners = []
        requirement_files = self._find_requirement_files(repo)
        scanners.extend(self._run_pip_audit(repo, req) for req in requirement_files)
        if requirement_files or (repo / "pyproject.toml").exists():
            scanners.append(self._run_safety(repo))
        if (repo / "package.json").exists():
            scanners.append(self._run_npm_audit(repo))

        findings = []
        for batch in await asyncio.gather(*scanners):
            findings.extend(batch)

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_complete", agent_name=self.name,
                phase=self.phase, message=f"Dependency scan complete: {len(findings)} issues found.",
                details={"findings": len(findings)},
            ))

        return findings

    @staticmethod
    def _find_requirement_files(repo: Path) -> list[Path]:
        found: list[Path] = []
        for path in repo.rglob("requirements*.txt"):
            if set(path.relative_to(repo).parts) & {"venv", ".venv", "node_modules"}:
                continue
            found.append(path)
        return sorted(found)[:10]

    async def _run_pip_audit(self, repo: Path, requirement_file: Path) -> list[Finding]:
        findings = []
        try:
            result = await asyncio.to_thread(
                subprocess.run,
                ["pip-audit", "--requirement", str(requirement_file),
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
                            file_path=requirement_file.relative_to(repo).as_posix(),
                            tool_source="pip-audit",
                            rule_id=vuln.get("id"),
                        ))
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            pass
        return findings

    async def _run_safety(self, repo: Path) -> list[Finding]:
        findings = []
        try:
            result = await asyncio.to_thread(
                subprocess.run,
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
            result = await asyncio.to_thread(
                subprocess.run,
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
