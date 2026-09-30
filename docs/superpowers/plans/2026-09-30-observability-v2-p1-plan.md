# PLAN.md — observability v2 P1 地基

**Spec**: `docs/superpowers/specs/2026-09-30-ctyun-proxy-observability-v2-design.md`  
**Worktree**: `fix/observability-v2-p1`  
**Scope**: P1 地基期 only — request id / upstream host / 阶段计时 / lock 文档化 / `_record_request` 签名扩展  
**Out of scope**: P2-P5 (histogram, token, probe, dashboard) — 仅常量声明  
**文件**: `ctyun-stream-fix-proxy.py` + `ctyun-stream-fix-proxy.test.py`

## Baseline

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py
Ran 127 tests in 57.733s
OK
Exit code: 0
```

127 tests all green (brief 声称 118 已过时，实测 127)。所有基线测试必须保持全绿。

## Global Constraints

1. **测试必须用 `/usr/bin/python3`**（README.md:84 明示；Homebrew python3.14 不兼容）。
2. **热路径计时用 `time.monotonic()`** 不用 `time.time()`（防 NTP 跳变污染直方图，P2 起用但 P1 先定规约）。
3. **锁纪律**：按 spec P1 锚点文档化现状；新增 `PROBE_LOCK` 在 P4 才创建（P1 仅注释声明，防止死代码）。
4. **No Placeholders**：所有生产/测试代码写死，无 TODO/省略号/"类似地"/指令性描述。
5. **P1 仅写 rid/host/ttfb_ms/stream/outcome**；`tokens`/`bytes_out` 在签名中但 callers 传 None/0（P3 填）。

## Anchor Reconciliation（spec 冲突裁决）

spec P1 列出 `import uuid` 与 `REQUEST_SEQ = 0`，但 `:858` 代码级锚点明确 rid 生成方式为 `rid = "r-%d" % (STATS["requests_total"])`（复用计数器，前缀 `r-` 区分外部 id）。按 spec 内部"详细锚点 > 枚举行"的优先级：
- **不 import uuid**（会被 linter 标记 unused-import → dead code）
- **不新增 REQUEST_SEQ**（无消费者 → dead code）
- 这两项不进入任何任务卡。

## 实测锚点（grep 验证）

| spec 引用 | 实测行号 | 内容 |
|-----------|---------|------|
| `:26` import | 26 | `import urllib.parse`（import 尾行） |
| `:53` 常量 | 53 | `PRIMED_TAIL_CAP = 262144` |
| `:312` _CFG_LOCK | 312 | `_CFG_LOCK = threading.Lock()` |
| `:313` STATS_LOCK | 313 | `STATS_LOCK = threading.Lock()` |
| `:320` RECENT_REQUESTS | 320 | `RECENT_REQUESTS = collections.deque(maxlen=100)` |
| `:558` _DAILY_FIELDS | 558 | `_DAILY_FIELDS = ("requests", "filtered", ...)` |
| `:680` _record_request | 680 | `def _record_request(method: str, path: str, ...)` |
| `:718-720` RECENT append | 718 | `RECENT_REQUESTS.append({"ts": time.time(), ...})` |
| `:858` _proxy | 858 | `def _proxy(self) -> None:` |
| `:872-874` _proxy except | 872-874 | `self._log(started, 499, ...)` + `_record_request(self.command, ...)` |
| `:900` _open_upstream 首呼 | 900 | `conn, resp = self._open_upstream(...)` |
| `:919` retry 呼 | 919 | `conn, resp = self._open_upstream(...)` |
| `:923-927` 502 _log+_record | 923-927 | 502 路径的 _log + _record_request |
| `:952-953` 空流 retry | 952-953 | `conn, resp = self._open_upstream(...)` |
| `:956-960` 空流 retry 502 | 956-960 | 空流重试失败路径 |
| `:965-967` 空流 retry 成功 | 965-967 | `self._relay_sse(resp, final=True)` |
| `:993-997` SSE success | 993-997 | SSE 路径 _log + _record_request |
| `:1009-1012` buffered | 1009-1012 | 非流式路径 _log + _record_request |
| `:1046` _send_sse_headers | 1046 | `def _send_sse_headers(self, resp) -> None:` |
| `:1143` _relay_buffered | 1143 | `def _relay_buffered(self, resp: ...)` |
| `:1161` _reply_502 | 1161 | `def _reply_502(self, exc: ...)` |
| `:1171` _log | 1171 | `def _log(self, started: float, ...)` |

---

## Task 1 [tier: B] — Constants + lock 文档化

**内容**: 新增 v2 常量（HIST_BUCKETS_MS / PROBE_* / DAILY_V2_FIELDS）、更新 RECENT_REQUESTS 注释、新增锁纪律注释块。纯模块级声明，无运行时行为变化。tier B（机械追加，多锚点但无设计决策）。

### 生产代码

**Edit 1-1**: 在 `PRIMED_TAIL_CAP = 262144`（行 53）后新增常量块。

old_string:
```
PRIMED_TAIL_CAP = 262144  # finish hold 尾段缓冲上限（超限 fail-open 防内存膨胀）

ERROR_RING_MAX = 50
```
new_string:
```
PRIMED_TAIL_CAP = 262144  # finish hold 尾段缓冲上限（超限 fail-open 防内存膨胀）

# observability v2 常量（P1 先声明；HIST_BUCKETS_MS P2 起用、PROBE_* P4 起用）
HIST_BUCKETS_MS = (100, 250, 500, 1000, 2000, 5000, 10000, 30000)  # 8 桶右开边界，+inf 隐式第 9 桶（对数等比 ≈ ×2.5，覆盖 100ms~30s+）
PROBE_INTERVAL_S_DEFAULT = 30      # v2 P4：probe 线程默认间隔（CTYUN_PROBE_INTERVAL_S env seam 可调）
PROBE_TIMEOUT_S = 5                # v2 P4：probe HEAD 请求超时
PROBE_MIN_INTERVAL_S = 10          # v2 P4：probe 间隔下限（防滥用）
PROBE_FAILURE_THRESHOLD = 3        # v2 P4：连续失败次数达此值触发 EVENTS probe_alert
PROBE_ALERT_DEBOUNCE_S = 300       # v2 P4：同类 probe 告警最小间隔（去抖）

ERROR_RING_MAX = 50
```

**Edit 1-2**: 在 `_DAILY_FIELDS`（行 558）后新增 `DAILY_V2_FIELDS`。

old_string:
```
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream",
                 "retries", "eof_without_done", "header_retries")


def load_daily_buckets(path: str) -> dict:
```
new_string:
```
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream",
                 "retries", "eof_without_done", "header_retries")

# v2 P3 起用（P1 先声明）：daily/daily_by_model 16 字段 schema。
# P3 切换前零引用；load/save 循环各按迭代时的 _DAILY_FIELDS 白名单，
# 切换后旧格式自动补 0，新格式被旧版加载时自动丢新字段。
DAILY_V2_FIELDS = _DAILY_FIELDS + ("tokens_prompt", "tokens_completion",
                                   "bytes_out", "stream_requests",
                                   "ttfb_sum_ms", "ttfb_count",
                                   "outcome_ok", "outcome_degraded",
                                   "outcome_failed")


def load_daily_buckets(path: str) -> dict:
```

**Edit 1-3**: 更新 `RECENT_REQUESTS` 注释（行 320）。

old_string:
```
RECENT_REQUESTS = collections.deque(maxlen=100)  # {"ts","method","path","status","dur_ms","filtered","model"}
```
new_string:
```
RECENT_REQUESTS = collections.deque(maxlen=100)  # {"ts","method","path","status","dur_ms","filtered","model","rid","upstream_host","ttfb_ms","stream","tokens","bytes_out","outcome"}
```

**Edit 1-4**: 在 `_CFG_LOCK`（行 312）前新增锁纪律注释块。

old_string:
```
_CFG_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
```
new_string:
```
# 锁纪律（现状审计，2026-09-30 文档化）：
#   STATS_LOCK — 保护 STATS（含 daily/daily_by_model 桶）/ RECENT_REQUESTS / EVENTS /
#                POISON_PREVIEWS / _stats_dirty
#   _CFG_LOCK  — 保护 UPSTREAM_BASE / _upstream_source / CAPTURE_ERRORS（只护写；
#                UPSTREAM_BASE 读侧无锁：Python 引用赋值原子，读线程看到任一完整旧值）
#   ERROR_LOCK — 保护 ERROR_EVENTS / _ERROR_EVENT_SEQ
#   LOG_LOCK   — 保护 LOG_RING / _LOG_SEQ
#   PROBE_LOCK — P4 新增，保护 _PROBE_STATE；独立于 STATS_LOCK：探测线程与请求线程
#                共享 STATS 时只经 STATS_LOCK 短持锁更新计数器，绝不持锁做网络 IO。
_CFG_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
```

### 测试代码

追加到 `ctyun-stream-fix-proxy.test.py` 末尾（`if __name__ == "__main__":` 之前）：

```python
class P1ConstantsTest(unittest.TestCase):
    """P1 地基：v2 常量声明（值精确断言，锚住 P2-P4 桶/间隔契约）。"""

    def test_v2_constants_declared(self) -> None:
        mod = load_proxy_module()
        self.assertEqual(mod.HIST_BUCKETS_MS,
                         (100, 250, 500, 1000, 2000, 5000, 10000, 30000))
        self.assertEqual(mod.PROBE_INTERVAL_S_DEFAULT, 30)
        self.assertEqual(mod.PROBE_TIMEOUT_S, 5)
        self.assertEqual(mod.PROBE_MIN_INTERVAL_S, 10)
        self.assertEqual(mod.PROBE_FAILURE_THRESHOLD, 3)
        self.assertEqual(mod.PROBE_ALERT_DEBOUNCE_S, 300)
        self.assertEqual(
            mod.DAILY_V2_FIELDS,
            mod._DAILY_FIELDS + ("tokens_prompt", "tokens_completion",
                                 "bytes_out", "stream_requests",
                                 "ttfb_sum_ms", "ttfb_count",
                                 "outcome_ok", "outcome_degraded",
                                 "outcome_failed"))
```

### 验证命令

```
# 红（失败）—— 常量未声明
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.P1ConstantsTest.test_v2_constants_declared 2>&1; echo "exit=$?"

# 绿——应用 4 个 Edit 后
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.P1ConstantsTest.test_v2_constants_declared 2>&1; echo "exit=$?"
# 预期: OK, exit=0

# 基线回归
/usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -3; echo "exit=$?"
# 预期: OK (Ran >=128 tests), exit=0
```

---

## Task 2 [tier: B] — `_record_request` 签名 + RECENT_REQUESTS 条目 + `_proxy` rid/host + TTFB 标记

**内容**: 扩展 `_record_request` 签名（7 个新参数）、更新 RECENT_REQUESTS 条目字典、在 `_proxy` 生成 rid/upstream_host、在 `_proxy_relay` 添加 `time.monotonic()` TTFB 标记、向 5 个 `_record_request` 调用点传递 rid/upstream_host/ttfb_ms/stream/outcome。tier B（多锚点机械改动，无设计决策）。

### 生产代码

**Edit 2-1**: `_proxy` 方法 — 生成 rid + upstream_host 并保存到 `self`。

old_string:
```
    def _proxy(self) -> None:
        started = time.time()
        with STATS_LOCK:
            STATS["requests_total"] += 1
            STATS["active"] += 1
        try:
```
new_string:
```
    def _proxy(self) -> None:
        started = time.time()
        with STATS_LOCK:
            STATS["requests_total"] += 1
            STATS["active"] += 1
            rid = "r-%d" % STATS["requests_total"]
            # 一次解析，转发路径全程复用；UPSTREAM_BASE 读侧无锁（
            # _CFG_LOCK 只护写，Python 引用赋值原子 → 读线程看到任一完整旧值）
            upstream_host = urllib.parse.urlparse(UPSTREAM_BASE).netloc
        self._req_id = rid
        self._upstream_host = upstream_host
        try:
```

**Edit 2-2**: `_record_request` 签名扩展（行 680-681）。

old_string:
```
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False) -> None:
```
new_string:
```
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None) -> None:
```

**Edit 2-3**: RECENT_REQUESTS 条目字典（行 718-720）。

old_string:
```
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model})
```
new_string:
```
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model,
                                "rid": rid, "upstream_host": upstream_host,
                                "ttfb_ms": ttfb_ms, "stream": stream,
                                "tokens": tokens, "bytes_out": bytes_out,
                                "outcome": outcome})
```

**Edit 2-4**: `_proxy_relay` — TTFB 标记（连接+响应头到达计时）。

old_string:
```
        header_retried = 0
        header_retry_reason = ""
        try:
            try:
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
```
new_string:
```
        header_retried = 0
        header_retry_reason = ""
        t_conn_start = time.monotonic()  # TTFB 起点：每次 _open_upstream 尝试前重记（重试等待不混入 TTFB）
        try:
            try:
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                t_headers_done = time.monotonic()
```

**Edit 2-5**: 头超时重试路径 — 重新标记。

old_string:
```
                _record_header_retry(model)
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
```
new_string:
```
                _record_header_retry(model)
                t_conn_start = time.monotonic()
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                t_headers_done = time.monotonic()
```

**Edit 2-6**: header 阶段之后计算 ttfb_ms。

old_string:
```
            return

        content_type = (resp.getheader("Content-Type") or "").lower()
```
new_string:
```
            return

        ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
        content_type = (resp.getheader("Content-Type") or "").lower()
```

**Edit 2-7**: 空流重试 — 重新标记。

old_string:
```
                conn.close()
                try:
                    conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                except (OSError, http.client.HTTPException) as retry_exc:
```
new_string:
```
                conn.close()
                t_conn_start = time.monotonic()
                try:
                    conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                    t_headers_done = time.monotonic()
                except (OSError, http.client.HTTPException) as retry_exc:
```

**Edit 2-8**: 空流重试成功后重算 ttfb_ms。

old_string:
```
                self._body_err_line = None
                filtered, truncated = self._relay_sse(resp, final=True)
                body_error = self._body_err_line is not None
            outcome = classify_outcome(status=resp.status, poison_filtered=filtered,
                                       body_error=body_error)
```
new_string:
```
                self._body_err_line = None
                filtered, truncated = self._relay_sse(resp, final=True)
                body_error = self._body_err_line is not None
                ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
            outcome = classify_outcome(status=resp.status, poison_filtered=filtered,
                                       body_error=body_error)
```

**Edit 2-9a**: `_proxy` client-abort 路径 — `_record_request` 传递 rid/upstream_host/outcome。

old_string:
```
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None)
```
new_string:
```
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome.category)
```

**Edit 2-9b**: 502 路径 — `_record_request` 传递 rid/upstream_host/outcome。

old_string:
```
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error)
```
new_string:
```
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome.category)
```

**Edit 2-9c**: 空流重试 502 路径 — `_record_request` 传递 rid/upstream_host/outcome。

old_string:
```
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model,
                                    error=outcome.counts_error)
```
new_string:
```
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model,
                                    error=outcome.counts_error,
                                    rid=self._req_id, upstream_host=self._upstream_host,
                                    outcome=outcome.category)
```

**Edit 2-9d**: SSE 成功路径 — `_record_request` 传递全量 v2 字段。

old_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error)
```
new_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category)
```

**Edit 2-9e**: 非流式路径 — `_record_request` 传递全量 v2 字段。

old_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error)
```
new_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category)
```

### 测试代码

追加到 `ctyun-stream-fix-proxy.test.py` 末尾（`if __name__ == "__main__":` 之前）：

```python
class RequestIdTest(unittest.TestCase):
    """P1 地基：request id / upstream host / ttfb / stream / outcome 全链路。

    黑盒子进程集成：经 CTYUN_UPSTREAM_BASE seam 指向 fake upstream，
    断言 RECENT_REQUESTS 条目携带 v2 字段且值正确。"""

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

    def test_recent_entry_carries_rid_host_ttfb_stream_outcome(self) -> None:
        """RECENT_REQUESTS 条目含 v2 七个新字段；值正确填充。"""
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertTrue(snap["recent"], "RECENT_REQUESTS must not be empty")
        entry = snap["recent"][-1]
        for key in ("rid", "upstream_host", "ttfb_ms", "stream",
                    "tokens", "bytes_out", "outcome"):
            self.assertIn(key, entry,
                          "recent entry must carry '%s' key; got keys %r"
                          % (key, sorted(entry.keys())))
        self.assertEqual(entry["rid"], "r-1",
                         "first request rid must be r-1 (fresh process)")
        self.assertEqual(entry["upstream_host"],
                         "127.0.0.1:%d" % self.upstream_port)
        self.assertIsInstance(entry["ttfb_ms"], float)
        self.assertGreaterEqual(entry["ttfb_ms"], 0)
        self.assertEqual(entry["stream"], 1,
                         "SSE request stream must be 1")
        self.assertEqual(entry["outcome"], "ok",
                         "clean SSE outcome must be ok")
        # P3 前占位字段
        self.assertIsNone(entry["tokens"])
        self.assertEqual(entry["bytes_out"], 0)
```

### 验证命令

```
# 红 — RECENT 条目缺少 rid/upstream_host 等键
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.RequestIdTest.test_recent_entry_carries_rid_host_ttfb_stream_outcome 2>&1; echo "exit=$?"

# 绿 — 应用全部 Edit 2-1..2-9e 后
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.RequestIdTest.test_recent_entry_carries_rid_host_ttfb_stream_outcome 2>&1; echo "exit=$?"
# 预期: OK, exit=0

# 基线回归
/usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -3; echo "exit=$?"
# 预期: OK (Ran >=129 tests), exit=0
```

---

## Task 3 [tier: B] — `_log` REQ 行扩展（新字段 + 调用点 wiring + 既有测试 regex 更新）

**内容**: 扩展 `_log` 签名（rid/upstream_host/ttfb_ms/stream/outcome）、REQ 行格式新增 5 字段、向 5 个 `_log` 调用点传递新参数、更新 2 个既有测试的正则表达式。tier B（机械改动多锚点但无决策）。

### 生产代码

**Edit 3-1**: `_log` 签名 + 格式（行 1171-1182）。

old_string:
```
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
new_string:
```
    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0, retry_reason: str = "", exc=None,
             rid=None, upstream_host=None, ttfb_ms=None, stream=None,
             outcome=None) -> None:
        exc_field = "-"
        if exc is not None:
            exc_field = re.sub(r"\s+", "_",
                               ("%s: %s" % (type(exc).__name__, exc)).strip())[:200]
        ttfb_field = "%.1fms" % ttfb_ms if ttfb_ms is not None else "-"
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d retry_reason=%s exc=%s "
                         "rid=%s host=%s ttfb=%s stream=%s outcome=%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            retry_reason or "-", exc_field,
                            rid or "-", upstream_host or "-", ttfb_field,
                            "-" if stream is None else ("1" if stream else "0"),
                            outcome or "-",
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

**Edit 3-2a**: `_proxy` client-abort — `_log` 传递 rid/upstream_host/outcome。

old_string:
```
            self._log(started, 499, outcome.log_result, 0)
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome.category)
```
new_string:
```
            self._log(started, 499, outcome.log_result, 0,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      outcome=outcome.category)
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome.category)
```

**Edit 3-2b**: 502 路径 — `_log` 传递 rid/upstream_host/outcome。

old_string:
```
            self._log(started, 502, outcome.log_result, 0, model=model, exc=exc,
                      retried=header_retried, retry_reason=header_retry_reason)
```
new_string:
```
            self._log(started, 502, outcome.log_result, 0, model=model, exc=exc,
                      retried=header_retried, retry_reason=header_retry_reason,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      outcome=outcome.category)
```

**Edit 3-2c**: 空流重试 502 — `_log` 传递 rid/upstream_host/outcome。

old_string:
```
                    self._log(started, 502, outcome.log_result, 0, model=model, retried=1,
                              retry_reason=retry_reason, exc=retry_exc)
```
new_string:
```
                    self._log(started, 502, outcome.log_result, 0, model=model, retried=1,
                              retry_reason=retry_reason, exc=retry_exc,
                              rid=self._req_id, upstream_host=self._upstream_host,
                              outcome=outcome.category)
```

**Edit 3-2d**: SSE 成功 — `_log` 传递全量 v2 字段。

old_string:
```
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason)
```
new_string:
```
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category)
```

**Edit 3-2e**: 非流式 — `_log` 传递全量 v2 字段。

old_string:
```
            self._log(started, resp.status, outcome.log_result, 0, model=model)
```
new_string:
```
            self._log(started, resp.status, outcome.log_result, 0, model=model,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category)
```

**Edit 3-3a**: 既有测试 `test_req_log_line_has_ts_and_model`（测试文件行 2256-2268）— 更新正则。

old_string:
```
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"retry_reason=\S+ "
                      r"exc=\S+ "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
```
new_string:
```
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"retry_reason=\S+ "
                      r"exc=\S+ "
                      r"rid=\S+ host=\S+ ttfb=\S+ stream=\d+ outcome=\S+ "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
```

**Edit 3-3b**: 既有测试 `test_req_error_line_carries_exc`（测试文件行 2288-2289）— 更新正则。

old_string:
```
        m = re.search(r"^REQ POST /v1/chat/completions -> 502 dur=\d+\.\ds "
                      r"result=error .*? exc=(\S+)\s+ts=", stderr, re.M)
```
new_string:
```
        m = re.search(r"^REQ POST /v1/chat/completions -> 502 dur=\d+\.\ds "
                      r"result=error .*? exc=(\S+)\s+rid=", stderr, re.M)
```

### 测试代码

`RequestIdTest` 追加以下方法（Task 2 已创建该类，Task 3 追加方法）：

```python
    def test_req_line_carries_rid_host_ttfb_stream_outcome(self) -> None:
        """REQ 行含 rid=r-N / host= / ttfb=<num>ms / stream=1 / outcome=ok。"""
        post_sse(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        m = re.search(
            r"rid=(r-\d+) host=127\.0\.0\.1:%d "
            r"ttfb=\d+\.\dms stream=1 outcome=ok ts=" % self.upstream_port,
            stderr, re.M)
        self.assertIsNotNone(m,
            "REQ 行必须带 rid/host/ttfb/stream/outcome 字段，stderr:\n" + stderr)
        self.assertEqual(m.group(1), "r-1")
```

（注意：Task 2 已创建 `RequestIdTest` 类及 `setUp`/`tearDown`/`test_recent_entry_*` 方法；Task 3 只追加上述方法到同一类。）

### 验证命令

```
# 红 — REQ 行不含 rid= 等新字段
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.RequestIdTest.test_req_line_carries_rid_host_ttfb_stream_outcome 2>&1; echo "exit=$?"

# 绿 — 应用全部 Edit 3-1..3-3b 后
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.RequestIdTest.test_req_line_carries_rid_host_ttfb_stream_outcome 2>&1; echo "exit=$?"
# 预期: OK, exit=0

# 回归 — 确保既有测试全绿（含 test_req_log_line_has_ts_and_model 和 test_req_error_line_carries_exc 的修改版正则）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -3; echo "exit=$?"
# 预期: OK (Ran >=130 tests), exit=0
```

---

## Task 4 [tier: B] — X-Request-Id 响应头（SSE / buffered / 502 三路径）

**内容**: 在 `_send_sse_headers`、`_relay_buffered`、`_reply_502` 三个方法中各追加一行 `self.send_header("X-Request-Id", self._req_id)`；新增 test helpers `post_sse_with_headers` / `get_plain_with_headers` 用于验证响应头。tier B（三处机械追加，单行改动）。

### 生产代码

**Edit 4-1**: `_send_sse_headers`（行 1046-1054）— 在 `Connection: close` 前追加 X-Request-Id。

old_string:
```
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
```
new_string:
```
        self.send_header("X-Request-Id", self._req_id)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
```

**Edit 4-2**: `_relay_buffered`（行 1143-1151）— 在 headers 区追加 X-Request-Id。

old_string:
```
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
```
new_string:
```
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", self._req_id)
        self.end_headers()
```

**Edit 4-3**: `_reply_502`（行 1161-1169）— 在 headers 区追加 X-Request-Id。

old_string:
```
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
```
new_string:
```
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Request-Id", self._req_id)
        self.send_header("Content-Length", str(len(payload)))
```

### 测试代码

**Test helper 1** — 追加到 `post_sse` 函数（行 417-428）之后：

```python
def post_sse_with_headers(port: int, payload: bytes = None):
    """同 post_sse 但返回 (body, x_request_id)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    if payload is None:
        payload = b'{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[]}'
    conn.request("POST", "/v1/chat/completions", body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    status = resp.status
    x_request_id = resp.getheader("X-Request-Id")
    data = resp.read()
    conn.close()
    assert status == 200, "expected SSE 200, got %d" % status
    return data, x_request_id
```

**Test helper 2** — 追加到 `get_plain` 函数（行 431-440）之后：

```python
def get_plain_with_headers(port: int):
    """同 get_plain 但返回 (data, content_length, x_request_id)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/plain")
    resp = conn.getresponse()
    status = resp.status
    data = resp.read()
    resp_len = resp.getheader("Content-Length")
    x_request_id = resp.getheader("X-Request-Id")
    conn.close()
    assert status == 200, "expected /plain 200, got %d" % status
    return data, resp_len, x_request_id
```

**Test methods** — 追加到 `RequestIdTest` 类：

```python
    def test_sse_response_carries_x_request_id_matching_req_line(self) -> None:
        """SSE 响应头 X-Request-Id 与 REQ 行 rid 一致。"""
        _, x_request_id = post_sse_with_headers(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        m = re.search(r"rid=(r-\d+) host=", stderr)
        self.assertIsNotNone(m, "REQ 行必须含 rid=，stderr:\n" + stderr)
        self.assertEqual(x_request_id, m.group(1),
                         "X-Request-Id 响应头必须与 REQ 行 rid 一致")

    def test_plain_response_carries_x_request_id(self) -> None:
        """非流式（/plain GET → _relay_buffered）响应也带 X-Request-Id。"""
        _, _, x_request_id = get_plain_with_headers(self.proxy_port)
        m = re.match(r"^r-\d+$", x_request_id or "")
        self.assertIsNotNone(m,
            "非流式响应必须带 X-Request-Id，got %r" % x_request_id)

    def test_502_response_carries_x_request_id(self) -> None:
        """代理合成 502 响应也带 X-Request-Id。"""
        dead_port = free_port()  # 死端口：连接即 ECONNREFUSED → 合成 502
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(dead_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.proc.proxy_port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     body=b'{"model":"m","stream":true,"messages":[]}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        x_request_id = resp.getheader("X-Request-Id")
        resp.read()
        conn.close()
        self.assertEqual(status, 502,
                         "死上游必须合成 502")
        m = re.match(r"^r-\d+$", x_request_id or "")
        self.assertIsNotNone(m,
            "502 响应必须带 X-Request-Id，got %r" % x_request_id)
```

### 验证命令

```
# 红 — 响应头缺失 X-Request-Id
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.RequestIdTest.test_sse_response_carries_x_request_id_matching_req_line ctyun-stream-fix-proxy.test.RequestIdTest.test_plain_response_carries_x_request_id ctyun-stream-fix-proxy.test.RequestIdTest.test_502_response_carries_x_request_id 2>&1; echo "exit=$?"

# 绿 — 应用 3 个 Edit 后
/usr/bin/python3 -m unittest ctyun-stream-fix-proxy.test.RequestIdTest.test_sse_response_carries_x_request_id_matching_req_line ctyun-stream-fix-proxy.test.RequestIdTest.test_plain_response_carries_x_request_id ctyun-stream-fix-proxy.test.RequestIdTest.test_502_response_carries_x_request_id 2>&1; echo "exit=$?"
# 预期: OK, exit=0

# 基线回归
/usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -3; echo "exit=$?"
# 预期: OK (Ran >=133 tests), exit=0
```

---

## 卡间执行顺序

Task 1 → Task 2 → Task 3 → Task 4（串行；每卡完成后跑基线回归确认无退化）。

## 全卡完成后的最终回归

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
# 预期: OK (Ran >=133 tests), exit=0
```