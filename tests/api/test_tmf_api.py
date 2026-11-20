"""TMF completeness through the API: GET /quality/{cut}/tmf, who may see it, and the
visibility property test extended to TMF payloads."""

import json
import re
import tempfile
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
import structlog
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from meridian.agents.base import AgentContext
from meridian.agents.quality.tmf_completeness import AGENT_ID, VERSION, TmfCompleteness
from meridian.api.app import create_app
from meridian.data.study_store import LayeredStudyReader, ParquetStudyReader
from meridian.domain import tmf as t
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.events import ActorSuspended, ActorWoke, Event, FindingRaised
from meridian.domain.findings import Evidence, Finding
from meridian.faults.reader import FullCutReader
from meridian.faults.tmf import TmfFault, TmfScenario, plant
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.tmf.generate import write_snapshot
from tests.agent_support import trigger
from tests.api.test_visibility import _strings
from tests.api_support import T0, Fixture, build, headers
from tests.tmf_support import tmf_base

CFG = Settings().agents.tmf_completeness
TMF_ROLES = frozenset(CFG.visible_to)
PATH = "/api/v1/quality/DC-BASE/tmf"


def _with_tmf(fx: Fixture, root: Path) -> tuple[TestClient, list[str]]:
    """DC-BASE with a planted eTMF index, the agent's findings raised from a real run."""
    base = tmf_base(root)
    sc = TmfScenario(
        set_name="dev",
        index=1,
        seed=7,
        base_cut="DC-BASE",
        faults=(
            TmfFault(type=t.MISSING_ARTIFACT, params={}),
            TmfFault(type=t.MONITORING_REPORT_MISSING, params={}),
            TmfFault(type=t.APPROVAL_AFTER_FIRST_SUBJECT, params={}),
        ),
    )
    planted = plant(sc, base, CFG.reference)
    study = root / "curated" / "MRD-201"
    write_snapshot(
        etmf_root=root / "etmf",
        study_id="MRD-201",
        cut=FullCutReader(study / "DC-BASE").read(),
        index=planted.index,
        seed=sc.seed,
        clock=FixedClock(T0),
    )
    reader = LayeredStudyReader(
        ParquetStudyReader(study, "DC-BASE"),
        ParquetStudyReader(root / "etmf" / "MRD-201", "DC-BASE"),
    )
    ctx = AgentContext(study=reader, clock=FixedClock(T0), config=CFG, log=structlog.get_logger())
    drafts = TmfCompleteness().run(trigger("DC-BASE"), ctx)
    fx.append(ActorWoke(agent_id=AGENT_ID, agent_version=VERSION, trigger_seq=1), actor=AGENT_ID)
    ids = []
    for d in drafts:
        e = fx.raise_finding(
            Finding(
                kind=d.kind,
                agent_id=AGENT_ID,
                agent_version=VERSION,
                study_id="MRD-201",
                site_id=d.site_id,
                severity=d.severity,
                title=d.title,
                summary=d.summary,
                evidence=d.evidence,
                visibility=Visibility.BLINDED,
                visible_to=frozenset(d.visible_to or ()),
                score=d.score,
            ),
            check=d.dedup_key.split("|")[5],
        )
        assert isinstance(e.payload, FindingRaised)
        assert e.payload.finding.finding_id is not None
        ids.append(e.payload.finding.finding_id)
    fx.append(
        ActorSuspended(agent_id=AGENT_ID, agent_version=VERSION, trigger_seq=1), actor=AGENT_ID
    )
    context = replace(fx.context(curated_root=root / "curated"), etmf_root=root / "etmf")
    return TestClient(create_app(context)), ids


def test_the_tmf_endpoint_serves_the_matrix_and_links_findings(tmp_path: Path) -> None:
    fx = build(tmp_path / "api", restricted=False)
    client, ids = _with_tmf(fx, tmp_path)
    assert len(ids) == 3
    body = client.get(PATH, headers=headers(Role.QA_AUDITOR)).json()
    assert body["run_completed"] is True
    assert body["agent_version"] == VERSION
    assert body["scope"] == "operational"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", body["export_date"])
    assert len(body["artifacts"]) == 16
    site_keys = [a["key"] for a in body["artifacts"] if a["level"] == "site"]
    assert len(body["trial"]) == 7
    for row in body["sites"]:
        assert [c["artifact"] for c in row["cells"]] == site_keys
    linked = {f for c in body["trial"] for f in c["finding_ids"]} | {
        f for row in body["sites"] for c in row["cells"] for f in c["finding_ids"]
    }
    assert linked == set(ids)  # every finding is linked from its cell
    assert {f["finding_id"] for f in body["findings"]} == set(ids)
    assert body["findings"][0]["severity"] == "high"
    missing = [c for row in body["sites"] for c in row["cells"] if c["status"] == "missing"]
    assert missing
    assert body["completeness_percent"] < 100
    statuses = {c["status"] for row in body["sites"] for c in row["cells"]}
    assert statuses <= {"filed", "filed_late", "missing", "not_expected"}


def test_only_the_agents_roles_may_see_the_tmf(tmp_path: Path) -> None:
    fx = build(tmp_path / "api", restricted=False)
    client, _ = _with_tmf(fx, tmp_path)
    for role in Role:
        r = client.get(PATH, headers=headers(role))
        if role in TMF_ROLES:
            assert r.status_code == 200, role
        else:
            assert r.status_code == 403, role
    assert Role.TOXICOLOGIST not in TMF_ROLES
    assert Role.UNBLINDED_STATISTICIAN not in TMF_ROLES
    assert (
        client.get("/api/v1/quality/DC-NOPE/tmf", headers=headers(Role.QA_AUDITOR)).status_code
        == 404
    )


def test_a_cut_without_an_etmf_snapshot_is_not_found(tmp_path: Path) -> None:
    fx = build(tmp_path / "api", restricted=False)
    tmf_base(tmp_path)
    context = replace(fx.context(curated_root=tmp_path / "curated"), etmf_root=tmp_path / "none")
    r = TestClient(create_app(context)).get(PATH, headers=headers(Role.QA_AUDITOR))
    assert r.status_code == 404


# ---- the property test, extended to TMF payloads


def tmf_finding(doc: str, site: str) -> Finding:
    return Finding(
        kind=FindingKind.TMF,
        agent_id=AGENT_ID,
        agent_version=VERSION,
        study_id="MRD-201",
        site_id=site,
        severity=Severity.MEDIUM,
        title=f"Form FDA 1572 of site {site} filed 61 days after its date",
        summary=f"Form FDA 1572 {doc} is dated 20 Jan 2013 and was filed 22 Mar 2013.",
        evidence=(
            Evidence(
                source=t.ETMF_INDEX,
                selector=f"DOCUMENT_ID={doc}; VERSION=1",
                statistic="filing:delay_days",
                value=61.0,
                threshold=30.0,
                role="primary",
                direction="high",
                display="61 days",
            ),
        ),
        visibility=Visibility.BLINDED,
        visible_to=TMF_ROLES,
        score=0.5,
    )


def hidden_only(events: list[Event], role: Role) -> set[str]:
    """Document ids of TMF findings this role may not see that appear in no finding it may."""
    hidden: set[str] = set()
    shown: set[str] = set()
    for e in events:
        if not isinstance(e.payload, FindingRaised):
            continue
        f = e.payload.finding
        strings = _strings(json.loads(f.model_dump_json())) | {e.payload.dedup_key}
        (shown if role in f.visible_to else hidden).update(strings)
    shown_text = "\n".join(sorted(shown))
    tokens = {x for s in hidden for x in re.findall(r"TMF-[0-9A-F]{10}", s)}
    return {x for x in tokens if x not in shown_text}


@pytest.fixture(scope="module")
def tmp_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


doc_id = st.from_regex(r"TMF-[0-9A-F]{10}", fullmatch=True)
PATHS = [
    "/api/v1/findings?scope=all&include_superseded=true&include_retired=true&limit=500",
    "/api/v1/review-queue?scope=all",
    "/api/v1/ledger?limit=1000",
    "/api/v1/ledger?order=desc&limit=1000",
    "/api/v1/fleet",
    "/api/v1/portfolio",
    PATH,
]


@settings(
    max_examples=30,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(docs=st.lists(doc_id, min_size=1, max_size=4, unique=True))
def test_no_endpoint_shows_a_tmf_finding_to_a_role_outside_visible_to(
    tmp_root: Path, docs: list[str]
) -> None:
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        fx = build(Path(tmp), restricted=False)
        ids = []
        for i, doc in enumerate(docs):
            e = fx.raise_finding(
                tmf_finding(doc, f"10{i:02d}"), check=t.check_id(t.LATE_FILING, doc)
            )
            assert isinstance(e.payload, FindingRaised)
            ids.append(e.payload.finding.finding_id)
        client = fx.client()
        for role in Role:
            secrets = hidden_only(fx.events, role)
            if role not in TMF_ROLES:
                assert set(docs) <= secrets  # the test has something to find
            for path in PATHS + [f"/api/v1/findings/{i}" for i in ids]:
                body = client.get(path, headers=headers(role)).text
                leaked = sorted(x for x in secrets if x in body)
                assert leaked == [], (role, path, leaked)
