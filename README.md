<div align="center">

# 🛠️ Multi-Agent Bug Detection & Auto-PR System

**Autonomous software maintenance via a 10-agent orchestrated pipeline with knowledge-graph-backed cross-file reasoning.**

Point it at a GitHub repository and it builds a semantic knowledge graph, hunts for **real** bugs (functional, security, performance, quality), plans ordered repairs, generates **surgical minimal-diff patches**, validates them, and opens **confidence-scored pull requests** with human-approval gates — all from a single Streamlit app.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B?logo=streamlit&logoColor=white)
![Claude](https://img.shields.io/badge/LLM-Claude-D97757?logo=anthropic&logoColor=white)
![NetworkX](https://img.shields.io/badge/Graph-NetworkX-2C3E50)
![License](https://img.shields.io/badge/License-MIT-green)

</div>

---

## 📑 Table of Contents

- [Why this exists](#-why-this-exists)
- [Key features](#-key-features)
- [Workflow diagram](#-workflow-diagram)
- [The 10 agents & 5 phases](#-the-10-agents--5-phases)
- [Knowledge graph](#-knowledge-graph)
- [Confidence scoring & approval gates](#-confidence-scoring--approval-gates)
- [Tech stack](#-tech-stack)
- [Project structure](#-project-structure)
- [Getting started](#-getting-started)
- [Configuration](#-configuration)
- [Using a third-party LLM proxy](#-using-a-third-party-llm-proxy-lightning-ai-etc)
- [How it works, step by step](#-how-it-works-step-by-step)
- [What it detects & language support](#-what-it-detects--language-support)
- [Limitations](#-limitations)
- [Roadmap](#-roadmap)
- [License](#-license)

---

## 🎯 Why this exists

Existing auto-fix tools (Semgrep Autofix, Patchwork, CodeRabbit) analyse each file **in isolation**. They can't answer the questions a senior engineer asks instinctively:

- *Why* does this bug exist (root cause vs. symptom)?
- *Which* modules does a change affect (blast radius)?
- *What* is the safest order to apply fixes?
- Does the fix *actually work* (tests + no new vulnerabilities)?

This system answers all four by reasoning **across files** on a knowledge graph, and it never ships a patch that hasn't passed validation. It also **won't spam you** — low-value style nits are shown as "report-only" and never become pull requests.

---

## ✨ Key features

| Capability | What it does |
|---|---|
| 🔍 **LLM Bug Hunter** | Two-stage discovery — cheap **Haiku** triages every source file, strong **Sonnet** confirms real defects with cited evidence and a concrete fix. Finds genuine bugs, not just "function is long". |
| 🕸️ **Knowledge graph** | A `networkx` directed graph of files, functions, classes, API endpoints, DB models & calls — with **imports resolved to real file→file edges** — the backbone for cross-file reasoning and blast-radius analysis. |
| 🧭 **Cross-file root cause & fixes** | Import-resolved dependency graph + data-flow tracing find where a bug *originates* — and a patch can edit the **root-cause file**, not just where the symptom surfaces. |
| 🩹 **Surgical, multi-file patches** | Anchored `SEARCH/REPLACE` edits change only what they must — **no whole-file rewrites** — and a single fix can span several files (symptom + root cause) in one PR, with a real unified diff. |
| ✅ **Validated fixes** | Four gates: AST syntax → pytest + bandit → regression (blast-radius) → differential security. Nothing ships that fails. |
| 📦 **One PR per file** | All of a file's issues are fixed and shipped together — no flood of near-duplicate PRs. |
| 🎚️ **Confidence scoring** | A 5-signal composite (0–100%) drives auto-merge eligibility vs. mandatory human review. |
| 🚦 **Approval gates** | Critical-path or low-confidence changes are opened as drafts requiring human sign-off. |
| 🧠 **Repository memory** | ChromaDB stores past fixes; similar future bugs get a confidence boost (optional, degrades gracefully). |
| 🫧 **Interactive bubble map** | Explore the repo as a force-directed graph — click any file to light up its blast radius. |

---

## 🔄 Workflow diagram

```mermaid
flowchart TB
    START(["GitHub repo URL"]) --> ORC
    ORC["🧠 Orchestrator — orchestrator.py<br/>sequencing · retries · partial-failure handling"]
    ORC --> CLONE["Phase 0 · Acquisition<br/>shallow clone into a disposable workdir"]

    CLONE --> P1_START{{"Phase 1 Start<br/>asyncio.gather spawns 3 parallel branches"}}

    subgraph B1["Branch 1 (Sequential Chain)"]
        direction TB
        A1["Agent 1 · Repo Mapper<br/>Build knowledge graph via ast.parse()<br/>Find: syntax errors, circular imports"]
        A4["Agent 4 · LLM Bug Hunter<br/>Stage A: Haiku triage (cheap, fast)<br/>Stage B: Sonnet confirm (expensive, accurate)<br/>Find: logic bugs, SQL injection, context-aware issues"]
        A1 -->|"passes graph to Agent 4"| A4
    end

    subgraph B2["Branch 2 (Independent)"]
        A2["Agent 2 · Dependency Analyzer<br/>Run: pip-audit, npm audit, safety<br/>Find: CVEs in libraries (requests, Django, etc)"]
    end

    subgraph B3["Branch 3 (Independent)"]
        A3["Agent 3 · Static Analysis<br/>Run: Bandit (Python), ESLint (JS/TS)<br/>Find: hardcoded secrets, eval(), pickle.loads()"]
    end

    P1_START -.->|"start simultaneously"| B1
    P1_START -.->|"start simultaneously"| B2
    P1_START -.->|"start simultaneously"| B3

    A4 --> GATHER
    A2 --> GATHER
    A3 --> GATHER
    GATHER{{"asyncio.gather<br/>waits for all 3 branches"}} --> DED

    DED{"Dedupe findings<br/>Split: fixable vs report-only"}

    subgraph P23["Phase 2 · Investigation → Phase 3 · Planning"]
        direction LR
        A5["Agent 5 · Bug Investigation<br/>Graph: blast radius, impact paths, data flow<br/>Memory: query ChromaDB for similar fixes<br/>LLM: root cause analysis"]
        A6["Agent 6 · Repair Planner<br/>Group by file · Topological sort<br/>Security files first"]
        A5 --> A6
    end

    DED -->|"fixable findings"| A5

    subgraph P45["Phase 4 · Fix and Validate → Phase 5 · Publication (per-file loop)"]
        direction LR
        A7["Agent 7 · Code Generation<br/>LLM generates SEARCH/REPLACE patches<br/>Can edit multiple files (cross-file fixes)"]
        A8["Agent 8 · Validation<br/>Gate 1: AST syntax check<br/>Gate 2: pytest<br/>Gate 3: Regression tests"]
        A9["Agent 9 · Security Verification<br/>Gate 4: Bandit pre/post diff"]
        A10["Agent 10 · PR Author<br/>Compute confidence score<br/>Create GitHub PR"]
        A7 --> A8 --> A9
        A9 -->|"all gates pass"| A10
    end

    A6 -->|"one file at a time"| A7
    A9 -->|"gate failed, retries < 3"| A7
    A10 -.->|"next file in plan"| A7

    DED -.->|"no fixable findings"| DONE(["COMPLETED<br/>report-only findings, no PR"])
    A9 -.->|"retries exhausted"| UNRES(["Unresolved<br/>no PR opened"])
    A10 --> PR(["Pull Request Created<br/>draft if confidence < 70%"])

    classDef orc fill:#eef2ff,stroke:#4f46e5,stroke-width:2px,color:#312e81
    classDef agent fill:#ffffff,stroke:#7c3aed,stroke-width:1.5px,color:#25324f
    classDef branch fill:#f0fdf4,stroke:#16a34a,stroke-width:2px,color:#14532d
    classDef step fill:#f8fafc,stroke:#94a3b8,stroke-width:1.4px,color:#334155
    classDef sync fill:#fef3c7,stroke:#f59e0b,stroke-width:2px,color:#78350f
    classDef dec fill:#fff7ed,stroke:#d97706,stroke-width:1.5px,color:#92400e
    classDef good fill:#ecfdf5,stroke:#059669,stroke-width:1.5px,color:#065f46
    classDef info fill:#f0f9ff,stroke:#0891b2,stroke-width:1.5px,color:#0e7490
    classDef bad fill:#fef2f2,stroke:#dc2626,stroke-width:1.5px,color:#991b1b

    class ORC orc
    class A1,A2,A3,A4,A5,A6,A7,A8,A9,A10 agent
    class B1,B2,B3 branch
    class START,CLONE step
    class P1_START,GATHER sync
    class DED dec
    class PR good
    class DONE info
    class UNRES bad
```

**Phase 1 Parallelism Explained:**

Phase 1 spawns **3 concurrent branches** (not 4 agents in parallel):

- **Branch 1 (Sequential Chain):** Agent 1 builds the knowledge graph → Agent 4 uses that graph for bug hunting
- **Branch 2 (Independent):** Agent 2 scans dependencies for CVEs (doesn't need the graph)
- **Branch 3 (Independent):** Agent 3 runs static analysis tools (doesn't need the graph)

All 3 branches start simultaneously. Total time = longest branch (typically Branch 1: Agent 1 + Agent 4).

**Why this design?** Agent 4 needs the knowledge graph to:
- Prioritize files by importance (graph in-degree)
- Send graph context to Haiku (what imports this file)
- Fetch related files for Sonnet confirmation

Agents 2 & 3 are independent — they only scan dependency files and source code patterns.


---

## 🧩 The 10 agents & 5 phases

Numbering matches the workflow diagram above.

| # | Agent | Module | Phase | What It Scans | What It Finds |
|---|-------|--------|-------|---------------|---------------|
| 1 | **Repo Mapper** | `repo_mapper.py` | 1 · Discovery | All Python/C/C++ files via `ast.parse()` and compiler | **Structural issues:** Python syntax errors, C/C++ compile errors, circular import chains. Builds the knowledge graph (files, functions, classes, imports/calls). |
| 2 | **Dependency Analyzer** | `dependency_analyzer.py` | 1 · Discovery | `requirements.txt`, `package.json`, `pyproject.toml` via pip-audit, npm audit, safety | **Library vulnerabilities:** CVEs in third-party packages (e.g., `requests==2.25.0` has CVE-2023-32681). |
| 3 | **Static Analysis** | `static_analysis.py` | 1 · Discovery | Python files via Bandit, JS/TS files via ESLint | **Known bad patterns:** hardcoded secrets, `eval()`, `pickle.loads()`, weak crypto (MD5), missing `except` clauses. Fast, deterministic, pattern-based. |
| 4 | **LLM Bug Hunter** | `llm_bug_hunter.py` | 1 · Discovery | Selected source files (prioritized by graph importance) via Haiku → Sonnet | **Context-aware bugs:** SQL injection (understands data flow), logic errors (missing return, off-by-one), broken API contracts, N+1 queries. Expensive but smart. **Chains after Agent 1** — needs the knowledge graph. |
| 5 | **Bug Investigation** | `bug_investigation.py` | 2 · Investigation | All fixable findings from Phase 1 | Enriches findings with: blast radius (affected files), impact paths, related tests, ChromaDB memory lookup, LLM root cause analysis. |
| 6 | **Repair Planner** | `repair_planner.py` | 3 · Planning | Enriched findings from Agent 5 | Groups findings by file, builds dependency graph, sorts topologically (security files first). Output: ordered repair plan (one item per file). |
| 7 | **Code Generation** | `code_generation.py` | 4 · Fix loop | Primary file + graph-selected related files | Generates minimal `SEARCH/REPLACE` patches via LLM. Can edit **multiple files** when root cause is in a dependency. |
| 8 | **Validation** | `validation_agent.py` | 4 · Fix loop | Generated patch applied to repo copy | **3 gates:** AST syntax check, pytest on related tests, regression tests on blast-radius files. |
| 9 | **Security Verification** | `security_verification.py` | 4 · Fix loop | Pre-patch vs post-patch Bandit scan | **Gate 4:** Differential security scan — confirms original vulnerability removed, no new issues introduced. |
| 10 | **PR Author** | `pr_author.py` | 5 · Publication | Validated patch + confidence score | Computes 5-signal confidence score, applies approval gate (draft if <70%), creates GitHub PR with structured description. |

The **Orchestrator** (`orchestrator.py`) sits outside this numbering: it owns sequencing,
concurrency (spawning the 3 parallel branches in Phase 1), the retry budget (max 3 attempts per fix),
and graceful partial-failure handling for all five phases.

---

## 🕸️ Knowledge graph

The graph (a `networkx.DiGraph`) is what makes cross-file reasoning possible.

```mermaid
flowchart LR
    File -->|DEFINES| Function
    File -->|DEFINES| Class
    File -->|IMPORTS| Module
    Function -->|CALLS| Function
    Function -->|SERVES_API| APIEndpoint
    Function -->|QUERIES_DB| DBModel
    Function -->|RETURNS_TO| Function
    Function -->|USES_CONFIG| ConfigVar
    Class -->|BELONGS_TO| ServiceBoundary
```

Imports are **resolved to the actual repository files they reference**, so file→file edges represent true dependencies (not just directory proximity). This is what makes blast radius and cross-file reasoning meaningful.

**Blast radius** = the set of nodes reachable within *k* hops (default 2) from a modified file. It drives which tests to run, whether a change is "critical path", and how the interactive bubble map highlights impact.

---

## 🎚️ Confidence scoring & approval gates

Each fix earns a weighted composite score (0–100%):

| Signal | Weight | Meaning |
|--------|--------|---------|
| pytest suite passes | **+40%** | Behaviour is preserved (strongest signal) |
| Post-fix security scan clean | **+25%** | Vulnerability confirmed removed, none introduced |
| AST syntax valid | **+10%** | Generated code is structurally correct |
| Memory cache hit | **+15%** | A similar past fix succeeded before |
| Fixed first (low dependency risk) | **+10%** | Topological-sort priority |

> Repos **without a test suite** are capped at **60%** and always require human approval.

| Confidence | Critical path? | Action |
|-----------|----------------|--------|
| ≥ 70% | No | Open PR **ready to merge** |
| ≥ 70% | Yes | Open **draft** + request approval (auto-merge blocked) |
| < 70% | No | Open **draft** + request approval |
| < 70% | Yes | Open **draft**, mark *requires security review* |

---

## 🧰 Tech stack

| Layer | Technology |
|-------|-----------|
| UI | **Streamlit** (single app, custom dark theme, `vis-network` bubble map) |
| LLM | **Claude** via the Anthropic SDK — Sonnet for reasoning/codegen, Haiku for triage (proxy-aware) |
| Discovery | LLM Bug Hunter + **Bandit** (Python) + **ESLint** (JS/TS) + **pip-audit / npm audit** |
| Knowledge graph | **NetworkX** + Python `ast` |
| Patching | Anchored `SEARCH/REPLACE` diff applier (`difflib`) |
| Test execution | **pytest** in a sandboxed temp copy |
| Memory | **ChromaDB** (optional, persistent, local) |
| Source control | **PyGitHub** + Git |
| Config | **Pydantic Settings** (`.env`-driven) |

---

## 📁 Project structure

```
app/
├── streamlit_app.py            # Streamlit UI — the single entry point
├── .streamlit/config.toml      # theme
├── .env                        # secrets (git-ignored)
├── backend/
│   ├── config.py               # Pydantic settings (env-driven)
│   ├── models.py               # Pydantic data models
│   ├── knowledge_graph.py      # networkx graph, blast radius, data-flow tracing
│   ├── requirements.txt
│   ├── agents/
│   │   ├── orchestrator.py         # drives the 5-phase pipeline
│   │   ├── repo_mapper.py          # Phase 1 — knowledge graph
│   │   ├── dependency_analyzer.py  # Phase 1 — CVE scan
│   │   ├── static_analysis.py      # Phase 1 — bandit + eslint
│   │   ├── llm_bug_hunter.py       # Phase 1 — Haiku triage → Sonnet confirm
│   │   ├── bug_investigation.py    # Phase 2 — root cause + blast radius
│   │   ├── repair_planner.py       # Phase 3 — group by file + topo-sort
│   │   ├── code_generation.py      # Phase 4 — anchored diffs
│   │   ├── validation_agent.py     # Phase 4 — AST + pytest + bandit
│   │   ├── security_verification.py# Phase 4 — differential scan
│   │   └── pr_author.py            # Phase 5 — one PR per file
│   └── utils/
│       ├── patcher.py              # SEARCH/REPLACE parser + applier + unified diff
│       ├── llm_client.py           # Anthropic client (base-URL / proxy aware)
│       ├── confidence_scorer.py    # 5-signal composite
│       ├── critical_path.py        # auth/crypto/security path detection
│       ├── github_client.py        # clone, branch, commit, PR
│       └── chroma_memory.py        # optional repository memory
```

---

## 🚀 Getting started

### Prerequisites
- **Python 3.11**
- **Git** on your PATH
- An **Anthropic API key** (or a compatible proxy — see below)
- *(optional)* a **GitHub token** to clone private repos / open real PRs

### Install
```bash
git clone https://github.com/<you>/<repo>.git
cd <repo>            # the folder containing streamlit_app.py
python -m venv venv
# Windows:  venv\Scripts\activate      macOS/Linux:  source venv/bin/activate
pip install -r backend/requirements.txt
```

### Configure
Create a `.env` file next to `streamlit_app.py`:
```dotenv
ANTHROPIC_API_KEY=sk-ant-...
GITHUB_TOKEN=ghp_...            # optional
# CLAUDE_MODEL_PRIMARY=claude-sonnet-4-6
# CLAUDE_MODEL_TRIAGE=claude-haiku-4-5-20251001
```

### Run
```bash
streamlit run streamlit_app.py
```
Open http://localhost:8501, paste a repository URL, and click **🫧 Build Repo Map** (no key needed) or **▶️ Run Pipeline**.

> Some deep-analysis steps shell out to external CLIs (`bandit`, `pytest`, `eslint`, `pip-audit`). Install the ones you need; the app runs without them and simply skips those checks.

---

## ⚙️ Configuration

All settings live in `backend/config.py` and can be overridden via `.env` / environment variables.

| Key | Default | Purpose |
|-----|---------|---------|
| `ANTHROPIC_API_KEY` | — | Required. Claude API key. |
| `ANTHROPIC_BASE_URL` | *(unset)* | Point at a compatible proxy instead of `api.anthropic.com`. |
| `GITHUB_TOKEN` | *(unset)* | Clone private repos / open PRs. |
| `CLAUDE_MODEL_PRIMARY` | `claude-sonnet-4-6` | Reasoning / confirmation / code-gen model. |
| `CLAUDE_MODEL_TRIAGE` | `claude-haiku-4-5-20251001` | Cheap triage model. |
| `LLM_USE_TEMPERATURE` | `false` | Some newer models reject `temperature`; keep off for those. |
| `bug_hunter_max_files` | `0` (no cap) | Cap the number of files the hunter analyses. |
| `bug_hunter_delay_seconds` | `1.0` | Pause between files to respect rate limits. |
| `fix_code_quality` | `false` | If `true`, code-quality nits are also fixed (not just reported). |
| `min_severity_to_fix` | `low` | Only PR issues at/above this severity. |
| `max_files_to_fix` | `0` (no cap) | Cap the number of PRs per run. |
| `blast_radius_default_hops` | `2` | k-hop reachability for blast radius. |
| `confidence_threshold_auto_merge` | `0.70` | Below this ⇒ draft + approval. |

---

## 🔌 Using a third-party LLM proxy (Lightning AI, etc.)

The app talks to the LLM through the Anthropic SDK, so any Anthropic-compatible endpoint works. Set the base URL, key, and model(s) in `.env`:

```dotenv
ANTHROPIC_BASE_URL=https://your-proxy.example.com/
ANTHROPIC_API_KEY=<proxy-key>
CLAUDE_MODEL_PRIMARY=claude-opus-4-8
CLAUDE_MODEL_TRIAGE=claude-opus-4-8
LLM_USE_TEMPERATURE=false
```

The Streamlit app loads `.env` into the environment at startup, so no keys need to be re-typed in the UI.

---

## 🧠 How it works, step by step

1. **Clone** the target repo into a temp directory.
2. **Discovery (parallel):** the Repo Mapper builds the knowledge graph; the Dependency Analyzer scans for CVEs; the Bug Hunter triages every file with Haiku and confirms real defects with Sonnet.
3. **Gate:** findings split into **fixable** (security / functional / performance) and **report-only** (code-quality nits — shown in the UI, never PR'd).
4. **Investigation:** each fixable finding gets a root cause, blast radius, affected modules, and a memory lookup.
5. **Planning:** findings are grouped by file and topologically ordered (security first).
6. **Fix–Validate loop:** Code Gen produces minimal anchored edits — touching the symptom file and, when the root cause is elsewhere, the related file(s) — → Validation (AST + pytest + bandit + regression on every changed file) → Security Verification (differential scan across all changed files). On failure, the errors are fed back and it retries (≤3).
7. **Publication:** for each validated file, PR Author computes the confidence score, applies the critical-path/approval gate, and opens **one pull request per file** with a structured description, confidence badge, blast-radius summary, and the unified diff.

Throughout, the **Orchestrator** streams every agent event live to the Streamlit feed and handles partial failures without aborting the run.

---

## 🐛 What it detects & language support

### Error types

#### 1. Functional Bugs
| Example | Description |
|---|---|
| `None`/`null` dereference | Accessing a variable that could be None/null |
| Wrong condition | `>=` instead of `>`, inverted boolean logic |
| Off-by-one | Loop bounds wrong, index out of range |
| Missing edge case | Empty list, zero division, negative input not handled |
| Resource leak | File handles, DB connections left open |
| Broken API contract | Wrong return type, missing required field |

#### 2. Security Vulnerabilities
| Example | Description |
|---|---|
| SQL injection | `f"SELECT * FROM users WHERE id={user_input}"` |
| Command injection | `os.system(cmd)` with unvalidated user input |
| Path traversal | `open(user_path)` without sanitization |
| Hardcoded secrets | API keys or passwords embedded in source code |
| Unsafe deserialization | `pickle.loads(user_data)` from untrusted input |
| Weak cryptography | MD5/SHA1 for passwords, `random` instead of `secrets` |
| Missing authorization | No auth check before a sensitive operation |

#### 3. Performance Issues
| Example | Description |
|---|---|
| N+1 queries | Database query inside a loop |
| Hot-loop waste | Expensive computation repeated on every iteration |
| Unbounded memory growth | Appending to a list forever with no eviction |
| Quadratic blowup | Nested loops iterating over the same dataset |

#### 4. Code Quality *(report-only — shown in UI, no PR opened by default)*
- Dead code — unreachable branches or unused variables
- Duplicate logic — same computation copy-pasted across functions

> **Promote to fixable:** set `FIX_CODE_QUALITY=true` in `.env` to also open PRs for quality issues.

---

### Language support

| Language | LLM Bug Hunter | Static Analysis | Dependency Scan | Symbol Graph |
|---|---|---|---|---|
| **Python** | ✅ Full | ✅ Bandit | ✅ pip-audit, safety | ✅ Full AST — functions, classes, imports, call chains |
| **JavaScript** | ✅ Full | ✅ ESLint | ✅ npm audit | ⚠️ File-level only |
| **TypeScript** | ✅ Full | ✅ ESLint | ✅ npm audit | ⚠️ File-level only |
| **JSX / TSX** | ✅ Full | ✅ ESLint | ✅ npm audit | ⚠️ File-level only |
| **C / C++** | ✅ LLM only | ❌ | ❌ | ⚠️ `#include` edges only |
| **Go / Rust / Java** | ❌ | ❌ | ❌ | ❌ |

**What the tiers mean:**

- **Full symbol graph (Python)** — imports are resolved to real file→file edges, function calls are traced across files, the right tests are selected after a fix, and cross-file root-cause patches are possible.
- **File-level (JS/TS)** — the LLM reads and analyses code, ESLint/npm-audit run, but import chains are not resolved to real files so blast-radius and cross-file reasoning are coarser.
- **LLM only (C/C++)** — the model reads and flags bugs; only `#include "file.h"` edges are in the graph; no dependency scanner.

---

## ⚠️ Limitations

- **Language support:** optimised for **Python** (AST, pytest, bandit); **JS/TS** is secondary (ESLint, npm audit). Java/Go/Rust would need extra parsers.
- **Tests required for high confidence:** without a test suite, confidence is capped at 60% and every PR needs human approval.
- **Dynamic code:** heavy runtime magic (monkey-patching, metaclass factories) is conservatively over-approximated rather than fully resolved.
- **Cost:** scanning every file with an LLM has a token cost; use `bug_hunter_max_files` / `min_severity_to_fix` to bound it.

---

## 🗺️ Roadmap

- Sandboxed Docker execution for full CI parity
- Auto-merge for confidence ≥ 95% on non-critical paths
- Multi-repository / org-level scanning with shared memory
- 3D force-directed upgrade of the bubble map
- Full Java / Go / Rust support

---

## 📄 License

Released under the **MIT License**. See [`LICENSE`](LICENSE).

<div align="center">
<sub>Built with Claude · NetworkX · Streamlit</sub>
</div>
