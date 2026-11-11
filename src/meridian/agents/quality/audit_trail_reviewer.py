"""quality.audit_trail_reviewer 1.1.0: a review of the audit trail itself. Deterministic.

Input: the audit extract of the ledger from from_seq through through_seq (the trigger's,
an AuditReviewRequested), staged as /stage/audit under policies/quality.audit_trail.yaml.
It holds identifiers and hashes only (meridian.domain.audit): no evidence, narratives or
subject ids, and restricted events reduced to seq, hashes, type and time.

The checks (docs/audit-notes.md), one finding per occurrence, kind audit:

* audit_chain_linkage (high): every prev_hash equals the previous row's hash, the first
  row of a review from seq 1 links to the genesis hash, seq has no gap, and the extract
  reaches through_seq. Linkage only: restricted payloads are not exported, so hashes are
  not recomputed here; full verification stays on the host (`meridian ledger verify`).
* audit_unsandboxed_operational_run (high): a run on an operational cut whose executor is
  not a sandboxed one (openshell or, since 1.1.0, substrate), or whose completion lacks
  the image digest and, for openshell, the applied policy hash or, for substrate, the
  template spec hash and the golden snapshot id.
* audit_failed_run_unresolved (medium): a run on an operational cut that never completed
  (an ActorWoke with no ActorSuspended for the same agent, version and trigger before that
  key's next ActorWoke), with no later completed run of the same agent on the same cut.
  A run started less than in_progress_seconds before the review may still be running.
* audit_rapid_decisions (medium): at least rapid_decisions decisions by one actor within
  rapid_window_minutes. A signal of review without reading, not proof.
* audit_reason_quality (low): a dismissal whose reason is shorter than min_reason_chars or
  a placeholder ("ok", "n/a", ...); and, once per actor and reason, a dismissal reason
  identical to at least reuse_others other dismissal reasons of the same actor.
* audit_decision_on_retired (medium): a decision recorded after its finding was superseded
  (a newer version of the agent raised the same thing: same FINDING_GROUP) or retired (a
  newer version completed a run on the finding's cut without raising it).
* audit_data_quality (medium): a malformed row. The row is left out of the checks that
  need the bad field; the review never fails.

A finding about a person's decisions names that person (actor_under_review): they may not
decide it.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone, make_dedup_key
from meridian.data.study_store import MissingTableError
from meridian.domain import audit as a
from meridian.domain.enums import DecisionAction, EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import AuditReviewRequested, Event
from meridian.domain.findings import Evidence
from meridian.ledger.hashing import GENESIS_HASH
from meridian.platform.agent_settings import AuditTrailReviewerConfig

AGENT_ID = "quality.audit_trail_reviewer"
VERSION = "1.2.0"  # 1.1.0: substrate is a sandboxed executor (phase 4a step 2)
# 1.2.0: a run's start is ActorWoke's STARTED_AT when present (phase 4a step 3)
VISIBLE_TO_MAX = frozenset({Role.CLINICAL_OPS_LEAD, Role.QA_AUDITOR})
SEVERITY: dict[str, Severity] = {
    a.CHAIN_LINKAGE: Severity.HIGH,
    a.UNSANDBOXED_OPERATIONAL_RUN: Severity.HIGH,
    a.FAILED_RUN_UNRESOLVED: Severity.MEDIUM,
    a.RAPID_DECISIONS: Severity.MEDIUM,
    a.REASON_QUALITY: Severity.LOW,
    a.DECISION_ON_RETIRED: Severity.MEDIUM,
    a.DATA_QUALITY: Severity.MEDIUM,
}
SCORE = {Severity.HIGH: 1.0, Severity.MEDIUM: 0.5, Severity.LOW: 0.2}
OPENSHELL = "openshell"
SUBSTRATE = "substrate"
# What a sandboxed run's completion must record, by executor.
SANDBOX_RECORD: dict[str, tuple[tuple[str, str], ...]] = {
    OPENSHELL: ((a.IMAGE_DIGEST, "image digest"), (a.POLICY_SHA256, "applied policy hash")),
    SUBSTRATE: (
        (a.IMAGE_DIGEST, "image digest"),
        (a.TEMPLATE_SHA256, "template spec hash"),
        (a.GOLDEN_SNAPSHOT, "golden snapshot id"),
    ),
}
SOURCE = a.AUDIT_EXTRACT
LINKAGE_NOTE = (
    "Linkage only: the extract carries no payloads of restricted events, so hashes are not "
    "recomputed here; full hash verification stays on the host (meridian ledger verify)."
)
_HEX64 = re.compile(r"[0-9a-f]{64}")
_TYPES = frozenset(t.value for t in EventType)
_ACTIONS = frozenset(d.value for d in DecisionAction)
_PUNCT = re.compile(r"[^\w/ ]+")


def _str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _version(v: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(x) for x in v.split("."))
    except ValueError:
        return None


def _time(text: str) -> datetime | None:
    try:
        t = datetime.fromisoformat(text)
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def _clock(t: datetime) -> str:
    return f"{t.day} {t:%b %Y}, {t:%H:%M:%S} UTC"


# ---- rows


@dataclass(frozen=True, slots=True)
class Row:
    seq: int
    hash: str
    prev_hash: str
    type: str
    at: datetime | None
    redacted: bool
    values: dict[str, Any]

    def get(self, column: str) -> str:
        return _str(self.values.get(column))

    @property
    def started(self) -> datetime | None:
        """When a run started: STARTED_AT, which the parallel runner records because it
        appends ActorWoke after the run; RECORDED_AT on older events, appended at the start."""
        return _time(self.get(a.STARTED_AT)) or self.at

    def int(self, column: str) -> int | None:
        v = self.values.get(column)
        return v if isinstance(v, int) and not isinstance(v, bool) and v > 0 else None


@dataclass(frozen=True, slots=True)
class Problem:
    record: str
    field: str
    value: str


@dataclass(slots=True)
class Parsed:
    rows: list[Row] = field(default_factory=list)
    problems: list[Problem] = field(default_factory=list)
    # rows whose type-specific fields are usable (the rest are left out of those checks)
    usable: set[int] = field(default_factory=set)


_NEEDS: dict[str, tuple[str, ...]] = {
    # VERSION may be empty on a run: phase 1 events predate versions in run identity.
    EventType.ACTOR_WOKE.value: (a.AGENT, a.TRIGGER_SEQ),
    EventType.ACTOR_SUSPENDED.value: (a.AGENT, a.TRIGGER_SEQ),
    EventType.FINDING_RAISED.value: (a.FINDING_ID, a.FINDING_GROUP, a.AGENT, a.VERSION),
    EventType.HUMAN_DECISION.value: (a.ACTOR, a.FINDING_ID, a.DECISION_ACTION),
}


def parse(table: pl.DataFrame) -> Parsed:
    out = Parsed()
    if table.height == 0 or a.SEQ not in table.columns:
        if table.height:
            out.problems.append(Problem("extract", a.SEQ, "no SEQ column"))
        return out
    ordered = table.sort(a.SEQ, nulls_last=True, maintain_order=True)
    seen: set[int] = set()
    for n, r in enumerate(ordered.iter_rows(named=True), start=1):
        seq = r.get(a.SEQ)
        if not isinstance(seq, int) or seq < 1:
            out.problems.append(Problem(f"row-{n}", a.SEQ, _str(seq)))
            continue
        if seq in seen:
            out.problems.append(Problem(str(seq), a.SEQ, f"{seq} (duplicate)"))
            continue
        seen.add(seq)
        bad: list[Problem] = []
        h, prev = _str(r.get(a.HASH)), _str(r.get(a.PREV_HASH))
        for col, value in ((a.HASH, h), (a.PREV_HASH, prev)):
            if not _HEX64.fullmatch(value):
                bad.append(Problem(str(seq), col, value))
        typ = _str(r.get(a.TYPE))
        if typ not in _TYPES:
            bad.append(Problem(str(seq), a.TYPE, typ))
        at = _time(_str(r.get(a.RECORDED_AT)))
        if at is None:
            bad.append(Problem(str(seq), a.RECORDED_AT, _str(r.get(a.RECORDED_AT))))
        redacted_text = _str(r.get(a.REDACTED)) or "N"
        if redacted_text not in ("Y", "N"):
            bad.append(Problem(str(seq), a.REDACTED, redacted_text))
        redacted = redacted_text == "Y"
        row = Row(seq, h, prev, typ, at, redacted, dict(r))
        usable = not redacted and typ in _TYPES
        for col in _NEEDS.get(typ, ()) if usable else ():
            ok = row.int(col) is not None if col in a.INT_COLUMNS else bool(row.get(col))
            if not ok:
                bad.append(Problem(str(seq), col, row.get(col)))
                usable = False
        if (
            typ == EventType.HUMAN_DECISION.value
            and usable
            and row.get(a.DECISION_ACTION) not in _ACTIONS
        ):
            bad.append(Problem(str(seq), a.DECISION_ACTION, row.get(a.DECISION_ACTION)))
            usable = False
        out.problems += bad
        out.rows.append(row)
        if usable:
            out.usable.add(seq)
    return out


# ---- the checks


@dataclass(frozen=True, slots=True)
class Hit:
    check: str
    record: str
    title: str
    summary: str
    evidence: tuple[Evidence, ...]
    actor: str | None = None


def _ev(
    selector: str,
    statistic: str,
    display: str,
    *,
    value: float = 0.0,
    role: str = "primary",
    threshold: float | None = None,
    direction: str = "none",
) -> Evidence:
    return Evidence(
        source=SOURCE,
        selector=selector,
        statistic=statistic,
        value=value,
        threshold=threshold,
        role=role,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        display=display,
    )


def chain(p: Parsed, from_seq: int, through_seq: int) -> list[Hit]:
    hits: list[Hit] = []
    note = _ev("audit extract", "chain:scope", LINKAGE_NOTE, role="supporting")
    rows = p.rows
    expected = from_seq
    for i, r in enumerate(rows):
        if r.seq != expected:
            hits.append(
                Hit(
                    a.CHAIN_LINKAGE,
                    f"gap-{expected}-{r.seq}",
                    f"Audit trail: seq {expected} to {r.seq - 1} missing",
                    f"The extract goes from seq {expected - 1} to seq {r.seq}: "
                    f"{r.seq - expected} event(s) missing in between.",
                    (
                        _ev(
                            f"SEQ={r.seq}",
                            "chain:gap",
                            f"expected seq {expected}, found {r.seq}",
                            value=float(r.seq - expected),
                        ),
                        note,
                    ),
                )
            )
        expected = r.seq + 1
        before = rows[i - 1].hash if i > 0 else (GENESIS_HASH if r.seq == 1 else None)
        if (
            before is not None
            and _HEX64.fullmatch(r.prev_hash)
            and _HEX64.fullmatch(before)
            and r.prev_hash != before
        ):
            what = "the genesis hash" if i == 0 else f"the hash of seq {rows[i - 1].seq}"
            hits.append(
                Hit(
                    a.CHAIN_LINKAGE,
                    f"link-{r.seq}",
                    f"Audit trail: seq {r.seq} does not link to the event before it",
                    f"The prev_hash of seq {r.seq} is not {what}.",
                    (
                        _ev(f"SEQ={r.seq}", "chain:prev_hash", r.prev_hash[:12] + "..."),
                        _ev(
                            f"SEQ={rows[i - 1].seq if i else 0}",
                            "chain:hash_before",
                            before[:12] + "...",
                            role="supporting",
                        ),
                        note,
                    ),
                )
            )
    if rows and rows[-1].seq < through_seq:
        last = rows[-1].seq
        hits.append(
            Hit(
                a.CHAIN_LINKAGE,
                f"end-{last}",
                f"Audit trail: the extract stops at seq {last}",
                f"The review covers seq {from_seq} to {through_seq}, but the extract ends at "
                f"seq {last}.",
                (
                    _ev(
                        f"SEQ={last}",
                        "chain:end",
                        f"ends at {last}, review through {through_seq}",
                        value=float(through_seq - last),
                    ),
                    note,
                ),
            )
        )
    return hits


@dataclass(slots=True)
class Attempt:
    woke: Row | None
    done: Row | None


def _runs(rows: Sequence[Row]) -> dict[tuple[str, str, int], list[Attempt]]:
    """Per (agent, version, trigger): its attempts in order. A completion closes the open
    attempt of its key; one with no open attempt is a run of its own."""
    out: dict[tuple[str, str, int], list[Attempt]] = defaultdict(list)
    for r in rows:
        trig = r.int(a.TRIGGER_SEQ)
        assert trig is not None
        key = (r.get(a.AGENT), r.get(a.VERSION), trig)
        if r.type == EventType.ACTOR_WOKE.value:
            out[key].append(Attempt(r, None))
        else:
            attempts = out[key]
            if attempts and attempts[-1].done is None:
                attempts[-1].done = r
            else:
                attempts.append(Attempt(None, r))
    return out


def runs(p: Parsed, cfg: AuditTrailReviewerConfig, now: datetime) -> list[Hit]:
    run_rows = [
        r
        for r in p.rows
        if r.seq in p.usable
        and r.type in (EventType.ACTOR_WOKE.value, EventType.ACTOR_SUSPENDED.value)
    ]
    hits: list[Hit] = []
    by_key = _runs(run_rows)
    for (agent, version, trig), attempts in sorted(by_key.items()):
        any_row = attempts[0].woke or attempts[0].done
        assert any_row is not None
        if any_row.get(a.CUT_SCOPE) != a.OPERATIONAL:
            continue
        cut = any_row.get(a.CUT_ID) or "?"
        record = f"{agent}/{version}/{trig}"
        problems = []
        for at in attempts:
            if at.woke is not None and at.woke.get(a.EXECUTOR) not in SANDBOX_RECORD:
                problems.append(f"started with executor {at.woke.get(a.EXECUTOR) or 'none'}")
            d = at.done
            if d is not None:
                executor = d.get(a.EXECUTOR)
                if executor not in SANDBOX_RECORD:
                    problems.append(f"completed with executor {executor or 'none'}")
                for column, label in SANDBOX_RECORD.get(executor or "", SANDBOX_RECORD[OPENSHELL]):
                    if not d.get(column):
                        problems.append(f"no {label}")
                if (
                    at.woke is not None
                    and at.woke.get(a.EXECUTOR) in SANDBOX_RECORD
                    and executor in SANDBOX_RECORD
                    and at.woke.get(a.EXECUTOR) != executor
                ):
                    problems.append(
                        f"started in {at.woke.get(a.EXECUTOR)}, completed in {executor}"
                    )
        if problems:
            first = attempts[0].woke or attempts[0].done
            assert first is not None
            what = "; ".join(dict.fromkeys(problems))
            hits.append(
                Hit(
                    a.UNSANDBOXED_OPERATIONAL_RUN,
                    record,
                    f"{agent} {version} ran on operational cut {cut} without the sandbox record",
                    f"The run of {agent} {version} on {cut} (trigger seq {trig}): {what}. "
                    "Operational cuts may only be processed in an OpenShell sandbox (with the "
                    "image and the applied policy recorded) or a Substrate actor (with the "
                    "image, the template spec and the golden snapshot recorded).",
                    (
                        _ev(
                            f"SEQ={first.seq}; AGENT={agent}; TRIGGER_SEQ={trig}",
                            "run:sandbox",
                            what,
                        ),
                        _ev(
                            f"CUT_ID={cut}",
                            "run:cut_scope",
                            f"{cut} is operational",
                            role="supporting",
                        ),
                    ),
                )
            )
        completed_after = [
            at.done.seq
            for key, ats in by_key.items()
            if key[0] == agent and key[2] == trig
            for at in ats
            if at.done is not None
        ]
        for at in attempts:
            w = at.woke
            if w is None or at.done is not None:
                continue
            start = w.started
            if start is not None and now - start < timedelta(seconds=cfg.in_progress_seconds):
                continue  # may still be running
            if any(s > w.seq for s in completed_after):
                continue
            hits.append(
                Hit(
                    a.FAILED_RUN_UNRESOLVED,
                    f"{record}/{w.seq}",
                    f"{agent} {version} failed on operational cut {cut} and never completed",
                    f"The run that started at seq {w.seq}"
                    + (f" ({_clock(start)})" if start else "")
                    + f" has no completion, and no later run of {agent} completed on {cut}.",
                    (
                        _ev(
                            f"SEQ={w.seq}; AGENT={agent}; TRIGGER_SEQ={trig}",
                            "run:started",
                            _clock(start) if start else f"seq {w.seq}",
                            value=float(w.seq),
                        ),
                        _ev(f"CUT_ID={cut}", "run:later_completed", "none", role="supporting"),
                    ),
                )
            )
    return hits


def _decisions(p: Parsed) -> list[Row]:
    return [r for r in p.rows if r.seq in p.usable and r.type == EventType.HUMAN_DECISION.value]


def rapid(p: Parsed, cfg: AuditTrailReviewerConfig) -> list[Hit]:
    hits: list[Hit] = []
    window = timedelta(minutes=cfg.rapid_window_minutes)
    by_actor: dict[str, list[Row]] = defaultdict(list)
    for r in _decisions(p):
        if r.at is not None:
            by_actor[r.get(a.ACTOR)].append(r)
    for actor in sorted(by_actor):
        ds = sorted(by_actor[actor], key=lambda r: (r.at, r.seq))
        i = 0
        while i < len(ds):
            j = i
            while j + 1 < len(ds) and ds[j + 1].at - ds[i].at <= window:  # type: ignore[operator]
                j += 1
            n = j - i + 1
            if n >= cfg.rapid_decisions:
                first, last = ds[i], ds[j]
                assert first.at is not None
                assert last.at is not None
                span = (last.at - first.at).total_seconds() / 60
                hits.append(
                    Hit(
                        a.RAPID_DECISIONS,
                        f"{actor}/{first.seq}",
                        f"{actor} recorded {n} decisions in {span:.1f} minutes",
                        f"{actor} recorded {n} decisions between {_clock(first.at)} and "
                        f"{_clock(last.at)} (seq {first.seq} to {last.seq}); the threshold is "
                        f"{cfg.rapid_decisions} within {cfg.rapid_window_minutes:g} minutes. A "
                        "signal that findings were closed without being read, not proof.",
                        (
                            _ev(
                                f"ACTOR={actor}; SEQ={first.seq}-{last.seq}",
                                "decisions:count",
                                f"{n} decisions in {span:.1f} minutes",
                                value=float(n),
                                threshold=float(cfg.rapid_decisions),
                                direction="high",
                            ),
                            _ev(
                                f"ACTOR={actor}",
                                "decisions:window",
                                f"threshold {cfg.rapid_decisions} in "
                                f"{cfg.rapid_window_minutes:g} minutes",
                                role="supporting",
                            ),
                        ),
                        actor=actor,
                    )
                )
                i = j + 1
            else:
                i += 1
    return hits


def _norm(reason: str) -> str:
    return " ".join(_PUNCT.sub(" ", reason.lower()).split())


def reasons(p: Parsed, cfg: AuditTrailReviewerConfig) -> list[Hit]:
    hits: list[Hit] = []
    placeholders = {_norm(x) for x in cfg.placeholder_reasons} | set(cfg.placeholder_reasons)
    groups: dict[tuple[str, str], list[Row]] = defaultdict(list)
    for r in _decisions(p):
        if r.get(a.DECISION_ACTION) != DecisionAction.DISMISSED.value:
            continue
        actor, reason = r.get(a.ACTOR), r.get(a.DECISION_REASON)
        norm = _norm(reason)
        problem = None
        if reason.lower() in placeholders or norm in placeholders:
            problem = "a placeholder"
        elif len(reason) < cfg.min_reason_chars:
            problem = f"shorter than {cfg.min_reason_chars} characters"
        if problem:
            hits.append(
                Hit(
                    a.REASON_QUALITY,
                    str(r.seq),
                    f"Dismissal by {actor} gives no usable reason",
                    f"The dismissal of {r.get(a.FINDING_ID)} at seq {r.seq} has the reason "
                    f'"{reason}", which is {problem}: a reviewer cannot tell why it was closed.',
                    (
                        _ev(
                            f"SEQ={r.seq}; FINDING_ID={r.get(a.FINDING_ID)}",
                            "reason:text",
                            f'"{reason}" ({len(reason)} characters)',
                            value=float(len(reason)),
                            threshold=float(cfg.min_reason_chars),
                            direction="low",
                        ),
                    ),
                    actor=actor,
                )
            )
        else:
            groups[(actor, norm)].append(r)
    for (actor, _), rows in sorted(groups.items(), key=lambda kv: kv[1][0].seq):
        if len(rows) >= cfg.reuse_others + 1:
            first = rows[0]
            seqs = ", ".join(str(r.seq) for r in rows)
            hits.append(
                Hit(
                    a.REASON_QUALITY,
                    f"{actor}/reused/{first.seq}",
                    f"{actor} gave the same dismissal reason {len(rows)} times",
                    f"{len(rows)} dismissals by {actor} (seq {seqs}) share one reason: "
                    f'"{first.get(a.DECISION_REASON)}". A reason copied across findings may not '
                    "say why each one was closed.",
                    (
                        _ev(
                            f"ACTOR={actor}; SEQ={seqs}",
                            "reason:reused",
                            f"{len(rows)} dismissals with one reason",
                            value=float(len(rows)),
                            threshold=float(cfg.reuse_others + 1),
                            direction="high",
                        ),
                    ),
                    actor=actor,
                )
            )
    return hits


def retired(p: Parsed) -> list[Hit]:
    """Decisions after their finding was superseded or retired, as the ledger defines them
    (meridian.api.repository): superseded when a newer version of the agent raised the same
    finding group; retired when a newer version completed a run on its cut without it."""
    raised = [r for r in p.rows if r.seq in p.usable and r.type == EventType.FINDING_RAISED.value]
    by_id = {r.get(a.FINDING_ID): r for r in raised}
    raised_groups: set[tuple[str, str, int]] = set()  # (agent, version, group)
    for r in raised:
        g = r.int(a.FINDING_GROUP)
        assert g is not None
        raised_groups.add((r.get(a.AGENT), r.get(a.VERSION), g))
    completed = [
        r for r in p.rows if r.seq in p.usable and r.type == EventType.ACTOR_SUSPENDED.value
    ]
    hits: list[Hit] = []
    for d in _decisions(p):
        f = by_id.get(d.get(a.FINDING_ID))
        if f is None:
            continue
        own = _version(f.get(a.VERSION))
        group = f.int(a.FINDING_GROUP)
        if own is None or group is None:
            continue
        agent, cut = f.get(a.AGENT), f.get(a.CUT_ID)
        events: list[tuple[int, str]] = []
        for r in raised:
            v = _version(r.get(a.VERSION))
            if r.get(a.AGENT) == agent and r.int(a.FINDING_GROUP) == group and v and v > own:
                events.append((r.seq, f"superseded by {r.get(a.FINDING_ID)} ({r.get(a.VERSION)})"))
        for r in completed:
            v = _version(r.get(a.VERSION))
            if (
                r.get(a.AGENT) == agent
                and r.get(a.CUT_ID) == cut
                and v
                and v > own
                and (agent, r.get(a.VERSION), group) not in raised_groups
            ):
                events.append((r.seq, f"retired by {agent} {r.get(a.VERSION)} on {cut}"))
        before = sorted(e for e in events if e[0] < d.seq)
        if not before:
            continue
        seq, why = before[0]
        actor = d.get(a.ACTOR)
        hits.append(
            Hit(
                a.DECISION_ON_RETIRED,
                str(d.seq),
                f"{actor} decided {f.get(a.FINDING_ID)} after it was no longer current",
                f"The decision at seq {d.seq} ({d.get(a.DECISION_ACTION)}) is on "
                f"{f.get(a.FINDING_ID)}, which was {why} at seq {seq}. The current finding is "
                "the one to decide.",
                (
                    _ev(
                        f"SEQ={d.seq}; FINDING_ID={f.get(a.FINDING_ID)}",
                        "decision:finding_state",
                        why,
                        value=float(seq),
                    ),
                    _ev(
                        f"SEQ={seq}",
                        "decision:no_longer_current_at",
                        f"seq {seq}",
                        role="supporting",
                        value=float(seq),
                    ),
                ),
                actor=actor,
            )
        )
    return hits


def data_quality(p: Parsed) -> list[Hit]:
    hits = []
    seen: set[tuple[str, str]] = set()
    for q in p.problems:
        if (q.record, q.field) in seen:
            continue
        seen.add((q.record, q.field))
        what = "missing" if not q.value else f"not usable ({q.value[:40]})"
        hits.append(
            Hit(
                a.DATA_QUALITY,
                f"{q.record}#{q.field}",
                f"Audit extract: {q.field} of row {q.record} is {what.split(' (')[0]}",
                f"{q.field} of row {q.record} is {what}; the checks that need it skip the row. "
                "The rest of the extract is still reviewed.",
                (
                    _ev(f"SEQ={q.record}", "data_quality:field", q.field),
                    _ev(f"SEQ={q.record}", "data_quality:value", q.value[:40] or "empty"),
                ),
            )
        )
    return hits


def review(
    table: pl.DataFrame,
    from_seq: int,
    through_seq: int,
    cfg: AuditTrailReviewerConfig,
    now: datetime,
) -> list[Hit]:
    p = parse(table)
    return [
        *chain(p, from_seq, through_seq),
        *runs(p, cfg, now),
        *rapid(p, cfg),
        *reasons(p, cfg),
        *retired(p),
        *data_quality(p),
    ]


class AuditTrailReviewer:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.QUALITY
    subscribes_to = frozenset({EventType.AUDIT_REVIEW_REQUESTED})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = AuditTrailReviewerConfig

    def run(
        self, trigger: Event, ctx: AgentContext[AuditTrailReviewerConfig]
    ) -> Sequence[FindingDraft]:
        p = trigger.payload
        if not isinstance(p, AuditReviewRequested):
            raise ValueError(f"trigger is a {p.type}, not an audit review")
        try:
            table = ctx.study.table(a.AUDIT_EXTRACT)
        except MissingTableError:
            table = pl.DataFrame()
        hits = review(table, p.from_seq, p.through_seq, ctx.config, ctx.clock.now())
        drafts = [self._draft(h, ctx.config, ctx.study.study_id) for h in hits]
        ctx.log.info("audit.reviewed", rows=table.height, findings=len(drafts))
        return drafts

    def _draft(self, h: Hit, cfg: AuditTrailReviewerConfig, study_id: str) -> FindingDraft:
        severity = SEVERITY[h.check]
        return FindingDraft(
            dedup_key=make_dedup_key(
                agent_id=self.agent_id,
                agent_version=self.version,
                kind=FindingKind.AUDIT,
                study_id=study_id,
                site_id=None,
                cut_id=a.LEDGER_SCOPE,
                check=a.check_id(h.check, h.record),
            ),
            kind=FindingKind.AUDIT,
            site_id=None,
            severity=severity,
            title=h.title,
            summary=h.summary,
            evidence=h.evidence,
            score=SCORE[severity],
            visible_to=cfg.visible_to,
            actor_under_review=h.actor,
        )
