"""quality.tmf_completeness 1.0.0: is the Trial Master File complete and filed on time?
Deterministic; no model.

Inputs: the cut's eTMF index (etmf_index), its monitoring visit schedule
(monitoring_visits), the export date (etmf_export) and blinded DM, for the site
milestones only. Nothing unblinded, and no finding names a subject: DM gives each site's
first and last subject date, nothing else leaves it.

The rule (docs/tmf-notes.md, "The agent's rules"):

0. Records. Every input is sorted on all of its columns first, so no result depends on the
   row order of an export. Artifact names match config/tmf_expected.toml after
   upper-casing and collapsing whitespace. A record with a missing or unusable field it
   needs to be placed (DOCUMENT_ID, ARTIFACT, LEVEL, SITE, VERSION, DOCUMENT_DATE, or a
   document date after the day it was filed or after the export) gets a tmf_data_quality
   finding and is left out. A bad FILED_DATE or QC_STATUS gets a tmf_data_quality finding
   too, but the document still counts as filed; only the checks that need the field are
   skipped for it. A visit with a bad field gets a tmf_data_quality finding and is left
   out. One bad record never fails the cut.
1. A site is active when DM has a subject there or the schedule has a visit there; it is
   activated on its initiation visit, else on its first subject. A document indexed under
   a site other than the one it names (DOC_SITE) is misfiled (tmf_misfiled) and counts for
   neither site. A QC-rejected version never counts.
2. Expected: a trial-level artifact once the trial has a first subject; a site_activation
   artifact once its site is activated; a visit report once its visit is file_within_days
   old at export. Protocol Amendment is filed on issue and never expected. An expected
   artifact with no counting version is tmf_missing_artifact; a visit with no report of
   its type dated within visit_match_days is tmf_monitoring_report_missing (reports are
   matched to visits nearest first, each report to one visit).
3. Timing. The latest counting version of an artifact (by VERSION), and each matched visit
   report, filed more than file_within_days after its document date is tmf_late_filing.
   For an artifact with dated_before = site_first_subject (IRB or IEC Decision, Form FDA
   1572), at least one counting version must be dated on or before the site's first
   subject; otherwise tmf_approval_after_first_subject (high). A version still pending QC
   more than qc_within_days after it was filed, at export, is tmf_qc_overdue.

Severity: approval after first subject high; a missing artifact or visit report high when
it is a site-level essential record, medium otherwise; late filing, QC overdue, misfiled
and data quality medium.

Alongside the findings, assess() gives the expected-against-filed matrix and completeness
and timely-filing percentages per site and for the trial; the API serves them, the agent
raises findings only.
"""

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any, Literal

import polars as pl

from meridian.agents.base import AgentContext, FindingDraft, Zone, make_dedup_key
from meridian.agents.rbqm.common import cut_id_of
from meridian.data.study_store import MissingTableError, StudyReader
from meridian.domain import tmf as t
from meridian.domain.enums import EventType, FindingKind, Role, Severity, Visibility
from meridian.domain.events import Event
from meridian.domain.findings import Evidence
from meridian.platform.agent_settings import TmfCompletenessConfig
from meridian.platform.tmf_reference import ExpectedArtifact, TmfReference
from meridian.tmf.milestones import SiteMilestones, parse_day, site_milestones

AGENT_ID = "quality.tmf_completeness"
VERSION = "1.0.0"
VISIBLE_TO_MAX = frozenset(
    {Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR, Role.PORTFOLIO_LEAD}
)
SCORE = {Severity.HIGH: 1.0, Severity.MEDIUM: 0.5, Severity.LOW: 0.2}
DM = "dm"
CellStatus = Literal["filed", "filed_late", "missing", "not_expected"]


def readable(d: date) -> str:
    """20 Jan 2013."""
    return f"{d.day} {d:%b %Y}"


def _str(value: object) -> str:
    return "" if value is None else str(value).strip()


def _sorted_rows(table: pl.DataFrame) -> list[dict[str, Any]]:
    """Rows in an order that depends only on their content (every column, as text)."""
    if table.height == 0:
        return []
    keyed = table.with_columns(
        pl.concat_str(
            [pl.col(c).cast(pl.String).fill_null("") for c in table.columns], separator="\x1f"
        ).alias("__order")
    )
    ordered = keyed.sort(["__order"], maintain_order=True).drop("__order")
    return list(ordered.iter_rows(named=True))


# ---- records


@dataclass(frozen=True, slots=True)
class Doc:
    doc_id: str
    artifact: ExpectedArtifact
    site: str  # "" for trial level
    doc_site: str
    version: int
    day: date
    filed: date | None  # None: unusable FILED_DATE; timing checks skipped
    qc: str | None  # None: unusable QC_STATUS; the QC check skipped

    @property
    def misfiled(self) -> bool:
        return (
            self.artifact.level == t.SITE_LEVEL
            and bool(self.doc_site)
            and (self.doc_site != self.site)
        )

    @property
    def counts(self) -> bool:
        return self.qc != t.QC_REJECTED and not self.misfiled

    @property
    def delay(self) -> int | None:
        return None if self.filed is None else (self.filed - self.day).days

    @property
    def late(self) -> bool:
        return self.delay is not None and self.delay > self.artifact.file_within_days


@dataclass(frozen=True, slots=True)
class Visit:
    visit_id: str
    site: str
    visit_type: str
    day: date


@dataclass(frozen=True, slots=True)
class Problem:
    """A data-quality problem: record, field, raw value, source table, site."""

    record: str
    field: str
    value: str
    source: str
    site: str
    excluded: bool


@dataclass(frozen=True, slots=True)
class Discrepancy:
    check: str
    record: str
    site: str  # "" for trial level
    artifact: ExpectedArtifact | None = None
    doc: Doc | None = None
    visit: Visit | None = None
    milestone: date | None = None
    problem: Problem | None = None


@dataclass(slots=True)
class Cell:
    """One artifact at one site (or the trial): what was expected and what is filed."""

    artifact: str  # key
    site: str  # "" for the trial
    status: CellStatus
    expected: int = 0  # expected records (visits for a visit report)
    filed: int = 0  # of those, filed
    late: int = 0  # of those filed, filed late
    records: list[str] = field(default_factory=list)  # finding records about this cell


@dataclass(slots=True)
class Assessment:
    export_date: date | None
    sites: list[str]
    cells: list[Cell] = field(default_factory=list)
    discrepancies: list[Discrepancy] = field(default_factory=list)

    def percent(self, site: str | None) -> tuple[float | None, float | None]:
        """(completeness, timely filing) in percent for one site ("" the trial-level
        artifacts) or, with None, for everything; None where nothing is expected or filed.
        Timely filing is over filed records with a usable filed date."""
        cells = [c for c in self.cells if site is None or c.site == site]
        expected = sum(c.expected for c in cells)
        filed = sum(c.filed for c in cells)
        late = sum(c.late for c in cells)
        complete = None if expected == 0 else round(100 * filed / expected, 1)
        timely = None if filed == 0 else round(100 * (filed - late) / filed, 1)
        return complete, timely


def _table(study: StudyReader, name: str) -> pl.DataFrame:
    try:
        frame = study.table(name)
    except MissingTableError:
        return pl.DataFrame()
    return frame


def parse_documents(
    index: pl.DataFrame, ref: TmfReference, sites: set[str], export: date | None
) -> tuple[list[Doc], list[Problem]]:
    by_name = ref.by_name()
    docs: list[Doc] = []
    problems: list[Problem] = []
    seen: set[str] = set()
    unidentified = 0
    for r in _sorted_rows(index):
        doc_id = _str(r.get(t.DOCUMENT_ID))
        site = _str(r.get(t.SITE))
        if not doc_id:
            unidentified += 1
            problems.append(
                Problem(f"row-{unidentified}", t.DOCUMENT_ID, "", t.ETMF_INDEX, site, True)
            )
            continue
        found: list[Problem] = []

        def bad(col: str, excluded: bool, value: object = None, r: dict[str, Any] = r) -> None:
            raw = _str(r.get(col)) if value is None else _str(value)
            found.append(Problem(doc_id, col, raw, t.ETMF_INDEX, site, excluded))  # noqa: B023

        if doc_id in seen:
            bad(t.DOCUMENT_ID, True, f"{doc_id} (duplicate)")
        artifact = by_name.get(t.norm_name(_str(r.get(t.ARTIFACT))))
        if artifact is None:
            bad(t.ARTIFACT, True)
        level = _str(r.get(t.LEVEL)).lower()
        if level not in t.LEVELS or (artifact is not None and level != artifact.level):
            bad(t.LEVEL, True)
        if level == t.SITE_LEVEL and site not in sites:
            bad(t.SITE, True)
        try:
            version = int(_str(r.get(t.VERSION)))
        except ValueError:
            version = 0
        if version < 1:
            bad(t.VERSION, True)
        day = parse_day(r.get(t.DOCUMENT_DATE))
        filed = parse_day(r.get(t.FILED_DATE))
        if (
            day is None
            or (filed is not None and day > filed)
            or (export is not None and day > export)
        ):
            bad(t.DOCUMENT_DATE, True)
        if filed is None or (export is not None and filed > export):
            bad(t.FILED_DATE, False)
            filed = None
        qc: str | None = _str(r.get(t.QC_STATUS)).lower()
        if qc not in t.QC_STATUSES:
            bad(t.QC_STATUS, False)
            qc = None
        seen.add(doc_id)
        problems += found
        if any(p.excluded for p in found) or artifact is None or day is None:
            continue
        docs.append(
            Doc(
                doc_id=doc_id,
                artifact=artifact,
                site=site if artifact.level == t.SITE_LEVEL else "",
                doc_site=_str(r.get(t.DOC_SITE)) if artifact.level == t.SITE_LEVEL else "",
                version=version,
                day=day,
                filed=filed,
                qc=qc,
            )
        )
    return docs, problems


def parse_visits(table: pl.DataFrame) -> tuple[list[Visit], list[Problem]]:
    visits: list[Visit] = []
    problems: list[Problem] = []
    seen: set[str] = set()
    unidentified = 0
    for r in _sorted_rows(table):
        vid = _str(r.get(t.VISIT_ID))
        site = _str(r.get(t.SITE))
        if not vid or vid in seen:
            unidentified += 1
            record = vid or f"visit-row-{unidentified}"
            problems.append(Problem(record, t.VISIT_ID, vid, t.MONITORING_VISITS, site, True))
            continue
        seen.add(vid)
        vtype = _str(r.get(t.VISIT_TYPE)).lower()
        day = parse_day(r.get(t.VISIT_DATE))
        bad = [
            (col, _str(r.get(col)))
            for col, ok in (
                (t.SITE, bool(site)),
                (t.VISIT_TYPE, vtype in t.VISIT_TYPES),
                (t.VISIT_DATE, day is not None),
            )
            if not ok
        ]
        for col, raw in bad:
            problems.append(Problem(vid, col, raw, t.MONITORING_VISITS, site, True))
        if not bad and day is not None:
            visits.append(Visit(vid, site, vtype, day))
    return visits, problems


def _export(table: pl.DataFrame) -> tuple[date | None, list[Problem]]:
    rows = _sorted_rows(table)
    values = [parse_day(r.get(t.EXPORT_DATE)) for r in rows]
    days = [d for d in values if d is not None]
    if len(rows) == 1 and days:
        return days[0], []
    raw = ", ".join(_str(r.get(t.EXPORT_DATE)) for r in rows)
    return None, [Problem("export", t.EXPORT_DATE, raw, t.ETMF_EXPORT, "", False)]


def _latest(docs: Sequence[Doc]) -> Doc:
    return max(docs, key=lambda d: (d.version, d.filed or d.day, d.doc_id))


def match_reports(visits: Sequence[Visit], reports: Sequence[Doc], days: int) -> dict[str, Doc]:
    """visit id to its report: nearest first (then earlier visit, then document id), each
    report to one visit, at most `days` apart."""
    options = sorted(
        (abs((r.day - v.day).days), v.day, v.visit_id, r.doc_id, v, r)
        for v in visits
        for r in reports
        if abs((r.day - v.day).days) <= days
    )
    out: dict[str, Doc] = {}
    used: set[str] = set()
    for _, _, vid, did, _, r in options:
        if vid not in out and did not in used:
            out[vid] = r
            used.add(did)
    return out


def assess(study: StudyReader, cfg: TmfCompletenessConfig) -> Assessment:
    """Every discrepancy, and the expected-against-filed matrix, of one cut."""
    ref = cfg.reference
    dm = _table(study, DM)
    milestones = site_milestones(dm, cfg.first_subject_field, cfg.last_subject_field)
    export, problems = _export(_table(study, t.ETMF_EXPORT))
    visits, visit_problems = parse_visits(_table(study, t.MONITORING_VISITS))
    sites = sorted(set(milestones) | {v.site for v in visits})
    docs, doc_problems = parse_documents(_table(study, t.ETMF_INDEX), ref, set(sites), export)
    a = Assessment(export_date=export, sites=sites)
    reported: set[tuple[str, str]] = set()
    for p in (*problems, *visit_problems, *doc_problems):
        if (p.record, p.field) not in reported:
            reported.add((p.record, p.field))
            a.discrepancies.append(
                Discrepancy(t.DATA_QUALITY, f"{p.record}#{p.field}", p.site, problem=p)
            )
    for d in docs:
        if d.misfiled:
            a.discrepancies.append(Discrepancy(t.MISFILED, d.doc_id, d.site, d.artifact, doc=d))
        if (
            export is not None
            and d.qc == t.QC_PENDING
            and d.filed is not None
            and (export - d.filed).days > ref.rules.qc_within_days
        ):
            a.discrepancies.append(
                Discrepancy(t.QC_OVERDUE, d.doc_id, d.site, d.artifact, doc=d, milestone=export)
            )
    counting: dict[tuple[str, str], list[Doc]] = defaultdict(list)
    for d in docs:
        if d.counts:
            counting[(d.site, d.artifact.key)].append(d)
    firsts = [m.first_subject for m in milestones.values() if m.first_subject is not None]
    trial_first = min(firsts) if firsts else None
    activation = {s: _activation(s, visits, milestones.get(s)) for s in sites}

    def due(day: date | None, lag: int = 0) -> bool:
        return day is not None and (export is None or day + timedelta(days=lag) <= export)

    for art in ref.artifacts:
        scopes = [""] if art.level == t.TRIAL else sites
        for site in scopes:
            if art.visit_type is not None:
                a.cells.append(_visits_cell(a, art, site, visits, counting, ref, due))
                continue
            have = counting.get((site, art.key), [])
            milestone = trial_first if art.level == t.TRIAL else activation[site]
            expected = art.milestone != "on_issue" and due(milestone)
            cell = Cell(art.key, site, "not_expected", expected=int(expected))
            record = f"{site or t.TRIAL_RECORD}/{art.key}"
            if have:
                latest = _latest(have)
                cell.filed = int(expected)
                cell.status = "filed"
                if latest.late:
                    cell.late = int(expected)
                    cell.status = "filed_late"
                    a.discrepancies.append(
                        Discrepancy(t.LATE_FILING, latest.doc_id, site, art, doc=latest)
                    )
                    cell.records.append(latest.doc_id)
                first = milestones[site].first_subject if site in milestones else None
                if (
                    art.dated_before == "site_first_subject"
                    and first is not None
                    and not any(d.day <= first for d in have)
                ):
                    a.discrepancies.append(
                        Discrepancy(
                            t.APPROVAL_AFTER_FIRST_SUBJECT,
                            record,
                            site,
                            art,
                            doc=min(have, key=lambda d: (d.day, d.doc_id)),
                            milestone=first,
                        )
                    )
                    cell.records.append(record)
            elif expected:
                cell.status = "missing"
                a.discrepancies.append(
                    Discrepancy(t.MISSING_ARTIFACT, record, site, art, milestone=milestone)
                )
                cell.records.append(record)
            a.cells.append(cell)
    return a


def _activation(site: str, visits: Sequence[Visit], m: SiteMilestones | None) -> date | None:
    initiations = [v.day for v in visits if v.site == site and v.visit_type == t.INITIATION]
    if initiations:
        return min(initiations)
    return None if m is None else m.first_subject if m.first_subject else None


def _visits_cell(
    a: Assessment,
    art: ExpectedArtifact,
    site: str,
    visits: Sequence[Visit],
    counting: dict[tuple[str, str], list[Doc]],
    ref: TmfReference,
    due: Callable[[date | None, int], bool],
) -> Cell:
    mine = sorted(
        (v for v in visits if v.site == site and v.visit_type == art.visit_type),
        key=lambda v: (v.day, v.visit_id),
    )
    matched = match_reports(mine, counting.get((site, art.key), []), ref.rules.visit_match_days)
    cell = Cell(art.key, site, "not_expected")
    for v in mine:
        report = matched.get(v.visit_id)
        expected = due(v.day, art.file_within_days)
        if report is None:
            if expected:
                cell.expected += 1
                a.discrepancies.append(
                    Discrepancy(t.MONITORING_REPORT_MISSING, v.visit_id, site, art, visit=v)
                )
                cell.records.append(v.visit_id)
            continue
        cell.expected += 1
        cell.filed += 1
        if report.late:
            cell.late += 1
            a.discrepancies.append(
                Discrepancy(t.LATE_FILING, report.doc_id, site, art, doc=report, visit=v)
            )
            cell.records.append(report.doc_id)
    if cell.expected:
        cell.status = (
            "missing" if cell.filed < cell.expected else "filed_late" if cell.late else "filed"
        )
    return cell


# ---- the agent


def severity(d: Discrepancy) -> Severity:
    if d.check == t.APPROVAL_AFTER_FIRST_SUBJECT:
        return Severity.HIGH
    if d.check in (t.MISSING_ARTIFACT, t.MONITORING_REPORT_MISSING):
        art = d.artifact
        site_essential = art is not None and art.level == t.SITE_LEVEL and art.essential
        return Severity.HIGH if site_essential else Severity.MEDIUM
    return Severity.MEDIUM


class TmfCompleteness:
    agent_id = AGENT_ID
    version = VERSION
    zone = Zone.QUALITY
    subscribes_to = frozenset({EventType.DATA_CUT})
    visibility = Visibility.BLINDED
    visible_to = VISIBLE_TO_MAX
    config_model = TmfCompletenessConfig

    def run(
        self, trigger: Event, ctx: AgentContext[TmfCompletenessConfig]
    ) -> Sequence[FindingDraft]:
        cfg = ctx.config
        study = ctx.study
        cut_id = cut_id_of(trigger, study)
        a = assess(study, cfg)
        drafts = [_draft(self, cfg, study.study_id, cut_id, d) for d in a.discrepancies]
        complete, timely = a.percent(None)
        ctx.log.info(
            "tmf.assessed",
            sites=len(a.sites),
            findings=len(drafts),
            completeness_percent=complete,
            timely_percent=timely,
        )
        return drafts


def _ev(
    source: str,
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
        source=source,
        selector=selector,
        statistic=statistic,
        value=value,
        threshold=threshold,
        role=role,  # type: ignore[arg-type]
        direction=direction,  # type: ignore[arg-type]
        display=display,
    )


def _doc_sel(d: Doc) -> str:
    return f"DOCUMENT_ID={d.doc_id}; VERSION={d.version}"


def _where(site: str) -> str:
    return f"site {site}" if site else "the trial"


def _draft(
    agent: TmfCompleteness, cfg: TmfCompletenessConfig, study_id: str, cut_id: str, d: Discrepancy
) -> FindingDraft:
    title, summary, evidence = _text(d, cfg)
    sev = severity(d)
    return FindingDraft(
        dedup_key=make_dedup_key(
            agent_id=agent.agent_id,
            agent_version=agent.version,
            kind=FindingKind.TMF,
            study_id=study_id,
            site_id=d.site or None,
            cut_id=cut_id,
            check=t.check_id(d.check, d.record),
        ),
        kind=FindingKind.TMF,
        site_id=d.site or None,
        severity=sev,
        title=title,
        summary=summary,
        evidence=tuple(evidence),
        score=SCORE[sev],
        visible_to=cfg.visible_to,
    )


def _text(d: Discrepancy, cfg: TmfCompletenessConfig) -> tuple[str, str, list[Evidence]]:
    art = d.artifact
    name = art.name if art is not None else ""
    if d.check == t.DATA_QUALITY:
        p = d.problem
        assert p is not None
        what = "missing" if not p.value else f"not usable ({p.value})"
        effect = (
            "the record is left out"
            if p.excluded
            else "the record still counts as filed; the checks that need this field are skipped"
        )
        return (
            f"Data quality: {p.field} of {p.record} is {what.split(' (')[0]}",
            f"{p.field} of {p.record} in {p.source} is {what}; {effect}. Every other record "
            "of the cut is still checked.",
            [
                _ev(p.source, f"RECORD={p.record}", "data_quality:field", p.field),
                _ev(p.source, f"RECORD={p.record}", "data_quality:value", p.value or "empty"),
            ],
        )
    assert art is not None
    if d.check == t.MISSING_ARTIFACT:
        due = f" (expected from {readable(d.milestone)})" if d.milestone else ""
        return (
            f"{name} is missing from the TMF of {_where(d.site)}",
            f"The TMF of {_where(d.site)} holds no counting version of {name}{due}: none "
            "filed, or only versions rejected at QC or indexed to another site.",
            [
                _ev(
                    t.ETMF_INDEX,
                    f"ARTIFACT={name}; SITE={d.site or 'trial'}",
                    "artifact:filed",
                    "not filed",
                    value=0.0,
                ),
                _ev(
                    t.ETMF_INDEX,
                    f"ARTIFACT={name}",
                    "artifact:expected_from",
                    readable(d.milestone) if d.milestone else "expected",
                    role="supporting",
                    value=_ord(d.milestone),
                ),
            ],
        )
    if d.check == t.MONITORING_REPORT_MISSING:
        v = d.visit
        assert v is not None
        kind = v.visit_type.replace("_", "-")
        return (
            f"Site {d.site}: no report of the {kind} visit of {readable(v.day)}",
            f"The monitoring visit of {readable(v.day)} at site {d.site} has no {name} in the "
            f"TMF dated within {cfg.reference.rules.visit_match_days} days of it.",
            [
                _ev(
                    t.MONITORING_VISITS,
                    f"VISIT_ID={v.visit_id}",
                    "visit:date",
                    readable(v.day),
                    value=_ord(v.day),
                ),
                _ev(
                    t.ETMF_INDEX,
                    f"ARTIFACT={name}; SITE={d.site}",
                    "visit:report",
                    "no report",
                    threshold=float(cfg.reference.rules.visit_match_days),
                ),
            ],
        )
    doc = d.doc
    assert doc is not None
    if d.check == t.LATE_FILING:
        delay = doc.delay or 0
        return (
            f"{name} of {_where(d.site)} filed {delay} days after its date",
            f"{name} (version {doc.version}) is dated {readable(doc.day)} and was filed "
            f"{readable(doc.filed or doc.day)}, {delay} days later; the limit is "
            f"{art.file_within_days} days.",
            [
                _ev(
                    t.ETMF_INDEX,
                    _doc_sel(doc),
                    "filing:delay_days",
                    f"{delay} days",
                    value=float(delay),
                    threshold=float(art.file_within_days),
                    direction="high",
                ),
                _ev(
                    t.ETMF_INDEX,
                    _doc_sel(doc),
                    "filing:document_date",
                    readable(doc.day),
                    role="supporting",
                    value=_ord(doc.day),
                ),
                _ev(
                    t.ETMF_INDEX,
                    _doc_sel(doc),
                    "filing:filed_date",
                    readable(doc.filed or doc.day),
                    role="supporting",
                    value=_ord(doc.filed),
                ),
            ],
        )
    if d.check == t.APPROVAL_AFTER_FIRST_SUBJECT:
        first = d.milestone
        assert first is not None
        gap = (doc.day - first).days
        return (
            f"Site {d.site}: {name} dated after the site's first subject",
            f"The earliest {name} of site {d.site} is dated {readable(doc.day)}, {gap} days "
            f"after the site's first subject started ({readable(first)}). No version is "
            "dated on or before it: the site may have treated a subject without it.",
            [
                _ev(
                    t.ETMF_INDEX,
                    _doc_sel(doc),
                    "approval:document_date",
                    readable(doc.day),
                    value=_ord(doc.day),
                ),
                _ev(
                    DM,
                    f"SITEID={d.site}; {cfg.first_subject_field} min",
                    "site:first_subject",
                    readable(first),
                    value=_ord(first),
                ),
            ],
        )
    if d.check == t.MISFILED:
        return (
            f"{name} of site {doc.doc_site} is filed under site {doc.site}",
            f"{name} {doc.doc_id} names site {doc.doc_site} but is indexed to site "
            f"{doc.site}; it counts for neither site until it is refiled.",
            [
                _ev(t.ETMF_INDEX, _doc_sel(doc), "filing:indexed_site", doc.site),
                _ev(t.ETMF_INDEX, _doc_sel(doc), "filing:document_site", doc.doc_site),
            ],
        )
    # QC overdue: milestone is the export date.
    export = d.milestone
    assert export is not None
    filed = doc.filed or doc.day
    waited = (export - filed).days
    limit = cfg.reference.rules.qc_within_days
    return (
        f"{name} of {_where(d.site)} pending QC for {waited} days",
        f"{name} (version {doc.version}) was filed {readable(filed)} and is still pending QC "
        f"at the export of {readable(export)}, {waited} days later; the limit is {limit} days.",
        [
            _ev(
                t.ETMF_INDEX,
                _doc_sel(doc),
                "qc:pending_days",
                f"{waited} days pending",
                value=float(waited),
                threshold=float(limit),
                direction="high",
            ),
            _ev(
                t.ETMF_INDEX,
                _doc_sel(doc),
                "qc:filed_date",
                readable(filed),
                role="supporting",
                value=_ord(filed),
            ),
        ],
    )


def _ord(d: date | None) -> float:
    return float(d.toordinal()) if d is not None else 0.0
