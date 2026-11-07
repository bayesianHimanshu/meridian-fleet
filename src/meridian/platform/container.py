"""Explicit wiring. Everything is constructed here and passed by constructor."""

from dataclasses import dataclass
from types import TracebackType
from typing import Self

from meridian.data.ingest import IngestService
from meridian.data.study_store import ParquetStudyReader, StudyReader
from meridian.faults.inject import FaultInjector
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import Clock, SystemClock
from meridian.platform.config import Settings


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    clock: Clock
    ledger: SqliteLedger

    @classmethod
    def build(cls, settings: Settings, clock: Clock | None = None) -> Self:
        resolved_clock: Clock = clock if clock is not None else SystemClock()
        ledger = SqliteLedger(
            path=settings.ledger_path,
            clock=resolved_clock,
            finding_id_prefixes=settings.finding_id_prefixes,
        )
        return cls(settings=settings, clock=resolved_clock, ledger=ledger)

    def study_reader(self, cut_id: str) -> StudyReader:
        """Blinded and ops access to one cut. The only study reader the container offers."""
        return ParquetStudyReader(self.settings.curated_root / self.settings.study_id, cut_id)

    def ingest_service(self) -> IngestService:
        return IngestService(
            clock=self.clock,
            ledger=self.ledger,
            curated_root=self.settings.curated_root,
            sites_file=self.settings.sites_file,
            study_id=self.settings.study_id,
            ops_params=self.settings.ops_synth,
        )

    def fault_injector(self) -> FaultInjector:
        return FaultInjector(
            clock=self.clock,
            ledger=self.ledger,
            curated_root=self.settings.curated_root,
            study_id=self.settings.study_id,
            ground_truth_dir=self.settings.ground_truth_dir,
        )

    def close(self) -> None:
        self.ledger.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
