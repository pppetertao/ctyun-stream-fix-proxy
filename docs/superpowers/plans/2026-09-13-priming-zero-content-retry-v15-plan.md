# PLAN: priming 零内容流判别与 finish-form 重试

- **spec**: `docs/superpowers/specs/2026-09-13-priming-zero-content-retry-v15-design.md`
- **基线**: v1.4, 1453 行 main / 1669 行 test, 54 tests OK
- **解释器**: `/usr/bin/python3` (PATH python3 = Homebrew 3.14, 禁用)
- **卡数**: 2 (单文件项目，改动集中；2 卡拆分：Card 1 = 生产代码全量，Card 2 = 测试代码全量 + 验证)

## Global Constraints

1. **No Placeholders** — 每卡代码块完整、可直接抄进文件；无"参见 spec"/"略"式占位。
2. **TDD 顺序** — Card 2 先写完，此时 Card 1 尚未改动 → 新集成用例全红（finish 被即刻 flush → calls==1 / retry_reason 缺失）；Card 1 改后转绿。Card 2 的既有用例（54 tests）改动前全绿，改动后不回归一字。
3. **单次 dispatch** — 共 2 卡，不分段。主代理按 tier 路由 (both tier A → executor)。
4. **文件命名** — 以 worktree 内 `ctyun-stream-fix-proxy.py` / `ctyun-stream-fix-proxy.test.py` 为准。
5. **不 commit、不建分支、不改 spec、不动 `~/.local/bin/` 运行副本。**

---

## Card 1 (tier A): 生产代码改动 — ctyun-stream-fix-proxy.py

**改动点**: 9 处（对应 spec Files to Change 1-9）

**验证命令**: (Card 2 写完测试后) `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/priming-zero-content-retry && /usr/bin/python3 ctyun-stream-fix-proxy.test.py`

### 1.1 新常量 `PRIMED_TAIL_CAP` (:50 后)

```python
PRIMED_TAIL_CAP = 262144  # finish hold 尾段缓冲上限（超限 fail-open 防内存膨胀）
```

插在 `EMPTY_RETRY_MAX` 定义后、`sse_data_line_kind` 定义前。

### 1.2 `sse_data_line_kind` (:53-84) — finish_reason 从 content 拆出

**完整替换**（旧 :53-84 全量替换）:

```python
def sse_data_line_kind(line: bytes) -> str:
    """SSE data 行归类："done" / "content" / "finish" / "noise"。判定序：
    非 data: 前缀（注释行/event:/id:）→ noise；[DONE] → done；
    JSON 解析失败 → content（fail-open：宁可不重试，不误判合法流）；
    dict + choices 非空 list 时：delta.content 非空 str / delta.tool_calls 真值 →
    content；choice.finish_reason 非 None（且无前两者）→ finish；
    其余（reasoning-only、空 delta、choices 为空的 usage 帧、data:null、非 dict JSON）→ noise。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:"):
        return "noise"
    if DONE_RE.match(stripped):
        return "done"
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return "content"
    if not isinstance(data, dict):
        return "noise"
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "noise"
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta")
    delta = delta if isinstance(delta, dict) else {}
    content = delta.get("content")
    if isinstance(content, str) and content:
        return "content"
    if delta.get("tool_calls"):
        return "content"
    if choice.get("finish_reason") is not None:
        return "finish"
    return "noise"
```

**变更**: line 83 `return "content"` → `return "finish"`；docstring `"content" / "noise"` → `"content" / "finish" / "noise"`；docstring 最后一句尾部加 `choice.finish_reason 非 None（且无前两者）→ finish`。

### 1.3 新函数 `sse_line_has_usage` (:85 后，`_EmptyStream` 前)

```python
def sse_line_has_usage(line: bytes) -> bool:
    """判 SSE data 行是否携带非空 usage 帧（独立 usage 帧 choices=[] 与 ride-on finish
    帧都覆盖）；DONE/非 JSON/非 data 行 → False。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    usage = data.get("usage")
    return isinstance(usage, dict) and bool(usage)
```

### 1.4 `_EmptyStream.__init__` (:87-92) — 加 `reason` 参数

**完整替换**（旧 :87-92）:

```python
class _EmptyStream(Exception):
    """priming EOF 仍无 content/[DONE]：携带 filtered 计数与已滤毒缓冲行。"""
    def __init__(self, filtered: int, lines: list, reason: str = "eof-priming"):
        super().__init__("empty upstream sse stream")
        self.filtered = filtered
        self.lines = lines
        self.reason = reason
```

### 1.5 计数器接线 — `STATS` 加 `finish_retries_total` (5 处)

**A. STATS 字典 (:101-103)**:

```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0, "eof_without_done_total": 0,
         "finish_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {}}
```

**B. `save_stats_counters` counters 元组 (:247-248)**:

```python
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total", "eof_without_done_total",
                                          "finish_retries_total")}
```

**C. `load_stats_counters` key 清单 (:284-285)**:

```python
    for key in ("requests_total", "filtered_total", "errors_total", "empty_retries_total",
                "eof_without_done_total", "finish_retries_total"):
```

**D. `main()` 恢复块 (:1396-1400)**:

```python
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["eof_without_done_total"] = counters["eof_without_done_total"]
        STATS["finish_retries_total"] = counters["finish_retries_total"]
```

旧持久化文件缺 `finish_retries_total` 键 → `load_stats_counters` 返回 0（零迁移，无需额外处理）。

### 1.6 `_record_empty_retry` (:456-480) — 签名加 `reason`，条件累加 `finish_retries_total`

**完整替换**（旧 :456-480）:

```python
def _record_empty_retry(model=None, reason: str = "eof-priming") -> None:
    """空流重试计数：STATS 总量 + 当日桶 + daily_by_model。
    finish-no-usage 形态额外累加 finish_retries_total。
    entry/桶形状必须与 _record_request 同步：dm entry 同为 6 字段（本函数无
    error/status 参数，errors_* 仅保形状不归因）；旧持久化桶经 load_daily_buckets
    的 _DAILY_FIELDS 清洗已补键。形状不同步时 += 直接 KeyError。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["empty_retries_total"] += 1
        if reason == "finish-no-usage":
            STATS["finish_retries_total"] += 1
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["retries"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0})
        bucket["retries"] += 1
        EVENTS.append({"ts": time.time(), "kind": "retry", "model": model, "status": None})
        _stats_dirty = True
```

### 1.7 `_relay_sse` (:655-710) — 核心改造：finish hold + usage 判别

**完整替换**（旧 :655-710 整段。含旧 :708 `return filtered, truncated` 后的 `finally` 与 `_relay_buffered` 开头不动——仅替换 :655-710）:

```python
    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            filtered = 0
            saw_done = False   # 全程（priming+streaming）是否见过 [DONE] record
            truncated = False  # streaming 阶段 EOF 且全程无 done → 上游截断标记
            pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
            poisoned = False  # 当前 record 内是否命中毒行
            primed = []       # priming 阶段已滤毒缓冲的完整 record 行（含终结空行）
            priming = True    # True = 客户端尚未收到任何字节
            finish_hold = False  # finish record 触发 hold：缓冲尾段至 EOF 判 usage
            saw_usage = False    # 整流是否出现过非空 usage 帧
            hold_bytes = 0       # finish hold 期已缓冲的字节数

            def _flush_primed() -> None:
                """补发 SSE 头 + primed 缓冲并 flush，切换出 priming。"""
                self._send_sse_headers(resp)
                self.wfile.write(b"".join(primed))
                self.wfile.flush()

            while True:
                line = resp.readline()
                if line in (b"\n", b"\r\n", b""):
                    # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    saw_done = saw_done or ("done" in kinds)
                    if poisoned:
                        filtered += 1  # 整 record（含终结空行）丢弃，不损伤相邻字节（priming 期不进 primed）
                        _record_poison_preview(b"".join(pending))
                    elif priming:
                        primed.extend(pending)
                        if line:
                            primed.append(line)
                        if not saw_usage:
                            saw_usage = any(sse_line_has_usage(l) for l in pending)
                        if finish_hold:
                            hold_bytes += sum(len(l) for l in pending) + len(line)
                            if "content" in kinds:              # fail-open：finish 后反常 content
                                _flush_primed()
                                priming = False
                                finish_hold = False
                            elif hold_bytes > PRIMED_TAIL_CAP:  # 恶意/异常长尾 fail-open
                                _flush_primed()
                                priming = False
                                finish_hold = False
                            # done/finish/noise（含 usage）：继续缓冲尾段
                        elif "content" in kinds or "done" in kinds:
                            _flush_primed()
                            priming = False       # v1.4 原语义不变
                        elif "finish" in kinds:
                            finish_hold = True    # hold 至 EOF 判 usage
                    else:
                        for buf_line in pending:
                            self.wfile.write(buf_line)
                        if line:
                            self.wfile.write(line)
                        self.wfile.flush()
                    pending = []
                    poisoned = False
                    if line == b"":
                        if priming:  # EOF 仍 priming = 空流（零 record / reasoning-only 断流）
                            if finish_hold:
                                if saw_usage or final:
                                    _flush_primed()   # 合法零内容流（有 usage）或预算已尽 fail-open
                                else:
                                    raise _EmptyStream(filtered, primed,
                                                      reason="finish-no-usage")
                            elif final:
                                _flush_primed()       # v1.4 原语义：switch off / 重试流原样下发
                            else:
                                raise _EmptyStream(filtered, primed)
                        else:
                            truncated = not saw_done  # streaming EOF 无 done = 上游截断
                        break
                else:
                    if POISON_RE.match(line.rstrip(b"\r\n")):
                        poisoned = True
                    pending.append(line)
            return filtered, truncated
        finally:
            self.connection.settimeout(old_timeout)
```

**EOF 判定表（对应 spec）**:

| `finish_hold` | `saw_usage` | `final` | 动作 |
|---|---|---|---|
| T | T | 任意 | `_flush_primed()`（合法零内容流，不重试） |
| T | F | F | `raise _EmptyStream(..., reason="finish-no-usage")` → 既有重试路径 |
| T | F | T | `_flush_primed()`（预算已尽，fail-open 原样下发） |
| F | — | T | `_flush_primed()`（v1.4 原语义：switch off / 重试流） |
| F | — | F | `raise _EmptyStream(..., reason="eof-priming")`（v1.4 原语义） |

### 1.8 `_proxy_relay` (:597-631) — 接线 `retry_reason`、改调用签

**完整替换**（旧 :597-631。只改 SSE 分支 :599-631；`else` 分支 :626-630 不动）:

```python
        content_type = (resp.getheader("Content-Type") or "").lower()
        result = "ok" if resp.status < 400 else "upstream-err"
        if "text/event-stream" in content_type:
            retried = 0
            retry_reason = ""
            try:
                filtered, truncated = self._relay_sse(resp, final=(EMPTY_RETRY_MAX < 1))
            except _EmptyStream as exc:
                filtered = exc.filtered  # attempt-1 已滤毒缓冲随重试丢弃，filtered 只计交付流
                retried = 1
                retry_reason = exc.reason
                _record_empty_retry(model, exc.reason)
                conn.close()
                try:
                    conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                except (OSError, http.client.HTTPException) as retry_exc:
                    self._reply_502(retry_exc)  # 客户端尚未收到字节，502 语义与既有路径一致
                    self._log(started, 502, "error", 0, model=model, retried=1,
                              retry_reason=retry_reason)
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model, error=True)
                    return
                filtered, truncated = self._relay_sse(resp, final=True)
            result = "ok" if resp.status < 400 else "upstream-err"  # 重试后按实际 resp 重算
            if result == "ok" and truncated:
                # 仅覆盖 ok：upstream-err（status≥400 更有信息量）与 aborted（异常
                # 路径不经此处）不误标；priming EOF 由 retries 计数承载，避免双计数
                result = "eof-without-done"
                _record_eof_without_done(model)
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model)
```

### 1.9 `_log` (:739-745) — 签名加 `retry_reason`

**完整替换**（旧 :739-745）:

```python
    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0, retry_reason: str = "") -> None:
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d retry_reason=%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            retry_reason or "-",
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

---

## Card 2 (tier A): 测试代码改动 — ctyun-stream-fix-proxy.test.py

**验证命令**: `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/priming-zero-content-retry && /usr/bin/python3 ctyun-stream-fix-proxy.test.py`

**TDD 红相**: Card 2 写完后、Card 1 改前，运行验证命令——新集成用例全红（finish 流被即刻 flush → calls==1 / retry_reason 字段缺失 / `finish_retries_total` counter key 缺失）。

**TDD 绿相**: Card 1 改完后运行——全绿（新用例 + 既有 54 tests 不回归）。

### 2.1 新 SSE 常量 (:36 后)

```python
SSE_FINISH = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
SSE_FAULT_TAIL = SSE_REASONING + SSE_FINISH + SSE_DONE
```

### 2.2 `FakeUpstreamHandler` (:42-120 类体) — 加 `fault_finish_first` / `fault_finish_stream`

在既有类属性后（:50 后）加两属性：

```python
    fault_finish_first = False   # True → 首呼回 SSE_FAULT_TAIL 后断连，次呼正常 body
    fault_finish_stream = False  # True → 每呼回故障尾段（SSE_FAULT_TAIL）
```

`do_POST` 方法内在 `if self.empty_stream or (self.calls is not None ...` 分支（:67）前插入新分支：

```python
        if self.fault_finish_first:
            if self.calls is not None and len(self.calls) == 1:
                # 首呼：故障尾段（reasoning + finish + [DONE]，无 content/usage）
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(SSE_FAULT_TAIL)
                except ConnectionError:
                    pass
                self.close_connection = True
                return
            # 次呼（len(calls) > 1）fall through 到既有正常路径：
            # body_override 非 None 用 override（用例 B/D），否则默认 SSE_A+SSE_B+SSE_DONE
        if self.fault_finish_stream:
            # 每呼回故障尾段（故意无限重试→第二次 final=True fail-open）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(SSE_FAULT_TAIL)
            except ConnectionError:
                pass
            self.close_connection = True
            return
```

### 2.3 `make_fake_upstream` / `make_scripted_upstream` — 透传新属性

**A. `make_fake_upstream` (:134-147)** — attrs 加 `fault_finish_first=False, fault_finish_stream=False` 两个属性：

完整替换:

```python
def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
                       fail_500: bool = False, empty_stream: bool = False,
                       blank_stream: bool = False, body_override=None,
                       fault_finish_first: bool = False,
                       fault_finish_stream: bool = False,
                       scripted: bool = False) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
             "empty_stream": empty_stream, "blank_stream": blank_stream,
             "body_override": body_override,
             "fault_finish_first": fault_finish_first,
             "fault_finish_stream": fault_finish_stream}
    if scripted:
        attrs["calls"] = []
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,), attrs)
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1]
```

**B. `make_scripted_upstream` (:150-154)** — kwargs 透传新属性：

完整替换:

```python
def make_scripted_upstream(**kwargs) -> tuple:
    """带调用计数的假上游：返回 (port, calls)；calls 按上游被请求次数 append。
    kwargs 透传 empty_stream / blank_stream / body_override /
    fault_finish_first / fault_finish_stream（勿传 scripted/poison）。"""
    port = make_fake_upstream(False, scripted=True, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls
```

### 2.4 `test_sse_data_line_kind_matrix` (:423-446) — :432 断言调整 + 新增 case

替换 :431-433 的三行：

```python
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'), "finish")
```

在该方法末尾（:446 后，新方法前）加两新断言（同帧争用：content/tool_calls 优先于 finish）：

```python
        # content + finish 同帧 → "content"（delta.content 优先）
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\n'),
            "content")
        # tool_calls + finish 同帧 → "content"（delta.tool_calls 优先）
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"tool_calls":[{"id":"t1"}]},'
              b'"finish_reason":"stop"}]}\n'), "content")
```

### 2.5 新单测 `test_sse_line_has_usage_matrix` (:446 后)

```python
    def test_sse_line_has_usage_matrix(self) -> None:
        f = self.mod.sse_line_has_usage
        # 真：独立 usage 帧（choices=[]）
        self.assertTrue(f(b'data: {"id":"u","choices":[],'
                          b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n'))
        # 真：ride-on finish 帧（choices 有 finish + usage 顶层）
        self.assertTrue(f(b'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}],'
                          b'"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n'))
        # 假：空 usage {}
        self.assertFalse(f(b'data: {"id":"e","choices":[],"usage":{}}\n'))
        # 假：usage: null
        self.assertFalse(f(b'data: {"id":"n","choices":[],"usage":null}\n'))
        # 假：[DONE]
        self.assertFalse(f(b"data: [DONE]\n"))
        # 假：非 JSON
        self.assertFalse(f(b"data: {not json\n"))
        # 假：非 data 行
        self.assertFalse(f(b": keep-alive\n"))
        # 假：空 data
        self.assertFalse(f(b"data:\n"))
        # 假：usage 非 dict（如列表）
        self.assertFalse(f(b'data: {"usage":[1,2,3],"choices":[]}\n'))
```

### 2.6 计数器键 `finish_retries_total` 补全（4 处）

**A. `test_stats_persist_roundtrip_and_defaults` :453-455** — load 零值字典加键：

```python
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0, "eof_without_done_total": 0,
                          "finish_retries_total": 0})
```

**B. 同方法 :458-460** — corrupt JSON 零值字典加键：

```python
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0, "eof_without_done_total": 0,
                          "finish_retries_total": 0})
```

**C. `test_stats_snapshot_shape` :488-491** — snapshot 键清单加键：

```python
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "eof_without_done_total",
                    "finish_retries_total",
                    "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews", "events"):
```

**D. `test_dashboard_and_stats_served` :1154-1157** — admin 键清单加键：

```python
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "eof_without_done_total",
                    "finish_retries_total",
                    "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews"):
```

### 2.7 `test_req_log_line_has_ts_and_model` (:1321-1333) — 正则加 `retry_reason=`

完整替换（旧 :1321-1333）:

```python
    def test_req_log_line_has_ts_and_model(self) -> None:
        post_sse(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"retry_reason=\S+ "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
        self.assertIsNotNone(m, "REQ 行必须带 model= / retry_reason= / ts= 字段，stderr:\n" + stderr)
        time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z")  # %z 回析：防平台差异静默退化
```

### 2.8 新集成用例 (7 个，:1649 后追加)

以下用例仿 :1464-1569 scripted 结构，每个独立 `setUp`/`tearDown` 由 `ProxyIntegrationTestCase` 框架提供（`self.proc`、`self.upstream_port` 由 setUp 初始化）。

**用例 A: `test_finish_with_usage_zero_content_legal_no_retry`**

```python
    def test_finish_with_usage_zero_content_legal_no_retry(self) -> None:
        """finish + usage + [DONE] 零内容流：合法，不重试。"""
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_FINISH + SSE_USAGE + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "usage-bearing finish stream must not retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_FINISH + SSE_USAGE + SSE_DONE,
                         "legal zero-content stream must relay byte-exact, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "legal stream must stay result=ok, stderr:\n" + stderr)
```

**用例 B: `test_finish_without_usage_retried_second_stream_relayed`**

```python
    def test_finish_without_usage_retried_second_stream_relayed(self) -> None:
        """finish 无 usage 故障形态：首呼 fault_finish_first 回故障尾段触发重试，
        次呼回正常 content 流全量交付。"""
        upstream_port, calls = make_scripted_upstream(
            fault_finish_first=True, body_override=SSE_A + SSE_B + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "finish fault must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=finish-no-usage", stderr,
                      "REQ line must carry retry_reason=finish-no-usage, stderr:\n" + stderr)
```

**用例 C: `test_double_finish_fault_falls_back_after_two_calls`**

```python
    def test_double_finish_fault_falls_back_after_two_calls(self) -> None:
        """双 finish 故障：fault_finish_stream 每呼回故障尾段 → calls==2 fallback。"""
        upstream_port, calls = make_scripted_upstream(fault_finish_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "retry cap is 1: second fault finish ends the attempt, calls=%d"
                         % len(calls))
        self.assertEqual(data, SSE_FAULT_TAIL,
                         "attempt-2 buffer must be delivered as-is, got %r" % data)
```

**用例 D: `test_finish_fault_counters_and_req_line`**

```python
    def test_finish_fault_counters_and_req_line(self) -> None:
        """finish 故障计数器 + SIGTERM 落盘 + seed resume 续算。"""
        upstream_port, calls = make_scripted_upstream(
            fault_finish_first=True, body_override=SSE_A + SSE_B + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["empty_retries_total"], 1)
        self.assertGreaterEqual(snap["finish_retries_total"], 1,
                                "finish fault must increment finish_retries_total")
        self.assertGreaterEqual(snap["daily"][today]["retries"], 1)
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["retries"], 1)
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=5)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved["finish_retries_total"], 1)
        # seed resume：finish_retries_total 跨重启续算
        upstream_port2, calls2 = make_scripted_upstream(
            fault_finish_first=True, body_override=SSE_A + SSE_B + SSE_DONE)
        self.proc = start_proxy(
            upstream_port2, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % upstream_port2,
                          "stats": {"finish_retries_total": 5}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap2 = json.loads(body.decode("utf-8"))
        self.assertEqual(snap2["finish_retries_total"], 5,
                         "seeded finish_retries_total must resume from persist")
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls2), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["finish_retries_total"], 6)
```

**用例 E: `test_finish_then_content_fails_open_no_retry`**

```python
    def test_finish_then_content_fails_open_no_retry(self) -> None:
        """finish 后反常跟 content：fail-open 即刻 flush，不重试。"""
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_FINISH + SSE_A + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "finish-then-content must fail-open no retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_FINISH + SSE_A + SSE_DONE,
                         "finish-then-content must relay byte-exact, got %r" % data)
```

**用例 F: `test_finish_fault_retry_zero_disables`**

```python
    def test_finish_fault_retry_zero_disables(self) -> None:
        """CTYUN_EMPTY_RETRY=0：finish 故障不重试，原样下发。"""
        upstream_port, calls = make_scripted_upstream(fault_finish_first=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_EMPTY_RETRY": "0"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "CTYUN_EMPTY_RETRY=0 must disable retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_FAULT_TAIL)
```

**用例 G: `test_finish_tail_over_cap_fails_open`**

```python
    def test_finish_tail_over_cap_fails_open(self) -> None:
        """finish 后尾段超 PRIMED_TAIL_CAP(256KB) → fail-open 即刻 flush。"""
        # 构造 >256KB 尾段：11000 条噪音 data 行（26B/行 ≈ 286KB）无空行分隔，
        # 与 [DONE] 行合成单 record 后一次性计入 hold_bytes 超限。
        big_noise = b'data: {"comment":"noise"}\n' * 11000  # 286,000B > 262,144B
        body = SSE_REASONING + SSE_FINISH + big_noise + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "finish tail over cap must fail-open no retry, calls=%d" % len(calls))
        self.assertEqual(len(data), len(body),
                         "fail-open must deliver all bytes, expected %d got %d"
                         % (len(body), len(data)))
```

---

## R31 Evidence

[R31-S1] 问题存在的现场证据：2026-09-13 生产取证，model-io 日志 44 例实锤——上游"reasoning 起步即断流"新形态：流内仅少量 reasoning delta，随后 finish_reason 帧正常到达并带 [DONE] 收尾，但全程零 delta.content、零 tool_calls、零 usage 帧。代理侧 v1.4 对其记 `result=ok retried=0`（finish 被 priming 当首个信号 record 即刻 flush，重试路径不触发），客户端收到"合法但全空"回合即报 stream 错误。判别依据：正常完成的零内容流（如 max_tokens 被推理吃光）必有 usage 帧，故障形态无 usage 帧。

基线验证（改动前 baseline，54 tests OK，TDD 红相基准）：

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
Ran 54 tests in ...
OK
```

spec 5 section + Evidence 自身共 6 个顶格二级标题：

```
$ grep -nE "^## " /Users/peter/Documents/project/ctyun-stream-fix-proxy/docs/superpowers/specs/2026-09-13-priming-zero-content-retry-v15-design.md
3:## Goal
6:## Files to Change
70:## Acceptance Criteria
76:## Risks
83:## Exclusions
89:## R31 Evidence
```

[R31-S2] 根因陈述（带锚点）：`sse_data_line_kind`（ctyun-stream-fix-proxy.py:82-83）把 `choice.finish_reason is not None` 归入 `"content"`；`_relay_sse`（:679）priming 期见 `"content"`/`"done"` 即刻补头 flush → finish record 成为首个信号 record，priming 正常结束、流原样转发（:617-625 记 result=ok），`_EmptyStream` 重试路径（:603）永远不触发；usage 帧被归 `"noise"`（:72-73 choices 为空）无判别作用。修法即 spec 正文：finish 拆独立 kind → hold 至 EOF → `sse_line_has_usage` 判别 → 合法 flush / 故障弃缓冲重试（共享上限 1）。

锚点抽查（sed 实跑，与 Files to Change 及 [R31-S2] 断言逐字一致）：

```
$ sed -n '82p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/priming-zero-content-retry/ctyun-stream-fix-proxy.py
    if choice.get("finish_reason") is not None:
$ sed -n '83p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/priming-zero-content-retry/ctyun-stream-fix-proxy.py
        return "content"
$ sed -n '679p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/priming-zero-content-retry/ctyun-stream-fix-proxy.py
                        if "content" in kinds or "done" in kinds:
$ sed -n '432p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/priming-zero-content-retry/ctyun-stream-fix-proxy.test.py
            f(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'), "content")
```