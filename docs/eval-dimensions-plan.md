# 执行计划：补齐 LLM 评估维度 + UI 重构

> 创建于 2026-09-27 · 档级 **B**（涉及 DTO/数据模型 + 前后端联动）
> 依据：`llm_evl/web/preview.html`（UI 重构原型，已确认）

## 背景

当前 `llm-evl` 只测性能（TTFT / tok/s / E2E / 错误率），质量只有一个
"关键词命中 60% + 长度 40%"的粗分。表现为：不知道这次测了什么、质量不可信、
无法回答"要不要为提速多付钱"。UI 层叠卡片 + 渐变辉光，报告页是图表墙，
不给结论。

按 **3（后端补数据）→ 2（报告页重构）→ 1（全量换皮）** 推进：先有数据再改 UI。

---

## Step 3 · 后端补数据

### 3.1 错误分类（`error_type`）

**现状问题**：`client.py:98-100` 对 `json.JSONDecodeError` 直接 `continue`，
**解析失败被静默吞掉**，不计入错误率也不留痕。`finish_reason` 从未采集。
所有失败只有一个自由文本 `error` 字段，无法聚合。

**改动**：
- `models.py`：新增 `ErrorType` 枚举；`RequestResult` 加
  `error_type: str = ""`、`finish_reason: str = ""`、`malformed_chunks: int = 0`
- `client.py`：
  - HTTP 状态码分类：`http_429` / `http_4xx` / `http_5xx`
  - `httpx.TimeoutException` → `timeout`
  - `httpx.HTTPError` → `transport`
  - SSE JSON 解析失败 → `malformed_chunks += 1`（不再静默）
  - 采集 `finish_reason`，命中 `content_filter` → `content_filtered`
  - 收到零 content 且无 usage → `no_content`
- `metrics.py`：`CellAggregates` 加 `error_breakdown: dict[str, int]`、
  `malformed_chunks: int`
- `routes.py` / SSE 事件：透传新字段

**兼容性**：新字段全部有默认值，旧 `runs/*.json` 仍可读取。

### 3.2 成本维度（`price_in` / `price_out`）

**现状问题**：只统计 `output_tokens`，**`input_tokens` 根本没采集**；
无价格字段，因此无法算成本与性价比。

**改动**：
- `models.py`：`ProviderModel` / `Target` 加 `price_in`、`price_out`（¥/百万 token）
- `client.py`：从 `usage.prompt_tokens` 取 `input_tokens`（缺则 tiktoken 兜底）；
  `RequestResult` 加 `input_tokens`、`cost`
- `metrics.py`：`CellAggregates` 加 `total_input_tokens`、`total_cost`
- `targets.yaml.example`：补字段注释

**兼容性**：价格为 `None` → `cost = None`，成本维度显示"未覆盖"。

### 3.3 要点覆盖（`reference_points`）

**现状问题**：`0.3 < score < 0.7` 这类关键词分无法区分"答对"和"答长"。
胡编关键词得 0.60，正确但换词得 0.40，长废话反而加分。

**改动**：
- `models.py`：`PromptItem` 加 `reference_points: list[str]`
- `client.py` / `_score_quality`：新增 `quality_point_hits`、`quality_point_recall`。
  **权重规则（保持旧行为不变）**：
  - 无 `reference_points` → 完全沿用现有 0.6 关键词 / 0.4 长度
  - 有 `reference_points` → 要点 0.5，其余 0.5 在关键词/长度间按存在性分配
- `prompts.py`：加载/保存 `reference_points`

**兼容性**：现有 2 个质量测试断言（`== 1.0`、`0.3 < x < 0.7`）不设 points，走旧路径，不受影响。

### 3.4 统计显著性（Welch t-test）

**现状问题**：`compare.py:45` 用固定 `SAME_THRESHOLD_PCT = 2.0` 判
better/worse。5% 的随机抖动会被判成"退步"。

**改动**：
- 新增 `llm_evl/core/stats.py`：正则不完全 Beta 函数 → t 分布 CDF →
  `welch_ttest(a, b)` 返回 `(t, p, ci_low, ci_high)`
- `compare.py`：从 cell 的 `requests` 取**逐请求样本**（成功请求）做检验；
  不显著 → `verdict = "same"`；摘要增加 `n_significant_better/worse`
- 保持 `SAME_THRESHOLD_PCT` 作为 p 值不可得时的兜底

**依赖**：不引入 scipy，纯 Python 实现（避免新依赖）。

---

## Step 2 · 报告页重构

只改 `llm_evl/web/index.html` 的 `page==='results'` 模板：

1. **结论条** — 一句话结论 + 关键数字 + 样本量/显著性
2. **覆盖度条** — 性能/质量/稳定性/成本/对比，标出"未覆盖"
3. **五维评分卡** — 0–100 分 + 较基线 delta，未覆盖用禁用态
4. **维度 Tab** — 延迟/吞吐/质量/稳定性/成本/对比基线
5. **原始数据** — 默认折叠

后端就绪后，`cost`、`quality_point_recall`、`error_breakdown` 字段直接可用。

## Step 1 · 全量换皮

视觉：纯色三层表面 + 1px 发丝线 + 圆角 6px + 单一强调色，
去掉所有渐变/辉光/网格背景；自绘紧凑表格替换 Element Plus 表格；
导航 7 项 → 5 项。

---

## 任务进度表

| # | 任务 | 文件 | 状态 |
|---|------|------|------|
| 0 | 记录基线测试 | — | ✅ 31 passed |
| 1 | `ErrorType` 枚举 + `RequestResult` 新字段 | `core/models.py` | ✅ |
| 2 | 错误分类填充 + `malformed_chunks` 计数 | `core/client.py` | ✅ |
| 3 | `error_breakdown` 聚合 | `core/metrics.py` | ✅ |
| 4 | `price_in/out` + `input_tokens` + `cost` | `core/models.py` `core/client.py` | ✅ |
| 5 | 成本聚合 | `core/metrics.py` | ✅ |
| 6 | `reference_points` 要点覆盖 | `core/models.py` `core/client.py` `core/prompts.py` | ✅ |
| 7 | `stats.py` Welch t-test | `core/stats.py`（新建） | ✅ |
| 8 | compare 接入显著性 + BH FDR | `core/compare.py` | ✅ |
| 9 | 新字段 API 透传 | `api/routes.py` `api/run_manager.py` | ✅ |
| 10 | 补测试（42 个新用例） | `tests/test_eval_dimensions.py` | ✅ |
| 11 | 全量回归 | — | ✅ 73 passed |
| 11b | 端到端验证（真实 Evaluator + mock server） | `scripts/e2e_new_dimensions.py` | ✅ 13/13 |
| 12 | 报告页重构 | `web/index.html` | ✅ |
| 13 | 全量换皮（11 页统一设计系统） | `web/index.html` | ✅ |
| 13b | 功能/UI 一致性修正（4 处） | `web/index.html` | ✅ |
| 14 | 文档同步 + CHANGELOG | `CHANGELOG.md` | ✅ |
| 15 | 提交（需用户确认） | — | ☐ |

### 全量换皮的做法

**只替换整个 `<style>` 块，所有 class 名保持不变** —— 11 个页面模板的交互结构一行未改，
功能丢失在结构上不可能发生。用 `scripts/ui_inventory.py` 在换皮前后做程序化比对作为证据。

设计系统：三层表面 + 1px 发丝线 + 6px 圆角 + 单一强调色 `#5b8cff`；
CSS 变量 31 → 76，硬编码颜色 18 → **0**；ECharts 调色板改为运行时读 CSS 变量。
侧边栏图标由不一致的几何字符改为内联 SVG 精灵（16×16 统一）。

### 全量换皮中发现的 4 处功能/UI 不一致

1. **API key 明文全量显示** —— 供应商列表直接渲染完整密钥，超长密钥还会撑破布局。
   改为掩码指纹，仅点击才查看完整值。
2. **「调用日志」页暗示存在中转能力** —— 后端 `GET /api/relay/logs` 实际 404。
   现按 404 区分"暂无数据"与"功能未实现"，导航加 `未实现` 徽标。
3. **对比页把"无法检验"显示成"无差异"** —— `n_tested=0` 时仍显示"0 改善 / 0 退化"。
   改为中性标签 + 明确说明。
4. **Element Plus dark css-vars 是死代码** —— `<html>` 无 `.dark` 类，从未生效，已移除。

### 报告页实测发现并修复的问题

1. **`<b>` 标签被转义成字面量** —— 结论条用了 `{{ }}` 而非 `v-html`。
2. **质量维度误判为"已覆盖"** —— `_hasQuality` 原本把旧关键词分也算作覆盖，
   导致未配置要点时显示 `0/100` 与 `NaN%`。改为只有 `reference_points` 存在才算覆盖。
3. **稳定性页谎报"零错误"** —— 旧运行没有 `error_breakdown` 字段，
   `hasErrors` 因此为 false，而同页 dim 卡却显示 10.7% 错误率，自相矛盾。
   改为回退到 `n_errors`，并显式提示"旧版本运行，未分类"。

### 兼容性验证

21 个改动前保存的历史运行文件全部正常渲染（缺字段走 legacy 分支）。

### 实施过程中的发现（已量化）

1. **SSE 解析失败此前被静默吞掉**：`client.py` 对 `json.JSONDecodeError` 直接 `continue`，
   既不计入错误率也不留痕。现已用 `malformed_chunks` 计数并区分 `parse` / `no_content`。
2. **`input_tokens` 从未采集**，所以成本维度根本无法计算。已从 `usage.prompt_tokens` 取，
   缺失时用 tiktoken 兜底。
3. **`compare.py` 的 ±2% 判据是噪声**：蒙特卡洛验证表明 samples=20 时最小可检测差异
   （MDE）约 **20%**，远大于 2%。旧的 2% 阈值会把 5% 的抖动判成退步。
   改为 Welch t-test + BH-FDR（多重比较校正，否则 ~50 次比较会产生 ~2.5 个假警报）。
4. **`samples=20` 统计上不可用**：要检测 15% 的变化需每单元 **≈105** 个样本（80% 功效），
   要检测 10% 需 **≈236** 个。工具现在会直接报出 `resolution_pct` 和 `required_n`。
5. **`error_rate == 0` 之前返回 `na`**：但 0 错误率是"曾经健康"这一真实状态，不是缺数据。
   改为按指标区分（`ZERO_IS_VALID`），而 TTFT=0 这种物理上不可能的值仍按缺数据处理。
6. **质量权重保持向后兼容**：不设 `reference_points` 时仍是 0.6 关键词 / 0.4 长度，
   旧分数可与历史运行对比；设了要点才启用 0.5 要点 + 0.5 其余。


---

## 受影响文件清单（改前审阅）

| 文件 | 改动 | 风险 |
|------|------|------|
| `llm_evl/core/models.py` | 4 个 dataclass 加字段，全部带默认值 | 低（向后兼容） |
| `llm_evl/core/client.py` | 错误分支补 `error_type`；SSE 解析失败计数 | 中（改动控制流，需回归） |
| `llm_evl/core/metrics.py` | 聚合加 3 个字段 | 低 |
| `llm_evl/core/compare.py` | verdict 判据从固定 % 改为显著性 | 中（行为变化，需测试） |
| `llm_evl/core/stats.py` | 新建 | 低（纯函数） |
| `llm_evl/core/prompts.py` | 读写 `reference_points` | 低 |
| `llm_evl/api/routes.py` | 新字段透传 | 低 |
| `llm_evl/web/index.html` | 报告页 + 换皮 | 中（前端） |
| `tests/*` | 补测试 | — |

**不需要改**：`evaluator.py`（除透传新字段）、`tokenizer.py`、`run_manager.py` 主体。
