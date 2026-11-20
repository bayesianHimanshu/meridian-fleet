"""The Pages workflow (.github/workflows/pages.yml), read as text (no YAML parser is a
dependency): its triggers, its only permissions, actions pinned by full commit sha, the
release's assets checked against the manifest, and nothing committed but the page."""

import re
from pathlib import Path

WF = Path(".github/workflows/pages.yml").read_text(encoding="utf-8")


def test_triggers_are_a_published_release_or_a_manual_run_with_a_tag() -> None:
    assert re.search(
        r"^on:\n  release:\n    types: \[published\]\n  workflow_dispatch:\n", WF, re.M
    )
    assert "pull_request" not in WF
    assert "push:" not in WF


def test_permissions_are_exactly_contents_read_pages_write_id_token_write() -> None:
    block = re.search(r"^permissions:\n((?:  .*\n)+)", WF, re.M)
    assert block
    assert block.group(1) == "  contents: read\n  pages: write\n  id-token: write\n"
    assert WF.count("permissions:") == 1
    assert "concurrency:\n  group: pages\n  cancel-in-progress: false\n" in WF


def test_every_action_is_pinned_to_a_full_commit_sha_with_its_version() -> None:
    uses = re.findall(r"uses: (\S+)(.*)", WF)
    assert {u.split("@")[0] for u, _ in uses} == {
        "actions/checkout", "actions/configure-pages",
        "actions/upload-pages-artifact", "actions/deploy-pages",
    }  # fmt: skip
    for action, comment in uses:
        assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", action), action
        assert re.fullmatch(r" # v\d+\.\d+\.\d+", comment), comment


def test_the_site_is_the_release_s_verified_assets_and_the_page_only() -> None:
    assert "gh release download" in WF
    assert "sha256sum --check --strict ../docs/demo/assets.sha256" in WF
    assert "cp docs/demo/index.html _site/index.html" in WF
    assert "printf '%s\\n' .nojekyll captions.vtt demo.mp4 index.html transcript.json" in WF
    # The tag reaches shell only through the environment, never as an expression in run:.
    runs = re.findall(r"run: \|\n((?:          .*\n)+)", WF)
    assert runs
    assert all("${{" not in r for r in runs)


def test_the_video_is_never_committed() -> None:
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, text=True, check=True
    ).stdout.split("\0")
    assert not [f for f in tracked if f.endswith((".mp4", ".wav", ".onnx", ".zip"))]
