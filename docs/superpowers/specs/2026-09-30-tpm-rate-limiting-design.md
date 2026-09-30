# TPM 客户端侧限流设计（ctyun-stream-fix-proxy）

## Goal

为本机转发代理（7920 透传 / 7921 管理台）加按 API key 分桶的 60s 滚动窗口 TPM 限流（预算 110k tokens），超预算 FIFO 排队（上限 20、超时 120s 回仿上游 429），消除上游 `model_tpm_limit` 类事故，服务所有经本代理访问天翼云上游的客户端。

## 设计决策（定死）

1. **准入估算**：`estimate_request_tokens(body)` = `max(1, int(len(body) * TPM_TOKEN_RATIO))`；body 可解析为 JSON 且含 int `max_tokens` 时再累加该值（输出预留）。比率默认 0.55（实测中文 0.56 tok/char 吻合）。非 JSON body 按裸长度估，fail-open。
2. **窗口结构**：每 key 一个 `collections.deque[(ts, tokens)]` + 增量 `used` 计数；准入/settle 时 prune 掉 `now - 60s` 之前条目。settle 校正以追加 `(now, delta)` 条目实现（delta 可为负），`used` 钳制 ≥0。`TPM_BUCKETS` dict 上限 `TPM_KEY_CAP=64`，仅当桶窗口空且无排队时惰性驱逐。
3. **排队**：单个 `threading.Condition`（`TPM_LOCK`）护 `TPM_BUCKETS` + 全局 FIFO `TPM_WAITERS`（deque of `_TpmWaiter(key_id, est)`）。准入与入队同在锁内 → 原子。仅队首 waiter 可被准入（严格 FIFO，防惊群）；wait 循环 `TPM_LOCK.wait(timeout=min(1.0, remaining))` + deadline 检查，窗口过期靠 1s 粒度轮询，settle 时 `notify_all()`。排队期间不持有任何上游连接（hook 点在 `_open_upstream` 之前）。`est > TPM_LIMIT` 单请求直接拒（永远等不到）。
4. **429 合成**：新增 `_reply_tpm_429(reason)`，镜像 `_reply_502`（:1161）模式：status 429 + `Content-Type: application/json` + 定死 body `{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试","type":"rate_limit_error","code":"model_tpm_limit"}}` + `close_connection=True`。日志 result 用新值 `tpm-queue-full` / `tpm-queue-timeout`；**不改 `classify_outcome` 优先级链**（429 路径无上游 status 可分类，与 synth_502 一样在调用点直接记）；新增 `ERR_KIND_TPM_QUEUE_FULL` / `ERR_KIND_TPM_QUEUE_TIMEOUT` 入 `_KIND_CATEGORY`（:79），映射到新 `CLASS_TPM_LIMITED = "tpm_limited"`；`_record_request(..., status=429, error=True)`（与 synth_502 同口径计代理错误）。
5. **观测端点**：挂 7921 管理台 `AdminHandler.do_GET`（:1194 `/api/stats` 分支旁），路径 `/api/tpm_stats`（遵循 /api/* 惯例，即需求所称 tpm_stats 端点）。返回 `{"config":{"limit":N,"window_s":N,"queue_max":N},"queue_total":N,"buckets":[{"key":"sha256:ab12cd34…","used":N,"remaining":N,"queued":N,"rejected":N,"timeouts":N}]}`；key 只显 sha256 hex 前 12 位。dashboard HTML 不动（最小化）。

## Files to Change

- `ctyun-stream-fix-proxy.py:51`（常量区，`EMPTY_RETRY_MAX` 旁）— 新增 `TPM_LIMIT`（env `CTYUN_TPM_LIMIT` 默认 110000）、`TPM_WINDOW_S`（60）、`TPM_QUEUE_MAX`（20）、`TPM_QUEUE_TIMEOUT_S`（120）、`TPM_TOKEN_RATIO`（0.55）、`TPM_KEY_CAP=64`；env 均作测试 seam（惯例同 :43 `SEND_TIMEOUT_S`）。
- `ctyun-stream-fix-proxy.py:79 _KIND_CATEGORY` — 加 `CLASS_TPM_LIMITED` 常量与两条 kind 映射。
- `ctyun-stream-fix-proxy.py:139 sse_line_has_usage()` — 新增兄弟函数 `sse_line_usage(line: bytes) -> dict|None`（同解析序，返回 usage dict 而非 bool）。
- `ctyun-stream-fix-proxy.py:197 classify_outcome()` 之后 — 新增模块态 `TPM_LOCK = threading.Condition()`、`TPM_BUCKETS = {}`、`TPM_WAITERS = collections.deque()` 与函数：`tpm_key_id(auth_header: str|None) -> str|None`（无 Authorization → None = 不限流直通）、`estimate_request_tokens(body) -> int`、`_tpm_prune(bucket, now)`、`tpm_admit(key_id, est) -> (status, qwait_ms)`（"ok"/"full"/"timeout"，逻辑见决策 3）、`tpm_settle(key_id, est, actual)`、`tpm_snapshot() -> dict`。
- `ctyun-stream-fix-proxy.py:879 _proxy_relay()` — body 读取/`extract_model`（:882）之后、`_open_upstream`（:900）之前插准入 hook：`key_id = tpm_key_id(self.headers.get("Authorization"))`；非 None 时 `tpm_admit`，"full"/"timeout" → `_reply_tpm_429` + `_log` + `_record_request(error=True)` + `record_error_event` 后 return。header 重试与空流重试复用同一次 charge，不重复准入。
- `ctyun-stream-fix-proxy.py:1161 _reply_502()` — 新增兄弟 `_reply_tpm_429(reason: str)`。
- `ctyun-stream-fix-proxy.py:1094 _relay_sse()`（`saw_usage` 判定处）— 用 `sse_line_usage` 把 usage dict 存入 `self._tpm_usage`（取最后非空帧）；流结束（含 truncated）后 `tpm_settle(key_id, est, usage.total_tokens)`，无 usage 帧不校正。
- `ctyun-stream-fix-proxy.py:999 _relay_buffered` 分支 — 解析 `relayed_data` JSON（:1003 同处）取 `usage.total_tokens` 后 settle；502/异常路径全额退款（`tpm_settle(key_id, est, 0)`）。
- `ctyun-stream-fix-proxy.py:1171 _log()` — 加可选 kwargs `qwait_ms=None, tpm_used=None`，非 None 时追加 `qwait=%dms tpm=%d` 字段。
- `ctyun-stream-fix-proxy.py:1194 AdminHandler.do_GET` — 加 `elif path == "/api/tpm_stats": self._send_json(200, tpm_snapshot())`。
- `ctyun-stream-fix-proxy.test.py` — 新增 `TpmRateLimitTest(unittest.TestCase)`（集成，复用 `start_proxy` :383 + `make_scripted_upstream` :269 + `post_sse` :417 + `admin_get` :508，env seam 注小参数：`CTYUN_TPM_LIMIT=1000`、`CTYUN_TPM_QUEUE_TIMEOUT_S=2`、`CTYUN_TPM_QUEUE_MAX=2`）+ 纯函数单测（`load_proxy_module` :500 测 estimate/admit/settle/snapshot/prune/脱敏）。SSE usage 帧复用 `SSE_USAGE`（:42）。

## Acceptance Criteria

- `python3 ctyun-stream-fix-proxy.test.py` 全绿（含既有 127 例无回归）。
- 窗口耗尽排队：fake upstream 下首发大 body 占满预算后，第二请求阻塞至窗口滚过后放行，stderr REQ 行含 `qwait=` 且 `tpm=` 数值正确。
- 排队超时：预算不释放时第二请求 ~2s（seam 值）后收 429，body 字节等于定死 JSON（含 `"code":"model_tpm_limit"`），REQ 行 `result=tpm-queue-timeout`。
- 队列满：`CTYUN_TPM_QUEUE_MAX=2` 时第 3 个并发等待请求立即 429，`result=tpm-queue-full`。
- usage 回填：fake upstream 回 `SSE_USAGE`（total_tokens=2）后 settle 释放预算，后续请求不等窗口滚过即放行（证明校正生效）。
- SSE 透传不破坏：限流生效路径下 `post_sse` 输出仍字节等于 `SSE_A+SSE_B+SSE_DONE` 且以 `data: [DONE]` 结尾。
- `/api/tpm_stats`：压测后 GET 返回 200 JSON，bucket `used`/`rejected`/`timeouts` 与 REQ 行计数一致，key 字段为 `sha256:` 前缀脱敏（无原始 key 泄漏）。
- 无 Authorization 头请求（如 `get_plain` :431）直通不限流。
- **部署与端到端验证（主代理 DELIVER 阶段执行，非 implementer）**：`cp ctyun-stream-fix-proxy.py ~/.local/bin/` → `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy` → ①单请求 200；②4 并发 ×40k 字符请求（key 取自 `~/.zcode/v2/config.json` provider "TY Pro via proxy"；/tmp/tpm_probe.py 已不存在，用 4 线程 python 脚本或 4 并发 curl 代替）触发排队/429；③流式响应完整；④`curl http://192.168.5.234:7921/api/tpm_stats` 数值与 `~/.local/log/ctyun-fwd.err` 的 `qwait=`/`tpm=` 行吻合。

## Risks

- 严格全局 FIFO 存在跨 key 队头阻塞：key A 的大请求占队首时 key B 即使有预算也等。可接受（队列上限 20、窗口 60s），不做得按 key 出队的复杂度。
- 估算偏差：0.55 比率对英文/代码偏高估（保守方向，可接受）；`max_tokens` 缺失时输出侧欠估，由 usage settle 校正兜底，但窗口内可能瞬时超预算——方向与上游限流一致（宁可本地 429）。
- 排队期间客户端断连不可检测（线程阻塞在 `Condition.wait`）：唤醒后上游请求白打一次再撞 client_abort（:865 既有路径兜底）。概率低、代价小，不处理。
- 准入 hook 在 `_proxy_relay`（:879），与 header 重试（:909）/空流重试（:942）交互：重试不重复 charge 是关键正确性点，测试必须锁死（scripted upstream `calls` 计数）。
- `TPM_LOCK` 与 `STATS_LOCK`/`ERROR_LOCK` 锁序：tpm 路径内调 `_record_request`/`record_error_event` 必须在 `TPM_LOCK` 释放后（429 分支先 return 再记录），防锁序反转。

## Exclusions

- 不改 dashboard HTML/JS（`_DASHBOARD_SRC` :1332 不动）；tpm_stats 仅 API 可见。
- 不做按模型分桶、不做跨进程/持久化限流状态（重启清零可接受）。
- 不处理无 `max_tokens` 时的输出侧精确预估；不引入 tokenizer 依赖。
- 不改 `classify_outcome` 既有分支与 `_DAILY_FIELDS` 桶形状；429 计数复用 `errors_total`/`errors_proxy` 现有口径。
- 不动 plist / launchd 配置（部署仅 cp + kickstart）。
- 客户端断连排队检测、按 key 公平出队——见 Risks，明确不做。

## R31 Evidence

[R31-S1] 问题存在：上游 TPM 超限两种实测形态均有现场记录

形态①：HTTP 200 + body 内嵌限流错误，代理日志盲区记 `result=ok`（2026-09-30 10:23 kimi-k3-oc 事故）：

```
$ grep -nE 'ts=2026-09-30T10:2[234]' ~/.local/log/ctyun-fwd.err
23163:REQ POST /chat/completions -> 200 dur=15.9s result=ok filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- ts=2026-09-30T10:23:00+0800
23164:REQ POST /chat/completions -> 200 dur=1.9s result=ok filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- ts=2026-09-30T10:23:02+0800
```

同期 App 侧实际收到 `{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试","type":"rate_limit_error","code":"model_tpm_limit"}}`。

形态②：突发 4×~42k tokens 压测时第 3 个请求收 HTTP 500 包装 `{"status":500,"error":"Internal Server Error"}`、第 4 个 502 超时，窗口滚过后恢复——上游配额是共享 110k TPM，多客户端并发即互相踩踏。

[R31-S2] 根因：代理纯透传，无任何客户端侧速率控制

```
$ grep -ciE 'tpm|rate.?limit|throttle|429' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
0
$ grep -nE 'def _proxy_relay|_open_upstream\(self' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
879:    def _proxy_relay(self, started: float) -> None:
900:                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
```

`_proxy_relay` 读 body 后直接 `_open_upstream`（:900），中间无任何准入检查；body-error 观测（前一 episode 已上线 `classify_outcome(body_error=)`/`sse_line_has_usage`）只解决"看见"，不解决"不发生"。客户端侧限流是唯一能在多客户端共享上游配额下消除该类事故的手段。
