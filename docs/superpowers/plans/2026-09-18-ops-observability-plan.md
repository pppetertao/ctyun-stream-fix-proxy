# PLAN: 运维可观测性 — 失败留痕 / capture_errors 开关 / 日志 API / 错误分类网关

## Header

- **目标**：给单文件 stdlib 代理 `ctyun-stream-fix-proxy.py` 增加四件运维能力：
  1. 失败请求环形留痕（`GET /api/errors`）
  2. `capture_errors` 运行时开关（走 `/api/config`）
  3. stderr 日志 API 化读取（`GET /api/logs` 游标分页）
  4. 把散落在 `_relay_sse`/`_proxy` 的错误口径收敛为可单测的纯函数分类网关 `classify_outcome`
- **基线实录**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` — **76 tests, exit 0, OK**（28.049s）
- **验证命令**：`cd .worktrees/ops-observability && /usr/bin/python3 ctyun-stream-fix-proxy.test.py`
- **spec**：[2026-09-18-ops-observability-design.md](../specs/2026-09-18-ops-observability-design.md)

## Global Constraints

1. **TDD**：每卡先写失败测试，验证红，再实现至绿。异常：README 更新（卡 3）免测。
2. **统计口径零变化**：`errors_total` 仅 502 合成路径 +1；`eof_without_done_total`/`empty_retries_total`/`errors_upstream` 语义不变。现有 76 测试全绿是硬约束——任一回归即失败。
3. **锁纪律**：`record_error_event` 只取 `ERROR_LOCK`；`set_capture_errors` 锁外调 `save_stats_counters`（:449-451 同线程非重入死锁前例）；`LOG_RING` append 在信号处理器路径仅 `deque.append`+int 递增，无重入风险。
4. **No Placeholders**：卡内代码完整可执行，禁止"参照 spec 实现"等指令替代代码。
5. **Python 3.9 兼容**：只用 namedtuple/deque/现有语法，零新依赖。
6. **持久化文件 v2**：`{"upstream_base", "capture_errors", "stats"}`，旧文件缺键容错为 False。

---

## 任务卡

### Card 1 (Tier A): 基础设施 — 常量、模块态、纯函数、日志镜像

**涉及文件**：
- `ctyun-stream-fix-proxy.py`
- `ctyun-stream-fix-proxy.test.py`

**spec 锚点**：常量与模块态（:48-51/:117-127）、classify_outcome（:101 之后）、empty_stream_should_retry、_snapshot_text、record_error_event（:505 附近）、logs_snapshot、_safe_log_stderr（:282-289）

**生产代码**：

#### 1.1 新增常量（在 `PRIMED_TAIL_CAP` 行之后，`def sse_data_line_kind` 之前插入）

```python
ERROR_RING_MAX = 50
BODY_SNAPSHOT_CAP = 4096
RESPONSE_SNIPPET_CAP = 2048
LOG_RING_MAX = 1000
LOG_TAIL_DEFAULT = 100
LOG_PAGE_MAX = 500

CLASS_OK = "ok"
CLASS_CLIENT_ABORT = "client_abort"
CLASS_REQUEST_FAULT = "request_fault"
CLASS_UPSTREAM_FAULT = "upstream_fault"
CLASS_POISON_FIXED = "poison_fixed"

ERR_KIND_POISON = "poison_hit"
ERR_KIND_EMPTY_RETRY = "empty_retry"
ERR_KIND_EOF_NO_DONE = "eof_without_done"
ERR_KIND_SYNTH_502 = "synth_502"
ERR_KIND_UPSTREAM_5XX = "upstream_5xx"
ERR_KIND_REQUEST_4XX = "request_4xx"

_KIND_CATEGORY = {
    ERR_KIND_POISON: CLASS_POISON_FIXED,
    ERR_KIND_EMPTY_RETRY: CLASS_OK,
    ERR_KIND_EOF_NO_DONE: CLASS_UPSTREAM_FAULT,
    ERR_KIND_SYNTH_502: CLASS_UPSTREAM_FAULT,
    ERR_KIND_UPSTREAM_5XX: CLASS_UPSTREAM_FAULT,
    ERR_KIND_REQUEST_4XX: CLASS_REQUEST_FAULT,
}
```

#### 1.2 新增模块态（在 `EVENTS` 赋值行之后插入）

```python
CAPTURE_ERRORS = False  # _CFG_LOCK 守护
ERROR_EVENTS = collections.deque(maxlen=ERROR_RING_MAX)
ERROR_LOCK = threading.Lock()
_ERROR_EVENT_SEQ = 0  # ERROR_LOCK 内递增

LOG_RING = collections.deque(maxlen=LOG_RING_MAX)
LOG_LOCK = threading.Lock()
_LOG_SEQ = 0  # LOG_LOCK 内递增
```

#### 1.3 新增纯函数（在 `sse_line_has_usage` 函数之后插入）

```python
_Outcome = collections.namedtuple("_Outcome", "category log_result counts_error capture")


def classify_outcome(status=None, synth_502=False, client_abort=False,
                     eof_without_done=False, poison_filtered=0) -> _Outcome:
    """错误分类网关：输入场景标志 → 返回 (category, log_result, counts_error, capture)。

    判定优先级 client_abort > synth_502 > eof_without_done > status 阈值；
    status=None 且无 flags → ValueError。
    """
    if client_abort:
        return _Outcome(CLASS_CLIENT_ABORT, "aborted", False, False)
    if synth_502:
        return _Outcome(CLASS_UPSTREAM_FAULT, "error", True, True)
    if eof_without_done:
        return _Outcome(CLASS_UPSTREAM_FAULT, "eof-without-done", False, True)
    if status is None:
        raise ValueError("classify_outcome: status required when no flag is set")
    if status < 400:
        if poison_filtered > 0:
            return _Outcome(CLASS_POISON_FIXED, "ok", False, True)
        return _Outcome(CLASS_OK, "ok", False, False)
    if 400 <= status < 500:
        return _Outcome(CLASS_REQUEST_FAULT, "upstream-err", False, True)
    # status >= 500
    return _Outcome(CLASS_UPSTREAM_FAULT, "upstream-err", False, True)


def empty_stream_should_retry(budget: int) -> bool:
    """空流重试决策：预算 > 0 时允许重试。调用点 :656 由 final=(EMPTY_RETRY_MAX < 1)
    改为 final=not empty_stream_should_retry(EMPTY_RETRY_MAX)，语义等价。"""
    return budget > 0


def _snapshot_text(raw: bytes, cap: int) -> str:
    """字节快照截断：utf-8 replace 解码，超 cap 截断追加 trunc 标记。"""
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace")
    if len(text) <= cap:
        return text
    return text[:cap] + "\u2026[truncated]"


def record_error_event(kind, model=None, path=None, upstream_status=None, exc=None,
                       body=None, response=None, filtered=0, retried=0,
                       retry_reason="") -> None:
    """记录一条错误留痕事件到 ERROR_EVENTS 环。

    CAPTURE_ERRORS 为 False 时直接返回（off=只走现有计数，不留痕不抓 body）。
    条目 schema：{id, ts, kind, category, model, path, upstream_status,
    exc(<=200ch), body(<=4096ch), response(<=2048ch), filtered, retried,
    retry_reason}；id 进程内单调递增（ERROR_LOCK 内）。"""
    if not CAPTURE_ERRORS:
        return
    global _ERROR_EVENT_SEQ
    exc_text = ""
    if exc is not None:
        exc_text = re.sub(r"\s+", "_",
                          ("%s: %s" % (type(exc).__name__, exc)).strip())[:200]
    with ERROR_LOCK:
        _ERROR_EVENT_SEQ += 1
        ERROR_EVENTS.append({
            "id": _ERROR_EVENT_SEQ,
            "ts": time.time(),
            "kind": kind,
            "category": _KIND_CATEGORY.get(kind),
            "model": model,
            "path": path,
            "upstream_status": upstream_status,
            "exc": exc_text or None,
            "body": _snapshot_text(body, BODY_SNAPSHOT_CAP) if body else None,
            "response": _snapshot_text(response, RESPONSE_SNIPPET_CAP) if response else None,
            "filtered": filtered,
            "retried": retried,
            "retry_reason": retry_reason or None,
        })


def logs_snapshot(cursor=None, tail=None) -> dict:
    """日志分页纯函数：从 LOG_RING 取副本后计算，可单测。

    返回 dict：{"lines": [{"seq": int, "line": str}], "next_cursor": int,
    "oldest_seq": int, "ring_max": 1000}。

    参数校验（非法 → ValueError，先于空环早退，无条件生效）：
    - cursor 与 tail 同给 → ValueError
    - tail 非 int / tail < 1 / tail > LOG_RING_MAX → ValueError
    - cursor 非 int → ValueError
    """
    if cursor is not None and tail is not None:
        raise ValueError("cursor and tail are mutually exclusive")
    if tail is not None:
        if not isinstance(tail, int) or tail < 1 or tail > LOG_RING_MAX:
            raise ValueError("tail must be 1..%d" % LOG_RING_MAX)
    if cursor is not None:
        if not isinstance(cursor, int):
            raise ValueError("cursor must be an integer")
    with LOG_LOCK:
        ring_snap = list(LOG_RING)
    if not ring_snap:
        return {"lines": [], "next_cursor": 0, "oldest_seq": 0, "ring_max": LOG_RING_MAX}

    oldest_seq = ring_snap[0]["seq"]
    if tail is not None:
        lines = ring_snap[-tail:]
        return {"lines": lines, "next_cursor": lines[-1]["seq"] if lines else 0,
                "oldest_seq": oldest_seq, "ring_max": LOG_RING_MAX}
    if cursor is not None:
        # cursor=C 返回 seq>C 最多 LOG_PAGE_MAX 条 oldest->newest
        page = [l for l in ring_snap if l["seq"] > cursor][:LOG_PAGE_MAX]
        next_cursor = page[-1]["seq"] if page else cursor
        return {"lines": page, "next_cursor": next_cursor,
                "oldest_seq": oldest_seq, "ring_max": LOG_RING_MAX}
    # 缺省 tail=LOG_TAIL_DEFAULT
    lines = ring_snap[-LOG_TAIL_DEFAULT:]
    return {"lines": lines, "next_cursor": lines[-1]["seq"] if lines else 0,
            "oldest_seq": oldest_seq, "ring_max": LOG_RING_MAX}
```

#### 1.4 修改 `_safe_log_stderr`（在原函数体内 `print` 调用之后加 LOG_RING append）

将原函数：
```python
def _safe_log_stderr(msg: str) -> None:
    # 吞掉的是 stderr 写失败的一切 OSError ...
    try:
        print(msg, file=sys.stderr, flush=True)
    except OSError:
        pass
```

改为：
```python
def _safe_log_stderr(msg: str) -> None:
    # 吞掉的是 stderr 写失败的一切 OSError（BrokenPipeError=管道对端关闭、ENOSPC=磁盘满等）：
    # stderr 是 best-effort 诊断出口，无备用通道，且失败不得传播——传播会炸请求线程，
    # 并使 SIGTERM handler 的 except 分支二次抛出、阻断 SystemExit 退出路径。
    try:
        print(msg, file=sys.stderr, flush=True)
    except OSError:
        pass
    global _LOG_SEQ
    with LOG_LOCK:
        _LOG_SEQ += 1
        LOG_RING.append({"seq": _LOG_SEQ, "line": msg})
```

**测试代码**（追加到 `ProxyDashboardUnitTest` 类末尾，`test_kill_registered_progressive_and_idempotent` 之后）：

```python
    def test_classify_outcome_full_matrix(self) -> None:
        mod = self.mod
        f = mod.classify_outcome

        # client_abort
        o = f(client_abort=True)
        self.assertEqual(o.category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(o.log_result, "aborted")
        self.assertFalse(o.counts_error)
        self.assertFalse(o.capture)

        # synth_502
        o = f(synth_502=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "error")
        self.assertTrue(o.counts_error)
        self.assertTrue(o.capture)

        # eof_without_done
        o = f(eof_without_done=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "eof-without-done")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # status < 400, no poison
        o = f(status=200)
        self.assertEqual(o.category, mod.CLASS_OK)
        self.assertEqual(o.log_result, "ok")
        self.assertFalse(o.counts_error)
        self.assertFalse(o.capture)

        # status < 400, poison_filtered > 0
        o = f(status=200, poison_filtered=1)
        self.assertEqual(o.category, mod.CLASS_POISON_FIXED)
        self.assertEqual(o.log_result, "ok")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # 400 <= status < 500
        o = f(status=404)
        self.assertEqual(o.category, mod.CLASS_REQUEST_FAULT)
        self.assertEqual(o.log_result, "upstream-err")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # status >= 500
        o = f(status=502)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "upstream-err")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # priority: client_abort > synth_502 (flags 优先级验证)
        o = f(client_abort=True, synth_502=True)
        self.assertEqual(o.category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(o.log_result, "aborted")

        # priority: synth_502 > eof_without_done
        o = f(synth_502=True, eof_without_done=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "error")
        self.assertTrue(o.counts_error)

        # ValueError: status=None and no flags
        with self.assertRaises(ValueError):
            f()
        with self.assertRaises(ValueError):
            f(status=None)

    def test_empty_stream_should_retry(self) -> None:
        f = self.mod.empty_stream_should_retry
        self.assertTrue(f(1))
        self.assertTrue(f(10))
        self.assertFalse(f(0))
        self.assertFalse(f(-1))
        # 等价性验证：final = not f(budget) 必须与直接比较一致
        self.assertEqual(not f(0), not 0 > 0)
        self.assertEqual(not f(1), not 1 > 0)

    def test_snapshot_text_normal_and_truncation(self) -> None:
        f = self.mod._snapshot_text
        self.assertEqual(f(b"hello", 100), "hello")
        self.assertEqual(f(b"", 10), "")
        self.assertEqual(f(None, 10), "")
        long_bytes = b"x" * 5000
        result = f(long_bytes, 100)
        self.assertLessEqual(len(result), 100 + len("\u2026[truncated]"))
        self.assertTrue(result.endswith("[truncated]"))
        # 恰等于 cap 时不截断
        exact = b"a" * 50
        self.assertEqual(f(exact, 50), "a" * 50)
        # unicode 替换字符
        invalid = b"\xff\xfe"
        self.assertIn("\ufffd", f(invalid, 50))

    def test_record_error_event_off_no_capture(self) -> None:
        mod = self.mod
        orig_cap = mod.CAPTURE_ERRORS
        orig_events = mod.ERROR_EVENTS
        orig_seq = mod._ERROR_EVENT_SEQ
        mod.ERROR_EVENTS = collections.deque(maxlen=mod.ERROR_RING_MAX)
        mod._ERROR_EVENT_SEQ = 0
        try:
            mod.CAPTURE_ERRORS = False
            mod.record_error_event(mod.ERR_KIND_SYNTH_502, model="m", exc=ValueError("boom"))
            self.assertEqual(len(mod.ERROR_EVENTS), 0,
                             "CAPTURE_ERRORS=False must not add events")
            self.assertEqual(mod._ERROR_EVENT_SEQ, 0,
                             "seq must not advance when capture is off")
            mod.CAPTURE_ERRORS = True
            mod.record_error_event(mod.ERR_KIND_SYNTH_502, model="m", exc=ValueError("boom"))
            self.assertEqual(len(mod.ERROR_EVENTS), 1)
            self.assertEqual(mod._ERROR_EVENT_SEQ, 1)
            ev = mod.ERROR_EVENTS[0]
            self.assertEqual(ev["kind"], mod.ERR_KIND_SYNTH_502)
            self.assertEqual(ev["model"], "m")
            self.assertIsInstance(ev["id"], int)
            self.assertIsInstance(ev["ts"], float)
            self.assertIn("ValueError", ev["exc"])
        finally:
            mod.CAPTURE_ERRORS = orig_cap
            mod.ERROR_EVENTS = orig_events
            mod._ERROR_EVENT_SEQ = orig_seq

    def test_record_error_event_ring_eviction_and_id_monotonic(self) -> None:
        mod = self.mod
        orig_cap = mod.CAPTURE_ERRORS
        orig_events = mod.ERROR_EVENTS
        orig_seq = mod._ERROR_EVENT_SEQ
        mod.ERROR_EVENTS = collections.deque(maxlen=mod.ERROR_RING_MAX)
        mod._ERROR_EVENT_SEQ = 0
        try:
            mod.CAPTURE_ERRORS = True
            cap = mod.ERROR_RING_MAX
            for i in range(cap + 10):
                mod.record_error_event(mod.ERR_KIND_SYNTH_502)
            self.assertEqual(len(mod.ERROR_EVENTS), cap,
                             "ring must cap at ERROR_RING_MAX")
            self.assertEqual(mod.ERROR_EVENTS[-1]["id"], cap + 10,
                             "id must stay monotonic across eviction")
            # 最老 10 条已被淘汰
            self.assertGreater(mod.ERROR_EVENTS[0]["id"], 10)
        finally:
            mod.CAPTURE_ERRORS = orig_cap
            mod.ERROR_EVENTS = orig_events
            mod._ERROR_EVENT_SEQ = orig_seq

    def test_record_error_event_body_response_snapshot(self) -> None:
        mod = self.mod
        orig_cap = mod.CAPTURE_ERRORS
        orig_events = mod.ERROR_EVENTS
        orig_seq = mod._ERROR_EVENT_SEQ
        mod.ERROR_EVENTS = collections.deque(maxlen=mod.ERROR_RING_MAX)
        mod._ERROR_EVENT_SEQ = 0
        try:
            mod.CAPTURE_ERRORS = True
            body = b"x" * 5000
            resp = b"y" * 3000
            mod.record_error_event(
                mod.ERR_KIND_UPSTREAM_5XX, model="m",
                upstream_status=500, body=body, response=resp,
                filtered=3, retried=1, retry_reason="test")
            ev = mod.ERROR_EVENTS[0]
            self.assertLessEqual(len(ev["body"]), mod.BODY_SNAPSHOT_CAP + len("\u2026[truncated]"))
            self.assertLessEqual(len(ev["response"]), mod.RESPONSE_SNIPPET_CAP + len("\u2026[truncated]"))
            self.assertTrue(ev["body"].endswith("[truncated]"))
            self.assertTrue(ev["response"].endswith("[truncated]"))
            self.assertEqual(ev["filtered"], 3)
            self.assertEqual(ev["retried"], 1)
            self.assertEqual(ev["retry_reason"], "test")
            self.assertEqual(ev["upstream_status"], 500)
        finally:
            mod.CAPTURE_ERRORS = orig_cap
            mod.ERROR_EVENTS = orig_events
            mod._ERROR_EVENT_SEQ = orig_seq

    def test_logs_snapshot_empty_ring(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(maxlen=mod.LOG_RING_MAX)
        try:
            snap = mod.logs_snapshot()
            self.assertEqual(snap["lines"], [])
            self.assertEqual(snap["next_cursor"], 0)
            self.assertEqual(snap["oldest_seq"], 0)
            self.assertEqual(snap["ring_max"], mod.LOG_RING_MAX)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_tail_default_and_custom(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(
            [{"seq": i + 1, "line": "msg-%d" % (i + 1)} for i in range(200)],
            maxlen=mod.LOG_RING_MAX)
        try:
            # 缺省 tail=100
            snap = mod.logs_snapshot()
            self.assertEqual(len(snap["lines"]), 100)
            self.assertEqual(snap["lines"][0]["seq"], 101)
            self.assertEqual(snap["lines"][-1]["seq"], 200)
            self.assertEqual(snap["next_cursor"], 200)
            self.assertEqual(snap["oldest_seq"], 1)

            # tail=20
            snap = mod.logs_snapshot(tail=20)
            self.assertEqual(len(snap["lines"]), 20)
            self.assertEqual(snap["lines"][-1]["seq"], 200)
            self.assertEqual(snap["next_cursor"], 200)

            # tail 边界：1 和 LOG_RING_MAX
            snap = mod.logs_snapshot(tail=1)
            self.assertEqual(len(snap["lines"]), 1)
            snap = mod.logs_snapshot(tail=mod.LOG_RING_MAX)
            self.assertEqual(len(snap["lines"]), 200)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_cursor_incremental(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(
            [{"seq": i + 1, "line": "msg-%d" % (i + 1)} for i in range(50)],
            maxlen=mod.LOG_RING_MAX)
        try:
            # cursor 在中间
            snap = mod.logs_snapshot(cursor=20)
            lines = snap["lines"]
            self.assertEqual(len(lines), 30)  # seq 21..50
            self.assertEqual(lines[0]["seq"], 21)
            self.assertEqual(lines[-1]["seq"], 50)
            self.assertEqual(snap["next_cursor"], 50)
            self.assertEqual(snap["oldest_seq"], 1)

            # cursor 在最新 → 空结果
            snap = mod.logs_snapshot(cursor=50)
            self.assertEqual(snap["lines"], [])
            self.assertEqual(snap["next_cursor"], 50)

            # cursor 在最新之后（未来 cursor）
            snap = mod.logs_snapshot(cursor=99)
            self.assertEqual(snap["lines"], [])
            self.assertEqual(snap["next_cursor"], 99)

            # cursor 在 oldest 之前（淘汰可检测）
            snap = mod.logs_snapshot(cursor=0)
            self.assertEqual(len(snap["lines"]), 50)
            self.assertEqual(snap["oldest_seq"], 1)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_cursor_page_limit(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(
            [{"seq": i + 1, "line": "msg-%d" % (i + 1)} for i in range(800)],
            maxlen=mod.LOG_RING_MAX)
        try:
            snap = mod.logs_snapshot(cursor=0)
            # page 上限 LOG_PAGE_MAX=500
            self.assertLessEqual(len(snap["lines"]), mod.LOG_PAGE_MAX)
            self.assertEqual(snap["lines"][0]["seq"], 1)
            self.assertEqual(snap["lines"][-1]["seq"], mod.LOG_PAGE_MAX)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_invalid_params(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        # 子测试 1：空环状态下非法参数仍抛 ValueError（参数校验先于空环早退）
        mod.LOG_RING = collections.deque(maxlen=mod.LOG_RING_MAX)
        try:
            # cursor 和 tail 同给
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor=1, tail=10)
            # tail 越界
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=0)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=mod.LOG_RING_MAX + 1)
            # cursor 非 int
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor="abc")
            # tail 非 int
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail="abc")
        finally:
            mod.LOG_RING = orig_ring
        # 子测试 2：非空环状态下同样非法参数也抛 ValueError（防回归掩盖）
        mod.LOG_RING = collections.deque(
            [{"seq": 1, "line": "x"}], maxlen=mod.LOG_RING_MAX)
        try:
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor=1, tail=10)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=0)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=mod.LOG_RING_MAX + 1)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor="abc")
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail="abc")
        finally:
            mod.LOG_RING = orig_ring

    def test_safe_log_stderr_writes_to_log_ring(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        orig_seq = mod._LOG_SEQ
        mod.LOG_RING = collections.deque(maxlen=mod.LOG_RING_MAX)
        mod._LOG_SEQ = 0
        try:
            mod._safe_log_stderr("hello-ring")
            self.assertEqual(len(mod.LOG_RING), 1)
            self.assertEqual(mod.LOG_RING[0]["line"], "hello-ring")
            self.assertEqual(mod.LOG_RING[0]["seq"], 1)
            mod._safe_log_stderr("msg-2")
            self.assertEqual(len(mod.LOG_RING), 2)
            self.assertEqual(mod.LOG_RING[1]["seq"], 2)
        finally:
            mod.LOG_RING = orig_ring
            mod._LOG_SEQ = orig_seq
```

**验证命令**：
```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/ops-observability && /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest 2>&1
```

--- 

### Card 2 (Tier A): 集成 — 分类器接入、留痕落点、config 通道、新端点

**涉及文件**：
- `ctyun-stream-fix-proxy.py`
- `ctyun-stream-fix-proxy.test.py`

**spec 锚点**：_proxy_relay :651-687 行为敏感区、留痕落点 6 处（:642-648/:665-671/:657-664/:674-678/:679-687/_proxy :610-618）、persist_upstream :160-167、load_capture_errors、set_capture_errors、save_stats_counters :292-315、main() :1477-1494、AdminHandler.do_GET :839-852、AdminHandler.do_POST :854-883

**生产代码**：

#### 2.1 修改 `persist_upstream`（加 `capture_errors` 参数与键）

将原函数签名改为：
```python
def persist_upstream(base: str, path: str, capture_errors: bool = False) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base, "capture_errors": capture_errors},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)
```

#### 2.2 新增 `load_capture_errors`（在 `load_stats_counters` 之后）

```python
def load_capture_errors(path: str) -> bool:
    """从持久化文件读 capture_errors；缺/损坏/非 bool → False。"""
    data = _load_persist_file(path)
    val = data.get("capture_errors") if isinstance(data, dict) else None
    return val if isinstance(val, bool) else False
```

#### 2.3 新增 `set_capture_errors`（在 `set_upstream_base` 之后）

```python
def set_capture_errors(enabled: bool) -> None:
    global CAPTURE_ERRORS
    with _CFG_LOCK:
        CAPTURE_ERRORS = enabled
        persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=enabled)
    # save_stats_counters 内部也要拿 _CFG_LOCK：必须在锁外调用（同 :449-451 死锁注释）
    save_stats_counters(PERSIST_PATH)
```

#### 2.4 修改 `save_stats_counters`（v2 持久化：加 `capture_errors` 键）

在 `save_stats_counters` 函数中，`with _CFG_LOCK:` 块内读取 `CAPTURE_ERRORS` 并写入：

将：
```python
    with _CFG_LOCK:
        base = UPSTREAM_BASE
```

改为：
```python
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
```

将 `json.dump` 的第一个参数改为：
```python
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
                  fh, ensure_ascii=False)
```

#### 2.5 修改 `main()`（启动时加载 `capture_errors`）

将 `main()` 的 global 声明改为：

```python
def main() -> None:
    global UPSTREAM_BASE, _upstream_source, _stats_dirty, CAPTURE_ERRORS
```

在 `events = load_stats_events(PERSIST_PATH)` 之后、`with STATS_LOCK:` 之前插入：

```python
    CAPTURE_ERRORS = load_capture_errors(PERSIST_PATH)  # 启动时回填开关
```

（`CAPTURE_ERRORS` 已加入函数顶部的 global 声明，无需再内部声明。）

#### 2.6 修改 `_proxy_relay`：分类器接入 + 留痕落点 6 处

**注意**：删除原 :651 的 `result = "ok" if resp.status < 400 else "upstream-err"` 行——buffered 分支改由 classify_outcome 供给；SSE 分支由重试后 classifier 重算。保留 `content_type = ...` 与 `if "text/event-stream" in content_type:` 不变。

**A. 首呼 open 失败 (:642-648)** — 替换 `_log` + `_record_request` 附近：

将：
```python
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            self._log(started, 502, "error", 0, model=model, exc=exc)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=True)
            return
```

改为：
```python
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            outcome = classify_outcome(synth_502=True)
            self._log(started, 502, outcome.log_result, 0, model=model, exc=exc)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error)
            record_error_event(ERR_KIND_SYNTH_502, model=model, path=self.path,
                               exc=exc, body=body)
            return
```

**B. `_EmptyStream` 捕获处 (:657-664)** — 保留现有逻辑，只在 `_record_empty_retry` 之后增加留痕：

在 `_record_empty_retry(model, exc.reason)` 之后、`conn.close()` 之前插入：
```python
                record_error_event(ERR_KIND_EMPTY_RETRY, model=model, path=self.path,
                                   response=b"".join(exc.lines),
                                   retry_reason=exc.reason)
```

**C. 重试 open 失败 (:665-671)** — 替换 `_log` + `_record_request` 附近：

将：
```python
                except (OSError, http.client.HTTPException) as retry_exc:
                    self._reply_502(retry_exc)
                    self._log(started, 502, "error", 0, model=model, retried=1,
                              retry_reason=retry_reason, exc=retry_exc)
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model, error=True)
                    return
```

改为：
```python
                except (OSError, http.client.HTTPException) as retry_exc:
                    self._reply_502(retry_exc)
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

**D. 修改 `_relay_sse` 调用处的 `final=` 参数 (:656)** — 替换 `EMPTY_RETRY_MAX < 1` 为 `empty_stream_should_retry`：

将：
```python
                filtered, truncated = self._relay_sse(resp, final=(EMPTY_RETRY_MAX < 1))
```

改为：
```python
                filtered, truncated = self._relay_sse(resp,
                    final=not empty_stream_should_retry(EMPTY_RETRY_MAX))
```

**E. 终态 (:673-678 三元与 eof 标记)** — 替换重试后的 `result` 三元 + eof 标记段：

将：
```python
            result = "ok" if resp.status < 400 else "upstream-err"  # 重试后按实际 resp 重算
            if result == "ok" and truncated:
                # 仅覆盖 ok：upstream-err（status≥400 更有信息量）与 aborted（异常
                # 路径不经此处）不误标；priming EOF 由 retries 计数承载，避免双计数
                result = "eof-without-done"
                _record_eof_without_done(model)
```

改为：
```python
            outcome = classify_outcome(status=resp.status, poison_filtered=filtered)
            result = outcome.log_result  # 重试后按实际 resp 重算
            if truncated and result == "ok":
                # 仅覆盖 ok：upstream-err（status≥400 更有信息量）与 aborted（异常
                # 路径不经此处）不误标；priming EOF 由 retries 计数承载，避免双计数
                result = "eof-without-done"
                outcome = classify_outcome(eof_without_done=True)
                _record_eof_without_done(model)
            if outcome.capture:
                if outcome.category == CLASS_POISON_FIXED:
                    kind = ERR_KIND_POISON
                elif result == "eof-without-done":
                    kind = ERR_KIND_EOF_NO_DONE
                elif resp.status >= 500:
                    kind = ERR_KIND_UPSTREAM_5XX
                else:
                    kind = ERR_KIND_REQUEST_4XX
                record_error_event(kind, model=model, path=self.path,
                                   upstream_status=(resp.status
                                                    if resp.status >= 400 else None),
                                   body=body, filtered=filtered)
```

**F. 终态 log+record_request (:679-687)** — 替换 SSE 路径的 `_log` + `_record_request`：

将：
```python
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model)
```

改为：
```python
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error)
```

替换 buffered 分支 (:683-687)：

将：
```python
        else:
            self._relay_buffered(resp)
            self._log(started, resp.status, result, 0, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model)
```

改为：
```python
        else:
            relayed_data = self._relay_buffered(resp)
            outcome = classify_outcome(status=resp.status)
            self._log(started, resp.status, outcome.log_result, 0, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error)
            if outcome.capture:
                record_error_event(
                    ERR_KIND_UPSTREAM_5XX if resp.status >= 500 else ERR_KIND_REQUEST_4XX,
                    model=model, path=self.path,
                    upstream_status=resp.status if resp.status >= 400 else None,
                    body=body,
                    response=relayed_data[:RESPONSE_SNIPPET_CAP]
                    if resp.status >= 500 else None)
```

**补充：修改 `_relay_buffered` 使其返回 data（供 buffered 分支留痕使用）**

````python
    def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
        data = resp.read()
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            if data:
                self.wfile.write(data)
        finally:
            self.connection.settimeout(old_timeout)
        return data
````

**补充：启动 banner 导入日志环（`_safe_log_stderr` 替代 `print` to stderr）**

`main()` 中两处直接 `print(... file=sys.stderr, ...)` 改为 `_safe_log_stderr(...)`（使日志 API 镜像完整覆盖 start-up 信息）：

```python
# :1511-1513 admin bind failed
        _safe_log_stderr("ctyun-stream-fix-proxy: admin bind failed on %s:%d: %s — exit 1，"
                         "交由 launchd KeepAlive 重试" % (ADMIN_HOST, ADMIN_PORT, exc))

# :1529-1531 启动 banner
        _safe_log_stderr("ctyun-stream-fix-proxy listening on %s:%d -> %s (admin dashboard on %s:%d)"
                         % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE, ADMIN_HOST, ADMIN_PORT))
```

（`flush_stats_if_dirty` :431 的诊断 print 也改为 `_safe_log_stderr` 以使日志环覆盖完整 stderr：）
```python
        _safe_log_stderr("ctyun-stream-fix-proxy: stats flush failed: %s" % exc)
```

**G. _proxy 499 路径 (:610-618)** — 替换 `_log` 的 result 参数：

将：
```python
            self._log(started, 499, "aborted", 0)
```

改为：
```python
            outcome = classify_outcome(client_abort=True)
            self._log(started, 499, outcome.log_result, 0)
```

#### 2.7 修改 `AdminHandler.do_GET`（加路由 `/api/errors` 和 `/api/logs`）

在现有路由 if/elif 链 `elif path == "/api/config":` 与最后的 `else:` 之间插入：

```python
        elif path == "/api/errors":
            id_str = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query).get("id", [None])[0]
            if id_str is not None:
                # 单条详情（含 body/response），需鉴权
                try:
                    eid = int(id_str)
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "id must be an integer"})
                    return
                if not write_allowed(self.client_address[0],
                                     self.headers.get("X-Admin-Token") or "",
                                     os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                    self._send_json(403, {"error": "detail requires X-Admin-Token"})
                    return
                with ERROR_LOCK:
                    match = None
                    for ev in ERROR_EVENTS:
                        if ev["id"] == eid:
                            match = dict(ev)
                            break
                if match is None:
                    self._send_json(404, {"error": "event not found"})
                else:
                    self._send_json(200, match)
            else:
                # 列表（不含 body/response），无鉴权
                with ERROR_LOCK:
                    events = [{"id": e["id"], "ts": e["ts"], "kind": e["kind"],
                               "category": e["category"], "model": e["model"],
                               "path": e["path"],
                               "upstream_status": e["upstream_status"],
                               "exc": e["exc"], "filtered": e["filtered"],
                               "retried": e["retried"],
                               "retry_reason": e["retry_reason"]}
                              for e in ERROR_EVENTS]
                events.reverse()  # newest-first（锁外反转：events 已是新 list）
                with _CFG_LOCK:
                    cap_enabled = CAPTURE_ERRORS
                self._send_json(200, {"capture_errors": cap_enabled,
                                      "count": len(events),
                                      "events": events})
        elif path == "/api/logs":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            cursor = qs.get("cursor", [None])[0]
            tail = qs.get("tail", [None])[0]
            if cursor is not None:
                try:
                    cursor = int(cursor)
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "cursor must be an integer"})
                    return
            if tail is not None:
                try:
                    tail = int(tail)
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "tail must be an integer"})
                    return
            try:
                snap = logs_snapshot(cursor=cursor, tail=tail)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, snap)
```

#### 2.8 修改 `AdminHandler.do_GET /api/config`（加 `capture_errors` 字段）

将：
```python
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source}
            self._send_json(200, payload)
```

改为：
```python
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS}
            self._send_json(200, payload)
```

#### 2.9 修改 `AdminHandler.do_POST`（支持 `capture_errors` 键）

在 `base = data.get("upstream_base") ...` 判断之后、调用 `set_upstream_base` 之前增加 `capture_errors` 处理。将整个处理逻辑改为：

将：
```python
        base = data.get("upstream_base") if isinstance(data, dict) else None
        if not isinstance(base, str) or not valid_upstream_url(base):
            self._send_json(400, {"error": "upstream_base 需为 "
                                           "http(s)://host[:port]/path 形式的合法 URL"})
            return
        set_upstream_base(base)
        self._send_json(200, {"ok": True, "upstream_base": base, "source": "api"})
```

改为：
```python
        base = data.get("upstream_base") if isinstance(data, dict) else None
        cap = data.get("capture_errors") if isinstance(data, dict) else None  # 可选键
        if cap is not None and not isinstance(cap, bool):
            self._send_json(400, {"error": "capture_errors must be a boolean"})
            return
        # upstream_base 缺失/为 None 时允许单独 POST capture_errors；给出但非法仍 400。
        if base is not None and (not isinstance(base, str) or not valid_upstream_url(base)):
            self._send_json(400, {"error": "upstream_base 需为 "
                                           "http(s)://host[:port]/path 形式的合法 URL"})
            return
        if base is not None:
            set_upstream_base(base)
        if cap is not None:
            set_capture_errors(cap)
        with _CFG_LOCK:
            resp = {"ok": True, "upstream_base": UPSTREAM_BASE, "source": "api",
                    "capture_errors": CAPTURE_ERRORS}
        self._send_json(200, resp)
```

#### 2.10 修改 `set_upstream_base` 中的 `persist_upstream` 调用

`persist_upstream` 签名已变，调用处需传 `capture_errors`。将：

```python
        persist_upstream(base, PERSIST_PATH)
```

改为：

```python
        persist_upstream(base, PERSIST_PATH, capture_errors=CAPTURE_ERRORS)
```

**测试代码**（追加到 `AdminIntegrationTest` 类末尾）：

**前置：修改 `admin_get` 辅助函数**（加可选 headers 参数以测 token，spec 明确要求）：

将：
```python
def admin_get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
    resp = conn.getresponse()
    out = (resp.status, resp.read(), resp.getheader("Content-Type"))
    conn.close()
    return out
```

改为：
```python
def admin_get(port: int, path: str, headers: dict = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {}
    if headers:
        hdrs.update(headers)
    conn.request("GET", path, headers=hdrs)
    resp = conn.getresponse()
    out = (resp.status, resp.read(), resp.getheader("Content-Type"))
    conn.close()
    return out
```

---

```python
    def test_errors_endpoint_default_off(self) -> None:
        """默认 CAPTURE_ERRORS=False：/api/errors 返回 capture_errors:false + 空列表。"""
        status, body, ctype = admin_get(self.proc.admin_port, "/api/errors")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"))
        data = json.loads(body.decode("utf-8"))
        self.assertFalse(data["capture_errors"])
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["events"], [])
        # 列表项不应含 body/response
        for ev in data["events"]:
            self.assertNotIn("body", ev)
            self.assertNotIn("response", ev)

    def test_capture_errors_post_get_roundtrip(self) -> None:
        """POST capture_errors:true → GET /api/config 回读 → 持久化文件含键。"""
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("capture_errors"))

        # GET /api/config 回读
        status, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertTrue(cfg["capture_errors"])

        # GET /api/errors 报告 capture_errors:true
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["capture_errors"])

        # 持久化文件含 capture_errors
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertTrue(saved.get("capture_errors"))

    def test_capture_errors_bad_input(self) -> None:
        """capture_errors 非 bool → 400。"""
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                        "capture_errors": "yes"}).encode("utf-8"))
        self.assertEqual(status, 400)

    def test_capture_errors_alone_post(self) -> None:
        """单独 POST capture_errors（无 upstream_base）应 200，并维持上游。"""
        original_base = "http://127.0.0.1:%d" % self.upstream_port
        # 单独 POST capture_errors:true
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"capture_errors": True}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("capture_errors"))
        self.assertEqual(resp.get("upstream_base"), original_base)
        # GET /api/config 回读一致
        status, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertTrue(cfg["capture_errors"])
        self.assertEqual(cfg["upstream_base"], original_base)
        # 再单独 POST capture_errors:false
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"capture_errors": False}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertFalse(resp.get("capture_errors"))
        self.assertEqual(resp.get("upstream_base"), original_base)

    def test_errors_endpoint_kind_upstream_5xx(self) -> None:
        """上游 500 + capture_errors on → /api/errors kind=upstream_5xx 且 ?id= 含 response。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        bad_port = make_fake_upstream(False, fail_500=True)
        self.proc = start_proxy(bad_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % bad_port,
                        "capture_errors": True}).encode("utf-8"))
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m-500"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 500)
        resp.read()
        conn.close()
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["capture_errors"])
        self.assertGreaterEqual(data["count"], 1)
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("upstream_5xx", kinds)
        # 详情含 response 快照（newest-first → events[0] 为最新 500 事件）
        eid = data["events"][0]["id"]
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=%d" % eid)
        self.assertEqual(status, 200)
        ev = json.loads(body.decode("utf-8"))
        self.assertEqual(ev["kind"], "upstream_5xx")
        self.assertIn("response", ev)
        self.assertIsNotNone(ev["response"])

    def test_errors_detail_by_id_and_bad_params(self) -> None:
        """?id= 详情（含 body/response/exc）；404/400 分支。"""
        dead_port = free_port()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        self.proc = start_proxy(dead_port, free_port(),
                                extra_env={"CTYUN_ADMIN_TOKEN": "sekret"})
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % dead_port,
                        "capture_errors": True}).encode("utf-8"))
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502)
        resp.read()
        conn.close()
        # ?id=1（本机 + 带 token 头）→ 200 含 body/exc
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=1",
                                    headers={"X-Admin-Token": "sekret"})
        self.assertEqual(status, 200)
        ev = json.loads(body.decode("utf-8"))
        self.assertEqual(ev["id"], 1)
        self.assertEqual(ev["kind"], "synth_502")
        self.assertIn("body", ev)
        self.assertIsNotNone(ev["body"])
        self.assertIn("exc", ev)
        self.assertIsNotNone(ev["exc"])
        # ?id=9999 → 404
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=9999",
                                    headers={"X-Admin-Token": "sekret"})
        self.assertEqual(status, 404)
        # ?id=abc → 400
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=abc")
        self.assertEqual(status, 400)
        # 列表不含 body/response 键
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        for event in data["events"]:
            self.assertNotIn("body", event)
            self.assertNotIn("response", event)

    def test_errors_poison_hit(self) -> None:
        """poison=True 上游 + capture_errors on → kind=poison_hit。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        poison_port = make_fake_upstream(True)
        self.proc = start_proxy(poison_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % poison_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("poison_hit", kinds)

    def test_errors_eof_without_done(self) -> None:
        """截断流 + capture_errors on → kind=eof_without_done。"""
        # spec 测试列表写 "fault_finish_stream → eof_without_done"，但按 relay 代码实测
        # fault_finish_stream（reasoning+finish+DONE 尾段）attempt-2 final=True 走
        # _flush_primed fail-open，产出 EMPTY_RETRY(finish-no-usage) 而非 eof 标记；
        # eof_without_done 的真实签名是"有 content 无 [DONE] 即 EOF"（与既有
        # test_eof_without_done_marked_counted_and_persisted 同场景），故本测试用
        # body_override=SSE_A+SSE_B 复刻。五种 kind 的集成覆盖不受影响。
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream(body_override=SSE_A + SSE_B)
        self.proc = start_proxy(upstream_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("eof_without_done", kinds)

    def test_errors_empty_retry(self) -> None:
        """空流重试 + capture_errors on → kind=empty_retry。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream()
        self.proc = start_proxy(upstream_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("empty_retry", kinds)

    def test_capture_errors_restart_roundtrip(self) -> None:
        """capture_errors 经 POST→GET→重启（seed_persist）roundtrip 保持。"""
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        # 重启并 seed 持久化文件
        persist_path = os.path.join(self.proc.persist_dir, "settings.json")
        with open(persist_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist=saved)
        status, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertTrue(cfg["capture_errors"],
                        "capture_errors must survive restart via seed_persist")

    def test_logs_endpoint_tail(self) -> None:
        """GET /api/logs?tail=20 返回 <=20 行。"""
        # 先发一个请求确保 stderr 有内容
        post_sse(self.proxy_port)
        status, body, ctype = admin_get(self.proc.admin_port, "/api/logs?tail=20")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"))
        data = json.loads(body.decode("utf-8"))
        self.assertLessEqual(len(data["lines"]), 20)
        self.assertIn("lines", data)
        self.assertIn("next_cursor", data)
        self.assertIn("oldest_seq", data)
        self.assertIn("ring_max", data)
        for line in data["lines"]:
            self.assertIn("seq", line)
            self.assertIn("line", line)
            self.assertIsInstance(line["seq"], int)

    def test_logs_endpoint_cursor_incremental(self) -> None:
        """cursor 增量拉取至 lines=[]。"""
        post_sse(self.proxy_port)
        # 先取 tail 获取 cursor
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=10")
        data = json.loads(body.decode("utf-8"))
        cursor = data["next_cursor"]
        # cursor 增量拉取
        status, body, _ = admin_get(
            self.proc.admin_port, "/api/logs?cursor=%d" % cursor)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(data["lines"]), 0,
                         "cursor at latest should return empty lines")
        self.assertEqual(data["next_cursor"], cursor)

    def test_logs_endpoint_invalid_params(self) -> None:
        """cursor+tail 同给 / 非 int → 400。"""
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?cursor=1&tail=10")
        self.assertEqual(status, 400)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?cursor=abc")
        self.assertEqual(status, 400)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=0")
        self.assertEqual(status, 400)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=2000")
        self.assertEqual(status, 400)

    def test_logs_line_contains_req(self) -> None:
        """日志行含 "REQ POST" 等请求记录。"""
        post_sse(self.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=50")
        data = json.loads(body.decode("utf-8"))
        lines_text = " ".join(l["line"] for l in data["lines"])
        self.assertIn("REQ POST", lines_text)

    def test_classifier_stats_regression_errors_total(self) -> None:
        """统计口径回归：errors_total 仅 502 合成路径 +1；现有断言不变。"""
        # 正常 SSE 请求不应增 errors_total
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        before = json.loads(body.decode("utf-8"))["errors_total"]
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        after = json.loads(body.decode("utf-8"))["errors_total"]
        self.assertEqual(after, before,
                         "normal SSE must not increment errors_total")
        # 502 合成错误应 +1
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        dead_port = free_port()
        self.proc = start_proxy(dead_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502)
        resp.read()
        conn.close()
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["errors_total"], 1)
```

**验证命令**：
```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/ops-observability && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1
```

---

### Card 3 (Tier A): README 更新

**涉及文件**：
- `README.md`

**spec 锚点**：README.md :57 API 区补 /api/errors、/api/logs、capture_errors 与鉴权口径；:69 用例数更新（76 → 95+）；环境变量表 :45-54 零新增。

**生产代码**（README 文本变更，非可执行代码）：

在 API 区（现有 `/api/stats`、`/api/config` 文档之后）追加：

```markdown
- `GET /api/errors` — 错误留痕列表（newest-first，不含 body/response）。无鉴权（敏感度与 `/api/stats` 同级）。默认 off（`count:0`、`events:[]`）；关闭开关只停止新增，已留痕事件保留至环自然淘汰，期间 `?id=` 详情仍可读取。
- `GET /api/errors?id=N` — 单条错误事件详情（含 body/response 快照，≤4096/≤2048 字符）。**需鉴权**：本机（127.0.0.1/::1）放行，非本机需 `X-Admin-Token` 头（与 `POST /api/config` 同一 HMAC 比对）。id 不存在返回 404。
- `POST /api/config` — 现支持可选 `capture_errors` 布尔键：`{"upstream_base":"...","capture_errors":true}`。仅 upstream_base 必填，capture_errors 可选；可单独 POST 开关。
- `GET /api/logs` — stderr 日志镜像（内存环，重启即清）。缺省返回最近 100 行；`?tail=N`（1..1000）；`?cursor=C` 分页（返回 seq>C 最多 500 条，oldest->newest）；cursor 与 tail 互斥。无鉴权（行内容=method/path/status/model/异常文本，与 /api/stats 同级）。

capture_errors 开关：
- 默认 `false`。通过 `POST /api/config` 开启（`{"capture_errors":true}`），`GET /api/config` 可回读。
- off 时错误计数照常但不留痕不抓 body（杜绝 prompt 默认入内存）。
- 持久化于 `~/.local/etc/ctyun-stream-fix-proxy.json` v2（新键 `capture_errors`），跨重启保留。
- /api/errors 详情含用户 prompt 走 LAN 明文 HTTP + token 头，不建议跨不可信网段使用。
```

将用例数从 `54` 更新为 `103`（现有 baseline 76 + Card 1 单元 12 + Card 2 集成 15；**以全量绿后 unittest 输出 `Ran N tests` 为准**）：

找到 `README.md` 中 `54` 用例数的位置（:69），改为 `103`。

**验证命令**：
```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/ops-observability && grep -q "/api/errors" README.md && grep -q "/api/logs" README.md && grep -q "capture_errors" README.md && echo "README OK"
```

---

## Self-Review Checklist

1. **spec coverage** — 四件能力全卡覆盖：失败留痕（Card 1 record_error_event + Card 2 6 处落点）、capture_errors 开关（Card 2 persist/load/set + main()）、日志 API（Card 1 logs_snapshot + _safe_log_stderr 修改 + Card 2 /api/logs 路由）、分类网关（Card 1 classify_outcome/empty_stream_should_retry + Card 2 _proxy_relay 接入）。spec 锚点 28 处全对应。spec 测试列表 "fault_finish_stream → eof_without_done" 与 relay 实测行为不一致（attempt-2 final=True 走 fail-open 不触发 eof）；eof 集成测试改用 body_override=SSE_A+SSE_B（与既有 test_eof_without_done_marked_counted_and_persisted 同签名）。
2. **placeholder scan** — 零占位符。所有卡内代码均为完整 Python/JSON/Markdown。`_KIND_CATEGORY` 字典消解了 category 字段的 None 占位；buffered 分支的 data 重读已改为 `_relay_buffered` 返回值；`'outcome' in dir()` 已清除。
3. **type consistency** — Python 3.9 兼容（namedtuple/deque/标准库）；函数签名与 spec 一致（classify_outcome 8 行映射表逐字段锁定 category/log_result/counts_error/capture）；模块态变量类型明确（bool/deque/Lock/int）；`admin_get` 头参数签名 `headers: dict = None` 符合 spec 要求。
4. **可落盘性** — 验证命令均为 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（Card 2 全量 103 例；Card 1 `ProxyDashboardUnitTest` 单测类过滤，12 新例）；落盘路径 worktree 内；README verify 用 grep 检查关键词。
5. **锚点实测** — baseline 76 tests/exit 0 已实录 Header（2026-09-18 实测 `Ran 76 tests in 28.049s OK exit 0`）；Card 2 的 `_proxy_relay` 插入行号基于当前代码实测（:642-687 删除 :651 行 + 6 处落点均与当前代码匹配）；启动 banner :1529 与 :1511 两处 `print(file=sys.stderr)` 锚点确认需替换为 `_safe_log_stderr`。