"""Scenario files: which faults to plant, where, and with what target levels."""

import tomllib
from dataclasses import dataclass
from pathlib import Path

import polars as pl
from pydantic import BaseModel, ConfigDict, Field, model_validator

from meridian.data.derived import premature_discontinuation, treated_per_site
from meridian.faults.catalog import REGISTRY, STUDY_TARGET, FaultSpec
from meridian.faults.reader import FullCut

RBQM_CORE_SEED = 20261107
RBQM_CORE_BASE_CUT = "DC-BASE"
RBQM_CORE_OUT_CUT = "DC-RBQM-CORE"
MIN_TARGET_TREATED = 10
DISCONTINUATION_UPLIFT = 0.10

# Site faults in rank order: rank 1 (largest site) gets the first one.
RBQM_CORE_SITE_FAULTS: tuple[tuple[str, dict[str, float]], ...] = (
    ("fabricated_vitals", {"subject_share": 0.8, "sd_scale": 0.25, "terminal_zero_share": 0.4}),
    ("ae_under_reporting", {"drop_share": 0.7}),
    ("entry_lag_spike", {"target_median_days": 14.0}),
    ("query_backlog", {"target_median_age_days": 40.0}),
    ("deviation_cluster", {"rate_multiplier": 4.0}),
)


class ScenarioError(Exception):
    pass


class FaultEntry(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    type: str
    target: str
    params: dict[str, float]

    @model_validator(mode="after")
    def _known(self) -> FaultEntry:
        kind = REGISTRY.get(self.type)
        if kind is None:
            raise ValueError(f"unknown fault type {self.type!r}; known: {sorted(REGISTRY)}")
        if set(self.params) != set(kind.params):
            raise ValueError(f"{self.type} needs params {sorted(kind.params)}")
        if (kind.level == "study") != (self.target == STUDY_TARGET):
            raise ValueError(f"{self.type} is {kind.level}-level; target {self.target!r}")
        return self


class Scenario(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seed: int
    base_cut: str
    out_cut: str
    faults: tuple[FaultEntry, ...] = Field(min_length=1)

    def specs(self) -> list[FaultSpec]:
        return [
            FaultSpec(fault_id=f"F-{i:03d}", fault_type=f.type, target=f.target, params=f.params)
            for i, f in enumerate(self.faults, start=1)
        ]


def load_scenario(path: Path) -> Scenario:
    return Scenario.model_validate(tomllib.loads(path.read_text(encoding="utf-8")))


def _fmt(value: float) -> str:
    return repr(float(value))


def render_scenario(scenario: Scenario, header: list[str]) -> str:
    lines = [*(f"# {h}" if h else "#" for h in header), ""]
    lines += [
        f"seed = {scenario.seed}",
        f'base_cut = "{scenario.base_cut}"',
        f'out_cut = "{scenario.out_cut}"',
    ]
    for fault in scenario.faults:
        lines += ["", "[[faults]]", f'type = "{fault.type}"', f'target = "{fault.target}"']
        lines += ["[faults.params]"]
        lines += [f"{k} = {_fmt(v)}" for k, v in fault.params.items()]
    return "\n".join(lines) + "\n"


def generate_rbqm_core(base: FullCut) -> str:
    """Build the rbqm_core scenario text from a base cut. Stops if a target is too small."""
    sizes = treated_per_site(base.blinded["dm"]).sort(
        ["treated", "SITEID"], descending=[True, False]
    )
    ranked = sizes.head(len(RBQM_CORE_SITE_FAULTS))
    too_small = ranked.filter(pl.col("treated") < MIN_TARGET_TREATED)
    if too_small.height or ranked.height < len(RBQM_CORE_SITE_FAULTS):
        raise ScenarioError(
            "every target site needs at least "
            f"{MIN_TARGET_TREATED} treated subjects; site sizes: {sizes.rows()}"
        )
    discontinued, treated = premature_discontinuation(base.blinded["dm"], base.blinded["ds"])
    base_rate = discontinued / treated
    target_rate = round(base_rate + DISCONTINUATION_UPLIFT, 3)

    faults = [
        FaultEntry(type=name, target=str(site), params=params)
        for (name, params), site in zip(RBQM_CORE_SITE_FAULTS, ranked["SITEID"], strict=True)
    ]
    faults.append(
        FaultEntry(
            type="discontinuation_drift", target=STUDY_TARGET, params={"target_rate": target_rate}
        )
    )
    scenario = Scenario(
        seed=RBQM_CORE_SEED,
        base_cut=base.manifest.cut_id,
        out_cut=RBQM_CORE_OUT_CUT,
        faults=tuple(faults),
    )
    ranking = ", ".join(f"{s} ({n})" for s, n in sizes.head(8).iter_rows())
    header = [
        "RBQM core fault scenario. Generated once from "
        f"{base.manifest.cut_id}; always read from this file afterwards.",
        "",
        "Target sites: sites ranked by treated subjects (RFSTDTC not blank), descending,",
        "ties broken by site id ascending. Rank 1 gets fabricated_vitals, rank 2",
        "ae_under_reporting, rank 3 entry_lag_spike, rank 4 query_backlog, rank 5",
        f"deviation_cluster. Every target needs at least {MIN_TARGET_TREATED} treated subjects.",
        f"Top of the ranking (site, treated): {ranking}.",
        "",
        f"target_rate: {base.manifest.cut_id} premature discontinuation rate "
        f"{discontinued}/{treated} = {base_rate:.4f}, plus {DISCONTINUATION_UPLIFT:.2f},",
        "rounded to 3 decimals.",
    ]
    return render_scenario(scenario, header)


def ensure_rbqm_core(path: Path, base: FullCut) -> None:
    """Write the scenario file once. Never overwrites an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(generate_rbqm_core(base))


# Holdout and sweep (phase 1 step 4)

RBQM_HOLDOUT_SEED = 20261108
RBQM_HOLDOUT_OUT_CUT = "DC-RBQM-HOLDOUT"
HOLDOUT_RANKS: tuple[int, ...] = (6, 7, 8, 9, 10)
HOLDOUT_FALLBACK_RANKS: tuple[int, ...] = (5, 4, 3, 2, 1)
HOLDOUT_DISCONTINUATION_UPLIFT = 0.08
SWEEP_BASE_SEED = 20261109
CORE_FILE = "rbqm_core.toml"


def _ranked(base: FullCut) -> pl.DataFrame:
    return treated_per_site(base.blinded["dm"]).sort(
        ["treated", "SITEID"], descending=[True, False]
    )


def _base_rate(base: FullCut) -> tuple[int, int, float]:
    discontinued, treated = premature_discontinuation(base.blinded["dm"], base.blinded["ds"])
    return discontinued, treated, discontinued / treated


def generate_holdout(base: FullCut, core: Scenario) -> str:
    """Same fault types and parameters as rbqm_core, at different sites.

    Site faults go, in rbqm_core's fault order, to ranks 6 to 10 (skipping sites with
    fewer than 10 treated subjects). Faults left over go to ranks 5, 4, 3, 2, 1 in that
    order, never to the site that fault type had in rbqm_core, never to a site already
    used here.
    """
    ranked = _ranked(base)
    sites = ranked["SITEID"].to_list()
    treated = dict(ranked.iter_rows())
    site_faults = [f for f in core.faults if f.target != STUDY_TARGET]
    core_site = {f.type: f.target for f in site_faults}
    eligible = [
        sites[r - 1]
        for r in HOLDOUT_RANKS
        if r <= len(sites) and treated[sites[r - 1]] >= MIN_TARGET_TREATED
    ]
    skipped = [
        f"{sites[r - 1]} (rank {r}, {treated[sites[r - 1]]})"
        for r in HOLDOUT_RANKS
        if r <= len(sites) and treated[sites[r - 1]] < MIN_TARGET_TREATED
    ]
    assigned: list[FaultEntry] = []
    used: set[str] = set()
    notes: list[str] = []
    for fault, site in zip(site_faults, eligible, strict=False):
        assigned.append(FaultEntry(type=fault.type, target=site, params=fault.params))
        used.add(site)
    for fault in site_faults[len(assigned) :]:
        choice = next(
            (
                (r, sites[r - 1])
                for r in HOLDOUT_FALLBACK_RANKS
                if r <= len(sites)
                and sites[r - 1] not in used
                and sites[r - 1] != core_site[fault.type]
            ),
            None,
        )
        if choice is None:
            raise ScenarioError(f"no fallback site left for {fault.type}; ranking {ranked.rows()}")
        rank, site = choice
        notes.append(
            f"{fault.type} falls back to rank {rank} ({site}); it had {core_site[fault.type]} "
            "in rbqm_core."
        )
        assigned.append(FaultEntry(type=fault.type, target=site, params=fault.params))
        used.add(site)

    discontinued, n, rate = _base_rate(base)
    target_rate = round(rate + HOLDOUT_DISCONTINUATION_UPLIFT, 3)
    assigned.append(
        FaultEntry(
            type="discontinuation_drift", target=STUDY_TARGET, params={"target_rate": target_rate}
        )
    )
    scenario = Scenario(
        seed=RBQM_HOLDOUT_SEED,
        base_cut=base.manifest.cut_id,
        out_cut=RBQM_HOLDOUT_OUT_CUT,
        faults=tuple(assigned),
    )
    ranking = ", ".join(f"{s} ({t})" for s, t in ranked.head(12).iter_rows())
    header = [
        "RBQM holdout fault scenario. Generated once from "
        f"{base.manifest.cut_id} after the phase 1 thresholds were frozen;",
        "always read from this file afterwards. It is never used for tuning.",
        "",
        "Same fault types and parameters as rbqm_core, at sites rbqm_core did not touch:",
        "sites ranked by treated subjects descending (ties by site id), and the five site",
        "faults go, in rbqm_core's order, to ranks 6, 7, 8, 9, 10, skipping sites with",
        f"fewer than {MIN_TARGET_TREATED} treated subjects. Faults left over go to ranks 5, 4, "
        "3, 2, 1 in",
        "that order, never to the site the fault type had in rbqm_core.",
        f"Ranking (site, treated): {ranking}.",
        f"Skipped for size: {', '.join(skipped) if skipped else 'none'}.",
        *notes,
        "",
        f"target_rate: base rate {discontinued}/{n} = {rate:.4f}, plus "
        f"{HOLDOUT_DISCONTINUATION_UPLIFT:.2f}, rounded to 3 decimals.",
    ]
    return render_scenario(scenario, header)


def ensure_holdout(path: Path, base: FullCut) -> None:
    """Write the holdout file once, from rbqm_core.toml in the same directory."""
    core = load_scenario(path.parent / CORE_FILE)
    text = generate_holdout(base, core)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


# fault type, swept parameter, levels (for discontinuation_drift: uplift over the base rate)
SWEEP: tuple[tuple[str, str, tuple[float, ...]], ...] = (
    ("fabricated_vitals", "sd_scale", (0.75, 0.5, 0.25)),
    ("ae_under_reporting", "drop_share", (0.2, 0.4, 0.7)),
    ("entry_lag_spike", "target_median_days", (6.0, 9.0, 14.0)),
    ("query_backlog", "target_median_age_days", (18.0, 25.0, 40.0)),
    ("deviation_cluster", "rate_multiplier", (1.5, 2.5, 4.0)),
    ("discontinuation_drift", "target_rate", (0.03, 0.06, 0.10)),
)


def level_label(fault_type: str, level: float) -> str:
    text = f"{level:g}".replace(".", "p")
    return f"plus{text}" if fault_type == "discontinuation_drift" else text


@dataclass(frozen=True, slots=True)
class SweepItem:
    index: int
    fault_type: str
    parameter: str
    level: float
    file_name: str
    out_cut: str


def sweep_items() -> list[SweepItem]:
    items: list[SweepItem] = []
    for fault_type, parameter, levels in SWEEP:
        for level in levels:
            label = level_label(fault_type, level)
            items.append(
                SweepItem(
                    index=len(items),
                    fault_type=fault_type,
                    parameter=parameter,
                    level=level,
                    file_name=f"{fault_type}_{label}.toml",
                    out_cut=f"DC-SWEEP-{fault_type.upper().replace('_', '-')}-{label.upper()}",
                )
            )
    return items


def generate_sweep_item(base: FullCut, core: Scenario, item: SweepItem) -> str:
    """One fault per cut, at the site it had in rbqm_core, other parameters unchanged."""
    template = next(f for f in core.faults if f.type == item.fault_type)
    params = dict(template.params)
    note = f"{item.parameter} = {item.level:g}"
    if item.fault_type == "discontinuation_drift":
        discontinued, n, rate = _base_rate(base)
        params[item.parameter] = round(rate + item.level, 3)
        note = (
            f"target_rate = base {discontinued}/{n} = {rate:.4f} plus {item.level:.2f}, "
            "rounded to 3 decimals"
        )
    else:
        params[item.parameter] = item.level
    scenario = Scenario(
        seed=SWEEP_BASE_SEED + item.index,
        base_cut=base.manifest.cut_id,
        out_cut=item.out_cut,
        faults=(FaultEntry(type=item.fault_type, target=template.target, params=params),),
    )
    header = [
        f"Magnitude sweep {item.index + 1} of {len(sweep_items())}: {item.fault_type} at "
        f"{template.target}, the site it had in rbqm_core.",
        f"Swept: {note}. Other parameters as in rbqm_core. Seed {SWEEP_BASE_SEED} + {item.index}.",
        "Generated once after the phase 1 thresholds were frozen; never used for tuning.",
    ]
    return render_scenario(scenario, header)


def ensure_sweep(directory: Path, base: FullCut, core: Scenario) -> list[Path]:
    """Write any missing sweep file. Existing files are never rewritten."""
    directory.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    for item in sweep_items():
        path = directory / item.file_name
        if not path.exists():
            with path.open("x", encoding="utf-8") as handle:
                handle.write(generate_sweep_item(base, core, item))
        paths.append(path)
    return paths
