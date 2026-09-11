# 时间维度统计（近3天/近7天/本月/上月）设计

## Goal
把 dashboard 顶部汇总卡从"自首次运行累计+瞬时值"混合口径改为按四个时间维度（近3天/近7天/本月/上月）聚合，按天主表与按天×模型副表同步跟随所选维度，复用现有 daily 桶不新增持久化 schema。

## 设计决策（定稿，2-3 方案带推荐）

**D1 交互形态** — 方案A：顶卡上方 4 个 chip/tab（近3天/近7天/本月/上月），单选切换全部相关区块；方案B：四组并排小卡（4×5=20 个数字同屏）；方案C：下拉框。**推荐A**：dashboard 单页已密（顶卡+spark+3 表+流带），方案B 移动端溢出且不可读；需求要求主表/副表"也按四个时间段"→ 单一共享选中态天然满足；下拉隐藏维度、多一次点击。默认选中 `7d`，刷新不记忆（无 localStorage）。

**D2 口径定义（写死）** — ①近3天/近7天：滑动窗口**含今日**，本地时区（与 `today_key()` 一致）：近3天=[today-2, today]，近7天=[today-6, today]；今日为部分数据，快照值随时间增长。②本月/上月：自然月，本月=[当月1日, today]，上月=上个自然月整月（含 28/29/30/31 与跨年）。③**活跃连接不参与时间维度**：恒为实时 gauge（`STATS["active"]`，:392 增/:406 减），标签改"活跃连接·实时"避免误读。④毒行率按选中维度重算：`filtered_sum / requests_sum`，requests=0 显示"—"。⑤顶部错误数口径变更（写死）：时间维度内 `errors_proxy + errors_upstream` 合计（旧口径"仅代理错误累计"废弃）；footer 与 README 同步改写。

**D3 数据来源与压缩/颗粒度** — 方案A：复用现有 daily 五字段桶（`_DAILY_FIELDS` :224），后端聚合；方案B：新增月桶预聚合/压缩旧桶；方案C：纯前端算。**推荐A**。90 天 retention 充分论证：最远回溯需求 = 上月最长 31 天 + 当月最多已过 31 天 = **62 天 < 90**（余量 28 天可容进程停机 4 周不丢上月数据）。体积边界：单桶 JSON ≈70B，90 桶 ≈6-7KB，内存 <50KB，结构有界（`_prune_daily` :165-171 每次 save 兜底）；月桶引入第二套聚合路径+月 prune+迁移，此量级零收益。**不做压缩、不改 retention、不加月桶**。

**D4 /api/stats 兼容** — 方案A：后端聚合，快照新增**加性字段** `range_stats`（每维度 6 字段：requests/filtered/errors_proxy/errors_upstream/retries/days）+ `range_bounds`（每维度 [start,end] ISO 闭区间，前端零日历运算）；方案B：前端按 snap.daily 现场算；方案C：`?range=` 查询参数。**推荐A**：原字段逐字节不变 → README :59 已记载的外部消费者零破坏；口径/日历边界单一真源在后端，可进现有 unittest（JS 无测试基建）；payload 仅增 ~400B。方案C 破坏幂等轮询形状。

**D5 纳入/排除** — 按天主表**纳入**（需求点名）：`renderDaily` 用下发 bounds 过滤日期 key，废除 `slice(0,14)`（:888），上月最多 31 行；副表 daily_by_model **纳入**（同一 bounds 过滤，改 ~5 行，避免同屏两表口径分裂；标题"重启清零"既有语义不变）；最近请求/剥行流带/sparkline **不纳入**（内存瞬时数据，无时间维度语义）。

## Files to Change
- `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:12-25` import 区 — 加 `import datetime`（stdlib，docstring :6 "纯 stdlib"仍成立）。
- `ctyun-stream-fix-proxy.py:45` `DAILY_RETENTION_DAYS = 90` 旁 — 新增 `RANGE_KEYS = ("3d", "7d", "mtd", "last_month")`。
- `ctyun-stream-fix-proxy.py:160` `today_key()` 之后 — 新增三个纯函数（实现锚点，implementer 据此写死）：
  `def range_bounds(today: str, range_key: str) -> tuple:` 返回 `(start_key, end_key)` 闭区间 ISO 日期串；非法 range_key → `ValueError`。日历运算：`d = datetime.date.fromisoformat(today)`；本月起点 `d.replace(day=1)`；上月 = 起点 `- datetime.timedelta(days=1)` 再 `.replace(day=1)`，上月末 = 起点-1 天；ISO 串字典序=时间序（`_prune_daily` :166 已依赖此性质）。
  `def aggregate_daily_range(daily: dict, start: str, end: str) -> dict:` 窗口内桶逐字段求和（`k` 满足 `start <= k <= end`；缺字段按 0），返回 6 键 dict（五字段 + `days`=命中天数）。
  `def range_stats(daily: dict, today: str = None) -> dict:` 返回 `{"stats": {range_key: aggregate}, "bounds": {range_key: [start, end]}}`，覆盖 `RANGE_KEYS` 全部四键；`today` 缺省 `today_key()`（测试可注入固定 today）。
- `ctyun-stream-fix-proxy.py:349-361` `stats_snapshot()` — 从已复制的 `snap["daily"]` 出发（STATS_LOCK 外，避免拉长持锁），`snap["range_stats"] = plan["stats"]; snap["range_bounds"] = plan["bounds"]`。原字段零改动。
- `ctyun-stream-fix-proxy.py:731-737` 顶卡 HTML — `.stats-row` 上方插入 4 个 `<button class="range-tab" data-range="3d|7d|mtd|last_month">`（近3天/近7天/本月/上月，active 高亮）；`#st-active` 卡标签 `:735` 改"活跃连接·实时"。
- `ctyun-stream-fix-proxy.py:819-831` `renderStats(snap)` — 五值改读 `snap.range_stats[selectedRange]`（active 仍读 `snap.active`），毒行率重算同 :822-823 模式；`selectedRange` 默认 `"7d"`。
- `ctyun-stream-fix-proxy.py:885-906` `renderDaily(daily)` — `:888` 的 `slice(0, 14)` 废除，改按 `lastSnap.range_bounds[selectedRange]` 字符串比较过滤后倒序输出；行数上限=窗口定义自然上界。
- `ctyun-stream-fix-proxy.py:909` `renderDailyByModel(dbm)` — 同 bounds 过滤日期 key。
- `ctyun-stream-fix-proxy.py:743` 按天主表标题、`:752` 副表标题 — 文案随所选维度更新（如"按天统计（上月，新在上）"）。
- `ctyun-stream-fix-proxy.py:987-1004` `poll()` then 块 — 缓存 `lastSnap = snap` 供 tab 点击零请求重渲染；新增 `renderRangeTabs()` 与 click 委托（设 selectedRange → 用 lastSnap 调 renderStats/renderDaily/renderDailyByModel + 标题更新）。
- `ctyun-stream-fix-proxy.py:774-780` footer 口径文案 — 改写：顶部卡按所选时间段聚合（近3/7天含今日；本月/上月自然月，本地时区）；错误数=代理错误+上游5xx 合计；活跃连接恒实时；90 天日桶覆盖最远 62 天回溯，每 60s 落盘不变。
- `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:362` `ProxyDashboardUnitTest` 内新增（模式参照 `:521` patch today、`:498` 桶构造、`:613` prune）：`test_range_bounds_calendar_edges`（today=2026-03-01 月末切片：mtd 仅 03-01、last_month 含 02-01..02-28；today=2026-01-01 跨年：上月=2025-12 全月；闰日 2024-02-29；7d 含今日共 7 天）；`test_aggregate_daily_range_sums_and_days`（窗口内外桶混合、缺字段桶、空窗口全 0）；`test_range_stats_all_four_keys`（恰 4 键）；`test_stats_snapshot_includes_range_stats`（扩展 `:481`：当日桶 requests == range_stats["7d"].requests）。
- `/Users/peter/Documents/project/ctyun-stream-fix-proxy/README.md:16` 实时统计行、`:59` /api/stats 行（补 `range_stats`/`range_bounds` 加性字段）、`:73-76` 口径表（顶部卡行改时间维度口径 + 62<90 覆盖论证一行）、`:28` 90 天 prune 描述保持。

## Acceptance Criteria
- `python3 ctyun-stream-fix-proxy.test.py -v` exit 0，含上述 4 个新单测全绿；`:447`/`:481`/`:613` 既有测试不回归（持久化 schema 零改动证据）。
- `curl http://127.0.0.1:7921/api/stats` 返回含 `range_stats`（恰 4 键 × 6 字段）与 `range_bounds`；与旧响应 diff 仅新增两键，原字段值与键集不变。
- 注入 today=2026-03-01 的单测断言：mtd 仅含 2026-03-01，last_month 覆盖 2026-02-01..2026-02-28，7d 含今日共 7 天（边界防回归）。
- dashboard 手动验证：默认选中 近7天；切"上月"后按天主表仅显示上月日期行且 ≤31 行；副表同步过滤；活跃连接卡在任意 tab 下仍随请求实时增减；空窗口毒行率显示"—"；tab 切换不发起新 HTTP 请求（devtools network 确认）。
- footer 与 README 口径描述与实现一致（错误数=两口径合计）。

## Risks
- 口径语义变更：顶部错误数从"仅代理错误累计"改为"维度内 errors_proxy+errors_upstream 合计"，footer :778 旧句"与顶部错误数同口径"必须同 commit 改写，否则口径漂移误导用户。
- 前后端窗口逻辑漂移：已由 D4 方案A 消解——bounds 后端下发，前端仅字符串比较；implementer 不得在 JS 内另写日历运算。
- `daily_by_model` 内存不 prune（save :189 仅 prune daily，+1 天 key/天，重启清）——存量隐患，非本 episode；建议主代理记 `docs/superpowers/followups.md`。
- stats_snapshot 每 2s 多算 4×≤31 桶求和（<1ms）+ payload ~400B：量级无害；必须在 STATS_LOCK 外基于副本计算。
- 老持久化文件兼容：schema 不加新键，`load_stats_counters` :214 / `load_daily_buckets` :227 不动；手工改坏文件容错路径不受影响。

## Exclusions
- 月桶预聚合、旧日桶压缩、`DAILY_RETENTION_DAYS` 调整（62<90 已论证）。
- 第五个"累计（自首次运行）"tab：需求仅列四维度；累计值仍在 /api/stats 原字段，外部消费者不丢数据。
- 最近请求表、剥行流带、sparkline（内存瞬时数据，无时间维度语义）。
- daily_by_model 的 prune 与持久化（followup）。
- localStorage 记忆所选维度（刷新回默认 近7天）；时区配置化（固定本地时区，与 today_key 一致）。
