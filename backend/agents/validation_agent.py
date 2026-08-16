"""Agent 7: Validation Agent - runs the 4-gate validation pipeline.

Applies the already-computed patch (PatchResult.new_content) to a sandbox copy and
runs: AST syntax → tests + static analysis → regression → (security is done by the
Security Verification agent). Honestly reports when a repo has no test suite instead
of treating "no tests collected" as a failure.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional

from config import settings
from knowledge_graph import KnowledgeGraph
from models import Finding, PatchResult, ValidationResult, PipelineEvent
from utils.syntax_check import check_cpp_syntax, find_cpp_compiler, C_EXTS, CPP_EXTS, HEADER_EXTS

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
        result.test_runner_available = importlib.util.find_spec("pytest") is not None

        with tempfile.TemporaryDirectory() as tmpdir:
            sandbox = Path(tmpdir) / "repo"
            shutil.copytree(repo_path, sandbox, ignore=shutil.ignore_patterns(".git", "__pycache__", "*.pyc"))

            # Apply the precomputed new content (no re-parsing of markdown).
            if not self._write_patch(patch, sandbox):
                result.test_failures.append("Failed to write patched file to sandbox")
                return result

            # Gate 1: AST syntax (every changed file)
            result.gate_1_ast_valid = self._gate1_ast(sandbox, patch.changed_files or [patch.file_path])
            if not result.gate_1_ast_valid:
                result.test_failures.append("Gate 1: AST syntax error in patched code")

            # Gate 2: tests + static analysis
            if result.gate_1_ast_valid:
                changed = patch.changed_files or [finding.file_path]
                if repo_has_tests:
                    if result.test_runner_available:
                        pytest_ok, pytest_out, no_tests = self._gate2_pytest(sandbox, changed)
                    else:
                        pytest_ok, pytest_out, no_tests = (
                            False, "pytest is required but is not installed.", False,
                        )
                    if no_tests:
                        # No targeted test matched. When full-suite validation is
                        # enabled, continue to Gate 3 instead of treating the
                        # repository as if it had no tests.
                        if not settings.validation_run_full_suite:
                            result.tests_available = False
                        pytest_ok = True
                else:
                    pytest_ok, pytest_out = True, "Repository has no test suite — static analysis only."

                bandit_ok, bandit_out, bandit_available = self._gate2_bandit(
                    sandbox, changed,
                )
                result.static_analysis_available = bandit_available

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
                (
                    result.gate_3_no_regressions,
                    result.full_suite_run,
                ) = self._gate3_regressions(
                    sandbox, patch.changed_files or [finding.file_path], kg,
                )
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
        """Write every changed file's content into the sandbox."""
        if not patch.ok or not patch.files:
            return False
        try:
            for fp in patch.files:
                target = (sandbox / fp.file_path).resolve()
                target.relative_to(sandbox.resolve())
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(fp.new_content, encoding="utf-8")
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
    def _gate1_ast(self, sandbox: Path, file_paths: list[str]) -> bool:
        """Every changed file must exist and be syntactically valid.

        .py files are checked with ast.parse; C/C++ files with the system
        compiler's -fsyntax-only when one is available. Other languages pass on
        existence alone.
        """
        cpp_all = C_EXTS | CPP_EXTS | HEADER_EXTS
        for file_path in file_paths:
            target = sandbox / file_path
            if not target.exists():
                return False
            suffix = Path(file_path).suffix.lower()
            if file_path.endswith(".py"):
                try:
                    ast.parse(target.read_text(encoding="utf-8", errors="ignore"))
                except (SyntaxError, UnicodeDecodeError):
                    return False
            elif suffix in cpp_all and find_cpp_compiler():
                if check_cpp_syntax(target, sandbox):
                    return False
        return True

    # ── Test-file discovery ───────────────────────────────────────────
    def _find_test_files(self, sandbox: Path, source_files: list[str]) -> list[str]:
        """Locate test files for the given source files anywhere in the repo
        (including ``tests/`` directories): test_<stem>.py and <stem>_test.py."""
        found: list[str] = []
        for src in source_files:
            stem = Path(src).stem
            if not stem or stem.startswith("test_") or stem.endswith("_test"):
                continue
            for pattern in (f"test_{stem}.py", f"{stem}_test.py"):
                for match in sandbox.rglob(pattern):
                    rel = str(match.relative_to(sandbox))
                    # Skip vendored/virtualenv copies.
                    parts = set(Path(rel).parts)
                    if parts & {"node_modules", "venv", ".venv", "site-packages"}:
                        continue
                    if rel not in found:
                        found.append(rel)
        return found

    def _gate2_pytest(self, sandbox: Path, changed_files: list[str]) -> tuple[bool, str, bool]:
        """Run pytest scoped to the changed files' test modules.

        Looks for test files matching every changed file (not just the finding's
        epicenter) anywhere in the repo. Falls back to ``-k`` stem matching only
        when no test files are found by name.

        Returns (passed, output, no_tests_collected).
        """
        test_files = self._find_test_files(sandbox, changed_files)
        if test_files:
            cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--tb=short", "--no-header"] + test_files
        else:
            stems = sorted({Path(f).stem for f in changed_files if Path(f).stem})
            if not stems:
                return True, "No changed modules to test.", True
            cmd = [sys.executable, "-m", "pytest", "-x", "-q", "-k", " or ".join(stems),
                   "--tb=short", "--no-header"]
        try:
            result = subprocess.run(
                cmd,
                cwd=str(sandbox), capture_output=True, text=True,
                timeout=settings.validation_sandbox_timeout,
            )
            out = result.stdout + result.stderr
            if result.returncode == PYTEST_NO_TESTS:
                return True, "No tests matched the changed files.", True
            return result.returncode == 0, out, False
        except subprocess.TimeoutExpired:
            return False, "pytest timed out.", False
        except FileNotFoundError:
            return True, "pytest not available.", True

    def _gate2_bandit(self, sandbox: Path, file_paths: list[str]) -> tuple[bool, str, bool]:
        """Scan every changed Python file, not just the epicenter.

        A cross-file patch can introduce a vulnerability in the root-cause file,
        so scanning only the primary file would let it through the gate.
        """
        targets = [p for p in file_paths if p.endswith(".py")]
        if not targets:
            return True, "", False
        if shutil.which("bandit") is None:
            return True, "bandit is not installed; security signal unavailable.", False
        try:
            result = subprocess.run(
                ["bandit", "-f", "json", "-q"] + [str(sandbox / p) for p in targets],
                capture_output=True, text=True, timeout=60,
            )
            if not result.stdout:
                return True, "", True
            data = json.loads(result.stdout)
            issues = data.get("results", [])
            high = [i for i in issues if i.get("issue_severity") in ("HIGH", "CRITICAL")]
            return len(high) == 0, json.dumps(high[:5]), True
        except (subprocess.TimeoutExpired, json.JSONDecodeError, FileNotFoundError):
            return True, "bandit could not complete; security signal unavailable.", False

    def _gate3_regressions(
        self, sandbox: Path, changed_files: list[str], kg: KnowledgeGraph,
    ) -> tuple[bool, bool]:
        if settings.validation_run_full_suite:
            cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--tb=short", "--no-header"]
            full_suite = True
        else:
            # Focused fallback for unusually expensive repositories.
            affected: list[str] = []
            for changed in changed_files:
                blast = kg.blast_radius(
                    changed, hops=settings.blast_radius_default_hops,
                )
                for path in [changed] + blast["affected_files"]:
                    if path not in affected:
                        affected.append(path)
            test_modules = self._find_test_files(sandbox, affected)
            if not test_modules:
                return True, False
            cmd = [sys.executable, "-m", "pytest", "-x", "-q", "--tb=short", "--no-header"] + test_modules
            full_suite = False
        try:
            result = subprocess.run(
                cmd,
                cwd=str(sandbox), capture_output=True, text=True,
                timeout=settings.validation_sandbox_timeout,
            )
            return result.returncode in (0, PYTEST_NO_TESTS), full_suite
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False, full_suite
