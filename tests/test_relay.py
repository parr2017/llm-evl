"""Tests for the relay layer: routing, retries, circuit breaking, call log, API.

The tests that matter most here are the negative ones — "a 400 is NOT retried",
"a cooling provider is skipped", "a stream is forwarded byte-for-byte", "a
request without a key is refused". A relay that retries everything, rewrites an
upstream answer, or lets anonymous callers spend the operator's money is worse
than no relay at all.
"""

import asyncio
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
import yaml
from fastapi.testclient import TestClient

from llm_evl.api.relay_manager import relay_manager
from llm_evl.api.server import create_app, create_relay_app
from llm_evl.core.auth import find_client, hash_key
from llm_evl.core.models import Provider, ProviderModel
from llm_evl.core.relay import (
    UNSET_PRIORITY,
    UNSET_WEIGHT,
    CircuitBreaker,
    ProbeResult,
    ModelPool,
    NoAvailableMember,
    PoolNotFound,
    RelayCall,
    RelayConfig,
    RelayDisabled,
    RelayLog,
    RelayMember,
    RelayService,
    Router,
    build_model_pool,
    load_relay_config,
    probe_members,
    save_auth_config,
    rank_members,
    save_relay_config,
)
from conftest import (  # noqa: E402  (path set up below)
    TEST_ADMIN_PASSWORD,
    TEST_ADMIN_USER,
    auth_header,
    login as _login,
    make_key as _make_key,
    provider_payload,
    write_configs,
)

# Make the mock server importable from tests/.
sys.path.insert(0, str(Path(__file__).parent))
from mock_llm_server import make_app  # noqa: E402

# An SSE frame terminator, spelled without escape sequences so no editor or
# shell step can mangle it into a real newline inside a string literal.
SSE_SEP = chr(10) + chr(10)

GOOD_PORT = 9995      # healthy upstream
SICK_PORT = 9994      # always 429 upstream


# --------------------------------------------------------------- mock servers

def _serve(application, port):
    server = uvicorn.Server(uvicorn.Config(application, host="127.0.0.1",
                                           port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{port}/health", timeout=1).is_success:
                break
        except Exception:
            time.sleep(0.1)
    return server, thread


@pytest.fixture(scope="module")
def mock_upstreams():
    good, good_t = _serve(make_app(), GOOD_PORT)
    sick, sick_t = _serve(make_app(fault="429"), SICK_PORT)
    yield
    for s in (good, sick):
        s.should_exit = True
    good_t.join(timeout=5)
    sick_t.join(timeout=5)


def _provider(name, port, models=("shared-model",)):
    return Provider(
        name=name,
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key="test-key",
        models=[ProviderModel(name=m) for m in models],
    )


def _service(mock_upstreams, **cfg_kwargs):
    """Two providers offering the same model: one healthy, one always 429.

    Names are chosen so round_robin (which walks providers in name order)
    reaches the sick one first — retry behaviour is then deterministic
    instead of depending on dict ordering.
    """
    cfg = RelayConfig(enabled=True, **cfg_kwargs)
    providers = [_provider("a-sick", SICK_PORT), _provider("b-healthy", GOOD_PORT)]
    return RelayService(cfg, providers)


def _body(model="shared-model", stream=False):
    return {"model": model, "messages": [{"role": "user", "content": "hello"}],
            "stream": stream}


# --------------------------------------------------------------------- config

class TestConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        cfg = load_relay_config(str(tmp_path / "nope.yaml"))
        assert cfg.enabled is False           # safe default: no open proxy
        assert cfg.strategy == "round_robin"
        assert cfg.failure_threshold == 3
        assert cfg.cooldown_seconds == 60.0
        assert cfg.max_retries == 2

    def test_reads_relay_wrapper(self, tmp_path):
        p = tmp_path / "relay.yaml"
        p.write_text(yaml.safe_dump({"relay": {
            "enabled": True, "strategy": "priority", "max_retries": 5,
            "cooldown_seconds": 10,
        }}), encoding="utf-8")
        cfg = load_relay_config(str(p))
        assert cfg.enabled is True
        assert cfg.strategy == "priority"
        assert cfg.max_retries == 5
        assert cfg.cooldown_seconds == 10.0

    def test_round_trips_through_save(self, tmp_path):
        p = str(tmp_path / "relay.yaml")
        save_relay_config(p, {"enabled": True, "strategy": "weighted",
                              "models": {"m": {"members": {"a": {"weight": 2}}}}})
        cfg = load_relay_config(p)
        assert cfg.enabled is True
        assert cfg.strategy == "weighted"
        assert cfg.model_settings("m")["members"]["a"]["weight"] == 2

    def test_unknown_strategy_falls_back(self, tmp_path):
        p = tmp_path / "relay.yaml"
        p.write_text(yaml.safe_dump({"relay": {"strategy": "chaos"}}), encoding="utf-8")
        assert load_relay_config(str(p)).strategy == "round_robin"

    def test_broken_yaml_does_not_raise(self, tmp_path):
        p = tmp_path / "relay.yaml"
        p.write_text("enabled: [unclosed\n  - :::", encoding="utf-8")
        assert load_relay_config(str(p)).enabled is False

    def test_per_model_strategy_overrides_global(self):
        cfg = RelayConfig(strategy="round_robin",
                          models={"m": {"strategy": "weighted"}})
        assert cfg.strategy_for("m") == "weighted"
        assert cfg.strategy_for("other") == "round_robin"


# ---------------------------------------------------------------- model pool

class TestPool:
    def test_groups_same_model_across_providers(self):
        providers = [
            Provider(name="amd", base_url="u1", models=[ProviderModel(name="qwen"),
                                                       ProviderModel(name="solo")]),
            Provider(name="sensenova", base_url="u2", models=[ProviderModel(name="qwen")]),
        ]
        pools = build_model_pool(providers)
        assert set(pools) == {"qwen", "solo"}
        assert {m.provider for m in pools["qwen"].members} == {"amd", "sensenova"}
        assert pools["qwen"].multi is True
        assert pools["solo"].multi is False

    def test_single_provider_still_gets_a_pool(self):
        pools = build_model_pool([_provider("only", GOOD_PORT)])
        # A relay that required two providers would be useless for most setups.
        assert "shared-model" in pools

    def test_reads_weight_and_priority_from_config(self):
        cfg = RelayConfig(models={"qwen": {"members": {"amd": {"weight": 5,
                                                              "priority": 0}}}})
        providers = [Provider(name="amd", base_url="u1",
                              models=[ProviderModel(name="qwen")])]
        member = build_model_pool(providers, cfg)["qwen"].members[0]
        assert member.weight == 5
        assert member.priority == 0

    def test_zero_weight_is_preserved(self):
        """weight 0 means "never route here" — a falsy check would eat it."""
        cfg = RelayConfig(models={"qwen": {"members": {"amd": {"weight": 0}}}})
        providers = [Provider(name="amd", base_url="u1",
                              models=[ProviderModel(name="qwen")])]
        assert build_model_pool(providers, cfg)["qwen"].members[0].weight == 0

    def test_unknown_model_raises(self, mock_upstreams):
        with pytest.raises(PoolNotFound):
            _service(mock_upstreams).get_model("no-such-model")


# ------------------------------------------------------------ circuit breaker

class TestBreaker:
    def test_opens_after_threshold(self):
        b = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        for _ in range(2):
            b.record_failure("amd", "boom", now=1000.0)
        assert b.is_available("amd", now=1000.0) is True
        b.record_failure("amd", "boom", now=1000.0)
        assert b.is_available("amd", now=1000.0) is False

    def test_recovers_when_cooldown_expires(self):
        b = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        b.record_failure("amd", "boom", now=1000.0)
        assert b.is_available("amd", now=1030.0) is False
        assert b.is_available("amd", now=1061.0) is True

    def test_success_resets_the_counter(self):
        b = CircuitBreaker(failure_threshold=3, cooldown_seconds=60)
        b.record_failure("amd", now=1000.0)
        b.record_failure("amd", now=1000.0)
        b.record_success("amd")
        b.record_failure("amd", now=1000.0)
        # Two failures after a success must not open the breaker.
        assert b.is_available("amd", now=1000.0) is True

    def test_reset_clears_state(self):
        b = CircuitBreaker(failure_threshold=1, cooldown_seconds=60)
        b.record_failure("amd", now=1000.0)
        b.reset("amd")
        assert b.is_available("amd", now=1000.0) is True


# -------------------------------------------------------------------- routing

def _pool(*providers):
    """Build a pool from (provider, weight, priority) triples."""
    return ModelPool(name="m", members=[
        RelayMember(provider=p, model="m", base_url="u", weight=w, priority=pr)
        for p, w, pr in providers
    ])


class TestSelect:
    def test_round_robin_rotates(self):
        r = Router(RelayConfig(strategy="round_robin"))
        pool = _pool(("a", 1, 0), ("b", 1, 0))
        picked = [r.select(pool).provider for _ in range(4)]
        assert picked == ["a", "b", "a", "b"]

    def test_priority_picks_lowest_number(self):
        r = Router(RelayConfig(strategy="priority"))
        pool = _pool(("a", 1, 10), ("b", 1, 1), ("c", 1, 5))
        assert [r.select(pool).provider for _ in range(3)] == ["b", "b", "b"]

    def test_weighted_prefers_heavier_member(self):
        import random
        r = Router(RelayConfig(strategy="weighted"),
                   rng=random.Random(1234))
        pool = _pool(("light", 1, 0), ("heavy", 99, 0))
        picks = [r.select(pool).provider for _ in range(200)]
        assert picks.count("heavy") > picks.count("light")

    def test_random_stays_inside_the_pool(self):
        import random
        r = Router(RelayConfig(strategy="random"), rng=random.Random(7))
        pool = _pool(("a", 1, 0), ("b", 1, 0))
        assert {r.select(pool).provider for _ in range(50)} <= {"a", "b"}

    def test_cooling_member_is_skipped(self):
        b = CircuitBreaker()
        r = Router(RelayConfig(strategy="round_robin", failure_threshold=1,
                               cooldown_seconds=60), breaker=b)
        pool = _pool(("a", 1, 0), ("b", 1, 0))
        r.on_failure("a", "boom")
        assert {r.select(pool).provider for _ in range(5)} == {"b"}

    def test_all_cooling_raises(self):
        r = Router(RelayConfig(strategy="round_robin", failure_threshold=1,
                               cooldown_seconds=60))
        pool = _pool(("a", 1, 0),)
        r.on_failure("a", "boom")
        with pytest.raises(NoAvailableMember):
            r.select(pool)


# ------------------------------------------------------------------ call log

class TestLog:
    def test_ring_buffer_keeps_last_n(self):
        log = RelayLog(limit=3)
        for i in range(10):
            log.append(RelayCall(id=str(i), started_at=time.time(), model="m"))
        items = log.list()
        assert len(items) == 3
        assert items[0]["id"] == "9"          # newest first

    def test_filters_by_model(self):
        log = RelayLog()
        log.append(RelayCall(id="1", started_at=time.time(), model="a"))
        log.append(RelayCall(id="2", started_at=time.time(), model="b"))
        assert [c["id"] for c in log.list(model="a")] == ["1"]
        assert log.stats(model="a")["total"] == 1

    def test_stats_average_only_successes(self):
        log = RelayLog()
        log.append(RelayCall(id="1", started_at=time.time(), model="m", ok=True,
                             ttft=0.2, tps=100.0, tokens=10))
        log.append(RelayCall(id="2", started_at=time.time(), model="m", ok=True,
                             ttft=0.4, tps=200.0, tokens=20))
        log.append(RelayCall(id="3", started_at=time.time(), model="m", ok=False,
                             error="boom", error_type="timeout"))
        s = log.stats()
        assert s["total"] == 3 and s["ok"] == 2
        assert s["success_rate"] == pytest.approx(200 / 3)
        assert s["avg_ttft"] == pytest.approx(0.3)
        assert s["avg_tps"] == pytest.approx(150.0)
        assert s["total_tokens"] == 30

    def test_log_never_records_secrets(self):
        c = RelayCall(id="1", started_at=time.time(), model="m", ok=True)
        d = c.to_dict()
        # The whole point of a metrics-only record: no body, no headers, no key.
        assert "messages" not in d and "api_key" not in d and "headers" not in d


# ------------------------------------------------------------------ forwarding

class TestForward:
    async def _drain(self, stream):
        buf = b""
        async for chunk in stream:
            buf += chunk
        return buf

    async def test_non_stream_passthrough(self, mock_upstreams):
        svc = _service(mock_upstreams)
        out = await svc.forward(_body(stream=False))
        assert out.status_code == 200
        assert out.body["object"] == "chat.completion"
        assert out.headers["x-relay-provider"] == "b-healthy"
        assert out.call.ok is True
        assert out.call.tokens > 0
        # Non-streaming has no first-token signal; inventing one would be a lie.
        assert out.call.ttft is None
        assert out.call.e2e is not None

    async def test_stream_passthrough_is_verbatim(self, mock_upstreams):
        svc = _service(mock_upstreams)
        out = await svc.forward(_body(stream=True))
        raw = await self._drain(out.stream)
        text = raw.decode()
        assert text.startswith("data:")
        assert "[DONE]" in text
        assert "tok0" in text
        # A passthrough must not fabricate OpenAI framing the upstream lacked.
        assert '"object":"chat.completion"' not in text

    async def test_stream_metrics_are_measured(self, mock_upstreams):
        svc = _service(mock_upstreams)
        out = await svc.forward(_body(stream=True))
        await self._drain(out.stream)
        call = svc.log.list(limit=1)[0]
        assert call["ok"] is True
        assert call["ttft"] is not None and call["ttft"] > 0
        assert call["tps"] and call["tps"] > 0
        assert call["tokens"] > 0

    async def test_retryable_failure_switches_provider(self, mock_upstreams):
        svc = _service(mock_upstreams, max_retries=2)
        out = await svc.forward(_body(stream=False))
        assert out.status_code == 200
        assert out.call.provider == "b-healthy"
        assert out.call.tried == ["a-sick", "b-healthy"]
        assert out.call.retried == 1
        assert out.call.attempts == 2

    async def test_non_retryable_failure_is_not_retried(self, mock_upstreams):
        """A 400 is the caller's bug: the same request fails everywhere."""
        providers = [_provider("a-bad", GOOD_PORT, models=("mock-400",))]
        svc = RelayService(RelayConfig(enabled=True, max_retries=3), providers)
        out = await svc.forward(_body(model="mock-400", stream=False))
        assert out.status_code == 400
        assert out.call.attempts == 1
        assert out.call.retried == 0
        # The upstream's own error object is forwarded, not re-labelled: an
        # OpenAI SDK needs the provider's real error type to react.
        assert out.body["error"]["type"] == "mock_fault"

    async def test_all_providers_failing_returns_last_error(self, mock_upstreams):
        providers = [_provider("a-sick", SICK_PORT), _provider("b-sick", SICK_PORT)]
        svc = RelayService(RelayConfig(enabled=True, max_retries=1), providers)
        out = await svc.forward(_body(stream=False))
        assert out.status_code == 429
        assert out.call.attempts == 2
        assert out.call.tried == ["a-sick", "b-sick"]
        assert out.body["error"]["relay"]["attempts"] == 2

    async def test_breaker_opens_then_returns_503(self, mock_upstreams):
        """One provider, threshold 1: after one failure it is cooling down."""
        providers = [_provider("a-sick", SICK_PORT)]
        svc = RelayService(
            RelayConfig(enabled=True, max_retries=0, failure_threshold=1,
                        cooldown_seconds=60),
            providers,
        )
        first = await svc.forward(_body(stream=False))
        assert first.status_code == 429
        second = await svc.forward(_body(stream=False))
        assert second.status_code == 503
        assert "cooling down" in second.body["error"]["message"]
        assert svc.router.breaker.is_available("a-sick") is False

    async def test_disabled_relay_refuses(self, mock_upstreams):
        svc = RelayService(RelayConfig(enabled=False), [_provider("a", GOOD_PORT)])
        with pytest.raises(RelayDisabled):
            await svc.forward(_body())

    async def test_missing_model_is_400(self, mock_upstreams):
        svc = _service(mock_upstreams)
        with pytest.raises(ValueError):
            await svc.forward({"messages": []})

    async def test_empty_stream_is_logged_as_failure(self, mock_upstreams):
        providers = [_provider("a-empty", GOOD_PORT, models=("mock-empty",))]
        svc = RelayService(RelayConfig(enabled=True), providers)
        out = await svc.forward(_body(model="mock-empty", stream=True))
        await self._drain(out.stream)
        call = svc.log.list(limit=1)[0]
        assert call["ok"] is False
        assert call["error_type"] in ("no_content", "parse")

    async def test_malformed_chunk_does_not_break_passthrough(self, mock_upstreams):
        """A junk line is forwarded as-is and the real answer still arrives."""
        providers = [_provider("a-junk", GOOD_PORT, models=("mock-junk",))]
        svc = RelayService(RelayConfig(enabled=True), providers)
        out = await svc.forward(_body(model="mock-junk", stream=True))
        raw = (await self._drain(out.stream)).decode()
        assert "not json at all" in raw
        assert "tok0" in raw
        call = svc.log.list(limit=1)[0]
        assert call["ok"] is True and call["tokens"] > 0

    async def test_client_disconnect_is_still_logged(self, mock_upstreams):
        """A caller who hangs up mid-stream must still leave a record.

        Otherwise the attribution trail silently misses exactly the calls that
        went wrong, which are the ones you want to see.
        """
        svc = _service(mock_upstreams)
        out = await svc.forward(_body(stream=True), client_name="quitter")
        # Take one chunk and walk away, like a browser closing the tab.
        async for _ in out.stream:
            break
        await out.stream.aclose()
        call = svc.log.list(limit=1)[0]
        assert call["client"] == "quitter"
        assert call["ok"] is False
        assert call["error_type"] == "cancelled"
        assert call["stream"] is True

    async def test_models_payload_merges_pool(self, mock_upstreams):
        svc = _service(mock_upstreams)
        payload = svc.models_payload()
        ids = [m["id"] for m in payload["data"]]
        assert ids == ["shared-model"]
        assert payload["data"][0]["n_providers"] == 2
        assert payload["data"][0]["providers"] == ["a-sick", "b-healthy"]

    async def test_pool_overview_reports_breaker_state(self, mock_upstreams):
        svc = _service(mock_upstreams, failure_threshold=1)
        svc.router.breaker.record_failure("a-sick", now=time.time())
        overview = {p["name"]: p for p in svc.pool_overview()}
        breakers = {m["provider"]: m["breaker"]["state"]
                    for m in overview["shared-model"]["members"]}
        assert breakers["a-sick"] == "open"
        assert breakers["b-healthy"] == "closed"


# ----------------------------------------------------------------- API layer

def _write_config(tmp_path, relay_yaml, providers):
    """Write targets.yaml + relay.yaml, including a known admin password.

    Delegates to conftest so the auth section (and therefore the login helper)
    is configured the same way for every test that needs an app.
    """
    from conftest import write_configs
    return write_configs(tmp_path, relay_yaml, providers)


def _provider_payload(name, port, models=("shared-model",)):
    return {"name": name, "base_url": f"http://127.0.0.1:{port}/v1",
            "api_key": "test-key",
            "models": [{"name": m} for m in models]}


def _key(client, name="tester", groups=None):
    from conftest import make_key
    return make_key(client, name=name, groups=groups or [])


def _hdr(key):
    from conftest import auth_header
    return auth_header(key)


class TestRelayApi:
    def test_config_round_trip_and_toggle(self, tmp_path):
        tpath, rpath = _write_config(tmp_path, {"enabled": False},
                                     [_provider_payload("a", GOOD_PORT)])
        app = create_app(tpath, rpath)
        with TestClient(app) as raw:
            client = _login(raw)
            r = client.get("/api/relay/config")
            assert r.status_code == 200
            assert r.json()["config"]["enabled"] is False
            assert r.json()["pools"][0]["name"] == "shared-model"

            # The switch the user asked for: one POST turns forwarding on.
            r = client.post("/api/relay/config", json={"enabled": True})
            assert r.status_code == 200
            assert r.json()["config"]["enabled"] is True
            assert load_relay_config(rpath).enabled is True

    def test_partial_patch_keeps_other_fields(self, tmp_path):
        tpath, rpath = _write_config(
            tmp_path,
            {"enabled": True, "strategy": "weighted", "cooldown_seconds": 30},
            [_provider_payload("a", GOOD_PORT)],
        )
        with TestClient(create_app(tpath, rpath)) as raw:
            client = _login(raw)
            r = client.post("/api/relay/config", json={"failure_threshold": 5})
            cfg = r.json()["config"]
            assert cfg["failure_threshold"] == 5
            assert cfg["strategy"] == "weighted"        # untouched
            assert cfg["cooldown_seconds"] == 30         # untouched

    def test_logs_endpoint_and_filter(self, tmp_path):
        tpath, rpath = _write_config(tmp_path, {"enabled": True},
                                     [_provider_payload("a", GOOD_PORT)])
        with TestClient(create_app(tpath, rpath)) as raw:
            client = _login(raw)
            # No calls yet: a clean empty list, not a 404.
            r = client.get("/api/relay/logs")
            assert r.status_code == 200
            assert r.json()["logs"] == []

            # The app shares one manager, so injected calls are what it serves.
            relay_manager.log.append(_call("m1", ok=True))
            relay_manager.log.append(_call("m2", ok=False))
            r = client.get("/api/relay/logs?model=m1").json()
            assert [c["model"] for c in r["logs"]] == ["m1"]
            assert r["stats"]["total"] == 1
        relay_manager.log.clear()

    def test_breaker_reset_endpoint(self, tmp_path):
        tpath, rpath = _write_config(tmp_path, {"enabled": True},
                                     [_provider_payload("a", GOOD_PORT)])
        with TestClient(create_app(tpath, rpath)) as raw:
            client = _login(raw)
            relay_manager.breaker.record_failure("a")
            r = client.post("/api/relay/breakers/reset?provider=a")
            assert r.status_code == 200
            assert relay_manager.breaker.snapshot("a")["consecutive_failures"] == 0


def _call(model, ok=True):
    return RelayCall(id=model, started_at=time.time(), model=model, ok=ok,
                     provider="a", ttft=0.1, tps=50.0, tokens=5)


class TestRelayV1:
    """The LAN-facing surface. Every one of these needs a client key."""

    def _client(self, tmp_path, providers=None, relay=None):
        providers = providers or [_provider_payload("a-healthy", GOOD_PORT)]
        tpath, rpath = _write_config(
            tmp_path, relay if relay is not None else {"enabled": True}, providers)
        raw = TestClient(create_app(tpath, rpath))
        client = _login(raw)
        return client, _make_key(client)

    def test_models_endpoint(self, tmp_path):
        client, key = self._client(tmp_path, [
            _provider_payload("a", GOOD_PORT), _provider_payload("b", GOOD_PORT)])
        r = client.get("/v1/models", headers=_hdr(key))
        assert r.status_code == 200
        data = r.json()["data"]
        assert [m["id"] for m in data] == ["shared-model"]
        assert data[0]["n_providers"] == 2

    def test_disabled_relay_returns_403(self, tmp_path):
        client, key = self._client(tmp_path, relay={"enabled": False})
        assert client.get("/v1/models", headers=_hdr(key)).status_code == 403
        r = client.post("/v1/chat/completions", json=_body(), headers=_hdr(key))
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"].lower()

    def test_chat_completions_passthrough(self, mock_upstreams, tmp_path):
        client, key = self._client(tmp_path)
        r = client.post("/v1/chat/completions", json=_body(stream=False),
                        headers=_hdr(key))
        assert r.status_code == 200
        assert r.json()["object"] == "chat.completion"
        assert r.headers["x-relay-provider"] == "a-healthy"

    def test_chat_completions_streaming(self, mock_upstreams, tmp_path):
        client, key = self._client(tmp_path)
        r = client.post("/v1/chat/completions", json=_body(stream=True),
                        headers=_hdr(key))
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "tok0" in r.text and "[DONE]" in r.text

    def test_unknown_model_returns_openai_style_404(self, tmp_path):
        client, key = self._client(tmp_path)
        r = client.post("/v1/chat/completions", json=_body(model="ghost"),
                        headers=_hdr(key))
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "model_not_found"

    def test_v1_is_not_swallowed_by_the_spa_fallback(self, tmp_path):
        """Regression: the SPA fallback answers every unmatched path with
        index.html. Without the ordering fix in create_app, an OpenAI client
        pointed at this server would get HTML where it expects JSON."""
        tpath, rpath = _write_config(tmp_path, {"enabled": True},
                                     [_provider_payload("a", GOOD_PORT)])
        with TestClient(create_app(tpath, rpath)) as raw:
            r = raw.get("/v1/definitely-not-a-route")
            assert r.status_code == 404
            assert "text/html" not in r.headers.get("content-type", "")

    def test_existing_spa_still_serves_index(self, tmp_path):
        """The fallback must keep working for real UI paths."""
        tpath, rpath = _write_config(tmp_path, {"enabled": True},
                                     [_provider_payload("a", GOOD_PORT)])
        with TestClient(create_app(tpath, rpath)) as raw:
            client = _login(raw)
            r = client.get("/")
            assert r.status_code == 200
            assert "llm-evl" in r.text


class TestClientKeys:
    """The key gate in front of /v1."""

    def _unauthed(self, tmp_path, relay=None):
        tpath, rpath = _write_config(
            tmp_path, relay if relay is not None else {"enabled": True},
            [_provider_payload("a", GOOD_PORT)])
        return TestClient(create_app(tpath, rpath))

    def test_no_key_is_401(self, tmp_path):
        c = self._unauthed(tmp_path)
        r = c.get("/v1/models")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "no_client_keys"

    def test_missing_key_rejected_once_keys_exist(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        _make_key(c)
        r = c.get("/v1/models")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_api_key"
        assert "Authorization: Bearer" in r.json()["error"]["message"]

    def test_wrong_key_rejected(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        _make_key(c, name="alice")
        r = c.get("/v1/models", headers=_hdr("sk-r_totally-wrong"))
        assert r.status_code == 401

    def test_valid_key_accepted(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        key = _make_key(c, name="alice")
        assert c.get("/v1/models", headers=_hdr(key)).status_code == 200

    def test_key_accepted_as_query_param(self, tmp_path):
        # Some SDKs send ?api_key= instead of a header.
        c = self._unauthed(tmp_path)
        c = _login(c)
        key = _make_key(c)
        assert c.get(f"/v1/models?api_key={key}").status_code == 200

    def test_disabled_key_rejected(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        key = _make_key(c, name="bob")
        r = c.post("/api/relay/clients/bob/enabled?enabled=false")
        assert r.status_code == 200
        assert c.get("/v1/models", headers=_hdr(key)).status_code == 401

    def test_deleted_key_rejected(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        key = _make_key(c, name="carol")
        assert c.delete("/api/relay/clients/carol").status_code == 200
        assert c.get("/v1/models", headers=_hdr(key)).status_code == 401

    def test_key_is_never_stored_in_plaintext(self, tmp_path):
        tpath, rpath = _write_config(tmp_path, {"enabled": True},
                                     [_provider_payload("a", GOOD_PORT)])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            key = _make_key(c, name="dave")
            text = Path(rpath).read_text(encoding="utf-8")
            assert key not in text                     # the whole point
            assert hash_key(key) in text
            # And the API never hands the key back out.
            assert "key" not in c.get("/api/relay/config").json()["clients"][0]

    def test_key_requires_a_name(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        assert c.post("/api/relay/clients", json={"name": "  "}).status_code == 400

    def test_recreating_a_name_replaces_the_key(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        first = _make_key(c, name="erin")
        second = _make_key(c, name="erin")
        assert c.get("/v1/models", headers=_hdr(first)).status_code == 401
        assert c.get("/v1/models", headers=_hdr(second)).status_code == 200

    def test_unknown_client_404(self, tmp_path):
        c = self._unauthed(tmp_path)
        c = _login(c)
        assert c.delete("/api/relay/clients/ghost").status_code == 404
        assert c.post("/api/relay/clients/ghost/enabled").status_code == 404

    def test_group_scoped_key_is_restricted(self, tmp_path):
        c = self._unauthed(tmp_path, relay={
            "enabled": True,
            "groups": {"A": {"members": [{"provider": "a", "model": "shared-model"}]}},
        })
        c = _login(c)
        key = _make_key(c, name="team-a", groups=["A"])
        r = c.get("/v1/models", headers=_hdr(key))
        assert [m["id"] for m in r.json()["data"]] == ["A"]
        r = c.post("/v1/chat/completions", json=_body(model="shared-model"),
                   headers=_hdr(key))
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "model_not_allowed"

    def test_last_used_is_recorded(self, tmp_path, mock_upstreams):
        c = self._unauthed(tmp_path)
        c = _login(c)
        key = _make_key(c, name="frank")
        c.post("/v1/chat/completions", json=_body(stream=False), headers=_hdr(key))
        clients = c.get("/api/relay/config").json()["clients"]
        record = next(x for x in clients if x["name"] == "frank")
        assert record["last_used_at"] is not None
        assert record["last_used_ip"]

    def test_call_log_records_the_client_name(self, tmp_path, mock_upstreams):
        c = self._unauthed(tmp_path)
        c = _login(c)
        key = _make_key(c, name="grace")
        c.post("/v1/chat/completions", json=_body(stream=False), headers=_hdr(key))
        logs = c.get("/api/relay/logs").json()["logs"]
        assert logs[0]["client"] == "grace"
        # Attribution must not leak the key itself.
        assert key not in str(logs)


class TestGroups:
    """User-defined alias pools: the client sends a group name, not a model."""

    def _providers(self):
        return [
            Provider(name="amd", base_url="http://a/v1", api_key="k1",
                     models=[ProviderModel(name="qwen-flash"),
                             ProviderModel(name="qwen-max")]),
            Provider(name="sensenova", base_url="http://b/v1", api_key="k2",
                     models=[ProviderModel(name="deepseek-flash")]),
        ]

    def test_group_mixes_different_models(self):
        cfg = RelayConfig(groups={"A": {"members": [
            {"provider": "amd", "model": "qwen-flash", "weight": 1},
            {"provider": "sensenova", "model": "deepseek-flash", "weight": 1},
        ]}})
        pool = build_model_pool(self._providers(), cfg)["A"]
        assert pool.kind == "group"
        assert {m.model for m in pool.members} == {"qwen-flash", "deepseek-flash"}
        assert all(m.available for m in pool.members)

    def test_group_shadows_a_same_named_model(self):
        """An explicit group must win over the implicit same-name pool."""
        cfg = RelayConfig(groups={"qwen-flash": {"members": [
            {"provider": "sensenova", "model": "deepseek-flash"},
        ]}})
        pool = build_model_pool(self._providers(), cfg)["qwen-flash"]
        assert pool.kind == "group"
        assert [m.model for m in pool.members] == ["deepseek-flash"]

    def test_broken_member_is_kept_but_marked(self):
        """A deleted provider must not silently shrink someone's group."""
        cfg = RelayConfig(groups={"A": {"members": [
            {"provider": "amd", "model": "qwen-flash"},
            {"provider": "gone", "model": "whatever"},
        ]}})
        pool = build_model_pool(self._providers(), cfg)["A"]
        assert len(pool.members) == 2
        broken = [m for m in pool.members if not m.available]
        assert len(broken) == 1 and broken[0].provider == "gone"
        assert len(pool.usable_members()) == 1

    def test_group_with_no_usable_member_refuses_clearly(self):
        cfg = RelayConfig(groups={"A": {"members": [
            {"provider": "gone", "model": "whatever"},
        ]}})
        svc = RelayService(cfg, self._providers())
        with pytest.raises(NoAvailableMember) as ei:
            svc.router.select(svc.get_model("A"))
        assert "no usable member" in str(ei.value)

    def test_group_strategy_overrides_global(self):
        cfg = RelayConfig(strategy="round_robin", groups={"A": {
            "strategy": "priority",
            "members": [{"provider": "amd", "model": "qwen-flash", "priority": 3}],
        }})
        svc = RelayService(cfg, self._providers())
        assert svc.config.strategy_for("A") == "priority"
        assert svc.router.select(svc.get_model("A")).provider == "amd"

    def test_group_appears_in_models_payload(self):
        cfg = RelayConfig(enabled=True, groups={"A": {"description": "日常",
            "members": [{"provider": "amd", "model": "qwen-flash"}]}})
        svc = RelayService(cfg, self._providers())
        ids = {m["id"]: m for m in svc.models_payload()["data"]}
        assert ids["A"]["kind"] == "group"
        assert ids["A"]["providers"] == ["amd"]

    def test_forward_rewrites_model_to_the_member(self, mock_upstreams):
        """The critical bit: the upstream must receive its own model name, not
        the group alias. Otherwise the provider gets model='A' and 404s."""
        providers = [_provider("a-healthy", GOOD_PORT, models=("upstream-name",))]
        cfg = RelayConfig(enabled=True, groups={"A": {"members": [
            {"provider": "a-healthy", "model": "upstream-name"}]}})
        svc = RelayService(cfg, providers)
        out = asyncio.run(svc.forward(_body(model="A", stream=False)))
        assert out.status_code == 200
        assert out.call.model == "A"                    # what the client asked
        assert out.call.provider == "a-healthy"
        # The mock echoes the model it received.
        assert out.body["model"] == "upstream-name"

    def test_group_end_to_end_over_http(self, mock_upstreams, tmp_path):
        tpath, rpath = _write_config(tmp_path, {
            "enabled": True,
            "groups": {"A": {"description": "日常对话", "members": [
                {"provider": "a-healthy", "model": "shared-model"},
            ]}},
        }, [_provider_payload("a-healthy", GOOD_PORT)])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            key = _make_key(c)
            models = [m["id"] for m in c.get("/v1/models", headers=_hdr(key)).json()["data"]]
            assert "A" in models
            r = c.post("/v1/chat/completions", json=_body(model="A", stream=False),
                       headers=_hdr(key))
            assert r.status_code == 200
            assert r.json()["object"] == "chat.completion"
            log = c.get("/api/relay/logs").json()["logs"][0]
            assert log["model"] == "A"


# ------------------------------------------------------------- probing

def _member(provider, model, base_url=""):
    return RelayMember(provider=provider, model=model, base_url=base_url, available=True)


class TestProbe:
    async def test_healthy_member_reports_ttft(self, mock_upstreams):
        m = _member("good", "shared-model", f"http://127.0.0.1:{GOOD_PORT}/v1")
        (r,) = await probe_members([m])
        assert r.ok is True
        assert r.ttft is not None and r.ttft > 0
        assert r.tps and r.tps > 0
        assert r.error_type == ""
        assert r.provider == "good" and r.model == "shared-model"

    async def test_failing_member_reports_error_type(self, mock_upstreams):
        m = _member("sick", "shared-model", f"http://127.0.0.1:{SICK_PORT}/v1")
        (r,) = await probe_members([m])
        assert r.ok is False
        assert r.error_type == "rate_limit"
        assert "429" in r.error

    async def test_unreachable_endpoint_is_transport_error(self):
        m = _member("gone", "m", "http://127.0.0.1:9/v1")   # discard port
        (r,) = await probe_members([m], timeout=3)
        assert r.ok is False
        assert r.error_type in ("transport", "timeout")

    async def test_broken_member_is_reported_not_probed(self):
        """A member referencing a deleted provider must say so, not hang."""
        m = RelayMember(provider="gone", model="m", base_url="", available=False)
        (r,) = await probe_members([m])
        assert r.ok is False
        assert r.error_type == "missing_member"

    async def test_empty_list(self):
        assert await probe_members([]) == []

    async def test_results_keep_input_order(self, mock_upstreams):
        members = [
            _member("a", "shared-model", f"http://127.0.0.1:{GOOD_PORT}/v1"),
            _member("b", "shared-model", f"http://127.0.0.1:{SICK_PORT}/v1"),
            _member("c", "shared-model", f"http://127.0.0.1:{GOOD_PORT}/v1"),
        ]
        results = await probe_members(members)
        assert [r.provider for r in results] == ["a", "b", "c"]

    async def test_probe_does_not_trip_the_breaker(self, mock_upstreams):
        """A manual test is a measurement; it must not reroute production."""
        svc = _service(mock_upstreams, failure_threshold=1)
        m = _member("sick", "shared-model", f"http://127.0.0.1:{SICK_PORT}/v1")
        await probe_members([m])
        assert svc.router.breaker.is_available("sick") is True
        assert svc.router.breaker.snapshot("sick")["consecutive_failures"] == 0


class TestRank:
    def _pool(self):
        return [
            _member("fast", "m"),
            _member("slow", "m"),
            _member("dead", "m"),
            _member("weird", "m"),
        ]

    def test_live_before_dead_and_fastest_first(self):
        members = self._pool()
        results = [
            ProbeResult("fast", "m", ok=True, ttft=0.10),
            ProbeResult("slow", "m", ok=True, ttft=0.80),
            ProbeResult("dead", "m", ok=False, error_type="timeout"),
            # Reported ok but produced no first-token measurement: that is not
            # evidence of health, so it must not be ranked as if it were fast.
            ProbeResult("weird", "m", ok=True, ttft=None),
        ]
        order = [m.provider for m, _ in rank_members(members, results)]
        assert order[:2] == ["fast", "slow"]
        assert set(order[2:]) == {"dead", "weird"}

    def test_all_healthy_sorted_by_ttft(self):
        members = [_member("b", "m"), _member("a", "m"), _member("c", "m")]
        results = [
            ProbeResult("a", "m", ok=True, ttft=0.3),
            ProbeResult("b", "m", ok=True, ttft=0.1),
            ProbeResult("c", "m", ok=True, ttft=0.2),
        ]
        assert [m.provider for m, _ in rank_members(members, results)] == ["b", "c", "a"]

    def test_equal_ttft_is_deterministic(self):
        members = [_member("z", "m"), _member("a", "m")]
        results = [ProbeResult("z", "m", ok=True, ttft=0.5),
                   ProbeResult("a", "m", ok=True, ttft=0.5)]
        first = [m.provider for m, _ in rank_members(members, results)]
        second = [m.provider for m, _ in rank_members(members, results)]
        assert first == second == ["a", "z"]

    def test_all_dead_still_deterministic(self):
        members = [_member("z", "m"), _member("a", "m")]
        results = [ProbeResult("z", "m", ok=False), ProbeResult("a", "m", ok=False)]
        assert [m.provider for m, _ in rank_members(members, results)] == ["a", "z"]

    def test_missing_result_treated_as_unusable(self):
        members = [_member("a", "m")]
        assert [m.provider for m, _ in rank_members(members, [])] == ["a"]


class TestAutoPriorityApi:
    """End-to-end: probe -> rank -> persist."""

    def _cfg(self, tmp_path, group_members, strategy="priority"):
        return write_configs(
            tmp_path,
            {"enabled": True, "strategy": strategy,
             "groups": {"A": {"description": "d", "members": group_members}}},
            [provider_payload("good", GOOD_PORT, models=("shared-model",)),
             provider_payload("sick", SICK_PORT, models=("shared-model",))],
        )

    def test_reorders_and_persists(self, mock_upstreams, tmp_path):
        # Both start at different priorities already, with the sick one first,
        # so the probe has to overturn the hand-written order.
        tpath, rpath = self._cfg(tmp_path, [
            {"provider": "sick", "model": "shared-model", "weight": 1, "priority": 0},
            {"provider": "good", "model": "shared-model", "weight": 1, "priority": 1},
        ])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            r = c.post("/api/relay/groups/A/auto-priority")
            assert r.status_code == 200, r.text
            data = r.json()
            order = [(x["provider"], x["priority"]) for x in data["ranking"]]
            assert order[0][0] == "good"
            assert order[1] == ("sick", 1)

            saved = load_relay_config(rpath)
            members = saved.groups["A"]["members"]
            prio = {m["provider"]: m["priority"] for m in members}
            assert prio["good"] == 0 and prio["sick"] == 1
            # weights are not ours to rewrite
            assert all(m["weight"] == 1 for m in members)
            # the rest of the config survives
            assert saved.strategy == "priority"

    def test_probe_whole_group(self, mock_upstreams, tmp_path):
        tpath, rpath = self._cfg(tmp_path, [
            {"provider": "good", "model": "shared-model"},
            {"provider": "sick", "model": "shared-model"},
        ])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            r = c.post("/api/relay/probe", json={"group": "A"})
            assert r.status_code == 200
            by = {x["provider"]: x for x in r.json()["results"]}
            assert by["good"]["ok"] is True and by["good"]["ttft"] > 0
            assert by["sick"]["ok"] is False

    def test_probe_single_member(self, mock_upstreams, tmp_path):
        tpath, rpath = self._cfg(tmp_path, [
            {"provider": "good", "model": "shared-model"}])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            r = c.post("/api/relay/probe", json={
                "members": [{"provider": "good", "model": "shared-model"}]})
            assert r.status_code == 200
            assert len(r.json()["results"]) == 1
            assert r.json()["results"][0]["ok"] is True

    def test_probe_rejects_member_not_in_config(self, mock_upstreams, tmp_path):
        """Otherwise this endpoint could fetch arbitrary URLs (SSRF)."""
        tpath, rpath = self._cfg(tmp_path, [
            {"provider": "good", "model": "shared-model"}])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            r = c.post("/api/relay/probe", json={
                "members": [{"provider": "evil", "model": "x"}]})
            assert r.status_code == 400
            assert "not in targets.yaml" in r.json()["detail"]

    def test_auto_priority_unknown_group_404(self, tmp_path):
        tpath, rpath = self._cfg(tmp_path, [
            {"provider": "good", "model": "shared-model"}])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            assert c.post("/api/relay/groups/ghost/auto-priority").status_code == 404

    def test_auto_priority_empty_group_400(self, tmp_path):
        tpath, rpath = self._cfg(tmp_path, [])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            r = c.post("/api/relay/groups/A/auto-priority")
            assert r.status_code == 400
            assert "no members" in r.json()["detail"]

    def test_probe_result_appears_in_config(self, mock_upstreams, tmp_path):
        tpath, rpath = self._cfg(tmp_path, [
            {"provider": "good", "model": "shared-model"}])
        with TestClient(create_app(tpath, rpath)) as raw:
            c = _login(raw)
            c.post("/api/relay/probe", json={"group": "A"})
            pools = c.get("/api/relay/config").json()["pools"]
            group = next(p for p in pools if p["name"] == "A")
            probe = group["members"][0]["probe"]
            assert probe is not None and probe["ok"] is True


# ------------------------------------------------- reasoning-model streaming

class TestReasoningStream:
    """A reasoning model streams `reasoning_content` before any `content`.

    TTFT is the time the caller spent waiting for the first token. For these
    models the first token arrives as reasoning_content, and the old code only
    looked at `content` — so TTFT came back as None for the entire family
    (Qwen3.6, DeepSeek-V4-Flash, Qwen3.8-Flash-Next, ...), which also made the
    relay's "sort by TTFT" button useless on exactly those models.
    """

    @pytest.fixture
    def reasoning_server(self):
        """A mock server that thinks out loud before answering."""
        import json as _json
        import time as _time

        from fastapi import Request as _Request
        from fastapi.responses import StreamingResponse as _StreamingResponse
        from fastapi import FastAPI as _FastAPI

        application = _FastAPI()

        @application.post("/v1/chat/completions")
        async def chat(req: _Request):
            body = await req.json()

            async def gen():
                await asyncio.sleep(0.05)
                # Thinking phase: no `content` at all.
                for i in range(3):
                    yield "data: " + _json.dumps({
                        "model": "reasoner",
                        "choices": [{"index": 0, "delta": {"reasoning_content": f"think{i} "},
                                     "finish_reason": None}],
                    }) + "\n\n"
                    await asyncio.sleep(0.01)
                # Answer phase.
                for i in range(5):
                    yield "data: " + _json.dumps({
                        "model": "reasoner",
                        "choices": [{"index": 0, "delta": {"content": f"ans{i} "},
                                     "finish_reason": None}],
                    }) + "\n\n"
                    await asyncio.sleep(0.01)
                yield "data: " + _json.dumps({
                    "model": "reasoner",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 8, "total_tokens": 13},
                }) + "\n\n"
                yield "data: [DONE]\n\n"

            return _StreamingResponse(gen(), media_type="text/event-stream")

        return application

    @pytest.fixture
    def reasoning_upstream(self, reasoning_server):
        import threading
        import uvicorn as _uvicorn
        port = 9993
        server = _uvicorn.Server(_uvicorn.Config(reasoning_server, host="127.0.0.1",
                                                 port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                with httpx.Client() as c:
                    if c.post(f"http://127.0.0.1:{port}/v1/chat/completions",
                              json={"model": "reasoner", "messages": []},
                              timeout=2).status_code == 200:
                        break
            except Exception:
                time.sleep(0.1)
        yield port
        server.should_exit = True
        thread.join(timeout=5)

    async def test_ttft_is_measured_from_the_first_reasoning_token(self, reasoning_upstream):
        from llm_evl.core.client import stream_request
        from llm_evl.core.models import Target

        target = Target(name="r", base_url=f"http://127.0.0.1:{reasoning_upstream}/v1",
                        model="reasoner")
        async with httpx.AsyncClient() as client:
            r = await stream_request(client, target, "hi", temperature=0.0, timeout=20.0,
                                     include_usage=True, prompt_id="t", concurrency=1)
        assert r.ok is True
        # The whole point: TTFT is a number, not None.
        assert r.ttft is not None and r.ttft > 0
        # ITL accumulates across the thinking phase too.
        assert len(r.itl) >= 5
        # Token counting still uses the answer only, not the thinking.
        assert r.output_tokens == 8

    SSE_SEP = chr(10) + chr(10)   # an SSE frame terminator, spelled without escapes

    @pytest.mark.parametrize("alias", ["reasoning_content", "reasoning", "thinking"])
    async def test_every_reasoning_key_alias_is_timed(self, alias):
        """Vendors disagree on the field name; all three must produce a TTFT.

        The local vLLM serving Qwen3.6 sends `reasoning`, DeepSeek-style
        gateways send `reasoning_content`, some send `thinking`.
        """
        import json as _json
        import threading
        import uvicorn as _uvicorn
        from fastapi import FastAPI as _FastAPI, Request as _Request
        from fastapi.responses import StreamingResponse as _StreamingResponse

        application = _FastAPI()

        @application.post("/v1/chat/completions")
        async def chat(req: _Request):
            async def gen():
                await asyncio.sleep(0.02)
                yield "data: " + _json.dumps({
                    "model": "r",
                    "choices": [{"index": 0, "delta": {alias: "thinking..."},
                                 "finish_reason": None}],
                }) + SSE_SEP
                yield "data: " + _json.dumps({
                    "model": "r",
                    "choices": [{"index": 0, "delta": {"content": "answer"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                }) + SSE_SEP
                yield "data: [DONE]" + SSE_SEP

            return _StreamingResponse(gen(), media_type="text/event-stream")

        port = 9992
        server = _uvicorn.Server(_uvicorn.Config(application, host="127.0.0.1",
                                                 port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        import time as _t
        deadline = _t.time() + 10
        while _t.time() < deadline:
            try:
                with httpx.Client() as c:
                    c.post(f"http://127.0.0.1:{port}/v1/chat/completions",
                           json={"model": "r", "messages": []}, timeout=2)
                break
            except Exception:
                _t.sleep(0.1)
        try:
            from llm_evl.core.client import stream_request
            from llm_evl.core.models import Target

            target = Target(name="r", base_url=f"http://127.0.0.1:{port}/v1", model="r")
            async with httpx.AsyncClient() as client:
                r = await stream_request(client, target, "hi", temperature=0.0,
                                         timeout=20.0, include_usage=True,
                                         prompt_id="t", concurrency=1)
            assert r.ok is True
            assert r.ttft is not None and r.ttft > 0, alias
            assert "thinking" not in r.output_text
        finally:
            server.should_exit = True
            thread.join(timeout=5)

    async def test_reasoning_is_not_counted_as_output_text(self, reasoning_upstream):
        from llm_evl.core.client import stream_request
        from llm_evl.core.models import Target

        target = Target(name="r", base_url=f"http://127.0.0.1:{reasoning_upstream}/v1",
                        model="reasoner")
        async with httpx.AsyncClient() as client:
            r = await stream_request(client, target, "hi", temperature=0.0, timeout=20.0,
                                     include_usage=False, prompt_id="t", concurrency=1)
        # "think0 think1 ..." must not leak into the stored answer.
        assert "think" not in r.output_text
        assert "ans0" in r.output_text

    async def test_chat_stream_does_not_forward_reasoning_to_the_ui(self, reasoning_upstream):
        from llm_evl.core.client import stream_chat
        from llm_evl.core.models import Target

        target = Target(name="r", base_url=f"http://127.0.0.1:{reasoning_upstream}/v1",
                        model="reasoner")
        seen: list[str] = []
        async with httpx.AsyncClient() as client:
            r = await stream_chat(client, target, [{"role": "user", "content": "hi"}],
                                  temperature=0.0, timeout=20.0,
                                  on_token=lambda t: seen.append(t) or asyncio.sleep(0))
        assert r.ok is True
        assert r.ttft is not None and r.ttft > 0
        joined = "".join(seen)
        assert "ans0" in joined
        assert "think" not in joined      # the UI shows answers, not thinking


class TestKeylessProvider:
    """`Authorization: Bearer ` with an empty value is an illegal header.

    httpx rejects the request outright, so every keyless provider (a local
    vLLM with auth disabled, say) used to fail with a confusing transport error
    instead of simply working.
    """

    async def test_no_authorization_header_when_no_key(self, mock_upstreams):
        from llm_evl.core.client import stream_request
        from llm_evl.core.models import Target

        target = Target(name="local", base_url=f"http://127.0.0.1:{GOOD_PORT}/v1",
                        model="shared-model")   # no api_key, no api_key_env
        assert target.resolved_api_key() == ""
        async with httpx.AsyncClient() as client:
            r = await stream_request(client, target, "hi", temperature=0.0, timeout=20.0,
                                     include_usage=True, prompt_id="t", concurrency=1)
        assert r.ok is True
        assert r.error_type == ""

    async def test_header_still_sent_when_key_present(self, mock_upstreams):
        from llm_evl.core.client import stream_request
        from llm_evl.core.models import Target

        target = Target(name="k", base_url=f"http://127.0.0.1:{GOOD_PORT}/v1",
                        model="shared-model", api_key="secret")
        async with httpx.AsyncClient() as client:
            r = await stream_request(client, target, "hi", temperature=0.0, timeout=20.0,
                                     include_usage=True, prompt_id="t", concurrency=1)
        assert r.ok is True


class TestPriorityTiers:
    """`priority` must mean "prefer the top tier", not "always the one provider".

    Every member starts at the same default priority, so the naive
    `min(priority, provider)` pinned all traffic to a single upstream and a
    fresh group could never fail over. Ties now rotate inside the top tier.
    """

    def _router(self, cfg_strategy="priority"):
        return Router(RelayConfig(strategy=cfg_strategy))

    def test_lowest_priority_wins(self):
        r = self._router()
        pool = _pool(("a", 1, 10), ("b", 1, 1), ("c", 1, 5))
        assert [r.select(pool).provider for _ in range(4)] == ["b"] * 4

    def test_equal_priorities_rotate_instead_of_pinning(self):
        r = self._router()
        pool = _pool(("a", 1, 0), ("b", 1, 0), ("c", 1, 0))
        picked = [r.select(pool).provider for _ in range(6)]
        assert set(picked) == {"a", "b", "c"}
        # ...and the rotation is even, not "a, a, a, b, b, c".
        assert picked.count("a") == picked.count("b") == picked.count("c") == 2

    def test_rotation_stays_inside_the_top_tier(self):
        """A better member must never share traffic with a worse one."""
        r = self._router()
        pool = _pool(("fast", 1, 0), ("fast2", 1, 0), ("slow", 1, 9))
        picked = {r.select(pool).provider for _ in range(20)}
        assert picked == {"fast", "fast2"}

    def test_falls_back_when_the_top_tier_is_cooling(self):
        # threshold 1 so a single failure actually opens the breaker
        r = self._router()
        r.breaker.failure_threshold = 1
        pool = _pool(("a", 1, 0), ("b", 1, 1))
        r.on_failure("a", "boom")
        assert {r.select(pool).provider for _ in range(4)} == {"b"}

    def test_ties_rotate_even_when_one_is_cooling(self):
        r = self._router()
        r.breaker.failure_threshold = 1
        pool = _pool(("a", 1, 0), ("b", 1, 0))
        r.on_failure("a", "boom")
        assert r.select(pool).provider == "b"

    def test_single_member_needs_no_cursor(self):
        r = self._router()
        pool = _pool(("only", 1, 0))
        assert [r.select(pool).provider for _ in range(3)] == ["only"] * 3


class TestConfigShapeRoundTrip:
    """The save/load shape is a contract between the UI and the reader.

    The web UI writes `models.<model>.members.<provider>` and
    `groups.<name>.members[]`. A shape that saves fine but is read differently
    is the worst kind of bug: no error, just settings that quietly do nothing.
    """

    def test_member_priority_survives_save_and_reload(self, tmp_path):
        path = str(tmp_path / "relay.yaml")
        save_relay_config(path, {
            "enabled": True,
            "models": {"qwen": {"members": {
                "amd": {"weight": 1, "priority": 0},
                "sensenova": {"weight": 1, "priority": 1},
            }}},
        })
        cfg = load_relay_config(path)
        providers = [
            Provider(name="amd", base_url="u1", models=[ProviderModel(name="qwen")]),
            Provider(name="sensenova", base_url="u2", models=[ProviderModel(name="qwen")]),
        ]
        pool = build_model_pool(providers, cfg)["qwen"]
        assert {m.provider: m.priority for m in pool.members} == {"amd": 0, "sensenova": 1}

    def test_flat_members_shape_is_ignored_not_misread(self, tmp_path):
        """Writing providers directly under the model name must not be mistaken
        for member settings — it should simply carry no per-member overrides."""
        path = str(tmp_path / "relay.yaml")
        save_relay_config(path, {
            "enabled": True,
            "models": {"qwen": {"amd": {"priority": 0}}},
        })
        cfg = load_relay_config(path)
        providers = [Provider(name="amd", base_url="u1",
                              models=[ProviderModel(name="qwen")])]
        pool = build_model_pool(providers, cfg)["qwen"]
        # Defaults, not the stray `amd` key interpreted as settings.
        assert pool.members[0].priority == UNSET_PRIORITY
        assert pool.members[0].weight == UNSET_WEIGHT

    def test_group_members_round_trip(self, tmp_path):
        path = str(tmp_path / "relay.yaml")
        save_relay_config(path, {"enabled": True, "groups": {"A": {
            "description": "d", "strategy": "priority",
            "members": [
                {"provider": "amd", "model": "m1", "priority": 0},
                {"provider": "amd", "model": "m2", "priority": 1},
            ],
        }}})
        cfg = load_relay_config(path)
        assert cfg.strategy_for("A") == "priority"
        spec = cfg.groups["A"]
        assert [(m["provider"], m["model"], m["priority"]) for m in spec["members"]] == [
            ("amd", "m1", 0), ("amd", "m2", 1)]

    def test_group_strategy_beats_model_strategy(self, tmp_path):
        cfg = RelayConfig(
            strategy="round_robin",
            models={"A": {"strategy": "random"}},
            groups={"A": {"strategy": "priority", "members": []}},
        )
        assert cfg.strategy_for("A") == "priority"

    def test_missing_group_strategy_falls_back_to_model_then_global(self, tmp_path):
        cfg = RelayConfig(strategy="weighted",
                          models={"A": {"strategy": "random"}},
                          groups={"A": {"members": []}})
        assert cfg.strategy_for("A") == "random"
        cfg2 = RelayConfig(strategy="weighted", groups={"A": {"members": []}})
        assert cfg2.strategy_for("A") == "weighted"

    def test_auth_section_survives_a_relay_save(self, tmp_path):
        """Both sections live in one file; a relay save must not drop auth."""
        path = str(tmp_path / "relay.yaml")
        save_auth_config(path, {"admin": {"username": "admin",
                                          "password_hash": "pbkdf2_sha256$x"},
                               "session_secret": "s3cret"})
        save_relay_config(path, {"enabled": True, "strategy": "priority"})
        doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        assert doc["auth"]["admin"]["password_hash"] == "pbkdf2_sha256$x"
        assert doc["auth"]["session_secret"] == "s3cret"
        assert doc["relay"]["strategy"] == "priority"
