# llm-evl

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)

LLM 性能基准测试 + 对话对比工具。测量 TTFT、tokens/s、ITL 等全链路指标，支持 OpenAI 兼容 API 的任意端点。自带 Web UI，支持并发扫描、运行对比、混合 Prompt 模式、多模型并排对话对比。

## Features

### 基准测试
- **TTFT / ITL / E2E / tok/s** 全链路延迟指标
- **并发扫描** — 闭合循环 `asyncio.Semaphore`，默认 1/2/4/8/16/32 并发档位
- **混合 Prompt 模式** — 每个 worker 轮询取不同 prompt，更接近真实负载
- **质量评分** — 可选的关键词命中 + 最低 token 数评分
- **结果持久化** — JSON 格式，Web UI 可随时回看

### 对话对比
- **多模型并排** — 选择多个模型，发送同一条消息，实时对比回复内容
- **流式输出** — 打字机效果，实时显示每个模型的生成过程
- **性能指标** — 每条回复下方显示 TTFT、tok/s、E2E、token 数
- **多轮对话** — 支持上下文连续对话
- **彩色标识** — 每个模型独立颜色，左边框 + 指标 badge 区分

### Web UI
- 目标配置管理
- Prompt 管理（内置 + 自定义）
- 运行实时进度
- 结果分析（性能洞察、趋势图、数据透视表）
- 历史运行对比
- 对话对比页面

### CLI
- `llm-evl run` 适合 CI/自动化
- `llm-evl serve` 启动 Web UI

## Install

```bash
git clone https://github.com/parr2017/llm-evl.git
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
cp targets.yaml.example targets.yaml
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
# 浏览器打开 http://127.0.0.1:7788
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

## Web UI 功能说明

### 对话对比

进入「对话对比」页面：

1. 在顶部多选框中选择要对比的模型（至少一个）
2. 在输入框中输入消息，按 Enter 或点击「发送」
3. 每个模型的回复会并排显示，带独立颜色标识
4. 回复完成后显示性能指标（TTFT、tok/s、E2E、token 数）
5. 支持多轮对话，自动携带上下文

指标说明：
- ⚡ **TTFT** — 首字延迟（秒），越低越好
- 🚀 **tok/s** — 输出吞吐（每秒 token 数），越高越好
- ⏱ **E2E** — 端到端延迟（秒），包含首字等待 + 生成时间
- 📝 **tokens** — 输出 token 总数

### 运行对比

在「历史」页面点击某次运行的「对比」按钮，可快速设为基准。然后在「运行对比」页面选择两次运行进行对比：

- 按 `target × prompt × 并发` 对齐两次运行的单元
- 每个指标显示 `base → other` 及百分比变化
- 绿色 = 改善 / 红色 = 退化 / 灰色 = ±2% 内

### 性能洞察

结果页面自动分析数据，生成洞察：
- 首字延迟随并发增长趋势
- 吞吐量瓶颈分析
- 错误率异常检测
- 模型间排名对比

## Metrics

| 指标 | 说明 | 参考范围 |
|------|------|----------|
| **TTFT p50** | 首字延迟（中位数） | <1s 快 / 1-5s 正常 / >10s 慢 |
| **tok/s** | 输出吞吐（每秒 token 数） | >2000 快 / 500-2000 正常 / <500 慢 |
| **ITL p50** | token 间延迟（中位数） | 越低越好 |
| **E2E p50** | 端到端延迟 = TTFT + 生成时间 | - |
| **错误率** | 失败请求占比 | <10% 正常 / >50% 服务崩溃 |

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/targets` | GET | 获取目标列表 |
| `/api/targets` | POST | 保存目标配置 |
| `/api/targets/test` | POST | 测试连接 |
| `/api/run/start` | POST | 启动基准测试 |
| `/api/run/stop` | POST | 停止运行 |
| `/api/run/stream` | GET | 实时事件流（SSE） |
| `/api/chat/stream` | POST | 对话对比流式响应（SSE） |
| `/api/runs` | GET | 历史运行列表 |
| `/api/runs/<id>` | GET | 获取运行详情 |
| `/api/compare` | GET | 对比两次运行 |
| `/api/prompts` | GET | 获取 prompt 列表 |
| `/api/prompts` | POST | 保存 prompt 列表 |

## Project Structure

```
llm-evl/
├── llm_evl/
│   ├── core/
│   │   ├── client.py       # 流式 HTTP 客户端 + 计时 + 对话流式
│   │   ├── evaluator.py    # 基准测试引擎
│   │   ├── metrics.py      # 指标聚合
│   │   ├── models.py       # 数据模型
│   │   ├── prompts.py      # 内置 prompt 池
│   │   └── tokenizer.py    # token 计数
│   ├── api/
│   │   ├── routes.py       # FastAPI 路由（含对话对比 SSE 端点）
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

## License

[MIT](LICENSE)
