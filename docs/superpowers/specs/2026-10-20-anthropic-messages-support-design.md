# Anthropic Messages API 支持与默认上游切换

## Goal

让代理对 Anthropic Messages API（默认上游 `https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic`）的流式/非流式请求零改写透传，同时正确实时 flush、统计 usage、按 key 限流；OpenAI `/v1/chat/completions` 行为逐字节不变。

## 方案对比与推荐

| 方案 | 做法 | 代价 | 结论 |
|---|---|---|---|
| A 边界归一化（推荐） | 事件类型表识别 Anthropic 帧；usage 在解析边界归一为 OpenAI 键，复用既有统计/TPM 结算链路 | 3 个纯函数 + 2 处赋值点 | 推荐 |
| B 协议转换代理 | 把 Anthropic 帧重写为 OpenAI 帧下发 | 破坏字节透传与 prompt cache，tool_use 语义失真，客户端协议也错 | 拒绝 |
| C 仅 flush 不归一 | 只让 Anthropic 事件触发 priming flush | usage/TPM 仍记 0，验收不过 | 拒绝 |

推荐 A：转发层保持 byte-identical，只在"判定与统计"边界归一。

## Files to Change

### 1. `ctyun-stream-fix-proxy.py:32` `DEFAULT_UPSTREAM_BASE`

改为 `https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic`。同步 `:7` 模块 docstring 生产默认 URL、`:3820` dashboard `placeholder`。解析链不变：`resolve_upstream_base()` :1579（env > file > default）、`set_upstream_base()` :2411（POST /api/config 热切 + 持久化）。

### 2. `ctyun-stream-fix-proxy.py:259` `sse_data_line_kind()` — Anthropic 事件类型表

在 `_SSE_NO_PARSED` 附近新增常量；在 choices 分支之后、返回 `"noise"` 之前回退查表：

```python
_ANTHROPIC_EVENT_KIND = {
    "message_start": "content", "content_block_start": "content",
    "content_block_delta": "content", "content_block_stop": "noise",
    "message_delta": "finish", "message_stop": "done",
    "error": "content", "ping": "noise",
}
```

```python
choices = data.get("choices")
if isinstance(choices, list) and choices:
    # 原 OpenAI 判定体原样保留（content > tool_calls > finish > noise）
    return "noise"
etype = data.get("type")
if isinstance(etype, str) and etype in _ANTHROPIC_EVENT_KIND:
    return _ANTHROPIC_EVENT_KIND[etype]
return "noise"
```

边界：`body_has_error(data)` (:285) 仍最优先，Anthropic error 帧（`{"type":"error","error":{...}}`）继续走 content；choices 非空时 OpenAI 路径完全不变。`message_start`→content 使 priming 在首帧即 `_flush_primed()` (:3291)，`message_stop`→done 使 `saw_done`/截断判定正确，流不再缓冲到 EOF，也不触发 `_EmptyStream` 重放。

### 3. `ctyun-stream-fix-proxy.py:317` `sse_line_has_usage()` / `:340` `sse_line_usage()` — Anthropic usage 归一

新增模块级函数：

```python
def anthropic_usage_dict(data):
    if not isinstance(data, dict):
        return None
    etype = data.get("type")
    if etype == "message_start":
        message = data.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
    elif etype == "message_delta":
        usage = data.get("usage")
    else:
        return None
    if not isinstance(usage, dict) or not usage:
        return None
    out = {}
    for src, dst in (("input_tokens", "prompt_tokens"),
                     ("output_tokens", "completion_tokens")):
        value = usage.get(src)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[dst] = value
    cache_read = usage.get("cache_read_input_tokens")
    if isinstance(cache_read, int) and not isinstance(cache_read, bool) and cache_read >= 0:
        out["cache_read_input_tokens"] = cache_read
    return out or None
```

`sse_line_usage()` 在 `isinstance(data, dict)` 之后、取 `data.get("usage")` 之前插入：

```python
    anth = anthropic_usage_dict(data)
    if anth is not None:
        return anth
```

`sse_line_has_usage()` 同位置插入：

```python
    if anthropic_usage_dict(data) is not None:
        return True
```

必须先于顶层 `usage` 分支：`message_delta` 的 usage 原生是 Anthropic 键，先走原分支会把 `output_tokens` 原样返回，合并时无法覆盖 `completion_tokens`。非 Anthropic 输入返回原 dict/原语义不变。非流式 Anthropic body 的顶层 `usage` 走既有分支，由第 4 项别名兜底。

### 4. `ctyun-stream-fix-proxy.py:367` `usage_dict_tokens()` — alias + total 兜底

在 `return` 前改为：

```python
prompt = _int_field("prompt_tokens") or _int_field("input_tokens")
completion = _int_field("completion_tokens") or _int_field("output_tokens")
total = _int_field("total_tokens")
if total == 0 and (prompt or completion):
    total = prompt + completion
return (prompt, completion, total, cache_read, reasoning)
```

`cache_read_input_tokens` 已是既有第二别名，无需改。

### 5. `ctyun-stream-fix-proxy.py:3260` `_relay_sse()` — usage 按键合并

新增模块级纯函数：

```python
def usage_merge(acc, new):
    if not isinstance(new, dict) or not new:
        return acc
    merged = dict(acc) if isinstance(acc, dict) else {}
    merged.update(new)
    return merged
```

捕获点改为：

```python
usage_hit = sse_line_usage(buf_line, parsed=pre_parsed[i])
if usage_hit is not None:
    self._tpm_usage = usage_merge(self._tpm_usage, usage_hit)
    self._p3_usage_tokens = usage_dict_tokens(self._tpm_usage)
```

`message_start` 提供 input_tokens，`message_delta` 只提供 output_tokens，合并后两者都保留；OpenAI 整帧覆盖语义不变。

### 6. `ctyun-stream-fix-proxy.py:3065` / `:3122` — TPM settle 用归一 total

流式（:3065-3068）：

```python
tokens = usage_dict_tokens(self._tpm_usage) if self._tpm_usage is not None else None
if tpm_key is not None and tokens is not None:
    tpm_settle(tpm_key, tpm_est, tokens[2], model)
    tpm_final_used = tokens[2]
```

非流式（:3117-3125）：`p3_buf_tokens = usage_dict_tokens(usage)` 之后用 `p3_buf_tokens[2]` 结算，不再直接 `usage.get("total_tokens")`。

### 7. `ctyun-stream-fix-proxy.py:518` `tpm_key_id()` — x-api-key

```python
def tpm_key_id(auth_header, api_key_header=None):
    token = api_key_header.strip() if isinstance(api_key_header, str) else ""
    if not token and auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
```

调用点 :2892 改 `tpm_key_id(self.headers.get("Authorization"), self.headers.get("x-api-key"))`（email.message 头查大小写不敏感）。优先级 **x-api-key > Bearer**：x-api-key 是 Anthropic 原生计费凭证，网关 Bearer 可能多客户端共享，优先 Bearer 会让不同 x-api-key 撞同桶；空/非 str x-api-key 回落 Bearer。头原样透传已由 :2914-2923 保证（`x-api-key`/`anthropic-version` 不在 `STRIP_HEADERS` :1492）。

### 8. `ctyun-stream-fix-proxy.test.py`

- `:892` `test_sse_data_line_kind_matrix()` — 追加 8 个 Anthropic type 断言（含 `event:` 行 noise、body error 仍 content）。
- `:1007` `test_sse_line_usage_matrix()` — 追加 message_start/message_delta/空 usage。
- `:1143` `test_tpm_key_id()` — x-api-key 确定性、双头优先级、空值回落。
- `:5598` `UsageExtractTest` — `input_tokens/output_tokens` 别名与 total 兜底。
- `:6255` `RelaySseV3Test` 之后新增 `AnthropicSseTest`：复用 `_StubHandler`/`_FakeSseResp` (:6265-6321) 做"第二次 readline 时 wfile 已非空"的 flush 实时性断言；集成用 `make_scripted_upstream(body_override=...)` (:336) + `start_proxy` (:491) + `post_sse` (:525) 断言字节一致与 `calls == 1`。

## Acceptance Criteria

1. `python3 ctyun-stream-fix-proxy.test.py` 全绿（现有 OpenAI 用例零修改通过）。
2. `sse_data_line_kind`：message_start/content_block_start/content_block_delta/error → `content`；message_delta → `finish`；message_stop → `done`；content_block_stop/ping → `noise`。
3. `AnthropicSseTest` flush 举证：fake resp 在返回第二行前断言 `handler.wfile.getvalue()` 已包含 message_start 记录（证明未缓冲到 EOF）。
4. 集成：Anthropic SSE 输出与上游逐字节相同，上游 `calls == 1`（未触发 empty-retry），`filtered=0`。
5. usage：message_start(input=10)+message_delta(output=7) → daily `tokens_prompt=10`、`tokens_completion=7`、total=17；启用模型时 `tpm_settle` 用 17。
6. `tpm_key_id("Bearer b","k")==tpm_key_id(None,"k")` 且 ≠ `tpm_key_id("Bearer b")`；`tpm_key_id("Bearer b","")==tpm_key_id("Bearer b")`。
7. `DEFAULT_UPSTREAM_BASE == "https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic"`，env/file/api 优先级回归不变。
8. 非流式 Anthropic JSON 响应 body 与 `Content-Length` 逐字节原样，usage 正确入统计。

## Risks

- 已持久化的旧 base 优先于新默认（file > default），部署后不自动切换；需 POST /api/config 或清 `upstream_base` 键。
- Anthropic base 下 `/v1/chat/completions` 不存在；`_calibrate_send` :1279-1280 也硬拼该路径，默认部署的校准功能会 404（另开 episode）。
- message_start 即 flush 后流不可重放：上游中途断流只能标 `eof_without_done`，客户端收到半流（实时性的代价）。
- `usage_merge` 把 OpenAI 多 usage 帧从"整帧替换"改为"按键合并"；last-wins 等价，但 total 缺失时新兜底 prompt+completion，会改变历史 total=0 口径。
- x-api-key 优先：同一 x-api-key 配不同 Bearer 会合并计费（预期）；若运营把 x-api-key 当共享网关 key，配额会分散到各客户端。
- 429/502 合成错误体仍是 OpenAI schema，Anthropic SDK 可能解析 body 失败（status 语义不受影响）。
- fake upstream 若未回 `text/event-stream`，Anthropic 流会走 buffered 分支；集成用例必须显式设 SSE 头。

## Exclusions

- 不做 Anthropic→OpenAI 帧/body 转换、tool_use/thinking 专用解析（帧原样透传）。
- 不改 `normalize_null_assistant_content` :1611 与非流式透传语义。
- 不改统计 schema/admin UI/daily 字段形状；不加 env seam；不加依赖。
- 不统计 `cache_creation_input_tokens`（cache_write 沿用既有排除）。
- 不实现 Anthropic 校准与 Anthropic 专用 429 错误体。
- 不做路径改写：默认 base 下 OpenAI 路径不可达，需显式切 base。

## R31 Evidence

[R31-S1] 锚点行号实测（Grep 回显）

```
$ grep -n 'DEFAULT_UPSTREAM_BASE|def sse_data_line_kind|def sse_line_has_usage|def sse_line_usage' ctyun-stream-fix-proxy.py
32:DEFAULT_UPSTREAM_BASE = "https://eaichat.ctyun.cn/ai/platform/v2/cp"
259:def sse_data_line_kind(line: bytes, parsed=_SSE_NO_PARSED) -> str:
317:def sse_line_has_usage(line: bytes, parsed=_SSE_NO_PARSED) -> bool:
340:def sse_line_usage(line: bytes, parsed=_SSE_NO_PARSED):

$ grep -n 'def usage_dict_tokens|def tpm_key_id|def _relay_sse|def _open_upstream|def resolve_upstream_base|def set_upstream_base' ctyun-stream-fix-proxy.py
367:def usage_dict_tokens(usage) -> tuple:
518:def tpm_key_id(auth_header):
1579:def resolve_upstream_base(env_base: str, persist_path: str) -> tuple:
2411:def set_upstream_base(base: str) -> None:
3168:    def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):
3199:    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool,

$ grep -n 'tpm_key_id(self.headers|self._tpm_usage = usage_hit|usage.get("total_tokens")' ctyun-stream-fix-proxy.py
2892:        tpm_key = tpm_key_id(self.headers.get("Authorization"))
3065:                total = self._tpm_usage.get("total_tokens")
3122:                                total = usage.get("total_tokens")
3260:                            self._tpm_usage = usage_hit
```

[R31-S2] 根因定位

- 事件分类：`sse_data_line_kind` :259 只认 `choices[0].delta.content/tool_calls/finish_reason`，Anthropic 帧无 choices → :288-289 一律 `noise`；priming 仅在 :3291 `"content" in kinds or "done" in kinds` 时 flush，故 Anthropic 整流缓冲到 EOF，继而 :3331 抛 `_EmptyStream` 并重放一次（`EMPTY_RETRY_MAX=1` :59）。
- usage：`sse_line_usage` :340 只返回顶层 `usage`，而 message_start 的 usage 嵌套在 `message["usage"]`；`usage_dict_tokens` :367 只读 prompt/completion/total，故 Anthropic 的 input/output_tokens 记 0，daily 与 TPM settle（:3065/:3122）均失效。
- 限流：`tpm_key_id` :518 只解析 `Bearer `，x-api-key 客户端 `tpm_key=None` 直接绕过 :2892 准入。
- 端点：`DEFAULT_UPSTREAM_BASE` :32 仍指向 `/cp`；grep `anthropic|x-api-key` 在生产文件零命中，确认无既有 Anthropic 代码可复用或需保护。
