"""Agent Substrate pieces that need no cluster: the zoned executor's choice, the committed
templates, the conformance classification, the snapshot decoder and the extract columns."""

from compression import zstd
from pathlib import Path

import pytest

from meridian.domain import audit as a
from meridian.domain.events import ActorSuspended, SubstrateRun
from meridian.platform.config import Settings
from meridian.sandbox import substrate_conformance as sc
from meridian.sandbox import substrate_proofs as sp
from meridian.sandbox.executor import ExecutionError, RunPlan
from meridian.sandbox.substrate_templates import (
    CONFORMANCE,
    POOL_FILE,
    SUBSTRATE_POLICIES,
    PoolSpec,
    committed_image,
    committed_name,
    render_pool,
    render_template,
    substrate_eligible,
    template_name,
    template_path,
)
from meridian.sandbox.zoned import ZonedExecutor

REPO = Path(__file__).resolve().parents[2]
DEPLOY = REPO / "deploy" / "substrate"


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name
        self.ran: list[str] = []

    def executor_name(self, agent_id: str) -> str:
        return self.name

    def plan(self, agent_id: str, trigger: object) -> RunPlan:
        return RunPlan(self.name)

    def run(self, agent_id: str, trigger: object, cut_id: str, plan: object = None) -> object:
        self.ran.append(agent_id)
        return None

    def drain(self) -> None:
        return None


def test_the_zoned_executor_sends_each_policy_where_config_says() -> None:
    openshell, substrate = _Named("openshell"), _Named("substrate")
    zoned = ZonedExecutor(
        executors={"openshell": openshell, "substrate": substrate},  # type: ignore[dict-item]
        choice=Settings().runtime.executors,
        default="openshell",
    )
    assert zoned.executor_name("rbqm.kri_monitor") == "substrate"
    assert zoned.executor_name("safety.sae_reconciler") == "substrate"
    assert zoned.executor_name("quality.tmf_completeness") == "substrate"
    assert zoned.executor_name("quality.audit_trail_reviewer") == "substrate"
    assert zoned.executor_name("rbqm.narrator") == "openshell"
    assert zoned.executor_name("safety.term_adjudicator") == "openshell"
    with pytest.raises(ExecutionError, match="planned for openshell"):
        zoned.run("rbqm.csm", None, "DC-BASE", RunPlan("openshell"))  # type: ignore[arg-type]


@pytest.mark.parametrize("policy", ["rbqm.narrator", "safety.local_model"])
def test_a_policy_with_network_rules_may_not_choose_substrate(policy: str) -> None:
    assert not substrate_eligible(policy)
    with pytest.raises(ExecutionError, match="network rules"):
        ZonedExecutor(
            executors={"openshell": _Named("openshell"), "substrate": _Named("substrate")},  # type: ignore[dict-item]
            choice={policy: "substrate"},
            default="openshell",
        )


def test_the_configured_substrate_policies_are_the_four_without_network() -> None:
    chosen = {p for p, e in Settings().runtime.executors.items() if e == "substrate"}
    assert chosen == set(SUBSTRATE_POLICIES)
    assert all(substrate_eligible(p) for p in chosen)


@pytest.mark.parametrize("policy", [*SUBSTRATE_POLICIES, CONFORMANCE])
def test_the_committed_templates_render_from_the_code(policy: str) -> None:
    path = template_path(DEPLOY, policy)
    image = committed_image(path)
    settings = Settings().substrate
    assert path.read_text(encoding="utf-8") == render_template(
        policy, settings.atespace, image, settings.actor_memory_mib
    )
    if policy != CONFORMANCE:
        assert committed_name(path) == template_name(policy, image, settings.actor_memory_mib)


def test_the_committed_pool_renders_from_the_code() -> None:
    s = Settings().substrate
    pool = PoolSpec(
        s.namespace, s.worker_pool, s.workers, s.worker_image, s.sandbox_class, s.worker_memory_mib
    )
    assert (DEPLOY / POOL_FILE).read_text(encoding="utf-8") == render_pool(pool)


def test_all_committed_templates_name_one_image() -> None:
    images = {committed_image(template_path(DEPLOY, p)) for p in (*SUBSTRATE_POLICIES, CONFORMANCE)}
    assert len(images) == 1


def _answer(**kw: object) -> dict[str, object]:
    return {"outcome": "ok", "message": "m", **kw}


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (_answer(connected=True, closed=True, reply=None, reset=104), "refused"),
        (_answer(connected=True, reply="HTTP/1.1 400 Bad Request", server=None), "refused"),
        (_answer(connected=True, reply="HTTP/1.1 200 OK", server="ollama"), "allowed"),
        (_answer(connected=True, reply="HTTP/1.1 400 Bad Request", server="nginx"), "allowed"),
        ({"outcome": "refused", "connected": False, "message": "ECONNREFUSED"}, "refused"),
        ({"outcome": "error", "connected": False, "message": "?"}, "inconclusive"),
    ],
)
def test_a_network_check_counts_only_the_gateways_refusal(
    answer: dict[str, object], outcome: str
) -> None:
    spec = sc.specs("10.0.0.9")[0]
    assert sc.classify(spec, answer, None).outcome == outcome


def test_the_conformance_checks_cover_the_three_required_targets() -> None:
    targets = {s.target for s in sc.specs("10.244.0.9") if s.kind == sc.NETWORK}
    assert {"192.0.2.1:443", "172.17.0.1:11434", "10.244.0.9:8080"} <= targets


def test_the_sparse_snapshot_format_is_decoded() -> None:
    """magic, u32 version, then one zstd stream (Substrate pkg/objectstorage/sparsezstd.go)."""
    payload = (2 * 4096).to_bytes(8, "little") + b"MERIDIAN-CANARY-x" * 300
    data = sp.SPARSE_MAGIC + (2).to_bytes(4, "little") + zstd.compress(payload)
    assert b"MERIDIAN-CANARY-x" * 3 not in data  # compressed: only the decoder can see it
    assert sp._decompressed("pages.img.zstd", data) == [data, payload]
    report = sp.SearchReport("t")
    report.scan("pages.img.zstd", data, b"MERIDIAN-CANARY-x" * 3)
    assert report.hits == ["pages.img.zstd (decompressed)"]
    assert report.decompressed == 1


def test_the_bucket_layout_flags_actor_snapshots_and_unknown_tags() -> None:
    keys = [
        "atespaces/meridian/tags/g1/pages.img.zstd",
        "atespaces/meridian/tags/old/pages.img.zstd",
        "atespaces/meridian/actors/u1/snapshots/s1/pages.img.zstd",
    ]
    layout = sp.bucket_layout(keys, "meridian", {"g1"})
    assert layout["outside_tags"] == [keys[2]]
    assert layout["non_golden_tags"] == ["old"]


def test_the_canary_is_planted_verbatim(tmp_path: Path) -> None:
    import polars as pl

    path = tmp_path / "ae.parquet"
    pl.DataFrame({"AETERM": ["HEADACHE", "NAUSEA"]}).write_parquet(path)
    canary = sp.new_canary()
    sp.plant(path, "AETERM", canary)
    assert canary.encode() in path.read_bytes()
    assert pl.read_parquet(path)["AETERM"].to_list() == [canary, "NAUSEA"]


def test_the_extract_records_a_substrate_runs_template_and_golden_snapshot() -> None:
    assert {a.TEMPLATE_SHA256, a.GOLDEN_SNAPSHOT} <= set(a.COLUMNS)
    run = SubstrateRun(
        version="v1.0.0",
        commit="b" * 40,
        atespace="meridian",
        template="rbqm-x",
        template_sha256="7" * 64,
        template_file="deploy/substrate/templates/rbqm.yaml",
        template_file_sha256="6" * 64,
        image="localhost:5001/meridian-agent@sha256:" + "c" * 64,
        worker_pool="meridian/meridian",
        sandbox_class="gvisor",
        actor="mer-x",
        golden_tag="t",
        golden_snapshot="g",
    )
    done = ActorSuspended(agent_id="rbqm.csm", trigger_seq=1, executor="substrate", substrate=run)
    assert done.substrate is not None
    assert done.substrate.golden_snapshot == "g"
