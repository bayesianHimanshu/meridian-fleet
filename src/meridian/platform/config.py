"""Settings loaded from config/default.toml, overridable by MERIDIAN_ environment variables.

Precedence, highest first: constructor arguments, environment variables, the TOML file,
field defaults. The TOML file path defaults to config/default.toml relative to the
working directory and can be changed with MERIDIAN_CONFIG_FILE. Nested values use a
double underscore in environment variables, for example MERIDIAN_OPS_SYNTH__ENTRY_LAG_SIGMA.

MERIDIAN_CONFIG_OVERLAY names a second TOML file whose tables are merged over the first
(phase 4b step 3: deploy/gke/meridian.toml points [substrate] at the GKE cluster).
"""

import os
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

from meridian.data.ops_synth import OpsSynthParams
from meridian.platform.agent_settings import (
    AuditTrailReviewerConfig,
    CsmConfig,
    DoseTranslationConfig,
    IndReadinessConfig,
    KriMonitorConfig,
    PipelineIntelligenceConfig,
    QtlDetectorConfig,
    SaeReconcilerConfig,
    SignalScoutConfig,
    StageGatePackConfig,
    TmfCompletenessConfig,
    ToxStudyMonitorConfig,
    TranslationalBridgeConfig,
)

CONFIG_FILE_ENV = "MERIDIAN_CONFIG_FILE"
CONFIG_OVERLAY_ENV = "MERIDIAN_CONFIG_OVERLAY"
_SHA256 = re.compile(r"[0-9a-f]{64}")
DEFAULT_CONFIG_FILE = Path("config/default.toml")

DEFAULT_FINDING_ID_PREFIXES: dict[str, str] = {
    "rbqm": "RBQM",
    "safety": "SAF",
    "quality": "QA",
    "preclinical": "PRE",
    "portfolio": "SIG",  # phase 5 step 3a: portfolio.safety_signal_scout
    "tmf": "TMF",
    "unblinded": "DSMB",
    "human": "OBS",  # findings raised by a person (QA observations), not an agent
}

PILOT_BASE_URL = (
    "https://github.com/cdisc-org/sdtm-adam-pilot-project/raw/master/"
    "updated-pilot-submission-package/900172/m5/datasets/cdiscpilot01/tabulations/sdtm"
)


def config_file_path() -> Path:
    return Path(os.environ.get(CONFIG_FILE_ENV, str(DEFAULT_CONFIG_FILE)))


GKE_ENV_FILE = Path("deploy/gke/env.sh")
_PLACEHOLDER = re.compile(r"\$\{([A-Z][A-Z0-9_]*)\}")


class MissingPlaceholderError(ValueError):
    pass


def env_file_values(path: Path) -> dict[str, str]:
    """KEY=value lines of a shell env file (export allowed, quotes stripped), non-empty only."""
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(
            r"""^\s*(?:export\s+)?([A-Z][A-Z0-9_]*)=["']?([^"'#]*?)["']?\s*(?:#.*)?$""", line
        )
        if m and m.group(2).strip():
            out[m.group(1)] = m.group(2).strip()
    return out


def expand_placeholders(value: object, lookup: dict[str, str], source: str) -> object:
    """${NAME} in every string of a TOML document, from lookup; a missing name is an error
    that names it (and never shows a value)."""
    if isinstance(value, dict):
        return {k: expand_placeholders(v, lookup, source) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_placeholders(v, lookup, source) for v in value]
    if isinstance(value, str):

        def one(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in lookup:
                raise MissingPlaceholderError(
                    f"{source} needs ${{{name}}}: set {name} in {GKE_ENV_FILE} (copied from "
                    "deploy/gke/env.example.sh) or in the environment"
                )
            return lookup[name]

        return _PLACEHOLDER.sub(one, value)
    return value


class OverlayTomlSource(TomlConfigSettingsSource):
    """The overlay, with ${NAME} filled from the environment, else deploy/gke/env.sh."""

    def _read_file(self, file_path: Path) -> dict[str, object]:  # type: ignore[override]
        data = super()._read_file(file_path)
        lookup = {**env_file_values(GKE_ENV_FILE), **os.environ}
        expanded = expand_placeholders(data, lookup, str(file_path))
        assert isinstance(expanded, dict)
        return expanded


def config_overlay_path() -> Path | None:
    value = os.environ.get(CONFIG_OVERLAY_ENV)
    return Path(value) if value else None


class PilotSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = PILOT_BASE_URL
    domains: tuple[str, ...] = ("dm", "ae", "vs", "lb", "ds", "sv", "ex")
    timeout_seconds: float = 120.0


POINTCROSS_COMMIT = "398a6d33ced9359ffb58c46650a6d488811176b1"
POINTCROSS_BASE_URL = (
    "https://raw.githubusercontent.com/phuse-org/phuse-scripts/"
    f"{POINTCROSS_COMMIT}/data/send/PointCross"
)


class PreclinicalSettings(BaseModel):
    """MRD-101's GLP rat study ([preclinical] in TOML; docs/preclinical-data.md)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    asset_id: str = "MRD-101"
    study_id: str = "MRD-101-TOX-01"
    cut_id: str = "TOX-FINAL"
    treatment: str = "MRD-101"
    source_study_id: str = "PC201708"
    source_treatment: str = "PCDRUG"
    base_url: str = POINTCROSS_BASE_URL
    raw_dir: Path = Path("data/raw/send_pointcross")
    send_root: Path = Path("data/send")
    # Phase 5 step 2: the NOAEL decisions staged for preclinical.dose_translation, one
    # folder per trigger (<noael_root>/<trigger seq>/noael), written by the host from the
    # ledger; and the nonclinical inventory cuts (<inventory_root>/<asset>/<cut>/inventory).
    noael_root: Path = Path("var/preclinical/noael")
    inventory_root: Path = Path("data/inventory_cuts")
    inventory_file: Path = Path("data/inventory/MRD-101/inventory.csv")
    timeout_seconds: float = 120.0
    # Domain to the sha256 of its .xpt at the pinned commit; fetch and ingest refuse others.
    files: dict[str, str] = Field(default_factory=dict)


class AgentsSettings(BaseModel):
    """Which agents run, and each agent's own configuration ([agents.<name>] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: tuple[str, ...] = ("rbqm.kri_monitor", "rbqm.qtl_detector", "rbqm.csm")
    kri_monitor: KriMonitorConfig = Field(default_factory=KriMonitorConfig)
    qtl_detector: QtlDetectorConfig
    csm: CsmConfig = Field(default_factory=CsmConfig)
    sae_reconciler: SaeReconcilerConfig = Field(default_factory=SaeReconcilerConfig)
    tmf_completeness: TmfCompletenessConfig = Field(default_factory=TmfCompletenessConfig)
    audit_trail_reviewer: AuditTrailReviewerConfig = Field(default_factory=AuditTrailReviewerConfig)
    tox_study_monitor: ToxStudyMonitorConfig = Field(default_factory=ToxStudyMonitorConfig)
    dose_translation: DoseTranslationConfig = Field(default_factory=DoseTranslationConfig)
    ind_readiness: IndReadinessConfig = Field(default_factory=IndReadinessConfig)
    translational_bridge: TranslationalBridgeConfig = Field(
        default_factory=TranslationalBridgeConfig
    )
    safety_signal_scout: SignalScoutConfig = Field(default_factory=SignalScoutConfig)
    pipeline_intelligence: PipelineIntelligenceConfig = Field(
        default_factory=PipelineIntelligenceConfig
    )
    stage_gate_pack: StageGatePackConfig = Field(default_factory=StageGatePackConfig)

    @model_validator(mode="before")
    @classmethod
    def _bridge_uses_the_dose_config(cls, data: object) -> object:
        """translational_bridge's margins use [agents.dose_translation] itself: the same
        factor and citation as the proposal, never a copy that could drift."""
        if isinstance(data, dict) and "dose_translation" in data:
            bridge = dict(data.get("translational_bridge") or {})
            if "dose" in bridge:
                raise ValueError("set the conversion in [agents.dose_translation] only")
            bridge["dose"] = data["dose_translation"]
            data = {**data, "translational_bridge": bridge}
        return data


class RuntimeSettings(BaseModel):
    """Where agents run ([runtime] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # openshell by default; inprocess only when asked for, and only for evaluation cuts
    # ([cuts] operational_executors). zoned: each policy's executor from `executors`.
    executor: Literal["inprocess", "openshell", "zoned"] = "openshell"
    # With executor = "zoned": the executor of each committed policy (rbqm,
    # safety.sae_reconciler, ...). A policy not listed runs in openshell. Only a policy
    # without network rules may name substrate (meridian.sandbox.zoned checks it).
    executors: dict[str, Literal["openshell", "substrate"]] = Field(default_factory=dict)
    # The agent image; empty means meridian-agent:<package version>.
    image: str = ""
    exec_timeout_seconds: int = Field(120, ge=1)
    # Keep a failed sandbox (and log its name) instead of deleting it.
    keep_failed: bool = False
    policies_dir: Path = Path("policies")
    # Phase 5 step 3a: a longer sandbox exec timeout for an agent that needs it (the signal
    # scout makes about 250 paced requests to openFDA). Agent id to seconds.
    agent_timeouts: dict[str, int] = Field(default_factory=dict)
    # Background sandbox deletion: thread pool size; 0 deletes synchronously (step 1).
    delete_workers: int = Field(4, ge=0, le=32)
    # A deleted sandbox must leave `openshell sandbox list` within this many seconds.
    delete_confirm_seconds: float = Field(60.0, gt=0)
    # Append-only record of every sandbox this code creates and its teardown.
    sandboxes_log: Path = Path("var/sandboxes.jsonl")
    # meridian sandbox conformance: how long to poll openshell logs for the denial line.
    conformance_log_timeout_seconds: float = Field(30.0, gt=0)


class LlmSettings(BaseModel):
    """The model the narrator calls ([llm] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str = "claude-haiku-5-5"
    max_tokens: int = Field(300, ge=1)
    # None leaves temperature out of the request. claude-haiku-5-5 refuses it: HTTP 400
    # "`temperature` is deprecated for this model." (measured 2026-11-07).
    temperature: float | None = None
    disable_thinking: bool = True
    timeout_seconds: float = Field(30.0, gt=0)


class LocalModelSettings(BaseModel):
    """The local model on the host ([local_model] in TOML), for the safety_local zone.

    Reached only from the safety.local_model sandbox. base_url must name the address and
    port in that policy (a test checks it). A model is pinned by digest: a job refuses to
    run if Ollama serves a different digest for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = "http://172.17.0.1:11434"
    # The local narrator's model (and the single model of the term pair evaluation).
    model: str = "phi4-mini:3.8b-q4_K_M"
    # safety.term_adjudicator 1.1.0 asks each of these the same question (agreement rule).
    adjudicator_models: tuple[str, ...] = ("phi4-mini:3.8b-q4_K_M", "qwen3.5:4b-q4_K_M")
    # Model name to the digest /api/tags reports for it.
    digests: dict[str, str] = Field(default_factory=dict)
    num_ctx: int = Field(4096, ge=256)
    seed: int = 20261109
    temperature: float = Field(0.0, ge=0.0)
    num_predict: int = Field(256, ge=1)
    think: bool = False
    timeout_seconds: float = Field(120.0, gt=0)
    max_calls_per_pass: int = Field(200, ge=1)
    # Requests per sandbox. One exec of 77 calls (about 12 minutes, nothing on stdout until
    # the end) was cut by the gateway: "exec relay closed before the command reported an
    # exit status". A pass is split into jobs of at most this many, one sandbox each.
    max_per_job: int = Field(16, ge=1)

    @model_validator(mode="after")
    def _pinned(self) -> LocalModelSettings:
        for model in (self.model, *self.adjudicator_models):
            if model not in self.digests:
                raise ValueError(f"local model {model} has no pinned digest")
        if len(set(self.adjudicator_models)) < 2:
            raise ValueError("the agreement rule needs two or more different models")
        for name, digest in self.digests.items():
            if not _SHA256.fullmatch(digest):
                raise ValueError(f"digest of {name} is not 64 hex characters")
        return self

    def pinned(self, model: str | None = None) -> str:
        return self.digests[model or self.model]


class NarratorSettings(BaseModel):
    """rbqm.narrator ([narrator] in TOML)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Model calls per runner pass; findings over budget wait for the next pass.
    max_calls_per_pass: int = Field(20, ge=1)


class CutsSettings(BaseModel):
    """Which cuts are the study's operational data ([cuts] in TOML).

    Every other cut (null, faulted, holdout, sweep) is an evaluation cut. Kept here, not
    inferred from cut ids.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    operational: tuple[str, ...] = ("DC-BASE",)
    # The only executors that may process an operational cut: the runner refuses any
    # other (InProcessExecutor runs agent code in this process, outside any sandbox).
    operational_executors: tuple[Literal["openshell", "substrate"], ...] = (
        "openshell",
        "substrate",
    )


class GkeSettings(BaseModel):
    """The GKE cluster of phase 4b ([substrate.gke]; deploy/gke/meridian.toml). Names only:
    every credential comes from gcloud's own login."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # No defaults: the project's identifiers are never committed. deploy/gke/meridian.toml
    # fills them from ${PROJECT_ID} and ${PROJECT_NUMBER} (deploy/gke/env.sh or the
    # environment), and SubstrateSettings refuses target "gke" without them.
    project_id: str = ""
    project_number: str = ""
    region: str = "us-central1"
    zone: str = "us-central1-a"
    cluster: str = "meridian-substrate"
    bucket: str = ""
    ar_repo: str = "meridian"
    gcloud: str = "gcloud"

    @property
    def registry(self) -> str:
        return f"{self.region}-docker.pkg.dev/{self.project_id}/{self.ar_repo}"

    def workload_principal(self, namespace: str, service_account: str) -> str:
        """A Kubernetes ServiceAccount's IAM principal under Workload Identity Federation."""
        return (
            f"principal://iam.googleapis.com/projects/{self.project_number}/locations/global/"
            f"workloadIdentityPools/{self.project_id}.svc.id.goog/subject/ns/{namespace}/"
            f"sa/{service_account}"
        )


class SubstrateSettings(BaseModel):
    """Agent Substrate on the local kind cluster ([substrate] in TOML), or on GKE with
    target = "gke" (deploy/gke/meridian.toml, through MERIDIAN_CONFIG_OVERLAY).

    version and commit pin the release the control plane was deployed from (the runner
    refuses to start if ate-setup's install record names another version); the worker
    image and the agent image are pinned by digest in deploy/substrate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    target: Literal["kind", "gke"] = "kind"
    gke: GkeSettings = Field(default_factory=GkeSettings)
    version: str = "v1.0.0"
    commit: str = Field("b34fbee742906494b5eb4f1f36987176bf8770a4", pattern=r"^[0-9a-f]{40}$")
    install_record: Path = Path("~/.cache/ate-setup/installs/kind-kind.yaml")
    context: str = "kind-kind"
    kubectl: str = "kubectl"
    atespace: str = "meridian"
    namespace: str = "meridian"  # the Kubernetes namespace of the WorkerPool
    worker_pool: str = "meridian"
    workers: int = Field(2, ge=1, le=32)  # WorkerPool replicas: one actor per worker
    # Each worker pod requests and is limited to worker_memory_mib; each actor to
    # actor_memory_mib, which must fit what a worker offers (64 MiB less).
    worker_memory_mib: int = Field(1024, ge=256, le=8192)
    actor_memory_mib: int = Field(768, ge=128, le=8128)
    # ateworker-gvisor built with ko from the pinned release (docs/substrate-eval.md).
    worker_image: str = Field(
        "localhost:5001/ateworker-gvisor@sha256:"
        "ad6957f41bccaa8bb5f3eb5b34b7812babb862f1a3187a11578c21a7d799491c",
        pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$",
    )
    sandbox_class: Literal["gvisor"] = "gvisor"
    # Phase 4b step 3: the WorkerPool's pods go only to nodes with this label and
    # tolerate this taint (key=value:effect). On GKE both are on the Spot node pool
    # (docs/gke-plan.md 6.5); on kind the node carries the label, and the taint only for
    # `meridian substrate placement-check` (one node: a lasting taint would keep every
    # other pod from being scheduled again). The taint is Substrate's own convention for a
    # node pool reserved to one sandbox class, ate.dev/sandboxClass=<class>: the atelet
    # DaemonSet tolerates that key for any value, and a worker without atelet on its node
    # never registers (step 4: a taint of our own kept atelet off the Spot node).
    worker_node_label: str = Field("meridian.dev/pool=substrate", pattern=r"^[\w./-]+=[\w.-]+$")
    worker_taint: str = Field(
        "ate.dev/sandboxClass=gvisor:NoSchedule",
        pattern=r"^ate\.dev/sandboxClass=[\w.-]+:NoSchedule$",
    )
    registry: str = "localhost:5001"
    deploy_dir: Path = Path("deploy/substrate")
    port: int = Field(8080, ge=1024, le=65535)  # what meridian-worker serve listens on
    request_timeout_seconds: float = Field(180.0, gt=0)
    actors_log: Path = Path("var/actors.jsonl")
    api_audience: str = "api.ate-system.svc"  # the audience ate-api-server accepts
    # A run that finds no free worker (ResourceExhausted) waits and retries this long.
    placement_wait_seconds: float = Field(120.0, ge=0)
    # Phase 4b. The laptop sends each run to the in-cluster dispatcher through one
    # long-lived port-forward and holds no Substrate identity (since step 3 the runner
    # ServiceAccount of phase 4a is gone; its measurements stay in docs/gke-plan.md 2a).
    # The dispatcher runs in its own namespace as its own ServiceAccount, the only member
    # of the atespace's access policy and of atenet-router's ingress allow-list; its image
    # is pinned by digest in dispatcher_deploy_dir. The laptop's half of the shared token
    # lives outside the repository (dispatcher_token_file, 0600).
    dispatcher_namespace: str = "meridian-dispatch"
    dispatcher_service_account: str = "dispatcher"
    dispatcher_deployment: str = "meridian-dispatcher"
    dispatcher_port: int = Field(8090, ge=1024, le=65535)
    dispatcher_token_file: Path = Path("~/.config/meridian/dispatcher.token")
    dispatcher_deploy_dir: Path = Path("deploy/dispatcher")
    # The CONNECT listener of atenet-router and ate-api-server's in-cluster address.
    router_address: str = "https://atenet-router.ate-system.svc:8444"
    api_endpoint: str = "api.ate-system.svc:443"
    # A run is retried once on a new actor when its worker was lost (Spot preemption).
    worker_lost_retries: int = Field(1, ge=0, le=1)

    @model_validator(mode="after")
    def _gke_needs_its_project(self) -> SubstrateSettings:
        if self.target == "gke":
            missing = [
                name
                for name, value in (
                    ("PROJECT_ID", self.gke.project_id),
                    ("PROJECT_NUMBER", self.gke.project_number),
                    ("the snapshot bucket", self.gke.bucket),
                )
                if not value
            ]
            if missing:
                raise ValueError(
                    "target gke needs " + ", ".join(missing) + ": set them in deploy/gke/env.sh "
                    "(copied from deploy/gke/env.example.sh) or in the environment"
                )
        return self

    @property
    def node_selector(self) -> dict[str, str]:
        key, value = self.worker_node_label.split("=", 1)
        return {key: value}

    @property
    def toleration(self) -> dict[str, str]:
        pair, effect = self.worker_taint.rsplit(":", 1)
        key, value = pair.split("=", 1)
        return {"key": key, "operator": "Equal", "value": value, "effect": effect}


class ApiSettings(BaseModel):
    """The Fleet API ([api] in TOML). It always binds to 127.0.0.1."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    port: int = Field(8765, ge=1024, le=65535)
    people_file: Path = Path("config/people.toml")
    portfolio_file: Path = Path("config/portfolio.toml")
    openapi_file: Path = Path("openapi/meridian-api.json")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MERIDIAN_", env_nested_delimiter="__", extra="ignore", frozen=True
    )

    ledger_path: Path = Path("var/ledger.sqlite3")
    default_seed: int = 0
    finding_id_prefixes: dict[str, str] = Field(
        default_factory=lambda: dict(DEFAULT_FINDING_ID_PREFIXES)
    )
    study_id: str = "MRD-201"
    raw_dir: Path = Path("data/raw/cdisc_pilot")
    curated_root: Path = Path("data/curated")
    sites_file: Path = Path("config/sites.toml")
    ground_truth_dir: Path = Path("data/ground_truth")
    # The synthetic safety database: one snapshot per cut (meridian.safetydb).
    safety_root: Path = Path("data/safety_db")
    # The synthetic eTMF index: one snapshot per cut (meridian.tmf).
    etmf_root: Path = Path("data/etmf")
    # Audit extracts of the ledger, one folder per review (<audit_root>/<trigger seq>/audit).
    audit_root: Path = Path("var/audit")
    # Phase 5 step 3a: each signal scan's openFDA responses, kept as the run's inputs
    # (<signal_root>/<trigger seq>/signal/openfda_responses.parquet).
    signal_root: Path = Path("var/signals")
    # Phase 5 step 3b: the portfolio facts staged for a review or a gate request, built by
    # the host as the portfolio lead (<portfolio_root>/<trigger seq>/portfolio).
    portfolio_root: Path = Path("var/portfolio")
    evals_dir: Path = Path("data/evals")
    pilot: PilotSource = Field(default_factory=PilotSource)
    preclinical: PreclinicalSettings = Field(default_factory=PreclinicalSettings)
    ops_synth: OpsSynthParams = Field(default_factory=OpsSynthParams)
    agents: AgentsSettings
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    narrator: NarratorSettings = Field(default_factory=NarratorSettings)
    local_model: LocalModelSettings | None = None
    api: ApiSettings = Field(default_factory=ApiSettings)
    cuts: CutsSettings = Field(default_factory=CutsSettings)
    substrate: SubstrateSettings = Field(default_factory=SubstrateSettings)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        overlay = config_overlay_path()
        if overlay is not None and not overlay.is_file():
            raise FileNotFoundError(f"{CONFIG_OVERLAY_ENV} names {overlay}, which does not exist")
        files = [TomlConfigSettingsSource(settings_cls, toml_file=config_file_path())]
        if overlay is not None:  # earlier sources win; tables are merged key by key
            files.insert(0, OverlayTomlSource(settings_cls, toml_file=overlay))
        return (init_settings, env_settings, *files)
