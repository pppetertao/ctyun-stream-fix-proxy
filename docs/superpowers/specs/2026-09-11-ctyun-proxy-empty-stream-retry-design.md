# ctyun-proxy SSE 空流透明重试（v1.4）

背景：2026-09-10/11 溯源实证 deepseek-v4-pro-0813-oc 空返回签名 = 上游 200 + SSE，先吐少量 reasoning delta（<1k 字符）后流被掐断：无 `delta.content`、无 `tool_calls`、无 finish_reason、无 usage、无 `[DONE]`；ZCode SDK 视为正常完成（finishReason "other"），turn 空产出（同窗 95/344，28%）。本 episode 在代理侧做 priming 缓冲 + 空流判定 + 透明重试一次。代码 canonical = 项目目录（与 `~/.local/bin/` 运行副本 byte-identical），`/usr/bin/python3` 3.9.6，纯 stdlib，禁 3.10+ 语法。

## Goal

对上游 SSE 空流（全程无 content-bearing record 且无 `[DONE]` 即 EOF）在客户端未收到任何字节前用原 body 透明重试一次（env `CTYUN_EMPTY_RETRY`，默认 1，0=关闭），使 ZCode subagent 的 v4-pro 空返回在代理层自愈；全程可观测（`empty_retries_total` 计数链 + REQ 行 `retried=` 字段），毒行剥除与非 SSE/4xx/5xx/499 路径语义零变更。

## Files to Change

### 1. 编辑 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`

**A. 模块级新增（置于 `POISON_RE` :46 之后）**

```python
DONE_RE = re.compile(rb"^data:\s*\[DONE\]\s*$")
EMPTY_RETRY_MAX = int(os.environ.get("CTYUN_EMPTY_RETRY", "1"))  # seam 惯例同 :41


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
```

**B. 计数链（4 处既有锚点 + 1 个新函数）**

- `STATS` :53-54 加 `"empty_retries_total": 0`。
- `save_stats_counters` :140 与 `load_stats_counters` :170 的三键 tuple 均扩为 `("requests_total", "filtered_total", "errors_total", "empty_retries_total")`（旧持久化文件缺键 → :172 isinstance 兜底回 0）。
- `_DAILY_FIELDS` :176 追加 `"retries"`（`load_daily_buckets` :190-192 对缺字段旧桶回 0，向后兼容）。
- `_record_request` :244 by_model entry 初始化改 `{"requests": 0, "filtered": 0, "retries": 0}`；:248-250 daily `setdefault` 初值加 `"retries": 0`。**两处必须同步，否则后建的 entry/桶遇 `_record_empty_retry` 的 `entry["retries"] += 1` 直接 KeyError。** `_record_request` 本身不加参数（重试计数全部归新函数，请求计数不因重试翻倍）。
- 新增 `_record_empty_retry(model=None)`（置于 `_record_poison_preview` :263-268 之后，仿其 STATS_LOCK 模式）：`STATS["empty_retries_total"] += 1`；by_model entry（get-or-create，BY_MODEL_CAP 上限同 :243-245）`entry["retries"] += 1`；daily `setdefault(today_key(), 五键含 retries)` 后 `bucket["retries"] += 1`；置 `_stats_dirty = True`。
- `main()` :926-928 计数回装加 `STATS["empty_retries_total"] = counters["empty_retries_total"]`。`stats_snapshot` :271-281 经 `dict(STATS)` 自动带出新字段，零改动。

**C. `_proxy_relay` :328-375：连接抽取 + SSE 分支重试**

- :344-354 建连段抽为 `def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):`——urlparse + HTTP(S)Connection(timeout=UPSTREAM_TIMEOUT) + `conn.request` + `return conn, conn.getresponse()`，OSError/HTTPException 向上抛由调用方按 502 语义处置；原 :346-361 改为 try/except 包住 helper，502 分支逐字保留。
- SSE 分支 :365-369 替换为：

```python
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
```

（重试上限 1 由线性结构构造性保证；`EMPTY_RETRY_MAX < 1` 时首次即 `final=True`，永不走 except。`_relay_buffered` 分支 :370-374 与 :375 `conn.close()` 不动。）

**D. `_relay_sse` :377-415 两阶段重写 + 头发送抽取**

- :378-385 原样抽为 `def _send_sse_headers(self, resp) -> None:`（send_response + 跳头拷贝 + Connection: close + end_headers + `self.close_connection = True`）。
- 签名改 `def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> int:`；:386-387 settimeout 与 :414-415 finally 恢复照旧包住全函数。循环改：

```python
filtered = 0
pending = []
poisoned = False
primed = []      # priming 阶段已滤毒缓冲的完整 record 行（含终结空行）
priming = True   # True=客户端未收到任何字节
while True:
    line = resp.readline()
    if line in (b"\n", b"\r\n", b""):
        kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
        if poisoned:
            filtered += 1  # 毒行剥除两阶段照旧（含 priming：不进 primed）
            _record_poison_preview(b"".join(pending))
        elif priming:
            primed.extend(pending)
            if line:
                primed.append(line)
            if "content" in kinds or "done" in kinds:
                self._send_sse_headers(resp)  # 首个信号 record：补发头 + 整段前缀
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
```

字节序保证：转 streaming 后后续 record 走原直写分支，客户端所见 = 前缀缓冲 + 信号 record + 后续，与非 priming 透传逐字节一致。priming 期间客户端断开在首次写（转 streaming 或 final flush）才撞 EPIPE → 照旧被 :315 捕获记 499。

**E. `_log` :444-448**：签名加 `retried: int = 0`，格式串改 `"...filtered=%d model=%s retried=%d ts=%s"`（retried 置 ts= 前；`model=%s` 子串 grep 习惯不破坏）。既有 :321/:357/:372 调用点靠默认值 0，无需改。

### 2. 编辑 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`

- **常量区 :34 后**：`SSE_REASONING = b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}\n\n'`。
- **FakeUpstreamHandler :41-44 加类属性** `empty_stream = False`、`blank_stream = False`（空 200 零字节）、`body_override = None`、`calls = None`（共享 list 时按调用序出流：首次空流、后续正常）。`do_POST` 在 fail_500 块 :50-58 后插：`calls` 非 None 先 append 计数；`empty_stream or (calls is not None and len(calls) == 1)` → 回 200 SSE 头，`blank_stream` 则零字节否则写 `SSE_REASONING`，无 `[DONE]` 收尾，close_connection=True；正常路径 :62 的 body 构建改 `body = self.body_override if self.body_override is not None else SSE_A + SSE_B`。
- **`make_fake_upstream` :106-113**：签名加 `empty_stream=False, blank_stream=False, body_override=None, scripted=False`；scripted 时 type() 字典注入 `"calls": []`。
- **ProxyDashboardUnitTest 新增 `test_sse_data_line_kind_matrix`（test_extract_model :371-380 后）**：矩阵覆盖 content/tool_calls/finish_reason/[DONE]/注释行/data:null/空 delta/choices 空 usage 帧/非 JSON fail-open→content。
- **AdminIntegrationTest 新增 8 集成用例（:847-864 后，仿 :818-825 terminate-and-restart 模式）**：
  ① `test_empty_stream_retried_and_second_attempt_relayed`：scripted 上游 → `post_sse` 断言 `calls==2`、data 逐字节 `SSE_A+SSE_B+SSE_DONE`、stderr 含 `retried=1`；
  ② `test_priming_prefix_flushed_in_order_no_retry`：`body_override=SSE_REASONING+SSE_A+SSE_DONE` → `calls==1`、data 逐字节相等（缓冲前缀先 flush）；
  ③ `test_done_without_content_is_legal_no_retry`：`body_override=SSE_REASONING+SSE_DONE` → `calls==1`、data 逐字节相等；
  ④ `test_double_empty_stream_falls_back_after_two_calls`：`empty_stream=True` → `calls==2`、客户端收到 `SSE_REASONING`（attempt-2 缓冲按现状语义下发）；
  ⑤ `test_zero_record_empty_200_retried`：`empty_stream+blank_stream` → `calls==2`、data==b""；
  ⑥ `test_ctyun_empty_retry_zero_disables`：`empty_stream=True` + `extra_env={"CTYUN_EMPTY_RETRY":"0"}`（start_proxy :189-190 seam）→ `calls==1`、data==`SSE_REASONING`；
  ⑦ `test_empty_retry_counter_persists_and_resumes`：①场景后 SIGTERM 读 settings.json 断言 `stats.empty_retries_total>=1` 且当日桶 `retries>=1`；再以 `seed_persist={"stats":{"empty_retries_total":5,...}}` 重启（仿 :712-728）→ snap 为 5，再跑一次空流 → 6；
  ⑧ `test_by_model_retries_dimension`：①场景后 `/api/stats` 断言 `by_model["deepseek-v4-pro-0813-oc"]["retries"]>=1` 且 `requests==1`（重试不翻倍请求计数）、`snap["empty_retries_total"]>=1`。
- **既有用例更新（实测仅 3 处）**：`test_stats_persist_roundtrip_and_defaults` :387-392 两个 equality 字典各加 `"empty_retries_total": 0`；`test_req_log_line_has_ts_and_model` :744-748 正则在 `model=...` 与 `ts=` 间插 `retried=\d+ `；`test_stats_snapshot_shape` :421-424 与 `test_dashboard_and_stats_served` :613-616 的 key 断言列表各加 `"empty_retries_total"`。其余 29 用例零触碰（`filtered=1`/`result=aborted`/`model=-` 均子串断言，存活）。

### 3. 同步部署副本

- 项目目录两文件全绿后字节级复制到 `/Users/peter/.local/bin/`（同名两文件），`cmp` 双向验证 exit 0；运行副本生效需 launchd 重启（`launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`，dashboard :689 既有提示），由主代理执行。

## Acceptance Criteria

- ① `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（32 既有 + 9 新 = 41 用例），exit 0，贴 stdout 末尾。
- ② `/usr/bin/python3 -m py_compile` 两文件 exit 0。
- ③ 上述 ①-⑧ 八个场景用例逐一通过（含 retried= 字段、calls 次数、字节序、计数链断言）。
- ④ `cmp` 项目目录 vs `~/.local/bin/` 两文件均 exit 0。
- ⑤ `node ~/.zcode/scripts/validate-r31-evidence.mjs <本 spec>` exit 0。
- ⑥ 实机冒烟（重启后）：发一条经代理正常流请求，REQ 行含 `retried=0`，`/api/stats` JSON 含 `empty_retries_total` 键；dashboard 页面无需改动即可加载。

## Risks

- **RST 不重试**：上游 RST 掐断时 `resp.readline()` 抛 ConnectionResetError → 走 :315 499 路径不触发重试；仅干净 FIN/EOF 触发。实证签名（SDK 视为正常结束）为 FIN 形态，接受此局限。
- **entry/桶形状双初始化点**：`_record_request` :244/:248-250 与 `_record_empty_retry` 若不同步加 `retries` 键即 KeyError——本 spec 已双写，①⑧用例兜底。
- **priming 延迟**：reasoning 字节整段延后到首个 content record 才可见；本代理流量全部为 ZCode subagent 非交互调用，用户已知悉接受。
- **重试放大**：上限 1 次、仅空流签名触发（同窗实测 28% 且特定 v4-pro 路径），上游压力增幅有界。
- **JSON 解析失败 fail-open 归 content**：畸形 data 行不触发重试（宁漏不误）；reasoning_content 键名变化等上游演进由 fail-open 兜底不误伤。
- **choices 为空的 usage-only 流**（无 [DONE] 即 EOF）会被判空重试——罕见且重试无害。attempt-1 已滤毒计数随缓冲丢弃，`filtered` 只计交付流（口径：剥行数对齐客户端所见字节）。
- **回归护栏**：不碰 stderr drain/kill_registered 基建（:123-156、:548-584）、`_relay_buffered`、`_reply_502`、499 路径；①全量 41 绿兜底。

## Exclusions

- dashboard UI 改版（新字段由 stats JSON 自动带出，页面照旧轮询）。
- 非 SSE（buffered）响应重试、上游 5xx/建连失败重试（502 语义照旧）、重试次数 >1。
- glm-5.3-oc 路径的任何差异化处理；ZCode SDK 侧改动。
- 499 abort 路径的 model/retried 归因改造（仍 model=- retried=0 默认）。
- 持久化格式迁移/版本号（旧文件缺键回 0 即可）；launchd plist、鉴权、剥行正则、上游热切换逻辑均不动。

## R31 Evidence

[R31-S1] 问题现场命中（2026-09-11 Read 全文实测）：`_relay_sse` 逐 record 直写客户端、无缓冲判定、`_log` 无 retried 字段——
```
$ grep -n 'def _proxy_relay\|def _relay_sse\|def _log\|POISON_RE = ' ctyun-stream-fix-proxy.py
46:POISON_RE = re.compile(rb"^data:\s*null\s*$")
328:    def _proxy_relay(self, started: float) -> None:
377:    def _relay_sse(self, resp: http.client.HTTPResponse) -> int:
444:    def _log(self, started: float, status: int, result: str, filtered: int, model=None) -> None:
$ grep -n '"requests_total", "filtered_total", "errors_total"' ctyun-stream-fix-proxy.py
140:        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total")}
170:    for key in ("requests_total", "filtered_total", "errors_total"):
```
[R31-S2] 根因：空流签名（reasoning delta 后 EOF 无 [DONE]）在 `_relay_sse` :392-408 逐字节直写语义下与正常流不可区分，客户端必然收到 200+截断流；修复面 = 两阶段 priming（D）+ 重试编排（C）+ 计数链（B）+ 日志字段（E）。baseline = 全量 32 用例（grep 实测）——
```
$ grep -c 'def test_' ctyun-stream-fix-proxy.test.py
32
$ grep -n 'filtered=%d model=%s' ctyun-stream-fix-proxy.py
445:        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d model=%s ts=%s"
```
