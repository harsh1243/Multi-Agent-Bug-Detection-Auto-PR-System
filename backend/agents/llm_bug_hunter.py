"""LLM Bug Hunter — real cross-file bug discovery without external scanners.

Two stages, tiered for cost:
  Stage A (Haiku, cheap): scan every source file and flag *candidate* issues.
  Stage B (Sonnet, strong): for files with candidates, confirm each candidate
      against the full file, discard false positives, and attach a concrete fix.

This replaces the silently-dead semgrep/bandit path (semgrep doesn't run on
Windows and the scanners are usually not installed), so the system actually
finds genuine functional/security/performance/quality bugs.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from config import settings
from knowledge_graph import KnowledgeGraph
from models import Finding, IssueClass, FindingSeverity, PipelineEvent
from utils.llm_client import llm, LLMError


_ISSUE_CLASS = {
    "security_vulnerability": IssueClass.SECURITY_VULNERABILITY,
    "security": IssueClass.SECURITY_VULNERABILITY,
    "functional_bug": IssueClass.FUNCTIONAL_BUG,
    "functional": IssueClass.FUNCTIONAL_BUG,
    "bug": IssueClass.FUNCTIONAL_BUG,
    "performance": IssueClass.PERFORMANCE,
    "code_quality": IssueClass.CODE_QUALITY,
    "quality": IssueClass.CODE_QUALITY,
}

_SEVERITY = {
    "critical": FindingSeverity.CRITICAL,
    "high": FindingSeverity.HIGH,
    "medium": FindingSeverity.MEDIUM,
    "low": FindingSeverity.LOW,
    "info": FindingSeverity.INFO,
}


class LLMBugHunterAgent:
    """Finds real bugs via a Haiku-triage → Sonnet-confirm pipeline."""

    def __init__(self):
        self.name = "Bug Hunter"
        self.phase = "phase_1_discovery"

    async def run(
        self,
        repo_path: str,
        kg: KnowledgeGraph,
        event_emitter: Optional[Any] = None,
    ) -> list[Finding]:
        if not settings.bug_hunter_enabled:
            return []

        await self._emit(event_emitter, "agent_start", "Hunting for real bugs (LLM analysis)...")

        files = self._select_files(repo_path, kg)
        if not files:
            await self._emit(event_emitter, "agent_complete", "Bug Hunter: no source files to analyze.")
            return []

        await self._emit(
            event_emitter, "agent_progress",
            f"Bug Hunter: triaging {len(files)} source files with Haiku...",
            details={"files": len(files)},
        )

        findings: list[Finding] = []
        for idx, rel in enumerate(files, start=1):
            # Throttle to respect the proxy's per-tier rate limit.
            if idx > 1 and settings.bug_hunter_delay_seconds > 0:
                await asyncio.sleep(settings.bug_hunter_delay_seconds)
            abs_path = Path(repo_path) / rel
            try:
                code = abs_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if not code.strip():
                continue

            # ── Stage A: cheap triage ──
            try:
                candidates = await self._triage(rel, code)
            except (LLMError, ValueError) as e:
                await self._emit(
                    event_emitter, "agent_warning",
                    f"Bug Hunter: triage failed for {rel}: {str(e)[:400]}",
                )
                continue

            if not candidates:
                continue

            await self._emit(
                event_emitter, "finding_investigating",
                f"Bug Hunter: confirming {len(candidates)} candidate(s) in {rel} ({idx}/{len(files)})...",
                details={"file": rel, "candidates": len(candidates)},
            )

            # ── Stage B: strong confirmation ──
            try:
                confirmed = await self._confirm(rel, code, candidates)
            except (LLMError, ValueError) as e:
                await self._emit(
                    event_emitter, "agent_warning",
                    f"Bug Hunter: confirmation failed for {rel}: {str(e)[:400]}",
                )
                continue

            for c in confirmed:
                f = self._to_finding(rel, c, code)
                if f is not None:
                    findings.append(f)

        await self._emit(
            event_emitter, "agent_complete",
            f"Bug Hunter: {len(findings)} confirmed issue(s) across {len(files)} files.",
            details={"findings": len(findings)},
        )
        return findings

    # ── File selection ────────────────────────────────────────────────
    def _select_files(self, repo_path: str, kg: KnowledgeGraph) -> list[str]:
        """All source files of supported languages, ranked by importance."""
        langs = set(settings.bug_hunter_languages)
        files = []
        for n, a in kg.graph.nodes(data=True):
            if a.get("type") != "File" or a.get("language") not in langs:
                continue
            size = int(a.get("size", 0) or 0)
            if size == 0 or size > settings.bug_hunter_max_file_bytes:
                continue
            # Importance: in-degree (how many things touch it) + size signal.
            try:
                degree = kg.graph.degree(n)
            except Exception:
                degree = 0
            files.append((n, degree, size))

        # Rank by connectivity (centrality) then size — most-impactful first.
        files.sort(key=lambda t: (t[1], t[2]), reverse=True)
        ordered = [f[0] for f in files]
        cap = settings.bug_hunter_max_files
        return ordered[:cap] if cap and cap > 0 else ordered

    # ── Stage A: triage (Haiku) ───────────────────────────────────────
    async def _triage(self, rel_path: str, code: str) -> list[dict]:
        numbered = self._number_lines(code)
        prompt = f"""You are a senior code reviewer doing a fast triage pass.

File: {rel_path}
```
{numbered}
```

List ONLY genuine, concrete defects you can point to a specific line for. Look for:
- functional_bug: null/None dereference, wrong condition, off-by-one, missing edge case, broken API contract, resource leak
- security_vulnerability: injection (SQL/command/path), unsafe deserialization, hardcoded secret, weak crypto, missing authz
- performance: N+1 query, work inside a hot loop, unbounded memory growth, quadratic blowup
- code_quality: dead code, duplicate logic (only if clearly wrong)

Do NOT report style/formatting, "function is long", naming, or speculative issues.
Return a JSON array (possibly empty) of:
[{{"class":"functional_bug|security_vulnerability|performance|code_quality","severity":"low|medium|high|critical","line":<int>,"title":"<short>","why":"<one sentence>"}}]"""
        data = await llm.call_json(prompt, model_tier="haiku", max_tokens=settings.llm_token_budget_hunt)
        return self._coerce_list(data)

    # ── Stage B: confirm (Sonnet) ─────────────────────────────────────
    async def _confirm(self, rel_path: str, code: str, candidates: list[dict]) -> list[dict]:
        numbered = self._number_lines(code)
        cand_json = "\n".join(
            f'- line {c.get("line", "?")}: [{c.get("class","?")}/{c.get("severity","?")}] '
            f'{c.get("title","")} — {c.get("why","")}'
            for c in candidates
        )
        prompt = f"""You are a staff engineer verifying triage findings before any code is changed.

File: {rel_path}
```
{numbered}
```

Candidate issues from a fast triage pass:
{cand_json}

For EACH candidate, decide if it is a REAL defect (not a false positive). Discard anything
speculative, stylistic, or that you cannot justify from the code. For each CONFIRMED issue,
provide a precise root cause and a concrete, minimal fix description (what to change and why).

Return ONLY a JSON array of confirmed issues:
[{{"class":"...","severity":"low|medium|high|critical","line":<int>,
   "title":"<specific>","description":"<what's wrong>","evidence":"<cite the code/why it's real>",
   "root_cause":"<why it exists>","suggested_fix":"<concrete minimal change>"}}]
If none are real, return []."""
        data = await llm.call_json(prompt, model_tier="sonnet", max_tokens=settings.llm_token_budget_investigation)
        return self._coerce_list(data)

    # ── Helpers ───────────────────────────────────────────────────────
    def _to_finding(self, rel_path: str, c: dict, code: str) -> Optional[Finding]:
        cls = _ISSUE_CLASS.get(str(c.get("class", "")).lower())
        if cls is None:
            return None
        sev = _SEVERITY.get(str(c.get("severity", "medium")).lower(), FindingSeverity.MEDIUM)
        line = c.get("line")
        try:
            line = int(line) if line is not None else None
        except (TypeError, ValueError):
            line = None
        snippet = self._snippet(code, line)
        return Finding(
            issue_class=cls,
            severity=sev,
            title=(c.get("title") or "Issue").strip()[:120],
            description=(c.get("description") or c.get("why") or "").strip(),
            file_path=rel_path,
            line_number=line,
            code_snippet=snippet,
            tool_source="llm",
            confidence=0.8,
            root_cause=(c.get("root_cause") or "").strip() or None,
            evidence=(c.get("evidence") or "").strip() or None,
            suggested_fix=(c.get("suggested_fix") or "").strip() or None,
        )

    @staticmethod
    def _coerce_list(data: Any) -> list[dict]:
        if isinstance(data, list):
            return [d for d in data if isinstance(d, dict)]
        if isinstance(data, dict):
            for key in ("issues", "findings", "results", "candidates"):
                if isinstance(data.get(key), list):
                    return [d for d in data[key] if isinstance(d, dict)]
        return []

    @staticmethod
    def _number_lines(code: str, limit: int = 1200) -> str:
        lines = code.splitlines()
        out = [f"{i+1}: {ln}" for i, ln in enumerate(lines[:limit])]
        if len(lines) > limit:
            out.append(f"... ({len(lines) - limit} more lines truncated)")
        return "\n".join(out)

    @staticmethod
    def _snippet(code: str, line: Optional[int], radius: int = 3) -> str:
        if not line:
            return ""
        lines = code.splitlines()
        i = line - 1
        if i < 0 or i >= len(lines):
            return ""
        lo = max(0, i - radius)
        hi = min(len(lines), i + radius + 1)
        return "\n".join(lines[lo:hi])

    async def _emit(self, emitter, event_type: str, message: str, details: dict | None = None) -> None:
        if emitter:
            await emitter(PipelineEvent(
                event_type=event_type, agent_name=self.name, phase=self.phase,
                message=message, details=details or {},
            ))
