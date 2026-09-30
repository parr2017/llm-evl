"""FastAPI app factory + uvicorn entrypoint.

Two apps, two listeners, one process:

- **management** (default 127.0.0.1:7788) — the web UI and every ``/api/*``
  endpoint, including ``/api/providers``, which returns provider keys. Bound to
  loopback so it is not reachable from the LAN at all.
- **relay** (default 0.0.0.0:7789) — *only* ``/v1/chat/completions`` and
  ``/v1/models``. No management endpoint, no static files, and every call needs
  a client key. This is the port colleagues point their SDKs at.

Keeping them on separate listeners is the point: if both lived on one
LAN-facing port, a port-forward or a stray ``0.0.0.0`` bind would put every
provider credential on the network.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

from fastapi import FastAPI

from .auth_routes import ensure_admin, install_auth_middleware
from .auth_routes import router as auth_router
from .auth_routes import configure as configure_auth
from .relay_manager import relay_manager
from .relay_routes import router as relay_router
from .relay_routes import v1_router as relay_v1_router
from .routes import router
from .run_manager import manager

logger = logging.getLogger(__name__)

DEFAULT_RELAY_PORT = 7789


def create_app(config_path: str = "targets.yaml",
               relay_config_path: str = "relay.yaml") -> FastAPI:
    """The management app: UI + /api/*. Login-gated, meant for loopback."""
    app = FastAPI(title="llm-evl", version="0.1.0")
    manager.config_path = config_path
    relay_manager.targets_config_path = config_path
    relay_manager.relay_config_path = relay_config_path
    # The auth module reads the same file as the relay (separate top-level
    # sections), so it needs to be told where that file is.
    configure_auth(relay_config_path)

    @app.on_event("startup")
    def _warn_plaintext():
        names = manager.plaintext_warning()
        if names:
            logger.warning(
                "Plaintext API keys detected in config for targets: %s. "
                "Consider using api_key_env instead.", ", ".join(names)
            )

    # Order matters: the relay's /v1/* routes must be matched before
    # routes.py's SPA fallback, which answers *every* unmatched path with
    # index.html (it only special-cases /api/ and /v1/). Registering the relay
    # second would make /v1/chat/completions return HTML instead of JSON.
    app.include_router(auth_router)
    app.include_router(relay_router)
    app.include_router(relay_v1_router)
    app.include_router(router)

    # Fail-closed: every path needs a session except /v1/*, /login and the
    # login endpoint itself.
    install_auth_middleware(app)
    return app


def create_relay_app(config_path: str = "targets.yaml",
                     relay_config_path: str = "relay.yaml") -> FastAPI:
    """The LAN-facing app: relay endpoints only, nothing else.

    Only ``/v1/*`` is mounted — deliberately *not* ``/api/relay/*``, which can
    mint client keys. So a port scan of this port finds two endpoints, both
    key-gated, and no way to create a key from here.
    """
    app = FastAPI(title="llm-evl relay", version="0.1.0", docs_url=None,
                  redoc_url=None, openapi_url=None)
    relay_manager.targets_config_path = config_path
    relay_manager.relay_config_path = relay_config_path
    app.include_router(relay_v1_router)
    return app


def _lan_ip() -> str:
    """Best-effort LAN address, so the startup banner can tell colleagues
    where to point their SDK. Opening a UDP socket does not send anything; it
    just asks the OS which interface would be used."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def run(host: str = "127.0.0.1", port: int = 7788,
        config_path: str = "targets.yaml",
        open_browser: bool = True,
        relay_config_path: str = "relay.yaml",
        relay_host: str = "0.0.0.0",
        relay_port: int = DEFAULT_RELAY_PORT) -> None:
    """Launch both listeners in one process.

    Two uvicorn servers share a single event loop, so the relay's breaker state
    and call log are the same objects the management UI reads.
    """
    import uvicorn
    import webbrowser

    generated = ensure_admin(relay_config_path)

    app = create_app(config_path, relay_config_path)
    relay_app = create_relay_app(config_path, relay_config_path)

    # The UI has to show colleagues the address they can actually reach. The
    # management origin (127.0.0.1:7788) is both the wrong port and loopback
    # only, so it must not be what we hand out.
    shown_host = _lan_ip() if relay_host in ("0.0.0.0", "::") else relay_host
    relay_manager.public_base_url = f"http://{shown_host}:{relay_port}/v1"

    servers = [
        uvicorn.Server(uvicorn.Config(app, host=host, port=port,
                                      log_level="info")),
        uvicorn.Server(uvicorn.Config(relay_app, host=relay_host, port=relay_port,
                                      log_level="warning")),
    ]

    if open_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()

    # flush=True on every line: stdout is block-buffered when it is redirected
    # to a log file, and the generated password is shown exactly once — losing
    # it to a buffer means locking yourself out of your own tool.
    print(f"\n  管理端 (仅本机)  http://{host}:{port}", flush=True)
    print(f"  中转口 (内网)    http://{relay_host}:{relay_port}/v1"
          f"   ← 内网访问用 {_lan_ip()}:{relay_port}/v1", flush=True)
    if generated:
        print("\n  " + "=" * 58, flush=True)
        print("  首次启动，已生成管理员密码（只显示这一次）：", flush=True)
        print(f"      {generated}", flush=True)
        print("  用户名：admin", flush=True)
        print("  忘记密码：删除 relay.yaml 里的 auth 段后重启，会重新生成", flush=True)
        print("  " + "=" * 58, flush=True)
    print("\n  Press Ctrl+C to stop.\n", flush=True)

    async def _serve_all() -> None:
        # Only the first server may install signal handlers; the second would
        # raise if it tried, since signal handling is main-thread only.
        for s in servers[1:]:
            s.install_signal_handlers = lambda: None
        await asyncio.gather(*(s.serve() for s in servers))

    asyncio.run(_serve_all())
