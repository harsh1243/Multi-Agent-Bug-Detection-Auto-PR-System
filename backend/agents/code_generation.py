"""Agent 6: Code Generation — surgical, minimal-diff patches per file.

Generates one patch per epicenter file that fixes ALL of that file's findings in a
single call, using anchored SEARCH/REPLACE edits (never whole-file regeneration).
Returns a structured ``PatchResult`` with the new content and a real unified diff.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from config import settings
from models import Finding, PatchResult, PipelineEvent
from utils import patcher
from utils.llm_client import llm, LLMError


_EDIT_INSTRUCTIONS = """You fix code with MINIMAL, SURGICAL edits. Output ONLY anchored
search/replace blocks — never the whole file, never reformatted code. Each block:

<<<<<<< SEARCH
<exact code that currently exists, copied verbatim incl. indentation>
=======
<the replacement code>
>>>>>>> REPLACE

Rules:
- The SEARCH text must match the current file EXACTLY and be unique (include enough
  surrounding lines to be unambiguous, but no more than needed).
- Change only what is required to fix the issue. Do not touch unrelated lines,
  imports, whitespace, or formatting.
- Preserve the file's existing style and indentation.
- You may output multiple blocks. Output nothing except the blocks."""


class CodeGenerationAgent:
    """Generates atomic patch sets grouped by epicenter file."""

    def __init__(self):
        self.name = "Code Generation"
        self.phase = "phase_4_fix_validate"

    async def generate_file_patch(
        self,
        file_path: str,
        findings: list[Finding],
        repo_path: str,
        fix_strategy: str = "",
        event_emitter: Optional[Any] = None,
    ) -> PatchResult:
        """Generate one minimal patch that fixes every finding in ``file_path``."""
        await self._emit(
            event_emitter, "agent_start",
            f"Generating minimal patch for {file_path} ({len(findings)} issue(s))...",
            details={"file": file_path, "issues": len(findings)},
        )

        abs_path = Path(repo_path) / file_path
        if not abs_path.exists():
            return PatchResult(file_path=file_path, errors=[f"File not found: {file_path}"])

        try:
            original = abs_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return PatchResult(file_path=file_path, errors=[f"Could not read file: {e}"])

        prompt = self._build_prompt(file_path, original, findings, fix_strategy)
        try:
            raw = await llm.generate_code(
                prompt, system=_EDIT_INSTRUCTIONS,
                max_tokens=settings.llm_token_budget_code_gen,
            )
        except LLMError as e:
            return PatchResult(file_path=file_path, errors=[f"LLM error: {e}"])

        result = self._build_result(file_path, original, raw)
        await self._emit(
            event_emitter, "agent_complete",
            f"Patch for {file_path}: {result.edits_applied}/{result.edits_total} edits applied"
            + ("" if result.ok else f" — {'; '.join(result.errors)[:160]}"),
            details={"file": file_path, "ok": result.ok, "applied": result.edits_applied},
        )
        return result

    async def regenerate_with_error(
        self,
        file_path: str,
        findings: list[Finding],
        repo_path: str,
        previous: PatchResult,
        error_output: str,
        event_emitter: Optional[Any] = None,
    ) -> PatchResult:
        """Retry patch generation, feeding back apply/validation errors."""
        await self._emit(
            event_emitter, "agent_retry",
            f"Retrying patch for {file_path} with feedback...",
            details={"file": file_path, "error": error_output[:200]},
        )

        abs_path = Path(repo_path) / file_path
        try:
            original = abs_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            return PatchResult(file_path=file_path, errors=[f"Could not read file: {e}"])

        feedback = "\n".join(filter(None, [
            "Your previous edits did not work. Reasons:",
            "; ".join(previous.errors) if previous.errors else "",
            error_output.strip()[:1500] if error_output else "",
            "Produce corrected anchored SEARCH/REPLACE blocks. Re-copy the SEARCH text "
            "EXACTLY from the current file shown below.",
        ]))
        prompt = self._build_prompt(file_path, original, findings, "", extra=feedback)

        try:
            raw = await llm.generate_code(
                prompt, system=_EDIT_INSTRUCTIONS,
                max_tokens=settings.llm_token_budget_code_gen,
            )
        except LLMError as e:
            return PatchResult(file_path=file_path, errors=[f"LLM error: {e}"])

        return self._build_result(file_path, original, raw)

    # ── Internals ─────────────────────────────────────────────────────
    def _build_prompt(
        self, file_path: str, original: str, findings: list[Finding],
        fix_strategy: str, extra: str = "",
    ) -> str:
        issues = []
        for i, f in enumerate(findings, start=1):
            parts = [
                f"{i}. [{f.issue_class.value}/{f.severity.value}] {f.title}"
                + (f" (line {f.line_number})" if f.line_number else ""),
                f"   What's wrong: {f.description}" if f.description else "",
                f"   Root cause: {f.root_cause}" if f.root_cause else "",
                f"   Suggested fix: {f.suggested_fix}" if f.suggested_fix else "",
            ]
            issues.append("\n".join(p for p in parts if p))
        issues_text = "\n".join(issues)

        numbered = self._number_lines(original)
        blocks = [
            f"Fix the following issue(s) in `{file_path}` with minimal anchored edits.",
            "",
            "Issues to fix:",
            issues_text,
        ]
        if fix_strategy:
            blocks += ["", f"Overall strategy: {fix_strategy}"]
        if extra:
            blocks += ["", extra]
        blocks += [
            "",
            "Current file (with line numbers for reference — do NOT include the numbers in SEARCH text):",
            "```",
            numbered,
            "```",
        ]
        return "\n".join(blocks)

    def _build_result(self, file_path: str, original: str, raw: str) -> PatchResult:
        edits = patcher.parse_edits(raw)
        applied = patcher.apply_edits(original, edits)
        diff = patcher.unified_diff(original, applied.new_content, file_path) if applied.ok else ""
        return PatchResult(
            file_path=file_path,
            ok=applied.ok,
            new_content=applied.new_content if applied.ok else None,
            unified_diff=diff,
            edits_applied=applied.applied,
            edits_total=applied.total,
            errors=applied.errors,
            raw_response=raw,
        )

    @staticmethod
    def _number_lines(code: str) -> str:
        return "\n".join(f"{i+1}: {ln}" for i, ln in enumerate(code.splitlines()))

    async def _emit(self, emitter, event_type: str, message: str, details: dict | None = None) -> None:
        if emitter:
            await emitter(PipelineEvent(
                event_type=event_type, agent_name=self.name, phase=self.phase,
                message=message, details=details or {},
            ))
