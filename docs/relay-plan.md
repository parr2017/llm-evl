# 执行计划：中转站（OpenAI 兼容转发层）

> 创建于 2026-09-30 · 档级 **C**（新模块 + 对外公开端点 + 前后端联动 + 新增公开数据面）
> 依据：`llm_evl/web/index.html:1358-1435`（中转站两页 UI 占位）、`CHANGELOG.md:37`（已记录该能力后端为 404）

---

## 人话摘要

**解决什么问题**：现在「中转站」两个页面是假的——导航上写着「调用日志 · 未实现」，点进去是后端 404。
项目已经能测一堆供应商，但没有「一个入口统一转发」的转发层，所以这套中转能力目前**只是界面占位，一行后端代码都没有**。

**做什么**：把占位变成真功能。客户端只写一个 `base_url`（`http://127.0.0.1:7788/v1`）+ 一个模型名，
本项目负责在「同名模型的多个供应商」里挑一家转发过去，失败了自动换一家，每次调用记一条日志。

**怎么做**：新增 `llm_evl/core/relay.py`（选路 + 重试 + 熔断 + 日志）→ 新增 `llm_evl/api/relay_routes.py`
（对外 `/v1/chat/completions`、`/v1/models`，对内 `/api/relay/*`）→ 配置放独立 `relay.yaml`
→ 前端两页接真实数据。日志只放内存（最近 1000 条），重启清空。

**做完效果**：

```bash
curl http://127.0.0.1:7788/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"deepseek-v4-flash","messages":[{"role":"user","content":"hi"}]}'
```

这条请求会命中 `sensenova` 或 `amd`（按策略），结果原样返回；上游 429/5xx/超时则自动换另一家重试；
3 次连续失败的那家在 60 秒内不再被选中。「调用日志」页能看到每一次调用的模型、命中供应商、TTFT、tok/s、重试次数。

---

## 一、背景与目标

### 验收标准（做完必须全部为真）

| # | 标准 | 怎么验 |
|---|------|--------|
| 1 | `/v1/models` 返回按模型名合并后的池子 | curl 返回 `data[].id` 去重后 = 供应商配置里的模型名集合 |
| 2 | `/v1/chat/completions` 流式与非流式都能透传 | mock server 端到端脚本 2/2 通过 |
| 3 | 4 种策略都能选中池内成员 | 单测 4 例 |
| 4 | 429/5xx/超时自动换供应商重试；400 不重试 | 单测 2 例 + mock server 故障注入 |
| 5 | 连续失败 3 次 → 冷却 60s → 恢复 | 单测 1 例（用假时钟） |
| 6 | 每次调用产生一条日志，字段齐全，环形上限生效 | 单测 1 例 |
| 7 | UI 两页显示真实数据，无「未实现」字样 | 浏览器实测 + `scripts/ui_inventory.py` 前后 diff |
| 8 | 现有功能零回归 | `pytest tests/` 全绿（基线 73 passed） |

### 非目标（本轮明确不做）

- ❌ 客户端鉴权（relay key）——用户已选不含此项，见下方决策点 1
- ❌ 多进程 / 多 worker 共享熔断状态与日志（单进程内存态）
- ❌ 日志落盘、按时间轮转、导出 CSV
- ❌ 流式响应中途换供应商（技术原因见风险 3）
- ❌ 改动现有基准测试链路（`client.py` / `evaluator.py` 主体不碰）

---

## 二、现状与约束（已确认事实，附证据）

| 事实 | 证据 |
|------|------|
| UI 已有中转站两页，但是占位 | `web/index.html:1358-1399`（模型池）、`1402-1435`（调用日志） |
| 前端已在优雅处理 404 空态 | `web/index.html:2440-2461`（`relayLogsUnavailable`） |
| 导航显式标注「未实现」 | `web/index.html:2349-2354`（`badge:"未实现", badgeWarn:true`） |
| UI 已暗示设计：base_url / 4 策略 / 熔断参数 | `web/index.html:1365-1378`（`http://127.0.0.1:7788/v1`、`round_robin/random/weighted/priority`、`连续失败 3 次 → 冷却 60s`） |
| 后端零 relay 代码 | 全仓 `grep -i relay\|中转 --include=*.py` → 0 命中 |
| 模型池前端已在客户端算（同名聚合） | `web/index.html:2389-2401`（`modelPool` computed） |
| 供应商→扁平 target 的现成转换 | `core/models.py:147-161`（`Provider.to_targets()`，已带 `price_in/out`） |
| 配置加载统一走 `config_path` | `api/run_manager.py:27`、`api/server.py:16-30`（`create_app(config_path)`） |
| 已有 mock OpenAI 兼容 server 可复用 | `tests/mock_llm_server.py:18-51`（流式 + `/v1/models`） |
| SPA 兜底路由会吞掉未注册路径 | `api/routes.py:398-417`（`spa_fallback` 返回 index.html；仅对 `api/` 前缀返回 JSON 404） |
| 已有错误分类可复用做重试判据 | `core/models.py:22-50`（`ErrorType`，含 `RATE_LIMIT`/`SERVER_ERROR`/`TIMEOUT`/`TRANSPORT`） |

**约束**：
- SPA 兜底在 `routes.py`，且按 include 顺序匹配 → **relay 路由必须先于 `routes` router 注册**（见步骤 9）
- 不能引入新依赖（沿用 `httpx` / `fastapi` / `pyyaml`，与 `pyproject.toml:13-21` 一致）
- `scripts/ui_inventory.py` 是既有的「功能零丢失」取证工具，本轮继续用它做前后 diff

**假设（待验证）**：
- 假设：真实供应商对同名模型的响应体结构一致（OpenAI 兼容）→ 步骤 6 的 mock 测试只能证明「对 OpenAI 兼容端点成立」，
  真实供应商在步骤 13 手工抽查 1-2 个验证
- 假设：`relay.yaml` 不存在时应使用默认值而非报错（与 `targets.yaml` 缺失时报错的行为不同，因为中转是可选项）→ 步骤 1 验证

---

## 三、方案与取舍

### 决策 1：转发层放哪（新路由 vs 塞进现有 routes.py）

| 做法 | 优点 | 代价 |
|------|------|------|
| **新建 `api/relay_routes.py`（推荐）** | 与基准测试路由物理隔离；`include_router` 顺序可控（兜底问题自然解决）；中转是独立关注点 | 多一个文件 |
| 塞进 `routes.py` | 不多文件 | `/v1/*` 注册位置在文件中部，容易被误放到兜底路由之后，重构一挪就坏 |

### 决策 2：转发实现放 `core/relay.py` 而非路由内联

路由层只做「解析请求 → 调 service → 拼响应」。选路/重试/熔断/日志是纯逻辑，放 `core/` 后可单测、不依赖 FastAPI
（与本仓 `core/` 纯逻辑 + `api/` 转层的既有分层一致）。

### 决策 3：熔断状态存内存 dict，不落盘

理由：进程重启后冷却状态丢失的后果只是「多试一次坏供应商」，不值得引入持久化复杂度与写盘失败处理。

### 决策 4：非流式请求也走同一条转发路径

不做两条代码路径。区别只在「要不要把上游 SSE 逐块吐给客户端」，其余（选路、重试、熔断、日志）完全共用。

### 决策 5：日志只记指标，不记请求体

理由：请求体含用户 prompt、`Authorization` 含真实 key。日志字段固定为
`时间 / 请求模型 / 命中供应商 / 状态 / TTFT / tok/s / tokens / 重试次数 / 错误类型`，**不含任何密钥与 prompt 正文**。

---

## 四、影响面

| 文件 | 改动 | 风险 |
|------|------|------|
| `llm_evl/core/relay.py` | **新建**（配置 / 池 / 熔断 / 选路 / 日志 / 转发） | 中（新数据面，需完整测试） |
| `llm_evl/api/relay_routes.py` | **新建**（`/v1/*` + `/api/relay/*`） | 中（公开端点） |
| `llm_evl/api/server.py` | `create_app` 增 `relay_config_path` 参数；注册 relay router（须在 `routes` 之前） | 中（注册顺序） |
| `llm_evl/cli/main.py` | `serve` 增 `--relay-config` 选项 | 低 |
| `relay.yaml` / `relay.yaml.example` | **新建** | 低 |
| `llm_evl/web/index.html` | 中转两页接真实数据 + 权重/优先级可编辑 | 中（前端） |
| `tests/test_relay.py` | **新建** | — |
| `tests/mock_llm_server.py` | 增故障注入（`mock-429` / `mock-500` 模型） | 低 |
| `scripts/e2e_relay.py` | **新建**（端到端验证） | — |
| `README.md` / `CHANGELOG.md` / 本文件 | 文档同步 | 低 |

**不改动**：`core/client.py`、`core/evaluator.py`、`core/metrics.py`、`core/models.py`、`api/run_manager.py`

---

## 五、步骤分解

> 每步 = 做什么 + 改哪个文件 + 怎么验证（预期输出）。一步一验证，可独立完成。

### Phase A · 核心逻辑（不暴露任何端点，可安全回滚）

| # | 做什么 | 文件 | 怎么验证 |
|---|--------|------|---------|
| A1 | 定义 `RelayConfig`（enabled/strategy/failure_threshold/cooldown_seconds/max_retries/timeout/log_limit）+ `load_relay_config`（**文件缺失返回默认值，不抛错**）+ `save_relay_config` | `core/relay.py` | `pytest tests/test_relay.py -k Config` → 5 passed（默认值/读文件/写回/非法 strategy 回落默认/坏 YAML 不崩） |
| A2 | `build_model_pool(providers)`：按模型名跨供应商聚合成 `ModelPool`，成员带 `weight`/`priority`（来自 relay.yaml，缺省 1 / 999） | `core/relay.py` | `pytest -k Pool` → 3 passed（同名聚合 / 单供应商单点 / 读权重与优先级） |
| A3 | `CircuitBreaker`：连续失败计数、冷却到期自动恢复、成功清零；`round_robin` 用单调递增游标 | `core/relay.py` | `pytest -k Breaker` → 4 passed（阈值触发 / 冷却过期恢复 / 成功清零 / 游标轮转） |
| A4 | `select_member(pool, cfg, breaker)`：实现 `round_robin`/`random`/`weighted`/`priority`，跳过冷却中的成员 | `core/relay.py` | `pytest -k Select` → 6 passed（四策略各 1 + 冷却成员被跳过 + 池空报错） |
| A5 | `RelayCall` 数据类 + `RelayLog` 环形缓冲（`deque(maxlen=log_limit)`，支持按模型过滤与统计） | `core/relay.py` | `pytest -k Log` → 3 passed（上限截断 / 模型过滤 / 统计聚合） |
| A6 | `RelayService.forward()`：选供应商 → 发请求 → 失败按可重试性换下一家（最多 `max_retries`）→ 记日志。**流式：只在收到第一个上游 chunk 之前允许换供应商** | `core/relay.py` | `pytest -k Forward` → 5 passed（成功直通 / 429 换供应商 / 400 不重试 / 全失败返 502 / 日志字段齐全），用 `tests/mock_llm_server.py` |
| A7 | 写 `relay.yaml.example`；确认 `.gitignore` 不误伤 | `relay.yaml.example` | 文件存在；`.venv\Scripts\python -c "import yaml;yaml.safe_load(open('relay.yaml.example'))"` 无异常 |

### Phase B · API 层

| # | 做什么 | 文件 | 怎么验证 |
|---|--------|------|---------|
| B1 | 管理端点：`GET /api/relay/config`（含合并后的池与各成员熔断态）、`POST /api/relay/config`（保存策略/权重/优先级/熔断参数）、`GET /api/relay/logs?limit&model`、`POST /api/relay/breakers/reset` | `api/relay_routes.py` | `pytest -k RelayApi` → 4 passed（读/写往返/日志过滤/重置熔断） |
| B2 | 对外端点：`POST /v1/chat/completions`（流式 SSE 直通 + 非流式 JSON 直通）、`GET /v1/models`（合并池）。**enabled=false 时返回 403 并说明如何开启** | `api/relay_routes.py` | `pytest -k RelayV1` → 4 passed（models 合并 / 非流式透传 / 流式透传 / disabled 403） |
| B3 | `create_app` 注册 relay router **在 `routes` 之前**，加 `relay_config_path`；CLI `serve --relay-config` | `api/server.py` `cli/main.py` | 单测证明 `/v1/chat/completions` **不会**落到 `spa_fallback`（老行为是返回 index.html）；`llm-evl serve --help` 含新选项 |
| B4 | 真实上游抽查：手工 curl 1-2 个真实供应商模型（**会产生少量真实费用**），确认透传与 TTFT 正常 | — | curl 返回 200 且 body 含 `choices`；日志页出现对应记录 |

### Phase C · 前端

| # | 做什么 | 文件 | 怎么验证 |
|---|--------|------|---------|
| C1 | 改前取证 | — | `python scripts/ui_inventory.py > logs/ui_before.json` |
| C2 | 模型池页接真实数据：策略下拉可改可存、熔断参数可改可存、成员行显示并可编辑 `权重`/`优先级`、显示每成员熔断状态（正常/冷却中）；**删掉「规划中」提示** | `web/index.html` | `python scripts/ui_inventory.py > logs/ui_after.json` + diff：**零删除项**；浏览器实测 |
| C3 | 调用日志页接真实数据：去掉「未实现」徽标与 404 空态文案，保留模型筛选并加供应商筛选，日志行显示重试次数与错误类型 | `web/index.html` | 同上 + 浏览器实测两种情况（有数据 / 无数据） |
| C4 | 端到端脚本：真 mock server 跑 `models` / 流式 / 非流式 / 429 重试 / 熔断冷却，输出 PASS-FAIL 汇总 | `scripts/e2e_relay.py` | 脚本自身输出 `5/5 passed` |

### Phase D · 收尾

| # | 做什么 | 怎么验证 |
|---|--------|---------|
| D1 | 全量回归 | `pytest tests/` → 全绿（73 基线 + 新增用例） |
| D2 | 文档同步：`README.md` 加「中转站」章节（含 curl 示例 + 安全提示）、`CHANGELOG.md` 新条目、勾本文件进度表 | 三处文件均已更新 |
| D3 | 提交（**需用户确认后执行**） | `git status` 变更清单经用户过目 |

---

## 六、风险与回滚

1. **`/v1` 是无鉴权的开放转发口，会用你的真 key 打真实供应商并产生费用。**
   缓解：默认 `enabled: false`，必须显式开启；服务只监听 `127.0.0.1`；文档写明**不要** `--host 0.0.0.0`。
   回滚：把 `relay.yaml` 的 `enabled` 改回 `false`（立即生效，下次请求 403），无需重启。

2. **SPA 兜底吞掉 `/v1/*`** —— 老兜底会把任何未注册路径返回 `index.html`（`routes.py:398-417`），
   症状是 curl 拿到一坨 HTML 而不是 JSON 错误。
   缓解：relay router 先 include（B3）+ 专门的回归单测锁死这个行为。
   回滚：无（纯新增路由，不影响既有路径）。

3. **流式响应一旦开始下发，就不能换供应商** —— 客户端已经收到一半内容，此时换供应商会产生
   「两段回答拼在一起」的破损输出。
   缓解：只在收到第一个上游 chunk **之前**允许重试/换供应商；之后出错只能断流并记一条 `ok=false` 日志。
   这一点在 A6 的实现与测试里显式体现。

4. **上游 SSE 格式异常** → 透传时把畸形块原样转发（不吞）。与 `core/client.py:120-124` 的处理不同，
   那里吞掉并计数是因为「客户端自己要出报告」，这里必须「不篡改上游内容」。
   缓解：畸形块计数进日志的 `error_type=parse`，UI 可区分。

5. **多 worker / 重启导致熔断状态丢失** → 后果仅是「多试一次坏供应商」，可接受（决策 3）。
   若将来要修，需要把熔断状态挪到 Redis 之类，本轮不做。

6. **回滚总开关**：`relay.enabled=false` 即刻停用中转；删除 `api/relay_routes.py` 的 include 即完全下线。
   两者都不影响基准测试与 Web UI 其余 11 个页面。

---

## 七、决策点（需拍板）

| # | 问题 | 推荐 | 备选 | 状态 |
|---|------|------|------|------|
| 1 | `/v1` 是否要客户端鉴权（relay key）？ | **本轮不做**（用户已选），靠 `enabled: false` + 仅本机监听兜底 | 加一层 Bearer 校验（+1 天，+2 处代码） | ✅ 用户已定：本轮不做 |
| 2 | `relay.enabled` 默认值 | **默认 `false`**，但**开关做在界面上**（用户要求：「在界面就能控制开关」）。「模型池」页有 `el-switch` + 保存按钮，写入 `relay.yaml`，即时生效、无需重启 | 默认 `true`（开箱即用，但服务一起来就对外暴露转发口） | ✅ 用户已定：界面可开关，落盘默认 false |
| 3 | 流式中途失败是否换供应商 | **不换**，断流 + 记日志（风险 3 已论证） | 立即换供应商（会产生破损输出） | ✅ 用户已定：不换 |

### 决策 2 的落点

`enabled` 不再只是「改 YAML 文件」：模型池页顶部有开关，切换后点「保存配置」即
`POST /api/relay/config` → 写 `relay.yaml` → 下一次请求立即按新配置执行
（服务每次调用都重读配置，无需重启）。默认仍为 `false`，避免服务一起来就
用真实密钥对外转发。

---

## 八、交付物清单

- [ ] `llm_evl/core/relay.py`（新）
- [ ] `llm_evl/api/relay_routes.py`（新）
- [ ] `relay.yaml` + `relay.yaml.example`（新）
- [ ] `tests/test_relay.py`（新，约 25 个用例）
- [ ] `scripts/e2e_relay.py`（新）
- [ ] `tests/mock_llm_server.py`（增故障注入）
- [ ] `api/server.py` / `cli/main.py`（接线）
- [ ] `llm_evl/web/index.html`（中转两页接通）
- [ ] `README.md` / `CHANGELOG.md` / 本计划进度表

---

---

# 阶段二 · 鉴权 + 自建聚合组（同端口、内网可用）

> 创建于 2026-09-30 · 在阶段一（转发层已落地）之上追加 · 档级 **C**（新增认证面 + 公开数据面扩展）
> 用户需求原文：「apikey 也要加上，我希望内网的其他人也能调用」「加一个登陆页面，另外中转口还是使用相同端口」
> 「我可以自己选择聚合组，例如我创建一个 A modelName，外部调用时就写 modelName 是 A，但是里面放什么模型，我自己选择」

## 人话摘要

**解决什么问题**：阶段一的中转口是「谁都能调」的，既没有客户端 key，也无法安全地开放给内网。
同时中转只能按「同名模型」聚合，用户没法把**不同模型**归到一个对外名字下。

**做什么**：三件事。
1. **管理端登录页** —— 挡住 UI 和所有 `/api/*`。同端口意味着内网可达，而 `/api/providers`
   会明文返回全部供应商密钥，所以登录必须 fail-closed：默认所有路径都要登录，只放行登录页与中转口。
2. **中转客户端 key** —— `/v1/*` 需 `Authorization: Bearer <key>`，key 只存哈希、按 key 记归属。
3. **自建聚合组** —— 用户建一个组 `A`，自己往里放任意 `(供应商, 模型)`；外部只写 `model: "A"`。

**做完后效果**：

```yaml
# relay.yaml（界面上配，存的是同样的内容）
groups:
  A:
    description: 日常对话
    strategy: weighted
    members:
      - {provider: sensenova, model: deepseek-v4-flash, weight: 1, priority: 0}
      - {provider: amd,        model: qwen3.8-flash,    weight: 3, priority: 1}
```

```bash
# 同事只需一个 key + 一个组名，看不到背后是谁
curl http://10.0.0.5:7788/v1/chat/completions \
  -H 'Authorization: Bearer sk-r_xxxx' \
  -d '{"model":"A","messages":[{"role":"user","content":"hi"}]}'
```

---

## 一、目标与验收标准

| # | 标准 | 怎么验 |
|---|------|--------|
| 1 | 未登录时 `/`、`/api/*` 全部被挡（HTML 重定向到 `/login`，接口返回 401 JSON） | 单测 + 浏览器 |
| 2 | 登录后 UI 与全部既有 API 正常 | 既有 125 个测试 + 浏览器 |
| 3 | `/v1/*` **不**要求登录，但要求 Bearer key | 单测（无 key → 401，有错 key → 401） |
| 4 | key 以哈希存储，列表只显示前缀，完整值只在创建时出现一次 | 单测 + 检查 `relay.yaml` |
| 5 | 首次启动无管理员密码 → 自动生成随机密码、打印到日志、落盘哈希 | 单测 + 看日志 |
| 6 | 自建组可用：组内**不同模型**都能被选中，且转发时 `model` 字段已改写成成员的真实模型名 | 单测（关键：不改写上游会收到 `model:"A"`） |
| 7 | 组名与自动同名池同名时，组优先 | 单测 |
| 8 | 组可按 key 授权（某个 key 只能用指定的组） | 单测 |
| 9 | 调用日志记录是哪个 key 调的 | 单测 |
| 10 | 原有 125 个测试 + 阶段一 e2e 30/30 不回归 | `pytest` + `scripts/e2e_relay.py` |

---

## 二、现状与约束（附证据）

| 事实 | 证据 |
|------|------|
| `/api/providers` 返回**明文** `api_key`（UI 只做掩码，接口不拦） | `core/models.py:136-145`（`to_config_dict` 含 `api_key`） |
| SPA 兜底会把未登录访问 `/` 直接吐出 index.html | `api/routes.py:398-417` |
| 转发时 `payload` 原样发上游，`model` 未按成员改写 | `core/relay.py` `_attempt`（阶段一遗留：同名池时恰好正确，**组场景下会直接把 `model:"A"` 发给上游**） |
| 模型池目前只按模型名聚合，成员必与池同名 | `core/relay.py::build_model_pool` |
| 无任何认证/会话代码 | 全仓 grep `session|cookie|login|password` → 0 命中 |
| 无新依赖可用（依赖表见 `pyproject.toml:13-21`） | 认证必须纯标准库（`hashlib` / `hmac` / `secrets`） |
| Web UI 是单文件 `index.html`（无外部资源） | `llm_evl/web/` 目录 |

**约束**：
- 不引第三方依赖（passlib / bcrypt 一律不用）；PBKDF2-HMAC-SHA256 由 `hashlib.pbkdf2_hmac` 提供
- 登录页单独一个 HTML 文件（`login.html`），由 SPA 兜底直接命中静态文件，不进 Vue 应用
- 认证用中间件而非逐路由依赖：逐路由容易漏掉一个端点 = 一个未受保护的洞（fail-closed）

---

## 三、方案与取舍

### 决策 1：认证用中间件，不用 `Depends` 逐路由

| 做法 | 优点 | 代价 |
|------|------|------|
| **中间件 + 路径白名单（推荐）** | 新加端点默认受保护，漏不掉 | 白名单写错会挡住自己（可用测试锁死） |
| 逐路由 `Depends(require_admin)` | 显式 | 每加一个端点都要记得加，忘一个就是洞 |

### 决策 2：会话用签名 Cookie，不用服务端 session 表

无状态：cookie = `过期时间戳.HMAC-SHA256(secret, 用户名+时间戳)`，密钥落 `relay.yaml`。
好处：无需存 session、重启不掉线、不能伪造。单进程 LAN 工具，够用。

### 决策 3：密码与 key 都只存哈希

- 管理员密码：`pbkdf2_sha256$迭代次数$盐$摘要`，比较用 `hmac.compare_digest`
- 客户端 key：`sha256$摘要`（key 本身是高熵随机串，不需要慢哈希），比较同样用 `compare_digest`
- 列表只显示前缀（`sk-r_a1b2…`），完整值仅创建时返回一次
- 代价：忘了 key 只能重建（UI 提供「重建」），换不来「随时查看完整 key」

### 决策 4：组与自动同名池共存，组优先

用户不建组时，行为与阶段一完全一样（模型名即池名），不破坏已有配置与测试。
组名与模型名冲突时**组优先** —— 显式配置胜过隐式聚合。

### 决策 5：转发时按成员改写 `model` 字段

组的成员模型名各不相同，必须把 `model` 改成成员的真实模型名再发给上游。
同时响应头加 `x-relay-model`，方便排查「组 A 到底打到了哪个模型」。

### 决策 6：组内成员失效时保留并标记，不静默消失

供应商被删/模型被改名后，组里的引用会失效。静默消失会让用户的组「悄悄少一个供应商」，
所以保留该成员并标 `available: false`，选路跳过它，UI 显示红色「配置有误」。

---

## 四、影响面

| 文件 | 改动 | 风险 |
|------|------|------|
| `llm_evl/core/auth.py` | **新建**：PBKDF2 密码、key 生成/校验、会话签名 | 中（认证代码，写错就是洞） |
| `llm_evl/core/relay.py` | 加 `clients` / `groups` 配置、组池构建、key 校验、payload 改写 | 中（动转发主链路） |
| `llm_evl/api/auth_routes.py` | **新建**：`/login`、`/api/auth/{login,logout,status}` | 中 |
| `llm_evl/api/server.py` | 认证中间件 + 注册 auth router | 中（中间件影响所有请求） |
| `llm_evl/api/relay_routes.py` | `/v1/*` 强制 Bearer key + 组授权 | 中 |
| `llm_evl/api/relay_manager.py` | 配置保存时保留 `auth:` 段 | 低 |
| `llm_evl/web/login.html` | **新建**：登录页 | 低 |
| `llm_evl/web/index.html` | 401 跳登录、聚合组管理、客户端 key 管理 | 中（前端） |
| `tests/test_relay.py` / 新增 `tests/test_auth.py` | 新增用例 | — |
| `relay.yaml.example` / `README.md` / `CHANGELOG.md` | 文档 | 低 |

---

## 五、步骤分解

| # | 做什么 | 文件 | 怎么验证（预期） |
|---|--------|------|------------------|
| E1 | `hash_password` / `verify_password` / `new_password` / `generate_key` / `hash_key` / `sign_session` / `verify_session` | `core/auth.py` | `pytest tests/test_auth.py` → 全绿；含「错误密码不通过」「篡改 cookie 不通过」「过期 cookie 不通过」 |
| E2 | `RelayConfig` 加 `clients` / `groups`；`load/save` 支持两段（`relay:` + `auth:`）互不覆盖 | `core/relay.py` | 既有 48 个 relay 测试仍全绿 |
| E3 | `build_model_pool` 支持组：成员模型名可与组名不同；组名优先；失效成员标 `available:false` | `core/relay.py` | 新增 4 例（组聚合 / 组优先 / 混合模型 / 失效成员） |
| E4 | `forward` 按成员改写 `model`，响应头 `x-relay-model`，日志记 client key | `core/relay.py` | 新增 2 例（组内不同模型确实按名发出；日志含 key） |
| E5 | 认证中间件 + 路径白名单 + `/login` 路由 | `api/server.py` `api/auth_routes.py` | 未登录：`/`→302 `/login`、`/api/targets`→401；`/v1/*` 不受中间件管 |
| E6 | 首次启动自动生成管理员密码并打印 | `api/auth_routes.py` 或 `server.py` | 启动日志出现一次性密码；`relay.yaml` 只落哈希 |
| E7 | `/v1/*` 强制 Bearer key、校验启用状态、组授权、更新 `last_used` | `api/relay_routes.py` | 无 key 401 / 错 key 401 / 停用 key 401 / 越权组 403 / 正常 200 |
| E8 | 登录页 + 前端 401 跳登录 + 登出 | `web/login.html` `web/index.html` | 浏览器实测：登不进 → 跳登录页；登录后 UI 正常；登出后回到登录页 |
| E9 | 聚合组管理 UI（建组 / 删组 / 加删成员 / 权重优先级 / 策略） | `web/index.html` | 浏览器实测建组 `A` → 保存 → `/v1/models` 出现 `A` |
| E10 | 客户端 key 管理 UI（新建 / 停用 / 删除 / 一次性展示 / 复制） | `web/index.html` | 浏览器实测；确认 `relay.yaml` 无明文 key |
| E11 | 补测试 | `tests/test_auth.py` `tests/test_relay.py` | `pytest tests/` 全绿（目标 ≥ 160） |
| E12 | 扩展 e2e：登录 → 建 key → 建组 → 外部调用 → 日志归属 | `scripts/e2e_relay.py` | 脚本输出全绿 |
| E13 | 文档同步 + CHANGELOG + 本表勾选 | `README.md` `CHANGELOG.md` | — |

---

## 六、风险与回滚

1. **认证中间件挡住自己**（配置/路由白名单写错 → 全部 401，进不去也改不了配置）。
   缓解：白名单集中在 `server.py` 一个常量里 + 专门单测；**应急开关**：环境变量
   `LLM_EVL_NO_AUTH=1` 可临时关闭中间件（本机排障用，默认不开）。
   回滚：设该环境变量，或 `git checkout` 本轮文件。
2. **用户把自己锁在门外**（忘记自动生成的密码）。
   缓解：密码以哈希落盘，忘了就删 `auth` 段 → 下次启动重新生成并打印；UI 上提示这一条。
3. **中转口 key 泄露 = 别人能用你的供应商花钱**。
   缓解：key 只存哈希、可随时停用、调用日志按 key 归属、每个 key 可限定可用组。
4. **同端口暴露带来的剩余风险**：登录后 `/api/providers` 仍会明文返回供应商密钥。
   这是阶段一就有的行为，本轮只是加了门禁。若要更严，可后续让管理端也走掩码接口。
5. **组内模型名与上游不一致导致 404**：转发时已按成员改写 `model`，并在响应头给出
   `x-relay-model`，便于一眼定位。
6. **回滚总开关**：`relay.enabled: false` 停转发；`LLM_EVL_NO_AUTH=1` 停门禁；两者互不依赖。

---

## 七、决策点

| # | 问题 | 本轮采用 | 理由 |
|---|------|----------|------|
| 1 | key 存哈希还是明文 | **哈希** | relay.yaml 泄露不该等于中转口失窃；UI 仍能认人（看前缀） |
| 2 | 是否按 key 记归属 | **记** | 内网多人共用时能看出谁在刷、能单独停掉一个人 |
| 3 | 是否支持按 key 限定可用组 | **支持（可选，默认全允许）** | 多人分组的常见需求，实现约 10 行 |
| 4 | 登录态 | **签名 Cookie，12 小时** | 无状态、重启不掉线 |
| 5 | 中转口与 UI 是否同端口 | **不同端口**（用户最终决定：管理端 7788 只听 `127.0.0.1`，中转口单独一个端口听 `0.0.0.0`） | 同端口需要给全部 `/api/*` 也加门禁，且管理 API 仍在内网暴露面上 |

### 决策 5 的落点（用户中途改了主意）

原方案是同端口 + 全站登录门禁。用户最终选择**端口分离**：

| 服务 | 监听 | 暴露内容 | 谁能访问 |
|------|------|----------|----------|
| 管理端 | `127.0.0.1:7788` | Web UI + 全部 `/api/*`（含明文供应商密钥） | 只有本机 |
| 中转口 | `0.0.0.0:7789` | **只有** `/v1/*` | 内网，需 Bearer key |

同一个进程跑两个 uvicorn Server（`asyncio.gather`），共享同一份熔断与日志状态。
第二个 app 只挂 relay router，**没有管理端点、没有静态文件** —— 内网扫描器能看到的
只有 `/v1/chat/completions` 和 `/v1/models` 两个端点，且都要 key。

这样一来 `/api/providers` 的明文密钥**根本不在内网暴露面上**，登录页退居第二道防线
（防端口转发/误暴露），不再是唯一防线。

**用户同时决定**：登录后 `/api/providers` 仍返回明文密钥（不改成掩码，也不做
「输密码才显示」）。已撤回掩码改动。残余风险记录在案：**管理员账号一旦被冒用，
所有供应商密钥一次性全丢**。

---

## 任务进度表（阶段二）

| # | 任务 | 状态 | 验证证据 |
|---|------|------|----------|
| E1 | `core/auth.py` 密码 / key / 会话 | ✅ | `tests/test_auth.py` **20 passed** |
| E2 | `RelayConfig` 加 clients/groups；两段式配置互不覆盖 | ✅ | 既有 relay 测试不回归 + `test_relay_save_does_not_clobber_the_auth_section` |
| E3 | 组池构建：混合模型 / 组优先 / 失效成员标记 | ✅ | `TestGroups` 5 例 |
| E4 | 转发按成员改写 `model` + 日志记 client | ✅ | `test_forward_rewrites_model_to_the_member` 等 2 例 + 浏览器实测上游收到 `Qwen3.6` |
| E5 | 认证中间件 + 白名单 + `/login` | ✅ | `TestGateBlocks` / `TestLogin` 12 例 |
| E6 | 首次启动自动生成管理员密码并打印 | ✅ | `TestBootstrap` 3 例 + 实际启动日志（一次性密码只出现在服务日志里，不入版本库） |
| E7 | `/v1/*` 强制 Bearer + 组授权 + last_used | ✅ | `TestClientKeys` 11 例 |
| E8 | 登录页 + 前端 401 跳登录 + 登出 | ✅ | 浏览器实测：未登录 302 → 错误密码报错 → 登录成功 → 登出后重新锁上 |
| E9 | 聚合组管理 UI | ✅ | 浏览器实测：建组 A → 加成员 → 保存落盘 |
| E10 | 客户端 key 管理 UI | ✅ | 浏览器实测：新建 key 弹窗一次性展示；`relay.yaml` 确认只有哈希 |
| E11 | 补测试 | ✅ | `pytest tests/` **193 passed**（125 → 193） |
| E12 | 扩展 e2e | ✅ | `scripts/e2e_relay.py` **60/60 passed** |
| E13 | 文档同步 + CHANGELOG | ✅ | `README.md`（新增登录/双端口/聚合组章节 + API 表）、`CHANGELOG.md`、`relay.yaml.example` |
| — | 提交 | ☐ | 待用户确认 |

## 阶段二实施中修正的偏差

1. **端口从「同端口」改为「分离」**（用户中途决定）。因此 `v1_router` 与 `relay_router`
   必须拆开，且中转口只挂前者 —— 见 CHANGELOG「我自己挖的一个洞」。
2. **配置结构：`models.<名>.members.<供应商>` 之外新增 `groups.<名>.members[]`**，
   因为组内成员的模型名与组名不同，权重/优先级必须挂在成员上。
3. **`/api/providers` 的明文密钥按用户决定保留**，不改成掩码；改为靠端口分离 + 登录门禁
   降低暴露面。残余风险已写进 README 与 CHANGELOG。

## 阶段二顺带修掉的既有 bug（同一类根因）

浏览器不认 `<foo />` 自闭合写法 → 兄弟节点被吞 → 静默消失。共 4 处，其中 2 处是上一轮
就存在的既有 bug（「历史」页目标数/单元数两列从未显示、「Prompt 管理」少 2 列）。
验证手段：逐页比对表头单元格数与行单元格数（不是肉眼扫）。

## 遗留

- **`/api/providers` 登录后仍返回明文密钥**（用户选择）。管理员被冒用 = 密钥全丢。
- **中转口无速率限制**：一个 key 可以狂刷。多人共用时若需要限流，应在 `/v1` 前加一层
  按 key 的令牌桶（下一轮）。
- **熔断与日志仍是单进程内存**：多 worker 或重启会丢。
- **`LLM_EVL_NO_AUTH=1` 是绕过门禁的应急开关**，部署时不要长期设置。

---

# 阶段三 · 组内模型通断测试 + 按实测自动重排优先级

> 创建于 2026-09-30 · 档级 **B**（新增探测能力 + 一个写配置的按钮）· 依赖阶段一/二

## 人话摘要

**解决什么问题**：建好聚合组后，成员里哪个是死的、哪个慢，只能靠真调用去撞。
本项目本来就是干测速的，却不能测自己中转配置里的成员 —— 有点讽刺。

**做什么**：组内每个成员加一个「测」按钮，组级加「测试全部」和「按实测重排优先级」。
探测复用已有的 `core/client.stream_request`（已经在「供应商测试」页证明可用）。

**做完后效果**：点一次「按实测重排优先级」→ 依次测每个成员 → 通的按 TTFT 从快到慢排好，
不通的全部沉到队尾 → 存进 `relay.yaml`。组策略为 `priority` 时，调用方直接享受这个顺序
（最快先试，失败自动退到下一个）。

---

## 一、目标与验收标准

| # | 标准 | 怎么验 |
|---|------|--------|
| 1 | 可单独测一个成员，5 秒内出结果（通/断 + TTFT + tok/s + 错误类型） | 单测 + 浏览器 |
| 2 | 可一次测整组 | 单测 |
| 3 | 未知 (供应商, 模型) 被拒 400（防止这个端点被当任意 URL 探测器） | 单测 |
| 4 | 自动重排：通的在前按 TTFT 升序，不通的全在最后；同 TTFT 按供应商名（结果可复现） | 单测 |
| 5 | 重排结果落盘到 `relay.yaml` 的 `groups.<名>.members[].priority` | 单测 + 读文件 |
| 6 | 组名不存在 / 组内无成员 → 明确报错，不静默 | 单测 |
| 7 | 策略不是 `priority` 时给出提示（优先级不参与该策略的选路） | 浏览器实测 |
| 8 | 探测结果在刷新页面后仍可见（内存态） | 浏览器实测 |
| 9 | 既有 193 个测试 + e2e 60/60 不回归 | pytest + e2e |

## 二、现状与约束

| 事实 | 证据 |
|------|------|
| 已有可复用的探测实现（返回 ok/ttft/tps/error_type） | `core/client.py::stream_request` |
| 「供应商测试」页已经在用它 | `api/run_manager.py:171-251`（`test_provider`） |
| 转发时能拿到成员的真实模型名与 base_url | `core/relay.py::RelayMember._target()` |
| `stream_request` 目前不能限制输出长度 | `core/client.py:48-61`（无 `max_tokens`） |
| 组的 priority 目前只能手改 | `web/index.html` 成员行的 `el-input-number` |

**约束**：
- 探测是**真实调用**（真实供应商、真实 token 消耗），必须让用户在界面上知道 → 页面上写明
- 探测**不触碰熔断器**：熔断反映真实流量，不该被一次手动测试改写
- 不新增依赖；不新增 npm/构建步骤

## 三、方案与取舍

### 决策 1：探测放 `RelayService.probe_members()`，不放路由

路由只做「解析 → 调 service → 拼响应」。探测是纯逻辑（并发 + 收集 + 排序），
放 `core/` 才能单测，也和现有分层一致。

### 决策 2：`stream_request` 加一个可选 `max_tokens`

不传时行为完全不变（基准测试链路零影响），只有探测传 `max_tokens=8`。
不加的话一次探测会生成完整回答（几百 token × N 个成员），又慢又花钱。

### 决策 3：排序规则放后端（`rank_members`），不放前端

规则是业务判断（「不通的必须垫底」），放前端就没法单测；而且后端能保证
「探测 → 排序 → 落盘」是一个原子步骤，前端不会算出半截状态。

### 决策 4：点「重排」总是重新探测，不复用旧结果

按钮语义是「按实测重排」= 现在的实测。拿 10 分钟前的数据排序，
会把已经挂掉的模型排到最前面 —— 比不点这个按钮更糟。

### 决策 5：探测必须校验成员存在于配置中

否则这个端点就成了「让服务器去请求任意 URL」的 SSRF 通道（虽然要管理员登录，
但没必要留这个面）。只允许探测 `targets.yaml` 里真实存在的 `(供应商, 模型)`。

## 四、影响面

| 文件 | 改动 | 风险 |
|------|------|------|
| `llm_evl/core/client.py` | `stream_request` 加可选 `max_tokens`（默认 None，行为不变） | 低 |
| `llm_evl/core/relay.py` | `ProbeResult`、`probe_members()`、`rank_members()` | 中（新增并发逻辑） |
| `llm_evl/api/relay_manager.py` | 探测结果内存存储 + `auto_priority()` | 中（要写配置） |
| `llm_evl/api/relay_routes.py` | `POST /api/relay/probe`、`POST /api/relay/groups/<名>/auto-priority` | 中（新公开端点） |
| `llm_evl/web/index.html` | 成员行「测」+ 结果展示；组级「测试全部」「按实测重排」 | 中（前端） |
| `tests/test_relay.py` | 新增 `TestProbe` / `TestAutoPriority` | — |
| `README.md` / `CHANGELOG.md` / `relay.yaml.example` | 文档 | 低 |

## 五、步骤分解

| # | 做什么 | 怎么验证（预期） |
|---|--------|------------------|
| F1 | `stream_request` 加 `max_tokens` 可选参数 | 既有 193 测试全绿（不传时行为不变） |
| F2 | `probe_members()`：并发（信号量 6）逐个探测，返回 ok/ttft/tps/error_type | 4 例：通/断/错误类型/未知成员被拒 |
| F3 | `rank_members()`：通→TTFT 升序→不通垫底；同 TTFT 按供应商名 | 4 例：混合/全通/全断/同 TTFT 可复现 |
| F4 | `RelayManager.auto_priority()`：探测→排序→写配置 | 3 例：落盘/组不存在/组为空 |
| F5 | 两个路由 + 探测结果进 `/api/relay/config` | 随 F4 覆盖 |
| F6 | 前端：成员行「测」+ 结果；组级两个按钮；策略提示 | 浏览器实测 5 项 |
| F7 | 补测试 | `pytest tests/` 全绿（目标 ≥ 200） |
| F8 | 文档同步 + CHANGELOG + 本表勾选 | — |

## 六、风险与回滚

1. **探测会产生真实费用与真实流量**。缓解：`max_tokens=8`、短 prompt、页面上写明、
   并发上限 6；不点就不测。
2. **探测把上游打挂**：6 个并发 × 一个「hi」不会有实质影响；若担心可先只测单个成员。
3. **`auto_priority` 会覆盖手调过的 priority**。缓解：按钮在 UI 上二次确认，
   并在成功提示里打印新顺序，让你能看出改了什么。
4. **策略不是 priority 时排序无效**：不改策略的话重排没有任何效果。
   缓解：按钮后提示当前策略；若要按顺序调用，需把策略设为 `priority`。
5. **回滚**：删掉两个路由即完全下线；`stream_request` 的新参数不传即无行为变化。

## 七、决策点

| # | 问题 | 本轮采用 | 理由 |
|---|------|----------|------|
| 1 | 排序依据 | 通断 + TTFT | 用户明确指定 |
| 2 | 探测是否改权重 | **只改 priority** | 用户说的是「优先级」 |
| 3 | 探测是否影响熔断 | **不影响** | 熔断代表真实流量健康度 |
| 4 | 重排是否复用旧探测结果 | **每次重新探测** | 旧数据可能已失效 |
| 5 | 探测是否算真实调用 | **是**（max_tokens=8） | 无法假装；已在 UI 与文档写明 |

## 任务进度表（阶段三）

| # | 任务 | 状态 | 验证证据 |
|---|------|------|----------|
| F1 | `max_tokens` 可选参数 | ✅ | 不传时行为不变，220 个测试全绿 |
| F2 | `probe_members()` | ✅ | `TestProbe` 7 例（通/断/错误类型/顺序/不触发熔断） |
| F3 | `rank_members()` | ✅ | `TestRank` 5 例（含同 TTFT 可复现） |
| F4 | `auto_priority()` 落盘 | ✅ | `TestAutoPriorityApi` 7 例（落盘/权重不动/404/400） |
| F5 | 两个路由 | ✅ | 含"未知成员被拒 400"（防 SSRF） |
| F6 | 前端 UI | ✅ | 浏览器实测：单测 / 全测 / 重排 / 策略提示，控制台 0 错误 |
| F7 | 补测试 | ✅ | `pytest tests/` **220 passed**（193 → 220） |
| F8 | 文档同步 | ✅ | README / CHANGELOG / relay.yaml.example |
| — | 提交 | ☐ | 待用户确认 |

## 阶段三实测发现（重要）

对着真实供应商跑「测试全部」，结果暴露了两个既有缺陷：

1. **TTFT 对整个推理模型族测不出来。** `client.py` 只把 `delta.content` 当作首字信号，
   而推理模型先流 `reasoning_content` / `reasoning` / `thinking`。结果：6 个成员里 5 个
   「通但 TTFT 为空」—— 首字延迟恰恰是这个项目的主指标，却在主力模型上全程失明。
   已修：三种字段名都计时（只影响计时，不计入 token 数、不进 TTS、不转发给对话 UI）。
   **副作用**：推理模型的 TTFT 现在会与历史运行不可比（旧数据是"没测到"）。
2. **无 key 的供应商全都连不上。** `client.py` 无条件发 `Authorization: Bearer `，
   空值是非法 header，httpx 直接拒（本该是正常请求）。本地关掉鉴权的 vLLM 会中招。
   已修：没有 key 就不发这个 header。

顺带修掉的两处自己写错的地方：
- `on_token(None)`：推理 delta 没有 content 时仍会回调，导致对话页 SSE 收到 null token（测试抓到）
- 一次性密码被 stdout 缓冲吞掉（首次启动时用户拿不到密码 = 把自己锁在门外）

## 遗留

- 中转口无速率限制（多人共用时需要按 key 限流）
- 熔断与日志仍是单进程内存
- 探测不参与熔断（刻意）；若希望"测不通就自动踢出轮换"，需要另设开关

---

# 阶段四 · 配置自动保存 + 权重移出界面 + 组按优先级先选

> 2026-09-30 · 档级 **B**（前端交互重构 + 一处路由语义修正）· 无新端点

## 验收

| # | 标准 | 结果 |
|---|------|------|
| 1 | 任何配置改动自动落盘，无需保存按钮 | ✅ 卡头状态胶囊实测 |
| 2 | 连续输入防抖、离散动作立即保存 | ✅ 700ms 防抖 / 开关与增删成员立即 |
| 3 | 保存失败不假装成功，改动留在页面 | ✅ 状态转红 + 错误写进 title |
| 4 | 界面不再出现权重输入 | ✅ 成员行只剩 测 / 优先级 / 移除 |
| 5 | 新建组默认 priority | ✅ |
| 6 | 组按优先级先选真实生效 | ✅ 4 次调用 100% 命中 priority 0 |
| 7 | 同优先级成员不会永久钉死一个供应商 | ✅ 层内轮转，6 例测试 |
| 8 | 写入结构 = 读取结构 | ✅ `TestConfigShapeRoundTrip` 6 例 |
| 9 | 232 测试 + e2e 60/60 + 零删除 | ✅ |

## 本阶段修掉的三个问题

1. **`priority` 在同优先级下永久钉死一个供应商**（路由语义缺陷）：
   `min(priority, provider)` + 全体默认 999 = 刚建的组永不故障转移。
   改为「取最低优先级层 + 层内轮转」。
2. **自动保存写出的 `models` 结构与读端不一致**（我自己引入的）：
   写成 `models.<模型>.<供应商>`，读端找 `models.<模型>.members.<供应商>`。
   保存成功、读取静默忽略 —— 不报错的 bug 最难查，故补了契约测试。
3. **测试期间残留一个多余聚合组** `deepseekv4-flash`：来源是早期浏览器误点击，
   已确认当前代码不复现并删除。

## 遗留

- 中转口无限流（多人共用建议加按 key 令牌桶）
- 熔断/日志仍是单进程内存
- 探测不参与熔断（刻意设计）
- `weighted` 策略仍可用但界面上暂不暴露权重入口
