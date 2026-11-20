"""scripts/public_scan.py: the identifier guard (hook, check.sh, CI) and the export's full
scan. A match is reported by rule, file and line, never by its value."""

import importlib.util
import sys
from pathlib import Path

import pytest

_spec = importlib.util.spec_from_file_location("public_scan", Path("scripts/public_scan.py"))
assert _spec is not None
assert _spec.loader is not None
ps = importlib.util.module_from_spec(_spec)
sys.modules["public_scan"] = ps
_spec.loader.exec_module(ps)

BILLING = "0A1B2C-3D4E5F-60718A"  # billing-shaped, made up
NUMBER = "123456789012"  # 12 digits, made up
PROJECT = "meridian-example-777"


def _tree(root: Path, files: dict[str, str | bytes]) -> Path:
    for name, body in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            p.write_bytes(body)
        else:
            p.write_text(body)
    return root


def test_the_guard_refuses_billing_and_12_digit_numbers_in_guarded_dirs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {
        "docs/a.md": f"line one\naccount {BILLING}\n",
        "src/b.py": f'NUMBER = "{NUMBER}"\n',
        "deploy/c.yaml": "ok: true\n",
        "tests/d.py": f'FAKE = "{NUMBER}"\n',  # outside the guarded dirs
        "uv.lock": f"size = {NUMBER}\n",  # allowed
        "src/e.py": 'u = "01890000-0000-7000-8000-000000000001"\n',  # a UUID group
    })  # fmt: skip
    assert ps.main(["--identifiers", "--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "refused: a billing account pattern: docs/a.md:2" in err
    assert "refused: a 12-digit number: src/b.py:1" in err
    assert err.count("refused:") == 2
    assert BILLING not in err
    assert NUMBER not in err


def test_the_guard_refuses_the_exact_values_of_env_sh_and_never_prints_them(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, {
        "deploy/gke/env.sh": f'export PROJECT_ID="{PROJECT}"\n'
                             'export BUCKET_NAME="${PROJECT_ID}-ate-snapshots"\n'
                             'export REGION="us-central1"\n',
        "README.md": f"see {PROJECT}-ate-snapshots\n",
        "tests/t.py": "region = 'us-central1'\n",  # not an identifying variable
    })  # fmt: skip
    assert ps.main(["--identifiers", "--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "the value of PROJECT_ID in deploy/gke/env.sh: README.md:1" in err
    assert "the value of BUCKET_NAME in deploy/gke/env.sh: README.md:1" in err
    assert "env.sh: deploy/gke/env.sh" not in err  # the file itself is not scanned
    assert PROJECT not in err
    assert "tests/t.py" not in err


def test_the_full_scan_refuses_secrets_emails_home_paths_and_large_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    key = "sk-ant-" + "x" * 30
    # Built at run time, so this file itself passes the full scan of an export.
    email = "someone" + "@" + "realmail.net"
    home = "/" + "home/someone/x"
    root = _tree(tmp_path, {
        "a.txt": f"key {key}\n",
        "b.md": f"mail {email}\nok t@example.invalid and noreply@anthropic.com\n",
        "c.sh": f"cd {home}\n",
        "big.bin": b"x" * (5 * 1024 * 1024 + 1),
        "d.py": 'PREFIX = "sk-ant-"  # a prefix check, not a key\n',
    })  # fmt: skip
    assert ps.main(["--full", "--root", str(root)]) == 1
    err = capsys.readouterr().err
    assert "an Anthropic key: a.txt:1" in err
    assert "a real email address: b.md:1" in err
    assert "a home path: c.sh:1" in err
    assert "a file over 5 MB: big.bin" in err
    assert err.count("refused:") == 4
    assert key not in err
    assert email not in err


def test_a_clean_tree_passes(tmp_path: Path) -> None:
    root = _tree(tmp_path, {"docs/a.md": "nothing here\n", "src/x.py": "x = 1\n"})
    assert ps.main(["--full", "--root", str(root)]) == 0


def test_an_export_is_checked_against_another_tree_s_env_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _tree(tmp_path / "private", {"deploy/gke/env.sh": f'PROJECT_NUMBER="{NUMBER}"\n'})
    export = _tree(tmp_path / "export", {"tests/x.py": f"n = {NUMBER}\n"})
    assert ps.main(["--full", "--root", str(export)]) == 0  # no env file of its own
    capsys.readouterr()
    args = ["--full", "--root", str(export), "--env-file", str(env / "deploy/gke/env.sh")]
    assert ps.main(args) == 1
    assert "the value of PROJECT_NUMBER" in capsys.readouterr().err
