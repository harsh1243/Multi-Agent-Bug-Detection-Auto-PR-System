"""Agent 1: Repository Mapper - builds the knowledge graph."""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from knowledge_graph import KnowledgeGraph
from models import Finding, IssueClass, FindingSeverity, PipelineEvent
from utils.syntax_check import check_cpp_syntax, find_cpp_compiler


class RepoMapperAgent:
    """Builds a semantic knowledge graph of the repository."""

    def __init__(self):
        self.name = "Repo Mapper"
        self.phase = "phase_1_discovery"

    async def run(self, repo_path: str, repo_name: str, event_emitter: Optional[Any] = None) -> tuple[KnowledgeGraph, list[Finding]]:
        """Build knowledge graph and return structural findings."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message=f"Building knowledge graph for {repo_name}...",
            ))

        kg = KnowledgeGraph(repo_path, repo_name)
        graph = kg.build()

        findings = []

        # Python syntax errors — deterministic, from the real parser (ast.parse
        # during graph construction). Exact line + message, no LLM needed.
        for err in kg.syntax_errors:
            loc = f"line {err['line']}" if err.get("line") else "unknown line"
            findings.append(Finding(
                issue_class=IssueClass.FUNCTIONAL_BUG,
                severity=FindingSeverity.HIGH,
                title=f"Python syntax error: {err['message']}",
                description=(
                    f"`{err['file']}` fails to parse ({loc}): {err['message']}. "
                    f"The module cannot be imported or executed until this is fixed."
                    + (f"\n\n    {err['text']}" if err.get("text") else "")
                ),
                file_path=err["file"],
                line_number=err.get("line"),
                code_snippet=err.get("text") or "",
                tool_source="repo_mapper:ast",
            ))

        # C/C++ syntax errors — deterministic, via `<compiler> -fsyntax-only`.
        # Skipped silently when no compiler is on PATH.
        if find_cpp_compiler():
            findings.extend(await self._check_cpp_files(kg, repo_path))

        # Detect circular dependencies
        try:
            cycles = list(itertools.islice(nx.simple_cycles(graph), 10))
            for cycle in cycles:
                file_nodes = [n for n in cycle if graph.nodes[n].get("type") == "File"]
                if len(file_nodes) >= 2:
                    findings.append(Finding(
                        issue_class=IssueClass.CODE_QUALITY,
                        severity=FindingSeverity.MEDIUM,
                        title=f"Circular dependency detected",
                        description=f"Files form a circular import chain: {' -> '.join(file_nodes)}",
                        file_path=file_nodes[0],
                        tool_source="repo_mapper",
                    ))
        except Exception:
            pass

        # NOTE: the naive "function > 50 lines" heuristic was removed — line count is
        # not a defect. Real code-quality issues (dead code, duplicate logic, etc.) are
        # surfaced by the LLM Bug Hunter with actual evidence, not an arbitrary threshold.

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_complete", agent_name=self.name,
                phase=self.phase,
                message=f"Knowledge graph built: {graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges. {len(findings)} structural findings.",
                details={"nodes": graph.number_of_nodes(), "edges": graph.number_of_edges(), "findings": len(findings)},
            ))

        return kg, findings

    async def _check_cpp_files(self, kg: KnowledgeGraph, repo_path: str) -> list[Finding]:
        """Compiler syntax check for every C/C++ file in the graph."""
        root = Path(repo_path)
        cpp_files = [n for n, a in kg.graph.nodes(data=True)
                     if a.get("type") == "File" and a.get("language") in ("c", "cpp")]
        findings: list[Finding] = []
        results = await asyncio.gather(*[
            asyncio.to_thread(check_cpp_syntax, root / f, root) for f in cpp_files
        ])
        for rel, errors in zip(cpp_files, results):
            for err in errors[:3]:  # a cascade of errors usually has one root cause
                findings.append(Finding(
                    issue_class=IssueClass.FUNCTIONAL_BUG,
                    severity=FindingSeverity.HIGH,
                    title=f"C/C++ syntax error: {err['message'][:80]}",
                    description=(
                        f"`{rel}` fails to compile (line {err['line']}): {err['message']}"
                    ),
                    file_path=rel,
                    line_number=err.get("line"),
                    tool_source="repo_mapper:compiler",
                ))
        return findings
