"""safety.sae_reconciler 1.1.1: serious adverse events in the clinical database (SDTM AE)
against the cases in the sponsor's safety database. Deterministic; no model.

Inputs: blinded AE (rows with AESER = Y), DM (subjects and sites) and the blinded case table
of the same cut. Nothing unblinded: the case table never holds the treatment column.

The rule (docs/safety-notes.md, "The matching rule, 1.1"):

0. Records. Every input is sorted on all of its columns first, so no result depends on the
   row order of an export. Safety rows are grouped by case id. The latest version (by
   receipt, then CASE_VERSION) is the case; earlier versions are history. Timeliness is
   measured on the initial version. A record with a missing or unparseable field it needs
   for matching (subject, sequence number, preferred term, onset) gets an sae_data_quality
   finding and is left out of matching. A field needed by one check only (awareness,
   receipt, an outcome, causality or criteria flag outside the terminology) gets an
   sae_data_quality finding, and that check is skipped for the record. One bad record never
   fails the cut.
1. Onsets are intervals: a full date is one day, YYYY-MM a month, YYYY a year. The gap
   between two onsets is the number of days between the intervals, 0 when they overlap.
2. Pairing, per subject and preferred term (upper case, whitespace collapsed): an optimal
   assignment of clinical SAEs to cases. Pairs are allowed when the gap is at most
   fallback_window_days. The total cost is minimised:
       cost(pair) = cost_per_day * gap + cost_outcome * [outcomes differ]
                    + cost_causality * [causalities differ]
       cost(unpaired record) = U = cost_per_day * window + cost_outcome + cost_causality + 1
   so pairing two records always beats leaving both unpaired, and within the window the cost,
   not the window, decides who pairs with whom. Ties go to the lexicographically smallest
   assignment (clinical records by AESEQ, cases by case id). Groups with more than
   MAX_EXACT_CASES cases fall back to greedy pairing in cost order.
3. What is left, in this order:
   a. Subject typo: a case whose subject is not enrolled, with exactly one enrolled subject
      at the same site within edit distance 1 (Damerau, so a transposition counts as one)
      that has an unpaired SAE with the same term within the window, gives
      sae_subject_mismatch (high). Two or more candidates give no guess.
   b. Recoded term: a subject with exactly one unpaired SAE and one unpaired case, with
      different terms and onsets at most term_mismatch_days apart, gives sae_term_mismatch
      (medium). Whether two terms mean the same event is not decided here: that is a
      candidate for the local model (step 3b).
   c. Probable duplicate: an unpaired case with another case of the same subject and term
      within the window gives sae_probable_duplicate (medium), not an orphan.
   d. A clinical SAE left over has no case (high); a case left over has no clinical SAE
      (high), and the evidence says whether the clinical AE exists but is non-serious.
4. Per pair: an onset gap above onset_tolerance_days is an onset mismatch. A pair that
   involves a partial onset and agrees within the tolerance gives an sae_partial_date note
   (low) instead. Outcome and causality are compared after mapping through
   config/safety_terms.toml; causality is compared by its binary class when either side is
   RELATED or NOT RELATED. The six seriousness criteria are compared flag by flag where both
   sides record them (seriousness_criteria_mismatch, medium).
5. Per case: received more than late_after_hours after awareness (initial version) is late.
   A site with at least site_late_min late cases gets one site-level finding.

Evidence names the clinical record by USUBJID and AESEQ, and the safety record by case id,
version and USUBJID, so every subject-level finding carries its subject in the ledger.

1.1.1 changes text and evidence only, never which findings are raised (MATCHING_VERSION):
a late report gives its delay as "126 hours (5.2 days)" in title and summary, and its
evidence names the paired clinical record (USUBJID, AESEQ) when there is one.
"""

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import cache
from typing import Any

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone, make_dedup_key
from meridian.agents.rbqm.common import cut_id_of
from meridian.domain import safety as s
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import SaeReconcilerConfig

AGENT_ID = "safety.sae_reconciler"
VERSION = "1.1.1"
# The version whose matching rule this version runs: which findings it raises, per record.
# A version that changes only text or evidence keeps it, and the stress sets' recorded
# outcomes (keyed by it) stay valid without another run of the sealed holdout.
MATCHING_VERSION = "1.1.0"
AE = "ae"
# The most any of its findings may reach; the config narrows it per finding.
VISIBLE_TO_MAX = frozenset(
    {Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR, Role.PORTFOLIO_LEAD}
)
SEVERITY: dict[str, Severity] = {
    s.SAE_MISSING: Severity.HIGH,
    s.CASE_WITHOUT_SAE: Severity.HIGH,
    s.LATE_REPORT: Severity.HIGH,
    s.LATE_SITE: Severity.HIGH,
    s.SUBJECT_MISMATCH: Severity.HIGH,
    s.ONSET_MISMATCH: Severity.MEDIUM,
    s.OUTCOME_MISMATCH: Severity.MEDIUM,
    s.CAUSALITY_MISMATCH: Severity.MEDIUM,
    s.DATA_QUALITY: Severity.MEDIUM,
    s.PROBABLE_DUPLICATE: Severity.MEDIUM,
    s.CRITERIA_MISMATCH: Severity.MEDIUM,
    s.TERM_MISMATCH: Severity.MEDIUM,
    s.PARTIAL_DATE: Severity.LOW,
}
SCORE = {Severity.HIGH: 1.0, Severity.MEDIUM: 0.5, Severity.LOW: 0.2}
# Exact assignment (dynamic programming over subsets) up to this many cases in one group.
MAX_EXACT_CASES = 12
_YEAR = re.compile(r"\d{4}")
_MONTH = re.compile(r"\d{4}-\d{2}")
_MIN_INSTANT = datetime.min.replace(tzinfo=UTC)


# ---- values


@dataclass(frozen=True, slots=True)
class Onset:
    """An onset as an interval of days: one day, a month (YYYY-MM) or a year (YYYY)."""

    lo: date
    hi: date
    text: str

    @property
    def partial(self) -> bool:
        return self.lo != self.hi


def parse_onset(value: object) -> Onset | None:
    """None when empty or unparseable."""
    text = _str(value).strip()
    try:
        if _YEAR.fullmatch(text):
            year = int(text)
            return Onset(date(year, 1, 1), date(year, 12, 31), text)
        if _MONTH.fullmatch(text):
            first = date(int(text[:4]), int(text[5:7]), 1)
            nxt = date(first.year + first.month // 12, first.month % 12 + 1, 1)
            return Onset(first, nxt - timedelta(days=1), text)
        if len(text) >= 10 and (len(text) == 10 or text[10] == "T"):
            day = date.fromisoformat(text[:10])
            return Onset(day, day, text)
    except ValueError:
        return None
    return None


def gap(a: Onset, b: Onset) -> int:
    """Days between two onset intervals; 0 when they overlap."""
    return max(0, (b.lo - a.hi).days, (a.lo - b.hi).days)


def parse_instant(value: object) -> datetime | None:
    """An ISO 8601 date-time; a value without an offset is read as UTC. None if unusable."""
    text = _str(value).strip()
    if len(text) < 16 or text[10] != "T":
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def edit_distance(a: str, b: str) -> int:
    """Optimal string alignment distance: insertions, deletions, substitutions and
    transpositions of adjacent characters each count one."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _str(value: object) -> str:
    return "" if value is None else str(value)


def _sorted_rows(table: pl.DataFrame) -> list[dict[str, Any]]:
    """Rows in an order that depends only on their content (every column, as text)."""
    if table.height == 0:
        return []
    keyed = table.with_columns(
        pl.concat_str(
            [pl.col(c).cast(pl.String).fill_null("") for c in table.columns], separator="\x1f"
        ).alias("__order")
    )
    columns = [*table.columns, "__order"]
    ordered = keyed.select(columns).sort(["__order"], maintain_order=True)
    return [{k: v for k, v in r.items() if k != "__order"} for r in ordered.iter_rows(named=True)]


# ---- records


@dataclass(frozen=True, slots=True)
class ClinicalSae:
    usubjid: str
    aeseq: int
    site: str
    term: str  # normalised preferred term
    onset: Onset
    outcome: str | None  # canonical; "" not recorded; None outside the terminology
    outcome_raw: str
    causality: str | None
    causality_raw: str
    criteria: tuple[tuple[str, str | None], ...]  # (case column, Y / N / None)

    @property
    def key(self) -> str:
        return s.clinical_key(self.usubjid, self.aeseq)


@dataclass(frozen=True, slots=True)
class Case:
    caseid: str
    version: int
    usubjid: str
    site: str
    term: str
    onset: Onset
    outcome: str | None
    outcome_raw: str
    causality: str | None
    causality_raw: str
    criteria: tuple[tuple[str, str | None], ...]
    aware: datetime | None  # initial version
    receipt: datetime | None  # initial version
    aware_text: str
    receipt_text: str

    @property
    def delay_hours(self) -> float | None:
        if self.aware is None or self.receipt is None:
            return None
        return (self.receipt - self.aware).total_seconds() / 3600


@dataclass(frozen=True, slots=True)
class Problem:
    """A data-quality problem: record key, field, raw value, and where it is."""

    record: str
    field: str
    value: str
    source: str
    selector: str
    site: str
    excluded: bool


@dataclass(frozen=True, slots=True)
class Pair:
    clinical: ClinicalSae
    case: Case


@dataclass(slots=True)
class Matching:
    pairs: list[Pair] = field(default_factory=list)
    subject_mismatches: list[Pair] = field(default_factory=list)
    term_mismatches: list[Pair] = field(default_factory=list)
    duplicates: list[tuple[Case, Case]] = field(default_factory=list)
    clinical_only: list[ClinicalSae] = field(default_factory=list)
    cases_only: list[Case] = field(default_factory=list)


def _flag(value: object) -> str | None:
    """Y, N, None for not recorded; "?" for anything else."""
    text = s.norm_text(_str(value))
    if text in ("Y", "N"):
        return text
    return None if not text else "?"


def clinical_saes(
    ae: pl.DataFrame, sites: dict[str, str], cfg: SaeReconcilerConfig
) -> tuple[list[ClinicalSae], list[Problem]]:
    if "AESER" not in ae.columns:  # an empty export has no columns: no serious AE
        return [], []
    serious = ae.filter(pl.col("AESER").cast(pl.String).str.strip_chars().str.to_uppercase() == "Y")
    out: list[ClinicalSae] = []
    problems: list[Problem] = []
    seen: set[tuple[str, int]] = set()
    for r in _sorted_rows(serious):
        u = _str(r.get("USUBJID")).strip()
        seq_text = _str(r.get("AESEQ")).strip()
        try:
            seq: int | None = int(float(seq_text))
        except ValueError:
            seq = None
        record = f"{u or '?'}/{seq if seq is not None else '?'}"
        site = sites.get(u, "")
        sel = f"USUBJID={u}; AESEQ={seq_text}; AESER=Y"

        def problem(
            col: str,
            excluded: bool,
            value: object = None,
            r: dict[str, Any] = r,
            record: str = record,
            sel: str = sel,
            site: str = site,
        ) -> None:
            raw = _str(r.get(col)) if value is None else _str(value)
            problems.append(Problem(record, col, raw, AE, sel, site, excluded))

        term = s.norm_text(_str(r.get("AEDECOD")))
        onset = parse_onset(r.get("AESTDTC"))
        bad = False
        for col, ok in (
            ("USUBJID", bool(u)),
            ("AESEQ", seq is not None),
            ("AEDECOD", bool(term)),
            ("AESTDTC", onset is not None),
        ):
            if not ok:
                problem(col, True)
                bad = True
        if not bad and seq is not None and (u, seq) in seen:
            problem("AESEQ", True, f"{seq_text} (duplicate)")
            bad = True
        if bad or seq is None or onset is None:
            continue
        seen.add((u, seq))
        outcome_raw, causality_raw = _str(r.get("AEOUT")), _str(r.get("AEREL"))
        outcome = cfg.terms.outcome_of(outcome_raw)
        causality = cfg.terms.causality_of(causality_raw)
        if outcome is None:
            problem("AEOUT", False)
        if causality is None:
            problem("AEREL", False)
        criteria: list[tuple[str, str | None]] = []
        for case_col, ae_col in s.CRITERIA.items():
            flag = _flag(r.get(ae_col))
            if flag == "?":
                problem(ae_col, False)
                flag = None
            criteria.append((case_col, flag))
        out.append(
            ClinicalSae(
                usubjid=u,
                aeseq=seq,
                site=site,
                term=term,
                onset=onset,
                outcome=outcome,
                outcome_raw=outcome_raw,
                causality=causality,
                causality_raw=causality_raw,
                criteria=tuple(criteria),
            )
        )
    return out, problems


def _version_of(row: dict[str, Any]) -> int | None:
    try:
        return int(float(_str(row.get(s.CASE_VERSION)).strip()))
    except ValueError:
        return None


def cases(table: pl.DataFrame, cfg: SaeReconcilerConfig) -> tuple[list[Case], list[Problem]]:
    """One Case per case id (its latest version), and the data-quality problems."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    problems: list[Problem] = []
    unidentified = 0
    for r in _sorted_rows(table):
        cid = _str(r.get(s.CASEID)).strip()
        if not cid:
            unidentified += 1
            u = _str(r.get(s.USUBJID)).strip()
            problems.append(
                Problem(
                    f"unidentified-{unidentified}",
                    s.CASEID,
                    "",
                    s.SAFETY_CASES,
                    f"CASEID=; USUBJID={u}",
                    _str(r.get(s.SITEID)).strip(),
                    True,
                )
            )
            continue
        groups[cid].append(r)
    out: list[Case] = []
    for cid in sorted(groups):
        rows = groups[cid]  # already in content order: the tie-break of the version order
        order = sorted(
            range(len(rows)),
            key=lambda i: (
                parse_instant(rows[i].get(s.RECEIPT_DTC)) or _MIN_INSTANT,
                _version_of(rows[i]) or 0,
                i,
            ),
        )
        initial, latest = rows[order[0]], rows[order[-1]]
        version = _version_of(latest) or len(order)
        u = _str(latest.get(s.USUBJID)).strip()
        site = _str(latest.get(s.SITEID)).strip()
        sel = f"CASEID={cid}; VERSION={version}; USUBJID={u}"
        found: set[str] = set()

        def problem(
            col: str,
            row: dict[str, Any],
            excluded: bool,
            cid: str = cid,
            sel: str = sel,
            site: str = site,
            found: set[str] = found,
        ) -> None:
            if col not in found:
                found.add(col)
                problems.append(
                    Problem(cid, col, _str(row.get(col)), s.SAFETY_CASES, sel, site, excluded)
                )

        term = s.norm_text(_str(latest.get(s.PREFERRED_TERM)))
        onset = parse_onset(latest.get(s.ONSET))
        bad = False
        for col, ok in (
            (s.USUBJID, bool(u)),
            (s.PREFERRED_TERM, bool(term)),
            (s.ONSET, onset is not None),
        ):
            if not ok:
                problem(col, latest, True)
                bad = True
        aware = parse_instant(initial.get(s.AWARE_DTC))
        if aware is None:
            problem(s.AWARE_DTC, initial, False)
        for row in (initial, *[rows[i] for i in order[1:]]):
            if parse_instant(row.get(s.RECEIPT_DTC)) is None:
                problem(s.RECEIPT_DTC, row, False)
        if bad or onset is None:
            continue
        outcome_raw = _str(latest.get(s.OUTCOME))
        causality_raw = _str(latest.get(s.CAUSALITY))
        outcome = cfg.terms.outcome_of(outcome_raw)
        causality = cfg.terms.causality_of(causality_raw)
        if outcome is None:
            problem(s.OUTCOME, latest, False)
        if causality is None:
            problem(s.CAUSALITY, latest, False)
        criteria: list[tuple[str, str | None]] = []
        for case_col in s.CRITERIA:
            flag = _flag(latest.get(case_col))
            if flag == "?":
                problem(case_col, latest, False)
                flag = None
            criteria.append((case_col, flag))
        out.append(
            Case(
                caseid=cid,
                version=version,
                usubjid=u,
                site=site,
                term=term,
                onset=onset,
                outcome=outcome,
                outcome_raw=outcome_raw,
                causality=causality,
                causality_raw=causality_raw,
                criteria=tuple(criteria),
                aware=aware,
                receipt=parse_instant(initial.get(s.RECEIPT_DTC)),
                aware_text=_str(initial.get(s.AWARE_DTC)),
                receipt_text=_str(initial.get(s.RECEIPT_DTC)),
            )
        )
    return out, problems


# ---- comparison and pairing


def outcome_differs(a: str | None, b: str | None) -> bool:
    return a is not None and b is not None and a != b


def causality_differs(a: str | None, b: str | None, cfg: SaeReconcilerConfig) -> bool:
    if a is None or b is None or a == b:
        return False
    if not a or not b:
        return True  # recorded against not recorded
    binary = {"RELATED", "NOT RELATED"}
    if a in binary or b in binary:
        return cfg.terms.causality_class(a) != cfg.terms.causality_class(b)
    return True


def criteria_differ(c: ClinicalSae, k: Case) -> list[tuple[str, str, str]]:
    """(case column, clinical flag, case flag) of each criterion recorded on both sides that
    differs."""
    theirs = dict(k.criteria)
    out = []
    for col, mine in c.criteria:
        other = theirs.get(col)
        if mine is not None and other is not None and mine != other:
            out.append((col, mine, other))
    return out


def pair_cost(c: ClinicalSae, k: Case, cfg: SaeReconcilerConfig) -> int:
    return (
        cfg.cost_per_day * gap(c.onset, k.onset)
        + cfg.cost_outcome * outcome_differs(c.outcome, k.outcome)
        + cfg.cost_causality * causality_differs(c.causality, k.causality, cfg)
    )


def assign(
    clinical: Sequence[ClinicalSae], found: Sequence[Case], cfg: SaeReconcilerConfig
) -> list[tuple[int, int]]:
    """The minimum-cost assignment within one group (see the module docstring), as
    (clinical index, case index) pairs."""
    window = cfg.fallback_window_days
    unpaired = cfg.cost_per_day * window + cfg.cost_outcome + cfg.cost_causality + 1
    costs: list[list[int | None]] = [
        [pair_cost(c, k, cfg) if gap(c.onset, k.onset) <= window else None for k in found]
        for c in clinical
    ]
    n, m = len(clinical), len(found)
    if m > MAX_EXACT_CASES:
        options = sorted(
            (cost, i, j)
            for i, row in enumerate(costs)
            for j, cost in enumerate(row)
            if cost is not None
        )
        used_i: set[int] = set()
        used_j: set[int] = set()
        greedy = []
        for _, i, j in options:
            if i not in used_i and j not in used_j:
                greedy.append((i, j))
                used_i.add(i)
                used_j.add(j)
        return sorted(greedy)

    @cache
    def best(i: int, mask: int) -> tuple[int, tuple[int, ...]]:
        if i == n:
            return unpaired * (m - mask.bit_count()), ()
        rest = best(i + 1, mask)
        choice = (unpaired + rest[0], (-1, *rest[1]))
        for j in range(m):
            cost = costs[i][j]
            if cost is not None and not mask >> j & 1:
                sub = best(i + 1, mask | 1 << j)
                choice = min(choice, (cost + sub[0], (j, *sub[1])))
        return choice

    _, picks = best(0, 0)
    return [(i, j) for i, j in enumerate(picks) if j >= 0]


def match(
    clinical: Sequence[ClinicalSae],
    found: Sequence[Case],
    enrolled: dict[str, str],
    cfg: SaeReconcilerConfig,
) -> Matching:
    """Pairing (2) and what is left (3a to 3d). enrolled maps subject to site."""
    m = Matching()
    by_group_c: dict[tuple[str, str], list[ClinicalSae]] = defaultdict(list)
    by_group_k: dict[tuple[str, str], list[Case]] = defaultdict(list)
    for c in sorted(clinical, key=lambda c: (c.usubjid, c.aeseq)):
        by_group_c[(c.usubjid, c.term)].append(c)
    for k in sorted(found, key=lambda k: k.caseid):
        by_group_k[(k.usubjid, k.term)].append(k)
    left_c: list[ClinicalSae] = []
    left_k: list[Case] = []
    for g in sorted(set(by_group_c) | set(by_group_k)):
        cs, ks = by_group_c.get(g, []), by_group_k.get(g, [])
        picked = assign(cs, ks, cfg)
        m.pairs.extend(Pair(cs[i], ks[j]) for i, j in picked)
        left_c.extend(c for i, c in enumerate(cs) if i not in {p[0] for p in picked})
        left_k.extend(k for j, k in enumerate(ks) if j not in {p[1] for p in picked})
    window = cfg.fallback_window_days

    # a. subject typo
    for k in sorted(left_k, key=lambda k: k.caseid):
        if k.usubjid in enrolled:
            continue
        options = [
            c
            for c in left_c
            if c.term == k.term
            and enrolled.get(c.usubjid) == k.site
            and edit_distance(c.usubjid, k.usubjid) == 1
            and gap(c.onset, k.onset) <= window
        ]
        if len({c.usubjid for c in options}) == 1:
            c = min(options, key=lambda c: (pair_cost(c, k, cfg), c.aeseq))
            m.subject_mismatches.append(Pair(c, k))
            left_c.remove(c)
            left_k.remove(k)

    # b. recoded term
    for u in sorted({c.usubjid for c in left_c}):
        mine_c = [c for c in left_c if c.usubjid == u]
        mine_k = [k for k in left_k if k.usubjid == u]
        if len(mine_c) == 1 and len(mine_k) == 1:
            c, k = mine_c[0], mine_k[0]
            if c.term != k.term and gap(c.onset, k.onset) <= cfg.term_mismatch_days:
                m.term_mismatches.append(Pair(c, k))
                left_c.remove(c)
                left_k.remove(k)

    # c. probable duplicates
    paired = {p.case.caseid for p in (*m.pairs, *m.subject_mismatches, *m.term_mismatches)}
    unpaired_ids = {k.caseid for k in left_k}
    seen: set[tuple[str, str]] = set()
    for k in sorted(left_k, key=lambda k: k.caseid):
        siblings = sorted(
            (
                (gap(k.onset, x.onset), x.caseid, x)
                for x in found
                if x.caseid != k.caseid
                and (x.usubjid, x.term) == (k.usubjid, k.term)
                and gap(k.onset, x.onset) <= window
            ),
            key=lambda t: (t[0], t[1]),
        )
        if not siblings:
            continue
        matched = [t for t in siblings if t[1] in paired]
        if not matched:
            # None of them paired: the first by case id stays an orphan, the rest are its
            # duplicates.
            cluster = sorted({k.caseid, *(t[1] for t in siblings)} & unpaired_ids)
            if k.caseid == cluster[0]:
                continue
        partner = (matched or siblings)[0][2]
        pair = tuple(sorted((k.caseid, partner.caseid)))
        if pair not in seen:
            seen.add((pair[0], pair[1]))
            first, second = (k, partner) if k.caseid < partner.caseid else (partner, k)
            m.duplicates.append((first, second))
        left_k.remove(k)

    m.clinical_only = sorted(left_c, key=lambda c: (c.usubjid, c.aeseq))
    m.cases_only = sorted(left_k, key=lambda k: k.caseid)
    m.pairs.sort(key=lambda p: (p.clinical.usubjid, p.clinical.aeseq))
    return m


def _delay(hours: float) -> str:
    """One form for a report delay, in titles, summaries and evidence: 126 hours (5.2 days)."""
    return f"{hours:.0f} hours ({hours / 24:.1f} days)"


def _ordinal(d: date | None) -> float:
    return float(d.toordinal()) if d is not None else 0.0


# ---- the agent


class SaeReconciler:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.SAFETY
    subscribes_to = frozenset({EventType.DATA_CUT})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = SaeReconcilerConfig

    def run(self, trigger: Event, ctx: AgentContext[SaeReconcilerConfig]) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        dm = study.table("dm")
        sites = {
            _str(u).strip(): _str(site).strip()
            for u, site in dm.select("USUBJID", "SITEID").sort(["USUBJID", "SITEID"]).iter_rows()
        }
        ae = study.table(AE)
        clinical, problems_c = clinical_saes(ae, sites, cfg)
        found, problems_k = cases(study.table(s.SAFETY_CASES), cfg)
        m = match(clinical, found, sites, cfg)
        out = _Findings(self, cfg, study.study_id, cut_id)

        reported: set[tuple[str, str]] = set()
        for problem in (*problems_c, *problems_k):
            if (problem.record, problem.field) not in reported:  # one per record and field
                reported.add((problem.record, problem.field))
                out.data_quality(problem)
        for c in m.clinical_only:
            out.missing_case(c)
        for k in m.cases_only:
            out.orphan_case(k, ae)
        for a, b in m.duplicates:
            out.duplicate(a, b)
        for p in m.subject_mismatches:
            out.subject_mismatch(p)
        for p in m.term_mismatches:
            out.term_mismatch(p)
        for p in m.pairs:
            c, k = p.clinical, p.case
            g = gap(c.onset, k.onset)
            if (c.onset.partial or k.onset.partial) and g <= cfg.onset_tolerance_days:
                out.partial_date(p, g)
            elif g > cfg.onset_tolerance_days:
                out.onset(p, g)
            if outcome_differs(c.outcome, k.outcome):
                out.field(p, s.OUTCOME_MISMATCH)
            if causality_differs(c.causality, k.causality, cfg):
                out.field(p, s.CAUSALITY_MISMATCH)
            diffs = criteria_differ(c, k)
            if diffs:
                out.criteria(p, diffs)
        late_by_site: dict[str, list[Case]] = defaultdict(list)
        paired = {
            p.case.caseid: p.clinical for p in (*m.pairs, *m.subject_mismatches, *m.term_mismatches)
        }
        for k in found:
            delay = k.delay_hours
            if delay is not None and delay > cfg.late_after_hours:
                out.late(k, delay, paired.get(k.caseid))
                late_by_site[k.site].append(k)
        for site in sorted(late_by_site):
            site_cases = [k for k in found if k.site == site]
            if len(late_by_site[site]) >= cfg.site_late_min:
                out.late_site(site, late_by_site[site], site_cases)
        ctx.log.info(
            "safety.reconciled",
            clinical_saes=len(clinical),
            cases=len(found),
            pairs=len(m.pairs),
            data_quality=len(problems_c) + len(problems_k),
            findings=len(out.drafts),
        )
        return out.drafts


class _Findings:
    """Builds one draft per discrepancy, with its evidence and its visibility."""

    def __init__(
        self, agent: SaeReconciler, cfg: SaeReconcilerConfig, study_id: str, cut_id: str
    ) -> None:
        self.agent = agent
        self.cfg = cfg
        self.study_id = study_id
        self.cut_id = cut_id
        self.drafts: list[FindingDraft] = []

    def _add(
        self,
        check: str,
        record: str,
        site: str,
        title: str,
        summary: str,
        evidence: list[Evidence],
        *,
        subject_level: bool = True,
    ) -> None:
        severity = SEVERITY[check]
        self.drafts.append(
            FindingDraft(
                dedup_key=make_dedup_key(
                    agent_id=self.agent.agent_id,
                    agent_version=self.agent.version,
                    kind=FindingKind.SAE_RECONCILIATION,
                    study_id=self.study_id,
                    site_id=site or None,
                    cut_id=self.cut_id,
                    check=s.check_id(check, record),
                ),
                kind=FindingKind.SAE_RECONCILIATION,
                site_id=site or None,
                severity=severity,
                title=title,
                summary=summary,
                evidence=tuple(evidence),
                score=SCORE[severity],
                visible_to=self.cfg.subject_visible_to
                if subject_level
                else self.cfg.site_visible_to,
            )
        )

    @staticmethod
    def _clinical_sel(c: ClinicalSae) -> str:
        return f"USUBJID={c.usubjid}; AESEQ={c.aeseq}; AESER=Y"

    @staticmethod
    def _case_sel(k: Case) -> str:
        return f"CASEID={k.caseid}; VERSION={k.version}; USUBJID={k.usubjid}"

    def _ev(
        self,
        source: str,
        selector: str,
        statistic: str,
        display: str,
        *,
        value: float = 0.0,
        role: str = "primary",
        direction: str = "none",
        threshold: float | None = None,
    ) -> Evidence:
        return Evidence(
            source=source,
            selector=selector,
            statistic=statistic,
            value=value,
            threshold=threshold,
            role=role,  # type: ignore[arg-type]
            direction=direction,  # type: ignore[arg-type]
            display=display,
        )

    def _onsets(self, c: ClinicalSae | None, k: Case | None, role: str) -> list[Evidence]:
        out: list[Evidence] = []
        if c is not None:
            out.append(
                self._ev(
                    AE,
                    self._clinical_sel(c),
                    "onset:clinical",
                    c.onset.text,
                    value=_ordinal(c.onset.lo),
                    role=role,
                )
            )
        if k is not None:
            out.append(
                self._ev(
                    s.SAFETY_CASES,
                    self._case_sel(k),
                    "onset:safety_db",
                    k.onset.text,
                    value=_ordinal(k.onset.lo),
                    role=role,
                )
            )
        return out

    def data_quality(self, p: Problem) -> None:
        what = "missing" if not p.value.strip() else f"not usable ({p.value.strip()})"
        effect = (
            "the record is left out of matching"
            if p.excluded
            else "the checks that need this field are skipped for the record"
        )
        where = "clinical record" if p.source == AE else "safety case"
        self._add(
            s.DATA_QUALITY,
            f"{p.record}#{p.field}",
            p.site,
            f"Data quality: {p.field} of {where} {p.record} is {what.split(' (')[0]}",
            f"{p.field} of {where} {p.record} is {what}; {effect}. Every other record of the "
            "cut is still reconciled.",
            [
                self._ev(p.source, p.selector, "data_quality:field", p.field),
                self._ev(p.source, p.selector, "data_quality:value", p.value or "empty"),
            ],
        )

    def missing_case(self, c: ClinicalSae) -> None:
        self._add(
            s.SAE_MISSING,
            c.key,
            c.site,
            f"Serious AE of {c.usubjid} has no case in the safety database",
            f"{c.term.capitalize()} (onset {c.onset.text}) is serious in the clinical "
            "database, and no safety database case matches it: none with the same subject "
            f"and preferred term and an onset within {self.cfg.fallback_window_days} days.",
            [
                self._ev(AE, self._clinical_sel(c), "preferred_term:clinical", c.term, value=1.0),
                self._ev(
                    s.SAFETY_CASES,
                    f"USUBJID={c.usubjid}; PREFERRED_TERM={c.term}",
                    "preferred_term:safety_db",
                    "no matching case",
                    threshold=float(self.cfg.fallback_window_days),
                ),
                *self._onsets(c, None, "supporting"),
            ],
        )

    def orphan_case(self, k: Case, ae: pl.DataFrame) -> None:
        near = [
            r
            for r in _sorted_rows(ae)
            if _str(r.get("USUBJID")).strip() == k.usubjid
            and s.norm_text(_str(r.get("AEDECOD"))) == k.term
            and (o := parse_onset(r.get("AESTDTC"))) is not None
            and gap(o, k.onset) <= self.cfg.fallback_window_days
        ]
        if near:
            r = min(near, key=lambda r: _str(r.get("AESEQ")))
            clinical_display = f"recorded non-serious (AESEQ {r['AESEQ']}, AESER = {r['AESER']})"
            selector = f"USUBJID={k.usubjid}; AESEQ={r['AESEQ']}"
            why = "the clinical database holds the event but records it as non-serious"
        else:
            clinical_display = "no matching adverse event"
            selector = f"USUBJID={k.usubjid}; AEDECOD={k.term}"
            why = "the clinical database holds no adverse event that matches it"
        self._add(
            s.CASE_WITHOUT_SAE,
            k.caseid,
            k.site,
            f"Safety case {k.caseid} has no serious AE in the clinical database",
            f"Case {k.caseid} ({k.term.capitalize()}, subject {k.usubjid}, onset "
            f"{k.onset.text}) matches no serious AE: {why}.",
            [
                self._ev(
                    s.SAFETY_CASES,
                    self._case_sel(k),
                    "seriousness:safety_db",
                    f"serious case, {k.term}",
                    value=1.0,
                ),
                self._ev(AE, selector, "seriousness:clinical", clinical_display),
                *self._onsets(None, k, "supporting"),
            ],
        )

    def duplicate(self, a: Case, b: Case) -> None:
        def show(k: Case) -> str:
            return f"{k.caseid} v{k.version}, onset {k.onset.text}, received {k.receipt_text}"

        self._add(
            s.PROBABLE_DUPLICATE,
            f"{a.caseid}+{b.caseid}",
            a.site or b.site,
            f"Safety cases {a.caseid} and {b.caseid} are probably one event",
            f"Subject {a.usubjid}, {a.term.capitalize()}: two cases with onsets "
            f"{a.onset.text} and {b.onset.text} ({gap(a.onset, b.onset)} days apart, within "
            f"{self.cfg.fallback_window_days}), for which the clinical database has fewer "
            "serious AEs. Probably a follow-up booked as a new case.",
            [
                self._ev(s.SAFETY_CASES, self._case_sel(a), "case:first", show(a)),
                self._ev(s.SAFETY_CASES, self._case_sel(b), "case:second", show(b)),
            ],
        )

    def subject_mismatch(self, p: Pair) -> None:
        c, k = p.clinical, p.case
        self._add(
            s.SUBJECT_MISMATCH,
            k.caseid,
            k.site,
            f"Safety case {k.caseid} names subject {k.usubjid}, probably {c.usubjid}",
            f"Subject {k.usubjid} of case {k.caseid} is not enrolled. {c.usubjid} at the same "
            f"site is one character away and has an unmatched serious AE "
            f"({c.term.capitalize()}, onset {c.onset.text}) like the case's "
            f"({k.term.capitalize()}, onset {k.onset.text}).",
            [
                self._ev(AE, self._clinical_sel(c), "subject:clinical", c.usubjid),
                self._ev(s.SAFETY_CASES, self._case_sel(k), "subject:safety_db", k.usubjid),
                *self._onsets(c, k, "supporting"),
            ],
        )

    def term_mismatch(self, p: Pair) -> None:
        c, k = p.clinical, p.case
        self._add(
            s.TERM_MISMATCH,
            c.key,
            c.site,
            f"Preferred term of a serious AE of {c.usubjid} differs from its probable case",
            f"The subject's only unmatched serious AE ({c.term}, onset {c.onset.text}) and only "
            f"unmatched case {k.caseid} ({k.term}, onset {k.onset.text}) are "
            f"{gap(c.onset, k.onset)} days apart with different preferred terms: probably one "
            "event coded differently (for example another MedDRA version). Not decided "
            "here whether the terms mean the same event.",
            [
                self._ev(AE, self._clinical_sel(c), "preferred_term:clinical", c.term),
                self._ev(s.SAFETY_CASES, self._case_sel(k), "preferred_term:safety_db", k.term),
                *self._onsets(c, k, "supporting"),
            ],
        )

    def partial_date(self, p: Pair, g: int) -> None:
        c, k = p.clinical, p.case
        side = "clinical" if c.onset.partial else "safety database"
        if c.onset.partial and k.onset.partial:
            side = "clinical and safety database"
        self._add(
            s.PARTIAL_DATE,
            c.key,
            c.site,
            f"Onset of a serious AE of {c.usubjid} is incomplete",
            f"{c.term.capitalize()}: onset {c.onset.text} in the clinical database and "
            f"{k.onset.text} in safety case {k.caseid}. The {side} onset is partial and "
            "agrees with the other; matched through it, not a mismatch. Ask for the full date.",
            [
                *self._onsets(c, k, "primary"),
                self._ev(
                    f"{AE}, {s.SAFETY_CASES}",
                    f"{self._clinical_sel(c)} | {self._case_sel(k)}",
                    "onset:interval_gap_days",
                    f"{g} days between the onsets",
                    value=float(g),
                    role="supporting",
                ),
            ],
        )

    def onset(self, p: Pair, g: int) -> None:
        c, k = p.clinical, p.case
        self._add(
            s.ONSET_MISMATCH,
            c.key,
            c.site,
            f"Onset of a serious AE of {c.usubjid} differs by {g} days",
            f"{c.term.capitalize()}: onset {c.onset.text} in the clinical database and "
            f"{k.onset.text} in safety case {k.caseid}, {g} days apart; more than "
            f"{self.cfg.onset_tolerance_days} day is a mismatch. Paired by the lowest total "
            f"cost within {self.cfg.fallback_window_days} days.",
            [
                *self._onsets(c, k, "primary"),
                self._ev(
                    f"{AE}, {s.SAFETY_CASES}",
                    f"{self._clinical_sel(c)} | {self._case_sel(k)}",
                    "onset:difference_days",
                    f"{g} days apart (tolerance {self.cfg.onset_tolerance_days} day)",
                    value=float(g),
                    threshold=float(self.cfg.onset_tolerance_days),
                    role="supporting",
                    direction="high",
                ),
            ],
        )

    def field(self, p: Pair, check: str) -> None:
        c, k = p.clinical, p.case
        is_outcome = check == s.OUTCOME_MISMATCH
        label = "outcome" if is_outcome else "causality"
        a = (c.outcome if is_outcome else c.causality) or ""
        b = (k.outcome if is_outcome else k.causality) or ""
        a_raw = c.outcome_raw if is_outcome else c.causality_raw
        b_raw = k.outcome_raw if is_outcome else k.causality_raw
        clinical_col, case_col = ("AEOUT", s.OUTCOME) if is_outcome else ("AEREL", s.CAUSALITY)

        def show(canonical: str, raw: str) -> str:
            if not canonical:
                return "not recorded"
            return canonical if s.norm_text(raw) == canonical else f"{canonical} ({raw.strip()})"

        self._add(
            check,
            c.key,
            c.site,
            f"{label.capitalize()} of a serious AE of {c.usubjid} differs from its case",
            f"{c.term.capitalize()} (onset {c.onset.text}): {label} {show(a, a_raw)} in the "
            f"clinical database ({clinical_col}), {show(b, b_raw)} in safety case {k.caseid} "
            f"({case_col}), compared through config/safety_terms.toml.",
            [
                self._ev(AE, self._clinical_sel(c), f"{label}:clinical", show(a, a_raw)),
                self._ev(s.SAFETY_CASES, self._case_sel(k), f"{label}:safety_db", show(b, b_raw)),
                *self._onsets(c, k, "supporting"),
            ],
        )

    def criteria(self, p: Pair, diffs: list[tuple[str, str, str]]) -> None:
        c, k = p.clinical, p.case
        mine = ", ".join(f"{s.CRITERIA[col]}={a}" for col, a, _ in diffs)
        theirs = ", ".join(f"{col}={b}" for col, _, b in diffs)
        self._add(
            s.CRITERIA_MISMATCH,
            c.key,
            c.site,
            f"Seriousness criteria of a serious AE of {c.usubjid} differ from its case",
            f"{c.term.capitalize()} (onset {c.onset.text}): {mine} in the clinical database, "
            f"{theirs} in safety case {k.caseid}.",
            [
                self._ev(
                    AE, self._clinical_sel(c), "criteria:clinical", mine, value=float(len(diffs))
                ),
                self._ev(
                    s.SAFETY_CASES,
                    self._case_sel(k),
                    "criteria:safety_db",
                    theirs,
                    value=float(len(diffs)),
                ),
            ],
        )

    def late(self, k: Case, hours: float, c: ClinicalSae | None = None) -> None:
        delay = _delay(hours)
        clinical = (
            [
                self._ev(
                    AE,
                    self._clinical_sel(c),
                    "clinical_record",
                    f"{c.usubjid} AESEQ {c.aeseq}, {c.term}, onset {c.onset.text}",
                    role="supporting",
                )
            ]
            if c is not None
            else []
        )
        self._add(
            s.LATE_REPORT,
            k.caseid,
            k.site,
            f"Safety case {k.caseid} reached the sponsor {delay} after awareness",
            f"Subject {k.usubjid}, {k.term.capitalize()}, case {k.caseid} (latest version "
            f"{k.version}): the site became aware at {k.aware_text} and the sponsor "
            f"received the initial report at {k.receipt_text}, {delay} later; the "
            f"expectation is within {self.cfg.late_after_hours:g} hours.",
            [
                self._ev(
                    s.SAFETY_CASES,
                    self._case_sel(k),
                    "report_delay_hours",
                    f"{delay} after awareness",
                    value=round(hours, 2),
                    threshold=self.cfg.late_after_hours,
                    direction="high",
                ),
                self._ev(
                    s.SAFETY_CASES,
                    self._case_sel(k),
                    "case",
                    f"case {k.caseid}, version {k.version}, subject {k.usubjid}",
                    role="supporting",
                ),
                *clinical,
                self._ev(
                    s.SAFETY_CASES,
                    self._case_sel(k),
                    "awareness:safety_db",
                    k.aware_text,
                    role="supporting",
                ),
                self._ev(
                    s.SAFETY_CASES,
                    self._case_sel(k),
                    "receipt:safety_db",
                    k.receipt_text,
                    role="supporting",
                ),
            ],
        )

    def late_site(self, site: str, late: list[Case], all_cases: list[Case]) -> None:
        hours = sorted(k.delay_hours or 0.0 for k in late)
        mid = len(hours) // 2
        median = hours[mid] if len(hours) % 2 else (hours[mid - 1] + hours[mid]) / 2
        self._add(
            s.LATE_SITE,
            site,
            site,
            f"Site {site} reports serious AEs late",
            f"{len(late)} of the site's {len(all_cases)} safety cases reached the sponsor more "
            f"than {self.cfg.late_after_hours:g} hours after the site became aware (median "
            f"{median:.0f} hours); a site with {self.cfg.site_late_min} or more late cases is "
            "flagged as a whole.",
            [
                self._ev(
                    s.SAFETY_CASES,
                    f"SITEID={site}",
                    "late_cases",
                    f"{len(late)} of {len(all_cases)} cases late",
                    value=float(len(late)),
                    threshold=float(self.cfg.site_late_min),
                    direction="high",
                ),
                self._ev(
                    s.SAFETY_CASES,
                    f"SITEID={site}; late cases",
                    "median_report_delay_hours",
                    f"median {median:.0f} hours after awareness",
                    value=round(median, 2),
                    threshold=self.cfg.late_after_hours,
                    role="supporting",
                    direction="high",
                ),
            ],
            subject_level=False,
        )
