"""Development identity: fail closed, and say so on every response."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from meridian.domain.enums import Role
from tests.api_support import PEOPLE, build, headers


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    return build(tmp_path).client()


@pytest.mark.parametrize(
    "sent",
    [
        {},
        {"X-Meridian-Actor": "asha.rao"},
        {"X-Meridian-Role": "clinical_ops_lead"},
        {"X-Meridian-Actor": "nobody", "X-Meridian-Role": "clinical_ops_lead"},
        {"X-Meridian-Actor": "asha.rao", "X-Meridian-Role": "unblinded_statistician"},
        {"X-Meridian-Actor": "asha.rao", "X-Meridian-Role": "CLINICAL_OPS_LEAD"},
        {"X-Meridian-Actor": "", "X-Meridian-Role": ""},
    ],
    ids=["none", "no role", "no actor", "unknown actor", "role mismatch", "case", "empty"],
)
def test_unknown_or_mismatched_callers_get_401(client: TestClient, sent: dict[str, str]) -> None:
    for path in ("/api/v1/whoami", "/api/v1/findings", "/api/v1/ledger", "/api/v1/ledger/verify"):
        response = client.get(path, headers=sent)
        assert response.status_code == 401, path
        assert response.headers["X-Meridian-Auth"] == "development"
    post = client.post(
        "/api/v1/findings/RBQM-0001/decisions", json={"action": "accepted"}, headers=sent
    )
    assert post.status_code == 401


@pytest.mark.parametrize("role", list(Role))
def test_each_demo_person_is_who_the_file_says(client: TestClient, role: Role) -> None:
    response = client.get("/api/v1/whoami", headers=headers(role))
    assert response.status_code == 200
    assert response.json()["actor"] == PEOPLE[role]
    assert response.json()["role"] == role.value
    assert response.json()["auth"] == "development"


def test_every_response_carries_the_development_auth_header(client: TestClient) -> None:
    assert client.get("/api/v1/health").headers["X-Meridian-Auth"] == "development"
    ok = client.get("/api/v1/findings", headers=headers(Role.CLINICAL_OPS_LEAD))
    assert ok.headers["X-Meridian-Auth"] == "development"
    missing = client.get("/api/v1/findings/RBQM-9999", headers=headers(Role.CLINICAL_OPS_LEAD))
    assert missing.status_code == 404
    assert missing.headers["X-Meridian-Auth"] == "development"


def test_the_openapi_description_says_this_is_not_authentication(client: TestClient) -> None:
    schema = client.get("/api/openapi.json").json()
    assert "not real authentication" in schema["info"]["description"]


def test_cors_allows_only_the_vite_dev_server(client: TestClient) -> None:
    preflight = {
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "X-Meridian-Actor, X-Meridian-Role",
    }
    for origin in ("http://127.0.0.1:5173", "http://localhost:5173"):
        r = client.options("/api/v1/findings", headers={"Origin": origin, **preflight})
        assert r.headers.get("access-control-allow-origin") == origin
    r = client.options("/api/v1/findings", headers={"Origin": "http://evil.example", **preflight})
    assert "access-control-allow-origin" not in r.headers
