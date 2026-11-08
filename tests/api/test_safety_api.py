"""SAE reconciliation through the API: who sees which safety finding, and nothing leaks."""

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

from meridian.domain import safety as s
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.events import ActorSuspended, ActorWoke, Event, FindingRaised
from meridian.domain.findings import Evidence, Finding
from meridian.platform.config import Settings
from tests.api.test_visibility import _strings
from tests.api_support import Fixture, build, headers

CFG = Settings().agents.sae_reconciler
SUBJECT_ROLES = frozenset(CFG.subject_visible_to)
SITE_ROLES = frozenset(CFG.site_visible_to)
AGENT = "safety.sae_reconciler"
SUBJECT = "201-1018-006"


def subject_finding(subject: str = SUBJECT, case: str = "SC-1BD5008FBE") -> Finding:
    return Finding(
        kind=FindingKind.SAE_RECONCILIATION,
        agent_id=AGENT,
        agent_version="1.0.0",
        study_id="MRD-201",
        site_id="1018",
        severity=Severity.MEDIUM,
        title=f"Outcome of a serious AE of {subject} differs from its case",
        summary=f"Syncope: outcome RECOVERED/RESOLVED in the clinical database, FATAL in {case}.",
        evidence=(
            Evidence(
                source="ae",
                selector=f"USUBJID={subject}; AESEQ=4; AESER=Y",
                statistic="outcome:clinical",
                value=0.0,
                role="primary",
                direction="none",
                display="RECOVERED/RESOLVED",
            ),
            Evidence(
                source=s.SAFETY_CASES,
                selector=f"CASEID={case}",
                statistic="outcome:safety_db",
                value=0.0,
                role="primary",
                direction="none",
                display="FATAL",
            ),
        ),
        visibility=Visibility.BLINDED,
        visible_to=SUBJECT_ROLES,
        score=0.5,
    )


def site_finding() -> Finding:
    return Finding(
        kind=FindingKind.SAE_RECONCILIATION,
        agent_id=AGENT,
        agent_version="1.0.0",
        study_id="MRD-201",
        site_id="1018",
        severity=Severity.HIGH,
        title="Site 1018 reports serious AEs late",
        summary="2 of the site's 2 safety cases reached the sponsor more than 24 hours late.",
        evidence=(
            Evidence(
                source=s.SAFETY_CASES,
                selector="SITEID=1018",
                statistic="late_cases",
                value=2.0,
                threshold=2.0,
                role="primary",
                direction="high",
                display="2 of 2 cases late",
            ),
            Evidence(
                source=s.SAFETY_CASES,
                selector="SITEID=1018; late cases",
                statistic="median_report_delay_hours",
                value=60.0,
                threshold=24.0,
                role="supporting",
                direction="high",
                display="median 60 hours after awareness",
            ),
        ),
        visibility=Visibility.BLINDED,
        visible_to=SITE_ROLES,
        score=1.0,
    )


def with_safety(fx: Fixture) -> tuple[str, str]:
    """A reconciler run on DC-BASE that raised one subject-level and one site finding."""
    fx.append(ActorWoke(agent_id=AGENT, agent_version="1.0.0", trigger_seq=1), actor=AGENT)
    a = fx.raise_finding(subject_finding(), check=s.check_id(s.OUTCOME_MISMATCH, f"{SUBJECT}/4"))
    b = fx.raise_finding(site_finding(), check=s.check_id(s.LATE_SITE, "1018"))
    fx.append(ActorSuspended(agent_id=AGENT, agent_version="1.0.0", trigger_seq=1), actor=AGENT)
    ids = []
    for e in (a, b):
        assert isinstance(e.payload, FindingRaised)
        assert e.payload.finding.finding_id
        ids.append(e.payload.finding.finding_id)
    return ids[0], ids[1]


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build(tmp_path, restricted=False)


def _get(client: TestClient, role: Role, path: str) -> Any:
    r = client.get(path, headers=headers(role))
    assert r.status_code == 200, (path, r.text)
    return r.json()


def test_reconciliation_per_role(fx: Fixture) -> None:
    subject_id, site_id = with_safety(fx)
    assert subject_id.startswith("SAF-")
    assert site_id.startswith("SAF-")
    client = fx.client()
    for role in Role:
        body = _get(client, role, "/api/v1/safety/DC-BASE/reconciliation")
        listed = [i["finding_id"] for i in body["items"]]
        counts = {c["check"]: c["count"] for c in body["by_check"]}
        assert set(counts) == set(s.CHECKS)  # every check, zero counts too
        assert body["run_completed"] is True
        assert body["agent_version"] == "1.0.0"
        text = json.dumps(body)
        if role in SUBJECT_ROLES:
            assert listed == [subject_id, site_id]
            (item,) = [i for i in body["items"] if i["finding_id"] == subject_id]
            assert (item["subject_id"], item["left"], item["right"]) == (
                SUBJECT,
                "RECOVERED/RESOLVED",
                "FATAL",
            )
            assert (item["left_label"], item["right_label"]) == ("Clinical outcome", "Case outcome")
            assert counts[s.OUTCOME_MISMATCH] == 1
            assert counts[s.LATE_SITE] == 1
        elif role in SITE_ROLES:  # portfolio_lead: the site-level finding only
            assert listed == [site_id]
            assert counts[s.OUTCOME_MISMATCH] == 0
            assert counts[s.LATE_SITE] == 1
            assert body["items"][0]["subject_id"] is None
            assert SUBJECT not in text
            assert "SC-1BD5008FBE" not in text
        else:  # toxicologist, unblinded statistician
            assert listed == []
            assert sum(counts.values()) == 0
            assert body["by_site"] == []


def test_hidden_safety_findings_are_404_and_redacted_in_the_ledger(fx: Fixture) -> None:
    subject_id, site_id = with_safety(fx)
    client = fx.client()
    for role in Role:
        detail = client.get(f"/api/v1/findings/{subject_id}", headers=headers(role))
        site = client.get(f"/api/v1/findings/{site_id}", headers=headers(role))
        assert detail.status_code == (200 if role in SUBJECT_ROLES else 404), role
        assert site.status_code == (200 if role in SITE_ROLES else 404), role
        ledger = _get(client, role, "/api/v1/ledger?limit=1000")
        text = json.dumps(ledger)
        if role not in SUBJECT_ROLES:
            assert SUBJECT not in text, role
            assert "SC-1BD5008FBE" not in text, role
        for path in ("/api/v1/findings?scope=all&limit=500", "/api/v1/review-queue"):
            assert (SUBJECT in json.dumps(_get(client, role, path))) == (role in SUBJECT_ROLES)


def test_an_unknown_cut_is_404(fx: Fixture) -> None:
    r = fx.client().get("/api/v1/safety/DC-NOPE/reconciliation", headers=headers(Role.QA_AUDITOR))
    assert r.status_code == 404


def test_the_rbqm_heatmap_ignores_safety_findings(fx: Fixture) -> None:
    from meridian.api.identity import Person
    from meridian.api.repository import Repository

    with_safety(fx)
    ctx = fx.context()
    with fx.ledger() as ledger:
        repo = Repository(
            ledger=ledger,
            person=Person(actor="nina.fernandes", name="Nina", role=Role.QA_AUDITOR),
            clock=ctx.clock,
            study_id="MRD-201",
            registry=ctx.registry,
            policies_dir=ctx.policies_dir,
            ground_truth_dir=ctx.ground_truth_dir,
            portfolio=ctx.portfolio,
        )
        flagged = repo.flagged_checks("DC-BASE")
    agents = {f.agent_id for flags in flagged.values() for f in flags}
    assert agents
    assert all(a.startswith("rbqm.") for a in agents)


def test_unblinded_case_counts_only_for_the_unblinded_statistician(tmp_path: Path) -> None:
    from meridian.platform.clock import FixedClock
    from meridian.safetydb.generate import ensure_clean_snapshot
    from tests.safety_support import base_cut

    fx = build(tmp_path / "api", restricted=False)
    cut = base_cut(tmp_path)
    ensure_clean_snapshot(
        safety_root=tmp_path / "safety", study_id="MRD-201", cut=cut, clock=FixedClock(fx_t0())
    )
    context = fx.context(curated_root=tmp_path / "curated")
    from dataclasses import replace

    from fastapi.testclient import TestClient as Client

    from meridian.api.app import create_app

    client = Client(create_app(replace(context, safety_root=tmp_path / "safety")))
    for role in Role:
        r = client.get("/api/v1/safety/unblinded-cases", headers=headers(role))
        if role is Role.UNBLINDED_STATISTICIAN:
            body = r.json()
            assert r.status_code == 200
            assert body["cases"] > body["cases_with_treatment"] > 0
            assert set(body) == {"cut_id", "cases", "cases_with_treatment"}
        else:
            assert r.status_code == 403, role


def fx_t0() -> Any:
    from tests.api_support import T0

    return T0


# ---- the property test, extended to safety payloads


def hidden_only(events: list[Event], role: Role) -> set[str]:
    """Strings of findings this role may not see that appear in no finding it may see."""
    hidden: set[str] = set()
    shown: set[str] = set()
    for e in events:
        if not isinstance(e.payload, FindingRaised):
            continue
        f = e.payload.finding
        strings = _strings(json.loads(f.model_dump_json())) | {e.payload.dedup_key}
        (shown if role in f.visible_to else hidden).update(strings)
    shown_text = "\n".join(sorted(shown))
    # Subject ids and case ids inside the hidden strings (agent ids and fixed words are
    # public: they name who ran, not whom it is about).
    tokens = {t for x in hidden for t in re.findall(r"201-\d{4}-\d{3}|SC-[0-9A-F]{10}", x)}
    return {x for x in tokens if x not in shown_text}


@pytest.fixture(scope="module")
def tmp_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


subject = st.from_regex(r"201-10[0-9]{2}-0[0-9]{2}", fullmatch=True)
PATHS = [
    "/api/v1/findings?scope=all&include_superseded=true&include_retired=true&limit=500",
    "/api/v1/review-queue?scope=all",
    "/api/v1/ledger?limit=1000",
    "/api/v1/ledger?order=desc&limit=1000",
    "/api/v1/safety/DC-BASE/reconciliation",
    "/api/v1/fleet",
    "/api/v1/portfolio",
]


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(subjects=st.lists(subject, min_size=1, max_size=4, unique=True), with_site=st.booleans())
def test_no_endpoint_leaks_a_subject_id_to_a_role_outside_visible_to(
    tmp_root: Path, subjects: list[str], with_site: bool
) -> None:
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        fx = build(Path(tmp), restricted=False)
        fx.append(ActorWoke(agent_id=AGENT, agent_version="1.0.0", trigger_seq=1), actor=AGENT)
        ids = []
        for i, u in enumerate(subjects):
            e = fx.raise_finding(
                subject_finding(u, case=f"SC-{i:010X}"),
                check=s.check_id(s.OUTCOME_MISMATCH, f"{u}/{i}"),
            )
            assert isinstance(e.payload, FindingRaised)
            ids.append(e.payload.finding.finding_id)
        if with_site:
            fx.raise_finding(site_finding(), check=s.check_id(s.LATE_SITE, "1018"))
        client = fx.client()
        for role in Role:
            secrets = hidden_only(fx.events, role)
            if role not in SUBJECT_ROLES:
                assert set(subjects) <= secrets  # the test has something to find
            for path in PATHS + [f"/api/v1/findings/{i}" for i in ids]:
                body = client.get(path, headers=headers(role)).text
                leaked = sorted(x for x in secrets if x in body)
                assert leaked == [], (role, path, leaked)


def late_finding(case: str) -> Finding:
    """A late report: its evidence names the case only, as safety.sae_reconciler 1.0.0's does."""
    sel = f"CASEID={case}"
    return Finding(
        kind=FindingKind.SAE_RECONCILIATION,
        agent_id=AGENT,
        agent_version="1.0.0",
        study_id="MRD-201",
        site_id="1018",
        severity=Severity.HIGH,
        title=f"Safety case {case} reached the sponsor 2.5 days after awareness",
        summary="The sponsor received the case 60 hours after the site became aware.",
        evidence=(
            Evidence(
                source=s.SAFETY_CASES,
                selector=sel,
                statistic="report_delay_hours",
                value=60.0,
                threshold=24.0,
                role="primary",
                direction="high",
                display="60 hours after awareness",
            ),
            Evidence(
                source=s.SAFETY_CASES,
                selector=sel,
                statistic="awareness:safety_db",
                value=0.0,
                role="supporting",
                direction="none",
                display="2026-01-01T08:00",
            ),
            Evidence(
                source=s.SAFETY_CASES,
                selector=sel,
                statistic="receipt:safety_db",
                value=0.0,
                role="supporting",
                direction="none",
                display="2026-01-03T20:00",
            ),
        ),
        visibility=Visibility.BLINDED,
        visible_to=SUBJECT_ROLES,
        score=1.0,
    )


def test_late_case_items_carry_the_subject_for_subject_level_roles(tmp_path: Path) -> None:
    from dataclasses import replace

    from meridian.api.app import create_app
    from meridian.data.study_store import ParquetStudyReader
    from meridian.platform.clock import FixedClock
    from meridian.safetydb.generate import ensure_clean_snapshot
    from tests.safety_support import base_cut

    fx = build(tmp_path / "api", restricted=False)
    cut = base_cut(tmp_path)
    ensure_clean_snapshot(
        safety_root=tmp_path / "safety", study_id="MRD-201", cut=cut, clock=FixedClock(fx_t0())
    )
    cases = ParquetStudyReader(tmp_path / "safety" / "MRD-201", "DC-BASE").table(s.SAFETY_CASES)
    case, subject = cases.sort(s.CASEID).select(s.CASEID, s.USUBJID).row(0)
    fx.append(ActorWoke(agent_id=AGENT, agent_version="1.0.0", trigger_seq=1), actor=AGENT)
    raised = fx.raise_finding(late_finding(case), check=s.check_id(s.LATE_REPORT, case))
    fx.raise_finding(site_finding(), check=s.check_id(s.LATE_SITE, "1018"))
    fx.append(ActorSuspended(agent_id=AGENT, agent_version="1.0.0", trigger_seq=1), actor=AGENT)
    assert isinstance(raised.payload, FindingRaised)
    late_id = raised.payload.finding.finding_id
    context = replace(fx.context(), safety_root=tmp_path / "safety")
    client = TestClient(create_app(context))
    for role in Role:
        body = _get(client, role, "/api/v1/safety/DC-BASE/reconciliation")
        by_id = {i["finding_id"]: i for i in body["items"]}
        text = json.dumps(body)
        if role in SUBJECT_ROLES:
            assert by_id[late_id]["check"] == s.LATE_REPORT
            assert by_id[late_id]["subject_id"] == subject
            assert by_id[late_id]["site_id"] == "1018"
        else:
            assert late_id not in by_id
            assert subject not in text, role
            assert case not in text, role
    # Without the safety database snapshot the item is still served, without a subject.
    bare = TestClient(create_app(replace(context, safety_root=tmp_path / "absent")))
    item = _get(bare, Role.MEDICAL_MONITOR, "/api/v1/safety/DC-BASE/reconciliation")["items"]
    assert [i["subject_id"] for i in item if i["finding_id"] == late_id] == [None]
