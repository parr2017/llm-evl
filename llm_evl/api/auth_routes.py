"""Admin login: password gate, session cookie, login page.

Threat model for this module, in one paragraph: the management API returns
plaintext provider keys, so ``/api/*`` and the UI must not be reachable by
anyone but the operator. The main defence is that the management server binds
to 127.0.0.1 only (see ``server.run``); this login is the second layer, for when
that port gets forwarded, tunnelled or container-published by accident.

Design notes:

- **Middleware, not per-route dependencies.** A new endpoint added later is
  protected by default. Forgetting a decorator is how admin panels get breached.
- **Stateless signed cookie.** Nothing to store, nothing to leak, and a restart
  does not log everyone out.
- **First run bootstraps a password.** If no hash is configured, one is
  generated and printed once. Silently running with no password would be the
  worst possible default for a service holding credentials.
- **Escape hatch.** ``LLM_EVL_NO_AUTH=1`` disables the gate for local
  troubleshooting. Default is off, and the warning is loud.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from ..core.auth import (
    COOKIE_NAME,
    hash_password,
    new_password,
    sign_session,
    verify_password,
    verify_session,
)
from ..core.relay import load_auth_config, save_auth_config

logger = logging.getLogger(__name__)

router = APIRouter()

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

SESSION_HOURS = 12

# Paths that must stay reachable without a session, or nobody could ever log in.
PUBLIC_PATHS = frozenset({
    "/login",
    "/api/auth/login",
    "/api/auth/status",
    "/favicon.ico",
})

_state = {"config_path": "relay.yaml"}


def configure(config_path: str) -> None:
    _state["config_path"] = config_path


# --------------------------------------------------------------- bootstrap

def ensure_admin(relay_config_path: str) -> str | None:
    """Make sure there is an admin password; return a generated one, if any.

    Called once at startup. Returns the plaintext password when it had to
    invent one (so the caller can print it exactly once), otherwise None.
    """
    auth = load_auth_config(relay_config_path)
    admin = auth.get("admin") if isinstance(auth.get("admin"), dict) else {}
    if admin.get("password_hash"):
        return None
    password = new_password()
    save_auth_config(relay_config_path, {
        "admin": {
            "username": str(admin.get("username") or "admin"),
            "password_hash": hash_password(password),
        },
        # A per-install secret signs session cookies. Persisted so sessions
        # survive a restart; without it every restart would invalidate them.
        "session_secret": auth.get("session_secret") or secrets.token_hex(32),
        "session_hours": int(auth.get("session_hours") or SESSION_HOURS),
    })
    return password


def auth_settings() -> dict:
    auth = load_auth_config(_state["config_path"])
    admin = auth.get("admin") if isinstance(auth.get("admin"), dict) else {}
    return {
        "username": str(admin.get("username") or "admin"),
        "password_hash": str(admin.get("password_hash") or ""),
        "session_secret": str(auth.get("session_secret") or ""),
        "session_hours": int(auth.get("session_hours") or SESSION_HOURS),
    }


# ------------------------------------------------------------ middleware

def auth_disabled() -> bool:
    return os.environ.get("LLM_EVL_NO_AUTH", "").strip() in ("1", "true", "yes", "on")


def current_user(request: Request) -> str | None:
    """The logged-in username for this request, or None."""
    if auth_disabled():
        return "no-auth"
    s = auth_settings()
    if not s["password_hash"] or not s["session_secret"]:
        return None
    return verify_session(request.cookies.get(COOKIE_NAME, ""), s["session_secret"])


def install_auth_middleware(app) -> None:
    """Attach the fail-closed gate to an app.

    Everything requires a session except the login page, the login endpoint and
    anything under /v1/ (the relay surface, which authenticates with its own
    client keys — see relay_routes).
    """

    @app.middleware("http")
    async def _require_login(request: Request, call_next):
        path = request.url.path
        if path.startswith("/v1/") or path in PUBLIC_PATHS:
            return await call_next(request)

        if current_user(request) is None:
            # Browsers get sent to the login page; API clients (fetch, curl)
            # get a JSON 401 they can act on instead of an HTML login form.
            if "text/html" in request.headers.get("accept", ""):
                return RedirectResponse("/login", status_code=302)
            return JSONResponse(
                status_code=401,
                content={"detail": "not authenticated"},
            )
        return await call_next(request)


# ---------------------------------------------------------------- routes

class LoginRequest(BaseModel):
    username: str = ""
    password: str = ""


@router.get("/login")
def login_page(request: Request):
    # Already signed in? Skip the form.
    if current_user(request) is not None:
        return RedirectResponse("/", status_code=302)
    return FileResponse(WEB_DIR / "login.html")


@router.post("/api/auth/login")
def do_login(req: LoginRequest, request: Request):
    s = auth_settings()
    if not s["password_hash"]:
        raise HTTPException(503, "admin password is not configured yet")
    ok_user = secrets.compare_digest(req.username or "", s["username"])
    ok_pass = verify_password(req.password or "", s["password_hash"])
    if not (ok_user and ok_pass):
        # Same message for both failures: telling them which half was wrong
        # turns the form into a username oracle.
        logger.info("failed login attempt for %r from %s", req.username,
                    request.client.host if request.client else "?")
        raise HTTPException(401, "用户名或密码错误")
    token = sign_session(s["username"], s["session_secret"],
                         ttl_seconds=s["session_hours"] * 3600)
    resp = JSONResponse({"ok": True, "username": s["username"]})
    resp.set_cookie(
        COOKIE_NAME, token,
        max_age=s["session_hours"] * 3600,
        httponly=True,      # JavaScript cannot read it: XSS cannot exfiltrate it
        samesite="lax",     # not sent on cross-site POSTs
        path="/",
    )
    return resp


@router.post("/api/auth/logout")
def do_logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE_NAME, path="/")
    return resp


@router.get("/api/auth/status")
def auth_status(request: Request):
    s = auth_settings()
    return {
        "authenticated": current_user(request) is not None,
        "auth_disabled": auth_disabled(),
        "username": s["username"],
        "password_configured": bool(s["password_hash"]),
    }
