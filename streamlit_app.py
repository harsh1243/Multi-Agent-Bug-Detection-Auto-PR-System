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
import re
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

# The backend modules use bare imports (e.g. ``from config import settings``),
# so the backend directory must be on sys.path before any of them are imported.
BACKEND_DIR = Path(__file__).parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _load_streamlit_secrets() -> None:
    """Copy ``st.secrets`` into os.environ so cloud deploys can be configured.

    On Streamlit Community Cloud there is no .env file (it is gitignored), and
    the sidebar only collects the API key and GitHub token — there is nowhere to
    supply ANTHROPIC_BASE_URL or the model IDs. Without the base URL a proxy key
    (e.g. an ``aero_live_...`` Aerolink key) is sent to api.anthropic.com, which
    rejects it with 401 "API key is invalid".

    Secrets are set in the app's Settings → Secrets panel as TOML. Every
    top-level scalar is forwarded, so any Settings field can be configured this
    way. Existing environment variables win, and .env is loaded afterwards, so
    local runs keep their current precedence.
    """
    try:
        secrets = st.secrets
    except Exception:
        # No secrets configured (normal for local runs) — nothing to do.
        return
    try:
        for key, val in secrets.items():
            if isinstance(val, (str, int, float, bool)) and key not in os.environ:
                os.environ[str(key)] = str(val)
    except Exception:
        pass


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


_load_streamlit_secrets()
_load_local_env()


# --------------------------------------------------------------------------- #
# Page config
# --------------------------------------------------------------------------- #
st.set_page_config(
    page_title="Code Impact & Auto Repair",
    page_icon="◈",
    layout="wide",
    initial_sidebar_state="expanded",
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
          header[data-testid="stHeader"] {background:transparent;}
          [data-testid="stAppViewContainer"] {
            background:
              radial-gradient(circle at 15% 0%, rgba(99,102,241,.12), transparent 28rem),
              radial-gradient(circle at 92% 8%, rgba(168,85,247,.10), transparent 24rem),
              #0b0f1a;
          }
          .block-container {padding-top: 1.35rem; padding-bottom: 4rem; max-width: 1380px;}

          h1,h2,h3 {letter-spacing:-.025em;}
          [data-testid="stCaptionContainer"] {color:#7f8aa3;}
          div[data-testid="stVerticalBlockBorderWrapper"] {
            border-color:#232b40 !important;border-radius:16px !important;
            background:linear-gradient(145deg,rgba(20,26,42,.94),rgba(13,18,32,.94));
            box-shadow:0 12px 32px rgba(0,0,0,.12);
          }

          .section-title {display:flex;align-items:flex-end;gap:18px;margin:26px 0 13px;}
          .section-title .copy {flex:1;}
          .section-title .eyebrow {color:#818cf8;font-size:.69rem;font-weight:800;
            text-transform:uppercase;letter-spacing:1.4px;margin-bottom:5px;}
          .section-title h2 {color:#eef2ff;font-size:1.28rem;line-height:1.15;margin:0;}
          .section-title p {color:#8b95ad;font-size:.82rem;margin:5px 0 0;max-width:760px;}

          .panel {background:linear-gradient(145deg,rgba(20,26,42,.96),rgba(13,18,32,.96));
            border:1px solid #232b40;border-radius:16px;padding:18px 20px;
            box-shadow:0 14px 35px rgba(0,0,0,.14);}
          .panel-title {font-size:.82rem;color:#dce3f5;font-weight:800;margin-bottom:12px;}

          .feature-grid {display:grid;grid-template-columns:repeat(3,minmax(0,1fr));
            gap:12px;margin:12px 0 18px;}
          .feature {background:linear-gradient(145deg,#141a2a,#101626);border:1px solid #232b40;
            border-radius:15px;padding:17px 18px;min-height:128px;}
          .feature .f-icon {font-size:1.25rem;width:38px;height:38px;display:flex;align-items:center;
            justify-content:center;border-radius:11px;background:#6366f122;margin-bottom:12px;}
          .feature .f-title {color:#e6eaf3;font-size:.9rem;font-weight:800;}
          .feature .f-copy {color:#8f9ab2;font-size:.76rem;line-height:1.55;margin-top:6px;}

          .workflow {display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:9px;margin-top:12px;}
          .wf {position:relative;background:#111728;border:1px solid #222b43;border-radius:13px;
            padding:14px 13px;min-height:104px;}
          .wf:not(:last-child):after {content:'→';position:absolute;right:-11px;top:37px;color:#58627a;
            z-index:3;font-size:.8rem;}
          .wf .num {font-size:.64rem;font-weight:900;color:#818cf8;letter-spacing:1px;}
          .wf .name {font-size:.78rem;font-weight:800;color:#dce3f5;margin-top:7px;}
          .wf .desc {font-size:.67rem;color:#78849d;margin-top:5px;line-height:1.35;}

          .dist-list {display:flex;flex-direction:column;gap:11px;}
          .dist-row {display:grid;grid-template-columns:120px 1fr 34px;gap:11px;align-items:center;}
          .dist-label {color:#aeb7ca;font-size:.73rem;font-weight:700;white-space:nowrap;overflow:hidden;
            text-overflow:ellipsis;}
          .dist-track {height:8px;border-radius:999px;background:#222a3e;overflow:hidden;}
          .dist-fill {height:100%;border-radius:999px;box-shadow:0 0 12px currentColor;}
          .dist-value {color:#dbe2f1;text-align:right;font-size:.72rem;font-weight:800;}

          .health-grid {display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:9px;}
          .health {background:#111728;border:1px solid #222b43;border-radius:12px;padding:12px;}
          .health .h-top {display:flex;align-items:center;justify-content:space-between;gap:6px;}
          .health .h-name {font-size:.68rem;color:#8f9ab2;font-weight:800;text-transform:uppercase;
            letter-spacing:.5px;}
          .health .h-state {font-size:.72rem;font-weight:800;margin-top:7px;}
          .health.pass {border-color:#22c55e44;background:#102019;}
          .health.pass .h-state {color:#4ade80;}
          .health.warn {border-color:#f59e0b44;background:#211b10;}
          .health.warn .h-state {color:#fbbf24;}
          .health.fail {border-color:#ef444444;background:#241315;}
          .health.fail .h-state {color:#fb7185;}

          .empty-state {text-align:center;padding:34px 24px;border:1px dashed #2a3550;border-radius:16px;
            background:rgba(17,23,40,.68);}
          .empty-state .e-icon {font-size:1.8rem;margin-bottom:8px;}
          .empty-state .e-title {font-size:.92rem;color:#e1e7f4;font-weight:800;}
          .empty-state .e-copy {font-size:.77rem;color:#818ba2;max-width:520px;margin:7px auto 0;line-height:1.55;}

          .connection-row {display:flex;flex-direction:column;gap:7px;margin:7px 0 3px;}
          .connection {display:flex;align-items:center;gap:9px;background:#111728;border:1px solid #202940;
            border-radius:10px;padding:8px 10px;}
          .connection .dot {width:8px;height:8px;border-radius:50%;flex:none;}
          .connection .c-name {font-size:.72rem;color:#aeb8cc;font-weight:700;flex:1;}
          .connection .c-state {font-size:.65rem;color:#69758e;font-weight:700;}

          @media (max-width:900px) {
            .feature-grid {grid-template-columns:1fr;}
            .workflow {grid-template-columns:1fr 1fr;}
            .wf:after {display:none;}
            .health-grid {grid-template-columns:1fr 1fr;}
          }

          .pill {padding:2px 10px;border-radius:999px;font-size:.72rem;font-weight:700;
                 white-space:nowrap;display:inline-block;letter-spacing:.2px;}

          .hero {position:relative;overflow:hidden;
                 background:linear-gradient(125deg,#312e81 0%,#5b21b6 50%,#7e22ce 100%);
                 border:1px solid #a78bfa55;border-radius:20px;padding:27px 30px;margin-bottom:18px;
                 box-shadow:0 18px 48px rgba(76,29,149,.28);}
          .hero:after {content:'';position:absolute;width:330px;height:330px;border-radius:50%;
                 right:-90px;top:-190px;background:rgba(255,255,255,.11);filter:blur(2px);}
          .hero .hero-kicker {display:inline-flex;align-items:center;gap:7px;color:#ddd6fe;
                 font-size:.68rem;font-weight:900;text-transform:uppercase;letter-spacing:1.4px;
                 margin-bottom:8px;}
          .hero .hero-kicker:before {content:'';width:7px;height:7px;border-radius:50%;
                 background:#86efac;box-shadow:0 0 10px #4ade80;}
          .hero h1 {margin:0;font-size:1.9rem;color:#fff;font-weight:850;letter-spacing:-.7px;}
          .hero p {margin:9px 0 0;color:#e9e5ff;font-size:.9rem;max-width:850px;line-height:1.55;}
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

          section[data-testid="stSidebar"] {background:linear-gradient(180deg,#0d1220,#0a0f1b);
            border-right:1px solid #1e2740;}
          section[data-testid="stSidebar"] .block-container {padding-top:1.2rem;}
          .stButton>button {border-radius:10px;font-weight:700;min-height:2.65rem;
            border-color:#2c3650;transition:transform .15s ease,border-color .15s ease;}
          .stButton>button:hover {transform:translateY(-1px);border-color:#6366f1;}
          [data-testid="stTextInput"] input {border-radius:10px;background:#101626;border-color:#273149;}
          [data-baseweb="tab-list"] {gap:6px;background:#101626;padding:5px;border-radius:12px;}
          [data-baseweb="tab"] {height:38px;border-radius:9px;padding:0 14px;}
          [data-baseweb="tab-highlight"] {background:#6366f1;border-radius:8px;}
          div[data-testid="stExpander"] {border:none;}

          /* ── Live pipeline stepper ── */
          .stepper {display:flex;gap:8px;flex-wrap:wrap;margin:4px 0 14px;}
          .step {flex:1;min-width:118px;background:#141a2a;border:1px solid #232b40;
                 border-radius:13px;padding:12px 14px;transition:all .3s ease;}
          .step .st-ic {font-size:1.2rem;line-height:1;}
          .step .st-name {font-size:.73rem;font-weight:800;color:#8b95ad;margin-top:7px;
                 text-transform:uppercase;letter-spacing:.5px;}
          .step .st-state {font-size:.68rem;margin-top:3px;color:#6b7280;font-weight:600;}
          .step.done {border-color:#22c55e55;background:#0f2018;}
          .step.done .st-name {color:#4ade80;}
          .step.done .st-state {color:#4ade80;}
          .step.active {border-color:#6366f1;background:#161a33;
                 box-shadow:0 0 0 1px #6366f1, 0 10px 26px rgba(99,102,241,.30);}
          .step.active .st-name {color:#a5b4fc;}
          .step.active .st-state {color:#a5b4fc;}
          .step.active .st-ic {animation:bob 1.15s ease-in-out infinite;}
          .step.pending {opacity:.5;}
          @keyframes bob {0%,100%{transform:translateY(0)}50%{transform:translateY(-3px)}}

          /* ── Live agent feed ── */
          .feed-wrap {display:flex;flex-direction:column;gap:8px;max-height:450px;
                 overflow-y:auto;padding:2px 6px 2px 2px;}
          .feed-wrap::-webkit-scrollbar {width:8px;}
          .feed-wrap::-webkit-scrollbar-thumb {background:#2a3450;border-radius:8px;}
          .feed-card {display:flex;gap:11px;align-items:flex-start;background:#141a2a;
                 border:1px solid #232b40;border-left:3px solid #6366f1;border-radius:11px;
                 padding:9px 13px;animation:slidein .26s ease;}
          @keyframes slidein {from{opacity:0;transform:translateY(-7px)}to{opacity:1;transform:none}}
          .feed-ic {width:31px;height:31px;border-radius:9px;display:flex;align-items:center;
                 justify-content:center;font-size:1.02rem;flex-shrink:0;}
          .feed-body {flex:1;min-width:0;}
          .feed-top {display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
          .feed-agent {font-weight:800;font-size:.82rem;}
          .feed-phase {font-size:.64rem;color:#9aa4bd;background:#0e1422;padding:1px 8px;
                 border-radius:6px;text-transform:uppercase;letter-spacing:.4px;font-weight:700;}
          .feed-time {font-size:.68rem;color:#5b647d;margin-left:auto;
                 font-family:ui-monospace,Consolas,monospace;}
          .feed-msg {color:#c3cad9;font-size:.84rem;margin-top:3px;line-height:1.45;
                 word-break:break-word;}
          .live-dot {display:inline-block;width:9px;height:9px;border-radius:50%;
                 background:#22c55e;margin-right:8px;box-shadow:0 0 9px #22c55e;
                 animation:pulse 1.25s ease-in-out infinite;vertical-align:middle;}
          @keyframes pulse {0%,100%{opacity:1;transform:scale(1)}50%{opacity:.35;transform:scale(.65)}}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _hero() -> None:
    st.markdown(
        """
        <div class="hero">
          <div class="hero-kicker">Repository intelligence workspace</div>
          <h1>Code Impact &amp; Autonomous Repair</h1>
          <p>Trace Python dependencies, investigate defects across files, generate atomic minimal
          patches, and publish evidence-backed pull requests from one explainable workflow.</p>
          <div class="pills">
            <span>🕸️ Directional impact graph</span><span>🔍 Cross-file investigation</span>
            <span>🩹 Atomic patches</span><span>✅ Full-suite validation</span>
            <span>🧠 Scoped repair memory</span>
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


def _section_header(eyebrow: str, title: str, description: str = "") -> None:
    copy = f'<p>{_esc(description)}</p>' if description else ""
    st.markdown(
        '<div class="section-title"><div class="copy">'
        f'<div class="eyebrow">{_esc(eyebrow)}</div>'
        f'<h2>{_esc(title)}</h2>{copy}</div></div>',
        unsafe_allow_html=True,
    )


def _empty_state(icon: str, title: str, message: str) -> None:
    st.markdown(
        '<div class="empty-state">'
        f'<div class="e-icon">{icon}</div>'
        f'<div class="e-title">{_esc(title)}</div>'
        f'<div class="e-copy">{_esc(message)}</div></div>',
        unsafe_allow_html=True,
    )


def _connections_html(api_key: str, github_token: str, base_url: str | None) -> str:
    rows = [
        ("Anthropic", bool(api_key), "Ready" if api_key else "Required"),
        ("GitHub", bool(github_token), "Authenticated" if github_token else "Public only"),
        ("LLM endpoint", True, "Proxy" if base_url else "Official"),
    ]
    html = ['<div class="connection-row">']
    for name, ready, state in rows:
        color = "#22c55e" if ready else "#f59e0b"
        html.append(
            '<div class="connection">'
            f'<span class="dot" style="background:{color};box-shadow:0 0 8px {color};"></span>'
            f'<span class="c-name">{_esc(name)}</span>'
            f'<span class="c-state">{_esc(state)}</span></div>'
        )
    html.append("</div>")
    return "".join(html)


def _is_github_repo_url(value: str) -> bool:
    return bool(re.match(r"^https://github\.com/[^/\s]+/[^/\s]+(?:\.git)?/?$", value.strip()))


def _distribution_html(counts: dict[str, int], colors: dict[str, str]) -> str:
    total = max(sum(counts.values()), 1)
    rows = ['<div class="dist-list">']
    for label, count in sorted(counts.items(), key=lambda item: item[1], reverse=True):
        pct = 100 * count / total
        color = colors.get(label, "#6366f1")
        rows.append(
            '<div class="dist-row">'
            f'<div class="dist-label">{_esc(label.replace("_", " ").title())}</div>'
            f'<div class="dist-track"><div class="dist-fill" '
            f'style="width:{pct:.1f}%;background:{color};color:{color};"></div></div>'
            f'<div class="dist-value">{count}</div></div>'
        )
    rows.append("</div>")
    return "".join(rows)


def _health_html(items: list[tuple[str, str, str]]) -> str:
    """items: (name, state text, pass|warn|fail)."""
    cards = ['<div class="health-grid">']
    icons = {"pass": "✓", "warn": "?", "fail": "!"}
    for name, state, kind in items:
        cards.append(
            f'<div class="health {kind}"><div class="h-top">'
            f'<span class="h-name">{_esc(name)}</span><span>{icons.get(kind, "•")}</span>'
            f'</div><div class="h-state">{_esc(state)}</div></div>'
        )
    cards.append("</div>")
    return "".join(cards)


def _render_setup_guide() -> None:
    _section_header(
        "How it works",
        "From repository to evidence-backed pull request",
        "A focused Python workflow with visible reasoning at every stage.",
    )
    st.markdown(
        """
        <div class="feature-grid">
          <div class="feature"><div class="f-icon">🕸️</div>
            <div class="f-title">Explainable impact graph</div>
            <div class="f-copy">Maps imports, functions, methods, calls, consumers and tests, then
            records the exact dependency path behind every affected file.</div></div>
          <div class="feature"><div class="f-icon">🩹</div>
            <div class="f-title">Atomic cross-file repair</div>
            <div class="f-copy">Uses anchored edits inside graph-selected files. If one edit is
            missing or ambiguous, the complete patch is rejected and retried.</div></div>
          <div class="feature"><div class="f-icon">🧠</div>
            <div class="f-title">Validated repair memory</div>
            <div class="f-copy">Stores only successful fixes with repository scope, stable IDs,
            validation evidence and semantic retrieval for future investigations.</div></div>
        </div>
        <div class="panel"><div class="panel-title">Pipeline workflow</div>
          <div class="workflow">
            <div class="wf"><div class="num">01</div><div class="name">Discover</div>
              <div class="desc">Graph, scanners and LLM triage</div></div>
            <div class="wf"><div class="num">02</div><div class="name">Investigate</div>
              <div class="desc">Root cause and impact paths</div></div>
            <div class="wf"><div class="num">03</div><div class="name">Plan</div>
              <div class="desc">Order repairs by dependency</div></div>
            <div class="wf"><div class="num">04</div><div class="name">Validate</div>
              <div class="desc">Atomic patch and full test suite</div></div>
            <div class="wf"><div class="num">05</div><div class="name">Publish</div>
              <div class="desc">Confidence-scored GitHub PR</div></div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


# --------------------------------------------------------------------------- #
# Live run visuals (phase stepper, running metrics, rich agent feed)
# --------------------------------------------------------------------------- #
# Per-agent identity: (icon, accent colour) keyed by the agent's ``.name``.
AGENT_STYLE = {
    "Orchestrator":          ("🧠", "#8b5cf6"),
    "Repo Mapper":           ("🗺️", "#3b82f6"),
    "Dependency Analyzer":   ("📦", "#f59e0b"),
    "Static Analysis":       ("🔬", "#14b8a6"),
    "Bug Hunter":            ("🔍", "#ef4444"),
    "Bug Investigation":     ("🧭", "#a855f7"),
    "Repair Planner":        ("📝", "#06b6d4"),
    "Code Generation":       ("🩹", "#22c55e"),
    "Validation Agent":      ("✅", "#10b981"),
    "Security Verification": ("🔐", "#f43f5e"),
    "PR Author":             ("🚀", "#6366f1"),
}
_DEFAULT_AGENT_STYLE = ("🤖", "#6b7280")

# Left-border accent per event type (falls back to the agent colour).
EVENT_ACCENT = {
    "agent_start":           "#3b82f6",
    "agent_progress":        "#6366f1",
    "agent_complete":        "#22c55e",
    "agent_warning":         "#f59e0b",
    "agent_retry":           "#eab308",
    "agent_error":           "#ef4444",
    "finding_discovered":    "#a855f7",
    "finding_investigating": "#a855f7",
    "finding_investigated":  "#a855f7",
    "pr_created":            "#22c55e",
    "orchestrator_update":   "#8b5cf6",
}

# The pipeline phases, in order, for the live stepper. Keys are JobStatus values.
STEP_PHASES = [
    ("cloning",                 "Clone",          "📥"),
    ("phase_1_discovery",       "Discovery",      "🔍"),
    ("phase_2_investigation",   "Investigation",  "🧭"),
    ("phase_3_planning",        "Planning",       "📝"),
    ("phase_4_fix_validate",    "Fix & Validate", "🩹"),
    ("phase_5_publication",     "Publication",    "🚀"),
]
_STEP_INDEX = {key: i for i, (key, _, _) in enumerate(STEP_PHASES)}


def _stepper_html(active_idx: int) -> str:
    """Render the phase stepper. Steps before ``active_idx`` are done, the one at
    it is running, the rest are queued. Pass ``len(STEP_PHASES)`` for 'all done'."""
    out = ['<div class="stepper">']
    for i, (_key, name, icon) in enumerate(STEP_PHASES):
        if i < active_idx:
            cls, state, ic = "done", "done", "✅"
        elif i == active_idx:
            cls, state, ic = "active", "running…", icon
        else:
            cls, state, ic = "pending", "queued", icon
        out.append(
            f'<div class="step {cls}"><div class="st-ic">{ic}</div>'
            f'<div class="st-name">{_esc(name)}</div>'
            f'<div class="st-state">{state}</div></div>'
        )
    out.append("</div>")
    return "".join(out)


def _live_metrics_html(elapsed: float, n_events: int, n_findings: int, n_prs: int) -> str:
    mm, ss = divmod(int(max(elapsed, 0)), 60)

    def card(label: str, value) -> str:
        return (f'<div class="mcard"><div class="lbl">{_esc(label)}</div>'
                f'<div class="val accent">{_esc(value)}</div></div>')

    return (
        '<div class="mcards">'
        + card("Elapsed", f"{mm:d}:{ss:02d}")
        + card("Events", n_events)
        + card("Fixable bugs", n_findings)
        + card("Pull requests", n_prs)
        + "</div>"
    )


def _feed_card_html(item) -> str:
    """Render one pipeline event as a rich, agent-coloured feed card."""
    agent = item.agent_name or "Orchestrator"
    icon, color = AGENT_STYLE.get(agent, _DEFAULT_AGENT_STYLE)
    accent = EVENT_ACCENT.get(item.event_type, color)
    phase = (item.phase or "").replace("phase_", "P").replace("_", " ").strip()
    try:
        ts = item.timestamp.strftime("%H:%M:%S")
    except Exception:
        ts = ""
    phase_html = f'<span class="feed-phase">{_esc(phase)}</span>' if phase else ""
    return (
        f'<div class="feed-card" style="border-left-color:{accent};">'
        f'<div class="feed-ic" style="background:{color}22;color:{color};">{icon}</div>'
        f'<div class="feed-body"><div class="feed-top">'
        f'<span class="feed-agent" style="color:{color};">{_esc(agent)}</span>'
        f'{phase_html}<span class="feed-time">{_esc(ts)}</span></div>'
        f'<div class="feed-msg">{_esc(item.message)}</div></div></div>'
    )


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
    from models import JobStatus

    async def callback(event) -> None:
        job.events.append(event)
        event_queue.put(event)

    try:
        orchestrator = OrchestratorAgent()
        asyncio.run(orchestrator.run_pipeline(job, callback))
    except Exception as exc:  # surface any failure to the UI
        job.status = JobStatus.FAILED
        job.error_message = str(exc)
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
        _empty_state("✨", "Nothing to show", empty_msg)
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
                bits.append(
                    f"**💥 Directional impact** — {f.blast_radius} dependent file(s){extra} "
                    f"(graph confidence: {f.impact_confidence})"
                )
            if f.direct_dependents:
                bits.append(f"**Immediate consumers** — {', '.join(f.direct_dependents[:6])}")
            if f.related_tests:
                bits.append(f"**Related tests** — {', '.join(f.related_tests[:6])}")
            if f.similar_past_fixes:
                bits.append(f"**🧠 Memory** — {len(f.similar_past_fixes)} similar past fix(es)")
            if bits:
                st.markdown("\n\n".join(bits))
            if f.code_snippet:
                with st.expander("View code"):
                    st.code(f.code_snippet)
            if f.impact_paths:
                with st.expander("Why these files are affected"):
                    st.markdown("\n".join(
                        f"- `{' → '.join(path)}`" for path in f.impact_paths[:12]
                    ))


def _render_findings_explorer(
    findings,
    empty_msg: str,
    key_prefix: str,
    show_blast: bool = True,
    compact: bool = False,
) -> None:
    if not findings:
        _render_findings(findings, empty_msg, show_blast, compact)
        return

    available_severity = sorted(
        {f.severity.value for f in findings},
        key=lambda value: ["critical", "high", "medium", "low", "info"].index(value),
    )
    available_classes = sorted({f.issue_class.value for f in findings})
    c1, c2, c3 = st.columns([1.5, 1, 1])
    with c1:
        search = st.text_input(
            "Search findings", placeholder="Title, file, description…",
            key=f"{key_prefix}_search",
        ).strip().lower()
    with c2:
        severity = st.multiselect(
            "Severity", available_severity, default=available_severity,
            key=f"{key_prefix}_severity",
        )
    with c3:
        issue_classes = st.multiselect(
            "Class", available_classes, default=available_classes,
            format_func=lambda value: CLASS_LABEL.get(value, value),
            key=f"{key_prefix}_class",
        )

    filtered = [
        f for f in findings
        if f.severity.value in severity and f.issue_class.value in issue_classes
        and (
            not search
            or search in f.title.lower()
            or search in f.file_path.lower()
            or search in (f.description or "").lower()
        )
    ]
    st.caption(f"Showing {len(filtered)} of {len(findings)} findings")
    _render_findings(
        filtered,
        "No findings match the selected filters.",
        show_blast=show_blast,
        compact=compact,
    )


def _render_repair_plan(job) -> None:
    plan = job.repair_plan
    if not plan or not plan.items:
        _empty_state("🗺️", "No repair plan", "A repair plan appears after fixable findings are investigated.")
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
        _empty_state(
            "🚀", "No pull requests prepared",
            "Validated atomic patches will appear here with confidence and approval evidence.",
        )
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
            additions = sum(
                line.startswith("+") and not line.startswith("+++")
                for line in pr.diff_content.splitlines()
            )
            deletions = sum(
                line.startswith("-") and not line.startswith("---")
                for line in pr.diff_content.splitlines()
            )
            st.caption(
                f"{len(pr.finding_ids) or 1} finding(s) · {len(pr.files_changed)} changed file(s) · "
                f"+{additions} / -{deletions} lines"
            )
            if pr.root_cause_explanation:
                st.markdown(f"**🧭 Root cause** — {pr.root_cause_explanation}")

            # Confidence signals as inline chips
            sig = [
                ("Tests", score.tests_signal, score.tests_available),
                ("Security", score.security_clean_signal, score.security_scanner_available),
                ("AST", score.ast_valid_signal, True),
                ("Memory", score.cache_hit_signal, True),
                ("Fix order", score.fix_order_signal, True),
            ]
            chips = '<div style="display:flex;gap:6px;flex-wrap:wrap;margin:8px 0 2px;">'
            for name, val, available in sig:
                on = val > 0
                c = "#22c55e" if on else "#4b5563"
                marker = "✓" if on else ("?" if not available else "○")
                suffix = " unavailable" if not available else ""
                chips += (f'<span class="pill" style="background:{c}22;color:{c};'
                          f'border:1px solid {c}55;">{marker} {name}{suffix}</span>')
            chips += "</div>"
            st.markdown(chips, unsafe_allow_html=True)

            if pr.github_pr_url:
                st.markdown(f"🔗 **[Open pull request on GitHub]({pr.github_pr_url})**")
            else:
                st.caption("No GitHub PR opened (no GITHUB_TOKEN configured, or dry run).")

            if pr.diff_content:
                with st.expander("View diff"):
                    st.code(pr.diff_content, language="diff")


def _render_overview(job) -> None:
    report_only = getattr(job, "report_only_findings", [])
    unresolved = getattr(job, "unresolved_findings", [])
    all_findings = list(job.findings) + list(report_only)

    confidences = [pr.confidence_score.total_score for pr in job.pull_requests]
    avg_confidence = sum(confidences) / len(confidences) if confidences else 0.0
    duration = "—"
    if job.completed_at and job.created_at:
        seconds = max(0, int((job.completed_at - job.created_at).total_seconds()))
        minutes, remainder = divmod(seconds, 60)
        duration = f"{minutes}m {remainder:02d}s"

    _metric_cards([
        ("Pipeline", job.status.value.replace("_", " ").title(), False),
        ("PRs prepared", len(job.pull_requests), True),
        ("Avg. confidence", f"{avg_confidence:.0%}" if confidences else "—", False),
        ("Duration", duration, False),
        ("Unresolved", len(unresolved), False),
    ])

    severity_counts = {
        severity: sum(f.severity.value == severity for f in all_findings)
        for severity in ("critical", "high", "medium", "low", "info")
    }
    severity_counts = {key: value for key, value in severity_counts.items() if value}
    class_counts = {
        issue_class: sum(f.issue_class.value == issue_class for f in all_findings)
        for issue_class in CLASS_LABEL
    }
    class_counts = {key: value for key, value in class_counts.items() if value}

    left, right = st.columns(2)
    with left:
        st.markdown(
            '<div class="panel"><div class="panel-title">Severity distribution</div>'
            + (_distribution_html(severity_counts, SEVERITY_COLOR) if severity_counts
               else '<div class="dist-label">No findings</div>')
            + "</div>",
            unsafe_allow_html=True,
        )
    with right:
        st.markdown(
            '<div class="panel"><div class="panel-title">Finding classes</div>'
            + (_distribution_html(class_counts, CLASS_COLOR) if class_counts
               else '<div class="dist-label">No findings</div>')
            + "</div>",
            unsafe_allow_html=True,
        )

    validations = list(job.validation_results)
    ast_kind = "pass" if validations and all(v.gate_1_ast_valid for v in validations) else (
        "warn" if not validations else "fail"
    )
    test_runs = [v for v in validations if v.tests_available]
    tests_kind = "pass" if test_runs and all(v.gate_2_tests_passed for v in test_runs) else (
        "warn" if not test_runs else "fail"
    )
    security_runs = [v for v in validations if v.security_scanner_available]
    security_kind = "pass" if security_runs and all(v.gate_4_security_clean for v in security_runs) else (
        "warn" if not security_runs else "fail"
    )
    full_runs = [v for v in validations if v.tests_available]
    suite_kind = "pass" if full_runs and all(v.full_suite_run for v in full_runs) else "warn"

    st.markdown("<br>", unsafe_allow_html=True)
    st.markdown(
        '<div class="panel"><div class="panel-title">Validation health</div>'
        + _health_html([
            ("Syntax", "Passed" if ast_kind == "pass" else ("No patches" if ast_kind == "warn" else "Failed"), ast_kind),
            ("Targeted tests", "Passed" if tests_kind == "pass" else ("Unavailable" if tests_kind == "warn" else "Failed"), tests_kind),
            ("Full suite", "Executed" if suite_kind == "pass" else "Not available", suite_kind),
            ("Security", "Clean" if security_kind == "pass" else ("Unavailable" if security_kind == "warn" else "Failed"), security_kind),
        ])
        + "</div>",
        unsafe_allow_html=True,
    )

    file_counts: dict[str, int] = {}
    for finding in all_findings:
        file_counts[finding.file_path] = file_counts.get(finding.file_path, 0) + 1
    if file_counts:
        st.markdown("#### Most affected files")
        rows = [
            {"File": path, "Findings": count}
            for path, count in sorted(file_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
        ]
        _safe_table(rows)

    if unresolved:
        st.warning(
            f"{len(unresolved)} fixable finding(s) remain unresolved because no complete, "
            "validated atomic patch could be produced."
        )


def _render_activity(job) -> None:
    events = list(job.events)
    if not events:
        _empty_state("📡", "No activity recorded", "Pipeline events will appear here after a run.")
        return
    filters = sorted({event.agent_name or "Orchestrator" for event in events})
    selected = st.multiselect(
        "Agents", filters, default=filters, key="activity_agents",
    )
    visible = [event for event in events if (event.agent_name or "Orchestrator") in selected]
    st.caption(f"{len(visible)} events · newest first")
    st.markdown(
        '<div class="feed-wrap">'
        + "".join(_feed_card_html(event) for event in reversed(visible[-100:]))
        + "</div>",
        unsafe_allow_html=True,
    )


def _render_results(job) -> None:
    report_only = getattr(job, "report_only_findings", [])
    unresolved = getattr(job, "unresolved_findings", [])
    tab_overview, tab_pr, tab_fix, tab_report, tab_plan, tab_activity = st.tabs(
        ["◈ Overview",
         f"🚀 Pull Requests ({len(job.pull_requests)})",
         f"🐞 Fixable Bugs ({len(job.findings)})",
         f"📋 Report-only ({len(report_only)})",
         "🗺️ Repair Plan",
         f"📡 Activity ({len(job.events)})"]
    )
    with tab_overview:
        _render_overview(job)
    with tab_pr:
        _render_pull_requests(job)
        if unresolved:
            st.divider()
            st.markdown(f"#### ⚠️ Unresolved ({len(unresolved)})")
            st.caption("Fixable issues where no validated patch could be produced after retries.")
            _render_findings(unresolved, "None.", show_blast=False, compact=True)
    with tab_fix:
        st.caption("Real bugs / security / performance issues — these drive the pull requests.")
        _render_findings_explorer(
            job.findings, "No fixable bugs were found.", "fixable", show_blast=True,
        )
    with tab_report:
        st.caption("Informational findings (e.g. code-quality nits). These never open PRs.")
        _render_findings_explorer(
            report_only, "No report-only findings.", "report", show_blast=False, compact=True,
        )
    with tab_plan:
        _render_repair_plan(job)
    with tab_activity:
        _render_activity(job)


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
    import gc
    import shutil
    from knowledge_graph import KnowledgeGraph
    from utils.github_client import GitHubClient
    from config import settings

    gh = GitHubClient()
    _, repo_name = gh.get_repo_info(repo_url)
    hops = getattr(settings, "blast_radius_default_hops", 2)

    tmp = tempfile.mkdtemp()
    try:
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
        for source, target, attrs in graph.edges(data=True):
            if source in file_set and target in file_set and attrs.get("type") in {"IMPORTS", "TESTS"}:
                pair_weight[(source, target)] = pair_weight.get((source, target), 0) + 3
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

        result = {
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
    finally:
        # Windows-compatible cleanup
        gc.collect()
        time.sleep(0.5)
        try:
            shutil.rmtree(tmp, ignore_errors=False)
        except Exception:
            time.sleep(1.0)
            try:
                shutil.rmtree(tmp, ignore_errors=True)
            except Exception:
                pass

    return result


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
            '<div style="display:flex;align-items:center;gap:10px;margin-bottom:3px;">'
            '<div style="width:36px;height:36px;border-radius:11px;background:linear-gradient(135deg,'
            '#6366f1,#9333ea);display:flex;align-items:center;justify-content:center;box-shadow:'
            '0 8px 20px #6366f133;">◈</div><div>'
            '<div style="font-weight:850;font-size:1.05rem;line-height:1.1;color:#eef2ff;">Code Impact</div>'
            '<div style="color:#707b94;font-size:.68rem;margin-top:3px;letter-spacing:.4px;">REPAIR WORKSPACE</div>'
            '</div></div>',
            unsafe_allow_html=True,
        )
        st.divider()

        st.markdown("**Analysis target**")
        repo_url = st.text_input(
            "GitHub repository",
            placeholder="https://github.com/owner/repository",
            key="target_repo_url",
        )
        branch = st.text_input("Base branch", value="main", key="target_branch")
        if repo_url:
            if _is_github_repo_url(repo_url):
                st.caption("✓ Repository URL looks valid")
            else:
                st.caption("⚠ Use a full https://github.com/owner/repo URL")

        with st.expander("🔐 Connections", expanded=True):
            api_key = st.text_input(
                "Anthropic API Key",
                type="password",
                value=os.environ.get("ANTHROPIC_API_KEY", ""),
                help="Used for discovery, investigation, patch generation and PR summaries.",
            )
            github_token = st.text_input(
                "GitHub Token",
                type="password",
                value=os.environ.get("GITHUB_TOKEN", ""),
                help="Optional for public repository maps; required to push branches and open PRs.",
            )
        base_url = os.environ.get("ANTHROPIC_BASE_URL")
        st.markdown(
            _connections_html(api_key, github_token, base_url),
            unsafe_allow_html=True,
        )

        st.divider()
        run_clicked = st.button(
            "▶ Run full analysis", type="primary", use_container_width=True,
            help="Discover, investigate, repair, validate and prepare pull requests.",
        )
        map_clicked = st.button(
            "◌ Explore repository map", use_container_width=True,
            help="Build only the structural graph. No Anthropic key is required.",
        )

        st.divider()
        has_workspace = "repo_map" in st.session_state or "last_job" in st.session_state
        if has_workspace:
            st.markdown("**Current workspace**")
            if "last_job" in st.session_state:
                latest = st.session_state["last_job"]
                st.caption(
                    f"Latest run: {latest.repo_owner}/{latest.repo_name} · "
                    f"{latest.status.value.replace('_', ' ')}"
                )
            if "repo_map" in st.session_state:
                st.caption(f"Map: {st.session_state['repo_map']['repo']}")
            clear_clicked = st.button("Clear workspace", use_container_width=True)
        else:
            clear_clicked = False
        st.caption(
            "Source repositories are cloned into temporary directories. Generated changes are "
            "published only after validation gates pass."
        )

    if clear_clicked:
        st.session_state.pop("repo_map", None)
        st.session_state.pop("last_job", None)
        st.rerun()

    # ----- Handle "Build Repo Map" (no Anthropic key required) -----
    if map_clicked:
        if not repo_url:
            st.error("Please provide a Repository URL in the sidebar.")
            st.stop()
        if not _is_github_repo_url(repo_url):
            st.error("Use a valid GitHub URL in the form https://github.com/owner/repository.")
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
        if not _is_github_repo_url(repo_url):
            st.error("Use a valid GitHub URL in the form https://github.com/owner/repository.")
            st.stop()
        if not branch.strip():
            st.error("Please provide a base branch.")
            st.stop()

        # Make config available to the backend before it imports/instantiates settings.
        # Secrets (if present) take precedence over the sidebar, so cloud deploys with
        # secrets configured don't get overwritten by a stale sidebar box.
        if api_key and "ANTHROPIC_API_KEY" not in st.secrets:
            os.environ["ANTHROPIC_API_KEY"] = api_key
        if github_token and "GITHUB_TOKEN" not in st.secrets:
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

        _section_header(
            "Live execution", "Agent pipeline",
            f"Analyzing {repo_url.rstrip('/').split('/')[-1]} on branch {branch}.",
        )
        run_status = st.status("🚀 Starting pipeline…", expanded=False)
        stepper_box = st.empty()
        metrics_box = st.empty()
        st.markdown(
            '<div style="font-weight:800;font-size:.98rem;margin:12px 0 4px;">'
            '<span class="live-dot"></span>Live Agent Feed</div>',
            unsafe_allow_html=True,
        )
        feed_box = st.empty()

        card_htmls: list[str] = []
        error_payload = None
        start_t = time.monotonic()
        max_idx = 0

        # Initial paint so the stepper/metrics are visible before the first event.
        stepper_box.markdown(_stepper_html(max_idx), unsafe_allow_html=True)
        metrics_box.markdown(_live_metrics_html(0, 0, 0, 0), unsafe_allow_html=True)

        while True:
            try:
                item = event_queue.get(timeout=0.2)
            except queue.Empty:
                if not worker.is_alive() and event_queue.empty():
                    break
                # Keep the elapsed timer alive during quiet stretches (e.g. an LLM call).
                metrics_box.markdown(
                    _live_metrics_html(time.monotonic() - start_t, len(card_htmls),
                                       len(job.findings), len(job.pull_requests)),
                    unsafe_allow_html=True,
                )
                continue

            if item is _DONE:
                break
            if isinstance(item, tuple) and item and item[0] == "__error__":
                error_payload = item
                continue

            # Normal PipelineEvent → rich feed card (newest first, capped).
            card_htmls.append(_feed_card_html(item))
            feed_box.markdown(
                '<div class="feed-wrap">' + "".join(reversed(card_htmls[-50:])) + "</div>",
                unsafe_allow_html=True,
            )

            # Advance the stepper monotonically (status flips back to phase 4 between PRs).
            max_idx = max(max_idx, _STEP_INDEX.get(job.status.value, max_idx))
            stepper_box.markdown(_stepper_html(max_idx), unsafe_allow_html=True)
            metrics_box.markdown(
                _live_metrics_html(time.monotonic() - start_t, len(card_htmls),
                                   len(job.findings), len(job.pull_requests)),
                unsafe_allow_html=True,
            )
            run_status.update(
                label=f"⚙️ {item.agent_name or 'Orchestrator'} · {job.status.value.replace('_', ' ')}"
            )

        elapsed = time.monotonic() - start_t
        if error_payload is not None:
            run_status.update(label=f"❌ Pipeline failed after {int(elapsed)}s", state="error")
        else:
            run_status.update(label=f"✅ Pipeline complete in {int(elapsed)}s", state="complete")
            stepper_box.markdown(_stepper_html(len(STEP_PHASES)), unsafe_allow_html=True)
        metrics_box.markdown(
            _live_metrics_html(elapsed, len(card_htmls), len(job.findings), len(job.pull_requests)),
            unsafe_allow_html=True,
        )

        worker.join(timeout=1.0)

        if error_payload is not None:
            st.error(f"Pipeline failed: {error_payload[1]}")
            with st.expander("Traceback"):
                st.code(error_payload[2])

        # Persist the finished job for re-rendering across reruns.
        st.session_state["last_job"] = job

    # ----- Show the repository bubble map (latest build) -----
    if "repo_map" in st.session_state:
        _section_header(
            "Repository intelligence",
            f"Dependency map · {st.session_state['repo_map']['repo']}",
            "Select a file to reveal the consumers that may be affected by a change.",
        )
        _render_bubble_map(st.session_state["repo_map"])

    # ----- Show results (latest run) -----
    if "last_job" in st.session_state:
        _section_header(
            "Analysis report", "Results and evidence",
            "Review repair output, impact reasoning, validation signals and the full activity trail.",
        )
        _render_results(st.session_state["last_job"])

    # ----- Landing hint -----
    if "repo_map" not in st.session_state and "last_job" not in st.session_state:
        _render_setup_guide()


if __name__ == "__main__":
    main()
