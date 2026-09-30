"""Tests for the admin login gate.

The gate is fail-closed: every path needs a session except /v1/* (relay, which
has its own client keys) and the login page itself. These tests pin that down,
because the failure mode is silent — a forgotten exemption looks exactly like a
working app until you notice the endpoint was never protected.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from llm_evl.api.server import create_app, create_relay_app
from llm_evl.core.auth import hash_password
from conftest import (
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USER,
    login,
    provider_payload,
    write_configs,
)


@pytest.fixture
def app_paths(tmp_path):
    return write_configs(tmp_path, {"enabled": True},
                         [provider_payload("a", 9995)])


@pytest.fixture
def anon(app_paths):
    """A client that has NOT logged in.

    ``follow_redirects=False`` matters here: TestClient follows redirects by
    default, which would hide the 302-to-/login behaviour these tests assert.
    """
    tpath, rpath = app_paths
    with TestClient(create_app(tpath, rpath), follow_redirects=False) as c:
        yield c


# ------------------------------------------------------------------ the gate

class TestGateBlocks:
    def test_api_requires_login(self, anon):
        assert anon.get("/api/targets").status_code == 401
        assert anon.get("/api/providers").status_code == 401
        assert anon.get("/api/relay/config").status_code == 401

    def test_api_client_gets_json_not_a_login_page(self, anon):
        """A browser's fetch() sends Accept: */*, so it must get JSON it can
        act on — not an HTML login form it would try to parse as data."""
        r = anon.get("/api/targets")
        assert r.status_code == 401
        assert "not authenticated" in r.json()["detail"]

    def test_browser_navigation_gets_the_login_page(self, anon):
        r = anon.get("/api/targets", headers={"Accept": "text/html"})
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_browser_is_redirected_to_login(self, anon):
        r = anon.get("/", headers={"Accept": "text/html"})
        assert r.status_code == 302
        assert r.headers["location"] == "/login"

    def test_login_page_is_public(self, anon):
        r = anon.get("/login")
        assert r.status_code == 200
        assert "登录" in r.text

    def test_forged_cookie_rejected(self, anon):
        anon.cookies.set("llm_evl_session", "9999999999.admin.deadbeef")
        assert anon.get("/api/targets").status_code == 401

    def test_v1_is_not_gated_by_the_admin_login(self, anon):
        """The relay has its own key auth; the admin session is irrelevant."""
        r = anon.get("/v1/models")
        assert r.status_code != 302          # not redirected to /login
        assert r.status_code == 401          # refused, but by the key check
        assert r.json()["error"]["code"] == "no_client_keys"


class TestLogin:
    def test_correct_password_sets_session(self, anon):
        r = anon.post("/api/auth/login", json={
            "username": TEST_ADMIN_USER, "password": TEST_ADMIN_PASSWORD})
        assert r.status_code == 200
        assert "llm_evl_session" in r.cookies or "llm_evl_session" in anon.cookies
        # And it actually works.
        assert anon.get("/api/targets").status_code == 200

    def test_wrong_password_rejected(self, anon):
        r = anon.post("/api/auth/login", json={
            "username": TEST_ADMIN_USER, "password": "nope"})
        assert r.status_code == 401

    def test_wrong_username_rejected(self, anon):
        r = anon.post("/api/auth/login", json={
            "username": "root", "password": TEST_ADMIN_PASSWORD})
        assert r.status_code == 401

    def test_error_does_not_reveal_which_half_failed(self, anon):
        a = anon.post("/api/auth/login", json={"username": TEST_ADMIN_USER,
                                               "password": "bad"})
        b = anon.post("/api/auth/login", json={"username": "root",
                                               "password": TEST_ADMIN_PASSWORD})
        assert a.json()["detail"] == b.json()["detail"]

    def test_status_reports_authentication(self, anon):
        r = anon.get("/api/auth/status")
        assert r.status_code == 200
        assert r.json()["authenticated"] is False
        login(anon)
        assert anon.get("/api/auth/status").json()["authenticated"] is True

    def test_logout_clears_the_session(self, anon):
        login(anon)
        assert anon.get("/api/targets").status_code == 200
        assert anon.post("/api/auth/logout").status_code == 200
        assert anon.get("/api/targets").status_code == 401

    def test_login_page_redirects_when_already_signed_in(self, anon):
        login(anon)
        r = anon.get("/login", headers={"Accept": "text/html"})
        assert r.status_code == 302 and r.headers["location"] == "/"


class TestBootstrap:
    def test_password_is_generated_when_missing(self, tmp_path):
        rpath = tmp_path / "relay.yaml"
        rpath.write_text("relay:\n  enabled: false\n", encoding="utf-8")
        from llm_evl.api.auth_routes import ensure_admin
        pw = ensure_admin(str(rpath))
        assert pw and len(pw) >= 8
        import yaml
        doc = yaml.safe_load(rpath.read_text(encoding="utf-8"))
        # Hash is persisted, plaintext is not.
        assert doc["auth"]["admin"]["password_hash"].startswith("pbkdf2_sha256$")
        assert pw not in rpath.read_text(encoding="utf-8")
        assert doc["auth"]["session_secret"]

    def test_generated_password_actually_works(self, tmp_path):
        rpath = tmp_path / "relay.yaml"
        rpath.write_text("relay:\n  enabled: false\n", encoding="utf-8")
        from llm_evl.api.auth_routes import ensure_admin
        pw = ensure_admin(str(rpath))
        tpath = tmp_path / "targets.yaml"
        tpath.write_text("providers: []\n", encoding="utf-8")
        with TestClient(create_app(str(tpath), str(rpath))) as c:
            r = c.post("/api/auth/login", json={"username": "admin", "password": pw})
            assert r.status_code == 200

    def test_existing_hash_is_left_alone(self, tmp_path):
        rpath = tmp_path / "relay.yaml"
        rpath.write_text(
            f"relay:\n  enabled: false\nauth:\n  admin:\n    password_hash: {hash_password('x')}\n",
            encoding="utf-8")
        from llm_evl.api.auth_routes import ensure_admin
        assert ensure_admin(str(rpath)) is None

    def test_relay_save_does_not_clobber_the_auth_section(self, tmp_path):
        """Both sections live in relay.yaml; a relay save must not delete the
        password the owner is currently logged in with."""
        import yaml

        tpath, rpath = write_configs(tmp_path, {"enabled": True},
                                     [provider_payload("a", 9995)])
        with TestClient(create_app(tpath, rpath)) as c:
            login(c)
            c.post("/api/relay/config", json={"enabled": False, "strategy": "priority"})
            doc = yaml.safe_load(Path(rpath).read_text(encoding="utf-8"))
            assert doc["relay"]["strategy"] == "priority"
            assert doc["auth"]["admin"]["password_hash"]
            assert doc["auth"]["session_secret"]
            # Still logged in, i.e. the session secret survived too.
            assert c.get("/api/targets").status_code == 200


class TestEscapeHatch:
    def test_env_var_disables_the_gate(self, app_paths, monkeypatch):
        monkeypatch.setenv("LLM_EVL_NO_AUTH", "1")
        tpath, rpath = app_paths
        with TestClient(create_app(tpath, rpath)) as c:
            assert c.get("/api/targets").status_code == 200

    def test_off_by_default(self, anon, monkeypatch):
        monkeypatch.delenv("LLM_EVL_NO_AUTH", raising=False)
        assert anon.get("/api/targets").status_code == 401


class TestRelayOnlyApp:
    """The LAN-facing app must expose nothing but /v1."""

    @pytest.fixture
    def relay_client(self, app_paths):
        tpath, rpath = app_paths
        with TestClient(create_relay_app(tpath, rpath)) as c:
            yield c

    def test_v1_present(self, relay_client):
        assert relay_client.get("/v1/models").status_code == 401

    def test_no_management_endpoints(self, relay_client):
        # The control surface can mint client keys; it must not exist here.
        for path in ("/api/targets", "/api/providers", "/api/relay/config",
                     "/api/relay/clients", "/api/auth/status", "/api/auth/login"):
            assert relay_client.get(path).status_code == 404, path

    def test_cannot_mint_a_key_from_the_lan_port(self, relay_client):
        r = relay_client.post("/api/relay/clients", json={"name": "intruder"})
        assert r.status_code == 404
        r = relay_client.post("/v1/chat/completions", json={
            "model": "shared-model", "messages": [{"role": "user", "content": "x"}]})
        assert r.status_code == 401

    def test_no_ui(self, relay_client):
        # A port scanner hitting the relay port must not find the dashboard.
        r = relay_client.get("/")
        assert r.status_code == 404
        assert "text/html" not in r.headers.get("content-type", "")

    def test_no_login_needed_to_reach_key_check(self, relay_client):
        # It answers 401 (key required) rather than 302 (login required):
        # an OpenAI SDK must never be redirected to an HTML form.
        r = relay_client.get("/v1/models")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "no_client_keys"
