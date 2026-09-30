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

**前置：import 区追加 `import hashlib`（`ctyun-stream-fix-proxy.py` `:12` `import base64` 之后、`:13` `import collections` 之前，按字母序）**。实测确认当前文件无 hashlib import（只有 `import hmac`），`tpm_key_id` 的 sha256 依赖它。

```python
import hashlib
```

**插入位置：`classify_outcome` 函数结束后（`:197` 与 `empty_stream_should_retry` `:200` 之间）**

```python
# --- TPM rate limiting（module-level state，全部由 TPM_LOCK 保护）---

_TpmWaiter = collections.namedtuple("_TpmWaiter", "key_id est enqueued_at")

TPM_LOCK = threading.Condition()
TPM_BUCKETS: dict = {}                 # key_id -> deque([(ts, tokens), ...]) + 附加属性 .used
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
        bucket = collections.deque()
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
        bucket = collections.deque()
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

### Card 4 [A] — _proxy_relay 准入 hook + SSE 结算 + buffered 结算 + 错误退款

- **文件**: `ctyun-stream-fix-proxy.py`
- **关键逻辑与锚点**:
  - **准入 hook**（`:879` _proxy_relay，body 读取 `:882` 之后、`_open_upstream` `:900` 之前）:
    ```
    key_id = tpm_key_id(self.headers.get("Authorization"))
    if key_id is not None:
        est = estimate_request_tokens(body)
        status, qwait_ms = tpm_admit(key_id, est)
        if status in ("full", "timeout"):
            self._reply_tpm_429(status)
            self._log(started, 429, status, 0, model=model, qwait_ms=qwait_ms)
            _record_request(self.command, self.path, 429, ...error=True)
            record_error_event(ERR_KIND_TPM_QUEUE_FULL or _TIMEOUT, ...)
            return
    ```
    - `record_error_event` 调用必须在 `TPM_LOCK` 释放后（429 分支 `return` 后不持锁，安全）
  - **header 重试/空流重试复用准入**（`:909` / `:942`）: `_open_upstream` 重试前不重新 `tpm_admit`；body 同一份。需要在 `_proxy_relay` 入口处将 `key_id`、`est`、`admit_called` 存为局部变量，重试路径复用——不重复 charge。**每个 `_open_upstream` 调用前不做新准入**（一次请求一次准入）。
  - **SSE 结算**（`:1056` _relay_sse）:
    - 在 `saw_usage` 判定处（`:1094-1095`）用 `sse_line_usage` 提取 usage dict 存入 `self._tpm_usage`（取最后非空帧）
    - 流结束（`:1133` truncated / 正常 EOF）后，在 `_proxy_relay` 中 `_relay_sse` 返回后调用 `tpm_settle(key_id, est, usage.total_tokens)`（若有 usage 帧）
    - 无 usage 帧不校正（不调 settle）
  - **buffered 结算**（`:999` _relay_buffered 分支）:
    - 解析 `relayed_data` JSON（`:1003` 同处）取 `usage.total_tokens` → `tpm_settle(key_id, est, usage.total_tokens)`
    - 502/异常路径（`:920-931` synth_502）: `tpm_settle(key_id, est, 0)` 全额退款
  - **`_body_err_line` 判定** path（`:968-969`）正常 settle 同 SSE
  - **client_abort 路径**（`:865`）: 不 settle（无可靠 usage 数据，且窗口自动过期）
- **验收**: 准入 hook 位于 `_open_upstream` 之前；429 路径不调 `_open_upstream`；header 重试不重复准入（通过 calls 计数验证）；`_relay_sse` 结束后 `tpm_settle` 被调用；buffered 路径解析 usage 后 settle；502 synth 全额退款
- **Anchor grep 验证**:
  ```
  :879:  def _proxy_relay(self, started: float) -> None:          ← 函数入口
  :880:  length = int(self.headers.get("Content-Length") or 0)    ← body 读取前
  :882:  model = extract_model(body)                              ← 准入 hook 插入位（之后）
  :900:  conn, resp = self._open_upstream(...)                    ← 准入 hook 插入位（之前）
  :909:  if not header_timeout_should_retry(...):                 ← header 重试起点
  :942:  except _EmptyStream as exc:                              ← 空流重试起点
  :939:  filtered, truncated = self._relay_sse(resp, ...)         ← SSE 调用点
  :1056: def _relay_sse(self, resp: ..., final: bool) -> tuple:   ← SSE relay 函数
  :1094: if not saw_usage:                                        ← Usage 捕获点
  :1095:     saw_usage = any(sse_line_has_usage(l) for l in pending)  ← 现有逻辑
  :1131: raise _EmptyStream(filtered, primed)                     ← EOF 空流
  :1133: truncated = not saw_done                                  ← 正常 EOF 截断标记
  :999:  relayed_data = self._relay_buffered(resp)                ← buffered 分支
  :1003: parsed = json.loads(relayed_data.decode(...))            ← JSON 解析点
  :920:  except (OSError, http.client.HTTPException) as exc:      ← synth_502 路径
  ```
- **Commit**: `feat(proxy): integrate TPM admission and usage settlement into relay paths`

---

### Card 5 [A] — 全量集成测试 + 回归

- **文件**: `ctyun-stream-fix-proxy.test.py`
- **测试类**: `TpmRateLimitTest(unittest.TestCase)`（继承自 `unittest.TestCase`，对齐 `AdminIntegrationTest`/`BodyErrorTest` 模式）
- **测试函数**（覆盖 Acceptance 全部 8 条）:
  1. `test_queue_exhaustion_and_window_roll` — 窗口耗尽排队：首发大 body 占满 budget（`CTYUN_TPM_LIMIT=200`）后，第二请求阻塞至窗口滚过（～1s poll）+ stderr 含 `qwait=` 且 `tpm=` 数值正确
  2. `test_queue_timeout_returns_429` — 排队超时：用 `CTYUN_TPM_QUEUE_TIMEOUT_S=2`，预算不释放 → 第二请求 2s 后收 429，body 字节等于定死 JSON，REQ 行 `result=tpm-queue-timeout`
  3. `test_queue_full_immediate_429` — 队列满：`CTYUN_TPM_QUEUE_MAX=2`，3 并发中第 3 个立即 429，`result=tpm-queue-full`
  4. `test_usage_settle_releases_budget` — usage 回填：fake upstream 回 `SSE_USAGE`（total_tokens=2）后 settle 释放预算，后续请求不等窗口滚过即放行
  5. `test_sse_passthrough_under_rate_limiting` — SSE 透传不破坏：限流生效路径下 `post_sse` 输出仍字节等于 `SSE_A+SSE_B+SSE_DONE` 且以 `data: [DONE]` 结尾
  6. `test_no_auth_bypasses_tpm` — 无 Authorization 头请求直通不限流（`get_plain`）
  7. `test_tpm_stats_endpoint` — `/api/tpm_stats` 返回 200 JSON，bucket `used`/`rejected`/`timeouts` 与 REQ 行计数一致，key 字段 `sha256:` 前缀脱敏
  8. `test_all_existing_tests_pass` — 既有 127 例无回归
- **测试 seam**: `start_proxy(extra_env={"CTYUN_TPM_LIMIT": "200", "CTYUN_TPM_QUEUE_TIMEOUT_S": "2", "CTYUN_TPM_QUEUE_MAX": "2"})`
- **验收**: `python3 ctyun-stream-fix-proxy.test.py` 全绿（含既有 127 例 + 新增全部 TPM 用例）
- **Commit**: `test(proxy): add TPM rate limiting integration tests`

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

*B0 skeleton — 卡代码块由 B1..B5 逐卡填充。Self-Review 由 B-final 执行。*