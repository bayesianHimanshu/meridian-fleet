"""The host's side of the two phase 5 step 2 agents' inputs: nothing here runs in a sandbox.

* **NOAEL extract.** preclinical.dose_translation wakes on a NoaelDecision and needs the
  study's decisions up to it; agents never read the ledger, so the host writes them, with
  the run's context (the study and its SEND cut), to <noael_root>/<trigger seq>/noael as
  parquet tables. That folder is staged as /stage/noael next to the SEND study. Since step
  3a it also holds, for preclinical.translational_bridge (which also wakes on the
  acceptance of a dose proposal), the findings the current decisions cite and the study's
  current dose proposal.
* **Inventory cuts.** The committed inventory CSV is written as a parquet table to
  <inventory_root>/<asset>/<cut>/inventory/studies.parquet, the cut named after the CSV's
  sha256 (a changed inventory is a new cut), and an InventoryLoaded event recorded once:
  preclinical.ind_readiness's trigger.
"""

import hashlib
import os
import shutil
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import polars as pl

from meridian.data.stage_layout import INVENTORY, NOAEL
from meridian.domain.enums import EventType, FindingKind, Visibility
from meridian.domain.events import (
    INVENTORY_CUT_PREFIX,
    Event,
    EventDraft,
    FindingRaised,
    HumanDecision,
    InventoryLoaded,
    NoaelDecision,
)
from meridian.ledger.store import LedgerReader, LedgerWriter
from meridian.preclinical.noael_stage import (
    BRIDGE_FINDINGS,
    BRIDGE_PROPOSAL,
    CONTEXT_TABLE,
    NOAEL_TABLE,
    noael_dir,
)
from meridian.preclinical.readiness import INVENTORY_TABLE

_DECISION_SCHEMA = {
    "seq": pl.Int64,
    "decision_id": pl.String,
    "study_id": pl.String,
    "sex": pl.String,
    "established": pl.Boolean,
    "noael_dose": pl.Float64,
    "loael_dose": pl.Float64,
    "dose_unit": pl.String,
    "decided_by": pl.String,
    "supersedes": pl.String,
    "recorded_at": pl.String,
}


def trigger_study(events: Sequence[Event], trigger: Event) -> str:
    """The SEND study a preclinical trigger is about: a NoaelDecision's own, or for a
    HumanDecision (step 3a) the study of the finding it decides."""
    p = trigger.payload
    if isinstance(p, NoaelDecision):
        return p.study_id
    if isinstance(p, HumanDecision):
        for e in events:
            q = e.payload
            if isinstance(q, FindingRaised) and q.finding.finding_id == p.decision.finding_id:
                return q.finding.study_id
        raise ValueError(f"no finding {p.decision.finding_id} for trigger {trigger.seq}")
    raise ValueError(f"trigger {trigger.seq} ({p.type}) has no SEND study")


def noael_extract(events: Sequence[Event], trigger: Event) -> pl.DataFrame:
    """The study's NOAEL decisions up to and including the trigger, in seq order. Only the
    fields the agent uses: no rationale, no finding ids."""
    study = trigger_study(events, trigger)
    rows = []
    for e in sorted(events, key=lambda e: e.seq):
        p = e.payload
        if isinstance(p, NoaelDecision) and p.study_id == study and e.seq <= trigger.seq:
            rows.append(
                {
                    "seq": e.seq,
                    "decision_id": p.decision_id,
                    "study_id": p.study_id,
                    "sex": p.sex,
                    "established": p.established,
                    "noael_dose": p.noael_dose,
                    "loael_dose": p.loael_dose,
                    "dose_unit": p.dose_unit,
                    "decided_by": p.decided_by,
                    "supersedes": p.supersedes or "",
                    "recorded_at": e.recorded_at.isoformat(),
                }
            )
    return pl.DataFrame(rows, schema=_DECISION_SCHEMA)


_FINDING_SCHEMA = {
    "finding_id": pl.String,
    "check": pl.String,
    "title": pl.String,
    "cited_by": pl.String,
}
_PROPOSAL_SCHEMA = {
    "finding_id": pl.String,
    "seq": pl.Int64,
    "check": pl.String,
    "status": pl.String,
    "decided_by": pl.String,
    "starting_dose_mg": pl.Float64,
    "mrsd_mg_per_kg": pl.Float64,
    "human_weight_kg": pl.Float64,
    "basis_decision_id": pl.String,
}


def _current_ids(decisions: pl.DataFrame) -> list[str]:
    superseded = {s for s in decisions["supersedes"].to_list() if s}
    latest: dict[str, str] = {}
    for row in decisions.sort("seq").iter_rows(named=True):
        if row["decision_id"] not in superseded:
            latest[row["sex"]] = row["decision_id"]
    return list(latest.values())


def _basis_of(p: FindingRaised) -> str:
    """The NOAEL decision a dose proposal rests on (its primary DECISION= selector)."""
    return next(
        (
            part.removeprefix("DECISION=")
            for ev in p.finding.evidence
            if ev.role == "primary"
            for part in ev.selector.split("; ")
            if part.startswith("DECISION=")
        ),
        "",
    )


def bridge_extract(
    events: Sequence[Event], trigger: Event, decisions: pl.DataFrame
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """(the findings the current decisions cite, the study's current dose proposal) as the
    ledger stands at the trigger: titles and checks only, no evidence."""
    study = trigger_study(events, trigger)
    before = [e for e in sorted(events, key=lambda e: e.seq) if e.seq <= trigger.seq]
    current = set(_current_ids(decisions))
    cited: dict[str, list[str]] = {}
    for e in before:
        p = e.payload
        if isinstance(p, NoaelDecision) and p.decision_id in current:
            for fid in p.finding_ids:
                cited.setdefault(fid, []).append(p.decision_id)
    raised = {
        e.payload.finding.finding_id: e
        for e in before
        if isinstance(e.payload, FindingRaised) and e.payload.finding.finding_id is not None
    }
    rows = []
    for fid in sorted(cited):
        found = raised.get(fid)
        if found is None or not isinstance(found.payload, FindingRaised):
            continue
        rows.append(
            {
                "finding_id": fid,
                "check": found.payload.dedup_key.split("|")[-1],
                "title": found.payload.finding.title,
                "cited_by": ",".join(sorted(cited[fid])),
            }
        )
    findings = pl.DataFrame(rows, schema=_FINDING_SCHEMA)
    # The proposal: the study's newest whose basis is a current decision, as the ledger
    # stands when this extract is written (it comes after the decision it rests on, so a
    # NOAEL decision's own trigger cannot hold it; a stale proposal from a revised NOAEL
    # is never used). Its status is the latest decision on it by then.
    everything = sorted(events, key=lambda e: e.seq)
    proposals = [
        e
        for e in everything
        if isinstance(e.payload, FindingRaised)
        and e.payload.finding.kind is FindingKind.DOSE_PROPOSAL
        and e.payload.finding.study_id == study
        and _basis_of(e.payload) in current
    ]
    prop_rows = []
    if proposals:
        newest = max(proposals, key=lambda e: e.seq)
        assert isinstance(newest.payload, FindingRaised)
        f = newest.payload.finding
        decided = [
            e.payload.decision
            for e in everything
            if isinstance(e.payload, HumanDecision)
            and e.payload.decision.finding_id == f.finding_id
        ]
        values = {ev.statistic: ev for ev in f.evidence}

        def value(stat: str) -> float | None:
            ev = values.get(stat)
            return None if ev is None else ev.value

        start = values.get("mrsd:starting_dose_mg")
        basis = _basis_of(newest.payload)
        prop_rows.append(
            {
                "finding_id": f.finding_id,
                "seq": newest.seq,
                "check": newest.payload.dedup_key.split("|")[-1].split(":", 1)[0],
                "status": decided[-1].action.value if decided else "open",
                "decided_by": decided[-1].actor if decided else "",
                "starting_dose_mg": value("mrsd:starting_dose_mg"),
                "mrsd_mg_per_kg": value("mrsd:mrsd_mg_per_kg"),
                "human_weight_kg": None if start is None else start.threshold,
                "basis_decision_id": basis,
            }
        )
    return findings, pl.DataFrame(prop_rows, schema=_PROPOSAL_SCHEMA)


def write_noael_extract(
    events: Sequence[Event], trigger: Event, send_cut: str, noael_root: Path
) -> Path:
    """Write <noael_root>/<seq>/noael/: the decisions, the context, and (step 3a) the
    bridge's findings and proposal tables, replacing any earlier copy for the same
    trigger. events: the ledger's events (at least the NOAEL decisions; with the findings
    and decisions for the bridge's tables). Returns <noael_root>/<seq>."""
    study = trigger_study(events, trigger)
    target = noael_dir(noael_root, trigger.seq)
    area = target / NOAEL
    if area.exists():
        shutil.rmtree(area)
    area.mkdir(parents=True)
    decisions = noael_extract(events, trigger)
    decisions.write_parquet(area / f"{NOAEL_TABLE}.parquet")
    pl.DataFrame(
        {"study_id": [study], "send_cut_id": [send_cut], "trigger_seq": [trigger.seq]}
    ).write_parquet(area / f"{CONTEXT_TABLE}.parquet")
    findings, proposal = bridge_extract(events, trigger, decisions)
    findings.write_parquet(area / f"{BRIDGE_FINDINGS}.parquet")
    proposal.write_parquet(area / f"{BRIDGE_PROPOSAL}.parquet")
    return target


def inventory_cut_id(csv_bytes: bytes) -> str:
    return INVENTORY_CUT_PREFIX + hashlib.sha256(csv_bytes).hexdigest()[:8]


def inventory_dir(inventory_root: Path, asset_id: str, cut_id: str) -> Path:
    return inventory_root / asset_id / cut_id


def write_inventory(csv_path: Path, inventory_root: Path, asset_id: str) -> tuple[str, Path, str]:
    """Write the CSV as <inventory_root>/<asset>/<cut>/inventory/studies.parquet (every
    column a string, as written). Returns (cut id, cut dir, sha256 of the CSV). The same
    CSV gives the same cut; an existing cut is left as it is."""
    data = csv_path.read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    cut = inventory_cut_id(data)
    target = inventory_dir(inventory_root, asset_id, cut)
    if (target / INVENTORY / f"{INVENTORY_TABLE}.parquet").exists():
        return cut, target, sha
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=target.parent, prefix=".tmp-"))
    try:
        (tmp / INVENTORY).mkdir()
        frame = pl.read_csv(csv_path, infer_schema=False)
        frame.write_parquet(tmp / INVENTORY / f"{INVENTORY_TABLE}.parquet")
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)
    return cut, target, sha


def inventory_events(reader: LedgerReader, asset_id: str | None = None) -> list[Event]:
    out = []
    for e in reader.read(event_types=[EventType.INVENTORY_LOADED]):
        if (
            isinstance(e, Event)
            and isinstance(e.payload, InventoryLoaded)
            and (asset_id is None or e.payload.asset_id == asset_id)
        ):
            out.append(e)
    return out


def record_inventory(
    ledger: LedgerReader,
    writer: LedgerWriter,
    *,
    occurred_at: datetime,
    actor: str,
    asset_id: str,
    cut_id: str,
    path: Path,
    source_sha256: str,
) -> tuple[Event, bool]:
    """Append InventoryLoaded once per (asset, cut). Returns (event, appended)."""
    for e in inventory_events(ledger, asset_id):
        assert isinstance(e.payload, InventoryLoaded)
        if e.payload.cut_id == cut_id:
            return e, False
    event = writer.append(
        EventDraft(
            occurred_at=occurred_at,
            actor=actor,
            study_id=asset_id,
            visibility=Visibility.BLINDED,
            payload=InventoryLoaded(
                asset_id=asset_id,
                cut_id=cut_id,
                path=path.as_posix(),
                source_sha256=source_sha256,
            ),
        )
    )
    return event, True
