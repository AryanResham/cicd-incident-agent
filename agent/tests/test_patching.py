import pytest

from agent import patching
from agent.llm import Edit
from agent.patching import PatchError, PatchRejected


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "main.py").write_text("import os\n\nPORT = 1\n\ndef f():\n    return PORT\n")
    (tmp_path / "requirements.txt").write_text("fastapi\n")
    (tmp_path / "tests" / "test_main.py").write_text("def test_x():\n    assert False\n")
    return tmp_path


@pytest.mark.parametrize("path, allowed", [
    ("app/main.py", True), ("app/static/index.html", True), ("requirements.txt", True),
    ("requirements-dev.txt", True), ("Dockerfile", True), (".dockerignore", True),
    ("tests/test_main.py", False), (".github/workflows/ci-cd.yml", False), ("agent/fix.py", False),
    ("app/tests/test_x.py", False), ("app/test_hack.py", False), ("app/conftest.py", False),
    ("../etc/passwd", False), ("/etc/passwd", False), ("README.md", False), ("pytest.ini", False),
    ("scenarios/S1/scenario.json", False),
])
def test_allow_list(path, allowed):
    assert (patching.path_problem(path) is None) is allowed


def test_apply_writes_files_and_reports_changes(repo):
    changes = patching.apply_edits(repo, [Edit("app/main.py", "PORT = 1", "PORT = 8000"),
                                          Edit("requirements.txt", "fastapi\n", "fastapi\nrequests\n")])
    assert (repo / "app" / "main.py").read_text().count("PORT = 8000") == 1
    assert changes["requirements.txt"] == ("fastapi\n", "fastapi\nrequests\n")
    diff = patching.unified_diff(changes)
    assert "-PORT = 1" in diff and "+requests" in diff


def test_multiple_edits_to_the_same_file_chain(repo):
    changes = patching.compute_changes(repo, [Edit("app/main.py", "PORT = 1", "PORT = 2"),
                                              Edit("app/main.py", "PORT = 2", "PORT = 3")])
    old, new = changes["app/main.py"]
    assert "PORT = 1" in old and "PORT = 3" in new


def test_editing_tests_is_rejected_and_nothing_is_written(repo):  # E2
    with pytest.raises(PatchRejected, match="tests"):
        patching.apply_edits(repo, [Edit("app/main.py", "PORT = 1", "PORT = 2"),
                                    Edit("tests/test_main.py", "assert False", "assert True")])
    assert "PORT = 1" in (repo / "app" / "main.py").read_text()


def test_search_not_found_or_ambiguous(repo):
    with pytest.raises(PatchError, match="not found"):
        patching.compute_changes(repo, [Edit("app/main.py", "PORT = 99", "x")])
    with pytest.raises(PatchError, match="ambiguous"):
        patching.compute_changes(repo, [Edit("app/main.py", "PORT", "X")])


def test_crlf_files_still_match(repo):
    (repo / "app" / "main.py").write_bytes(b"A = 1\r\nB = 2\r\n")
    changes = patching.compute_changes(repo, [Edit("app/main.py", "A = 1\nB = 2", "A = 1\nB = 3")])
    assert changes["app/main.py"][1] == "A = 1\nB = 3\n"


def test_new_file_with_empty_search(repo):
    changes = patching.apply_edits(repo, [Edit("app/config.py", "", "X = 1\n")])
    assert (repo / "app" / "config.py").read_text() == "X = 1\n"
    assert changes["app/config.py"] == ("", "X = 1\n")
    with pytest.raises(PatchError, match="empty search"):
        patching.compute_changes(repo, [Edit("app/main.py", "", "x")])


def test_missing_file(repo):
    with pytest.raises(PatchError, match="does not exist"):
        patching.compute_changes(repo, [Edit("app/nope.py", "a", "b")])


def test_size_caps(repo):
    with pytest.raises(PatchRejected, match="too big"):
        patching.compute_changes(repo, [Edit("app/main.py", "PORT = 1", "\n".join(f"X{i} = {i}" for i in range(200)))])
    with pytest.raises(PatchRejected, match="too many edits"):
        patching.compute_changes(repo, [Edit("app/main.py", "a", "b")] * 11)
    with pytest.raises(PatchRejected, match="empty"):
        patching.compute_changes(repo, [])
