# ctyun-stream-fix-proxy observability v2 设计（速度/Token/归因/探测 + dashboard 重构）

## Goal

为 7920 转发代理与 7921 admin 补齐四类观测能力（A 速度/延迟、B Token/内容、C 归因/可靠性、D 主动探测）并重构 dashboard 前端，使 7921 单页能回答"现在哪个模型快/慢、错率多少、token 用量、上游是否健康、某次请求落到哪个上游"。零外部依赖、热路径打点 < 微秒级、向后兼容旧持久化 JSON、单文件部署保持。

## 实施分期（实施期强制拆分，本 spec 为总设计）

scope 跨 ≥4 子系统，按 AGENTS.md §0 拆为独立 episode 串行执行；每期独立 PLAN/IMPLEMENT/VERIFY/REVIEW/DELIVER 闭环，**本 spec 为唯一权威源**：

- **P1（地基）**：request id / upstream host 快照 / 阶段计时字段 / lock 现状审计与文档化 / `_record_request` 签名扩展。只动 `ctyun-stream-fix-proxy.py` 的 :858-1026 + `_record_request:680` + `RECENT_REQUESTS` schema。为后续期次铺路。
- **P2（A 速度）**：TTFB + 阶段拆分 + per-model fixed-bucket histogram（内存态）+ bytes/s + chunk/s + stall 检测。`/api/stats` 新增 `perf` 节。不动持久化。
- **P3（B Token/用量）**：usage 帧抽取（prompt/completion tokens）+ daily_by_model v2 schema（token/字节/SSE 计数字段）+ 向后兼容加载 + persist。streaming/non-streaming 占比。
- **P4（C 归因 + D 探测）**：三态 outcome 细分落 daily 字段、probe 线程（间隔/超时/去抖）、`/api/health` 与 EVENTS 告警。
- **P5（dashboard 重构）**：拆分 `_DASHBOARD_SRC` 为逻辑区块字符串常量 + 新卡片渲染（模型速度表 / token 表 / 健康卡 / 三态可用率）。零外部资源、保持 textContent 防 XSS。

各期 acceptance criteria 在对应期的 PLAN.md 内具体化；本 spec 的 Acceptance Criteria 是 P1-P5 全部完成后的终态口径。

## Supersede 说明（B 类 token 记录）

2026-09-05 旧 spec 决策过"不记 token 用量"。本次用户在 2026-09-30 确认 scope 时**明确推翻该决策**：usage 帧数值（prompt_tokens/completion_tokens/total_tokens）将按天×模型持久化，供容量与成本观察使用。该推翻不影响旧决策中的"usage 帧只作空流判定布尔信号"代码路径——那条路径保留，本设计在其上**叠加**数值抽取，不替换。

## Files to Change

> 全部改动集中于 `ctyun-stream-fix-proxy.py`（生产）+ `ctyun-stream-fix-proxy.test.py`（测试）+ `README.md`（文档同步）。锚点以 P1-P5 分期归属列出，每期 PLAN.md 自行展开。

### P1 地基（request id / host 快照 / 阶段计时 / lock 文档化）

- `ctyun-stream-fix-proxy.py:26` import 区 — 追加 `import uuid`（stdlib，request id 生成）。
- `ctyun-stream-fix-proxy.py:53-60` 常量区 — 新增 `HIST_BUCKETS_MS = (100, 250, 500, 1000, 2000, 5000, 10000, 30000)`（8 桶右开边界，+inf 隐式第 9 桶；对数等比 ≈ ×2.5，覆盖 100ms~30s+）；`PROBE_INTERVAL_S_DEFAULT = 30`、`PROBE_TIMEOUT_S = 5`、`PROBE_MIN_INTERVAL_S = 10`、`PROBE_FAILURE_THRESHOLD = 3`（连续失败次数触发 EVENTS）、`PROBE_ALERT_DEBOUNCE_S = 300`（同类告警最小间隔）；`DAILY_V2_FIELDS = _DAILY_FIELDS + ("tokens_prompt", "tokens_completion", "bytes_out", "stream_requests", "ttfb_sum_ms", "ttfb_count", "outcome_ok", "outcome_degraded", "outcome_failed")`（P3 起用，P1 先声明）。
- `ctyun-stream-fix-proxy.py:312-333` 模块级状态区 — 新增 `REQUEST_SEQ = 0`（STATS_LOCK 内递增的 request id 序列）与 `RECENT_REQUESTS` 注释更新（条目 schema 增 `rid`/`upstream_host`/`ttfb_ms`/`stream`/`tokens`/`bytes_out`/`outcome` 字段，见下）。
- `ctyun-stream-fix-proxy.py:680 _record_request(method, path, status, dur_ms, filtered, model=None, error=False)` — 签名扩为 `_record_request(method, path, status, dur_ms, filtered, model=None, error=False, rid=None, upstream_host=None, ttfb_ms=None, stream=None, tokens=None, bytes_out=0, outcome=None)`（全部默认 None/0 保持向后兼容；P1 仅写 rid/host/ttfb_ms/stream/outcome，P3 写 tokens/bytes_out）。`RECENT_REQUESTS.append` 处（:718-720）条目字典新增上述键；daily 桶写入在 P3 扩展，P1 不动。
- `ctyun-stream-fix-proxy.py:858 _proxy(self)` — 在 `with STATS_LOCK: STATS["requests_total"] += 1` 同块内生成 `rid = "r-%d" % (STATS["requests_total"])`（复用计数器即单调；前缀 `r-` 区分外部 id）；保存到 `self._req_id = rid` 供 `_log` 与 `_record_request` 引用；同一锁块内取 `upstream_host = urllib.parse.urlparse(UPSTREAM_BASE).netloc`（一次解析，转发路径全程复用；UPSTREAM_BASE 读侧无锁，但 `_CFG_LOCK` 只保护写——读线程看到任一完整字符串值即可，Python 引用赋值原子）。
- `ctyun-stream-fix-proxy.py:1028 _open_upstream(self, method, path, body, fwd_headers)` — 无签名变化；调用方 `_proxy_relay` 在其前后用 `time.monotonic()` 记 `t_conn_start`/`t_headers_done`（连接+响应头到达）。**计时纪律**：热路径用 `time.monotonic()` 不用 `time.time()`（防 NTP 跳变污染直方图）。
- `ctyun-stream-fix-proxy.py:1171 _log(self, started, status, result, filtered, model=None, retried=0, retry_reason="", exc=None)` — REQ 行追加 `rid=%s host=%s ttfb=%sms stream=%s outcome=%s` 五个字段（值缺失打 `-`，与现有 `exc=-` 风格一致）；保留既有字段位置不变，新字段追加在行尾 `ts=` 之前。
- `ctyun-stream-fix-proxy.py:1046 _send_sse_headers(self, resp)` — 在 `self.send_header("Connection", "close")` 前追加 `self.send_header("X-Request-Id", self._req_id)`（响应头回传，供客户端把应用层日志关联到代理 REQ 行）。
- `ctyun-stream-fix-proxy.py:1143 _relay_buffered` / `:1161 _reply_502` — 非流式响应与 502 路径同样写 `X-Request-Id` 头（各一行）。
- `ctyun-stream-fix-proxy.py:312-313` `_CFG_LOCK`/`STATS_LOCK` 注释 — 文档化锁策略现状：`STATS_LOCK` 保护 STATS/RECENT_REQUESTS/EVENTS/POISON_PREVIEWS/daily 桶；`_CFG_LOCK` 保护 UPSTREAM_BASE/CAPTURE_ERRORS；`ERROR_LOCK` 保护 ERROR_EVENTS/_ERROR_EVENT_SEQ；`LOG_LOCK` 保护 LOG_RING/_LOG_SEQ。**新增 `PROBE_LOCK`（P4 用）独立于 STATS_LOCK**——探测线程与请求线程共享 STATS 时只经 `STATS_LOCK` 短持锁更新计数器，绝不持锁做网络 IO。

### P2 速度（histogram 桶 + 阶段拆分 + 吞吐 + stall）

- `ctyun-stream-fix-proxy.py:314 STATS` — 新增内存态（**不持久化**，重启清零）键：
  - `"ttfb_hist": {"<model>": [0]*9}`（9 桶计数；模型 cap BY_MODEL_CAP 复用）、
  - `"phase_ms": {"connect": [0]*9, "headers": [0]*9, "body": [0]*9}`（全局三阶段，不按模型——维度太多浪费内存）、
  - `"rates": {"bytes_out_total": 0, "chunks_total": 0, "tokens_total": 0, "window_start": <monotonic>, "window_bytes": 0, "window_chunks": 0, "window_tokens": 0}`（60s 滑动窗算 bytes/s、chunk/s、tok/s）、
  - `"stalls_total": 0`（相邻 SSE record 间隔 > 5s 的次数，仅流式）。
- `ctyun-stream-fix-proxy.py:1056 _relay_sse(self, resp, final)` — 在打点约束下插入：
  - 进入时记 `self._t_first_byte_mark = None`（仍未收到任何字节）；record 终结块（:1081）首个非毒 record flush 时若 `self._t_first_byte_mark is None` 赋 `time.monotonic()`，记录 `ttfb_ms = (t_first_byte - t_conn_start) * 1000` 待 `_record_request` 用。
  - 相邻 record 间隔：上次 record 终结的 monotonic 与本次相减，>5s 且 `priming=False` → `STATS["stalls_total"] += 1`（持 STATS_LOCK 一次自增）。
  - 每 record 字节数累加到 `self._relay_bytes`，chunks 计数 +1。
  - **热路径性能**：每 record 仅 `time.monotonic()` 一次 + 整数加法 + 桶下标计算（bisect 右开 8 桶，≤3 次比较），无锁操作除 stall 自增（<0.1% record 命中）；禁止 per-record 日志/落盘。
- `ctyun-stream-fix-proxy.py:1143 _relay_buffered(self, resp)` — 非流式路径在 `resp.read()` 前后记 monotonic，`bytes_out = len(data)`，`stream=False`；TTFB 此路径= `t_body_done - t_conn_start`。
- `ctyun-stream-fix-proxy.py:879 _proxy_relay` — 汇总 `ttfb_ms`/`stream`/`bytes_out`/`chunks`/`tokens`（P3 填）传给 `_record_request`；histogram 桶更新与 rates window 滚动在 `_record_request` 内持 STATS_LOCK 完成（60s 滚动窗口重置只在新请求进入且 `now - window_start >= 60` 时发生一次）。
- `ctyun-stream-fix-proxy.py:815 stats_snapshot()` — 返回字典新增 `"perf"` 节：`{"ttfb_p50_ms_by_model": {...}, "ttfb_p90_ms_by_model": {...}, "phase_p50_ms": {...}, "bytes_per_s": float, "chunks_per_s": float, "tokens_per_s": float, "stalls_total": int, "stream_share": float}`；P50/P90 由直方图线性插值（stdlib 无 numpy，手写 ≤20 行纯函数 `hist_percentile(buckets, edges, q)`）。

### P3 Token / daily_by_model v2

- `ctyun-stream-fix-proxy.py:139 sse_line_has_usage(line)` — 保留原函数；新增 `sse_line_extract_usage(line) -> tuple | None`：同解析路径但返回 `(prompt_tokens, completion_tokens, total_tokens)` 三元组（int ≥0；缺失/非法 → None）；复用 `json.loads` 一次。`_relay_sse` record 终结块在已判 usage 帧时顺手调一次，零额外 parse。
- `ctyun-stream-fix-proxy.py:558 _DAILY_FIELDS` — 扩为 16 字段元组（见 P1 `DAILY_V2_FIELDS` 常量）；`aggregate_daily_range` / `range_stats` / `_prune_daily` 零改动（按字段名迭代）。
- `ctyun-stream-fix-proxy.py:680 _record_request` 与 `:732 _record_empty_retry` / `:763 _record_eof_without_done` / `:789 _record_header_retry` — 四处 entry 创建字面量从 7 字段升到 16 字段（新字段全初始 0）；increment 点：tokens_prompt/completion 只在 `_record_request` 的 `tokens` 参数非 None 时 +=；bytes_out/stream_requests/outcome_* 同。
- `ctyun-stream-fix-proxy.py:540 load_stats_counters` / `:562 load_daily_buckets` / `:580 load_daily_by_model_buckets` — **向后兼容**：旧格式（7 字段桶）读入时新字段填 0；新格式（16 字段桶）写入后旧版本进程读入只取自己认识的 7 字段，其余忽略（load 循环已按 `_DAILY_FIELDS` 迭代，新代码的 `_DAILY_FIELDS` 含 16 字段 → 旧桶自动补 0；旧代码的 7 字段 `_DAILY_FIELDS` → 新桶自动丢新字段，不崩）。无需版本号字段。
- `ctyun-stream-fix-proxy.py:501 save_stats_counters` — 落盘体积估算：daily 90 天 × 16 int + daily_by_model 90 天 × 32 模型 × 16 int，最坏 ~750KB JSON，仍在可接受范围（当前 ~350KB）；`_prune_daily` 已 cap。`usage` 价目表（可选 cost 估算）默认 off，配置键 `model_pricing` 在 PERSIST 文件顶层，env seam `CTYUN_MODEL_PRICING`（JSON 字符串）；**P3 不实现 cost UI**，仅落 schema。

### P4 三态 outcome + 主动探测

- `ctyun-stream-fix-proxy.py:172 classify_outcome` — 返回的 `_Outcome` 增字段 `tri_state`（`"ok" | "degraded" | "failed"`），由 category 推导：`CLASS_OK → "ok"`、`CLASS_POISON_FIXED/CLASS_CLIENT_ABORT → "degraded"`（重试/剥行/中断都属"对用户仍交付但非完美"`）、其余 → "failed"`。`_record_request` 据 `outcome.tri_state` 增 daily `outcome_ok/degraded/failed` 三桶（P3 字段已就位）。
- `ctyun-stream-fix-proxy.py:1185 AdminHandler` — 新增 `GET /api/health`：返回 `{"upstream": {"host": str, "last_probe_ts": float, "last_probe_ok": bool, "last_probe_latency_ms": float, "consecutive_failures": int, "probe_enabled": bool}}`；新增 `POST /api/probe`（鉴权同 `/api/config`）切 probe on/off（`{"enabled": bool}`），持久化到 persist 文件顶层 `probe_enabled`。
- `ctyun-stream-fix-proxy.py:1900 main()` — 在 `_dirty_flush_loop` 启动后启动 `_probe_loop` daemon 线程：
  - 间隔 `PROBE_INTERVAL_S_DEFAULT = 30`（env seam `CTYUN_PROBE_INTERVAL_S`，最小 10s 防滥用）。
  - 每轮向 `UPSTREAM_BASE` 根 path 发 `HEAD` 请求（无 body，超时 5s），用独立 `http.client.HTTPSConnection`/`HTTPConnection`，与转发线程无共享连接。
  - 失败计数 `consecutive_failures` 达 `PROBE_FAILURE_THRESHOLD=3` 且距上次同类告警 ≥ `PROBE_ALERT_DEBOUNCE_S=300` → 追加 EVENTS `{"kind": "probe_alert", "reason": "consecutive_failures", ...}`；EVENTS kind 集合从 3 扩到 4（probe_alert），`load_stats_events`（:611）白名单相应加一项。
  - 探测线程**失败零副作用**：不写 STATS 请求计数、不触发 `_record_request`、不污染 histogram；只更新独立 `_PROBE_STATE` dict（PROBE_LOCK 保护）。
- 延迟突增告警：probe 最近 20 次延迟的 P90 > 历史 7 天 probe P90 的 2 倍且样本 ≥ 20 → 同样 EVENTS 一条。该阈值常量 `PROBE_LATENCY_SPIKE_FACTOR = 2.0`。

### P5 Dashboard 重构

- `ctyun-stream-fix-proxy.py:1332-1886 _DASHBOARD_SRC` — **决策：保持内嵌字符串方案（方案 a），但按逻辑拆 6 段常量**：
  - `_DASH_HEAD`（doctype/CSS/header）、
  - `_DASH_SECTIONS_STATIC`（上游端点卡 + range tabs + 顶部 stats-row）、
  - `_DASH_SECTIONS_TABLES`（按天/按天×模型/最近请求/剥行流带）、
  - `_DASH_SECTIONS_V2`（**新增**：模型速度对比表 / token 用量按天×模型表 / 上游健康卡 / 延迟分布 SVG / 三态可用率）、
  - `_DASH_JS_CORE`（`$`/`el`/`fmt*`/`setConn`/evt tooltip）、
  - `_DASH_JS_V2`（`renderPerf`/`renderTokens`/`renderHealth`/`renderTriState`/`poll` 装配）。
  - `DASHBOARD_HTML = ("".join([_DASH_HEAD, _DASH_SECTIONS_STATIC, _DASH_SECTIONS_TABLES, _DASH_SECTIONS_V2, _DASH_JS_CORE, _DASH_JS_V2, "</script></body></html>"])).encode("utf-8")`。
  - **拒绝方案 b（拆静态文件由 admin serve）**：破坏"cp 一个 .py + kickstart"的单文件部署特性（README.md:36-41 明示部署简洁性是特色），admin 还要新增静态路径路由与 MIME 表，超出收益。
  - **保持 textContent 防 XSS 模式**：新卡片渲染一律 `el(tag, cls, text)` helper，不引入 `innerHTML`。
- 新卡片数据流：`/api/stats` 的 `perf` 节 + `daily_by_model` v2 字段 + `/api/health` 合并到 `lastSnap`，2s 轮询不变；range tabs 复用现有 4 键切换模型速度/token 表。

### 测试与文档

- `ctyun-stream-fix-proxy.test.py:500 load_proxy_module()` — 不动；新增测试类（每期归属）：
  - **P1**：`RequestIdTest`（REQ 行含 `rid=r-N`、`X-Request-Id` 响应头存在且与 REQ 行 rid 一致、RECENT_REQUESTS 条目含 rid/host 字段）。
  - **P2**：`PerfHistTest`（histogram 9 桶边界；`hist_percentile` 函数矩阵：空桶/单桶满/均匀分布/末尾桶溢出）；`TtfbStreamTest`（fake upstream sleep 100ms 后吐流 → ttfb_ms ∈ [80, 500]；stall 检测：fake upstream 中途 sleep 6s → stalls_total +1，**用 env seam 把 stall 阈值降到 0.5s 加速**）。
  - **P3**：`UsageExtractTest`（usage 帧抽取矩阵：标准/缺 prompt_tokens/缺 completion/extra key/非 dict）；`DailyV2CompatTest`（旧 7 字段 JSON 加载→16 字段内存桶零值补齐；新 16 字段写入→7 字段加载函数仍只返 7 字段不崩）；`TokenPersistTest`（P3 端到端：POST 一次带 usage 帧 SSE → SIGTERM → 重启 → daily_by_model 含 tokens_prompt>0）。
  - **P4**：`ClassifyOutcomeTriStateTest`（CLASS_OK→ok / CLASS_POISON_FIXED→degraded / CLASS_CLIENT_ABORT→degraded / CLASS_UPSTREAM_FAULT→failed / CLASS_BODY_ERROR→failed / CLASS_REQUEST_FAULT→failed）；`ProbeLoopTest`（fake upstream 计数 HEAD 请求；`CTYUN_PROBE_INTERVAL_S=0.5` 加速；连续 3 次 5xx → EVENTS 含 probe_alert；去抖：5 分钟内不重发）；`ProbeNoSideEffectTest`（probe 失败期间 STATS.requests_total 不增）。
  - **P5**：`DashboardV2SkeletonTest`（HTML 含新卡片容器 id：`perf-model-body`/`token-daily-body`/`upstream-health`/`tri-state-card`；JS 函数 `renderPerf`/`renderTokens`/`renderHealth` 出现于 `_DASH_JS_V2`；`textContent` 出现次数 ≥ innerHTML 出现次数；**innerHTML 0 次**——正则 `r"\.innerHTML\s*="+` 在 `_DASHBOARD_SRC` 各段常量中 0 命中）。
- `README.md:62-76 API` 节 — 新增 `/api/health`、`POST /api/probe`、说明 `X-Request-Id` 响应头与 REQ 行 rid 字段；`README.md:86-95 计数口径` 表追加三态可用率/模型速度/token 用量三行；env seam 表加 `CTYUN_PROBE_INTERVAL_S` / `CTYUN_MODEL_PRICING`（P3 占位）。

## Acceptance Criteria

> 终态口径（P1-P5 全部完成后）。分期 PLAN 各自再列当期 criteria。

- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（在 118 基础上 +P1..P5 新增 ≥25 用例；必须用 `/usr/bin/python3`，不得用 Homebrew 3.14——README.md:84）。
- 转发一次标准带 usage 帧 SSE → REQ 行含 `rid=r-N host=<upstream> ttfb=<num>ms stream=1 outcome=ok`，响应头含 `X-Request-Id: r-N`；`/api/stats` `perf` 节非空，`daily_by_model` 当日当模型 entry 含 `tokens_prompt ≥ 1` 与 `outcome_ok = 1`。
- 旧持久化 JSON（2026-09-30 之前的 7 字段版）启动加载不报错；新格式 JSON 被旧版二进制加载（降级演练）不崩（仅丢新字段）。
- probe 线程默认 30s 一次 HEAD 请求；fake upstream 连续 3 次 5xx 后 5 分钟内 EVENTS 仅 1 条 probe_alert（去抖生效）；probe 期间 STATS.requests_total 不增、客户请求不受影响。
- dashboard 加载 `view-source:` 全文 0 个 `innerHTML=`；新卡片渲染模型速度对比表（含 P50/P90 列）、token 用量表、上游健康卡（含最近 probe 时间/延迟/连续失败）、三态可用率。
- 性能：跑 `test_client_abort_is_quiet_and_not_error`（1.2MB big 流）相对 main 分支耗时增幅 < 10%；`_relay_sse` 热循环每 record 新增 CPU 指令 < 1μs（人工 review 打点代码量佐证，无需 microbenchmark）。

## Risks

- **R1 热路径性能**（最高）：`_relay_sse` 是转发核心，每 record 已有一次 json.loads 用于 kind 判定；本设计叠加 usage 抽取（共享 loads 结果，零额外 parse）+ 阶段计时（每 record 1 次 `time.monotonic()`）+ 桶下标计算。若 implementer 误把 stall 检测写成持锁 per-record 自增，并发下 STATS_LOCK 抖动。spec 已明示"stall 自增 <0.1% record 命中，其他打点零锁"；PLAN-B 阶段必须重申。
- **R2 持久化向后兼容**：旧版本二进制读新格式 JSON 必须 graceful degrade（只丢新字段不崩）。当前 `load_daily_buckets`/`load_daily_by_model_buckets` 已按 `_DAILY_FIELDS` 白名单迭代，天然兼容；但 `load_stats_events` 的 kind 白名单（:623）写死 `("proxy","upstream","retry")`——P4 加 `probe_alert` 时若旧版本二进制读新格式 events 会直接 continue 丢弃（正确），但要测试锁住。**降级演练测试**必须存在（P3 `DailyV2CompatTest` + P4 events 白名单测试）。
- **R3 probe 线程并发与副作用**：探测线程与请求线程共享 STATS/EVENTS。设计已用独立 `_PROBE_STATE` + PROBE_LOCK，仅 EVENTS append 走 STATS_LOCK（与 `_record_empty_retry` :739 同款模式，已验证）。风险在 implementer 误把 probe 计数走 `_record_request` 路径——spec 明示"失败零副作用"，测试锚 `ProbeNoSideEffectTest` 强制。
- **R4 histogram 维度爆炸**：`ttfb_hist` 按模型分桶，BY_MODEL_CAP=32 复用；若实现错把 stage 维度也按模型分，内存从 32×9 桶膨胀到 32×9×3。spec 明示 phase_ms 全局不按模型，PLAN-B 卡住。
- **R5 dashboard 拆分字符串常量拼接顺序**：6 段常量 join 漏一段或顺序错 → 页面白屏但后端 200。`DashboardV2SkeletonTest` 用正则断言关键容器 id 与函数名存在兜底；同时 AdminIntegrationTest 现有 `test_dashboard_html_full_page` 已断言基本骨架，重构后仍须通过。
- **R6 部署副本同步**：与 body-error spec 同款风险——生产副本 `~/.local/bin/ctyun-stream-fix-proxy.py` 与仓库源需手工 cp + launchctl kickstart，每期 DELIVER 都需提醒。

## Exclusions

- **不改转发语义**：剥行/重试/finish-hold/priming 路径零行为变化；观测是叠加层。
- **不引入第三方依赖**：numpy/prometheus_client/structlog 等一律不引；P50/P90 手写直方图插值。
- **不拆 dashboard 为静态文件**：方案 b 已拒绝（破坏单文件部署）。
- **不做 cost 计费 UI**：`model_pricing` schema 落 P3，但前端展示与 currency 计算排除；后续 episode。
- **不做分布式 trace / OpenTelemetry**：rid 是进程内单调序列，不与外部 trace id 互操作。
- **不记录请求体/响应体到 token 统计**：usage 数值仅从 SSE usage 帧抽取；prompt 文本内容仍只走 ERROR_EVENTS 现有 capture_errors 路径（默认 off）。
- **不动 followups.md 现有遗留项**（test.py:552 teardown 偶发超时——独立 episode 处理）。
- **不改 launchd plist 模板**、不改端口、不改 `X-Admin-Token` 鉴权模型。
- **probe 不解析响应体**：HEAD 请求只取 status 与 TTFB，不验证业务可达性（那是端到端测试的职责，不是代理的）。

## R31 Evidence

[R31-S1] 现状锚点（grep -n 行号形式，避免 fenced 块行首 `#` 触发 R31 校验截断）：

```
$ grep -nE '^(STATS|RECENT_REQUESTS|EVENTS|ERROR_EVENTS|_DAILY_FIELDS|_CFG_LOCK|STATS_LOCK|ERROR_LOCK|LOG_LOCK|PROBE)' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
312:_CFG_LOCK = threading.Lock()
313:STATS_LOCK = threading.Lock()
314:STATS = {"requests_total": 0, ...
320:RECENT_REQUESTS = collections.deque(maxlen=100)
322:EVENTS = collections.deque(maxlen=100)
327:ERROR_EVENTS = collections.deque(maxlen=ERROR_RING_MAX)
328:ERROR_LOCK = threading.Lock()
331:LOG_RING = collections.deque(maxlen=LOG_RING_MAX)
332:LOG_LOCK = threading.Lock()
558:_DAILY_FIELDS = ("requests", "filtered", ...
```

```
$ grep -nE 'def (_record_request|_record_empty_retry|_record_eof_without_done|_record_header_retry|stats_snapshot|sse_line_has_usage|sse_line_body_error|classify_outcome|_relay_sse|_relay_buffered|_log|_open_upstream|_proxy|_proxy_relay|main)' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
139:def sse_line_has_usage(line: bytes) -> bool:
155:def sse_line_body_error(line: bytes) -> bool:
172:def classify_outcome(status=None, ...
680:def _record_request(method: str, path: str, ...
732:def _record_empty_retry(model=None, ...
763:def _record_eof_without_done(model=None) -> None:
789:def _record_header_retry(model=None) -> None:
815:def stats_snapshot() -> dict:
858:    def _proxy(self) -> None:
879:    def _proxy_relay(self, started: float) -> None:
1028:    def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):
1056:    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
1143:    def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
1171:    def _log(self, started: float, status: int, result: str, ...
1900:def main() -> None:
```

[R31-S2] 需求来源：用户 2026-09-30 在 brainstorming 中明确确认 A/B/C/D 四类全量 scope + dashboard 重构，且明确推翻 2026-09-05 "不记 token" 旧决策（见本 spec Supersede 段）。supersede 决策的事实来源是主代理转述，spec 已将 Supersede 单独成段供 reviewer 校验。

[R31-S3] 测试基建锚点：现有测试 seam 完备（CTYUN_UPSTREAM_BASE / CTYUN_LISTEN_PORT / CTYUN_ADMIN_PORT / CTYUN_PERSIST_PATH + spawn 子进程），118 用例全绿（README.md:84 明示 `/usr/bin/python3`）；新增 P1-P5 测试沿用 `start_proxy`（test.py:383）/ `make_fake_upstream`（test.py:220）/ `make_scripted_upstream`（test.py:269）/ `make_stall_upstream`（test.py:277）/ `make_sleep_stall_upstream`（test.py:286）/ `load_proxy_module`（test.py:500）模式，无需新基建。
