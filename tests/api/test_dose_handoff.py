"""The hand-off to the clinic (phase 5 step 2), end to end through the API and the runner:
no proposal without a NOAEL decision; a NOAEL wakes preclinical.dose_translation; the
medical monitor decides the proposal and the toxicologist whose NOAEL it rests on may not;
an accepted proposal is MRD-101's planned first-in-human starting dose with its chain
(phase 5 step 3a: not MRD-110's, a separate asset); a revised NOAEL
gives a new proposal that supersedes the old; "not established" gives no starting dose;
and the IND readiness checklist with its percentage, live on the portfolio."""

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from meridian.agents.catalog import build_registry
from meridian.agents.runner import AgentRunner, RunReport
from meridian.api.app import create_app
from meridian.api.models import AssetStatic, AssetStatus
from meridian.api.repository import PortfolioEntry
from meridian.data.send import record_send_study
from meridian.data.study_store import SendStudyReader
from meridian.domain.enums import FindingKind, Role
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.preclinical.inputs import record_inventory, write_inventory, write_noael_extract
from meridian.preclinical.inventory import INVENTORY_FILE
from meridian.preclinical.synth import STUDY as TOX
from meridian.preclinical.synth import Effect, StudySpec
from meridian.sandbox.executor import InProcessExecutor
from tests.api_support import Fixture, build, headers
from tests.support import T0
from tests.tox_support import CUT, write_study

ROOT = Path(__file__).resolve().parents[2]
SPEC = StudySpec(
    seed=61,
    effects=(
        Effect(endpoint="lb:ALT", factors=(1, 1.1, 1.6, 2.5)),
        Effect(endpoint="mi:LIVER:HYPERTROPHY", counts=(0, 0, 3, 8)),
    ),
)
RATIONALE = (
    "ALT is higher with dose from 30 mg/kg/day and comes with hepatocellular hypertrophy at "
    "30 and 100 mg/kg/day, considered adverse together. Nothing of note at 10: no group "
    "there differs from the concurrent control by the pre-registered rule."
)
CLINICAL = (Role.CLINICAL_OPS_LEAD, Role.UNBLINDED_STATISTICIAN)
PROPOSAL_ROLES = (Role.TOXICOLOGIST, Role.MEDICAL_MONITOR, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR)
STATIC = AssetStatic(
    title="FIH",
    stage="phase_1",
    indication="Healthy volunteers",
    intervention=None,
    milestone="x",
    milestone_due="x",
    progress_percent=1,
    probability_of_success=None,
    status=AssetStatus(level="good", text="x"),
)


def _runner(fx: Fixture, ledger: Any) -> AgentRunner:
    settings = Settings()
    registry = build_registry(settings.agents)
    send = fx.root / "send" / TOX

    def prepare(trigger: Any, cut: str, decisions: Any) -> None:
        write_noael_extract(decisions, trigger, cut, fx.root / "noael")

    return AgentRunner(
        ledger=ledger,
        registry=registry,
        executor=InProcessExecutor(
            registry=registry,
            reader_factory=lambda c: SendStudyReader(send, c),
            send_reader_factory=lambda c: SendStudyReader(send, c),
            noael_root=fx.root / "noael",
            inventory_root=fx.root / "inventory",
            log=structlog.get_logger(),
        ),
        clock=FixedClock(T0),
        study_id="MRD-201",
        log=structlog.get_logger(),
        operational_cuts=(),
        operational_executors=("inprocess",),  # the inventory is operational; tests run here
        prepare_noael=prepare,
    )


def run(fx: Fixture, agents: set[str]) -> RunReport:
    with fx.ledger() as ledger:
        return _runner(fx, ledger).run_pending(agents=agents)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    fx = build(tmp_path / "fx")
    fx.root = tmp_path
    write_study(tmp_path / "send", SPEC)
    with fx.ledger() as ledger:
        record_send_study(
            ledger,
            occurred_at=T0,
            actor="system.ingest",
            study_id=TOX,
            cut_id=CUT,
            path=tmp_path / "send" / TOX / CUT,
        )
        cut, path, sha = write_inventory(ROOT / INVENTORY_FILE, tmp_path / "inventory", "MRD-101")
        record_inventory(
            ledger,
            ledger,
            occurred_at=T0,
            actor="system.ingest",
            asset_id="MRD-101",
            cut_id=cut,
            path=path,
            source_sha256=sha,
        )
    assert run(fx, {"preclinical.tox_study_monitor", "preclinical.ind_readiness"}).completed
    return fx


def client(fx: Fixture) -> TestClient:
    ctx = replace(
        fx.context(),
        send_root=fx.root / "send",
        inventory_root=fx.root / "inventory",
        preclinical_assets=(("MRD-101", TOX),),
        operational_cuts=("DC-BASE", CUT),
        portfolio=(
            *fx.context().portfolio,
            PortfolioEntry("MRD-101", STATIC, False),
            PortfolioEntry("MRD-110", STATIC, False),
        ),
    )
    return TestClient(create_app(ctx))


def _tox_ids(c: TestClient) -> list[str]:
    body = c.get(f"/api/v1/preclinical/{TOX}/noael", headers=headers(Role.TOXICOLOGIST)).json()
    return [x["finding_id"] for x in body["candidates"]]


def _noael(c: TestClient, **change: Any) -> dict[str, Any]:
    body = {
        "sex": "combined",
        "established": True,
        "noael_dose": 10.0,
        "loael_dose": 30.0,
        "rationale": RATIONALE,
        "finding_ids": _tox_ids(c)[:1],
        **change,
    }
    r = c.post(f"/api/v1/preclinical/{TOX}/noael", json=body, headers=headers(Role.TOXICOLOGIST))
    assert r.status_code == 201, r.text
    out: dict[str, Any] = r.json()
    return out


def _proposals(c: TestClient, role: Role, **query: Any) -> list[dict[str, Any]]:
    params = {"agent": "preclinical.dose_translation", "limit": 500, **query}
    r = c.get("/api/v1/findings", params=params, headers=headers(role))
    assert r.status_code == 200
    items: list[dict[str, Any]] = r.json()["items"]
    return items


def _asset(c: TestClient, role: Role, asset: str) -> dict[str, Any]:
    assets = c.get("/api/v1/portfolio", headers=headers(role)).json()["assets"]
    live: dict[str, Any] = next(a for a in assets if a["asset_id"] == asset)["live"]
    return live


def test_no_proposal_without_a_noael_decision(fx: Fixture) -> None:
    report = run(fx, {"preclinical.dose_translation"})
    assert report.completed == []  # no NoaelDecision: nothing to wake on
    c = client(fx)
    panel = c.get(f"/api/v1/preclinical/{TOX}/dose-translation", headers=headers(Role.TOXICOLOGIST))
    assert panel.json()["status"] == "no_decision"
    assert "records nothing" in panel.json()["why"]
    assert _asset(c, Role.PORTFOLIO_LEAD, "MRD-101")["starting_dose"]["status"] == "no_decision"
    # The medical monitor is told nothing preclinical until a proposal exists.
    assert _asset(c, Role.MEDICAL_MONITOR, "MRD-101")["starting_dose"] == {
        "status": "no_proposal",
        "dose_mg": None,
        "mrsd_mg_per_kg": None,
        "human_weight_kg": None,
        "why": "No dose proposal has been raised yet.",
        "chain": [],
    }


def test_the_handoff_from_noael_to_planned_starting_dose(fx: Fixture) -> None:
    c = client(fx)
    decision = _noael(c)
    assert (
        c.get(
            f"/api/v1/preclinical/{TOX}/dose-translation", headers=headers(Role.TOXICOLOGIST)
        ).json()["status"]
        == "not_run"
    )
    report = run(fx, {"preclinical.dose_translation"})
    assert report.completed == [("preclinical.dose_translation", decision["seq"])]
    c = client(fx)
    for role in PROPOSAL_ROLES:
        (p,) = _proposals(c, role)
        assert p["kind"] == "dose_proposal"
    for role in CLINICAL:
        assert _proposals(c, role) == []
    (p,) = _proposals(c, Role.MEDICAL_MONITOR)
    fid = p["finding_id"]
    assert p["actor_under_review"] == "leela.nair"
    assert "9.68 mg" in p["title"]  # 10 / 6.2 / 10 x 60
    url = f"/api/v1/findings/{fid}/decisions"
    accept = {"action": "accepted", "reason": "Starting dose agreed for SAD cohort 1."}
    tox = c.post(url, json=accept, headers=headers(Role.TOXICOLOGIST))
    assert tox.status_code == 403  # the self-decision rule: the proposal rests on her NOAEL
    assert "actor under review" in tox.text
    assert c.post(url, json=accept, headers=headers(Role.PORTFOLIO_LEAD)).status_code == 403
    assert c.post(url, json=accept, headers=headers(Role.QA_AUDITOR)).status_code == 403
    assert c.post(url, json=accept, headers=headers(Role.MEDICAL_MONITOR)).status_code == 201
    live = _asset(c, Role.PORTFOLIO_LEAD, "MRD-101")["starting_dose"]
    assert live["status"] == "planned"
    assert live["dose_mg"] == pytest.approx(10 / 6.2 / 10 * 60)
    assert live["human_weight_kg"] == pytest.approx(60.0)
    assert [link["step"] for link in live["chain"]] == ["study", "noael", "proposal", "decision"]
    assert live["chain"][1]["ref"] == decision["decision_id"]
    assert live["chain"][1]["who"] == "leela.nair"
    assert live["chain"][1]["seq"] == decision["seq"]
    assert live["chain"][2]["ref"] == fid
    assert live["chain"][3]["who"] == "karan.mehta"
    assert all(isinstance(link["seq"], int) for link in live["chain"])
    assert _asset(c, Role.TOXICOLOGIST, "MRD-101")["starting_dose"]["status"] == "planned"
    for role in CLINICAL:
        assert _asset(c, role, "MRD-101")["starting_dose"] == {
            "status": "not_shown",
            "dose_mg": None,
            "mrsd_mg_per_kg": None,
            "human_weight_kg": None,
            "why": "not shown to your role",
            "chain": [],
        }
    panel = c.get(
        f"/api/v1/preclinical/{TOX}/dose-translation", headers=headers(Role.PORTFOLIO_LEAD)
    ).json()
    assert panel["status"] == "proposed"
    steps = [s["statistic"] for s in panel["current"]["steps"]]
    assert steps[:2] == ["mrsd:noael_mg_per_kg", "mrsd:hed_mg_per_kg"]
    assert panel["current"]["status"] == "accepted"
    assert panel["current"]["basis_decision_id"] == decision["decision_id"]


def test_a_revised_noael_supersedes_the_proposal(fx: Fixture) -> None:
    c = client(fx)
    first = _noael(c)
    run(fx, {"preclinical.dose_translation"})
    c = client(fx)
    (old,) = _proposals(c, Role.MEDICAL_MONITOR)
    accept = {"action": "accepted", "reason": "Agreed."}
    c.post(
        f"/api/v1/findings/{old['finding_id']}/decisions",
        json=accept,
        headers=headers(Role.MEDICAL_MONITOR),
    )
    _noael(c, noael_dose=30.0, loael_dose=100.0, supersedes=first["decision_id"])
    run(fx, {"preclinical.dose_translation"})
    c = client(fx)
    (new,) = _proposals(c, Role.MEDICAL_MONITOR)
    assert new["finding_id"] != old["finding_id"]
    everything = _proposals(c, Role.MEDICAL_MONITOR, include_superseded=True)
    by_id = {p["finding_id"]: p for p in everything}
    assert by_id[old["finding_id"]]["superseded_by"] == new["finding_id"]
    assert "29 mg" in new["title"]  # 30 / 6.2 / 10 x 60 = 29.03
    live = _asset(c, Role.MEDICAL_MONITOR, "MRD-101")["starting_dose"]
    assert live["status"] == "proposed"
    assert f"the accepted {old['finding_id']} was superseded" in live["why"]
    panel = c.get(
        f"/api/v1/preclinical/{TOX}/dose-translation", headers=headers(Role.TOXICOLOGIST)
    ).json()
    assert panel["current"]["finding_id"] == new["finding_id"]
    assert panel["superseded"] == [old["finding_id"]]


def test_not_established_gives_no_starting_dose(fx: Fixture) -> None:
    c = client(fx)
    _noael(c, established=False, noael_dose=None, loael_dose=10.0)
    run(fx, {"preclinical.dose_translation"})
    c = client(fx)
    (p,) = _proposals(c, Role.MEDICAL_MONITOR)
    assert p["title"].startswith("No starting dose can be derived")
    assert _asset(c, Role.MEDICAL_MONITOR, "MRD-101")["starting_dose"]["status"] == (
        "no_starting_dose"
    )
    panel = c.get(
        f"/api/v1/preclinical/{TOX}/dose-translation", headers=headers(Role.TOXICOLOGIST)
    ).json()
    assert panel["status"] == "no_starting_dose"


def test_the_ind_readiness_checklist_and_its_percentage(fx: Fixture) -> None:
    c = client(fx)
    body = c.get(
        "/api/v1/preclinical/assets/MRD-101/ind-readiness", headers=headers(Role.TOXICOLOGIST)
    ).json()
    assert body["status"] == "assessed"
    assert (body["percent_met"], body["met"], body["applicable"]) == (63.6, 7, 11)
    reqs = {r["id"]: r for r in body["requirements"]}
    assert reqs["repeat_dose_rodent"]["status"] == "met"
    assert reqs["repeat_dose_rodent"]["study_id"] == "MRD-101-TOX-01"
    gap = reqs["in_vitro_metabolism_protein_binding"]
    assert gap["problems"] == ["required_study_missing"]
    (fid,) = gap["finding_ids"]
    detail = c.get(f"/api/v1/findings/{fid}", headers=headers(Role.PORTFOLIO_LEAD))
    assert detail.status_code == 200
    assert reqs["repeat_dose_nonrodent"]["problems"] == ["report_not_ready"]
    live = _asset(c, Role.PORTFOLIO_LEAD, "MRD-101")
    assert (live["ind_status"], live["ind_readiness_percent"]) == ("assessed", 63.6)
    queue = c.get("/api/v1/review-queue", headers=headers(Role.PORTFOLIO_LEAD)).json()
    assert fid in {i["finding_id"] for i in queue["items"]}  # an inventory cut is operational
    for role in (*CLINICAL, Role.MEDICAL_MONITOR):
        r = c.get("/api/v1/preclinical/assets/MRD-101/ind-readiness", headers=headers(role))
        assert r.status_code == 403
        assert _asset(c, role, "MRD-101")["ind_status"] == "not_shown"


def test_the_roles_are_the_agents_visible_to() -> None:
    from meridian.api import visibility

    agents = Settings().agents
    assert set(agents.dose_translation.visible_to) == visibility.DOSE_PROPOSAL_ROLES
    assert set(agents.ind_readiness.visible_to) == visibility.PRECLINICAL_ROLES
    assert visibility.DECIDERS[FindingKind.DOSE_PROPOSAL] == {
        Role.MEDICAL_MONITOR,
        Role.TOXICOLOGIST,
    }


def test_the_dose_is_mrd101s_own_and_mrd110_shows_none(fx: Fixture) -> None:
    """Phase 5 step 3a: MRD-110 is a separate asset already in phase 1. The accepted
    proposal is MRD-101's planned first-in-human starting dose, derived at read time; the
    ledger keeps the text dose_translation 1.0.0 recorded."""
    c = client(fx)
    _noael(c)
    run(fx, {"preclinical.dose_translation"})
    c = client(fx)
    (p,) = _proposals(c, Role.MEDICAL_MONITOR)
    assert p["title"].startswith("Proposed MRD-110 starting dose")  # as recorded, unchanged
    c.post(
        f"/api/v1/findings/{p['finding_id']}/decisions",
        json={"action": "accepted", "reason": "Provisional."},
        headers=headers(Role.MEDICAL_MONITOR),
    )
    for role in (Role.MEDICAL_MONITOR, Role.PORTFOLIO_LEAD):
        assets = c.get("/api/v1/portfolio", headers=headers(role)).json()["assets"]
        by_id = {a["asset_id"]: a for a in assets}
        assert by_id["MRD-110"]["live"] is None
        dose = by_id["MRD-101"]["live"]["starting_dose"]
        assert dose["status"] == "planned"
        proposal = next(link for link in dose["chain"] if link["step"] == "proposal")
        assert proposal["label"] == (
            "Proposed first-in-human starting dose for MRD-101: 9.68 mg for a 60 kg adult "
            "(MRSD 0.161 mg/kg)"
        )
    panel = c.get(
        f"/api/v1/preclinical/{TOX}/dose-translation", headers=headers(Role.TOXICOLOGIST)
    ).json()["current"]
    assert panel["title"].startswith("Proposed first-in-human starting dose for MRD-101")
    assert panel["recorded_title"].startswith("Proposed MRD-110 starting dose")
