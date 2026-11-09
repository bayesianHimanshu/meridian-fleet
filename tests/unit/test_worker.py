"""meridian-worker: canonical JSON on stdout, logs on stderr, exit codes."""

from pathlib import Path

import pytest
import structlog

from meridian import worker
from meridian.agents.catalog import build_registry
from meridian.agents.execute import DraftsDocument, drafts_document, run_agent
from meridian.data.study_store import ParquetStudyReader
from meridian.ledger.hashing import canonical_json
from meridian.sandbox.staging import stage_cut
from tests.rbqm_support import STUDY
from tests.worker_support import cut_trigger, tiny_cut

REPO = Path(__file__).resolve().parents[2]
CONFIG = REPO / "config" / "default.toml"
AGENTS = ["rbqm.csm", "rbqm.kri_monitor", "rbqm.qtl_detector"]


@pytest.fixture
def staged(tmp_path: Path) -> Path:
    cut = tiny_cut(tmp_path / "curated")
    return stage_cut(cut, cut_trigger(), tmp_path / "stage")


def _args(stage: Path, agent: str = "rbqm.csm") -> list[str]:
    return [
        "run",
        "--agent",
        agent,
        "--trigger",
        str(stage / "meta" / "trigger.json"),
        "--cut-dir",
        str(stage),
        "--config",
        str(CONFIG),
    ]


@pytest.mark.parametrize("agent", AGENTS)
def test_prints_the_in_process_drafts_as_canonical_json(
    staged: Path, tmp_path: Path, agent: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert worker.main(_args(staged, agent)) == worker.EXIT_OK
    out = capsys.readouterr()
    doc = DraftsDocument.loads(out.out)
    assert out.out == canonical_json(doc.model_dump(mode="json"))
    # Same drafts as in process on the full cut (manifest and all).
    registered = build_registry(worker.load_agents_settings(CONFIG)).get(agent)
    reader = ParquetStudyReader(tmp_path / "curated" / STUDY, "DC-TINY")
    expected = drafts_document(
        registered, run_agent(registered, cut_trigger(), reader, structlog.get_logger())
    )
    assert out.out == expected
    assert doc.agent_id == agent
    assert "worker.done" in out.err  # logs go to stderr only


def test_bad_arguments_exit_2(staged: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as info:
        worker.main(["run", "--agent", "rbqm.csm"])
    assert info.value.code == worker.EXIT_BAD_ARGS
    assert worker.main(_args(staged, "rbqm.nope")) == worker.EXIT_BAD_ARGS
    assert capsys.readouterr().out == ""


def test_missing_data_exits_3(staged: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (staged / "blinded" / "dm.parquet").unlink()
    assert worker.main(_args(staged)) == worker.EXIT_DATA
    args = _args(staged)
    args[args.index("--trigger") + 1] = str(staged / "missing.json")
    assert worker.main(args) == worker.EXIT_DATA
    assert capsys.readouterr().out == ""


def test_agent_error_exits_4(
    staged: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from meridian.agents.rbqm.csm import Csm

    def boom(*_: object) -> None:
        raise RuntimeError("agent bug")

    monkeypatch.setattr(Csm, "run", boom)
    assert worker.main(_args(staged)) == worker.EXIT_AGENT
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "agent bug" in captured.err


def test_the_worker_reads_the_safety_terms_next_to_its_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In the sandbox the working directory is not the repo: the terms table is found next
    to the config file, not under ./config."""
    import shutil

    from meridian.platform.agent_settings import SAFETY_TERMS_FILE

    config_dir = tmp_path / "opt" / "config"
    config_dir.mkdir(parents=True)
    shutil.copy(CONFIG, config_dir / "default.toml")
    shutil.copy(CONFIG.parent / SAFETY_TERMS_FILE, config_dir / SAFETY_TERMS_FILE)
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("MERIDIAN_CONFIG_FILE", raising=False)
    agents = worker.load_agents_settings(config_dir / "default.toml")
    assert agents.sae_reconciler.terms.outcome_of("Recovered") == "RECOVERED/RESOLVED"
