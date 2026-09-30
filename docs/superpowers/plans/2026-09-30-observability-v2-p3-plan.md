# PLAN — observability v2 P3（Token / daily_by_model v2）

**Spec 权威源**：`docs/superpowers/specs/2026-09-30-ctyun-proxy-observability-v2-design.md`（P3 节 + Risks R2）
**分支/worktree**：`fix/observability-v2-p3`，基于 main@9905ccc（P1+P2 已合入；main 另已合入 TPM 限流特性）
**本 PLAN 范围**：仅 P3。P4（三态 outcome 正式化/probe）、P5（dashboard 重构）不进本 PLAN。
**卡片数**：4（全 A/B，完整代码写死，executor 转写）。串行依赖：卡1 → 卡2 → 卡3 → 卡4。

---

## Global Constraints

1. **TDD 顺序**：每卡先写失败测试（`/usr/bin/python3 ctyun-stream-fix-proxy.test.py <TestClass>.<method>`，禁 `-m unittest`），红 → 实现 → 绿 → 全量回归。exit code 取真值：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py ...; echo $?`。
2. **baseline**：169 全绿（已实跑，2026-09-30）。VERIFY 只对"baseline 绿 → 现在红"负责；若 baseline 阶段有红（未出现），先报告主代理，不自行扩 scope。
3. **R1 热路径纪律（最高优先级）**：`_relay_sse` 每 record 不得新增 json.loads。usage 抽取复用现有 `sse_line_usage` 已解析的 dict（卡3 在现有 TPM 捕获循环内加一行 `usage_dict_tokens` 纯函数调用）；禁止 per-record 持锁、禁止 per-record 落盘/日志。每 record 新增成本 = 一次 dict.get 级纯函数调用。
4. **R2 向后兼容**：`load_daily_buckets`/`load_daily_by_model_buckets` 已按 `_DAILY_FIELDS` 白名单迭代——切换 16 字段后旧 7 字段桶自动补 0，新 16 字段桶被旧版二进制读入自动丢新字段。**不加版本号字段**（spec P3 明示）。降级演练由卡2 `DailyV2CompatTest` 锁死。
5. **零新依赖**：stdlib only。不引入 numpy/structlog 等。
6. **锁纪律**：所有 daily/daily_by_model/rates 变更仍在 `STATS_LOCK` 内一次完成（沿用现有 `_record_request` 模式）；`MODEL_PRICING` 仅 main() 启动时写、之后只读，不参与运行时锁。
7. **不改转发语义**：剥行/重试/finish-hold/priming 路径零行为变化；观测是叠加层。
8. **热切换一致性**：`set_upstream_base`/`set_capture_errors` 的 `persist_upstream` 调用后紧跟 `save_stats_counters`，model_pricing 键不会被持久化覆盖丢失（卡3 说明，无代码变化）。
9. **禁手改 spec 外文件**：改动仅限 `ctyun-stream-fix-proxy.py` / `ctyun-stream-fix-proxy.test.py` / `README.md`。
10. **spec 行号漂移说明**：spec R31 锚点行号是 TPM 合入前的快照。TPM 特性（~300 行）已并入 main，worktree 内实际行号整体后移。本 PLAN 全部使用**实测锚点表**（见文末），执行时以实测表为准，spec 行号仅作函数名对照。

---

## Anchor Reconciliation（spec 与 main 已合入 TPM 特性的冲突调和）

**冲突**：spec P3 要求"新增 `sse_line_extract_usage(line)`，同解析路径返回三元组，复用 json.loads 一次"。但 main 已合入 TPM 限流特性，其中已存在 `sse_line_usage(line)`（:206，返回 usage dict）且 `_relay_sse` record 终结块已有逐行调用它捕获 `self._tpm_usage`（:1572-1576）。

**裁决**（依据 brief 锚点调和输入 2 与 spec R1"共享 loads 结果，零额外 parse"）：
1. **不新增**独立做 json.loads 的 `sse_line_extract_usage` 解析函数。改为两层纯函数拆分，保证"复用解析一次"：
   - 新增 `usage_dict_tokens(usage: dict) -> tuple | None`：接收**已解析**的 usage dict，提取 `(prompt_tokens, completion_tokens, total_tokens)`。纯函数，零 IO、零 parse。
   - 新增 `sse_line_extract_usage(line: bytes) -> tuple | None`：薄包装 = `usage_dict_tokens(sse_line_usage(line))`，满足 spec 的函数名与测试接口字面（UsageExtractTest 锚此名），但解析实际委托给已存在的 `sse_line_usage`（单次 json.loads）。
   - `_relay_sse` 热循环**不调用** `sse_line_extract_usage`（那会二次调用 `sse_line_usage`→二次 parse 判空）；而是在现有 TPM 捕获循环内，`usage_hit = sse_line_usage(buf_line)` 命中后追加一行 `self._p3_usage_tokens = usage_dict_tokens(usage_hit)`——零额外 json.loads。
2. **非流式路径**同样复用：`_proxy_relay` buffered 分支已对响应体做一次 `json.loads`（:1460）供 body_error 与 TPM settle 共用；P3 在该分支同块内追加 `usage_dict_tokens(usage)`，不新增 parse。
3. spec 字面"新增函数"与"复用解析一次"原则冲突时，**原则优先**（brief 明示）；函数名保留，落点与 spec 的"同解析路径"一致（同一 sse_line_usage 解析路径）。

**R2 补充**：`load_stats_events` 的 kind 白名单（:928 `("proxy", "upstream", "retry")`）是 P4（probe_alert）的降级点，本 P3 不动（spec R2 明示 P4 处理）。

---

## 任务卡 1/4 — `usage_dict_tokens` + `sse_line_extract_usage`（tier A）

**目标**：P3 数值抽取纯函数落地，spec 测试接口名 `sse_line_extract_usage` 可用；`UsageExtractTest` 矩阵锁死抽取语义。生产路径零接入（卡3 才接入热循环）。

**改动文件**：
- `ctyun-stream-fix-proxy.py`（新增两函数）
- `ctyun-stream-fix-proxy.test.py`（新增测试类）

### 生产代码（ctyun-stream-fix-proxy.py）

位置：`sse_line_usage` 函数之后（实测 :224 `sse_line_usage` 结束处、`sse_line_body_error` 之前）。以 `sse_line_body_error(line: bytes) -> bool:` 定义行为插入锚点。

```python
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


def sse_line_extract_usage(line: bytes):
    """SSE data 行 usage 帧数值抽取：返回 (prompt_tokens, completion_tokens, total_tokens)。

    无 usage 帧（非 data: 前缀 / [DONE] / 非 JSON / 非 dict / usage 空）→ None。
    解析委托 sse_line_usage（单次 json.loads，与 sse_line_has_usage 同解析路径）；
    热路径上不重复调用本函数——_relay_sse 直接用 usage_dict_tokens 消费已解析 dict（R1）。
    """
    usage = sse_line_usage(line)
    if usage is None:
        return None
    return usage_dict_tokens(usage)
```

### 测试代码（ctyun-stream-fix-proxy.test.py）

位置：`P1ConstantsTest` 类之后（实测 :3894 起）插入。

```python
class UsageExtractTest(unittest.TestCase):
    """P3 Token期：usage 帧数值抽取矩阵（标准/缺 prompt/缺 completion/extra key/非 dict/空 usage/负值）。"""

    @classmethod
    def setUpClass(cls) -> None:
        # staticmethod 包装：裸函数赋类属性会成为 method descriptor（self.extract 多传一参）
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

### 验证命令

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py UsageExtractTest; echo "EXIT=$?"
```
红（ImportError/AttributeError：`sse_line_extract_usage` 不存在）→ 实现 → 绿。随后全量回归见卡4 收尾。

---

## 任务卡 2/4 — daily_by_model 切 16 字段 + `_record_request` 增量 + 向后兼容加载（tier A）

**目标**：`_DAILY_FIELDS` 切换为 `DAILY_V2_FIELDS`（16 字段）；四处 entry 创建 + `_record_request` 增量点升级；旧 7 字段持久化加载补 0、新格式降级演练由 `DailyV2CompatTest` 锁死；同步修正 4 个受 schema 变化影响的既有测试断言。

**改动文件**：
- `ctyun-stream-fix-proxy.py`
- `ctyun-stream-fix-proxy.test.py`

### 生产代码（ctyun-stream-fix-proxy.py）

**2a. `_DAILY_FIELDS` 切换**（实测 :854-855）：

旧：
```python
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream",
                 "retries", "eof_without_done", "header_retries")
```
新：
```python
# v2 P3：daily/daily_by_model 切换 16 字段 schema（DAILY_V2_FIELDS 已在 P1 声明，见下）。
# load/save 循环与四处 entry 创建均按 _DAILY_FIELDS 迭代 → 切换后旧 7 字段桶自动补 0，
# 新 16 字段桶被旧版二进制读入自动丢新字段（R2 双向 degrade，无需版本号）。
_DAILY_FIELDS = DAILY_V2_FIELDS
```
注意：`DAILY_V2_FIELDS` 定义在 :860-864，位于 `_DAILY_FIELDS` 定义之后——切换后 `_DAILY_FIELDS` 直接引用该常量（同一 tuple 对象），无需移动定义位置。

**2b. 新增 `_outcome_tri_state` 纯函数**（插在 `classify_outcome` 之后，实测 :268 后、`# --- TPM rate limiting` 注释前）：

```python
def _outcome_tri_state(outcome) -> str:
    """classify_outcome category 字符串 → 三态（"ok"|"degraded"|"failed"）。

    P4 将把此映射内聚进 classify_outcome 返回的 _Outcome 新增 tri_state 字段；
    P3 先行用本函数落 daily outcome_* 桶（字段已由 DAILY_V2_FIELDS 就位）。
    """
    if outcome == CLASS_OK:
        return "ok"
    if outcome in (CLASS_POISON_FIXED, CLASS_CLIENT_ABORT):
        return "degraded"
    return "failed"
```

**2c. `_record_request` 签名扩展**（实测 :985-989）：

旧：
```python
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None,
                    chunks: int = 0, phase_ms: dict = None) -> None:
```
新：
```python
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None,
                    chunks: int = 0,
                    tokens_prompt: int = 0, tokens_completion: int = 0,
                    phase_ms: dict = None) -> None:
```

**2d. `_record_request` 的 daily_by_model entry 创建**（实测 :999-1003）：

旧：
```python
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0}
```
新：
```python
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                # v2 P3：16 字段 schema（DAILY_V2_FIELDS），与另三处创建点形状同步
                entry_dm = day_models[model] = dict.fromkeys(_DAILY_FIELDS, 0)
```

**2e. `_record_request` 的 daily 总桶创建**（实测 :1011-1014）：

旧：
```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "header_retries": 0})
```
新：
```python
        bucket = STATS["daily"].setdefault(
            today_key(), dict.fromkeys(_DAILY_FIELDS, 0))
```

**2f. `_record_request` 增量点**——在 `entry_dm` 的 errors 增量后（实测 :1004-1010 块后）插入 daily_by_model 新字段增量；在 `bucket` 的 errors 增量后（实测 :1015-1020 块后）插入 daily 总桶新字段增量：

```python
            # v2 P3：dm entry 增量（tokens/bytes/stream/延迟/三态归因）
            if tokens_prompt > 0:
                entry_dm["tokens_prompt"] += tokens_prompt
            if tokens_completion > 0:
                entry_dm["tokens_completion"] += tokens_completion
            if bytes_out > 0:
                entry_dm["bytes_out"] += bytes_out
            if stream:
                entry_dm["stream_requests"] += 1
            if ttfb_ms is not None:
                entry_dm["ttfb_sum_ms"] += round(ttfb_ms)
                entry_dm["ttfb_count"] += 1
            if outcome is not None:
                entry_dm["outcome_" + _outcome_tri_state(outcome)] += 1
```
（插在 `entry_dm` 的 errors 增量分支之后、`bucket = STATS["daily"].setdefault` 之前）

```python
        # v2 P3：daily 总桶增量（与 dm entry 同口径）
        if tokens_prompt > 0:
            bucket["tokens_prompt"] += tokens_prompt
        if tokens_completion > 0:
            bucket["tokens_completion"] += tokens_completion
        if bytes_out > 0:
            bucket["bytes_out"] += bytes_out
        if stream:
            bucket["stream_requests"] += 1
        if ttfb_ms is not None:
            bucket["ttfb_sum_ms"] += round(ttfb_ms)
            bucket["ttfb_count"] += 1
        if outcome is not None:
            bucket["outcome_" + _outcome_tri_state(outcome)] += 1
```
（插在 `bucket` 的 errors 增量之后、`if error or status >= 500:` EVENTS 块之前）

**2g. 另三处 entry 创建点形状同步**（`_record_empty_retry` 实测 :1085-1089 / :1092-1095、`_record_eof_without_done` 实测 :1112-1116 / :1119-1122、`_record_header_retry` 实测 :1138-1142 / :1145-1148）——三函数各两处（dm entry + daily 桶），共六处字面量替换：

旧（以 `_record_empty_retry` 的 dm entry 为例，另两处同形）：
```python
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0}
```
新：
```python
                # v2 P3：16 字段 schema（与 _record_request 创建点形状同步）
                entry_dm = day_models[model] = dict.fromkeys(_DAILY_FIELDS, 0)
```
daily 桶创建三处同法：
旧：
```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "header_retries": 0})
```
新：
```python
        bucket = STATS["daily"].setdefault(
            today_key(), dict.fromkeys(_DAILY_FIELDS, 0))
```
注意：三函数 docstring 中"dm entry 同为 6 字段/形状"等字样随改注释为"16 字段（DAILY_V2_FIELDS）"。

**2h. `aggregate_daily_range` docstring 修正**（实测 :740-754，零逻辑改动，仅注释）：

旧 docstring 行：
```python
    返回 7 键 dict：_DAILY_FIELDS 六字段 + days=命中桶数。
```
新：
```python
    返回 len(_DAILY_FIELDS)+1 键 dict：_DAILY_FIELDS 全字段（v2 P3 起 16 字段）+ days=命中桶数。
```

### 测试代码（ctyun-stream-fix-proxy.test.py）

**2i. 既有测试断言同步**（受 schema 变化影响的 4 个既有测试，必须随本卡一并修正，否则全量回归红）：

1. `test_aggregate_daily_range_sums_and_days`（实测 :1273-1282）两处精确 dict 断言升 16 字段：
旧：
```python
        self.assertEqual(out, {"requests": 8, "filtered": 1, "errors_proxy": 0,
                               "errors_upstream": 1, "retries": 0,
                               "eof_without_done": 0, "header_retries": 0,
                               "days": 2})
```
新：
```python
        expected = dict.fromkeys(mod.DAILY_V2_FIELDS, 0)
        expected.update({"requests": 8, "filtered": 1, "errors_upstream": 1,
                         "retries": 0, "days": 2})
        self.assertEqual(out, expected)
```
（`mod` 为该测试函数内的 `f = self.mod.aggregate_daily_range` 对应的模块对象；函数首行补 `mod = self.mod`。空窗口断言同法改为 `dict.fromkeys(mod.DAILY_V2_FIELDS, 0)` + `"days": 0`。）

2. `test_daily_by_model_matrix_fallback`（实测 :1393-1397 与 :1426-1430）两处 shape 断言：
旧：
```python
        self.assertEqual(
            set(dm_today["m-a"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries",
             "eof_without_done", "header_retries"},
            "dm entry shape must stay in sync across both creation sites")
```
新：
```python
        self.assertEqual(
            set(dm_today["m-a"]),
            set(mod.DAILY_V2_FIELDS),
            "dm entry shape must stay in sync across both creation sites (16 fields)")
```
第二处（:1426-1430，`_record_empty_retry` 创建点）同法替换，断言消息改为 "empty-retry creation site must keep dm entry shape in sync (16 fields)"。

3. `test_daily_bucket_accumulation_and_dual_error_semantics`（实测 :1341-1343）手工预置桶升 16 字段：
旧：
```python
        bucket = mod.STATS["daily"].setdefault(
            today, {"requests": 0, "filtered": 0, "errors_proxy": 0,
                    "errors_upstream": 0, "retries": 0})
```
新：
```python
        bucket = mod.STATS["daily"].setdefault(
            today, dict.fromkeys(mod.DAILY_V2_FIELDS, 0))
```
（若仍用 5 字段手工桶，`_record_request` 新增量点 `bucket["tokens_prompt"] += ...` 必 KeyError——同步升级是硬依赖。）

4. `test_daily_by_model_persist_roundtrip`（实测 :1545-1560 用例1 matrix、:1581-1587 用例3 断言、:1599-1603 用例4 断言）——矩阵 entry 升 16 字段（save→load 全等断言才能成立）：
旧 matrix（三处 entry 字面量）：
```python
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0},
```
新（统一写法，每处替换）：
```python
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0},
```
用例3 断言（:1582-1586）与用例4 断言（:1599-1603）同法补 9 个新字段 0 值（坏字段归 0 / 缺字段补 0 的 16 字段期望）。

**2j. 新增 `DailyV2CompatTest`**（插在 `ProxyDashboardUnitTest` 类之后或文件尾部）：

```python
class DailyV2CompatTest(unittest.TestCase):
    """P3 Token期：daily_by_model v2 向后兼容双向 degrade（R2 锁死）。

    旧 7 字段 JSON → 16 字段内存桶零值补齐；
    新 16 字段 JSON → 7 字段加载函数（模拟旧版二进制）只取 7 字段不崩。
    """

    def test_legacy_7_field_load_fills_new_fields_with_zero(self) -> None:
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p3compat-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": {
                "2026-01-02": {"requests": 5, "filtered": 1, "errors_proxy": 0,
                               "errors_upstream": 2, "retries": 1,
                               "eof_without_done": 0, "header_retries": 0}},
                "daily_by_model": {
                    "2026-01-02": {"m1": {"requests": 3, "filtered": 0,
                                          "errors_proxy": 0, "errors_upstream": 0,
                                          "retries": 0, "eof_without_done": 0,
                                          "header_retries": 0}}}}}, fh)
        buckets = mod.load_daily_buckets(path)
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(set(buckets["2026-01-02"]), set(mod.DAILY_V2_FIELDS),
                         "legacy 7-field bucket must load with 16-field zero-fill")
        self.assertEqual(buckets["2026-01-02"]["tokens_prompt"], 0)
        self.assertEqual(buckets["2026-01-02"]["stream_requests"], 0)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(set(dbm["2026-01-02"]["m1"]), set(mod.DAILY_V2_FIELDS),
                         "legacy 7-field dm entry must load with 16-field zero-fill")
        self.assertEqual(dbm["2026-01-02"]["m1"]["tokens_completion"], 0)

    def test_new_16_field_downgrade_reads_7_fields_without_crash(self) -> None:
        """降级演练：16 字段 JSON 被'旧版 7 字段白名单'加载函数读入 → 只返 7 字段不崩。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p3compat-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        entry16 = dict.fromkeys(mod.DAILY_V2_FIELDS, 0)
        entry16.update({"requests": 4, "tokens_prompt": 11, "tokens_completion": 7,
                        "stream_requests": 2, "bytes_out": 999})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "2026-01-02": {"m1": entry16}}}}, fh)
        legacy_7 = ("requests", "filtered", "errors_proxy", "errors_upstream",
                    "retries", "eof_without_done", "header_retries")

        def legacy_loader(raw) -> dict:
            # 模拟旧版二进制：_DAILY_FIELDS 7 字段白名单迭代
            return {k: raw.get(k, 0) for k in legacy_7}

        raw_entry = json.loads(open(path, encoding="utf-8").read())[
            "stats"]["daily_by_model"]["2026-01-02"]["m1"]
        out = legacy_loader(raw_entry)
        self.assertEqual(out["requests"], 4)
        self.assertEqual(set(out), set(legacy_7),
                         "old 7-field whitelist must silently drop the 9 new fields")
        self.assertNotIn("tokens_prompt", out)

    def test_roundtrip_16_field_full_equality(self) -> None:
        """16 字段桶 save→load 全等（含新字段值）。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p3compat-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig = mod.STATS["daily_by_model"]
        try:
            mod.STATS["daily_by_model"] = {}
            mod._record_request("POST", "/x", 200, 3.0, 0, model="m-a", stream=1,
                                ttfb_ms=120.0, outcome=mod.CLASS_OK,
                                tokens_prompt=11, tokens_completion=7,
                                tokens=18, bytes_out=500, chunks=2)
            entry = mod.STATS["daily_by_model"][mod.today_key()]["m-a"]
            self.assertEqual(entry["tokens_prompt"], 11)
            self.assertEqual(entry["tokens_completion"], 7)
            self.assertEqual(entry["stream_requests"], 1)
            self.assertEqual(entry["bytes_out"], 500)
            self.assertEqual(entry["ttfb_sum_ms"], 120)
            self.assertEqual(entry["ttfb_count"], 1)
            self.assertEqual(entry["outcome_ok"], 1)
            self.assertEqual(entry["outcome_degraded"], 0)
            self.assertEqual(entry["outcome_failed"], 0)
            mod.save_stats_counters(path)
            self.assertEqual(mod.load_daily_by_model_buckets(path),
                             mod.STATS["daily_by_model"],
                             "16-field buckets must roundtrip verbatim")
        finally:
            mod.STATS["daily_by_model"] = orig

    def test_outcome_tri_state_mapping(self) -> None:
        """_outcome_tri_state 映射矩阵（P4 正式化前的 P3 落桶口径）。"""
        mod = load_proxy_module()
        f = mod._outcome_tri_state
        self.assertEqual(f(mod.CLASS_OK), "ok")
        self.assertEqual(f(mod.CLASS_POISON_FIXED), "degraded")
        self.assertEqual(f(mod.CLASS_CLIENT_ABORT), "degraded")
        self.assertEqual(f(mod.CLASS_UPSTREAM_FAULT), "failed")
        self.assertEqual(f(mod.CLASS_BODY_ERROR), "failed")
        self.assertEqual(f(mod.CLASS_REQUEST_FAULT), "failed")
```

### 验证命令

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py DailyV2CompatTest; echo "EXIT=$?"
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest; echo "EXIT=$?"
```
红（16 字段断言失败 / KeyError）→ 实现 → 绿。第二行确保 2i 的 4 个既有测试修正同步到位（它们都在 `ProxyDashboardUnitTest` 类内）。

---

## 任务卡 3/4 — tokens 穿透 relay 路径 + stream_requests + model_pricing schema（tier A）

**目标**：usage 数值从 `_relay_sse`/buffered 路径经 `_proxy_relay` 传入 `_record_request`；`CTYUN_MODEL_PRICING` env seam + persist 顶层 `model_pricing` schema（默认 off，无 cost UI）；`/api/config` GET 回显；README env seam 表一行。

**改动文件**：
- `ctyun-stream-fix-proxy.py`
- `ctyun-stream-fix-proxy.test.py`
- `README.md`

### 生产代码（ctyun-stream-fix-proxy.py）

**3a. `_relay_sse` 现有 TPM 捕获循环追加 P3 tokens 捕获**（实测 :1572-1576）：

旧：
```python
                    # TPM usage 捕获：本 record 内所有行取最后非空 usage 帧
                    for buf_line in pending:
                        usage_hit = sse_line_usage(buf_line)
                        if usage_hit is not None:
                            self._tpm_usage = usage_hit
```
新：
```python
                    # TPM usage 捕获：本 record 内所有行取最后非空 usage 帧；
                    # v2 P3 同循环顺手消费已解析 dict——零额外 json.loads（R1）
                    for buf_line in pending:
                        usage_hit = sse_line_usage(buf_line)
                        if usage_hit is not None:
                            self._tpm_usage = usage_hit
                            self._p3_usage_tokens = usage_dict_tokens(usage_hit)
```

**3b. `_proxy_relay` 两处 `self._tpm_usage = None` 重置处并行初始化 `_p3_usage_tokens`**（实测 :1352 与 :1391）：

两处旧（同形）：
```python
                self._tpm_usage = None
```
两处新（同形）：
```python
                self._tpm_usage = None
                self._p3_usage_tokens = None
```

**3c. 流式 `_record_request` 调用传 tokens**（实测 :1441-1448）：

旧：
```python
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```
新：
```python
            p3_tokens = self._p3_usage_tokens
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category,
                            tokens=p3_tokens[2] if p3_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_tokens[0] if p3_tokens else 0,
                            tokens_completion=p3_tokens[1] if p3_tokens else 0,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```
（插入位置在 `outcome = classify_outcome(...)` 与 `_record_request` 之间；注意 `p3_tokens` 读取发生在 `_record_request` 调用前即可，与 TPM settle 同区域。）

**3d. buffered 路径 P3 tokens 抽取**（实测 :1457-1472）：

旧：
```python
            tpm_final_used = tpm_est if tpm_key is not None else None
            if relayed_data:
                try:
                    parsed = json.loads(relayed_data.decode("utf-8", "replace"))
                except ValueError:
                    pass  # non-JSON body -> no body error, fail-open
                else:
                    body_error = body_has_error(parsed)
                    # TPM settle：解析 usage.total_tokens 校正（非 dict / 无 usage 不校正）
                    if tpm_key is not None and isinstance(parsed, dict):
                        usage = parsed.get("usage")
                        if isinstance(usage, dict):
                            total = usage.get("total_tokens")
                            if isinstance(total, int) and total >= 0:
                                tpm_settle(tpm_key, tpm_est, total)
                                tpm_final_used = total
```
新：
```python
            tpm_final_used = tpm_est if tpm_key is not None else None
            p3_buf_tokens = None
            if relayed_data:
                try:
                    parsed = json.loads(relayed_data.decode("utf-8", "replace"))
                except ValueError:
                    pass  # non-JSON body -> no body error, fail-open
                else:
                    body_error = body_has_error(parsed)
                    # TPM settle：解析 usage.total_tokens 校正（非 dict / 无 usage 不校正）；
                    # v2 P3：同一 parsed dict 顺手抽 usage 数值——零额外 json.loads（R1）
                    if isinstance(parsed, dict):
                        usage = parsed.get("usage")
                        if isinstance(usage, dict):
                            p3_buf_tokens = usage_dict_tokens(usage)
                            if tpm_key is not None:
                                total = usage.get("total_tokens")
                                if isinstance(total, int) and total >= 0:
                                    tpm_settle(tpm_key, tpm_est, total)
                                    tpm_final_used = total
```

**3e. buffered `_record_request` 调用传 tokens**（实测 :1479-1486）：

旧：
```python
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```
新：
```python
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category,
                            tokens=p3_buf_tokens[2] if p3_buf_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_buf_tokens[0] if p3_buf_tokens else 0,
                            tokens_completion=p3_buf_tokens[1] if p3_buf_tokens else 0,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

**3f. `MODEL_PRICING` 全局 + `load_model_pricing` 函数**（`CAPTURE_ERRORS` 全局声明后，实测 :622 后）：

```python
MODEL_PRICING: dict = {}  # v2 P3：model 价目表（可选 cost 估算），main() 启动时置值，之后只读
```

`load_capture_errors` 函数之后（实测 :851 后）：
```python
def load_model_pricing(path: str) -> dict:
    """从持久化文件顶层读 model_pricing；缺/损坏/非 dict → {}。
    env seam CTYUN_MODEL_PRICING（JSON 字符串）优先级更高，由 main() 覆盖。"""
    data = _load_persist_file(path)
    val = data.get("model_pricing") if isinstance(data, dict) else None
    return val if isinstance(val, dict) else {}
```

**3g. `save_stats_counters` 落盘含 model_pricing**（实测 :817-821）：

旧：
```python
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
                  fh, ensure_ascii=False)
```
新：
```python
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "model_pricing": MODEL_PRICING,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
                  fh, ensure_ascii=False)
```

**3h. `main()` 启动装载 model_pricing**（实测 :2453 `CAPTURE_ERRORS = load_capture_errors(...)` 后）：

```python
    # v2 P3：model_pricing 装载（env seam 优先，否则持久化文件；解析失败回落 {}）
    pricing_env = os.environ.get("CTYUN_MODEL_PRICING", "")
    if pricing_env:
        try:
            MODEL_PRICING = json.loads(pricing_env)
        except ValueError:
            # 吞掉的是 env 里非法 JSON 字符串：价目表 best-effort，回落 {} 即可，无其他路径可达。
            MODEL_PRICING = {}
        if not isinstance(MODEL_PRICING, dict):
            MODEL_PRICING = {}
    else:
        MODEL_PRICING = load_model_pricing(PERSIST_PATH)
```
注意：`main()` 顶部 `global` 声明行（实测 :2446）需追加 `MODEL_PRICING`：
```python
    global UPSTREAM_BASE, _upstream_source, _stats_dirty, CAPTURE_ERRORS, MODEL_PRICING
```

**3i. `/api/config` GET 回显 model_pricing**（实测 :1741-1745）：

旧：
```python
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS}
            self._send_json(200, payload)
```
新：
```python
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS,
                           "model_pricing": MODEL_PRICING}
            self._send_json(200, payload)
```

**3j. README env seam 表一行**（实测 :58 行后、`CTYUN_HEADER_RETRY` 行后）：

```markdown
| `CTYUN_MODEL_PRICING` | 空 | 模型价目表 JSON 字符串（可选 cost 估算数据源）；仅落 persist 顶层 `model_pricing` schema，不实现任何计费 UI |
```

### 测试代码（ctyun-stream-fix-proxy.test.py）

**3k. `TokenRelayTest`**（插在 `UsageExtractTest` 后）：

```python
class TokenRelayTest(unittest.TestCase):
    """P3 Token期：usage 数值穿透 relay 路径落 daily_by_model + stream_requests 计数。"""

    def setUp(self) -> None:
        upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_sse_usage_frame_lands_in_daily_by_model(self) -> None:
        """POST 带 SSE_USAGE(prompt=1,completion=1,total=2) 的流 → daily_by_model 当日
        tokens_prompt=1/tokens_completion=1，daily 总桶 stream_requests=1。"""
        data = post_sse(self.proxy_port)
        self.assertEqual(data, SSE_USAGE + SSE_A + SSE_B + SSE_DONE,
                         "stream must relay byte-exact")
        today = time.strftime("%Y-%m-%d")
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        dm = snap["daily_by_model"].get(today, {})
        self.assertEqual(len(dm), 1, "exactly one model expected, got %r" % sorted(dm))
        entry = list(dm.values())[0]
        self.assertEqual(entry["tokens_prompt"], 1)
        self.assertEqual(entry["tokens_completion"], 1)
        self.assertEqual(entry["stream_requests"], 1)
        self.assertEqual(entry["requests"], 1)
        daily_today = snap["daily"][today]
        self.assertEqual(daily_today["tokens_prompt"], 1,
                         "daily total bucket must mirror dm tokens_prompt")
        self.assertEqual(daily_today["stream_requests"], 1)
        # RECENT 条目 tokens 键携带数值三元组
        entry_recent = snap["recent"][-1]
        self.assertIsNone(entry_recent.get("_p3_usage_tokens"),
                          "internal capture attr must not leak into snapshot")
        self.assertIsNotNone(entry_recent.get("tokens"),
                             "RECENT tokens key must be filled by P3")
```

**3l. `ModelPricingSeamTest`**：

```python
class ModelPricingSeamTest(unittest.TestCase):
    """P3 Token期：CTYUN_MODEL_PRICING env seam + persist model_pricing schema（默认 off）。"""

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_default_off_persists_empty_schema(self) -> None:
        """无 env → /api/config model_pricing == {}；SIGTERM 后 persist 文件含该键。"""
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], {},
                         "model_pricing must default to {} (cost UI off)")
        post_sse(self.proxy_port)   # 触发一次 dirty 落盘
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["model_pricing"], {},
                         "persist top-level model_pricing schema must be present")

    def test_env_seam_overrides_persist(self) -> None:
        """CTYUN_MODEL_PRICING env JSON → /api/config 回显该 dict（默认 off 被覆盖）。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        pricing = {"deepseek-v4": {"prompt": 0.1, "completion": 0.2}}
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            extra_env={"CTYUN_MODEL_PRICING": json.dumps(pricing)})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], pricing,
                         "env seam must override default with full dict")

    def test_bad_env_json_falls_back_empty(self) -> None:
        """非法 JSON env → {} 不崩。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={"CTYUN_MODEL_PRICING": "{not-json"})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], {})

    def test_persist_resume_roundtrip(self) -> None:
        """persist 文件已有 model_pricing（无 env）→ 重启后 /api/config 回读同值。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        pricing = {"m-x": {"prompt": 1.5, "completion": 2.5}}
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                          "model_pricing": pricing})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], pricing,
                         "persist model_pricing must resume across restart")
```

### 验证命令

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py TokenRelayTest; echo "EXIT=$?"
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ModelPricingSeamTest; echo "EXIT=$?"
```
红（AttributeError `_p3_usage_tokens` / KeyError `tokens_prompt` 为 0 / config 无 model_pricing 键）→ 实现 → 绿。

---

## 任务卡 4/4 — `TokenPersistTest` 端到端 + 全量回归收尾（tier A）

**目标**：spec P3 端到端验收——POST 一次带 usage 帧 SSE → SIGTERM → 重启 → daily_by_model 含 tokens_prompt>0；随后跑全量 169+ 用例回归，确认 baseline 零回退。

**改动文件**：
- `ctyun-stream-fix-proxy.test.py`（仅新增测试类）

### 测试代码（ctyun-stream-fix-proxy.test.py）

插在 `ModelPricingSeamTest` 后：

```python
class TokenPersistTest(unittest.TestCase):
    """P3 Token期端到端：POST 带 usage 帧 SSE → SIGTERM → 重启 → daily_by_model tokens_prompt>0。

    spec P3 Acceptance：daily_by_model 当日当模型 entry 含 tokens_prompt ≥ 1（持久化闭环）。"""

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_token_persist_survives_sigterm_restart(self) -> None:
        """POST 一次带 usage 帧 SSE → SIGTERM → 重启 → daily_by_model tokens_prompt>0。"""
        today = time.strftime("%Y-%m-%d")
        data = post_sse(self.proxy_port)
        self.assertEqual(data, SSE_USAGE + SSE_A + SSE_B + SSE_DONE,
                         "stream must relay byte-exact")

        # 内存态
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        dm = snap["daily_by_model"][today]
        self.assertEqual(len(dm), 1)
        model_key = list(dm)[0]
        self.assertEqual(dm[model_key]["tokens_prompt"], 1)
        self.assertEqual(dm[model_key]["tokens_completion"], 1)

        # SIGTERM 落盘
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        persist_file = os.path.join(self.proc.persist_dir, "settings.json")
        with open(persist_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["stats"]["daily_by_model"][today][model_key][
                             "tokens_prompt"], 1,
                         "tokens_prompt must hit disk on SIGTERM")

        # 重启（同 persist 内容 seed）→ daily_by_model tokens_prompt>0
        self.proc = start_proxy(self.upstream_port, free_port(), seed_persist=saved)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap2 = json.loads(body.decode("utf-8"))
        dm2 = snap2["daily_by_model"].get(today, {})
        self.assertIn(model_key, dm2, "model entry must survive restart")
        self.assertGreaterEqual(dm2[model_key]["tokens_prompt"], 1,
                                "tokens_prompt must survive SIGTERM+restart")
        self.assertGreaterEqual(dm2[model_key]["tokens_completion"], 1,
                                "tokens_completion must survive SIGTERM+restart")
        self.assertGreaterEqual(dm2[model_key]["stream_requests"], 1,
                                "stream_requests must survive SIGTERM+restart")
```

### 验证命令（含 whole-branch 全量回归收尾）

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py TokenPersistTest; echo "EXIT=$?"
/usr/bin/python3 ctyun-stream-fix-proxy.test.py; echo "EXIT=$?"
```
红（首跑 TokenPersistTest 前，端到端闭环未通——取决于前三卡状态；本卡验证以绿+全量绿为 gate）。全量预期 ≥169+20 新增（UsageExtractTest 10 + DailyV2CompatTest 4 + TokenRelayTest 1 + ModelPricingSeamTest 4 + TokenPersistTest 1 = 20 新增；最终以实际输出为准）。全量必须 100% 绿（OK），无 FAILED/ERROR。

---

## 实测锚点表（2026-09-30 实跑 grep，TPM 合入后的 worktree 行号）

```
ctyun-stream-fix-proxy.py:
:139-147   sse_line_has_usage 定义块开始（保留不动）
:190       def sse_line_has_usage(line: bytes) -> bool:
:206       def sse_line_usage(line: bytes):          ← 卡1 复用的解析函数
:224       sse_line_usage 结束（return usage if ...）
:226       def sse_line_body_error(line: bytes) -> bool:   ← 卡1 新函数插入锚点（其前）
:243       def classify_outcome(...)
:268       classify_outcome 结束 → 卡2b _outcome_tri_state 插入点
:601       _CFG_LOCK = threading.Lock()
:602       STATS_LOCK = threading.Lock()
:603-613   STATS = {...}（含 v2 P2 ttfb_hist/phase_ms/rates/stalls_total）
:616       RECENT_REQUESTS（schema 注释含 tokens/bytes_out/outcome）
:622       CAPTURE_ERRORS = False  ← 卡3f MODEL_PRICING 全局插入点（其后）
:740       def aggregate_daily_range   ← 卡2h docstring 修正
:797       def save_stats_counters     ← 卡3g json.dump 含 model_pricing
:817-821   json.dump({"upstream_base": base, "capture_errors": ...})  ← 卡3g 替换点
:836       def load_stats_counters
:847       def load_capture_errors     ← 卡3f load_model_pricing 插入点（:851 后）
:854-855   _DAILY_FIELDS = (...)       ← 卡2a 切换为 DAILY_V2_FIELDS
:860-864   DAILY_V2_FIELDS = _DAILY_FIELDS + (...)（16 字段，P1 已声明）
:867       def load_daily_buckets（for field in _DAILY_FIELDS 循环——零改动自动兼容）
:885       def load_daily_by_model_buckets（for field in _DAILY_FIELDS 循环——零改动自动兼容）
:916       def load_stats_events（kind 白名单 :928，P3 不动）
:985-989   def _record_request(...)    ← 卡2c 签名扩展
:999-1003  _record_request dm entry 创建   ← 卡2d
:1004-1010 _record_request dm entry 增量   ← 卡2f dm 增量插入点（后）
:1011-1014 _record_request daily 桶创建    ← 卡2e
:1015-1020 _record_request daily 桶增量    ← 卡2f daily 增量插入点（后）
:1021-1025 EVENTS append（error/upstream 事件流，不动）
:1026-1032 RECENT_REQUESTS.append（tokens 键已存在，卡3 自动填充）
:1033-1059 v2 P2 histogram/rates 块（if tokens: 处 :1056——P3 起 tokens 非 None 即累计）
:1070       def _record_empty_retry    ← 卡2g entry×2
:1085-1089 _record_empty_retry dm entry ← 卡2g
:1092-1095 _record_empty_retry daily 桶 ← 卡2g
:1101       def _record_eof_without_done ← 卡2g entry×2
:1112-1116 _record_eof_without_done dm entry ← 卡2g
:1119-1122 _record_eof_without_done daily 桶 ← 卡2g
:1127       def _record_header_retry   ← 卡2g entry×2
:1138-1142 _record_header_retry dm entry ← 卡2g
:1145-1148 _record_header_retry daily 桶 ← 卡2g
:1153       def stats_snapshot
:1224       def _proxy(self)
:1255       def _proxy_relay(self, started: float)
:1352       self._tpm_usage = None     ← 卡3b（流式入口重置）
:1391       self._tpm_usage = None     ← 卡3b（空流重试重置）
:1429-1434 TPM settle 块（total_tokens 校正，不动）
:1441-1448 流式 _record_request 调用   ← 卡3c
:1450-1500 buffered 分支
:1457       tpm_final_used = tpm_est if ...  ← 卡3d
:1460       parsed = json.loads(...)   ← 卡3d 复用解析
:1479-1486 buffered _record_request 调用 ← 卡3e
:1532       def _relay_sse
:1572-1576 TPM usage 捕获循环           ← 卡3a
:1652       def _relay_buffered
:1702       def _log
:1728       class AdminHandler
:1741-1745 /api/config GET            ← 卡3i
:2445       def main()
:2446       global UPSTREAM_BASE, ...  ← 卡3h 追加 MODEL_PRICING
:2453       CAPTURE_ERRORS = load_capture_errors(PERSIST_PATH) ← 卡3h 插入点（后）

ctyun-stream-fix-proxy.test.py:
:43        SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
:245       def make_fake_upstream(...)
:300       def make_scripted_upstream(**kwargs) -> tuple:
:414       def start_proxy(upstream_port, proxy_port, extra_env=None, seed_persist=None)
:448       def post_sse(port, payload=None) -> bytes
:561       def load_proxy_module()
:573       def admin_get(port, path, headers=None)
:1263      test_aggregate_daily_range_sums_and_days ← 卡2i-1 断言修正
:1341-1343 test_daily_bucket_accumulation_... 手工桶 ← 卡2i-3
:1374      test_daily_by_model_matrix_fallback ← 卡2i-2 两处 shape 断言
:1537      test_daily_by_model_persist_roundtrip ← 卡2i-4 三处断言修正
:3894      class P1ConstantsTest
:3915      class RequestIdTest（test_recent_entry... :3955 断言 tokens is None——默认流无 usage，卡3 后仍成立）
:4403      class TtfbStreamTest
```

---

## Plan Self-Review（5 项）

1. **Spec coverage**：P3 六要点全覆盖——①usage 数值抽取（卡1 双函数 + 卡3 热循环零额外 parse）②daily_by_model 16 字段 + 四处 entry 创建/increment（卡2）③向后兼容双向 degrade（卡2 DailyV2CompatTest，R2）④streaming 占比 stream_requests（卡2 增量 + 卡3 穿透）⑤CTYUN_MODEL_PRICING env seam + persist model_pricing（卡3，默认 off 无 cost UI）⑥TokenPersistTest 端到端（卡4）。spec P3 提及的 `aggregate_daily_range`/`range_stats`/`_prune_daily` "零改动"要求由卡2 仅改 docstring 满足。
2. **Placeholder scan**：四卡全部含完整生产代码块 + 完整测试代码块 + 验证命令；无"待定/后续补/TODO"字样。
3. **Type consistency**：`tokens` 参数语义全卡一致——`_record_request.tokens` = total（int|None，进 RECENT 与 rates）；`tokens_prompt`/`tokens_completion` = int（进 daily 桶）；`usage_dict_tokens` 返回三元组 `(p, c, t)` 与 `_relay_sse._p3_usage_tokens` 消费点（`p3_tokens[0]/[1]/[2]`）下标一致。`_outcome_tri_state` 返回 `"ok"|"degraded"|"failed"` 与 `outcome_ok/degraded/failed` 字段名拼接一致。
4. **可落盘性**：每卡代码块均含精确 old/new 替换文本与插入锚点（实测行号），executor 可直接转写；卡2i 列出 4 个受 schema 变化影响的既有测试修正（缺此卡全量回归必红，已实测核对断言行）。
5. **锚点实测**：文末实测锚点表为本 PLAN 写作时实跑 grep 结果；spec R31 行号（TPM 合入前）已注明漂移并在表内给出当前行号。TPM 重叠冲突已在 Anchor Reconciliation 节裁决（复用解析一次原则 > 新增函数字面）。

**风险备忘**：卡2 与卡3 强耦合（tokens_prompt 增量依赖 16 字段 entry 创建）；卡序串行 1→2→3→4 不可乱。卡3 的 `test_sse_usage_frame_lands_in_daily_by_model` 断言 `len(dm)==1`——依赖 post_sse 默认 payload 单 model 且 fake upstream 无并发，若环境抖动出现多模型键，改为 `assertGreaterEqual(len(dm), 1)` 并取 `max(dm, key=...)`（executor 遇此情况 STOP 报告，不自行决策）。

**写作期已裁决的锚点冲突（实测驱动）**：卡1 `test_empty_usage_dict_all_zero` 原稿断言 `(0,0,0)`，但委托函数 `sse_line_usage` 对空 usage dict `{}`（falsy）返回 `None`（同 `sse_line_has_usage` 的 `bool(usage)` 判据，:203/:224 实测），extract 透传该 None 语义。已改为 `test_empty_usage_dict_returns_none` + `assertIsNone`，与 `sse_line_extract_usage` docstring"usage 空 → None"一致，也与 `test_has_usage_bool_consistent_with_extract` 的 has_usage⟺extract 非 None 恒等式自洽。新增用例总数=20（UsageExtractTest 10 + DailyV2CompatTest 4 + TokenRelayTest 1 + ModelPricingSeamTest 4 + TokenPersistTest 1），全量预期 169+20=189。
