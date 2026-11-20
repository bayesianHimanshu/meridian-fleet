"""scripts/publish-demo-release.sh in a throwaway repository, with a fake gh on PATH that
only records its arguments: what it checks, what it uploads, and that it never makes a tag."""

import hashlib
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path("scripts/publish-demo-release.sh").resolve()
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
       "-c", "init.defaultBranch=main", "-c", "commit.gpgsign=false",
       "-c", "tag.gpgsign=false"]  # fmt: skip


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    origin = tmp_path / "remote" / "bayesianHimanshu" / "meridian-fleet.git"
    origin.mkdir(parents=True)
    _git(origin, "init", "--bare", "-q")
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-q")
    assets = tmp_path / "assets"
    assets.mkdir()
    lines = []
    for name, body in (("demo.mp4", b"video"), ("captions.vtt", b"WEBVTT\n"),
                       ("transcript.json", b"{}\n")):  # fmt: skip
        (assets / name).write_bytes(body)
        lines.append(f"{hashlib.sha256(body).hexdigest()}  {name}")
    (work / "docs" / "demo").mkdir(parents=True)
    (work / "docs" / "releases").mkdir(parents=True)
    (work / "docs" / "demo" / "assets.sha256").write_text("\n".join(lines) + "\n")
    (work / "docs" / "demo" / "index.html").write_text("<video src=demo.mp4></video>\n")
    (work / "docs" / "releases" / "v9.9.9.md").write_text("notes\n")
    (work / "scripts").mkdir()
    shutil.copy(SCRIPT, work / "scripts" / SCRIPT.name)
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "one")
    _git(work, "tag", "v9.9.9")
    _git(work, "remote", "add", "origin", str(origin))
    _git(work, "push", "-q", "origin", "main", "v9.9.9")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(
        '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$GH_LOG"\n'
        'for a in "$@"; do case "$a" in *.zip) cp "$a" "$GH_ZIP";; esac; done\n'
    )
    gh.chmod(0o755)
    return work, assets, bin_dir


def _run(work: Path, assets: Path, bin_dir: Path, answer: str, tag: str = "v9.9.9"
         ) -> subprocess.CompletedProcess[str]:  # fmt: skip
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}",
           "GH_LOG": str(work.parent / "gh.log"), "GH_ZIP": str(work.parent / "up.zip"),
           "GIT_CONFIG_GLOBAL": "/dev/null"}  # fmt: skip
    return subprocess.run(["bash", "scripts/publish-demo-release.sh", tag, str(assets)],
                          cwd=work, env=env, input=answer + "\n", capture_output=True,
                          text=True, check=False)  # fmt: skip


def test_it_uploads_the_three_files_and_the_offline_zip_after_yes(
    repo: tuple[Path, Path, Path],
) -> None:
    work, assets, bin_dir = repo
    r = _run(work, assets, bin_dir, "yes")
    assert r.returncode == 0, r.stderr
    args = (work.parent / "gh.log").read_text().split("\n")
    assert args[:5] == ["release", "create", "v9.9.9", "--repo", "bayesianHimanshu/meridian-fleet"]
    assert "--verify-tag" in args  # gh may never create the tag
    assert "docs/releases/v9.9.9.md" in args
    uploaded = [Path(a).name for a in args if a.endswith((".mp4", ".vtt", ".json", ".zip"))]
    assert uploaded == ["demo.mp4", "captions.vtt", "transcript.json", "meridian-demo-v9.9.9.zip"]
    with zipfile.ZipFile(work.parent / "up.zip") as z:
        assert sorted(z.namelist()) == ["meridian-demo-v9.9.9/", "meridian-demo-v9.9.9/demo.mp4",
                                        "meridian-demo-v9.9.9/index.html"]  # fmt: skip
    assert 'Type "yes"' in r.stdout
    assert _git(work, "tag") == "v9.9.9\n"  # no tag made


def test_without_yes_nothing_is_created(repo: tuple[Path, Path, Path]) -> None:
    work, assets, bin_dir = repo
    r = _run(work, assets, bin_dir, "no")
    assert r.returncode == 2
    assert "nothing created" in r.stderr
    assert not (work.parent / "gh.log").exists()


def test_a_changed_asset_or_a_missing_tag_is_refused(repo: tuple[Path, Path, Path]) -> None:
    work, assets, bin_dir = repo
    (assets / "captions.vtt").write_bytes(b"WEBVTT\nchanged\n")
    r = _run(work, assets, bin_dir, "yes")
    assert r.returncode == 2
    assert "do not match docs/demo/assets.sha256" in r.stderr
    r = _run(work, assets, bin_dir, "yes", tag="v9.9.8")
    assert r.returncode == 2
    assert "no local tag v9.9.8" in r.stderr
    assert not (work.parent / "gh.log").exists()
