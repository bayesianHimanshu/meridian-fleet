"""Visibility through the API: restricted data never reaches a role that may not see it."""

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

from meridian.domain.enums import (
    DecisionAction,
    EventType,
    FindingKind,
    Role,
    Severity,
    Visibility,
)
from meridian.domain.events import (
    Event,
    FindingNarrative,
    FindingRaised,
    HumanDecision,
    RedactedEvent,
)
from meridian.domain.findings import Decision
from meridian.ledger.hashing import canonical_json
from tests.api_support import (
    OPAQUE_CUT,
    STUDY,
    Fixture,
    blinded_finding,
    build,
    headers,
    restricted_finding,
)

BLINDED_ROLES = [r for r in Role if r is not Role.UNBLINDED_STATISTICIAN]
MAX_EXAMPLES = 60


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build(tmp_path)


def _get(client: TestClient, role: Role, path: str) -> Any:
    response = client.get(path, headers=headers(role))
    assert response.status_code == 200, (path, response.text)
    return response.json()


def test_restricted_findings_are_invisible_and_uncounted_for_blinded_roles(fx: Fixture) -> None:
    client = fx.client()
    for role in BLINDED_ROLES:
        page = _get(client, role, "/api/v1/findings")
        assert all(i["visibility"] == "blinded" for i in page["items"])
        assert "DSMB-0001" not in json.dumps(page)
        queue = _get(client, role, "/api/v1/review-queue")
        assert "DSMB-0001" not in json.dumps(queue)
        assert client.get("/api/v1/findings/DSMB-0001", headers=headers(role)).status_code == 404
    clinical = _get(client, Role.CLINICAL_OPS_LEAD, "/api/v1/findings")
    assert clinical["total"] == 2
    portfolio = _get(client, Role.CLINICAL_OPS_LEAD, "/api/v1/portfolio")
    (mrd201,) = [a for a in portfolio["assets"] if a["asset_id"] == STUDY]
    assert mrd201["live"] == {"open_findings": 2}  # the restricted finding is not counted
    # Roles outside the findings' visible_to see none of them, and count none.
    assert _get(client, Role.PORTFOLIO_LEAD, "/api/v1/findings")["total"] == 0


def test_the_unblinded_statistician_sees_restricted_findings_in_their_visible_to(
    fx: Fixture,
) -> None:
    client = fx.client()
    page = _get(client, Role.UNBLINDED_STATISTICIAN, "/api/v1/findings")
    assert [i["finding_id"] for i in page["items"]] == ["DSMB-0001"]
    detail = _get(client, Role.UNBLINDED_STATISTICIAN, "/api/v1/findings/DSMB-0001")
    assert detail["narratives"][0]["headline"] == "SECRETHEADLINE"
    # RBQM findings are not in their visible_to.
    r = client.get("/api/v1/findings/RBQM-0001", headers=headers(Role.UNBLINDED_STATISTICIAN))
    assert r.status_code == 404


def test_ledger_redaction_through_the_api_matches_the_ledger(fx: Fixture) -> None:
    client = fx.client()
    with fx.ledger() as ledger:
        for role in Role:
            expected = [json.loads(e.model_dump_json()) for e in ledger.read(viewer_role=role)]
            got = _get(client, role, "/api/v1/ledger?limit=1000")["entries"]
            assert got == expected, role
    redacted = [
        e for e in _get(client, Role.QA_AUDITOR, "/api/v1/ledger")["entries"] if "marker" in e
    ]
    assert [e["event_type"] for e in redacted] == ["finding_raised", "finding_narrative"]
    assert all(
        set(e) == {"seq", "event_type", "recorded_at", "hash", "prev_hash", "marker"}
        for e in redacted
    )


def test_ledger_pages_and_type_filter(fx: Fixture) -> None:
    client = fx.client()
    first = _get(client, Role.QA_AUDITOR, "/api/v1/ledger?limit=3")
    assert [e["seq"] for e in first["entries"]] == [1, 2, 3]
    assert first["next_since"] == 3
    rest = _get(client, Role.QA_AUDITOR, "/api/v1/ledger?since=3&limit=1000")
    assert rest["entries"][0]["seq"] == 4
    assert rest["next_since"] is None
    cuts = _get(client, Role.QA_AUDITOR, "/api/v1/ledger?type=data_cut")
    assert [e["payload"]["type"] for e in cuts["entries"]] == ["data_cut", "data_cut"]


def test_opaque_cut_labels_only_for_qa_and_ground_truth_never(fx: Fixture) -> None:
    client = fx.client()
    for role in Role:
        cuts = _get(client, role, f"/api/v1/studies/{STUDY}/cuts")
        text = json.dumps(cuts)
        assert "SECRETFAULT" not in text
        assert "fault_type" not in text
        (opaque,) = [c for c in cuts if c["cut_id"] == OPAQUE_CUT]
        assert opaque["opaque"] is True
        if role is Role.QA_AUDITOR:
            assert (opaque["label"], opaque["kind"]) == ("rbqm_holdout9", "holdout")
        else:
            assert (opaque["label"], opaque["kind"]) == (None, None)
        (base,) = [c for c in cuts if c["cut_id"] == "DC-BASE"]
        assert (base["kind"], base["label"], base["agent_runs_completed"]) == ("base", None, 1)


# ---- the property test


def _strings(obj: object, keys: bool = False) -> set[str]:
    """String values (and, with keys, field names) anywhere in a JSON value."""
    if isinstance(obj, dict):
        out = {s for v in obj.values() for s in _strings(v, keys)}
        return out | ({str(k) for k in obj} if keys else set())
    if isinstance(obj, list):
        return {s for v in obj for s in _strings(v, keys)}
    return {obj} if isinstance(obj, str) else set()


# Enum values and fixed words appear in any payload; they are not information.
_COMMON = {
    v.value for e in (Role, Visibility, Severity, FindingKind, EventType, DecisionAction) for v in e
}


# Short digit or hex strings (a site id "1003", a version "0.1.0") occur by chance inside
# the hashes and uuids of any response, so a substring search cannot tell them apart.
_HEXLIKE = re.compile(r"[0-9a-f.:-]{1,15}")


def restricted_only(events: list[Event]) -> set[str]:
    """String values of restricted payloads that appear nowhere in a blinded event (not
    even inside a longer public string). Field names are structure, not information."""
    restricted: set[str] = set()
    public: set[str] = set()
    for e in events:
        payload = json.loads(canonical_json(e.payload))
        if e.visibility is Visibility.RESTRICTED:
            restricted |= _strings(payload)
        else:
            public |= _strings(payload, keys=True) | {e.actor, e.study_id or ""}
    public_text = "\n".join(sorted(public))
    return {
        s
        for s in restricted - _COMMON
        if len(s) >= 4 and s not in public_text and not _HEXLIKE.fullmatch(s)
    }


word = st.from_regex(r"[A-Za-z]{6,12}", fullmatch=True)
spec = st.fixed_dictionaries(
    {
        "restricted": st.booleans(),
        "title": word,
        "summary": word,
        "display": word,
        "narrative": st.one_of(st.none(), word),
        "decision": st.sampled_from([None, "accepted", "escalated", "dismissed"]),
        "reason": word,
    }
)


@pytest.fixture(scope="module")
def tmp_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _build_random(root: Path, specs: list[dict[str, Any]]) -> Fixture:
    fx = build(root, restricted=False)
    for i, s in enumerate(specs):
        if s["restricted"]:
            finding = restricted_finding(s["title"], s["summary"], s["display"])
        else:
            finding = blinded_finding(f"{1001 + i}", s["title"], s["summary"])
        raised = fx.raise_finding(finding, check=f"c{i}")
        assert isinstance(raised.payload, FindingRaised)
        fid = raised.payload.finding.finding_id
        assert fid is not None
        if s["narrative"]:
            fx.append(
                FindingNarrative(
                    finding_id=fid,
                    finding_seq=raised.seq,
                    agent_id="rbqm.narrator",
                    agent_version="1.1.0",
                    status="accepted",
                    headline=s["narrative"],
                    explanation=s["narrative"] + " explained",
                    model="fake",
                    request_sha256="d" * 64,
                ),
                finding.visibility,
            )
        if s["decision"]:
            role = Role.UNBLINDED_STATISTICIAN if s["restricted"] else Role.CLINICAL_OPS_LEAD
            fx.append(
                HumanDecision(
                    decision=Decision(
                        finding_id=fid,
                        action=DecisionAction(s["decision"]),
                        actor="someone",
                        role=role,
                        reason=s["reason"],
                    )
                ),
                finding.visibility,
            )
    return fx


GET_PATHS = [
    "/api/v1/whoami",
    "/api/v1/studies",
    f"/api/v1/studies/{STUDY}/cuts",
    "/api/v1/findings?limit=500",
    "/api/v1/findings?status=open&limit=500",
    "/api/v1/findings?status=dismissed&limit=500",
    "/api/v1/review-queue",
    "/api/v1/ledger?limit=1000",
    "/api/v1/ledger/verify",
    "/api/v1/fleet",
    "/api/v1/portfolio",
    "/api/v1/rbqm/DC-BASE/sites",
]


@settings(
    max_examples=MAX_EXAMPLES,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(specs=st.lists(spec, min_size=1, max_size=6))
def test_no_get_endpoint_leaks_a_restricted_string(
    tmp_root: Path, specs: list[dict[str, Any]]
) -> None:
    with tempfile.TemporaryDirectory(dir=tmp_root) as tmp:
        fx = _build_random(Path(tmp), specs)
        secrets = restricted_only(fx.events)
        ids = sorted(
            e.payload.finding.finding_id or ""
            for e in fx.events
            if isinstance(e.payload, FindingRaised)
        )
        paths = GET_PATHS + [f"/api/v1/findings/{i}" for i in ids]
        client = fx.client()
        for role in BLINDED_ROLES:
            for path in paths:
                body = client.get(path, headers=headers(role)).text
                leaked = sorted(s for s in secrets if s in body)
                assert leaked == [], (role, path, leaked)
        if any(s["restricted"] for s in specs):
            assert secrets  # the test has something to find


def test_restricted_only_ignores_strings_that_are_also_public() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        fx = _build_random(
            Path(tmp),
            [
                {
                    "restricted": True,
                    "title": "Shared",
                    "summary": "OnlySecret",
                    "display": "dd",
                    "narrative": None,
                    "decision": None,
                    "reason": "r",
                },
                {
                    "restricted": False,
                    "title": "Shared",
                    "summary": "Public",
                    "display": "dd",
                    "narrative": None,
                    "decision": None,
                    "reason": "r",
                },
            ],
        )
        secrets = restricted_only(fx.events)
    assert "OnlySecret" in secrets
    assert "Shared" not in secrets
    assert "DSMB-0001" in secrets  # the restricted finding's id itself
    assert not any(isinstance(e, RedactedEvent) for e in fx.events)
