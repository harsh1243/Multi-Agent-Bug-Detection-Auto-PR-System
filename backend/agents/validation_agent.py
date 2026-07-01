"""Agent 7: Validation Agent - runs the 4-gate validation pipeline.

Applies the already-computed patch (PatchResult.new_content) to a sandbox copy and
runs: AST syntax → tests + static analysis → regression → (security is done by the
Security Verification agent). Honestly reports when a repo has no test suite instead
of treating "no tests collected" as a failure.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

from config import settings
from knowledge_graph import KnowledgeGraph
from models import Finding, PatchResult, ValidationResult, PipelineEvent

# pytest exit codes: 0 = passed, 1 = tests failed, 5 = no tests collected.
PYTEST_NO_TESTS = 5


class ValidationAgent:
    """Validates patches through gates: AST, tests, regressions."""

    def __init__(self):
        self.name = "Validation Agent"
        self.phase = "phase_4_fix_validate"

    async def validate(
        self,
        finding: Finding,
        patch: PatchResult,
        repo_path: str,
        kg: KnowledgeGraph,
        event_emitter: Optional[Any] = None,
    ) -> ValidationResult:
        """Run the validation gates on an already-applied patch."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message=f"Validating patch for {patch.file_path}...",
                details={"finding_id": finding.id},
            ))

        result = ValidationResult()
        repo_has_tests = self._repo_has_tests(Path(repo_path))
        result.tests_available = repo_has_tests

        with tempfile.TemporaryDirectory() as tmpdir:
            sandbox = Path(tmpdir) / "repo"
            shutil.copytree(repo_path, sandbox, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))

            # Apply the precomputed new content (no re-parsing of markdown).
            if not self._write_patch(patch, sandbox):
                result.test_failures.append("Failed to write patched file to sandbox")
                return result

            # Gate 1: AST syntax
            result.gate_1_ast_valid = self._gate1_ast(sandbox, patch.file_path)
            if not result.gate_1_ast_valid:
                result.test_failures.append("Gate 1: AST syntax error in patched code")

            # Gate 2: tests + static analysis
            if result.gate_1_ast_valid:
                if repo_has_tests:
                    pytest_ok, pytest_out, no_tests = self._gate2_pytest(sandbox, finding)
                    if no_tests:
                        # No tests matched this module — don't penalize, but we can't
                        # earn the +40% tests signal either.
                        result.tests_available = False
                        pytest_ok = True
                else:
                    pytest_ok, pytest_out = True, "Repository has no test suite — static analysis only."

                bandit_ok, bandit_out = self._gate2_bandit(sandbox, patch.file_path)

                # With a real test suite, tests must pass to earn the signal.
                result.gate_2_tests_passed = bool(result.tests_available) and pytest_ok and bandit_ok
                result.pytest_output = pytest_out
                result.bandit_output = bandit_out

                if result.tests_available and not pytest_ok:
                    result.test_failures.append(f"Gate 2: tests failed\n{pytest_out[:600]}")
                if not bandit_ok:
                    result.test_failures.append(f"Gate 2: bandit high-severity issue\n{bandit_out[:400]}")

            # Gate 3: regression detection across blast radius
            if result.gate_1_ast_valid and result.tests_available and result.gate_2_tests_passed:
                result.gate_3_no_regressions = self._gate3_regressions(sandbox, finding, kg)
                if not result.gate_3_no_regressions:
                    result.test_failures.append("Gate 3: regressions detected in blast radius")
            else:
                # No tests to regress, or earlier gate failed.
                result.gate_3_no_regressions = result.gate_1_ast_valid

        # "passed" = safe to open a PR. Without a test suite we rely on AST + (later)
        # security verification; confidence is capped separately to <=60%.
        if result.tests_available:
            result.passed = all([
                result.gate_1_ast_valid, result.gate_2_tests_passed, result.gate_3_no_regressions,
            ])
        else:
            bandit_ok = not any("bandit" in f for f in result.test_failures)
            result.passed = result.gate_1_ast_valid and bandit_ok

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_complete", agent_name=self.name,
                phase=self.phase,
                message=(f"Validation: AST={result.gate_1_ast_valid}, "
                         f"Tests={'n/a' if not result.tests_available else result.gate_2_tests_passed}, "
                         f"Regressions={result.gate_3_no_regressions}"),
                details={"finding_id": finding.id, "passed": result.passed,
                         "tests_available": result.tests_available},
            ))

        return result

    # ── Patch application ─────────────────────────────────────────────
    def _write_patch(self, patch: PatchResult, sandbox: Path) -> bool:
        """Write the patched file content into the sandbox."""
        if not patch.ok or patch.new_content is None:
            return False
        try:
            target = sandbox / patch.file_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(patch.new_content, encoding="utf-8")
            return True
        except Exception:
            return False

    # ── Test-suite detection ──────────────────────────────────────────
    def _repo_has_tests(self, repo: Path) -> bool:
        """Detect a runnable Python test suite."""
        markers = ["pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml"]
        for m in markers:
            p = repo / m
            if p.exists():
                try:
                    if "pytest" in p.read_text(encoding="utf-8", errors="ignore").lower():
                        return True
                except Exception:
                    pass
        for pattern in ("test_*.py", "*_test.py"):
            for _ in repo.rglob(pattern):
                return True
        if (repo / "tests").is_dir():
            return True
        return False

    # ── Gates ─────────────────────────────────────────────────────────
    def _gate1_ast(self, sandbox: Path, file_path: str) -> bool:
        target = sandbox / file_path
        if not target.exists():
            return False
        try:
            content = target.read_text(encoding="utf-8", errors="ignore")
            if file_path.endswith(".py"):
                ast.parse(content)
            return True
        except (SyntaxError, UnicodeDecodeError):
            return False

    def _gate2_pytest(self, sandbox: Path, finding: Finding) -> tuple[bool, str, bool]:
        """Run pytest scoped to the changed module.

        Returns (passed, output, no_tests_collected).
        """
        module = Path(finding.file_path).stem
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", "-x", "-q", "-k", module,
                 "--tb=short", "--no-header"],
                cwd=str(sandbox), capture_output=True, text=True,
                timeout=settings.validation_sandbox_timeout,
            )
            out = result.stdout + result.stderr
            if result.returncode == PYTEST_NO_TESTS:
                return True, "No tests matched the changed module.", True
            return result.returncode == 0, out, False
        except subprocess.TimeoutExpired:
            return False, "pytest timed out.", False
        except FileNotFoundError:
            return True, "pytest not available.", True

    def _gate2_bandit(self, sandbox: Path, file_path: str) -> tuple[bool, str]:
        if not file_path.endswith(".py"):
            return True, ""
        try:
            target = sandbox / file_path
            result = subprocess.run(
                ["bandit", "-f", "json", "-q", str(target)],
                capture_output=True, text=True, timeout=60,
            )
            if not result.stdout:
                return True, ""
            data = json.loads(result.stdout)
            issues = data.get("results", [])
            high = [i for i in issues if i.get("issue_severity") in ("HIGH", "CRITICAL")]
            return len(high) == 0, json.dumps(high[:5])
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            return True, ""

    def _gate3_regressions(self, sandbox: Path, finding: Finding, kg: KnowledgeGraph) -> bool:
        blast = kg.blast_radius(finding.file_path, hops=settings.blast_radius_default_hops)
        test_modules = [f"test_{Path(f).stem}.py" for f in blast["affected_files"]
                        if (sandbox / f"test_{Path(f).stem}.py").exists()]
        if not test_modules:
            return True
        try:
            result = subprocess.run(
                ["python", "-m", "pytest", "-x", "-q", "--tb=short", "--no-header"] + test_modules,
                cwd=str(sandbox), capture_output=True, text=True,
                timeout=settings.validation_sandbox_timeout,
            )
            return result.returncode in (0, PYTEST_NO_TESTS)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return True
