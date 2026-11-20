"""Where an agent run happens: in process, or in a fresh OpenShell sandbox.

Either way the result is the same list of FindingDrafts; only the host runner writes to
the ledger. The OpenShell executor creates one sandbox per run from the agent image with
the zone's policy, uploads a staged copy of the cut (the trigger and the cut areas the
zone's policy may read, nothing else) into /stage, runs meridian-worker, reads back the
effective policy and always deletes the sandbox.
"""

import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import structlog
from pydantic import ValidationError

from meridian.agents.base import FindingDraft, zone_of
from meridian.agents.execute import DraftsDocument, run_agent
from meridian.agents.registry import AgentRegistry
from meridian.agents.runner import RunPlan as RunPlan
from meridian.data.cut_writer import BLINDED_AREA
from meridian.data.inputs_manifest import MANIFEST_FILE, build_manifest
from meridian.data.study_store import (
    SAFETY_DB_ZONES,
    SEND_ZONES,
    TMF_ZONES,
    AuditStageReader,
    InventoryStageReader,
    LayeredStudyReader,
    NoaelStageReader,
    PortfolioStageReader,
    SignalStageReader,
    StudyReader,
)
from meridian.domain.events import (
    AuditReviewRequested,
    DataCut,
    Event,
    GateRequest,
    HumanDecision,
    InventoryLoaded,
    NoaelDecision,
    PortfolioReviewRequested,
    SendStudyLoaded,
    SignalScanRequested,
    SubstrateRun,
    portfolio_study,
)
from meridian.portfolio.document import ResponseRecord, SignalDocument
from meridian.portfolio.store import prior_area, signal_dir
from meridian.preclinical.noael_stage import noael_dir
from meridian.sandbox.openshell import OpenShellClient, OpenShellError
from meridian.sandbox.policies import (
    ALL_POLICIES,
    AUDIT,
    INVENTORY,
    META,
    NOAEL,
    PORTFOLIO,
    SEND,
    SIGNAL,
    TMF,
    expected_effective,
    policy_for_agent,
    policy_path,
    sha256_bytes,
    sha256_text,
)
from meridian.sandbox.staging import (
    SANDBOX_CUT_DIR,
    SANDBOX_TRIGGER,
    assert_clean_stage,
    cut_areas_for,
    stage_areas,
    stage_audit,
    stage_cut,
    uploads,
)
from meridian.sandbox.tracking import BackgroundDeleter, SandboxTracker

INPROCESS = "inprocess"
OPENSHELL = "openshell"
WORKER = "/opt/meridian/venv/bin/meridian-worker"
SIGNAL_WORKER = "/opt/meridian/venv/bin/meridian-signal-worker"
SIGNAL_AGENT = "portfolio.safety_signal_scout"
STDERR_TAIL = 2000


class ExecutionError(Exception):
    """The run failed; the runner records nothing and retries on the next pass."""


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    drafts: tuple[FindingDraft, ...]
    executor: str
    image_digest: str | None
    policy_sha256: str | None
    duration_ms: int
    stderr_tail: str
    inputs_manifest_sha256: str | None = None
    policy_file: str | None = None
    policy_file_sha256: str | None = None
    substrate: SubstrateRun | None = None
    # Wall-clock milliseconds of each step of the run (not recorded in the ledger).
    phases_ms: tuple[tuple[str, int], ...] = ()
    # Phase 5 step 3a: what the run fetched from a public source (portfolio_signal), bodies
    # included; the runner keeps them as the run's inputs and records their hashes.
    fetched: tuple[ResponseRecord, ...] = ()


class Executor(Protocol):
    @property
    def name(self) -> str: ...

    def executor_name(self, agent_id: str) -> str: ...

    def plan(self, agent_id: str, trigger: Event) -> RunPlan: ...

    def capacity(self, executor: str) -> int:
        """Runs this executor takes at once; the runner queues the rest."""
        ...

    def run(
        self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
    ) -> ExecutionResult: ...

    def drain(self) -> None: ...


@dataclass(frozen=True, slots=True)
class StagedRun:
    """A run's inputs, staged on the host exactly as the zone's policy allows."""

    stage: Path
    areas: tuple[str, ...]
    inputs_manifest_sha256: str


def preclinical_sources(
    agent_id: str,
    trigger: Event,
    cut_id: str,
    send_root: Path | None,
    noael_root: Path | None,
    inventory_root: Path | None,
) -> dict[str, Path]:
    """The host folders a preclinical run reads, by area, for its trigger: the SEND cut
    for a SendStudyLoaded; the SEND cut and the NOAEL extract for a NoaelDecision; the
    inventory cut for an InventoryLoaded."""
    p = trigger.payload
    if not isinstance(p, SendStudyLoaded | NoaelDecision | HumanDecision | InventoryLoaded):
        raise ExecutionError(
            f"trigger {trigger.seq} is not the SendStudyLoaded of {cut_id}, a NOAEL decision "
            "or an inventory load"
        )
    if isinstance(p, InventoryLoaded):
        if p.cut_id != cut_id:
            raise ExecutionError(f"trigger {trigger.seq} is not the inventory load of {cut_id}")
        if inventory_root is None:
            raise ExecutionError(f"{agent_id} reads an inventory; no inventory store is set")
        area = inventory_root / p.asset_id / cut_id / INVENTORY
        if not area.is_dir():
            raise ExecutionError(f"no inventory cut {cut_id} under {inventory_root}")
        return {INVENTORY: area}
    if send_root is None:
        raise ExecutionError(f"{agent_id} reads a SEND study; no SEND store is set")
    send_dir = send_root / cut_id / SEND
    if not send_dir.is_dir():
        raise ExecutionError(f"no SEND cut {cut_id} under {send_root}")
    if isinstance(p, SendStudyLoaded):
        if p.cut_id != cut_id:
            raise ExecutionError(f"trigger {trigger.seq} is not the SendStudyLoaded of {cut_id}")
        return {SEND: send_dir}
    if isinstance(p, NoaelDecision | HumanDecision):
        if noael_root is None:
            raise ExecutionError(f"{agent_id} reads NOAEL decisions; no NOAEL extract is set")
        area = noael_dir(noael_root, trigger.seq) / NOAEL
        if not area.is_dir():
            raise ExecutionError(f"no NOAEL extract for trigger {trigger.seq} under {noael_root}")
        return {SEND: send_dir, NOAEL: area}
    raise AssertionError(p)  # unreachable: the trigger types are checked above


def stage_run(
    *,
    agent_id: str,
    trigger: Event,
    cut_id: str,
    policy_name: str,
    dest: Path,
    study_root: Path,
    safety_root: Path | None,
    etmf_root: Path | None,
    audit_root: Path | None,
    send_root: Path | None = None,
    noael_root: Path | None = None,
    inventory_root: Path | None = None,
    signal_root: Path | None = None,
    portfolio_root: Path | None = None,
) -> StagedRun:
    """Stage meta/ and the cut areas the policy reads (or the audit extract, or a SEND
    study) under dest, check the stage and the inputs manifest, and return it. Shared by
    the OpenShell and Substrate executors, so both receive the same files."""
    policy = ALL_POLICIES[policy_name]
    if SIGNAL in policy.reads:
        # portfolio_signal (phase 5 step 3a): the trigger, and for a replay the run's
        # recorded responses; never any study data.
        if not isinstance(trigger.payload, SignalScanRequested):
            raise ExecutionError(f"trigger {trigger.seq} is not a signal scan")
        kept = None if signal_root is None else signal_dir(signal_root, trigger.seq) / SIGNAL
        sources = {SIGNAL: kept} if kept is not None and kept.is_dir() else {}
        base = trigger.payload.reuses_responses_of
        if not sources and base is not None:
            # 1.0.1: a rerun on an earlier scan's kept responses, staged as prior_responses.
            sources = {SIGNAL: prior_area(signal_root, trigger.seq, base)}
        stage = stage_areas(sources, trigger, dest)
        recomputed = build_manifest(stage)
        if (stage / META / MANIFEST_FILE).read_text(encoding="utf-8") != recomputed.dumps():
            raise ExecutionError("staged files changed after the inputs manifest was written")
        return StagedRun(
            stage=stage, areas=(*sources, META), inputs_manifest_sha256=recomputed.sha256()
        )
    if isinstance(trigger.payload, SignalScanRequested):
        raise ExecutionError(f"{agent_id} may not run a signal scan ({policy_name})")
    if PORTFOLIO in policy.reads:
        # The portfolio zone (phase 5 step 3b): the facts the host built as the portfolio
        # lead for this review or gate request, and nothing else.
        if isinstance(trigger.payload, SendStudyLoaded):
            raise ExecutionError(f"{agent_id} may not read a SEND study ({policy_name} does not)")
        if not isinstance(trigger.payload, PortfolioReviewRequested | GateRequest):
            raise ExecutionError(f"trigger {trigger.seq} is not a portfolio review or gate")
        if portfolio_root is None:
            raise ExecutionError("a portfolio run needs the portfolio facts folder")
        area = portfolio_root / str(trigger.seq) / PORTFOLIO
        if not area.is_dir():
            raise ExecutionError(f"no portfolio facts for trigger {trigger.seq}")
        stage = stage_areas({PORTFOLIO: area}, trigger, dest)
        recomputed = build_manifest(stage)
        if (stage / META / MANIFEST_FILE).read_text(encoding="utf-8") != recomputed.dumps():
            raise ExecutionError("staged files changed after the inputs manifest was written")
        return StagedRun(
            stage=stage, areas=(PORTFOLIO, META), inputs_manifest_sha256=recomputed.sha256()
        )
    if isinstance(trigger.payload, PortfolioReviewRequested | GateRequest):
        raise ExecutionError(f"{agent_id} may not read portfolio facts ({policy_name})")
    if SEND in policy.reads:
        # The preclinical zone: a SEND study, never a clinical cut (phase 5 step 1), and
        # since step 2 a study's NOAEL decisions or an asset's study inventory.
        sources = preclinical_sources(
            agent_id, trigger, cut_id, send_root, noael_root, inventory_root
        )
        if not set(sources) <= set(policy.reads):
            raise ExecutionError(f"{policy_name} may not read {sorted(sources)}")
        stage = stage_areas(sources, trigger, dest)
        recomputed = build_manifest(stage)
        written = (stage / META / MANIFEST_FILE).read_text(encoding="utf-8")
        if written != recomputed.dumps():
            raise ExecutionError("staged files changed after the inputs manifest was written")
        sha = recomputed.sha256()
        return StagedRun(stage=stage, areas=(*sources, META), inputs_manifest_sha256=sha)
    if isinstance(trigger.payload, NoaelDecision | InventoryLoaded | HumanDecision):
        raise ExecutionError(f"{agent_id} may not read preclinical inputs ({policy_name})")
    if isinstance(trigger.payload, SendStudyLoaded):
        raise ExecutionError(f"{agent_id} may not read a SEND study ({policy_name} does not)")
    audit = AUDIT in policy.reads
    if audit:
        if not isinstance(trigger.payload, AuditReviewRequested):
            raise ExecutionError(f"trigger {trigger.seq} is not an audit review")
        if audit_root is None:
            raise ExecutionError("an audit review needs the audit extract folder")
    elif not isinstance(trigger.payload, DataCut) or trigger.payload.cut_id != cut_id:
        raise ExecutionError(f"trigger {trigger.seq} is not the DataCut of {cut_id}")
    extra = (
        [safety_root / cut_id / BLINDED_AREA]
        if zone_of(agent_id) in SAFETY_DB_ZONES and safety_root is not None
        else []
    )
    tmf = None
    if not audit and TMF in policy.reads:
        if etmf_root is None:
            raise ExecutionError(f"{agent_id} reads the eTMF index; no eTMF store is set")
        tmf = etmf_root / cut_id / BLINDED_AREA
        if not tmf.is_dir():
            raise ExecutionError(f"no eTMF snapshot for cut {cut_id}")
    areas = (AUDIT,) if audit else cut_areas_for(policy.reads)
    staged = (*areas, TMF) if tmf is not None else areas
    if audit:
        assert audit_root is not None
        stage = stage_audit(audit_root / str(trigger.seq) / AUDIT, trigger, dest)
    else:
        stage = stage_cut(study_root / cut_id, trigger, dest, extra, areas, tmf)
    assert_clean_stage(stage, staged)  # again, right before it leaves the host
    recomputed = build_manifest(stage)
    written = (stage / META / MANIFEST_FILE).read_text(encoding="utf-8")
    if written != recomputed.dumps():
        raise ExecutionError("staged files changed after the inputs manifest was written")
    return StagedRun(stage=stage, areas=(*staged, META), inputs_manifest_sha256=recomputed.sha256())


def _ms(start: float) -> int:
    return round((time.perf_counter() - start) * 1000)


class InProcessExecutor:
    """Phase 1 behaviour: the agent runs in this process on a ParquetStudyReader."""

    name = INPROCESS

    def __init__(
        self,
        *,
        registry: AgentRegistry,
        reader_factory: Callable[[str], StudyReader],
        log: structlog.typing.FilteringBoundLogger,
        safety_reader_factory: Callable[[str], StudyReader] | None = None,
        tmf_reader_factory: Callable[[str], StudyReader] | None = None,
        audit_root: Path | None = None,
        send_reader_factory: Callable[[str], StudyReader] | None = None,
        noael_root: Path | None = None,
        inventory_root: Path | None = None,
        signal_root: Path | None = None,
        portfolio_root: Path | None = None,
    ) -> None:
        self._portfolio_root = portfolio_root  # <portfolio_root>/<trigger seq>/portfolio
        self._signal_root = signal_root  # <signal_root>/<trigger seq>/signal (step 3a)
        self._noael_root = noael_root  # <noael_root>/<trigger seq>/noael (phase 5 step 2)
        self._inventory_root = inventory_root  # <inventory_root>/<asset>/<cut>/inventory
        self._audit_root = audit_root  # <audit_root>/<trigger seq>/audit: one per review
        self._send_reader_factory = send_reader_factory  # cut id to a SEND study reader
        self._registry = registry
        self._reader_factory = reader_factory
        self._safety_reader_factory = safety_reader_factory
        self._tmf_reader_factory = tmf_reader_factory
        self._log = log

    def _reader(self, agent_id: str, cut_id: str) -> StudyReader:
        if zone_of(agent_id) in SEND_ZONES:
            # The preclinical zone reads its SEND study and nothing of a clinical cut.
            if self._send_reader_factory is None:
                raise ExecutionError(f"{agent_id} reads a SEND study; no SEND reader is set")
            return self._send_reader_factory(cut_id)
        reader = self._reader_factory(cut_id)
        if zone_of(agent_id) in SAFETY_DB_ZONES and self._safety_reader_factory is not None:
            return LayeredStudyReader(reader, self._safety_reader_factory(cut_id))
        if zone_of(agent_id) in TMF_ZONES and self._tmf_reader_factory is not None:
            return LayeredStudyReader(reader, self._tmf_reader_factory(cut_id))
        return reader

    def _preclinical_reader(self, agent_id: str, trigger: Event, cut_id: str) -> StudyReader:
        """The same areas a sandbox would get (preclinical_sources), read in place."""
        if zone_of(agent_id) not in SEND_ZONES:
            raise ExecutionError(f"{agent_id} may not read preclinical inputs")
        p = trigger.payload
        if isinstance(p, InventoryLoaded):
            if self._inventory_root is None:
                raise ExecutionError(f"{agent_id} reads an inventory; no inventory store is set")
            return InventoryStageReader.at_cut_dir(
                self._inventory_root / p.asset_id / cut_id, cut_id=cut_id, study_id=p.asset_id
            )
        assert isinstance(p, NoaelDecision | HumanDecision)
        if self._noael_root is None:
            raise ExecutionError(f"{agent_id} reads NOAEL decisions; no NOAEL extract is set")
        send = self._reader(agent_id, cut_id)
        noael = NoaelStageReader.at_cut_dir(
            noael_dir(self._noael_root, trigger.seq), cut_id=cut_id, study_id=send.study_id
        )
        return LayeredStudyReader(send, noael)

    def executor_name(self, agent_id: str) -> str:
        return self.name

    def capacity(self, executor: str) -> int:
        """One run at a time, as before phase 4a step 3."""
        return 1

    def plan(self, agent_id: str, trigger: Event) -> RunPlan:
        return RunPlan(self.name)

    def run(
        self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
    ) -> ExecutionResult:
        start = time.perf_counter()
        registered = self._registry.get(agent_id)
        log = self._log.bind(agent_id=agent_id, trigger_seq=trigger.seq, cut_id=cut_id)
        if isinstance(trigger.payload, AuditReviewRequested):
            if self._audit_root is None:
                raise ExecutionError("an audit review needs the audit extract folder")
            reader: StudyReader = AuditStageReader.at_cut_dir(
                self._audit_root / str(trigger.seq),
                cut_id=cut_id,
                study_id=trigger.payload.study_id,
            )
        elif isinstance(trigger.payload, NoaelDecision | InventoryLoaded | HumanDecision):
            reader = self._preclinical_reader(agent_id, trigger, cut_id)
        elif isinstance(trigger.payload, PortfolioReviewRequested | GateRequest):
            if self._portfolio_root is None:
                raise ExecutionError("a portfolio run needs the portfolio facts folder")
            reader = PortfolioStageReader.at_cut_dir(
                self._portfolio_root / str(trigger.seq),
                cut_id=cut_id,
                study_id=portfolio_study(trigger),
            )
        elif isinstance(trigger.payload, SignalScanRequested):
            # In process only on recorded responses (a replay): a live scan fetches from
            # openFDA and runs only in its sandbox (portfolio_signal).
            kept = None if self._signal_root is None else signal_dir(self._signal_root, trigger.seq)
            if kept is None or not (kept / SIGNAL).is_dir():
                raise ExecutionError(
                    f"no recorded responses for scan seq {trigger.seq}: a live scan runs only "
                    "in the portfolio_signal sandbox"
                )
            reader = SignalStageReader.at_cut_dir(
                kept, cut_id=trigger.payload.scan_id, study_id=trigger.payload.study_id
            )
        else:
            reader = self._reader(agent_id, cut_id)
        drafts = run_agent(registered, trigger, reader, log)
        # A signal scan replayed in process names the kept responses it ran on, as a live
        # run names those it fetched (the runner records them on ActorWoke).
        fetched: tuple[ResponseRecord, ...] = ()
        if isinstance(trigger.payload, SignalScanRequested):
            fetched = tuple(
                ResponseRecord.model_validate(r)
                for r in reader.table("openfda_responses").iter_rows(named=True)
            )
        return ExecutionResult(
            drafts=tuple(drafts),
            executor=self.name,
            image_digest=None,
            policy_sha256=None,
            duration_ms=_ms(start),
            stderr_tail="",
            fetched=fetched,
        )

    def drain(self) -> None:
        """Nothing runs in the background in process."""


# The installed gateway (0.1.7) refuses sandbox names longer than 19 characters.
MAX_SANDBOX_NAME = 19
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


def _base36(n: int) -> str:
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = _B36[r] + out
        if n == 0:
            return out


def agent_code(agent_id: str) -> str:
    """Two letters: initials of the agent name, or its first two letters if one word."""
    words = agent_id.split(".", 1)[1].split("_")
    return "".join(w[0] for w in words)[:2] if len(words) > 1 else words[0][:2]


def sandbox_name(agent_id: str, trigger_seq: int) -> str:
    """mer-<agent code>-<trigger seq in base 36>-<8 hex>, at most 19 characters.

    For example rbqm.kri_monitor on DataCut seq 275 gives mer-km-7n-1a2b3c4d. The seq is
    dropped if it would not fit (beyond seq 46655); the 8 hex keep names unique.
    """
    tail = uuid.uuid4().hex[:8]
    name = f"mer-{agent_code(agent_id)}-{_base36(trigger_seq)}-{tail}"
    return name if len(name) <= MAX_SANDBOX_NAME else f"mer-{agent_code(agent_id)}-{tail}"


class OpenShellExecutor:
    name = OPENSHELL

    def __init__(
        self,
        *,
        client: OpenShellClient,
        registry: AgentRegistry,
        study_root: Path,
        image: str,
        image_digest: str,
        policies_dir: Path,
        exec_timeout_seconds: int,
        keep_failed: bool,
        log: structlog.typing.FilteringBoundLogger,
        tracker: SandboxTracker,
        deleter: BackgroundDeleter,
        staging_dir: Path | None = None,
        name_factory: Callable[[str, int], str] = sandbox_name,
        safety_root: Path | None = None,
        etmf_root: Path | None = None,
        audit_root: Path | None = None,
        send_root: Path | None = None,
        noael_root: Path | None = None,
        inventory_root: Path | None = None,
        signal_root: Path | None = None,
        portfolio_root: Path | None = None,
        agent_timeouts: dict[str, int] | None = None,
    ) -> None:
        self._portfolio_root = portfolio_root
        self._noael_root = noael_root
        self._inventory_root = inventory_root
        self._signal_root = signal_root
        self._agent_timeouts = dict(agent_timeouts or {})
        self._send_root = send_root  # <send_root>/<study>: SEND cuts (preclinical zone)
        self._audit_root = audit_root  # <audit_root>/<trigger seq>/audit: one per review
        self._safety_root = safety_root  # <safety_root>/<study>: snapshots by cut id
        self._etmf_root = etmf_root  # <etmf_root>/<study>: eTMF snapshots by cut id
        self._client = client
        self._tracker = tracker
        self._deleter = deleter
        self._registry = registry
        self._study_root = study_root
        self._image = image
        self._image_digest = image_digest
        self._policies_dir = policies_dir
        self._timeout = exec_timeout_seconds
        self._keep_failed = keep_failed
        self._log = log
        self._staging_dir = staging_dir
        self._name_factory = name_factory

    def executor_name(self, agent_id: str) -> str:
        return self.name

    def capacity(self, executor: str) -> int:
        """One run at a time, as before phase 4a step 3."""
        return 1

    def plan(self, agent_id: str, trigger: Event) -> RunPlan:
        return RunPlan(self.name)

    def run(
        self, agent_id: str, trigger: Event, cut_id: str, plan: RunPlan | None = None
    ) -> ExecutionResult:
        start = time.perf_counter()
        registered = self._registry.get(agent_id)
        policy_name = policy_for_agent(agent_id)
        policy = ALL_POLICIES[policy_name]
        policy_file = policy_path(policy_name, self._policies_dir)
        name = self._name_factory(agent_id, trigger.seq)
        log = self._log.bind(agent_id=agent_id, trigger_seq=trigger.seq, sandbox=name)
        with tempfile.TemporaryDirectory(prefix="meridian-stage-", dir=self._staging_dir) as tmp:
            staged = stage_run(
                agent_id=agent_id,
                trigger=trigger,
                cut_id=cut_id,
                policy_name=policy_name,
                dest=Path(tmp),
                study_root=self._study_root,
                safety_root=self._safety_root,
                etmf_root=self._etmf_root,
                audit_root=self._audit_root,
                send_root=self._send_root,
                noael_root=self._noael_root,
                inventory_root=self._inventory_root,
                signal_root=self._signal_root,
                portfolio_root=self._portfolio_root,
            )
            stage = staged.stage
            inputs_sha = staged.inputs_manifest_sha256
            ok = False
            # Recorded before the call: a failed create or upload can leave it behind.
            self._tracker.record(name, "created")
            # Hashed from the bytes handed to create, so the record names that exact file.
            policy_file_sha256 = sha256_bytes(policy_file.read_bytes())
            try:
                self._client.create(name, self._image, policy_file, uploads(stage))
                timeout = self._agent_timeouts.get(agent_id, self._timeout)
                out = self._client.exec(name, self._argv(agent_id), timeout)
                effective = self._client.policy(name)
                stderr_tail = out.stderr[-STDERR_TAIL:]
                if effective.strip() != expected_effective(policy).strip():
                    raise ExecutionError(
                        f"sandbox {name} reports a policy that differs from {policy_file}"
                    )
                if out.exit_code != 0:
                    raise ExecutionError(
                        f"meridian-worker exited {out.exit_code} in {name}: {stderr_tail}"
                    )
                fetched: tuple[ResponseRecord, ...] = ()
                if agent_id == SIGNAL_AGENT:
                    drafts, fetched = self._parse_signal(
                        out.stdout, registered.agent.agent_id, registered.agent.version
                    )
                else:
                    drafts = self._parse(
                        out.stdout, registered.agent.agent_id, registered.agent.version
                    )
                ok = True
            except OpenShellError as exc:
                raise ExecutionError(f"openshell failed for {name}: {exc}") from exc
            finally:
                self._finish(name, ok, log)
        result = ExecutionResult(
            drafts=drafts,
            executor=self.name,
            image_digest=self._image_digest,
            policy_sha256=sha256_text(effective),
            duration_ms=_ms(start),
            stderr_tail=stderr_tail,
            inputs_manifest_sha256=inputs_sha,
            policy_file=policy_path(policy_name).as_posix(),
            policy_file_sha256=policy_file_sha256,
            fetched=fetched,
        )
        log.info("sandbox.run_done", duration_ms=result.duration_ms, drafts=len(drafts))
        return result

    def _argv(self, agent_id: str) -> list[str]:
        return [
            SIGNAL_WORKER if agent_id == SIGNAL_AGENT else WORKER,
            "run",
            "--agent",
            agent_id,
            "--trigger",
            SANDBOX_TRIGGER,
            "--cut-dir",
            SANDBOX_CUT_DIR,
        ]

    @staticmethod
    def _parse_signal(
        stdout: str, agent_id: str, version: str
    ) -> tuple[tuple[FindingDraft, ...], tuple[ResponseRecord, ...]]:
        """The signal worker's document: the drafts and every response it fetched. A live
        scan must have fetched (a replay runs in process, on the kept responses)."""
        try:
            doc = SignalDocument.loads(stdout)
        except (ValueError, ValidationError) as exc:
            raise ExecutionError(f"signal worker stdout is not a signal document: {exc}") from exc
        if (doc.agent_id, doc.agent_version) != (agent_id, version):
            raise ExecutionError(
                f"worker ran {doc.agent_id}@{doc.agent_version}, expected {agent_id}@{version}"
            )
        allowed = {"/drug/event.json", "/drug/label.json"}
        stray = sorted({r.path for r in doc.responses} - allowed)
        if stray:
            raise ExecutionError(f"the signal worker reports requests to {stray}")
        return doc.drafts, doc.responses

    @staticmethod
    def _parse(stdout: str, agent_id: str, version: str) -> tuple[FindingDraft, ...]:
        try:
            doc = DraftsDocument.loads(stdout)
        except (ValueError, ValidationError) as exc:
            raise ExecutionError(f"worker stdout is not a drafts document: {exc}") from exc
        if (doc.agent_id, doc.agent_version) != (agent_id, version):
            raise ExecutionError(
                f"worker ran {doc.agent_id}@{doc.agent_version}, expected {agent_id}@{version}"
            )
        return doc.drafts

    def _finish(self, name: str, ok: bool, log: structlog.typing.FilteringBoundLogger) -> None:
        if not ok and self._keep_failed:
            # Tracked as created and never delete_requested: meridian sandbox reap finds it.
            log.warning("sandbox.kept_for_debugging", sandbox=name)
            return
        self._deleter.submit(name)

    def drain(self) -> None:
        """Wait until every sandbox handed to the deleter is deleted or failed to delete."""
        report = self._deleter.drain()
        if report.failed:
            self._log.error("sandbox.deletes_failed", sandboxes=report.failed)
