from agents.code_generation import CodeGenerationAgent
from utils.patcher import Edit, apply_edits


def test_search_replace_rejects_ambiguous_anchor():
    result = apply_edits("value = 1\nvalue = 1\n", [Edit("value = 1", "value = 2")])
    assert not result.ok
    assert result.applied == 0
    assert "ambiguous" in result.errors[0]


def test_multi_edit_patch_is_atomic_when_one_edit_fails():
    response = """### FILE: module.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
### FILE: module.py
<<<<<<< SEARCH
missing = True
=======
missing = False
>>>>>>> REPLACE
"""
    result = CodeGenerationAgent()._build_result(
        "module.py", {"module.py": "value = 1\n"}, response,
    )

    assert not result.ok
    assert result.edits_applied == 1
    assert result.edits_total == 2
    assert result.errors


def test_multi_file_patch_succeeds_only_when_every_edit_applies():
    response = """### FILE: first.py
<<<<<<< SEARCH
value = 1
=======
value = 2
>>>>>>> REPLACE
### FILE: second.py
<<<<<<< SEARCH
enabled = False
=======
enabled = True
>>>>>>> REPLACE
"""
    result = CodeGenerationAgent()._build_result(
        "first.py",
        {"first.py": "value = 1\n", "second.py": "enabled = False\n"},
        response,
    )

    assert result.ok
    assert result.edits_applied == result.edits_total == 2
    assert result.changed_files == ["first.py", "second.py"]


def test_code_generation_rejects_paths_outside_repository(tmp_path):
    try:
        CodeGenerationAgent._safe_repo_path(tmp_path, "../outside.py")
    except ValueError:
        pass
    else:
        raise AssertionError("path traversal should be rejected")
