"""Agent 5: Repair Planner - groups fixes by file and orders them by dependency.

One repair item per epicenter file (bundling all of that file's findings), so the
fix loop produces ONE pull request per file instead of one per finding. Security
files are prioritised; remaining files are ordered topologically by the cross-file
dependency graph.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Optional

import networkx as nx

from config import settings
from knowledge_graph import KnowledgeGraph
from models import Finding, RepairPlan, RepairPlanItem, PipelineEvent, IssueClass


class RepairPlannerAgent:
    """Creates a file-grouped, topologically ordered repair plan."""

    def __init__(self):
        self.name = "Repair Planner"
        self.phase = "phase_3_planning"

    async def run(
        self,
        findings: list[Finding],
        kg: KnowledgeGraph,
        event_emitter: Optional[Any] = None,
    ) -> RepairPlan:
        """Generate an ordered, file-grouped repair plan."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase,
                message=f"Planning repairs for {len(findings)} fixable finding(s)...",
            ))

        # ── Group findings by epicenter file ──
        by_file: dict[str, list[Finding]] = defaultdict(list)
        for f in findings:
            by_file[f.file_path].append(f)

        # ── File-level dependency graph (A before B if A's file is in B's blast radius) ──
        dep_graph = nx.DiGraph()
        for fp in by_file:
            dep_graph.add_node(fp)
        for fp, group in by_file.items():
            affected = set()
            for f in group:
                affected.update(f.affected_files)
            for other in by_file:
                if other != fp and other in affected:
                    dep_graph.add_edge(fp, other)  # fix fp before other

        # ── Security files first, then topological within each bucket ──
        def is_security(fp: str) -> bool:
            return any(f.issue_class == IssueClass.SECURITY_VULNERABILITY for f in by_file[fp])

        sec_files = [fp for fp in by_file if is_security(fp)]
        other_files = [fp for fp in by_file if not is_security(fp)]
        ordered_files = self._topo(dep_graph, sec_files) + self._topo(dep_graph, other_files)

        # ── Build one item per file ──
        items: list[RepairPlanItem] = []
        for fp in ordered_files:
            group = sorted(by_file[fp], key=lambda f: f.severity_rank, reverse=True)
            primary = group[0]
            files_to_modify = [fp]
            for f in group:
                for af in f.affected_files[:3]:
                    if af not in files_to_modify:
                        files_to_modify.append(af)

            cache_boost = 0.15 if any(f.similar_past_fixes for f in group) else 0.0
            deps = [d for d in dep_graph.predecessors(fp)] if fp in dep_graph else []

            items.append(RepairPlanItem(
                finding_id=primary.id,
                finding_ids=[f.id for f in group],
                epicenter_file=fp,
                files_to_modify=files_to_modify[:6],
                fix_strategy=self._strategy(group),
                confidence_pre_score=0.5 + cache_boost,
                dependencies=deps,
            ))

        plan = RepairPlan(
            items=items,
            total_confidence_pre_score=sum(i.confidence_pre_score for i in items) / max(len(items), 1),
            estimated_fix_time_seconds=len(items) * 45,
        )

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_complete", agent_name=self.name,
                phase=self.phase,
                message=f"Repair plan: {len(items)} file(s) to fix → {len(items)} PR(s).",
                details={"items": len(items), "files": len(by_file)},
            ))

        return plan

    def _topo(self, graph: nx.DiGraph, nodes: list[str]) -> list[str]:
        """Topologically sort a subset of files; fall back to input order on cycles."""
        if not nodes:
            return []
        try:
            sub = graph.subgraph(nodes)
            return list(nx.topological_sort(sub))
        except (nx.NetworkXError, nx.NetworkXUnfeasible):
            return list(nodes)

    def _strategy(self, group: list[Finding]) -> str:
        """Concise combined fix instruction for a file's findings."""
        parts = []
        for f in group:
            line = f" (line {f.line_number})" if f.line_number else ""
            fix = f.suggested_fix or f.root_cause or f.description or "fix the issue"
            parts.append(f"- {f.title}{line}: {fix}")
        return "Fix the following in this file:\n" + "\n".join(parts)
