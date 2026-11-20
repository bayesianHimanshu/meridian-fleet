"""meridian stats (wrap-up): the numbers come from the ledger and nothing else."""

import json
from pathlib import Path

from meridian.data.datacut import record_data_cut
from meridian.domain.enums import DecisionAction, EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import EventDraft, FindingRaised, HumanDecision
from meridian.domain.findings import Decision, Evidence, Finding
from meridian.platform.clock import FixedClock
from meridian.project_stats import compute, markdown
from tests.support import T0, make_ledger


def test_counts_events_findings_decisions_and_leak_check_files(tmp_path: Path) -> None:
    (tmp_path / "leakcheck").mkdir()
    (tmp_path / "leakcheck" / "kind-20261120T000000Z.json").write_text(
        json.dumps({"passed": True, "runs": [{}, {}], "failures": []})
    )
    with make_ledger(tmp_path / "l.sqlite3", FixedClock(T0)) as ledger:
        for cut in ("DC-BASE", "DC-NULL-S1"):
            record_data_cut(ledger, clock=FixedClock(T0), actor="system.ingest",
                            study_id="MRD-201", cut_id=cut, path=Path(cut))  # fmt: skip
        finding = Finding(
            kind=FindingKind.KRI_BREACH, agent_id="rbqm.kri_monitor", agent_version="2.2.0",
            study_id="MRD-201", site_id="1001", severity=Severity.MEDIUM, title="t",
            summary="s", evidence=(Evidence(source="x", selector="y", statistic="z",
                                            value=1.0, threshold=None),),
            visibility=Visibility.BLINDED, visible_to=frozenset({Role.CLINICAL_OPS_LEAD}),
            score=1.0,
        )  # fmt: skip
        raised = ledger.append(
            EventDraft(occurred_at=T0, actor="rbqm.kri_monitor", study_id="MRD-201",
                       visibility=Visibility.BLINDED,
                       payload=FindingRaised(finding=finding, dedup_key="k"))
        )  # fmt: skip
        assert isinstance(raised.payload, FindingRaised)
        fid = raised.payload.finding.finding_id
        assert fid is not None
        ledger.append(
            EventDraft(occurred_at=T0, actor="asha.rao", study_id="MRD-201",
                       visibility=Visibility.BLINDED,
                       payload=HumanDecision(decision=Decision(
                           finding_id=fid, action=DecisionAction.ACCEPTED, actor="asha.rao",
                           role=Role.CLINICAL_OPS_LEAD)))
        )  # fmt: skip
        s = compute(
            ledger,
            operational_cuts=("DC-BASE",),
            people={"asha.rao": "clinical_ops_lead"},
            var_dir=tmp_path,
        )
    assert s["computed_at_seq"] == 4
    assert s["verification"]["ok"] is True
    assert s["events"]["by_type"] == {
        EventType.DATA_CUT.value: 2,
        EventType.FINDING_RAISED.value: 1,
        EventType.HUMAN_DECISION.value: 1,
    }
    assert s["findings"]["by_zone_and_status"] == {"rbqm": {"accepted": 1}}
    assert s["findings"]["by_agent_version"] == {"rbqm.kri_monitor 2.2.0": 1}
    assert s["human_decisions"]["by_role_and_action"] == {"clinical_ops_lead: accepted": 1}
    assert s["human_decisions"]["seqs"] == {"clinical_ops_lead: accepted": "4"}
    assert (s["leak_checks"]["run"], s["leak_checks"]["passed"]) == (1, 1)
    assert s["leak_checks"]["files"][0]["runs"] == 2
    page = markdown(s)
    assert "seq **4**" in page
    assert "clinical_ops_lead: accepted | 1 | 4" in page
