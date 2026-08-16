from utils.chroma_memory import RepositoryMemory


def test_memory_ids_are_stable_and_repository_scoped():
    first = RepositoryMemory._fix_id(
        "Owner/Repo", "functional_bug", "backend\\service.py", "Wrong boundary check",
    )
    same = RepositoryMemory._fix_id(
        "owner/repo", "FUNCTIONAL_BUG", "backend/service.py", "wrong boundary check",
    )
    other_repo = RepositoryMemory._fix_id(
        "owner/other", "functional_bug", "backend/service.py", "wrong boundary check",
    )

    assert first == same
    assert first != other_repo
