# ctyun-proxy 按天统计 spec（simplified mode）

背景：v1.1（ddfa9f5）的计数持久化只有生命周期累计值，无法回看"昨天/前天错误多少、请求多少"；上游 5xx 透传不计入 errors_total（其口径=代理自身错误），用户视角的"错误情况"缺一块。用户已确认：不记 token，只按天记录请求与错误。本 episode 全程主代理执行（用户显式指令，不派子代理）。

## Goal

① daily 分桶持久化：`STATS` 与持久化文件新增 `daily`——按本地日期 `YYYY-MM-DD` 分桶，每桶 `{"requests","filtered","errors_proxy","errors_upstream"}` 四计数，SIGTERM/60s 脏刷同现有机制落盘，重启续算；② 错误拆双口径：`errors_proxy`（代理自身，现 `error=True` 路径=上游连接失败 502）与 `errors_upstream`（上游响应 `status >= 500` 透传，4xx 不算）；**`errors_total` 语义不变**（= 代理自身错误，v1.1 中断静默化"499 不计"语义保持）；③ dashboard 新增「按天统计」卡（表格：日期|请求|剥行|代理错误|上游5xx，最近 14 天倒序）；④ 保留滚动 90 天（落盘时 prune）。

## Files to Change

### 1. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`（现状 857 行）

- **常量（:44 `BY_MODEL_CAP` 旁）**：`DAILY_RETENTION_DAYS = 90`。
- **`today_key() -> str`（新增纯函数，`extract_model` :98 旁）**：`return time.strftime("%Y-%m-%d", time.localtime())`。模块级函数以便测试 patch 模拟跨天。
- **`STATS`（:52-53）**：初始 dict 加 `"daily": {}`。
- **`_record_request`（:180-198）**：在 STATS_LOCK 内加 daily 分桶——`bucket = STATS["daily"].setdefault(today_key(), {"requests": 0, "filtered": 0, "errors_proxy": 0, "errors_upstream": 0})`；`bucket["requests"] += 1`；`bucket["filtered"] += filtered`；`if error: bucket["errors_proxy"] += 1`；`if status >= 500: bucket["errors_upstream"] += 1`。499 aborted（status<500 且 error=False）两边都不进——与中断静默化语义一致。既有 errors_total/filtered_total/by_model 逻辑零改动。
- **`save_stats_counters`（:112-124）**：counters 快照加 `"daily": {k: dict(v) for ...}` 深拷贝；写文件前 `_prune_daily(daily)`——保留按 key 降序前 `DAILY_RETENTION_DAYS` 天（ISO 日期字符串排序即时间序）。新增 `_prune_daily(daily: dict) -> dict` 纯函数（原地更新或返回新 dict 均可，签名测试锁定）。
- **`load_stats_counters`（:127-141）**：返回 dict 加 `"daily"` 键——文件缺/损坏/stats 键缺 → `{"daily": {}}` + 三零值（现状路径不动）；`stats["daily"]` 非 dict → `{}`；桶非 dict → 跳过；桶字段仅收 `int >= 0`，其余按 0（手工改坏文件不崩）。
- **`main()`（:803-808）**：`STATS["daily"] = counters["daily"]`（随三计数一起覆盖赋值）。
- **`stats_snapshot`（:209-218）**：snap 加 `"daily": {k: dict(v) for ...}`（深拷贝，JSON 可序列化）。
- **`DASHBOARD_SRC`（:458-785）**：「按模型」卡（:556-559）后新增 `<section class="card">` 按天统计卡：`<table>` 表头 日期|请求|剥行|代理错误|上游5xx，`<tbody id="daily-body">`；JS 加 `renderDaily(daily)`（key 降序取前 14，动态数据 textContent，空态「暂无按天统计」），`poll()` 成功回调（:736-740）加 `renderDaily(snap.daily || {})`；footer（:574-577）文案改「累计与按天计数跨重启保留（每 60s 落盘…）」。

### 2. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`（现状 571 行）

- **单元（ProxyDashboardUnitTest）**：① `_record_request` daily 累进——200 请求 → `daily[today]["requests"]>=1`；filtered 进当天桶；`_record_request(..., 500, error=False)` → `errors_upstream+1` 且 `errors_total` 不变；`_record_request(..., 502, error=True)` → `errors_proxy+1`；499（error=False）→ 两错误计数均不增；② patch `mod.today_key`（`unittest.mock.patch` 或直接赋值恢复）模拟次日 → 两个独立桶各自累计；③ save/load 往返含 daily + legacy 文件（无 daily 键）→ `daily == {}` + 损坏 daily（非 dict）→ `{}` + prune：预置 95 个历史桶（`2026-01-01` 起递增）→ `save_stats_counters` 后文件内桶数 `<= 90` 且最新桶保留；④ `stats_snapshot()` 含 `daily` 且为拷贝（改 snap 不污染 STATS）。
- **集成（AdminIntegrationTest）**：⑤ `post_sse` 后 `/api/stats` `daily` 当天桶 `requests >= 1`，SIGTERM 落盘文件含同值；⑥ 假上游 handler 加 `fail_500` 类属性（True → do_POST 回 `send_response(500)` + JSON body）→ 经代理请求得 500 → snap `daily[...]["errors_upstream"] >= 1` 且 `errors_total == 0`；⑦ `seed_persist` 带 `stats.daily`（如 `{"2026-01-01": {"requests": 5, "filtered": 1, "errors_proxy": 0, "errors_upstream": 2}}`）重启 → `/api/stats` daily 含该桶；⑧ dashboard HTML 断言「按天统计」「上游5xx」`<th>`、`daily-body`。
- **既有 20 测试零改动全绿**（剥行回归/权限矩阵/计数续算不变）。

## Acceptance Criteria

- ① `/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` 全绿（原 20 + 新增 ≥8），贴 stdout + exit 0。
- ② `/usr/bin/python3 -m py_compile` 两文件 exit 0。
- ③ 部署实机：`launchctl kickstart -k` 后 `/api/stats` 含 `daily` 且当天桶计数延续重启前（现网 203 请求基础上 +1 验证）；发一个经代理请求后 `daily` 当天 `requests` 增 1。
- ④ 归档 `~/Documents/project/ctyun-stream-fix-proxy/` 与 `~/.local/bin/` 两文件 cmp byte-identical。

## Risks

- 跨天竞态：桶 key 在 `_record_request` 内（STATS_LOCK 下）取 `today_key()`，午夜前后的两个请求进各自日期桶——语义正确，无需额外同步。
- prune 仅在落盘时执行：两次落盘之间内存最多 91 桶，文件体积影响可忽略（60s 脏刷兜底 + SIGTERM 必刷）。
- `/api/stats` 2s 轮询带全量 daily（≤90 桶 × ~80B ≈ 7KB）：流量可忽略。
- errors_total（跨重启累计）与 daily.errors_proxy（按天）加总口径不同：dashboard 分开展示，footer 注明口径；不合并避免歧义。
- load 对手工改坏的 daily 容错归零/跳桶，不因文件损坏崩溃。

## Exclusions

- 不做 daily.by_model（按天×模型交叉维度）——累计层 by_model 已覆盖模型维度，用户确认"只记录错误和请求次数"。
- 不记 token（已实测 glm-5.3 流式可取 / v4-pro 流式上游不给，用户明确放弃）。
- 不做 recent 明细持久化、不做按天图表化视图（表格即够）。
- 不动 launchd plist、7920 剥行正则、转发头、admin 鉴权模型。

## R31 Evidence

[R31-S1] 问题现场命中（2026-09-05 采集）：① `/api/stats` 无任何时间维度字段；② 持久化文件只有三计数，无按天数据——重启不丢但永远只有累计值；③ 上游 5xx 不入错误计数：本会话实测 glm-5.3 非流式连续 3 次撞网关 500 后 `errors_total` 仍为 0（5xx 走 `_proxy_relay` :305/:310 正常记录路径，`error=False`）。
```
$ curl -s http://127.0.0.1:7921/api/stats | jq '{has_daily: has("daily"), errors_total}'
{
  "has_daily": false,
  "errors_total": 0
}
$ jq '.stats' ~/.local/etc/ctyun-stream-fix-proxy.json
{
  "requests_total": 203,
  "filtered_total": 0,
  "errors_total": 0
}
```
[R31-S2] 根因：`STATS`（:52）与 `_record_request`（:180）只维护生命周期累计计数，无时间键分桶；`save_stats_counters`（:112）只序列化三计数；上游 5xx 在 `_record_request` 中仅进 `errors_total` 之外的默认路径（`error` 形参只在 :297 上游连接失败 502 时为 True），错误双口径在数据层未拆分。修复面全部在代理进程内。
```
$ /usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -3
Ran 20 tests in 9.011s

OK
$ /usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py && echo compile-ok
compile-ok
```
