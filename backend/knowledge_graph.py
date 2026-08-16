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
        # Python files that failed to parse: {"file", "line", "column", "message",
        # "text"}. Populated in _pass2_ast; the Repo Mapper turns these into findings.
        self.syntax_errors: list[dict] = []
        self._trees: dict[str, ast.AST] = {}
        self._source_by_file: dict[str, str] = {}
        self._functions_by_file: dict[str, dict[str, str]] = {}
        self._import_aliases: dict[str, dict[str, str]] = {}
        # Cached consumer→dependency maps. The graph is immutable after build(),
        # and blast_radius() is called once per file by the bubble map, so
        # recomputing this per call made large repositories quadratic.
        self._dep_maps_cache: tuple | None = None

    # ── Graph Construction ────────────────────────────────────────────

    def build(self) -> nx.DiGraph:
        """Build the complete knowledge graph in 4 passes."""
        self._pass1_structure()
        self._pass2_ast()
        self._pass3_framework()
        self._pass4_dynamic()
        self._dep_maps_cache = None  # graph just changed; drop any stale cache
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
                # Graph keys are always repository-relative POSIX paths. Keeping
                # one canonical representation prevents Windows absolute paths and
                # mixed separators from breaking graph lookups and de-duplication.
                rel_path = file_path.relative_to(self.repo_path).as_posix()
                lang = self._detect_language(file)
                if lang:
                    self._language = lang

                try:
                    size = file_path.stat().st_size
                except OSError:
                    # Broken symlink or unreadable file — skip it instead of
                    # aborting the whole repository scan.
                    continue

                self.graph.add_node(
                    rel_path, type="File", path=rel_path,
                    language=lang, size=size,
                    service_boundary=str(rel_root.parts[0]) if rel_root.parts else "root"
                )

                # BELONGS_TO edge
                if rel_root.parts:
                    parent = str(rel_root.parts[0])
                    if parent in self.graph and parent != rel_path:
                        self.graph.add_edge(rel_path, parent, type="BELONGS_TO")

    def _pass2_ast(self) -> None:
        """Parse Python files with AST; index C/C++ includes.

        A file that fails to parse is recorded in ``self.syntax_errors`` (exact
        line + message from the real parser) instead of being silently skipped —
        a syntax error is itself a high-severity functional bug.
        """
        self._build_module_index()
        for node, attrs in list(self.graph.nodes(data=True)):
            if attrs.get("type") != "File":
                continue
            lang = attrs.get("language")

            if lang in ("c", "cpp"):
                self._extract_cpp_includes(node)
                continue
            if lang != "python":
                continue

            file_path = self.repo_path / node
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                tree = ast.parse(content)
            except SyntaxError as e:
                self.syntax_errors.append({
                    "file": node,
                    "line": e.lineno,
                    "column": e.offset,
                    "message": e.msg or "invalid syntax",
                    "text": (e.text or "").rstrip(),
                })
                continue
            except UnicodeDecodeError:
                continue

            self._trees[node] = tree
            self._source_by_file[node] = content
            self._index_python_symbols(node, tree, content)

        # Definitions must exist before calls are resolved. This second pass is
        # what allows a caller to reference a function declared later in the file.
        for file_node, tree in self._trees.items():
            self._import_aliases[file_node] = {}
            for item in ast.walk(tree):
                if isinstance(item, (ast.Import, ast.ImportFrom)):
                    self._extract_imports(file_node, item)
            self._extract_calls(file_node, tree)

        self._add_test_edges()

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
                ".java": "java", ".c": "c", ".h": "c",
                ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp",
                ".hpp": "cpp", ".hh": "cpp"}.get(ext)

    def _extract_cpp_includes(self, file_node: str) -> None:
        """Draw file→file edges for local ``#include "..."`` directives."""
        file_path = self.repo_path / file_node
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return
        for match in re.finditer(r'^\s*#\s*include\s+"([^"]+)"', content, re.MULTILINE):
            header = match.group(1)
            # Resolve relative to the including file's dir, then the repo root.
            # Node keys use native OS separators — normalize candidates to match.
            for base in (Path(file_node).parent, None):
                cand_path = (base / header) if base is not None else Path(header)
                cand = Path(*cand_path.parts).as_posix()
                if cand in self.graph and cand != file_node:
                    self.graph.add_edge(file_node, cand, type="IMPORTS", resolved=True)
                    break

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

    def _index_python_symbols(self, file_node: str, tree: ast.AST, content: str) -> None:
        """Index top-level functions, classes, and class methods.

        Symbol IDs include the class name for methods, so two classes can both
        define ``save`` without being collapsed into the same graph node.
        """
        symbols: dict[str, str] = {}
        for item in getattr(tree, "body", []):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbol_id = self._extract_function(file_node, item, content, item.name)
                symbols[item.name] = symbol_id
            elif isinstance(item, ast.ClassDef):
                self._extract_class(file_node, item, content)
                for child in item.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        qualified = f"{item.name}.{child.name}"
                        symbol_id = self._extract_function(
                            file_node, child, content, qualified, owner_class=item.name,
                        )
                        symbols[qualified] = symbol_id
        self._functions_by_file[file_node] = symbols

    def _extract_imports(self, file_node: str, node: ast.AST) -> None:
        candidates: list[str] = []
        aliases = self._import_aliases.setdefault(file_node, {})
        if isinstance(node, ast.Import):
            for alias in node.names:
                self.graph.add_edge(file_node, alias.name.split(".")[0], type="IMPORTS")
                candidates.append(alias.name)
                aliases[alias.asname or alias.name.split(".")[0]] = alias.name
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
                    aliases[alias.asname or alias.name] = f"{module}.{alias.name}"

        # Resolve module paths to real repo files → file→file dependency edges.
        for mod in candidates:
            target = self._resolve_module_to_file(mod)
            if target and target != file_node:
                self.graph.add_edge(file_node, target, type="IMPORTS", resolved=True)

    def _extract_calls(self, file_node: str, tree: ast.AST) -> None:
        """Resolve common Python call shapes into symbol/file dependencies.

        This intentionally handles the understandable, high-value cases:
        local functions, ``self.method()``, imported functions, and
        ``module.function()``. Dynamic reflection remains low-confidence.
        """
        indexed: list[tuple[ast.AST, Optional[str]]] = []
        for item in getattr(tree, "body", []):
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                indexed.append((item, None))
            elif isinstance(item, ast.ClassDef):
                indexed.extend(
                    (child, item.name) for child in item.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )

        for item, owner_class in indexed:
            qualified = f"{owner_class}.{item.name}" if owner_class else item.name
            caller = self._functions_by_file.get(file_node, {}).get(qualified)
            if not caller:
                continue
            for child in ast.walk(item):
                if not isinstance(child, ast.Call):
                    continue
                target = self._resolve_call_target(file_node, child.func, owner_class)
                if target and target != caller:
                    self.graph.add_edge(caller, target, type="CALLS", resolved=True)

    def _resolve_call_target(
        self, file_node: str, func: ast.AST, owner_class: Optional[str],
    ) -> Optional[str]:
        symbols = self._functions_by_file.get(file_node, {})
        aliases = self._import_aliases.get(file_node, {})

        if isinstance(func, ast.Name):
            if func.id in symbols:
                return symbols[func.id]
            imported = aliases.get(func.id)
            return self._resolve_imported_symbol(imported) if imported else None

        if not isinstance(func, ast.Attribute):
            return None
        chain = self._attribute_chain(func)
        if not chain:
            return None
        if chain[0] in ("self", "cls") and owner_class and len(chain) == 2:
            return symbols.get(f"{owner_class}.{chain[1]}")

        imported = aliases.get(chain[0])
        if imported:
            suffix = ".".join(chain[1:])
            return self._resolve_imported_symbol(
                ".".join(part for part in (imported, suffix) if part)
            )
        return None

    @staticmethod
    def _attribute_chain(node: ast.AST) -> list[str]:
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
            return list(reversed(parts))
        return []

    def _resolve_imported_symbol(self, dotted: Optional[str]) -> Optional[str]:
        if not dotted:
            return None
        parts = dotted.split(".")
        for cut in range(len(parts) - 1, 0, -1):
            module = ".".join(parts[:cut])
            symbol = ".".join(parts[cut:])
            target_file = self._resolve_module_to_file(module)
            if not target_file:
                continue
            target_symbol = self._functions_by_file.get(target_file, {}).get(symbol)
            return target_symbol or target_file
        return self._resolve_module_to_file(dotted)

    def _add_test_edges(self) -> None:
        """Mark explicit test-file to production-file relationships."""
        for file_node, attrs in list(self.graph.nodes(data=True)):
            if attrs.get("type") != "File" or attrs.get("language") != "python":
                continue
            name = Path(file_node).name
            if not (name.startswith("test_") or name.endswith("_test.py")):
                continue
            attrs["is_test"] = True
            for target in list(self.graph.successors(file_node)):
                if self.graph.nodes[target].get("type") == "File":
                    self.graph.add_edge(file_node, target, type="TESTS", resolved=True)

    def _extract_function(
        self,
        file_node: str,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        content: str,
        qualified_name: Optional[str] = None,
        owner_class: Optional[str] = None,
    ) -> str:
        qualified_name = qualified_name or node.name
        func_id = f"{file_node}::{qualified_name}"
        lines = content.splitlines()
        signature = lines[node.lineno - 1][:80] if node.lineno <= len(lines) else ""

        # Complexity: count branching
        complexity = 1 + sum(1 for _ in ast.walk(node) if isinstance(_, (ast.If, ast.For, ast.While, ast.ExceptHandler)))

        self.graph.add_node(
            func_id, type="Function", name=node.name, qualified_name=qualified_name,
            signature=signature, line_start=node.lineno,
            line_end=node.end_lineno or node.lineno,
            decorators=[d.id if isinstance(d, ast.Name) else str(d) for d in node.decorator_list],
            complexity=complexity, file=file_node, owner_class=owner_class,
        )
        self.graph.add_edge(file_node, func_id, type="DEFINES")
        return func_id

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

    def analyze_change(self, file: str, hops: int = 2) -> dict[str, Any]:
        """Return directional, explainable impact for a changed Python file.

        File dependency edges point from consumer to dependency. Impact therefore
        travels in the reverse direction: if ``orders.py`` imports ``money.py``, a
        change in ``money.py`` can affect ``orders.py``. Returned paths make that
        reasoning visible in the UI and in LLM prompts.
        """
        file = file.replace("\\", "/")
        if file not in self.graph or self.graph.nodes[file].get("type") != "File":
            return {
                "changed_file": file, "direct_dependencies": [],
                "direct_dependents": [], "transitive_dependents": [],
                "affected_files": [], "related_tests": [], "impact_paths": [],
                "crosses_service_boundary": False, "service_boundaries": [],
                "confidence": "low",
            }

        dependencies, dependents, evidence = self._file_dependency_maps()
        direct_dependencies = sorted(dependencies.get(file, set()))
        direct_dependents_all = sorted(dependents.get(file, set()))

        paths: list[list[str]] = []
        visited = {file}
        frontier: list[tuple[str, list[str]]] = [(file, [file])]
        for _ in range(max(0, hops)):
            next_frontier: list[tuple[str, list[str]]] = []
            for current, path in frontier:
                for consumer in sorted(dependents.get(current, set())):
                    if consumer in visited:
                        continue
                    visited.add(consumer)
                    new_path = path + [consumer]
                    paths.append(new_path)
                    next_frontier.append((consumer, new_path))
            frontier = next_frontier

        impacted = [p[-1] for p in paths]
        tests = sorted({
            f for f in impacted + direct_dependents_all
            if self.graph.nodes.get(f, {}).get("is_test")
        })
        production = [f for f in impacted if f not in tests]
        direct_production = [f for f in direct_dependents_all if f not in tests]

        boundaries = {
            self.graph.nodes[f].get("service_boundary")
            for f in [file] + production
            if self.graph.nodes[f].get("service_boundary")
        }
        used_relations = {
            evidence.get((path[i + 1], path[i]), "")
            for path in paths for i in range(len(path) - 1)
        }
        confidence = "high" if used_relations & {"CALLS", "TESTS"} else (
            "medium" if paths else "low"
        )
        return {
            "changed_file": file,
            "direct_dependencies": direct_dependencies,
            "direct_dependents": direct_production,
            "transitive_dependents": production,
            "affected_files": production,
            "related_tests": tests,
            "impact_paths": paths,
            "crosses_service_boundary": len(boundaries) > 1,
            "service_boundaries": sorted(boundaries),
            "confidence": confidence,
        }

    def _file_dependency_maps(
        self,
    ) -> tuple[dict[str, set[str]], dict[str, set[str]], dict[tuple[str, str], str]]:
        """Collapse file and symbol edges into consumer -> dependency maps."""
        if self._dep_maps_cache is not None:
            return self._dep_maps_cache
        dependencies: dict[str, set[str]] = {}
        dependents: dict[str, set[str]] = {}
        evidence: dict[tuple[str, str], str] = {}

        def file_of(node: str) -> Optional[str]:
            attrs = self.graph.nodes.get(node, {})
            if attrs.get("type") == "File":
                return node
            return attrs.get("file")

        for source, target, attrs in self.graph.edges(data=True):
            relation = attrs.get("type")
            if relation not in {"IMPORTS", "TESTS", "CALLS"}:
                continue
            consumer = file_of(source)
            dependency = file_of(target)
            if not consumer or not dependency or consumer == dependency:
                continue
            if self.graph.nodes.get(consumer, {}).get("type") != "File":
                continue
            if self.graph.nodes.get(dependency, {}).get("type") != "File":
                continue
            dependencies.setdefault(consumer, set()).add(dependency)
            dependents.setdefault(dependency, set()).add(consumer)
            evidence[(consumer, dependency)] = relation
        self._dep_maps_cache = (dependencies, dependents, evidence)
        return self._dep_maps_cache

    def related_files(self, file: str, hops: int = 2, limit: int = 6) -> list[str]:
        """Files worth showing to code generation, ordered by relevance."""
        impact = self.analyze_change(file, hops=hops)
        ordered = (
            impact["direct_dependencies"]
            + impact["direct_dependents"]
            + impact["related_tests"]
            + impact["transitive_dependents"]
        )
        out: list[str] = []
        for candidate in ordered:
            if candidate != file and candidate not in out:
                out.append(candidate)
        return out[:limit]

    def context_summary(self, file: str, hops: int = 2) -> str:
        """Small, interview-explainable context block for LLM analysis."""
        impact = self.analyze_change(file, hops=hops)
        funcs = sorted(
            a.get("qualified_name", a.get("name", ""))
            for _, a in self.graph.nodes(data=True)
            if a.get("type") == "Function" and a.get("file") == file
        )
        return (
            f"Defined symbols: {', '.join(funcs[:20]) or 'none'}\n"
            f"Direct dependencies: {', '.join(impact['direct_dependencies'][:8]) or 'none'}\n"
            f"Direct dependents: {', '.join(impact['direct_dependents'][:8]) or 'none'}\n"
            f"Related tests: {', '.join(impact['related_tests'][:8]) or 'none'}"
        )

    def blast_radius(self, file: str, hops: int = 2) -> dict[str, Any]:
        """Compute blast radius of a file modification."""
        impact = self.analyze_change(file, hops=hops)
        files = impact["affected_files"]
        related_nodes: set[str] = set()
        for affected_file in [file] + files:
            if affected_file in self.graph:
                related_nodes.update(self.graph.successors(affected_file))
        for node in list(related_nodes):
            if self.graph.nodes[node].get("type") == "Function":
                related_nodes.update(self.graph.successors(node))
        functions = [n for n in related_nodes if self.graph.nodes[n].get("type") == "Function"]
        apis = [n for n in related_nodes if self.graph.nodes[n].get("type") == "APIEndpoint"]
        db_models = [n for n in related_nodes if self.graph.nodes[n].get("type") == "DBModel"]

        # Check service boundary crossing
        file_boundaries = set(impact["service_boundaries"])
        crosses_boundary = impact["crosses_service_boundary"]

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
            "direct_dependents": impact["direct_dependents"],
            "related_tests": impact["related_tests"],
            "impact_paths": impact["impact_paths"],
            "confidence": impact["confidence"],
        }

    def trace_data_flow(self, file: str, line: int | None = None) -> dict[str, Any]:
        """Trace symbol callers/callees and file dependencies around a location."""
        upstream = set()
        downstream = set()

        # Find functions in the file
        funcs = [
            n for n, a in self.graph.nodes(data=True)
            if a.get("file") == file and a.get("type") == "Function"
            and (
                line is None
                or int(a.get("line_start", 0)) <= line <= int(a.get("line_end", 0))
            )
        ]
        if not funcs and line is not None:
            funcs = [n for n, a in self.graph.nodes(data=True)
                     if a.get("file") == file and a.get("type") == "Function"]

        for func in funcs:
            # Upstream: who calls this function
            for caller in self.graph.predecessors(func):
                if self.graph.edges[caller, func].get("type") == "CALLS":
                    upstream.add(caller)
            # Downstream: what this function calls
            for callee in self.graph.successors(func):
                if self.graph.edges[func, callee].get("type") in ("CALLS", "QUERIES_DB"):
                    downstream.add(callee)

        impact = self.analyze_change(file)

        return {
            "upstream_callers": list(upstream),
            "downstream_callees": list(downstream),
            "entry_functions": funcs,
            "file_dependencies": impact["direct_dependencies"],
            "file_dependents": impact["direct_dependents"],
            "impact_paths": impact["impact_paths"],
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
