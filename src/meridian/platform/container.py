"""Explicit wiring. Everything is constructed here and passed by constructor."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self

import structlog

from meridian.agents.catalog import build_registry
from meridian.agents.local_model import LocalModelExecutor
from meridian.agents.narration import NarrationExecutor, NarratorRunner
from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import AgentRunner
from meridian.agents.safety.adjudication import AdjudicatorRunner, reader_lookup
from meridian.agents.safety.local_narration import LocalNarratorRunner
from meridian.audit.extract import extract_rows, write_extract
from meridian.data.ingest import IngestService
from meridian.data.stage_layout import AUDIT
from meridian.data.study_store import ParquetStudyReader, StudyReader
from meridian.domain.events import AuditReviewRequested, Event, LedgerEntry
from meridian.evals.scorecard import ScorecardStore
from meridian.faults.inject import FaultInjector
from meridian.faults.safety import SafetyInjector
from meridian.faults.tmf import TmfInjector
from meridian.ledger.store import SqliteLedger
from meridian.platform.clock import Clock, SystemClock
from meridian.platform.config import LocalModelSettings, Settings
from meridian.sandbox import image
from meridian.sandbox.executor import (
    OPENSHELL,
    ExecutionError,
    Executor,
    InProcessExecutor,
    OpenShellExecutor,
)
from meridian.sandbox.image import default_image
from meridian.sandbox.local_model import OpenShellLocalModelExecutor
from meridian.sandbox.narration import OpenShellNarrationExecutor
from meridian.sandbox.openshell import OpenShellClient, SubprocessOpenShellClient
from meridian.sandbox.substrate import (
    RUNNER_VERBS,
    KubectlAteClient,
    ServiceAccountToken,
    SubstrateClient,
    free_workers,
    pool_ready_workers,
    read_install_record,
)
from meridian.sandbox.substrate_executor import (
    SUBSTRATE,
    SubstrateExecutor,
    ZoneTemplate,
    load_template,
)
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker
from meridian.sandbox.zoned import ZONED, ZonedExecutor


@dataclass(frozen=True, slots=True)
class Container:
    settings: Settings
    clock: Clock
    ledger: SqliteLedger
    # One runner token per container (minted on first use, shared by its clients).
    _tokens: dict[str, ServiceAccountToken] = field(default_factory=dict, repr=False)

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

    def etmf_reader(self, cut_id: str) -> StudyReader:
        """The blinded area of one cut's eTMF snapshot: the index, visits and export date."""
        return ParquetStudyReader(self.settings.etmf_root / self.settings.study_id, cut_id)

    def write_audit_extract(self, trigger: Event, entries: Sequence[LedgerEntry]) -> Path:
        """The audit extract of an audit review, under <audit_root>/<trigger seq>/audit."""
        p = trigger.payload
        assert isinstance(p, AuditReviewRequested)
        rows = extract_rows(
            entries,
            from_seq=p.from_seq,
            through_seq=p.through_seq,
            operational_cuts=self.settings.cuts.operational,
        )
        return write_extract(rows, self.settings.audit_root / str(trigger.seq) / AUDIT)

    def tmf_injector(self) -> TmfInjector:
        return TmfInjector(
            clock=self.clock,
            ledger=self.ledger,
            curated_root=self.settings.curated_root,
            etmf_root=self.settings.etmf_root,
            study_id=self.settings.study_id,
            ground_truth_dir=self.settings.ground_truth_dir,
            ref=self.settings.agents.tmf_completeness.reference,
        )

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
            tmf_reader_factory=self.etmf_reader,
            audit_root=self.settings.audit_root,
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
            etmf_root=self.settings.etmf_root / self.settings.study_id,
            audit_root=self.settings.audit_root,
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

    def local_model_settings(self) -> LocalModelSettings:
        if self.settings.local_model is None:
            raise RuntimeError("no [local_model] in the configuration")
        return self.settings.local_model

    def local_model_executor(
        self,
        log: structlog.typing.FilteringBoundLogger,
        client: OpenShellClient | None = None,
    ) -> OpenShellLocalModelExecutor:
        """The safety.local_model sandbox: the only way real local model calls are made."""
        rt = self.settings.runtime
        resolved = client or SubprocessOpenShellClient()
        tracker = self.sandbox_tracker()
        return OpenShellLocalModelExecutor(
            client=resolved,
            image=self.image_ref(),
            policies_dir=rt.policies_dir,
            base_url=self.local_model_settings().base_url,
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

    def adjudicator_runner(
        self,
        log: structlog.typing.FilteringBoundLogger,
        executor: LocalModelExecutor | None = None,
    ) -> AdjudicatorRunner:
        return AdjudicatorRunner(
            ledger=self.ledger,
            executor=executor or self.local_model_executor(log),
            clock=self.clock,
            settings=self.local_model_settings(),
            lookup=reader_lookup(self.study_reader, self.safety_db_reader),
            window_days=self.settings.agents.sae_reconciler.fallback_window_days,
            log=log,
        )

    def local_narrator_runner(
        self,
        log: structlog.typing.FilteringBoundLogger,
        executor: LocalModelExecutor | None = None,
    ) -> LocalNarratorRunner:
        return LocalNarratorRunner(
            ledger=self.ledger,
            executor=executor or self.local_model_executor(log),
            clock=self.clock,
            settings=self.local_model_settings(),
            log=log,
        )

    def sandbox_tracker(self) -> SandboxTracker:
        return SandboxTracker(self.settings.runtime.sandboxes_log, self.clock)

    def runner_token(self) -> ServiceAccountToken:
        """The runner's ServiceAccount token, minted on first use and shared by its clients."""
        s = self.settings.substrate
        if "runner" not in self._tokens:
            self._tokens["runner"] = ServiceAccountToken(
                path=s.runner_token_file,
                namespace=s.namespace,
                service_account=s.runner_service_account,
                audience=s.api_audience,
                kubectl=s.kubectl,
                context=s.context,
            )
        return self._tokens["runner"]

    def substrate_client(self, *, guarded: bool = True) -> KubectlAteClient:
        """The runner's client: its own identity (atespace editor) and the verb allow-list,
        so it refuses suspend, revert and tag before Substrate sees them. guarded=False is
        the same identity without the allow-list (only the guard check uses it)."""
        s = self.settings.substrate
        return KubectlAteClient(
            s.atespace,
            kubectl=s.kubectl,
            context=s.context,
            timeout_seconds=s.request_timeout_seconds,
            token_file=self.runner_token(),
            allowed=RUNNER_VERBS if guarded else None,
        )

    def substrate_admin_client(self) -> KubectlAteClient:
        """The shared bootstrap identity, unguarded: deploy and the proofs' positive control."""
        s = self.settings.substrate
        return KubectlAteClient(
            s.atespace,
            kubectl=s.kubectl,
            context=s.context,
            timeout_seconds=s.request_timeout_seconds,
        )

    def substrate_policies(self) -> list[str]:
        """The policies [runtime] executors sends to substrate."""
        return sorted(p for p, e in self.settings.runtime.executors.items() if e == SUBSTRATE)

    def check_substrate_release(self) -> None:
        """Refuse to run if ate-setup's install record names another release."""
        s = self.settings.substrate
        record = read_install_record(s.install_record.expanduser())
        if record.get("substrateVersion") != s.version or record.get("outcome") != "succeeded":
            raise ExecutionError(
                f"ate-setup's install record ({s.install_record}) says "
                f"{record.get('substrateVersion')} ({record.get('outcome')}), not {s.version}"
            )

    def substrate_templates(
        self, client: SubstrateClient, policies: Sequence[str] | None = None
    ) -> dict[str, ZoneTemplate]:
        deploy_dir = self.settings.substrate.deploy_dir
        wanted = self.substrate_policies() if policies is None else list(policies)
        return {p: load_template(client, deploy_dir, p) for p in wanted}

    def substrate_executor(
        self,
        log: structlog.typing.FilteringBoundLogger,
        client: SubstrateClient | None = None,
        policies: Sequence[str] | None = None,
    ) -> SubstrateExecutor:
        from meridian.sandbox.substrate_deploy import registry_config_digest

        self.check_substrate_release()
        resolved = client or self.substrate_client()
        templates = self.substrate_templates(resolved, policies)
        images = {t.image for t in templates.values()}
        if len(images) != 1:
            raise ExecutionError(f"the substrate templates name {len(images)} images: {images}")
        return SubstrateExecutor(
            client=resolved,
            registry=self.agent_registry(),
            settings=self.settings.substrate,
            templates=templates,
            image_digest=registry_config_digest(images.pop()),
            study_root=self.settings.curated_root / self.settings.study_id,
            tracker=SandboxTracker(self.settings.substrate.actors_log, self.clock),
            log=log,
            safety_root=self.settings.safety_root / self.settings.study_id,
            etmf_root=self.settings.etmf_root / self.settings.study_id,
            audit_root=self.settings.audit_root,
            delete_confirm_seconds=self.settings.runtime.delete_confirm_seconds,
            free_workers=None if client is not None else self._free_workers(resolved),
            placement_wait_seconds=self.settings.substrate.placement_wait_seconds,
        )

    def _free_workers(self, client: SubstrateClient) -> Callable[[], int] | None:
        if not isinstance(client, KubectlAteClient):
            return None
        s = self.settings.substrate

        def ready() -> int:
            return pool_ready_workers(s.kubectl, s.context, s.namespace, s.worker_pool)

        return lambda: free_workers(client, ready)

    def zoned_executor(self, log: structlog.typing.FilteringBoundLogger) -> ZonedExecutor:
        """openshell for every policy, except those [runtime] executors sends to substrate."""
        choice = dict(self.settings.runtime.executors)
        executors: dict[str, Executor] = {OPENSHELL: self.openshell_executor(log)}
        if SUBSTRATE in choice.values():
            executors[SUBSTRATE] = self.substrate_executor(log)
        return ZonedExecutor(executors=executors, choice=choice, default=OPENSHELL)

    def executor(self, log: structlog.typing.FilteringBoundLogger) -> Executor:
        """The executor [runtime] executor names."""
        if self.settings.runtime.executor == OPENSHELL:
            return self.openshell_executor(log)
        if self.settings.runtime.executor == ZONED:
            return self.zoned_executor(log)
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
            operational_cuts=self.settings.cuts.operational,
            operational_executors=self.settings.cuts.operational_executors,
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
