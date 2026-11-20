"""preclinical.translational_bridge through the runner and the API (phase 5 step 3a): it
wakes on the acceptance of a dose proposal, maps the NOAEL's findings to monitoring with
margins against the accepted dose, the medical monitor alone decides it, a revised NOAEL
supersedes it, and the Translational panel and the Portfolio show it."""

from typing import Any

from fastapi.testclient import TestClient

from meridian.domain.enums import Role
from meridian.preclinical.synth import STUDY as TOX
from tests.api.test_dose_handoff import _asset, _noael, _proposals, client, fx, run
from tests.api_support import Fixture, headers

__all__ = ["fx"]
BRIDGE = "preclinical.translational_bridge"


def _alt_ids(c: TestClient) -> list[str]:
    body = c.get(f"/api/v1/preclinical/{TOX}/noael", headers=headers(Role.TOXICOLOGIST)).json()
    return [x["finding_id"] for x in body["candidates"] if x["title"].startswith("ALT")]


def _recs(c: TestClient, role: Role, **query: Any) -> list[dict[str, Any]]:
    params = {"agent": BRIDGE, "limit": 500, **query}
    items: list[dict[str, Any]] = c.get(
        "/api/v1/findings", params=params, headers=headers(role)
    ).json()["items"]
    return items


def _accepted_proposal(fx: Fixture) -> TestClient:
    c = client(fx)
    _noael(c, finding_ids=_alt_ids(c))
    run(fx, {"preclinical.dose_translation"})
    c = client(fx)
    (p,) = _proposals(c, Role.MEDICAL_MONITOR)
    r = c.post(
        f"/api/v1/findings/{p['finding_id']}/decisions",
        json={"action": "accepted", "reason": "Provisional starting dose."},
        headers=headers(Role.MEDICAL_MONITOR),
    )
    assert r.status_code == 201
    return c


def test_the_bridge_wakes_on_the_acceptance_and_maps_alt_to_the_liver(fx: Fixture) -> None:
    c = _accepted_proposal(fx)
    assert _asset(c, Role.PORTFOLIO_LEAD, "MRD-101")["monitoring"]["status"] == "none"
    report = run(fx, {BRIDGE})
    (done,) = report.completed  # one run: on the acceptance, not also on the NOAEL
    c = client(fx)
    (rec,) = _recs(c, Role.MEDICAL_MONITOR)
    assert rec["kind"] == "monitoring_recommendation"
    assert rec["title"].endswith("first-in-human study: liver")
    panel = c.get(
        f"/api/v1/preclinical/{TOX}/translational", headers=headers(Role.TOXICOLOGIST)
    ).json()
    assert panel["status"] == "recommended"
    (item,) = panel["current"]["items"]
    assert item["organ"] == "liver"
    assert item["measure"].startswith("ALT, AST, ALP and total bilirubin")
    assert {s["source"] for s in item["sources"]} == {"FDA 2009 DILI"}
    assert any("ALT or AST >8xULN" in s["quote"] for s in item["sources"])
    assert {s["section"] for s in item["sources"]} == {"IV.A.2", "IV.A.3", "IV.A.4", "IV.A.5"}
    assert sorted(item["findings"]) == sorted(_alt_ids(c))
    margins = {m["label"]: m for m in panel["current"]["margins"]}
    assert round(margins["NOAEL"]["ratio"], 6) == 10.0
    assert round(margins["LOAEL"]["ratio"], 6) == 30.0  # LOAEL 30 / NOAEL 10, x 10
    assert "accepted by karan.mehta" in panel["current"]["starting_dose"]
    # The medical monitor alone decides it.
    url = f"/api/v1/findings/{rec['finding_id']}/decisions"
    decision = {"action": "accepted", "reason": "Monitoring plan adopted for the protocol."}
    for role in (Role.TOXICOLOGIST, Role.PORTFOLIO_LEAD, Role.QA_AUDITOR):
        assert c.post(url, json=decision, headers=headers(role)).status_code == 403, role
    assert c.post(url, json=decision, headers=headers(Role.MEDICAL_MONITOR)).status_code == 201
    brief = _asset(c, Role.PORTFOLIO_LEAD, "MRD-101")["monitoring"]
    assert (brief["status"], brief["organs"], brief["decided_by"]) == (
        "accepted",
        ["Liver"],
        "karan.mehta",
    )
    for role in (Role.CLINICAL_OPS_LEAD, Role.UNBLINDED_STATISTICIAN):
        assert _recs(c, role) == []
        assert _asset(c, role, "MRD-101")["monitoring"]["status"] == "not_shown"
    assert done[1] > 0


def test_a_revised_noael_gives_a_new_recommendation_that_supersedes(fx: Fixture) -> None:
    c = _accepted_proposal(fx)
    run(fx, {BRIDGE})
    c = client(fx)
    (old,) = _recs(c, Role.MEDICAL_MONITOR)
    current = c.get(f"/api/v1/preclinical/{TOX}/noael", headers=headers(Role.TOXICOLOGIST))
    first = current.json()["current"][0]["decision_id"]
    _noael(c, noael_dose=30.0, loael_dose=100.0, supersedes=first, finding_ids=_alt_ids(c))
    run(fx, {"preclinical.dose_translation", BRIDGE})
    c = client(fx)
    (new,) = _recs(c, Role.MEDICAL_MONITOR)
    assert new["finding_id"] != old["finding_id"]
    everything = {
        r["finding_id"]: r for r in _recs(c, Role.MEDICAL_MONITOR, include_superseded=True)
    }
    assert everything[old["finding_id"]]["superseded_by"] == new["finding_id"]
    panel = c.get(
        f"/api/v1/preclinical/{TOX}/translational", headers=headers(Role.TOXICOLOGIST)
    ).json()
    assert panel["superseded"] == [old["finding_id"]]
    # The run on the revised NOAEL: its extract was written before dose_translation's new
    # proposal existed, and the old proposal rests on a superseded decision, so there is no
    # starting dose to compare with until the new proposal is accepted (the next trigger).
    assert [m["ratio"] for m in panel["current"]["margins"]] == [None, None]
    assert panel["current"]["starting_dose"].startswith("No planned starting dose")
