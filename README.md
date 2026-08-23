# llm-evl — LLM performance benchmark

Measure TTFT (time to first token), tokens/s, inter-token latency and more
across OpenAI-compatible LLM endpoints, with a local web UI.

## Install

```bash
cd D:\pxx\VIZAInUse\llm_evl
python -m venv .venv
.venv\Scripts\activate
pip install -e .
```

## Configure

```bash
copy targets.yaml.example targets.yaml
# edit targets.yaml: set base_url / model / api_key or api_key_env
```

## Run

```bash
# Web UI (auto-opens browser at http://127.0.0.1:7788)
llm-evl serve
# or
python -m llm_evl serve

# Minimal CLI (for automation)
llm-evl run --config targets.yaml -o run.json

# List configured targets
llm-evl list-targets
```

> CLI runs also persist to `runs/run_<id>.json`, so they appear in the UI
> History page alongside UI runs. `-o` writes an additional copy if given.

## Metrics

- **TTFT** time to first token (p50 / p90 / p99)
- **tokens/s** per-request output throughput (mean, p50); aggregate at concurrency > 1
- **ITL** inter-token latency (p50 / p90)
- **E2E** end-to-end latency; **generation duration** = E2E − TTFT
- **total output tokens**, **error rate**, **timeout count**

## Concurrency sweep

Closed-loop (`asyncio.Semaphore`) over `[1, 2, 4, 8, 16, 32]` by default.
Every metric is measured at each concurrency level.

## Run comparison

The Web UI has a「运行对比」page for comparing two historical runs:

- Cells are aligned on `target × prompt × concurrency`; only cells present in
  BOTH runs produce rows.
- For each cell every metric shows `base → other` plus a percentage change,
  tagged as improved (green) / regressed (red) / within ±2% (same).
  TTFT / E2E / ITL / error-rate count lower-is-better; tokens/s is
  higher-is-better.
- Summary cards show the mean percentage change per metric across all matched
  cells.

API equivalent: `GET /api/compare?base=<run_id>&other=<run_id>`.
