"""Knowledge graph builder and query engine using networkx."""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path
from typing import Any, Optional

import networkx as nx

from config import settings


class KnowledgeGraph:
    """Directed knowledge graph of a repository for cross-file reasoning."""

    def __init__(self, repo_path: str, repo_name: str):
        self.repo_path = Path(repo_path).resolve()
        self.repo_name = repo_name
        self.graph = nx.DiGraph()
        self._framework: Optional[str] = None
        self._language: Optional[str] = None

    # ── Graph Construction ────────────────────────────────────────────

    def build(self) -> nx.DiGraph:
        """Build the complete knowledge graph in 4 passes."""
        self._pass1_structure()
        self._pass2_ast()
        self._pass3_framework()
        self._pass4_dynamic()
        return self.graph

    def _pass1_structure(self) -> None:
        """Create File nodes and ServiceBoundary nodes from directory structure."""
        for root, dirs, files in os.walk(self.repo_path):
            # Skip hidden dirs, venv, node_modules, etc.
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in {
                "venv", "__pycache__", "node_modules", ".git", "build", "dist"
            }]

            rel_root = Path(root).relative_to(self.repo_path)

            # Create service boundaries from top-level dirs
            if rel_root.parts and len(rel_root.parts) == 1:
                boundary = str(rel_root.parts[0])
                if boundary not in self.graph:
                    self.graph.add_node(
                        boundary, type="ServiceBoundary",
                        name=boundary, entry_points=[], external_deps=[]
                    )

            for file in files:
                file_path = Path(root) / file
                rel_path = str(file_path.relative_to(self.repo_path))
                lang = self._detect_language(file)
                if lang:
                    self._language = lang

                self.graph.add_node(
                    rel_path, type="File", path=rel_path,
                    language=lang, size=file_path.stat().st_size,
                    service_boundary=str(rel_root.parts[0]) if rel_root.parts else "root"
                )

                # BELONGS_TO edge
                if rel_root.parts:
                    parent = str(rel_root.parts[0])
                    if parent in self.graph and parent != rel_path:
                        self.graph.add_edge(rel_path, parent, type="BELONGS_TO")

    def _pass2_ast(self) -> None:
        """Parse Python files with AST."""
        self._build_module_index()
        for node, attrs in list(self.graph.nodes(data=True)):
            if attrs.get("type") != "File" or attrs.get("language") != "python":
                continue

            file_path = self.repo_path / node
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content)
            except (SyntaxError, UnicodeDecodeError):
                continue

            for item in ast.walk(tree):
                if isinstance(item, (ast.Import, ast.ImportFrom)):
                    self._extract_imports(node, item)
                elif isinstance(item, ast.FunctionDef) or isinstance(item, ast.AsyncFunctionDef):
                    self._extract_function(node, item, content)
                elif isinstance(item, ast.ClassDef):
                    self._extract_class(node, item, content)

    def _pass3_framework(self) -> None:
        """Extract framework-specific patterns (FastAPI, Django, Flask, ORMs)."""
        for node, attrs in list(self.graph.nodes(data=True)):
            if attrs.get("type") != "File":
                continue

            file_path = self.repo_path / node
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue

            # FastAPI routes
            self._extract_fastapi_routes(node, content)
            # Flask routes
            self._extract_flask_routes(node, content)
            # Django URLs
            self._extract_django_urls(node, content)
            # ORM queries
            self._extract_orm_queries(node, content)
            # Config vars
            self._extract_config_usage(node, content)

    def _pass4_dynamic(self) -> None:
        """Mark edges with low confidence for dynamic patterns."""
        for u, v, attrs in self.graph.edges(data=True):
            edge_type = attrs.get("type", "")
            if edge_type in ("CALLS", "QUERIES_DB"):
                # Check if target was resolved via string/import fallback
                if attrs.get("resolved", True) is False:
                    attrs["confidence"] = "low"
                else:
                    attrs["confidence"] = "high"

    # ── Extractors ────────────────────────────────────────────────────

    def _detect_language(self, file: str) -> Optional[str]:
        ext = Path(file).suffix
        return {".py": "python", ".js": "javascript", ".ts": "typescript",
                ".jsx": "jsx", ".tsx": "tsx", ".go": "go", ".rs": "rust",
                ".java": "java"}.get(ext)

    def _build_module_index(self) -> None:
        """Index python files by importable module path + stem, for import resolution.

        Enables mapping an ``import a.b.c`` / ``from a.b import c`` to the actual repo
        file node, so we can draw real file→file dependency edges (not just edges to
        bare module-name nodes). That is what makes blast radius meaningful.
        """
        self._file_by_module: dict[str, str] = {}
        self._file_by_stem: dict[str, list[str]] = {}
        for n, a in self.graph.nodes(data=True):
            if a.get("type") != "File" or a.get("language") != "python":
                continue
            parts = Path(n).with_suffix("").parts
            for i in range(len(parts)):
                self._file_by_module.setdefault(".".join(parts[i:]), n)
            stem = Path(n).stem
            self._file_by_stem.setdefault(stem, []).append(n)
            if stem == "__init__":
                pkg = ".".join(Path(n).parent.parts)
                if pkg:
                    self._file_by_module.setdefault(pkg, n)

    def _resolve_module_to_file(self, mod: str) -> Optional[str]:
        """Best-effort map an import path (e.g. 'pkg.mod' or 'mod') to a repo file."""
        idx = getattr(self, "_file_by_module", None)
        if not mod or idx is None:
            return None
        mod = mod.replace("/", ".").strip(".")
        if mod in idx:
            return idx[mod]
        parts = mod.split(".")
        for i in range(1, len(parts)):
            cand = ".".join(parts[i:])
            if cand in idx:
                return idx[cand]
        matches = self._file_by_stem.get(parts[-1], [])
        return matches[0] if len(matches) == 1 else None

    def _extract_imports(self, file_node: str, node: ast.AST) -> None:
        candidates: list[str] = []
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.graph.add_edge(file_node, alias.name.split(".")[0], type="IMPORTS")
                candidates.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and node.level > 0:
                # Relative import: resolve against this file's package directory.
                base = Path(file_node).parent.parts
                keep = base[: len(base) - (node.level - 1)] if node.level - 1 > 0 else base
                prefix = ".".join(keep)
                module = ".".join(p for p in (prefix, module) if p)
            if module:
                self.graph.add_edge(file_node, module.split(".")[0], type="IMPORTS")
                candidates.append(module)
                for alias in node.names:
                    candidates.append(f"{module}.{alias.name}")

        # Resolve module paths to real repo files → file→file dependency edges.
        for mod in candidates:
            target = self._resolve_module_to_file(mod)
            if target and target != file_node:
                self.graph.add_edge(file_node, target, type="IMPORTS", resolved=True)

    def _extract_function(self, file_node: str, node: ast.FunctionDef | ast.AsyncFunctionDef, content: str) -> None:
        func_id = f"{file_node}::{node.name}"
        lines = content.splitlines()
        signature = lines[node.lineno - 1][:80] if node.lineno <= len(lines) else ""

        # Complexity: count branching
        complexity = 1 + sum(1 for _ in ast.walk(node) if isinstance(_, (ast.If, ast.For, ast.While, ast.ExceptHandler)))

        self.graph.add_node(
            func_id, type="Function", name=node.name,
            signature=signature, line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            decorators=[d.id if isinstance(d, ast.Name) else str(d) for d in node.decorator_list],
            complexity=complexity, file=file_node,
        )
        self.graph.add_edge(file_node, func_id, type="DEFINES")

        # CALLS edges
        for child in ast.walk(node):
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                target = f"{file_node}::{child.func.id}"
                if target in self.graph:
                    self.graph.add_edge(func_id, target, type="CALLS")

    def _extract_class(self, file_node: str, node: ast.ClassDef, content: str) -> None:
        class_id = f"{file_node}::{node.name}"
        bases = [b.id if isinstance(b, ast.Name) else ast.dump(b) for b in node.bases]
        methods = [n.name for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]

        self.graph.add_node(
            class_id, type="Class", name=node.name,
            bases=bases, methods=methods,
            line_start=node.lineno, line_end=node.end_lineno or node.lineno,
            file=file_node,
        )
        self.graph.add_edge(file_node, class_id, type="DEFINES")

    def _extract_fastapi_routes(self, file_node: str, content: str) -> None:
        pattern = r'@\w+\.(get|post|put|delete|patch|head|options)\s*\(\s*["\']([^"\']+)'
        for match in re.finditer(pattern, content, re.IGNORECASE):
            method, route = match.groups()
            ep_id = f"API:{route}:{method.upper()}"
            self.graph.add_node(ep_id, type="APIEndpoint", route=route, method=method.upper(), auth_required=False)
            self.graph.add_edge(file_node, ep_id, type="SERVES_API")

    def _extract_flask_routes(self, file_node: str, content: str) -> None:
        pattern = r'@\w+\.route\s*\(\s*["\']([^"\']+)'
        for match in re.finditer(pattern, content):
            route = match.group(1)
            ep_id = f"API:{route}:FLASK"
            self.graph.add_node(ep_id, type="APIEndpoint", route=route, method="ANY", auth_required=False)
            self.graph.add_edge(file_node, ep_id, type="SERVES_API")

    def _extract_django_urls(self, file_node: str, content: str) -> None:
        pattern = r'path\s*\(\s*["\']([^"\']+)'
        for match in re.finditer(pattern, content):
            route = match.group(1)
            ep_id = f"API:{route}:DJANGO"
            self.graph.add_node(ep_id, type="APIEndpoint", route=route, method="ANY", auth_required=False)
            self.graph.add_edge(file_node, ep_id, type="SERVES_API")

    def _extract_orm_queries(self, file_node: str, content: str) -> None:
        patterns = [
            r'(\w+)\.objects\.(filter|get|all|create|update)',
            r'session\.query\s*\(\s*(\w+)',
            r'\.query\s*\(\s*(\w+)',
            r'select\s*\(\s*(\w+)',
        ]
        for pat in patterns:
            for match in re.finditer(pat, content):
                model = match.group(1)
                model_id = f"DB:{model}"
                self.graph.add_node(model_id, type="DBModel", table_name=model)
                # Link to the first function in file as querier
                for n, a in self.graph.nodes(data=True):
                    if a.get("file") == file_node and a.get("type") == "Function":
                        self.graph.add_edge(n, model_id, type="QUERIES_DB")
                        break

    def _extract_config_usage(self, file_node: str, content: str) -> None:
        patterns = [
            r'os\.environ\.get\s*\(\s*["\']([^"\']+)',
            r'os\.environ\[["\']([^"\']+)',
            r'config\[["\']([^"\']+)',
            r'config\.get\s*\(\s*["\']([^"\']+)',
        ]
        for pat in patterns:
            for match in re.finditer(pat, content):
                key = match.group(1)
                cfg_id = f"CFG:{key}"
                self.graph.add_node(cfg_id, type="ConfigVar", key=key, source_file=file_node)
                self.graph.add_edge(file_node, cfg_id, type="USES_CONFIG")

    # ── Query Methods ─────────────────────────────────────────────────

    def get_neighbours(self, file: str, hop: int = 1) -> set[str]:
        """Get all nodes within `hop` hops from `file`."""
        if file not in self.graph:
            return set()
        nodes = {file}
        for _ in range(hop):
            new_nodes = set()
            for n in nodes:
                new_nodes.update(self.graph.predecessors(n))
                new_nodes.update(self.graph.successors(n))
            nodes |= new_nodes
        return nodes - {file}

    def blast_radius(self, file: str, hops: int = 2) -> dict[str, Any]:
        """Compute blast radius of a file modification."""
        affected = self.get_neighbours(file, hops)
        files = [n for n in affected if self.graph.nodes[n].get("type") == "File"]
        functions = [n for n in affected if self.graph.nodes[n].get("type") == "Function"]
        apis = [n for n in affected if self.graph.nodes[n].get("type") == "APIEndpoint"]
        db_models = [n for n in affected if self.graph.nodes[n].get("type") == "DBModel"]

        # Check service boundary crossing
        file_boundaries = {
            self.graph.nodes[n].get("service_boundary")
            for n in files if self.graph.nodes[n].get("service_boundary")
        }
        crosses_boundary = len(file_boundaries) > 1

        category = "narrow" if len(files) <= 2 else "moderate" if len(files) <= 8 else "wide"

        return {
            "affected_files": files,
            "affected_functions": functions,
            "affected_apis": apis,
            "affected_db_models": db_models,
            "file_count": len(files),
            "function_count": len(functions),
            "api_count": len(apis),
            "crosses_service_boundary": crosses_boundary,
            "service_boundaries": list(file_boundaries),
            "category": category,
        }

    def trace_data_flow(self, file: str, line: int | None = None) -> dict[str, Any]:
        """Trace data flow upstream and downstream from a file."""
        upstream = set()
        downstream = set()

        # Find functions in the file
        funcs = [n for n, a in self.graph.nodes(data=True)
                 if a.get("file") == file and a.get("type") == "Function"]

        for func in funcs:
            # Upstream: who calls this function
            for caller in self.graph.predecessors(func):
                if self.graph.edges[caller, func].get("type") == "CALLS":
                    upstream.add(caller)
            # Upstream data sources
            for pred in self.graph.predecessors(func):
                if self.graph.edges[pred, func].get("type") in ("QUERIES_DB", "SERVES_API"):
                    upstream.add(pred)
            # Downstream: what this function calls
            for callee in self.graph.successors(func):
                if self.graph.edges[func, callee].get("type") == "CALLS":
                    downstream.add(callee)

        return {
            "upstream_callers": list(upstream),
            "downstream_callees": list(downstream),
            "entry_functions": funcs,
        }

    def to_dict(self) -> dict:
        """Serialize graph to dict."""
        return {
            "nodes": [
                {"id": n, **{k: str(v) if isinstance(v, Path) else v for k, v in a.items()}}
                for n, a in self.graph.nodes(data=True)
            ],
            "edges": [
                {"source": u, "target": v, **d}
                for u, v, d in self.graph.edges(data=True)
            ],
        }

    def save(self, path: Path) -> None:
        """Save graph to GraphML."""
        nx.write_graphml(self.graph, str(path))

    @classmethod
    def load(cls, path: Path, repo_path: str, repo_name: str) -> "KnowledgeGraph":
        """Load graph from GraphML."""
        kg = cls(repo_path, repo_name)
        kg.graph = nx.read_graphml(str(path))
        return kg
