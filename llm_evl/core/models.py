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
    token_source: str = TokenSource.NONE.value
    tokens_per_second: float | None = None    # output_tokens / generation_duration
    # inter-token latencies (seconds), one entry per token after the first
    itl: list[float] = field(default_factory=list)
    # quality (0..1, None if not measured)
    quality_score: float | None = None
    quality_keyword_hits: int = 0
    quality_length_ok: bool = False
    # outcome
    ok: bool = False
    error: str = ""
    timed_out: bool = False
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
    # quality (None if not measured for this prompt)
    quality_mean: float | None = None
    quality_keyword_hit_ratio: float | None = None
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
