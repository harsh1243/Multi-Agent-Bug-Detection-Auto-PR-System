"""Agent 6: Code Generation — surgical, minimal-diff patches (cross-file capable).

Generates a patch that fixes ALL of an epicenter file's findings in a single call,
using anchored SEARCH/REPLACE edits (never whole-file regeneration). The model is
also shown the epicenter file's immediate dependencies/dependents, so it can fix a
bug at its true ROOT CAUSE in another file when that is where the fix belongs.

Each edit may be tagged with ``### FILE: <path>``; untagged edits target the
epicenter file. The returned ``PatchResult`` can therefore span multiple files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from config import settings
from models import Finding, FilePatch, PatchResult, PipelineEvent
from utils import patcher
from utils.llm_client import llm, LLMError


# Related files larger than this are skipped: anchored SEARCH/REPLACE needs the full
# file in-context to match against, and huge files blow the prompt budget.
_MAX_RELATED_BYTES = 20_000


_EDIT_INSTRUCTIONS = """You fix code with MINIMAL, SURGICAL edits. Output ONLY anchored
search/replace blocks — never a whole file, never reformatted code. Each block:

### FILE: <exact path of the file this edit changes>
<<<<<<< SEARCH
<exact code that currently exists, copied verbatim incl. indentation>
=======
<the replacement code>
>>>>>>> REPLACE

Rules:
- Prefer fixing the bug at its ROOT CAUSE. If the true cause lives in one of the
  other files shown, edit THAT file (tag the block with its path). Otherwise edit
  the primary file. You may edit more than one file.
- Precede EVERY block with a `### FILE:` line naming the file it changes, using one
  of the exact paths shown below.
- The SEARCH text must match that file EXACTLY and be unique (include enough
  surrounding lines to be unambiguous, but no more than needed).
- Change only what is required. Do not touch unrelated lines, imports, whitespace,
  or formatting. Output nothing except `### FILE:` lines and the blocks."""


class CodeGenerationAgent:
    """Generates atomic, possibly cross-file patch sets for an epicenter file."""

    def __init__(self):
        self.name = "Code Generation"
        self.phase = "phase_4_fix_validate"

    async def generate_file_patch(
        self,
        file_path: str,
        findings: list[Finding],
        repo_path: str,
        fix_strategy: str = "",
        related_files: Optional[list[str]] = None,
        event_emitter: Optional[Any] = None,
    ) -> PatchResult:
        """Generate one minimal patch that fixes every finding in ``file_path``
        (optionally editing a related file where the real root cause lives)."""
        await self._emit(
            event_emitter, "agent_start",
            f"Generating minimal patch for {file_path} ({len(findings)} issue(s))...",
            details={"file": file_path, "issues": len(findings)},
        )

        if not (Path(repo_path) / file_path).exists():
            return PatchResult(file_path=file_path, errors=[f"File not found: {file_path}"])

        file_contents = self._collect_files(file_path, related_files or [], repo_path)
        if file_path not in file_contents:
            return PatchResult(file_path=file_path, errors=[f"Could not read file: {file_path}"])

        prompt = self._build_prompt(file_path, file_contents, findings, fix_strategy)
        try:
            raw = await llm.generate_code(
                prompt, system=_EDIT_INSTRUCTIONS,
                max_tokens=settings.llm_token_budget_code_gen,
            )
        except LLMError as e:
            return PatchResult(file_path=file_path, errors=[f"LLM error: {e}"])

        result = self._build_result(file_path, file_contents, raw)
        touched = ", ".join(result.changed_files) or file_path
        tail = "" if result.ok else f" — {'; '.join(result.errors)[:160]}"
        await self._emit(
            event_emitter, "agent_complete",
            f"Patch for {file_path}: {result.edits_applied}/{result.edits_total} edits "
            f"across {touched}{tail}",
            details={"file": file_path, "ok": result.ok, "applied": result.edits_applied,
                     "files": result.changed_files},
        )
        return result

    async def regenerate_with_error(
        self,
        file_path: str,
        findings: list[Finding],
        repo_path: str,
        previous: PatchResult,
        error_output: str,
        related_files: Optional[list[str]] = None,
        event_emitter: Optional[Any] = None,
    ) -> PatchResult:
        """Retry patch generation, feeding back apply/validation errors."""
        await self._emit(
            event_emitter, "agent_retry",
            f"Retrying patch for {file_path} with feedback...",
            details={"file": file_path, "error": error_output[:200]},
        )

        file_contents = self._collect_files(file_path, related_files or [], repo_path)
        if file_path not in file_contents:
            return PatchResult(file_path=file_path, errors=[f"Could not read file: {file_path}"])

        feedback = "\n".join(filter(None, [
            "Your previous edits did not work. Reasons:",
            "; ".join(previous.errors) if previous.errors else "",
            error_output.strip()[:1500] if error_output else "",
            "Produce corrected anchored SEARCH/REPLACE blocks. Re-copy each SEARCH text "
            "EXACTLY from the current file contents shown below, and tag every block with "
            "its `### FILE:` path.",
        ]))
        prompt = self._build_prompt(file_path, file_contents, findings, "", extra=feedback)

        try:
            raw = await llm.generate_code(
                prompt, system=_EDIT_INSTRUCTIONS,
                max_tokens=settings.llm_token_budget_code_gen,
            )
        except LLMError as e:
            return PatchResult(file_path=file_path, errors=[f"LLM error: {e}"])

        return self._build_result(file_path, file_contents, raw)

    # ── Internals ─────────────────────────────────────────────────────
    def _collect_files(self, primary: str, related: list[str], repo_path: str) -> dict[str, str]:
        """Read the primary file plus any small-enough related files, keyed by path."""
        contents: dict[str, str] = {}
        root = Path(repo_path)
        try:
            contents[primary] = (root / primary).read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return contents
        for rel in related:
            if rel == primary or rel in contents:
                continue
            p = root / rel
            try:
                if p.exists() and p.stat().st_size <= _MAX_RELATED_BYTES:
                    contents[rel] = p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
        return contents

    def _build_prompt(
        self, file_path: str, file_contents: dict[str, str], findings: list[Finding],
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

        blocks = [
            f"Fix the following issue(s) originating in `{file_path}` with minimal anchored edits.",
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
            f"PRIMARY FILE — `{file_path}` (edit here unless the root cause is elsewhere;",
            "line numbers are for reference only — do NOT include them in SEARCH text):",
            f"### FILE: {file_path}",
            "```",
            self._number_lines(file_contents[file_path]),
            "```",
        ]

        related = [p for p in file_contents if p != file_path]
        if related:
            blocks += [
                "",
                "RELATED FILES (this file's dependencies/dependents — edit one of these "
                "ONLY if the true root cause lives there):",
            ]
            for rp in related:
                blocks += [
                    f"### FILE: {rp}",
                    "```",
                    self._number_lines(file_contents[rp]),
                    "```",
                    "",
                ]
        return "\n".join(blocks)

    def _build_result(self, primary_path: str, file_contents: dict[str, str], raw: str) -> PatchResult:
        grouped = patcher.parse_file_edits(raw, primary_path)
        # Model-emitted paths may differ in separators/casing — map to real keys.
        norm_lookup = {self._norm(k): k for k in file_contents}

        file_patches: list[FilePatch] = []
        errors: list[str] = []
        combined: list[str] = []
        applied_total = 0
        edits_total = 0

        for path, edits in grouped.items():
            key = path if path in file_contents else norm_lookup.get(self._norm(path))
            if key is None:
                errors.append(f"Edit targets unknown file '{path}' — ignored.")
                continue
            original = file_contents[key]
            ar = patcher.apply_edits(original, edits)
            edits_total += ar.total
            applied_total += ar.applied
            if ar.errors:
                errors.extend(f"{key}: {e}" for e in ar.errors)
            if ar.ok and ar.applied > 0:
                diff = patcher.unified_diff(original, ar.new_content, key)
                file_patches.append(FilePatch(
                    file_path=key, new_content=ar.new_content, unified_diff=diff,
                    edits_applied=ar.applied, edits_total=ar.total,
                ))
                combined.append(diff)

        return PatchResult(
            file_path=primary_path,
            ok=len(file_patches) > 0,
            files=file_patches,
            unified_diff="".join(combined),
            edits_applied=applied_total,
            edits_total=edits_total,
            errors=errors,
            raw_response=raw,
        )

    @staticmethod
    def _norm(p: str) -> str:
        return p.replace("\\", "/").strip().lstrip("./").lower()

    @staticmethod
    def _number_lines(code: str) -> str:
        return "\n".join(f"{i+1}: {ln}" for i, ln in enumerate(code.splitlines()))

    async def _emit(self, emitter, event_type: str, message: str, details: dict | None = None) -> None:
        if emitter:
            await emitter(PipelineEvent(
                event_type=event_type, agent_name=self.name, phase=self.phase,
                message=message, details=details or {},
            ))
