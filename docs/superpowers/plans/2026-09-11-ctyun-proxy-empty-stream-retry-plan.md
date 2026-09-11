# PLAN — ctyun-proxy SSE 空流透明重试（v1.4）

## Header

- **Spec**: `docs/superpowers/specs/2026-09-11-ctyun-proxy-empty-stream-retry-design.md`（已批准；锚点行号与当前 worktree 文件逐一对过）
- **分支/工作树**: `fix/empty-stream-retry` @ `.worktrees/empty-stream-retry/`（spec 已 commit 于 1b9bc2c）
- **改动文件（仅两个，其他零触碰）**:
  - `ctyun-stream-fix-proxy.py`
  - `ctyun-stream-fix-proxy.test.py`
- **基线（2026-09-11 实测）**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 32 tests in 12.157s / OK / exit 0`
- **PLAN 自验（2026-09-11 /tmp 沙箱 dry-run）**: 本卡全部 OLD/NEW 块机械抽取应用（OLD 唯一命中，T7 两处）→ red 阶段精确 10 红（9 failures + 1 error，与 Task 1 红清单逐条一致）→ green 阶段 `Ran 41 tests / OK / exit 0` + 两文件 `py_compile` exit 0
- **部署**：复制两文件到 `~/.local/bin/` + `cmp` + `launchctl kickstart` = 主代理 DELIVER 职责，**不进任何卡**
- **卡数/tier**: 2 卡，全部 **tier A（转写卡）** → executor；无 C 卡（全部测试基于本地 fake upstream，无真机/运行时数据/集成边界实测留白）

## Global Constraints

1. 解释器固定 `/usr/bin/python3`（3.9.6，已实测）。禁 3.10+ 语法：无 `match/case`、无 PEP 604 类型联合（`X | Y`）、无括号化 context managers。
2. 纯 stdlib，不加任何依赖。
3. 所有命令 cwd = worktree 根：`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/empty-stream-retry`。
4. **语义零变更区**（卡内代码是唯一允许的变更，不得顺手改）：毒行剥除判定与 preview 口径、`_relay_buffered`、`_reply_502`、499 路径、`_safe_log_stderr`/drain/kill_registered 基建、dashboard HTML/JS、admin 鉴权。
5. **No Placeholders**：每处 Edit 给出唯一命中的 OLD 块 + 完整 NEW 块，用 Edit 工具逐字转写（含缩进与注释）。OLD 定位失败或不唯一 → **STOP 报主代理**，不得自行适配。
6. Task 1 结束 = **精确红清单**（10 红 = 9 failures + 1 error，31 绿）；Task 2 结束 = **41 全绿**。红/绿漂移出清单 → STOP。
7. Conventional Commits（两条，信息写死于 Task 2 末尾）。
8. claim 完成前贴实际命令 stdout + exit code（VERIFY 铁律，同次 dispatch 内完成）。

---

## Task 1 [tier A] — baseline 登记 + 失败测试（TDD red）

**文件**: 仅 `ctyun-stream-fix-proxy.test.py`（生产代码零触碰）。**本卡不 commit**（red 中间态，commit 在 Task 2 末尾统一做）。

### 1.1 baseline 登记

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/empty-stream-retry && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 32 tests in 12.xxx s

OK
Exit code: 0
```

非 32 绿 → 基线漂移，STOP 报主代理。

### 1.2 Edit T1 — 常量区加 `SSE_REASONING`（锚点 :34）

OLD:
```python
SSE_DONE = b"data: [DONE]\n\n"
```

NEW:
```python
SSE_DONE = b"data: [DONE]\n\n"
SSE_REASONING = b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}\n\n'
```

### 1.3 Edit T2 — `FakeUpstreamHandler` 类属性（锚点 :40-44）

OLD:
```python
class FakeUpstreamHandler(BaseHTTPRequestHandler):
    poison = False
    tag = "/plain"  # /plain 响应携带的路径标记，供"上游热切换后路由命中"断言区分
    big = False     # True → 1.2MB 大 SSE 流，供 client-abort 测试把代理写缓冲打穿
    fail_500 = False  # True → do_POST 回 500 JSON（上游 5xx 透传计数测试用）
```

NEW:
```python
class FakeUpstreamHandler(BaseHTTPRequestHandler):
    poison = False
    tag = "/plain"  # /plain 响应携带的路径标记，供"上游热切换后路由命中"断言区分
    big = False     # True → 1.2MB 大 SSE 流，供 client-abort 测试把代理写缓冲打穿
    fail_500 = False  # True → do_POST 回 500 JSON（上游 5xx 透传计数测试用）
    empty_stream = False  # True → 每次 POST 回空流（reasoning 后 EOF，无 [DONE]）
    blank_stream = False  # True → 空流形态为零字节 body（200 + SSE 头 + 立即 EOF）
    body_override = None  # 非 None → 正常路径 body 用此值（priming 前缀/合法 DONE 场景）
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流
```

### 1.4 Edit T3 — `do_POST` 空流分支 + `body_override` 完整替换（锚点 :50-65）

OLD:
```python
        if self.fail_500:
            body = b'{"error":"upstream exploded"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        if self.big:
            self._respond_big_sse()
            return
        body = SSE_A + SSE_B
        if self.poison:
            body += SSE_POISON
        body += SSE_DONE
```

NEW:
```python
        if self.fail_500:
            body = b'{"error":"upstream exploded"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        if self.calls is not None:
            self.calls.append(1)
        if self.empty_stream or (self.calls is not None and len(self.calls) == 1
                                 and self.body_override is None):
            # 空流签名：200 + SSE 头 + 少量 reasoning delta 后无 [DONE] 即 EOF
            # （blank_stream 则零字节）。body_override 场景首呼走正常路径：
            # spec ②③ 的 calls==1 断言要求首响应即 override 内容、不触发重试。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if not self.blank_stream:
                try:
                    self.wfile.write(SSE_REASONING)
                except ConnectionError:
                    # 吞掉的是代理侧已放弃读取后的写出端 Broken pipe（预期路径）：
                    # 测试假上游无需留痕，无其他路径可达。
                    pass
            self.close_connection = True
            return
        if self.big:
            self._respond_big_sse()
            return
        if self.body_override is not None:
            body = self.body_override  # 完整 body 替换：调用方自带整段流，不再追加 poison/DONE
        else:
            body = SSE_A + SSE_B
            if self.poison:
                body += SSE_POISON
            body += SSE_DONE
```

### 1.5 Edit T4 — `make_fake_upstream` 扩展 + `make_scripted_upstream`（锚点 :106-113）

OLD:
```python
def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
                       fail_500: bool = False) -> int:
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,),
                   {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1]
```

NEW:
```python
def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
                       fail_500: bool = False, empty_stream: bool = False,
                       blank_stream: bool = False, body_override=None,
                       scripted: bool = False) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
             "empty_stream": empty_stream, "blank_stream": blank_stream,
             "body_override": body_override}
    if scripted:
        attrs["calls"] = []
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,), attrs)
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1]


def make_scripted_upstream(**kwargs) -> tuple:
    """带调用计数的假上游：返回 (port, calls)；calls 按上游被请求次数 append。
    kwargs 透传 empty_stream / blank_stream / body_override（勿传 scripted/poison）。"""
    port = make_fake_upstream(False, scripted=True, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls
```

### 1.6 Edit T5 — 单测 `test_sse_data_line_kind_matrix`（`test_extract_model` 后，锚点 :379-380）

OLD:
```python
        self.assertIsNone(f(b'{"model":123}'))
        self.assertIsNone(f(b'{"model":""}'))
```

NEW:
```python
        self.assertIsNone(f(b'{"model":123}'))
        self.assertIsNone(f(b'{"model":""}'))

    def test_sse_data_line_kind_matrix(self) -> None:
        f = self.mod.sse_data_line_kind
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\n'), "content")
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\r\n'), "content")
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":""}}]}\n'), "noise")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1"}]},"index":0}]}\n'),
            "content")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'), "content")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}\n'), "noise")
        self.assertEqual(f(b"data: [DONE]\n"), "done")
        self.assertEqual(f(b"data:[DONE]\r\n"), "done")
        self.assertEqual(f(b": keep-alive comment\n"), "noise")
        self.assertEqual(f(b"event: message\n"), "noise")
        self.assertEqual(f(b"id: 42\n"), "noise")
        self.assertEqual(f(b"data: null\n"), "noise")
        self.assertEqual(f(b'data: {"choices":[]}\n'), "noise")
        self.assertEqual(f(b'data: {"id":"x","choices":[{"delta":{}}]}\n'), "noise")
        self.assertEqual(f(b'data: {"usage":{"total_tokens":9},"choices":[]}\n'), "noise")
        self.assertEqual(f(b"data: [1,2,3]\n"), "noise")
        self.assertEqual(f(b"data: {not-json\n"), "content",
                         "非 JSON data 行 fail-open 归 content（宁漏不误）")
```

### 1.7 Edit T6 — `test_stats_persist_roundtrip_and_defaults` 两个 equality 字典（锚点 :387-392）

OLD:
```python
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0})
```

NEW:
```python
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0})
```

### 1.8 Edit T7 — 两处 key 断言列表（锚点 :421-424 与 :613-616，**replace_all 一次替换两处**）

OLD（在文件中出现 2 次，`test_stats_snapshot_shape` 与 `test_dashboard_and_stats_served` 各一）:
```python
        for key in ("requests_total", "filtered_total", "errors_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews"):
            self.assertIn(key, snap)
```

NEW:
```python
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews"):
            self.assertIn(key, snap)
```

### 1.9 Edit T8 — `test_req_log_line_has_ts_and_model` 正则插 `retried`（锚点 :744-748）

OLD:
```python
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
```

NEW:
```python
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
```

### 1.10 Edit T9 — 8 个集成用例（`test_daily_buckets_resume_from_persist` 后、`if __name__` 前，锚点 :863-867）

OLD:
```python
        self.assertEqual(snap["daily"]["2026-01-01"]["requests"], 5)
        self.assertEqual(snap["daily"]["2026-01-01"]["errors_upstream"], 2)


if __name__ == "__main__":
```

NEW:
```python
        self.assertEqual(snap["daily"]["2026-01-01"]["requests"], 5)
        self.assertEqual(snap["daily"]["2026-01-01"]["errors_upstream"], 2)

    def test_empty_stream_retried_and_second_attempt_relayed(self) -> None:
        upstream_port, calls = make_scripted_upstream()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "empty stream must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)

    def test_priming_prefix_flushed_in_order_no_retry(self) -> None:
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_A + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "content-bearing stream must not retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_A + SSE_DONE,
                         "priming prefix must flush first and in order, got %r" % data)

    def test_done_without_content_is_legal_no_retry(self) -> None:
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "[DONE] without content is legal, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_DONE)

    def test_double_empty_stream_falls_back_after_two_calls(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "retry cap is 1: second empty stream ends the attempt, calls=%d"
                         % len(calls))
        self.assertEqual(data, SSE_REASONING,
                         "attempt-2 buffer must be delivered as-is, got %r" % data)

    def test_zero_record_empty_200_retried(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True, blank_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.assertEqual(data, b"", "zero-record stream must relay zero bytes")

    def test_ctyun_empty_retry_zero_disables(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_EMPTY_RETRY": "0"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "CTYUN_EMPTY_RETRY=0 must disable retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING)

    def test_empty_retry_counter_persists_and_resumes(self) -> None:
        upstream_port, calls = make_scripted_upstream()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=5)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertGreaterEqual(saved["stats"]["empty_retries_total"], 1)
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(saved["stats"]["daily"][today]["retries"], 1)
        upstream_port2, calls2 = make_scripted_upstream()
        self.proc = start_proxy(
            upstream_port2, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % upstream_port2,
                          "stats": {"empty_retries_total": 5}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["empty_retries_total"], 5,
                         "seeded counter must resume from persist")
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls2), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["empty_retries_total"], 6)

    def test_by_model_retries_dimension(self) -> None:
        upstream_port, calls = make_scripted_upstream()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        entry = snap["by_model"]["deepseek-v4-pro-0813-oc"]
        self.assertGreaterEqual(entry["retries"], 1)
        self.assertEqual(entry["requests"], 1,
                         "retry must not double-count model requests")
        self.assertGreaterEqual(snap["empty_retries_total"], 1)


if __name__ == "__main__":
```

### 1.11 验证（red）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/empty-stream-retry && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 41 tests in xx.xxx s

FAILED (failures=9, errors=1)
Exit code: 1
```

**精确红清单（10 红，其余 31 绿）**:

| # | 用例 | 红法 |
|---|------|------|
| 1 | `ProxyDashboardUnitTest.test_sse_data_line_kind_matrix` | ERROR: AttributeError（`sse_data_line_kind` 尚不存在） |
| 2 | `AdminIntegrationTest.test_empty_stream_retried_and_second_attempt_relayed` | FAIL: len(calls) 1 != 2（无重试） |
| 3 | `AdminIntegrationTest.test_double_empty_stream_falls_back_after_two_calls` | FAIL: len(calls) 1 != 2 |
| 4 | `AdminIntegrationTest.test_zero_record_empty_200_retried` | FAIL: len(calls) 1 != 2 |
| 5 | `AdminIntegrationTest.test_empty_retry_counter_persists_and_resumes` | FAIL: len(calls) 1 != 2 |
| 6 | `AdminIntegrationTest.test_by_model_retries_dimension` | FAIL: len(calls) 1 != 2 |
| 7 | `ProxyDashboardUnitTest.test_stats_persist_roundtrip_and_defaults` | FAIL: 字典缺 `empty_retries_total` 键 |
| 8 | `AdminIntegrationTest.test_req_log_line_has_ts_and_model` | FAIL: REQ 行无 `retried=`，正则不匹配 |
| 9 | `ProxyDashboardUnitTest.test_stats_snapshot_shape` | FAIL: snap 缺 `empty_retries_total` 键 |
| 10 | `AdminIntegrationTest.test_dashboard_and_stats_served` | FAIL: /api/stats JSON 缺键 |

新用例 `test_priming_prefix_flushed_in_order_no_retry` / `test_done_without_content_is_legal_no_retry` / `test_ctyun_empty_retry_zero_disables` 在 red 阶段**本来就绿**（现状透传即满足，属行为护栏），不得"修红"。红名单外任何红/绿变化 = 锚点漂移 → STOP。

```
$ /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.test.py
Exit code: 0
```

---

## Task 2 [tier A] — 生产实现 + 全量 41 绿（TDD green）

**文件**: 仅 `ctyun-stream-fix-proxy.py`。

### 2.1 Edit P1 — 模块级新增（`POISON_RE` 后，锚点 :46-48）

OLD:
```python
POISON_RE = re.compile(rb"^data:\s*null\s*$")
HOP_HEADERS = {"connection", "keep-alive", "proxy-connection",
               "te", "trailer", "transfer-encoding", "upgrade"}
```

NEW:
```python
POISON_RE = re.compile(rb"^data:\s*null\s*$")
DONE_RE = re.compile(rb"^data:\s*\[DONE\]\s*$")
EMPTY_RETRY_MAX = int(os.environ.get("CTYUN_EMPTY_RETRY", "1"))  # env seam，惯例同 SEND_TIMEOUT_S


def sse_data_line_kind(line: bytes) -> str:
    """SSE data 行归类："done" / "content" / "noise"。判定序：
    非 data: 前缀（注释行/event:/id:）→ noise；[DONE] → done；
    JSON 解析失败 → content（fail-open：宁可不重试，不误判合法流）；
    dict + choices 非空 list 时：delta.content 非空 str / delta.tool_calls 真值 /
    choice.finish_reason 非 None 任一 → content；其余（reasoning-only、空 delta、
    choices 为空的 usage 帧、data:null、非 dict JSON）→ noise。"""
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
        return "content"
    return "noise"


class _EmptyStream(Exception):
    """priming EOF 仍无 content/[DONE]：携带 filtered 计数与已滤毒缓冲行。"""
    def __init__(self, filtered: int, lines: list):
        super().__init__("empty upstream sse stream")
        self.filtered = filtered
        self.lines = lines


HOP_HEADERS = {"connection", "keep-alive", "proxy-connection",
               "te", "trailer", "transfer-encoding", "upgrade"}
```

### 2.2 Edit P2 — `STATS` 初始化加键（锚点 :53-54）

OLD:
```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "active": 0, "by_model": {}, "daily": {}}
```

NEW:
```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0,
         "active": 0, "by_model": {}, "daily": {}}
```

### 2.3 Edit P3 — `save_stats_counters` 四键（锚点 :140）

OLD:
```python
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total")}
```

NEW:
```python
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total")}
```

### 2.4 Edit P4 — `load_stats_counters` 四键（锚点 :166-170）

OLD:
```python
def load_stats_counters(path: str) -> dict:
    """从持久化文件读累计计数；缺文件/损坏/legacy 无 stats 键 → 三零值。"""
    stats = _load_persist_file(path).get("stats")
    out = {}
    for key in ("requests_total", "filtered_total", "errors_total"):
```

NEW:
```python
def load_stats_counters(path: str) -> dict:
    """从持久化文件读累计计数；缺文件/损坏/legacy 无 stats 键 → 各键零值。"""
    stats = _load_persist_file(path).get("stats")
    out = {}
    for key in ("requests_total", "filtered_total", "errors_total", "empty_retries_total"):
```

### 2.5 Edit P5 — `_DAILY_FIELDS` 追加（锚点 :176）

OLD:
```python
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream")
```

NEW:
```python
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream", "retries")
```

### 2.6 Edit P6 — `_record_request` 两处形状同步（锚点 :243-244 与 :248-250，两个独立 Edit）

P6a OLD:
```python
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0}
```

P6a NEW:
```python
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0, "retries": 0}
```

P6b OLD:
```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0})
```

P6b NEW:
```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0})
```

### 2.7 Edit P7 — 新函数 `_record_empty_retry`（`_record_poison_preview` 后，锚点 :263-268）

OLD:
```python
def _record_poison_preview(raw: bytes) -> None:
    preview = raw.decode("utf-8", "replace")
    if len(preview) > 200:
        preview = preview[:200]
    with STATS_LOCK:
        POISON_PREVIEWS.append({"ts": time.time(), "preview": preview})
```

NEW:
```python
def _record_poison_preview(raw: bytes) -> None:
    preview = raw.decode("utf-8", "replace")
    if len(preview) > 200:
        preview = preview[:200]
    with STATS_LOCK:
        POISON_PREVIEWS.append({"ts": time.time(), "preview": preview})


def _record_empty_retry(model=None) -> None:
    """空流重试计数：STATS 总量 + by_model retries 维度 + 当日桶。
    entry/桶形状必须与 _record_request 同步含 retries 键（旧持久化桶经
    load_daily_buckets 的 _DAILY_FIELDS 清洗已补键），否则 += 直接 KeyError。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["empty_retries_total"] += 1
        if model:
            by_model = STATS["by_model"]
            entry = by_model.get(model)
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry is not None:  # 键数达上限后新模型不记录，防内存膨胀
                entry["retries"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0})
        bucket["retries"] += 1
        _stats_dirty = True
```

### 2.8 Edit P8 — `_proxy_relay` 建连抽取 + SSE 分支重试 + 新方法 `_open_upstream`（锚点 :344-375）

OLD:
```python
        parsed = urllib.parse.urlparse(UPSTREAM_BASE)
        upstream_path = parsed.path.rstrip("/") + self.path
        try:
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(
                    parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
            else:
                conn = http.client.HTTPConnection(
                    parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
            conn.request(self.command, upstream_path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            self._log(started, 502, "error", 0, model=model)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=True)
            return

        content_type = (resp.getheader("Content-Type") or "").lower()
        result = "ok" if resp.status < 400 else "upstream-err"
        if "text/event-stream" in content_type:
            filtered = self._relay_sse(resp)
            self._log(started, resp.status, result, filtered, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model)
        else:
            self._relay_buffered(resp)
            self._log(started, resp.status, result, 0, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model)
        conn.close()
```

NEW:
```python
        try:
            conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            self._log(started, 502, "error", 0, model=model)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=True)
            return

        content_type = (resp.getheader("Content-Type") or "").lower()
        result = "ok" if resp.status < 400 else "upstream-err"
        if "text/event-stream" in content_type:
            retried = 0
            try:
                filtered = self._relay_sse(resp, final=(EMPTY_RETRY_MAX < 1))
            except _EmptyStream as exc:
                filtered = exc.filtered  # attempt-1 已滤毒缓冲随重试丢弃，filtered 只计交付流
                retried = 1
                _record_empty_retry(model)
                conn.close()
                try:
                    conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                except (OSError, http.client.HTTPException) as retry_exc:
                    self._reply_502(retry_exc)  # 客户端尚未收到字节，502 语义与既有路径一致
                    self._log(started, 502, "error", 0, model=model, retried=1)
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model, error=True)
                    return
                filtered = self._relay_sse(resp, final=True)
            result = "ok" if resp.status < 400 else "upstream-err"  # 重试后按实际 resp 重算
            self._log(started, resp.status, result, filtered, model=model, retried=retried)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model)
        else:
            self._relay_buffered(resp)
            self._log(started, resp.status, result, 0, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model)
        conn.close()

    def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):
        parsed = urllib.parse.urlparse(UPSTREAM_BASE)
        upstream_path = parsed.path.rstrip("/") + path
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
        conn.request(method, upstream_path, body=body, headers=fwd_headers)
        return conn, conn.getresponse()
```

（重试上限 1 由线性结构构造性保证：`EMPTY_RETRY_MAX < 1` 时首次即 `final=True` 永不走 except；except 内第二次调用恒 `final=True`。）

### 2.9 Edit P9 — `_relay_sse` 两阶段重写 + 新方法 `_send_sse_headers`（锚点 :377-415）

OLD:
```python
    def _relay_sse(self, resp: http.client.HTTPResponse) -> int:
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            filtered = 0
            pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
            poisoned = False  # 当前 record 内是否命中毒行
            while True:
                line = resp.readline()
                if line in (b"\n", b"\r\n", b""):
                    # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                    if poisoned:
                        filtered += 1  # 整 record（含终结空行）丢弃，不损伤相邻字节
                        _record_poison_preview(b"".join(pending))
                    else:
                        for buf_line in pending:
                            self.wfile.write(buf_line)
                        if line:
                            self.wfile.write(line)
                        self.wfile.flush()
                    pending = []
                    poisoned = False
                    if line == b"":
                        break
                else:
                    if POISON_RE.match(line.rstrip(b"\r\n")):
                        poisoned = True
                    pending.append(line)
            return filtered
        finally:
            self.connection.settimeout(old_timeout)
```

NEW:
```python
    def _send_sse_headers(self, resp) -> None:
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> int:
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            filtered = 0
            pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
            poisoned = False  # 当前 record 内是否命中毒行
            primed = []       # priming 阶段已滤毒缓冲的完整 record 行（含终结空行）
            priming = True    # True = 客户端尚未收到任何字节
            while True:
                line = resp.readline()
                if line in (b"\n", b"\r\n", b""):
                    # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    if poisoned:
                        filtered += 1  # 整 record（含终结空行）丢弃，不损伤相邻字节（priming 期不进 primed）
                        _record_poison_preview(b"".join(pending))
                    elif priming:
                        primed.extend(pending)
                        if line:
                            primed.append(line)
                        if "content" in kinds or "done" in kinds:
                            # 首个信号 record：补发头 + 整段前缀，转 streaming
                            self._send_sse_headers(resp)
                            self.wfile.write(b"".join(primed))
                            self.wfile.flush()
                            priming = False
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
                            if final:  # 按现状语义收尾：缓冲原样下发（含合法 [DONE] 零内容流）
                                self._send_sse_headers(resp)
                                self.wfile.write(b"".join(primed))
                                self.wfile.flush()
                            else:
                                raise _EmptyStream(filtered, primed)
                        break
                else:
                    if POISON_RE.match(line.rstrip(b"\r\n")):
                        poisoned = True
                    pending.append(line)
            return filtered
        finally:
            self.connection.settimeout(old_timeout)
```

（字节序保证：转 streaming 后续 record 走原直写分支，客户端所见 = 前缀缓冲 + 信号 record + 后续，与非 priming 透传逐字节一致。priming 期间客户端断开在首次写（转 streaming 或 final flush）才撞 EPIPE → 照旧被 `_proxy` 的 ConnectionError 捕获记 499。）

### 2.10 Edit P10 — `_log` 加 `retried` 字段（锚点 :444-448）

OLD:
```python
    def _log(self, started: float, status: int, result: str, filtered: int, model=None) -> None:
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d model=%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-",
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

NEW:
```python
    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0) -> None:
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

（既有 :321/:357/:372 调用点靠默认值 retried=0，无需改。）

### 2.11 Edit P11 — `main()` 计数回装（锚点 :925-930）

OLD:
```python
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["daily"] = daily
        _stats_dirty = False
```

NEW:
```python
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["daily"] = daily
        _stats_dirty = False
```

（`stats_snapshot` 经 `dict(STATS)` 自动带出新字段，零改动。）

### 2.12 验证（green）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/empty-stream-retry && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 41 tests in xx.xxx s

OK
Exit code: 0
```

```
$ /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
Exit code: 0
```

任何用例红 → 贴原文诊断，最多修 3 轮；仍红 → STOP 报主代理。

### 2.13 commit（两条，顺序固定）

```
$ git status   # 自检：只含 ctyun-stream-fix-proxy.py 与 ctyun-stream-fix-proxy.test.py 两个改动文件
$ git add ctyun-stream-fix-proxy.test.py
$ git commit -m "test(proxy): 空流重试 9 用例 + 既有断言适配"
$ git add ctyun-stream-fix-proxy.py
$ git commit -m "feat(proxy): SSE 空流透明重试（priming 缓冲 + 判空重试一次 + 计数链）"
```

注：第一条 commit 单独检出时是 TDD red 中间态（新测试 + 旧实现），预期；**分支 tip 必须 41 绿**（2.12 已验证后才 commit）。commit 前自检：分支 = `fix/empty-stream-retry`，author 非 agent placeholder。

---

## Spec 覆盖对照

| spec 条目 | 落点 |
|---|---|
| A. 模块级新增（DONE_RE / EMPTY_RETRY_MAX / sse_data_line_kind / _EmptyStream） | Task 2 P1 |
| B. 计数链（STATS / save / load 四键、_DAILY_FIELDS、_record_request 两处形状、_record_empty_retry、main 回装；stats_snapshot 零改动） | Task 2 P2-P7, P11 |
| C. _proxy_relay（_open_upstream 抽取 + SSE 分支判空重试一次 + 502 语义保留） | Task 2 P8 |
| D. _relay_sse 两阶段 priming 重写 + _send_sse_headers 抽取 | Task 2 P9 |
| E. _log retried 字段（置 ts= 前，model=%s 子串习惯不破坏） | Task 2 P10 |
| 测试：SSE_REASONING 常量 | Task 1 T1 |
| 测试：FakeUpstreamHandler 四新属性 + do_POST 空流分支 + body_override | Task 1 T2, T3 |
| 测试：make_fake_upstream 扩展 + scripted calls 注入 | Task 1 T4 |
| 测试：test_sse_data_line_kind_matrix 矩阵单测 | Task 1 T5 |
| 测试：集成用例 ①-⑧ | Task 1 T9 |
| 测试：既有断言更新（persist roundtrip 两字典 / req_log 正则 / 两处 key 列表） | Task 1 T6, T7, T8 |
| 部署同步（~/.local/bin 复制 + cmp + launchd kickstart） | 不进卡（主代理 DELIVER） |

## Spec 偏差记录（PLAN-B 裁定，生产行为零偏差）

1. **do_POST 空流触发条件加 `and self.body_override is None`，且 `body_override` 为完整 body 替换**：spec 字面条件（`empty_stream or (calls is not None and len(calls) == 1)`）与其测试断言 ②③（`body_override=... → calls==1`）互斥——scripted 首呼必回空流 → 代理必重试 → calls==2；spec 一行式 `body = override if ... else SSE_A+SSE_B` 若保留既有后续 `body += SSE_DONE` 追加，override 流尾会多出一个 `[DONE]`，②③ 的逐字节断言必挂。以 spec 测试断言为准：body_override 场景首呼走正常路径且 override 即完整 body（不追加 poison/DONE）。仅影响测试假上游行为，生产代码与 spec 逐字一致。
2. **纯文字层面**（语义零变更）：`load_stats_counters` docstring "三零值"→"各键零值"（四键后原文失真）；`EMPTY_RETRY_MAX` 注释引用 ":41" 改写为 "惯例同 SEND_TIMEOUT_S"（行号漂移）；`_log` 格式串拆为两个相邻字面量（控行宽，拼接后与 spec 格式串逐字符一致）。
