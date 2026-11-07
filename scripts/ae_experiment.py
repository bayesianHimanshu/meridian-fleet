"""The pre-registered AE expectation experiment, on the dev cuts only.

Runs rbqm.kri_monitor in memory (nothing is written to the ledger) once per expected-AE
variant, and rbqm.csm once (it does not depend on the variant), on DC-BASE, DC-NULL-S1
to S5, DC-RBQM-CORE and the three AE sweep cuts. Applies the selection rule written in
docs/ae-experiment.md before the experiment ran, and writes the results below the
pre-registration, which is left byte for byte as committed.

    uv run python scripts/ae_experiment.py [--doc PATH]
"""

import argparse
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from scipy import stats

from meridian.agents.base import Agent, AgentConfig, AgentContext, FindingDraft
from meridian.data.study_store import StudyReader
from meridian.domain.dedup import STUDY_SCOPE, parse_dedup_key
from meridian.domain.enums import Visibility
from meridian.domain.events import DataCut, Event
from meridian.evals.matching import FindingRecord, match_faults
from meridian.evals.rules import is_rule_fault, is_rule_finding
from meridian.faults.catalog import FaultRecord
from meridian.faults.inject import GroundTruth
from meridian.platform.agent_settings import KriMonitorConfig
from meridian.platform.clock import FixedClock
from meridian.platform.config import Settings
from meridian.platform.container import Container
from meridian.stats.ae_expectation import AE_VARIANTS, AeVariant, ae_expectation
from meridian.stats.robust import quasi_poisson_test
from meridian.stats.site_metrics import sites_with_min_treated

NOW = datetime(2026, 11, 7, tzinfo=UTC)
MARKER = "<!-- Results below are written by scripts/ae_experiment.py -->"
BASE, CORE = "DC-BASE", "DC-RBQM-CORE"
NULLS = tuple(f"DC-NULL-S{i}" for i in range(1, 6))
AE_SWEEP = tuple(f"DC-SWEEP-AE-UNDER-REPORTING-{x}" for x in ("0P2", "0P4", "0P7"))
SIMPLICITY: tuple[AeVariant, ...] = ("a", "b", "c", "e", "d")


def _trigger(cut: str) -> Event:
    return Event(
        seq=1,
        event_id=uuid.uuid7(),
        occurred_at=NOW,
        recorded_at=NOW,
        actor="ae_experiment",
        visibility=Visibility.BLINDED,
        payload=DataCut(cut_id=cut, study_id="MRD-201", path=cut),
        prev_hash="0" * 64,
        hash="1" * 64,
    )


def _records(drafts: Sequence[FindingDraft], agent: Agent[Any], cut: str) -> list[FindingRecord]:
    return [
        FindingRecord(
            seq=i,
            finding_id=f"{agent.agent_id}#{cut}#{i}",
            agent_id=agent.agent_id,
            agent_version=agent.version,
            kind=d.kind,
            site=d.site_id or STUDY_SCOPE,
            cut_id=cut,
            check=parse_dedup_key(d.dedup_key).check,
            severity=d.severity.value,
            score=d.score,
            title=d.title,
        )
        for i, d in enumerate(drafts)
    ]


@dataclass(frozen=True, slots=True)
class VariantResult:
    variant: AeVariant
    phi_base: float
    ae_fp: dict[str, int]  # clean cut -> AE false positives
    detector_fp: dict[str, int]  # clean cut -> all detector false positives
    detected: dict[str, bool]  # AE dev cut -> AE fault detected
    target_z: dict[str, float | None]  # AE dev cut -> z at the AE target

    @property
    def eligible(self) -> bool:
        return all(v == 0 for v in self.ae_fp.values())

    @property
    def n_detected(self) -> int:
        return sum(self.detected.values())


def choose(results: Sequence[VariantResult]) -> tuple[AeVariant, str]:
    """The pre-registered rule. Returns the variant and the reason, in words."""
    by_id = {r.variant: r for r in results}
    eligible = [r for r in results if r.eligible]
    if not eligible:
        return "a", "no variant is eligible, so a is kept"
    best = min(eligible, key=lambda r: (-r.n_detected, r.phi_base, SIMPLICITY.index(r.variant)))
    if best.n_detected <= by_id["a"].n_detected:
        return "a", (
            f"no eligible variant detects more AE faults on dev than a "
            f"({by_id['a'].n_detected}), so a is kept and AE under-reporting is a known "
            "limitation"
        )
    ties = [r.variant for r in eligible if r.n_detected == best.n_detected]
    reason = f"{best.variant} is eligible and detects {best.n_detected} of 4 AE faults on dev"
    if len(ties) > 1:
        reason += f"; tied with {', '.join(t for t in ties if t != best.variant)}, it has the "
        reason += f"lowest phi on DC-BASE ({best.phi_base:.2f}) or comes first in a, b, c, e, d"
    return best.variant, reason


def _ae_target_z(
    reader: StudyReader, cfg: KriMonitorConfig, variant: AeVariant, target: str
) -> float | None:
    dm = reader.table("dm")
    tested, _ = sites_with_min_treated(dm, cfg.min_subjects)
    rows = ae_expectation(
        variant,
        dm=dm,
        sv=reader.table("sv"),
        ae=reader.table("ae"),
        lb=reader.table("lb"),
        tested=tested,
        early_window_days=cfg.early_window_days,
    )
    result = quasi_poisson_test(rows, lower_tail=True)
    return next((s.z for s in result.sites if s.site == target), None)


def _phi(reader: StudyReader, cfg: KriMonitorConfig, variant: AeVariant) -> float:
    dm = reader.table("dm")
    tested, _ = sites_with_min_treated(dm, cfg.min_subjects)
    rows = ae_expectation(
        variant,
        dm=dm,
        sv=reader.table("sv"),
        ae=reader.table("ae"),
        lb=reader.table("lb"),
        tested=tested,
        early_window_days=cfg.early_window_days,
    )
    return quasi_poisson_test(rows, lower_tail=True).phi


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--doc", type=Path, default=Path("docs/ae-experiment.md"))
    args = parser.parse_args()
    settings = Settings()
    structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(30))
    log = structlog.get_logger()
    clean_cuts = (BASE, *NULLS)
    ae_cuts = (CORE, *AE_SWEEP)
    results: list[VariantResult] = []
    with Container.build(settings) as c:
        readers = {cut: c.study_reader(cut) for cut in (*clean_cuts, *ae_cuts)}
        faults: dict[str, list[FaultRecord]] = {
            cut: list(GroundTruth.load(settings.ground_truth_dir / f"{cut}.json").faults)
            for cut in ae_cuts
        }
        registry = c.agent_registry()
        kri = registry.get("rbqm.kri_monitor")
        csm = registry.get("rbqm.csm")

        def run(agent: Agent[Any], config: AgentConfig, cut: str) -> list[FindingRecord]:
            ctx = AgentContext(study=readers[cut], clock=FixedClock(NOW), config=config, log=log)
            return _records(agent.run(_trigger(cut), ctx), agent, cut)

        csm_findings = {cut: run(csm.agent, csm.config, cut) for cut in readers}
        base_dm = readers[BASE].table("dm")
        base_cfg = kri.config
        assert isinstance(base_cfg, KriMonitorConfig)
        for variant in AE_VARIANTS:
            v: AeVariant = variant  # type: ignore[assignment]
            cfg = base_cfg.model_copy(update={"ae_variant": v})
            ae_fp: dict[str, int] = {}
            det_fp: dict[str, int] = {}
            detected: dict[str, bool] = {}
            target_z: dict[str, float | None] = {}
            for cut in clean_cuts:
                found = [*run(kri.agent, cfg, cut), *csm_findings[cut]]
                detectors = [f for f in found if not is_rule_finding(f)]
                det_fp[cut] = len(detectors)
                ae_fp[cut] = sum(1 for f in detectors if f.check == "ae_rate")
            for cut in ae_cuts:
                found = [*run(kri.agent, cfg, cut), *csm_findings[cut]]
                ae_faults = [f for f in faults[cut] if f.fault_type == "ae_under_reporting"]
                (ae_fault,) = ae_faults
                detector_faults = [f for f in faults[cut] if not is_rule_fault(f)]
                matched = match_faults(
                    detector_faults, [f for f in found if not is_rule_finding(f)]
                )
                detected[cut] = any(
                    m.detected for m in matched.matches if m.fault_id == ae_fault.fault_id
                )
                target_z[cut] = _ae_target_z(readers[cut], cfg, v, ae_fault.target)
            results.append(
                VariantResult(
                    variant=v,
                    phi_base=_phi(readers[BASE], cfg, v),
                    ae_fp=ae_fp,
                    detector_fp=det_fp,
                    detected=detected,
                    target_z=target_z,
                )
            )
    chosen, reason = choose(results)
    n_tested = len(sites_with_min_treated(base_dm, base_cfg.min_subjects)[0])
    cutoff = float(stats.norm.ppf(base_cfg.alpha / n_tested))
    _write(args.doc, results, chosen, reason, (cutoff, n_tested, base_cfg.alpha))
    print(f"chosen variant: {chosen} ({AE_VARIANTS[chosen]}): {reason}")


def _write(
    doc: Path,
    results: Sequence[VariantResult],
    chosen: AeVariant,
    reason: str,
    threshold: tuple[float, int, float],
) -> None:
    cutoff, n_tested, alpha = threshold
    text = doc.read_text(encoding="utf-8")
    head = text.split(MARKER, 1)[0].rstrip() + "\n"

    def yn(value: bool) -> str:
        return "yes" if value else "no"

    def z(value: float | None) -> str:
        return "n/a" if value is None else f"{value:.2f}"

    lines = [
        "",
        MARKER,
        "",
        "## Results on dev",
        "",
        "Agents run in memory by `scripts/ae_experiment.py`; nothing here is in the ledger.",
        "Detector false positives count ae_rate, deviations and variance findings; rule",
        "findings are scored against condition truth instead and are not counted.",
        "",
        "| Variant | phi DC-BASE | AE FP DC-BASE | AE FP S1..S5 | Detector FP DC-BASE "
        "| Detector FP S1..S5 | CORE | 0.2 | 0.4 | 0.7 | AE detected | Eligible |",
        "| --- | ---: | ---: | --- | ---: | --- | --- | --- | --- | --- | ---: | --- |",
    ]
    for r in results:
        null_ae = ", ".join(str(r.ae_fp[n]) for n in NULLS)
        null_det = ", ".join(str(r.detector_fp[n]) for n in NULLS)
        dev = " | ".join(yn(r.detected[c]) for c in (CORE, *AE_SWEEP))
        lines.append(
            f"| {r.variant} {AE_VARIANTS[r.variant]} | {r.phi_base:.2f} | {r.ae_fp[BASE]} | "
            f"{null_ae} | {r.detector_fp[BASE]} | {null_det} | {dev} | {r.n_detected} of 4 | "
            f"{yn(r.eligible)} |"
        )
    lines += [
        "",
        "z of the AE target site (1010 on every dev cut). A finding needs z below about "
        f"{cutoff:.2f} (Bonferroni over {n_tested} tested sites at alpha {alpha:g}):",
        "",
        "| Variant | CORE | 0.2 | 0.4 | 0.7 |",
        "| --- | ---: | ---: | ---: | ---: |",
        *(
            f"| {r.variant} | " + " | ".join(z(r.target_z[c]) for c in (CORE, *AE_SWEEP)) + " |"
            for r in results
        ),
        "",
        "## Decision",
        "",
        f"Chosen variant: **{chosen} ({AE_VARIANTS[chosen]})**. By the rule: {reason}.",
        "",
    ]
    doc.write_text(head + "\n".join(lines).rstrip() + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
