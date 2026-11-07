"""Scenario files: which faults to plant, where, and with what target levels."""

import hashlib
import itertools
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import numpy as np
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
    # Readable name of an opaque scenario. It reaches the ground truth and the report,
    # never the cut id or the cut.
    label: str | None = None

    def specs(self) -> list[FaultSpec]:
        return [
            FaultSpec(fault_id=f"F-{i:03d}", fault_type=f.type, target=f.target, params=f.params)
            for i, f in enumerate(self.faults, start=1)
        ]


OPAQUE_PREFIX = "DC-"


def opaque_cut_id(scenario_bytes: bytes) -> str:
    """DC- plus the first 8 hex characters of sha256 of the scenario file bytes."""
    return OPAQUE_PREFIX + hashlib.sha256(scenario_bytes).hexdigest()[:8]


def load_scenario(path: Path) -> Scenario:
    """A scenario file without out_cut gets the opaque cut id of its bytes."""
    raw = path.read_bytes()
    data = tomllib.loads(raw.decode("utf-8"))
    if "out_cut" not in data:
        data["out_cut"] = opaque_cut_id(raw)
    return Scenario.model_validate(data)


def _fmt(value: float) -> str:
    return repr(float(value))


def render_scenario(scenario: Scenario, header: list[str], *, opaque: bool = False) -> str:
    """TOML text. opaque: no out_cut line (the cut id comes from the file bytes)."""
    lines = [*(f"# {h}" if h else "#" for h in header), ""]
    lines += [f"seed = {scenario.seed}", f'base_cut = "{scenario.base_cut}"']
    if opaque:
        lines.append(f'label = "{scenario.label}"')
    else:
        lines.append(f'out_cut = "{scenario.out_cut}"')
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


# Holdout 2 and the AE holdouts (phase 1 step 5). Opaque cut ids from here on.

HOLDOUT2_SEED = 20261110
HOLDOUT2_FILE = "rbqm_holdout2.toml"
HOLDOUT2_LABEL = "rbqm_holdout2"
HOLDOUT2_DISCONTINUATION_UPLIFT = 0.07
AE_HOLDOUT_SEED = 20261111
AE_HOLDOUT_DIR = "ae_holdout"
AE_HOLDOUT_COUNT = 3
AE_HOLDOUT_DROP_SHARE = 0.7
HOLDOUT_FILE = "rbqm_holdout.toml"
SWEEP_DIR = "sweep"


def _selection_rng(seed: int) -> np.random.Generator:
    """The root stream of SeedSequence(seed). Injection uses its spawned children, so the
    site draw and the fault draws never share a stream."""
    return np.random.Generator(np.random.PCG64(np.random.SeedSequence(seed)))


def fault_site_pairs(scenarios: Sequence[Scenario]) -> set[tuple[str, str]]:
    return {(f.type, f.target) for s in scenarios for f in s.faults if f.target != STUDY_TARGET}


def eligible_sites(base: FullCut) -> list[str]:
    """Sites with at least MIN_TARGET_TREATED treated subjects, by site id."""
    sizes = treated_per_site(base.blinded["dm"])
    return sorted(sizes.filter(pl.col("treated") >= MIN_TARGET_TREATED)["SITEID"].to_list())


def _earlier(directory: Path) -> list[Scenario]:
    paths = [directory / CORE_FILE, directory / HOLDOUT_FILE]
    paths += sorted((directory / SWEEP_DIR).glob("*.toml"))
    return [load_scenario(p) for p in paths]


def generate_holdout2(base: FullCut, core: Scenario, earlier: Sequence[Scenario]) -> str:
    """The six fault types of rbqm_core with its parameters, at sites drawn by the rng.

    Every assignment of the five site faults to distinct sites with at least 10 treated
    subjects that repeats no fault-site pair of `earlier` is listed in sorted order, and
    the rng picks one uniformly. discontinuation_drift targets base + 0.07.
    """
    sites = eligible_sites(base)
    used = fault_site_pairs(earlier)
    site_faults = [f for f in core.faults if f.target != STUDY_TARGET]
    options = [
        combo
        for combo in itertools.permutations(sites, len(site_faults))
        if all((f.type, site) not in used for f, site in zip(site_faults, combo, strict=True))
    ]
    if not options:
        raise ScenarioError("no assignment of holdout 2 sites avoids every earlier pair")
    pick = options[int(_selection_rng(HOLDOUT2_SEED).integers(len(options)))]
    discontinued, n, rate = _base_rate(base)
    faults = [
        FaultEntry(type=f.type, target=site, params=f.params)
        for f, site in zip(site_faults, pick, strict=True)
    ]
    faults.append(
        FaultEntry(
            type="discontinuation_drift",
            target=STUDY_TARGET,
            params={"target_rate": round(rate + HOLDOUT2_DISCONTINUATION_UPLIFT, 3)},
        )
    )
    scenario = Scenario(
        seed=HOLDOUT2_SEED,
        base_cut=base.manifest.cut_id,
        out_cut="(opaque)",
        faults=tuple(faults),
        label=HOLDOUT2_LABEL,
    )
    header = [
        "RBQM holdout 2. Generated once from "
        f"{base.manifest.cut_id} after the AE expectation variant was frozen;",
        "always read from this file afterwards. Never used for tuning. The cut id is opaque:",
        "DC- plus the first 8 hex characters of sha256 of this file's bytes.",
        "",
        "Same fault types and parameters as rbqm_core. Sites: every assignment of the five",
        f"site faults to distinct sites with at least {MIN_TARGET_TREATED} treated subjects "
        f"({', '.join(sites)})",
        "that repeats no fault-site pair of rbqm_core, rbqm_holdout or the sweep, in sorted",
        f"order; {len(options)} assignments, one picked by the root stream of "
        f"SeedSequence({HOLDOUT2_SEED}).",
        "",
        f"target_rate: base rate {discontinued}/{n} = {rate:.4f}, plus "
        f"{HOLDOUT2_DISCONTINUATION_UPLIFT:.2f}, rounded to 3 decimals.",
    ]
    return render_scenario(scenario, header, opaque=True)


def generate_ae_holdouts(base: FullCut, earlier: Sequence[Scenario]) -> list[str]:
    """Three cuts, one ae_under_reporting fault each (drop_share 0.7), at three different
    sites with at least 10 treated subjects that were never an AE target before."""
    before = sorted({t for kind, t in fault_site_pairs(earlier) if kind == "ae_under_reporting"})
    candidates = [s for s in eligible_sites(base) if s not in before]
    if len(candidates) < AE_HOLDOUT_COUNT:
        raise ScenarioError(f"only {len(candidates)} sites were never an AE target")
    picked = _selection_rng(AE_HOLDOUT_SEED).choice(
        np.array(candidates, dtype=object), size=AE_HOLDOUT_COUNT, replace=False
    )
    texts: list[str] = []
    for n, site in enumerate((str(x) for x in picked), start=1):
        scenario = Scenario(
            seed=AE_HOLDOUT_SEED + n,
            base_cut=base.manifest.cut_id,
            out_cut="(opaque)",
            faults=(
                FaultEntry(
                    type="ae_under_reporting",
                    target=site,
                    params={"drop_share": AE_HOLDOUT_DROP_SHARE},
                ),
            ),
            label=f"{AE_HOLDOUT_DIR}/{n}",
        )
        header = [
            f"AE holdout {n} of {AE_HOLDOUT_COUNT}. Generated once from {base.manifest.cut_id} "
            "after the AE expectation",
            "variant was frozen; never used for tuning. The cut id is opaque (sha256 of this "
            "file).",
            "",
            f"Site: sites with at least {MIN_TARGET_TREATED} treated subjects that were never "
            f"an AE target ({', '.join(before)}",
            f"were): {', '.join(candidates)}. Three drawn without replacement by the root "
            f"stream of SeedSequence({AE_HOLDOUT_SEED});",
            f"this file takes draw {n}. Injection seed {AE_HOLDOUT_SEED} + {n}.",
        ]
        texts.append(render_scenario(scenario, header, opaque=True))
    return texts


def _write_once(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        handle.write(text)


def ensure_holdout2(directory: Path, base: FullCut) -> Path:
    path = directory / HOLDOUT2_FILE
    if not path.exists():
        core = load_scenario(directory / CORE_FILE)
        _write_once(path, generate_holdout2(base, core, _earlier(directory)))
    return path


def ensure_ae_holdouts(directory: Path, base: FullCut) -> list[Path]:
    """Write the AE holdout files once (all three or none), after holdout 2."""
    paths = [directory / AE_HOLDOUT_DIR / f"{n}.toml" for n in range(1, AE_HOLDOUT_COUNT + 1)]
    if not any(p.exists() for p in paths):
        earlier = [*_earlier(directory), load_scenario(directory / HOLDOUT2_FILE)]
        for path, text in zip(paths, generate_ae_holdouts(base, earlier), strict=True):
            _write_once(path, text)
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise ScenarioError(f"AE holdout files missing: {missing}")
    return paths


# Phase 2 step 1: a fresh copy of rbqm_core for the sandbox run.

CORE_SANDBOX_FILE = "rbqm_core_sandbox.toml"
CORE_SANDBOX_SEED = 20261112
CORE_SANDBOX_LABEL = "rbqm_core_sandbox"


def generate_core_sandbox(core: Scenario) -> str:
    """rbqm_core's faults, sites and parameters, a new seed, an opaque cut id."""
    scenario = Scenario(
        seed=CORE_SANDBOX_SEED,
        base_cut=core.base_cut,
        out_cut="(opaque)",
        faults=core.faults,
        label=CORE_SANDBOX_LABEL,
    )
    header = [
        "A fresh copy of rbqm_core for phase 2 step 1: the same faults, sites and parameters,",
        f"seed {CORE_SANDBOX_SEED}, scored after the agents ran in OpenShell sandboxes.",
        "The cut id is opaque: DC- plus the first 8 hex characters of sha256 of this file.",
    ]
    return render_scenario(scenario, header, opaque=True)


def ensure_core_sandbox(path: Path, base: FullCut) -> None:
    """Write the file once, from rbqm_core.toml in the same directory."""
    del base  # the copy depends on rbqm_core only
    _write_once(path, generate_core_sandbox(load_scenario(path.parent / CORE_FILE)))
