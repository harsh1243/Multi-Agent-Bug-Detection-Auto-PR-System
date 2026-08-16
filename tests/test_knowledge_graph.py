from knowledge_graph import KnowledgeGraph


def _write(repo, relative, content):
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_directional_cross_file_impact_and_related_tests(tmp_path):
    _write(tmp_path, "core.py", "def calculate(value):\n    return value * 2\n")
    _write(
        tmp_path,
        "service.py",
        "from core import calculate\n\ndef total(value):\n    return calculate(value)\n",
    )
    _write(
        tmp_path,
        "api.py",
        "import service\n\ndef handler(value):\n    return service.total(value)\n",
    )
    _write(
        tmp_path,
        "tests/test_service.py",
        "from service import total\n\ndef test_total():\n    assert total(2) == 4\n",
    )

    kg = KnowledgeGraph(str(tmp_path), "example")
    kg.build()

    impact = kg.analyze_change("core.py", hops=3)
    assert impact["direct_dependents"] == ["service.py"]
    assert "api.py" in impact["affected_files"]
    assert "tests/test_service.py" in impact["related_tests"]
    assert ["core.py", "service.py", "api.py"] in impact["impact_paths"]

    reverse = kg.analyze_change("api.py", hops=3)
    assert "service.py" in reverse["direct_dependencies"]
    assert "service.py" not in reverse["affected_files"]
    assert "core.py" not in reverse["affected_files"]


def test_self_method_calls_are_symbol_edges(tmp_path):
    _write(
        tmp_path,
        "worker.py",
        "class Worker:\n"
        "    def run(self):\n"
        "        return self.validate()\n\n"
        "    def validate(self):\n"
        "        return True\n",
    )
    kg = KnowledgeGraph(str(tmp_path), "example")
    graph = kg.build()

    assert graph.has_edge("worker.py::Worker.run", "worker.py::Worker.validate")
    assert graph.edges["worker.py::Worker.run", "worker.py::Worker.validate"]["type"] == "CALLS"

