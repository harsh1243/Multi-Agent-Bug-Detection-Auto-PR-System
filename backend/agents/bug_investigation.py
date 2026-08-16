"""Agent 4: Bug Investigation - root cause analysis with cross-file reasoning."""

from __future__ import annotations

from typing import Any, Optional

from config import settings
from knowledge_graph import KnowledgeGraph
from models import Finding, IssueClass, FindingSeverity, PipelineEvent
from utils.chroma_memory import RepositoryMemory
from utils.llm_client import llm


class BugInvestigationAgent:
    """Determines root cause, severity, and impact via knowledge graph + LLM."""

    def __init__(self, memory: Optional[RepositoryMemory] = None):
        self.name = "Bug Investigation"
        self.phase = "phase_2_investigation"
        self.memory = memory or RepositoryMemory()

    async def run(
        self,
        findings: list[Finding],
        kg: KnowledgeGraph,
        event_emitter: Optional[Any] = None,
        repo_key: Optional[str] = None,
    ) -> list[Finding]:
        """Investigate each finding with cross-file reasoning."""
        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_start", agent_name=self.name,
                phase=self.phase, message=f"Investigating {len(findings)} findings...",
            ))

        investigated = []
        for i, finding in enumerate(findings):
            if event_emitter:
                await event_emitter(PipelineEvent(
                    event_type="finding_investigating", agent_name=self.name,
                    phase=self.phase, message=f"Investigating {finding.title} ({i+1}/{len(findings)})...",
                    details={"finding_id": finding.id, "file": finding.file_path},
                ))

            # Directional change-impact analysis: dependency -> consumers.
            impact = kg.analyze_change(
                finding.file_path, hops=settings.impact_max_hops,
            )
            blast = kg.blast_radius(finding.file_path, hops=settings.impact_max_hops)
            finding.blast_radius = blast["file_count"]
            finding.affected_files = blast["affected_files"]
            finding.affected_symbols = [
                symbol.split("::")[-1] for symbol in blast["affected_functions"]
            ]
            finding.direct_dependents = impact["direct_dependents"]
            finding.related_tests = impact["related_tests"]
            finding.impact_paths = impact["impact_paths"]
            finding.impact_confidence = impact["confidence"]
            finding.affected_modules = sorted({
                kg.graph.nodes[n].get("service_boundary", "")
                for n in blast["affected_files"]
                if n in kg.graph and kg.graph.nodes[n].get("service_boundary")
            })

            # Data-flow tracing
            flow = kg.trace_data_flow(finding.file_path, finding.line_number)

            # Query ChromaDB for similar past fixes
            similar = self.memory.query_similar(
                bug_type=finding.issue_class.value,
                code_snippet=finding.code_snippet or finding.title,
                repo_key=repo_key or kg.repo_name,
            )
            finding.similar_past_fixes = similar

            # LLM root cause analysis (skip if the Bug Hunter already produced one,
            # saving a Sonnet call per finding).
            if not finding.root_cause:
                finding.root_cause = await self._llm_root_cause(finding, blast, flow)

            investigated.append(finding)

            if event_emitter:
                await event_emitter(PipelineEvent(
                    event_type="finding_investigated", agent_name=self.name,
                    phase=self.phase, message=f"Root cause: {finding.root_cause[:100]}...",
                    details={"finding_id": finding.id, "blast_radius": blast["file_count"]},
                ))

        if event_emitter:
            await event_emitter(PipelineEvent(
                event_type="agent_complete", agent_name=self.name,
                phase=self.phase, message=f"Investigation complete for {len(investigated)} findings.",
                details={"investigated": len(investigated)},
            ))

        return investigated

    async def _llm_root_cause(
        self, finding: Finding, blast: dict, flow: dict
    ) -> str:
        """Use LLM to determine root cause with graph context."""
        upstream = flow.get("upstream_callers", [])[:5]
        downstream = flow.get("downstream_callees", [])[:5]

        prompt = f"""Analyze this code issue and determine the root cause:

Issue: {finding.title}
Description: {finding.description}
File: {finding.file_path}:{finding.line_number or '?'}
Code snippet:
```
{finding.code_snippet or 'N/A'}
```

Impact Analysis:
- Affected files: {blast.get('file_count', 0)}
- Crosses service boundary: {blast.get('crosses_service_boundary', False)}
- Direct dependent files: {blast.get('direct_dependents', [])[:5]}
- Related tests: {blast.get('related_tests', [])[:5]}
- Dependency paths: {blast.get('impact_paths', [])[:5]}
- Upstream callers: {upstream}
- Downstream callees: {downstream}

Provide a concise root cause analysis (2-3 sentences) explaining WHY this issue exists and which files must be changed to fix it at the source (not just the symptom)."""

        try:
            return await llm.reason(prompt, max_tokens=512)
        except Exception:
            return f"Potential {finding.issue_class.value} in {finding.file_path} affecting {blast.get('file_count', 0)} files."
