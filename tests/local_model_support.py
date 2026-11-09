"""Reconciler findings in a ledger, and the term lookup over the same records, for the
local model agents' tests."""

import copy
import json
from typing import Any

import polars as pl

from meridian.agents.safety import sae_reconciler
from meridian.agents.safety import term_adjudicator as ta
from meridian.domain import safety as s
from meridian.domain.enums import Visibility
from meridian.domain.events import Event, EventDraft, FindingRaised
from meridian.domain.findings import Finding
from meridian.ledger.store import SqliteLedger
from meridian.llm.local import FakeLocalModelClient, LocalModelError
from meridian.platform.config import LocalModelSettings
from tests.sae_stress_support import DEV, load, run, tables
from tests.support import T0

MODEL = "granite4:3b"
DIGEST = "a" * 64
SETTINGS = LocalModelSettings(model=MODEL, digests={MODEL: DIGEST}, max_calls_per_pass=10)
RECODED = load(DEV / "01_recoded_term.json")


def unpaired_spec(days: int = 3) -> dict[str, Any]:
    """The recoded term, with the case's onset `days` later: beyond term_mismatch_days, so
    the reconciler raises a missing case and an orphan case instead."""
    spec = copy.deepcopy(RECODED)
    spec["safety_cases"][0]["ONSET"] = f"2025-03-{14 + days:02d}"
    return spec


def raise_spec(ledger: SqliteLedger, spec: dict[str, Any]) -> list[Event]:
    """Run safety.sae_reconciler on the spec and raise every draft as a finding."""
    out = []
    for d in run(spec):
        finding = Finding(
            kind=d.kind,
            agent_id=sae_reconciler.AGENT_ID,
            agent_version=sae_reconciler.VERSION,
            study_id="MRD-201",
            site_id=d.site_id,
            severity=d.severity,
            title=d.title,
            summary=d.summary,
            evidence=d.evidence,
            visibility=Visibility.BLINDED,
            visible_to=frozenset(d.visible_to or ()),
            score=d.score,
        )
        out.append(
            ledger.append(
                EventDraft(
                    occurred_at=T0,
                    actor=sae_reconciler.AGENT_ID,
                    study_id="MRD-201",
                    visibility=Visibility.BLINDED,
                    payload=FindingRaised(finding=finding, dedup_key=d.dedup_key),
                )
            )
        )
    return out


def spec_lookup(spec: dict[str, Any]) -> ta.TermLookup:
    t = tables(spec)

    def clinical(ref: ta.ClinicalRef) -> ta.Term | None:
        rows = (
            t["ae"]
            .filter((pl.col("USUBJID") == ref.usubjid) & (pl.col("AESEQ") == ref.aeseq))
            .to_dicts()
        )
        return ta.Term(rows[0]["AEDECOD"], rows[0]["AETERM"]) if rows else None

    def case(ref: ta.CaseRef) -> ta.Term | None:
        rows = t["safety_cases"].filter(pl.col(s.CASEID) == ref.caseid).to_dicts()
        return ta.Term(rows[-1][s.PREFERRED_TERM], rows[-1][s.REPORTED_TERM]) if rows else None

    return ta.TermLookup(clinical, case)


def answer(verdict: str = "yes", reason: str = "Both name the same allergic reaction.") -> str:
    return json.dumps({"same_event": verdict, "reason": reason})


def fake(
    *script: str | LocalModelError, digest: str = DIGEST, thinking: str = ""
) -> FakeLocalModelClient:
    return FakeLocalModelClient(script=script, digests={MODEL: digest}, thinking=thinking)
