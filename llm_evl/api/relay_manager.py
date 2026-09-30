"""RelayManager: owns relay state that must survive across requests.

The providers and the relay config are re-read on every request (both are
small YAML files, and a benchmark UI expects a config edit to take effect on
the next click). What is *not* re-read is the state that only makes sense
cumulatively: circuit-breaker health and the recent-call log.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..core.evaluator import load_providers
from ..core.relay import (
    CircuitBreaker,
    PoolNotFound,
    ProbeResult,
    RelayConfig,
    RelayLog,
    RelayMember,
    RelayService,
    load_relay_config,
    probe_members,
    rank_members,
    save_relay_config,
)

logger = logging.getLogger(__name__)


class RelayManager:
    def __init__(self, targets_config_path: str = "targets.yaml",
                 relay_config_path: str = "relay.yaml"):
        self.targets_config_path = targets_config_path
        self.relay_config_path = relay_config_path
        self.breaker = CircuitBreaker()
        self.log = RelayLog()
        # Last probe per (provider, model). In-memory on purpose: it is a
        # reading of "right now", and a stale one is worse than none.
        self.probes: dict[tuple[str, str], dict] = {}
        # Filled in by server.run() once the listening ports are known.
        self.public_base_url: str = ""

    def config(self) -> RelayConfig:
        return load_relay_config(self.relay_config_path)

    def providers(self) -> list:
        try:
            return load_providers(self.targets_config_path)
        except FileNotFoundError:
            logger.warning("relay: %s not found; no providers to serve",
                           self.targets_config_path)
            return []
        except Exception as exc:
            logger.warning("relay: failed to load %s: %s",
                           self.targets_config_path, exc)
            return []

    def service(self) -> RelayService:
        """A fresh service view over current config + shared mutable state."""
        return RelayService(
            self.config(), self.providers(), breaker=self.breaker, log=self.log
        )

    def save_config(self, data: dict[str, Any]) -> RelayConfig:
        cfg = save_relay_config(self.relay_config_path, data)
        self.breaker.apply_config(cfg)
        self.log.set_limit(cfg.log_limit)
        return cfg

    def overview(self) -> dict[str, Any]:
        """Config + pools + client keys + breaker state, for the UI."""
        svc = self.service()
        cfg = svc.config
        return {
            "config": cfg.to_dict(),
            "config_path": self.relay_config_path,
            "endpoints": {
                # What colleagues should point their SDK at. Empty when the app
                # was created without going through `llm-evl serve` (tests).
                "public_base_url": self.public_base_url or "/v1",
                "chat": "/v1/chat/completions",
                "models": "/v1/models",
            },
            "pools": _with_probes(svc.pool_overview(), self.probes),
            "group_names": sorted(cfg.groups.keys()),
            "clients": [
                {k: v for k, v in c.items() if k != "key_hash"}
                for c in cfg.clients
            ],
            "n_providers": len(self.providers()),
            "stats": self.log.stats(),
        }

    def logs(self, limit: int = 200, model: str = "", provider: str = "",
             client: str = "") -> dict[str, Any]:
        return {
            "logs": self.log.list(limit=limit, model=model, provider=provider,
                                  client=client),
            "stats": self.log.stats(model=model),
            "log_limit": self.log.limit,
        }

    def stats(self, model: str = "") -> dict[str, Any]:
        return self.log.stats(model=model)

    # ---- connectivity probing / auto priority ----

    def _resolve_members(self, group: str = "",
                         members: list[dict] | None = None) -> list[RelayMember]:
        """Turn a group name or an explicit member list into RelayMembers.

        Explicit members are validated against the configured providers on
        purpose: without that check this endpoint would let an authenticated
        caller make the server fetch an arbitrary URL, which is an SSRF
        primitive we have no reason to offer.
        """
        svc = self.service()
        if group:
            pool = svc.get_model(group)
            return list(pool.members)
        if not members:
            raise ValueError("provide either 'group' or 'members'")
        catalogue = {
            (m.provider, m.model)
            for pool in svc.pools().values()
            for m in pool.members
        }
        out: list[RelayMember] = []
        for entry in members:
            provider = str((entry or {}).get("provider") or "")
            model = str((entry or {}).get("model") or "")
            if (provider, model) not in catalogue:
                raise ValueError(
                    f"unknown member {provider}/{model}: it is not in targets.yaml"
                )
            out.append(RelayMember(provider=provider, model=model, base_url=""))
        # Fill in base_url/keys from the real configuration.
        by_key = {
            (m.provider, m.model): m
            for pool in svc.pools().values() for m in pool.members
        }
        for i, m in enumerate(out):
            src = by_key[(m.provider, m.model)]
            out[i] = RelayMember(
                provider=src.provider, model=src.model, base_url=src.base_url,
                api_key=src.api_key, api_key_env=src.api_key_env,
                weight=src.weight, priority=src.priority, available=src.available,
            )
        return out

    def probe(self, group: str = "",
              members: list[dict] | None = None) -> list[dict]:
        """Measure reachability + TTFT for the given members (or a whole group)."""
        targets = self._resolve_members(group, members)
        results = asyncio.run(probe_members(targets))
        for r in results:
            self.probes[(r.provider, r.model)] = r.to_dict()
        return [r.to_dict() for r in results]

    def auto_priority(self, group: str) -> dict[str, Any]:
        """Re-probe a group, then rewrite member priorities best-first.

        Always re-measures instead of trusting the previous run: ordering by
        stale data can put a since-broken model at the front, which is worse
        than not offering the button at all.
        """
        svc = self.service()
        pool = svc.get_model(group)            # PoolNotFound -> 404
        if not pool.members:
            raise ValueError(f"group {group!r} has no members")
        results = asyncio.run(probe_members(list(pool.members)))
        for r in results:
            self.probes[(r.provider, r.model)] = r.to_dict()

        ranked = rank_members(list(pool.members), results)
        new_priority: dict[tuple[str, str], int] = {}
        for i, (m, _r) in enumerate(ranked):
            new_priority[(m.provider, m.model)] = i

        cfg = svc.config
        spec = dict((cfg.groups.get(group) or {}))
        # Written in ranked order, not the original array order: a human
        # opening relay.yaml should see the same sequence the router uses.
        ranked_members = [m for m, _r in ranked]
        spec["members"] = [
            {
                "provider": m.provider,
                "model": m.model,
                "weight": int(m.weight),
                "priority": new_priority[(m.provider, m.model)],
            }
            for m in ranked_members
        ]
        groups = dict(cfg.groups)
        groups[group] = spec
        relay_manager_cfg = {**cfg.to_dict(), "groups": groups}
        self.save_config(relay_manager_cfg)

        return {
            "group": group,
            "strategy": cfg.strategy_for(group),
            "results": [r.to_dict() for r in results],
            "ranking": [
                {
                    "provider": m.provider,
                    "model": m.model,
                    "priority": new_priority[(m.provider, m.model)],
                    "ok": r.ok,
                    "ttft": r.ttft,
                    "error_type": r.error_type,
                }
                for m, r in ranked
            ],
        }

    def reset_breakers(self, provider: str = "") -> dict[str, Any]:
        self.breaker.reset(provider)
        return {"ok": True, "reset": provider or "all"}


def _with_probes(pools: list[dict], probes: dict) -> list[dict]:
    """Annotate each member with its last probe reading, if any."""
    for pool in pools:
        for m in pool.get("members", []):
            m["probe"] = probes.get((m["provider"], m["model"]))
    return pools


relay_manager = RelayManager()
