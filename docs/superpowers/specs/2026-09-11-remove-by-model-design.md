# 移除 dashboard「按模型（自上次重启起累计）」卡片 — 设计 spec

## Goal
从 ctyun-stream-fix-proxy dashboard 移除"按模型·自上次重启起累计，重启清零"卡片，并连带清除其后端计数链路，做到零死代码；按天×模型副表与顶部汇总卡不受影响。

## 方案决策（写死）

**方案 A（仅前端）**：删 HTML 卡片 + `renderModelChips` + 调用点。`STATS["by_model"]` 写入、snapshot 字段保留。
Trade-off：改动最小，但后端成为死代码（`/api/stats` 唯一消费者是 dashboard 自身，`/api/stats` handler 在 `ctyun-stream-fix-proxy.py:590 self._send_json(200, stats_snapshot())`，无其他调用方），违反"无死代码"铁律。**不推荐。**

**方案 B（前后端全删，推荐）**：方案 A + 删除 `STATS["by_model"]` 初始化、`_record_request`/`_record_empty_retry` 的 by_model 写入分支、`stats_snapshot` 的 `by_model` 深拷贝行、`.chip-model` 死 CSS、footer 文案同步、相关测试改写。共 2 文件，无死代码残留。**采纳。**

**方案 C（前端删 + 后端保留并在 footer 标注内部指标）**：同 A 的死代码问题，还扩大 footer 文案负担。**不推荐。**

**snapshot 字段口径（二选一已定）**：`stats_snapshot()` **不再返回 `by_model`**（删 `ctyun-stream-fix-proxy.py:356`），不留"保留但前端不用"的半死状态。

## Files to Change

全部位于 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/`。

**`ctyun-stream-fix-proxy.py`**（8 处，锚点已实测核实）：
1. `:750-753` HTML — 删除整个 `<section class="card">`（751 标题行含 `<span class="chip">自上次重启起累计，重启清零</span>`，752 `<div class="strip" id="model-chips">`）。
2. `:882-899` — 删除 `function renderModelChips(byModel) {...}` 全函数。
3. `:1027` `poll()` 的 `.then` 回调 — 删除 `renderModelChips(snap.by_model || {});` 调用行。
4. `:101` `STATS` 初始化字面量 — 删除 `"by_model": {}` 键（保留同行的 `"daily_by_model": {}`）。
5. `:288-295` `_record_request(method, path, status, dur_ms, filtered, model=None, error=False)` — 删除 by_model 写入块（289-295：`by_model = STATS["by_model"]` 至 `entry["filtered"] += filtered`）；**保留** `if model:` 外层守卫与 296-302 的 `daily_by_model` 写入块。
6. `:334-339` `_record_empty_retry(model=None)` — 删除 by_model 写入块（335-339）；同样**保留** `if model:` 守卫与 340-345 的 daily_by_model 写入。
7. `:356` `stats_snapshot() -> dict` — 删除 `snap["by_model"] = {k: dict(v) ...}` 行；保留 358-360 `daily_by_model` 双层深拷贝。
8. `:702-704` CSS — 删除 `.chip-model` 两条规则（`:697 .strip` 仍被 `#poison-strip` 使用，保留）。另 `:788` footer 文案"按模型与按天×模型计数自进程启动累计，不持久化（重启清零）"改为"按天×模型计数自进程启动累计，不持久化（重启清零）"；`:789` 主表≥副表口径句不动。

**`ctyun-stream-fix-proxy.test.py`**（3 处）：
1. `:474-484` `test_by_model_accumulation_and_cap` — 删除 `mod.STATS["by_model"]` 断言（477-481），保留并保留名 `daily_by_model` 每日 cap 断言（483-484）；改名为 `test_daily_by_model_cap`。
2. `:817-819`（集成测试内）— 删除 `snap["by_model"]` 两行断言；保留 822-824 的 `snap["daily_by_model"]` 断言。
3. `:1089-1099` `test_by_model_retries_dimension` — 整个删除；retries 归因覆盖已由 `test_daily_by_model_matrix_fallback`（:538-557，551 行断言 retry 落入 daily_by_model）承担，不损失覆盖。

**保留不动（核实结论）**：`BY_MODEL_CAP = 32`（`:44`，daily_by_model 分支 298/342 仍用）；`RECENT_REQUESTS` 的 `model` 字段（`:104`/`:312-314`）与最近请求表模型列（`renderRecent`）。

## Acceptance Criteria
- `grep -n "model-chips\|renderModelChips\|by_model\|chip-model"` 在 `ctyun-stream-fix-proxy.py` 中零命中（`daily_by_model` 命中除外）。
- `stats_snapshot()` 返回 dict 无 `by_model` 键（决策已写死：删除而非保留）；`daily_by_model` 键仍在。
- dashboard 页面无"按模型"独立卡片；请求流经代理后按天×模型副表正常出数（行为回归）。
- `python3 -m unittest ctyun-stream-fix-proxy.test -v`（或项目既有测试命令）全绿，stderr 无意外错误。
- 无未使用变量/import/死 CSS；footer 文案与实际口径一致。

## Risks
- **`/api/stats` 外部消费者兼容**：若有用户自建脚本轮询 `/api/stats` 读 `by_model`，删除该键会破坏其解析。代码库内唯一消费者是 dashboard（`:590`），无证据存在外部消费者；若用户后续反馈，可在 `/api/stats` 层加 `"by_model": {}` 兼容壳（届时另开 episode）。风险接受。
- **footer 文案遗漏**：`:788-789` 同时描述"按模型"与"按天×模型"，只删卡片不改文案会造成口径说明与页面不符；已列为改动点 8。
- **`_record_request`/`_record_empty_retry` 结构性误删**：两函数中 by_model 与 daily_by_model 写入块交错（288-302、334-345），误删外层 `if model:` 守卫或 daily_by_model 块会连带打掉副表数据；锚点已写明保留范围。
- **测试连带失败**：`test_by_model_accumulation_and_cap`/集成测试/`test_by_model_retries_dimension` 直接引用 `by_model`，不改测试则 VERIFY 必红；已列为测试文件 3 处改动点。

## Exclusions
- 顶部汇总卡（requests_total/filtered_total/errors_total/active/毒行率）不动。
- 按天主表（`renderDaily` :915-938，`#daily-body`）不动。
- 按天×模型副表（`renderDailyByModel` :939 起，`#daily-model-body`，后端 daily_by_model 全链路）不动。
- 请求节奏 sparkline、剥行流带、最近请求表（含模型列）不动。
- 持久化文件格式（`save_stats_counters` :184-200 从未持久化 by_model）不变更，老持久化文件无需迁移。
- 不加 `/api/stats` 兼容壳、不改部署脚本。
