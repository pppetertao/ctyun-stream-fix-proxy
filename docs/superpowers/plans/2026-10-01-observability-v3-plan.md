# PLAN — Observability v3（perf tab 十项 + token 统计七项）

> **来源 spec**：`docs/superpowers/specs/2026-10-01-observability-v3-design.md`（已 commit 5f7b2cb）
> **worktree**：`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3`（分支 `fix/observability-v3`）
> **baseline**：253 tests 全绿 @ main d07e0d6（2026-10-01 实测；spec/AC 中「229 口径」字样一律按 253 理解与书写）
> **总卡数**：9（卡 1-6、9 = tier A；卡 7-8 = tier B；无 C 卡——本项目纯 stdlib + unittest，全部断言可由单元测试 + 本地 fake-upstream 子进程集成测试达成，无真机/运行时数据/DI 实测依赖，不命中 C 档留白三判据）
> **卡序**：按依赖序串行：卡 1 无依赖 → 卡 2 ← 卡 1 → 卡 3 ← 卡 1/2 → 卡 4 ← 卡 1/2 → 卡 5 无依赖 → 卡 6 ← 卡 2/3/4 → 卡 7 ← 卡 6 → 卡 8 ← 卡 5/6/7 → 卡 9 ← 全卡
> **分段**：B1=卡 1-3；B2=卡 4-6；B3=卡 7-8；B4=卡 9；B-final=Self-Review 5 项。每段一次 dispatch，逐卡补全生产代码块 + 测试代码块 + 验证命令（本文件当前为 B0 骨架，卡内代码由 B1..Bn 填）。

## Global Constraints

1. **TDD 铁律（每卡）**：每卡先写该卡的失败测试（RED：仅该卡 AC 对应的新增/更新断言失败，其余绿），再改生产代码（GREEN），最后跑全量验证。禁止先实现后补测试。
2. **实现卡串行**：9 卡严格按依赖序逐卡执行，前卡 GREEN 才开下一卡；不并行改同一文件区域。
3. **锁序**：`TPM_LOCK → STATS_LOCK` 固定，**禁反向**（卡 4 的 `tpm_settle` 持 TPM_LOCK 内获取 STATS_LOCK，改动处必须写锁序注释；禁止在任何 STATS_LOCK 持锁路径上新增 TPM_LOCK 获取）。
4. **dashboard 渲染全 `textContent`，禁 `innerHTML`**：12 张新卡（及一切动态填充）必须走 `textContent`/`createElement`；`test_no_inner_html_anywhere` 全量扫描必须 0 命中（R8 XSS 门禁）。
5. **无死代码**：`usage_dict_tokens` 解析出的 5 元组每个字段必须落桶（cache_read→daily/dm 桶、reasoning→daily/dm 桶、cache_write 不解析但 schema 占位，见 Exclusions）；新 STATS 键全部有快照出口或持久化落点，不得有写了没人读的键。
6. **纯 stdlib，无新依赖**：生产与测试均只用标准库 + 现有文件内函数；不改 plist/launchd/deploy；不碰 worktree 内 spec 之外的文件。
7. **验证命令（每卡）**：
   ```bash
   cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
   /usr/bin/python3 ctyun-stream-fix-proxy.test.py
   ```
   项目无 lint/build 配置，verify 即全量 unittest。每卡 GREEN 判据：`Ran N tests ... OK`，0 failure / 0 error。卡 1 起 N=253+新增；最终卡 9 收尾全量 OK。
8. **baseline 红绿责任**：卡 1 前 executor 复跑 baseline 确认 253 OK；VERIFY 只对「baseline 绿 → 现在红」负责。R2 已知回归面（DAILY_V2_FIELDS 16→20 破坏的既有断言）由卡 1 同步更新，不允许任何卡以「已知会红」为借口跳过。
9. **Conventional Commits**：每卡一个 commit，`feat:`/`test:` 按卡内容取；一个逻辑改动一个 commit。本 PLAN 与卡内代码在 PLAN-B 阶段不 commit，commit 属 executor 执行阶段行为。
10. **LF 换行**：所有编辑保持 LF，禁止 CRLF。
11. **文件边界**：唯一修改两个文件——`ctyun-stream-fix-proxy.py`（生产）、`ctyun-stream-fix-proxy.test.py`（测试）。spec 决策留给 implementer 的开放项（见下）由填卡段定死后写入卡内。

## 开放决策（spec 留给 implementer 的开放项，填卡段定死）

| # | 开放项 | spec 出处 | 定调责任段 |
|---|--------|-----------|-----------|
| O1 | dashboard 新卡并入既有常量段 vs 新增 `_DASH_SECTIONS_V3`/`_DASH_JS_V3`（join 等式断言 `test_segment_constants_present_and_join` 必须同步） | spec Files :56（:3488-3492） | B3 填卡 7 时定死并写进卡 7/8 |
| O2 | AC10 的 12 张新卡关键 id/文案清单（spec 声明「implementer 定 id 清单」） | spec AC10 | B3 填卡 7/8 时定死 |

---

## 任务卡清单（B0 骨架——仅 tier/文件/接口签名/验收，代码由 B1..Bn 填）

### 卡 1｜常量与 schema 迁移 + usage 五元组解析（tier A）

- **目标**：DAILY_V2_FIELDS 16→20 字段（含 cache_write 占位）；新增 4 个容量常量与 `_DAILY_BY_KEY_FIELDS`；`usage_dict_tokens` 返回 3 元组改 5 元组并新增 `_int_nested` 辅助；同步更新既有 16 字段硬断言。
- **文件**：`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：
  - 常量区（:48-49 后）：`BY_KEY_CAP = 64`、`QWAIT_RING_MAX = 500`、`SETTLE_RING_MAX = 500`、`HOURLY_RING_MAX = 49`
  - `DAILY_V2_FIELDS`（:1207-1211）：16 元组 → 20 元组，追加 `("tokens_cache_read", "tokens_cache_write", "tokens_reasoning", "requests_zero_token")`
  - 新增 `_DAILY_BY_KEY_FIELDS = ("requests", "tokens_prompt", "tokens_completion", "tokens_cache_read", "tokens_cache_write", "tokens_reasoning", "bytes_out", "stream_requests")`（8 字段，决策 2）
  - `usage_dict_tokens(usage) -> tuple`（:303-319）：`(prompt, completion, total)` → `(prompt, completion, total, cache_read, reasoning)`；新增模块级 `_int_nested(usage, parent_key, child_key) -> int`（非 dict → 0）；cache_read 三级别名序 `prompt_cache_hit_tokens` → `cache_read_input_tokens` → `prompt_tokens_details.cached_tokens`；reasoning 两级 `completion_tokens_details.reasoning_tokens` → `reasoning_tokens`（决策 8）
  - `sse_line_extract_usage(line)`（:322-332）：返回值随 5 元组同步（docstring 更新）
  - 零改动确认（不写码）：`_prune_daily`（:1052-1060）、`load_daily_buckets`/`load_daily_by_model_buckets`（:1219-1265）白名单自动覆盖 20 字段
- **验收**：AC1、AC3；既有 253 tests 更新断言后全绿（RED 面：P1ConstantsTest 的 DAILY_V2_FIELDS 16 元组字面断言、:1750/:1771/:1782 的 "16 fields" 消息、DailyV2CompatTest 注释/消息与 downgrade 用例 9→13 新字段措辞）。
- **依赖**：无

### 卡 2｜STATS 新键 + `_record_request` 扩展（tier A）

- **目标**：STATS 初始化追加 6 个新键；`_record_request` 签名加 4 kwargs，锁内新增 5 组写入（daily_by_key、phase_ms_by_model 双写、qwait 环、hourly_tokens 滚动、cache_read/reasoning/zero_token 桶增量）。
- **文件**：`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：
  - `STATS`（:861-871）追加：`"daily_by_key": {}`、`"phase_ms_by_model": {}`、`"stalls_by_model": {}`、`"qwait_ms_by_model": {}`、`"tpm_settle_ratio_by_model": {}`、`"hourly_tokens": collections.deque(maxlen=HOURLY_RING_MAX)`
  - `_record_request(...)`（:1484-1597）签名追加：`tokens_cache_read: int = 0`、`tokens_reasoning: int = 0`、`qwait_ms=None`、`key_id12=None`
  - 锁内新写入（spec Files :46）：① dm/daily 桶 `tokens_cache_read`/`tokens_reasoning`（`> 0` 守卫同 :1524-1527 式）与 `requests_zero_token`（tokens 为 None/0 且 status<500 且非 error 时 +1，T5 口径）；② `daily_by_key` 更新（`setdefault(today_key())` + `len < BY_KEY_CAP` 才建 `dict.fromkeys(_DAILY_BY_KEY_FIELDS, 0)` entry）；③ `phase_ms_by_model` 双写（:1578-1581 全局循环旁，cap BY_MODEL_CAP）；④ `qwait_ms_by_model[model].append(qwait_ms)`（非 None 且 model 存在）；⑤ `hourly_tokens` 小时滚动（当前小时首请求建条目 `{"hour_start_ts", "tokens_prompt", "tokens_completion"}`，跨小时 append 新条目）
- **验收**：AC2（写入侧）、AC4（phase 双写部分）、AC6、AC7
- **依赖**：卡 1

### 卡 3｜`_relay_sse` model kwarg + `_proxy_relay` 结算点透传（tier A）

- **目标**：`_relay_sse` 签名加 `model=None` kwarg（决策 3）；stall 自增处写 per-model；`_proxy_relay` 流式与非流式两结算点补传 4 kwargs 与 `model=model`。
- **文件**：`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：
  - `_relay_sse(self, resp, final, model=None) -> tuple`（:2069，model 放 final 后，默认 None 保持旧两参调用兼容）
  - 调用点 :1877 与 :1917（`_proxy_relay` 内）：`self._relay_sse(resp, ..., model=model)`（model 为 :1781 局部变量）
  - stall 自增处（:2151-2156）：同锁内 `if model is not None` 守卫 + cap 后 `STATS["stalls_by_model"][model] += 1`；`stalls_total += 1` 保留
  - 流式结算点（:1967 `_record_request` 调用）：补 `tokens_cache_read=p3_tokens[3] if p3_tokens else 0`、`tokens_reasoning=p3_tokens[4] if p3_tokens else 0`、`qwait_ms=tpm_qwait_ms`、`key_id12=tpm_key[:12] if tpm_key else None`
  - 非流式结算点（:2012-2022 `_record_request` 调用）：同上（p3_buf_tokens/tpm_qwait_ms/tpm_key 同域局部变量）
- **验收**：AC4（stalls 双路径：传 model 时 per-model+全局同增，model=None 只增全局）
- **依赖**：卡 1、卡 2

### 卡 4｜`tpm_settle` 比率记录 + daily_by_key 持久化（tier A）

- **目标**：`tpm_settle` 结算时记录 actual/est 比率（T4 数据源，锁序注释）；`save_stats_counters` 加 daily_by_key 第三路深拷 + prune；新增 `load_daily_by_key_buckets`。
- **文件**：`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：
  - `tpm_settle(key_id, est, actual, model) -> None`（:591-610）：`delta != 0` 分支后，`if est > 0 and actual >= 0:` append `actual / est` 到 `STATS["tpm_settle_ratio_by_model"][model]`；TPM_LOCK 内获取 STATS_LOCK（锁序注释 TPM_LOCK→STATS_LOCK）
  - `save_stats_counters(path)`（:1077-1107）：:1084 后加 `_prune_daily(STATS["daily_by_key"])`；:1086-1087 后加 daily_by_key 深拷 `{d: {k: dict(v) for k, v in keys.items()} for d, keys in ...}`；`json.dump` 载荷加 `daily_by_key=daily_by_key`
  - 新增 `load_daily_by_key_buckets(path) -> dict`（:1266 之后）：仿 :1237-1265 外层 ISO round-trip 校验、内层 key 非空 str ≤12 hex 字符、`len(bucket) >= BY_KEY_CAP` 截断、`_DAILY_BY_KEY_FIELDS` 8 字段白名单清洗
- **验收**：AC5（settle 比率部分）、AC2（save 落盘含 daily_by_key 键 + load 缺键/损坏/非 ISO/超 cap 容错）
- **依赖**：卡 1、卡 2

### 卡 5｜probe history 二元组 + `/api/probe_history`（tier A）

- **目标**：history deque 条目标量 ms → `(ts, ms)` 二元组并同步全部消费点；新增 `probe_history_snapshot()`（降采样 ≤200 点）；`do_GET` 加新端点路由。
- **文件**：`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：
  - `_PROBE_STATE["history"]`（:884）条目形态 `ms` → `(ts, ms)`；append 点（:1390 成功分支）同步 `state["history"].append((time.time(), round(latency_ms, 1)))`
  - `_latency_p90(samples)`（:1304-1310）与 `_probe_spike_due(history, factor)`（:1313-1324）：二元组消费适配（`for _, ms in history` 解包，排序样本取 ms；返回值语义与旧标量版等价，AC8 回归断言兜底）
  - 新增 `probe_history_snapshot() -> dict`（:1425 之后）：PROBE_LOCK 内取 history 浅拷贝，`step = max(1, len // 200)` 均匀抽稀，返回 `{"points": [[ts, ms], ...], "count_total": n}`；`probe_state_snapshot` 6 键契约不动
  - `do_GET`（:2271-2291）：`/api/health` 分支后加 `elif path == "/api/probe_history": self._send_json(200, probe_history_snapshot())`
- **验收**：AC8、AC9（`/api/probe_history` 200 + `points`/`count_total`）
- **依赖**：无（PROBE 域独立；与卡 1-4 无代码交叉）

### 卡 6｜`stats_snapshot` 扩展（tier A）

- **目标**：`stats_snapshot` 把全部新键挂进快照：顶层 `daily_by_key`/`hourly_tokens`，perf 子树 per-model 分位键（P1/P6/P7/P8/T4 前端数据源），推广 `_percentile` 辅助。
- **文件**：`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：
  - 新增 `_percentile(samples, q) -> float`：`_latency_p90` 算法推广（最近秩法 `idx = int(q * (n - 1))`，空序列 0.0）
  - `stats_snapshot() -> dict`（:1676-1720）：浅拷区（:1679-1689）加 `snap["daily_by_key"]` 深拷（两层 dict 拷贝）、`qwait/settle` deque 转 list；perf 节（:1699-1719）追加 `phase_p50_ms_by_model`/`phase_p90_ms_by_model`（per-model 三阶段分位）、`stalls_by_model`（dict 直出）、`qwait_p50_ms_by_model`/`qwait_p90_ms_by_model`（`_percentile`）、`tpm_settle_ratio_p50_by_model`/`tpm_settle_ratio_p90_by_model`；`perf["ttfb_hist_by_model"] = ttfb_hist` 原样浅拷；`snap["hourly_tokens"] = list(...)`
- **验收**：AC9（`/api/stats` 含 `daily_by_key`/`hourly_tokens`/`perf.qwait_p50_ms_by_model`/`perf.stalls_by_model`/`perf.ttfb_hist_by_model`）、AC5（qwait/settle 分位与手工最近秩一致）
- **依赖**：卡 2、卡 3、卡 4（新 STATS 键全部有写入源后 snapshot 才可断言非空）

### 卡 7｜Dashboard overview/tokens 区（T1/T2/T3/T5/T7）（tier B）

- **目标**：overview/tokens 区 5 张卡 + token 按天×模型表扩列：T1 per-key token 表、T2 cache_read 列、T3 reasoning 列、T5 0-token 聚合卡、T7 月末配额投影卡（纯前端）。HTML/JS 全部 `textContent` 渲染。定调 O1（常量段并入 vs 新增，join 等式同步）与 O2 中本卡 id 清单。
- **文件**：`ctyun-stream-fix-proxy.py`（`_DASH_SECTIONS_*`/`_DASH_JS_*` 常量，:2628 起）、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：dashboard 常量段（HTML 卡容器 id + 对应 `_DASH_JS_*` 渲染函数）；数据源 `snap.daily_by_key`（当日）、`range_stats` mtd、token 按天×模型表 daily 桶 `tokens_cache_read`/`tokens_reasoning`、`tokens_cache_write` 列（T2 占位列直出，解析为 0）
- **验收**：AC10（overview/tokens 部分：T1 表 id、扩列 th、0-token 卡、投影卡；`test_no_inner_html_anywhere` 扩展覆盖后 0 命中；若新增常量段则 `test_segment_constants_present_and_join` 等式同步）
- **依赖**：卡 6

### 卡 8｜Dashboard perf tab 区（P1-P10）（tier B）

- **目标**：perf tab 10 张卡按「延迟 / 错误与限流 / 流量与 token / 健康」四组布局（决策 9）：P1 按模型 TTFB 直方图、P2 错误/重试率卡、P3 TPM 限流观测（fetch `/api/tpm_stats`）、P4 错误事件流（fetch `/api/errors`，`?id=` 鉴权）、P5 流量画像、P6 三阶段按模型分位表、P7 stalls 按模型卡、P8 qwait 分位卡、P9 慢请求 Top N（前端降序取 10）、P10 probe 趋势线（fetch `/api/probe_history`）。全部 `textContent` 渲染。定调 O2 中本卡 id 清单。
- **文件**：`ctyun-stream-fix-proxy.py`（`_DASH_SECTIONS_*`/`_DASH_JS_*` 常量，:2628 起）、`ctyun-stream-fix-proxy.test.py`
- **接口签名**：dashboard 常量段（10 卡容器 id + 渲染函数）；数据源 `perf.ttfb_hist_by_model`（9 桶）、`perf.phase_p*_ms_by_model`、`perf.stalls_by_model`、`perf.qwait_p*_ms_by_model`、`perf.tpm_settle_ratio_p*_by_model`、`snap.hourly_tokens`（SVG 曲线）、`snap.recent`（慢请求）、`/api/tpm_stats`、`/api/errors`、`/api/probe_history`
- **验收**：AC10（perf 部分：10 卡关键 id/文案、fetch 引用；`test_no_inner_html_anywhere` 0 命中）
- **依赖**：卡 5、卡 6、卡 7（卡 7 定调的常量段组织方式为卡 8 编辑前提）

### 卡 9｜测试收尾与 AC 全量核对（tier A）

- **目标**：补 `ObservabilityV3Test` 汇总类（若前卡未覆盖的 AC 残余）、复核 R2 回归面全部同步、whole-branch 全量验证与 AC1-AC11 逐条核对清单落账。
- **文件**：`ctyun-stream-fix-proxy.test.py`（`ObservabilityV3Test` 类 + 受影响用例）
- **接口签名**：无生产代码改动（仅测试）；如发现残余生产缺口 → STOP 上报回 PLAN，不在本卡临时扩 scope
- **验收**：AC11（全量 `Ran N tests ... OK`；特别核对 test.py :1632/:1637/:1657/:1697/:1749 随常量走的断言、:1750/:1771/:1782 消息已改 20、:4715/:4720 消息、DailyV2CompatTest 措辞、:1566 cap、:2054/:2092 prune、`test_model_list_auto_generated`）
- **依赖**：卡 1-8

---

## 分段计划（后续 dispatch）

| 段 | 内容 | 产出 |
|----|------|------|
| B1 | 卡 1-3 逐卡补全（每卡：Edit 锚点实测、完整生产代码块、完整测试代码块、验证命令、预期 RED/GREEN 测试数） | PLAN.md 卡 1-3 代码填满 |
| B2 | 卡 4-6 逐卡补全 | PLAN.md 卡 4-6 代码填满 |
| B3 | 卡 7-8 逐卡补全（含 O1/O2 定调写入） | PLAN.md 卡 7-8 代码填满 |
| B4 | 卡 9 补全 | PLAN.md 卡 9 代码填满 |
| B-final | Self-Review 5 项跨全卡统查（spec coverage / placeholder scan / type consistency / 可落盘性 / 锚点实测），就地修正 | PLAN.md 定稿 |

> B0 本段不 commit；填卡段与 B-final 亦不 commit（PLAN-B 阶段只写文档，commit 是 executor 执行阶段行为）。

---

# B1 段：卡 1-3 填码

## 卡 1｜常量与 schema 迁移 + usage 五元组解析（tier A）— 代码

### 目标

DAILY_V2_FIELDS 16→20 字段；新增 BY_KEY_CAP/QWAIT_RING_MAX/SETTLE_RING_MAX/HOURLY_RING_MAX 与 `_DAILY_BY_KEY_FIELDS`（8 字段）；`usage_dict_tokens` 3→5 元组 + `_int_nested` 辅助；`sse_line_extract_usage` 返回值注释同步；既有 16 字段硬断言全部更新为 20。

### 文件

`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

### TDD 顺序

**Step 1 — 失败测试（仅编辑 test.py，预期 RED）**  
**Step 2 — 实现（编辑 proxy.py，预期 GREEN）**

---

### Step 1：失败测试（RED）

#### 改动 T1.1 — P1ConstantsTest.test_v2_constants_declared：16→20 元组字面断言

**old_string**（锚：test.py :4450-4459，全文件唯一——字面 16 字段 tuple）：

```
        # v2 P3 起 _DAILY_FIELDS 即 DAILY_V2_FIELDS（16 字段，同一 tuple）：
        # 契约按 16 字段字面精确断言，不再写成 "旧 7 字段 + 9 新字段" 的增量式。
        self.assertEqual(
            mod.DAILY_V2_FIELDS,
            ("requests", "filtered", "errors_proxy", "errors_upstream",
             "retries", "eof_without_done", "header_retries",
             "tokens_prompt", "tokens_completion",
             "bytes_out", "stream_requests",
             "ttfb_sum_ms", "ttfb_count",
             "outcome_ok", "outcome_degraded", "outcome_failed"))
```

**new_string**：

```
        # v2 P3 起 _DAILY_FIELDS 即 DAILY_V2_FIELDS（v3 扩 20 字段，同一 tuple）：
        # 契约按 20 字段字面精确断言。
        self.assertEqual(
            mod.DAILY_V2_FIELDS,
            ("requests", "filtered", "errors_proxy", "errors_upstream",
             "retries", "eof_without_done", "header_retries",
             "tokens_prompt", "tokens_completion",
             "bytes_out", "stream_requests",
             "ttfb_sum_ms", "ttfb_count",
             "outcome_ok", "outcome_degraded", "outcome_failed",
             "tokens_cache_read", "tokens_cache_write",
             "tokens_reasoning", "requests_zero_token"))
```

#### 改动 T1.2 — test.py:1750 断言消息 16→20 fields

**old_string**（锚：test.py :1750，"16 fields" 全文件唯一）：

```
            "dm entry shape must stay in sync across both creation sites (16 fields)")
```

**new_string**：

```
            "dm entry shape must stay in sync across both creation sites (20 fields)")
```

#### 改动 T1.3 — test.py:1771-1782 站点 2 注释与消息

**old_string**（锚：test.py :1771-1782，含 "16 字段" + "(16 fields)" 组合）：

```
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 16 字段（DAILY_V2_FIELDS）——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            set(mod.DAILY_V2_FIELDS),
            "empty-retry creation site must keep dm entry shape in sync (16 fields)")
```

**new_string**：

```
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 20 字段（DAILY_V2_FIELDS）——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            set(mod.DAILY_V2_FIELDS),
            "empty-retry creation site must keep dm entry shape in sync (20 fields)")
```

#### 改动 T1.4 — DailyV2CompatTest：docstring、消息、方法名、降级措辞（9→13）

**Edit T1.4a** — docstring 16→20（old: test.py :4694-4695，新全文替换）：

```
class DailyV2CompatTest(unittest.TestCase):
    """P3 Token期：daily_by_model v2 向后兼容双向 degrade（R2 锁死）。

    旧 7 字段 JSON → 16 字段内存桶零值补齐；
    新 16 字段 JSON → 7 字段加载函数（模拟旧版二进制）只取 7 字段不崩。
    """
```
→
```
class DailyV2CompatTest(unittest.TestCase):
    """P3 Token期：daily_by_model v2 向后兼容双向 degrade（R2 锁死）。

    旧 7 字段 JSON → 20 字段内存桶零值补齐；
    新 20 字段 JSON → 7 字段加载函数（模拟旧版二进制）只取 7 字段不崩。
    """
```

**Edit T1.4b** — 消息 "16-field zero-fill" → "20-field zero-fill"（2 处）：

1. old: `"legacy 7-field bucket must load with 16-field zero-fill")` → new: `"legacy 7-field bucket must load with 20-field zero-fill")`
2. old: `"legacy 7-field dm entry must load with 16-field zero-fill")` → new: `"legacy 7-field dm entry must load with 20-field zero-fill")`

**Edit T1.4c** — 降级测试方法名与 docstring 16→20 + 消息 9→13：

old_string:
```
    def test_new_16_field_downgrade_reads_7_fields_without_crash(self) -> None:
        """降级演练：16 字段 JSON 被'旧版 7 字段白名单'加载函数读入 → 只返 7 字段不崩。"""
```
new_string:
```
    def test_new_20_field_downgrade_reads_7_fields_without_crash(self) -> None:
        """降级演练：20 字段 JSON 被'旧版 7 字段白名单'加载函数读入 → 只返 7 字段不崩。"""
```

old_string:
```
                         "old 7-field whitelist must silently drop the 9 new fields")
```
new_string:
```
                         "old 7-field whitelist must silently drop the 13 new fields")
```

**Edit T1.4d** — roundtrip 测试方法名与 docstring + 消息 16→20：

old_string:
```
    def test_roundtrip_16_field_full_equality(self) -> None:
        """16 字段桶 save→load 全等（含新字段值）。"""
```
new_string:
```
    def test_roundtrip_full_equality(self) -> None:
        """20 字段桶 save→load 全等（含新字段值）。"""
```

old_string:
```
                             "16-field buckets must roundtrip verbatim")
```
new_string:
```
                             "20-field buckets must roundtrip verbatim")
```

#### 改动 T1.5 — UsageExtractTest 全类替换（8 方法更新 + 7 新方法）

**old_string**（锚：从 `class UsageExtractTest(unittest.TestCase):` 到 test_has_usage 末行，即 test.py :4462-4526）：

```
class UsageExtractTest(unittest.TestCase):
    """P3 Token期：usage 帧数值抽取矩阵（标准/缺 prompt/缺 completion/extra key/非 dict/空 usage/负值）。"""

    @classmethod
    def setUpClass(cls) -> None:
        # staticmethod：模块级函数赋类属性默认成 method descriptor，
        # self.extract(line) 会多传 cls 导致 TypeError。
        cls.extract = staticmethod(load_proxy_module().sse_line_extract_usage)

    def test_standard_usage_frame(self) -> None:
        line = (b'data: {"id":"u","choices":[],"usage":'
                b'{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n')
        self.assertEqual(self.extract(line), (10, 5, 15))

    def test_missing_prompt_tokens_defaults_zero(self) -> None:
        line = b'data: {"usage":{"completion_tokens":5,"total_tokens":10}}\n\n'
        self.assertEqual(self.extract(line), (0, 5, 10))

    def test_missing_completion_tokens_defaults_zero(self) -> None:
        line = b'data: {"usage":{"prompt_tokens":3,"total_tokens":8}}\n\n'
        self.assertEqual(self.extract(line), (3, 0, 8))

    def test_extra_key_ignored(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"extra":"x","nested":{"a":1}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3))

    def test_non_dict_returns_none(self) -> None:
        self.assertIsNone(self.extract(b"data: [DONE]\n\n"))
        self.assertIsNone(self.extract(b'data: "just a string"\n\n'))
        self.assertIsNone(self.extract(b"not even a data line\n\n"))
        self.assertIsNone(self.extract(b"data: null\n\n"))
        self.assertIsNone(self.extract(b"data: 42\n\n"))

    def test_empty_usage_dict_returns_none(self) -> None:
        # 委托函数 sse_line_usage 对空 usage dict（{} 为 falsy）返回 None——与
        # sse_line_has_usage 的 bool(usage) 判据同源；extract 透传该 None 语义。
        line = b'data: {"usage":{}}\n\n'
        self.assertIsNone(self.extract(line))

    def test_negative_tokens_coerced_zero(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":-1,"completion_tokens":5,'
                b'"total_tokens":10}}\n\n')
        self.assertEqual(self.extract(line), (0, 5, 10))

    def test_non_int_fields_coerced_zero(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":"9","completion_tokens":null,'
                b'"total_tokens":true}}\n\n')
        self.assertEqual(self.extract(line), (0, 0, 0))

    def test_ride_on_finish_frame_usage_extracted(self) -> None:
        line = (b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}\n\n')
        self.assertEqual(self.extract(line), (7, 2, 9))

    def test_has_usage_bool_consistent_with_extract(self) -> None:
        """与 sse_line_has_usage 布尔判据一致性：has_usage=True ⟺ extract 非 None。"""
        mod = load_proxy_module()
        for line in (SSE_USAGE,
                     b'data: {"usage":{}}\n\n',
                     b"data: [DONE]\n\n",
                     b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'):
            self.assertEqual(mod.sse_line_has_usage(line),
                             mod.sse_line_extract_usage(line) is not None,
                             "has_usage/extract must agree on %r" % line)
```

**new_string**：

```
class UsageExtractTest(unittest.TestCase):
    """P3 Token期：usage 帧数值抽取矩阵（标准/缺 prompt/缺 completion/extra key/非 dict/空 usage/负值）；
    v3 扩 5 元组（cache_read/reasoning 兜底 0）。"""

    @classmethod
    def setUpClass(cls) -> None:
        # staticmethod：模块级函数赋类属性默认成 method descriptor，
        # self.extract(line) 会多传 cls 导致 TypeError。
        cls.extract = staticmethod(load_proxy_module().sse_line_extract_usage)

    def test_standard_usage_frame(self) -> None:
        line = (b'data: {"id":"u","choices":[],"usage":'
                b'{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n')
        self.assertEqual(self.extract(line), (10, 5, 15, 0, 0))

    def test_missing_prompt_tokens_defaults_zero(self) -> None:
        line = b'data: {"usage":{"completion_tokens":5,"total_tokens":10}}\n\n'
        self.assertEqual(self.extract(line), (0, 5, 10, 0, 0))

    def test_missing_completion_tokens_defaults_zero(self) -> None:
        line = b'data: {"usage":{"prompt_tokens":3,"total_tokens":8}}\n\n'
        self.assertEqual(self.extract(line), (3, 0, 8, 0, 0))

    def test_extra_key_ignored(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"extra":"x","nested":{"a":1}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 0))

    def test_non_dict_returns_none(self) -> None:
        self.assertIsNone(self.extract(b"data: [DONE]\n\n"))
        self.assertIsNone(self.extract(b'data: "just a string"\n\n'))
        self.assertIsNone(self.extract(b"not even a data line\n\n"))
        self.assertIsNone(self.extract(b"data: null\n\n"))
        self.assertIsNone(self.extract(b"data: 42\n\n"))

    def test_empty_usage_dict_returns_none(self) -> None:
        # 委托函数 sse_line_usage 对空 usage dict（{} 为 falsy）返回 None——与
        # sse_line_has_usage 的 bool(usage) 判据同源；extract 透传该 None 语义。
        line = b'data: {"usage":{}}\n\n'
        self.assertIsNone(self.extract(line))

    def test_negative_tokens_coerced_zero(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":-1,"completion_tokens":5,'
                b'"total_tokens":10}}\n\n')
        self.assertEqual(self.extract(line), (0, 5, 10, 0, 0))

    def test_non_int_fields_coerced_zero(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":"9","completion_tokens":null,'
                b'"total_tokens":true}}\n\n')
        self.assertEqual(self.extract(line), (0, 0, 0, 0, 0))

    def test_ride_on_finish_frame_usage_extracted(self) -> None:
        line = (b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}\n\n')
        self.assertEqual(self.extract(line), (7, 2, 9, 0, 0))

    def test_has_usage_bool_consistent_with_extract(self) -> None:
        """与 sse_line_has_usage 布尔判据一致性：has_usage=True ⟺ extract 非 None。"""
        mod = load_proxy_module()
        for line in (SSE_USAGE,
                     b'data: {"usage":{}}\n\n',
                     b"data: [DONE]\n\n",
                     b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'):
            self.assertEqual(mod.sse_line_has_usage(line),
                             mod.sse_line_extract_usage(line) is not None,
                             "has_usage/extract must agree on %r" % line)

    # ---- v3：cache_read / reasoning 五元组第 4/5 位解析矩阵 ----

    def test_cache_read_hit_tokens_primary_alias(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_cache_hit_tokens":4}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 4, 0))

    def test_cache_read_input_tokens_alias(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"cache_read_input_tokens":40}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 40, 0))

    def test_cache_read_nested_cached_tokens(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_tokens_details":'
                b'{"cached_tokens":400}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 400, 0))

    def test_cache_read_alias_priority_first_hit_wins(self) -> None:
        # 多级别名同时存在 → 首个命中（prompt_cache_hit_tokens）优先
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_cache_hit_tokens":4,'
                b'"cache_read_input_tokens":40,"prompt_tokens_details":'
                b'{"cached_tokens":400}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 4, 0))

    def test_reasoning_nested_completion_details(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"completion_tokens_details":'
                b'{"reasoning_tokens":5}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 5))

    def test_reasoning_top_level(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"reasoning_tokens":50}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 50))

    def test_cache_read_non_int_nested_coerced_zero(self) -> None:
        # prompt_tokens_details 非 dict → cache_read 归 0
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_tokens_details":"oops"}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 0))
```

#### 改动 T1.6 — 新增 SchemaV3MigrationTest 类（插在 DailyV2CompatTest 之后、RequestIdTest 之前）

**old_string**（锚：test.py :4791-4793，DailyV2CompatTest 类末行 + 空行 + RequestIdTest 类头）：

```
        self.assertEqual(f(mod.CLASS_REQUEST_FAULT), "failed")


class RequestIdTest(unittest.TestCase):
```

**new_string**：

```
        self.assertEqual(f(mod.CLASS_REQUEST_FAULT), "failed")


class SchemaV3MigrationTest(unittest.TestCase):
    """v3 schema 迁移：DAILY_V2_FIELDS 16→20 双向 degrade，daily_by_key 8 字段常量。"""

    def test_daily_v2_fields_count_20_and_order(self) -> None:
        mod = load_proxy_module()
        self.assertEqual(len(mod.DAILY_V2_FIELDS), 20)
        self.assertEqual(
            mod.DAILY_V2_FIELDS,
            ("requests", "filtered", "errors_proxy", "errors_upstream",
             "retries", "eof_without_done", "header_retries",
             "tokens_prompt", "tokens_completion",
             "bytes_out", "stream_requests",
             "ttfb_sum_ms", "ttfb_count",
             "outcome_ok", "outcome_degraded", "outcome_failed",
             "tokens_cache_read", "tokens_cache_write",
             "tokens_reasoning", "requests_zero_token"))

    def test_legacy_16_field_persist_loads_with_4_zero_fill(self) -> None:
        """AC1：写一份 16 字段 legacy persist（无 4 新键）→ load 后每桶 20 键、4 新键 0。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3schema-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        legacy16 = ("requests", "filtered", "errors_proxy", "errors_upstream",
                    "retries", "eof_without_done", "header_retries",
                    "tokens_prompt", "tokens_completion",
                    "bytes_out", "stream_requests",
                    "ttfb_sum_ms", "ttfb_count",
                    "outcome_ok", "outcome_degraded", "outcome_failed")
        entry = dict.fromkeys(legacy16, 0)
        entry.update({"requests": 5, "tokens_prompt": 11, "stream_requests": 2})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": {"2026-01-02": entry},
                                 "daily_by_model": {"2026-01-02": {"m1": entry}}}},
                      fh)
        buckets = mod.load_daily_buckets(path)
        self.assertEqual(len(buckets["2026-01-02"]), 20,
                         "legacy 16-field file must load as 20-field bucket")
        self.assertEqual(set(buckets["2026-01-02"]), set(mod.DAILY_V2_FIELDS))
        for f in ("tokens_cache_read", "tokens_cache_write",
                  "tokens_reasoning", "requests_zero_token"):
            self.assertEqual(buckets["2026-01-02"][f], 0,
                             "%s must zero-fill from legacy 16-field file" % f)
        self.assertEqual(buckets["2026-01-02"]["tokens_prompt"], 11)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(len(dbm["2026-01-02"]["m1"]), 20)
        self.assertEqual(set(dbm["2026-01-02"]["m1"]), set(mod.DAILY_V2_FIELDS))
        self.assertEqual(dbm["2026-01-02"]["m1"]["tokens_cache_read"], 0)

    def test_new_20_field_downgrade_reads_16_without_crash(self) -> None:
        """AC1 反向：20 字段被'旧版 16 字段白名单'加载丢 4 新键不崩。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3downgrade-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        entry20 = dict.fromkeys(mod.DAILY_V2_FIELDS, 0)
        entry20.update({"requests": 4, "tokens_cache_read": 9,
                        "tokens_reasoning": 7, "requests_zero_token": 1})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "2026-01-02": {"m1": entry20}}}}, fh)
        legacy_16 = ("requests", "filtered", "errors_proxy", "errors_upstream",
                     "retries", "eof_without_done", "header_retries",
                     "tokens_prompt", "tokens_completion",
                     "bytes_out", "stream_requests",
                     "ttfb_sum_ms", "ttfb_count",
                     "outcome_ok", "outcome_degraded", "outcome_failed")

        def legacy_loader(raw):
            return {k: raw.get(k, 0) for k in legacy_16}

        raw_entry = json.loads(open(path, encoding="utf-8").read())[
            "stats"]["daily_by_model"]["2026-01-02"]["m1"]
        out = legacy_loader(raw_entry)
        self.assertEqual(out["requests"], 4)
        self.assertEqual(set(out), set(legacy_16),
                         "old 16-field whitelist must silently drop the 4 new fields")
        self.assertNotIn("tokens_cache_read", out)

    def test_daily_by_key_fields_constant_8(self) -> None:
        mod = load_proxy_module()
        # 用 getattr 防 AttributeError——RED 期常量未定义时干净 FAIL
        self.assertEqual(
            getattr(mod, "_DAILY_BY_KEY_FIELDS", None),
            ("requests", "tokens_prompt", "tokens_completion",
             "tokens_cache_read", "tokens_cache_write",
             "tokens_reasoning", "bytes_out", "stream_requests"))

    def test_v3_capacity_constants(self) -> None:
        mod = load_proxy_module()
        self.assertEqual(getattr(mod, "BY_KEY_CAP", None), 64)
        self.assertEqual(getattr(mod, "QWAIT_RING_MAX", None), 500)
        self.assertEqual(getattr(mod, "SETTLE_RING_MAX", None), 500)
        self.assertEqual(getattr(mod, "HOURLY_RING_MAX", None), 49)


class RequestIdTest(unittest.TestCase):
```

#### 改动 T1.7 — ProxyDashboardUnitTest.test_daily_by_model_persist_roundtrip：matrix/expected 字面量扩 20 字段

**old_string**（锚：test.py :1897-1923，全文件唯一的 3 日期 × 16 字段 matrix 字面量）：

```
            matrix = {
                "2026-01-02": {
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0}}}
```

**new_string**（每 entry 追加 4 零值字段：`"tokens_cache_read": 0, "tokens_cache_write": 0, "tokens_reasoning": 0, "requests_zero_token": 0`）：

```
            matrix = {
                "2026-01-02": {
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0,
                           "tokens_cache_read": 0, "tokens_cache_write": 0,
                           "tokens_reasoning": 0, "requests_zero_token": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0,
                           "tokens_cache_read": 0, "tokens_cache_write": 0,
                           "tokens_reasoning": 0, "requests_zero_token": 0}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0,
                           "tokens_cache_read": 0, "tokens_cache_write": 0,
                           "tokens_reasoning": 0, "requests_zero_token": 0}}}
```

**Edit T1.7b** — 同方法（`test_daily_by_model_persist_roundtrip`）用例 3 损坏结构的 expected dict（test.py :1948-1960）：

**old_string**：

```
        self.assertEqual(mod.load_daily_by_model_buckets(path),
                         {"2026-01-02": {"m2": {"requests": 5, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 2, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0,
                                                "tokens_prompt": 0,
                                                "tokens_completion": 0,
                                                "bytes_out": 0, "stream_requests": 0,
                                                "ttfb_sum_ms": 0, "ttfb_count": 0,
                                                "outcome_ok": 0, "outcome_degraded": 0,
                                                "outcome_failed": 0}}},
                         "non-dict bucket/entry must be skipped; bad fields coerced to 0")
```

**new_string**：

```
        self.assertEqual(mod.load_daily_by_model_buckets(path),
                         {"2026-01-02": {"m2": {"requests": 5, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 2, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0,
                                                "tokens_prompt": 0,
                                                "tokens_completion": 0,
                                                "bytes_out": 0, "stream_requests": 0,
                                                "ttfb_sum_ms": 0, "ttfb_count": 0,
                                                "outcome_ok": 0, "outcome_degraded": 0,
                                                "outcome_failed": 0,
                                                "tokens_cache_read": 0,
                                                "tokens_cache_write": 0,
                                                "tokens_reasoning": 0,
                                                "requests_zero_token": 0}}},
                         "non-dict bucket/entry must be skipped; bad fields coerced to 0")
```

**Edit T1.7c** — 同方法用例 4 缺字段补齐的 expected dict（test.py :1972-1981）：

**old_string**：

```
        self.assertEqual(dbm["2026-01-03"]["m1"],
                         {"requests": 1, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0, "eof_without_done": 0,
                          "header_retries": 0,
                          "tokens_prompt": 0, "tokens_completion": 0,
                          "bytes_out": 0, "stream_requests": 0,
                          "ttfb_sum_ms": 0, "ttfb_count": 0,
                          "outcome_ok": 0, "outcome_degraded": 0,
                          "outcome_failed": 0},
                         "missing fields must be filled with 0")
```

**new_string**：

```
        self.assertEqual(dbm["2026-01-03"]["m1"],
                         {"requests": 1, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0, "eof_without_done": 0,
                          "header_retries": 0,
                          "tokens_prompt": 0, "tokens_completion": 0,
                          "bytes_out": 0, "stream_requests": 0,
                          "ttfb_sum_ms": 0, "ttfb_count": 0,
                          "outcome_ok": 0, "outcome_degraded": 0,
                          "outcome_failed": 0,
                          "tokens_cache_read": 0, "tokens_cache_write": 0,
                          "tokens_reasoning": 0, "requests_zero_token": 0},
                         "missing fields must be filled with 0")
```

### 卡 1 Step 1 验证命令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 RED**：
- 总测试数：265（253 + 7 UsageExtract 新方法 + 5 SchemaV3MigrationTest）
- 失败：22（P1ConstantsTest×1 + UsageExtractTest×14 含 7 旧更新为 5-tuple + 7 新 cache_read/reasoning + SchemaV3MigrationTest×4: count20/constant/by-key-fields/legacy-load + T1.7/T1.7b/T1.7c 矩阵补齐×3: assertEqual 16≠20）
- 通过：~243（含 SchemaV3MigrationTest.test_new_20_field_downgrade——该测试 GREEN-in-RED，因 legacy_16 白名单 guard 先于生产变更生效）
- exit code：1

---

### Step 2：实现（GREEN）

#### 改动 P1.1 — :48-49 常量区：追加 4 新常量

**old_string**：

```
BY_MODEL_CAP = 32
DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）
```

**new_string**：

```
BY_MODEL_CAP = 32
BY_KEY_CAP = 64              # v3 T1：daily_by_key 每日 key 数上限（对齐 TPM_KEY_CAP :106）
QWAIT_RING_MAX = 500         # v3 P8：per-model qwait 样本环形容量
SETTLE_RING_MAX = 500        # v3 T4：per-model TPM 结算偏差（actual/est）环形容量
HOURLY_RING_MAX = 49         # v3 T6：小时级 token 曲线条目上限（48h 视图 + 当前小时）
DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）
```

#### 改动 P1.2 — :1207-1211 DAILY_V2_FIELDS 16→20 字段 + :1213-1216 注释同步

**old_string**（2 处一并进行——declaration 区 + 注释区）：

```
# v2 P3 起用（P1 先声明）：daily/daily_by_model 16 字段 schema。
# P3 切换前零引用；load/save 循环各按迭代时的 _DAILY_FIELDS 白名单，
# 切换后旧格式自动补 0，新格式被旧版加载时自动丢新字段。
DAILY_V2_FIELDS = _DAILY_FIELDS + ("tokens_prompt", "tokens_completion",
                                   "bytes_out", "stream_requests",
                                   "ttfb_sum_ms", "ttfb_count",
                                   "outcome_ok", "outcome_degraded",
                                   "outcome_failed")

# v2 P3：daily/daily_by_model 切换 16 字段 schema（DAILY_V2_FIELDS 已在 P1 声明，见下）。
# load/save 循环与四处 entry 创建均按 _DAILY_FIELDS 迭代 → 切换后旧 7 字段桶自动补 0，
# 新 16 字段桶被旧版二进制读入自动丢新字段（R2 双向 degrade，无需版本号）。
_DAILY_FIELDS = DAILY_V2_FIELDS
```

**new_string**：

```
# v2 P3 起用（P1 先声明）：daily/daily_by_model 16 字段 schema；v3 扩 20 字段。
# P3 切换前零引用；load/save 循环各按迭代时的 _DAILY_FIELDS 白名单，
# 切换后旧格式自动补 0，新格式被旧版加载时自动丢新字段。
DAILY_V2_FIELDS = _DAILY_FIELDS + ("tokens_prompt", "tokens_completion",
                                   "bytes_out", "stream_requests",
                                   "ttfb_sum_ms", "ttfb_count",
                                   "outcome_ok", "outcome_degraded",
                                   "outcome_failed",
                                   # v3 追加 4 字段（决策 1）：cache_write 占位不解析，
                                   # reasoning 落 daily 供 T3 按天×模型扩列，zero_token 供 T5
                                   "tokens_cache_read", "tokens_cache_write",
                                   "tokens_reasoning", "requests_zero_token")

# v2 P3：daily/daily_by_model 切换 16 字段 schema（DAILY_V2_FIELDS 已在 P1 声明，见下）；
# v3 扩 20 字段——load/save 循环与四处 entry 创建均按 _DAILY_FIELDS 迭代 → 切换后旧
# 16 字段桶自动补 4 个 0，新 20 字段桶被旧版二进制读入自动丢 4 新字段（R2 双向 degrade，
# 无需版本号）。
_DAILY_FIELDS = DAILY_V2_FIELDS
```

#### 改动 P1.3 — :1216 后新增 `_DAILY_BY_KEY_FIELDS`

**old_string**：

```
_DAILY_FIELDS = DAILY_V2_FIELDS


def load_daily_buckets(path: str) -> dict:
```

**new_string**：

```
_DAILY_FIELDS = DAILY_V2_FIELDS

# v3 T1：daily_by_key 8 字段（DAILY_V2_FIELDS 子集——砍 errors/retries/outcome/ttfb，
# per-key 维度不做错误归因与延迟，控制持久化体积；cache_write 占位不解析，决策 2）。
_DAILY_BY_KEY_FIELDS = ("requests", "tokens_prompt", "tokens_completion",
                        "tokens_cache_read", "tokens_cache_write",
                        "tokens_reasoning", "bytes_out", "stream_requests")


def load_daily_buckets(path: str) -> dict:
```

#### 改动 P1.4 — :303-319 usage_dict_tokens 全函数替换（3→5 元组 + _int_nested）

**old_string**：

```
def usage_dict_tokens(usage) -> tuple:
    """从已解析的 usage dict 提取 (prompt_tokens, completion_tokens, total_tokens)。

    非 dict → None；各字段取 int ≥0 原值，缺失/非 int/负数 → 0（best-effort）。
    零 IO、零 parse：入参必须是 json.loads 已产出的 dict（复用解析一次，R1）。
    """
    if not isinstance(usage, dict):
        return None

    def _int_field(key):
        value = usage.get(key)
        if isinstance(value, bool):  # bool 是 int 子类：JSON true/false 不是合法计数，归 0
            return 0
        return value if isinstance(value, int) and value >= 0 else 0

    return (_int_field("prompt_tokens"), _int_field("completion_tokens"),
            _int_field("total_tokens"))
```

**new_string**：

```
def usage_dict_tokens(usage) -> tuple:
    """从已解析的 usage dict 提取 (prompt_tokens, completion_tokens, total_tokens,
    cache_read_tokens, reasoning_tokens)（v3 五元组，决策 8）。

    非 dict → None；各字段取 int ≥0 原值，缺失/非 int/负数 → 0（best-effort）。
    零 IO、零 parse：入参必须是 json.loads 已产出的 dict（复用解析一次，R1）。

    cache_read 三级别名序，首个命中即取：
      prompt_cache_hit_tokens → cache_read_input_tokens
      → prompt_tokens_details.cached_tokens
    reasoning 两级：completion_tokens_details.reasoning_tokens → reasoning_tokens（顶层）。
    cache_write 本次不解析（Exclusions：待上游实证字段形态后另开 episode）。
    """
    if not isinstance(usage, dict):
        return None

    def _int_field(key):
        value = usage.get(key)
        if isinstance(value, bool):  # bool 是 int 子类：JSON true/false 不是合法计数，归 0
            return 0
        return value if isinstance(value, int) and value >= 0 else 0

    def _int_nested(parent_key, child_key):
        """取 usage[parent_key][child_key] int ≥0，非 dict/缺失/非 int → 0。"""
        parent = usage.get(parent_key)
        if not isinstance(parent, dict):
            return 0
        value = parent.get(child_key)
        if isinstance(value, bool):
            return 0
        return value if isinstance(value, int) and value >= 0 else 0

    # cache_read：三级别名序，首个命中即取
    cache_read = _int_field("prompt_cache_hit_tokens")
    if cache_read == 0:
        cache_read = _int_field("cache_read_input_tokens")
    if cache_read == 0:
        cache_read = _int_nested("prompt_tokens_details", "cached_tokens")

    # reasoning：两级，首个命中即取
    reasoning = _int_nested("completion_tokens_details", "reasoning_tokens")
    if reasoning == 0:
        reasoning = _int_field("reasoning_tokens")

    return (_int_field("prompt_tokens"), _int_field("completion_tokens"),
            _int_field("total_tokens"), cache_read, reasoning)
```

#### 改动 P1.5 — :322-332 sse_line_extract_usage docstring 更新

**old_string**：

```
def sse_line_extract_usage(line: bytes):
    """SSE data 行 usage 帧数值抽取：返回 (prompt_tokens, completion_tokens, total_tokens)。

    无 usage 帧（非 data: 前缀 / [DONE] / 非 JSON / 非 dict / usage 空）→ None。
    解析委托 sse_line_usage（单次 json.loads，与 sse_line_has_usage 同解析路径）；
    热路径上不重复调用本函数——_relay_sse 直接用 usage_dict_tokens 消费已解析 dict（R1）。
    """
```

**new_string**：

```
def sse_line_extract_usage(line: bytes):
    """SSE data 行 usage 帧数值抽取：返回 (prompt_tokens, completion_tokens, total_tokens,
    cache_read_tokens, reasoning_tokens)（v3 五元组，委托 usage_dict_tokens）。

    无 usage 帧（非 data: 前缀 / [DONE] / 非 JSON / 非 dict / usage 空）→ None。
    解析委托 sse_line_usage（单次 json.loads，与 sse_line_has_usage 同解析路径）；
    热路径上不重复调用本函数——_relay_sse 直接用 usage_dict_tokens 消费已解析 dict（R1）。
    """
```

#### 改动 P1.6 — proxy.py 内 :1268-1265 load_daily_by_model_buckets 尾部注释 "同 load_daily_buckets"（零功能改动，仅确认白名单自动适配）——不编辑，仅凭 Card 1 AC1 实测验证。

### 卡 1 Step 2 + 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 GREEN**：
- 265 tests，exit code 0，0 failure / 0 error
- 新增测试数：+12（7 UsageExtract + 5 SchemaV3MigrationTest）

---

## 卡 2｜STATS 新键 + `_record_request` 扩展（tier A）— 代码

### 目标

STATS 初始化追加 6 个新键；`_record_request` 签名加 4 kwargs（tokens_cache_read/tokens_reasoning/qwait_ms/key_id12），锁内新增 5 组写入：daily_by_key、phase_ms_by_model 双写、qwait 环、hourly_tokens 滚动、cache_read/reasoning/zero_token 桶增量。依赖卡 1。

### 文件

`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

### TDD 顺序

**Step 1 — 失败测试（RED）**  
**Step 2 — 实现（GREEN）**

---

### Step 1：失败测试（RED）——新增 `RecordRequestV3Test` 类

插在 SchemaV3MigrationTest 之后、RequestIdTest 之前。即：在 B1 卡 1 T1.6 新增的 SchemaV3MigrationTest 末行 `def test_v3_capacity_constants(...)` 最后的 `self.assertEqual(getattr(mod, "HOURLY_RING_MAX", None), 49)` 行之后、`class RequestIdTest(unittest.TestCase):` 之前。

**old_string**（锚：卡 1 已写入了 SchemaV3MigrationTest 末尾 → RequestIdTest；本次 Annex 后 PLAN 中的实际锚点是 `RequestIdTest` 类头前两空行）：

```
        self.assertEqual(getattr(mod, "HOURLY_RING_MAX", None), 49)


class RequestIdTest(unittest.TestCase):
```

**new_string**（在 SchemaV3MigrationTest 后插入 RecordRequestV3Test）：

```
        self.assertEqual(getattr(mod, "HOURLY_RING_MAX", None), 49)


class RecordRequestV3Test(unittest.TestCase):
    """v3 _record_request 新维度：daily_by_key/phase 双写/qwait 环/hourly/zero_token。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_daily_by_key_records_tokens(self) -> None:
        mod = self.mod
        today = mod.today_key()
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            mod._record_request("POST", "/k1", 200, 1.0, 0, model="m1",
                                key_id12="abcdef123456",
                                tokens_prompt=10, tokens_completion=5,
                                tokens_cache_read=4, tokens_reasoning=3,
                                tokens=22, bytes_out=100, stream=1)
            entry = mod.STATS["daily_by_key"][today]["abcdef123456"]
            self.assertEqual(entry["requests"], 1)
            self.assertEqual(entry["tokens_prompt"], 10)
            self.assertEqual(entry["tokens_completion"], 5)
            self.assertEqual(entry["tokens_cache_read"], 4)
            self.assertEqual(entry["tokens_reasoning"], 3)
            self.assertEqual(entry["bytes_out"], 100)
            self.assertEqual(entry["stream_requests"], 1)
            self.assertEqual(entry["tokens_cache_write"], 0,
                             "cache_write 不解析，恒 0（Exclusions）")
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_daily_by_key_cap_stops_new_keys(self) -> None:
        mod = self.mod
        today = mod.today_key()
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            for i in range(mod.BY_KEY_CAP):
                mod._record_request("POST", "/c", 200, 1.0, 0,
                                    key_id12="key%012d" % i)
            mod._record_request("POST", "/c", 200, 1.0, 0,
                                key_id12="overflow-key-1")
            day_keys = mod.STATS["daily_by_key"][today]
            self.assertEqual(len(day_keys), mod.BY_KEY_CAP,
                             "cap 后新 key 不记录；got %d keys" % len(day_keys))
            self.assertNotIn("overflow-key-1", day_keys)
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_phase_ms_dual_write_global_and_per_model(self) -> None:
        mod = self.mod
        before_g = {k: list(v) for k, v in mod.STATS["phase_ms"].items()}
        orig_m = mod.STATS["phase_ms_by_model"]
        mod.STATS["phase_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/p", 200, 1.0, 0, model="m1",
                                phase_ms={"connect": 120.0, "headers": 300.0,
                                          "body": 600.0})
            # HIST_BUCKETS_MS=(100,250,500,1000,...): 120→idx1, 300→idx2, 600→idx3
            self.assertEqual(mod.STATS["phase_ms"]["connect"][1],
                             before_g["connect"][1] + 1, "全局 connect +1")
            self.assertEqual(mod.STATS["phase_ms"]["headers"][2],
                             before_g["headers"][2] + 1, "全局 headers +1")
            self.assertEqual(mod.STATS["phase_ms"]["body"][3],
                             before_g["body"][3] + 1, "全局 body +1")
            self.assertEqual(mod.STATS["phase_ms_by_model"]["m1"]["connect"][1], 1)
            self.assertEqual(mod.STATS["phase_ms_by_model"]["m1"]["headers"][2], 1)
            self.assertEqual(mod.STATS["phase_ms_by_model"]["m1"]["body"][3], 1)
        finally:
            mod.STATS["phase_ms_by_model"] = orig_m

    def test_phase_ms_model_none_only_global(self) -> None:
        mod = self.mod
        orig_m = mod.STATS["phase_ms_by_model"]
        mod.STATS["phase_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/p", 200, 1.0, 0,
                                phase_ms={"connect": 50.0, "headers": 80.0,
                                          "body": 200.0})
            self.assertEqual(mod.STATS["phase_ms_by_model"], {},
                             "model=None 不写 per-model 直方图")
        finally:
            mod.STATS["phase_ms_by_model"] = orig_m

    def test_qwait_ring_accumulates(self) -> None:
        mod = self.mod
        orig_q = mod.STATS["qwait_ms_by_model"]
        mod.STATS["qwait_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/q", 200, 1.0, 0, model="m1", qwait_ms=15)
            mod._record_request("POST", "/q", 200, 1.0, 0, model="m1", qwait_ms=25)
            self.assertEqual(list(mod.STATS["qwait_ms_by_model"]["m1"]), [15, 25])
            # 无 qwait / 无 model 不落
            mod._record_request("POST", "/q", 200, 1.0, 0)
            mod._record_request("POST", "/q", 200, 1.0, 0, model="m2")
            self.assertEqual(list(mod.STATS["qwait_ms_by_model"]["m1"]), [15, 25])
            self.assertNotIn("m2", mod.STATS["qwait_ms_by_model"],
                             "qwait_ms=None 不得建 m2 环")
        finally:
            mod.STATS["qwait_ms_by_model"] = orig_q

    def test_hourly_tokens_cross_boundary(self) -> None:
        mod = self.mod
        orig_time_func = mod.time.time
        try:
            mod.time.time = lambda: 1698825600.0  # 2023-11-01 00:00:00 UTC
            mod.STATS["hourly_tokens"].clear()
            mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                                tokens_prompt=10, tokens_completion=5)
            self.assertEqual(len(mod.STATS["hourly_tokens"]), 1)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["hour_start_ts"],
                             1698825600)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["tokens_prompt"], 10)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["tokens_completion"], 5)
            # 同小时追加：累积不新增条目
            mod.time.time = lambda: 1698827400.0  # +30 min
            mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                                tokens_prompt=3, tokens_completion=2)
            self.assertEqual(len(mod.STATS["hourly_tokens"]), 1)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["tokens_prompt"], 13)
            # 跨小时：新增条目
            mod.time.time = lambda: 1698829200.0  # +1h
            mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                                tokens_prompt=1, tokens_completion=0)
            self.assertEqual(len(mod.STATS["hourly_tokens"]), 2)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["hour_start_ts"],
                             1698829200)
        finally:
            mod.time.time = orig_time_func

    def test_requests_zero_token_increments(self) -> None:
        mod = self.mod
        today = mod.today_key()
        # tokens=None 且 status<500 → zero_token +1
        mod._record_request("POST", "/z1", 200, 1.0, 0, model="m1")
        b = mod.STATS["daily"][today]
        self.assertEqual(b["requests_zero_token"], 1)
        # tokens=0 且 status<500 → +1
        mod._record_request("POST", "/z2", 200, 1.0, 0, model="m1", tokens=0)
        self.assertEqual(b["requests_zero_token"], 2)
        # status>=500 → 不计
        mod._record_request("POST", "/z3", 500, 1.0, 0, model="m1")
        self.assertEqual(b["requests_zero_token"], 2)
        # error=True → 不计
        mod._record_request("POST", "/z4", 200, 1.0, 0, error=True)
        self.assertEqual(b["requests_zero_token"], 2)

    def test_tokens_cache_read_reasoning_in_daily_bucket(self) -> None:
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/t", 200, 1.0, 0, model="m1",
                            tokens_prompt=10, tokens_completion=5,
                            tokens_cache_read=4, tokens_reasoning=3)
        b = mod.STATS["daily"][today]
        self.assertEqual(b["tokens_cache_read"], 4)
        self.assertEqual(b["tokens_reasoning"], 3)
        self.assertEqual(b["tokens_cache_write"], 0,
                         "cache_write 不解析，桶字段恒 0（Exclusions）")


class RequestIdTest(unittest.TestCase):
```

### 卡 2 Step 1 验证命令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 RED**：
- 总测试数：273（265 + 8 RecordRequestV3Test）
- 失败：8（全部新增测试因 STATS 键缺失 / _record_request kwargs 不存在而 ERROR/FAIL）
- exit code：1

---

### Step 2：实现（GREEN）

#### 改动 P2.1 — STATS 初始化：:861-871 追加 6 新键

**old_string**（锚：STATS dict 尾部 `"stalls_total": 0}` + `_stats_dirty` 行）：

```
         "stalls_total": 0}
_stats_dirty = False  # STATS_LOCK 保护：计数落盘脏标记（SIGTERM/60s 脏刷消费）
```

**new_string**：

```
         "stalls_total": 0,
         # v3：per-key/每模型扩展观测（决策 2/3/4/5）
         "daily_by_key": {},          # {day: {key_id12: _DAILY_BY_KEY_FIELDS 8 字段}}，BY_KEY_CAP 截断
         "phase_ms_by_model": {},     # {model: {"connect":[0]*9,"headers":[0]*9,"body":[0]*9}}，cap BY_MODEL_CAP
         "stalls_by_model": {},       # {model: int}，cap BY_MODEL_CAP
         "qwait_ms_by_model": {},     # {model: deque(maxlen=QWAIT_RING_MAX)}，cap BY_MODEL_CAP
         "tpm_settle_ratio_by_model": {},  # {model: deque(maxlen=SETTLE_RING_MAX)}，cap BY_MODEL_CAP
         "hourly_tokens": collections.deque(maxlen=HOURLY_RING_MAX),
         #  ^ {"hour_start_ts","tokens_prompt","tokens_completion"} 每小时滚动（内存态不持久化）
         }
_stats_dirty = False  # STATS_LOCK 保护：计数落盘脏标记（SIGTERM/60s 脏刷消费）
```

#### 改动 P2.2 — `_record_request` 签名加 4 kwargs（:1484-1490）

**old_string**：

```
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None,
                    chunks: int = 0,
                    tokens_prompt: int = 0, tokens_completion: int = 0,
                    phase_ms: dict = None) -> None:
```

**new_string**：

```
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None,
                    chunks: int = 0,
                    tokens_prompt: int = 0, tokens_completion: int = 0,
                    phase_ms: dict = None,
                    # v3：cache_read/reasoning 落桶 + TPM 排队观测 + per-key 标识
                    tokens_cache_read: int = 0, tokens_reasoning: int = 0,
                    qwait_ms=None, key_id12=None) -> None:
```

#### 改动 P2.3 — dm entry 增量区：追加 cache_read/reasoning/zero_token（:1524-1536 内）

**old_string**（锚：`if tokens_completion > 0:` ... 后的 `if bytes_out > 0:`）：

```

            if entry_dm is not None:
                if tokens_prompt > 0:
                    entry_dm["tokens_prompt"] += tokens_prompt
                if tokens_completion > 0:
                    entry_dm["tokens_completion"] += tokens_completion
                if bytes_out > 0:
```

**new_string**：

```

            if entry_dm is not None:
                if tokens_prompt > 0:
                    entry_dm["tokens_prompt"] += tokens_prompt
                if tokens_completion > 0:
                    entry_dm["tokens_completion"] += tokens_completion
                if tokens_cache_read > 0:
                    entry_dm["tokens_cache_read"] += tokens_cache_read
                if tokens_reasoning > 0:
                    entry_dm["tokens_reasoning"] += tokens_reasoning
                if bytes_out > 0:
```

#### 改动 P2.4 — dm entry 增量区尾部：追加 requests_zero_token（:1535-1536 `if tri_state` 之后）

**old_string**（锚：`if tri_state is not None:` 结尾两行）：

```

                if tri_state is not None:
                    entry_dm["outcome_" + tri_state] += 1
```

**new_string**：

```

                if tri_state is not None:
                    entry_dm["outcome_" + tri_state] += 1
                if not error and status < 500 and (tokens is None or tokens == 0):
                    entry_dm["requests_zero_token"] += 1
```

#### 改动 P2.5 — daily 总桶增量区：追加 cache_read/reasoning/zero_token（:1548-1558）

**old_string**：

```
        # v2 P3：daily 总桶增量（与 dm entry 同口径）
        if tokens_prompt > 0:
            bucket["tokens_prompt"] += tokens_prompt
        if tokens_completion > 0:
            bucket["tokens_completion"] += tokens_completion
        if bytes_out > 0:
```

**new_string**：

```
        # v2 P3：daily 总桶增量（与 dm entry 同口径）；v3 追加 cache_read/reasoning
        if tokens_prompt > 0:
            bucket["tokens_prompt"] += tokens_prompt
        if tokens_completion > 0:
            bucket["tokens_completion"] += tokens_completion
        if tokens_cache_read > 0:
            bucket["tokens_cache_read"] += tokens_cache_read
        if tokens_reasoning > 0:
            bucket["tokens_reasoning"] += tokens_reasoning
        if bytes_out > 0:
```

在 `bucket["outcome_" + tri_state]` 块之后（:1557-1558）追加 zero_token：

**old_string**：

```
        if tri_state is not None:
            bucket["outcome_" + tri_state] += 1
        if error or status >= 500:
```

**new_string**：

```
        if tri_state is not None:
            bucket["outcome_" + tri_state] += 1
        if not error and status < 500 and (tokens is None or tokens == 0):
            bucket["requests_zero_token"] += 1
        if error or status >= 500:
```

#### 改动 P2.6 — daily_by_key 更新（在 daily 总桶之后、error/status 之前，即 :1557 区域后）

**old_string**（锚：:1558 `if error or status >= 500:`——整段 daily_by_key 插入其前）：

```
        if error or status >= 500:
            # 分类优先级与计数一致（error 分支胜过 status>=500）：error=True → proxy，
            # 其余 status>=500 → upstream；499 中断两边都不入流（同计数口径）。
            EVENTS.append({"ts": time.time(), "kind": "proxy" if error else "upstream",
                           "model": model, "status": status})
```

**new_string**（:1558 前插入 daily_by_key 块）：

```
        # v3 T1：per-key day 桶（8 字段 _DAILY_BY_KEY_FIELDS），BY_KEY_CAP 独立截断
        if key_id12:
            day_keys = STATS["daily_by_key"].setdefault(today_key(), {})
            entry_k = day_keys.get(key_id12)
            if entry_k is None and len(day_keys) < BY_KEY_CAP:
                entry_k = day_keys[key_id12] = dict.fromkeys(_DAILY_BY_KEY_FIELDS, 0)
            if entry_k is not None:
                entry_k["requests"] += 1
                if tokens_prompt > 0:
                    entry_k["tokens_prompt"] += tokens_prompt
                if tokens_completion > 0:
                    entry_k["tokens_completion"] += tokens_completion
                if tokens_cache_read > 0:
                    entry_k["tokens_cache_read"] += tokens_cache_read
                if tokens_reasoning > 0:
                    entry_k["tokens_reasoning"] += tokens_reasoning
                if bytes_out > 0:
                    entry_k["bytes_out"] += bytes_out
                if stream:
                    entry_k["stream_requests"] += 1
        if error or status >= 500:
            # 分类优先级与计数一致（error 分支胜过 status>=500）：error=True → proxy，
            # 其余 status>=500 → upstream；499 中断两边都不入流（同计数口径）。
            EVENTS.append({"ts": time.time(), "kind": "proxy" if error else "upstream",
                           "model": model, "status": status})
```

#### 改动 P2.7 — phase_ms 双写（:1578-1581 全局循环后追加 per-model）

**old_string**（锚：phase_ms 循环结尾 + 空行 + now_mono）：

```
        if phase_ms:
            for name in ("connect", "headers", "body"):
                STATS["phase_ms"][name][
                    bisect.bisect_right(HIST_BUCKETS_MS, phase_ms[name])] += 1
        now_mono = time.monotonic()
```

**new_string**：

```
        if phase_ms:
            for name in ("connect", "headers", "body"):
                STATS["phase_ms"][name][
                    bisect.bisect_right(HIST_BUCKETS_MS, phase_ms[name])] += 1
            if model:
                phase_by_model = STATS["phase_ms_by_model"]
                if model not in phase_by_model and len(phase_by_model) < BY_MODEL_CAP:
                    phase_by_model[model] = {
                        "connect": [0] * 9, "headers": [0] * 9, "body": [0] * 9}
                if model in phase_by_model:
                    for name in ("connect", "headers", "body"):
                        phase_by_model[model][name][
                            bisect.bisect_right(HIST_BUCKETS_MS,
                                                phase_ms[name])] += 1
        now_mono = time.monotonic()
```

#### 改动 P2.8 — qwait 环 + hourly_tokens 滚动（在 _stats_dirty = True 前、rates 块后，即 :1595 附近）

**old_string**（锚：:1595 `if tokens:` 结尾 + `_stats_dirty = True`）：

```
        if tokens:  # P3 填 tokens；P2 阶段恒 None → 不累计
            rates["tokens_total"] += tokens
            rates["window_tokens"] += tokens
        _stats_dirty = True
```

**new_string**：

```
        if tokens:  # P3 填 tokens；P2 阶段恒 None → 不累计
            rates["tokens_total"] += tokens
            rates["window_tokens"] += tokens
        # v3 P8：per-model qwait 样本环形记录（决策 4）
        if qwait_ms is not None and model:
            qwait_ring = STATS["qwait_ms_by_model"]
            if model not in qwait_ring and len(qwait_ring) < BY_MODEL_CAP:
                qwait_ring[model] = collections.deque(maxlen=QWAIT_RING_MAX)
            if model in qwait_ring:
                qwait_ring[model].append(qwait_ms)
        # v3 T6：小时级 token 曲线滚动（决策 5）
        hour_start = int(time.time() // 3600 * 3600)
        hourly = STATS["hourly_tokens"]
        if not hourly or hourly[-1]["hour_start_ts"] != hour_start:
            hourly.append({"hour_start_ts": hour_start,
                           "tokens_prompt": 0, "tokens_completion": 0})
        if tokens_prompt > 0:
            hourly[-1]["tokens_prompt"] += tokens_prompt
        if tokens_completion > 0:
            hourly[-1]["tokens_completion"] += tokens_completion
        _stats_dirty = True
```

#### 改动 P2.9 — :1608-1673 三处 retry recorder 零改动确认（`dict.fromkeys(_DAILY_FIELDS, 0)` 自动追随 20 字段）——不编辑

#### 改动 P2.10 — :1676-1692 stats_snapshot 序列化安全覆盖（防止 Card 2 新加 deques 致 /api/stats 500）

Card 2 在 STATS 中新增了 `hourly_tokens`（deque）、`qwait_ms_by_model`（dict of deques）、`tpm_settle_ratio_by_model`（dict of deques）。`stats_snapshot` 的 `snap = dict(STATS)` 会把这些 deques 裸引用装入 snap，经 `json.dumps` 抛 `TypeError`——**不修则卡 2 GREEN 阶段全量测试中任一次 /api/stats 请求 500**，需立即覆盖。

**old_string**（锚：`stats_snapshot` 中 `snap["events"]` 行与 `ttfb_hist` 行之间）：

```
        snap["events"] = [dict(e) for e in EVENTS]  # 逐条浅拷贝（对齐 daily 模式），oldest→newest
        ttfb_hist = {m: list(h) for m, h in STATS["ttfb_hist"].items()}
```

**new_string**：

```
        snap["events"] = [dict(e) for e in EVENTS]  # 逐条浅拷贝（对齐 daily 模式），oldest→newest
        # v3：新键含 deque → 转为 list 才能通过 json.dumps（TYPE ERROR RISK——本 card 引入 key
        # 但卡 6 才扩 perf 节，序列化安全必须提前）
        snap["hourly_tokens"] = list(STATS["hourly_tokens"])
        snap["qwait_ms_by_model"] = {m: list(ring)
                                     for m, ring in STATS["qwait_ms_by_model"].items()}
        snap["tpm_settle_ratio_by_model"] = {m: list(ring)
                                             for m, ring in STATS["tpm_settle_ratio_by_model"].items()}
        ttfb_hist = {m: list(h) for m, h in STATS["ttfb_hist"].items()}
```

> **注**：`snap["daily_by_key"]`（dict of dicts）是 JSON 原生的序列化类型，留待卡 6 做深拷贝（防止返回 live ref）。卡 2 阶段 `/api/stats` 响应暂含 raw daily_by_key ref——既有测试不依赖该键，无 crash 风险。`snap["phase_ms_by_model"]` 同理（live ref，可序列化）。

### 卡 2 Step 2 验证命令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 GREEN**：
- 273 tests，exit code 0
- 新增测试数较卡 1：+8（RecordRequestV3Test）

---

## 卡 3｜`_relay_sse` model kwarg + `_proxy_relay` 结算点透传（tier A）— 代码

### 目标

`_relay_sse(self, resp, final, model=None)` 签名加 model kwarg；stall 自增处追加 `stalls_by_model[model] += 1`（model is not None 守卫 + cap）；`_proxy_relay` 流式与非流式两结算点补传 `tokens_cache_read`/`tokens_reasoning`/`qwait_ms`/`key_id12` 与 `model=model` 给 `_relay_sse` 调用。依赖卡 1（5 元组）、卡 2（STATS 键 stalls_by_model/_record_request 新 kwargs）。

### 文件

`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

### TDD 顺序

**Step 1 — 失败测试（RED）**  
**Step 2 — 实现（GREEN）**

---

### Step 1：失败测试（RED）——新增 `RelaySseV3Test` 类

插在 RecordRequestV3Test 之后、RequestIdTest 之前。

**old_string**（锚：卡 2 已写入的 RecordRequestV3Test 末行——此文本仅存在于卡 2 执行之后）：

```
                         "cache_write 不解析，桶字段恒 0（Exclusions）")


class RequestIdTest(unittest.TestCase):
```

**new_string**（在 RecordRequestV3Test 后插入 RelaySseV3Test）：

```

class RelaySseV3Test(unittest.TestCase):
    """v3 _relay_sse model kwarg + stalls per-model 双写（决策 3）。

    用 handler stub + fake resp 进程内直驱 _relay_sse：STALL_THRESHOLD_S 降至 0.0
    后任意相邻 record 间隔即触发 stall，可确定性断言 stalls_total 与
    stalls_by_model 的双写语义（含 model=None 只增全局）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    class _StubHandler:
        """_relay_sse 可运行的最小 handler stub（wfile/conn/响应三件套）。"""

        def __init__(self):
            self.wfile = io.BytesIO()
            self._req_id = "req-1"
            self.close_connection = False
            self._t_last_record = None
            self._t_first_byte_mark = None
            self._relay_bytes = 0
            self._relay_chunks = 0
            self._body_err_line = None
            self._tpm_usage = None
            self._p3_usage_tokens = None

        class _StubConn:
            def gettimeout(self): return 600
            def settimeout(self, _v): pass
        connection = _StubConn()

        def send_response(self, _code): pass
        def send_header(self, _k, _v): pass
        def end_headers(self): pass

    class _FakeSseResp:
        """fake resp：readline 依序吐行，耗尽后 b"" EOF；getheaders 空即可。"""
        status = 200

        def __init__(self, lines):
            self._lines = list(lines)
            self._i = 0

        def readline(self):
            if self._i < len(self._lines):
                line = self._lines[self._i]
                self._i += 1
                return line
            return b""

        def getheaders(self):
            return [("Content-Type", "text/event-stream")]

    def _drive(self, lines):
        """统一驱动：STALL_THRESHOLD_S=0.0 下走完 [SSE_A, b"\n", SSE_DONE, b"\n"] 流。"""
        mod = self.mod
        orig_thresh = mod.STALL_THRESHOLD_S
        try:
            mod.STALL_THRESHOLD_S = 0.0
            handler = self._StubHandler()
            resp = self._FakeSseResp(lines)
            return handler._relay_sse(resp, final=True, model="m1"), handler
        finally:
            mod.STALL_THRESHOLD_S = orig_thresh

    def test_relay_sse_has_model_kwarg(self) -> None:
        import inspect
        sig = inspect.signature(self.mod._relay_sse)
        self.assertIn("model", sig.parameters)
        self.assertEqual(sig.parameters["model"].default, None)

    def test_stall_increments_global_and_per_model(self) -> None:
        mod = self.mod
        orig_sm = dict(mod.STATS["stalls_by_model"])
        mod.STATS["stalls_by_model"] = {}
        stalls_before = mod.STATS["stalls_total"]
        try:
            (filtered, truncated), handler = self._drive(
                [SSE_A, b"\n", SSE_DONE, b"\n"])
            self.assertEqual(filtered, 0)
            self.assertFalse(truncated)
            self.assertGreater(mod.STATS["stalls_total"], stalls_before,
                               "相邻 record 间隔必须计入 stalls_total")
            self.assertGreater(mod.STATS["stalls_by_model"].get("m1", 0), 0,
                               "model=m1 时 stalls_by_model[m1] 必须同步 +1")
        finally:
            mod.STATS["stalls_by_model"] = orig_sm

    def test_stall_model_none_only_global(self) -> None:
        mod = self.mod
        orig_sm = dict(mod.STATS["stalls_by_model"])
        mod.STATS["stalls_by_model"] = {}
        orig_thresh = mod.STALL_THRESHOLD_S
        try:
            mod.STALL_THRESHOLD_S = 0.0
            handler = self._StubHandler()
            resp = self._FakeSseResp([SSE_A, b"\n", SSE_DONE, b"\n"])
            handler._relay_sse(resp, final=True)  # model 默认 None
            self.assertEqual(mod.STATS["stalls_by_model"], {},
                             "model=None 不得写 stalls_by_model")
        finally:
            mod.STALL_THRESHOLD_S = orig_thresh
            mod.STATS["stalls_by_model"] = orig_sm

    def test_record_request_kwargs_on_stream_settlement(self) -> None:
        """流式结算点：_record_request 新 kwargs 全部透传落桶验证。
        直接调 _record_request 模拟卡 3 完工后的实际调用形态。"""
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/s", 200, 100.0, 0, model="m1",
                            stream=1,
                            tokens_prompt=10, tokens_completion=5,
                            tokens_cache_read=4, tokens_reasoning=3,
                            qwait_ms=15, key_id12="abcdef123456",
                            tokens=22, outcome=mod.CLASS_OK)
        self.assertEqual(
            mod.STATS["daily_by_key"][today]["abcdef123456"]["tokens_prompt"], 10)
        self.assertEqual(
            mod.STATS["daily_by_key"][today]["abcdef123456"]["tokens_cache_read"], 4)
        self.assertEqual(
            mod.STATS["daily_by_key"][today]["abcdef123456"]["tokens_reasoning"], 3)
        qwait = list(mod.STATS["qwait_ms_by_model"].get("m1", []))
        self.assertIn(15, qwait)

    def test_record_request_kwargs_on_non_stream_settlement(self) -> None:
        """非流式结算点：同流式验证、但 stream=0。"""
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/n", 200, 50.0, 0, model="m2",
                            stream=0,
                            tokens_prompt=7, tokens_completion=3,
                            tokens_cache_read=2, tokens_reasoning=1,
                            qwait_ms=8, key_id12="deadbeef0001",
                            tokens=14, outcome=mod.CLASS_OK)
        by_key = mod.STATS["daily_by_key"][today].get("deadbeef0001")
        self.assertIsNotNone(by_key)
        self.assertEqual(by_key["tokens_prompt"], 7)
        self.assertEqual(by_key["tokens_cache_read"], 2)
        self.assertEqual(by_key["tokens_reasoning"], 1)
        # qwait 落 m2 环
        self.assertIn(8, list(mod.STATS["qwait_ms_by_model"].get("m2", [])))


class RequestIdTest(unittest.TestCase):
```

### 卡 3 Step 1 验证命令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 RED**：
- 总测试数：278（273 + 5 RelaySseV3Test）
- 失败：1（仅 test_relay_sse_has_model_kwarg FAIL——model kwarg 缺失；其余 4：test_stall_increments_global_and_per_model 与 test_stall_model_none_only_global 在 RED 期调用 `handler._relay_sse(resp, final=True, model="m1")` 因 model kwarg 缺失抛 TypeError → ERROR/FAIL；test_record_request_kwargs_on_stream/non_stream 依赖 Card 2 的 _record_request 新 kwargs 已到位 → PASS）实际 RED 数 2-3（1 FAIL + 2 ERROR）。
- exit code：1

---

### Step 2：实现（GREEN）

#### 改动 P3.1 — `_relay_sse` 签名 + model kwarg（:2069）

**old_string**：

```
    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
```

**new_string**：

```
    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool,
                   model=None) -> tuple:
```

#### 改动 P3.2 — stall 自增处追加 stalls_by_model（:2151-2156）

**old_string**：

```
                            if self._t_last_record is not None \
                                    and now - self._t_last_record > STALL_THRESHOLD_S:
                                # v2 P2：stall 自增是热循环内唯一持锁点，且仅跨阈值间隙命中
                                # （正常流相邻 record 间隔 << 阈值，<0.1% 命中；R1 纪律）
                                with STATS_LOCK:
                                    STATS["stalls_total"] += 1
```

**new_string**：

```
                            if self._t_last_record is not None \
                                    and now - self._t_last_record > STALL_THRESHOLD_S:
                                # v2 P2：stall 自增是热循环内唯一持锁点，且仅跨阈值间隙命中
                                # （正常流相邻 record 间隔 << 阈值，<0.1% 命中；R1 纪律）
                                with STATS_LOCK:
                                    STATS["stalls_total"] += 1
                                    # v3 P7：per-model stall（决策 3）——model=None 只增全局
                                    if model is not None:
                                        stalls_m = STATS["stalls_by_model"]
                                        if model not in stalls_m \
                                                and len(stalls_m) < BY_MODEL_CAP:
                                            stalls_m[model] = 0
                                        if model in stalls_m:
                                            stalls_m[model] += 1
```

#### 改动 P3.3 — `_proxy_relay` 两处 `_relay_sse` 调用补 model（:1877-1878 与 :1917）

**Edit P3.3a** — stream first call:

**old_string**：

```
                filtered, truncated = self._relay_sse(resp,
                    final=not empty_stream_should_retry(EMPTY_RETRY_MAX))
```

**new_string**：

```
                filtered, truncated = self._relay_sse(resp,
                    final=not empty_stream_should_retry(EMPTY_RETRY_MAX),
                    model=model)
```

**Edit P3.3b** — retry second call:

**old_string**：

```
                filtered, truncated = self._relay_sse(resp, final=True)
                body_error = self._body_err_line is not None
                ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
```

**new_string**：

```
                filtered, truncated = self._relay_sse(resp, final=True,
                                                      model=model)
                body_error = self._body_err_line is not None
                ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
```

#### 改动 P3.4 — 流式结算点：`_record_request` 调用补 4 kwargs（:1967-1977）

**旧锚**（:1967-1977 完整调用，全文件唯一含 `stream=1` 的 `_record_request` 调用）：

**old_string**：

```
            p3_tokens = self._p3_usage_tokens
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome,
                            tokens=p3_tokens[2] if p3_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_tokens[0] if p3_tokens else 0,
                            tokens_completion=p3_tokens[1] if p3_tokens else 0,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

**new_string**：

```
            p3_tokens = self._p3_usage_tokens
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome,
                            tokens=p3_tokens[2] if p3_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_tokens[0] if p3_tokens else 0,
                            tokens_completion=p3_tokens[1] if p3_tokens else 0,
                            tokens_cache_read=p3_tokens[3] if p3_tokens else 0,
                            tokens_reasoning=p3_tokens[4] if p3_tokens else 0,
                            qwait_ms=tpm_qwait_ms,
                            key_id12=tpm_key[:12] if tpm_key else None,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

#### 改动 P3.5 — 非流式结算点：`_record_request` 调用补 4 kwargs（:2012-2022）

**old_string**（全文件唯一含 `stream=0` 的 `_record_request` 调用）：

```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome,
                            tokens=p3_buf_tokens[2] if p3_buf_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_buf_tokens[0] if p3_buf_tokens else 0,
                            tokens_completion=p3_buf_tokens[1] if p3_buf_tokens else 0,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

**new_string**：

```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome,
                            tokens=p3_buf_tokens[2] if p3_buf_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_buf_tokens[0] if p3_buf_tokens else 0,
                            tokens_completion=p3_buf_tokens[1] if p3_buf_tokens else 0,
                            tokens_cache_read=p3_buf_tokens[3] if p3_buf_tokens else 0,
                            tokens_reasoning=p3_buf_tokens[4] if p3_buf_tokens else 0,
                            qwait_ms=tpm_qwait_ms,
                            key_id12=tpm_key[:12] if tpm_key else None,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

### 卡 3 Step 2 验证命令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 GREEN**：
- 278 tests，exit code 0
- 新增测试数较卡 2：+5（RelaySseV3Test）

---

### B1 段收尾确认

- Card 1: 265 tests OK（+12）
- Card 2: 273 tests OK（+8）
- Card 3: 278 tests OK（+5）
- **无锚点/方案冲突**——全部锚点在 B0 阶段经 grep/sed 核验通过；Card 3 RelaySseV3Test 使用 handler stub + fake resp 进程内直驱 _relay_sse 以实现确定性 stall per-model 断言，栈要求（_StubHandler 的 wfile/connection/send_response/send_header/end_headers）均已在 _relay_sse 源码追踪中确认可满足。

---

# B2 段：卡 4-6 填码

## 卡 4｜tpm_settle 比率 + daily_by_key 持久化（tier A）— 代码

### 目标

tpm_settle 记录 actual/est 比率入 `tpm_settle_ratio_by_model` 环（锁序注释）；save_stats_counters daily_by_key 第三路深拷+prune+落盘；新增 load_daily_by_key_buckets + main() 接线。依赖卡 1（常量 _DAILY_BY_KEY_FIELDS）、卡 2（STATS 键）。

### 文件

`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

### TDD 顺序

**Step 1 — 失败测试（RED）**  
**Step 2 — 实现（GREEN）**

---

### Step 1：失败测试（RED）——新增 `SettlePersistV3Test` 类

插在 RelaySseV3Test 之后、RequestIdTest 之前（卡 3 已写入 RelaySseV3Test。执行顺序保证：卡 3 → 卡 4）。

**old_string**（锚：RelaySseV3Test 末行——此文本仅存在于卡 3 执行之后）：

```
        self.assertIn(8, list(mod.STATS["qwait_ms_by_model"].get("m2", [])))


class RequestIdTest(unittest.TestCase):
```

**new_string**：

```

class SettlePersistV3Test(unittest.TestCase):
    """v3 tpm_settle 比率 + daily_by_key save/load roundtrip（决策 2/4）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_tpm_settle_records_ratio(self) -> None:
        mod = self.mod
        orig_buckets = mod.TPM_BUCKETS
        orig_ring = mod.STATS["tpm_settle_ratio_by_model"]
        mod.TPM_BUCKETS = {}
        mod.STATS["tpm_settle_ratio_by_model"] = {}
        try:
            mod.TPM_BUCKETS[("k1", "m1")] = mod._TpmBucket()
            # actual=90, est=100 → 0.9
            mod.tpm_settle("k1", 100, 90, "m1")
            self.assertEqual(
                list(mod.STATS["tpm_settle_ratio_by_model"]["m1"]), [0.9],
                "actual/est=0.9 必须入环")
            # est=0 / actual<0 → 不追加
            mod.tpm_settle("k1", 0, 90, "m1")
            mod.tpm_settle("k1", 100, -5, "m1")
            self.assertEqual(
                len(mod.STATS["tpm_settle_ratio_by_model"]["m1"]), 1,
                "est<=0 或 actual<0 不得追加比率样本")
            # est==actual → 1.0（delta==0 不退款但比率仍有观测价值）
            mod.tpm_settle("k1", 100, 100, "m1")
            self.assertEqual(
                list(mod.STATS["tpm_settle_ratio_by_model"]["m1"]), [0.9, 1.0])
        finally:
            mod.TPM_BUCKETS = orig_buckets
            mod.STATS["tpm_settle_ratio_by_model"] = orig_ring

    def test_save_stats_counters_includes_daily_by_key(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykey-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        today = mod.today_key()
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            mod._record_request("POST", "/k", 200, 1.0, 0, model="m1",
                                key_id12="abcdef123456", tokens_prompt=10,
                                tokens_completion=5, stream=1)
            mod.save_stats_counters(path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("daily_by_key", data.get("stats", {}),
                          "落盘必须含 daily_by_key 键")
            entry = data["stats"]["daily_by_key"][today]["abcdef123456"]
            self.assertEqual(entry["tokens_prompt"], 10)
            self.assertEqual(entry["stream_requests"], 1)
            # roundtrip 全等
            self.assertEqual(
                mod.load_daily_by_key_buckets(path),
                mod.STATS["daily_by_key"],
                "save→load roundtrip 必须全等")
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_load_daily_by_key_buckets_tolerant(self) -> None:
        mod = self.mod
        loader = getattr(mod, "load_daily_by_key_buckets", None)
        self.assertIsNotNone(loader, "load_daily_by_key_buckets 必须存在")
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykey-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": {
                "2026-01-02": {
                    "abcdef123456": {"requests": 5, "tokens_prompt": 11},
                    "BAD_KEY_WITH_UPPER": {"requests": 1},
                    "toolongkeymorethan12": {"requests": 1},
                    "": {"requests": 1}, "nothex!": {"requests": 1},
                    "1234": "not-a-dict",
                }}}}, fh)
        out = loader(path)
        self.assertEqual(set(out["2026-01-02"]), {"abcdef123456"},
                         "非 1-12 位小写 hex 或空 key 必须丢弃")
        self.assertEqual(set(out["2026-01-02"]["abcdef123456"]),
                         set(mod._DAILY_BY_KEY_FIELDS), "8 字段白名单清洗")
        self.assertEqual(out["2026-01-02"]["abcdef123456"]["tokens_prompt"], 11)
        self.assertEqual(out["2026-01-02"]["abcdef123456"]["tokens_cache_write"], 0)
        # 损坏 / 非 ISO → {}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": "not-a-dict"}}, fh)
        self.assertEqual(loader(path), {})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": {"20260101": {"abcd": {}}}}}, fh)
        self.assertEqual(loader(path), {}, "非 ISO 日期 key 必须跳过")

    def test_load_daily_by_key_cap_truncates(self) -> None:
        mod = self.mod
        loader = getattr(mod, "load_daily_by_key_buckets", None)
        self.assertIsNotNone(loader)
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykeycap-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": {
                "2026-01-02": {"%012x" % i: {"requests": i}
                               for i in range(mod.BY_KEY_CAP + 10)}}}}, fh)
        out = loader(path)
        day = out["2026-01-02"]
        self.assertEqual(len(day), mod.BY_KEY_CAP,
                         "超 BY_KEY_CAP 必须按文件出现序截断")
        self.assertIn("%012x" % (mod.BY_KEY_CAP - 1), day)
        self.assertNotIn("%012x" % mod.BY_KEY_CAP, day)

    def test_daily_by_key_prune_applies_retention(self) -> None:
        mod = self.mod
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykeyprune-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        try:
            mod._record_request("POST", "/p", 200, 1.0, 0, model="m1",
                                key_id12="abcdef123456")
            old_day = (mod.datetime.date.fromisoformat(mod.today_key())
                       - mod.datetime.timedelta(
                           days=mod.DAILY_RETENTION_DAYS + 5)).isoformat()
            mod.STATS["daily_by_key"][old_day] = {
                "abcdef123456": dict.fromkeys(mod._DAILY_BY_KEY_FIELDS, 0)}
            mod.save_stats_counters(path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertNotIn(old_day, data.get("stats", {}).get("daily_by_key", {}),
                             "过期日期桶必须被 prune")
            self.assertIn(mod.today_key(), data["stats"]["daily_by_key"])
        finally:
            mod.STATS["daily_by_key"] = orig


class RequestIdTest(unittest.TestCase):
```

### 卡 4 Step 1 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 RED**：
- 总测试数：283（278 + 5）
- 失败：5（全部 5 方法因生产未就位而 FAIL——tpm_settle 比率缺位、save 缺键、loader 未定义、cap 缺函数、prune 缺键；防御性 getattr/assertIn 保证干净 FAIL 而非 ERROR）
- exit code：1

---

### Step 2：实现（GREEN）

#### 改动 P4.1 — tpm_settle 比率追加（锁序注释）

**old_string**（锚：proxy.py :638-645 区域，`delta = actual - est` 到 `TPM_LOCK.notify_all()`）：

```
        delta = actual - est
        if delta != 0:
            bucket.append((now, delta))
        bucket.used = bucket.used + delta
        TPM_LOCK.notify_all()
```

**new_string**：

```
        delta = actual - est
        if delta != 0:
            bucket.append((now, delta))
        bucket.used = bucket.used + delta
        # v3 T4：TPM 结算偏差样本（actual/est 比率）入环供分位观测（决策 4）。
        # 锁序（R1 防死锁）：本函数持 TPM_LOCK，STATS_LOCK 必须在 TPM_LOCK 内获取——
        # 全库固定 TPM_LOCK → STATS_LOCK。_record_request 只持 STATS_LOCK，
        # tpm_snapshot 只持 TPM_LOCK——无反向获取路径。
        # 禁止在 STATS_LOCK 持锁路径上新增任何 TPM_LOCK 获取。
        if est > 0 and actual >= 0:
            with STATS_LOCK:
                ring = STATS["tpm_settle_ratio_by_model"]
                if model not in ring and len(ring) < BY_MODEL_CAP:
                    ring[model] = collections.deque(maxlen=SETTLE_RING_MAX)
                if model in ring:
                    ring[model].append(actual / est)
        TPM_LOCK.notify_all()
```

#### 改动 P4.2 — save_stats_counters daily_by_key 第三路

**Edit P4.2a** — prune + 深拷：

**old_string**（proxy.py :1128-1133）：

```
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
        daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                          for d, models in STATS["daily_by_model"].items()}
        events = [dict(e) for e in EVENTS]  # 逐条浅拷贝：磁盘与内存一致（≤100 条）
```

**new_string**：

```
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_key"])    # v3 T1：value-agnostic prune 复用
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
        daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                          for d, models in STATS["daily_by_model"].items()}
        daily_by_key = {d: {k: dict(v) for k, v in keys.items()}
                        for d, keys in STATS["daily_by_key"].items()}
        events = [dict(e) for e in EVENTS]  # 逐条浅拷贝：磁盘与内存一致（≤100 条）
```

**Edit P4.2b** — json.dump payload：

**old_string**（proxy.py :1149-1150）：

```
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
```

**new_string**：

```
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 daily_by_key=daily_by_key, events=events)},
```

#### 改动 P4.3 — 新增 load_daily_by_key_buckets

**old_string**（锚：load_daily_by_model_buckets 函数末行 `return out` + 空行 + `load_stats_events`）：

```
        out[key] = bucket
    return out


def load_stats_events(path: str) -> list:
```

**new_string**：

```
        out[key] = bucket
    return out


def load_daily_by_key_buckets(path: str) -> dict:
    """读日期→key_id12→_DAILY_BY_KEY_FIELDS 矩阵；缺/损坏/legacy 无 daily_by_key 键 → {}。

    外层 ISO 日期 round-trip 校验同 load_daily_by_model_buckets；内层 key 必须是
    1-12 位小写 hex 字符串（sha256 前 12 位脱敏口径，决策 2），超 BY_KEY_CAP 按
    文件出现序截断；8 字段白名单清洗（_DAILY_BY_KEY_FIELDS），坏字段补 0。
    """
    stats = _load_persist_file(path).get("stats")
    raw = stats.get("daily_by_key") if isinstance(stats, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, keys in raw.items():
        try:
            d = datetime.date.fromisoformat(key)
        except (ValueError, TypeError):
            continue
        if str(d) != key or not isinstance(keys, dict):
            continue
        bucket = {}
        for kid, entry in keys.items():
            if (not isinstance(kid, str) or not kid or len(kid) > 12
                    or re.fullmatch(r"[0-9a-f]+", kid) is None):
                continue  # 非 1-12 位小写 hex → 丢弃（坏数据不崩）
            if not isinstance(entry, dict) or len(bucket) >= BY_KEY_CAP:
                continue  # cap 截断：按文件出现序保留前 BY_KEY_CAP 个
            clean = {}
            for field in _DAILY_BY_KEY_FIELDS:  # 同 load_daily_buckets 逐字段规则
                value = entry.get(field)
                clean[field] = value if isinstance(value, int) and value >= 0 else 0
            bucket[kid] = clean
        out[key] = bucket
    return out


def load_stats_events(path: str) -> list:
```

#### 改动 P4.4 — main() 启动接线（load + STATS 赋值）

**Edit P4.4a** — load call（proxy.py :3559-3560）：

**old_string**：

```
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    daily_by_model = load_daily_by_model_buckets(PERSIST_PATH)  # 按天×模型矩阵跨重启续算
```

**new_string**：

```
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    daily_by_model = load_daily_by_model_buckets(PERSIST_PATH)  # 按天×模型矩阵跨重启续算
    daily_by_key = load_daily_by_key_buckets(PERSIST_PATH)  # v3 T1：按天×key 矩阵跨重启续算
```

**Edit P4.4b** — STATS 赋值（proxy.py :3599-3600）：

**old_string**：

```
        STATS["daily"] = daily
        STATS["daily_by_model"] = daily_by_model
```

**new_string**：

```
        STATS["daily"] = daily
        STATS["daily_by_model"] = daily_by_model
        STATS["daily_by_key"] = daily_by_key
```

### 卡 4 Step 2 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 GREEN**：283 tests，exit 0。新增 +5（SettlePersistV3Test）。

---

## 卡 5｜probe history 二元组 + /api/probe_history（tier A）— 代码

### 目标

probe history deque 条目标量 ms → (ts, ms) 二元组，_probe_loop  append 点同步；_latency_p90 双形态适配（tuple/scalar，保留旧测试兼容）；_probe_spike_due docstring 更新；新增 probe_history_snapshot（降采样 ≤200 点）；do_GET 加 /api/probe_history。依赖无（PROBE 域独立）。

### 设计决策

spec Files :53 原文「消费处改解包 `for _, ms in history`」——此处 PLAN 调整为 **_latency_p90 双形态**（接受 tuple 与 scalar 可迭代）：`isinstance(next(iter(samples)), tuple)` 分支分别处理。理由：AC11「现有全部用例绿」——test_probe_spike_due_matrix 传标量列表，纯 tuple-only 会致其 TypeError；AC8「回归」直测标量等价性可落地。

### 文件

`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

### TDD 顺序

**Step 1 — RED**  
**Step 2 — GREEN**

---

### Step 1：失败测试——新增 `ProbeHistoryV3Test` + 更新 test_probe_spike_due_matrix

**Edit T5.1** — 在 SettlePersistV3Test 之后、RequestIdTest 之前插入（卡 4 已写入 SettlePersistV3Test）：

**old_string**（锚：卡 4 SettlePersistV3Test 末行 + 空行 + RequestIdTest）：

```
            mod.STATS["daily_by_key"] = orig


class RequestIdTest(unittest.TestCase):
```

**new_string**：

```

class ProbeHistoryV3Test(unittest.TestCase):
    """v3 probe history (ts, ms) 二元组 + 快照降采样 + /api/probe_history 端点（决策 7）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_probe_history_snapshot_downsamples_to_200(self) -> None:
        mod = self.mod
        orig_hist = mod._PROBE_STATE["history"]
        try:
            # 注入 1000 条 monotonically 递增 ts 的二元组
            mod._PROBE_STATE["history"] = collections.deque(
                [(float(i), float(i * 10)) for i in range(1000)])
            snap = mod.probe_history_snapshot()
            self.assertLessEqual(len(snap["points"]), 200)
            self.assertEqual(snap["count_total"], 1000)
            ts = [p[0] for p in snap["points"]]
            self.assertEqual(ts, sorted(ts), "points 必须单调 ts")
        finally:
            mod._PROBE_STATE["history"] = orig_hist

    def test_probe_history_snapshot_small_returns_all(self) -> None:
        mod = self.mod
        orig_hist = mod._PROBE_STATE["history"]
        try:
            mod._PROBE_STATE["history"] = collections.deque(
                [(10.0, 5.0), (11.0, 6.0), (12.0, 7.0)])
            snap = mod.probe_history_snapshot()
            self.assertEqual(snap["points"],
                             [[10.0, 5.0], [11.0, 6.0], [12.0, 7.0]])
            self.assertEqual(snap["count_total"], 3)
        finally:
            mod._PROBE_STATE["history"] = orig_hist

    def test_latency_p90_tuples_equivalent_to_scalar_oracle(self) -> None:
        mod = self.mod
        values = [50.0] * 180 + [500.0] * 20
        ordered = sorted(values)
        expected = float(ordered[int(0.9 * (len(ordered) - 1))])
        tuples = [(float(i), v) for i, v in enumerate(values)]
        self.assertEqual(mod._latency_p90(tuples), expected,
                         "二元组输入与手工最近秩（标量 oracle）一致")
        self.assertEqual(mod._latency_p90(values), expected,
                         "旧标量形态继续支持（AC8 回归）")
        self.assertEqual(mod._latency_p90([]), 0.0)

    def test_probe_spike_due_tuples(self) -> None:
        mod = self.mod
        f = mod._probe_spike_due
        # 全平 → 无突增
        self.assertFalse(f([(0.0, 50.0)] * 200))
        # 180×50 + 20×500：基线 P90≈50，最近 20 P90=500 > 100 → 告警
        self.assertTrue(f([(0.0, 50.0)] * 180 + [(0.0, 500.0)] * 20))

    def test_probe_history_endpoint_returns_json(self) -> None:
        upstream_port = make_fake_upstream(False)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port,
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "3600"})
        try:
            _, body, _ = admin_get(proc.admin_port, "/api/probe_history")
            snap = json.loads(body.decode("utf-8"))
            self.assertIn("points", snap)
            self.assertIn("count_total", snap)
            self.assertIsInstance(snap["points"], list)
        finally:
            stop_proxy(proc)
            stop_fake_upstreams()


class RequestIdTest(unittest.TestCase):
```

### 卡 5 Step 1 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 RED**：
- 总测试数：288（283 + 5）
- 失败：~4（probe_history_snapshot/downsamples/small/via getattr → FAIL；endpoint → 404 FAIL；latency_p90 与 spike_due tuple 已由双形态兜底 PASS，仅无端点=FAIL）

---

### Step 2：实现（GREEN）

#### 改动 P5.1 — _PROBE_STATE 注释（proxy.py :918）

**old_string**：

```
    "history": collections.deque(maxlen=PROBE_HISTORY_MAX),  # 成功 probe 延迟（ms）历史
```

**new_string**：

```
    "history": collections.deque(maxlen=PROBE_HISTORY_MAX),  # 成功 probe (ts, ms) 历史（v3 二元组）
```

#### 改动 P5.2 — _latency_p90 双形态适配（proxy.py :1349-1356）

**old_string**：

```
def _latency_p90(samples) -> float:
    """延迟样本（ms，可迭代）的 P90（最近秩法：idx = int(0.9 * (n - 1))）。
    空序列 → 0.0。样本上限 PROBE_HISTORY_MAX，排序成本可忽略（每成功 probe 一次）。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return float(ordered[int(0.9 * (len(ordered) - 1))])
```

**new_string**：

```
def _latency_p90(samples) -> float:
    """延迟样本 P90（最近秩法：idx = int(0.9 * (n - 1))），空 → 0.0。

    v3 双形态：接受 ms 标量可迭代（旧口径）或 (ts, ms) 二元组可迭代（probe history），
    自动按首元素形态选择排序键。样本上限 PROBE_HISTORY_MAX，排序成本可忽略。
    """
    if not samples:
        return 0.0
    first = next(iter(samples))
    if isinstance(first, tuple):
        ordered = sorted(ms for _, ms in samples)
    else:
        ordered = sorted(samples)
    return float(ordered[int(0.9 * (len(ordered) - 1))])
```

#### 改动 P5.3 — _probe_spike_due docstring（proxy.py :1358-1370 area）

**old_string**：

```
def _probe_spike_due(history, factor: float = PROBE_LATENCY_SPIKE_FACTOR) -> bool:
    """延迟突增判定：最近 20 次成功 probe 的 P90 > 全历史 P90 × factor 且样本 ≥ 20。

    history：成功 probe 延迟（ms）deque（新在尾）；len < 20 → False（样本不足不告警）。
    """
```

**new_string**：

```
def _probe_spike_due(history, factor: float = PROBE_LATENCY_SPIKE_FACTOR) -> bool:
    """延迟突增判定：最近 20 次成功 probe 的 P90 > 全历史 P90 × factor 且样本 ≥ 20。

    history：成功 probe (ts, ms) 二元组 deque（v3 双形态，_latency_p90 内部处理）；
    len < 20 → False（样本不足不告警）。
    """
```

#### 改动 P5.4 — probe_history_snapshot（proxy.py :1470 之后）

**old_string**（锚：probe_state_snapshot 返回 snap 后 + flush_stats_if_dirty）：

```
            "probe_enabled": PROBE_ENABLED,
        }
    return snap


def flush_stats_if_dirty(path: str) -> None:
```

**new_string**：

```
            "probe_enabled": PROBE_ENABLED,
        }
    return snap


def probe_history_snapshot() -> dict:
    """P10 趋势线数据源：PROBE_LOCK 内取 history 浅拷贝，均匀抽稀 ≤200 点。

    返回 {"points": [[ts, ms], ...], "count_total": n}。step = max(1, n // 200)，
    points 保持追加序（时间单调）。probe_state_snapshot 6 键契约不动（本函数独立端点）。
    """
    with PROBE_LOCK:
        history = list(_PROBE_STATE["history"])
    total = len(history)
    step = max(1, total // 200)
    points = [[ts, ms] for ts, ms in history[::step]]
    return {"points": points, "count_total": total}


def flush_stats_if_dirty(path: str) -> None:
```

#### 改动 P5.5 — do_GET 加 /api/probe_history（proxy.py :2334-2335 area）

**old_string**（锚：/api/health 分支末行）：

```
        elif path == "/api/health":
            self._send_json(200, {"upstream": probe_state_snapshot()})
```

**new_string**：

```
        elif path == "/api/health":
            self._send_json(200, {"upstream": probe_state_snapshot()})
        elif path == "/api/probe_history":
            self._send_json(200, probe_history_snapshot())
```

#### 改动 P5.6 — _probe_loop append（proxy.py :1430-1435 area）

**old_string**（锚：probe 成功分支内）：

```
            state["last_probe_ts"] = time.time()
            state["last_probe_ok"] = ok
            state["last_probe_latency_ms"] = round(latency_ms, 1)
            if ok:
                state["consecutive_failures"] = 0
                state["history"].append(round(latency_ms, 1))
```

**new_string**：

```
            state["last_probe_ts"] = time.time()
            state["last_probe_ok"] = ok
            state["last_probe_latency_ms"] = round(latency_ms, 1)
            if ok:
                state["consecutive_failures"] = 0
                # v3 P10：条目改 (ts, ms) 二元组（ts 重用已写入的 last_probe_ts）
                state["history"].append((state["last_probe_ts"],
                                         round(latency_ms, 1)))
```

### 卡 5 Step 2 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 GREEN**：288 tests，exit 0。新增 +5（ProbeHistoryV3Test）。**AC8 回归**：test_probe_spike_due_matrix（标量形态）继续保持绿（_latency_p90 双形态适配）。**R5 freeze**：三消费点同卡对齐——_probe_loop append (ts, ms) → _latency_p90 tuple 分支 → _probe_spike_due 元数据。

---

## 卡 6｜stats_snapshot 扩展（tier A）— 代码

### 目标

新增 `_percentile(samples, q)` 推广；stats_snapshot 浅拷区加 `daily_by_key` 深拷、phase/qwait/settle/stalls 本地拷贝；perf 节追加 per-model 三阶段分位、stalls_by_model 直出、qwait/settle 分位、ttfb_hist_by_model 原样浅拷。依赖卡 2/3/4（所有新 STATS 键已有写入源 + P2.10 序列化安全已就位）。

### 文件

`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

---

### Step 1：失败测试——新增 `SnapshotV3Test`

插在 ProbeHistoryV3Test 之后、RequestIdTest 之前。

**old_string**（锚：ProbeHistoryV3Test 末行——此文本仅存于卡 5 执行后）：

```
            stop_fake_upstreams()


class RequestIdTest(unittest.TestCase):
```

**new_string**：

```

class SnapshotV3Test(unittest.TestCase):
    """v3 stats_snapshot perf 新键 + daily_by_key 深拷 + percentile 推广（决策 6）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_stats_snapshot_contains_v3_keys(self) -> None:
        mod = self.mod
        snap = mod.stats_snapshot()
        self.assertIn("daily_by_key", snap)
        self.assertIn("hourly_tokens", snap)
        self.assertIsInstance(snap["hourly_tokens"], list)
        perf = snap["perf"]
        for key in ("ttfb_hist_by_model", "phase_p50_ms_by_model",
                    "phase_p90_ms_by_model", "stalls_by_model",
                    "qwait_p50_ms_by_model", "qwait_p90_ms_by_model",
                    "tpm_settle_ratio_p50_by_model",
                    "tpm_settle_ratio_p90_by_model"):
            self.assertIn(key, perf, "perf 缺 v3 键 %s" % key)

    def test_qwait_percentile_matches_nearest_rank(self) -> None:
        mod = self.mod
        orig = mod.STATS["qwait_ms_by_model"]
        mod.STATS["qwait_ms_by_model"] = {}
        try:
            samples = [float((i * 7) % 100) for i in range(100)]
            for v in samples:
                mod._record_request("POST", "/q", 200, 1.0, 0, model="m1",
                                    qwait_ms=v)
            snap = mod.stats_snapshot()
            perf = snap["perf"]
            ordered = sorted(samples)
            expected_p90 = round(float(ordered[int(0.9 * (len(ordered) - 1))]), 1)
            expected_p50 = round(float(ordered[int(0.5 * (len(ordered) - 1))]), 1)
            self.assertEqual(perf["qwait_p90_ms_by_model"]["m1"], expected_p90)
            self.assertEqual(perf["qwait_p50_ms_by_model"]["m1"], expected_p50)
        finally:
            mod.STATS["qwait_ms_by_model"] = orig

    def test_settle_ratio_percentile_matches_nearest_rank(self) -> None:
        mod = self.mod
        orig_b = mod.TPM_BUCKETS
        orig_r = mod.STATS["tpm_settle_ratio_by_model"]
        mod.TPM_BUCKETS = {}
        mod.STATS["tpm_settle_ratio_by_model"] = {}
        try:
            mod.TPM_BUCKETS[("k1", "m1")] = mod._TpmBucket()
            # 50 个样本：0.5, 0.6, ..., 1.4（10 个值各 5 次）
            for i in range(50):
                ratio = 0.5 + (i % 10) * 0.1
                mod.tpm_settle("k1", 100, int(100 * ratio), "m1")
            ordered = sorted(
                [0.5 + (i % 10) * 0.1 for i in range(50)])
            expected_p90 = round(float(ordered[int(0.9 * (len(ordered) - 1))]), 3)
            snap = mod.stats_snapshot()
            self.assertEqual(
                snap["perf"]["tpm_settle_ratio_p90_by_model"]["m1"],
                expected_p90)
        finally:
            mod.TPM_BUCKETS = orig_b
            mod.STATS["tpm_settle_ratio_by_model"] = orig_r

    def test_hourly_tokens_json_serializable(self) -> None:
        mod = self.mod
        mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                            tokens_prompt=5, tokens_completion=2)
        snap = mod.stats_snapshot()
        self.assertIsInstance(snap["hourly_tokens"], list)
        self.assertGreaterEqual(len(snap["hourly_tokens"]), 1)
        self.assertIn("hour_start_ts", snap["hourly_tokens"][-1])
        json.dumps(snap)  # deque 裸引用会 TypeError——此 j 即证明已安全转换

    def test_ttfb_hist_by_model_in_perf(self) -> None:
        mod = self.mod
        orig = mod.STATS["ttfb_hist"]
        mod.STATS["ttfb_hist"] = {"m1": [0] * 9, "m2": [0] * 9}
        try:
            snap = mod.stats_snapshot()
            self.assertEqual(snap["perf"]["ttfb_hist_by_model"],
                             {"m1": [0] * 9, "m2": [0] * 9})
        finally:
            mod.STATS["ttfb_hist"] = orig

    def test_daily_by_key_snapshot_is_deep_copy(self) -> None:
        mod = self.mod
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            mod._record_request("POST", "/k", 200, 1.0, 0, model="m1",
                                key_id12="abcdef123456", tokens_prompt=10)
            snap = mod.stats_snapshot()
            entry = snap["daily_by_key"][mod.today_key()]["abcdef123456"]
            self.assertEqual(entry["tokens_prompt"], 10)
            # 深拷贝验证：篡改 snap 不影响 STATS
            entry["tokens_prompt"] = 999
            self.assertEqual(
                mod.STATS["daily_by_key"][mod.today_key()]["abcdef123456"][
                    "tokens_prompt"], 10)
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_phase_percentile_by_model(self) -> None:
        mod = self.mod
        orig = mod.STATS["phase_ms_by_model"]
        mod.STATS["phase_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/p", 200, 1.0, 0, model="m1",
                                phase_ms={"connect": 50.0, "headers": 150.0,
                                          "body": 400.0})
            p50 = mod.stats_snapshot()["perf"]["phase_p50_ms_by_model"]["m1"]
            self.assertEqual(set(p50), {"connect", "headers", "body"})
            self.assertGreaterEqual(p50["connect"], 0)
        finally:
            mod.STATS["phase_ms_by_model"] = orig


class RequestIdTest(unittest.TestCase):
```

### 卡 6 Step 1 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 RED**：
- 总测试数：295（288 + 7）
- 失败：~5（test_contains_v3_keys 缺 perf 键 FAIL、qwait/settle percentiles 键不存在 FAIL、ttfb_hist_by_model FAIL、phase_percentile FAIL、daily_by_key_snapshot_is_copy 深拷缺位致 RAW ref mutation 溯源 FAIL。test_hourly_tokens_json_serializable PASS——P2.10 已覆盖序列化安全）

---

### Step 2：实现（GREEN）

#### 改动 P6.1 — _percentile 推广（proxy.py :1356 后，_latency_p90 尾）

**old_string**（锚：_latency_p90 return + 空行 + _probe_spike_due——注意此文本存在于卡 5 执行后，_latency_p90 尾为双形态版）：

```
        ordered = sorted(ms for _, ms in samples)
    else:
        ordered = sorted(samples)
    return float(ordered[int(0.9 * (len(ordered) - 1))])


def _probe_spike_due(history, factor: float = PROBE_LATENCY_SPIKE_FACTOR) -> bool:
```

**new_string**：

```
        ordered = sorted(ms for _, ms in samples)
    else:
        ordered = sorted(samples)
    return float(ordered[int(0.9 * (len(ordered) - 1))])


def _percentile(samples, q: float) -> float:
    """样本（可迭代，元素可比）的 q 分位（最近秩法：idx = int(q * (n - 1))）。
    空序列 → 0.0。_latency_p90 = _percentile(samples, 0.9) 的推广（v3 P8/T4 分位用）。
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return float(ordered[int(q * (len(ordered) - 1))])


def _probe_spike_due(history, factor: float = PROBE_LATENCY_SPIKE_FACTOR) -> bool:
```

#### 改动 P6.2 — stats_snapshot 浅拷区加 daily_by_key 深拷（proxy.py :1726-1729）

**old_string**：

```
        snap["daily_by_model"] = {
            d: {m: dict(v) for m, v in models.items()}
            for d, models in STATS["daily_by_model"].items()}
        snap["recent"] = list(RECENT_REQUESTS)
```

**new_string**：

```
        snap["daily_by_model"] = {
            d: {m: dict(v) for m, v in models.items()}
            for d, models in STATS["daily_by_model"].items()}
        snap["daily_by_key"] = {
            d: {k: dict(v) for k, v in keys.items()}
            for d, keys in STATS["daily_by_key"].items()}
        snap["recent"] = list(RECENT_REQUESTS)
```

#### 改动 P6.3 — stats_snapshot 本地拷贝（proxy.py :1731-1734）

**old_string**（anchor relies on Card 2 P2.10 having already modified this block — the phase_hist line exists in both pre and post P2.10 states）：

```
        ttfb_hist = {m: list(h) for m, h in STATS["ttfb_hist"].items()}
        phase_hist = {k: list(v) for k, v in STATS["phase_ms"].items()}
        rates = dict(STATS["rates"])
```

**new_string**：

```
        ttfb_hist = {m: list(h) for m, h in STATS["ttfb_hist"].items()}
        phase_hist = {k: list(v) for k, v in STATS["phase_ms"].items()}
        phase_by_model = {m: {k: list(v) for k, v in hists.items()}
                          for m, hists in STATS["phase_ms_by_model"].items()}
        qwait_by_model = {m: list(ring)
                          for m, ring in STATS["qwait_ms_by_model"].items()}
        settle_by_model = {m: list(ring)
                           for m, ring in STATS["tpm_settle_ratio_by_model"].items()}
        stalls_by_model = dict(STATS["stalls_by_model"])
        rates = dict(STATS["rates"])
```

#### 改动 P6.4 — perf 节初始化追加 v3 键（proxy.py :1743-1747）

**old_string**：

```
    perf = {"ttfb_p50_ms_by_model": {}, "ttfb_p90_ms_by_model": {},
            "phase_p50_ms": {}, "bytes_per_s": 0.0, "chunks_per_s": 0.0,
            "tokens_per_s": 0.0, "stalls_total": stalls_total,
            "stream_share": 0.0}
```

**new_string**：

```
    perf = {"ttfb_p50_ms_by_model": {}, "ttfb_p90_ms_by_model": {},
            "phase_p50_ms": {}, "bytes_per_s": 0.0, "chunks_per_s": 0.0,
            "tokens_per_s": 0.0, "stalls_total": stalls_total,
            "stream_share": 0.0,
            # v3：P1 直方图数据源 + per-model 分位（P6/P7/P8/T4）
            "ttfb_hist_by_model": ttfb_hist,
            "phase_p50_ms_by_model": {}, "phase_p90_ms_by_model": {},
            "stalls_by_model": stalls_by_model,
            "qwait_p50_ms_by_model": {}, "qwait_p90_ms_by_model": {},
            "tpm_settle_ratio_p50_by_model": {},
            "tpm_settle_ratio_p90_by_model": {}}
```

#### 改动 P6.5 — perf 节追加 per-model 分位循环（proxy.py :1752-1754）

**old_string**（锚：全局 phase_p50 loop + elapsed 行）：

```
    for name in ("connect", "headers", "body"):
        perf["phase_p50_ms"][name] = round(
            hist_percentile(phase_hist[name], HIST_BUCKETS_MS, 0.5), 1)
    elapsed = time.monotonic() - rates["window_start"]
```

**new_string**：

```
    for name in ("connect", "headers", "body"):
        perf["phase_p50_ms"][name] = round(
            hist_percentile(phase_hist[name], HIST_BUCKETS_MS, 0.5), 1)
    # v3 P6：per-model 三阶段分位（9 桶直方图，与全局同口径）
    for model, hists in phase_by_model.items():
        perf["phase_p50_ms_by_model"][model] = {
            name: round(hist_percentile(hists[name], HIST_BUCKETS_MS, 0.5), 1)
            for name in ("connect", "headers", "body")}
        perf["phase_p90_ms_by_model"][model] = {
            name: round(hist_percentile(hists[name], HIST_BUCKETS_MS, 0.9), 1)
            for name in ("connect", "headers", "body")}
    # v3 P8/T4：qwait 与结算偏差最近秩分位（_percentile 推广，round 口径与 AC5 手工计算一致）
    for model, samples in qwait_by_model.items():
        perf["qwait_p50_ms_by_model"][model] = round(_percentile(samples, 0.5), 1)
        perf["qwait_p90_ms_by_model"][model] = round(_percentile(samples, 0.9), 1)
    for model, samples in settle_by_model.items():
        perf["tpm_settle_ratio_p50_by_model"][model] = round(
            _percentile(samples, 0.5), 3)
        perf["tpm_settle_ratio_p90_by_model"][model] = round(
            _percentile(samples, 0.9), 3)
    elapsed = time.monotonic() - rates["window_start"]
```

### 卡 6 Step 2 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v3
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期 GREEN**：295 tests，exit 0。新增 +7（SnapshotV3Test）。

---

### B2 段收尾确认

- Card 4: 283 tests OK（+5）
- Card 5: 288 tests OK（+5；含 AC8 回归——test_probe_spike_due_matrix 旧标量绿）
- Card 6: 295 tests OK（+7）
- **冲突检查**：
  - **C4**（card-boundary 冲突）— Card 2 引入 deques 致 /api/stats 序列化崩溃，已在本 dispatch 中 amend Card 2 增加 P2.10（序列化安全覆盖），Card 6 在此基础上扩 perf 节。无残留冲突。
  - **C5**（spec 偏离）— _latency_p90 未按 spec 原文「改解包」做纯 tuple-only，而是**双形态**（tuple + scalar），理由：AC11「现有全部用例绿」——test_probe_spike_due_matrix 传标量列表，纯 tuple 会 TypeError。AC8「与旧标量版等价（回归）」经新 test_latency_p90_tuples_equivalent_to_scalar_oracle 直测。在 plan 中定死此决策并注明。
  - 全部生产锚点已 B0 核验，卡内 old_string 均唯一锚定。无 placeholder 残留。
