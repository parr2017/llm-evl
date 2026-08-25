# llm-evl

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

LLM 性能基准测试工具：测量 TTFT（首字延迟）、tokens/s、ITL（token 间延迟）等指标，支持 OpenAI 兼容 API 的任意端点。自带 Web UI，支持并发扫描、运行对比、混合 Prompt 模式。

## Features

- **TTFT / ITL / E2E / tok/s** 全链路延迟指标
- **并发扫描** — 闭合循环 `asyncio.Semaphore`，默认 1/2/4/8/16/32 并发档位
- **混合 Prompt 模式** — 每个 worker 轮询取不同 prompt，更接近真实负载
- **Web UI** — 目标配置、Prompt 管理、实时进度、结果分析、历史对比
- **CLI** — `llm-evl run` 适合 CI/自动化
- **质量评分** — 可选的关键词命中 + 最低 token 数评分
- **结果持久化** — JSON 格式，Web UI 可随时回看

## Install

```bash
git clone https://github.com/YOUR_USERNAME/llm-evl.git
cd llm-evl
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
# source .venv/bin/activate
pip install -e .
```

## Quick Start

### 1. 配置目标

```bash
cp llm_evl/targets.yaml.example targets.yaml
```

编辑 `targets.yaml`，填入你的 API 端点：

```yaml
targets:
  - name: gpt-4o
    base_url: https://api.openai.com/v1
    model: gpt-4o
    api_key_env: OPENAI_API_KEY

  - name: deepseek
    base_url: https://api.deepseek.com/v1
    model: deepseek-chat
    api_key_env: DEEPSEEK_API_KEY
```

> `api_key` 可明文写入（方便本地调试），也可通过 `api_key_env` 引用环境变量（推荐）。

### 2. Web UI

```bash
llm-evl serve
# 浏览器自动打开 http://127.0.0.1:7788
```

### 3. CLI

```bash
# 完整扫描
llm-evl run --config targets.yaml -o run.json

# 混合 Prompt 模式
llm-evl run --config targets.yaml --mix-prompts

# 指定目标
llm-evl run --config targets.yaml --target gpt-4o,deepseek

# 查看已配置目标
llm-evl list-targets
```

> CLI 结果自动保存到 `runs/run_<id>.json`，也会出现在 Web UI 历史页面。

## Metrics

| 指标 | 说明 | 参考范围 |
|------|------|----------|
| **TTFT p50** | 首字延迟（中位数） | <1s 快 / 1-5s 正常 / >10s 慢 |
| **tok/s** | 输出吞吐（每秒 token 数） | >2000 快 / 500-2000 正常 / <500 慢 |
| **ITL p50** | token 间延迟（中位数） | 越低越好 |
| **E2E p50** | 端到端延迟 = TTFT + 生成时间 | - |
| **错误率** | 失败请求占比 | <10% 正常 / >50% 服务崩溃 |

## Concurrency Sweep

闭合循环并发模型：在并发级 `c` 下，`c` 个 worker 从共享队列取请求槽位，前一个完成后立即发起下一个，直到收集完 N 个有效样本。每个指标在每个并发级独立测量。

## Run Comparison

Web UI 提供「运行对比」页面：

- 按 `target × prompt × 并发` 对齐两次运行的单元
- 每个指标显示 `base → other` 及百分比变化
- 绿色 = 改善 / 红色 = 退化 / 灰色 = ±2% 内

API: `GET /api/compare?base=<run_id>&other=<run_id>`

## Mixed Prompts Mode

开启 `--mix-prompts` 后，矩阵从 `target × prompt × concurrency` 简化为 `target × concurrency`，每个 worker 轮询从 prompt 池中取不同 prompt，更真实地模拟生产环境的混合负载场景。

## Project Structure

```
llm-evl/
├── llm_evl/
│   ├── core/
│   │   ├── client.py       # 流式 HTTP 客户端 + 计时
│   │   ├── evaluator.py    # 基准测试引擎
│   │   ├── metrics.py      # 指标聚合
│   │   ├── models.py       # 数据模型
│   │   ├── prompts.py      # 内置 prompt 池
│   │   └── tokenizer.py    # token 计数
│   ├── api/
│   │   ├── routes.py       # FastAPI 路由
│   │   ├── run_manager.py  # 运行生命周期管理
│   │   └── server.py       # FastAPI 应用
│   ├── cli/
│   │   └── main.py         # Click CLI
│   └── web/
│       └── index.html      # Vue3 + Element Plus 单文件前端
├── targets.yaml.example    # 配置模板
├── pyproject.toml
└── LICENSE
```

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/targets` | GET | 获取目标列表 |
| `/api/targets` | POST | 保存目标配置 |
| `/api/targets/test` | POST | 测试连接 |
| `/api/run/start` | POST | 启动基准测试 |
| `/api/run/stop` | POST | 停止运行 |
| `/api/run/status` | GET | 获取运行状态（SSE） |
| `/api/run/stream` | GET | 实时事件流（SSE） |
| `/api/runs` | GET | 历史运行列表 |
| `/api/runs/<id>` | GET | 获取运行详情 |
| `/api/compare` | GET | 对比两次运行 |
| `/api/prompts` | GET | 获取 prompt 列表 |
| `/api/prompts` | POST | 保存 prompt 列表 |

## License

[MIT](LICENSE)
