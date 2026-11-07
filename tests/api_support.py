"""A temp ledger and Fleet API app for API tests, built from fixtures.

The ledger holds two DataCuts (DC-BASE and an opaque one), a sandboxed run of
rbqm.kri_monitor 2.2.0 on DC-BASE that raised one blinded finding (with ActorWoke and
ActorSuspended carrying executor, image digest, policy and manifest hashes), a second
blinded finding, one restricted finding of an unblinded agent with its own narrative, a
narrator 1.1.0 narrative on the blinded finding, and a ContainmentCheckCompleted.
"""

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from meridian.agents.catalog import build_registry
from meridian.api.app import create_app, load_portfolio
from meridian.api.deps import ApiContext
from meridian.api.identity import People
from meridian.domain.dedup import make_dedup_key
from meridian.domain.enums import FindingKind, Role, Severity, Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    ContainmentCheckCompleted,
    DataCut,
    Event,
    EventDraft,
    EventPayload,
    FindingNarrative,
    FindingRaised,
)
from meridian.domain.findings import Evidence, Finding
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import FixedClock
from meridian.platform.config import DEFAULT_FINDING_ID_PREFIXES, Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
T0 = datetime(2026, 11, 8, 9, 0, tzinfo=UTC)
STUDY = "MRD-201"
OPAQUE_CUT = "DC-1a2b3c4d"
IMAGE_DIGEST = "sha256:" + "a" * 64
POLICY_SHA = "b" * 64
MANIFEST_SHA = "c" * 64
RBQM_ROLES = frozenset({Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR})
PEOPLE = {
    Role.CLINICAL_OPS_LEAD: "asha.rao",
    Role.MEDICAL_MONITOR: "karan.mehta",
    Role.QA_AUDITOR: "nina.fernandes",
    Role.PORTFOLIO_LEAD: "rahul.iyer",
    Role.TOXICOLOGIST: "leela.nair",
    Role.UNBLINDED_STATISTICIAN: "omar.siddiqui",
}


def headers(role: Role) -> dict[str, str]:
    return {"X-Meridian-Actor": PEOPLE[role], "X-Meridian-Role": role.value}


def evidence(display: str = "18 observed (5.1 expected)") -> Evidence:
    return Evidence(
        source="ops/deviations",
        selector="SITEID=1016",
        statistic="deviations:observed",
        value=18.0,
        role="primary",
        direction="high",
        expected=5.1,
        display=display,
    )


def blinded_finding(site: str, title: str, summary: str, agent_version: str = "2.2.0") -> Finding:
    return Finding(
        kind=FindingKind.KRI_BREACH,
        agent_id="rbqm.kri_monitor",
        agent_version=agent_version,
        study_id=STUDY,
        site_id=site,
        severity=Severity.HIGH,
        title=title,
        summary=summary,
        evidence=(evidence(),),
        visibility=Visibility.BLINDED,
        visible_to=RBQM_ROLES,
        score=4.2,
    )


def restricted_finding(title: str, summary: str, display: str) -> Finding:
    return Finding(
        kind=FindingKind.KRI_BREACH,
        agent_id="unblinded.efficacy_watch",
        agent_version="0.1.0",
        study_id=STUDY,
        site_id="1003",
        severity=Severity.MEDIUM,
        title=title,
        summary=summary,
        evidence=(evidence(display),),
        visibility=Visibility.RESTRICTED,
        visible_to=frozenset({Role.UNBLINDED_STATISTICIAN}),
        score=1.0,
    )


@dataclass
class Fixture:
    root: Path
    ledger_path: Path
    events: list[Event] = field(default_factory=list)

    def ledger(self) -> SqliteLedger:
        return SqliteLedger(
            path=self.ledger_path,
            clock=FixedClock(T0),
            finding_id_prefixes=DEFAULT_FINDING_ID_PREFIXES,
        )

    def append(
        self, payload: EventPayload, visibility: Visibility = Visibility.BLINDED, actor: str = "t"
    ) -> Event:
        with self.ledger() as ledger:
            event = ledger.append(
                EventDraft(
                    occurred_at=T0,
                    actor=actor,
                    study_id=STUDY,
                    visibility=visibility,
                    payload=payload,
                )
            )
        self.events.append(event)
        return event

    def raise_finding(
        self, finding: Finding, cut: str = "DC-BASE", check: str = "deviations"
    ) -> Event:
        key = make_dedup_key(
            agent_id=finding.agent_id,
            agent_version=finding.agent_version,
            kind=finding.kind,
            study_id=STUDY,
            site_id=finding.site_id,
            cut_id=cut,
            check=check,
        )
        return self.append(
            FindingRaised(finding=finding, dedup_key=key), finding.visibility, finding.agent_id
        )

    def context(self, curated_root: Path | None = None) -> ApiContext:
        settings = Settings()
        return ApiContext(
            ledger_path=self.ledger_path,
            finding_id_prefixes=dict(DEFAULT_FINDING_ID_PREFIXES),
            clock=FixedClock(T0),
            study_id=STUDY,
            people=People.load(REPO_ROOT / "config" / "people.toml"),
            registry=build_registry(settings.agents),
            agents=settings.agents,
            policies_dir=REPO_ROOT / "policies",
            ground_truth_dir=self.root / "ground_truth",
            curated_root=curated_root or self.root / "curated",
            portfolio=load_portfolio(REPO_ROOT / "config" / "portfolio.toml"),
        )

    def client(self, curated_root: Path | None = None) -> TestClient:
        return TestClient(create_app(self.context(curated_root)))


def build(root: Path, *, restricted: bool = True) -> Fixture:
    """The standard fixture. Restricted strings all contain "SECRET"."""
    root.mkdir(parents=True, exist_ok=True)
    fx = Fixture(root=root, ledger_path=root / "ledger.sqlite3")
    with fx.ledger() as ledger:
        ledger.initialize()
    (root / "ground_truth").mkdir()
    (root / "ground_truth" / f"{OPAQUE_CUT}.json").write_text(
        json.dumps({"label": "rbqm_holdout9", "faults": [{"fault_type": "SECRETFAULT"}]})
    )
    base = fx.append(DataCut(cut_id="DC-BASE", study_id=STUDY, path="curated/MRD-201/DC-BASE"))
    fx.append(DataCut(cut_id=OPAQUE_CUT, study_id=STUDY, path=f"curated/MRD-201/{OPAQUE_CUT}"))
    fx.append(
        ActorWoke(
            agent_id="rbqm.kri_monitor",
            agent_version="2.2.0",
            trigger_seq=base.seq,
            executor="openshell",
        ),
        actor="rbqm.kri_monitor",
    )
    fx.raise_finding(
        blinded_finding(
            "1016",
            "Site 1016: more protocol deviations than expected",
            "Site 1016 has 18 protocol deviations; the rest of the study implies 5.1.",
        )
    )
    fx.raise_finding(
        blinded_finding(
            "1004", "Site 1004: data entry lag above threshold", "Median entry lag is 14.0 days."
        ),
        check="data_entry_lag",
    )
    fx.append(
        ActorSuspended(
            agent_id="rbqm.kri_monitor",
            agent_version="2.2.0",
            trigger_seq=base.seq,
            executor="openshell",
            image_digest=IMAGE_DIGEST,
            policy_sha256=POLICY_SHA,
            inputs_manifest_sha256=MANIFEST_SHA,
        ),
        actor="rbqm.kri_monitor",
    )
    fx.append(
        FindingNarrative(
            finding_id="RBQM-0001",
            finding_seq=4,
            agent_id="rbqm.narrator",
            agent_version="1.1.0",
            status="accepted",
            headline="Site 1016: 18 protocol deviations against 5.1 expected",
            explanation="Site 1016 recorded 18 observed (5.1 expected).",
            model="fake",
            request_sha256="d" * 64,
            response_sha256="e" * 64,
            executor="openshell",
            sandbox="mer-nr-00000000",
        ),
        actor="rbqm.narrator",
    )
    if restricted:
        raised = fx.raise_finding(
            restricted_finding(
                "SECRETTITLE arm imbalance at site 1003",
                "SECRETSUMMARY: treatment arm SECRETARM shows more events.",
                "SECRETDISPLAY 3 of 4",
            ),
            check="efficacy",
        )
        fx.append(
            FindingNarrative(
                finding_id="DSMB-0001",
                finding_seq=raised.seq,
                agent_id="rbqm.narrator",
                agent_version="1.1.0",
                status="accepted",
                headline="SECRETHEADLINE",
                explanation="SECRETEXPLANATION for Site 1003.",
                model="fake",
                request_sha256="f" * 64,
            ),
            Visibility.RESTRICTED,
            "rbqm.narrator",
        )
    fx.append(
        ContainmentCheckCompleted(
            zone="rbqm",
            sandbox="mer-cf-rb-00000000",
            image="meridian-agent:0.1.0",
            image_digest=IMAGE_DIGEST,
            policy_sha256=POLICY_SHA,
            checks=(),
            log_line_found=True,
            all_refused=True,
            passed=True,
        ),
        actor="system.conformance",
    )
    return fx
