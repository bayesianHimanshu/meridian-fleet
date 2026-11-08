"""The property test by finding: no GET response shows a role anything of a finding it
cannot open.

test_visibility's property test looks for strings of restricted payloads. It did not see
that GET /ledger showed RBQM FindingRaised events in full to portfolio_lead (outside their
visible_to) while GET /findings/{id} returned 404, because those findings were blinded.
This test generates random ledgers of findings with random visible_to (RBQM, safety
subject-level and site-level, restricted), narratives and decisions, and checks, for every
role and every GET endpoint, that no response contains the finding id, the subject id or an
evidence display string of a finding that role cannot open (its detail page is 404).

test_the_property_fails_under_the_old_ledger_redaction runs the same check against the
ledger redaction as it was before the fix (a test double for
visibility.redact_with_findings), so the test is shown to catch that leak.
"""

import tempfile
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st

from meridian.api import visibility
from meridian.domain import safety as s
from meridian.domain.enums import DecisionAction, FindingKind, Role, Severity, Visibility
from meridian.domain.events import FindingNarrative, FindingRaised, HumanDecision, LedgerEntry
from meridian.domain.findings import Decision, Evidence, Finding
from tests.api_support import STUDY, Fixture, build, headers

ROLES = list(Role)
KINDS = ("rbqm", "safety_subject", "safety_v11", "safety_site", "restricted")
# safety.sae_reconciler 1.1 checks, with 1.1 evidence (case id, version and subject).
V11_CHECKS = (
    s.DATA_QUALITY,
    s.PROBABLE_DUPLICATE,
    s.PARTIAL_DATE,
    s.CRITERIA_MISMATCH,
    s.SUBJECT_MISMATCH,
    s.TERM_MISMATCH,
    s.LATE_REPORT,
)

PATHS = [
    "/api/v1/whoami",
    "/api/v1/studies",
    f"/api/v1/studies/{STUDY}/cuts",
    "/api/v1/findings?scope=all&include_superseded=true&include_retired=true&limit=500",
    "/api/v1/findings?status=dismissed&limit=500",
    "/api/v1/review-queue?scope=all",
    "/api/v1/ledger?limit=1000",
    "/api/v1/ledger?order=desc&limit=1000",
    "/api/v1/ledger?type=finding_raised&limit=1000",
    "/api/v1/ledger/verify",
    "/api/v1/fleet",
    "/api/v1/portfolio",
    "/api/v1/rbqm/DC-BASE/sites",
    "/api/v1/safety/DC-BASE/reconciliation",
]

spec = st.fixed_dictionaries(
    {
        "kind": st.sampled_from(KINDS),
        "visible_to": st.sets(st.sampled_from(ROLES), min_size=1),
        "word": st.from_regex(r"[A-Za-z]{6,10}", fullmatch=True),
        "narrative": st.booleans(),
        "decision": st.sampled_from([None, "accepted", "escalated", "dismissed"]),
    }
)
specs_strategy = st.lists(spec, min_size=1, max_size=6)


def _finding(i: int, sp: dict[str, Any]) -> tuple[Finding, str, str | None]:
    """The finding of spec i, its check, and its subject id (if subject-level). Every
    display string and subject id is unique to its finding (the index is in it)."""
    display = f"Disp{i}x{sp['word']}"
    roles = frozenset(sp["visible_to"])
    kind = sp["kind"]
    ev = Evidence(
        source=s.SAFETY_CASES if kind.startswith("safety") else "ops/deviations",
        selector=f"SITEID=10{i:02d}",
        statistic="x",
        value=1.0,
        role="primary",
        direction="high",
        display=display,
    )
    subject = f"201-10{i:02d}-0{i:02d}" if kind in ("safety_subject", "safety_v11") else None
    if kind == "safety_subject":
        ev = ev.model_copy(update={"selector": f"USUBJID={subject}; AESEQ=1"})
    v11 = V11_CHECKS[i % len(V11_CHECKS)]
    if kind == "safety_v11":
        ev = ev.model_copy(update={"selector": f"CASEID=SC-V{i:02d}; VERSION=2; USUBJID={subject}"})
    agent, version, fkind, check = {
        "rbqm": ("rbqm.kri_monitor", "2.2.0", FindingKind.KRI_BREACH, f"c{i}"),
        "restricted": ("unblinded.efficacy_watch", "0.1.0", FindingKind.KRI_BREACH, f"e{i}"),
        "safety_subject": (
            "safety.sae_reconciler",
            "1.0.0",
            FindingKind.SAE_RECONCILIATION,
            s.check_id(s.OUTCOME_MISMATCH, f"{subject}/1"),
        ),
        "safety_v11": (
            "safety.sae_reconciler",
            "1.1.0",
            FindingKind.SAE_RECONCILIATION,
            s.check_id(v11, f"SC-V{i:02d}"),
        ),
        "safety_site": (
            "safety.sae_reconciler",
            "1.0.0",
            FindingKind.SAE_RECONCILIATION,
            s.check_id(s.LATE_SITE, f"10{i:02d}"),
        ),
    }[kind]
    restricted = kind == "restricted"
    finding = Finding(
        kind=fkind,
        agent_id=agent,
        agent_version=version,
        study_id=STUDY,
        site_id=f"10{i:02d}",
        severity=Severity.MEDIUM,
        title=f"Finding {i}" + (f" about {subject}" if subject else ""),
        summary=f"Summary {i}" + (f" of {subject}" if subject else ""),
        evidence=(ev,),
        visibility=Visibility.RESTRICTED if restricted else Visibility.BLINDED,
        visible_to=roles,
        score=1.0,
    )
    return finding, check, subject


def build_random(root: Path, specs: list[dict[str, Any]]) -> tuple[Fixture, list[dict[str, Any]]]:
    """The standard fixture plus the generated findings; returns what each one exposes."""
    fx = build(root, restricted=False)
    out: list[dict[str, Any]] = []
    for i, sp in enumerate(specs):
        finding, check, subject = _finding(i, sp)
        raised = fx.raise_finding(finding, check=check)
        assert isinstance(raised.payload, FindingRaised)
        fid = raised.payload.finding.finding_id
        assert fid is not None
        if sp["narrative"]:
            fx.append(
                FindingNarrative(
                    finding_id=fid,
                    finding_seq=raised.seq,
                    agent_id="rbqm.narrator",
                    agent_version="1.1.0",
                    status="accepted",
                    headline=f"Headline {i}",
                    explanation=f"Explained {i}",
                    model="fake",
                    request_sha256="d" * 64,
                ),
                finding.visibility,
            )
        if sp["decision"]:
            fx.append(
                HumanDecision(
                    decision=Decision(
                        finding_id=fid,
                        action=DecisionAction(sp["decision"]),
                        actor="someone",
                        role=Role.UNBLINDED_STATISTICIAN
                        if sp["kind"] == "restricted"
                        else Role.CLINICAL_OPS_LEAD,
                        reason=f"Reason {i}",
                    )
                ),
                finding.visibility,
            )
        tokens = {fid, finding.evidence[0].display or ""} | ({subject} if subject else set())
        out.append({"finding_id": fid, "tokens": tokens})
    # The fixture's own two RBQM findings are visible to the RBQM roles only.
    for e in fx.events:
        p = e.payload
        if isinstance(p, FindingRaised) and p.finding.agent_id == "rbqm.kri_monitor":
            fid = p.finding.finding_id
            if fid is not None and all(o["finding_id"] != fid for o in out):
                displays = {ev.display or "" for ev in p.finding.evidence} - {""}
                # The display is shared by both fixture findings; only the id is unique.
                out.append(
                    {
                        "finding_id": fid,
                        "tokens": {fid} | (displays - {"18 observed (5.1 expected)"}),
                    }
                )
    return fx, out


def leaks(fx: Fixture, exposed: list[dict[str, Any]]) -> list[tuple[str, str, list[str]]]:
    """(role, path, leaked tokens) for every response that shows a role a token of a
    finding whose detail page it gets 404 on."""
    client = fx.client()
    found: list[tuple[str, str, list[str]]] = []
    for role in ROLES:
        hidden: set[str] = set()
        shown: set[str] = set()
        for x in exposed:
            r = client.get(f"/api/v1/findings/{x['finding_id']}", headers=headers(role))
            assert r.status_code in (200, 404), r.text
            (shown if r.status_code == 200 else hidden).update(x["tokens"])
        secrets = hidden - shown
        if not secrets:
            continue
        paths = PATHS + [f"/api/v1/findings/{x['finding_id']}" for x in exposed]
        for path in paths:
            body = client.get(path, headers=headers(role)).text
            leaked = sorted(t for t in secrets if t in body)
            if leaked:
                found.append((role.value, path, leaked))
    return found


@pytest.fixture(scope="module")
def tmp_root() -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield Path(tmp)


def _check(root: Path, specs: list[dict[str, Any]]) -> list[tuple[str, str, list[str]]]:
    with tempfile.TemporaryDirectory(dir=root) as tmp:
        fx, exposed = build_random(Path(tmp), specs)
        return leaks(fx, exposed)


@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@given(specs=specs_strategy)
def test_no_get_response_shows_anything_of_a_finding_the_role_cannot_open(
    tmp_root: Path, specs: list[dict[str, Any]]
) -> None:
    assert _check(tmp_root, specs) == []


# ---- the test has teeth


def old_redact_with_findings(
    role: Role, entry: LedgerEntry, visible_finding_ids: Collection[str]
) -> LedgerEntry:
    """The ledger redaction before the fix: restricted events only, nothing by finding."""
    return visibility.redact(role, entry)


@contextmanager
def old_redaction() -> Iterator[None]:
    with mock.patch.object(visibility, "redact_with_findings", old_redact_with_findings):
        yield


def minimal_failure(root: Path) -> tuple[list[dict[str, Any]], list[tuple[str, str, list[str]]]]:
    """Hypothesis' shrunk example of a ledger that leaks under the old redaction."""

    def check(specs: list[dict[str, Any]]) -> list[tuple[str, str, list[str]]]:
        return _check(root, specs)

    with old_redaction():
        example = find(
            specs_strategy,
            lambda specs: bool(check(specs)),
            settings=settings(max_examples=200, deadline=None, database=None),
        )
        return example, check(example)


def test_the_property_fails_under_the_old_ledger_redaction(tmp_root: Path) -> None:
    example, found = minimal_failure(tmp_root)
    assert found
    paths = {path for _, path, _ in found}
    assert paths <= {p for p in PATHS if p.startswith("/api/v1/ledger")}, found
    # The leak of phase 3 step 1: the fixture's RBQM findings, in full, to portfolio_lead.
    portfolio = {t for role, _, tokens in found if role == "portfolio_lead" for t in tokens}
    assert {"RBQM-0001", "RBQM-0002"} <= portfolio, found
    # And the same ledger passes with the redaction as it is.
    assert _check(tmp_root, example) == []
