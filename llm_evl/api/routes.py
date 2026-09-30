"""FastAPI routes: targets CRUD, run control (SSE), history, static serve."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse, FileResponse
from pydantic import BaseModel

from ..core.client import stream_chat
from ..core.evaluator import load_targets, filter_targets
from ..core.models import RunConfig
from .run_manager import manager

router = APIRouter()


# ---- models for request bodies ----

class StartRunRequest(BaseModel):
    concurrency_levels: list[int] | None = None
    samples: int | None = None
    warmup: int | None = None
    timeout: float | None = None
    temperature: float | None = None
    target_names: list[str] | None = None
    prompt_ids: list[str] | None = None
    prompts_file: str | None = None
    include_usage: bool | None = None
    mix_prompts: bool | None = None


class SaveTargetsRequest(BaseModel):
    targets: list[dict]


class SavePromptsRequest(BaseModel):
    prompts: list[dict]


# ---- targets ----

@router.get("/api/targets")
def get_targets():
    try:
        return {"targets": manager.list_targets()}
    except FileNotFoundError:
        raise HTTPException(404, f"config not found: {manager.config_path}")
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/api/targets")
def save_targets(req: SaveTargetsRequest):
    try:
        manager.save_targets(req.targets)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, str(exc))


class TestTargetRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    temperature: float | None = None


@router.post("/api/targets/test")
async def test_target(req: TestTargetRequest):
    try:
        return await manager.test_target(req.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---- providers ----

class SaveProvidersRequest(BaseModel):
    providers: list[dict]


class FetchModelsRequest(BaseModel):
    base_url: str
    api_key: str | None = None
    provider_name: str | None = None


class TestProviderRequest(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    models: list[dict] = []


class ProviderBenchmarkRequest(BaseModel):
    provider_name: str
    model_names: list[str]
    concurrency_levels: list[int] | None = None
    samples: int | None = None
    warmup: int | None = None
    timeout: float | None = None
    temperature: float | None = None
    prompt_ids: list[str] | None = None
    prompts_file: str | None = None
    include_usage: bool | None = None
    mix_prompts: bool | None = None


@router.get("/api/providers")
def get_providers():
    try:
        return {"providers": manager.list_providers()}
    except FileNotFoundError:
        raise HTTPException(404, f"config not found: {manager.config_path}")
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/api/providers")
def save_providers(req: SaveProvidersRequest):
    try:
        manager.save_providers(req.providers)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/api/providers/fetch-models")
async def fetch_models(req: FetchModelsRequest):
    try:
        # Resolve api_key: explicit > provider by name > env
        api_key = req.api_key or ""
        if not api_key and req.provider_name:
            try:
                from ..core.evaluator import load_providers
                for p in load_providers(manager.config_path):
                    if p.name == req.provider_name:
                        api_key = p.resolved_api_key()
                        break
            except Exception:
                pass
        models = await manager.fetch_models(req.base_url, api_key)
        return {"models": models}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"provider returned HTTP {exc.response.status_code}")
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/api/providers/test")
async def test_provider(req: TestProviderRequest):
    try:
        return await manager.test_provider(req.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(500, str(exc))


@router.post("/api/providers/benchmark")
async def provider_benchmark(req: ProviderBenchmarkRequest):
    defaults = RunConfig()
    config = RunConfig(
        concurrency_levels=req.concurrency_levels or [1, 2, 4],
        samples=req.samples or defaults.samples,
        warmup=req.warmup if req.warmup is not None else defaults.warmup,
        timeout=req.timeout or defaults.timeout,
        temperature=req.temperature if req.temperature is not None else defaults.temperature,
        target_names=[],
        prompt_ids=req.prompt_ids or [],
        prompts_file=req.prompts_file or "",
        include_usage=req.include_usage if req.include_usage is not None else defaults.include_usage,
        mix_prompts=req.mix_prompts if req.mix_prompts is not None else defaults.mix_prompts,
    )
    try:
        run_id = await manager.start_provider_benchmark(
            req.provider_name, req.model_names, config
        )
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"run_id": run_id}


# ---- prompts ----

@router.get("/api/prompts")
def get_prompts():
    return {"prompts": manager.list_prompts()}


@router.post("/api/prompts")
def save_prompts(req: SavePromptsRequest):
    try:
        manager.save_custom_prompts(req.prompts)
        return {"ok": True}
    except Exception as exc:
        raise HTTPException(500, str(exc))


# ---- run control ----

@router.post("/api/run/start")
async def start_run(req: StartRunRequest):
    defaults = RunConfig()
    config = RunConfig(
        concurrency_levels=req.concurrency_levels or defaults.concurrency_levels,
        samples=req.samples or defaults.samples,
        warmup=req.warmup if req.warmup is not None else defaults.warmup,
        timeout=req.timeout or defaults.timeout,
        temperature=req.temperature if req.temperature is not None else defaults.temperature,
        target_names=req.target_names or [],
        prompt_ids=req.prompt_ids or [],
        prompts_file=req.prompts_file or "",
        include_usage=req.include_usage if req.include_usage is not None else defaults.include_usage,
        mix_prompts=req.mix_prompts if req.mix_prompts is not None else defaults.mix_prompts,
    )
    try:
        run_id = await manager.start(config)
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))
    except Exception as exc:
        raise HTTPException(400, str(exc))
    return {"run_id": run_id}


@router.post("/api/run/stop")
def stop_run():
    stopped = manager.stop()
    return {"stopped": stopped}


@router.get("/api/run/status")
def run_status():
    if manager.run_result is None:
        return {"running": False}
    r = manager.run_result
    return {
        "running": manager.is_running,
        "run_id": r.run_id,
        "status": r.status,
        "started_at": r.started_at,
        "n_targets": len(r.targets),
        "n_cells": len(r.cells),
        "config": r.config,
    }


@router.get("/api/run/events")
async def run_events():
    """SSE stream of progress events for the current run."""
    if manager._events is None:
        raise HTTPException(409, "no run in progress")

    async def gen():
        async for ev in manager.events():
            payload = ev.to_dict()
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ---- history ----

@router.get("/api/runs")
def list_runs():
    return {"runs": manager.list_runs()}


@router.get("/api/runs/{run_id}")
def get_run(run_id: str):
    data = manager.get_run(run_id)
    if data is None:
        raise HTTPException(404, f"run not found: {run_id}")
    return data


# ---- chat comparison ----

class ChatRequest(BaseModel):
    message: str
    targets: list[str]
    history: list[dict] = []  # [{role:"user",content:"..."}, {role:"assistant",content:"..."}]
    temperature: float = 0.0
    timeout: float = 120.0


@router.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """Stream chat responses from multiple models concurrently via SSE."""
    if not req.targets:
        raise HTTPException(400, "no targets selected")
    if not req.message.strip():
        raise HTTPException(400, "message cannot be empty")

    # Load and filter targets
    try:
        all_targets = load_targets(manager.config_path)
    except Exception as exc:
        raise HTTPException(500, f"failed to load targets: {exc}")

    selected = filter_targets(all_targets, req.targets)
    if not selected:
        raise HTTPException(404, "no matching targets found")

    # Build messages for each model
    messages = req.history + [{"role": "user", "content": req.message}]

    async def gen():
        async with httpx.AsyncClient() as client:
            shared_queue = asyncio.Queue()

            async def run_one(target):
                try:
                    async def callback(text: str):
                        await shared_queue.put({"type": "token", "target": target.name, "content": text})
                    result = await stream_chat(
                        client, target, messages,
                        temperature=req.temperature,
                        timeout=req.timeout,
                        on_token=callback,
                    )
                    await shared_queue.put({
                        "type": "done",
                        "target": target.name,
                        "ttft": round(result.ttft, 4) if result.ttft else None,
                        "tok_s": round(result.tokens_per_second, 1) if result.tokens_per_second else None,
                        "e2e": round(result.e2e, 4) if result.e2e else None,
                        "tokens": result.output_tokens,
                        "error": result.error or None,
                        # Lets the UI say "rate limited" instead of dumping
                        # the raw provider message.
                        "error_type": result.error_type or None,
                        "cost": round(result.cost, 6) if result.cost is not None else None,
                        "ok": result.ok,
                    })
                except Exception as exc:
                    await shared_queue.put({
                        "type": "done",
                        "target": target.name,
                        "ttft": None, "tok_s": None, "e2e": None, "tokens": 0,
                        "error": str(exc), "error_type": "transport", "ok": False,
                    })
                finally:
                    await shared_queue.put(None)

            tasks = [asyncio.create_task(run_one(t)) for t in selected]
            n_pending = len(selected)
            while n_pending > 0:
                evt = await shared_queue.get()
                if evt is None:
                    n_pending -= 1
                else:
                    yield f"data: {json.dumps(evt, ensure_ascii=False)}\n\n"
            for t in tasks:
                await t

        yield f"data: {json.dumps({'type': 'all_done'})}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.get("/api/compare")
def compare_runs(base: str, other: str):
    """Compare two historical runs cell-by-cell (target x prompt x concurrency)."""
    from ..core.compare import compare_runs as compute_compare

    base_run = manager.get_run(base)
    if base_run is None:
        raise HTTPException(404, f"run not found: {base}")
    other_run = manager.get_run(other)
    if other_run is None:
        raise HTTPException(404, f"run not found: {other}")
    return compute_compare(base_run, other_run)


# ---- static frontend ----

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@router.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


@router.get("/{full_path:path}")
def spa_fallback(full_path: str):
    """Serve index.html for any non-API, non-file path (SPA fallback).

    API paths under /api/ are handled above and never reach here. As a guard
    against stale backends / route drift, any /api/ or /v1/ path that does
    reach here returns a JSON 404 (never HTML) so frontend fetch() — and any
    OpenAI SDK pointed at this server — gets a clear error instead of a
    confusing "Unexpected token '<'" parse failure.
    """
    if full_path.startswith(("api/", "v1/")) or full_path in ("api", "v1"):
        raise HTTPException(404, f"API endpoint not found: /{full_path}")
    candidate = (WEB_DIR / full_path).resolve()
    # Prevent path traversal.
    try:
        candidate.relative_to(WEB_DIR)
    except ValueError:
        raise HTTPException(404)
    if candidate.is_file():
        return FileResponse(candidate)
    return FileResponse(WEB_DIR / "index.html")
