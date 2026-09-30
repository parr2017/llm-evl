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

### 中转站（OpenAI 兼容转发，给内网共用）
- **两个端口** — 管理端 `127.0.0.1:7788`（含供应商密钥，仅本机）；中转口 `0.0.0.0:7789`（只有 `/v1/*`）
- **登录门禁** — 管理端全部接口与页面需登录；首次启动自动生成一次性密码打印在日志里
- **客户端 key** — 每人一个，只存哈希、只显示一次，可随时停用/删除，可限定只能调某些组
- **自建聚合组** — 建一个组 `A` 放任意模型，外部只写 `model: "A"`，看不到背后是谁
- **同名模型池** — 未建组的模型按名字自动跨供应商聚合，无需配置
- **四种选路策略** — 轮询 / 随机 / 加权 / 优先级，逐成员可配权重与优先级
- **故障转移** — 429 / 5xx / 超时 / 连接失败自动换供应商重试；4xx 参数错误直接返回
- **熔断冷却** — 同一供应商连续失败 N 次后冷却 M 秒，期间自动跳过
- **原样透传** — 流式与非流式字节级转发，上游错误对象不被改写
- **调用日志 + 归属** — 模型、命中供应商、重试、TTFT、tok/s，以及**是哪个 key 调的**（只存内存，不记 prompt 与密钥）

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
- 中转站（模型池配置 + 调用日志）

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

### 4. 中转站（可选，给内网同事用）

```bash
cp relay.yaml.example relay.yaml
llm-evl serve
```

启动后会打印两个地址和**一次性管理员密码**（只显示这一次）：

```
管理端 (仅本机)  http://127.0.0.1:7788
中转口 (内网)    http://0.0.0.0:7789/v1   ← 内网访问用 <你的内网IP>:7789/v1
============================================================
首次启动，已生成管理员密码（只显示这一次）：
    XXXXX-XXXX
用户名：admin
```

浏览器打开 `http://127.0.0.1:7788` 登录，然后到「中转站 · 模型池」页：

1. 打开**中转开关**
2. 在**聚合组**里建一个组（例如 `A`），点「加成员（可多选）」—— 弹窗里列出全部
   供应商的全部模型，可按名字搜索、勾选多个一次加入
3. 在**客户端 key** 里点「新建 key」，填同事的名字 → key 只显示一次，复制给他

聚合组的配置**全部自动保存**，没有保存按钮；卡头会显示
`已自动保存 09:18:24` 这样的状态。组策略默认 `priority`（按实测顺序先用），
点「按实测重排优先级」后顺序立刻生效。界面上只调**优先级**，权重保持默认。

同事侧（任何 OpenAI 兼容客户端）：

```python
from openai import OpenAI
client = OpenAI(base_url="http://<你的内网IP>:7789/v1", api_key="sk-r_xxxx")
client.chat.completions.create(model="A", messages=[{"role": "user", "content": "你好"}])
```

```bash
# 这个 key 能看到哪些模型（聚合组 + 同名模型池）
curl -H 'Authorization: Bearer sk-r_xxxx' http://<你的内网IP>:7789/v1/models
```

**为什么要分两个端口**：管理端有 `/api/providers`，会返回 `targets.yaml` 里的**明文供应商密钥**。
绑在 `127.0.0.1` 上，内网就完全碰不到；中转端口只挂 `/v1/*` 两个端点，且都要 key。

> ⚠️ 中转口开启后，同事用 key 调用会**真实消耗供应商额度**。不需要时把中转开关关掉即可（无需重启）。
> 管理端登录是第二道防线，防的是端口转发 / 容器端口映射 / 内网穿透这类意外暴露。
> **登录一旦被冒用，所有供应商密钥会一次性全部泄露** —— 这是当前设计的已知残余风险。

失败处理规则：

| 上游情况 | 中转行为 |
|----------|----------|
| 429 / 5xx / 超时 / 连接失败 | 换一家供应商重试（最多 `max_retries` 次） |
| 4xx（如参数写错） | 不重试，原样返回上游错误 |
| 同一供应商连续失败 `failure_threshold` 次 | 冷却 `cooldown_seconds` 秒，期间自动跳过 |
| 流式响应已下发后中断 | 不换供应商（换会把两段回答拼在一起），断流并记一条失败日志 |
| 客户端中途断开 | 记一条 `cancelled` 日志（谁中断的仍然查得到） |

### 组内测试与自动排序

「中转站 · 模型池」页的每个成员行有「测」按钮，组标题栏有「测试全部」和
「按实测重排优先级」：

- **测试**：并发（上限 6）对每个成员发一次 `max_tokens=8` 的流式请求，测出通断、
  首字延迟与错误类型。**会产生真实调用**（几十个 token）。
- **按实测重排优先级**：重新测一遍，然后按「通的优先 → TTFT 从快到慢 → 不通的垫底」
  重写 `priority`。同 TTFT 按供应商名排序，结果可复现。**只改 priority，不动 weight。**

两个刻意的设计：

- **测试不会改动熔断状态**。熔断反映真实调用方的体验，一次手动测试不该改变生产选路。
- **优先级只在 `priority` 策略下参与选路**。组策略是 weighted / round_robin 时，
  页面会明确提示「重排结果暂时不生效」，而不是让你以为已经生效。

> ⚠️ 推理模型（Qwen3.6、DeepSeek-V4-Flash 等）先流 `reasoning_content` / `reasoning` / `thinking`
> 才轮到正文。首字延迟按**第一个 token**（含思考）计算，所以这些模型现在也能测出 TTFT ——
> 但因此**与 2026-09-30 之前的历史运行不可比**（旧值是"没测到"）。

### 聚合组 vs 同名模型池

|  | 同名模型池（自动） | 聚合组（自建） |
|--|------------------|--------------|
| 组名 | 就是真实模型名 | 你自己起的名字，如 `A` |
| 成员 | 所有提供该模型的供应商 | 你指定的任意 `(供应商, 模型)`，模型名可以各不相同 |
| 配置 | 无需配置，自动聚合 | 在「模型池」页建组、加成员 |
| 适合 | 同一个模型做故障转移 | 想把"日常对话"这类场景固定到某几个模型上 |

组名与真实模型名冲突时**组优先**（显式配置胜过隐式聚合）。组内某个供应商/模型被删掉时，
该成员会保留并标记为「配置有误」，而不是静默消失。


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
| `/v1/chat/completions` | POST | **中转**：OpenAI 兼容入口（流式 / 非流式） |
| `/v1/models` | GET | **中转**：合并后的模型池 |
| `/api/relay/config` | GET / POST | **中转**：配置、模型池与熔断状态 |
| `/api/relay/logs` | GET | **中转**：调用日志（`?limit=&model=&provider=`） |
| `/api/relay/breakers/reset` | POST | **中转**：复位熔断状态 |
| `/api/relay/clients` | POST / DELETE | **中转**：创建 / 删除客户端 key |
| `/api/relay/clients/<name>/enabled` | POST | **中转**：停用 / 启用某个 key |
| `/api/auth/login` / `logout` / `status` | POST / GET | 管理端登录 / 登出 / 状态 |

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
│   │   ├── auth.py         # 密码哈希 / 客户端 key / 会话签名
│   │   ├── relay.py        # 中转：聚合组 / 模型池 / 选路 / 熔断 / 调用日志
│   │   ├── stats.py        # 纯 Python 统计显著性（Welch t-test 等）
│   │   └── tokenizer.py    # token 计数
│   ├── api/
│   │   ├── routes.py       # FastAPI 路由（含对话对比 SSE 端点）
│   │   ├── auth_routes.py  # 管理端登录门禁 + 登录页
│   │   ├── relay_routes.py # 中转路由：/v1/* 与 /api/relay/*
│   │   ├── relay_manager.py# 中转跨请求状态（熔断 + 日志）
│   │   ├── run_manager.py  # 运行生命周期管理
│   │   └── server.py       # FastAPI 应用
│   ├── cli/
│   │   └── main.py         # Click CLI
│   └── web/
│       ├── index.html      # Vue3 + Element Plus 单文件前端
│       └── login.html      # 登录页（单文件，带自己的样式）
├── targets.yaml.example    # 供应商/目标配置模板
├── relay.yaml.example      # 中转配置模板（策略 / 熔断 / 权重）
├── pyproject.toml
└── LICENSE
```

## License

[MIT](LICENSE)
