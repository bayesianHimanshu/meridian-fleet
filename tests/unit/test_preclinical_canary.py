"""The preclinical leak check's canary (phase 5 step 1b): planted in a copy of the SEND
cut, in MI's MIORRES, a column the agent never reads, so it is in the input bytes but can
never reach a finding."""

from pathlib import Path

import structlog

from meridian.agents.base import AgentContext
from meridian.agents.preclinical.tox_study_monitor import ToxStudyMonitor
from meridian.data.send import write_send_cut
from meridian.data.study_store import SendStudyReader
from meridian.platform.clock import FixedClock
from meridian.preclinical.synth import STUDY, Edit, StudySpec, build
from meridian.sandbox.substrate_proofs import make_copies
from tests.support import T0
from tests.tox_support import CUT, config, send_trigger

CANARY = "MERIDIAN-CANARY-test0123456789"


def test_the_canary_is_in_the_send_copy_and_never_in_a_finding(tmp_path: Path) -> None:
    src = tmp_path / "src"
    tumour = Edit(
        kind="mi_finding",
        sex="M",
        group=3,
        animal=0,
        specimen="LIVER",
        finding="HEPATOCELLULAR CARCINOMA",
        category="MALIGNANT",
    )
    tables = build(StudySpec(seed=81, edits=(tumour,)))
    tables["mi"] = tables["mi"].with_columns(tables["mi"]["MISTRESC"].alias("MIORRES"))
    write_send_cut(
        send_root=src / "send", study_id=STUDY, cut_id=CUT, tables=tables, created_at=T0, sources=()
    )
    for area in ("curated", "safety", "etmf"):
        for part in ("blinded", "ops"):
            (src / area / "S" / "C" / part).mkdir(parents=True)
    roots = make_copies(
        canary=CANARY,
        dest=tmp_path / "copy",
        study_id="S",
        cut_id="C",
        curated_root=src / "curated",
        safety_root=src / "safety",
        etmf_root=src / "etmf",
        audit_root=src / "audit",
        audit_seq=None,
        send=(src / "send", STUDY, CUT),
    )
    assert roots.send_root is not None
    mi = roots.send_root / STUDY / CUT / "send" / "mi.parquet"
    assert CANARY.encode() in mi.read_bytes()
    assert roots.planted[-1].endswith("mi.parquet")
    ctx = AgentContext(
        study=SendStudyReader(roots.send_root / STUDY, CUT),
        clock=FixedClock(T0),
        config=config(),
        log=structlog.get_logger(),
    )
    drafts = list(ToxStudyMonitor().run(send_trigger(), ctx))
    assert any("neoplasm:" in d.dedup_key for d in drafts)  # the planted row was read
    assert all(CANARY not in d.model_dump_json() for d in drafts)
