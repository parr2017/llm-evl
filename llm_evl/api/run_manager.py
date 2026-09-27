"""RunManager: single in-flight benchmark, SSE event drain, run persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from ..core.evaluator import (
    Evaluator,
    filter_targets,
    has_plaintext_keys,
    load_providers,
    load_targets,
)
from ..core.models import ProgressEvent, Provider, ProviderModel, RunConfig, RunResult, RunStatus

logger = logging.getLogger(__name__)

RUNS_DIR = Path("runs")


class RunManager:
    """Holds the current in-flight run and streams its events via a queue."""

    def __init__(self, config_path: str = "targets.yaml"):
        self.config_path = config_path
        self.evaluator: Evaluator | None = None
        self.run_result: RunResult | None = None
        self._task: asyncio.Task | None = None
        self._events: asyncio.Queue[ProgressEvent] | None = None
        self._lock = asyncio.Lock()

    @property
    def is_running(self) -> bool:
        return (
            self.evaluator is not None
            and self.run_result is not None
            and self.run_result.status == RunStatus.RUNNING.value
            and self._task is not None
            and not self._task.done()
        )

    def list_targets(self) -> list[dict]:
        targets = load_targets(self.config_path)
        return [t.to_config_dict() for t in targets]

    def save_targets(self, targets_payload: list[dict]) -> None:
        import yaml

        # Merge: preserve existing api_key when the incoming payload doesn't
        # specify one (the UI can't read it back, only has_api_key). This stops
        # a save from wiping plaintext keys that the user didn't re-enter.
        existing: dict[str, "Target"] = {}  # type: ignore[type-arg]
        try:
            for t in load_targets(self.config_path):
                existing[t.name] = t
        except Exception:
            pass

        merged: list[dict] = []
        for d in targets_payload:
            d = dict(d)  # copy so we don't mutate caller
            name = d.get("name")
            if not d.get("api_key") and name in existing:
                ex = existing[name]
                if ex.api_key:
                    d["api_key"] = ex.api_key  # preserve plaintext key
            # Drop has_api_key — it's a UI-only flag, not a config field.
            d.pop("has_api_key", None)
            merged.append(d)

        doc = {"targets": merged}
        Path(self.config_path).write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    # ---- provider management ----

    def list_providers(self) -> list[dict]:
        providers = load_providers(self.config_path)
        return [p.to_config_dict() for p in providers]

    def save_providers(self, providers_payload: list[dict]) -> None:
        import yaml

        # Preserve existing api_key values when not provided in payload.
        existing_providers: dict[str, Provider] = {}
        try:
            for p in load_providers(self.config_path):
                existing_providers[p.name] = p
        except Exception:
            pass

        merged: list[dict] = []
        for d in providers_payload:
            d = dict(d)
            name = d.get("name", "")
            # Drop empty api_key — it means "keep existing", not "clear it"
            if not d.get("api_key"):
                d.pop("api_key", None)
            # Preserve api_key if not provided
            if not d.get("api_key") and name in existing_providers:
                ex = existing_providers[name]
                if ex.api_key:
                    d["api_key"] = ex.api_key
            d.pop("has_api_key", None)
            # Ensure models list exists
            if "models" not in d:
                d["models"] = []
            merged.append(d)

        doc = {"providers": merged}
        Path(self.config_path).write_text(
            yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    async def fetch_models(self, base_url: str, api_key: str = "") -> list[dict]:
        """Fetch available models from a provider's /v1/models endpoint.

        Returns [{name, context_length}] where context_length is captured when
        the provider advertises it (common aliases: vLLM's max_model_len,
        context_length, context_window, ...) and None otherwise.
        """
        import httpx

        url = base_url.rstrip("/") + "/models"
        headers = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # Aliases in priority order — vLLM / custom servers expose
        # max_model_len; others use context_length / context_window etc.
        ctx_aliases = (
            "max_model_len",
            "context_length",
            "context_window",
            "max_context_length",
            "max_input_tokens",
        )

        async with httpx.AsyncClient() as client:
            resp = await client.get(url, headers=headers, timeout=15.0)
            resp.raise_for_status()
            data = resp.json()

        def _ctx_of(item: dict) -> int | None:
            for key in ctx_aliases:
                v = item.get(key)
                if v is None:
                    continue
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    return n
            return None

        models: list[dict] = []
        for item in (data.get("data") or []):
            mid = item.get("id", "")
            if mid:
                models.append({"name": mid, "context_length": _ctx_of(item)})
        models.sort(key=lambda m: m["name"])
        return models

    async def test_provider(self, provider_dict: dict) -> dict:
        """Quick connectivity test: send one request to each model concurrently.

        Returns {models: [{name, ok, ttft, tok_s, error}, ...]}.
        """
        import asyncio
        import httpx

        from ..core.client import stream_request
        from ..core.models import Target

        provider_name = provider_dict.get("name", "test")
        base_url = provider_dict.get("base_url", "")
        api_key = provider_dict.get("api_key", "")
        models = provider_dict.get("models", [])

        if not base_url or not models:
            return {"models": [], "error": "missing base_url or models"}

        # Resolve api_key: explicit > config lookup > env
        if not api_key:
            try:
                for p in load_providers(self.config_path):
                    if p.name == provider_name:
                        api_key = p.resolved_api_key()
                        break
            except Exception:
                pass

        async def test_one(model_name: str, temperature: float | None) -> dict:
            target = Target(
                name=f"{provider_name}/{model_name}",
                base_url=base_url,
                model=model_name,
                api_key=api_key,
                temperature=temperature,
            )
            temp = temperature if temperature is not None else 0.0
            async with httpx.AsyncClient() as client:
                rr = await stream_request(
                    client, target, "hi",
                    temperature=temp,
                    timeout=20.0,
                    include_usage=True,
                    prompt_id="test",
                    concurrency=1,
                )
            return {
                "name": model_name,
                "ok": rr.ok,
                "ttft": round(rr.ttft, 4) if rr.ttft else None,
                "e2e": round(rr.e2e, 4) if rr.e2e else None,
                "tokens_per_second": round(rr.tokens_per_second, 1) if rr.tokens_per_second else None,
                "output_tokens": rr.output_tokens,
                "input_tokens": rr.input_tokens,
                "cost": round(rr.cost, 6) if rr.cost is not None else None,
                "error": rr.error or None,
                "error_type": rr.error_type or None,
            }

        tasks = [
            test_one(m["name"], m.get("temperature"))
            for m in models
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        model_results = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                model_results.append({
                    "name": models[i]["name"],
                    "ok": False,
                    "ttft": None, "e2e": None, "tokens_per_second": None,
                    "output_tokens": 0, "input_tokens": 0, "cost": None,
                    "error": str(r),
                    "error_type": "transport",
                })
            else:
                model_results.append(r)

        return {"models": model_results}

    async def start_provider_benchmark(
        self,
        provider_name: str,
        model_names: list[str],
        config: RunConfig,
    ) -> str:
        """Start a benchmark run for selected models under a provider."""
        async with self._lock:
            if self.is_running:
                raise RuntimeError("a run is already in progress")

            providers = load_providers(self.config_path)
            provider = None
            for p in providers:
                if p.name == provider_name:
                    provider = p
                    break
            if provider is None:
                raise ValueError(f"provider not found: {provider_name}")

            # Filter to selected models
            provider_models = [m for m in provider.models if m.name in model_names]
            if not provider_models:
                raise ValueError("no matching models found")

            # Create a temporary provider with only selected models
            filtered_provider = Provider(
                name=provider.name,
                base_url=provider.base_url,
                api_key=provider.api_key,
                api_key_env=provider.api_key_env,
                models=provider_models,
            )
            targets = filtered_provider.to_targets()

            self.evaluator = Evaluator(config, targets)
            self.run_result = self.evaluator.run_result
            self._events = asyncio.Queue()
            self._task = asyncio.create_task(self._consume())
            return self.run_result.run_id

    def plaintext_warning(self) -> list[str]:
        try:
            return has_plaintext_keys(load_targets(self.config_path))
        except Exception:
            return []

    def list_prompts(self) -> list[dict]:
        from ..core.prompts import BUILTIN_PROMPTS, load_custom_prompts, PROMPTS_FILE
        out = []
        for p in BUILTIN_PROMPTS:
            out.append({
                "id": p.id, "label": p.label, "text": p.text, "bucket": p.bucket,
                "expected_keywords": p.expected_keywords,
                "min_output_tokens": p.min_output_tokens,
                # Without this the UI cannot show or edit reference points,
                # and the quality dimension stays permanently uncovered.
                "reference_points": p.reference_points,
                "custom": False,
            })
        for p in load_custom_prompts():
            out.append({
                "id": p.id, "label": p.label, "text": p.text, "bucket": p.bucket,
                "expected_keywords": p.expected_keywords,
                "min_output_tokens": p.min_output_tokens,
                "reference_points": p.reference_points,
                "custom": True,
            })
        return out

    def save_custom_prompts(self, prompts_payload: list[dict]) -> None:
        from ..core.prompts import save_custom_prompts as _save, PROMPTS_FILE
        from ..core.models import PromptItem
        items = [
            PromptItem(
                id=d.get("id") or f"custom-{i+1}",
                label=d.get("label", "Custom"),
                text=d.get("text", ""),
                bucket=d.get("bucket", "medium"),
                expected_keywords=list(d.get("expected_keywords") or []),
                min_output_tokens=d.get("min_output_tokens"),
                reference_points=list(d.get("reference_points") or []),
                custom=True,
            )
            for i, d in enumerate(prompts_payload)
        ]
        _save(items)

    async def test_target(self, target_dict: dict) -> dict:
        """Send a minimal streaming probe to verify the target works.

        Resolves the api_key the same way save_targets does: if the incoming
        config omits api_key, reuse the existing one from targets.yaml so an
        existing target can be tested without re-entering its key.
        Returns {ok, ttft, output_tokens, tokens_per_second, error, detail}.
        """
        import asyncio
        import httpx

        from ..core.client import stream_request
        from ..core.models import Target

        # Resolve api_key: explicit > existing on disk > env.
        api_key = target_dict.get("api_key") or ""
        if not api_key:
            try:
                for t in load_targets(self.config_path):
                    if t.name == target_dict.get("name") and t.api_key:
                        api_key = t.api_key
                        break
            except Exception:
                pass

        target = Target(
            name=target_dict.get("name") or "test",
            base_url=target_dict.get("base_url", ""),
            model=target_dict.get("model", ""),
            api_key=api_key,
            api_key_env=target_dict.get("api_key_env", ""),
            temperature=target_dict.get("temperature"),
        )
        if not target.base_url or not target.model:
            return {"ok": False, "error": "missing base_url or model"}

        temp = target.temperature if target.temperature is not None else 0.0
        async with httpx.AsyncClient() as client:
            rr = await stream_request(
                client, target, "hi",
                temperature=temp,
                timeout=20.0,
                include_usage=True,
                prompt_id="test",
                concurrency=1,
            )
        return {
            "ok": rr.ok,
            "ttft": rr.ttft,
            "e2e": rr.e2e,
            "output_tokens": rr.output_tokens,
            "tokens_per_second": rr.tokens_per_second,
            "error": rr.error,
            "detail": (rr.output_text[:200] if rr.ok else ""),
        }

    async def start(self, config: RunConfig) -> str:
        async with self._lock:
            if self.is_running:
                raise RuntimeError("a run is already in progress")
            targets = load_targets(self.config_path)
            targets = filter_targets(targets, config.target_names)
            self.evaluator = Evaluator(config, targets)
            self.run_result = self.evaluator.run_result
            self._events = asyncio.Queue()
            # Background task drains the generator into the queue (same loop).
            self._task = asyncio.create_task(self._consume())
            return self.run_result.run_id

    async def _consume(self) -> None:
        """Drain Evaluator.iter_events() into self._events, then terminate."""
        assert self.evaluator is not None and self._events is not None
        try:
            async for ev in self.evaluator.iter_events():
                await self._events.put(ev)
        except asyncio.CancelledError:
            # Hard abort path: status already set to aborted by the generator.
            pass
        except Exception as exc:
            logger.exception("run consumer failed")
            await self._events.put(ProgressEvent(type="log", message=f"consumer error: {exc}"))
            if self.run_result is not None:
                self.run_result.status = RunStatus.FAILED.value
        finally:
            # Persist + emit a terminal run_done the SSE reader waits for.
            if self.run_result is not None:
                if self.run_result.finished_at is None:
                    self.run_result.finished_at = __import__("time").time()
                self._persist(self.run_result)
            await self._events.put(
                ProgressEvent(
                    type="run_done",
                    progress=1.0,
                    message=self.run_result.status if self.run_result else "unknown",
                )
            )

    def stop(self) -> bool:
        if self.evaluator and self.is_running:
            self.evaluator.abort()  # cooperative
            if self._task:
                self._task.cancel()  # hard cancel
            return True
        return False

    async def events(self):
        """Async generator yielding ProgressEvents until run_done."""
        if self._events is None:
            return
        queue = self._events
        while True:
            ev = await queue.get()
            yield ev
            if ev.type == "run_done":
                self._reset()
                break

    def _persist(self, run_result: RunResult) -> None:
        RUNS_DIR.mkdir(exist_ok=True)
        path = RUNS_DIR / f"run_{run_result.run_id}.json"
        path.write_text(
            json.dumps(run_result.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        logger.info("saved run to %s", path)

    def _reset(self) -> None:
        self.evaluator = None
        self._events = None
        self._task = None
        # keep self.run_result until next start for /api/run/status inspection

    def list_runs(self) -> list[dict]:
        if not RUNS_DIR.exists():
            return []
        out = []
        for p in RUNS_DIR.glob("run_*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                tgts = data.get("targets", [])
                out.append({
                    "run_id": data["run_id"],
                    "started_at": data["started_at"],
                    "finished_at": data.get("finished_at"),
                    "status": data["status"],
                    "n_targets": len(tgts),
                    "n_cells": len(data.get("cells", [])),
                    "target_names": [t.get("name") for t in tgts],
                    "models": sorted({t.get("model", "") for t in tgts if t.get("model")}),
                    "file": p.name,
                })
            except Exception:
                continue
        out.sort(key=lambda r: r["started_at"] or 0, reverse=True)
        return out

    def get_run(self, run_id: str) -> dict | None:
        path = RUNS_DIR / f"run_{run_id}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


manager = RunManager()
