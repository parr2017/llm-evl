"""Closed-loop sweep engine over the full benchmark matrix.

Per Q7: closed-loop concurrency — at level c, c workers pull from a shared
queue of sample slots; each fires the next request as soon as the previous
finishes, until N effective samples are collected. Per Q22: single request
errors are counted, never abort the run; abort is cooperative + cancellation.

The engine is a single async generator of ProgressEvent. The CLI drains it in
one event loop; the API RunManager drains it into a queue (on the persistent
server loop) for SSE. This avoids the "background task on a closing loop" trap.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

import httpx
import yaml
from pathlib import Path

from .client import stream_request
from .metrics import aggregate_requests
from .models import (
    CellResult,
    ProgressEvent,
    RunConfig,
    RunResult,
    RunStatus,
    Target,
)
from .prompts import get_prompts

logger = logging.getLogger(__name__)


def load_targets(config_path: str) -> list[Target]:
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    targets = raw.get("targets") or []
    if not targets:
        raise ValueError(f"No targets found in {config_path}")
    return [Target.from_dict(t) for t in targets]


def filter_targets(targets: list[Target], names: list[str]) -> list[Target]:
    if not names:
        return targets
    wanted = set(names)
    out = [t for t in targets if t.name in wanted]
    missing = wanted - {t.name for t in out}
    if missing:
        raise ValueError(f"Unknown target(s): {sorted(missing)}")
    return out


def has_plaintext_keys(targets: list[Target]) -> list[str]:
    return [t.name for t in targets if t.has_plaintext_key()]


class Evaluator:
    """Drives the benchmark matrix as an async generator of ProgressEvent."""

    def __init__(self, config: RunConfig, targets: list[Target]):
        self.config = config
        self.targets = targets
        self._aborted = False
        self.run_result = RunResult(
            run_id=uuid.uuid4().hex[:12],
            started_at=time.time(),
            status=RunStatus.RUNNING.value,
            config=self.config.to_dict(),
            targets=[t.to_config_dict() for t in self.targets],
        )
        prompts = get_prompts(self.config.prompt_ids, self.config.prompts_file)
        self.run_result.prompts = [
            {"id": p.id, "label": p.label, "bucket": p.bucket, "text": p.text}
            for p in prompts
        ]
        self._prompts = prompts

    def abort(self) -> None:
        self._aborted = True

    async def iter_events(self):
        """Yield ProgressEvent(s) for the whole matrix, then stops.

        On normal completion the consumer will see the final cell_done for the
        last cell; the RunManager emits the terminal run_done. On cancellation
        the generator stops (cooperative via _aborted, hard via CancelledError)
        and run_result.status reflects the outcome.
        """
        run_result = self.run_result
        if self.config.mix_prompts:
            total_cells = len(self.targets) * len(self.config.concurrency_levels)
        else:
            total_cells = (
                len(self.targets) * len(self._prompts) * len(self.config.concurrency_levels)
            )
        done_cells = 0
        try:
            async with httpx.AsyncClient() as client:
                for target in self.targets:
                    if self._aborted:
                        break
                    temp = self.config.temperature
                    if target.temperature is not None:
                        temp = target.temperature
                    if self.config.mix_prompts:
                        # Mixed mode: one cell per (target × concurrency), prompts rotated across workers.
                        for c in self.config.concurrency_levels:
                            if self._aborted:
                                break
                            cell = await self._run_cell_mixed(
                                client, target, self._prompts, c, temp
                            )
                            run_result.cells.append(cell)
                            done_cells += 1
                            yield ProgressEvent(
                                type="cell_done",
                                cell=cell.to_dict(),
                                progress=done_cells / total_cells if total_cells else 1.0,
                                message=f"{target.name} / 混合 / c={c} done",
                            )
                    else:
                        # Normal mode: one cell per (target × prompt × concurrency).
                        for prompt in self._prompts:
                            if self._aborted:
                                break
                            for c in self.config.concurrency_levels:
                                if self._aborted:
                                    break
                                cell = await self._run_cell(
                                    client, target, prompt, c, temp
                                )
                                run_result.cells.append(cell)
                                done_cells += 1
                                yield ProgressEvent(
                                    type="cell_done",
                                    cell=cell.to_dict(),
                                    progress=done_cells / total_cells if total_cells else 1.0,
                                    message=f"{target.name} / {prompt.label} / c={c} done",
                                )
            run_result.status = (
                RunStatus.ABORTED.value if self._aborted else RunStatus.COMPLETED.value
            )
        except asyncio.CancelledError:
            run_result.status = RunStatus.ABORTED.value
            raise
        except Exception as exc:
            logger.exception("run failed")
            run_result.status = RunStatus.FAILED.value
            yield ProgressEvent(type="log", message=f"run failed: {exc}")
            run_result.status = RunStatus.FAILED.value
        finally:
            run_result.finished_at = time.time()

    async def _run_cell(self, client, target, prompt, concurrency, temperature) -> CellResult:
        cell = CellResult(
            target=target.name,
            prompt_id=prompt.id,
            prompt_label=prompt.label,
            bucket=prompt.bucket,
            concurrency=concurrency,
        )
        yield_msg = f"start {target.name} / {prompt.label} / c={concurrency}"
        # We cannot yield from here (it's a normal coroutine). The cell_start
        # is emitted by the caller pattern; for simplicity we just log via the
        # first request_done events. Cell-level progress is delivered via
        # cell_done at the end (sufficient for the UI table + progress bar).
        del yield_msg

        # Warmup (discarded). Per Q6.
        for _ in range(self.config.warmup):
            try:
                await stream_request(
                    client, target, prompt.text,
                    temperature=temperature,
                    timeout=self.config.timeout,
                    include_usage=self.config.include_usage,
                    prompt_id=prompt.id,
                    concurrency=concurrency,
                    expected_keywords=prompt.expected_keywords,
                    min_output_tokens=prompt.min_output_tokens,
                )
            except Exception:
                logger.debug("warmup error (ignored)")

        n = self.config.samples
        queue: asyncio.Queue[int] = asyncio.Queue()
        for i in range(n):
            queue.put_nowait(i)

        async def worker():
            results: list = []
            while not self._aborted:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return results
                rr = await stream_request(
                    client, target, prompt.text,
                    temperature=temperature,
                    timeout=self.config.timeout,
                    include_usage=self.config.include_usage,
                    prompt_id=prompt.id,
                    concurrency=concurrency,
                    expected_keywords=prompt.expected_keywords,
                    min_output_tokens=prompt.min_output_tokens,
                )
                results.append(rr)
            return results

        workers = [asyncio.create_task(worker()) for _ in range(concurrency)]
        worker_results = await asyncio.gather(*workers, return_exceptions=True)
        for wr in worker_results:
            if isinstance(wr, BaseException):
                logger.warning("worker error: %s", wr)
                continue
            cell.requests.extend(wr)

        cell.aggregates = aggregate_requests(cell.requests, concurrency)
        return cell

    async def _run_cell_mixed(self, client, target, prompts, concurrency, temperature) -> CellResult:
        """Run a cell with mixed prompts: each worker rotates through the prompt pool."""
        cell = CellResult(
            target=target.name,
            prompt_id="mixed",
            prompt_label="混合",
            bucket="mixed",
            concurrency=concurrency,
        )

        # Warmup (discarded). Rotate prompts during warmup too.
        for i in range(self.config.warmup):
            prompt = prompts[i % len(prompts)]
            try:
                await stream_request(
                    client, target, prompt.text,
                    temperature=temperature,
                    timeout=self.config.timeout,
                    include_usage=self.config.include_usage,
                    prompt_id=prompt.id,
                    concurrency=concurrency,
                    expected_keywords=prompt.expected_keywords,
                    min_output_tokens=prompt.min_output_tokens,
                )
            except Exception:
                logger.debug("warmup error (ignored)")

        n = self.config.samples
        queue: asyncio.Queue[int] = asyncio.Queue()
        for i in range(n):
            queue.put_nowait(i)

        async def worker(worker_id: int):
            results: list = []
            while not self._aborted:
                try:
                    slot = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return results
                # Round-robin: worker_i picks prompt[slot % len(prompts)]
                prompt = prompts[slot % len(prompts)]
                rr = await stream_request(
                    client, target, prompt.text,
                    temperature=temperature,
                    timeout=self.config.timeout,
                    include_usage=self.config.include_usage,
                    prompt_id=prompt.id,
                    concurrency=concurrency,
                    expected_keywords=prompt.expected_keywords,
                    min_output_tokens=prompt.min_output_tokens,
                )
                results.append(rr)
            return results

        workers = [asyncio.create_task(worker(i)) for i in range(concurrency)]
        worker_results = await asyncio.gather(*workers, return_exceptions=True)
        for wr in worker_results:
            if isinstance(wr, BaseException):
                logger.warning("worker error: %s", wr)
                continue
            cell.requests.extend(wr)

        cell.aggregates = aggregate_requests(cell.requests, concurrency)
        return cell
