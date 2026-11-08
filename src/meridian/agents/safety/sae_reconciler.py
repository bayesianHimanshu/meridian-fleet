"""safety.sae_reconciler 1.0.0: serious adverse events in the clinical database (SDTM AE)
against the cases in the sponsor's safety database. Deterministic; no model.

Inputs: blinded AE (rows with AESER = Y) and the blinded case table of the same cut.
Nothing unblinded: the case table never holds the treatment column.

Matching rule (docs/safety-notes.md):
1. Exact: the same subject, the same normalised preferred term (upper case, whitespace
   collapsed) and the same onset day (a date-time and a date on the same day agree).
2. Fallback, for what is left: the same subject and preferred term with onsets at most
   fallback_window_days apart, closest first (ties: lower AESEQ, then lower case id).
   A fallback pair is one record with a field difference, not two missing records.
Partial clinical onsets (YYYY or YYYY-MM) have no day and match neither way.

Then, per pair: onset more than onset_tolerance_days apart, outcome or causality
different (normalised text). Per case: received more than late_after_hours after site
awareness. A clinical SAE left over has no case; a case left over has no clinical SAE
(the evidence says whether the clinical AE exists but is recorded non-serious). A site
with at least site_late_min late cases gets one site-level finding.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone, make_dedup_key
from meridian.agents.rbqm.common import cut_id_of
from meridian.domain import safety as s
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import SaeReconcilerConfig

AGENT_ID = "safety.sae_reconciler"
VERSION = "1.0.0"
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
    s.ONSET_MISMATCH: Severity.MEDIUM,
    s.OUTCOME_MISMATCH: Severity.MEDIUM,
    s.CAUSALITY_MISMATCH: Severity.MEDIUM,
}


@dataclass(frozen=True, slots=True)
class ClinicalSae:
    usubjid: str
    aeseq: int
    site: str
    term: str  # normalised preferred term
    onset_text: str
    onset: date | None
    outcome: str
    causality: str

    @property
    def key(self) -> str:
        return s.clinical_key(self.usubjid, self.aeseq)


@dataclass(frozen=True, slots=True)
class Case:
    caseid: str
    usubjid: str
    site: str
    term: str
    onset_text: str
    onset: date | None
    outcome: str
    causality: str
    aware: str
    receipt: str

    @property
    def delay_hours(self) -> float:
        return (s.parse_dtc(self.receipt) - s.parse_dtc(self.aware)).total_seconds() / 3600


@dataclass(frozen=True, slots=True)
class Pair:
    clinical: ClinicalSae
    case: Case
    fallback: bool


@dataclass(frozen=True, slots=True)
class Matching:
    pairs: tuple[Pair, ...]
    clinical_only: tuple[ClinicalSae, ...]
    cases_only: tuple[Case, ...]


def _str(value: object) -> str:
    return "" if value is None else str(value)


def clinical_saes(ae: pl.DataFrame, sites: dict[str, str]) -> list[ClinicalSae]:
    rows = ae.filter(pl.col("AESER").str.strip_chars().str.to_uppercase() == "Y").sort(
        ["USUBJID", "AESEQ"]
    )
    return [
        ClinicalSae(
            usubjid=_str(r["USUBJID"]),
            aeseq=int(r["AESEQ"]),
            site=sites.get(_str(r["USUBJID"]), ""),
            term=s.norm_text(r["AEDECOD"]),
            onset_text=_str(r["AESTDTC"]),
            onset=s.onset_day(r["AESTDTC"]),
            outcome=s.norm_text(r["AEOUT"]),
            causality=s.norm_text(r["AEREL"]),
        )
        for r in rows.iter_rows(named=True)
    ]


def cases(table: pl.DataFrame) -> list[Case]:
    return [
        Case(
            caseid=_str(r[s.CASEID]),
            usubjid=_str(r[s.USUBJID]),
            site=_str(r[s.SITEID]),
            term=s.norm_text(r[s.PREFERRED_TERM]),
            onset_text=_str(r[s.ONSET]),
            onset=s.onset_day(r[s.ONSET]),
            outcome=s.norm_text(r[s.OUTCOME]),
            causality=s.norm_text(r[s.CAUSALITY]),
            aware=_str(r[s.AWARE_DTC]),
            receipt=_str(r[s.RECEIPT_DTC]),
        )
        for r in table.sort(s.CASEID).iter_rows(named=True)
    ]


def match(clinical: Sequence[ClinicalSae], found: Sequence[Case], window_days: int) -> Matching:
    """Exact pass, then the fallback pass on what is left (see the module docstring)."""
    left_c = sorted(clinical, key=lambda c: (c.usubjid, c.aeseq))
    left_k = sorted(found, key=lambda k: k.caseid)
    pairs: list[Pair] = []
    for c in list(left_c):
        if c.onset is None:
            continue
        hit = next(
            (k for k in left_k if (k.usubjid, k.term, k.onset) == (c.usubjid, c.term, c.onset)),
            None,
        )
        if hit is not None:
            pairs.append(Pair(c, hit, fallback=False))
            left_c.remove(c)
            left_k.remove(hit)
    candidates = sorted(
        (
            (abs((k.onset - c.onset).days), c.aeseq, k.caseid, c, k)
            for c in left_c
            for k in left_k
            if c.onset is not None
            and k.onset is not None
            and (c.usubjid, c.term) == (k.usubjid, k.term)
            and abs((k.onset - c.onset).days) <= window_days
        ),
        key=lambda t: (t[3].usubjid, t[0], t[1], t[2]),
    )
    for _, _, _, c, k in candidates:
        if c in left_c and k in left_k:
            pairs.append(Pair(c, k, fallback=True))
            left_c.remove(c)
            left_k.remove(k)
    pairs.sort(key=lambda p: (p.clinical.usubjid, p.clinical.aeseq))
    return Matching(tuple(pairs), tuple(left_c), tuple(left_k))


def _ordinal(d: date | None) -> float:
    return float(d.toordinal()) if d is not None else 0.0


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
            _str(u): _str(site)
            for u, site in dm.select("USUBJID", "SITEID").sort("USUBJID").iter_rows()
        }
        ae = study.table(AE)
        clinical = clinical_saes(ae, sites)
        found = cases(study.table(s.SAFETY_CASES))
        m = match(clinical, found, cfg.fallback_window_days)
        out = _Findings(self, cfg, study.study_id, cut_id)

        for c in m.clinical_only:
            out.missing_case(c)
        for k in m.cases_only:
            out.orphan_case(k, ae)
        for p in m.pairs:
            c, k = p.clinical, p.case
            if c.onset is not None and k.onset is not None:
                gap = abs((k.onset - c.onset).days)
                if gap > cfg.onset_tolerance_days:
                    out.onset(p, gap)
            if c.outcome != k.outcome:
                out.field(p, s.OUTCOME_MISMATCH, "outcome", "AEOUT", s.OUTCOME)
            if c.causality != k.causality:
                out.field(p, s.CAUSALITY_MISMATCH, "causality", "AEREL", s.CAUSALITY)
        late_by_site: dict[str, list[Case]] = defaultdict(list)
        for k in found:
            if k.delay_hours > cfg.late_after_hours:
                out.late(k)
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
            fallback_pairs=sum(p.fallback for p in m.pairs),
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
                severity=SEVERITY[check],
                title=title,
                summary=summary,
                evidence=tuple(evidence),
                score=1.0 if SEVERITY[check] is Severity.HIGH else 0.5,
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
        return f"CASEID={k.caseid}"

    def _onsets(self, c: ClinicalSae | None, k: Case | None, role: str) -> list[Evidence]:
        out: list[Evidence] = []
        if c is not None:
            out.append(
                Evidence(
                    source=AE,
                    selector=self._clinical_sel(c),
                    statistic="onset:clinical",
                    value=_ordinal(c.onset),
                    role="primary" if role == "primary" else "supporting",
                    direction="none",
                    display=c.onset_text or "not recorded",
                )
            )
        if k is not None:
            out.append(
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=self._case_sel(k),
                    statistic="onset:safety_db",
                    value=_ordinal(k.onset),
                    role="primary" if role == "primary" else "supporting",
                    direction="none",
                    display=k.onset_text or "not recorded",
                )
            )
        return out

    def missing_case(self, c: ClinicalSae) -> None:
        self._add(
            s.SAE_MISSING,
            c.key,
            c.site,
            f"Serious AE of {c.usubjid} has no case in the safety database",
            f"{c.term.capitalize()} (onset {c.onset_text or 'not recorded'}) is serious in the "
            "clinical database, and no safety database case matches it: none with the same "
            "subject and preferred term and an onset within "
            f"{self.cfg.fallback_window_days} days.",
            [
                Evidence(
                    source=AE,
                    selector=self._clinical_sel(c),
                    statistic="preferred_term:clinical",
                    value=1.0,
                    role="primary",
                    direction="none",
                    display=c.term,
                ),
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=f"USUBJID={c.usubjid}; PREFERRED_TERM={c.term}",
                    statistic="preferred_term:safety_db",
                    value=0.0,
                    threshold=float(self.cfg.fallback_window_days),
                    role="primary",
                    direction="none",
                    display="no matching case",
                ),
                *self._onsets(c, None, "supporting"),
            ],
        )

    def orphan_case(self, k: Case, ae: pl.DataFrame) -> None:
        # Is there a clinical AE for it at all, recorded non-serious?
        near = ae.filter(
            (pl.col("USUBJID") == k.usubjid)
            & (pl.col("AEDECOD").str.strip_chars().str.to_uppercase() == k.term)
        ).sort("AESEQ")
        downgraded = [
            r
            for r in near.iter_rows(named=True)
            if k.onset is not None
            and (d := s.onset_day(r["AESTDTC"])) is not None
            and abs((d - k.onset).days) <= self.cfg.fallback_window_days
        ]
        if downgraded:
            r = downgraded[0]
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
            f"{k.onset_text}) matches no serious AE: {why}.",
            [
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=self._case_sel(k),
                    statistic="seriousness:safety_db",
                    value=1.0,
                    role="primary",
                    direction="none",
                    display=f"serious case, {k.term}",
                ),
                Evidence(
                    source=AE,
                    selector=selector,
                    statistic="seriousness:clinical",
                    value=0.0,
                    role="primary",
                    direction="none",
                    display=clinical_display,
                ),
                *self._onsets(None, k, "supporting"),
            ],
        )

    def onset(self, p: Pair, gap: int) -> None:
        c, k = p.clinical, p.case
        self._add(
            s.ONSET_MISMATCH,
            c.key,
            c.site,
            f"Onset of a serious AE of {c.usubjid} differs by {gap} days",
            f"{c.term.capitalize()}: onset {c.onset_text} in the clinical database and "
            f"{k.onset_text} in safety case {k.caseid}, {gap} days apart; more than "
            f"{self.cfg.onset_tolerance_days} day is a mismatch. Matched by the fallback rule "
            f"(same subject and preferred term, onsets within {self.cfg.fallback_window_days} "
            "days).",
            [
                *self._onsets(c, k, "primary"),
                Evidence(
                    source=f"{AE}, {s.SAFETY_CASES}",
                    selector=f"{self._clinical_sel(c)} | {self._case_sel(k)}",
                    statistic="onset:difference_days",
                    value=float(gap),
                    threshold=float(self.cfg.onset_tolerance_days),
                    role="supporting",
                    direction="high",
                    display=f"{gap} days apart (tolerance {self.cfg.onset_tolerance_days} day)",
                ),
            ],
        )

    def field(self, p: Pair, check: str, label: str, clinical_col: str, case_col: str) -> None:
        c, k = p.clinical, p.case
        a = c.outcome if check == s.OUTCOME_MISMATCH else c.causality
        b = k.outcome if check == s.OUTCOME_MISMATCH else k.causality
        self._add(
            check,
            c.key,
            c.site,
            f"{label.capitalize()} of a serious AE of {c.usubjid} differs from its case",
            f"{c.term.capitalize()} (onset {c.onset_text}): {label} {a} in the clinical "
            f"database ({clinical_col}), {b} in safety case {k.caseid} ({case_col}).",
            [
                Evidence(
                    source=AE,
                    selector=self._clinical_sel(c),
                    statistic=f"{label}:clinical",
                    value=0.0,
                    role="primary",
                    direction="none",
                    display=a or "not recorded",
                ),
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=self._case_sel(k),
                    statistic=f"{label}:safety_db",
                    value=0.0,
                    role="primary",
                    direction="none",
                    display=b or "not recorded",
                ),
                *self._onsets(c, k, "supporting"),
            ],
        )

    def late(self, k: Case) -> None:
        hours = k.delay_hours
        self._add(
            s.LATE_REPORT,
            k.caseid,
            k.site,
            f"Safety case {k.caseid} reached the sponsor {hours / 24:.1f} days after awareness",
            f"Subject {k.usubjid}, {k.term.capitalize()}: the site became aware at {k.aware} "
            f"and the sponsor received the case at {k.receipt}, {hours:.0f} hours later; the "
            f"expectation is within {self.cfg.late_after_hours:g} hours.",
            [
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=self._case_sel(k),
                    statistic="report_delay_hours",
                    value=round(hours, 2),
                    threshold=self.cfg.late_after_hours,
                    role="primary",
                    direction="high",
                    expected=self.cfg.late_after_hours,
                    display=f"{hours:.0f} hours after awareness",
                ),
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=self._case_sel(k),
                    statistic="awareness:safety_db",
                    value=0.0,
                    role="supporting",
                    direction="none",
                    display=k.aware,
                ),
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=self._case_sel(k),
                    statistic="receipt:safety_db",
                    value=0.0,
                    role="supporting",
                    direction="none",
                    display=k.receipt,
                ),
            ],
        )

    def late_site(self, site: str, late: list[Case], all_cases: list[Case]) -> None:
        """No subject ids anywhere in it: portfolio_lead may see this finding."""
        hours = sorted(k.delay_hours for k in late)
        median = (
            hours[len(hours) // 2]
            if len(hours) % 2
            else sum(hours[len(hours) // 2 - 1 : len(hours) // 2 + 1]) / 2
        )
        self._add(
            s.LATE_SITE,
            site,
            site,
            f"Site {site} reports serious AEs late",
            f"{len(late)} of the site's {len(all_cases)} safety cases reached the sponsor more "
            f"than {self.cfg.late_after_hours:g} hours after the site became aware (median "
            f"{median:.0f} hours); a site with {self.cfg.site_late_min} or more late cases is "
            "flagged.",
            [
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=f"SITEID={site}",
                    statistic="late_cases",
                    value=float(len(late)),
                    threshold=float(self.cfg.site_late_min),
                    role="primary",
                    direction="high",
                    display=f"{len(late)} of {len(all_cases)} cases late",
                ),
                Evidence(
                    source=s.SAFETY_CASES,
                    selector=f"SITEID={site}; late cases",
                    statistic="median_report_delay_hours",
                    value=round(median, 2),
                    threshold=self.cfg.late_after_hours,
                    role="supporting",
                    direction="high",
                    expected=self.cfg.late_after_hours,
                    display=f"median {median:.0f} hours after awareness",
                ),
            ],
            subject_level=False,
        )
