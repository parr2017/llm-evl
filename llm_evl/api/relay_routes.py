"""Relay routes.

Two audiences:

- ``/v1/*``  — the OpenAI-compatible surface a client talks to. It speaks the
  upstream's language, including its status codes, so an existing SDK works
  without the client knowing a relay exists. These are the only routes on the
  LAN-facing server, and every one of them requires a client key.
- ``/api/relay/*`` — the control surface the web UI uses to inspect and steer
  the relay (config, groups, client keys, breaker health, recent calls). These
  live on the loopback-only management server.

This router must be registered *before* the main ``routes`` router: the SPA
fallback in ``routes.py`` answers every unmatched path with index.html, and
without that ordering ``/v1/chat/completions`` would return HTML.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..core.auth import find_client, generate_key, make_client
from ..core.relay import PoolNotFound, RelayDisabled
from .relay_manager import relay_manager

logger = logging.getLogger(__name__)

# Two routers, deliberately separate.
#
#   router     -> /api/relay/*  control surface, management server only
#   v1_router  -> /v1/*         the only thing the LAN-facing server mounts
#
# Keeping them apart is a security boundary, not a style choice: the control
# surface can mint client keys, so mounting it on the LAN port would let any
# caller grant themselves access to the relay.
router = APIRouter()
v1_router = APIRouter()

# How stale ``last_used_at`` may get before the config file is rewritten.
# Updating it on every request would mean a disk write per relayed call; a
# minute of lag is irrelevant for "when did this person last use the relay".
_LAST_USED_WRITE_INTERVAL = 60.0


class RelayConfigRequest(BaseModel):
    enabled: bool | None = None
    strategy: str | None = None
    failure_threshold: int | None = None
    cooldown_seconds: float | None = None
    max_retries: int | None = None
    timeout: float | None = None
    log_limit: int | None = None
    models: dict | None = None
    groups: dict | None = None


class CreateClientRequest(BaseModel):
    name: str = ""
    groups: list[str] = []


class ProbeRequest(BaseModel):
    # Either a whole group...
    group: str = ""
    # ...or an explicit member list. Explicit entries are validated against
    # targets.yaml; see RelayManager._resolve_members.
    members: list[dict] | None = None


# ------------------------------------------------------------ key checking

def _bearer(request: Request) -> str:
    raw = request.headers.get("authorization", "") or ""
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    # Some SDKs put the key in the query string instead of the header.
    return (request.query_params.get("api_key") or "").strip()


def _api_error(status: int, message: str, code: str) -> JSONResponse:
    """An OpenAI-shaped error body, so SDKs parse our failures like upstream's."""
    return JSONResponse(
        status_code=status,
        content={"error": {"message": message, "type": code, "code": code}},
    )


def authenticate_client(request: Request) -> tuple[dict | None, JSONResponse | None]:
    """Check the presented Bearer key. Returns ``(client, error_response)``.

    The presented key is only ever compared against stored hashes, so this
    function cannot learn anybody's plaintext key.
    """
    cfg = relay_manager.config()
    if not cfg.clients:
        return None, _api_error(
            401,
            "relay has no client keys yet — create one in 中转站 · 模型池 "
            "(客户端 key), then send: Authorization: Bearer <key>",
            "no_client_keys",
        )
    presented = _bearer(request)
    if not presented:
        return None, _api_error(
            401, "missing API key. Send it as: Authorization: Bearer <key>",
            "invalid_api_key",
        )
    client = find_client(cfg.clients, presented)
    if client is None:
        logger.info("relay: rejected bad key from %s",
                    request.client.host if request.client else "?")
        return None, _api_error(401, "invalid API key", "invalid_api_key")
    if not client.get("enabled", True):
        return None, _api_error(
            401, f"API key '{client.get('name', '?')}' is disabled", "invalid_api_key")
    return client, None


def _allowed_groups(client: dict) -> list[str]:
    g = client.get("groups")
    return [str(x) for x in g] if isinstance(g, list) else []


def _touch_client(client: dict, ip: str) -> None:
    """Record last use, throttled so a busy relay is not a busy disk.

    The update is applied to the same config object that is written back.
    Re-reading the file here and mutating a *different* copy would silently
    drop the change.
    """
    now = time.time()
    last = float(client.get("last_used_at") or 0)
    if now - last < _LAST_USED_WRITE_INTERVAL and client.get("last_used_ip") == ip:
        return
    cfg = relay_manager.config()
    for stored in cfg.clients:
        if stored.get("name") == client.get("name"):
            stored["last_used_at"] = now
            stored["last_used_ip"] = ip
            try:
                relay_manager.save_config(cfg.to_dict())
            except Exception as exc:  # noqa: BLE001 - bookkeeping must not break a call
                logger.warning("relay: failed to persist client last_used: %s", exc)
            return


# ---- control surface (web UI, loopback only) ----

@router.get("/api/relay/config")
def get_relay_config():
    return relay_manager.overview()


@router.post("/api/relay/config")
def save_relay_config(req: RelayConfigRequest):
    """Merge the incoming partial config over what is on disk.

    The UI edits a few fields at a time (a weight here, a strategy there), so a
    partial payload must not wipe the rest of the file. Client keys are never
    taken from this payload — they have their own endpoints, so a stray save
    cannot resurrect a revoked key.
    """
    current = relay_manager.config().to_dict()
    patch = {k: v for k, v in req.model_dump(exclude_none=True).items()
             if k != "clients"}
    current.update(patch)
    try:
        relay_manager.save_config(current)
    except Exception as exc:
        raise HTTPException(500, f"failed to save relay config: {exc}")
    return relay_manager.overview()


@router.get("/api/relay/logs")
def relay_logs(limit: int = 200, model: str = "", provider: str = "", client: str = ""):
    try:
        limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        limit = 200
    return relay_manager.logs(limit=limit, model=model, provider=provider, client=client)


@router.get("/api/relay/stats")
def relay_stats(model: str = ""):
    return relay_manager.stats(model=model)


@router.post("/api/relay/logs/clear")
def clear_relay_logs():
    relay_manager.log.clear()
    return {"ok": True}


@router.post("/api/relay/breakers/reset")
def reset_relay_breakers(provider: str = ""):
    return relay_manager.reset_breakers(provider)


# ---- connectivity probing ----

@router.post("/api/relay/probe")
def probe(req: ProbeRequest):
    """Test one member or a whole group: reachability + TTFT + tok/s.

    Sends a real (tiny) request per member, so it costs a few tokens each.
    Does not touch the circuit breaker — see core.relay.probe_member.
    """
    try:
        return {"results": relay_manager.probe(req.group, req.members)}
    except PoolNotFound as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.post("/api/relay/groups/{group}/auto-priority")
def auto_priority(group: str):
    """Re-probe the group and rewrite member priorities best-first.

    Live members first ordered by TTFT; unreachable ones sink to the bottom.
    Only ``priority`` changes — weights are left alone.
    """
    try:
        return relay_manager.auto_priority(group)
    except PoolNotFound as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


# ---- client keys ----

@router.post("/api/relay/clients")
def create_client(req: CreateClientRequest):
    """Mint a client key. The plaintext is returned exactly once, here."""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "name is required")
    key = generate_key()
    record = make_client(name, key, groups=req.groups)
    cfg = relay_manager.config()
    existing = [c for c in cfg.clients if c.get("name") != name]
    try:
        relay_manager.save_config({**cfg.to_dict(), "clients": [*existing, record]})
    except Exception as exc:
        raise HTTPException(500, f"failed to save client: {exc}")
    return {
        "client": record,
        "key": key,
        "warning": "save this key now — only its hash is stored, it cannot be shown again",
    }


@router.post("/api/relay/clients/{name}/enabled")
def toggle_client(name: str, enabled: bool = True):
    cfg = relay_manager.config()
    clients, found = [], False
    for c in cfg.clients:
        if c.get("name") == name:
            found = True
            c = {**c, "enabled": bool(enabled)}
        clients.append(c)
    if not found:
        raise HTTPException(404, f"client not found: {name}")
    relay_manager.save_config({**cfg.to_dict(), "clients": clients})
    return {"ok": True, "clients": clients}


@router.delete("/api/relay/clients/{name}")
def delete_client(name: str):
    cfg = relay_manager.config()
    clients = [c for c in cfg.clients if c.get("name") != name]
    if len(clients) == len(cfg.clients):
        raise HTTPException(404, f"client not found: {name}")
    relay_manager.save_config({**cfg.to_dict(), "clients": clients})
    return {"ok": True, "clients": clients}


# ---- OpenAI-compatible surface (LAN-facing, key required) ----

@v1_router.get("/v1/models")
def relay_models(request: Request):
    svc = relay_manager.service()
    if not svc.config.enabled:
        raise HTTPException(403, _disabled_msg())
    client, err = authenticate_client(request)
    if err is not None:
        return err
    payload = svc.models_payload()
    allowed = _allowed_groups(client)
    if allowed:
        # A key scoped to some groups must not be able to enumerate the rest.
        payload["data"] = [m for m in payload["data"] if m["id"] in allowed]
    return payload


@v1_router.post("/v1/chat/completions")
async def relay_chat_completions(request: Request):
    svc = relay_manager.service()
    if not svc.config.enabled:
        raise HTTPException(403, _disabled_msg())

    client, err = authenticate_client(request)
    if err is not None:
        return err
    _touch_client(client, request.client.host if request.client else "")

    try:
        body = await request.json()
    except Exception:
        return _api_error(400, "request body must be JSON", "invalid_request_error")
    if not isinstance(body, dict):
        return _api_error(400, "request body must be a JSON object",
                          "invalid_request_error")
    model = str(body.get("model") or "").strip()

    allowed = _allowed_groups(client)
    if allowed and model not in allowed:
        return _api_error(
            403,
            f"key '{client.get('name', '?')}' may not call model {model!r}; "
            f"allowed: {', '.join(allowed)}",
            "model_not_allowed",
        )

    try:
        outcome = await svc.forward(body, client_name=str(client.get("name", "")))
    except RelayDisabled as exc:
        raise HTTPException(403, str(exc))
    except PoolNotFound as exc:
        return _api_error(404, str(exc), "model_not_found")
    except ValueError as exc:
        return _api_error(400, str(exc), "invalid_request_error")

    headers = dict(outcome.headers)
    if outcome.stream is not None:
        return StreamingResponse(
            outcome.stream,
            status_code=outcome.status_code,
            media_type="text/event-stream",
            headers={**headers, "x-accel-buffering": "no"},
        )
    return JSONResponse(
        status_code=outcome.status_code,
        content=outcome.body if outcome.body is not None else {},
        headers=headers,
    )


def _disabled_msg() -> str:
    return (
        "relay is disabled. Open 中转站 · 模型池 in the web UI and turn the "
        f"中转开关 on (config: {relay_manager.relay_config_path})."
    )
