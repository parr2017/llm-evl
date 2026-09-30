"""Dataclasses for the benchmark data model.

Hierarchy (per Q20): Run -> Cell[target x prompt x concurrency] -> RequestResult[N]
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class TokenSource(str, Enum):
    """Where the output token count came from."""

    USAGE = "usage"          # provider returned usage in final chunk
    TIKTOKEN = "tiktoken"    # local tiktoken fallback (approximate)
    NONE = "none"            # no tokens (error / empty)


class ErrorType(str, Enum):
    """Why a request failed.

    Before this existed every failure was a free-text ``error`` string, so
    "3 timeouts" and "3 stream-parse bugs" were indistinguishable in
    aggregate. The report now groups failures by these buckets.
    """

    NONE = ""                    # success
    TIMEOUT = "timeout"          # httpx.TimeoutException
    RATE_LIMIT = "rate_limit"    # HTTP 429
    CLIENT_ERROR = "client_error"      # other HTTP 4xx
    SERVER_ERROR = "server_error"      # HTTP 5xx
    CONTENT_FILTERED = "content_filtered"  # provider finish_reason filter
    TRANSPORT = "transport"      # connect/read failure (httpx.HTTPError)
    NO_CONTENT = "no_content"    # stream finished without any content delta
    PARSE = "parse"              # stream produced no usable chunk
    CANCELLED = "cancelled"      # run aborted mid-flight

    @classmethod
    def from_status(cls, status: int) -> "ErrorType":
        """Map an HTTP status code to a bucket."""
        if status == 429:
            return cls.RATE_LIMIT
        if 400 <= status < 500:
            return cls.CLIENT_ERROR
        if status >= 500:
            return cls.SERVER_ERROR
        return cls.CLIENT_ERROR


@dataclass
class ProviderModel:
    """A single model under a provider."""

    name: str
    temperature: float | None = None
    context_length: int | None = None   # advertised token window (may be unknown)
    # Price in CNY per 1M tokens. None -> cost dimension stays "not covered".
    price_in: float | None = None
    price_out: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "ProviderModel":
        return cls(
            name=d["name"],
            temperature=d.get("temperature"),
            context_length=_coerce_int(d.get("context_length")),
            price_in=d.get("price_in"),
            price_out=d.get("price_out"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "temperature": self.temperature,
            "context_length": self.context_length,
            "price_in": self.price_in,
            "price_out": self.price_out,
        }


def _coerce_int(v: Any) -> int | None:
    """Coerce a value to int, tolerating None and numeric strings."""
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


@dataclass
class Provider:
    """A model provider (vendor) with a base_url and multiple models."""

    name: str
    base_url: str
    api_key: str = ""
    api_key_env: str = ""
    models: list[ProviderModel] = field(default_factory=list)

    def resolved_api_key(self) -> str:
        import os
        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    def has_plaintext_key(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Provider":
        models = [ProviderModel.from_dict(m) for m in (d.get("models") or [])]
        return cls(
            name=d["name"],
            base_url=d["base_url"],
            api_key=d.get("api_key", ""),
            api_key_env=d.get("api_key_env", ""),
            models=models,
        )

    def to_dict(self) -> dict[str, Any]:
        """Full serialization (includes api_key for internal use)."""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "api_key_env": self.api_key_env,
            "models": [m.to_dict() for m in self.models],
        }

    def to_config_dict(self) -> dict[str, Any]:
        """Serializable form for UI display (exposes api_key for local use).

        Note: this is what ``GET /api/providers`` returns, so the plaintext key
        travels to the browser. The whole management API sits behind the admin
        login (see ``api/auth_routes.py``) — that login is the only thing
        standing between a LAN caller and every provider credential here.
        """
        return {
            "name": self.name,
            "base_url": self.base_url,
            "api_key": self.api_key,
            "has_api_key": bool(self.api_key),
            "api_key_env": self.api_key_env,
            "models": [m.to_dict() for m in self.models],
        }

    def to_targets(self) -> list["Target"]:
        """Flatten to a list of Target objects for benchmark evaluation."""
        return [
            Target(
                name=f"{self.name}/{m.name}",
                base_url=self.base_url,
                model=m.name,
                api_key=self.api_key,
                api_key_env=self.api_key_env,
                temperature=m.temperature,
                price_in=m.price_in,
                price_out=m.price_out,
            )
            for m in self.models
        ]


class RunStatus(str, Enum):
    RUNNING = "running"
    COMPLETED = "completed"
    ABORTED = "aborted"
    FAILED = "failed"


@dataclass
class Target:
    """A single LLM endpoint to benchmark."""

    name: str
    base_url: str
    model: str
    api_key: str = ""
    api_key_env: str = ""
    temperature: float | None = None   # None -> use run default
    price_in: float | None = None      # CNY per 1M input tokens
    price_out: float | None = None     # CNY per 1M output tokens

    def resolved_api_key(self) -> str:
        """Resolve the API key: explicit api_key wins, else read env var."""
        import os

        if self.api_key:
            return self.api_key
        if self.api_key_env:
            return os.environ.get(self.api_key_env, "")
        return ""

    def has_plaintext_key(self) -> bool:
        return bool(self.api_key)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Target":
        return cls(
            name=d["name"],
            base_url=d["base_url"],
            model=d["model"],
            api_key=d.get("api_key", ""),
            api_key_env=d.get("api_key_env", ""),
            temperature=d.get("temperature"),
            price_in=d.get("price_in"),
            price_out=d.get("price_out"),
        )

    def to_config_dict(self) -> dict[str, Any]:
        """Serializable form for UI display (never exposes the key value)."""
        return {
            "name": self.name,
            "base_url": self.base_url,
            "model": self.model,
            "has_api_key": bool(self.api_key),
            "api_key_env": self.api_key_env,
            "temperature": self.temperature,
            "price_in": self.price_in,
            "price_out": self.price_out,
        }


@dataclass
class PromptItem:
    """A single prompt with an expected output-length bucket.

    Quality scoring is opt-in: set expected_keywords and/or min_output_tokens
    to get a quality_score per request. custom=True marks user-defined prompts
    (editable in the UI); built-ins are read-only.
    """

    id: str
    label: str
    text: str
    bucket: str   # "short" | "medium" | "long"
    expected_keywords: list[str] = field(default_factory=list)
    min_output_tokens: int | None = None
    # Reference answer broken into checkable points. When set, it becomes the
    # primary quality signal (recall = hit / total) instead of raw keyword
    # matching, which cannot tell "right answer" from "long answer".
    reference_points: list[str] = field(default_factory=list)
    custom: bool = False   # user-defined via prompts.yaml (editable in UI)


@dataclass
class RunConfig:
    """Parameters for one benchmark run."""

    concurrency_levels: list[int] = field(default_factory=lambda: [1, 2, 4, 8, 16, 32])
    samples: int = 20               # effective requests per cell
    warmup: int = 2                 # warmup requests discarded per cell
    timeout: float = 120.0          # single-request timeout (seconds)
    temperature: float = 0.0        # deterministic sampling by default
    target_names: list[str] = field(default_factory=list)   # empty -> all
    prompt_ids: list[str] = field(default_factory=list)     # empty -> all built-in
    prompts_file: str = ""          # optional override file
    include_usage: bool = True      # stream_options.include_usage
    mix_prompts: bool = False       # mix prompts within each cell (more realistic)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RequestResult:
    """One raw request's timing and outcome."""

    target: str
    prompt_id: str
    concurrency: int
    # timing (seconds)
    ttft: float | None = None       # time to first token
    tts: float | None = None        # time to first sentence boundary (。.!?！？\n)
    e2e: float | None = None        # end-to-end latency
    generation_duration: float | None = None   # e2e - ttft
    # token / throughput
    output_tokens: int = 0
    input_tokens: int = 0
    token_source: str = TokenSource.NONE.value
    tokens_per_second: float | None = None    # output_tokens / generation_duration
    # cost (CNY). None when the target has no price configured.
    cost: float | None = None
    # inter-token latencies (seconds), one entry per token after the first
    itl: list[float] = field(default_factory=list)
    # quality (0..1, None if not measured)
    quality_score: float | None = None
    quality_keyword_hits: int = 0
    quality_length_ok: bool = False
    quality_point_hits: int = 0
    quality_point_total: int = 0
    quality_point_recall: float | None = None
    # outcome
    ok: bool = False
    error: str = ""
    error_type: str = ErrorType.NONE.value
    timed_out: bool = False
    finish_reason: str = ""
    # SSE lines that failed json.loads(). Previously swallowed silently, which
    # made client-side stream bugs indistinguishable from model failures.
    malformed_chunks: int = 0
    # metadata
    started_at: float = field(default_factory=time.time)
    output_text: str = ""   # kept for quality scoring; trimmed for storage

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CellAggregates:
    """Aggregated metrics over a cell's successful requests."""

    n: int = 0
    n_errors: int = 0
    error_rate: float = 0.0
    # quantiles (seconds)
    ttft_p50: float | None = None
    ttft_p90: float | None = None
    ttft_p99: float | None = None
    e2e_p50: float | None = None
    e2e_p90: float | None = None
    itl_p50: float | None = None
    itl_p90: float | None = None
    tts_p50: float | None = None     # time to first sentence
    tts_p90: float | None = None
    # throughput
    tokens_per_second_mean: float | None = None
    tokens_per_second_p50: float | None = None
    aggregate_tokens_per_second: float | None = None   # concurrency > 1
    # volume
    total_output_tokens: int = 0
    total_input_tokens: int = 0
    total_cost: float | None = None          # None -> no target had a price
    # quality (None if not measured for this prompt)
    quality_mean: float | None = None
    quality_keyword_hit_ratio: float | None = None
    quality_point_recall: float | None = None
    # failures
    error_breakdown: dict[str, int] = field(default_factory=dict)  # error_type -> count
    malformed_chunks: int = 0
    # warning flags
    high_error: bool = False   # error_rate > 0.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CellResult:
    """Aggregates for one (target, prompt, concurrency) cell."""

    target: str
    prompt_id: str
    prompt_label: str
    bucket: str
    concurrency: int
    requests: list[RequestResult] = field(default_factory=list)
    aggregates: CellAggregates = field(default_factory=CellAggregates)

    def to_dict(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "prompt_id": self.prompt_id,
            "prompt_label": self.prompt_label,
            "bucket": self.bucket,
            "concurrency": self.concurrency,
            "requests": [r.to_dict() for r in self.requests],
            "aggregates": self.aggregates.to_dict(),
        }


@dataclass
class RunResult:
    """Top-level result object for one benchmark run."""

    run_id: str
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    status: str = RunStatus.RUNNING.value
    config: dict[str, Any] = field(default_factory=dict)
    targets: list[dict[str, Any]] = field(default_factory=list)
    prompts: list[dict[str, Any]] = field(default_factory=list)
    cells: list[CellResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status": self.status,
            "config": self.config,
            "targets": self.targets,
            "prompts": self.prompts,
            "cells": [c.to_dict() for c in self.cells],
        }


@dataclass
class ProgressEvent:
    """One SSE progress event pushed to the UI."""

    type: str            # "cell_start" | "request_done" | "cell_done" | "run_done" | "log"
    cell: dict[str, Any] = field(default_factory=dict)
    request: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    progress: float = 0.0   # 0..1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
