# PLAN: header-stall 短超时

## Header

- **目标**: 上游连接+响应头阶段短超时（默认 45s），超时自动重试一次，仍失败快速 502；body/流式阶段逐读 600s 不变。
- **baseline 实测**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` — Ran 103 tests in 39.011s — OK（exit 0）
- **验证命令**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`
- **分支**: fix/header-stall-timeout（worktree `.worktrees/header-stall-timeout/`）
- **spec**: `docs/superpowers/specs/2026-09-18-header-stall-timeout-design.md`

## Global Constraints

### TDD
- 先写失败测试 → 实现 → 测试通过。每卡 brief 自带测试命令，实现者同次 dispatch 内完成验证并贴实际 stdout/exit code。

### 统计口径零变化
- `classify_outcome` 既有的 7 行映射不可动（5 个 if-branch + raise ValueError）；既有的 6 个 error kind（poison/empty_retry/eof_no_done/synth_502/upstream_5xx/request_4xx）+ 对应 `_KIND_CATEGORY` 条目不动。新增 `ERR_KIND_HEADER_TIMEOUT` 为纯增量键，不改变既有分类逻辑。

### socket.timeout / RemoteDisconnected 的 except 顺序是硬约束
- Python 3.9+ 中 `socket.timeout` 是 `OSError` 的子类；`http.client.RemoteDisconnected` 同时 ⊂ `OSError` 与 `http.client.HTTPException`。内层 `except (socket.timeout, http.client.RemoteDisconnected)` 必须排在 `except (OSError, http.client.HTTPException)` 之前，否则被父类吞掉直接 502 不重试。spec 的嵌套 try（内层 catch 两异常，外层 catch (OSError, HTTPException)）已保证顺序；实现时不可把内层异常挪到外层或与 OSError 合并。
- 两异常同为"响应头阶段未收到任何响应字节"（实测 stall 上游读 body 后关连接，代理侧 `getresponse()` 抛 RemoteDisconnected 而非 socket.timeout）：priming 不可见论证同样成立，重试安全。kind/retry_reason 保持 `header_timeout`/"header-timeout"（阶段命名，不按异常命名）。

### No Placeholders
- 卡内代码全部写死在生产代码块与测试代码块中，不使用"参照 spec 行号实现"或"按 spec 描述添加"等指令式占位。

## 任务卡

- **卡数**: 2
- **卡 1**: 生产代码 + README（tier A）
- **卡 2**: 测试代码（tier A）

---

### 卡 1: 生产代码 + README（tier A）

**文件**: `ctyun-stream-fix-proxy.py`（生产代码）, `README.md`

**spec 锚点**: design.md :12-16（HEADER_TIMEOUT_S/HEADER_RETRY_MAX）、:17（ERR_KIND_HEADER_TIMEOUT+KIND_CATEGORY）、:18（header_timeout_should_retry）、:19-20（_open_upstream 分段）、:21-33（_proxy_relay 嵌套 try）、:34（retried 变量替换）、:35-37（_record_header_retry + 计数器六处 init + _DAILY_FIELDS + save/load）、README :45-46

#### 生产代码变更 (`ctyun-stream-fix-proxy.py`)

**1. 新增 HEADER_TIMEOUT_S（:38 后）**

在 `UPSTREAM_TIMEOUT = 600` 后添加：

```python
HEADER_TIMEOUT_S = max(1.0, float(os.environ.get("CTYUN_HEADER_TIMEOUT", "45")))
```

**2. 新增 HEADER_RETRY_MAX（:50 后）**

在 `EMPTY_RETRY_MAX = int(...)` 后添加：

```python
HEADER_RETRY_MAX = max(0, int(os.environ.get("CTYUN_HEADER_RETRY", "1")))
```

**3. 新增错误种类常量（ERR_KIND_POISON 等块末）**

在 `ERR_KIND_REQUEST_4XX = "request_4xx"` 后添加：

```python
ERR_KIND_HEADER_TIMEOUT = "header_timeout"
```

**4. _KIND_CATEGORY 新增映射（:79 行后）**

在 `_KIND_CATEGORY` dict 的 `ERR_KIND_REQUEST_4XX: CLASS_REQUEST_FAULT,` 后、`}` 前添加：

```python
    ERR_KIND_HEADER_TIMEOUT: CLASS_UPSTREAM_FAULT,
```

**5. 新增 header_timeout_should_retry 函数（empty_stream_should_retry 旁）**

```python
def header_timeout_should_retry(budget: int) -> bool:
    """头超时重试决策：budget > 0 时允许重试。调用点 :822（_proxy_relay 首呼 socket.timeout catch）。"""
    return budget > 0
```

**6. _open_upstream 分段超时（:908-918）**

将现有 `_open_upstream` 中的两个 `timeout=UPSTREAM_TIMEOUT` 改为 `timeout=HEADER_TIMEOUT_S`，并在 `getresponse()` 后 `return` 前插入 `sock.settimeout(UPSTREAM_TIMEOUT)`：

```python
    def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):
        parsed = urllib.parse.urlparse(UPSTREAM_BASE)
        upstream_path = parsed.path.rstrip("/") + path
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        conn.request(method, upstream_path, body=body, headers=fwd_headers)
        resp = conn.getresponse()
        if conn.sock is not None:
            conn.sock.settimeout(UPSTREAM_TIMEOUT)
        return conn, resp
```

**7. _proxy_relay 首呼嵌套 try（:821-832 替换）**

替换现有的单层 `try/except (OSError, http.client.HTTPException)` 为嵌套 try：

```python
        header_retried = 0
        header_retry_reason = ""
        try:
            try:
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
            except socket.timeout as first_exc:
                if not header_timeout_should_retry(HEADER_RETRY_MAX):
                    raise
                header_retried = 1
                header_retry_reason = "header-timeout"
                record_error_event(ERR_KIND_HEADER_TIMEOUT, model=model, path=self.path,
                                   exc=first_exc, body=body, retry_reason="header-timeout")
                _record_header_retry(model)
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
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

**8. SSE 分支 retried 初始值替换（:836）**

将 `retried = 0` 改为 `retried = header_retried`：

```python
            retried = header_retried
```

**9. 空流重试 retried 替换（:843）**

将 `retried = 1` 改为 `retried = header_retried + 1`：

```python
                retried = header_retried + 1
```

**10. _log 签名已支持 retried/retry_reason 参数**

现有 `_log` 方法签名 `:1041-1042` 已含 `retried: int = 0, retry_reason: str = ""`，无需改动。卡 1 中对 `_log` 的调用新增 `retried=header_retried, retry_reason=header_retry_reason` 参数（已在第 7 步含入）。

**11. 新增 _record_header_retry 函数（_record_eof_without_done 旁，:715-737 镜像）**

```python
def _record_header_retry(model=None) -> None:
    """头超时重试计数：STATS 总量 + 当日桶 + daily_by_model。
    逐行镜像 _record_eof_without_done（entry/桶形状同步），仅去掉 EVENTS 追加——
    头重试信号由计数器 + 错误留痕环承载（spec Exclusions：事件流不扩展）。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["header_retries_total"] += 1
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0}
            if entry_dm is not None:
                entry_dm["header_retries"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "header_retries": 0})
        bucket["header_retries"] += 1
        _stats_dirty = True
```

**12. STATS 初始值加 header_retries_total（:268-270）**

在 `STATS` dict 中添加 `"header_retries_total": 0`：

```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0, "eof_without_done_total": 0,
         "finish_retries_total": 0, "header_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {}}
```

**13. 六处 entry/桶 init 加 "header_retries": 0**

每处在既有 `"eof_without_done": 0` 后加 `"header_retries": 0`：

- `_record_request` dm entry init（:644-648）：
```python
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0}
```

- `_record_request` daily 桶 init（:656-659）：
```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "header_retries": 0})
```

- `_record_empty_retry` dm entry init（:700-703）：
```python
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0}
```

- `_record_empty_retry` daily 桶 init（:706-709）：
```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "header_retries": 0})
```

- `_record_eof_without_done` dm entry init（:725-729）：
```python
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0}
```

- `_record_eof_without_done` daily 桶 init（:732-736）：
```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "header_retries": 0})
```

**14. _DAILY_FIELDS 加 "header_retries"（:512-513）**

```python
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream",
                 "retries", "eof_without_done", "header_retries")
```

**15. save_stats_counters counters 元组加 key（:458-460）**

```python
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total", "eof_without_done_total",
                                          "finish_retries_total", "header_retries_total")}
```

**16. load_stats_counters 循环同加（:498-499）**

```python
    for key in ("requests_total", "filtered_total", "errors_total", "empty_retries_total",
                "eof_without_done_total", "finish_retries_total", "header_retries_total"):
```

**17. main() 启动回填加一行（:1780-1785 后）**

在 `STATS["finish_retries_total"] = counters["finish_retries_total"]` 后添加：

```python
        STATS["header_retries_total"] = counters["header_retries_total"]
```

#### README 变更 (`README.md`)

**18. Env 表加两行（:49 后）**

在 `CTYUN_SEND_TIMEOUT` 行后添加：

```
| `CTYUN_HEADER_TIMEOUT` | `45` | 头阶段（连接+响应头）超时（秒），最小值 1 |
| `CTYUN_HEADER_RETRY` | `1` | 头超时自动重试次数（0 禁用） |
```

**19. 架构段行为清单加一条（:27 后）**

在 `- 统计每 60s 脏刷 ...` 行后或行为清单合适位置添加：

```
- 头阶段（连接+请求发送+响应头读取）默认 45s 短超时、超时自动重试一次（重试对客户端不可见）；响应头到达后恢复长超时 600s 逐读 body/流式阶段
```

#### 验证命令

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

卡 1 提交后：baseline 测试应仍全绿（新增统计键不影响既有测试，新错误种类 entry 致远测试未覆盖）。注意：baseline 已经在 Header 证实 103 tests OK。

---

### 卡 2: 测试代码（tier A）

**文件**: `ctyun-stream-fix-proxy.test.py`

**spec 锚点**: design.md :41-42（FakeUpstreamHandler stall 类属性 + ThreadingHTTPServer + 8 组测试）

#### 变更 1: FakeUpstreamHandler 新增 stall 类属性

在 `FakeUpstreamHandler` 类属性区（:47-58）添加：

```python
    stall_all = False       # True → 每呼读 body 后不写任何响应直接返回（触发 socket.timeout）
    stall_calls = ()        # 1-based 呼叫序号元组：命中则 stall（不写响应不关连接）
```

同时修改 `do_POST` 方法，在 `length = int(self.headers.get("Content-Length") or 0)` 读取 body 后（原 :61-65 段之后），插入 stall 逻辑。calls append 必须挪到 stall 检查**之前**，保证 `calls` 计上游被请求总次数（含 stall 呼），与既有 FakeUpstreamHandler 每呼必 append 的口径一致：

```python
        # --- calls 计上游被请求总次数（含 stall 呼）：append 提前到 stall 检查之前 ---
        if self.calls is not None:
            self.calls.append(1)
        # --- stall 路径：读 body 后静默返回不写响应（触发代理 socket.timeout）---
        if self.stall_all:
            # 每呼均 stall：读 body 后直接返回，不写任何响应不关连接
            self.close_connection = True
            return
        if self.stall_calls and self.calls is not None:
            call_num = len(self.calls)  # 当前呼叫序号（1-based，append 之后 len 即为序号）
            if call_num in self.stall_calls:
                self.close_connection = True
                return
        # --- end stall ---
```

注意：stall 命中分支必须用 `self.close_connection = True` 而非 `return` 前的常规 close——stall handler 不写任何响应字节，代理侧 `getresponse()` 将等待响应头直到 `HEADER_TIMEOUT_S` 超时。`calls` 计上游被请求总次数（含 stall 呼），让断言（如 5a 的 calls==2、5b 的 calls==2、5c 的 calls==1、5e compound 的 calls==3）与"每次上游被代理请求"一一对应。

#### 变更 2: make_fake_upstream 支持 ThreadingHTTPServer（对 stall 变体）

修改 `make_fake_upstream` 函数（:169-187），对 stall 相关的构造使用 `ThreadingHTTPServer` 替代 `HTTPServer`（因为 stall handler 在单线程 HTTPServer 上会阻塞 `serve_forever`，导致 `shutdown()` 挂死测试）：

```python
def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
                       fail_500: bool = False, empty_stream: bool = False,
                       blank_stream: bool = False, body_override=None,
                       fault_finish_first: bool = False,
                       fault_finish_stream: bool = False,
                       scripted: bool = False, record_bodies: bool = False,
                       stall_all: bool = False, stall_calls: tuple = ()) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
             "empty_stream": empty_stream, "blank_stream": blank_stream,
             "body_override": body_override,
             "fault_finish_first": fault_finish_first,
             "fault_finish_stream": fault_finish_stream,
             "stall_all": stall_all,
             "stall_calls": stall_calls,
             "bodies": [] if record_bodies else None}
    if scripted:
        attrs["calls"] = []
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,), attrs)
    # stall 变体使用 ThreadingHTTPServer：单线程 HTTPServer 的 serve_forever 会卡死
    # 在 stall 连接上，shutdown 挂测试；daemon_threads=True 保证 teardown 不阻塞
    use_threading = stall_all or stall_calls
    if use_threading:
        from http.server import ThreadingHTTPServer
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
    else:
        server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1]
```

注意：`from http.server import ThreadingHTTPServer` 导入已存在于文件顶部 `from http.server import BaseHTTPRequestHandler, HTTPServer` 行，需改为：

```python
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
```

#### 变更 3: 新增 make_stall_upstream 辅助函数

```python
def make_stall_upstream(stall_all: bool = False, stall_calls: tuple = (), **kwargs) -> tuple:
    """带调用计数的 stall 假上游：返回 (port, calls)。
    stall_all/stall_calls 控制哪些呼叫不写响应直接返回。
    其余 kwargs 透传 empty_stream / body_override 等。"""
    port = make_fake_upstream(False, scripted=True,
                              stall_all=stall_all, stall_calls=stall_calls, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls
```

#### 变更 4: 新增测试用例（ProxyDashboardUnitTest 类内）

**测试 4a: header_timeout_should_retry 真值表**

```python
    def test_header_timeout_should_retry(self) -> None:
        f = self.mod.header_timeout_should_retry
        self.assertTrue(f(1))
        self.assertTrue(f(10))
        self.assertFalse(f(0))
        self.assertFalse(f(-1))
        self.assertEqual(not f(0), not 0 > 0)
        self.assertEqual(not f(1), not 1 > 0)
```

**测试 4b: _KIND_CATEGORY 映射 + classify_outcome 矩阵回归**

```python
    def test_header_timeout_kind_category_and_classify_regression(self) -> None:
        mod = self.mod
        # 新 kind 映射正确
        self.assertEqual(mod._KIND_CATEGORY.get(mod.ERR_KIND_HEADER_TIMEOUT),
                         mod.CLASS_UPSTREAM_FAULT,
                         "header_timeout must be classified as upstream_fault")
        # 既有的 6 个 kind 映射不变（逐条锁死）
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_POISON], mod.CLASS_POISON_FIXED)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_EMPTY_RETRY], mod.CLASS_OK)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_EOF_NO_DONE], mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_SYNTH_502], mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_UPSTREAM_5XX], mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_REQUEST_4XX], mod.CLASS_REQUEST_FAULT)
        # classify_outcome 矩阵不受影响（关键路径逐条锁死）
        self.assertEqual(mod.classify_outcome(client_abort=True).category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(mod.classify_outcome(synth_502=True).category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod.classify_outcome(eof_without_done=True).category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod.classify_outcome(status=200).category, mod.CLASS_OK)
        self.assertEqual(mod.classify_outcome(status=200, poison_filtered=1).category, mod.CLASS_POISON_FIXED)
        self.assertEqual(mod.classify_outcome(status=404).category, mod.CLASS_REQUEST_FAULT)
        self.assertEqual(mod.classify_outcome(status=502).category, mod.CLASS_UPSTREAM_FAULT)
```

**测试 4c: 白盒分段测试——_open_upstream 超时切换**

```python
    def test_open_upstream_header_timeout_then_body_long_timeout(self) -> None:
        """白盒：patch 假上游，调 _open_upstream 直连，断言 getresponse 后 sock timeout 已恢复 UPSTREAM_TIMEOUT。"""
        mod = self.mod
        import types
        # 构造一个简单限定的 request handler：先存下调用参数，再构造假响应
        saved_port = free_port()
        # 用真实假上游验证：正常上游无 stall，直调 _open_upstream
        upstream_port = make_fake_upstream(False)
        try:
            # Patch UPSTREAM_BASE 指向假上游
            orig_base = mod.UPSTREAM_BASE
            mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % upstream_port
            try:
                # 临时设短超时
                orig_ht = mod.HEADER_TIMEOUT_S
                mod.HEADER_TIMEOUT_S = 0.5
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    conn, resp = mod.ProxyHandler._open_upstream(
                        types.SimpleNamespace(), "POST", "/v1/chat/completions",
                        body, {"Content-Type": "application/json"})
                    # 断言 getresponse 后 sock timeout 已恢复 UPSTREAM_TIMEOUT
                    self.assertIsNotNone(conn.sock)
                    self.assertEqual(conn.sock.gettimeout(), mod.UPSTREAM_TIMEOUT,
                                     "after getresponse, sock timeout must be UPSTREAM_TIMEOUT (body phase)")
                    resp.read()
                    conn.close()
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()

    def test_open_upstream_stall_raises_socket_timeout(self) -> None:
        """白盒：stall 上游 → _open_upstream 在 HEADER_TIMEOUT_S 内抛 socket.timeout。"""
        mod = self.mod
        import types
        upstream_port = make_fake_upstream(False, stall_all=True)
        try:
            orig_base = mod.UPSTREAM_BASE
            mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % upstream_port
            try:
                orig_ht = mod.HEADER_TIMEOUT_S
                mod.HEADER_TIMEOUT_S = 0.5
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    with self.assertRaises(socket.timeout):
                        mod.ProxyHandler._open_upstream(
                            types.SimpleNamespace(), "POST", "/v1/chat/completions",
                            body, {"Content-Type": "application/json"})
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()
```

#### 变更 5: 新增集成测试用例（AdminIntegrationTest 类内）

**测试 5a: stall 首呼重试成功——calls==2 + 完整流 + header_timeout 留痕**

```python
    def test_header_stall_retry_success(self) -> None:
        """stall_calls=(1,) → 首呼 stall 触发 header-timeout 重试 → 次呼正常 → calls==2 + SSE 完整。"""
        upstream_port, calls = make_stall_upstream(stall_calls=(1,))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "header stall must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        # header_timeout 留痕事件
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("header-timeout", stderr,
                      "REQ line must carry retry_reason=header-timeout, stderr:\n" + stderr)
        # /api/errors 含 kind=header_timeout（需先开启 capture_errors）
        # 检查计数器
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved.get("header_retries_total", 0), 1,
                                "header_retries_total must be >=1 after stall retry")
```

**测试 5b: 双 stall（stall_all）→ 重试失败 → 502**

```python
    def test_header_stall_both_timeout_returns_502(self) -> None:
        """stall_all → 首呼+重试均超时 → 502 + calls==2 + synth_502 留痕含 retried=1。"""
        upstream_port, calls = make_stall_upstream(stall_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        body = b'{"model":"m","stream":true,"messages":[]}'
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 502, "double stall must synthesize 502, got %d" % status)
        self.assertEqual(len(calls), 2,
                         "stall_all must trigger exactly one retry then fail, calls=%d" % len(calls))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "502 REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=header-timeout", stderr,
                      "502 REQ line must carry retry_reason=header-timeout, stderr:\n" + stderr)
```

**测试 5c: CTYUN_HEADER_RETRY=0 禁重试 → stall_all 快速 502 + calls==1**

```python
    def test_header_stall_retry_disabled(self) -> None:
        """CTYUN_HEADER_RETRY=0 + stall_all → 502 + calls==1（不重试）。"""
        upstream_port, calls = make_stall_upstream(stall_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1",
                                          "CTYUN_HEADER_RETRY": "0"})
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        body = b'{"model":"m","stream":true,"messages":[]}'
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502,
                         "stall_all with retry disabled must return 502, got %d" % resp.status)
        resp.read()
        conn.close()
        self.assertEqual(len(calls), 1,
                         "CTYUN_HEADER_RETRY=0 must disable retry, calls=%d" % len(calls))
```

**测试 5d: 体阶段回归——滴流间隔 > HEADER_TIMEOUT 仍完整收流**

```python
    def test_header_timeout_does_not_leak_into_body_phase(self) -> None:
        """滴流上游（chunk 间隔 1.5s > HEADER_TIMEOUT_S=1）→ 完整收流含 [DONE]。
        证明 getresponse 后的 settimeout(UPSTREAM_TIMEOUT) 生效，短超时未漏进体阶段。"""
        # 使用 body_override 构造慢速流：分块写，间隔 > HEADER_TIMEOUT_S
        import time as _time
        class SlowHandler(FakeUpstreamHandler):
            slow_body = SSE_A + SSE_B + SSE_DONE

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    self.rfile.read(length)
                if self.calls is not None:
                    self.calls.append(1)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    # chunk 1: SSE_A
                    self.wfile.write(SSE_A)
                    self.wfile.flush()
                    _time.sleep(1.5)  # > HEADER_TIMEOUT_S=1
                    # chunk 2: SSE_B
                    self.wfile.write(SSE_B)
                    self.wfile.flush()
                    _time.sleep(1.5)
                    # chunk 3: SSE_DONE
                    self.wfile.write(SSE_DONE)
                    self.wfile.flush()
                except ConnectionError:
                    pass
                self.close_connection = True

            def log_message(self, format, *args):
                pass

        calls = []
        handler = type("SlowHandler", (SlowHandler,), {"calls": calls})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        FAKE_SERVERS.append(server)
        upstream_port = server.server_address[1]

        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        # 滴流各 chunk 间隔 1.5s > HEADER_TIMEOUT_S=1，若短超时泄漏到体阶段
        # 读 SSE_B 时必超时断开；完整收到即证明体阶段仍为 UPSTREAM_TIMEOUT=600s
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "drip stream must relay completely despite chunks spaced > HEADER_TIMEOUT_S, "
                         "proving body-phase timeout remains UPSTREAM_TIMEOUT. Got: %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "drip stream must stay result=ok, stderr:\n" + stderr)
        self.assertNotIn("header-timeout", stderr,
                         "body phase must not trigger header-timeout, stderr:\n" + stderr)
```

**测试 5e: 叠加——stall_calls=(1,) + empty_stream → calls==3 + retried=2**

```python
    def test_header_stall_then_empty_stream_compound(self) -> None:
        """stall_calls=(1,) + empty_stream=True → 首呼 header-stall 重试 →
        次呼（empty_stream）触发空流重试 → 三呼正常 → calls==3 + retried=2。"""
        upstream_port, calls = make_stall_upstream(
            stall_calls=(1,), empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        # 呼 1: stall → header-timeout retry
        # 呼 2: empty_stream → 空流重试
        # 呼 3: 正常 body_override=None 走默认 SSE_A+SSE_B+SSE_DONE
        self.assertEqual(len(calls), 3,
                         "header stall + empty stream compound must yield 3 calls, got %d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-3 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=2", stderr,
                      "compound scenario must carry retried=2, stderr:\n" + stderr)
        self.assertIn("header-timeout", stderr,
                      "compound scenario must mention header-timeout, stderr:\n" + stderr)
        self.assertIn("eof-priming", stderr,
                      "compound scenario must mention eof-priming, stderr:\n" + stderr)
```

#### 验证命令

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

卡 2 完成后预期：baseline 103 + 新增 8 组 = 111 tests，全绿。