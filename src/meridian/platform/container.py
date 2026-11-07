"""Explicit wiring. Everything is constructed here and passed by constructor."""

from dataclasses import dataclass
from types import TracebackType
from typing import Self

import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.narration import NarrationExecutor, NarratorRunner
from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import AgentRunner
from meridian.data.ingest import IngestService
from meridian.data.study_store import ParquetStudyReader, StudyReader
from meridian.evals.scorecard import ScorecardStore
from meridian.faults.inject import FaultInjector
from meridian.faults.safety import SafetyInjector
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import Clock, SystemClock
from meridian.platform.config import Settings
from meridian.sandbox import image
from meridian.sandbox.executor import (
    OPENSHELL,
    Executor,
    InProcessExecutor,
    OpenShellExecutor,
)
from meridian.sandbox.image import default_image
from meridian.sandbox.narration import OpenShellNarrationExecutor
from meridian.sandbox.openshell import OpenShellClient, SubprocessOpenShellClient
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker


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

    def safety_db_reader(self, cut_id: str) -> StudyReader:
        """The blinded area of one cut's safety database snapshot (never its unblinded/)."""
        return ParquetStudyReader(self.settings.safety_root / self.settings.study_id, cut_id)

    def safety_injector(self) -> SafetyInjector:
        return SafetyInjector(
            clock=self.clock,
            ledger=self.ledger,
            curated_root=self.settings.curated_root,
            safety_root=self.settings.safety_root,
            study_id=self.settings.study_id,
            ground_truth_dir=self.settings.ground_truth_dir,
        )

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

    def agent_registry(self) -> AgentRegistry:
        """Every known agent, with its config from settings; enabled per settings."""
        return build_registry(self.settings.agents)

    def inprocess_executor(self, log: structlog.typing.FilteringBoundLogger) -> InProcessExecutor:
        return InProcessExecutor(
            registry=self.agent_registry(),
            reader_factory=self.study_reader,
            safety_reader_factory=self.safety_db_reader,
            log=log,
        )

    def image_ref(self) -> str:
        return self.settings.runtime.image or default_image()

    def openshell_executor(
        self,
        log: structlog.typing.FilteringBoundLogger,
        client: OpenShellClient | None = None,
    ) -> OpenShellExecutor:
        rt = self.settings.runtime
        ref = self.image_ref()
        resolved = client or SubprocessOpenShellClient()
        tracker = self.sandbox_tracker()
        deleter = BackgroundDeleter(
            delete=resolved.delete,
            tracker=tracker,
            workers=rt.delete_workers,
            log=log,
            live=resolved.names,
            confirm_seconds=rt.delete_confirm_seconds,
        )
        return OpenShellExecutor(
            client=resolved,
            tracker=tracker,
            deleter=deleter,
            registry=self.agent_registry(),
            study_root=self.settings.curated_root / self.settings.study_id,
            image=ref,
            image_digest=image.inspect(ref).digest,
            policies_dir=rt.policies_dir,
            exec_timeout_seconds=rt.exec_timeout_seconds,
            keep_failed=rt.keep_failed,
            log=log,
            safety_root=self.settings.safety_root / self.settings.study_id,
        )

    def narrator_runner(
        self,
        log: structlog.typing.FilteringBoundLogger,
        executor: NarrationExecutor | None = None,
        client: OpenShellClient | None = None,
    ) -> NarratorRunner:
        """The narrator pass. Real calls go through the OpenShell executor only."""
        if executor is None:
            rt = self.settings.runtime
            resolved = client or SubprocessOpenShellClient()
            tracker = self.sandbox_tracker()
            executor = OpenShellNarrationExecutor(
                client=resolved,
                image=self.image_ref(),
                policies_dir=rt.policies_dir,
                tracker=tracker,
                deleter=BackgroundDeleter(
                    delete=resolved.delete,
                    tracker=tracker,
                    workers=rt.delete_workers,
                    log=log,
                    live=resolved.names,
                    confirm_seconds=rt.delete_confirm_seconds,
                ),
                log=log,
            )
        return NarratorRunner(
            ledger=self.ledger,
            executor=executor,
            clock=self.clock,
            llm=self.settings.llm,
            max_calls=self.settings.narrator.max_calls_per_pass,
            log=log,
        )

    def sandbox_tracker(self) -> SandboxTracker:
        return SandboxTracker(self.settings.runtime.sandboxes_log, self.clock)

    def executor(self, log: structlog.typing.FilteringBoundLogger) -> Executor:
        """The executor [runtime] executor names."""
        if self.settings.runtime.executor == OPENSHELL:
            return self.openshell_executor(log)
        return self.inprocess_executor(log)

    def agent_runner(
        self, log: structlog.typing.FilteringBoundLogger, executor: Executor | None = None
    ) -> AgentRunner:
        return AgentRunner(
            ledger=self.ledger,
            registry=self.agent_registry(),
            executor=executor or self.executor(log),
            clock=self.clock,
            study_id=self.settings.study_id,
            log=log,
        )

    def registered_versions(self) -> dict[str, str]:
        """agent_id to version of every enabled agent: the default eval version filter."""
        return {r.agent.agent_id: r.agent.version for r in self.agent_registry().enabled()}

    def scorecard_store(self) -> ScorecardStore:
        return ScorecardStore(self.settings.evals_dir)

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
