"""FastAPI app factory + uvicorn entrypoint."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI

from .routes import router
from .run_manager import manager

logger = logging.getLogger(__name__)


def create_app(config_path: str = "targets.yaml") -> FastAPI:
    app = FastAPI(title="llm-evl", version="0.1.0")
    manager.config_path = config_path

    @app.on_event("startup")
    def _warn_plaintext():
        names = manager.plaintext_warning()
        if names:
            logger.warning(
                "Plaintext API keys detected in config for targets: %s. "
                "Consider using api_key_env instead.", ", ".join(names)
            )

    app.include_router(router)
    return app


def run(host: str = "127.0.0.1", port: int = 7788,
        config_path: str = "targets.yaml",
        open_browser: bool = True) -> None:
    """Launch uvicorn, optionally opening the browser."""
    import uvicorn
    import webbrowser

    app = create_app(config_path)
    url = f"http://{host}:{port}"

    if open_browser:
        import threading
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    print(f"\n  llm-evl running at  {url}\n  Press Ctrl+C to stop.\n")
    uvicorn.run(app, host=host, port=port, log_level="info")
