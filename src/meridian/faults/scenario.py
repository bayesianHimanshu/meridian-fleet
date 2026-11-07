"""Scenario files: which faults to plant, where, and with what target levels."""

import tomllib
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
