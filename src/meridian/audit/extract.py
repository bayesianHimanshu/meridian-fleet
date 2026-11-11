"""The audit extract: the host's export of the ledger for quality.audit_trail_reviewer.

One row per ledger event from from_seq through through_seq (meridian.domain.audit has the
columns). The host reads the ledger with full access and decides itself what leaves:

* A restricted event, and any event about a restricted finding, keeps only seq, hashes,
  type and time (REDACTED = "Y").
* No evidence, narrative, title, summary, assessment text, observation text or payload
  beyond the listed identifiers.
* No subject id: a finding's check (which may name a subject) is replaced by FINDING_GROUP,
  and any subject id a person typed into a decision reason is masked as "[subject]".

The extract is written once per review under <audit_root>/<trigger seq>/ and staged as
/stage/audit; the inputs manifest of the run hashes it.
"""

import re
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

import polars as pl

from meridian.data.cut_writer import write_parquet_deterministic
from meridian.domain import audit as a
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import Visibility
from meridian.domain.events import (
    ActorSuspended,
    ActorWoke,
    AuditReviewRequested,
    DataCut,
    Event,
    FindingAssessment,
    FindingNarrative,
    FindingRaised,
    HumanDecision,
    LedgerEntry,
    QualityObservation,
    RedactedEvent,
)
from meridian.ledger.hashing import format_utc

SUBJECT = re.compile(r"\b\d{3}-\d{4}-\d{3}\b")
SUBJECT_MASK = "[subject]"
EXTRACT_FILE = f"{a.AUDIT_EXTRACT}.parquet"


def mask_subjects(text: str) -> str:
    return SUBJECT.sub(SUBJECT_MASK, text)


def _empty(seq: int, e: LedgerEntry) -> dict[str, Any]:
    row: dict[str, Any] = dict.fromkeys(a.COLUMNS)
    row[a.SEQ] = seq
    row[a.HASH] = e.hash
    row[a.PREV_HASH] = e.prev_hash
    row[a.TYPE] = e.event_type.value
    row[a.RECORDED_AT] = format_utc(e.recorded_at)
    row[a.REDACTED] = "N"
    return row


def _about(e: Event) -> tuple[str, ...]:
    p = e.payload
    if isinstance(p, FindingRaised):
        return () if p.finding.finding_id is None else (p.finding.finding_id,)
    if isinstance(p, FindingNarrative):
        return (p.finding_id,)
    if isinstance(p, FindingAssessment):
        return p.finding_ids
    if isinstance(p, HumanDecision):
        return (p.decision.finding_id,)
    if isinstance(p, QualityObservation) and p.refers_to_finding_id:
        return (p.refers_to_finding_id,)
    return ()


def extract_rows(
    entries: Sequence[LedgerEntry],
    *,
    from_seq: int,
    through_seq: int,
    operational_cuts: Collection[str],
) -> list[dict[str, Any]]:
    """The extract of seq from_seq through through_seq. entries: the ledger read with full
    access (restricted events unredacted), in seq order."""
    operational = set(operational_cuts)
    restricted_findings = {
        e.payload.finding.finding_id
        for e in entries
        if isinstance(e, Event)
        and isinstance(e.payload, FindingRaised)
        and e.payload.finding.visibility is Visibility.RESTRICTED
        and e.payload.finding.finding_id is not None
    }
    trigger_cut: dict[int, str] = {}
    groups: dict[tuple[str, ...], int] = {}
    rows: list[dict[str, Any]] = []

    def scope(cut: str | None) -> str | None:
        if cut is None:
            return None
        return a.OPERATIONAL if cut in operational or cut == a.LEDGER_SCOPE else a.EVALUATION

    for e in entries:
        if isinstance(e, Event):
            p = e.payload
            if isinstance(p, DataCut):
                trigger_cut[e.seq] = p.cut_id
            elif isinstance(p, AuditReviewRequested):
                trigger_cut[e.seq] = a.LEDGER_SCOPE
        if not from_seq <= e.seq <= through_seq:
            continue
        row = _empty(e.seq, e)
        if (
            isinstance(e, RedactedEvent)
            or e.visibility is Visibility.RESTRICTED
            or any(f in restricted_findings for f in _about(e))
        ):
            row[a.REDACTED] = "Y"
            rows.append(row)
            continue
        p = e.payload
        row[a.ACTOR] = e.actor
        if isinstance(p, DataCut):
            row[a.CUT_ID] = p.cut_id
            row[a.CUT_SCOPE] = scope(p.cut_id)
        elif isinstance(p, ActorWoke | ActorSuspended):
            cut = trigger_cut.get(p.trigger_seq)
            row |= {
                a.AGENT: p.agent_id,
                a.VERSION: p.agent_version,
                a.TRIGGER_SEQ: p.trigger_seq,
                a.CUT_ID: cut,
                a.CUT_SCOPE: scope(cut),
                a.EXECUTOR: p.executor,
                a.RUN_STATUS: a.STARTED if isinstance(p, ActorWoke) else a.COMPLETED,
            }
            if isinstance(p, ActorWoke) and p.started_at is not None:
                row[a.STARTED_AT] = format_utc(p.started_at)
            if isinstance(p, ActorSuspended):
                row |= {
                    a.IMAGE_DIGEST: p.image_digest,
                    a.POLICY_SHA256: p.policy_sha256,
                    a.POLICY_FILE_SHA256: p.policy_file_sha256,
                    a.TEMPLATE_SHA256: None if p.substrate is None else p.substrate.template_sha256,
                    a.GOLDEN_SNAPSHOT: None if p.substrate is None else p.substrate.golden_snapshot,
                }
        elif isinstance(p, FindingRaised):
            parts = parse_dedup_key(p.dedup_key)
            identity = (
                parts.agent_id,
                parts.kind,
                parts.study_id,
                parts.site,
                parts.cut_id,
                parts.check,
            )
            group = groups.setdefault(identity, len(groups) + 1)
            row |= {
                a.FINDING_ID: p.finding.finding_id,
                a.FINDING_GROUP: group,
                a.CUT_ID: parts.cut_id,
                a.CUT_SCOPE: scope(parts.cut_id),
                a.AGENT: p.finding.agent_id,
                a.VERSION: p.finding.agent_version,
            }
        elif isinstance(p, HumanDecision):
            d = p.decision
            row |= {
                a.ACTOR: d.actor,
                a.ROLE: d.role.value,
                a.FINDING_ID: d.finding_id,
                a.DECISION_ACTION: d.action.value,
                a.DECISION_REASON: None if d.reason is None else mask_subjects(d.reason),
            }
        elif isinstance(p, FindingNarrative | FindingAssessment):
            row |= {a.FINDING_ID: _about(e)[0], a.AGENT: p.agent_id, a.VERSION: p.agent_version}
        elif isinstance(p, QualityObservation):
            row |= {a.ACTOR: p.raised_by, a.FINDING_ID: p.refers_to_finding_id}
        rows.append(row)
    return rows


def frame(rows: Sequence[dict[str, Any]]) -> pl.DataFrame:
    schema = {c: (pl.Int64 if c in a.INT_COLUMNS else pl.String) for c in a.COLUMNS}
    return pl.DataFrame(list(rows), schema=schema, orient="row").sort(a.SEQ, maintain_order=True)


def write_extract(rows: Sequence[dict[str, Any]], directory: Path) -> Path:
    """Write the extract as <directory>/audit_extract.parquet (once; never rewritten)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / EXTRACT_FILE
    if path.exists():
        raise FileExistsError(f"{path} exists; an audit extract is never rewritten")
    write_parquet_deterministic(frame(rows), path)
    return path
