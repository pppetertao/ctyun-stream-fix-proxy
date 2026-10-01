# Observability v3（perf tab 十项 + token 统计七项）— 设计文档

状态：待实现（本 spec 落盘不 commit）
范围：单文件纯 stdlib Python 本地反代（主文件 `ctyun-stream-fix-proxy.py` 约 3600 行，测试 `ctyun-stream-fix-proxy.test.py`，unittest 框架，运行命令 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`，当前 229 绿口径以 CI 最近全绿为准）。

## Goal

为 ctyun-stream-fix-proxy 的 dashboard 补齐 17 项可观测能力：perf tab 十项（P1-P10，全部按模型拆分）+ token 统计七项（T1-T7），把已存在但未暴露的后端数据（ttfb_hist、phase_ms、stalls、tpm_snapshot、ERROR_EVENTS、probe history、RECENT_REQUESTS、usage 结算 delta）全部接到 UI，并新增 per-key token 用量、cache/reasoning token 解析、TPM 估算偏差留存、0-token 流聚合、小时级 token 曲线、月末配额投影。服务所有经浏览器访问本代理运行台的运维/开发者。

## 设计决策（定死）

1. **daily 桶 schema 迁移（16→20 字段，最大风险）**：`DAILY_V2_FIELDS`（:1207-1211，现 16 字段）扩为 20 字段，追加 `("tokens_cache_read", "tokens_cache_write", "tokens_reasoning", "requests_zero_token")`（T2 cache、T3 reasoning、T5 0-token；cache_write 占位——解析不做但 schema 先留，避免下次再迁移）。reasoning 落 daily 的理由：usage_dict_tokens 5 元组第 5 位解析出来后必须有消费落点（无死代码铁律），daily 持久化是 token 按天×模型表（决策 9 T3 扩列）的数据源，落 daily 后 dm/daily 桶、range_stats、token 按天×模型表全链路自然贯通；不落 daily 则只能进 RECENT_REQUESTS 内存环，与 T2 cache_read 口径不一致（同一次解析两个字段一个落一个不落，语义割裂）。迁移机制沿用现有 `_DAILY_FIELDS = DAILY_V2_FIELDS` 赋值（:1216）：load 循环按 `_DAILY_FIELDS` 白名单逐字段 `bucket.get(field)` 补 0（:1230-1232、:1260-1262），旧 16 字段文件加载自动补 4 个 0；新 20 字段文件被旧版二进制读入时旧白名单丢 4 新字段（R2 双向 degrade，无版本号）。**不新增独立 migration 代码**——白名单清洗机制本身就是迁移层。

2. **daily_by_key（T1）**：新增 `STATS["daily_by_key"]`，结构 `{day: {key_id12: {field: int}}}`，key 用 `tpm_key_id`（:419-434）返回的 sha256 hex 前 12 位（与 `tpm_snapshot` :635 的 `sha256:<12>` 脱敏口径一致，不存全位）。字段集 = `_DAILY_FIELDS` 子集 `("requests", "tokens_prompt", "tokens_completion", "tokens_cache_read", "tokens_cache_write", "tokens_reasoning", "bytes_out", "stream_requests")`（8 字段，砍 errors/retries/outcome/ttfb——per-key 维度不做错误归因与延迟，控制持久化体积）。prune 复用 `_prune_daily`（:1052-1060，value-agnostic）；key 上限新增 `BY_KEY_CAP = 64`（对齐 TPM_KEY_CAP :106），加载截断仿 `load_daily_by_model_buckets` :1257 的 cap 语义。持久化：`save_stats_counters` :1083-1087 加第三路深拷 + prune，`load` 侧新增 `load_daily_by_key_buckets(path)` 仿 :1237-1265。

3. **phase_ms / stalls 拆 per-model（P6/P7）**：新增 `STATS["phase_ms_by_model"]`（`{model: {"connect":[0]*9,"headers":[0]*9,"body":[0]*9}}`）与 `STATS["stalls_by_model"]`（`{model: int}`），cap 复用 BY_MODEL_CAP。**保留全局 `phase_ms`（:867）与 `stalls_total`（:871）不动**——全局版是存量 API 契约（`stats_snapshot` :1699-1719 已返回），per-model 是新增键，两者并存（双写）。stalls per-model 自增点的 model 来源（tracer 已实测闭环）：`_relay_sse`（:2069，签名 `(self, resp, final)`）函数体内零 model 引用——model 是调用方 `_proxy_relay`（:1778）的局部变量（:1781 `extract_model`），两个调用点 :1877（`self._relay_sse(resp, ...)`）与 :1917（`self._relay_sse(resp, final=True)`）。**方案：`_relay_sse` 加 `model=None` kwarg**（放 `final` 后，默认 None 保持其他测试/调用兼容），两个调用点传 `model=model`（:1781 局部变量）；stall 自增处（:2151-2156）同锁内 `if model is not None` 守卫 + cap 后 `stalls_by_model[model] += 1`。备选（否掉）：把 stalls_by_model 更新挪到 `_proxy_relay` 结算时按"本次请求是否发生 stall"反推——stall 判定在 relay 循环内实时发生，结算点无 stall 计数上下文，反推需新增实例属性透传，比加参数更绕。

4. **qwait / TPM 偏差环形容量（P8/T4）**：新增 `STATS["qwait_ms_by_model"]`（`{model: collections.deque(maxlen=500)}`，QWAIT_RING_MAX=500）与 `STATS["tpm_settle_ratio_by_model"]`（`{model: collections.deque(maxlen=500)}`，SETTLE_RING_MAX=500）。500 的理由：per-model 分位 P50/P90 需 ≥20 样本，500 覆盖近 500 次请求且内存可控（≤32 模型 × 500 × 8B ≈ 128KB）。qwait 样本来源：`tpm_admit` 返回的 qwait_ms 已在 `_proxy_relay` :1791 接收（局部变量 `tpm_qwait_ms`，:1787 初始化、:2010 `_log` 已消费），结算时随 `_record_request` 新增 kwarg `qwait_ms=None` 传入（流式 :1967-... 与非流式 :2012-2022 两结算点都在 `_proxy_relay` 内，均可访问该局部变量）；T4 比率样本在 `tpm_settle` :591-610 内计算 `actual/est`（est>0 且 actual≥0 时）追加。命名对齐 auto-calibrate spec：比率样本键名 `tpm_settle_ratio_by_model`，不与其 `tpm_body_err_samples`/`tpm_probe_results` 撞车。

5. **小时级 token 曲线（T6）**：新增 `STATS["hourly_tokens"]` = `collections.deque(maxlen=49)`，条目 `{hour_start_ts, tokens_prompt, tokens_completion}`，每小时整点滚动。**内存态不持久化**（重启清零可接受，与 ttfb_hist/phase_ms/rates 同策略——save/load 白名单均不含）；持久化 daily 桶已有天级口径，小时级只是 48h 放大视图，落盘收益不抵成本。

6. **新数据出口**：**扩 `/api/stats`（`stats_snapshot` :1676-1720）** 而非新 API——P1（ttfb_hist 已返回）、P5（perf 节已返回 bytes_per_s 等，只缺渲染）、P6/P7/P8/T4 新键全部挂进 `snap["perf"]` 子树与 `snap["daily_by_key"]`。理由：dashboard 现有 `poll()` 每 2s 全量拉 `/api/stats`，新增独立 API 会引入第二次 fetch 与状态同步复杂度；perf 节是既成扩展点。**例外**：P3 消费 `/api/tpm_stats`（已存在）、P4 消费 `/api/errors`（已存在）、P10 新增 `GET /api/probe_history`（降采样 ~200 点）——前两者零后端改动，后者因 `_PROBE_STATE` 独立于 STATS_LOCK（PROBE_LOCK 护）且 history 量大（20160 条），走独立端点不拖累 `/api/stats` 主路径。

7. **probe history 导出（P10）**：`probe_state_snapshot`（:1411-1425）保持 6 键契约不动（`/api/health` 消费者不破坏）；新增 `probe_history_snapshot()` 在 PROBE_LOCK 内取 `history` deque 浅拷贝，均匀抽稀到 ≤200 点（`step = max(1, len//200)`），返回 `{"points": [[ts, ms], ...], "count_total": n}`。注意 history 只存 ms 不存 ts——**扩展 deque 条目为 `(ts, ms)` 二元组**（写入点 `_probe_loop` 成功分支，需同步改 `_probe_spike_due` :1313-1324 与 `_latency_p90` 消费处：从 `for ms in history` 改 `for _, ms in history`）。

8. **usage_dict_tokens 扩展（T2/T3）**：`usage_dict_tokens`（:303-319）现返回 3 元组，**改为返回 5 元组** `(prompt, completion, total, cache_read, reasoning)`，签名变更需同步全部调用点（:2115 `_relay_sse`、:2000 `_proxy_relay` 非流式结算分支、:332 `sse_line_extract_usage`）。cache_read 解析顺序：`prompt_cache_hit_tokens` → `cache_read_input_tokens` → `prompt_tokens_details.cached_tokens`（嵌套 dict 的 `cached_tokens` int 字段），首个命中即取；reasoning 解析：`completion_tokens_details.reasoning_tokens`（嵌套）→ `reasoning_tokens`（顶层）。全部 best-effort：缺失/非 int/负数 → 0（沿用 `_int_field` 语义，嵌套 dict 先 `isinstance(value, dict)` 守卫）。cache_write（`prompt_cache_miss_tokens`/`cache_creation_input_tokens`）本次**不解析**——见 Exclusions（先验证上游实际回传字段再扩，避免猜错别名）。reasoning 第 5 位消费落点：`_record_request` 新增 kwarg `tokens_reasoning: int = 0`，两个结算点（`_proxy_relay` 流式 :1967 区域、非流式 :2012-2022）传 `tokens[4]`，落 dm/daily 桶 `tokens_reasoning` 字段（决策 1）。

9. **perf tab 分组布局**：P1-P10 全部进 perf tab（沿用 2026-10-01-dashboard-tabs-design.md 的 tab 结构），按「延迟」「错误与限流」「流量与 token」「健康」四组排列：延迟组=P1 TTFB 直方图 + P6 三阶段 + P9 慢请求 Top N；错误与限流组=P2 错误/重试率卡 + P3 TPM 限流观测 + P4 错误事件流；流量与 token 组=P5 流量画像 + P8 qwait 分位 + T4 偏差分位 + T6 小时曲线；健康组=P10 probe 趋势线。T1/T2/T3/T5/T7 进 overview/tokens 区（T1 per-key 表、T2 cache_read 与 T3 reasoning 进 token 按天×模型表扩列——数据源 daily 桶 `tokens_cache_read`/`tokens_reasoning` 字段、T5 0-token 聚合卡、T7 月末投影卡进 overview stats-row 附近）。

10. **T7 月末配额投影**：纯前端。`range_stats`（:1037-1049）已返回 mtd 聚合，`range_bounds` 含 `[start, end]`；前端取 mtd tokens_total ÷ 当月已过天数 × 当月总天数。零后端改动。

## Files to Change

所有生产代码进 `ctyun-stream-fix-proxy.py`；测试进 `ctyun-stream-fix-proxy.test.py`。锚点为当前工作树行号。

- `ctyun-stream-fix-proxy.py:48-49`（常量区）——新增 `BY_KEY_CAP = 64`、`QWAIT_RING_MAX = 500`、`SETTLE_RING_MAX = 500`、`HOURLY_RING_MAX = 49`。
- `ctyun-stream-fix-proxy.py:303-319` `usage_dict_tokens(usage) -> tuple` —— 返回 3 元组改 5 元组 `(prompt, completion, total, cache_read, reasoning)`；新增 `_int_nested(parent_key, child_key)` 辅助（嵌套 dict 取 int，非 dict → 0）；cache_read 按决策 8 三级别名顺序，reasoning 按两级。
- `ctyun-stream-fix-proxy.py:322-332` `sse_line_extract_usage(line)` —— 返回值随 `usage_dict_tokens` 同步 5 元组。
- `ctyun-stream-fix-proxy.py:591-610` `tpm_settle(key_id, est, actual, model)` —— `delta != 0` 分支后追加：`if est > 0 and actual >= 0: ratio = actual / est` 并 append 到 `STATS["tpm_settle_ratio_by_model"][model]`（需拿 STATS_LOCK——**注意锁序**：`tpm_settle` 持 TPM_LOCK，STATS_LOCK 必须在 TPM_LOCK 内获取，锁序固定 TPM_LOCK→STATS_LOCK，与 `_record_request` 不冲突因后者不持 TPM_LOCK；implementer 须在代码注释写明锁序防死锁）。
- `ctyun-stream-fix-proxy.py:861-871` `STATS` 初始化 —— 新增键：`daily_by_key: {}`、`phase_ms_by_model: {}`、`stalls_by_model: {}`、`qwait_ms_by_model: {}`、`tpm_settle_ratio_by_model: {}`、`hourly_tokens: collections.deque(maxlen=HOURLY_RING_MAX)`。
- `ctyun-stream-fix-proxy.py:1052-1060` `_prune_daily` —— 不改（value-agnostic 注释已覆盖 daily_by_key 复用）。
- `ctyun-stream-fix-proxy.py:1077-1107` `save_stats_counters(path)` —— :1084 后加 `_prune_daily(STATS["daily_by_key"])`；:1086-1087 后加 `daily_by_key` 三路深拷 `{d: {k: dict(v) for k, v in keys.items()} for d, keys in ...}`；:1104 `json.dump` 载荷 `dict(counters, daily=..., daily_by_model=..., daily_by_key=daily_by_key, events=...)`。
- `ctyun-stream-fix-proxy.py:1207-1216` `DAILY_V2_FIELDS` —— 16→20 字段，追加 `("tokens_cache_read", "tokens_cache_write", "tokens_reasoning", "requests_zero_token")`（cache_write 字段占位保留——解析不做但 schema 先留，避免下次再迁移；tokens_reasoning 服务 T3 落 daily，决策 1 理由；requests_zero_token 服务 T5）。
- `ctyun-stream-fix-proxy.py:1219-1234` `load_daily_buckets` / `:1237-1265` `load_daily_by_model_buckets` —— 零改动（白名单迭代自动覆盖 20 字段）。
- 新增 `load_daily_by_key_buckets(path)`（放 :1266 之后）——仿 :1237-1265：外层 ISO round-trip 校验、内层 key 非空 str ≤12 hex 字符、`len(bucket) >= BY_KEY_CAP` 截断、8 字段白名单清洗（决策 2 字段集）。
- `ctyun-stream-fix-proxy.py:1484-1597` `_record_request(...)` —— 签名加 kwargs：`tokens_cache_read: int = 0`、`tokens_reasoning: int = 0`、`qwait_ms=None`、`key_id12=None`（str 或 None，调用方已从 `tpm_key_id` 截前 12）。锁内追加：① dm/daily 桶 `tokens_cache_read`/`tokens_reasoning`/`requests_zero_token` 增量（cache_read/reasoning 沿 :1524-1527 `if tokens_prompt > 0` 同式 `> 0` 守卫；tokens==0 且 status<500 且非 error 时 `requests_zero_token += 1`，T5 口径）；② `daily_by_key` 更新：`if key_id12: day_keys = STATS["daily_by_key"].setdefault(today_key(), {}); entry = day_keys.get(key_id12); if entry is None and len(day_keys) < BY_KEY_CAP: entry = day_keys[key_id12] = dict.fromkeys(_DAILY_BY_KEY_FIELDS, 0)`（`_DAILY_BY_KEY_FIELDS` 新常量 = 决策 2 的 8 字段）；③ `phase_ms_by_model` 双写（:1578-1581 全局循环旁加 per-model 版，cap BY_MODEL_CAP）；④ `qwait_ms_by_model[model].append(qwait_ms)`（qwait_ms 非 None 且 model 存在时）；⑤ `hourly_tokens` 滚动：当前小时首请求建条目 `{"hour_start_ts": int(time.time()//3600*3600), "tokens_prompt":0,"tokens_completion":0}`，非同小时 append 新条目（deque 自动挤掉最旧）。
- `ctyun-stream-fix-proxy.py:1608-1631` `_record_empty_retry` / `:1634-1652` `_record_eof_without_done` / `:1655-1673` `_record_header_retry` —— 8 处 `dict.fromkeys(_DAILY_FIELDS, 0)` 创建点（:1513/:1538/:1624/:1628/:1646/:1650/:1667/:1671）零改动（`_DAILY_FIELDS` 已指向 20 字段，自动同步）。
- `ctyun-stream-fix-proxy.py:1676-1720` `stats_snapshot()` —— 浅拷区（:1679-1689）加 `daily_by_key` 深拷、`qwait/settle` deque 转 list；perf 节（:1699-1719）追加：`phase_p50_ms_by_model`/`phase_p90_ms_by_model`（per-model 三阶段分位）、`stalls_by_model`（dict 直出）、`qwait_p50_ms_by_model`/`qwait_p90_ms_by_model`（deque 排序取最近秩，复用 `_latency_p90` 算法推广 `_percentile(samples, q)`）、`tpm_settle_ratio_p50_by_model`/`tpm_settle_ratio_p90_by_model`；`perf["ttfb_hist_by_model"] = ttfb_hist` 原样浅拷（P1 直方图数据源）；`snap["daily_by_key"]` 顶层键；`snap["hourly_tokens"] = list(...)`。
- `ctyun-stream-fix-proxy.py:2069` `_relay_sse(self, resp, final)` —— 签名加 `model=None` kwarg（放 `final` 后）；调用点 :1877 与 :1917（`_proxy_relay` 内）传 `model=model`（:1781 局部变量）；usage 消费处 :2115 `_p3_usage_tokens = usage_dict_tokens(usage_hit)` 返回值 5 元组解包同步。
- `ctyun-stream-fix-proxy.py:2151-2156` `_relay_sse` stall 自增 —— `STATS["stalls_total"] += 1` 旁加 `if model is not None`（签名新增参数）+ cap BY_MODEL_CAP 守卫后 `stalls_by_model[model] += 1`（model=None 时不记 per-model，仅全局，与 ttfb_hist 的 `model is not None` 守卫口径一致）。
- `ctyun-stream-fix-proxy.py:1955-1975`（`_proxy_relay` 流式结算点，:1967 `_record_request` 调用）—— 补 `tokens_cache_read=p3_tokens[3] if p3_tokens else 0`、`tokens_reasoning=p3_tokens[4] if p3_tokens else 0`、`qwait_ms=tpm_qwait_ms`、`key_id12=tpm_key[:12] if tpm_key else None`（p3_tokens 为 :1966 局部变量，tpm_qwait_ms :1787/:1791、tpm_key :1785 均为 `_proxy_relay` 局部变量）。
- `ctyun-stream-fix-proxy.py:1997-2022`（`_proxy_relay` 非流式结算分支）—— `p3_buf_tokens = usage_dict_tokens(usage)`（:2000）5 元组同步；`_record_request` 调用（:2012-2022）补 `tokens_cache_read=p3_buf_tokens[3] if p3_buf_tokens else 0`、`tokens_reasoning=p3_buf_tokens[4] if p3_buf_tokens else 0`、`qwait_ms=tpm_qwait_ms`（:2010 `_log` 已消费该局部变量）、`key_id12=tpm_key[:12] if tpm_key else None`。
- `ctyun-stream-fix-proxy.py:884` `_PROBE_STATE["history"]` —— deque 条目从 `ms` 标量改 `(ts, ms)` 二元组；写入点（`_probe_loop` 成功 append 处，约 :1390-1408 区域）同步；`_latency_p90`（:1304-1310）与 `_probe_spike_due`（:1313-1324）消费处改解包 `for _, ms in history`。
- 新增 `probe_history_snapshot()`（放 :1425 之后）——PROBE_LOCK 内取 history，均匀抽稀 ≤200 点，返回 `{"points": [[ts, ms], ...], "count_total": len}`。
- `ctyun-stream-fix-proxy.py:2271-2291` `do_GET` 分发 —— `/api/health` 分支后加 `elif path == "/api/probe_history": self._send_json(200, probe_history_snapshot())`。
- `ctyun-stream-fix-proxy.py:3488-3492` `DASHBOARD_HTML` join —— 若新增 dashboard 常量段（P1-P10/T1-T7 的 HTML/JS）则 join 列表同步，命名沿用 `_DASH_SECTIONS_*`/`_DASH_JS_*` 风格（implementer 决定并入既有段还是新增 `_DASH_SECTIONS_V3`/`_DASH_JS_V3`——join 等式断言 `test_segment_constants_present_and_join` 必须同步）。
- Dashboard HTML/JS（`_DASH_SECTIONS_V2` :2628 起、`_DASH_JS_CORE`/`_DASH_JS_V2`）——新增 12 张卡：P1 按模型 TTFB 直方图（仿 `renderLatencyDist` :3151-3189 的 SVG 条，数据源 `perf.ttfb_hist_by_model` 9 桶数组）；P2 错误/重试率卡（daily_by_model 当日桶 retries/errors_proxy/errors_upstream/header_retries/eof_without_done ÷ requests）；P3 TPM 限流观测卡（fetch `/api/tpm_stats` 渲染 buckets 表）；P4 错误事件流（fetch `/api/errors` 列表渲染，点行展开详情走 `?id=` 鉴权端点）；P5 流量画像（`perf.bytes_per_s` 等已返回，只补渲染 + daily 桶 stream_share）；P6 三阶段按模型分位表；P7 stalls 按模型卡（`perf.stalls_by_model`）；P8 qwait P50/P90 卡；P9 慢请求 Top N（`snap.recent` 前端按 dur_ms 降序取前 10，零后端改动）；P10 probe SVG 折线（fetch `/api/probe_history`）；T1 per-key token 表（`snap.daily_by_key` 当日）；T5 0-token 聚合卡（daily 桶 `requests_zero_token`）；T6 小时曲线 SVG（`snap.hourly_tokens`）；T7 月末投影卡（前端算）；T2/T3 进既有 token 按天×模型表扩列（daily 桶 `tokens_cache_read`/`tokens_reasoning`）。全部 `textContent` 渲染（防 XSS 门禁 `test_no_inner_html_anywhere`）。
- `ctyun-stream-fix-proxy.test.py` —— 新增 `ObservabilityV3Test(unittest.TestCase)` 类 + 更新既有受影响用例（见 Risks R2）。

## Acceptance Criteria

全部测试用 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 运行，必须全绿（现有 229 口径用例 + 新增用例 + 本次更新的受影响用例，0 failure / 0 error）。

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

- AC1（schema 迁移，单元级）：写一份 16 字段 legacy persist 文件（无 `tokens_cache_read` 等 4 新键），`load_daily_buckets` / `load_daily_by_model_buckets` 加载后每桶 20 键齐全、4 新键为 0；`DAILY_V2_FIELDS` 长度断言 == 20；反向：20 字段文件被"旧白名单"（测试内手工构造 16 字段 tuple 替换 `mod._DAILY_FIELDS`）加载丢 4 新键不抛错。
- AC2（daily_by_key 链路，单元级）：`_record_request(..., key_id12="abcdef123456", tokens_prompt=10, ...)` 后 `STATS["daily_by_key"][today]["abcdef123456"]["tokens_prompt"] == 10`；超 BY_KEY_CAP 后新 key 不记录；`save_stats_counters` 落盘文件含 `daily_by_key` 键；`load_daily_by_key_buckets` 对缺键/损坏/非 ISO 日期/超 cap → 容错返回。
- AC3（usage 5 元组 + reasoning 落桶，单元级）：`usage_dict_tokens({"prompt_tokens":1,"completion_tokens":2,"total_tokens":3,"prompt_cache_hit_tokens":4,"completion_tokens_details":{"reasoning_tokens":5}})` == `(1,2,3,4,5)`；别名路径 `cache_read_input_tokens` 与 `prompt_tokens_details.cached_tokens` 各命中；缺字段 → 0；`sse_line_extract_usage` 同步 5 元组；`_record_request(..., tokens_cache_read=4, tokens_reasoning=5)` 后 dm/daily 桶 `tokens_cache_read == 4`、`tokens_reasoning == 5`。
- AC4（P6/P7 per-model，单元级）：`_record_request(..., model="m1", phase_ms={"connect":10,"headers":20,"body":30})` 后 `STATS["phase_ms_by_model"]["m1"]["connect"]` 对应桶 +1 且全局 `phase_ms` 同桶也 +1（双写）；`_relay_sse` 加 `model` 参数后 stall 路径（调用点传 `model="m1"`）断言 `stalls_by_model["m1"]` +1 且 `stalls_total` 同 +1；`model=None` 调用只增 `stalls_total`。
- AC5（qwait/settle 分位，单元级）：注入 100 个 qwait 样本后 `stats_snapshot()["perf"]["qwait_p90_ms_by_model"]["m1"]` 与手工最近秩计算一致；`tpm_settle(k, est=100, actual=90, model="m1")` 后 `tpm_settle_ratio_by_model["m1"]` 含 0.9。
- AC6（T6 小时环，单元级）：跨小时边界的两次 `_record_request` 产生 2 个 `hourly_tokens` 条目，`hour_start_ts` 差 3600；超 49 条挤出最旧。
- AC7（T5 0-token，单元级）：`tokens=None`（或 0）且 status<500 的请求后 daily 桶 `requests_zero_token` +1；status>=500 不计。
- AC8（P10 probe history，单元级）：构造 `_PROBE_STATE["history"]` 存 1000 条 `(ts, ms)`，`probe_history_snapshot()["points"]` 长度 ≤200 且单调 ts；`_latency_p90`/`_probe_spike_due` 对二元组 deque 计算结果与旧标量版等价（回归）。
- AC9（API 形态，集成级）：`GET /api/probe_history` 200 + JSON 含 `points`/`count_total`；`GET /api/stats` 含 `daily_by_key`/`hourly_tokens`/`perf.qwait_p50_ms_by_model`/`perf.stalls_by_model`/`perf.ttfb_hist_by_model` 键。
- AC10（dashboard，集成级 `AdminIntegrationTest` 风格）：`GET /` HTML 含 12 张新卡关键 id/文案（implementer 定 id 清单）+ token 按天×模型表新增 cache_read/reasoning 列；`test_no_inner_html_anywhere` 扩展后全量扫描仍 0 命中；`test_segment_constants_present_and_join` join 等式同步（若新增常量段）。
- AC11（回归）：现有全部用例绿；特别地 `test_daily_by_model_cap`（:1566）、:1632/:1637/:1657/:1697/:1749 的 `DAILY_V2_FIELDS` 相关断言（16 字段字样出现在 :1750 断言消息，需更新为 20）、:2054/:2092 prune 用例、`test_model_list_auto_generated` 等不受影响或已同步。

## Risks

- R1（锁序死锁）：`tpm_settle` 持 TPM_LOCK 内需拿 STATS_LOCK 写 `tpm_settle_ratio_by_model`。锁序固定 TPM_LOCK→STATS_LOCK；`_record_request` 只持 STATS_LOCK 不持 TPM_LOCK，`tpm_snapshot` 只持 TPM_LOCK——无反向路径，安全。但 implementer 必须在 `tpm_settle` 改动处注释锁序，且**禁止**在 STATS_LOCK 持锁路径上新增任何 TPM_LOCK 获取。
- R2（schema 迁移破坏既有用例）：以下用例类别会被 16→20 打破，必须同步更新——① `DAILY_V2_FIELDS` 字段数/集合断言（test.py:1632/:1637/:1657/:1697/:1749，:1750 断言消息含 "16 fields" 字样，改 20）；② 任何硬编码 16 字段 dict 构造的 fixture；③ persist 文件 fixture 若断言字段全集。`dict.fromkeys(mod.DAILY_V2_FIELDS, 0)` 形式的断言（:1632 等）天然随常量走、不需改，仅字段数硬断言需改。
- R3（`_relay_sse` 签名变更回归面）：`_relay_sse` 加 `model=None` kwarg（tracer 实测闭环——函数体内零 model 引用，model 来自 `_proxy_relay` :1781 局部变量，方案定死为加参数）。回归面：① 两个真实调用点 :1877/:1917 必须同步传 `model=model`，漏传则 stalls_by_model 恒空；② 测试内若有直接调 `_relay_sse` 的用例，旧两参调用（位置参数 `resp, final`）兼容（model 是 kwarg 默认 None）；③ 任何 mock/patch `_relay_sse` 的用例需确认签名断言不含参数个数硬校验。AC4 断言双路径（传 model/不传）。
- R4（持久化文件膨胀）：daily 桶 16→20 字段 + 新增 daily_by_key（≤90 天 × ≤64 key × 8 字段）。最坏体积估算：daily_by_key ≈ 90×64×8×~8B ≈ 360KB JSON——可接受（现有 daily_by_model 90×32×16 已同量级）。`save_stats_counters` 是 SIGTERM/60s 脏刷调用，非热路径。
- R5（probe history 条目形态变更）：deque 从标量 ms 改 `(ts, ms)` 二元组，所有消费点（`_latency_p90` :1304、`_probe_spike_due` :1313、`_probe_loop` append 处）必须同 PR 同步，漏一处即 TypeError。AC8 回归断言兜底。
- R6（usage_dict_tokens 签名变更是 breaking change）：3 个调用点（:332/:2115/:2000）+ 测试内直接调用本函数的用例全部同步；返回值位置索引 `[0]/[1]/[2]` 语义不变（prompt/completion/total 仍前三位），新增 `[3]/[4]`——旧调用点只取前 3 的解包方式（如 `p3_buf_tokens[2]`）零改动兼容。
- R7（`/api/stats` 响应体积增长）：新增 daily_by_key + hourly_tokens + 多个 per-model perf 键，poll 每 2s 全量拉。估算增量 <50KB/次（daily_by_key 当日桶 ≤64×8 字段），本机 loopback 可接受；不做分页/增量拉取（Exclusions）。
- R8（XSS 门禁）：12 张新卡全部 `textContent` 渲染，`test_no_inner_html_anywhere` 必须扩展覆盖新常量段（若新增 `_DASH_SECTIONS_V3`/`_DASH_JS_V3`），与 dashboard-tabs spec 决策 7 同纪律。
- R9（T2 cache 别名猜错）：上游实际回传字段名未实证。决策 8 给三级别名 + best-effort 0 兜底；若三级全不命中，cache_read 恒 0 不报错，后续 episode 按真实流量样本补别名（风险已隔离，不阻塞本 episode 其他 16 项）。reasoning 别名同理（两级兜底，恒 0 可接受）。

## Exclusions

本次**不做**：

- 不做 cache_write（`prompt_cache_miss_tokens`/`cache_creation_input_tokens`）解析——daily schema 已留 `tokens_cache_write` 占位字段（避免下次迁移），解析逻辑待上游真实字段实证后另开 episode。
- 不做 `/api/stats` 分页/增量拉取/压缩（R7 评估可接受）。
- 不做 probe history 持久化（沿用内存态不持久化策略，与 ttfb_hist 等一致）。
- 不做 P4 错误事件流的后端改动（`/api/errors` 已存在，本次纯前端消费）；不做错误事件的长期持久化扩容（ERROR_RING_MAX=50 不动）。
- 不改 `probe_state_snapshot` 6 键契约（`/api/health` 消费者不破坏，P10 走新端点）。
- 不改全局 `phase_ms`/`stalls_total` 存量行为（双写并存，不删除旧键）。
- 不做 TPM 自动校准（`.worktrees/tpm-auto-calibrate` 独立 episode，本 spec 仅决策 4 对齐其命名防撞车，不实现其任何功能）。
- 不做 per-key 的错误归因/延迟统计（决策 2 砍字段理由）。
- 不引入新依赖（纯 stdlib）；不改 plist/launchd/deploy；不改 `_relay_sse`/`_relay_buffered` 的剥毒语义（仅 `_relay_sse` 加 `model` kwarg 透传，剥毒/观测逻辑零改动）；不改 `classify_outcome`。
- 不做小时级 token 曲线的持久化（决策 5）；不做 T7 的后端投影计算（纯前端）。
- followups.md 当前为空，无既有遗留项纳入。

## R31 Evidence

[R31-S1] 问题存在：17 项观测能力中 10 项的后端数据已存在但 dashboard 零消费/零暴露。

证据①（已返回未渲染——P1/P5：`ttfb_hist` per-model 9 桶已入 STATS 且 `stats_snapshot` 计算 p50/p90，dashboard renderPerf 未渲染；bytes_per_s/chunks_per_s/tokens_per_s/stream_share 已返回未渲染）：

```
$ grep -n 'ttfb_hist\|bytes_per_s\|stalls_total' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
866:         "ttfb_hist": {},    # {"<model>": [0]*9}
868:         "rates": {"bytes_out_total": 0, ...
871:         "stalls_total": 0}
1572:        if ttfb_ms is not None and model:
1577:                ttfb_hist[model][bisect.bisect_right(HIST_BUCKETS_MS, ttfb_ms)] += 1
1699:    perf = {"ttfb_p50_ms_by_model": {}, ...
1701:            "tokens_per_s": 0.0, "stalls_total": stalls_total,
```

证据②（已存 API 零消费——P3/P4：`/api/tpm_stats` handler :2279-2280 与 `/api/errors` handler :2292-2333 存在，dashboard JS 无任何 fetch 引用）：

```
$ grep -n 'tpm_stats\|/api/errors' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
613:def tpm_snapshot() -> dict:
614:    """TPM 状态快照（/api/tpm_stats 端点用）。
2279:        elif path == "/api/tpm_stats":
2292:        elif path == "/api/errors":
```

证据③（`_relay_sse` 无 model 上下文——P7 落点需签名透传，tracer 实测）：`_relay_sse`（:2069）签名 `(self, resp, final)`，函数体内零 model 引用；model 是 `_proxy_relay`（:1778）局部变量（:1781），调用点 :1877/:1917。

```
$ grep -n 'def _relay_sse\|self\._relay_sse(\|def _proxy_relay\|tpm_key = tpm_key_id\|tpm_qwait_ms = ' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
1778:    def _proxy_relay(self, started: float) -> None:
1785:        tpm_key = tpm_key_id(self.headers.get("Authorization"))
1787:        tpm_qwait_ms = None
1791:            tpm_status, tpm_qwait_ms = tpm_admit(tpm_key, tpm_est, model)
1877:                filtered, truncated = self._relay_sse(resp,
1917:                filtered, truncated = self._relay_sse(resp, final=True)
2069:    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
```

[R31-S2] 根因：daily 桶 schema 白名单机制（`_DAILY_FIELDS = DAILY_V2_FIELDS` :1216 + load 循环 :1230/:1260 逐字段 `.get(field)` 补 0）本身是双向 degrade 迁移层——16→20 扩字段无需版本号/独立 migration 代码；新增维度（daily_by_key）复用 `_prune_daily` value-agnostic prune 与 BY_MODEL_CAP 式 cap 截断即可。

```
$ sed -n '1204,1216p;1230,1233p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
# v2 P3 起用（P1 先声明）：daily/daily_by_model 16 字段 schema。
DAILY_V2_FIELDS = _DAILY_FIELDS + ("tokens_prompt", "tokens_completion",
                                   "bytes_out", "stream_requests",
                                   "ttfb_sum_ms", "ttfb_count",
                                   "outcome_ok", "outcome_degraded",
                                   "outcome_failed")
_DAILY_FIELDS = DAILY_V2_FIELDS
        for field in _DAILY_FIELDS:
            value = bucket.get(field)
            clean[field] = value if isinstance(value, int) and value >= 0 else 0
```

load 白名单按迭代时 `_DAILY_FIELDS` 清洗：旧 16 字段文件加载自动补 4 新键为 0；新 20 字段文件被旧版二进制读入自动丢 4 新键——双向 graceful degrade 已被既有注释（:1213-1215）与测试口径确认。
