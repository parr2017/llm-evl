"""Relay layer: one OpenAI-compatible entry point in front of many providers.

The benchmark answers "how fast is model X on provider A". The relay answers a
different question: "I asked for model X, give me *a* model X" — picking among
every provider that offers that same model name, retrying on the failures that
are worth retrying, and recording what happened.

Design rules that shaped this module:

- **Faithful passthrough.** Upstream bytes are forwarded verbatim (streaming and
  non-streaming). We measure, we never rewrite. This is deliberately different
  from ``core/client.py``, which owns the parsing because it produces reports.
- **Retries only where retrying helps.** 429 / 5xx / timeout / transport errors
  switch to another provider; a 400 (bad request) is the caller's bug and is
  returned as-is, because retrying it just burns three upstreams.
- **No switch after the first byte.** Once content has been streamed to the
  client, a retry would splice two different providers' answers together. From
  that point on we can only cut the stream and log the failure.
- **No secrets in the log.** A relay sees every prompt and every API key, so the
  call log records metrics only — never the request body, never the key.
- **Optional feature, default off.** ``enabled: false`` means the OpenAI-facing
  endpoints refuse to forward; a benchmark box should not silently proxy
  traffic onto the operator's paid keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import yaml

from .models import ErrorType, Provider, Target
from .tokenizer import count_tokens

logger = logging.getLogger(__name__)

# The four strategies exposed in the UI. Anything else falls back to
# round_robin, which is the only one that is always safe to use.
STRATEGIES = ("round_robin", "random", "weighted", "priority")
DEFAULT_STRATEGY = "round_robin"

# Sentinel priority for "not configured". Lower than any real value, so an
# explicit priority always outranks an unset one.
UNSET_PRIORITY = 999
UNSET_WEIGHT = 1

# Errors where "try the next provider" is a reasonable bet. A 4xx that is not
# 429 means the request itself is wrong, so the same request to the next
# provider fails identically.
RETRYABLE_ERROR_TYPES = frozenset({
    ErrorType.RATE_LIMIT.value,
    ErrorType.SERVER_ERROR.value,
    ErrorType.TIMEOUT.value,
    ErrorType.TRANSPORT.value,
})

# Cap on how much generated text we keep in memory purely to count tokens.
# Upstream's own `usage` is preferred; this only matters when it is missing.
_MAX_TEXT_FOR_COUNTING = 200_000


# --------------------------------------------------------------------- config

@dataclass
class RelayConfig:
    """Everything the relay needs that is not in targets.yaml.

    ``models`` is per-model-name tuning on top of the global strategy. Weights
    and priorities are per *member* (provider), because that is the unit the UI
    edits and the unit the router actually chooses between::

        models:
          deepseek-v4-flash:
            strategy: weighted      # optional per-model override
            members:
              sensenova: {weight: 1, priority: 0}
              amd:        {weight: 3, priority: 1}

    ``groups`` is the user-facing feature: a named alias whose members may be
    *different* models on different providers. Clients then send ``model: "A"``
    and never learn which upstream actually answered::

        groups:
          A:
            description: 日常对话
            strategy: weighted
            members:
              - {provider: sensenova, model: deepseek-v4-flash, weight: 1}
              - {provider: amd,        model: qwen3.8-flash,    weight: 3}

    ``clients`` are the keys allowed to call ``/v1``. Only hashes are stored;
    the plaintext exists exactly once, in the response that created them.
    """

    enabled: bool = False
    strategy: str = DEFAULT_STRATEGY
    failure_threshold: int = 3       # consecutive failures before cooldown
    cooldown_seconds: float = 60.0
    max_retries: int = 2             # extra attempts after the first
    timeout: float = 120.0
    log_limit: int = 1000            # in-memory ring buffer size
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    clients: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.strategy not in STRATEGIES:
            logger.warning(
                "unknown relay strategy %r, falling back to %s",
                self.strategy, DEFAULT_STRATEGY,
            )
            self.strategy = DEFAULT_STRATEGY
        self.failure_threshold = max(1, int(self.failure_threshold or 1))
        self.cooldown_seconds = max(0.0, float(self.cooldown_seconds or 0.0))
        self.max_retries = max(0, int(self.max_retries or 0))
        self.timeout = max(1.0, float(self.timeout or 1.0))
        self.log_limit = max(1, int(self.log_limit or 1))

    def model_settings(self, model: str) -> dict[str, Any]:
        return dict(self.models.get(model) or {})

    def strategy_for(self, model: str) -> str:
        """Per-model override wins over the global strategy.

        A group's own ``strategy`` wins over a per-model override: a group is
        the more specific, more intentional object.
        """
        group = self.groups.get(model) or {}
        if group.get("strategy") in STRATEGIES:
            return group["strategy"]
        s = self.model_settings(model).get("strategy")
        return s if s in STRATEGIES else self.strategy

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "strategy": self.strategy,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
            "max_retries": self.max_retries,
            "timeout": self.timeout,
            "log_limit": self.log_limit,
            "models": self.models,
            "groups": self.groups,
            "clients": self.clients,
        }


def _read_yaml(path: str) -> dict[str, Any]:
    """Read a YAML config, tolerating absence and corruption.

    The relay and its credentials are optional, so unlike targets.yaml (whose
    absence is a configuration error) a missing or broken relay.yaml must never
    stop the app from booting.
    """
    p = Path(path)
    if not p.exists():
        return {}
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception as exc:
        logger.warning("relay config %s unreadable (%s); using defaults", path, exc)
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_yaml(path: str, doc: dict[str, Any]) -> None:
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else []


def config_from_doc(doc: dict[str, Any]) -> RelayConfig:
    """Build a RelayConfig from a whole-file document (relay.yaml).

    Accepts the document either wrapped in a ``relay:`` key or bare, so hand
    written files that people copy from the example keep working.
    """
    data = doc.get("relay")
    if not isinstance(data, dict):
        data = doc
    return RelayConfig(
        enabled=bool(data.get("enabled", False)),
        strategy=str(data.get("strategy") or DEFAULT_STRATEGY),
        failure_threshold=data.get("failure_threshold", 3),
        cooldown_seconds=data.get("cooldown_seconds", 60.0),
        max_retries=data.get("max_retries", 2),
        timeout=data.get("timeout", 120.0),
        log_limit=data.get("log_limit", 1000),
        models=_as_dict(data.get("models")),
        groups=_as_dict(data.get("groups")),
        clients=[c for c in _as_list(data.get("clients")) if isinstance(c, dict)],
    )


def load_relay_config(path: str) -> RelayConfig:
    return config_from_doc(_read_yaml(path))


def save_relay_config(path: str, data: dict[str, Any]) -> RelayConfig:
    """Persist a relay config dict, normalising it on the way out.

    Sibling top-level sections (notably ``auth``) are preserved: a save from
    the relay UI must never silently delete the admin password hash or session
    secret that live in the same file.
    """
    cfg = RelayConfig(
        enabled=bool(data.get("enabled", False)),
        strategy=str(data.get("strategy") or DEFAULT_STRATEGY),
        failure_threshold=data.get("failure_threshold", 3),
        cooldown_seconds=data.get("cooldown_seconds", 60.0),
        max_retries=data.get("max_retries", 2),
        timeout=data.get("timeout", 120.0),
        log_limit=data.get("log_limit", 1000),
        models=_as_dict(data.get("models")),
        groups=_as_dict(data.get("groups")),
        clients=[c for c in _as_list(data.get("clients")) if isinstance(c, dict)],
    )
    doc = _read_yaml(path)
    doc["relay"] = cfg.to_dict()
    _write_yaml(path, doc)
    return cfg


def load_auth_config(path: str) -> dict[str, Any]:
    """Read the ``auth:`` section (admin credentials + session secret)."""
    return _as_dict(_read_yaml(path).get("auth"))


def save_auth_config(path: str, data: dict[str, Any]) -> dict[str, Any]:
    """Write the ``auth:`` section, leaving ``relay:`` untouched."""
    doc = _read_yaml(path)
    merged = _as_dict(doc.get("auth"))
    merged.update(_as_dict(data))
    doc["auth"] = merged
    _write_yaml(path, doc)
    return merged


# ----------------------------------------------------------------- model pool

@dataclass
class RelayMember:
    """One upstream that can serve a pool entry.

    ``model`` is the *upstream's* model name, which for a group need not match
    the name the client asked for. ``available`` is False when a group's member
    no longer exists in the provider config: it is kept visible (so the UI can
    say "this reference is broken") but never selected.
    """

    provider: str
    model: str
    base_url: str
    api_key: str = ""
    api_key_env: str = ""
    price_in: float | None = None
    price_out: float | None = None
    context_length: int | None = None
    weight: int = UNSET_WEIGHT
    priority: int = UNSET_PRIORITY
    available: bool = True

    def resolved_api_key(self) -> str:
        return self._target().resolved_api_key()

    def _target(self) -> Target:
        return Target(
            name=f"{self.provider}/{self.model}",
            base_url=self.base_url,
            model=self.model,
            api_key=self.api_key,
            api_key_env=self.api_key_env,
            price_in=self.price_in,
            price_out=self.price_out,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "weight": self.weight,
            "priority": self.priority,
            "context_length": self.context_length,
            "available": self.available,
            "has_price": self.price_in is not None or self.price_out is not None,
        }


@dataclass
class ModelPool:
    """Everything the router may choose between for one requested model name.

    ``kind`` is ``"auto"`` for same-name aggregation (the model name *is* the
    pool) or ``"group"`` for a user-defined alias whose members can be entirely
    different models.
    """

    name: str
    members: list[RelayMember] = field(default_factory=list)
    kind: str = "auto"
    description: str = ""

    @property
    def multi(self) -> bool:
        return len(self.members) > 1

    def usable_members(self) -> list[RelayMember]:
        return [m for m in self.members if m.available]

    def to_dict(self, breaker: "CircuitBreaker | None" = None) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "description": self.description,
            "multi": self.multi,
            "members": [
                {**m.to_dict(), "breaker": breaker.snapshot(m.provider) if breaker else {}}
                for m in self.members
            ],
        }


def build_model_pool(
    providers: list[Provider],
    config: RelayConfig | None = None,
) -> dict[str, ModelPool]:
    """Build every pool: user-defined groups plus automatic same-name pools.

    Two sources feed the same structure so the router does not care which it
    got. Precedence when a group is named like a model: the group wins, because
    a hand-written group is a deliberate choice and silently ignoring it would
    route to the wrong upstreams.
    """
    cfg = config or RelayConfig()
    by_provider: dict[str, Provider] = {p.name: p for p in providers}
    # (provider, model) -> the model entry, for validating group members.
    catalogue: dict[tuple[str, str], Any] = {
        (p.name, m.name): m for p in providers for m in p.models
    }

    pools: dict[str, ModelPool] = {}

    # --- automatic: same model name offered by several providers ---
    for p in providers:
        for m in p.models:
            settings = cfg.model_settings(m.name)
            per_member = (settings.get("members") or {}).get(p.name) or {}
            pools.setdefault(m.name, ModelPool(name=m.name)).members.append(
                RelayMember(
                    provider=p.name,
                    model=m.name,
                    base_url=p.base_url,
                    api_key=p.api_key,
                    api_key_env=p.api_key_env,
                    price_in=m.price_in,
                    price_out=m.price_out,
                    context_length=m.context_length,
                    weight=_int_or(per_member.get("weight"), UNSET_WEIGHT),
                    priority=_int_or(per_member.get("priority"), UNSET_PRIORITY),
                )
            )

    # --- user-defined groups ---
    for name, group in cfg.groups.items():
        if not isinstance(group, dict):
            continue
        members: list[RelayMember] = []
        for entry in _as_list(group.get("members")):
            if not isinstance(entry, dict):
                continue
            provider = str(entry.get("provider") or "")
            model = str(entry.get("model") or "")
            prov = by_provider.get(provider)
            model_entry = catalogue.get((provider, model))
            # A member can be broken (provider deleted, model renamed). Keep it
            # so the operator sees the dangling reference instead of watching a
            # group silently shrink.
            members.append(RelayMember(
                provider=provider,
                model=model,
                base_url=prov.base_url if prov else "",
                api_key=prov.api_key if prov else "",
                api_key_env=prov.api_key_env if prov else "",
                price_in=getattr(model_entry, "price_in", None),
                price_out=getattr(model_entry, "price_out", None),
                context_length=getattr(model_entry, "context_length", None),
                weight=_int_or(entry.get("weight"), UNSET_WEIGHT),
                priority=_int_or(entry.get("priority"), UNSET_PRIORITY),
                available=prov is not None and model_entry is not None,
            ))
        pools[name] = ModelPool(
            name=name,
            members=members,
            kind="group",
            description=str(group.get("description") or ""),
        )

    for pool in pools.values():
        pool.members.sort(key=lambda mem: (mem.provider, mem.model))
    return pools


def _int_or(value: Any, default: int) -> int:
    """Coerce to int, treating None as "not configured".

    0 is a legitimate value (weight 0 = never route here, priority 0 = highest),
    so a falsy check would silently discard it.
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ------------------------------------------------------------ circuit breaker

@dataclass
class _BreakerState:
    consecutive_failures: int = 0
    cooldown_until: float = 0.0
    last_error: str = ""
    last_failure_at: float = 0.0


class CircuitBreaker:
    """Per-provider failure tracking: N consecutive failures → cool down.

    State lives in memory. A restart forgets it, which costs at most one extra
    attempt against a provider that just died — an acceptable price for not
    adding persistence (and its failure modes) to an optional feature.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0):
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._state: dict[str, _BreakerState] = {}

    def _state_of(self, provider: str) -> _BreakerState:
        st = self._state.get(provider)
        if st is None:
            st = _BreakerState()
            self._state[provider] = st
        return st

    def is_available(self, provider: str, now: float | None = None) -> bool:
        st = self._state.get(provider)
        if st is None:
            return True
        if st.consecutive_failures < self.failure_threshold:
            return True
        return (now if now is not None else time.time()) >= st.cooldown_until

    def record_success(self, provider: str) -> None:
        st = self._state_of(provider)
        st.consecutive_failures = 0
        st.cooldown_until = 0.0
        st.last_error = ""

    def record_failure(self, provider: str, error: str = "", now: float | None = None) -> None:
        st = self._state_of(provider)
        t = now if now is not None else time.time()
        st.consecutive_failures += 1
        st.last_error = error
        st.last_failure_at = t
        if st.consecutive_failures >= self.failure_threshold:
            st.cooldown_until = t + self.cooldown_seconds

    def cooldown_remaining(self, provider: str, now: float | None = None) -> float:
        st = self._state.get(provider)
        if st is None or st.cooldown_until <= 0:
            return 0.0
        return max(0.0, st.cooldown_until - (now if now is not None else time.time()))

    def snapshot(self, provider: str, now: float | None = None) -> dict[str, Any]:
        st = self._state.get(provider)
        remaining = self.cooldown_remaining(provider, now)
        return {
            "consecutive_failures": st.consecutive_failures if st else 0,
            "state": "open" if remaining > 0 else "closed",
            "cooldown_remaining": round(remaining, 1),
            "last_error": st.last_error if st else "",
        }

    def reset(self, provider: str = "") -> None:
        if provider:
            self._state.pop(provider, None)
        else:
            self._state.clear()

    def apply_config(self, config: RelayConfig) -> None:
        self.failure_threshold = max(1, int(config.failure_threshold))
        self.cooldown_seconds = max(0.0, float(config.cooldown_seconds))


class PoolNotFound(LookupError):
    """The requested model name is not offered by any configured provider."""


class NoAvailableMember(RuntimeError):
    """Every provider for this model is currently cooling down."""


class RelayDisabled(RuntimeError):
    """The relay is switched off in relay.yaml."""


class Router:
    """Chooses which provider serves a model, honouring the breaker."""

    def __init__(self, config: RelayConfig, breaker: CircuitBreaker | None = None,
                 rng: random.Random | None = None):
        self.config = config
        self.breaker = breaker if breaker is not None else CircuitBreaker(
            config.failure_threshold, config.cooldown_seconds
        )
        self.breaker.apply_config(config)
        self._rng = rng or random.Random()
        self._cursors: dict[str, int] = {}

    def apply_config(self, config: RelayConfig) -> None:
        self.config = config
        self.breaker.apply_config(config)

    def select(self, pool: ModelPool) -> RelayMember:
        """Pick one member, skipping broken members and cooling-down providers.

        Ordering is deterministic (provider, model) so round_robin rotation is
        reproducible instead of depending on dict iteration order.
        """
        candidates = [m for m in pool.members if m.available]
        if not candidates:
            raise NoAvailableMember(
                f"pool {pool.name!r} has no usable member "
                f"({len(pool.members)} configured, all referencing missing providers/models)"
            )
        available = [m for m in candidates if self.breaker.is_available(m.provider)]
        if not available:
            raise NoAvailableMember(
                f"all {len(candidates)} providers for {pool.name!r} are cooling down"
            )

        strategy = self.config.strategy_for(pool.name)
        if strategy == "priority":
            # "Prefer the highest priority, then spread inside that tier."
            # Picking min() outright would pin every call to one provider
            # whenever two members share a priority — which is the default
            # state for every member, so a freshly created group would never
            # fail over at all.
            best = min(m.priority for m in available)
            tier = [m for m in available if m.priority == best]
            if len(tier) == 1:
                return tier[0]
            idx = self._cursors.get(pool.name, 0) % len(tier)
            self._cursors[pool.name] = idx + 1
            return tier[idx]
        if strategy == "random":
            return self._rng.choice(available)
        if strategy == "weighted":
            weights = [max(0, m.weight) for m in available]
            if sum(weights) <= 0:
                return self._rng.choice(available)
            return self._rng.choices(available, weights=weights, k=1)[0]
        # round_robin (default)
        idx = self._cursors.get(pool.name, 0) % len(available)
        self._cursors[pool.name] = idx + 1
        return available[idx]

    def on_success(self, provider: str) -> None:
        self.breaker.record_success(provider)

    def on_failure(self, provider: str, error: str = "") -> None:
        self.breaker.record_failure(provider, error)


# ------------------------------------------------------------------ call log

@dataclass
class RelayCall:
    """One relayed request. Metrics only — never prompts or keys."""

    id: str
    started_at: float
    model: str
    provider: str = ""
    # Which client key made the call (label only — never the key itself).
    # With one relay shared by a team, "who is hammering the provider" is the
    # first question asked when a bill arrives.
    client: str = ""
    ok: bool = False
    stream: bool = False
    ttft: float | None = None
    tps: float | None = None
    e2e: float | None = None
    tokens: int = 0
    input_tokens: int = 0
    attempts: int = 1
    retried: int = 0
    tried: list[str] = field(default_factory=list)   # providers, in order
    error: str = ""
    error_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "started_at": self.started_at,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started_at)),
            "model": self.model,
            "provider": self.provider,
            "client": self.client,
            "ok": self.ok,
            "stream": self.stream,
            "ttft": self.ttft,
            "tps": self.tps,
            "e2e": self.e2e,
            "tokens": self.tokens,
            "input_tokens": self.input_tokens,
            "attempts": self.attempts,
            "retry": self.retried,
            "tried": list(self.tried),
            "error": self.error,
            "error_type": self.error_type,
        }


class RelayLog:
    """Bounded in-memory ring buffer of recent calls.

    A relay is a long-lived process; an unbounded list would be a slow memory
    leak. ``log_limit`` entries is plenty for a UI page and costs nothing.
    """

    def __init__(self, limit: int = 1000):
        self._items: deque[RelayCall] = deque(maxlen=max(1, int(limit)))
        self.limit = max(1, int(limit))

    def set_limit(self, limit: int) -> None:
        limit = max(1, int(limit))
        if limit == self.limit:
            return
        self.limit = limit
        kept = list(self._items)[-limit:]
        self._items = deque(kept, maxlen=limit)

    def append(self, call: RelayCall) -> None:
        self._items.append(call)

    def list(self, limit: int = 200, model: str = "", provider: str = "",
             client: str = "") -> list[dict[str, Any]]:
        items = list(self._items)
        if model:
            items = [c for c in items if c.model == model]
        if provider:
            items = [c for c in items if c.provider == provider]
        if client:
            items = [c for c in items if c.client == client]
        items.reverse()  # newest first
        if limit and limit > 0:
            items = items[:limit]
        return [c.to_dict() for c in items]

    def stats(self, model: str = "") -> dict[str, Any]:
        items = [c for c in self._items if not model or c.model == model]
        ok = [c for c in items if c.ok]
        ttfts = [c.ttft for c in ok if c.ttft is not None]
        tpss = [c.tps for c in ok if c.tps is not None]
        retried = sum(1 for c in items if c.retried)
        n = len(items)

        def avg(xs: list[float]) -> float | None:
            return sum(xs) / len(xs) if xs else None

        return {
            "total": n,
            "ok": len(ok),
            "failed": n - len(ok),
            "success_rate": (len(ok) / n * 100) if n else None,
            "avg_ttft": avg(ttfts),
            "avg_tps": avg(tpss),
            "retried": retried,
            "total_tokens": sum(c.tokens for c in ok),
        }

    def clear(self) -> None:
        self._items.clear()


# ---------------------------------------------------------------------- probe

# One cheap request per member: the point is "is it alive, and how fast is the
# first token", not a benchmark. Bounded by max_tokens and a short timeout so a
# dead endpoint cannot hold the dialog hostage.
PROBE_PROMPT = "hi"
PROBE_MAX_TOKENS = 8
PROBE_TIMEOUT = 20.0
PROBE_CONCURRENCY = 6


@dataclass
class ProbeResult:
    """Outcome of one connectivity/TTFT probe against a single member."""

    provider: str
    model: str
    ok: bool = False
    ttft: float | None = None
    tps: float | None = None
    e2e: float | None = None
    tokens: int = 0
    error: str = ""
    error_type: str = ""
    at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "ttft": self.ttft,
            "tps": self.tps,
            "e2e": self.e2e,
            "tokens": self.tokens,
            "error": self.error,
            "error_type": self.error_type,
            "at": self.at,
        }


async def probe_member(
    client: httpx.AsyncClient,
    member: RelayMember,
    *,
    timeout: float = PROBE_TIMEOUT,
) -> ProbeResult:
    """Send one tiny streaming request and report reachability + TTFT.

    Deliberately does NOT touch the circuit breaker: a manual test is a
    measurement, not traffic. The breaker should reflect what real callers
    experience, otherwise pressing a button changes production routing.
    """
    from .client import stream_request

    result = ProbeResult(provider=member.provider, model=member.model)
    if not member.available:
        result.error = (f"member references a provider/model that no longer exists "
                        f"({member.provider}/{member.model})")
        result.error_type = "missing_member"
        return result

    try:
        rr = await stream_request(
            client, member._target(), PROBE_PROMPT,
            temperature=0.0,
            timeout=timeout,
            include_usage=True,
            prompt_id="relay-probe",
            concurrency=1,
            max_tokens=PROBE_MAX_TOKENS,
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never explode
        result.error = str(exc)
        result.error_type = "probe_error"
        return result

    result.ok = rr.ok
    result.ttft = rr.ttft
    # tps is recorded but not surfaced: with max_tokens=8 the generation window
    # is a few milliseconds, so the number swings wildly and reads as a
    # throughput claim it cannot support. TTFT is the honest signal here.
    result.tps = rr.tokens_per_second
    result.e2e = rr.e2e
    result.tokens = rr.output_tokens
    result.error = rr.error
    result.error_type = rr.error_type
    return result


async def probe_members(
    members: list[RelayMember],
    *,
    timeout: float = PROBE_TIMEOUT,
    concurrency: int = PROBE_CONCURRENCY,
) -> list[ProbeResult]:
    """Probe several members concurrently, bounded by a semaphore.

    Results come back in the order the members were given, so the UI can zip
    them against its rows without re-sorting.
    """
    if not members:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async with httpx.AsyncClient(timeout=timeout) as client:
        async def one(m: RelayMember) -> ProbeResult:
            async with sem:
                return await probe_member(client, m, timeout=timeout)

        return list(await asyncio.gather(*(one(m) for m in members)))


def rank_members(
    members: list[RelayMember],
    results: list[ProbeResult],
) -> list[tuple[RelayMember, ProbeResult]]:
    """Order members best-first: reachable first by TTFT, dead ones last.

    Two properties matter more than the exact rule:
    - **A dead member never outranks a live one.** Routing to a known-dead
      upstream wastes the caller's latency and money.
    - **Ties are broken deterministically** (provider then model name), so the
      same inputs always produce the same order and a diff means something
      actually changed.
    """
    by_key = {(r.provider, r.model): r for r in results}

    def sort_key(item: tuple[RelayMember, ProbeResult]):
        m, r = item
        reachable = 1 if (r.ok and r.ttft is not None) else 0
        return (
            0 if reachable else 1,          # live before dead
            r.ttft if r.ttft is not None else 0.0,   # fastest first
            m.provider,                    # deterministic tie-break
            m.model,
        )

    paired = [(m, by_key.get((m.provider, m.model), ProbeResult(m.provider, m.model)))
              for m in members]
    return sorted(paired, key=sort_key)


# ------------------------------------------------------------------- service

@dataclass
class RelayOutcome:
    """Result of one relay attempt sequence, ready to be turned into a response."""

    status_code: int
    call: RelayCall
    body: dict[str, Any] | None = None
    stream: Any = None            # async iterator of raw bytes, when streaming
    headers: dict[str, str] = field(default_factory=dict)


def _error_body(message: str, error_type: str, call: RelayCall) -> dict[str, Any]:
    return {
        "error": {
            "message": message,
            "type": error_type or "relay_error",
            "code": error_type or "relay_error",
            "relay": {
                "model": call.model,
                "attempts": call.attempts,
                "tried": list(call.tried),
            },
        }
    }


def _classify_status(status: int) -> str:
    return ErrorType.from_status(status).value


def _upstream_error_body(raw: bytes, fallback: dict[str, Any],
                         call: "RelayCall | None" = None) -> dict[str, Any]:
    """Use the upstream's own error object when it sent a JSON one.

    Wrapping it would replace the provider's real error type (``rate_limit_exceeded``,
    ``context_length_exceeded``, ...) with our generic label, which is exactly
    the information a client needs to react. The relay's own metadata is added
    under a namespaced ``error.relay`` key so nothing upstream is overwritten.
    Falls back to our own body when the upstream's error was not JSON.
    """
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, UnicodeDecodeError):
        return fallback
    if not isinstance(data, dict) or not data:
        return fallback
    if call is not None and isinstance(data.get("error"), dict):
        data["error"]["relay"] = {
            "model": call.model,
            "attempts": call.attempts,
            "tried": list(call.tried),
        }
    return data


class RelayService:
    """Forwards OpenAI chat-completion requests to a chosen provider.

    Built per request from the current providers + config, but the breaker and
    the call log are shared objects owned by the caller (the API layer) so
    state survives across requests.
    """

    def __init__(
        self,
        config: RelayConfig,
        providers: list[Provider],
        breaker: CircuitBreaker | None = None,
        log: RelayLog | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        rng: random.Random | None = None,
    ):
        self.config = config
        self.providers = providers
        self.log = log if log is not None else RelayLog(config.log_limit)
        self.log.set_limit(config.log_limit)
        self.router = Router(config, breaker=breaker, rng=rng)
        self._client_factory = client_factory or (
            lambda: httpx.AsyncClient(timeout=config.timeout)
        )
        self._pools = build_model_pool(providers, config)

    # ---- introspection ----

    def pools(self) -> dict[str, ModelPool]:
        return self._pools

    def get_model(self, model: str) -> ModelPool:
        pool = self._pools.get(model)
        if pool is None:
            raise PoolNotFound(f"model {model!r} is not offered by any provider")
        return pool

    def models_payload(self) -> dict[str, Any]:
        """OpenAI-compatible /v1/models over every pool.

        A group appears as a single model id, which is the whole point: a
        client that only knows "A" can discover it here without ever seeing the
        upstream names behind it.
        """
        data = []
        for name, pool in sorted(self._pools.items()):
            usable = pool.usable_members()
            ctxs = [m.context_length for m in usable if m.context_length]
            data.append({
                "id": name,
                "object": "model",
                "created": 0,
                "owned_by": "llm-evl-relay",
                "context_length": min(ctxs) if ctxs else None,
                "kind": pool.kind,
                "providers": [m.provider for m in usable],
                "n_providers": len(usable),
            })
        return {"object": "list", "data": data}

    def pool_overview(self) -> list[dict[str, Any]]:
        out = []
        for name, pool in self._pools.items():
            d = pool.to_dict(self.router.breaker)
            # Present members in the order they will actually be tried.
            # Without this the UI lists them alphabetically, so after
            # "按实测重排优先级" the numbers change but the rows do not move —
            # the one thing the operator is trying to read.
            d["members"] = sorted(
                d["members"],
                key=lambda m: (m.get("priority", UNSET_PRIORITY),
                               m.get("provider", ""), m.get("model", "")),
            )
            d["strategy"] = self.config.strategy_for(name)
            d["multi"] = pool.multi
            d["n_usable"] = len(pool.usable_members())
            out.append(d)
        # Groups first: they are the hand-made, intentional surface.
        out.sort(key=lambda d: (d["kind"] != "group", -len(d["members"]), d["name"]))
        return out

    # ---- forwarding ----

    async def forward(self, body: dict[str, Any], *,
                      client_name: str = "") -> RelayOutcome:
        """Relay one request.

        ``client_name`` is the label of the client key that authenticated the
        call. It is only ever recorded in the log — never used for routing, and
        never sent upstream.
        """
        if not self.config.enabled:
            raise RelayDisabled(
                "relay is disabled; set enabled: true in relay.yaml (or flip the "
                "switch on the 中转站 · 模型池 page) to start forwarding"
            )
        model = str(body.get("model") or "").strip()
        if not model:
            raise ValueError("missing 'model' in request body")
        stream = bool(body.get("stream"))
        pool = self.get_model(model)

        call = RelayCall(
            id=uuid.uuid4().hex[:12],
            started_at=time.time(),
            model=model,
            stream=stream,
            client=client_name or "",
        )
        max_attempts = self.config.max_retries + 1
        last_error = ""
        last_error_type = ""

        for attempt in range(1, max_attempts + 1):
            call.attempts = attempt
            try:
                member = self.router.select(pool)
            except NoAvailableMember as exc:
                # Nothing left to try: either every provider is cooling down or
                # every member of the group points at something that no longer
                # exists. Either way a retry cannot help.
                call.error = str(exc)
                call.error_type = (
                    ErrorType.RATE_LIMIT.value if pool.usable_members()
                    else "no_usable_member"
                )
                call.e2e = time.time() - call.started_at
                self.log.append(call)
                return RelayOutcome(
                    status_code=503 if pool.usable_members() else 422,
                    call=call,
                    body=_error_body(str(exc), call.error_type, call),
                )

            call.tried.append(member.provider)
            t0 = time.perf_counter()
            # The `model` the client sent is the *pool* name; the upstream only
            # understands its own model name. For a group these differ, so the
            # rewrite is mandatory, not cosmetic.
            payload = {**body, "model": member.model}
            # The client is NOT used as a context manager here: on the
            # streaming path its lifetime must outlive this loop iteration,
            # because the response body is consumed later by the generator
            # handed to the HTTP layer. Closing it here would kill the stream.
            client = self._client_factory()
            try:
                outcome = await self._attempt(
                    client, member, payload, call, stream, t0
                )
            except httpx.TimeoutException:
                outcome = self._failed_attempt(
                    call, 504, f"timeout after {self.config.timeout}s "
                               f"calling {member.provider}",
                    ErrorType.TIMEOUT.value, member.provider,
                )
            except httpx.HTTPError as exc:
                outcome = self._failed_attempt(
                    call, 502, f"transport error calling {member.provider}: {exc}",
                    ErrorType.TRANSPORT.value, member.provider,
                )
            except Exception as exc:  # noqa: BLE001 - relay must not 500 silently
                logger.exception("relay attempt failed unexpectedly")
                outcome = self._failed_attempt(
                    call, 502, f"relay error calling {member.provider}: {exc}",
                    "relay_error", member.provider,
                )
            finally:
                # Whoever did not take ownership of the client closes it.
                # The streaming success path hands it to _stream_passthrough.
                if outcome.stream is None:
                    await client.aclose()

            if outcome.status_code < 400:
                call.retried = attempt - 1
                return outcome

            # Failure: decide whether trying another provider can help.
            self.router.on_failure(member.provider, outcome.call.error)
            last_error = outcome.call.error
            last_error_type = outcome.call.error_type
            retryable = outcome.call.error_type in RETRYABLE_ERROR_TYPES
            if not retryable or attempt >= max_attempts:
                outcome.call.retried = attempt - 1
                self.log.append(outcome.call)
                return outcome
            logger.info(
                "relay: %s failed (%s), trying another provider for %s",
                member.provider, last_error_type, model,
            )

        # Unreachable: the loop always returns. Kept as a guard.
        call.error = last_error or "relay failed"
        call.error_type = last_error_type or "relay_error"
        self.log.append(call)
        return RelayOutcome(
            status_code=502,
            call=call,
            body=_error_body(call.error, call.error_type, call),
        )

    def _failed_attempt(self, call: RelayCall, status: int, message: str,
                        error_type: str, provider: str) -> RelayOutcome:
        call.ok = False
        call.provider = provider
        call.error = message
        call.error_type = error_type
        call.e2e = time.time() - call.started_at
        return RelayOutcome(
            status_code=status,
            call=call,
            body=_error_body(message, error_type, call),
        )

    async def _attempt(self, client: httpx.AsyncClient, member: RelayMember,
                       payload: dict[str, Any], call: RelayCall,
                       stream: bool, t0: float) -> RelayOutcome:
        """One upstream attempt. Never raises for upstream problems."""
        url = member.base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {member.resolved_api_key()}",
            "Content-Type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"

        if stream:
            req = client.build_request("POST", url, json=payload, headers=headers,
                                       timeout=self.config.timeout)
            resp = await client.send(req, stream=True)
            if resp.status_code >= 400:
                raw = await resp.aread()
                await resp.aclose()
                etype = _classify_status(resp.status_code)
                detail = raw[:300].decode("utf-8", "replace").strip()
                outcome = self._failed_attempt(
                    call, resp.status_code,
                    f"HTTP {resp.status_code} from {member.provider}: {detail}",
                    etype, member.provider,
                )
                # The upstream's own error object is forwarded untouched: an
                # OpenAI SDK on the other end already knows how to read it,
                # and re-wrapping it only loses information.
                outcome.body = _upstream_error_body(raw, outcome.body, call)
                outcome.headers = {"x-relay-provider": member.provider}
                return outcome

            call.provider = member.provider
            self.router.on_success(member.provider)
            call.ok = True
            outcome = RelayOutcome(
                status_code=resp.status_code,
                call=call,
                stream=self._stream_passthrough(client, resp, member, call),
                headers={
                    "x-relay-provider": member.provider,
                    "cache-control": "no-cache",
                },
            )
            return outcome

        resp = await client.post(url, json=payload, headers=headers,
                                 timeout=self.config.timeout)
        if resp.status_code >= 400:
            etype = _classify_status(resp.status_code)
            detail = resp.text[:300].strip()
            outcome = self._failed_attempt(
                call, resp.status_code,
                f"HTTP {resp.status_code} from {member.provider}: {detail}",
                etype, member.provider,
            )
            outcome.body = _upstream_error_body(resp.content, outcome.body, call)
            outcome.headers = {"x-relay-provider": member.provider}
            return outcome

        call.ok = True
        call.provider = member.provider
        self.router.on_success(member.provider)
        try:
            data = resp.json()
        except ValueError:
            data = {"raw": resp.text}

        # Non-streaming has no token-by-token signal, so TTFT would be a lie
        # here. Leave it None and let e2e speak.
        call.e2e = time.perf_counter() - t0
        usage = data.get("usage") if isinstance(data, dict) else None
        text = _text_of(data)
        n_tokens, _src = count_tokens(text, usage)
        call.tokens = n_tokens
        call.input_tokens = int((usage or {}).get("prompt_tokens") or 0)
        self.log.append(call)
        return RelayOutcome(
            status_code=resp.status_code,
            call=call,
            body=data,
            headers={"x-relay-provider": member.provider},
        )

    async def _stream_passthrough(
        self,
        client: httpx.AsyncClient,
        resp: httpx.Response,
        member: RelayMember,
        call: RelayCall,
    ) -> AsyncIterator[bytes]:
        """Forward upstream bytes verbatim while measuring them.

        Bytes go out untouched — the relay is not allowed to rewrite an
        upstream's answer. A parallel line-splitter exists only to read
        metrics (TTFT, token count, usage) and is deliberately forgiving: a
        malformed line increments ``malformed`` and nothing else, because the
        client's answer matters more than our bookkeeping.

        Retries are impossible from here on: the client already has bytes, so
        a failure is reported by cutting the stream and logging it.
        """
        t0 = time.perf_counter()
        first_content_at: float | None = None
        buf = b""
        parts: list[str] = []
        kept_chars = 0
        usage: dict | None = None
        malformed = 0
        error_type = ""
        error = ""
        completed = False
        try:
            async for raw in resp.aiter_bytes():
                yield raw
                buf += raw
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.strip()
                    if not text.startswith(b"data:"):
                        continue
                    payload = text[len(b"data:"):].strip()
                    if payload == b"[DONE]":
                        continue
                    try:
                        chunk = json.loads(payload)
                    except (ValueError, UnicodeDecodeError):
                        malformed += 1
                        continue
                    if not isinstance(chunk, dict):
                        malformed += 1
                        continue
                    u = chunk.get("usage")
                    if isinstance(u, dict) and u:
                        usage = u
                    delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                    content = delta.get("content")
                    if content and first_content_at is None:
                        first_content_at = time.perf_counter()
                    if content and kept_chars < _MAX_TEXT_FOR_COUNTING:
                        parts.append(str(content))
                        kept_chars += len(str(content))
            completed = True
        except httpx.TimeoutException:
            error_type = ErrorType.TIMEOUT.value
            error = f"stream timeout calling {member.provider}"
            raise
        except httpx.HTTPError as exc:
            error_type = ErrorType.TRANSPORT.value
            error = f"stream transport error calling {member.provider}: {exc}"
            raise
        finally:
            # This generator owns both the response and its client now.
            for closeable in (resp, client):
                try:
                    await closeable.aclose()
                except Exception:  # noqa: BLE001 - closing must never mask the result
                    pass
            if not completed and not error_type:
                # The client hung up mid-stream. Without this branch the call
                # would leave no log record at all, and the caller who
                # disconnected would be invisible in the attribution trail —
                # which is exactly the case you most want to see.
                error_type = ErrorType.CANCELLED.value
                error = (f"client disconnected before the stream from "
                         f"{member.provider} finished")
            self._finish_stream(call, member, t0, first_content_at, parts, usage,
                                malformed, error_type, error)

    def _finish_stream(self, call: RelayCall, member: RelayMember, t0: float,
                       first_content_at: float | None, parts: list[str],
                       usage: dict | None, malformed: int,
                       error_type: str, error: str) -> None:
        t_end = time.perf_counter()
        text = "".join(parts)
        n_tokens, _src = count_tokens(text, usage)
        call.tokens = n_tokens
        call.input_tokens = int((usage or {}).get("prompt_tokens") or 0)
        call.e2e = t_end - t0
        if first_content_at is not None:
            call.ttft = first_content_at - t0
            gen = t_end - first_content_at
            call.tps = (n_tokens / gen) if gen > 0 and n_tokens else None
        if error_type:
            call.ok = False
            call.error_type = error_type
            call.error = error or "stream failed"
        elif not n_tokens and not call.input_tokens:
            # A stream that ended with no content and no usage produced nothing
            # the client can use. Reporting it as a success would be a lie, and
            # it is the single most common way an upstream silently fails.
            call.ok = False
            call.error_type = (
                ErrorType.PARSE.value if malformed else ErrorType.NO_CONTENT.value
            )
            call.error = (
                f"upstream stream produced no usable content"
                + (f" ({malformed} malformed chunks)" if malformed else "")
            )
        self.log.append(call)


def _text_of(data: Any) -> str:
    """Best-effort extraction of the assistant text from a non-stream reply."""
    if not isinstance(data, dict):
        return ""
    choices = data.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    return str(msg.get("content") or "")
