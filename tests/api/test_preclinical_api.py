"""The preclinical API (phase 5 step 1): who sees the SEND study, its endpoint tables and
findings; the NOAEL decision recorded by the toxicologist alone, validated, append-only,
superseded by name; the QA auditor's observation on it; the portfolio's live NOAEL."""

from pathlib import Path
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient

from meridian.agents.catalog import build_registry
from meridian.agents.runner import AgentRunner
from meridian.api import visibility
from meridian.api.app import create_app
from meridian.data.send import record_send_study
from meridian.data.study_store import SendStudyReader
from meridian.domain.enums import Role
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.preclinical.synth import STUDY as TOX
from meridian.preclinical.synth import Effect, StudySpec
from meridian.sandbox.executor import InProcessExecutor
from tests.api_support import Fixture, build, headers
from tests.support import T0
from tests.tox_support import CUT, write_study

CLINICAL = (Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.UNBLINDED_STATISTICIAN)
PRE = (Role.TOXICOLOGIST, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR)
SPEC = StudySpec(
    seed=61,
    effects=(
        Effect(endpoint="lb:ALT", factors=(1, 1.1, 1.6, 2.5)),
        Effect(endpoint="mi:LIVER:HYPERTROPHY", counts=(0, 0, 3, 8)),
    ),
)
NOAEL = {
    "sex": "combined",
    "established": True,
    "noael_dose": 10.0,
    "loael_dose": 30.0,
    "rationale": (
        "ALT is higher with dose from 30 mg/kg/day and comes with hepatocellular hypertrophy "
        "at 30 and 100 mg/kg/day, considered adverse together. Nothing of note at 10: no "
        "group there differs from the concurrent control by the pre-registered rule."
    ),
}
LONG = " The recovery groups and the pathology tables were read with the findings."


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    fx = build(tmp_path / "fx")
    send_root = tmp_path / "send"
    write_study(send_root, SPEC)
    settings = Settings()
    registry = build_registry(settings.agents)
    with fx.ledger() as ledger:
        record_send_study(
            ledger,
            occurred_at=T0,
            actor="system.ingest",
            study_id=TOX,
            cut_id=CUT,
            path=send_root / TOX / CUT,
        )
        runner = AgentRunner(
            ledger=ledger,
            registry=registry,
            executor=InProcessExecutor(
                registry=registry,
                reader_factory=lambda c: SendStudyReader(send_root / TOX, c),
                send_reader_factory=lambda c: SendStudyReader(send_root / TOX, c),
                log=structlog.get_logger(),
            ),
            clock=FixedClock(T0),
            study_id="MRD-201",
            log=structlog.get_logger(),
            operational_cuts=(),
        )
        report = runner.run_pending(agents={"preclinical.tox_study_monitor"})
        assert report.completed
    fx.root = tmp_path
    return fx


def client(fx: Fixture) -> TestClient:
    ctx = fx.context()
    from dataclasses import replace

    ctx = replace(
        ctx,
        send_root=fx.root / "send",
        preclinical_assets=(("MRD-101", TOX),),
        operational_cuts=("DC-BASE", CUT),
    )
    return TestClient(create_app(ctx))


def _findings(c: TestClient, role: Role) -> list[dict[str, object]]:
    r = c.get("/api/v1/findings", headers=headers(role))
    assert r.status_code == 200
    body = r.json()
    items = body["items"] if isinstance(body, dict) else body
    return [f for f in items if f["agent_id"] == "preclinical.tox_study_monitor"]


def _pre_ids(c: TestClient) -> list[str]:
    return [str(f["finding_id"]) for f in _findings(c, Role.TOXICOLOGIST)]


def _ids_of(c: TestClient, sex: str) -> list[str]:
    """The study's current findings about one sex, from the NOAEL candidates."""
    body = c.get(f"/api/v1/preclinical/{TOX}/noael", headers=headers(Role.TOXICOLOGIST)).json()
    return [x["finding_id"] for x in body["candidates"] if x["sex"] == sex]


def test_the_preclinical_roles_are_the_agents_visible_to() -> None:
    assert set(Settings().agents.tox_study_monitor.visible_to) == visibility.PRECLINICAL_ROLES


def test_only_the_preclinical_roles_see_the_study_and_its_tables(fx: Fixture) -> None:
    c = client(fx)
    for role in PRE:
        studies = c.get("/api/v1/preclinical/studies", headers=headers(role))
        assert studies.status_code == 200, role
        (study,) = studies.json()
        assert study["study_id"] == TOX
        assert study["asset_id"] == "MRD-101"
        main = [g for g in study["groups"] if g["phase"] == "main"]
        assert [(g["dose"], g["males"], g["females"]) for g in main] == [
            (0.0, 10, 10),
            (10.0, 10, 10),
            (30.0, 10, 10),
            (100.0, 10, 10),
        ]
        assert (
            c.get(f"/api/v1/preclinical/{TOX}/endpoints", headers=headers(role)).status_code == 200
        )
    for role in CLINICAL:
        for path in ("studies", f"{TOX}/endpoints", f"{TOX}/noael"):
            assert c.get(f"/api/v1/preclinical/{path}", headers=headers(role)).status_code == 403
        assert _findings(c, role) == []


def test_endpoint_tables_carry_flags_and_the_findings_raised_on_them(fx: Fixture) -> None:
    body = (
        client(fx)
        .get(f"/api/v1/preclinical/{TOX}/endpoints", headers=headers(Role.TOXICOLOGIST))
        .json()
    )
    alt = next(
        t
        for t in body["tables"]
        if t["endpoint"] == "lb:ALT" and t["sex"] == "M" and t["phase"] == "main"
    )
    assert alt["check"] == "dose_related_change"
    assert alt["finding_id"] is not None
    assert [g["flagged"] for g in alt["groups"]] == [False, False, True, True]
    assert alt["groups"][0]["percent_vs_control"] is None
    hyper = next(
        t for t in body["tables"] if t["endpoint"] == "mi:LIVER:HYPERTROPHY" and t["sex"] == "F"
    )
    assert [g["affected"] for g in hyper["groups"]] == [0, 0, 3, 8]
    support = {(d["phase"], d["dose"]): d for d in body["by_dose"]}
    assert alt["finding_id"] in support[("main", 30.0)]["dose_related"]
    assert ("main", 10.0) not in support


def test_only_the_toxicologist_records_a_noael(fx: Fixture) -> None:
    c = client(fx)
    body = {**NOAEL, "finding_ids": _pre_ids(c)[:1]}
    for role in (Role.PORTFOLIO_LEAD, Role.QA_AUDITOR, *CLINICAL):
        r = c.post(f"/api/v1/preclinical/{TOX}/noael", json=body, headers=headers(role))
        assert r.status_code == 403, role
    r = c.post(f"/api/v1/preclinical/{TOX}/noael", json=body, headers=headers(Role.TOXICOLOGIST))
    assert r.status_code == 201, r.text
    view = r.json()
    assert (view["noael_dose"], view["loael_dose"], view["dose_unit"]) == (10.0, 30.0, "mg/kg/day")
    assert view["decided_by"] == "leela.nair"
    assert view["current"] is True


@pytest.mark.parametrize(
    ("change", "status"),
    [
        ({"noael_dose": 15.0}, 422),  # not a dose of the study
        ({"noael_dose": 0.0}, 422),  # the control is not a dose level
        ({"loael_dose": 10.0}, 422),  # not above the NOAEL
        ({"rationale": "too short"}, 422),
        ({"established": False}, 422),  # a dose with "not established"
        ({"finding_ids": ["PRE-9999"]}, 404),  # no such finding
        ({"finding_ids": ["RBQM-0001"]}, 404),  # a clinical finding the toxicologist cannot see
        ({"finding_ids": []}, 422),  # a LOAEL resting on nothing
        ({"supersedes": "NOAEL-0123456789ab"}, 409),  # nothing to supersede
    ],
)
def test_a_noael_is_validated(fx: Fixture, change: dict[str, object], status: int) -> None:
    c = client(fx)
    body = {**NOAEL, "finding_ids": _pre_ids(c)[:1], **change}
    r = c.post(f"/api/v1/preclinical/{TOX}/noael", json=body, headers=headers(Role.TOXICOLOGIST))
    assert r.status_code == status, r.text


def test_not_established_needs_no_dose(fx: Fixture) -> None:
    c = client(fx)
    body = {
        "sex": "M",
        "established": False,
        "loael_dose": 10.0,
        "finding_ids": _ids_of(c, "M")[:1],
        "rationale": "Changes already at the lowest dose; no NOAEL in this study." + LONG * 3,
    }
    r = c.post(f"/api/v1/preclinical/{TOX}/noael", json=body, headers=headers(Role.TOXICOLOGIST))
    assert r.status_code == 201, r.text
    assert r.json()["noael_dose"] is None


def test_the_candidates_carry_sex_and_the_flagged_doses(fx: Fixture) -> None:
    body = client(fx).get(f"/api/v1/preclinical/{TOX}/noael", headers=headers(Role.TOXICOLOGIST))
    candidates = body.json()["candidates"]
    assert body.json()["rationale_min_chars"] == 200
    alt = [x for x in candidates if x["title"].startswith("ALT higher with dose")]
    assert sorted(x["sex"] for x in alt) == ["F", "M"]
    for x in alt:
        assert x["main_doses"] == [30.0, 100.0], x
    assert {x["sex"] for x in candidates} <= {"M", "F", None}


def test_a_sex_specific_decision_rests_only_on_that_sexs_findings(fx: Fixture) -> None:
    c = client(fx)
    url, tox = f"/api/v1/preclinical/{TOX}/noael", headers(Role.TOXICOLOGIST)
    male, female = _ids_of(c, "M"), _ids_of(c, "F")
    wrong = c.post(url, json={**NOAEL, "sex": "F", "finding_ids": male[:1]}, headers=tox)
    assert wrong.status_code == 422
    assert f"{male[0]} is about M" in wrong.text
    mixed = {**NOAEL, "sex": "M", "finding_ids": [male[0], female[0]]}
    assert c.post(url, json=mixed, headers=tox).status_code == 422
    assert (
        c.post(url, json={**NOAEL, "sex": "F", "finding_ids": female[:2]}, headers=tox).status_code
        == 201
    )
    both = {**NOAEL, "sex": "combined", "finding_ids": [male[0], female[0]]}
    assert c.post(url, json=both, headers=tox).status_code == 201  # any sex for combined


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"established": False, "noael_dose": None, "loael_dose": None}, "needs the LOAEL"),
        ({"established": False, "noael_dose": None, "loael_dose": 30.0}, "the LOAEL is 10"),
        ({"noael_dose": 30.0, "loael_dose": 10.0}, "must be above the NOAEL"),
        ({"rationale": "Adverse liver changes from 30 mg/kg/day." * 3}, "at least 200"),
    ],
)
def test_the_api_applies_the_noael_rules(
    fx: Fixture, change: dict[str, object], message: str
) -> None:
    c = client(fx)
    body = {**NOAEL, "finding_ids": _pre_ids(c)[:1], **change}
    r = c.post(f"/api/v1/preclinical/{TOX}/noael", json=body, headers=headers(Role.TOXICOLOGIST))
    assert r.status_code == 422, r.text
    assert message in r.text


def test_a_revision_supersedes_by_name_and_both_are_kept(fx: Fixture) -> None:
    c = client(fx)
    url, tox = f"/api/v1/preclinical/{TOX}/noael", headers(Role.TOXICOLOGIST)
    ids = _pre_ids(c)
    first = c.post(url, json={**NOAEL, "finding_ids": ids[:1]}, headers=tox).json()
    again = c.post(url, json={**NOAEL, "finding_ids": ids[:1]}, headers=tox)
    assert again.status_code == 409  # a second decision must name the current one
    revised = {
        **NOAEL,
        "noael_dose": 30.0,
        "loael_dose": 100.0,
        "finding_ids": ids[:2],
        "supersedes": first["decision_id"],
        "rationale": "On review of the 30 mg/kg/day pathology, the NOAEL is 30." + LONG * 3,
    }
    second = c.post(url, json=revised, headers=tox)
    assert second.status_code == 201, second.text
    stale = c.post(url, json=revised, headers=tox)  # names a decision no longer current
    assert stale.status_code == 409
    history = c.get(url, headers=headers(Role.QA_AUDITOR)).json()
    assert [d["decision_id"] for d in history["current"]] == [second.json()["decision_id"]]
    assert [d["decision_id"] for d in history["history"]] == [
        second.json()["decision_id"],
        first["decision_id"],
    ]
    assert history["history"][1]["superseded_by"] == second.json()["decision_id"]
    assert history["may_record"] is False
    assert c.get(url, headers=tox).json()["may_record"] is True
    other_sex = {**NOAEL, "sex": "F", "finding_ids": _ids_of(c, "F")[:1]}
    assert c.post(url, json=other_sex, headers=tox).status_code == 201  # per sex


def test_the_portfolio_shows_the_current_noael_to_preclinical_roles_only(fx: Fixture) -> None:
    c = client(fx)

    def mrd101(role: Role) -> dict[str, Any]:
        assets = c.get("/api/v1/portfolio", headers=headers(role)).json()["assets"]
        live: dict[str, Any] = next(a for a in assets if a["asset_id"] == "MRD-101")["live"]
        return live

    assert mrd101(Role.PORTFOLIO_LEAD)["noael_status"] == "not_yet_decided"
    c.post(
        f"/api/v1/preclinical/{TOX}/noael",
        json={**NOAEL, "finding_ids": _pre_ids(c)[:1]},
        headers=headers(Role.TOXICOLOGIST),
    )
    live = mrd101(Role.PORTFOLIO_LEAD)
    assert live["noael_status"] == "decided"
    assert [(n["sex"], n["noael_dose"]) for n in live["noael"]] == [("combined", 10.0)]
    for role in CLINICAL:
        live = mrd101(role)
        assert (live["open_findings"], live["noael_status"], live["noael"]) == (0, "not_shown", [])
        assert (live["ind_status"], live["ind_readiness_percent"]) == ("not_shown", None)
        # The medical monitor decides dose proposals; with none raised, nothing preclinical
        # is told, not even that a NOAEL exists.
        dose = live["starting_dose"]
        if role is Role.MEDICAL_MONITOR:
            assert (dose["status"], dose["chain"]) == ("no_proposal", [])
            assert TOX not in dose["why"]
        else:
            assert dose["status"] == "not_shown"


def test_the_ledger_hides_a_noael_from_clinical_roles(fx: Fixture) -> None:
    c = client(fx)
    seq = c.post(
        f"/api/v1/preclinical/{TOX}/noael",
        json={**NOAEL, "finding_ids": _pre_ids(c)[:1]},
        headers=headers(Role.TOXICOLOGIST),
    ).json()["seq"]
    for role, shown in ((Role.QA_AUDITOR, True), (Role.CLINICAL_OPS_LEAD, False)):
        r = c.get("/api/v1/ledger?type=noael_decision", headers=headers(role))
        assert r.status_code == 200
        body = r.json()
        entries = body["entries"] if isinstance(body, dict) else body
        (entry,) = [e for e in entries if e["seq"] == seq]
        assert (entry.get("payload") is not None) is shown, (role, entry)
        assert "rationale" not in str(entry) or shown


def test_the_qa_auditor_observes_a_noael_and_someone_else_decides_it(fx: Fixture) -> None:
    c = client(fx)
    seq = c.post(
        f"/api/v1/preclinical/{TOX}/noael",
        json={**NOAEL, "finding_ids": _pre_ids(c)[:1]},
        headers=headers(Role.TOXICOLOGIST),
    ).json()["seq"]
    obs = c.post(
        "/api/v1/quality/observations",
        headers=headers(Role.QA_AUDITOR),
        json={
            "title": "NOAEL rationale omits the recovery data",
            "description": "The rationale does not say whether the ALT rise reversed.",
            "severity": "medium",
            "ledger_seq": seq,
        },
    )
    assert obs.status_code == 201, obs.text
    fid = obs.json()["finding_id"]
    tox = c.get(f"/api/v1/findings/{fid}", headers=headers(Role.TOXICOLOGIST))
    assert tox.status_code == 200
    assert (
        tox.json()["finding"]["actor_under_review"] == "leela.nair"
        if "finding" in tox.json()
        else True
    )
    for role in CLINICAL:
        assert c.get(f"/api/v1/findings/{fid}", headers=headers(role)).status_code == 404
    url = f"/api/v1/findings/{fid}/decisions"
    decision = {"action": "accepted", "reason": "Agreed; the rationale is revised."}
    assert c.post(url, json=decision, headers=headers(Role.TOXICOLOGIST)).status_code == 403
    assert c.post(url, json=decision, headers=headers(Role.PORTFOLIO_LEAD)).status_code == 201


PRECLINICAL_GETS = [
    "/api/v1/preclinical/studies",
    f"/api/v1/preclinical/{TOX}/endpoints",
    f"/api/v1/preclinical/{TOX}/noael",
    "/api/v1/portfolio",
    "/api/v1/findings?scope=all&include_superseded=true&include_retired=true&limit=500",
    "/api/v1/review-queue?scope=all",
    "/api/v1/ledger?limit=1000",
    "/api/v1/ledger?type=noael_decision&limit=1000",
    "/api/v1/ledger?type=finding_raised&limit=1000",
    "/api/v1/fleet",
    "/api/v1/whoami",
]


@pytest.mark.parametrize("role", list(Role))
def test_no_get_shows_preclinical_data_to_a_role_outside_the_preclinical_roles(
    fx: Fixture, role: Role
) -> None:
    c = client(fx)
    ids = _pre_ids(c)
    seq = c.post(
        f"/api/v1/preclinical/{TOX}/noael",
        json={**NOAEL, "finding_ids": ids[:1]},
        headers=headers(Role.TOXICOLOGIST),
    ).json()["seq"]
    c.post(
        "/api/v1/quality/observations",
        headers=headers(Role.QA_AUDITOR),
        json={
            "title": "QUILLSECRET observation",
            "description": "On the NOAEL rationale.",
            "severity": "low",
            "ledger_seq": seq,
        },
    )
    secrets = [*ids, TOX, "nothing of note at 10", "QUILLSECRET"]
    for path in PRECLINICAL_GETS:
        r = c.get(path, headers=headers(role))
        text = r.text
        leaked = [s for s in secrets if s in text]
        if visibility.can_see_preclinical(role):
            continue
        assert leaked == [], (role, path, leaked)
