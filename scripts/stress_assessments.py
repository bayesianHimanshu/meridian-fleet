"""The assessment safety.term_adjudicator gives the stress cases of the reconciliation set.

For each stress case (tests/fixtures/sae_stress*/NN_*.json) this runs
safety.sae_reconciler (the current version) on the case's records in memory, raises its
findings into a scratch ledger (never var/ledger.sqlite3), and runs one adjudicator pass
in the safety.local_model sandbox with the configured model. It prints the findings, the
candidates and each assessment.

    uv run python scripts/stress_assessments.py tests/fixtures/sae_stress_holdout/02_*.json
"""

import argparse
import json
import tempfile
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import polars as pl
import structlog

from meridian.agents.base import AgentContext
from meridian.agents.safety import sae_reconciler
from meridian.agents.safety import term_adjudicator as ta
from meridian.agents.safety.adjudication import AdjudicatorRunner
from meridian.domain import safety as s
from meridian.domain.enums import Visibility
from meridian.domain.events import DataCut, Event, EventDraft, FindingAssessment, FindingRaised
from meridian.domain.findings import Finding
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import FixedClock
from meridian.platform.config import DEFAULT_FINDING_ID_PREFIXES, Settings
from meridian.platform.container import Container

STUDY = "MRD-201"
CUT = "DC-STRESS"
NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)
AE_CRITERIA = ("AESDTH", "AESLIFE", "AESHOSP", "AESDISAB", "AESCONG")
CASE_CRITERIA = ("SDTH", "SLIFE", "SHOSP", "SDISAB", "SCONG")


def _frame(rows: list[dict[str, Any]], defaults: dict[str, Any]) -> pl.DataFrame:
    filled = [{**defaults, **r} for r in rows]
    columns = list(dict.fromkeys(k for r in filled for k in r))
    return pl.DataFrame([{c: r.get(c) for c in columns} for r in filled])


def tables(spec: dict[str, Any]) -> dict[str, pl.DataFrame]:
    """The case's records, filled in as tests/sae_stress_support.py fills them."""
    ae = _frame(spec["ae"], {"STUDYID": STUDY, "DOMAIN": "AE", **dict.fromkeys(AE_CRITERIA, "N")})
    cases = _frame(spec["safety_cases"], dict.fromkeys(CASE_CRITERIA, "N"))
    return {"dm": pl.DataFrame(spec["dm"]), "ae": ae, s.SAFETY_CASES: cases}


class Reader:
    study_id = STUDY
    cut_id = CUT

    def __init__(self, t: dict[str, pl.DataFrame]) -> None:
        self._t = t

    def table(self, name: str) -> pl.DataFrame:
        return self._t[name]

    def tables(self) -> list[str]:
        return sorted(self._t)


def _trigger() -> Event:
    return Event(
        seq=1,
        event_id=uuid.UUID("01890000-0000-7000-8000-000000000001"),
        occurred_at=NOW,
        recorded_at=NOW,
        actor="system.ingest",
        study_id=STUDY,
        visibility=Visibility.BLINDED,
        payload=DataCut(cut_id=CUT, study_id=STUDY, path=f"curated/{STUDY}/{CUT}"),
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def lookup(t: dict[str, pl.DataFrame]) -> ta.TermLookup:
    def clinical(ref: ta.ClinicalRef) -> ta.Term | None:
        rows = (
            t["ae"]
            .filter((pl.col("USUBJID") == ref.usubjid) & (pl.col("AESEQ") == ref.aeseq))
            .to_dicts()
        )
        return ta.Term(str(rows[0]["AEDECOD"]), str(rows[0]["AETERM"])) if rows else None

    def case(ref: ta.CaseRef) -> ta.Term | None:
        rows = t[s.SAFETY_CASES].filter(pl.col(s.CASEID) == ref.caseid).to_dicts()
        if not rows:
            return None
        return ta.Term(str(rows[-1][s.PREFERRED_TERM]), str(rows[-1][s.REPORTED_TERM]))

    return ta.TermLookup(clinical, case)


def run_case(path: Path, c: Container, log: structlog.typing.FilteringBoundLogger) -> None:
    spec = json.loads(path.read_text(encoding="utf-8"))
    t = tables(spec)
    cfg = c.settings.agents.sae_reconciler
    ctx = AgentContext(study=Reader(t), clock=FixedClock(NOW), config=cfg, log=log)
    drafts = list(sae_reconciler.SaeReconciler().run(_trigger(), ctx))
    print(f"== {path.name} ({spec['case']}), reconciler {sae_reconciler.VERSION}")
    with tempfile.TemporaryDirectory(prefix="meridian-stress-") as tmp:
        ledger = SqliteLedger(
            path=Path(tmp) / "scratch.sqlite3",
            clock=FixedClock(NOW),
            finding_id_prefixes=dict(DEFAULT_FINDING_ID_PREFIXES),
        )
        ledger.initialize()
        raised = []
        for d in drafts:
            finding = Finding(
                kind=d.kind,
                agent_id=sae_reconciler.AGENT_ID,
                agent_version=sae_reconciler.VERSION,
                study_id=STUDY,
                site_id=d.site_id,
                severity=d.severity,
                title=d.title,
                summary=d.summary,
                evidence=d.evidence,
                visibility=Visibility.BLINDED,
                visible_to=frozenset(d.visible_to or ()),
                score=d.score,
            )
            event = ledger.append(
                EventDraft(
                    occurred_at=NOW,
                    actor=sae_reconciler.AGENT_ID,
                    study_id=STUDY,
                    visibility=Visibility.BLINDED,
                    payload=FindingRaised(finding=finding, dedup_key=d.dedup_key),
                )
            )
            raised.append(event)
            assert isinstance(event.payload, FindingRaised)
            print(f"  {event.payload.finding.finding_id} {ta.check_of(event)}: {d.title}")
        candidates = ta.candidates(raised, cfg.fallback_window_days)
        if not candidates:
            print("  no candidate: no assessment")
            ledger.close()
            return
        runner = AdjudicatorRunner(
            ledger=ledger,
            executor=c.local_model_executor(log),
            clock=FixedClock(NOW),
            settings=c.local_model_settings(),
            lookup=lookup(t),
            window_days=cfg.fallback_window_days,
            log=log,
        )
        for cand in candidates:
            clinical, case = lookup(t).clinical(cand.clinical), lookup(t).case(cand.case)
            print(f"  candidate {cand.kind} {'+'.join(cand.finding_ids)}: {clinical} / {case}")
        report = runner.run_pass()
        for event in report.events:
            a = event.payload
            assert isinstance(a, FindingAssessment)
            detail = (
                f"{a.verdict}: {a.reason}"
                if a.status == "accepted"
                else ",".join(a.rejection_reasons) or a.error
            )
            print(
                f"  assessment {'+'.join(a.finding_ids)} {a.status} {detail} "
                f"(model {a.model}, digest {(a.model_digest or '')[:12]}, "
                f"{a.latency_ms} ms, sandbox {a.sandbox})"
            )
        ledger.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("cases", nargs="+", type=Path)
    args = parser.parse_args()
    with Container.build(Settings()) as c:
        log: structlog.typing.FilteringBoundLogger = structlog.get_logger()
        for path in args.cases:
            run_case(path, c, log)


if __name__ == "__main__":
    main()
