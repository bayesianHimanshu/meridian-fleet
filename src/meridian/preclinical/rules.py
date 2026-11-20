"""From the analysis to the findings of preclinical.tox_study_monitor 1.1.0
(docs/preclinical-eval.md, "1.1.0: changes and targets"). Pure: the analysis and the SEND
tables in, the items to raise out; the agent words them and the Fleet API shows which
endpoint table each one rests on.

- One finding per endpoint and sex (main study), every dose group as evidence, with its
  recovery status (persists, partly reverses, reverses, not assessed) as evidence.
- A change that meets a rule only at recovery: recovery_only_change or _incidence.
- Relative organ weights at a dose with body weight at least bw_secondary_pct below
  control and no matching absolute change: one organ_weight_secondary_to_bw finding per
  sex, phase and dose.
- Any neoplasm in a treated group (neoplasm), any unscheduled death in a treated group
  with its cause (unscheduled_death): raised regardless of statistics.
"""

from collections.abc import Mapping
from dataclasses import dataclass

import polars as pl

from meridian.platform.agent_settings import ToxStudyMonitorConfig
from meridian.preclinical.analysis import (
    MAIN,
    RECOVERY,
    SCHEDULED,
    Analysis,
    GroupStat,
    Summary,
    by_key,
)

RECOVERY_ONLY_CHANGE = "recovery_only_change"
RECOVERY_ONLY_INCIDENCE = "recovery_only_incidence"
SECONDARY_TO_BW = "organ_weight_secondary_to_bw"
NEOPLASM = "neoplasm"
UNSCHEDULED_DEATH = "unscheduled_death"

PERSISTS, PARTLY, REVERSES, NOT_ASSESSED = (
    "persists",
    "partly reverses",
    "reverses",
    "not assessed",
)


@dataclass(frozen=True, slots=True)
class RecoveryStatus:
    status: str
    dose: float
    main_change: float | None  # percent (continuous) or excess points (incidence)
    recovery_change: float | None
    summary: Summary | None  # the recovery table, if any


@dataclass(frozen=True, slots=True)
class EndpointItem:
    """A main-study finding (rule = the 1.0.0 check name) or a recovery-only one."""

    check: str
    rule: str
    summary: Summary
    recovery: RecoveryStatus | None = None


@dataclass(frozen=True, slots=True)
class SecondaryItem:
    check: str
    sex: str
    phase: str
    rank: int
    dose: float
    body_weight: GroupStat
    organs: tuple[tuple[Summary, Summary | None], ...]  # (relative, absolute) per organ


@dataclass(frozen=True, slots=True)
class NeoplasmItem:
    check: str
    sex: str
    summaries: tuple[Summary, ...]  # main first, then recovery, where any animal has it


@dataclass(frozen=True, slots=True)
class DeathItem:
    check: str
    usubjid: str
    sex: str
    phase: str
    rank: int
    dose: float
    day: float | None
    disposition: str
    causes: tuple[str, ...]
    cause_source: str  # "MI" (MIDTHREL Y), "DD", or "" when not recorded


Item = EndpointItem | SecondaryItem | NeoplasmItem | DeathItem


@dataclass(frozen=True, slots=True)
class Raised:
    items: tuple[Item, ...]
    # Each endpoint table (Summary.key) to the check of the finding that carries it.
    carried_by: dict[str, str]


def _flagged(s: Summary) -> list[GroupStat]:
    return [g for g in s.groups if g.flagged and g.rank > 0]


def _group(s: Summary | None, rank: int) -> GroupStat | None:
    if s is None:
        return None
    return next((g for g in s.groups if g.rank == rank), None)


def _group_at_dose(s: Summary | None, dose: float) -> GroupStat | None:
    if s is None:
        return None
    return next((g for g in s.groups if g.dose == dose and g.rank > 0), None)


def recovery_status(
    main: Summary, recovery: Summary | None, has_recovery_groups: bool, cfg: ToxStudyMonitorConfig
) -> RecoveryStatus | None:
    """Judged at the highest flagged main dose against the recovery group of that dose."""
    if not has_recovery_groups:
        return None
    flagged = _flagged(main)
    if not flagged:
        return None
    top = max(flagged, key=lambda g: g.rank)
    frac = cfg.recovery_persists_fraction
    if main.endpoint.kind == "continuous":
        rg = _group_at_dose(recovery, top.dose)
        m = top.pct
        if rg is None or rg.pct is None or m is None:
            return RecoveryStatus(NOT_ASSESSED, top.dose, m, None, recovery)
        r = rg.pct
        if r * m < 0 or abs(r) < main.magnitude:
            status = REVERSES
        elif abs(r) >= frac * abs(m):
            status = PERSISTS
        else:
            status = PARTLY
        return RecoveryStatus(status, top.dose, m, r, recovery)
    e_m = (top.pct or 0.0) - (main.groups[0].pct or 0.0)
    if recovery is None:  # no animal affected in that sex at recovery
        return RecoveryStatus(REVERSES, top.dose, e_m, 0.0, None)
    rg = _group_at_dose(recovery, top.dose)
    if rg is None:
        return RecoveryStatus(NOT_ASSESSED, top.dose, e_m, None, recovery)
    e_r = (rg.pct or 0.0) - (recovery.groups[0].pct or 0.0)
    if e_r <= 0:
        status = REVERSES
    elif e_r >= frac * e_m:
        status = PERSISTS
    else:
        status = PARTLY
    return RecoveryStatus(status, top.dose, e_m, e_r, recovery)


def _deaths(analysis: Analysis, tables: Mapping[str, pl.DataFrame]) -> list[DeathItem]:
    animals = analysis.design.animals.filter(pl.col("unscheduled") & (pl.col("rank") > 0))
    if not animals.height:
        return []
    ds, mi, dd = tables.get("ds"), tables.get("mi"), tables.get("dd")
    out = []
    for a in animals.sort("USUBJID").iter_rows(named=True):
        uid = a["USUBJID"]
        disposition = ""
        if ds is not None:
            terms = [
                str(t).upper()
                for t in ds.filter(pl.col("USUBJID") == uid)["DSDECOD"].to_list()
                if str(t).upper() not in SCHEDULED
            ]
            disposition = terms[0] if terms else ""
        causes: list[str] = []
        source = ""
        if mi is not None and "MIDTHREL" in mi.columns:
            rows = mi.filter(
                (pl.col("USUBJID") == uid)
                & (pl.col("MIDTHREL").cast(pl.String).str.to_uppercase() == "Y")
            ).sort("MISPEC", "MISTRESC", maintain_order=True)
            causes = [
                f"{str(r['MISPEC']).title()}: {str(r['MISTRESC']).lower()}"
                for r in rows.iter_rows(named=True)
            ]
            source = "MI" if causes else ""
        if not causes and dd is not None and "DDSTRESC" in dd.columns:
            causes = [
                str(c).lower()
                for c in dd.filter(pl.col("USUBJID") == uid)["DDSTRESC"].to_list()
                if c
            ]
            source = "DD" if causes else ""
        out.append(
            DeathItem(
                check=f"{UNSCHEDULED_DEATH}:{uid}",
                usubjid=uid,
                sex=a["SEX"],
                phase=a["phase"],
                rank=a["rank"],
                dose=a["dose"],
                day=a["death_day"],
                disposition=disposition,
                causes=tuple(causes),
                cause_source=source,
            )
        )
    return out


def raise_findings(
    analysis: Analysis, tables: Mapping[str, pl.DataFrame], cfg: ToxStudyMonitorConfig
) -> Raised:
    by = by_key(analysis)
    has_recovery = bool(analysis.design.doses(RECOVERY))
    carried: dict[str, str] = {}
    items: list[Item] = []

    def low_bw_ranks(sex: str, phase: str) -> dict[int, GroupStat]:
        bw = by.get(f"bw:BW:{sex}:{phase}")
        if bw is None:
            return {}
        return {
            g.rank: g
            for g in bw.groups
            if g.rank > 0 and g.pct is not None and g.pct <= -cfg.bw_secondary_pct
        }

    def explained(rel: Summary, low: Mapping[int, GroupStat]) -> bool:
        """Every flagged dose of this relative weight has body weight down and no matching
        absolute change."""
        flagged = _flagged(rel)
        if not flagged:
            return False
        absolute = by.get(rel.key.replace(":rel:", ":abs:"))
        for g in flagged:
            if g.rank not in low:
                return False
            a = _group(absolute, g.rank)
            if a is not None and a.flagged and (a.pct or 0) * (g.pct or 0) > 0:
                return False
        return True

    secondary: dict[tuple[str, str, int], list[Summary]] = {}
    main_has_finding: set[str] = set()  # endpoint:sex
    neoplastic = [s for s in analysis.summaries if s.endpoint.neoplastic]
    for phase in (MAIN, RECOVERY):
        for s in analysis.summaries:
            if s.phase != phase or s.endpoint.neoplastic:
                continue
            ep_sex = f"{s.endpoint.endpoint}:{s.sex}"
            if phase == RECOVERY and ep_sex in main_has_finding:
                continue
            low = low_bw_ranks(s.sex, phase)
            if s.endpoint.endpoint.endswith(":rel") and low and explained(s, low):
                for g in _flagged(s):
                    secondary.setdefault((s.sex, phase, g.rank), []).append(s)
                if phase == MAIN:
                    main_has_finding.add(ep_sex)
                continue
            if s.check is None:
                continue
            if phase == MAIN:
                main_has_finding.add(ep_sex)
                rec = by.get(f"{s.endpoint.endpoint}:{s.sex}:{RECOVERY}")
                status = recovery_status(s, rec, has_recovery, cfg)
                item = EndpointItem(f"{s.check}:{s.key}", s.check, s, status)
                carried[s.key] = item.check
                if status is not None and status.summary is not None:
                    carried[status.summary.key] = item.check
            else:
                rule = (
                    RECOVERY_ONLY_CHANGE
                    if s.endpoint.kind == "continuous"
                    else RECOVERY_ONLY_INCIDENCE
                )
                item = EndpointItem(f"{rule}:{s.key}", rule, s)
                carried[s.key] = item.check
            items.append(item)
    for (sex, phase, rank), rels in sorted(
        secondary.items(), key=lambda kv: (kv[0][1] != MAIN, kv[0][0] != "M", kv[0][2])
    ):
        check = f"{SECONDARY_TO_BW}:{sex}:{phase}:r{rank}"
        organs = tuple(
            (rel, by.get(rel.key.replace(":rel:", ":abs:")))
            for rel in sorted(rels, key=lambda r: r.endpoint.endpoint)
        )
        bw = low_bw_ranks(sex, phase)[rank]
        items.append(SecondaryItem(check, sex, phase, rank, bw.dose, bw, organs))
        for rel, _ in organs:
            carried.setdefault(rel.key, check)
    by_neoplasm: dict[tuple[str, str], list[Summary]] = {}
    for s in neoplastic:
        by_neoplasm.setdefault((s.endpoint.endpoint, s.sex), []).append(s)
    for (endpoint, sex), summaries in sorted(
        by_neoplasm.items(), key=lambda kv: (kv[0][0], kv[0][1] != "M")
    ):
        if not any(g.rank > 0 and g.affected > 0 for s in summaries for g in s.groups):
            continue
        check = f"{NEOPLASM}:{endpoint}:{sex}"
        ordered = tuple(sorted(summaries, key=lambda s: s.phase != MAIN))
        items.append(NeoplasmItem(check, sex, ordered))
        for s in ordered:
            carried[s.key] = check
    items.extend(_deaths(analysis, tables))
    return Raised(tuple(items), carried)
