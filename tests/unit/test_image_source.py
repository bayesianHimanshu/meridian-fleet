"""The agent image names the commit it was built from, and deploy refuses a dirty one
(phase 4a step 3, item 4)."""

import subprocess
from pathlib import Path

import pytest

from meridian.sandbox.image import ImageInfo, source_state
from meridian.sandbox.substrate_deploy import DeployError, require_clean_source


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / ".gitignore").write_text("__pycache__/\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def _info(revision: str | None, dirty: bool | None) -> ImageInfo:
    return ImageInfo("meridian-agent:0.1.0", "sha256:" + "a" * 64, 1, "1000", revision, dirty)


def test_a_committed_tree_is_clean(repo: Path) -> None:
    state = source_state(repo)
    assert state.commit == _git(repo, "rev-parse", "HEAD").strip()
    assert not state.dirty


@pytest.mark.parametrize("change", ["modify", "stage", "untracked", "delete"])
def test_any_change_to_an_image_source_is_dirty(repo: Path, change: str) -> None:
    if change == "modify":
        (repo / "src" / "a.py").write_text("x = 2\n")
    elif change == "stage":
        (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
        _git(repo, "add", "pyproject.toml")
    elif change == "untracked":
        (repo / "src" / "b.py").write_text("y = 1\n")
    else:
        (repo / "src" / "a.py").unlink()
    assert source_state(repo).dirty


def test_files_outside_the_image_and_ignored_files_do_not_count(repo: Path) -> None:
    (repo / "design").mkdir()
    (repo / "design" / "notes.md").write_text("draft\n")
    (repo / "src" / "__pycache__").mkdir()
    (repo / "src" / "__pycache__" / "a.pyc").write_bytes(b"\0")
    assert not source_state(repo).dirty


def test_deploy_takes_only_a_labelled_clean_image_of_a_known_commit(repo: Path) -> None:
    head = _git(repo, "rev-parse", "HEAD").strip()
    assert require_clean_source(_info(head, False), repo) == head
    with pytest.raises(DeployError, match="dirty tree"):
        require_clean_source(_info(head, True), repo)
    with pytest.raises(DeployError, match="has no org"):
        require_clean_source(_info(None, None), repo)
    with pytest.raises(DeployError, match="not in this repo"):
        require_clean_source(_info("f" * 40, False), repo)
