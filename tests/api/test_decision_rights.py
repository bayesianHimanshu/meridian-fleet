"""Segregation of duties in the decision endpoint (phase 3 step 4b), QA observations, the
Audit tab, and the visibility property test extended to audit findings and observations."""

import json
import re
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meridian.domain import audit as a
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.events import ActorSuspended, ActorWoke, Event, FindingRaised, HumanDecision
from meridian.domain.findings import Evidence, Finding
from tests.api.test_visibility import _strings
from tests.api_support import PEOPLE, Fixture, build, headers

ALL = frozenset(Role)
AUDIT_ROLES = frozenset({Role.QA_AUDITOR, Role.CLINICAL_OPS_LEAD})
OBSERVATION_ROLES = frozenset({Role.QA_AUDITOR, Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR})
REVIEWER = "quality.audit_trail_reviewer"


def finding(
    kind: FindingKind,
    under_review: str | None = None,
    visible_to: frozenset[Role] = ALL,
    title: str = "A finding",
    agent: str = "rbqm.kri_monitor",
) -> Finding:
    return Finding(
        kind=kind,
        agent_id=agent,
        agent_version="1.0.0",
        study_id="MRD-201",
        severity=Severity.MEDIUM,
        title=title,
        summary=f"{title}.",
        evidence=(Evidence(source="x", selector="SEQ=1", statistic="s", value=0.0, display="d"),),
        visibility=Visibility.BLINDED,
        visible_to=visible_to,
        score=0.5,
        actor_under_review=under_review,
    )


def _raise(fx: Fixture, f: Finding, check: str) -> str:
    e = fx.raise_finding(f, cut=a.LEDGER_SCOPE, check=check)
    assert isinstance(e.payload, FindingRaised)
    fid = e.payload.finding.finding_id
    assert fid is not None
    return fid


def _decide(client: TestClient, role: Role, fid: str, action: str = "accepted") -> Any:
    return client.post(
        f"/api/v1/findings/{fid}/decisions",
        json={"action": action, "reason": "Checked against the record; nothing further."},
        headers=headers(role),
    )


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build(tmp_path, restricted=True)


@pytest.mark.parametrize("kind", list(FindingKind))
def test_nobody_decides_a_finding_that_names_them_on_every_kind(
    fx: Fixture, kind: FindingKind
) -> None:
    asha = PEOPLE[Role.CLINICAL_OPS_LEAD]
    fid = _raise(fx, finding(kind, under_review=asha), f"x:{kind.value}")
    client = fx.client()
    r = _decide(client, Role.CLINICAL_OPS_LEAD, fid)
    assert r.status_code == 403
    assert "names them as the actor under review" in r.json()["detail"]
    other = Role.CLINICAL_OPS_LEAD if kind is FindingKind.AUDIT else Role.MEDICAL_MONITOR
    if other is Role.CLINICAL_OPS_LEAD:
        # An audit finding about asha.rao: only clinical_ops_lead decides audit findings, so
        # it waits for another clinical_ops_lead; the medical monitor may not decide it.
        assert _decide(client, Role.MEDICAL_MONITOR, fid).status_code == 403
    else:
        assert _decide(client, other, fid).status_code == 201
    # A finding about someone else is decided as before.
    free = _raise(fx, finding(kind, under_review="someone.else"), f"y:{kind.value}")
    assert _decide(fx.client(), Role.CLINICAL_OPS_LEAD, free).status_code == 201


def test_audit_findings_are_decided_by_clinical_ops_only(fx: Fixture) -> None:
    fid = _raise(
        fx, finding(FindingKind.AUDIT, visible_to=AUDIT_ROLES, agent=REVIEWER), "audit_x:1"
    )
    client = fx.client()
    assert _decide(client, Role.QA_AUDITOR, fid).status_code == 403
    for role in Role:
        if role not in AUDIT_ROLES:
            assert _decide(client, role, fid).status_code == 404  # never seen, never decided
    assert _decide(client, Role.CLINICAL_OPS_LEAD, fid).status_code == 201


def _observe(client: TestClient, role: Role, **body: Any) -> Any:
    payload = {
        "title": "Thin dismissal",
        "description": "The reason says nothing checkable.",
        "severity": "medium",
        **body,
    }
    return client.post("/api/v1/quality/observations", json=payload, headers=headers(role))


def test_only_the_qa_auditor_raises_an_observation(fx: Fixture) -> None:
    client = fx.client()
    for role in Role:
        r = _observe(client, role)
        assert r.status_code == (201 if role is Role.QA_AUDITOR else 403), role
    body = _observe(client, Role.QA_AUDITOR).json()
    assert body["finding_id"].startswith("OBS-")
    assert body["observation_seq"] + 1 == body["finding_seq"]  # appended together


def test_an_observation_is_seen_and_decided_by_the_right_roles(fx: Fixture) -> None:
    client = fx.client()
    fid = _observe(client, Role.QA_AUDITOR).json()["finding_id"]
    for role in Role:
        r = client.get(f"/api/v1/findings/{fid}", headers=headers(role))
        assert r.status_code == (200 if role in OBSERVATION_ROLES else 404), role
        listed = client.get("/api/v1/quality/observations", headers=headers(role))
        assert listed.status_code == (200 if role in OBSERVATION_ROLES else 403), role
    detail = client.get(f"/api/v1/findings/{fid}", headers=headers(Role.MEDICAL_MONITOR)).json()
    assert detail["finding"]["kind"] == "qa_observation"
    items = client.get("/api/v1/quality/observations", headers=headers(Role.QA_AUDITOR)).json()
    assert items[0]["raised_by"] == PEOPLE[Role.QA_AUDITOR]
    assert _decide(client, Role.QA_AUDITOR, fid).status_code == 403  # the auditor never decides
    assert _decide(client, Role.MEDICAL_MONITOR, fid, "escalated").status_code == 201
    assert _decide(client, Role.CLINICAL_OPS_LEAD, fid).status_code == 201
    queue = client.get("/api/v1/review-queue", headers=headers(Role.CLINICAL_OPS_LEAD)).json()
    fresh = _observe(client, Role.QA_AUDITOR).json()["finding_id"]
    queue = client.get("/api/v1/review-queue", headers=headers(Role.MEDICAL_MONITOR)).json()
    assert fresh in {i["finding_id"] for i in queue["items"]}


def test_a_reference_must_exist_and_be_visible_to_the_auditor(fx: Fixture) -> None:
    client = fx.client()
    restricted = next(
        e
        for e in fx.events
        if isinstance(e.payload, FindingRaised)
        and e.payload.finding.visibility is Visibility.RESTRICTED
    )
    assert isinstance(restricted.payload, FindingRaised)
    hidden_id = restricted.payload.finding.finding_id
    last = fx.events[-1].seq
    for body in (
        {"finding_id": "RBQM-9999"},
        {"finding_id": hidden_id},
        {"ledger_seq": last + 100},
        {"ledger_seq": restricted.seq},
    ):
        r = _observe(client, Role.QA_AUDITOR, **body)
        assert r.status_code == 404, body
        assert "SECRET" not in r.text
    assert _observe(client, Role.QA_AUDITOR, finding_id="RBQM-0001").status_code == 201
    assert _observe(client, Role.QA_AUDITOR, ledger_seq=1).status_code == 201
    assert _observe(client, Role.QA_AUDITOR, finding_id="RBQM-1").status_code == 422


def test_an_observation_about_a_hidden_finding_reaches_only_those_who_see_it(fx: Fixture) -> None:
    audit_id = _raise(fx, finding(FindingKind.AUDIT, visible_to=AUDIT_ROLES, agent=REVIEWER), "a:1")
    client = fx.client()
    fid = _observe(client, Role.QA_AUDITOR, finding_id=audit_id).json()["finding_id"]
    assert (
        client.get(f"/api/v1/findings/{fid}", headers=headers(Role.MEDICAL_MONITOR)).status_code
        == 404
    )
    assert (
        client.get(f"/api/v1/findings/{fid}", headers=headers(Role.CLINICAL_OPS_LEAD)).status_code
        == 200
    )


def test_an_observation_about_a_decision_names_its_decider(fx: Fixture) -> None:
    client = fx.client()
    assert _decide(client, Role.MEDICAL_MONITOR, "RBQM-0001", "escalated").status_code == 201
    seq = max(
        e.seq
        for e in fx.ledger().read()
        if isinstance(e, Event) and isinstance(e.payload, HumanDecision)
    )
    fid = _observe(client, Role.QA_AUDITOR, ledger_seq=seq).json()["finding_id"]
    r = _decide(client, Role.MEDICAL_MONITOR, fid)
    assert r.status_code == 403
    assert "actor under review" in r.json()["detail"]
    assert _decide(client, Role.CLINICAL_OPS_LEAD, fid).status_code == 201


def test_the_audit_tab_shows_the_last_review_and_its_findings(fx: Fixture) -> None:
    client = fx.client()
    empty = client.get("/api/v1/quality/audit", headers=headers(Role.QA_AUDITOR)).json()
    assert (empty["chain_linkage"], empty["review_to_seq"]) == ("not_reviewed", None)
    trigger = fx.events[-1].seq + 1
    from meridian.domain.events import AuditReviewRequested

    fx.append(
        AuditReviewRequested(study_id="MRD-201", through_seq=trigger - 1), actor="meridian.audit"
    )
    fx.append(
        ActorWoke(
            agent_id=REVIEWER,
            agent_version="1.0.0",
            trigger_seq=trigger,
            executor="openshell",
            review_from_seq=1,
            review_to_seq=trigger - 1,
        ),
        actor=REVIEWER,
    )
    fx.raise_finding(
        finding(
            FindingKind.AUDIT,
            visible_to=AUDIT_ROLES,
            agent=REVIEWER,
            title="Audit trail: seq 3 does not link",
        ),
        cut=a.LEDGER_SCOPE,
        check="audit_chain_linkage:link-3",
    )
    fx.append(
        ActorSuspended(
            agent_id=REVIEWER, agent_version="1.0.0", trigger_seq=trigger, executor="openshell"
        ),
        actor=REVIEWER,
    )
    for role in Role:
        r = client.get("/api/v1/quality/audit", headers=headers(role))
        assert r.status_code == (200 if role in AUDIT_ROLES else 403), role
    body = client.get("/api/v1/quality/audit", headers=headers(Role.CLINICAL_OPS_LEAD)).json()
    assert (body["review_from_seq"], body["review_to_seq"]) == (1, trigger - 1)
    assert body["review_completed"] is True
    assert (body["chain_linkage"], body["chain_findings"]) == ("broken", 1)
    counts = {c["check"]: c["count"] for c in body["by_check"]}
    assert counts[a.CHAIN_LINKAGE] == 1
    assert set(counts) == set(a.CHECKS)


# ---- the property test, extended to audit findings and QA observations

token = st.from_regex(r"TOKEN[A-Z]{8}", fullmatch=True)
PATHS = [
    "/api/v1/findings?scope=all&include_superseded=true&include_retired=true&limit=500",
    "/api/v1/review-queue?scope=all",
    "/api/v1/ledger?limit=1000",
    "/api/v1/ledger?order=desc&limit=1000",
    "/api/v1/fleet",
    "/api/v1/portfolio",
    "/api/v1/quality/audit",
    "/api/v1/quality/observations",
]


@pytest.fixture(scope="module")
def tmp_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _hidden_only(events: list[Event], role: Role) -> set[str]:
    hidden: set[str] = set()
    shown: set[str] = set()
    for e in events:
        p = e.payload
        if isinstance(p, FindingRaised):
            strings = _strings(json.loads(p.finding.model_dump_json()))
            (shown if role in p.finding.visible_to else hidden).update(strings)
    text = "\n".join(sorted(shown))
    return {t for s in hidden for t in re.findall(r"TOKEN[A-Z]{8}", s) if t not in text}


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(
    audit_tokens=st.lists(token, min_size=1, max_size=3, unique=True),
    observation_tokens=st.lists(token, min_size=1, max_size=3, unique=True),
)
def test_no_endpoint_shows_audit_findings_or_observations_outside_visible_to(
    tmp_root: Path, audit_tokens: list[str], observation_tokens: list[str]
) -> None:
    if set(audit_tokens) & set(observation_tokens):
        return
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        fx = build(Path(tmp), restricted=False)
        ids = [
            _raise(
                fx,
                finding(
                    FindingKind.AUDIT,
                    under_review="asha.rao",
                    visible_to=AUDIT_ROLES,
                    agent=REVIEWER,
                    title=f"Audit {t}",
                ),
                f"audit_rapid_decisions:{t}",
            )
            for t in audit_tokens
        ]
        client = fx.client()
        for t in observation_tokens:
            r = _observe(
                client, Role.QA_AUDITOR, title=f"Observation {t}", description=f"About {t}."
            )
            ids.append(r.json()["finding_id"])
        events = [e for e in fx.ledger().read() if isinstance(e, Event)]
        for role in Role:
            secrets = _hidden_only(events, role)
            if role not in AUDIT_ROLES:
                assert set(audit_tokens) <= secrets
            if role not in OBSERVATION_ROLES:
                assert set(observation_tokens) <= secrets
            for path in PATHS + [f"/api/v1/findings/{i}" for i in ids]:
                body = client.get(path, headers=headers(role)).text
                leaked = sorted(x for x in secrets if x in body)
                assert leaked == [], (role, path, leaked)


def test_the_finding_says_whether_the_caller_may_decide_it(fx: Fixture) -> None:
    asha = PEOPLE[Role.CLINICAL_OPS_LEAD]
    fid = _raise(
        fx,
        finding(FindingKind.AUDIT, under_review=asha, visible_to=AUDIT_ROLES, agent=REVIEWER),
        "audit_rapid_decisions:asha/1",
    )
    client = fx.client()
    mine = client.get(f"/api/v1/findings/{fid}", headers=headers(Role.CLINICAL_OPS_LEAD)).json()
    assert (mine["finding"]["may_decide"], mine["finding"]["actor_under_review"]) == (False, asha)
    assert "actor under review" in mine["finding"]["decide_blocked_reason"]
    qa = client.get(f"/api/v1/findings/{fid}", headers=headers(Role.QA_AUDITOR)).json()
    assert qa["finding"]["may_decide"] is False
    other = client.get("/api/v1/findings/RBQM-0001", headers=headers(Role.MEDICAL_MONITOR)).json()
    assert other["finding"]["may_decide"] is True
    assert other["finding"]["decide_blocked_reason"] is None
