# PLAN.md — TPM 残留三项（per-model 预算 + 超大空闲放行 + 拒绝计数）

## Header

- **Plan**: 2026-09-30-tpm-residuals-plan
- **Spec**: docs/superpowers/specs/2026-09-30-tpm-residuals-design.md
- **Base commit**: 9905ccc（feat(proxy): P2 perf snapshot...）
- **Spec base**: a2212b2（vs 盘上 9905ccc，P2 perf snapshot 已合入 → 锚点漂移；行号以下文实测为准）
- **Files affected**: `ctyun-stream-fix-proxy.py` + `ctyun-stream-fix-proxy.test.py`
- **Total cards**: 3（全 A 档，≤4 → 单次 dispatch，不分段）
- **Verification**: `python3 ctyun-stream-fix-proxy.test.py`（baseline 169 例全绿，已实测）

## P2 漂移对齐声明

spec 锚点以 a2212b2 为基准；worktree 基点 9905ccc 已合入 P2 perf snapshot（`HIST_BUCKETS_MS`/`PROBE_*`/`STALL_THRESHOLD_S`/`PerfHistTest`/`TtfbStreamTest`/`StallTailGapTest` 等），行号漂移：

| spec 锚点 | spec 行号 | 盘上实际行号 | 漂移原因 |
|-----------|----------|------------|---------|
| `TPM_LIMIT` | :390 (implied) | :57 | P2 常量段 +30 行插入 |
| `_TpmWaiter` | :273 | :273 | 未漂移（P2 未动此段） |
| `TPM_BUCKETS` | :281 | :281 | 同上 |
| `_tpm_get_bucket` | :340 | :340 | 同上 |
| `tpm_admit` | :370 | :370 | 同上 |
| `tpm_settle` | :424 | :424 | 同上 |
| `tpm_snapshot` | :444 | :444 | 同上 |
| `extract_model` | ~:1258 | :671 | 漂移至 P1 段（与 spec 无关，import 位置不同） |
| `_proxy_relay` | :1258 | :1255 | P2 函数插入漂移 -3 |
| tpm_admit 调用 | :1268 | :1268 | 位置不变 |
| tpm_settle 调用 | :1327/:1373/:1433/:1471 | :1327/:1373/:1433/:1471 | 同上 |

**语义不变**：上述行号漂移属无关插入（P2 常量/函数定义），TPM 核心段（:273–:480）与 `_proxy_relay` 内 TPM 调用未受 P2 变更影响。本文档以下所有行号均为盘上实测行号。

## Global Constraints

1. **Python 3 only**：无类型标注（typed dict 注释），`model` 可为 `None`。
2. **验证命令**：`python3 ctyun-stream-fix-proxy.test.py`（各卡均全量跑，不得有回归）。
3. **TDD 顺序**：每卡先写/改测试 → 测试红 → 写/改生产代码 → 测试绿 → 全量回归绿。
4. **每卡独立 commit**：commit message 按卡下方给出。
5. **测试确定性优先**：延续 `_tpm_cleanup`/`_TpmBucket`/受控 `now` 模式 + env seam。
6. **`_log` 签名不动**：P1+TPM 双字段叠加态，本 episode 仅新增 tpm_used 字段值不改方法签名。
7. **tpm_snapshot 不加 `model` 参数**：spec 决策 1 措辞提及 snapshot 签名加 model，但 Files 锚点清单与 Acceptance 无一使用；快照始终返回全量桶，条目以 `"model"` 字段展示，不按 model 过滤。（偏差理由详见 Card 2 设计备注。）
8. **`_parse_tpm_limit_by_model` 前置于常量区**：spec 锚点 "estimate_request_tokens() 之后" 不可行——该函数在模块加载时即由 `TPM_LIMIT_BY_MODEL = _parse_tpm_limit_by_model(...)`（:63 常量区）调用，函数定义必须先于调用。前置至 `# TPM rate limiting` 注释下方，语义不变。（偏差理由：Python 模块级执行序。）
9. **`_parse_tpm_limit_by_model` 日志机制**：spec 定 "跳过该项并 `_safe_log_stderr` 一行"。`_safe_log_stderr` 定义于 :783（晚于常量区 :63 模块加载时调用点），调用时尚未定义会 NameError。改为内联 `print(file=sys.stderr, flush=True)` + try/except OSError——语义不变（一行 stderr + fail-open），且避免了 LOG_RING/LOG_LOCK 同样不可用（:627-629）的连锁问题。
10. **`tpm_limit_for` 置于 `estimate_request_tokens` 之后**：spec 锚点可满足（运行时查询，无前向依赖），保持与 spec 一致。

---

## 卡 1/3（A 档）—— per-model 预算解析器 + 常量 + `tpm_limit_for`

**TDD 顺序**：先加 fail 测试（parse + limit_for；新增全红）→ 加生产代码 → 测试绿 → 全量 `python3 ctyun-stream-fix-proxy.test.py` 169 + 5 例全绿。

**生产代码变更（`ctyun-stream-fix-proxy.py`）**：

### 插入 1：两个解析函数，放在 `# TPM rate limiting（per-key 滚动窗口 + FIFO 排队）` 注释下方（~:55 之后，:56 之前）

定位：在 `HEADER_RETRY_MAX = max(0, int(os.environ.get("CTYUN_HEADER_RETRY", "1")))`（:54）之后的空行后、`# TPM rate limiting（per-key 滚动窗口 + FIFO 排队）`（:56）注释**之前**插入。

```python
# per-model 预算解析（fail-open；模块加载时即调用 → 前置于此，保证常量区 :63 可见）
def _parse_tpm_limit_by_model_log(item: str) -> None:
    # 模块加载时（常量区）_safe_log_stderr（:783）尚未定义，内联其等价实现；
    # 吞掉的是 stderr 写失败的 OSError（BrokenPipeError/ENOSPC 等）：stderr 是
    # best-effort 诊断出口，无备用通道，且失败不得传播——传播会中断模块导入。
    try:
        print("ctyun-stream-fix-proxy: CTYUN_TPM_LIMIT_BY_MODEL: "
              "skip malformed entry %r" % item, file=sys.stderr, flush=True)
    except OSError:
        pass


def _parse_tpm_limit_by_model(env_str: str) -> dict:
    """解析 CTYUN_TPM_LIMIT_BY_MODEL（"model:limit,model:limit"）→ {model: limit}。

    fail-open：逐项 split(":")，非两项 / limit 非 int / limit 负值 → 跳过该项并
    stderr 一行；空串 / 全畸形 → {}。内置默认（kimi-k3-oc:30000）不并入本表，
    由 tpm_limit_for 兜底——保证快照 config.limit_by_model 原样展示 env 表。
    """
    result = {}
    if not env_str or not env_str.strip():
        return result
    for item in env_str.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2 or not parts[0].strip():
            _parse_tpm_limit_by_model_log(item)
            continue
        model = parts[0].strip()
        try:
            limit = int(parts[1].strip())
        except ValueError:
            # 吞掉的是非整数值（"abc" 等）：fail-open 跳过该项并已 log，无其他路径可达。
            _parse_tpm_limit_by_model_log(item)
            continue
        if limit < 0:
            _parse_tpm_limit_by_model_log(item)
            continue
        result[model] = limit
    return result
```

### 插入 2：常量，紧跟 TPM_KEY_CAP（:62 之后）

`TPM_KEY_CAP = int(os.environ.get("CTYUN_TPM_KEY_CAP", "64"))`（:62）之后插入：

```python
# per-model 预算（决策 2）：env seam "model:limit,model:limit"（逗号分隔）；
# 解析 fail-open 见 _parse_tpm_limit_by_model；内置默认不并入本表以保快照原样展示。
TPM_LIMIT_BY_MODEL: dict = _parse_tpm_limit_by_model(
    os.environ.get("CTYUN_TPM_LIMIT_BY_MODEL", ""))
_TPM_LIMIT_BY_MODEL_BUILTIN: dict = {"kimi-k3-oc": 30000}  # 实证 31k 触顶留余量；env 表优先覆盖
```

### 插入 3：`tpm_limit_for`，放在 `estimate_request_tokens` 返回之后、`_tpm_prune` 之前（:339 之后）

`estimate_request_tokens` 函数返回后（~:339，即 `return base` 行后）、`def _tpm_prune`（~:341-342）之前插入：

```python

def tpm_limit_for(model) -> int:
    """per-model 预算查询：env 表 → 内置默认表 → 全局 TPM_LIMIT。

    model 为 None / 空字符串 / 查不到 → 回退 TPM_LIMIT（全局默认）。"""
    if model:
        limit = TPM_LIMIT_BY_MODEL.get(model)
        if limit is not None:
            return limit
        limit = _TPM_LIMIT_BY_MODEL_BUILTIN.get(model)
        if limit is not None:
            return limit
    return TPM_LIMIT
```

**测试代码变更（`ctyun-stream-fix-proxy.test.py`）**：

以下 5 个新方法加入 `ProxyDashboardUnitTest` 类（:651），建议放在 `test_tpm_constants_env_seam`（:928–950 区域）之后，并在该类中已有 `import` 顶层无需新增 imports。

```python
    def test_parse_tpm_limit_by_model_normal(self) -> None:
        f = self.mod._parse_tpm_limit_by_model
        self.assertEqual(f("kimi:30000,glm:110000"), {"kimi": 30000, "glm": 110000})
        self.assertEqual(f(" kimi:30000 , glm-5.3-oc:110000 "),
                         {"kimi": 30000, "glm-5.3-oc": 110000})
        self.assertEqual(f("kimi:30000"), {"kimi": 30000})

    def test_parse_tpm_limit_by_model_malformed(self) -> None:
        f = self.mod._parse_tpm_limit_by_model
        # 非两项 / limit 非 int / 负值 → 跳过
        self.assertEqual(f("kimi"), {})
        self.assertEqual(f("kimi:abc"), {})
        self.assertEqual(f("kimi:-1"), {})
        # 混合：OK + 畸形的串；有效项保留
        self.assertEqual(f("kimi:30000,bad,glm:abc,neg:-5,deepseek:110000"),
                         {"kimi": 30000, "deepseek": 110000})
        # 空项跳过
        self.assertEqual(f("kimi:30000,,glm:110000"), {"kimi": 30000, "glm": 110000})

    def test_parse_tpm_limit_by_model_empty(self) -> None:
        self.assertEqual(self.mod._parse_tpm_limit_by_model(""), {})
        self.assertEqual(self.mod._parse_tpm_limit_by_model("   "), {})

    def test_tpm_limit_for_fallback_and_env_override(self) -> None:
        mod = self.mod
        orig = mod.TPM_LIMIT_BY_MODEL
        try:
            # ① env 表空 → kimi 命中内置默认，其余回全局 TPM_LIMIT
            mod.TPM_LIMIT_BY_MODEL = {}
            self.assertEqual(mod.tpm_limit_for("kimi-k3-oc"), 30000)
            self.assertEqual(mod.tpm_limit_for("glm-5.3-oc"), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for(None), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for(""), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for("deepseek-v4-pro-0813-oc"),
                             mod.TPM_LIMIT)
            # ② env 表覆盖内置默认
            mod.TPM_LIMIT_BY_MODEL = {"kimi-k3-oc": 1000, "glm-5.3-oc": 110000}
            self.assertEqual(mod.tpm_limit_for("kimi-k3-oc"), 1000)
            self.assertEqual(mod.tpm_limit_for("glm-5.3-oc"), 110000)
            self.assertEqual(mod.tpm_limit_for("other"), mod.TPM_LIMIT)
        finally:
            mod.TPM_LIMIT_BY_MODEL = orig
```

现有测试方法更新：

_（1）`test_tpm_constants_load`_（~:909）——追加两行断言：

```python
    def test_tpm_constants_load(self) -> None:
        """TPM 常量通过 load_proxy_module 可见且取值正确。"""
        mod = self.mod
        self.assertEqual(mod.TPM_LIMIT, 110000)
        self.assertEqual(mod.TPM_WINDOW_S, 60)
        self.assertEqual(mod.TPM_QUEUE_MAX, 20)
        self.assertEqual(mod.TPM_QUEUE_TIMEOUT_S, 120)
        self.assertEqual(mod.TPM_TOKEN_RATIO, 0.25)
        self.assertEqual(mod.TPM_KEY_CAP, 64)
        self.assertEqual(mod._TPM_LIMIT_BY_MODEL_BUILTIN, {"kimi-k3-oc": 30000})
        self.assertIsInstance(mod.TPM_LIMIT_BY_MODEL, dict)
        self.assertEqual(mod.CLASS_TPM_LIMITED, "tpm_limited")
        self.assertEqual(mod.ERR_KIND_TPM_QUEUE_FULL, "tpm_queue_full")
        self.assertEqual(mod.ERR_KIND_TPM_QUEUE_TIMEOUT, "tpm_queue_timeout")
        self.assertIn(mod.ERR_KIND_TPM_QUEUE_FULL, mod._KIND_CATEGORY)
        self.assertIn(mod.ERR_KIND_TPM_QUEUE_TIMEOUT, mod._KIND_CATEGORY)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_TPM_QUEUE_FULL],
                         mod.CLASS_TPM_LIMITED)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_TPM_QUEUE_TIMEOUT],
                         mod.CLASS_TPM_LIMITED)
```

_（2）`test_tpm_constants_env_seam`_（~:928）——追加 CTYUN_TPM_LIMIT_BY_MODEL 到 subprocess code 与断言：

```python
    def test_tpm_constants_env_seam(self) -> None:
        """TPM 常量从环境变量读取（用 subprocess 验证 env seam）。"""
        import subprocess
        code = (
            "import os; "
            "os.environ['CTYUN_TPM_LIMIT']='50000'; "
            "os.environ['CTYUN_TPM_WINDOW_S']='30'; "
            "os.environ['CTYUN_TPM_QUEUE_MAX']='5'; "
            "os.environ['CTYUN_TPM_QUEUE_TIMEOUT_S']='10'; "
            "os.environ['CTYUN_TPM_TOKEN_RATIO']='0.3'; "
            "os.environ['CTYUN_TPM_KEY_CAP']='8'; "
            "os.environ['CTYUN_TPM_LIMIT_BY_MODEL']='kimi:5000,glm:6000'; "
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('m', %r); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "print(m.TPM_LIMIT, m.TPM_WINDOW_S, m.TPM_QUEUE_MAX, "
            "m.TPM_QUEUE_TIMEOUT_S, m.TPM_TOKEN_RATIO, m.TPM_KEY_CAP, "
            "repr(m.TPM_LIMIT_BY_MODEL))"
        ) % PROXY_SCRIPT
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0,
                         "env seam subprocess failed stderr:\n" + proc.stderr)
        parts = proc.stdout.strip().split(" ", 6)
        self.assertEqual(parts, ["50000", "30", "5", "10.0", "0.3", "8",
                                 "{'kimi': 5000, 'glm': 6000}"])
```

**验证命令**：
```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/tpm-residuals
python3 ctyun-stream-fix-proxy.test.py
```
预期全绿（baseline 169 例 + 5 新增 = 174 例全绿）。

**Commit message**:
```
feat(tpm): per-model budget parser with fail-open env seam

Add _parse_tpm_limit_by_model (fail-open "model:limit,model:limit" parser),
_tpm_limit_for (env → builtin → global fallback), and module-level
TPM_LIMIT_BY_MODEL / _TPM_LIMIT_BY_MODEL_BUILTIN constants.

Unit tests: normal parse, malformed skip+log, empty/whitespace, fallback
chain, env override, subprocess env-seam end-to-end.
```

---

## 卡 2/3（A 档）—— TPM 层全量重构（数据结构 + admit + settle + snapshot + _proxy_relay）

**TDD 顺序**：先改测试（8 unit + 2 integration → 红：signature mismatch/逻辑变更）→ 再改生产代码（6 函数 + 5 调用点）→ 测试绿 → `python3 ctyun-stream-fix-proxy.test.py` 全绿。

**生产代码变更（`ctyun-stream-fix-proxy.py`）**：

### 改 1：`_TpmWaiter` 加 `model` 字段（:273）

```python
_TpmWaiter = collections.namedtuple("_TpmWaiter", "key_id est enqueued_at model")
```

### 改 2：TPM_BUCKETS / _tpm_rejected / _tpm_timeouts 注释更新（:281-:285）

```python
TPM_BUCKETS: dict = {}                 # (key_id, model) -> _TpmBucket（deque 条目 + 附加属性 .used）
TPM_WAITERS: collections.deque = collections.deque()  # FIFO 排队（of _TpmWaiter）
_tpm_rejected: dict = {}               # (key_id, model) -> queue-full 拒绝计数（快照用）
_tpm_timeouts: dict = {}               # (key_id, model) -> queue-timeout 计数（快照用）
```

### 改 3：`_tpm_get_bucket(key_id, model)` 二元组桶键（:340–:355）

```python
def _tpm_get_bucket(key_id: str, model):
    """取 (key_id, model) 的桶；不存在则惰性创建（cap 满时先驱逐空桶）。仅在 TPM_LOCK 内调用。"""
    k = (key_id, model)
    bucket = TPM_BUCKETS.get(k)
    if bucket is None:
        if len(TPM_BUCKETS) >= TPM_KEY_CAP:
            # 惰性驱逐：窗口空且无排队的桶（窗口自动过期，最旧优先）
            idle = [bkey for bkey, b in TPM_BUCKETS.items()
                    if b.used <= 0
                    and not any((w.key_id, w.model) == bkey for w in TPM_WAITERS)]
            for bkey in idle:
                del TPM_BUCKETS[bkey]
        bucket = _TpmBucket()
        bucket.used = 0
        TPM_BUCKETS[k] = bucket
    return bucket
```

### 改 4：`tpm_admit(key_id, est, model)` 全量重写（:370–:423）

```python
def tpm_admit(key_id: str, est: int, model):
    """TPM 准入（与入队同原子）。返回 (status, qwait_ms)。

    status: "ok"（准入）/ "full"（队列满或 est 超预算且窗口非空闲，立即 429）/
            "timeout"（排队超时，429）。
    规则：
    - budget = tpm_limit_for(model)（per-model 表 → 内置默认 → TPM_LIMIT）。
    - est > budget → 空闲放行判定：prune 后 bucket.used <= 0 且无同桶 waiter →
      放行（超大请求占满窗口，后续排队至窗口滚过属预期）；否则 ("full", 0)
      并计 rejected（先 touch 建桶，拒绝计数可见）。
    - 窗口内充足 → 入桶 ("ok", 0)。
    - 否则入队 FIFO；仅队首 waiter 可被准入（严格 FIFO 防惊群）；
      wait 循环 TPM_LOCK.wait(timeout=min(1.0, remaining)) + deadline 检查
      （窗口过期靠 1s 粒度轮询，settle 时 notify_all 提前唤醒）。
    - 队满（len(TPM_WAITERS) >= TPM_QUEUE_MAX）→ ("full", 0) 不入队。
    - deadline 到仍未准入 → ("timeout", 0) 并自队列移除。
    排队期间不持有任何上游连接（hook 点在 _open_upstream 之前）。
    """
    budget = tpm_limit_for(model)
    started = time.time()
    with TPM_LOCK:
        bucket = _tpm_get_bucket(key_id, model)
        _tpm_prune(bucket, started)
        if est > budget:
            # 超大请求唯一例外：窗口空闲（used<=0）且无同桶排队 → 放行（决策 3）；
            # 否则硬拒并计 rejected（决策 4：先 touch 建桶，拒绝计数可见）。
            if bucket.used <= 0 and not any(
                    (w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS):
                bucket.append((started, est))
                bucket.used += est
                return ("ok", 0)
            _tpm_rejected[(key_id, model)] = \
                _tpm_rejected.get((key_id, model), 0) + 1
            return ("full", 0)
        if bucket.used + est <= budget:
            bucket.append((started, est))
            bucket.used += est
            return ("ok", 0)
        if len(TPM_WAITERS) >= TPM_QUEUE_MAX:
            _tpm_rejected[(key_id, model)] = \
                _tpm_rejected.get((key_id, model), 0) + 1
            return ("full", 0)
        waiter = _TpmWaiter(key_id=key_id, est=est, enqueued_at=started,
                            model=model)
        TPM_WAITERS.append(waiter)
        deadline = started + TPM_QUEUE_TIMEOUT_S
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    _tpm_timeouts[(key_id, model)] = \
                        _tpm_timeouts.get((key_id, model), 0) + 1
                    return ("timeout", 0)
                head = TPM_WAITERS[0] if TPM_WAITERS else None
                if head is waiter:
                    now = time.time()
                    bucket = _tpm_get_bucket(key_id, model)
                    _tpm_prune(bucket, now)
                    if bucket.used + est <= budget:
                        TPM_WAITERS.popleft()
                        bucket.append((now, est))
                        bucket.used += est
                        return ("ok", (now - started) * 1000)
                TPM_LOCK.wait(timeout=min(1.0, remaining))
        finally:
            # 超时/异常路径把自己从队列移除；已 popleft 的正常准入不会走到这。
            # 同一性摘除（_tpm_remove_waiter）：值相等的其它 waiter 不受牵连
            _tpm_remove_waiter(waiter)
```

### 改 5：`tpm_settle(key_id, est, actual, model)`（:424–:443）

```python
def tpm_settle(key_id: str, est: int, actual: int, model) -> None:
    """结算：以实际 usage 校正窗口占用。

    prune 后追加校正条目 (now, delta)，delta = actual - est（可为负 = 退款）；
    used 存原始值 used + delta，与追加条目自此保持一致（可为负 = 窗口欠账，
    随条目过期 prune 自愈）。settle 后 notify_all 唤醒排队 waiter 重试准入。
    """
    now = time.time()
    with TPM_LOCK:
        bucket = TPM_BUCKETS.get((key_id, model))
        if bucket is None:
            return  # 该 (key, model) 从未准入（不可能路径，防御处理）
        _tpm_prune(bucket, now)
        delta = actual - est
        if delta != 0:
            bucket.append((now, delta))
        bucket.used = bucket.used + delta
        TPM_LOCK.notify_all()
```

### 改 6：`tpm_snapshot()` 二元组桶遍历 + model 字段 + per-model remaining + limit_by_model（:444–:480）

```python
def tpm_snapshot() -> dict:
    """TPM 状态快照（/api/tpm_stats 端点用）。

    返回 {"config": {"limit", "window_s", "queue_max", "limit_by_model"},
          "queue_total", "buckets": [...]}；
    bucket 条目 {"key"（sha256: 前缀 + 前 12 位 hex）, "model", "used",
                 "remaining", "queued", "rejected", "timeouts"}——key 脱敏，
    无原始 key 泄漏。
    展示层钳位：存储 used 可为负（窗口欠账，见 _tpm_prune 不变量），
    对外 used 取 max(0, used)，remaining 按 per-model 预算计算，避免暴露/展示负值。
    """
    now = time.time()
    with TPM_LOCK:
        buckets = []
        for (key_id, model), bucket in TPM_BUCKETS.items():
            _tpm_prune(bucket, now)
            if bucket.used <= 0 and not any(
                    (w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS):
                continue  # 空桶不展示（噪声）
            buckets.append({
                "key": "sha256:" + key_id[:12],
                "model": model,
                "used": max(0, bucket.used),
                "remaining": max(0, tpm_limit_for(model) - bucket.used),
                "queued": sum(1 for w in TPM_WAITERS
                              if (w.key_id, w.model) == (key_id, model)),
                "rejected": _tpm_rejected.get((key_id, model), 0),
                "timeouts": _tpm_timeouts.get((key_id, model), 0),
            })
        return {
            "config": {
                "limit": TPM_LIMIT,
                "window_s": TPM_WINDOW_S,
                "queue_max": TPM_QUEUE_MAX,
                "limit_by_model": TPM_LIMIT_BY_MODEL,
            },
            "queue_total": len(TPM_WAITERS),
            "buckets": buckets,
        }
```

### 改 7：`_proxy_relay` 中 5 处 TPM 调用加 `model` 实参（:1255–:1500）

tpm_admit 调用（:1268）：
```python
            tpm_status, tpm_qwait_ms = tpm_admit(tpm_key, tpm_est, model)
```

tpm_settle 调用：
- :1327 → `tpm_settle(tpm_key, tpm_est, 0, model)`
- :1373 → `tpm_settle(tpm_key, tpm_est, 0, model)`
- :1433 → `tpm_settle(tpm_key, tpm_est, total, model)`
- :1471 → `tpm_settle(tpm_key, tpm_est, total, model)`

**设计备注**：`tpm_snapshot()` 签名不加 `model` 参数——spec 决策 1 措辞提到 "snapshot 签名加 model"，但 Files 锚点清单（:444 条目）与 Acceptance 无一使用（快照返回全量桶，model 作为条目字段而非过滤条件）。若加此参数为 dead interface。

---

**测试代码变更（`ctyun-stream-fix-proxy.test.py`）**：

### A. 单元测试更新（ProxyDashboardUnitTest 类 ~:651）

_（1）`test_tpm_admit_ok_immediate`_（~:1003）：

```python
    def test_tpm_admit_ok_immediate(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        status, qwait = mod.tpm_admit("key:imm", 100, "m:imm")
        self.assertEqual(status, "ok")
        self.assertEqual(qwait, 0)
        self.assertEqual(mod.TPM_BUCKETS[("key:imm", "m:imm")].used, 100)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
```

_（2）`test_tpm_admit_full_oversized`_（~:1012）——全量重写为新语义：

```python
    def test_tpm_admit_full_oversized(self) -> None:
        """est > budget：窗口空闲放行（决策 3）、忙时硬拒并计 rejected（决策 4）。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        # ① 空闲：used=0 且无同桶 waiter → 放行（budget 来自 tpm_limit_for）
        status, qwait = mod.tpm_admit("key:big", mod.TPM_LIMIT + 1, "m:big")
        self.assertEqual(status, "ok", "idle window must release oversized request")
        self.assertEqual(qwait, 0)
        self.assertEqual(mod.TPM_BUCKETS[("key:big", "m:big")].used,
                         mod.TPM_LIMIT + 1)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
        # ② 忙时：used > 0 → 硬拒 + rejected 计数（同桶）
        status2, qwait2 = mod.tpm_admit("key:big", mod.TPM_LIMIT + 1, "m:big")
        self.assertEqual(status2, "full", "busy window must reject oversized request")
        self.assertEqual(qwait2, 0)
        self.assertEqual(mod._tpm_rejected.get(("key:big", "m:big")), 1)
        self.assertEqual(len(mod.TPM_WAITERS), 0)  # 不入队
        # ③ 窗口滚过后（模拟 prune 后 used<=0）→ 再次空闲放行，模型隔离
        bucket = mod.TPM_BUCKETS[("key:big", "m:big")]
        bucket.clear()
        bucket.used = 0
        status3, qwait3 = mod.tpm_admit("key:big", mod.TPM_LIMIT + 1, "m:big")
        self.assertEqual(status3, "ok")
```

_（3）`test_tpm_admit_queue_full`_（~:1021）：

```python
    def test_tpm_admit_queue_full(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_QUEUE_MAX = 2
        try:
            mod.tpm_admit("key:qfull", mod.TPM_LIMIT, "m:qfull")  # 占满预算
            # 两个 waiter 已在队（绕过 wait 循环用私有结构直接入队，验证队满拒绝分支）
            with mod.TPM_LOCK:
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
            status3, qwait3 = mod.tpm_admit("key:qfull", 1, "m:qfull")
            self.assertEqual(status3, "full")
            self.assertEqual(qwait3, 0)
            self.assertEqual(mod._tpm_rejected.get(("key:qfull", "m:qfull")), 1)
            self.assertEqual(len(mod.TPM_WAITERS), 2)  # 第三个未入队
        finally:
            mod.TPM_QUEUE_MAX = 20
            self._tpm_cleanup(mod)
```

_（4）`test_tpm_settle_corrects_usage`_（~:1040）：

```python
    def test_tpm_settle_corrects_usage(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.tpm_admit("key:settle", 500, "m:settle")
        # 实际 200 → 退款 300
        mod.tpm_settle("key:settle", 500, 200, "m:settle")
        self.assertEqual(mod.TPM_BUCKETS[("key:settle", "m:settle")].used, 200)
        # 实际 800 → 追加 300
        mod.tpm_settle("key:settle", 500, 800, "m:settle")
        self.assertEqual(mod.TPM_BUCKETS[("key:settle", "m:settle")].used, 500)
        # 0 → 全额退款
        mod.tpm_settle("key:settle", 500, 0, "m:settle")
        self.assertEqual(mod.TPM_BUCKETS[("key:settle", "m:settle")].used, 0)
```

_（5）`test_tpm_settle_prune_invariant_no_ghost_tokens`_（~:1054）——不修改（`_TpmBucket`/`_tpm_prune` 内部不变）。

_（6）`test_tpm_settle_notifies_waiters`_（~:1077）：

```python
    def test_tpm_settle_notifies_waiters(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_QUEUE_TIMEOUT_S = 5.0
        results = {}
        # 占满预算
        mod.tpm_admit("key:wake", mod.TPM_LIMIT, "m:wake")
        def waiter():
            results["w"] = mod.tpm_admit("key:wake", 10, "m:wake")
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)  # 给 waiter 入队时间（确定性：settle 后立即 notify）
        self.assertEqual(len(mod.TPM_WAITERS), 1)
        mod.tpm_settle("key:wake", mod.TPM_LIMIT, 0, "m:wake")  # 全额退款唤醒
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertIn("w", results)
        self.assertEqual(results["w"][0], "ok")
        self.assertGreaterEqual(results["w"][1], 0)
        mod.TPM_QUEUE_TIMEOUT_S = 120
```

_（7）`test_tpm_admit_queue_timeout`_（~:1098）：

```python
    def test_tpm_admit_queue_timeout(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_QUEUE_TIMEOUT_S = 0.3
        try:
            mod.tpm_admit("key:to", mod.TPM_LIMIT, "m:to")  # 占满
            started = time.time()
            status, qwait = mod.tpm_admit("key:to", 10, "m:to")
            self.assertEqual(status, "timeout")
            self.assertLess(time.time() - started, 3.0)
            self.assertEqual(mod._tpm_timeouts.get(("key:to", "m:to")), 1)
            self.assertEqual(len(mod.TPM_WAITERS), 0)  # 已自队列移除
        finally:
            mod.TPM_QUEUE_TIMEOUT_S = 120
            self._tpm_cleanup(mod)
```

_（8）`test_tpm_waiters_identity_removal`_（~:1114）——仅改 `_TpmWaiter` 构造：

```python
    def test_tpm_waiters_identity_removal(self) -> None:
        """队列自移除必须按同一性（值相等 race 回归）。

        两个值全等的 _TpmWaiter 同在队列时（并发同 key 同 est 同 enqueued_at），
        摘 w1 不得连带摘走 w2——旧实现用值相等的 deque.remove 会误删后者。
        """
        mod = self.mod
        self._tpm_cleanup(mod)
        w1 = mod._TpmWaiter("k", 10, 123.5, "m:ident")
        w2 = mod._TpmWaiter("k", 10, 123.5, "m:ident")
        self.assertEqual(w1, w2)      # namedtuple 值相等
        self.assertIsNot(w1, w2)      # 但非同一对象
        mod.TPM_WAITERS.append(w1)
        mod.TPM_WAITERS.append(w2)
        # 值相等实现（deque.remove(waiter)）在摘 w2 时会误删队首 w1 —— 本用例必红。
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w2)
        self.assertEqual(len(mod.TPM_WAITERS), 1,
                         "identity removal must drop exactly one entry, got %d"
                         % len(mod.TPM_WAITERS))
        self.assertTrue(any(w is w1 for w in mod.TPM_WAITERS),
                        "head twin w1 must survive removal of w2")
        self.assertFalse(any(w is w2 for w in mod.TPM_WAITERS),
                         "removed waiter w2 must be gone from the queue")
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w1)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
        self._tpm_cleanup(mod)
```

_（9）`test_tpm_snapshot_shape_and_masking`_（~:1143）：

```python
    def test_tpm_snapshot_shape_and_masking(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.tpm_admit(mod.tpm_key_id("Bearer key:snap"), 100, "m:snap")
        snap = mod.tpm_snapshot()
        self.assertEqual(snap["config"]["limit"], mod.TPM_LIMIT)
        self.assertEqual(snap["config"]["window_s"], mod.TPM_WINDOW_S)
        self.assertEqual(snap["config"]["queue_max"], mod.TPM_QUEUE_MAX)
        self.assertEqual(snap["config"]["limit_by_model"], mod.TPM_LIMIT_BY_MODEL)
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        # key 脱敏：sha256: 前缀 + sha256(key) 前 12 位 hex
        expected_short = "sha256:" + hashlib.sha256(b"key:snap").hexdigest()[:12]
        self.assertEqual(b["key"], expected_short)
        self.assertNotIn("key:snap", b["key"])  # 无原始 key 泄漏
        self.assertEqual(b["model"], "m:snap")
        self.assertEqual(b["used"], 100)
        self.assertEqual(b["remaining"], mod.TPM_LIMIT - 100)
        self.assertEqual(b["queued"], 0)
        self.assertEqual(b["rejected"], 0)
        self.assertEqual(b["timeouts"], 0)
```

### B. 集成测试更新

_（10）`TpmAdmitHookTest.test_oversized_est_rejected_before_upstream`_（~:4014 类，~:4027 方法）——替换类 docstring + 方法全量重写：

类 docstring（:4014-:4015）：
```python
class TpmAdmitHookTest(unittest.TestCase):
    """TPM 准入 hook 冒烟：est 超预算的带 key 请求空闲放行、忙时 429（不触上游）。"""
```

测试方法替换（~:4027 method）：
```python
    def test_oversized_idle_release_then_busy_429(self) -> None:
        """est > TPM_LIMIT(50)：窗口空闲首发放行（200，触上游）；
        窗口占用后同 key 同 model 再发 → 429（used>0），上游 calls 不再增长。"""
        # len ~209 bytes → est = max(1, 209*0.25) = 52 > 50
        big = (b'{"model":"x","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')
        # ① 空闲放行
        status, data = post_sse_auth(self.proxy_port, big, "Bearer test-key")
        self.assertEqual(status, 200, "idle window must release oversized request")
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        self.assertEqual(len(self.calls), 1)
        # ② 同 key 同 model 再发：used=52>0 → 429，不触上游
        status2, data2 = post_sse_auth(self.proxy_port, big, "Bearer test-key")
        self.assertEqual(status2, 429, "busy window must reject oversized request")
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        self.assertEqual(len(self.calls), 1, "rejected request must not hit upstream")
        # ③ 无 Authorization 的同体请求直通（向后兼容）
        status3, _ = post_sse_auth(self.proxy_port, big, None)
        self.assertEqual(status3, 200, "no-auth request must bypass TPM")
        self.assertEqual(len(self.calls), 2)
```

_（11）`TpmRateLimitTest.test_tpm_stats_endpoint`_（~:4257 方法）——更新 config 精确断言：

```python
    def test_tpm_stats_endpoint(self) -> None:
        """/api/tpm_stats 返回 200 JSON：config 与 env seam 一致（含 limit_by_model），
        bucket used 与 REQ 行 tpm= 计数一致，key 字段 sha256: 前缀脱敏，model 字段存在。"""
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # 41B -> est = 10
        auth = "Bearer test-key-stats"
        status, _ = post_sse_auth(self.proxy_port, small_body, auth)
        self.assertEqual(status, 200)
        expected_used = max(1, int(len(small_body) * TPM_RATIO))
        status, body, ctype = admin_get(self.proc.admin_port, "/api/tpm_stats")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"),
                        "Content-Type must be application/json, got %r" % ctype)
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["config"],
                         {"limit": 110, "window_s": 2, "queue_max": 2,
                          "limit_by_model": {}},
                         "config must reflect env seams, got %r" % snap["config"])
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        expected_short = "sha256:" + hashlib.sha256(b"test-key-stats").hexdigest()[:12]
        self.assertEqual(b["key"], expected_short,
                         "bucket key must be sha256: prefix + 12 hex, got %r" % b["key"])
        self.assertNotIn("test-key-stats", b["key"], "bucket key must not leak raw key")
        self.assertEqual(b["model"], "m",
                         "bucket must carry model field, got %r" % b["model"])
        self.assertEqual(b["used"], expected_used,
                         "bucket used must match REQ line tpm= value")
        self.assertEqual(b["remaining"], 110 - expected_used)
        self.assertEqual(b["queued"], 0)
        self.assertEqual(b["rejected"], 0)
        self.assertEqual(b["timeouts"], 0)
        # 与 REQ 行交叉校验：tpm= 数值 == bucket used
        stderr = stderr_text(self.proc)
        self.assertIn("tpm=%d" % expected_used, stderr,
                      "REQ line must carry tpm=%d, stderr:\n%s" % (expected_used, stderr))
```

**验证命令**：
```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/tpm-residuals
python3 ctyun-stream-fix-proxy.test.py
```
预期全绿（card 1 的 174 例 + 本卡无新增数，被改方法语义更新后全绿）。

**Commit message**:
```
feat(tpm): per-model bucket key (key_id,model) + oversized idle release + rejected counting

Change TPM bucket key from key_id to (key_id,model) tuple: _TpmWaiter gains
model field, _tpm_get_bucket/tpm_admit/tpm_settle accept model param,
_tpm_rejected/_tpm_timeouts keys become tuples, tpm_snapshot adds model field
and limit_by_model config.

Oversized idle release (decision 3): est>budget releases if bucket.used<=0
and no same-bucket waiter; otherwise 429 with rejected counted (decision 4).
Budget per tpm_limit_for (per-model table).

Update all unit tests (8 methods) and 2 integration tests (TpmAdmitHookTest
oversized semantics, TpmRateLimitTest stats config shape).

_proxy_relay passes model from extract_model through all admit/settle calls.
```

---

## 卡 3/3（A 档）—— TpmPerModelTest 集成测试

**TDD 顺序**：先写 6 个 fail 集成测试 → 验证全绿（card 2 的生产代码已就位）。

**新类（`ctyun-stream-fix-proxy.test.py`）**——加入文件尾部，在 `StallTailGapTest`（:4479）之后，`if __name__ == "__main__"` 之前：

```python

class TpmPerModelTest(unittest.TestCase):
    """per-model 预算集成测试（卡 3/3，spec Acceptance ①-⑥ 交叉验证）。

    kimi 独立 30k 预算 vs 全局 110k / 超大空闲放行 / 超大忙时 429 /
    拒绝计数可见 / tpm_stats 形态演进。
    复用 make_scripted_upstream + start_proxy(extra_env) + post_sse_auth +
    admin_get + stderr_text + stop_fake_upstreams。
    """

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_USAGE + SSE_DONE)  # 带 usage 帧 → settle
        self.proxy_port = free_port()

    def tearDown(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            stderr_text(self.proc)
            shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_kimi_independent_budget(self) -> None:
        """env CTYUN_TPM_LIMIT=110000 CTYUN_TPM_LIMIT_BY_MODEL="kimi-k3-oc:1000"：
        同 key 发 kimi 请求 est>1000 → 429；同 key 发 deepseek 请求 est>1000
        但 <=110000 → 200。证明 (key,model) 桶隔离（决策 1、决策 2）。
        """
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110000",
                                    "CTYUN_TPM_LIMIT_BY_MODEL":
                                        "kimi-k3-oc:1000",
                                    "CTYUN_TPM_WINDOW_S": "5",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                })
        # kimi 消息体 ~127B → est = 31（含 max_tokens 无，仅 len×ratio）
        # deepseek 消息体 ~125B → est ≈ 31
        # kimi budget 1000：est 31 ≤ 1000，但要测隔离，需 est > kimi budget。
        # 构造大 body 使 est > 1000（TPM_TOKEN_RATIO=0.25 → body ≥ 4001B）
        big_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                    b'"messages":[{"role":"user","content":"'
                    + b"x" * 3940
                    + b'"}]}')   # ~4003B → est ≈ 1000（临界），官方 est 刚好 >1000 需要更多
        # 使用 max_tokens 辅助拉高 est
        big_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                    b'"messages":[{"role":"user","content":"'
                    + b"x" * 1000
                    + b'"}],"max_tokens":3000}')   # len~1040 → max(1,1040*0.25)+3000=3260 > 1000
        auth = "Bearer test-per-model"
        status1, _ = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status1, 429,
                         "kimi est > kimi budget must be rejected (per-model bucket)")
        # deepseek 同一 key：大请求仍 ≤ 全局 110000
        big_ds = (b'{"model":"deepseek-v4-pro-0813-oc","stream":true,'
                  b'"messages":[{"role":"user","content":"'
                  + b"x" * 1000
                  + b'"}],"max_tokens":3000}')   # est 3260 ≤ 110000
        status2, _ = post_sse_auth(self.proxy_port, big_ds, auth)
        self.assertEqual(status2, 200,
                         "deepseek est ≤ global budget must pass (key,model isolated)")
        # stderr：kimi REQ 行 result=tpm-queue-full（或 full if oversized busy）
        stderr = stderr_text(self.proc)
        self.assertIn("tpm-queue-full", stderr,
                      "kimi reject must log tpm-queue-full, stderr:\n" + stderr)

    def test_oversized_idle_release_first_then_reject(self) -> None:
        """CTYUN_TPM_LIMIT=50：首发 est>50（空闲窗口）→ 200（放行，决策 3）；
        窗口内再发同 key 请求 → 429（used>0）。
        """
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "50",
                                    "CTYUN_TPM_WINDOW_S": "60",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                })
        # len ~209B → est = int(209*0.25) = 52 > 50
        big = (b'{"model":"y","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')
        auth = "Bearer test-oversized"
        status1, data1 = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status1, 200, "idle oversized must be released")
        self.assertEqual(len(self.calls), 1, "released request must hit upstream")
        # 同 key 立即再发 → 429（used=52>0）
        status2, data2 = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status2, 429, "busy oversized must be rejected")
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        self.assertEqual(len(self.calls), 1,
                         "rejected request must not hit upstream again")

    def test_oversized_busy_429(self) -> None:
        """CTYUN_TPM_LIMIT=50：先发正常请求占预算（est <=50 → 200），
        再发 est>50 请求 → 429（used>0，decision 3 忙时硬拒）。
        """
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "50",
                                    "CTYUN_TPM_WINDOW_S": "60",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                })
        auth = "Bearer test-busy"
        # small: ~45B → est ≈ 11 ≤ 50
        small = b'{"model":"z","stream":true,"messages":[]}'
        status1, _ = post_sse_auth(self.proxy_port, small, auth)
        self.assertEqual(status1, 200, "normal request must be admitted")
        # big: ~209B → est ≈ 52 > 50
        big = (b'{"model":"z","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')
        status2, data2 = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status2, 429, "oversized must be rejected when budget used")
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")

    def test_rejected_counting_in_tpm_stats(self) -> None:
        """est>limit 硬拒后 GET /api/tpm_stats：bucket rejected>=1 且 model 字段正确。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "50",
                                    "CTYUN_TPM_WINDOW_S": "60",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                })
        auth = "Bearer test-reject-count"
        big = (b'{"model":"w","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')   # est 52 > 50
        # 首发空闲 → 放行（used<=0, 200）
        post_sse_auth(self.proxy_port, big, auth)
        # 再发忙时 → 429 硬拒，rejected 应计数
        status, data = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status, 429)
        parsed = json.loads(data.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        # 查 tpm_stats
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(len(snap["buckets"]), 1,
                         "桶空则防撞忙致占满仍留痕，got %d 桶" % len(snap["buckets"]))
        b = snap["buckets"][0]
        self.assertEqual(b["model"], "w",
                         "bucket must carry model field")
        self.assertGreaterEqual(b["rejected"], 1,
                                "hard-rejected must be counted, got rejected=%d"
                                % b["rejected"])

    def test_tpm_stats_shape_with_limit_by_model(self) -> None:
        """/api/tpm_stats config 含 limit_by_model dict（默认空 {}），bucket 含 model 字段。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110",
                                    "CTYUN_TPM_WINDOW_S": "2",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                })
        auth = "Bearer test-shape"
        small = b'{"model":"v","stream":true,"messages":[]}'   # est = 10
        post_sse_auth(self.proxy_port, small, auth)
        _, body, ctype = admin_get(self.proc.admin_port, "/api/tpm_stats")
        self.assertTrue(ctype and ctype.startswith("application/json"))
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["config"]["limit_by_model"], {},
                         "limit_by_model must default to empty dict")
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        self.assertEqual(b["model"], "v",
                         "bucket must carry model field (not None/null)")
        self.assertFalse(b["model"] is None,
                         "model field must be the string value, not null")
        # limit_by_model 含 env 设置时（决策 6 "原样"）
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110",
                                    "CTYUN_TPM_WINDOW_S": "2",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                    "CTYUN_TPM_LIMIT_BY_MODEL":
                                        "glm-5.3-oc:110000",
                                })
        post_sse_auth(self.proc.proxy_port, small, auth)
        _, body2, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap2 = json.loads(body2.decode("utf-8"))
        self.assertEqual(snap2["config"]["limit_by_model"],
                         {"glm-5.3-oc": 110000},
                         "limit_by_model must reflect env seam dict as-is")

    def test_no_auth_bypasses_tpm(self) -> None:
        """无 Authorization 头请求直通不限流，stderr 无任何 TPM 字段（向后兼容 spec）。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port)
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(data, SSE_A + SSE_USAGE + SSE_DONE)
        stderr = stderr_text(self.proc)
        self.assertNotIn("qwait=", stderr,
                         "no-auth requests must not log TPM fields")
        self.assertNotIn("tpm=", stderr)
        self.assertNotIn("tpm-queue", stderr)
```

**验证命令**：
```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/tpm-residuals
python3 ctyun-stream-fix-proxy.test.py
```
预期全绿（card 1 的 174 + card 2 不变 + card 3 新增 6 = ~180 例）。

**Commit message**:
```
test(tpm): per-model integration tests for budget isolation and oversized release

Add TpmPerModelTest with 6 integration tests covering per-model budget
isolation (kimi 30k vs deepseek 110k), oversized idle release, oversized
busy 429, rejected counting in /api/tpm_stats, tpm_stats shape evolution
(model field + limit_by_model config), and no-auth bypass regression.
```

---

## Self-Review 5 项

**1. spec coverage**：覆盖 Acceptance 全部 9 条
- `_parse_tpm_limit_by_model("kimi:30000,glm:110000")` 返回正确 → Card 1 test_parse_tpm_limit_by_model_normal
- 畸形项跳过 + log → Card 1 test_parse_tpm_limit_by_model_malformed（内联 print 等效 stderr 一行）
- 空串 {} → Card 1 test_parse_tpm_limit_by_model_empty
- kimi 独立预算隔离 → Card 3 test_kimi_independent_budget
- 超大空闲放行 → Card 3 test_oversized_idle_release_first_then_reject
- 超大忙时 429 → Card 3 test_oversized_busy_429
- 拒绝计数可见 → Card 3 test_rejected_counting_in_tpm_stats
- tpm_stats 形态（model + limit_by_model）→ Card 2 snapshot 更新 + Card 3 test_tpm_stats_shape_with_limit_by_model
- 无 auth 直通 → Card 3 test_no_auth_bypasses_tpm
- config 精确断言更新 → Card 2 test_tpm_stats_endpoint

**2. placeholder scan**：零 placeholder。每卡生产代码块 + 测试代码块均已完整展开（行级 `snake_case` 变量、`for` 循环体、`return` 值、`assert*` 断言）。

**3. type consistency**：
- `key_id`: `str`（`tpm_key_id` 返回 `str | None`）→ 桶键 `(str, str | None)`
- `model`: `str | None`（`extract_model` 返回类型）→ 桶键 `None` 有效
- `tpm_limit_for(model)` → `int`：`model=None/""` → `TPM_LIMIT`，`model="kimi-k3-oc"` → 30k
- `_tpm_rejected`/`_tpm_timeouts` 键：`(str, str | None)` 二元组，与桶键同型
- `tpm_snapshot` remain `TPM_LOCK` 保护不变
- `_TpmWaiter.model` → `str | None`（与生产路径一致）

**4. 可落盘性**：所有行号基于盘上实测（grep `TPM_BUCKETS|_tpm_rejected|_tpm_get_bucket|tpm_admit|tpm_settle|tpm_snapshot|_proxy_relay`）验证，每段代码块的 old/new 边界精确到行。编辑位点：插入 3 处（card 1）、编辑 7 处生产 + 11 处测试（card 2）、新增 1 类 + 6 方法（card 3），均可执行。

**5. 锚点实测验证**：已通过以下 grep+sed 实测验证锚点存在且与代码块匹配：
```bash
grep -nE 'TPM_LIMIT|TPM_KEY_CAP|_TpmWaiter|TPM_BUCKETS|_tpm_rejected|_tpm_timeouts|def _tpm_get_bucket|def tpm_admit|def tpm_settle|def tpm_snapshot|def extract_model|def _proxy_relay' ctyun-stream-fix-proxy.py
```
结果行号与上表 P2 漂移对齐一致，每个目标函数签名、命名、缩进已核。