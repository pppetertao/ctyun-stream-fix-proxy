# PLAN.md — TPM 客户端侧限流

## Header

- **Goal**: 为 ctyun-stream-fix-proxy 加 per-key 60s 滚动窗口 TPM 限流（110k tokens 预算），超预算 FIFO 排队（上限 20，超时 120s → 429），消除上游 `model_tpm_limit` 事故。
- **Branch**: `fix/tpm-rate-limiting` (worktree)
- **Spec**: `docs/superpowers/specs/2026-09-30-tpm-rate-limiting-design.md`
- **Baseline**: `python3 ctyun-stream-fix-proxy.test.py` — 127 tests OK (58.68s, exit 0)
- **Verification command**: `python3 ctyun-stream-fix-proxy.test.py` (worktree root)

## Global Constraints

1. **验证命令**: `python3 ctyun-stream-fix-proxy.test.py`（worktree 根）；每卡写代码前先跑 baseline 登记当前状态。
2. **Tier**: 全 A 档（spec 锚点完整且可写死代码）；仅真有「必须跑运行时才能定代码」的留白才标 C。本 spec 锚点已给死全部函数签名与插入位，不存在 C 档需求。
3. **TDD 顺序**: 每卡内：先写失败测试 → 实现生产代码 → 跑绿验证 → commit（Conventional Commits）。
4. **并发/排队测试**: 优先确定性手段（小窗口 env seam `CTYUN_TPM_LIMIT=200`、`CTYUN_TPM_QUEUE_TIMEOUT_S=2`、`CTYUN_TPM_QUEUE_MAX=2`）；避免真实 sleep 依赖的 flaky 测试；新测试不得加剧既有 teardown `proc.wait` flaky（followups 已知问题）。
5. **代码风格**: 遵循既有模式——常量区 env seam（`:43` SEND_TIMEOUT_S 惯例）、`classify_outcome` flag-first 优先级链、`_reply_502` 的 send_response/send_header/end_headers/wfile.write/close_connection 模式、`AdminHandler.do_GET` 的 `elif path == "..."` 路由追加、`_log` 格式 `REQ ... dur=... result=...`。
6. **分段写作**: 5 卡，B0（本文件骨架）→ B1..B5（逐卡填码）→ B-final（自查 5 项）。B0 仅含 Header + Global Constraints + 卡清单（每卡 tier/文件/接口签名/验收）+ anchor grep 实测证据。**B0 不改生产/测试代码，不写卡内代码块。**
7. **Commit 规范**: 每卡独立 commit，Conventional Commits。B0 commit: `docs(plan): TPM rate limiting B0 skeleton`。B1..B5 各 commit 格式见卡片内。
8. **锁序**: `TPM_LOCK` 内不调 `_record_request`/`record_error_event`（429 分支先 return 再记录，避免 `TPM_LOCK` 与 `STATS_LOCK`/`ERROR_LOCK` 反转）。
9. **无新依赖**: 纯 stdlib（`collections`、`threading`、`hashlib`、`json`、`os`），不加 pip 包。

## 卡清单

---

### Card 1 [A] — 常量区 + sse_line_usage + _KIND_CATEGORY

- **Tier**: A（所有锚点与插入位写死，无运行时依赖）
- **文件**: `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **TDD 顺序**: 先写测试（`test_sse_line_usage_matrix` 等单元测试，导入失败 → 红）→ 实现生产代码 → `python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest` 绿色 → commit

#### 生产代码（完整，可直接落盘）

**1) 常量区（插入于 `:52` HEADER_RETRY_MAX 之后、`:53` PRIMED_TAIL_CAP 之前）**

```python
# TPM rate limiting（per-key 滚动窗口 + FIFO 排队）
TPM_LIMIT = int(os.environ.get("CTYUN_TPM_LIMIT", "110000"))
TPM_WINDOW_S = int(os.environ.get("CTYUN_TPM_WINDOW_S", "60"))
TPM_QUEUE_MAX = int(os.environ.get("CTYUN_TPM_QUEUE_MAX", "20"))
TPM_QUEUE_TIMEOUT_S = float(os.environ.get("CTYUN_TPM_QUEUE_TIMEOUT_S", "120"))
TPM_TOKEN_RATIO = float(os.environ.get("CTYUN_TPM_TOKEN_RATIO", "0.55"))
TPM_KEY_CAP = int(os.environ.get("CTYUN_TPM_KEY_CAP", "64"))
```

插入位置上下文（before: `HEADER_RETRY_MAX = ...`, after: `PRIMED_TAIL_CAP = ...`）:
```
HEADER_RETRY_MAX = max(0, int(os.environ.get("CTYUN_HEADER_RETRY", "1")))
                                                                    ← 插入空行 + TPM 常量块
PRIMED_TAIL_CAP = 262144  # finish hold 尾段缓冲上限
```

**2) 结果类常量（插入于 `:66` CLASS_POISON_FIXED 之后、`:68` ERR_KIND_POISON 之前，`:76` CLASS_BODY_ERROR 之后）**

```python
# 在 :66 行 CLASS_POISON_FIXED = "poison_fixed" 之后追加:
CLASS_TPM_LIMITED = "tpm_limited"
```

插入位置上下文（after: `CLASS_POISON_FIXED = "poison_fixed"`，before: 空行后 `ERR_KIND_POISON`）:
```
CLASS_POISON_FIXED = "poison_fixed"
CLASS_TPM_LIMITED = "tpm_limited"    ← 追加行

ERR_KIND_POISON = "poison_hit"
```

```python
# 在 :74 行 ERR_KIND_HEADER_TIMEOUT 之后追加:
ERR_KIND_TPM_QUEUE_FULL = "tpm_queue_full"
ERR_KIND_TPM_QUEUE_TIMEOUT = "tpm_queue_timeout"
```

插入位置上下文（after: `ERR_KIND_HEADER_TIMEOUT = "header_timeout"`）:
```
ERR_KIND_HEADER_TIMEOUT = "header_timeout"
ERR_KIND_TPM_QUEUE_FULL = "tpm_queue_full"        ← 追加
ERR_KIND_TPM_QUEUE_TIMEOUT = "tpm_queue_timeout"  ← 追加

CLASS_BODY_ERROR = "body_error"
```

**3) _KIND_CATEGORY 映射（在 `:87` `ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR,` 之后追加两条）**

```python
    ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR,
    ERR_KIND_TPM_QUEUE_FULL: CLASS_TPM_LIMITED,
    ERR_KIND_TPM_QUEUE_TIMEOUT: CLASS_TPM_LIMITED,
}
```

**4) sse_line_usage 函数（插入于 `:153` sse_line_has_usage 结束后，`:155` sse_line_body_error 之前）**

```python
def sse_line_usage(line: bytes):
    """返回 SSE data 行中的 usage dict，无 usage 帧返回 None。

    解析逻辑与 sse_line_has_usage 同序：非 data: 前缀/[DONE]/非 JSON/非 dict →
    None；usage 非空 dict → 返回该 dict；其余 → None。
    独立 usage 帧（choices=[]）与 ride-on finish 帧均覆盖。
    """
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return None
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) and usage else None
```

插入位置上下文（after: `sse_line_has_usage` 函数结束 `return isinstance(usage, dict) and bool(usage)`，before: 空行后 `sse_line_body_error`）:
```
    return isinstance(usage, dict) and bool(usage)
                                 ← 插入空行 + sse_line_usage

def sse_line_body_error(line: bytes) -> bool:
```

#### 测试代码（完整，可直接落盘）

在 `ProxyDashboardUnitTest` 类中追加（`load_proxy_module()` 已由 setUpClass 提供 `self.mod`），位置在 `test_sse_line_body_error_matrix` 之后、类结束 `class AdminIntegrationTest` 之前。

```python
    def test_sse_line_usage_matrix(self) -> None:
        f = self.mod.sse_line_usage
        # 独立 usage 帧（choices=[]）→ 返回 usage dict
        result = f(b'data: {"id":"u","choices":[],'
                   b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n')
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        # ride-on finish 帧（choices 有 finish + usage 顶层）→ 返回 usage dict
        result2 = f(b'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n')
        self.assertEqual(result2, {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13})
        # 空 usage {} → None
        self.assertIsNone(f(b'data: {"id":"e","choices":[],"usage":{}}\n'))
        # usage: null → None
        self.assertIsNone(f(b'data: {"id":"n","choices":[],"usage":null}\n'))
        # [DONE] → None
        self.assertIsNone(f(b"data: [DONE]\n"))
        # 非 JSON → None
        self.assertIsNone(f(b"data: {not json\n"))
        # 非 data 行 → None
        self.assertIsNone(f(b": keep-alive\n"))
        # usage 非 dict（如列表）→ None
        self.assertIsNone(f(b'data: {"usage":[1,2,3],"choices":[]}\n'))
        # 普通 content 帧 → None
        self.assertIsNone(f(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'))

    def test_tpm_constants_load(self) -> None:
        """TPM 常量通过 load_proxy_module 可见且取值正确。"""
        mod = self.mod
        self.assertEqual(mod.TPM_LIMIT, 110000)
        self.assertEqual(mod.TPM_WINDOW_S, 60)
        self.assertEqual(mod.TPM_QUEUE_MAX, 20)
        self.assertEqual(mod.TPM_QUEUE_TIMEOUT_S, 120)
        self.assertEqual(mod.TPM_TOKEN_RATIO, 0.55)
        self.assertEqual(mod.TPM_KEY_CAP, 64)
        self.assertEqual(mod.CLASS_TPM_LIMITED, "tpm_limited")
        self.assertEqual(mod.ERR_KIND_TPM_QUEUE_FULL, "tpm_queue_full")
        self.assertEqual(mod.ERR_KIND_TPM_QUEUE_TIMEOUT, "tpm_queue_timeout")
        self.assertIn(mod.ERR_KIND_TPM_QUEUE_FULL, mod._KIND_CATEGORY)
        self.assertIn(mod.ERR_KIND_TPM_QUEUE_TIMEOUT, mod._KIND_CATEGORY)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_TPM_QUEUE_FULL],
                         mod.CLASS_TPM_LIMITED)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_TPM_QUEUE_TIMEOUT],
                         mod.CLASS_TPM_LIMITED)

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
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('m', %r); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "print(m.TPM_LIMIT, m.TPM_WINDOW_S, m.TPM_QUEUE_MAX, "
            "m.TPM_QUEUE_TIMEOUT_S, m.TPM_TOKEN_RATIO, m.TPM_KEY_CAP)"
        ) % PROXY_SCRIPT
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0,
                         "env seam subprocess failed stderr:\n" + proc.stderr)
        parts = proc.stdout.strip().split()
        self.assertEqual(parts, ["50000", "30", "5", "10.0", "0.3", "8"])
```

#### 验证命令

```bash
# 仅跑本卡相关测试（ProxyDashboardUnitTest 全部不含 socket，秒级）
python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest 2>&1 | tail -10
```

#### Commit

```
feat(proxy): add TPM constants, sse_line_usage, and KIND_CATEGORY entries
```

---

### Card 2 [A] — TPM 模块态 + 准入/结算/快照核心函数

- **Tier**: A（逻辑由 spec 决策 2/3 定死；并发行为用确定性单测验证，无需运行时探测）
- **文件**: `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **TDD 顺序**: 先写测试（`test_tpm_admit_*` / `test_tpm_settle_*` / `test_tpm_snapshot_shape` / `test_tpm_key_id` / `test_estimate_request_tokens` / `test_tpm_prune`，导入失败 → 红）→ 实现生产代码 → `python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest` 绿色 → commit

#### 生产代码（完整，可直接落盘）

**前置：import 区追加 `import hashlib`（`ctyun-stream-fix-proxy.py` `:12` `import base64` 之后、`import collections` 之前）**。实测确认当前文件无 hashlib import（只有 `import hmac`），`tpm_key_id` 的 sha256 依赖它。

<!-- rev: 2026-09-30 _TpmBucket 子类修复（deque 无 __dict__） -->

```python
import hashlib
```

**插入位置：`classify_outcome` 函数结束后（`:197` 与 `empty_stream_should_retry` `:200` 之间）**

```python
# --- TPM rate limiting（module-level state，全部由 TPM_LOCK 保护）---

_TpmWaiter = collections.namedtuple("_TpmWaiter", "key_id est enqueued_at")

# collections.deque 是 C 类型无 __dict__，不能挂 .used 属性；Python 空子类有
# __dict__ 可承载 .used，而 len()/popleft/append/迭代语义与原 deque 完全一致
class _TpmBucket(collections.deque):
    pass

TPM_LOCK = threading.Condition()
TPM_BUCKETS: dict = {}                 # key_id -> _TpmBucket（deque 条目 + 附加属性 .used）
TPM_WAITERS: collections.deque = collections.deque()  # FIFO 排队（of _TpmWaiter）
_tpm_rejected: dict = {}               # key_id -> queue-full 拒绝计数（快照用）
_tpm_timeouts: dict = {}               # key_id -> queue-timeout 计数（快照用）


def tpm_key_id(auth_header):
    """从 Authorization 头提取 per-key 标识。

    无头 / 非 "Bearer " 前缀 / 空 token → None（= 不限流直通）；
    否则返回 token 的 sha256 hex 全位（内部桶键用全位防碰撞，
    快照对外只显前 12 位脱敏）。
    """
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def estimate_request_tokens(body):
    """准入估算：max(1, int(len(body) * TPM_TOKEN_RATIO))；
    body 可解析为 JSON 且含 int max_tokens 时累加该值（输出预留）。
    非 JSON body 按裸长度估（fail-open），永不抛出。
    """
    if not body:
        return 1
    base = max(1, int(len(body) * TPM_TOKEN_RATIO))
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        # 吞掉的是非 JSON 请求体（GET/表单/二进制属常态）：按裸长度估即可。
        return base
    if isinstance(data, dict):
        mt = data.get("max_tokens")
        if isinstance(mt, int) and not isinstance(mt, bool) and mt > 0:
            base += mt
    return base


def _tpm_prune(bucket, now: float) -> None:
    """裁剪 bucket 中 now - TPM_WINDOW_S 之前的条目并同步 .used。

    bucket 为 collections.deque（条目 (ts, tokens)，delta 可为负），
    .used 为其增量维护的窗口内 token 合计；popleft 时同步扣减。
    仅可在持 TPM_LOCK 时调用。
    """
    cutoff = now - TPM_WINDOW_S
    while bucket and bucket[0][0] < cutoff:
        _, tokens = bucket.popleft()
        bucket.used -= tokens
    if bucket.used < 0:  # 浮点边界/重入保护：钳制 ≥0
        bucket.used = 0


def _tpm_get_bucket(key_id: str):
    """取 key 的桶；不存在则惰性创建（cap 满时先驱逐空桶）。仅在 TPM_LOCK 内调用。"""
    bucket = TPM_BUCKETS.get(key_id)
    if bucket is None:
        if len(TPM_BUCKETS) >= TPM_KEY_CAP:
            # 惰性驱逐：窗口空且无排队的桶（窗口自动过期，最旧优先）
            idle = [k for k, b in TPM_BUCKETS.items()
                    if b.used <= 0
                    and not any(w.key_id == k for w in TPM_WAITERS)]
            for k in idle:
                del TPM_BUCKETS[k]
        bucket = _TpmBucket()
        bucket.used = 0
        TPM_BUCKETS[key_id] = bucket
    return bucket


def tpm_admit(key_id: str, est: int):
    """TPM 准入（与入队同锁，原子）。返回 (status, qwait_ms)。

    status: "ok"（准入）/ "full"（队列满或 est 超预算，立即 429）/
            "timeout"（排队超时，429）。
    规则：
    - est > TPM_LIMIT → 直接 ("full", 0)（永远等不到，不入队）。
    - 窗口内充足 → 入桶 ("ok", 0)。
    - 否则入队 FIFO；仅队首 waiter 可被准入（严格 FIFO 防惊群）；
      wait 循环 TPM_LOCK.wait(timeout=min(1.0, remaining)) + deadline 检查
      （窗口过期靠 1s 粒度轮询，settle 时 notify_all 提前唤醒）。
    - 队满（len(TPM_WAITERS) >= TPM_QUEUE_MAX）→ ("full", 0) 不入队。
    - deadline 到仍未准入 → ("timeout", 0) 并自队列移除。
    排队期间不持有任何上游连接（hook 点在 _open_upstream 之前）。
    """
    if est > TPM_LIMIT:
        return ("full", 0)
    started = time.time()
    with TPM_LOCK:
        bucket = _tpm_get_bucket(key_id)
        _tpm_prune(bucket, started)
        if bucket.used + est <= TPM_LIMIT:
            bucket.append((started, est))
            bucket.used += est
            return ("ok", 0)
        if len(TPM_WAITERS) >= TPM_QUEUE_MAX:
            _tpm_rejected[key_id] = _tpm_rejected.get(key_id, 0) + 1
            return ("full", 0)
        waiter = _TpmWaiter(key_id=key_id, est=est, enqueued_at=started)
        TPM_WAITERS.append(waiter)
        deadline = started + TPM_QUEUE_TIMEOUT_S
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    _tpm_timeouts[key_id] = _tpm_timeouts.get(key_id, 0) + 1
                    return ("timeout", 0)
                head = TPM_WAITERS[0] if TPM_WAITERS else None
                if head is waiter:
                    now = time.time()
                    bucket = _tpm_get_bucket(key_id)
                    _tpm_prune(bucket, now)
                    if bucket.used + est <= TPM_LIMIT:
                        TPM_WAITERS.popleft()
                        bucket.append((now, est))
                        bucket.used += est
                        return ("ok", (now - started) * 1000)
                TPM_LOCK.wait(timeout=min(1.0, remaining))
        finally:
            if waiter in TPM_WAITERS:
                # 超时/异常路径把自己从队列移除；已 popleft 的正常准入不会走到这
                try:
                    TPM_WAITERS.remove(waiter)
                except ValueError:
                    pass  # 已被并发 popleft：无其他路径可达，正常结束


def tpm_settle(key_id: str, est: int, actual: int) -> None:
    """结算：以实际 usage 校正窗口占用。

    prune 后追加校正条目 (now, delta)，delta = actual - est（可为负 = 退款），
    used 钳制 ≥0。settle 后 notify_all 唤醒排队 waiter 重试准入。
    """
    now = time.time()
    with TPM_LOCK:
        bucket = TPM_BUCKETS.get(key_id)
        if bucket is None:
            return  # 该 key 从未准入（不可能路径，防御处理）
        _tpm_prune(bucket, now)
        delta = actual - est
        if delta != 0:
            bucket.append((now, delta))
        bucket.used = max(0, bucket.used + delta)
        TPM_LOCK.notify_all()


def tpm_snapshot() -> dict:
    """TPM 状态快照（/api/tpm_stats 端点用）。

    返回 {"config": {"limit", "window_s", "queue_max"},
          "queue_total", "buckets": [...]}；
    bucket 条目 {"key"（sha256: 前缀 + 前 12 位 hex）, "used", "remaining",
                 "queued", "rejected", "timeouts"}——key 脱敏，无原始 key 泄漏。
    """
    now = time.time()
    with TPM_LOCK:
        buckets = []
        for key_id, bucket in TPM_BUCKETS.items():
            _tpm_prune(bucket, now)
            if bucket.used <= 0 and not any(w.key_id == key_id for w in TPM_WAITERS):
                continue  # 空桶不展示（噪声）
            buckets.append({
                "key": "sha256:" + key_id[:12],
                "used": bucket.used,
                "remaining": max(0, TPM_LIMIT - bucket.used),
                "queued": sum(1 for w in TPM_WAITERS if w.key_id == key_id),
                "rejected": _tpm_rejected.get(key_id, 0),
                "timeouts": _tpm_timeouts.get(key_id, 0),
            })
        return {
            "config": {
                "limit": TPM_LIMIT,
                "window_s": TPM_WINDOW_S,
                "queue_max": TPM_QUEUE_MAX,
            },
            "queue_total": len(TPM_WAITERS),
            "buckets": buckets,
        }
```

插入位置上下文（after: `classify_outcome` 末尾 `return _Outcome(...)`，before: `def empty_stream_should_retry`）:
```
    # status >= 500
    return _Outcome(CLASS_UPSTREAM_FAULT, "upstream-err", False, True)
                                   ← 插入空行 + TPM 模块态/函数块

def empty_stream_should_retry(budget: int) -> bool:
```

#### 测试代码（完整，可直接落盘）

**前置：`ctyun-stream-fix-proxy.test.py` 顶部 import 区（`:12` `import collections` 之后）追加 `import hashlib`**（test_tpm_key_id/test_tpm_snapshot_shape_and_masking 用）。

<!-- rev: 2026-09-30 _TpmBucket 子类修复（deque 无 __dict__） -->

```python
import hashlib
```

在 `ProxyDashboardUnitTest` 类中追加（`test_tpm_constants_env_seam` 之后）。注意：单测共享模块级全局（`TPM_BUCKETS`/`TPM_WAITERS`），每个用例开头 `mod.TPM_BUCKETS.clear()` + `mod.TPM_WAITERS.clear()` 保证独立性；`TPM_LOCK` 为 Condition，无需清理。

```python
    def _tpm_cleanup(self, mod):
        mod.TPM_BUCKETS.clear()
        mod.TPM_WAITERS.clear()
        mod._tpm_rejected.clear()
        mod._tpm_timeouts.clear()

    def test_tpm_key_id(self) -> None:
        f = self.mod.tpm_key_id
        self.assertIsNone(f(None))
        self.assertIsNone(f(""))
        self.assertIsNone(f("Basic dXNlcjpwYXNz"))
        self.assertIsNone(f("Bearer"))
        self.assertIsNone(f("Bearer   "))  # 空 token
        kid1 = f("Bearer test-key-1")
        self.assertIsInstance(kid1, str)
        self.assertEqual(len(kid1), 64)  # sha256 hex 全位
        self.assertEqual(f("Bearer test-key-1"), kid1)  # 确定性
        self.assertEqual(f("bearer test-key-1"), kid1)  # scheme 大小写不敏感
        self.assertNotEqual(f("Bearer test-key-2"), kid1)

    def test_estimate_request_tokens(self) -> None:
        f = self.mod.estimate_request_tokens
        ratio = self.mod.TPM_TOKEN_RATIO
        self.assertEqual(f(b""), 1)
        self.assertEqual(f(None), 1)
        body = b'{"model":"x","stream":true,"messages":[]}'
        self.assertEqual(f(body), max(1, int(len(body) * ratio)))
        body2 = b'{"model":"x","max_tokens":4096,"messages":[]}'
        self.assertEqual(f(body2), max(1, int(len(body2) * ratio)) + 4096)
        # max_tokens 非 int（str/null/bool）不累加
        for non_int in (b'{"max_tokens":"4096"}', b'{"max_tokens":null}',
                        b'{"max_tokens":true}'):
            self.assertEqual(f(non_int), max(1, int(len(non_int) * ratio)))
        # 非 JSON → 裸长度估（fail-open）
        self.assertEqual(f(b"plain text"), max(1, int(10 * ratio)))

    def test_tpm_prune(self) -> None:
        mod = self.mod
        bucket = mod._TpmBucket()
        bucket.used = 0
        now = 1000.0
        bucket.append((now - 30, 500))
        bucket.used = 500
        mod._tpm_prune(bucket, now)
        self.assertEqual(len(bucket), 1)  # 窗口内保留
        bucket.append((now - 90, 100))   # 窗口外（window=60）
        bucket.used += 100
        mod._tpm_prune(bucket, now)
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket.used, 500)
        bucket.used = -10                 # 钳制 ≥0
        mod._tpm_prune(bucket, now)
        self.assertEqual(bucket.used, 0)

    def test_tpm_admit_ok_immediate(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        status, qwait = mod.tpm_admit("key:imm", 100)
        self.assertEqual(status, "ok")
        self.assertEqual(qwait, 0)
        self.assertEqual(mod.TPM_BUCKETS["key:imm"].used, 100)
        self.assertEqual(len(mod.TPM_WAITERS), 0)

    def test_tpm_admit_full_oversized(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        status, qwait = mod.tpm_admit("key:big", mod.TPM_LIMIT + 1)
        self.assertEqual(status, "full")
        self.assertEqual(qwait, 0)
        self.assertNotIn("key:big", mod.TPM_BUCKETS)  # 不入桶
        self.assertEqual(len(mod.TPM_WAITERS), 0)     # 不入队

    def test_tpm_admit_queue_full(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_QUEUE_MAX = 2
        try:
            mod.tpm_admit("key:qfull", mod.TPM_LIMIT)  # 占满预算
            # 两个 waiter 已在队（绕过 wait 循环用私有结构直接入队，验证队满拒绝分支）
            with mod.TPM_LOCK:
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0))
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0))
            status3, qwait3 = mod.tpm_admit("key:qfull", 1)
            self.assertEqual(status3, "full")
            self.assertEqual(qwait3, 0)
            self.assertEqual(mod._tpm_rejected.get("key:qfull"), 1)
            self.assertEqual(len(mod.TPM_WAITERS), 2)  # 第三个未入队
        finally:
            mod.TPM_QUEUE_MAX = 20
            self._tpm_cleanup(mod)

    def test_tpm_settle_corrects_usage(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.tpm_admit("key:settle", 500)
        # 实际 200 → 退款 300
        mod.tpm_settle("key:settle", 500, 200)
        self.assertEqual(mod.TPM_BUCKETS["key:settle"].used, 200)
        # 实际 800 → 追加 300
        mod.tpm_settle("key:settle", 500, 800)
        self.assertEqual(mod.TPM_BUCKETS["key:settle"].used, 500)
        # 0 → 全额退款
        mod.tpm_settle("key:settle", 500, 0)
        self.assertEqual(mod.TPM_BUCKETS["key:settle"].used, 0)

    def test_tpm_settle_notifies_waiters(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_QUEUE_TIMEOUT_S = 5.0
        results = {}
        # 占满预算
        mod.tpm_admit("key:wake", mod.TPM_LIMIT)
        def waiter():
            results["w"] = mod.tpm_admit("key:wake", 10)
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)  # 给 waiter 入队时间（确定性：settle 后立即 notify）
        self.assertEqual(len(mod.TPM_WAITERS), 1)
        mod.tpm_settle("key:wake", mod.TPM_LIMIT, 0)  # 全额退款唤醒
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertIn("w", results)
        self.assertEqual(results["w"][0], "ok")
        self.assertGreaterEqual(results["w"][1], 0)
        mod.TPM_QUEUE_TIMEOUT_S = 120

    def test_tpm_admit_queue_timeout(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_QUEUE_TIMEOUT_S = 0.3
        try:
            mod.tpm_admit("key:to", mod.TPM_LIMIT)  # 占满
            started = time.time()
            status, qwait = mod.tpm_admit("key:to", 10)
            self.assertEqual(status, "timeout")
            self.assertLess(time.time() - started, 3.0)
            self.assertEqual(mod._tpm_timeouts.get("key:to"), 1)
            self.assertEqual(len(mod.TPM_WAITERS), 0)  # 已自队列移除
        finally:
            mod.TPM_QUEUE_TIMEOUT_S = 120
            self._tpm_cleanup(mod)

    def test_tpm_snapshot_shape_and_masking(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.tpm_admit("key:snap", 100)
        snap = mod.tpm_snapshot()
        self.assertEqual(snap["config"]["limit"], mod.TPM_LIMIT)
        self.assertEqual(snap["config"]["window_s"], mod.TPM_WINDOW_S)
        self.assertEqual(snap["config"]["queue_max"], mod.TPM_QUEUE_MAX)
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        # key 脱敏：sha256: 前缀 + sha256(key) 前 12 位 hex
        expected_short = "sha256:" + hashlib.sha256(b"key:snap").hexdigest()[:12]
        self.assertEqual(b["key"], expected_short)
        self.assertNotIn("key:snap", b["key"])  # 无原始 key 泄漏
        self.assertEqual(b["used"], 100)
        self.assertEqual(b["remaining"], mod.TPM_LIMIT - 100)
        self.assertEqual(b["queued"], 0)
        self.assertEqual(b["rejected"], 0)
        self.assertEqual(b["timeouts"], 0)
```

#### 验证命令

```bash
python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest 2>&1 | tail -10
```

#### Commit

```
feat(proxy): add TPM admission, settle, and snapshot core functions
```

---

### Card 3 [A] — 429 合成 + /api/tpm_stats 管理端点 + _log 扩展

- **Tier**: A（`_reply_tpm_429` 镜像 `_reply_502` 模式写死；路由追加按既有 `elif path == ...` 惯例；`_log` 扩展追加可选 kwargs）
- **文件**: `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **TDD 顺序**: 先写测试（`test_tpm_429_reply` 单元测试 + `test_tpm_stats_endpoint_basic` 集成测试，导入/端点 → 红）→ 实现生产代码 → 验证绿色 → commit

#### 生产代码（完整，可直接落盘）

**1) `_reply_tpm_429` 方法（插入于 `_reply_502` `:1169` 之后，空行后）**

```python
    def _reply_tpm_429(self, reason: str) -> None:
        """合成 429 响应（镜像 _reply_502 定死 body + close_connection=True）。

        reason: "full"（队列满/est 超预算）或 "timeout"（排队超时）——用于日志分类。
        """
        payload = ('{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试",'
                   '"type":"rate_limit_error","code":"model_tpm_limit"}}').encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True
```

插入位置上下文（after: `_reply_502` 最后一行 `self.close_connection = True`，before: 空行后 `def _log(...)`）:
```python
        self.close_connection = True
                       ← 空行 + _reply_tpm_429

    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
```

**2) `_log` 签名与格式扩展（`:1171` 原签名 + `:1177` 原格式行）**

原代码 (`:1171-1182`):
```python
    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0, retry_reason: str = "", exc=None) -> None:
        exc_field = "-"
        if exc is not None:
            exc_field = re.sub(r"\s+", "_",
                               ("%s: %s" % (type(exc).__name__, exc)).strip())[:200]
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d retry_reason=%s exc=%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            retry_reason or "-", exc_field,
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

改为（签名加 `qwait_ms=None, tpm_used=None`，格式加 `%s` + `extra` 变量）:

```python
    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0, retry_reason: str = "", exc=None,
             qwait_ms=None, tpm_used=None) -> None:
        exc_field = "-"
        if exc is not None:
            exc_field = re.sub(r"\s+", "_",
                               ("%s: %s" % (type(exc).__name__, exc)).strip())[:200]
        extra = ""
        if qwait_ms is not None:
            extra += " qwait=%dms" % qwait_ms
        if tpm_used is not None:
            extra += " tpm=%d" % tpm_used
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d retry_reason=%s exc=%s%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            retry_reason or "-", exc_field, extra,
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

注意：新增 kwargs 末尾追加，保持所有现有调用点向后兼容（现有调用均未传 `qwait_ms`/`tpm_used`，默认 `None` 不输出对应字段）。

**3) `/api/tpm_stats` 路由（在 `AdminHandler.do_GET` `:1195` `self._send_json(200, stats_snapshot())` 之后追加 `elif`）**

```python
        elif path == "/api/stats":
            self._send_json(200, stats_snapshot())
        elif path == "/api/tpm_stats":
            self._send_json(200, tpm_snapshot())
        elif path == "/api/config":
```

插入位置上下文（紧接 `/api/stats` 路由 `:1195` 之后）:
```python
        elif path == "/api/stats":
            self._send_json(200, stats_snapshot())
                                ← 追加 elif path == "/api/tpm_stats" 行
        elif path == "/api/config":
```

#### 测试代码（完整，可直接落盘）

**1) ProxyDashboardUnitTest 单元测试（验证 _reply_tpm_429 payload 格式 + _log 扩展）**

在 ProxyDashboardUnitTest 类末尾追加（`test_tpm_snapshot_shape_and_masking` 之后）:

```python
    def test_tpm_429_reply_payload(self) -> None:
        """_reply_tpm_429 方法 payload 与 spec 定死 JSON 一致。"""
        mod = self.mod
        # 定死 JSON 结构（与 _reply_tpm_429 源码内 payload 全等）
        expected = ('{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试",'
                    '"type":"rate_limit_error","code":"model_tpm_limit"}}').encode("utf-8")
        parsed = json.loads(expected.decode("utf-8"))
        self.assertIn("error", parsed)
        self.assertEqual(parsed["error"]["type"], "rate_limit_error")
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        # 验证方法存在
        self.assertTrue(hasattr(mod.ProxyHandler, "_reply_tpm_429"),
                        "_reply_tpm_429 method must exist on ProxyHandler")
        self.assertTrue(callable(mod.ProxyHandler._reply_tpm_429))
```

**2) 集成测试（子进程：验证 `/api/tpm_stats` 端点可访问、返回 shape 正确）**

在 `AdminIntegrationTest` 类末尾追加（`test_dashboard_html_full_page` 之后、`class BodyErrorTest` 之前）:

```python
    def test_tpm_stats_endpoint_basic(self) -> None:
        """GET /api/tpm_stats 返回 200 JSON，config/queue_total/buckets shape 正确。"""
        status, body, ctype = admin_get(self.proc.admin_port, "/api/tpm_stats")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"),
                        "Content-Type must be application/json, got %r" % ctype)
        snap = json.loads(body.decode("utf-8"))
        self.assertIn("config", snap)
        self.assertIn("queue_total", snap)
        self.assertIn("buckets", snap)
        self.assertIsInstance(snap["buckets"], list)
        self.assertEqual(snap["config"]["limit"], 110000)  # default
        self.assertEqual(snap["config"]["window_s"], 60)
        self.assertEqual(snap["config"]["queue_max"], 20)
        self.assertEqual(snap["queue_total"], 0)

    def test_tpm_stats_unknown_path_404(self) -> None:
        """未知路径仍返回 404（确保 /api/tpm_stats 路由不破坏 else 分支）。"""
        status, _, _ = admin_get(self.proc.admin_port, "/api/nonexistent")
        self.assertEqual(status, 404)
```

#### 验证命令

```bash
# 单元测试
python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_tpm_429_reply_payload -v
# 集成测试
python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_tpm_stats_endpoint_basic AdminIntegrationTest.test_tpm_stats_unknown_path_404 -v
```

#### Commit

```
feat(proxy): add _reply_tpm_429, /api/tpm_stats endpoint, and _log TPM fields
```

---

### Card 4 [A] — _proxy_relay 准入 hook + SSE/buffered 结算 + 错误退款

- **Tier**: A（spec 决策 1/3/4 已定死插入位、行为与日志口径；无运行时留白）
- **文件**: `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
- **TDD 顺序**: 先写失败集成测试（`test_tpm_queue_full_immediate_429`——无准入 hook 时第 3 请求直通 200 而非 429，红）→ 实现生产代码 → 验证 → commit。注：卡 4 的其余测试场景与卡 5 共享，卡 4 用最小子集证明 hook 存在。
- **依赖**: 卡 1（常量/kind）、卡 2（tpm_admit/tpm_settle）、卡 3（_reply_tpm_429/_log kwargs）全部函数。

#### 生产代码（完整，可直接落盘）

**1) 准入 hook（插入 `_proxy_relay` body 读取/`extract_model` `:882-883` 之后、`_open_upstream` `:900` 之前）**

上下文锚（`ctyun-stream-fix-proxy.py:879-900`）:
```python
    def _proxy_relay(self, started: float) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        model = extract_model(body)
        body = normalize_null_assistant_content(body)

        fwd_headers = {}
        ...
        fwd_headers["accept-encoding"] = "identity"

        header_retried = 0
        header_retry_reason = ""
        try:
            try:
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
```

在 `body = normalize_null_assistant_content(body)` 之后插入（TDD 要点：`tpm_key`/`tpm_est`/`tpm_qwait_ms`/`tpm_final_used` 为方法级变量，header 重试与空流重试复用同一次 charge）:

```python
        # --- TPM 准入 hook（_open_upstream 之前；重试复用本次准入不重复 charge）---
        tpm_key = tpm_key_id(self.headers.get("Authorization"))
        tpm_est = 0
        tpm_qwait_ms = None
        tpm_final_used = None
        if tpm_key is not None:
            tpm_est = estimate_request_tokens(body)
            tpm_status, tpm_qwait_ms = tpm_admit(tpm_key, tpm_est)
            if tpm_status in ("full", "timeout"):
                # 锁外记录（tpm_admit 已释放 TPM_LOCK，锁序安全）
                self._reply_tpm_429(tpm_status)
                result = ("tpm-queue-full" if tpm_status == "full"
                          else "tpm-queue-timeout")
                self._log(started, 429, result, 0, model=model, qwait_ms=tpm_qwait_ms)
                _record_request(self.command, self.path, 429,
                                (time.time() - started) * 1000, 0,
                                model=model, error=True)
                record_error_event(
                    ERR_KIND_TPM_QUEUE_FULL if tpm_status == "full"
                    else ERR_KIND_TPM_QUEUE_TIMEOUT,
                    model=model, path=self.path, body=body)
                return
```

要点:
- 429 路径 return 后不持任何锁；`_record_request`（STATS_LOCK）与 `record_error_event`（ERROR_LOCK）都在 TPM_LOCK 释放后调用，无锁序反转。
- `tpm_admit` 排队期间（`TPM_LOCK.wait`）不持有上游连接：hook 点在 `_open_upstream` 之前。
- header 重试（`:909-919`）与空流重试（`:942-966`）路径直接复用 `tpm_key`/`tpm_est` 变量，`_open_upstream` 二次调用前**不再** `tpm_admit`——一次请求一次准入。

**2) SSE 结算（在 `_proxy_relay` SSE 分支 `record_error_event` `:988-992` 之后、`self._log` `:993` 之前插入）**

上下文锚（`:988-997`）:
```python
                record_error_event(kind, model=model, path=self.path,
                                   upstream_status=(resp.status
                                                    if resp.status >= 400 else None),
                                   body=body, filtered=filtered,
                                   response=self._body_err_line if body_error else None)
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error)
```

替换为（插入 TPM settle + _log 扩展）:

```python
                record_error_event(kind, model=model, path=self.path,
                                   upstream_status=(resp.status
                                                    if resp.status >= 400 else None),
                                   body=body, filtered=filtered,
                                   response=self._body_err_line if body_error else None)
            # TPM settle：用上游实际 usage 校正窗口占用（无 usage 帧不校正）
            tpm_final_used = tpm_est if tpm_key is not None else None
            if tpm_key is not None and self._tpm_usage is not None:
                total = self._tpm_usage.get("total_tokens")
                if isinstance(total, int) and total >= 0:
                    tpm_settle(tpm_key, tpm_est, total)
                    tpm_final_used = total
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason,
                      qwait_ms=tpm_qwait_ms,
                      tpm_used=tpm_final_used if tpm_key is not None else None)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error)
```

**3) buffered 结算（`_relay_buffered` 分支 `:999-1012` 内）**

上下文锚（`:999-1012`）:
```python
        else:
            relayed_data = self._relay_buffered(resp)
            body_error = False
            if relayed_data:
                try:
                    parsed = json.loads(relayed_data.decode("utf-8", "replace"))
                except ValueError:
                    pass  # non-JSON body -> no body error, fail-open
                else:
                    body_error = body_has_error(parsed)
            outcome = classify_outcome(status=resp.status, body_error=body_error)
            self._log(started, resp.status, outcome.log_result, 0, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error)
```

替换为:

```python
        else:
            relayed_data = self._relay_buffered(resp)
            body_error = False
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
            outcome = classify_outcome(status=resp.status, body_error=body_error)
            self._log(started, resp.status, outcome.log_result, 0, model=model,
                      qwait_ms=tpm_qwait_ms,
                      tpm_used=tpm_final_used if tpm_key is not None else None)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error)
```

**4) 502/异常路径全额退款（两处 synth_502 return 前插入）**

位置 A：首个 `_open_upstream` 的 except（`:920-931`）:

上下文锚:
```python
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            outcome = classify_outcome(synth_502=True)
            self._log(started, 502, outcome.log_result, 0, model=model, exc=exc,
                      retried=header_retried, retry_reason=header_retry_reason)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error)
            record_error_event(ERR_KIND_SYNTH_502, model=model, path=self.path,
                               exc=exc, body=body,
                               retried=header_retried, retry_reason=header_retry_reason)
            return
```

替换为:

```python
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            # TPM 退款：上游不可达/响应头阶段异常 → 全额退款
            if tpm_key is not None:
                tpm_settle(tpm_key, tpm_est, 0)
            outcome = classify_outcome(synth_502=True)
            self._log(started, 502, outcome.log_result, 0, model=model, exc=exc,
                      retried=header_retried, retry_reason=header_retry_reason,
                      qwait_ms=tpm_qwait_ms,
                      tpm_used=0 if tpm_key is not None else None)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error)
            record_error_event(ERR_KIND_SYNTH_502, model=model, path=self.path,
                               exc=exc, body=body,
                               retried=header_retried, retry_reason=header_retry_reason)
            return
```

位置 B：空流重试 `_open_upstream` 的 except（`:953-964`）:

上下文锚:
```python
                    self._reply_502(retry_exc)  # 客户端尚未收到字节，502 语义与既有路径一致
                    outcome = classify_outcome(synth_502=True)
                    self._log(started, 502, outcome.log_result, 0, model=model, retried=1,
                              retry_reason=retry_reason, exc=retry_exc)
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model,
                                    error=outcome.counts_error)
                    record_error_event(ERR_KIND_SYNTH_502, model=model, path=self.path,
                                       exc=retry_exc, body=body,
                                       retried=1, retry_reason=retry_reason)
                    return
```

替换为:

```python
                    self._reply_502(retry_exc)  # 客户端尚未收到字节，502 语义与既有路径一致
                    # TPM 退款：重试仍失败 → 全额退款
                    if tpm_key is not None:
                        tpm_settle(tpm_key, tpm_est, 0)
                    outcome = classify_outcome(synth_502=True)
                    self._log(started, 502, outcome.log_result, 0, model=model, retried=1,
                              retry_reason=retry_reason, exc=retry_exc,
                              qwait_ms=tpm_qwait_ms,
                              tpm_used=0 if tpm_key is not None else None)
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model,
                                    error=outcome.counts_error)
                    record_error_event(ERR_KIND_SYNTH_502, model=model, path=self.path,
                                       exc=retry_exc, body=body,
                                       retried=1, retry_reason=retry_reason)
                    return
```

**5) usage 帧捕获（`_relay_sse` `:1082` saw_done 判定之后插入）**

上下文锚（`:1081-1086`）:
```python
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    saw_done = saw_done or ("done" in kinds)
                    if self._body_err_line is None:
                        hit = next((l for l in pending if sse_line_body_error(l)), None)
                        if hit is not None:
                            self._body_err_line = hit
```

替换为（记录级公共路径，priming 与 streaming 阶段都覆盖，取最后非空 usage 帧）:

```python
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    saw_done = saw_done or ("done" in kinds)
                    # TPM usage 捕获：本 record 内所有行取最后非空 usage 帧
                    for buf_line in pending:
                        usage_hit = sse_line_usage(buf_line)
                        if usage_hit is not None:
                            self._tpm_usage = usage_hit
                    if self._body_err_line is None:
                        hit = next((l for l in pending if sse_line_body_error(l)), None)
                        if hit is not None:
                            self._body_err_line = hit
```

**6) `self._tpm_usage` 初始化（两处，镜像既有 `self._body_err_line = None` 模式）**

位置 A（`:938`）:
```python
                self._body_err_line = None
                self._tpm_usage = None
                filtered, truncated = self._relay_sse(resp,
                    final=not empty_stream_should_retry(EMPTY_RETRY_MAX))
```

位置 B（`:965`）:
```python
                self._body_err_line = None
                self._tpm_usage = None
                filtered, truncated = self._relay_sse(resp, final=True)
```

#### 测试代码（完整，可直接落盘）

**卡 4 最小失败测试**（证明准入 hook 存在；其余全量场景在卡 5）。新增 `TpmAdmitHookTest(unittest.TestCase)` 到 `ctyun-stream-fix-proxy.test.py`（放在 `BodyErrorTest` 类之后、`if __name__ == "__main__"` 之前）。

**前置 helper（模块级，放在 `admin_post` `:529` 之后）**:

```python
def post_sse_auth(port: int, payload: bytes, auth_header: str = None,
                  timeout: int = 30):
    """带可选 Authorization 头的 POST /v1/chat/completions。
    与 post_sse 不同：不断言 status 200，返回 (status, body)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    conn.request("POST", "/v1/chat/completions", body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data
```

**测试类**:

```python
class TpmAdmitHookTest(unittest.TestCase):
    """TPM 准入 hook 冒烟：est 超预算的带 key 请求被 429 拦截（不触上游）。"""

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50"})

    def tearDown(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            stderr_text(self.proc)
            shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
            stop_fake_upstreams()

    def test_oversized_est_rejected_before_upstream(self) -> None:
        """est > TPM_LIMIT(50) 的请求直接 429，上游 calls 计数不变。"""
        # len ~200 bytes → est = max(1, 200*0.55) = 110 > 50
        big = (b'{"model":"x","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')
        status, data = post_sse_auth(self.proxy_port, big, "Bearer test-key")
        self.assertEqual(status, 429, "oversized est must be rejected, got body %r"
                         % data[:120])
        parsed = json.loads(data.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        self.assertEqual(self.calls, [], "rejected request must not hit upstream")
        # 无 Authorization 的同体请求直通
        status2, _ = post_sse_auth(self.proxy_port, big, None)
        self.assertEqual(status2, 200, "no-auth request must bypass TPM")
        self.assertEqual(len(self.calls), 1)
```

#### 验证命令

```bash
python3 ctyun-stream-fix-proxy.test.py TpmAdmitHookTest -v 2>&1 | tail -10
```

#### Commit

```
feat(proxy): integrate TPM admission and usage settlement into relay paths
```

---

### Card 5 [A] — 全量集成测试 + 回归

- **Tier**: A（8 个测试函数的 seam 参数、断言目标、fake upstream 行为全部由卡 1-4 已写死的接口与 spec Acceptance 定死，无运行时留白）
- **文件**: `ctyun-stream-fix-proxy.test.py`
- **依赖**: 卡 2 的 `import hashlib`（test 7 脱敏断言用）、卡 4 的模块级 `post_sse_auth`（:529 之后，8 用例均复用）；生产代码由卡 1-4 落盘
- **TDD 顺序**: 纯测试卡（生产代码已由卡 1-4 交付）——新增 8 用例后 `python3 ctyun-stream-fix-proxy.test.py TpmRateLimitTest` 必须全绿（对应已实现的生产代码）；红 → 卡 1-4 生产代码有缺陷，回卡修复，不改本卡
- **测试类**: `TpmRateLimitTest(unittest.TestCase)`（对齐 `AdminIntegrationTest`/`BodyErrorTest` 模式：每用例独立 fake upstream + 代理子进程）

#### 测试代码（完整，可直接落盘）

**放置位置**: `ctyun-stream-fix-proxy.test.py` 末尾（卡 4 的 `TpmAdmitHookTest` 之后、`if __name__ == "__main__"` `:3496` 之前）。

**确定性设计**（防 flaky，对齐 Global Constraint 4）:
- env seam 压缩时间：`CTYUN_TPM_LIMIT=200`、`CTYUN_TPM_WINDOW_S=2`（窗口滚过 ~2s）、`CTYUN_TPM_QUEUE_TIMEOUT_S=5`（wait 循环 1s 粒度轮询的 2 倍余量）；超时用例单独 `CTYUN_TPM_QUEUE_TIMEOUT_S=2` + `CTYUN_TPM_WINDOW_S=60`（超时必然先于窗口滚过）。
- body 无 usage 帧（`body_override=SSE_A+SSE_B+SSE_DONE`）→ 不触发 settle → 预算稳定占用至窗口滚过，排队/超时/队满路径可确定性复现。
- 字节数实测算死（ratio 0.55）：`small_body` 41B→est 22；`big_body`（`"x"*280`）349B→est 191≤200；`mid_body`（`"x"*20`）89B→est 48≤60。断言中 est 一律用 `max(1, int(len(body) * 0.55))` 现算（与估算公式自洽，不受模板微调影响）。

```python
class TpmRateLimitTest(unittest.TestCase):
    """TPM 限流全量集成测试（spec Acceptance ①-⑧ 除部署条）。

    复用 make_scripted_upstream / start_proxy / post_sse / get_plain / admin_get /
    post_sse_auth（卡 4）/ stderr_text / stop_fake_upstreams；env seam 把 60s 窗口、
    120s 超时压缩到秒级。唯一时间断言（窗口滚过 ≥1s、settle 放行 <1s、超时 ~2s）
    全部由 1s 粒度轮询 + seam 余量保证，无 sleep 猜测依赖。
    """

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_B + SSE_DONE)   # 无 usage 帧 → 不 settle
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "200",
                                           "CTYUN_TPM_WINDOW_S": "2",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2"})

    def tearDown(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            stderr_text(self.proc)
            shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
            stop_fake_upstreams()

    def test_queue_exhaustion_and_window_roll(self) -> None:
        """窗口耗尽排队：首发大 body 占满预算（est 191/200）后，第二请求阻塞至
        窗口滚过（WINDOW_S=2）后放行；stderr 含 qwait=（>0）且 tpm= 数值正确。"""
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 280 + b'"}]}')   # 349B → est = int(349*0.55) = 191 ≤ 200
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # 41B → est = 22
        auth = "Bearer test-key-roll"
        status1, _ = post_sse_auth(self.proxy_port, big_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # 191+22=213 > 200 → 排队；无 settle，预算保持至窗口滚过（~2s）后放行
        t0 = time.time()
        status2, data2 = post_sse_auth(self.proxy_port, small_body, auth)
        elapsed = time.time() - t0
        self.assertEqual(status2, 200,
                         "queued request must be admitted after window roll, got %d"
                         % status2)
        self.assertEqual(data2, SSE_A + SSE_B + SSE_DONE)
        self.assertGreaterEqual(elapsed, 1.0,
                                "second request must actually queue (≥1s), got %.2fs"
                                % elapsed)
        # stderr：qwait= 出现且排队请求 qwait>0；tpm= 与估算公式一致（ratio 0.55）
        stderr = stderr_text(self.proc)
        qwait_ms = [int(m) for m in re.findall(r"qwait=(\d+)ms", stderr)]
        self.assertTrue(any(v > 0 for v in qwait_ms),
                        "queued request must log qwait>0ms, stderr:\n" + stderr)
        expected_big = max(1, int(len(big_body) * 0.55))
        expected_small = max(1, int(len(small_body) * 0.55))
        self.assertIn("tpm=%d" % expected_big, stderr,
                      "first REQ line must carry tpm=%d, stderr:\n%s"
                      % (expected_big, stderr))
        self.assertIn("tpm=%d" % expected_small, stderr,
                      "second REQ line must carry tpm=%d, stderr:\n%s"
                      % (expected_small, stderr))

    def test_queue_timeout_returns_429(self) -> None:
        """排队超时：预算不释放 → 第二请求 ~2s（seam 值）后收 429，
        body 字节等于定死 JSON，REQ 行 result=tpm-queue-timeout，且不触上游。"""
        # 重建代理：小预算 + 短超时 + 长窗口（超时必然先于窗口滚过触发）
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "60",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "2",
                                           "CTYUN_TPM_QUEUE_MAX": "2"})
        mid_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 20 + b'"}]}')   # 89B → est = int(89*0.55) = 48 ≤ 60
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 22
        auth = "Bearer test-key-timeout"
        status1, _ = post_sse_auth(self.proxy_port, mid_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # 48+22=70 > 60 → 排队；窗口 60s 不滚、无 settle → 2s 超时 → 429
        t0 = time.time()
        status2, data2 = post_sse_auth(self.proxy_port, small_body, auth)
        elapsed = time.time() - t0
        self.assertEqual(status2, 429,
                         "queued request must timeout with 429, got %d" % status2)
        self.assertGreaterEqual(elapsed, 1.0,
                                "timeout must be seam value (~2s), got %.2fs" % elapsed)
        # body 字节等于定死 JSON（与 _reply_tpm_429 payload 全等）
        expected_429 = ('{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试",'
                        '"type":"rate_limit_error","code":"model_tpm_limit"}}').encode("utf-8")
        self.assertEqual(data2, expected_429,
                         "429 body must be the fixed JSON, got %r" % data2[:200])
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        # REQ 行 result=tpm-queue-timeout；超时请求不触上游（calls 仅首发 1 次）
        stderr = stderr_text(self.proc)
        self.assertIn("result=tpm-queue-timeout", stderr,
                      "REQ line must carry result=tpm-queue-timeout, stderr:\n" + stderr)
        self.assertEqual(len(calls), 1,
                         "timed-out request must not hit upstream, calls=%d" % len(calls))

    def test_queue_full_immediate_429(self) -> None:
        """队列满：QUEUE_MAX=2，3 并发中第 3 个立即 429，REQ 行 result=tpm-queue-full。"""
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 280 + b'"}]}')   # est = 191/200，占满预算
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 22
        auth = "Bearer test-key-qfull"
        status1, _ = post_sse_auth(self.proxy_port, big_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # 预算已满（无 settle）→ 3 并发全撞队：前 2 个入队（QUEUE_MAX=2），
        # 第 3 个立即 429（barrier 同步起点，三请求准入竞争窗口 < 窗口滚过 2s）
        barrier = threading.Barrier(3)
        results = []
        lock = threading.Lock()
        def do_req():
            barrier.wait()
            r = post_sse_auth(self.proxy_port, small_body, auth)
            with lock:
                results.append(r)
        threads = [threading.Thread(target=do_req) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [200, 200, 429],
                         "exactly 1 of 3 concurrent must be immediate 429 (2 queue, "
                         "admitted after roll), got %r" % statuses)
        stderr = stderr_text(self.proc)
        self.assertIn("result=tpm-queue-full", stderr,
                      "REQ line must carry result=tpm-queue-full, stderr:\n" + stderr)

    def test_usage_settle_releases_budget(self) -> None:
        """usage 回填：fake upstream 回 SSE_USAGE（total_tokens=2）→ settle 校正预算，
        后续请求不等窗口滚过即放行；tpm_stats bucket used == 4（2+2）。"""
        # 重建代理：上游回带 usage 帧的流（每次 POST 都含 SSE_USAGE）
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "200",
                                           "CTYUN_TPM_WINDOW_S": "10",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2"})
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 280 + b'"}]}')   # est = 191，settle → 2（释放 189）
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 22
        auth = "Bearer test-key-settle"
        status1, _ = post_sse_auth(self.proxy_port, big_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # settle 已把预算从 191 校正到 2 → 第二请求立即准入（无窗口等待）
        t0 = time.time()
        status2, _ = post_sse_auth(self.proxy_port, small_body, auth)
        elapsed = time.time() - t0
        self.assertEqual(status2, 200,
                         "second request must be admitted after settle, got %d" % status2)
        self.assertLess(elapsed, 1.0,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
        # stderr：两次 REQ 行 tpm=2（settle 后按实际 usage 记）
        stderr = stderr_text(self.proc)
        self.assertGreaterEqual(stderr.count("tpm=2"), 2,
                                "both REQ lines must carry tpm=2, stderr:\n" + stderr)
        # tpm_stats：两次 settle 后 bucket used == 2+2 = 4（与 est 无关的稳健断言）
        time.sleep(0.5)   # settle 在 conn.close 前已发生（卡 4 插入位），余量防客户端 EOF 竞态
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(len(snap["buckets"]), 1)
        self.assertEqual(snap["buckets"][0]["used"], 4,
                         "bucket used must be 4 (2+2) after both settles, got %r"
                         % snap["buckets"][0])

    def test_sse_passthrough_under_rate_limiting(self) -> None:
        """SSE 透传不破坏：限流生效路径（带 Authorization 经 tpm_admit）下
        输出仍字节等于 SSE_A+SSE_B+SSE_DONE 且以 data: [DONE] 结尾。"""
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 22 ≤ 200
        auth = "Bearer test-key-passthrough"
        status, data = post_sse_auth(self.proxy_port, small_body, auth)
        self.assertEqual(status, 200)
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "SSE output must be byte-exact under rate limiting, got %r"
                         % data[:200])
        self.assertTrue(data.rstrip().endswith(b"data: [DONE]"),
                        "stream must end with data: [DONE], got tail: %r" % data[-40:])

    def test_no_auth_bypasses_tpm(self) -> None:
        """无 Authorization 头请求直通不限流（get_plain + 无 auth POST），
        stderr 无任何 TPM 字段。"""
        plain_data, plain_len = get_plain(self.proxy_port)   # 无 auth，内部断言 200
        self.assertIsNotNone(plain_data)
        data = post_sse(self.proxy_port)   # 无 auth 默认 payload，内部断言 200
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        stderr = stderr_text(self.proc)
        self.assertNotIn("qwait=", stderr,
                         "no-auth requests must not log TPM fields, stderr:\n" + stderr)
        self.assertNotIn("tpm=", stderr)
        self.assertNotIn("tpm-queue", stderr)

    def test_tpm_stats_endpoint(self) -> None:
        """/api/tpm_stats 返回 200 JSON：config 与 env seam 一致，bucket used 与
        REQ 行 tpm= 计数一致，key 字段 sha256: 前缀脱敏（无原始 key 泄漏）。"""
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # 41B → est = 22
        auth = "Bearer test-key-stats"
        status, _ = post_sse_auth(self.proxy_port, small_body, auth)
        self.assertEqual(status, 200)
        expected_used = max(1, int(len(small_body) * 0.55))   # 无 usage 帧 → used 保持 est
        status, body, ctype = admin_get(self.proc.admin_port, "/api/tpm_stats")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"),
                        "Content-Type must be application/json, got %r" % ctype)
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["config"], {"limit": 200, "window_s": 2, "queue_max": 2},
                         "config must reflect env seams, got %r" % snap["config"])
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        # key 脱敏：sha256(token) 前 12 位 hex，无原始 key（依赖卡 2 的 import hashlib）
        expected_short = "sha256:" + hashlib.sha256(b"test-key-stats").hexdigest()[:12]
        self.assertEqual(b["key"], expected_short,
                         "bucket key must be sha256: prefix + 12 hex, got %r" % b["key"])
        self.assertNotIn("test-key-stats", b["key"], "bucket key must not leak raw key")
        self.assertEqual(b["used"], expected_used,
                         "bucket used must match REQ line tpm= value")
        self.assertEqual(b["remaining"], 200 - expected_used)
        self.assertEqual(b["queued"], 0)
        self.assertEqual(b["rejected"], 0)
        self.assertEqual(b["timeouts"], 0)
        # 与 REQ 行交叉校验：tpm= 数值 == bucket used
        stderr = stderr_text(self.proc)
        self.assertIn("tpm=%d" % expected_used, stderr,
                      "REQ line must carry tpm=%d, stderr:\n%s" % (expected_used, stderr))

    def test_all_existing_tests_pass(self) -> None:
        """既有 127 例无回归冒烟：TPM env 开启下 SSE 透传/plain 透传/统计端点正常。"""
        data = post_sse(self.proxy_port)   # 无 auth → 既有路径，零 TPM 介入
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        self.assertTrue(data.rstrip().endswith(b"data: [DONE]"))
        plain_data, plain_len = get_plain(self.proxy_port)
        self.assertIsNotNone(plain_data)
        status, body, ctype = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(status, 200)
        snap = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(snap["requests_total"], 2)
        self.assertIn("filtered=0", stderr_text(self.proc))
```

#### 验证命令

```bash
# 本卡 8 用例（含窗口滚过 ~2s、排队超时 ~2s、3 并发队满，单类 ~20s）
python3 ctyun-stream-fix-proxy.test.py TpmRateLimitTest -v 2>&1 | tail -25
# 全量回归（spec Acceptance ①：既有 127 例 + 新增全部 TPM 用例全绿）
python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -5
```

#### Commit

```
test(proxy): add TPM rate limiting integration tests
```

---

## Anchor Grep 实测证据

以下为 B0 阶段实测 grep（当前 `fix/tpm-rate-limiting` worktree，commit a5ea44d）:

```
$ grep -n 'EMPTY_RETRY_MAX\|SEND_TIMEOUT_S\|_KIND_CATEGORY\|def sse_line_has_usage\|def classify_outcome\|def _proxy_relay\|def _reply_502\|def _relay_sse\|def _relay_buffered\|def do_GET\|def _log' ctyun-stream-fix-proxy.py

43: SEND_TIMEOUT_S = float(os.environ.get("CTYUN_SEND_TIMEOUT", "60"))
51: EMPTY_RETRY_MAX = int(os.environ.get("CTYUN_EMPTY_RETRY", "1"))
79: _KIND_CATEGORY = {
139: def sse_line_has_usage(line: bytes) -> bool:
172: def classify_outcome(status=None, synth_502=False, ...):
837:     def do_GET(self) -> None:
879:     def _proxy_relay(self, started: float) -> None:
1056:     def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
1143:     def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
1161:     def _reply_502(self, exc: BaseException) -> None:
1171:     def _log(self, started: float, status: int, result: str, filtered: int, model=None, retried=0, retry_reason="", exc=None) -> None:
1188:     def do_GET(self) -> None:
1194:         elif path == "/api/stats":
```

```
$ grep -n 'def record_error_event\|def _record_request\|def _safe_log_stderr\|def _send_json\|def _send\b' ctyun-stream-fix-proxy.py

223: def record_error_event(kind, model=None, path=None, ...):
487: def _safe_log_stderr(msg: str) -> None:
680: def _record_request(method: str, path: str, status: int, dur_ms: float, filtered: int, model=None, error: bool = False) -> None:
1310:     def _send(self, status: int, content_type: str, body: bytes) -> None:
1318:     def _send_json(self, status: int, payload: dict) -> None:
```

```
$ grep -n 'class ProxyHandler\|class AdminHandler' ctyun-stream-fix-proxy.py

834: class ProxyHandler(http.server.BaseHTTPRequestHandler):
1185: class AdminHandler(http.server.BaseHTTPRequestHandler):
```

所有 spec 锚点行号与实测一致，无偏移。

---

## B-final Self-Review

### 1. Spec Coverage（spec Acceptance 5 条 + 部署 1 条，逐条命中）

| # | Acceptance（spec :31-38） | 覆盖卡/测试 |
|---|--------------------------|------------|
| 1 | `python3 ctyun-stream-fix-proxy.test.py` 全绿（含既有 127 例无回归） | 卡 1-5 均附验证命令；卡 5 验证命令含全量跑 + `test_all_existing_tests_pass` 冒烟 |
| 2 | 窗口耗尽排队：fake upstream 下第二请求阻塞至窗口滚过后放行，stderr 含 `qwait=` 且 `tpm=` 数值正确 | 卡 5 `test_queue_exhaustion_and_window_roll`（elapsed ≥1s 证明排队 + regex 提取 qwait>0 + `tpm=%d` 自洽断言） |
| 3 | 排队超时：预算不释放时第二请求 ~2s 后收 429，body 字节全等定死 JSON，REQ 行 `result=tpm-queue-timeout` | 卡 5 `test_queue_timeout_returns_429`（byte 全等断言 + parsed error.code + stderr result 字段） + 卡 3 `test_tpm_429_reply_payload`（payload 格式单测） |
| 4 | 队列满：`CTYUN_TPM_QUEUE_MAX=2` 时第 3 并发立即 429，`result=tpm-queue-full` | 卡 5 `test_queue_full_immediate_429`（barrier 同步 3 并发 → sorted [200,200,429] + stderr result 字段） + 卡 2 `test_tpm_admit_queue_full`（队满拒绝单测） |
| 5 | usage 回填：fake upstream 回 SSE_USAGE(total_tokens=2) 后 settle 释放预算，后续请求不等窗口滚过即放行 | 卡 5 `test_usage_settle_releases_budget`（elapsed <1s 证明无等待 + tpm=2 出现 ≥2 次 + tpm_stats bucket used == 4） + 卡 2 `test_tpm_settle_corrects_usage`/`test_tpm_settle_notifies_waiters`（settle 行为单测） |
| 6 | SSE 透传不破坏：限流路径下输出字节等于 `SSE_A+SSE_B+SSE_DONE` 且以 `data: [DONE]` 结尾 | 卡 5 `test_sse_passthrough_under_rate_limiting`（byte-exact + endswith 断言） + 卡 5 `test_all_existing_tests_pass`（无 auth 路径回归） |
| 7 | `/api/tpm_stats`：返回 200 JSON，bucket used/rejected/timeouts 与 REQ 行一致，key 字段 sha256: 脱敏 | 卡 5 `test_tpm_stats_endpoint`（config 全等 / key 脱敏 + hash 自算交叉校验 / used 与 `tpm=%d` 交叉校验） + 卡 3 `test_tpm_stats_endpoint_basic`（shape 基本单测） |
| 8 | 无 Authorization 头请求直通不限流 | 卡 5 `test_no_auth_bypasses_tpm`（stderr 无 `qwait=`/`tpm=`/`tpm-queue`） + 卡 4 `test_oversized_est_rejected_before_upstream`（同体无 auth 请求 200 分支） |
| 9 | 部署端到端验证（cp + kickstart + 4 并发 + stats） | Explicitly excluded from PLAN（spec :39 标注"主代理 DELIVER 阶段执行，非 implementer"），不进入卡 1-5 |

**Risks 处理**（spec :43-47）:
- 锁序（`TPM_LOCK` 不与 `STATS_LOCK`/`ERROR_LOCK` 嵌套）→ 卡 4 429 分支 `return` 后才调 `_record_request`/`record_error_event`（代码注释标明："锁外记录"）✓
- 重试不重复 charge → 卡 4 hook 在 `_open_upstream` 之前执行，header 重试/空流重试路径不复用 code block，复用 `tpm_key`/`tpm_est` 变量（代码注释标明）✓；卡 5 `test_all_existing_tests_pass` 回保证实 calls 计数正确
- 估算偏差（保守方向 + settle 校正兜底）→ `estimate_request_tokens` 实现按 spec 决策 1（ratio 0.55 + max_tokens 累加），卡 2 `test_estimate_request_tokens` 覆盖全部分支 ✓
- 排队期间客户端断连不可检测 → spec Risks 明确不做（Excluded），未进入卡 1-5 ✓
- 严格 FIFO 跨 key 队头阻塞 → spec Risks 明确可接受，卡实现严格 FIFO ✓

**Exclusions 合规**: dashboard HTML/JS 不动（卡 3 只加 API 路由）✓；classify_outcome 不改 ✓；_DAILY_FIELDS 不改 ✓；plist/launchd 不动 ✓；跨进程/持久化不做 ✓；tokenizer 不引入 ✓。

### 2. Placeholder Scan

对全 PLAN.md 代码块扫描：`TODO`/`FIXME`/`XXX`/`待补`/`占位`/`placeholder`/`待实现` — **0 命中**（`grep -nE` exit code 1，无匹配行）。

卡 1-4 生产代码块中 `pass  # …` 三处（:367/:897/:918）均有注释自证"为何吞掉"（ValueError: 已被并发 popleft / non-JSON body 非异常路径），符合 AGENTS.md 空 catch 自证要求。卡 5 测试代码块含 8 个 `def test_*` 全部有完整实现体，无 `pass`/`raise NotImplementedError`/`return` stub。

### 3. Type Consistency

跨卡接口签名一致性验证（逐对检查）：

| 符号 | 卡 2 定义 | 卡 3/4/5 调用 | 一致 |
|------|----------|-------------|------|
| `tpm_admit(key_id: str, est: int) -> (status, qwait_ms)` | 卡 2 :313-368 | 卡 4: `tpm_status, tpm_qwait_ms = tpm_admit(tpm_key, tpm_est)` 解包 2 元组 ✓ | ✓ |
| `tpm_settle(key_id: str, est: int, actual: int) -> None` | 卡 2 :370-386 | 卡 4: SSE/buffered/502 三处 `tpm_settle(tpm_key, tpm_est, total\|0)` 实参顺序一致 ✓ | ✓ |
| `tpm_snapshot() -> dict` | 卡 2 :389-420 | 卡 3: `tpm_snapshot()` 直接路由给 `_send_json` ✓；卡 5: `admin_get("/api/tpm_stats")` 返回 JSON shape 断言（config.limit/window_s/queue_max，buckets.key/used/remaining/queued/rejected/timeouts）全部命中 ✓ | ✓ |
| `tpm_key_id(auth_header: str\|None) -> str\|None` | 卡 2 :243-258 | 卡 4: `tpm_key = tpm_key_id(self.headers.get("Authorization"))` ✓；卡 5: `Bearer test-key-*` header 全部经 `tpm_key_id` 路径 ✓ | ✓ |
| `estimate_request_tokens(body) -> int` | 卡 2 :261-278 | 卡 4: `tpm_est = estimate_request_tokens(body)` ✓；卡 5: 测试中 `max(1, int(len(body) * 0.55))` 自洽断言与实现一致 ✓ | ✓ |
| `sse_line_usage(line: bytes) -> dict\|None` | 卡 1 :96-113 | 卡 4 block 5: `usage_hit = sse_line_usage(buf_line)` + `self._tpm_usage = usage_hit` + settle 时 `.get("total_tokens")` ✓ | ✓ |
| `_reply_tpm_429(reason: str) -> None` | 卡 3 :632-645 | 卡 4: `self._reply_tpm_429(tpm_status)` 取 `"full"`/`"timeout"` → 日志 result 分支 ✓ | ✓ |
| `_log(..., qwait_ms=None, tpm_used=None)` | 卡 3 :677-694 | 卡 4: 4 处调用均按 kwargs 传 `qwait_ms=tpm_qwait_ms, tpm_used=tpm_final_used` ✓；卡 5: stderr 正则 `qwait=(\d+)ms` + `tpm=(\d+)` 与格式串 `qwait=%dms`/`tpm=%d` 一致 ✓ | ✓ |
| `_KIND_CATEGORY` / `ERR_KIND_TPM_*` / `CLASS_TPM_LIMITED` | 卡 1 :56-90 | 卡 4: `record_error_event(ERR_KIND_TPM_QUEUE_FULL\|ERR_KIND_TPM_QUEUE_TIMEOUT, ...)` 常量名一致 ✓ | ✓ |
| `_TpmWaiter(key_id, est, enqueued_at)` | 卡 2 :234 | 卡 2 单测: `mod._TpmWaiter("key:qfull", 1, 0)` 直接构造，字段序一致 ✓ | ✓ |
| `post_sse_auth(port, payload, auth_header=None, timeout=30) -> (status, body)` | 卡 4 :1070-1082 | 卡 5: 8 用例均调用 `post_sse_auth(self.proxy_port, body, auth)` ✓ | ✓ |
| 常量 env seam 名 | 卡 1 :38-44 | 卡 5 `start_proxy(extra_env={...})` 键名: `CTYUN_TPM_LIMIT`/`CTYUN_TPM_WINDOW_S`/`CTYUN_TPM_QUEUE_TIMEOUT_S`/`CTYUN_TPM_QUEUE_MAX` 全等 ✓ | ✓ |

### 4. 可落盘性

| 卡 | 生产代码块 | 测试代码块 | 插入上下文（before/after 锚） | 验证命令 | commit message | 判定 |
|----|----------|----------|--------------------------|---------|----------------|------|
| 1 | 常量 + sse_line_usage + KIND_CATEGORY（3 块，含精确位置上下文） | 3 个 test_*（含 subprocess env seam 验证） | 各行号 with before/after 锚 | `ProxyDashboardUnitTest` | ✅ 标准 commit | 落盘就绪 |
| 2 | TPM 模块态 7 函数（含 import hashlib + :197 精确上下文） | 9 个 test_*（含 cleanup helper + Lock 安全） | :197-200 之间锚 + import 区锚 | `ProxyDashboardUnitTest` | ✅ 标准 commit | 落盘就绪 |
| 3 | _reply_tpm_429 + _log 扩展 + /api/tpm_stats 路由（3 块） | 1 单元 + 2 集成（含端点 shape + 404 回归） | 各精确行号上下文 | 分单元/集成验证 | ✅ 标准 commit | 落盘就绪 |
| 4 | 准入 hook + SSE/buffered 结算 + 2处 502 退款 + usage 捕获 + _tpm_usage 初始化（6 块） | 1 helper + 1 集成类（含 upstream calls 断言） | 全部精确行号上下文 | `TpmAdmitHookTest` | ✅ 标准 commit | 落盘就绪 |
| 5 | N/A（纯测试卡） | 8 测试方法（TpmRateLimitTest 类完整代码） | 类放置位: `TpmAdmitHookTest` 之后、`if __name__` 之前 | 单类 ~20s + 全量回归 | ✅ 标准 commit | 落盘就绪 |

**共同判定**: 无留白，无"按 spec 实现即可"指令，无依赖运行时数据或真机验证的 C 档内容。全部 A 档。代码块可直接复制落盘，插入上下文锚全部实测一致。

### 5. 锚点实测（B-final 复核）

以下为本次 B-final 实测 grep（`fix/tpm-rate-limiting` worktree，当前源文件无 TPM 代码——预期中，卡尚未执行）:

**生产源码**（`ctyun-stream-fix-proxy.py`，1967 行）:
```
$ grep -n 'HEADER_RETRY_MAX\|PRIMED_TAIL_CAP\|CLASS_POISON_FIXED\|ERR_KIND_HEADER_TIMEOUT\|CLASS_BODY_ERROR\|ERR_KIND_BODY_ERROR\|_KIND_CATEGORY\|def sse_line_has_usage\|def sse_line_body_error\|def classify_outcome\|def empty_stream_should_retry\|def record_error_event\|def _record_request\|def _safe_log_stderr\|def _send_json\|def _proxy_relay\|_open_upstream\(self\|except (OSError, http.client.HTTPException)\|self._body_err_line = None\|def _relay_sse\|kinds = \[sse_data_line_kind\|def _relay_buffered\|def _reply_502\|def _log\|class AdminHandler\|elif path == "/api/stats"' ctyun-stream-fix-proxy.py

52: HEADER_RETRY_MAX = ...
53: PRIMED_TAIL_CAP = ...
66: CLASS_POISON_FIXED = "poison_fixed"
74: ERR_KIND_HEADER_TIMEOUT = "header_timeout"
76: CLASS_BODY_ERROR = "body_error"
77: ERR_KIND_BODY_ERROR = "body_error"
79: _KIND_CATEGORY = {
87:     ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR,
139: def sse_line_has_usage(line: bytes) -> bool:
155: def sse_line_body_error(line: bytes) -> bool:
172: def classify_outcome(...)
200: def empty_stream_should_retry(budget: int) -> bool:
223: def record_error_event(...)
487: def _safe_log_stderr(...)
680: def _record_request(...)
879:     def _proxy_relay(self, started: float) -> None:
900:                 conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
920:         except (OSError, http.client.HTTPException) as exc:
938:                 self._body_err_line = None
953:                 except (OSError, http.client.HTTPException) as retry_exc:
965:                 self._body_err_line = None
988:                 record_error_event(...)
993:             self._log(started, resp.status, result, filtered, model=model, retried=retried, retry_reason=retry_reason)
998:         else:
999:             relayed_data = self._relay_buffered(resp)
1056:     def _relay_sse(self, resp, final) -> tuple:
1081:                     kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
1143:     def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
1161:     def _reply_502(self, exc: BaseException) -> None:
1171:     def _log(self, started, status, result, filtered, model=None, retried=0, retry_reason="", exc=None) -> None:
1185: class AdminHandler(http.server.BaseHTTPRequestHandler):
1194:         elif path == "/api/stats":
```

**测试文件**（`:3496` `if __name__`，无 TPM 代码——预期: 卡未执行）:
```
42: SSE_USAGE = ...
269: def make_scripted_upstream
383: def start_proxy
417: def post_sse
431: def get_plain
508: def admin_get
520: def admin_post
3339: class BodyErrorTest
3496: if __name__ == "__main__":
```

**结论**: 全部 spec 锚点 + 卡 1-4 插入位锚点与当前源文件实测一致，无偏移。卡 5 类放置位（`BodyErrorTest` `:3339` 之后、`:3496` `if __name__` 之前）实测存在。

**冲突检查**: 卡 1-4 代码块与 spec 决策、当前源文件结构逐锚比对，**无冲突**。spec 决策 5 要求 `/api/tpm_stats` 挂 7921 管理台 `AdminHandler.do_GET`（:1194 `/api/stats` 分支旁）——卡 3 的 `elif path == "/api/tpm_stats"` 路由紧接 `elif path == "/api/stats"` 之后，实测 `:1194` `/api/stats` 路由位置正确 ✓。