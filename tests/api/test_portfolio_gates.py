"""The portfolio's reviews and gates (phase 5 step 3b): the facts are built as the
portfolio lead and hold nothing that role may not see; an outlook and an evidence pack are
events, not findings; only the portfolio lead asks and decides; the QA auditor may observe."""

import json
import re
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from typing import Any

import structlog
from fastapi.testclient import TestClient

from meridian.agents.catalog import build_registry
from meridian.agents.portfolio.pipeline_intelligence import AGENT_ID as OUTLOOK_AGENT
from meridian.agents.portfolio.stage_gate_pack import AGENT_ID as PACK_AGENT
from meridian.agents.quality.audit_trail_reviewer import attribution, parse
from meridian.agents.runner import AgentRunner, RunReport
from meridian.api import portfolio_facts as pf
from meridian.api.app import create_app
from meridian.api.deps import make_repository
from meridian.audit.extract import extract_rows, frame
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event, FindingRaised, GatePack, PortfolioOutlook
from meridian.domain.findings import Evidence, Finding
from meridian.domain.identity import people_ids
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.sandbox.executor import InProcessExecutor
from tests.api_support import PEOPLE, Fixture, build, headers
from tests.support import T0

SUBJECT = "201-1018-006"
SECRET_SUBJECT_FINDING = f"Outcome of a serious AE of {SUBJECT} differs from its case"


def _fixture(tmp_path: Path) -> Fixture:
    """The standard fixture (a restricted finding whose strings contain SECRET), plus a
    subject-level safety finding only the medical monitor may see."""
    fx = build(tmp_path / "fx", restricted=True)
    fx.raise_finding(
        Finding(
            kind=FindingKind.SAE_RECONCILIATION,
            agent_id="safety.sae_reconciler",
            agent_version="1.1.1",
            study_id="MRD-201",
            site_id="1018",
            severity=Severity.HIGH,
            title=SECRET_SUBJECT_FINDING,
            summary=f"Subject {SUBJECT}: outcome differs.",
            evidence=(
                Evidence(source="ae", selector=f"USUBJID={SUBJECT}", statistic="outcome",
                         value=0.0, display="FATAL"),
            ),
            visibility=Visibility.BLINDED,
            visible_to=frozenset({Role.MEDICAL_MONITOR}),
            score=1.0,
        ),
        check=f"outcome_mismatch:{SUBJECT}/4",
    )  # fmt: skip
    return fx


def _repo(fx: Fixture, role: Role) -> Any:
    ctx = fx.context()
    person = ctx.people.people[PEOPLE[role]]
    return make_repository(ctx, person, fx.ledger())


def _run(fx: Fixture, root: Path, agent: str) -> RunReport:
    """The runner, in process, writing each trigger's facts as the container does."""
    ctx = fx.context()
    lead = ctx.people.people[PEOPLE[Role.PORTFOLIO_LEAD]]

    def prepare(trigger: Event) -> Path:
        with fx.ledger() as ledger:
            facts = pf.build(make_repository(ctx, lead, ledger))
            before = pf.previous([e for e in ledger.read() if isinstance(e, Event)], trigger.seq)
        return pf.write(root / str(trigger.seq), facts, before)

    registry = build_registry(Settings().agents)
    with fx.ledger() as ledger:
        runner = AgentRunner(
            ledger=ledger,
            registry=registry,
            executor=InProcessExecutor(
                registry=registry,
                reader_factory=lambda c: (_ for _ in ()).throw(AssertionError(c)),
                portfolio_root=root,
                log=structlog.get_logger(),
            ),
            clock=FixedClock(T0),
            study_id="MRD-201",
            log=structlog.get_logger(),
            operational_cuts=(),
            operational_executors=("inprocess",),
            prepare_portfolio=prepare,
        )
        return runner.run_pending(agents={agent})


def _client(fx: Fixture) -> TestClient:
    return TestClient(create_app(fx.context()))


def test_the_facts_are_built_as_the_portfolio_lead_and_hide_what_that_role_may_not_see(
    tmp_path: Path,
) -> None:
    fx = _fixture(tmp_path)
    facts = pf.build(_repo(fx, Role.PORTFOLIO_LEAD))
    text = json.dumps([asdict(f) for f in facts])
    assert "SECRET" not in text  # nothing restricted (unblinded)
    assert SUBJECT not in text  # no subject id
    assert SECRET_SUBJECT_FINDING not in text  # nothing hidden from the portfolio lead
    assert not re.search(r"\b\d{3}-\d{4}-\d{3}\b", text)
    visible = {
        i["finding_id"]
        for i in _client(fx)
        .get("/api/v1/findings", params={"limit": 500}, headers=headers(Role.PORTFOLIO_LEAD))
        .json()["items"]
    }
    assert {f.finding_id for f in facts if f.finding_id} <= visible
    assert {f.asset_id for f in facts} == {"MRD-101", "MRD-110", "MRD-201", "MRD-301"}
    assert all(f.static and f.seq is None for f in facts if f.asset_id in ("MRD-110", "MRD-301"))
    qtl = next(f for f in facts if f.group == "qtl")
    assert qtl.status == "not_shown"
    try:
        pf.build(_repo(fx, Role.MEDICAL_MONITOR))
    except pf.NotThePortfolioLeadError:
        pass
    else:
        raise AssertionError("facts were built as another role")


def test_an_outlook_per_review_with_changes_and_only_for_its_roles(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    c = _client(fx)
    lead = headers(Role.PORTFOLIO_LEAD)
    assert (
        c.post(
            "/api/v1/portfolio/reviews", json={}, headers=headers(Role.MEDICAL_MONITOR)
        ).status_code
        == 403
    )
    first = c.post("/api/v1/portfolio/reviews", json={"note": "Q4"}, headers=lead)
    assert first.status_code == 201, first.text
    assert c.get("/api/v1/portfolio/outlook", headers=lead).json()["pending_reviews"] == [
        first.json()["seq"]
    ]
    report = _run(fx, tmp_path / "portfolio", OUTLOOK_AGENT)
    assert report.failed == []
    view = _client(fx).get("/api/v1/portfolio/outlook", headers=lead).json()
    assert view["status"] == "outlook"
    assert view["pending_reviews"] == []
    assets = {a["asset_id"]: a for a in view["outlook"]["assets"]}
    assert set(assets) == {"MRD-101", "MRD-110", "MRD-201", "MRD-301"}
    assert assets["MRD-110"]["status"] == "static"
    assert assets["MRD-110"]["static"] is True
    assert assets["MRD-201"]["changes"] == ["First outlook: nothing to compare with."]
    assert view["outlook"]["previous_outlook_seq"] is None
    # An outlook is an event, not a finding: nothing new in anyone's queue.
    with fx.ledger() as ledger:
        events = [e for e in ledger.read() if isinstance(e, Event)]
    assert not any(
        isinstance(e.payload, FindingRaised)
        and e.payload.finding.kind is FindingKind.PORTFOLIO_OUTLOOK
        for e in events
    )
    outlooks = [e for e in events if isinstance(e.payload, PortfolioOutlook)]
    assert len(outlooks) == 1
    # The same second again: refused (the id names the review's cut).
    assert c.post("/api/v1/portfolio/reviews", json={}, headers=lead).status_code == 409
    # A second review, a minute later, compares with the first.
    later = TestClient(
        create_app(replace(fx.context(), clock=FixedClock(T0 + timedelta(minutes=1))))
    )
    assert later.post("/api/v1/portfolio/reviews", json={}, headers=lead).status_code == 201
    _run(fx, tmp_path / "portfolio", OUTLOOK_AGENT)
    again = _client(fx).get("/api/v1/portfolio/outlook", headers=lead).json()
    assert again["outlook"]["previous_outlook_seq"] == outlooks[0].seq
    assert again["outlook"]["assets"][2]["changes"] == ["No change since the previous outlook."]
    # The QA auditor reads it; others do not, and the ledger hides it from them.
    qa = _client(fx).get("/api/v1/portfolio/outlook", headers=headers(Role.QA_AUDITOR))
    assert qa.status_code == 200
    for role in (Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.TOXICOLOGIST):
        assert (
            _client(fx).get("/api/v1/portfolio/outlook", headers=headers(role)).status_code == 403
        )
        page = (
            _client(fx)
            .get("/api/v1/ledger", params={"type": "portfolio_outlook"}, headers=headers(role))
            .json()
        )
        assert all("marker" in e for e in page["entries"])


def test_a_gate_pack_then_the_portfolio_leads_decision(tmp_path: Path) -> None:
    fx = _fixture(tmp_path)
    c = _client(fx)
    lead = headers(Role.PORTFOLIO_LEAD)
    bad = c.post("/api/v1/portfolio/gates", json={"asset_id": "MRD-101", "gate": "nda"},
                 headers=lead)  # fmt: skip
    assert bad.status_code in (400, 422)
    ind = {"asset_id": "MRD-101", "gate": "ind_submission"}
    asked = c.post("/api/v1/portfolio/gates", json=ind, headers=lead)
    assert asked.status_code == 201, asked.text
    seq = asked.json()["seq"]
    go = {"pack_seq": 1, "decision": "go", "rationale": "x" * 30}
    early = c.post(f"/api/v1/portfolio/gates/{seq}/decision", json=go, headers=lead)
    assert early.status_code == 409  # no pack yet
    assert _run(fx, tmp_path / "portfolio", PACK_AGENT).failed == []
    gates = _client(fx).get("/api/v1/portfolio/gates", headers=lead).json()
    (req,) = gates["requests"]
    pack = req["pack"]
    assert pack is not None
    statuses = {c["id"]: c["status"] for c in pack["criteria"]}
    # In this fixture MRD-101 has no preclinical data: nothing is met, signals n/a.
    assert statuses["noael_decided"] == "not_met"
    assert statuses["safety_signals_reviewed"] == "not_applicable"
    assert {c["basis"] for c in pack["criteria"]} == {"regulation", "policy"}
    assert all(c["quote"] for c in pack["criteria"] if c["basis"] == "regulation")
    assert not {"decision", "verdict", "go"} & set(pack)  # the pack has no verdict
    url = f"/api/v1/portfolio/gates/{seq}/decision"
    body = {"pack_seq": req["pack_seq"], "decision": "conditional_go",
            "rationale": "Proceed once the NOAEL and the dose are recorded."}  # fmt: skip
    c = _client(fx)
    assert c.post(url, json=body, headers=headers(Role.QA_AUDITOR)).status_code == 403
    assert c.post(url, json=body, headers=headers(Role.MEDICAL_MONITOR)).status_code == 403
    assert c.post(url, json=body, headers=lead).status_code == 422  # no condition
    body["conditions"] = ["Record the NOAEL decision", "Accept the starting dose"]
    done = c.post(url, json=body, headers=lead)
    assert done.status_code == 201, done.text
    assert c.post(url, json=body, headers=lead).status_code == 409  # once per request
    decided = c.get("/api/v1/portfolio/gates", headers=headers(Role.QA_AUDITOR)).json()
    assert decided["requests"][0]["decision"]["decision"]["decision"] == "conditional_go"
    assert decided["can_request"] is False
    # The QA auditor may raise an observation on the decision.
    obs = c.post(
        "/api/v1/quality/observations",
        json={"title": "Conditions without owners", "description": "Neither condition names "
              "who closes it or by when.", "severity": "low",
              "ledger_seq": done.json()["seq"]},
        headers=headers(Role.QA_AUDITOR),
    )  # fmt: skip
    assert obs.status_code == 201, obs.text
    with fx.ledger() as ledger:
        events = [e for e in ledger.read() if isinstance(e, Event)]
    assert sum(isinstance(e.payload, GatePack) for e in events) == 1
    # Every person-attributed event here came through the API: the audit check is quiet.
    rows = extract_rows(
        events, from_seq=1, through_seq=events[-1].seq, operational_cuts=(),
        people=people_ids(Path("config/people.toml")),
    )  # fmt: skip
    assert attribution(parse(frame(rows))) == []


def test_the_scheduled_review_names_no_person(tmp_path: Path) -> None:
    from meridian.domain.events import EventDraft, PortfolioReviewRequested

    fx = _fixture(tmp_path)
    with fx.ledger() as ledger:
        ledger.append(
            EventDraft(
                occurred_at=T0,
                actor="operator:test",
                visibility=Visibility.BLINDED,
                payload=PortfolioReviewRequested(
                    review_id="REVIEW-20261201T000000Z",
                    requested_by="system.schedule",
                    reason="scheduled",
                    period="2026-12",
                ),
            )
        )
    assert _run(fx, tmp_path / "portfolio", OUTLOOK_AGENT).completed
