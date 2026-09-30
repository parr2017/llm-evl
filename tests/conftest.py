"""Shared test fixtures.

The management API sits behind the admin login, so any test that talks to
``/api/*`` needs a session. Rather than disabling the gate globally (which
would make the auth tests meaningless), tests authenticate the same way a
browser does: POST /api/auth/login and keep the cookie.
"""

from __future__ import annotations

import pytest
import yaml
from fastapi.testclient import TestClient

from llm_evl.core.auth import hash_password
from llm_evl.api.server import create_app, create_relay_app

TEST_ADMIN_USER = "admin"
TEST_ADMIN_PASSWORD = "test-password-123"
TEST_SESSION_SECRET = "test-session-secret-0123456789"


def provider_payload(name: str, port: int, models=("shared-model",)) -> dict:
    return {
        "name": name,
        "base_url": f"http://127.0.0.1:{port}/v1",
        "api_key": "test-key",
        "models": [{"name": m} for m in models],
    }


def write_configs(tmp_path, relay: dict | None = None,
                  providers: list[dict] | None = None,
                  *, with_auth: bool = True) -> tuple[str, str]:
    """Write a targets.yaml + relay.yaml pair and return both paths."""
    tpath = tmp_path / "targets.yaml"
    tpath.write_text(
        yaml.safe_dump({"providers": providers or [provider_payload("a", 9995)]}),
        encoding="utf-8",
    )
    doc: dict = {"relay": relay if relay is not None else {"enabled": True}}
    if with_auth:
        doc["auth"] = {
            "admin": {
                "username": TEST_ADMIN_USER,
                "password_hash": hash_password(TEST_ADMIN_PASSWORD),
            },
            "session_secret": TEST_SESSION_SECRET,
            "session_hours": 12,
        }
    rpath = tmp_path / "relay.yaml"
    rpath.write_text(yaml.safe_dump(doc), encoding="utf-8")
    return str(tpath), str(rpath)


def login(client: TestClient) -> TestClient:
    """Sign in and return the same client (its cookie jar now holds a session)."""
    resp = client.post("/api/auth/login", json={
        "username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASSWORD,
    })
    assert resp.status_code == 200, resp.text
    return client


def make_key(client: TestClient, name: str = "tester", groups: list[str] | None = None) -> str:
    """Create a relay client key through the real API and return the plaintext."""
    resp = client.post("/api/relay/clients", json={"name": name, "groups": groups or []})
    assert resp.status_code == 200, resp.text
    return resp.json()["key"]


def auth_header(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture
def mgmt(tmp_path):
    """A management TestClient with a known admin password, already logged in."""
    tpath, rpath = write_configs(tmp_path)
    with TestClient(create_app(tpath, rpath)) as client:
        yield client
        from llm_evl.api.relay_manager import relay_manager
        relay_manager.log.clear()
        relay_manager.breaker.reset()


@pytest.fixture
def mgmt_paths(tmp_path):
    """The config paths, for tests that need to inspect the files themselves."""
    return write_configs(tmp_path)


@pytest.fixture
def relay_app(tmp_path):
    """The LAN-facing app: /v1 only, no management endpoints."""
    tpath, rpath = write_configs(tmp_path)
    return create_relay_app(tpath, rpath)
