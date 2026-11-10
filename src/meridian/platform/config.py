"""Settings loaded from config/default.toml, overridable by MERIDIAN_ environment variables.

Precedence, highest first: constructor arguments, environment variables, the TOML file,
field defaults. The TOML file path defaults to config/default.toml relative to the
working directory and can be changed with MERIDIAN_CONFIG_FILE. Nested values use a
double underscore in environment variables, for example MERIDIAN_OPS_SYNTH__ENTRY_LAG_SIGMA.
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
    KriMonitorConfig,
    QtlDetectorConfig,
    SaeReconcilerConfig,
    TmfCompletenessConfig,
)

CONFIG_FILE_ENV = "MERIDIAN_CONFIG_FILE"
_SHA256 = re.compile(r"[0-9a-f]{64}")
DEFAULT_CONFIG_FILE = Path("config/default.toml")

DEFAULT_FINDING_ID_PREFIXES: dict[str, str] = {
    "rbqm": "RBQM",
    "safety": "SAF",
    "quality": "QA",
    "preclinical": "PRE",
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


class PilotSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    base_url: str = PILOT_BASE_URL
    domains: tuple[str, ...] = ("dm", "ae", "vs", "lb", "ds", "sv", "ex")
    timeout_seconds: float = 120.0


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


class SubstrateSettings(BaseModel):
    """Agent Substrate on the local kind cluster ([substrate] in TOML).

    version and commit pin the release the control plane was deployed from (the runner
    refuses to start if ate-setup's install record names another version); the worker
    image and the agent image are pinned by digest in deploy/substrate.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: str = "v1.0.0"
    commit: str = Field("b34fbee742906494b5eb4f1f36987176bf8770a4", pattern=r"^[0-9a-f]{40}$")
    install_record: Path = Path("~/.cache/ate-setup/installs/kind-kind.yaml")
    context: str = "kind-kind"
    kubectl: str = "kubectl"
    atespace: str = "meridian"
    namespace: str = "meridian"  # the Kubernetes namespace of the WorkerPool
    worker_pool: str = "meridian"
    workers: int = Field(2, ge=1, le=16)  # WorkerPool replicas: one actor per worker
    # ateworker-gvisor built with ko from the pinned release (docs/substrate-eval.md).
    worker_image: str = Field(
        "localhost:5001/ateworker-gvisor@sha256:"
        "ad6957f41bccaa8bb5f3eb5b34b7812babb862f1a3187a11578c21a7d799491c",
        pattern=r"^[^@\s]+@sha256:[0-9a-f]{64}$",
    )
    sandbox_class: Literal["gvisor"] = "gvisor"
    registry: str = "localhost:5001"
    deploy_dir: Path = Path("deploy/substrate")
    port: int = Field(8080, ge=1024, le=65535)  # what meridian-worker serve listens on
    request_timeout_seconds: float = Field(180.0, gt=0)
    actors_log: Path = Path("var/actors.jsonl")
    # The runner's own identity: a ServiceAccount in `namespace` with `editor` on the
    # atespace only (deploy/substrate/access-policy.yaml), and a short-lived token for it.
    runner_service_account: str = "meridian-runner"
    runner_token_file: Path = Path("var/substrate/runner.token")
    api_audience: str = "api.ate-system.svc"  # the audience ate-api-server accepts
    # A run that finds no free worker (ResourceExhausted) waits and retries this long.
    placement_wait_seconds: float = Field(120.0, ge=0)


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
    evals_dir: Path = Path("data/evals")
    pilot: PilotSource = Field(default_factory=PilotSource)
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
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls, toml_file=config_file_path()),
        )
