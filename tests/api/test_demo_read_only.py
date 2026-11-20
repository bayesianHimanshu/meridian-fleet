"""The demo's read-only mode (docs/demo/README.md): every write is refused with 403 before
any route runs, the ledger file is unchanged, and each refusal is logged with its time."""

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meridian.api.app import MODE_HEADER, READ_ONLY_MODE, create_app
from meridian.domain.enums import Role
from tests.api_support import Fixture, build, headers

WRITES = [
    ("/api/v1/findings/RBQM-0001/decisions", {"action": "accepted"}, Role.CLINICAL_OPS_LEAD),
    ("/api/v1/quality/observations", {"title": "t", "summary": "s"}, Role.QA_AUDITOR),
    ("/api/v1/preclinical/MRD-101-TOX-01/noael", {"sex": "M"}, Role.TOXICOLOGIST),
    ("/api/v1/portfolio/reviews", {}, Role.PORTFOLIO_LEAD),
    ("/api/v1/portfolio/gates", {"asset_id": "MRD-101", "gate": "x"}, Role.PORTFOLIO_LEAD),
    ("/api/v1/portfolio/gates/1/decision", {}, Role.PORTFOLIO_LEAD),
]


@pytest.fixture
def fx(tmp_path: Path) -> Fixture:
    return build(tmp_path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_every_write_endpoint_is_refused_and_logged(fx: Fixture, tmp_path: Path) -> None:
    log = tmp_path / "refusals.jsonl"
    client = TestClient(create_app(fx.context(), read_only_log=log))
    ledger_file = fx.context().ledger_path
    before = _sha(ledger_file)
    for url, body, role in WRITES:
        r = client.post(url, json=body, headers=headers(role))
        assert r.status_code == 403, (url, r.text)
        assert r.headers[MODE_HEADER] == READ_ONLY_MODE
        assert "read-only demo mode" in r.json()["detail"]
    for method in ("put", "patch", "delete"):
        assert getattr(client, method)("/api/v1/ledger").status_code == 403
    assert _sha(ledger_file) == before
    rows = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(rows) == len(WRITES) + 3
    assert rows[0]["method"] == "POST"
    assert rows[0]["path"] == WRITES[0][0]
    assert rows[0]["actor"] == "asha.rao"
    assert rows[0]["at"]


def test_reads_still_work_in_read_only_mode(fx: Fixture, tmp_path: Path) -> None:
    client = TestClient(create_app(fx.context(), read_only_log=tmp_path / "r.jsonl"))
    r = client.get("/api/v1/review-queue", headers=headers(Role.CLINICAL_OPS_LEAD))
    assert r.status_code == 200
    assert r.headers[MODE_HEADER] == READ_ONLY_MODE
    assert client.get("/api/v1/ledger/verify", headers=headers(Role.QA_AUDITOR)).json()["ok"]
    assert not (tmp_path / "r.jsonl").exists()


def test_without_the_flag_a_write_is_not_refused_by_the_mode(fx: Fixture) -> None:
    r = fx.client().post(WRITES[0][0], json=WRITES[0][1], headers=headers(Role.CLINICAL_OPS_LEAD))
    assert r.status_code == 201
    assert MODE_HEADER not in r.headers


@pytest.mark.parametrize(
    "who",
    [
        {"X-Meridian-Actor": "operator:demo-recorder"},
        headers(Role.CLINICAL_OPS_LEAD),
        {},
    ],
    ids=["operator", "person", "no identity"],
)
def test_the_gate_refuses_whoever_asks(fx: Fixture, tmp_path: Path, who: dict[str, str]) -> None:
    """The demo's proof write does not depend on who sends it: the gate runs before
    identity is checked, so an operator, a person and no identity at all all get 403."""
    log = tmp_path / "r.jsonl"
    client = TestClient(create_app(fx.context(), read_only_log=log))
    before = _sha(fx.context().ledger_path)
    r = client.post(WRITES[0][0], json=WRITES[0][1], headers=who)
    assert r.status_code == 403
    assert r.json()["detail"] == "read-only demo mode: nothing can be recorded"
    assert _sha(fx.context().ledger_path) == before
    (row,) = [json.loads(line) for line in log.read_text().splitlines()]
    assert row["actor"] == who.get("X-Meridian-Actor")


def test_the_recorder_identity_is_an_operator_not_a_person() -> None:
    from meridian.demo.pipeline import RECORDER
    from meridian.domain.identity import is_operator, operator_id, people_ids

    assert RECORDER == operator_id("demo-recorder") == "operator:demo-recorder"
    assert is_operator(RECORDER)
    assert RECORDER not in people_ids(Path("config/people.toml"))
