# 按天×模型表与按天统计对齐（14 天 / 新在上 / 请求·剥行·代理错误·上游5xx·重试）

## Goal
让 dashboard「按天 × 模型」表与「按天统计」主表口径一致：同为最近 14 天、新在上、数值列对齐为 请求/剥行/代理错误/上游5xx/重试（推荐方案 A，独立双表对齐列；备选 B 合并单表+胶囊开关因改动面更大、失去双表同屏对比而落选）。

## 现状与关键事实
- `daily_by_model` 为纯内存结构：`save_stats_counters`（py:184-200，:189 仅拷贝 `daily`）与 `load_daily_buckets`（py:227-242）均不触及它；footer :788 明示"重启清零"→ **加字段无持久化迁移**。
- 错误归因只写 `daily` 桶（`_record_request` py:308-311：`if error→errors_proxy; elif status>=500→errors_upstream`）；dm entry 仅 `{"requests","filtered","retries"}`（py:298-299 与 py:342-343 两处创建，docstring py:327-329 约定形状必须同步）。
- 14 天截断/新在上两表前端均已具备：`renderDaily` py:918、`renderDailyByModel` py:942 均 `sort().reverse().slice(0, 14)`。
- `stats_snapshot` py:358-360 双层深拷贝自动透出新字段，零改动；`/api/stats`（py:589-590）形状向后兼容（纯新增键）。

## 方案对比
- **A（推荐）双表对齐列**：后端 dm entry 加 `errors_proxy/errors_upstream`（2 处），前端表头 5→7 列。改动 ~5 个锚点，无 API/持久化破坏。
- **B 合并单表+胶囊开关**：仍需同样的后端加字段（否则按模型视图列不齐），另加 toggle 状态/重渲染/双数据源切换/动态 colspan，测试面翻倍，且无法同屏对比两表。仅当用户更在意少一个 card 时选 B。
- **C = A + dm 持久化 90 天**：改 save/load 嵌套格式，超范围 → 排除。

## Files to Change
1. `ctyun-stream-fix-proxy.py:281-315 _record_request()` — dm 块（:296-302）entry 初始形状改 `{"requests":0,"filtered":0,"errors_proxy":0,"errors_upstream":0,"retries":0}`；在 `entry_dm["filtered"] += filtered` 后按 daily 桶同口径累加：`if error: entry_dm["errors_proxy"] += 1; elif status >= 500: entry_dm["errors_upstream"] += 1`。边界：499 aborted 调用（py:407-408）`model=None` 不进 dm；502 两条 `error=True` 带 model 路径（py:434-436、:455-456）归入该模型 errors_proxy；`BY_MODEL_CAP` 每日独立 cap（:298）不变。
2. `ctyun-stream-fix-proxy.py:326-350 _record_empty_retry()` — dm entry 创建（:342-343）同步 5 字段形状（docstring :327-329 同步约定保留），`entry_dm["retries"] += 1` 不变；error/status 参数不存在，不新增归因。
3. `ctyun-stream-fix-proxy.py:763-770`（按天×模型 card HTML）— thead :767 改 7 列 `日期/模型/请求/剥行/代理错误/上游5xx/重试`（顺序与主表 :758 数值列一致）；tbody :768 空态 colspan 5→7；card-title :764 保留"（内存累计，重启清零）"。footer :789 主表≥副表合计文案仍成立，不动。
4. `ctyun-stream-fix-proxy.py:939-968 renderDailyByModel(dbm)` — 循环体 :962-964 后按主表顺序插 2 个 cell：`el("td","num",String(ent.errors_proxy||0))`、`el("td","num",String(ent.errors_upstream||0))`（`|| 0` 防御旧形状/缺键）；行高亮 :959 由 `(ent.retries||0)>0` 改为与 `renderDaily` :929 一致的 `((ent.errors_proxy||0)+(ent.errors_upstream||0))>0`；空态 colspan 5→7（:946）。排序（天数倒序 slice 14、模型按 requests 降序 :954-956）不变。
5. `ctyun-stream-fix-proxy.test.py:538-557 test_daily_by_model_matrix_fallback` — 追加断言：`_record_request("POST","/dm",502,1.0,0,error=True,model="m-a")` 后 `STATS["daily_by_model"][today]["m-a"]["errors_proxy"]==1`；`_record_request("POST","/dm",500,1.0,0,model="m-a")` 后 `errors_upstream==1`；`_record_request(...,499,...,model=None)` 后 dm 键集不变（沿用 :543-545 模式）；`set(dm_entry)=={"requests","filtered","errors_proxy","errors_upstream","retries"}`。
6. `ctyun-stream-fix-proxy.test.py:820-824`（集成 `test_...`）— 在现有 `snap["daily_by_model"][today][...]` 断言旁追加 `errors_proxy/errors_upstream` 键存在且为 int≥0。

## Acceptance Criteria
- 单测（`python3 ctyun-stream-fix-proxy.test.py` 全绿）：dm entry 恒 5 字段；`error=True`→errors_proxy、`status≥500` 非 error→errors_upstream、499 两边均不计、重试仍归 retries；cap 32 与深拷贝（test.py:482-484、:611-620）回归通过。
- 集成：`/api/stats` 的 `daily_by_model[today][model]` 含 5 键；旧客户端/jq 只读 3 键不受影响（纯新增）。
- 前端（静态 + 实机 curl `/` HTML）：按天×模型表 7 列顺序与主表数值列一致；≤14 天、新在上；含错误的行琥珀高亮（`tr.hit`，CSS py:711 复用）；空态 colspan=7；两表同屏可直接逐列对比。
- footer 文案与实际口径一致（无过时描述）。

## Risks
- dm entry 形状双写点（py:298-299 / py:342-343）不同步会 `KeyError`——docstring 约定已有，测试断言 5 键集兜底。
- 部署后、重启前的过渡态不存在（dm 重启清零），但热更新（若进程未重启）内存中旧 3 字段 entry 会被前端 `|| 0` 兜住，仅显示 0，可容忍。
- 高亮口径从"retries>0"改为"errors>0"是行为变更：空流重试活跃但无错误的模型行不再高亮——与主表"保持一致"诉求优先，接受。
- `daily_by_model` 内存按天累积无 prune（90 天 prune 仅作用 `daily`，py:190）：进程长期不重启缓慢增长，本任务不处理（见 Exclusions）。

## Exclusions
- 不做方案 B（合并单表 + 胶囊开关）；不新增任何 toggle/交互状态。
- 不持久化 `daily_by_model`（方案 C）；不改 `save_stats_counters`/`load_daily_buckets`/`DAILY_RETENTION_DAYS`/`_prune_daily`。
- 不给 dm entry 增加内存 prune；不改 `BY_MODEL_CAP`、`/api/stats` 既有键形状、`stats_snapshot` 深拷贝实现。
- 不动代理转发逻辑、499/502 记账口径、其余 dashboard 卡片（按模型 chips、最近请求、sparkline）。
