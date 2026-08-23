"""Integration test: full Evaluator.iter_events() against a mock LLM server.

Starts a mock OpenAI-compatible streaming server in a background thread, then
runs the evaluator matrix end-to-end and asserts cells/metrics are produced.
"""

import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from llm_evl.core.evaluator import Evaluator
from llm_evl.core.models import RunConfig, RunStatus

# Make the mock server importable from tests/.
sys.path.insert(0, str(Path(__file__).parent))
from mock_llm_server import app as mock_app  # noqa: E402

MOCK_PORT = 9997


@pytest.fixture(scope="module")
def mock_server():
    config = uvicorn.Config(mock_app, host="127.0.0.1", port=MOCK_PORT,
                           log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    # Wait for readiness.
    import httpx
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{MOCK_PORT}/health", timeout=1).is_success:
                break
        except Exception:
            time.sleep(0.1)
    yield
    server.should_exit = True
    thread.join(timeout=5)


@pytest.mark.asyncio
async def test_evaluator_full_run(mock_server):
    from llm_evl.core.models import Target

    target = Target(name="mock", base_url=f"http://127.0.0.1:{MOCK_PORT}/v1",
                    model="mock-model", api_key="test-key")
    config = RunConfig(
        concurrency_levels=[1, 2],
        samples=3,
        warmup=1,
        timeout=30,
        temperature=0.0,
        include_usage=True,
    )
    evaluator = Evaluator(config, [target])

    cell_done = 0
    saw_run_terminal = False
    async for ev in evaluator.iter_events():
        if ev.type == "cell_done":
            cell_done += 1
            assert ev.cell["aggregates"]["n"] == 3
            assert ev.cell["aggregates"]["ttft_p50"] is not None

    assert cell_done == len(target and [1, 2]) * 3  # 2 concurrencies * 3 prompts = 6
    assert evaluator.run_result.status == RunStatus.COMPLETED.value
    assert len(evaluator.run_result.cells) == 6
    assert evaluator.run_result.finished_at is not None


@pytest.mark.asyncio
async def test_evaluator_cooperative_abort(mock_server):
    from llm_evl.core.models import Target

    target = Target(name="mock", base_url=f"http://127.0.0.1:{MOCK_PORT}/v1",
                    model="mock-model", api_key="test-key")
    config = RunConfig(concurrency_levels=[1], samples=5, warmup=0, timeout=30)
    evaluator = Evaluator(config, [target])

    count = 0
    async for ev in evaluator.iter_events():
        count += 1
        evaluator.abort()  # cooperative abort after first event
        if count > 1:
            break

    assert evaluator.run_result.status in (
        RunStatus.ABORTED.value, RunStatus.COMPLETED.value
    )
