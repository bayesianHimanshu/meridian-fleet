"""Apply a scenario to a base cut: write the faulted cut, its ground truth and a DataCut."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import polars as pl
from pydantic import BaseModel, ConfigDict

from meridian.data.cut_writer import (
    CutManifest,
    ParquetWriter,
    write_cut,
    write_parquet_deterministic,
)
from meridian.data.datacut import Ledger, record_data_cut
from meridian.data.files import sha256_file, write_bytes_atomic
from meridian.domain.events import Event
from meridian.faults.catalog import REGISTRY, FaultRecord, Tables
from meridian.faults.reader import FullCut, FullCutReader
from meridian.faults.scenario import Scenario, ensure_holdout, ensure_rbqm_core, load_scenario
from meridian.platform.clock import Clock

FAULTS_ACTOR = "system.faults"


class InjectionError(Exception):
    pass


class GroundTruth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cut_id: str
    scenario_path: str
    scenario_sha256: str
    seed: int
    base_cut: str
    base_manifest_sha256: str
    faults: tuple[FaultRecord, ...]

    def dumps(self) -> str:
        return json.dumps(self.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"

    @classmethod
    def load(cls, path: Path) -> GroundTruth:
        return cls.model_validate_json(path.read_bytes())


@dataclass(frozen=True, slots=True)
class InjectResult:
    cut_path: Path
    manifest: CutManifest
    cut_created: bool
    ground_truth_path: Path
    ground_truth: GroundTruth
    event: Event
    event_appended: bool


def apply_scenario(scenario: Scenario, base: FullCut) -> tuple[Tables, list[FaultRecord]]:
    """Apply faults in file order, each with its own child of SeedSequence(seed)."""
    tables: Tables = {**base.blinded, **base.ops}
    specs = scenario.specs()
    children = np.random.SeedSequence(scenario.seed).spawn(len(specs))
    records: list[FaultRecord] = []
    for spec, child in zip(specs, children, strict=True):
        rng = np.random.Generator(np.random.PCG64(child))
        tables, record = REGISTRY[spec.fault_type].fn(tables, spec, rng)
        records.append(record)
    return tables, records


class FaultInjector:
    def __init__(
        self,
        *,
        clock: Clock,
        ledger: Ledger,
        curated_root: Path,
        study_id: str,
        ground_truth_dir: Path,
        writer: ParquetWriter = write_parquet_deterministic,
        scenario_generators: dict[str, Callable[[Path, FullCut], None]] | None = None,
    ) -> None:
        self._clock = clock
        self._ledger = ledger
        self._curated_root = curated_root
        self._study_id = study_id
        self._ground_truth_dir = ground_truth_dir
        self._writer = writer
        self._generators = (
            {"rbqm_core": ensure_rbqm_core, "rbqm_holdout": ensure_holdout}
            if scenario_generators is None
            else scenario_generators
        )

    def _study_dir(self) -> Path:
        return self._curated_root / self._study_id

    def ensure_scenario(self, path: Path, base_cut: str = "DC-BASE") -> None:
        """Generate a known scenario file once from its base cut if it does not exist yet."""
        if path.exists():
            return
        generator = self._generators.get(path.stem)
        if generator is None:
            raise InjectionError(f"scenario file {path} does not exist")
        generator(path, FullCutReader(self._study_dir() / base_cut).read())

    def inject(self, scenario_path: Path) -> InjectResult:
        self.ensure_scenario(scenario_path)
        scenario = load_scenario(scenario_path)
        base_dir = self._study_dir() / scenario.base_cut
        if not base_dir.is_dir():
            raise InjectionError(f"base cut {scenario.base_cut} not found at {base_dir}")
        base = FullCutReader(base_dir).read()
        tables, records = apply_scenario(scenario, base)

        truth = GroundTruth(
            cut_id=scenario.out_cut,
            scenario_path=scenario_path.as_posix(),
            scenario_sha256=sha256_file(scenario_path),
            seed=scenario.seed,
            base_cut=scenario.base_cut,
            base_manifest_sha256=base.manifest_sha256,
            faults=tuple(records),
        )
        truth_text = truth.dumps()
        truth_path = self._ground_truth_dir / f"{scenario.out_cut}.json"
        if truth_path.exists() and truth_path.read_text(encoding="utf-8") != truth_text:
            raise InjectionError(
                f"{truth_path} already exists with different content; ground truth is written once"
            )

        written = write_cut(
            curated_root=self._curated_root,
            study_id=self._study_id,
            cut_id=scenario.out_cut,
            blinded={name: tables[name] for name in base.blinded},
            ops={name: tables[name] for name in base.ops},
            unblinded=base.unblinded,
            created_at=self._clock.now(),
            seed=base.manifest.seed,
            seed_record={},
            sources=[],
            writer=self._writer,
            derived_from_cut=base.manifest.cut_id,
            derived_from_manifest_sha256=base.manifest_sha256,
        )
        if not truth_path.exists():
            self._ground_truth_dir.mkdir(parents=True, exist_ok=True)
            write_bytes_atomic(truth_path, truth_text.encode("utf-8"))

        event, appended = record_data_cut(
            self._ledger,
            clock=self._clock,
            actor=FAULTS_ACTOR,
            study_id=self._study_id,
            cut_id=scenario.out_cut,
            path=written.path,
        )
        return InjectResult(
            cut_path=written.path,
            manifest=written.manifest,
            cut_created=written.created,
            ground_truth_path=truth_path,
            ground_truth=truth,
            event=event,
            event_appended=appended,
        )


def unchanged_unblinded(base: FullCut, cut_dir: Path) -> bool:
    """True when every unblinded table in cut_dir equals the base cut's."""
    return all(
        pl.read_parquet(cut_dir / "unblinded" / f"{name}.parquet").equals(frame, null_equal=True)
        for name, frame in base.unblinded.items()
    )
