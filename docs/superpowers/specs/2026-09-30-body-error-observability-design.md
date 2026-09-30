# 200 包错误体观测盲区修复设计（body-error observability）

## Goal

让代理对「HTTP 200 但 body 内嵌 JSON 顶层 `error` 对象」的上游响应（如天翼云 TPM 限流）可见、可计数、可留痕：新增 `body-err` 结果分类接入现有 REQ 日志 / ERROR_EVENTS 留痕 / /api/stats 计数 / dashboard 展示管线，替代当前 classify_outcome 只按 status<400 判 ok 导致的完全盲区（ctyun-stream-fix-proxy.py:155-158）。

## Files to Change

- `ctyun-stream-fix-proxy.py:140 classify_outcome(status=None, synth_502=False, client_abort=False, eof_without_done=False, poison_filtered=0, body_error=False) -> _Outcome` — 加 `body_error` 关键字参数（末位追加保持向后兼容）；优先级链改为 client_abort > synth_502 > body_error > eof_without_done > status；新分支返回 `_Outcome(CLASS_BODY_ERROR, "body-err", True, True)`（counts_error=True：透传失败响应=代理侧可见错误，与 synth_502 口径一致；capture=True）。
- `ctyun-stream-fix-proxy.py:62-74` 常量区 — 新增 `CLASS_BODY_ERROR = "body_error"` 与 `ERR_KIND_BODY_ERROR = "body_error"`，并在 `_KIND_CATEGORY` dict（:76-84）加 `ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR` 映射。命名决策：`body-err`（log_result）/`body_error`（kind/class）与现有 `eof-without-done`/`eof_without_done`、`upstream-err`/`upstream_5xx` 的双面命名风格一致。
- `ctyun-stream-fix-proxy.py:87 sse_data_line_kind` 之后新增顶层函数 `body_has_error(parsed) -> bool` — 纯函数：`isinstance(parsed, dict)` 且 `isinstance(parsed.get("error"), dict)` → True；其余（非 dict、error 键缺失、error 为 str/null/list）→ False。只认 JSON 顶层 `"error"` 键且值必须为对象（OpenAI 错误 schema），content 文本里出现 "error" 字样不误判（不做字符串匹配）。
- `ctyun-stream-fix-proxy.py:121 sse_line_has_usage` 之后新增顶层函数 `sse_line_body_error(line: bytes) -> bool` — 行级检测：rstrip → 非 `data:` 前缀或 DONE_RE 命中 → False；`json.loads(stripped[5:].strip().decode("utf-8", "replace"))` ValueError → False（fail-open 同 :99-102）；否则 `return body_has_error(data)`。解析模式逐行复用 `sse_line_has_usage`（:121-134）。
- `ctyun-stream-fix-proxy.py:1000 _relay_sse(self, resp, final) -> tuple` — 返回值由 `(filtered, truncated)` 扩为 `(filtered, truncated, body_error)`：局部变量 `body_err_line = None`（bytes，首条命中行）；在 record 终结块（:1025 `kinds = [sse_data_line_kind(buf_line) for buf_line in pending]` 之后）加 `if body_err_line is None: body_err_line = next((l for l in pending if sse_line_body_error(l)), None)`；return 语句（:1079）改为 `return filtered, truncated, body_err_line is not None`；新增模块级 `_BODY_ERR_LINE = None` 配合（见下一条：用实例属性而非返回值携带原始行，避免改 9 处调用签名）。简化为：命中行存 `self._body_err_line`（bytes），`_relay_sse` 返回签名不变；`_proxy_relay` 在 `_relay_sse` 返回后读 `self._body_err_line`（BaseHTTPRequestHandler 每请求独立实例，无并发问题）。
- `ctyun-stream-fix-proxy.py:903-954 _proxy_relay` SSE 分支 — 两处 `_relay_sse` 调用点（:903、:928）前重置 `self._body_err_line = None`；调用后取 `body_error = self._body_err_line is not None`；`classify_outcome(...)` 调用（:929、:935 eof 覆盖）传 `body_error=body_error`；`outcome.capture` 分支（:937-949）加 `if body_error: kind = ERR_KIND_BODY_ERROR` 优先于 poison/eof/status 映射，且 `record_error_event(kind, model=model, path=self.path, body=body, response=self._body_err_line, filtered=filtered)`（命中行原始字节 ≤4096 截断由 `record_error_event` 内 `_snapshot_text(response, RESPONSE_SNIPPET_CAP)` 承载，response cap 2048 够用）。**决策：body 错误帧原样透传给客户端**（不经 poison 剥除路径）——error 帧是上游的合法语义响应，客户端/App 侧需自行处理（事故中 App 侧网关已能解析并报 -32603）；代理职责是观测+分类，不篡改。
- `ctyun-stream-fix-proxy.py:955-969 _proxy_relay` 非流式分支 — `_relay_buffered(resp)` 返回后加：`body_error = body_has_error(json.loads(relayed_data.decode("utf-8","replace"))) if relayed_data else False`（json.loads ValueError 视为 False，非 JSON body 如 GET /plain 不受影响）；`classify_outcome(status=resp.status)`（:957）传 `body_error=body_error`；capture 分支加 `if body_error: kind = ERR_KIND_BODY_ERROR, response=relayed_data`（现有 `response=relayed_data[:RESPONSE_SNIPPET_CAP] if resp.status >= 500 else None` 改为 body_error 时也带）。
- `ctyun-stream-fix-proxy.py:645 _record_request(method, path, status, dur_ms, filtered, model=None, error=False)` — 无签名改动：body-err 走 `error=outcome.counts_error`=True → 计入 `errors_total` 与 daily `errors_proxy` 桶（与 synth_502 同口径：body-err 是代理"看见"的错误，upstream status 仍 200 不计 errors_upstream）。EVENTS 追加 `{"kind":"proxy"}`（现有 error=True 分支 :681 自动覆盖，无需改动）。**决策：计数归因 errors_proxy**——上游 HTTP 层无故障（200 正常返回），是代理观测能力发现的业务层错误，归 proxy 桶与 synth_502（代理合成 502）语义一致。
- `ctyun-stream-fix-proxy.py:1384-1413 DASHBOARD_HTML` — 最近请求表 `renderRecent`（JS :1563）无需改动（`result` 字段不进该表）；dashboard 改动最小化：`renderDaily`/`renderDailyByModel` 的 errors_proxy 列（:1637、:1677）自动涵盖 body-err（同 errors_proxy 桶）；**无新增展示列**——footer 说明文案（:1417-1423）追加一句「代理错误含 body 内嵌错误（HTTP 200 但 body 顶层 error 对象）」。
- `ctyun-stream-fix-proxy.test.py`（函数级锚点） — 新增测试类 `BodyErrorTest`（对齐既有 `ProxyLifecycleTestCase`/`AdminIntegrationTest` 模式）：
  - `test_sse_body_error_classified_and_logged`：`make_scripted_upstream(body_override=SSE_A + b'data: {"error":{"message":"模型请求 TPM 超限","type":"rate_limit_error","code":"model_tpm_limit"}}\n\n' + SSE_DONE)` → `post_sse` → stderr 含 `result=body-err`；/api/stats `errors_total` +1。
  - `test_sse_body_error_recorded`：capture_errors on → /api/errors 含 `kind=body_error`、`category=body_error`、response 含命中行。
  - `test_sse_content_mentioning_error_not_flagged`：`body_override=SSE_A + b'data: {"choices":[{"delta":{"content":"error occurred"}}]}\n\n' + SSE_DONE` → result=ok（不误判字符串）。
  - `test_buffered_body_error`：非流式 fake upstream 200 + JSON body `{"error":{"message":"x","type":"t","code":"c"}}`（新 `FakeUpstreamHandler` 属性 `fail_200_error=False` 或 `body_override` 复用——锚点 `ctyun-stream-fix-proxy.test.py:52 fail_500` 旁加 `fail_200_error` 属性，do_POST 分支回 200 + error JSON + `Content-Type: application/json`）→ result=body-err、errors_total +1。
  - `test_body_error_no_retry`：scripted upstream 首呼 error 帧 → `len(calls)==1`（不触发空流重试链路，error 帧属 content-bearing record，`sse_data_line_kind` 返回 "noise"——dict 无 choices 键 :105-107，priming 不被误推重试；需确认：error record 无 choices → noise → 若整流仅 error 一帧则 `_EmptyStream` 会触发重试。**决策：error 帧流不重试**——TPM 限流短期重试无意义（配额窗口未过），且错误体本身是完整语义响应非空流；通过让 `sse_data_line_kind` 对顶层 error dict 返回 "content"（新增分支在 :104 `if not isinstance(choices, list) or not choices:` 之前：`if body_has_error(data): return "content"`）实现——error 帧视为 content 触发 priming flush、不触发空流重试，同时 body_has_error 检测独立生效。此为关键集成点，spec 锚点 `ctyun-stream-fix-proxy.py:104`）。
  - `test_body_has_error_matrix`：纯函数单测（dict error / str error / 缺失 / 非 dict / error 内嵌 choices）。

## Acceptance Criteria

- `python3 ctyun-stream-fix-proxy.test.py` 全绿（含新增 BodyErrorTest ≥6 用例）。
- fake upstream 回 200 + SSE error 帧 → 代理 stderr REQ 行 `result=body-err`，/api/stats `errors_total` 与当日桶 `errors_proxy` 各 +1，/api/errors（capture on）含 `kind=body_error` 且 `?id=` 详情带 response 命中行（≤2048 字符截断标记存在）。
- fake upstream 回 200 + 非流式 JSON error body → 同上断言（result=body-err）。
- error 帧流不触发空流重试（calls==1），客户端收到原样 error 帧字节（透传不剥除）。
- content 文本含 "error" 字样的正常流 → result=ok，零误判。
- 现有回归锁死：`test_classify_outcome_full_matrix`（:1441）与 `test_header_timeout_kind_category_and_classify_regression`（:1774）矩阵不断言 body_error 默认值改变既有结果——`classify_outcome(status=200)` 仍 `(CLASS_OK, "ok", False, False)`。

## Risks

- **`sse_data_line_kind` 加 error→"content" 分支的回归面**：该函数是 priming/重试/finish-hold 三路判定的核心（:1025），error 帧被判 content 后会触发 `_flush_primed`（:1047-1049）提前结束 priming——这正是目标行为（error 帧要立即透传+不重试），但若上游先 error 帧后又续传正常 content（异常混合流），客户端会先收到 error 再收到 content；判定为可接受（上游行为本身异常，fail-open 透传优先）。测试锚点 `test_sse_data_line_kind_matrix`（:675）需加 error 帧断言锁死。
- **性能**：SSE 行级每行已有一次 json.loads（sse_data_line_kind :99）；新增 `sse_line_body_error` 若每行再 loads 一次则双倍解析。缓解：只在 record 终结块（:1025 每 record 一次而非每行）遍历 pending 调 `sse_line_body_error`，且仅 priming 阶段或 `body_err_line is None` 时执行（命中后短路）；实测 1.2MB big 流（`test_client_abort_is_quiet_and_not_error` :2179 场景）不得显著变慢。实现建议：kinds 计算同循环内顺带检测（implementer 决定合并遍历还是独立 next()，可接受方案为每 record ≤1 次全 pending 遍历）。
- **部署副本同源**：生产副本 `~/.local/bin/ctyun-stream-fix-proxy.py` 与仓库源需同步（本仓库无自动部署），DELIVER 后需人工 cp 并 `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`（属 Exclusions 之外的部署动作，spec 不覆盖）。
- **error 帧 JSON 超大**：单行 >4096 的 error 帧命中行存 `self._body_err_line` 全量 bytes，record_error_event 内 `_snapshot_text(response, 2048)` 截断（:216），内存风险有界（单行上限受上游 SSE 帧大小约束，现有 PRIMED_TAIL_CAP=262144 :53 管 priming 缓冲）。

## Exclusions

- 不对 body-err 做自动重试（TPM 限流重试无意义；其他 body 错误类型如 model_not_found 同为确定性错误，重试必败——重试语义属于"上游暂时性故障"（空流/头超时/连接重置），body 错误是上游明确业务响应）。
- 不新增 daily 桶字段（`_DAILY_FIELDS` :523 不动）与 STATS 总量键——body-err 复用 `errors_total`/`errors_proxy` 现有桶。
- 不改 EVENTS 事件流 kind 集合（"proxy"/"upstream"/"retry" :681）——body-err 走 error=True → "proxy" 现有分支。
- 不改 dashboard 表格列结构——仅 footer 文案一行。
- 不处理嵌套 error（如 `{"choices":[{"delta":{"content":"{\"error\":...}"}}]}`）与非 dict error 值（`{"error":"str"}`）——只认顶层 dict 形态（OpenAI/天翼云实际错误 schema）。
- 不修改生产部署副本（`~/.local/bin/`）与 launchd 配置。
- 新增 followups（若有）记 `docs/superpowers/followups.md`，不进本 spec。

## R31 Evidence

[R31-S1] 问题存在：现场日志证实 200 无错误标记，客户端却收到限流 429

现场日志（`~/.local/log/ctyun-fwd.err`）中 2026-09-30 10:23 时段 kimi-k3-oc 的请求全部记录为 `-> 200 result=ok`，与 App 侧同期收到的 `model_tpm_limit` 限流错误矛盾：

```
$ grep -nE 'ts=2026-09-30T10:2[234]' ~/.local/log/ctyun-fwd.err
23163:REQ POST /chat/completions -> 200 dur=15.9s result=ok filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- ts=2026-09-30T10:23:00+0800
23164:REQ POST /chat/completions -> 200 dur=1.9s result=ok filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- ts=2026-09-30T10:23:02+0800
23165:REQ POST /v1/chat/completions/control -> 404 dur=0.2s result=upstream-err filtered=0 model=- retried=0 retry_reason=- exc=- ts=2026-09-30T10:23:03+0800
```

全日志中代理从未记录过 `-> 429`——错误只存在于 body 内，HTTP 层完全不可见：

```
$ grep -cE '\-> 429' ~/.local/log/ctyun-fwd.err
0
```

App 侧实际收到的错误体（HTTP 200 内嵌返回）：

```
{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试","type":"rate_limit_error","code":"model_tpm_limit"}}
```

[R31-S2] 根因：classify_outcome 仅按 HTTP status 分类，body 错误体永不进入观测管线

`classify_outcome` 的 `status < 400` 分支恒返回 `CLASS_OK / "ok" / counts_error=False / capture=False`，没有任何 body 检查入口；`record_error_event` 在 `CAPTURE_ERRORS=False` 时于函数首行直接 return。二者叠加导致 body 内嵌错误既无分类、无留痕、无计数：

```
$ sed -n '144,163p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
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
        return _Outcome(CLASS_OK, "ok", False, False)   # <- 200 一律 ok，不查 body
    if 400 <= status < 500:
        return _Outcome(CLASS_REQUEST_FAULT, "upstream-err", False, True)
    # status >= 500
    return _Outcome(CLASS_UPSTREAM_FAULT, "upstream-err", False, True)
```

`record_error_event` 的 `CAPTURE_ERRORS=False` 早退（:197-198）：

```
$ sed -n '197,198p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
    if not CAPTURE_ERRORS:
        return
```
