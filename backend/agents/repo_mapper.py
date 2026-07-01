"""Agent 1: Repository Mapper - builds the knowledge graph."""

from __future__ import annotations

import ast
import os
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from config import settings
from knowledge_graph import KnowledgeGraph
from models import Finding, IssueClass, FindingSeverity, PipelineEvent


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

        # Detect circular dependencies
        try:
            cycles = list(nx.simple_cycles(graph))
            for cycle in cycles[:10]:
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
