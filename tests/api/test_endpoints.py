"""Traceability, the read endpoints, the committed OpenAPI document."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meridian.cli import openapi_text
from meridian.domain.enums import Role
from meridian.domain.events import DataCut
from meridian.platform.config import Settings
from tests.agent_support import clean, plant
from tests.api_support import (
    IMAGE_DIGEST,
    MANIFEST_SHA,
    POLICY_SHA,
    REPO_ROOT,
    STUDY,
    Fixture,
    build,
    headers,
)
from tests.rbqm_support import write_tables_cut

OPS = headers(Role.CLINICAL_OPS_LEAD)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build(tmp_path)


def _get(client: TestClient, path: str, h: dict[str, str] = OPS) -> Any:
    r = client.get(path, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_sandboxed_finding_traces_to_its_image_policy_and_inputs(fx: Fixture) -> None:
    detail = _get(fx.client(), "/api/v1/findings/RBQM-0001")
    run = detail["run"]
    assert (run["agent_id"], run["agent_version"], run["cut_id"]) == (
        "rbqm.kri_monitor",
        "2.2.0",
        "DC-BASE",
    )
    assert run["woke"]["executor"] == "openshell"
    assert run["woke"]["seq"] < detail["finding"]["seq"] < run["suspended"]["seq"]
    suspended = run["suspended"]
    assert suspended["executor"] == "openshell"
    assert suspended["image_digest"] == IMAGE_DIGEST
    assert suspended["policy_sha256"] == POLICY_SHA
    assert suspended["inputs_manifest_sha256"] == MANIFEST_SHA
    assert detail["evidence"][0]["display"] == "18 observed (5.1 expected)"
    assert detail["narratives"][0]["narrator_version"] == "1.1.0"
    assert detail["finding"]["latest_narrative"]["headline"].startswith("Site 1016")


def test_findings_filter_sort_and_page(fx: Fixture) -> None:
    client = fx.client()
    all_ = _get(client, "/api/v1/findings")
    assert [i["finding_id"] for i in all_["items"]] == ["RBQM-0002", "RBQM-0001"]  # newest first
    assert _get(client, "/api/v1/findings?site=1016")["total"] == 1
    assert _get(client, "/api/v1/findings?cut=DC-BASE&agent=rbqm.kri_monitor")["total"] == 2
    assert _get(client, "/api/v1/findings?severity=low")["total"] == 0
    assert _get(client, "/api/v1/findings?status=accepted")["total"] == 0
    page = _get(client, "/api/v1/findings?limit=1&offset=1")
    assert ([i["finding_id"] for i in page["items"]], page["total"]) == (["RBQM-0001"], 2)
    assert client.get("/api/v1/findings?severity=extreme", headers=OPS).status_code == 422
    one = all_["items"][1]
    assert (one["status"], one["decision_count"]) == ("open", 0)
    assert one["latest_narrative"]["narrator_version"] == "1.1.0"


def test_the_review_queue_holds_open_and_escalated_newest_first(fx: Fixture) -> None:
    client = fx.client()
    client.post("/api/v1/findings/RBQM-0002/decisions", json={"action": "accepted"}, headers=OPS)
    client.post("/api/v1/findings/RBQM-0001/decisions", json={"action": "escalated"}, headers=OPS)
    queue = _get(client, "/api/v1/review-queue")["items"]
    assert [(i["finding_id"], i["status"]) for i in queue] == [("RBQM-0001", "escalated")]
    assert queue[0]["severity"] == "high"


def test_studies_and_verify(fx: Fixture) -> None:
    client = fx.client()
    assert _get(client, "/api/v1/studies") == [{"study_id": STUDY, "cuts": 2}]
    assert client.get("/api/v1/studies/NOPE/cuts", headers=OPS).status_code == 404
    for role in Role:
        report = _get(client, "/api/v1/ledger/verify", headers(role))
        assert report["ok"] is True
        assert report["events_checked"] == len(fx.events)


def test_fleet_lists_agents_policies_runs_and_containment(fx: Fixture) -> None:
    fleet = _get(fx.client(), "/api/v1/fleet")
    by_id = {a["agent_id"]: a for a in fleet["agents"]}
    assert sorted(by_id) == [
        "rbqm.csm",
        "rbqm.kri_monitor",
        "rbqm.narrator",
        "rbqm.qtl_detector",
        "safety.sae_reconciler",
    ]
    assert by_id["safety.sae_reconciler"]["policy_file"].endswith("safety.sae_reconciler.yaml")
    kri = by_id["rbqm.kri_monitor"]
    expected_sha = (
        __import__("hashlib")
        .sha256((REPO_ROOT / "policies" / "rbqm.yaml").read_bytes())
        .hexdigest()
    )
    assert (kri["version"], kri["policy_file"].endswith("rbqm.yaml"), kri["policy_sha256"]) == (
        "2.2.0",
        True,
        expected_sha,
    )
    assert (kri["last_outcome"], kri["last_trigger_seq"]) == ("completed", 1)
    assert by_id["rbqm.csm"]["last_run_at"] is None
    narr = by_id["rbqm.narrator"]
    assert (
        narr["role"],
        narr["policy_file"].endswith("rbqm.narrator.yaml"),
        narr["last_outcome"],
    ) == ("narrator", True, "accepted")
    assert [(c["policy"], c["passed"]) for c in fleet["containment"]] == [("rbqm", True)]


def test_portfolio_marks_static_and_live_fields(fx: Fixture) -> None:
    assets = _get(fx.client(), "/api/v1/portfolio")["assets"]
    assert [a["asset_id"] for a in assets] == ["MRD-101", "MRD-110", "MRD-201", "MRD-301"]
    assert [a["static"]["stage"] for a in assets] == [
        "preclinical",
        "phase_1",
        "phase_2",
        "phase_3",
    ]
    assert [a["live"] for a in assets] == [None, None, {"open_findings": 2}, None]


def test_rbqm_sites_from_the_agents_stats(tmp_path: Path) -> None:
    fx = build(tmp_path / "l")
    blinded, ops, unblinded = clean()
    blinded, ops = plant(blinded, ops, "entry_lag_spike", "1003", {"target_median_days": 14.0})
    write_tables_cut(tmp_path / "curated", "DC-SITES", blinded, ops, unblinded)
    fx.append(DataCut(cut_id="DC-SITES", study_id=STUDY, path="curated/MRD-201/DC-SITES"))
    from tests.api_support import blinded_finding

    fx.raise_finding(
        blinded_finding("1003", "Site 1003: data entry lag above threshold", "lag"),
        cut="DC-SITES",
        check="data_entry_lag",
    )
    client = fx.client(curated_root=tmp_path / "curated")
    body = _get(client, "/api/v1/rbqm/DC-SITES/sites")
    sites = {s["site_id"]: s for s in body["sites"]}
    assert body["parameters"] == ["SYSBP", "DIABP", "PULSE"]
    assert sites["1003"]["median_entry_lag_days"] == pytest.approx(14.0, abs=1.0)
    assert sites["1003"]["flagged_checks"] == ["data_entry_lag"]
    assert all(s["flagged_checks"] == [] for k, s in sites.items() if k != "1003")
    assert set(sites["1001"]["csm_log_sd_z"]) == {"SYSBP", "DIABP", "PULSE"}
    assert sites["1001"]["treated"] > 0
    assert (
        client.get("/api/v1/rbqm/DC-SITES/sites", headers=headers(Role.PORTFOLIO_LEAD)).status_code
        == 403
    )
    assert client.get("/api/v1/rbqm/DC-NONE/sites", headers=OPS).status_code == 404


def test_the_committed_openapi_document_is_current() -> None:
    committed = (REPO_ROOT / "openapi" / "meridian-api.json").read_text(encoding="utf-8")
    assert committed == openapi_text(Settings()), "run `meridian api openapi` and commit it"
    schema = json.loads(committed)
    assert "/api/v1/findings/{finding_id}/decisions" in schema["paths"]
