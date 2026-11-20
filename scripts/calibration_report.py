"""Write docs/calibration-step3.md: the untuned starting point of the step 3 RBQM agents.

Reads the ledger, the base and faulted cuts (blinded and ops only, through the same
reader the agents use) and the ground truth file. It changes nothing.

    uv run python scripts/calibration_report.py [--out PATH]
"""

import argparse
import math
from collections import defaultdict
from pathlib import Path

import numpy as np
import polars as pl

from meridian.data.derived import treated_subjects
from meridian.data.study_store import StudyReader
from meridian.domain.dedup import parse_dedup_key
from meridian.domain.enums import EventType, Role
from meridian.domain.events import Event, FindingRaised
from meridian.domain.findings import Finding
from meridian.faults.inject import GroundTruth
from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.stats.site_metrics import exposure

VITALS = ("SYSBP", "DIABP", "PULSE")


def _fmt(value: float | None, digits: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return f"{value:.{digits}f}"


def site_table(study: StudyReader) -> list[str]:
    dm, sv = study.table("dm"), study.table("sv")
    treated = treated_subjects(dm)
    ids = treated["USUBJID"].to_list()
    exp = exposure(dm, sv)
    included = exp.per_subject["USUBJID"].to_list()
    site_of = treated.select("USUBJID", "SITEID")
    ae = (
        study.table("ae")
        .filter(pl.col("USUBJID").is_in(included))
        .join(site_of, on="USUBJID")
        .group_by("SITEID", maintain_order=True)
        .len()
    )
    lag = (
        study.table("data_entry")
        .filter(pl.col("USUBJID").is_in(ids))
        .group_by("SITEID", maintain_order=True)
        .agg(pl.col("entry_lag_days").median().alias("lag"))
    )
    age = (
        study.table("queries")
        .filter(pl.col("USUBJID").is_in(ids) & (pl.col("status") == "open"))
        .group_by("SITEID", maintain_order=True)
        .agg(pl.col("age_days").median().alias("age"))
    )
    dev = (
        study.table("deviations")
        .filter(pl.col("USUBJID").is_in(ids))
        .group_by("SITEID", maintain_order=True)
        .len()
    )
    vs = (
        study.table("vs")
        .filter(pl.col("VSTESTCD").is_in(VITALS) & pl.col("VSSTRESN").is_not_null())
        .join(site_of, on="USUBJID")
    )
    ae_n = dict(ae.iter_rows())
    lag_m = dict(lag.iter_rows())
    age_m = dict(age.iter_rows())
    dev_n = dict(dev.iter_rows())

    def sd_ratio(site: str, test: str) -> float | None:
        rows = vs.filter(pl.col("VSTESTCD") == test)
        own = rows.filter(pl.col("SITEID") == site)["VSSTRESN"].to_numpy()
        rest = rows.filter(pl.col("SITEID") != site)["VSSTRESN"].to_numpy()
        if len(own) < 2 or len(rest) < 2:
            return None
        return float(np.std(own, ddof=1) / np.std(rest, ddof=1))

    lines = [
        "| Site | Treated | Subject-days | AEs per 100 subject-days | Median entry lag (days) "
        "| Median open query age (days) | Deviations per treated subject "
        "| SD ratio SYSBP | SD ratio DIABP | SD ratio PULSE |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for site, n_treated, _inc, _exc, days in exp.frame.select(
        "SITEID", "treated", "included", "excluded", "subject_days"
    ).iter_rows():
        ratios = [_fmt(sd_ratio(site, t)) for t in VITALS]
        lines.append(
            f"| {site} | {n_treated} | {days} | "
            f"{_fmt(100 * ae_n.get(site, 0) / days if days else None)} | "
            f"{_fmt(lag_m.get(site), 1)} | {_fmt(age_m.get(site), 1)} | "
            f"{_fmt(dev_n.get(site, 0) / n_treated)} | {' | '.join(ratios)} |"
        )
    return lines


def findings_by_cut(container: Container) -> dict[str, list[tuple[Finding, str]]]:
    out: dict[str, list[tuple[Finding, str]]] = defaultdict(list)
    entries = container.ledger.read(
        event_types=[EventType.FINDING_RAISED], viewer_role=Role.UNBLINDED_STATISTICIAN
    )
    for entry in entries:
        assert isinstance(entry, Event)
        assert isinstance(entry.payload, FindingRaised)
        parts = parse_dedup_key(entry.payload.dedup_key)
        out[parts.cut_id].append((entry.payload.finding, parts.check))
    return out


def findings_section(findings: list[tuple[Finding, str]]) -> list[str]:
    if not findings:
        return ["No findings.", ""]
    by_agent: dict[str, list[tuple[Finding, str]]] = defaultdict(list)
    for finding, metric in findings:
        by_agent[finding.agent_id].append((finding, metric))
    lines: list[str] = []
    for agent_id in sorted(by_agent):
        rows = by_agent[agent_id]
        lines += [f"### {agent_id} ({len(rows)})", ""]
        lines += [
            "| Finding | Kind | Site | Check | Severity | Score | Title |",
            "| --- | --- | --- | --- | --- | ---: | --- |",
        ]
        for f, metric in rows:
            lines.append(
                f"| {f.finding_id} | {f.kind.value} | {f.site_id or 'STUDY'} | {metric} | "
                f"{f.severity.value} | {f.score:.2f} | {f.title} |"
            )
        lines.append("")
    return lines


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("docs/calibration-step3.md"))
    parser.add_argument("--base-cut", default="DC-BASE")
    parser.add_argument("--faulted-cut", default="DC-RBQM-CORE")
    args = parser.parse_args()

    settings = Settings()
    with Container.build(settings) as container:
        base = container.study_reader(args.base_cut)
        found = findings_by_cut(container)
        truth = GroundTruth.load(settings.ground_truth_dir / f"{args.faulted_cut}.json")
        base_findings = found.get(args.base_cut, [])
        faulted_findings = found.get(args.faulted_cut, [])
        agents = sorted({a for a in settings.agents.enabled})

        detection: list[tuple[str, str, str, str, str]] = []
        for record in truth.faults:
            matches = [
                f
                for f, metric in faulted_findings
                if f.kind is record.expected_kind
                and (f.site_id or "STUDY") == record.target
                and (record.kri is None or metric == record.kri)
            ]
            ids = ", ".join(f.finding_id or "-" for f in matches) or "none"
            what = record.expected_kind.value + (f" ({record.kri})" if record.kri else "")
            detection.append(
                (
                    record.fault_id,
                    f"{record.fault_type} at {record.target}",
                    what,
                    "yes" if matches else "no",
                    ids,
                )
            )

        kri = settings.agents.kri_monitor
        csm = settings.agents.csm
        qtl = settings.agents.qtl_detector
        lines = [
            "# Calibration, phase 1 step 3 (untuned)",
            "",
            "Generated by `scripts/calibration_report.py` from the ledger, the cuts "
            f"{args.base_cut} and {args.faulted_cut}, and the ground truth file. Thresholds are "
            "the step 3 defaults and were not tuned; step 4 does that with an eval harness.",
            "",
            "Thresholds in use: "
            f"kri_monitor alpha {kri.alpha}, lag_days {kri.lag_days}, query_age_days "
            f"{kri.query_age_days}, min_subjects {kri.min_subjects}, min_open_queries "
            f"{kri.min_open_queries}; qtl_detector limit {qtl.discontinuation_limit}, approaching "
            f"from limit - {qtl.approach_band}; csm z_threshold {csm.z_threshold}, sd_ratio_max "
            f"{csm.sd_ratio_max}, min_readings {csm.min_readings}.",
            "",
            "## Summary",
            "",
            "| Agent | Findings on clean " + args.base_cut + " (all false positives) "
            "| Findings on " + args.faulted_cut + " |",
            "| --- | ---: | ---: |",
        ]
        for agent_id in agents:
            n_base = sum(1 for f, _ in base_findings if f.agent_id == agent_id)
            n_fault = sum(1 for f, _ in faulted_findings if f.agent_id == agent_id)
            lines.append(f"| {agent_id} | {n_base} | {n_fault} |")
        detected = sum(1 for row in detection if row[3] == "yes")
        lines += [
            "",
            f"Planted faults detected: {detected} of {len(detection)}.",
            "",
            "## Planted faults",
            "",
            "| Fault | Planted | Expected finding | Detected | Finding ids |",
            "| --- | --- | --- | --- | --- |",
            *(f"| {a} | {b} | {c} | {d} | {e} |" for a, b, c, d, e in detection),
            "",
            f"## Sites on {args.base_cut}",
            "",
            "Treated subjects only. Subject-days run from RFSTDTC to the last SV visit. SD ratio "
            "is the site SD over the SD of all other sites pooled.",
            "",
            *site_table(base),
            "",
            f"## Findings on {args.base_cut}",
            "",
            "Every finding here is a false positive: no fault was planted in this cut.",
            "",
            *findings_section(base_findings),
            f"## Findings on {args.faulted_cut}",
            "",
            *findings_section(faulted_findings),
        ]
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        print(f"wrote {args.out}: {detected}/{len(detection)} planted faults detected")


if __name__ == "__main__":
    main()
