"""End-to-end check: run the real Evaluator against the mock LLM server and
confirm the new evaluation dimensions reach the persisted run JSON.

Not part of the pytest suite (it writes to disk); run directly:
    python scripts/e2e_new_dimensions.py
"""

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from llm_evl.core.evaluator import Evaluator            # noqa: E402
from llm_evl.core.models import RunConfig, RunStatus, Target  # noqa: E402
from llm_evl.core.compare import compare_runs           # noqa: E402
from mock_llm_server import app as mock_app             # noqa: E402

MOCK_PORT = 9996


def start_mock() -> uvicorn.Server:
    cfg = uvicorn.Config(mock_app, host="127.0.0.1", port=MOCK_PORT, log_level="error")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            if httpx.get(f"http://127.0.0.1:{MOCK_PORT}/health", timeout=1).is_success:
                break
        except Exception:
            time.sleep(0.1)
    return server


async def main() -> int:
    server = start_mock()
    try:
        target = Target(
            name="mock/qwen-test", base_url=f"http://127.0.0.1:{MOCK_PORT}/v1",
            model="qwen-test", api_key="k",
            # Priced, so the cost dimension has to come out non-None.
            price_in=1.0, price_out=4.0,
        )
        cfg = RunConfig(
            concurrency_levels=[1, 2], samples=6, warmup=0, timeout=30.0,
            target_names=[target.name], prompt_ids=["medium"],
        )
        evaluator = Evaluator(cfg, [target])
        async for _event in evaluator.iter_events():
            pass
        run = evaluator.run_result
        assert run.status == RunStatus.COMPLETED.value, run.status

        data = run.to_dict()
        cells = data["cells"]
        print(f"cells: {len(cells)}")
        for c in cells:
            a = c["aggregates"]
            print(f"  conc={c['concurrency']:>2}  n={a['n']}  errs={a['n_errors']}"
                  f"  breakdown={a['error_breakdown']}"
                  f"  in_tok={a['total_input_tokens']}"
                  f"  out_tok={a['total_output_tokens']}"
                  f"  cost={a['total_cost']}"
                  f"  point_recall={a['quality_point_recall']}"
                  f"  qual={a['quality_mean']}")

        agg = cells[0]["aggregates"]
        req = cells[0]["requests"][0]
        checks = [
            ("error_breakdown present", "error_breakdown" in agg),
            ("malformed_chunks present", "malformed_chunks" in agg),
            ("input tokens captured", agg["total_input_tokens"] > 0),
            ("output tokens captured", agg["total_output_tokens"] > 0),
            ("cost computed from prices", agg["total_cost"] is not None
             and agg["total_cost"] > 0),
            ("per-request error_type", "error_type" in req),
            ("per-request finish_reason", "finish_reason" in req),
            ("per-request input_tokens", req["input_tokens"] > 0),
            ("per-request cost", req["cost"] is not None),
            ("point recall computed", agg["quality_point_recall"] is not None),
            ("json serialisable", json.dumps(data) is not None),
        ]

        # compare_runs must handle a real run pair end to end.
        cmp = compare_runs(data, data)
        checks.append(("compare_runs self-compare", cmp["n_matched"] == len(cells)))
        checks.append(("compare exposes resolution", cmp["resolution_pct"] is not None))

        print()
        ok = True
        for name, passed in checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {name}")
            ok = ok and passed
        print()
        print("ALL CHECKS PASSED" if ok else "SOME CHECKS FAILED")
        return 0 if ok else 1
    finally:
        server.should_exit = True


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
