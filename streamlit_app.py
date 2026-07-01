"""
Streamlit UI for the Multi-Agent Bug Detection & Auto PR System (PS-01).

This is the single front-end for the project. It drives the existing async
agent pipeline (``backend/agents/orchestrator.py``) directly and renders a live
agent-event feed (the Streamlit replacement for the old FastAPI + SSE layer),
followed by findings, the repair plan, and confidence-scored pull requests.
"""

from __future__ import annotations

import asyncio
import html as _html
import itertools
import json
import os
import queue
import sys
import tempfile
import threading
import traceback
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

# The backend modules use bare imports (e.g. ``from config import settings``),
# so the backend directory must be on sys.path before any of them are imported.
BACKEND_DIR = Path(__file__).parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_local_env() -> None:
    """Load ``app/.env`` into os.environ (without overwriting already-set vars).

    Pydantic reads .env into Settings directly, but the sidebar and config gates
    read os.environ — loading it here makes a configured .env "just work" without
    re-typing keys, and exposes ANTHROPIC_BASE_URL to the LLM client.
    """
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    try:
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            # .env is authoritative for the app's own config — overwrite so editing
            # a key in .env always takes effect (even if a stale value is in the env).
            if key:
                os.environ[key] = val
    except Exception:
        pass


_load_local_env()


# --------------------------------------------------------------------------- #
# Page config
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Multi-Agent Bug Detection & Auto PR",
    page_icon="🛠️",
    layout="wide",
)

# Sentinel object pushed onto the event queue to signal the pipeline finished.
_DONE = object()

ISSUE_CLASS_LABEL = {
    "functional_bug": "🐛 Functional Bug",
    "security_vulnerability": "🔒 Security",
    "code_quality": "🧹 Code Quality",
    "performance": "⚡ Performance",
}

SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "info": "⚪",
}

SEVERITY_COLOR = {
    "critical": "#ef4444", "high": "#f97316", "medium": "#eab308",
    "low": "#3b82f6", "info": "#6b7280",
}
CLASS_COLOR = {
    "security_vulnerability": "#ef4444", "functional_bug": "#f59e0b",
    "performance": "#a855f7", "code_quality": "#14b8a6",
}
CLASS_LABEL = {
    "security_vulnerability": "Security", "functional_bug": "Functional Bug",
    "performance": "Performance", "code_quality": "Code Quality",
}


# --------------------------------------------------------------------------- #
# Presentation helpers (native Streamlit + injected CSS)
# --------------------------------------------------------------------------- #
def _esc(s) -> str:
    return _html.escape(str(s)) if s is not None else ""


def _pill(text: str, color: str, filled: bool = False) -> str:
    """A small rounded badge (returns an HTML span string)."""
    if filled:
        return (f'<span class="pill" style="background:{color};color:#fff;">'
                f'{_esc(text)}</span>')
    return (f'<span class="pill" style="background:{color}22;color:{color};'
            f'border:1px solid {color}55;">{_esc(text)}</span>')


def _inject_css() -> None:
    st.markdown(
        """
        <style>
          footer {visibility: hidden;}
          .block-container {padding-top: 1.6rem; padding-bottom: 3rem; max-width: 1320px;}

          .pill {padding:2px 10px;border-radius:999px;font-size:.72rem;font-weight:700;
                 white-space:nowrap;display:inline-block;letter-spacing:.2px;}

          .hero {background:linear-gradient(120deg,#4f46e5 0%,#7c3aed 55%,#9333ea 100%);
                 border-radius:18px;padding:24px 30px;margin-bottom:18px;
                 box-shadow:0 12px 34px rgba(99,102,241,.28);}
          .hero h1 {margin:0;font-size:1.85rem;color:#fff;font-weight:800;letter-spacing:-.6px;}
          .hero p {margin:8px 0 0;color:#ece9ff;font-size:.94rem;max-width:900px;}
          .hero .pills {margin-top:15px;display:flex;gap:8px;flex-wrap:wrap;}
          .hero .pills span {background:rgba(255,255,255,.16);color:#fff;padding:4px 12px;
                 border-radius:999px;font-size:.72rem;font-weight:600;}

          .mcards {display:flex;gap:14px;flex-wrap:wrap;margin:2px 0 16px;}
          .mcard {flex:1;min-width:155px;background:#141a2a;border:1px solid #232b40;
                  border-radius:14px;padding:15px 18px;}
          .mcard .lbl {color:#8b95ad;font-size:.72rem;font-weight:700;
                  text-transform:uppercase;letter-spacing:.6px;}
          .mcard .val {color:#e6eaf3;font-size:1.7rem;font-weight:800;margin-top:5px;line-height:1;}
          .mcard .val.accent {color:#a5b4fc;}

          .cardhead {display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:2px;}
          .cardhead .title {font-weight:700;font-size:1rem;color:#e6eaf3;}
          .cardhead .spacer {flex:1;}
          .fileref {font-family:ui-monospace,Consolas,monospace;font-size:.76rem;color:#9aa4bd;
                    background:#0e1422;padding:2px 8px;border-radius:6px;}

          .cbar {height:9px;border-radius:999px;background:#232b40;overflow:hidden;}
          .cbar>div {height:100%;border-radius:999px;}

          section[data-testid="stSidebar"] {background:#0d1220;border-right:1px solid #1e2740;}
          .stButton>button {border-radius:10px;font-weight:600;}
          div[data-testid="stExpander"] {border:none;}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _hero() -> None:
    st.markdown(
        """
        <div class="hero">
          <h1>🛠️ Multi-Agent Bug Detection &amp; Auto-PR</h1>
          <p>Autonomous software maintenance — a 10-agent pipeline with knowledge-graph
          cross-file reasoning, an LLM bug hunter, surgical minimal-diff patches, and
          confidence-scored pull requests with human approval gates.</p>
          <div class="pills">
            <span>🔍 LLM Bug Hunter</span><span>🕸️ Knowledge Graph</span>
            <span>🩹 Surgical Diffs</span><span>✅ Validated Fixes</span>
            <span>🚦 Approval Gates</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _metric_cards(cards) -> None:
    """cards: list of (label, value, accent_bool)."""
    html = '<div class="mcards">'
    for label, value, accent in cards:
        cls = "val accent" if accent else "val"
        html += (f'<div class="mcard"><div class="lbl">{_esc(label)}</div>'
                 f'<div class="{cls}">{_esc(value)}</div></div>')
    html += "</div>"
    st.markdown(html, unsafe_allow_html=True)


# --------------------------------------------------------------------------- #
# Pipeline execution (async backend driven from a background thread)
# --------------------------------------------------------------------------- #
def _run_pipeline_in_thread(job, event_queue: "queue.Queue") -> None:
    """Run the async orchestrator pipeline and stream events onto a queue.

    Runs in a background thread so the main Streamlit thread can poll the queue
    and render events live. A ``_DONE`` sentinel (or an error tuple) is always
    pushed at the end so the UI loop terminates.
    """
    # Imported here (not at module top) so configuration env vars set from the
    # sidebar are in place before ``config.Settings()`` is instantiated.
    from agents.orchestrator import OrchestratorAgent

    async def callback(event) -> None:
        job.events.append(event)
        event_queue.put(event)

    try:
        orchestrator = OrchestratorAgent()
        asyncio.run(orchestrator.run_pipeline(job, callback))
    except Exception as exc:  # surface any failure to the UI
        event_queue.put(("__error__", str(exc), traceback.format_exc()))
    finally:
        event_queue.put(_DONE)


def _build_job(repo_url: str, branch: str):
    """Construct a PipelineJob (imported lazily after env is configured)."""
    from models import PipelineJob

    return PipelineJob(repo_url=repo_url, repo_owner="", repo_name="", branch=branch)


# --------------------------------------------------------------------------- #
# Result renderers
# --------------------------------------------------------------------------- #
def _safe_table(rows) -> None:
    """Render rows as a table; fall back to markdown if pandas/numpy is unavailable.

    ``st.dataframe`` imports pandas → numpy, which can fail in locked-down
    environments (blocked native DLL). The markdown fallback keeps results visible.
    """
    if not rows:
        return
    try:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    except Exception:
        cols = list(rows[0].keys())
        header = "| " + " | ".join(cols) + " |"
        sep = "| " + " | ".join("---" for _ in cols) + " |"
        body = "\n".join(
            "| " + " | ".join(str(r.get(c, "") if r.get(c) is not None else "") for c in cols) + " |"
            for r in rows
        )
        st.markdown(header + "\n" + sep + "\n" + body)


def _finding_head(f, show_source: bool = True) -> str:
    sev = f.severity.value
    cls = f.issue_class.value
    loc = _esc(f.file_path) + (f":{f.line_number}" if f.line_number else "")
    src = (f'{_pill(f.tool_source, "#6366f1")}' if show_source and f.tool_source else "")
    return (
        '<div class="cardhead">'
        f'{_pill(sev.upper(), SEVERITY_COLOR.get(sev, "#6b7280"), filled=True)}'
        f'{_pill(CLASS_LABEL.get(cls, cls), CLASS_COLOR.get(cls, "#6366f1"))}'
        f'<span class="title">{_esc(f.title)}</span>'
        f'<span class="spacer"></span>{src}'
        f'<span class="fileref">{loc}</span>'
        '</div>'
    )


def _render_findings(findings, empty_msg: str, show_blast: bool = True,
                     compact: bool = False) -> None:
    if not findings:
        st.info(empty_msg)
        return

    findings = sorted(findings, key=lambda f: f.severity_rank, reverse=True)

    if compact:
        # Dense table for low-value / informational findings.
        rows = [{
            "Severity": f"{SEVERITY_EMOJI.get(f.severity.value, '')} {f.severity.value}",
            "Class": CLASS_LABEL.get(f.issue_class.value, f.issue_class.value),
            "Title": f.title,
            "File": f.file_path,
            "Line": f.line_number,
            "Source": f.tool_source,
        } for f in findings]
        _safe_table(rows)
        return

    for f in findings:
        with st.container(border=True):
            st.markdown(_finding_head(f), unsafe_allow_html=True)
            if f.description:
                st.markdown(f.description)
            bits = []
            if f.evidence:
                bits.append(f"**🔎 Evidence** — {f.evidence}")
            if f.root_cause:
                bits.append(f"**🧭 Root cause** — {f.root_cause}")
            if f.suggested_fix:
                bits.append(f"**🩹 Suggested fix** — {f.suggested_fix}")
            if show_blast and f.blast_radius:
                extra = f" across {', '.join(f.affected_modules)}" if f.affected_modules else ""
                bits.append(f"**💥 Blast radius** — {f.blast_radius} node(s){extra}")
            if f.similar_past_fixes:
                bits.append(f"**🧠 Memory** — {len(f.similar_past_fixes)} similar past fix(es)")
            if bits:
                st.markdown("\n\n".join(bits))
            if f.code_snippet:
                with st.expander("View code"):
                    st.code(f.code_snippet)


def _render_repair_plan(job) -> None:
    plan = job.repair_plan
    if not plan or not plan.items:
        st.info("No repair plan was generated.")
        return

    st.caption("Fixes are grouped by file and ordered by dependency (security first).")
    for i, item in enumerate(plan.items, start=1):
        n_issues = len(item.finding_ids) or 1
        with st.container(border=True):
            st.markdown(
                '<div class="cardhead">'
                f'{_pill(f"#{i}", "#6366f1", filled=True)}'
                f'<span class="title">{_esc(item.epicenter_file)}</span>'
                f'<span class="spacer"></span>'
                f'{_pill(f"{n_issues} issue(s)", "#14b8a6")}'
                f'{_pill(f"pre-score {item.confidence_pre_score:.0%}", "#8b95ad")}'
                '</div>',
                unsafe_allow_html=True,
            )
            st.markdown(item.fix_strategy)
            meta = f'<span class="fileref">modifies: {_esc(", ".join(item.files_to_modify) or "—")}</span>'
            if item.dependencies:
                meta += "  ·  depends on: " + ", ".join(f"`{d}`" for d in item.dependencies)
            st.markdown(meta, unsafe_allow_html=True)


def _render_pull_requests(job) -> None:
    prs = job.pull_requests
    if not prs:
        st.info("No pull requests were created yet.")
        return

    for pr in prs:
        score = pr.confidence_score
        pct = score.total_score * 100
        color = "#22c55e" if pct >= 70 else "#f59e0b" if pct >= 40 else "#ef4444"

        with st.container(border=True):
            head = f'<div class="cardhead"><span class="title">🚀 {_esc(pr.title)}</span><span class="spacer"></span>'
            if score.is_critical_path:
                head += _pill("⚠ CRITICAL PATH", "#ef4444", filled=True)
            head += _pill("DRAFT · needs approval" if pr.requires_approval else "READY TO MERGE",
                          "#f59e0b" if pr.requires_approval else "#22c55e")
            head += "</div>"
            st.markdown(head, unsafe_allow_html=True)

            # Confidence bar
            st.markdown(
                '<div style="display:flex;align-items:center;gap:12px;margin:4px 0 8px;">'
                f'<div class="cbar" style="flex:1;"><div style="width:{pct:.0f}%;background:{color};"></div></div>'
                f'<div style="font-weight:800;color:{color};font-size:1.05rem;min-width:44px;text-align:right;">{pct:.0f}%</div>'
                '</div>',
                unsafe_allow_html=True,
            )

            st.markdown(
                f'<span class="fileref">{_esc(pr.branch_name)}</span>'
                f'&nbsp;&nbsp;·&nbsp;&nbsp;{_esc(pr.blast_radius_summary)}',
                unsafe_allow_html=True,
            )
            if pr.root_cause_explanation:
                st.markdown(f"**🧭 Root cause** — {pr.root_cause_explanation}")

            # Confidence signals as inline chips
            sig = [("Tests", score.tests_signal), ("Security", score.security_clean_signal),
                   ("AST", score.ast_valid_signal), ("Memory", score.cache_hit_signal),
                   ("Fix order", score.fix_order_signal)]
            chips = '<div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 2px;">'
            for name, val in sig:
                on = val > 0
                c = "#22c55e" if on else "#4b5563"
                chips += (f'<span class="pill" style="background:{c}22;color:{c};'
                          f'border:1px solid {c}55;">{"✓" if on else "○"} {name}</span>')
            chips += "</div>"
            st.markdown(chips, unsafe_allow_html=True)

            if pr.github_pr_url:
                st.markdown(f"🔗 **[Open pull request on GitHub]({pr.github_pr_url})**")
            else:
                st.caption("No GitHub PR opened (no GITHUB_TOKEN configured, or dry run).")

            if pr.diff_content:
                with st.expander("View diff"):
                    st.code(pr.diff_content, language="diff")


def _render_results(job) -> None:
    report_only = getattr(job, "report_only_findings", [])
    unresolved = getattr(job, "unresolved_findings", [])
    status = job.status.value.replace("_", " ").title()

    _metric_cards([
        ("Status", status, False),
        ("Pull Requests", len(job.pull_requests), True),
        ("Fixable Bugs", len(job.findings), False),
        ("Report-only", len(report_only), False),
    ])

    tab_pr, tab_fix, tab_report, tab_plan = st.tabs(
        [f"🚀 Pull Requests ({len(job.pull_requests)})",
         f"🐞 Fixable Bugs ({len(job.findings)})",
         f"📋 Report-only ({len(report_only)})",
         "🗺️ Repair Plan"]
    )
    with tab_pr:
        _render_pull_requests(job)
        if unresolved:
            st.divider()
            st.markdown(f"#### ⚠️ Unresolved ({len(unresolved)})")
            st.caption("Fixable issues where no validated patch could be produced after retries.")
            _render_findings(unresolved, "None.", show_blast=False, compact=True)
    with tab_fix:
        st.caption("Real bugs / security / performance issues — these drive the pull requests.")
        _render_findings(job.findings, "No fixable bugs were found.", show_blast=True)
    with tab_report:
        st.caption("Informational findings (e.g. code-quality nits). These never open PRs.")
        _render_findings(report_only, "No report-only findings.", show_blast=False, compact=True)
    with tab_plan:
        _render_repair_plan(job)


# --------------------------------------------------------------------------- #
# Repository bubble map (knowledge graph + blast-radius highlighting)
# --------------------------------------------------------------------------- #
# Tuning knobs for readability/performance on large repositories.
_MAP_MAX_FILES = 400      # cap number of file bubbles
_MAP_MAX_EDGES = 1500     # cap number of drawn edges
_MAP_CLIQUE_CAP = 25      # skip hub nodes that would create huge cliques


def _build_repo_map(repo_url: str, branch: str) -> dict:
    """Clone the repo, build the knowledge graph, and return bubble-map data.

    Pure structural analysis (AST + regex) — no LLM is required. Returns a dict
    of vis-network ``nodes``/``edges`` plus a ``blast`` map (file id -> list of
    file ids affected if that file changes) and some summary ``stats``.
    """
    # Imported lazily so env config is in place before config.Settings() loads.
    from knowledge_graph import KnowledgeGraph
    from utils.github_client import GitHubClient
    from config import settings

    gh = GitHubClient()
    _, repo_name = gh.get_repo_info(repo_url)
    hops = getattr(settings, "blast_radius_default_hops", 2)

    with tempfile.TemporaryDirectory() as tmp:
        repo_path = gh.clone_repo(repo_url, Path(tmp) / "repo", branch)
        kg = KnowledgeGraph(str(repo_path), repo_name)
        graph = kg.build()

        file_nodes = [n for n, a in graph.nodes(data=True) if a.get("type") == "File"]
        truncated = len(file_nodes) > _MAP_MAX_FILES
        file_nodes = file_nodes[:_MAP_MAX_FILES]
        file_set = set(file_nodes)

        def boundary(n: str) -> str:
            return graph.nodes[n].get("service_boundary") or "root"

        nodes = []
        for n in file_nodes:
            a = graph.nodes[n]
            nodes.append({
                "id": n,
                "label": Path(n).name,
                "title": f"{n}  ·  {a.get('language') or 'other'}",
                "group": boundary(n),
                "value": max(1, int(a.get("size", 1))),
            })

        # File-to-file edges: connect files that share a structural connector
        # (an imported module, a called function, a small service boundary...).
        # Hubs attached to more than _MAP_CLIQUE_CAP files are skipped so a single
        # big package doesn't turn into an unreadable clique.
        pair_weight: dict[tuple[str, str], int] = {}
        for c, ca in graph.nodes(data=True):
            if c in file_set:
                continue
            adj = [f for f in (set(graph.predecessors(c)) | set(graph.successors(c))) if f in file_set]
            if not (2 <= len(adj) <= _MAP_CLIQUE_CAP):
                continue
            for x, y in itertools.combinations(sorted(adj), 2):
                pair_weight[(x, y)] = pair_weight.get((x, y), 0) + 1

        top_pairs = sorted(pair_weight.items(), key=lambda kv: -kv[1])[:_MAP_MAX_EDGES]
        edges = [{"from": x, "to": y, "value": w} for (x, y), w in top_pairs]

        # Blast radius per file (the set of files recoloured when it is clicked).
        blast: dict[str, list[str]] = {}
        for n in file_nodes:
            affected = kg.blast_radius(n, hops=hops).get("affected_files", [])
            blast[n] = [f for f in affected if f in file_set and f != n]

        boundaries = sorted({boundary(n) for n in file_nodes})

    return {
        "repo": f"{repo_name} ({branch})",
        "nodes": nodes,
        "edges": edges,
        "blast": blast,
        "boundaries": boundaries,
        "stats": {
            "files": len(file_nodes),
            "edges": len(edges),
            "boundaries": len(boundaries),
            "truncated": truncated,
            "total_nodes": graph.number_of_nodes(),
            "total_edges": graph.number_of_edges(),
            "hops": hops,
        },
    }


def _render_bubble_map(data: dict) -> None:
    """Render the interactive vis-network bubble map with click-to-highlight."""
    stats = data["stats"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Files", stats["files"])
    c2.metric("Connections", stats["edges"])
    c3.metric("Service boundaries", stats["boundaries"])
    c4.metric("Blast hops", stats["hops"])
    if stats["truncated"]:
        st.warning(
            f"Repository is large ({stats['total_nodes']} graph nodes). Showing the "
            f"first {_MAP_MAX_FILES} files for readability."
        )
    st.caption(
        "Click any bubble: it turns **red** and every file in its blast radius "
        "(affected if you change it) turns **orange**. Click empty space to reset."
    )

    nodes_json = json.dumps(data["nodes"])
    edges_json = json.dumps(data["edges"])
    blast_json = json.dumps(data["blast"])

    html = """
<style>
  #netwrap { position: relative; }
  #net {
    height: 680px;
    border: 1px solid #1b2742;
    border-radius: 12px;
    background: radial-gradient(circle at 50% 36%, #16213c 0%, #0b1122 52%, #05080f 100%);
  }
  #legend {
    position: absolute; top: 12px; left: 14px; z-index: 5;
    font: 11px/1.55 'Segoe UI', Arial; color: #aeb9d4;
    background: rgba(8,12,24,0.6); border: 1px solid #233150;
    border-radius: 8px; padding: 8px 11px; max-width: 240px;
    backdrop-filter: blur(2px);
  }
  #legend b { color: #eaf0fb; }
  #legend .row { display: flex; align-items: center; gap: 7px; margin-top: 4px; }
  #legend .dot { width: 10px; height: 10px; border-radius: 50%; box-shadow: 0 0 7px currentColor; }
</style>
<div id="netwrap">
  <div id="net"></div>
  <div id="legend"></div>
</div>
<script src="https://unpkg.com/vis-network/standalone/umd/vis-network.min.js"></script>
<script>
  const RAW_NODES = __NODES__;
  const EDGES = __EDGES__;
  const BLAST = __BLAST__;

  // Vibrant palette assigned per service-boundary group (space-graph look).
  const PALETTE = ["#f0883e","#36c5b0","#5cc8ff","#b07bff","#ff6fae","#7ee787",
                   "#ffd24d","#ff7b72","#56d4dd","#c08cff","#90e0a8","#ff9f6e"];
  const CLICK_COLOR  = "#ff3b3b";   // clicked file
  const AFFECT_COLOR = "#ffa53b";   // blast-radius files
  const FADE_COLOR   = "#27324d";   // de-emphasised files

  const groups = [...new Set(RAW_NODES.map(n => n.group))];
  const groupColor = {};
  groups.forEach((g, i) => groupColor[g] = PALETTE[i % PALETTE.length]);
  const nameOf = {};
  RAW_NODES.forEach(n => nameOf[n.id] = n.label);
  const baseColor = n => groupColor[n.group] || "#8aa0c8";

  let highlightOn = false;

  const visNodes = RAW_NODES.map(n => ({
    id: n.id, label: "", title: n.title, value: n.value, group: n.group, shape: "dot",
    color: { background: baseColor(n), border: "#0b1020",
             highlight: { background: baseColor(n), border: "#ffffff" } },
    shadow: { enabled: true, color: baseColor(n), size: 16, x: 0, y: 0 },
    font: { color: "#eaf0fb", size: 13, strokeWidth: 3, strokeColor: "#05080f", face: "Segoe UI, Arial" }
  }));

  const nodes = new vis.DataSet(visNodes);
  const edges = new vis.DataSet(EDGES.map(e => Object.assign({}, e)));
  const container = document.getElementById('net');
  const options = {
    nodes: { scaling: { min: 8, max: 44 }, borderWidth: 2 },
    edges: {
      color: { color: "rgba(150,172,214,0.20)", highlight: "rgba(255,255,255,0.8)" },
      arrows: { to: { enabled: true, scaleFactor: 0.45 } },
      smooth: { type: "continuous" }, width: 0.6, selectionWidth: 2
    },
    physics: { stabilization: { iterations: 250 },
               barnesHut: { gravitationalConstant: -16000, centralGravity: 0.22,
                            springLength: 160, springConstant: 0.035, damping: 0.5 } },
    interaction: { hover: true, tooltipDelay: 110, navigationButtons: true, keyboard: true }
  };
  const network = new vis.Network(container, { nodes, edges }, options);

  function restore() {
    highlightOn = false;
    nodes.update(RAW_NODES.map(n => ({
      id: n.id, label: "",
      color: { background: baseColor(n), border: "#0b1020" },
      shadow: { enabled: true, color: baseColor(n), size: 16, x: 0, y: 0 }
    })));
  }
  network.on('click', function (params) {
    if (!params.nodes || params.nodes.length === 0) { restore(); return; }
    const sel = params.nodes[0];
    const affected = new Set(BLAST[sel] || []);
    highlightOn = true;
    nodes.update(RAW_NODES.map(function (n) {
      let bg = FADE_COLOR;
      if (n.id === sel) bg = CLICK_COLOR;
      else if (affected.has(n.id)) bg = AFFECT_COLOR;
      const show = (n.id === sel || affected.has(n.id));
      return {
        id: n.id, label: show ? nameOf[n.id] : "",
        color: { background: bg, border: "#0b1020" },
        shadow: { enabled: true, color: bg, size: show ? 20 : 8, x: 0, y: 0 }
      };
    }));
  });
  network.on('hoverNode', function (params) {
    nodes.update({ id: params.node, label: nameOf[params.node] || "" });
  });
  network.on('blurNode', function (params) {
    if (!highlightOn) nodes.update({ id: params.node, label: "" });
  });

  // Legend: service-boundary colour key.
  let lg = "<b>Service boundaries</b>";
  groups.slice(0, 9).forEach(function (g) {
    lg += '<div class="row"><span class="dot" style="color:' + groupColor[g] +
          ';background:' + groupColor[g] + '"></span>' + g + '</div>';
  });
  if (groups.length > 9) lg += '<div class="row" style="opacity:.7">+' + (groups.length - 9) + ' more</div>';
  document.getElementById('legend').innerHTML = lg;
</script>
"""
    html = (html
            .replace("__NODES__", nodes_json)
            .replace("__EDGES__", edges_json)
            .replace("__BLAST__", blast_json))
    components.html(html, height=720, scrolling=False)


# --------------------------------------------------------------------------- #
# Main app
# --------------------------------------------------------------------------- #
def main() -> None:
    _inject_css()
    _hero()

    # ----- Sidebar: configuration -----
    with st.sidebar:
        st.markdown(
            '<div style="font-weight:800;font-size:1.15rem;line-height:1.1;">🛠️ Bug Detection</div>'
            '<div style="color:#8b95ad;font-size:.78rem;margin:2px 0 4px;">Auto-PR System</div>',
            unsafe_allow_html=True,
        )
        st.divider()

        st.markdown("**🔑 Credentials**")
        api_key = st.text_input(
            "Anthropic API Key",
            type="password",
            value=os.environ.get("ANTHROPIC_API_KEY", ""),
            help="Used by Claude for the bug hunt, planning, and code generation.",
        )
        github_token = st.text_input(
            "GitHub Token",
            type="password",
            value=os.environ.get("GITHUB_TOKEN", ""),
            help="Needed to clone private repos and open real pull requests.",
        )
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        chips = []
        chips.append("🟢 Anthropic" if api_key else "🔴 Anthropic")
        chips.append("🟢 GitHub" if github_token else "⚪ GitHub")
        st.caption("  ·  ".join(chips) + (f"  ·  🔌 proxy" if base_url else ""))

        st.divider()
        st.markdown("**📦 Target Repository**")
        repo_url = st.text_input(
            "Repository URL",
            placeholder="https://github.com/owner/repo",
        )
        branch = st.text_input("Branch", value="main")
        map_clicked = st.button("🫧 Build Repo Map", use_container_width=True)
        run_clicked = st.button("▶️ Run Pipeline", type="primary", use_container_width=True)

        st.divider()
        st.caption(
            "**🫧 Repo Map** — interactive bubble graph of the repo (no API key). "
            "Click a file to light up its blast radius."
        )
        st.caption(
            "**▶️ Run Pipeline** — Discovery → Investigation → Planning → "
            "Fix-Validate loop → Publication."
        )

    # ----- Handle "Build Repo Map" (no Anthropic key required) -----
    if map_clicked:
        if not repo_url:
            st.error("Please provide a Repository URL in the sidebar.")
            st.stop()
        # config.Settings() requires ANTHROPIC_API_KEY at import time, but the map
        # never calls Claude — use the provided key if any, else a harmless placeholder.
        os.environ.setdefault("ANTHROPIC_API_KEY", api_key or "not-required-for-repo-map")
        if api_key:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        if github_token:
            os.environ["GITHUB_TOKEN"] = github_token

        try:
            with st.spinner(f"Cloning {repo_url} and building the knowledge graph…"):
                st.session_state["repo_map"] = _build_repo_map(repo_url, branch)
        except Exception as exc:
            st.error(f"Failed to build repo map: {exc}")
            with st.expander("Traceback"):
                st.code(traceback.format_exc())
            st.stop()

    # ----- Handle a run -----
    if run_clicked:
        if not api_key:
            st.error("Please provide an Anthropic API Key in the sidebar.")
            st.stop()
        if not repo_url:
            st.error("Please provide a Repository URL in the sidebar.")
            st.stop()

        # Make config available to the backend before it imports/instantiates settings.
        os.environ["ANTHROPIC_API_KEY"] = api_key
        if github_token:
            os.environ["GITHUB_TOKEN"] = github_token

        try:
            job = _build_job(repo_url, branch)
        except Exception as exc:  # config / import errors surface here
            st.error(f"Failed to initialize pipeline: {exc}")
            st.code(traceback.format_exc())
            st.stop()

        event_queue: "queue.Queue" = queue.Queue()
        worker = threading.Thread(
            target=_run_pipeline_in_thread, args=(job, event_queue), daemon=True
        )
        worker.start()

        st.markdown("### 📡 Live Agent Feed")
        feed_box = st.empty()
        events_text: list[str] = []
        error_payload = None

        with st.status("Running pipeline…", expanded=True) as status:
            while True:
                try:
                    item = event_queue.get(timeout=0.2)
                except queue.Empty:
                    if not worker.is_alive():
                        # Drain anything left, then stop.
                        if event_queue.empty():
                            break
                    continue

                if item is _DONE:
                    break
                if isinstance(item, tuple) and item and item[0] == "__error__":
                    error_payload = item
                    continue

                # Normal PipelineEvent
                agent = item.agent_name or "—"
                phase = f" [{item.phase}]" if item.phase else ""
                events_text.append(f"**{agent}**{phase}: {item.message}")
                feed_box.markdown("\n\n".join(events_text[-60:]))
                status.update(label=f"Running… {job.status.value}")

            if error_payload is not None:
                status.update(label="Pipeline failed", state="error")
            else:
                status.update(label="Pipeline complete", state="complete")

        worker.join(timeout=1.0)

        if error_payload is not None:
            st.error(f"Pipeline failed: {error_payload[1]}")
            with st.expander("Traceback"):
                st.code(error_payload[2])

        # Persist the finished job for re-rendering across reruns.
        st.session_state["last_job"] = job

    # ----- Show the repository bubble map (latest build) -----
    if "repo_map" in st.session_state:
        st.divider()
        st.markdown(f"### 🫧 Repository Bubble Map — {_esc(st.session_state['repo_map']['repo'])}")
        _render_bubble_map(st.session_state["repo_map"])

    # ----- Show results (latest run) -----
    if "last_job" in st.session_state:
        st.divider()
        st.markdown("### 📊 Results")
        _render_results(st.session_state["last_job"])

    # ----- Landing hint -----
    if "repo_map" not in st.session_state and "last_job" not in st.session_state:
        st.markdown(
            """
            <div style="background:#141a2a;border:1px solid #232b40;border-radius:14px;
                        padding:22px 26px;margin-top:6px;">
              <div style="font-size:1.05rem;font-weight:700;color:#e6eaf3;">👋 Get started</div>
              <div style="color:#9aa4bd;margin-top:8px;line-height:1.7;">
                Enter a repository URL in the sidebar, then choose:
                <br>• <b>🫧 Build Repo Map</b> — explore the file graph &amp; blast radius (no API key needed).
                <br>• <b>▶️ Run Pipeline</b> — full bug-detection run that opens confidence-scored PRs
                (needs an Anthropic API key).
              </div>
            </div>
            """,
            unsafe_allow_html=True,
        )


if __name__ == "__main__":
    main()
