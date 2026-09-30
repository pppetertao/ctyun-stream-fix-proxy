# PLAN: 200 包错误体观测盲区修复（body-error observability）

## Header

- **目标**：让代理对「HTTP 200 但 body 内嵌 JSON 顶层 `error` 对象」的上游响应可见、可计数、可留痕。新增 `body-err` 结果分类接入现有 REQ 日志 / ERROR_EVENTS 留痕 / /api/stats 计数 / dashboard 展示管线。
- **分支**：`fix/body-error-observability`（worktree `.worktrees/body-error-observability`）
- **spec 路径**：`docs/superpowers/specs/2026-09-30-body-error-observability-design.md`
- **baseline**：`python3 ctyun-stream-fix-proxy.test.py` -> **118 tests OK**（52.792s），全部绿。
- **卡数**：4 卡（小计划，单次 dispatch，不分段）
- **验证命令**：`cd <worktree> && python3 ctyun-stream-fix-proxy.test.py`

## Global Constraints

1. **TDD 铁律**：每卡先写失败测试，再实现，跑绿后才进下一卡。
2. **卡序串行**：Card 1（纯函数/常量）-> Card 2（SSE 路径）-> Card 3（非流式+footer）-> Card 4（集成收口）。
3. **tier**：全部 A 档（spec 锚点已给死，executor 可机械转写）。
4. **commit**：每卡独立 Conventional Commit。PLAN.md 先 commit：`docs(plan): body-error observability execution plan`。
5. **风格一致**：匹配 `ProxyDashboardUnitTest`（in-process）与 `AdminIntegrationTest`（子进程）模式；生产代码匹配既有 `_Outcome` 命名、分支风格、注释习惯。
6. **向后兼容**：`classify_outcome(status=200)` 仍返回 `(CLASS_OK, "ok", False, False)`；基线 118 测试全绿。

---

## Task 1: 纯函数 + 常量 + classify_outcome 扩展 + 矩阵单测

- **tier**：A
- **文件**：`ctyun-stream-fix-proxy.py`（常量区 :62-84、sse_data_line_kind :87-118、sse_line_has_usage 之后、classify_outcome :140-162）、`ctyun-stream-fix-proxy.test.py`（ProxyDashboardUnitTest 类）
- **验证**：`python3 ctyun-stream-fix-proxy.test.py`
- **commit**：`feat(proxy): body_has_error/sse_line_body_error pure functions and classify_outcome body_error parameter`

### TDD Step: 先写失败测试

在 `ProxyDashboardUnitTest` 类中 `test_sse_line_has_usage_matrix` 方法之后（:729 之后、:731 之前），插入以下测试方法。同时修改 `test_sse_data_line_kind_matrix`（:675-706）末尾追加 error 帧断言。新增 `test_kind_category_body_error` 放在 `test_header_timeout_kind_category_and_classify_regression` 之前（:1774 之前）。新增 `test_classify_outcome_body_error` 放在 `test_classify_outcome_full_matrix` 之后（:1510 之后）。

**A. 追加到 `test_sse_data_line_kind_matrix` 末尾（:706 之后）：**

```python
        # error frame -> "content" (not "noise") — spec 集成关踺：error 帧视为 content
        # 触发 priming flush、不触发空流重试
        self.assertEqual(
            f(b'data: {"error":{"message":"TPM limit","type":"rate_limit_error"}}\n'),
            "content")
        self.assertEqual(
            f(b'data: {"error":{"message":"model x not found"}}\r\n'),
            "content")
```

**B. 在 :729 之后插入新测试方法：**

```python

    def test_body_has_error_matrix(self) -> None:
        f = self.mod.body_has_error
        # dict with error dict -> True (OpenAI error schema)
        self.assertTrue(f({"error": {"message": "TPM limit", "type": "rate_limit_error",
                                    "code": "model_tpm_limit"}}))
        self.assertTrue(f({"error": {"message": "x"}}))
        # error key present but value not dict -> False
        self.assertFalse(f({"error": "string error"}))
        self.assertFalse(f({"error": None}))
        self.assertFalse(f({"error": [1, 2, 3]}))
        self.assertFalse(f({"error": 42}))
        # error key missing -> False
        self.assertFalse(f({"choices": [{"delta": {"content": "hi"}}]}))
        self.assertFalse(f({}))
        # not dict -> False
        self.assertFalse(f("not a dict"))
        self.assertFalse(f(None))
        self.assertFalse(f([1, 2, 3]))
        self.assertFalse(f(42))
        # dict with both choices and error -> True (error takes priority)
        self.assertTrue(f({"error": {"message": "x"}, "choices": [{"delta": {}}]}))
        # error dict containing nested "choices" key -> True
        self.assertTrue(f({"error": {"message": "x", "choices": [{}]}}))

    def test_sse_line_body_error_matrix(self) -> None:
        f = self.mod.sse_line_body_error
        # error frame -> True
        self.assertTrue(f(b'data: {"error":{"message":"TPM limit",'
                         b'"type":"rate_limit_error","code":"model_tpm_limit"}}\n'))
        self.assertTrue(f(b'data: {"error":{"message":"x","type":"t","code":"c"}}\r\n'))
        # normal content line -> False
        self.assertFalse(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\n'))
        # content mentioning "error" in text -> False (no string matching)
        self.assertFalse(f(b'data: {"choices":[{"delta":{"content":"error occurred"}}]}\n'))
        # string error value -> False (body_has_error rejects non-dict error)
        self.assertFalse(f(b'data: {"error":"string_err"}\n'))
        self.assertFalse(f(b'data: {"error":null}\n'))
        # non-data prefix -> False
        self.assertFalse(f(b': keepalive\n'))
        self.assertFalse(f(b'event: message\n'))
        self.assertFalse(f(b'id: 42\n'))
        # [DONE] -> False
        self.assertFalse(f(b'data: [DONE]\n'))
        self.assertFalse(f(b'data:[DONE]\r\n'))
        # non-JSON data -> False (fail-open like sse_line_has_usage)
        self.assertFalse(f(b'data: {not-json\n'))
        # empty data -> False
        self.assertFalse(f(b'data: \n'))
        # non-dict JSON -> False
        self.assertFalse(f(b'data: [1,2,3]\n'))
```

**C. 在 :1774 之前插入 `test_kind_category_body_error`：**

```python

    def test_kind_category_body_error(self) -> None:
        mod = self.mod
        self.assertEqual(mod._KIND_CATEGORY.get(mod.ERR_KIND_BODY_ERROR),
                         mod.CLASS_BODY_ERROR,
                         "body_error must map to body_error category")
        self.assertEqual(len(mod._KIND_CATEGORY), 8,
                         "must have exactly 8 kind-category mappings")
```

**D. 在 :1510 之后插入 `test_classify_outcome_body_error`：**

```python

    def test_classify_outcome_body_error(self) -> None:
        mod = self.mod
        f = mod.classify_outcome
        # body_error=True -> CLASS_BODY_ERROR / "body-err" / counts_error=True / capture=True
        o = f(body_error=True)
        self.assertEqual(o.category, mod.CLASS_BODY_ERROR)
        self.assertEqual(o.log_result, "body-err")
        self.assertTrue(o.counts_error)
        self.assertTrue(o.capture)
        # body_error default False: status=200 stays CLASS_OK (no regression)
        o = f(status=200)
        self.assertEqual(o.category, mod.CLASS_OK)
        self.assertEqual(o.log_result, "ok")
        self.assertFalse(o.counts_error)
        # priority: client_abort > body_error
        o = f(client_abort=True, body_error=True)
        self.assertEqual(o.category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(o.log_result, "aborted")
        # priority: synth_502 > body_error
        o = f(synth_502=True, body_error=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "error")
        # priority: body_error > eof_without_done
        o = f(body_error=True, eof_without_done=True)
        self.assertEqual(o.category, mod.CLASS_BODY_ERROR)
        self.assertEqual(o.log_result, "body-err")
        # body_error + status=200: body_error wins over status < 400
        o = f(status=200, body_error=True)
        self.assertEqual(o.category, mod.CLASS_BODY_ERROR)
        self.assertEqual(o.log_result, "body-err")
        # body_error=False (default): no impact on status-based classification
        self.assertEqual(f(status=200).category, mod.CLASS_OK)
        self.assertEqual(f(status=502).category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(f(status=200, poison_filtered=1).category, mod.CLASS_POISON_FIXED)
```

### 实现: 生产代码变更

**变更 1**：常量区，`:74` `ERR_KIND_HEADER_TIMEOUT` 之后插入：

```python
CLASS_BODY_ERROR = "body_error"
ERR_KIND_BODY_ERROR = "body_error"
```

**变更 2**：`_KIND_CATEGORY` dict，`:83` 插入（`ERR_KIND_HEADER_TIMEOUT` 映射之后、`}` 之前）：

```python
    ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR,
```

**变更 3**：`:118` 之后（`sse_data_line_kind` 函数结束空行）、`:120`（`sse_line_has_usage` 定义）之间插入 `body_has_error`：

```python

def body_has_error(parsed) -> bool:
    """判 JSON 解析值是否含顶层 error 对象（OpenAI 错误 schema）。
    isinstance(parsed, dict) 且 isinstance(parsed.get("error"), dict) -> True；
    其余（非 dict、error 键缺失、error 为 str/null/list）-> False。
    不做字符串匹配：content 文本出现 "error" 字样不误判。"""
    if not isinstance(parsed, dict):
        return False
    error_val = parsed.get("error")
    return isinstance(error_val, dict)
```

**变更 4**：`:134` 之后（`sse_line_has_usage` 结束）、`:136`（`_Outcome` 定义）之间插入 `sse_line_body_error`：

```python

def sse_line_body_error(line: bytes) -> bool:
    """判 SSE data 行是否为 body error 帧。rstrip -> 非 data: 前缀或 [DONE] ->
    False；json.loads ValueError -> False（fail-open 同 sse_line_has_usage）；
    否则 return body_has_error(data)。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return False
    return body_has_error(data)
```

**变更 5**：`sse_data_line_kind`，`:103-104` `isinstance(data, dict)` 检查之后、`choices = data.get("choices")` 之前插入 error 分支：

老代码：
```
    if not isinstance(data, dict):
        return "noise"
    choices = data.get("choices")
```
新代码：
```
    if not isinstance(data, dict):
        return "noise"
    if body_has_error(data):
        return "content"
    choices = data.get("choices")
```

**变更 6**：`sse_data_line_kind` 的 docstring（:88-93），补充 error 帧说明：
老代码开头：
```
    """SSE data 行归类："done" / "content" / "finish" / "noise"。判定序：
    非 data: 前缀（注释行/event:/id:）-> noise；[DONE] -> done；
    JSON 解析失败 -> content（fail-open：宁可不重试，不误判合法流）；
    dict + choices 非空 list 时：delta.content 非空 str / delta.tool_calls 真值 ->
    content；choice.finish_reason 非 None（且无前两者）-> finish；
    其余（reasoning-only、空 delta、choices 为空的 usage 帧、data:null、非 dict JSON）-> noise。"""
```
新代码：
```
    """SSE data 行归类："done" / "content" / "finish" / "noise"。判定序：
    非 data: 前缀（注释行/event:/id:）-> noise；[DONE] -> done；
    JSON 解析失败 -> content（fail-open：宁可不重试，不误判合法流）；
    dict 且顶层 error 对象 -> content（body error 帧视为 content，触发 priming flush 不重试）；
    dict + choices 非空 list 时：delta.content 非空 str / delta.tool_calls 真值 ->
    content；choice.finish_reason 非 None（且无前两者）-> finish；
    其余（reasoning-only、空 delta、choices 为空的 usage 帧、data:null、非 dict JSON）-> noise。"""
```

**变更 7**：`classify_outcome` 签名，`:140-141` 加 `body_error=False`（末位追加，向后兼容）：

```python
def classify_outcome(status=None, synth_502=False, client_abort=False,
                     eof_without_done=False, poison_filtered=0,
                     body_error=False) -> _Outcome:
```

**变更 8**：`classify_outcome` docstring，`:144` 优先级链加入 body_error：

```
    """错误分类网关：输入场景标志 -> 返回 (category, log_result, counts_error, capture)。

    判定优先级 client_abort > synth_502 > body_error > eof_without_done > status 阈值；
    status=None 且无 flags -> ValueError。
    """
```

**变更 9**：`classify_outcome` 函数体，`:149-151` 之间（`synth_502` 之后、`eof_without_done` 之前）插入：

```python
    if body_error:
        return _Outcome(CLASS_BODY_ERROR, "body-err", True, True)
```

---

## Task 2: SSE 流路径 — _relay_sse 检测 + _proxy_relay SSE 分支接入

- **tier**：A
- **文件**：`ctyun-stream-fix-proxy.py`（`_relay_sse` :1000-1079、`_proxy_relay` :898-954）、`ctyun-stream-fix-proxy.test.py`（`BodyErrorTest` 类）
- **验证**：`python3 ctyun-stream-fix-proxy.test.py`
- **commit**：`feat(proxy): SSE body error detection in _relay_sse and _proxy_relay SSE branch`

### TDD Step: 先写失败测试

在 `ctyun-stream-fix-proxy.test.py` 顶部（:39 `SSE_DONE` 行之后），新增 SSE 错误帧常量：

```python
SSE_ERROR_FRAME = b'data: {"error":{"message":"\u6a21\u578b\u8bf7\u6c42 TPM \u8d85\u9650\uff0c\u8bf7\u51cf\u5c11 tokens \u540e\u91cd\u8bd5","type":"rate_limit_error","code":"model_tpm_limit"}}\n\n'
```

在 `AdminIntegrationTest` 类之后、`if __name__ == "__main__"` 之前（:3223 之前）新增 `BodyErrorTest` 类。类结构与 `AdminIntegrationTest` 对齐（setUp/tearDown 管理子进程）。

```python

class BodyErrorTest(unittest.TestCase):
    ""body error observability \u96c6\u6210\u6d4b\u8bd5\uff08SSE error \u5e27 + \u975e\u6d41\u5f0f error JSON\uff09\u3002
    \u5bf9\u9f50 AdminIntegrationTest \u6a21\u5f0f\uff1a\u6bcf\u7528\u4f8b\u72ec\u7acb\u5047\u4e0a\u6e38 + \u4ee3\u7406\u5b50\u8fdb\u7a0b\u3002"""

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            stderr_text(self.proc)
            shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
            stop_fake_upstreams()

    def test_sse_body_error_classified_and_logged(self) -> None:
        """SSE error 帧 -> stderr REQ 行 result=body-err + /api/stats errors_total +1。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        body = SSE_A + SSE_ERROR_FRAME + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        # error \u5e27\u539f\u6837\u900f\u4f20\uff08\u4e0d\u5265\u9664\uff09
        self.assertEqual(data, body,
                         "error frame must be relayed to client intact, got %r" % data[:200])
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body_bytes.decode("utf-8"))
        self.assertGreaterEqual(snap["errors_total"], 1,
                                "errors_total must increment for body error")
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["daily"][today]["errors_proxy"], 1,
                                "daily errors_proxy must increment for body error")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=body-err", stderr,
                      "REQ line must carry result=body-err, stderr:\n" + stderr)

    def test_body_error_no_retry(self) -> None:
        """SSE error 帧流不触发空流重试（calls==1）。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        body = SSE_ERROR_FRAME + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "error frame stream must not trigger retry, calls=%d" % len(calls))
        self.assertEqual(data, body,
                         "error-only stream must relay as-is, got %r" % data[:200])
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=body-err", stderr,
                      "error-only stream must carry result=body-err, stderr:\n" + stderr)
```

### 实现: 生产代码变更

**变更 1**：`_relay_sse` record 终结块，:1025-1026 `kinds` 计算之后追加 body error 检测：

老代码：
```python
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    saw_done = saw_done or ("done" in kinds)
```

新代码：
```python
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    saw_done = saw_done or ("done" in kinds)
                    if self._body_err_line is None:
                        hit = next((l for l in pending if sse_line_body_error(l)), None)
                        if hit is not None:
                            self._body_err_line = hit
```

`self._body_err_line` 由 `_proxy_relay` 在每次 `_relay_sse` 调用前重置为 None（见变更 2），`_relay_sse` 内只读不写初始值，命中后定向赋值。`_relay_sse` 返回签名不变（`return filtered, truncated`）。

**变更 2**：`_proxy_relay` SSE 分支（:898-954），在每处 `_relay_sse` 调用前重置 `self._body_err_line = None`，调用后读 `body_error = self._body_err_line is not None`，`classify_outcome` 传 `body_error=body_error`，capture 分支加 `ERR_KIND_BODY_ERROR` 优先处理。

完整替换 :898 行到 :954 行：

```python
        content_type = (resp.getheader("Content-Type") or "").lower()
        if "text/event-stream" in content_type:
            retried = header_retried
            retry_reason = header_retry_reason
            try:
                self._body_err_line = None
                filtered, truncated = self._relay_sse(resp,
                    final=not empty_stream_should_retry(EMPTY_RETRY_MAX))
                body_error = self._body_err_line is not None
            except _EmptyStream as exc:
                filtered = exc.filtered  # attempt-1 已滤毒缓冲随重试丢弃，filtered 只计交付流
                retried = header_retried + 1
                retry_reason = exc.reason
                _record_empty_retry(model, exc.reason)
                record_error_event(ERR_KIND_EMPTY_RETRY, model=model, path=self.path,
                                   response=b"".join(exc.lines),
                                   retry_reason=exc.reason)
                conn.close()
                try:
                    conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                except (OSError, http.client.HTTPException) as retry_exc:
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
                self._body_err_line = None
                filtered, truncated = self._relay_sse(resp, final=True)
                body_error = self._body_err_line is not None
            outcome = classify_outcome(status=resp.status, poison_filtered=filtered,
                                       body_error=body_error)
            result = outcome.log_result  # 重试后按实际 resp 重算
            if truncated and result == "ok":
                # 仅覆盖 ok：body-err/upstream-err（status>=400 更有信息量）与 aborted
                # （异常路径不经此处）不误标；priming EOF 由 retries 计数承载，避免双计数
                result = "eof-without-done"
                outcome = classify_outcome(eof_without_done=True)
                _record_eof_without_done(model)
            if outcome.capture:
                if body_error:
                    kind = ERR_KIND_BODY_ERROR
                elif outcome.category == CLASS_POISON_FIXED:
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
                                   body=body, filtered=filtered,
                                   response=self._body_err_line if body_error else None)
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error)
```

关键设计点：
- `self._body_err_line` 由 `_proxy_relay` 在调用前重置为 None。`_relay_sse` 在 record 终结块检测命中后赋值。`_proxy_relay` 调用后读 `self._body_err_line is not None`。
- `BaseHTTPRequestHandler` 每请求独立实例，无并发问题。
- capture 分支 `body_error` 优先于 `poison/eof/status`，命中行原字节传给 `record_error_event`（内部 `_snapshot_text(response, 2048)` 截断保护）。

---

## Task 3: 非流式路径 body error + footer 文案

- **tier**：A
- **文件**：`ctyun-stream-fix-proxy.py`（`_proxy_relay` 非流式分支 :955-969、DASHBOARD_HTML footer :1422）、`ctyun-stream-fix-proxy.test.py`（`FakeUpstreamHandler` + `make_fake_upstream` + `BodyErrorTest`）
- **验证**：`python3 ctyun-stream-fix-proxy.test.py`
- **commit**：`feat(proxy): non-streaming body error detection and dashboard footer update`

### TDD Step: 先写失败测试

**前置 1**：给 `FakeUpstreamHandler` 加 `fail_200_error` 属性。:52 `fail_500` 之后追加：

```python
    fail_200_error = False  # True -> do_POST 回 200 + JSON error body（非流式 body error 测试用）
```

**前置 2**：`FakeUpstreamHandler.do_POST`，:105 `if self.fail_500:` 之前插入 `fail_200_error` 分支：

```python
        if self.fail_200_error:
            body = b'{"error":{"message":"model tpm limit","type":"rate_limit_error","code":"model_tpm_limit"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
```

**前置 3**：`make_fake_upstream` 签名（:209）加 `fail_200_error` 参数，attrs 映射（:220）加 `"fail_200_error": fail_200_error`。

**BodyErrorTest 追加测试**（`test_body_error_no_retry` 之后，`tearDown` 之前）：

```python
    def test_buffered_body_error(self) -> None:
        """非流式 200 + JSON error body -> result=body-err，errors_total +1。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port = make_fake_upstream(False, fail_200_error=True)
        self.proc = start_proxy(upstream_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200,
                         "fail_200_error upstream must return 200, got %d" % resp.status)
        data = resp.read()
        conn.close()
        self.assertIn(b"error", data, "response must contain error body")
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body_bytes.decode("utf-8"))
        self.assertGreaterEqual(snap["errors_total"], 1,
                                "non-SSE body error must increment errors_total")
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["daily"][today]["errors_proxy"], 1,
                                "non-SSE body error must increment daily errors_proxy")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=body-err", stderr,
                      "non-SSE error must log result=body-err, stderr:\n" + stderr)
```

### 实现: 生产代码变更

**变更 1**：`_proxy_relay` 非流式分支（:955-969），加入 body error 检测。

老代码（:955-969）：
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

新代码：
```python
        else:
            relayed_data = self._relay_buffered(resp)
            body_error = False
            if relayed_data:
                try:
                    parsed = json.loads(relayed_data.decode("utf-8", "replace"))
                    body_error = body_has_error(parsed)
                except ValueError:
                    pass  # non-JSON body -> no body error, fail-open
            outcome = classify_outcome(status=resp.status, body_error=body_error)
            self._log(started, resp.status, outcome.log_result, 0, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error)
            if outcome.capture:
                if body_error:
                    kind = ERR_KIND_BODY_ERROR
                elif resp.status >= 500:
                    kind = ERR_KIND_UPSTREAM_5XX
                else:
                    kind = ERR_KIND_REQUEST_4XX
                record_error_event(
                    kind, model=model, path=self.path,
                    upstream_status=resp.status if resp.status >= 400 else None,
                    body=body,
                    response=relayed_data[:RESPONSE_SNIPPET_CAP]
                    if body_error or resp.status >= 500 else None)
```

**变更 2**：DASHBOARD_HTML footer（:1422），在「代理错误」定义处追加 body 内嵌错误说明。

老代码：
```html
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误，「上游5xx」=上游透传 status>=500
```

新代码：
```html
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（含 body 内嵌错误：HTTP 200 但 body 顶层 error 对象），「上游5xx」=上游透传 status>=500
```

---

## Task 4: BodyErrorTest 集成收口（>=6 用例）

- **tier**：A
- **文件**：`ctyun-stream-fix-proxy.test.py`（`BodyErrorTest` 类追加 2 个测试）
- **验证**：`python3 ctyun-stream-fix-proxy.test.py`
- **commit**：`test(proxy): BodyErrorTest integration coverage for capture and false-positive`

**说明**：Card 1-3 完成后生产代码已就位。本卡追加 2 个集成测试收口。总共 5 个 Card 2-4 集成用例（SSE classifed_logged + no_retry + buffered + recorded + content_not_flagged）+ Card 1 纯函数单测。

### TDD Step: 写测试（此时生产代码已就位，若前序实现正确则全绿）

在 `BodyErrorTest` 类中追加（`test_buffered_body_error` 之后，`tearDown` 之前）：

```python
    def test_sse_body_error_recorded(self) -> None:
        """capture_errors on + SSE error 帧 -> /api/errors kind=body_error，detail 含 response。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        body = SSE_A + SSE_ERROR_FRAME + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body_bytes, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body_bytes.decode("utf-8"))
        self.assertTrue(data["capture_errors"])
        self.assertGreaterEqual(data["count"], 1)
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("body_error", kinds,
                      "/api/errors must contain kind=body_error, got %r" % kinds)
        eid = data["events"][0]["id"]
        status, body_bytes, _ = admin_get(self.proc.admin_port, "/api/errors?id=%d" % eid)
        self.assertEqual(status, 200)
        ev = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(ev["kind"], "body_error")
        self.assertEqual(ev["category"], "body_error")
        self.assertIn("response", ev)
        self.assertIsNotNone(ev["response"])
        self.assertIn("error", ev["response"].lower(),
                      "detail response must contain the error frame text")

    def test_sse_content_mentioning_error_not_flagged(self) -> None:
        """content 文本含 'error' 字样 -> result=ok，零误判。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        body = (SSE_A +
                b'data: {"choices":[{"delta":{"content":"error occurred"}}]}\n\n' +
                SSE_B + SSE_DONE)
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(data, body,
                         "normal content mentioning 'error' must relay as-is, got %r" % data[:200])
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(snap["errors_total"], 0,
                         "false positive must not increment errors_total")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "content with 'error' text must stay result=ok, stderr:\n" + stderr)
        self.assertNotIn("body-err", stderr,
                         "content with 'error' text must NOT be flagged as body-err")
```

**注意**：`admin_get` 调用在 `proc.terminate()` 之前（进程还存在），确保 admin 端口可达。

---

## Card Execution Summary

| Card | Tier | 生产文件 | 测试文件 | 新增测试 | 生产变更 |
|------|------|----------|----------|----------|----------|
| 1 | A | proxy.py (:62-162) | test.py (ProxyDashboardUnitTest) | 5 单测 | 常量 2、_KIND_CATEGORY 1、函数 2、sse_data_line_kind 1、classify_outcome 2（签名+分支+doc） |
| 2 | A | proxy.py (:898-1079) | test.py (BodyErrorTest + SSE_ERROR_FRAME) | 2 集成 | _relay_sse 1、_proxy_relay SSE 分支完整重写 |
| 3 | A | proxy.py (:955-969, :1422) | test.py (FakeUpstreamHandler + make_fake_upstream + BodyErrorTest) | 1 集成 | _proxy_relay 非流式分支重写、footer 1 行 |
| 4 | A | 无 | test.py (BodyErrorTest) | 2 集成 | 无 |

**总计**：净增约 260 行测试 + ~70 行生产 + 1 行 footer。