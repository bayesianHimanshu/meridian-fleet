"""Cut scope, superseded findings, enrollment, people and the portfolio (phase 3 step 2)."""

import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from meridian.domain.enums import Role
from meridian.domain.events import DataCut
from tests.agent_support import clean
from tests.api_support import STUDY, Fixture, blinded_finding, build, headers
from tests.rbqm_support import write_tables_cut

OPS = headers(Role.CLINICAL_OPS_LEAD)


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    """The standard fixture plus an evaluation cut with one finding, and an older version
    of RBQM-0001's agent that raised the same (cut, kind, site, check)."""
    fx = build(tmp_path)
    fx.append(DataCut(cut_id="DC-RBQM-CORE", study_id=STUDY, path="curated/MRD-201/DC-RBQM-CORE"))
    fx.raise_finding(
        blinded_finding("1016", "Site 1016: more protocol deviations (evaluation)", "eval"),
        cut="DC-RBQM-CORE",
    )
    fx.raise_finding(
        blinded_finding(
            "1016",
            "Site 1016: more protocol deviations (old version)",
            "old",
            agent_version="2.1.0",
        ),
    )
    return fx


def _get(client: TestClient, path: str, h: dict[str, str] = OPS) -> Any:
    r = client.get(path, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def _ids(body: Any) -> list[str]:
    return [i["finding_id"] for i in body["items"]]


def test_cuts_carry_their_scope_from_config(fx: Fixture) -> None:
    cuts = {c["cut_id"]: c["scope"] for c in _get(fx.client(), f"/api/v1/studies/{STUDY}/cuts")}
    assert cuts["DC-BASE"] == "operational"
    assert cuts["DC-RBQM-CORE"] == "evaluation"
    assert set(cuts.values()) == {"operational", "evaluation"}


def test_findings_default_to_operational_and_not_superseded(fx: Fixture) -> None:
    client = fx.client()
    assert _ids(_get(client, "/api/v1/findings")) == ["RBQM-0002", "RBQM-0001"]
    widened = _get(client, "/api/v1/findings?scope=all&include_superseded=true")
    by_id = {i["finding_id"]: i for i in widened["items"]}
    assert set(by_id) == {"RBQM-0001", "RBQM-0002", "RBQM-0003", "RBQM-0004"}
    assert by_id["RBQM-0003"]["scope"] == "evaluation"
    old = by_id["RBQM-0004"]
    assert (old["agent_version"], old["superseded"], old["superseded_by"]) == (
        "2.1.0",
        True,
        "RBQM-0001",
    )
    assert (by_id["RBQM-0001"]["superseded"], by_id["RBQM-0001"]["superseded_by"]) == (False, None)
    assert _ids(_get(client, "/api/v1/findings?scope=evaluation")) == ["RBQM-0003"]
    assert _ids(_get(client, "/api/v1/findings?include_superseded=true")) == [
        "RBQM-0004",
        "RBQM-0002",
        "RBQM-0001",
    ]


def test_the_review_queue_never_shows_evaluation_cuts_by_default(fx: Fixture) -> None:
    client = fx.client()
    # Even an escalated evaluation finding stays out of the default queue.
    r = client.post(
        "/api/v1/findings/RBQM-0003/decisions", json={"action": "escalated"}, headers=OPS
    )
    assert r.status_code == 201
    for role in (Role.CLINICAL_OPS_LEAD, Role.MEDICAL_MONITOR, Role.QA_AUDITOR):
        queue = _get(client, "/api/v1/review-queue", headers(role))["items"]
        assert [i["scope"] for i in queue] == ["operational", "operational"]
        assert all(not i["superseded"] for i in queue)
        assert "RBQM-0003" not in json.dumps(queue)
    wide = _get(client, "/api/v1/review-queue?scope=all")["items"]
    assert "RBQM-0003" in [i["finding_id"] for i in wide]
    portfolio = _get(client, "/api/v1/portfolio")["assets"]
    (mrd201,) = [a for a in portfolio if a["asset_id"] == STUDY]
    assert mrd201["live"] == {"open_findings": 2}  # the default queue, not every cut


def test_enrollment_is_cumulative_and_has_no_unblinded_columns(tmp_path: Path) -> None:
    fx = build(tmp_path / "l")
    blinded, ops, unblinded = clean()
    write_tables_cut(tmp_path / "curated", "DC-SITES", blinded, ops, unblinded)
    fx.append(DataCut(cut_id="DC-SITES", study_id=STUDY, path="curated/MRD-201/DC-SITES"))
    client = fx.client(curated_root=tmp_path / "curated")
    body = _get(client, f"/api/v1/studies/{STUDY}/enrollment?cut=DC-SITES")
    assert set(body) == {"study_id", "cut_id", "months", "total", "sites"}
    assert all(set(s) == {"site_id", "cumulative"} for s in body["sites"])
    text = json.dumps(body)
    for column in ("ARM", "ACTARM", "ARMCD", "USUBJID", "AGE", "SEX", "unblinded"):
        assert column not in text
    total = body["total"]
    assert total == sorted(total)  # cumulative
    treated = blinded["dm"].filter(blinded["dm"]["RFSTDTC"] != "").height
    assert total[-1] == treated
    assert [
        sum(col) for col in zip(*(s["cumulative"] for s in body["sites"]), strict=True)
    ] == total
    assert all(len(m) == 7 and m[4] == "-" for m in body["months"])
    # The default cut is the operational one, which has no data in this fixture.
    assert client.get(f"/api/v1/studies/{STUDY}/enrollment", headers=OPS).status_code == 404
    assert (
        client.get(
            f"/api/v1/studies/{STUDY}/enrollment?cut=DC-SITES", headers=headers(Role.PORTFOLIO_LEAD)
        ).status_code
        == 403
    )


def test_people_lists_the_six_demo_people_without_headers(fx: Fixture) -> None:
    r = fx.client().get("/api/v1/people")
    assert r.status_code == 200
    assert r.headers["X-Meridian-Auth"] == "development"
    people = r.json()
    assert [p["actor"] for p in people] == [
        "asha.rao",
        "karan.mehta",
        "nina.fernandes",
        "rahul.iyer",
        "leela.nair",
        "omar.siddiqui",
    ]
    assert {p["role"] for p in people} == {r.value for r in Role}


def test_the_portfolio_has_the_mock_assets_and_mrd201s_real_indication(fx: Fixture) -> None:
    assets = {a["asset_id"]: a for a in _get(fx.client(), "/api/v1/portfolio")["assets"]}
    assert assets["MRD-101"]["static"]["title"] == "Oral ALK5 inhibitor"
    assert assets["MRD-101"]["static"]["indication"] == "Fibrosis"
    assert assets["MRD-110"]["static"]["indication"] == "Healthy volunteers"
    assert assets["MRD-301"]["static"]["indication"] == "Ulcerative colitis"
    mrd201 = assets["MRD-201"]["static"]
    assert mrd201["indication"] == "Mild to moderate Alzheimer's disease"
    assert mrd201["intervention"] == "Xanomeline transdermal"
    assert "pulmonary" not in json.dumps(assets).lower()


def test_the_ledger_pages_newest_first(fx: Fixture) -> None:
    client = fx.client()
    head = len(fx.events)
    first = _get(client, "/api/v1/ledger?order=desc&limit=3")
    assert [e["seq"] for e in first["entries"]] == [head, head - 1, head - 2]
    assert (first["next_since"], first["next_before"]) == (None, head - 2)
    second = _get(client, f"/api/v1/ledger?order=desc&limit=3&before={first['next_before']}")
    assert [e["seq"] for e in second["entries"]] == [head - 3, head - 4, head - 5]
    last = _get(client, "/api/v1/ledger?order=desc&limit=1000&before=3")
    assert ([e["seq"] for e in last["entries"]], last["next_before"]) == ([2, 1], None)
    # Redaction is the same in either order.
    qa = headers(Role.QA_AUDITOR)
    redacted = [
        e["seq"]
        for e in _get(client, "/api/v1/ledger?order=desc&limit=1000", qa)["entries"]
        if "marker" in e
    ]
    assert redacted
    assert all(
        "payload" not in e
        for e in _get(client, "/api/v1/ledger?order=desc&limit=1000", qa)["entries"]
        if e["seq"] in redacted
    )


def test_fleet_containment_lists_its_checks(fx: Fixture) -> None:
    from meridian.domain.events import ContainmentCheck, ContainmentCheckCompleted

    fx.append(
        ContainmentCheckCompleted(
            zone="safety",
            sandbox="mer-cf-sa-00000000",
            image="meridian-agent:0.1.0",
            image_digest="sha256:" + "a" * 64,
            policy_sha256="b" * 64,
            checks=(
                ContainmentCheck(
                    name="write_var_tmp",
                    expect="refused",
                    command=": > /var/tmp/x",
                    outcome="refused",
                    exit_code=1,
                    message="Permission denied",
                    passed=True,
                ),
            ),
            log_line_found=True,
            all_refused=True,
            passed=True,
        )
    )
    containment = {c["policy"]: c for c in _get(fx.client(), "/api/v1/fleet")["containment"]}
    assert containment["safety"]["checks"] == [
        {
            "name": "write_var_tmp",
            "expect": "refused",
            "outcome": "refused",
            "passed": True,
            "message": "Permission denied",
        }
    ]
    assert containment["rbqm"]["checks"] == []


def test_rbqm_sites_carry_the_qtl(tmp_path: Path) -> None:
    fx = build(tmp_path / "l")
    blinded, ops, unblinded = clean()
    write_tables_cut(tmp_path / "curated", "DC-SITES", blinded, ops, unblinded)
    fx.append(DataCut(cut_id="DC-SITES", study_id=STUDY, path="curated/MRD-201/DC-SITES"))
    qtl = _get(fx.client(curated_root=tmp_path / "curated"), "/api/v1/rbqm/DC-SITES/sites")["qtl"]
    assert qtl["name"] == "premature_discontinuation"
    assert qtl["limit"] == 0.617
    assert qtl["approach_from"] == pytest.approx(0.597)
    assert qtl["treated"] > 0
    assert qtl["discontinued"] / qtl["treated"] == pytest.approx(qtl["rate"])
    assert qtl["status"] in ("within", "approaching", "breached")
    thresholds = _get(fx.client(curated_root=tmp_path / "curated"), "/api/v1/rbqm/DC-SITES/sites")[
        "thresholds"
    ]
    assert thresholds == {
        "data_entry_lag_days": 7.0,
        "open_query_age_days": 21.0,
        "csm_log_sd_z": -3.5,
        "kri_alpha": 0.01,
    }
