# Changelog

## 2026-08-23 · 修复对比明细图例区标签畸形导致整个 Vue 应用不挂载

**现象**:补回 script 标签后页面仍全部不可用;无头浏览器实测 Vue 从未挂载,
但控制台零报错(静默失效)。

**根因**:「逐单元明细」图例行(549/551 行)存在畸形标签
`<span><span <span class="base-label">` —— 第二个 `<span` 被解析为属性,
结构错乱;且 559 行 `el-table-column` 内的 `<template #default>` 缺少
`</template>` 就直接闭合了列(对照 278-282 行正确写法)。浏览器按 HTML 规范
将后续内容留在惰性 template 内容里,包括页面尾部的 4 个 script 标签 →
**脚本永不执行、无任何报错**。

**定位过程**:无头 Edge dump-dom 发现脚本祖先链含 template;对 body 做
二分 + 自动补 `</template>` 闭合的对照实验,锁定 295~299 行引入的
template 吞噬,最终在 534~572 行区域确认两处缺陷。

**修复**:修正两处图例 span 标签;在 diff-val 之后、`</el-table-column>`
之前补回 `</template>`。无头浏览器验证:Vue 正常挂载、模板全编译
(原始 el-* 标签 0、未渲染 mustache 0)、目标表与历史表均由 API 数据渲染
(历史行显示新格式"08-23 08:33 · Qwen3.8-Q4 (7172b2)")、零控制台错误。

## 2026-08-23 · 修复 index.html 丢失 script 标签导致全站不可用

**现象**:服务启动后所有功能无法使用(页面空白/无交互)。

**原因**:index.html 内联 JS 的 `<script>`/`</script>` 标签丢失,
应用代码裸露在 body 中不被浏览器执行;且顶部 `const { createApp... }` +
BUILTIN_PROMPTS + api() 块重复两份(会触发重复声明 SyntaxError),
结尾 `app.use(ElementPlus); app.mount("#app");` 也重复两次。

**修复**:补回 `<script>`(CDN 引入后)与 `</script>`(</body> 前);
删除重复顶部块与重复挂载行。node --check 通过;
实测 GET / 返回含完整脚本的页面,/api/runs 返回 200。

## 2026-08-23 · 对比明细表可读性重构

**改动**(仅 index.html,前端):逐单元明细每个指标列由"基准→对比"单行改为
上下两行、各带"基准/对比"小标签;数值优侧绿色/劣侧红色/持平灰色(按指标方向判定);
变化标签升级为"+15.7% · 对比更差"文字结论;明细卡片顶部新增图例行,显示两次运行的
日期·模型·短 id 对应关系。后端接口不变。

**背景**:用户反馈原"15.046→17.403"写法看不出数值归属与优劣。

**验证**:node --check 通过;模板标签配对检查通过;服务重启后页面含新函数与图例。

## 2026-08-23 · 历史运行按开始时间倒序排列

**改动**:`run_manager.list_runs` 由按文件名(run_id)倒序改为按 `started_at` 倒序,
历史页表格与所有运行下拉均为最新在前。实测排序正确,服务已重启。

## 2026-08-23 · 运行选择器显示日期与模型

**改动**:`/api/runs` 列表新增 `target_names` / `models` 字段(run_manager.list_runs);
前端三个运行下拉(结果页、对比页基准/对比)标签由纯 run_id 改为
`MM-DD HH:mm · 模型名 (id 前 6 位)`(runLabel + fmtTimeShort),id 仅保留前 6 位作辅助。

**验证**:JS node --check 通过;服务重启后 `/api/runs` 实测返回新字段。

## 2026-08-23 · 新增并发趋势图与透视表

**改动**(仅 index.html,前端):在「结果」页三张柱状图之后新增两个板块:
1. **并发趋势折线图**:X轴=并发档位,每个target一条线,值=该target在该并发下所有prompt的平均值;底部radio组切换指标(首字p50/tok·s/E2E p50)。
2. **并发透视表**:行=target×prompt,列=各并发档位,单元格显示数值;每行最优值绿、最差红(沿用v-better/v-worse方向规则)。

**背景**:用户想要跨并发对比性能变化趋势，现有只能逐档手动切换。

**验证**:node --check 通过;服务重启后实测页面渲染包含新卡片(以真实历史 run 数据验证)。

## 2026-08-23 · 对比明细表可读性重构

**改动**(仅 index.html,前端):逐单元明细每个指标列由"基准→对比"单行改为
上下两行、各带"基准/对比"小标签;数值优侧绿色/劣侧红色/持平灰色(按指标方向判定);
变化标签升级为"+15.7% · 对比更差"文字结论;明细卡片顶部新增图例行,显示两次运行的
日期·模型·短 id 对应关系。后端接口不变。

**背景**:用户反馈原"15.046→17.403"写法看不出数值归属与优劣。

**验证**:node --check 通过;模板标签配对检查通过;服务重启后页面含新函数与图例。

## 2026-08-23 · 修复结果页 cellClass 未导出报错

**修复**:`index.html` setup 的 return 漏导出 `cellClass`,导致「结果」页渲染综合对比表时
抛 `TypeError: cellClass is not a function`(原有缺陷)。已加入导出,node --check 验证通过,
服务重启后页面正常。

## 2026-08-23 · 新增运行对比功能 (Web UI + /api/compare)

**背景**:此前只能查看单次 run 内各 target 的对比,无法对比两次测试结果。

**改动**:
- `llm_evl/core/compare.py`(新增):纯函数 `compare_runs(base, other)`,按
  `target × prompt_id × concurrency` 对齐两次 run 的 cells,对 6 项指标
  (ttft p50/p90、e2e_p50、itl_p50、tokens_per_second_mean、error_rate)
  计算绝对差与百分比变化,按"越低越好/越高越好"给出 better/worse/same(±2% 内)/na 判定,
  并输出总体平均变化 summary 与改善/退化计数。
- `llm_evl/api/routes.py`:新增 `GET /api/compare?base=<run_id>&other=<run_id>`。
- `llm_evl/web/index.html`:新增「运行对比」页(双下拉选择 run + 交换基准按钮 +
  总体变化卡片 + 可筛选(目标/并发)的逐单元差异明细表);历史页每行新增「对比」按钮,
  点击后以该 run 为基准跳转对比页。
- `tests/test_compare.py`(新增):9 个用例覆盖对齐、判定方向、未匹配计数、summary 均值、
  空值/零基准处理及 API 200/404。

**取舍**:cell 级全对齐而非仅汇总级;仅统计两次 run 共有单元,独有单元只报告数量不参与均值;
基准值为 None/0 时判定为 na(避免除零与无意义百分比)。

**验证**:`pytest tests/test_compare.py` 9 passed;JS 语法 node --check 通过;
服务重启后 `/api/compare` 实测返回正确 JSON。