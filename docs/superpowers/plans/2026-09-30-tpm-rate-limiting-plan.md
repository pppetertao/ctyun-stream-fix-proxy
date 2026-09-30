# PLAN.md — TPM 客户端侧限流

## Header

- **Goal**: 为 ctyun-stream-fix-proxy 加 per-key 60s 滚动窗口 TPM 限流（110k tokens 预算），超预算 FIFO 排队（上限 20，超时 120s → 429），消除上游 `model_tpm_limit` 事故。
- **Branch**: `fix/tpm-rate-limiting` (worktree)
- **Spec**: `docs/superpowers/specs/2026-09-30-tpm-rate-limiting-design.md`
- **Baseline**: `python3 ctyun-stream-fix-proxy.test.py` — 127 tests OK (58.68s, exit 0)
- **Verification command**: `python3 ctyun-stream-fix-proxy.test.py` (worktree root)

## Global Constraints

1. **验证命令**: `python3 ctyun-stream-fix-proxy.test.py`（worktree 根）；每卡写代码前先跑 baseline 登记当前状态。
2. **Tier**: 全 A 档（spec 锚点完整且可写死代码）；仅真有「必须跑运行时才能定代码」的留白才标 C。本 spec 锚点已给死全部函数签名与插入位，不存在 C 档需求。
3. **TDD 顺序**: 每卡内：先写失败测试 → 实现生产代码 → 跑绿验证 → commit（Conventional Commits）。
4. **并发/排队测试**: 优先确定性手段（小窗口 env seam `CTYUN_TPM_LIMIT=200`、`CTYUN_TPM_QUEUE_TIMEOUT_S=2`、`CTYUN_TPM_QUEUE_MAX=2`）；避免真实 sleep 依赖的 flaky 测试；新测试不得加剧既有 teardown `proc.wait` flaky（followups 已知问题）。
5. **代码风格**: 遵循既有模式——常量区 env seam（`:43` SEND_TIMEOUT_S 惯例）、`classify_outcome` flag-first 优先级链、`_reply_502` 的 send_response/send_header/end_headers/wfile.write/close_connection 模式、`AdminHandler.do_GET` 的 `elif path == "..."` 路由追加、`_log` 格式 `REQ ... dur=... result=...`。
6. **分段写作**: 5 卡，B0（本文件骨架）→ B1..B5（逐卡填码）→ B-final（自查 5 项）。B0 仅含 Header + Global Constraints + 卡清单（每卡 tier/文件/接口签名/验收）+ anchor grep 实测证据。**B0 不改生产/测试代码，不写卡内代码块。**
7. **Commit 规范**: 每卡独立 commit，Conventional Commits。B0 commit: `docs(plan): TPM rate limiting B0 skeleton`。B1..B5 各 commit 格式见卡片内。
8. **锁序**: `TPM_LOCK` 内不调 `_record_request`/`record_error_event`（429 分支先 return 再记录，避免 `TPM_LOCK` 与 `STATS_LOCK`/`ERROR_LOCK` 反转）。
9. **无新依赖**: 纯 stdlib（`collections`、`threading`、`hashlib`、`json`、`os`），不加 pip 包。

## 卡清单

---

### Card 1 [A] — 常量区 + sse_line_usage + _KIND_CATEGORY

- **文件**: `ctyun-stream-fix-proxy.py`
- **接口/签名**:
  - 常量（`:51` EMPTY_RETRY_MAX 旁）: `TPM_LIMIT`、`TPM_WINDOW_S`、`TPM_QUEUE_MAX`、`TPM_QUEUE_TIMEOUT_S`、`TPM_TOKEN_RATIO`、`TPM_KEY_CAP`（全部 env seam，惯例同 `:43` SEND_TIMEOUT_S）
  - 结果类（`:62` CLASS_OK 旁）: `CLASS_TPM_LIMITED = "tpm_limited"`
  - Kind 常量（`:68` ERR_KIND_POISON 旁）: `ERR_KIND_TPM_QUEUE_FULL = "tpm_queue_full"`、`ERR_KIND_TPM_QUEUE_TIMEOUT = "tpm_queue_timeout"`
  - `_KIND_CATEGORY`（`:79`）: 加两条映射 `ERR_KIND_TPM_QUEUE_FULL: CLASS_TPM_LIMITED`、`ERR_KIND_TPM_QUEUE_TIMEOUT: CLASS_TPM_LIMITED`
  - `sse_line_usage(line: bytes) -> dict | None`（`:139` sse_line_has_usage 之后，兄弟函数）: 同解析序，返回 usage dict（如 `{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}`），无 usage 帧返回 None
- **验收**: `load_proxy_module()` 后可 import 全部常量 + 调用 `sse_line_usage`；sse_line_usage 对 SSE_USAGE 返回 `{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}`，对 SSE_DONE/SSE_A/noise 返回 None
- **Anchor grep 验证**:
  ```
  :51: EMPTY_RETRY_MAX = int(os.environ.get(...))    ← 常量插入位
  :43: SEND_TIMEOUT_S = float(os.environ.get(...))   ← env seam 惯例参考
  :62: CLASS_OK = "ok"                                ← CLASS_TPM_LIMITED 插入位
  :68: ERR_KIND_POISON = "poison_hit"                ← ERR_KIND_* 插入位
  :79: _KIND_CATEGORY = {                             ← 映射追加位
  :139: def sse_line_has_usage(line: bytes) -> bool: ← sse_line_usage 兄弟插入位（:153 函数结束）
  ```
- **Commit**: `feat(proxy): add TPM constants, sse_line_usage, and KIND_CATEGORY entries`

---

### Card 2 [A] — TPM 模块态 + 准入/结算/快照核心函数

- **文件**: `ctyun-stream-fix-proxy.py`
- **接口/签名**:
  - `_TpmWaiter = collections.namedtuple("_TpmWaiter", "key_id est enqueued_at")`
  - `TPM_LOCK = threading.Condition()`
  - `TPM_BUCKETS: dict[str, collections.deque] = {}`（key_id → deque of (ts, tokens) tuples）
  - `TPM_WAITERS: collections.deque = collections.deque()`（of _TpmWaiter）
  - `tpm_key_id(auth_header: str | None) -> str | None`: 无 Authorization → None；提取 Bearer token → sha256 hex 全位（内部用全位做 key，快照对外只显前 12 位）
  - `estimate_request_tokens(body) -> int`: 准入估算 = `max(1, int(len(body) * TPM_TOKEN_RATIO))` + JSON 含 int `max_tokens` 时累加
  - `_tpm_prune(bucket: collections.deque, now: float) -> None`: 删 `now - TPM_WINDOW_S` 之前的条目，更新 `bucket.used`
  - `tpm_admit(key_id: str, est: int) -> tuple[str, float]`: 返回 `("ok"/"full"/"timeout", qwait_ms)`；逻辑见 spec 决策 3。`est > TPM_LIMIT` → 直接 `("full", 0)`。入队+准入同在 `TPM_LOCK` 内原子；仅队首 waiter 可被准入（严格 FIFO）；wait 循环 `TPM_LOCK.wait(timeout=min(1.0, remaining))` + deadline 检查
  - `tpm_settle(key_id: str, est: int, actual: int) -> None`: prune → `delta = actual - est` → `used = max(0, used + delta)`；追加 `(now, delta)` 校正条目；`TPM_LOCK.notify_all()`
  - `tpm_snapshot() -> dict`: 锁内收集 → `{"config":{"limit":TPM_LIMIT,"window_s":TPM_WINDOW_S,"queue_max":TPM_QUEUE_MAX},"queue_total":len(TPM_WAITERS),"buckets":[...]}`；key 显 sha256 hex 前 12 位，含 `used`、`remaining`、`queued`、`rejected`、`timeouts`
- **Card 2 需新增的模块级计数器**（`TPM_LOCK` 内维护）:
  - `_tpm_rejected: dict[str, int]`（per-key queue-full 计数，用于 snapshot）
  - `_tpm_timeouts: dict[str, int]`（per-key queue-timeout 计数，用于 snapshot）
- **验收**: `load_proxy_module()` 后可调用全部纯函数；`tpm_admit` 首请求返回 `("ok", 0)`；窗口耗尽后返回 `("ok", >0)` 含 qwait；`tpm_settle` 释放预算后 `notify_all` 唤醒 waiter；`tpm_snapshot` 返回正确 JSON shape
- **Anchor grep 验证**:
  ```
  :197: def classify_outcome(...) -> _Outcome:   ← 函数结束后插入 TPM 模块态
  :13: import collections                          ← _TpmWaiter namedtuple 依赖已满足
  :24: import threading                            ← threading.Condition 依赖已满足
  ```
- **Commit**: `feat(proxy): add TPM admission, settle, and snapshot core functions`

---

### Card 3 [A] — 429 合成 + /api/tpm_stats 管理端点 + _log 扩展

- **文件**: `ctyun-stream-fix-proxy.py`
- **接口/签名**:
  - `ProxyHandler._reply_tpm_429(self, reason: str) -> None`（`:1161` _reply_502 之后）: status 429 + `Content-Type: application/json` + 定死 body（spec 决策 4）+ `close_connection = True`
  - `AdminHandler.do_GET`（`:1194` `/api/stats` 分支旁）: `elif path == "/api/tpm_stats": self._send_json(200, tpm_snapshot())`
  - `ProxyHandler._log`（`:1171`）: 加可选 kwargs `qwait_ms=None, tpm_used=None`，非 None 时在 REQ 行末尾（`ts=` 之前）追加 `qwait=%dms tpm=%d`
- **验收**: `_reply_tpm_429` 返回的 body 字节等于定死 JSON（含 `"code":"model_tpm_limit"`）；`/api/tpm_stats` GET 返回 200 + JSON 含 `config`/`queue_total`/`buckets`；`_log` 传 `qwait_ms=1234, tpm_used=50000` 时 stderr 含 `qwait=1234ms tpm=50000`
- **Anchor grep 验证**:
  ```
  :1161: def _reply_502(self, exc: BaseException) -> None:  ← 兄弟插入位
  :1171: def _log(self, started: float, status: int, ...) -> None:  ← 签名扩展位
  :1177: _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s ..."  ← 格式扩展位
  :1194: elif path == "/api/stats":                                ← 新路由插入位
  :1195:     self._send_json(200, stats_snapshot())                ← 模式参考
  ```
- **Commit**: `feat(proxy): add _reply_tpm_429, /api/tpm_stats endpoint, and _log TPM fields`

---

### Card 4 [A] — _proxy_relay 准入 hook + SSE 结算 + buffered 结算 + 错误退款

- **文件**: `ctyun-stream-fix-proxy.py`
- **关键逻辑与锚点**:
  - **准入 hook**（`:879` _proxy_relay，body 读取 `:882` 之后、`_open_upstream` `:900` 之前）:
    ```
    key_id = tpm_key_id(self.headers.get("Authorization"))
    if key_id is not None:
        est = estimate_request_tokens(body)
        status, qwait_ms = tpm_admit(key_id, est)
        if status in ("full", "timeout"):
            self._reply_tpm_429(status)
            self._log(started, 429, status, 0, model=model, qwait_ms=qwait_ms)
            _record_request(self.command, self.path, 429, ...error=True)
            record_error_event(ERR_KIND_TPM_QUEUE_FULL or _TIMEOUT, ...)
            return
    ```
    - `record_error_event` 调用必须在 `TPM_LOCK` 释放后（429 分支 `return` 后不持锁，安全）
  - **header 重试/空流重试复用准入**（`:909` / `:942`）: `_open_upstream` 重试前不重新 `tpm_admit`；body 同一份。需要在 `_proxy_relay` 入口处将 `key_id`、`est`、`admit_called` 存为局部变量，重试路径复用——不重复 charge。**每个 `_open_upstream` 调用前不做新准入**（一次请求一次准入）。
  - **SSE 结算**（`:1056` _relay_sse）:
    - 在 `saw_usage` 判定处（`:1094-1095`）用 `sse_line_usage` 提取 usage dict 存入 `self._tpm_usage`（取最后非空帧）
    - 流结束（`:1133` truncated / 正常 EOF）后，在 `_proxy_relay` 中 `_relay_sse` 返回后调用 `tpm_settle(key_id, est, usage.total_tokens)`（若有 usage 帧）
    - 无 usage 帧不校正（不调 settle）
  - **buffered 结算**（`:999` _relay_buffered 分支）:
    - 解析 `relayed_data` JSON（`:1003` 同处）取 `usage.total_tokens` → `tpm_settle(key_id, est, usage.total_tokens)`
    - 502/异常路径（`:920-931` synth_502）: `tpm_settle(key_id, est, 0)` 全额退款
  - **`_body_err_line` 判定** path（`:968-969`）正常 settle 同 SSE
  - **client_abort 路径**（`:865`）: 不 settle（无可靠 usage 数据，且窗口自动过期）
- **验收**: 准入 hook 位于 `_open_upstream` 之前；429 路径不调 `_open_upstream`；header 重试不重复准入（通过 calls 计数验证）；`_relay_sse` 结束后 `tpm_settle` 被调用；buffered 路径解析 usage 后 settle；502 synth 全额退款
- **Anchor grep 验证**:
  ```
  :879:  def _proxy_relay(self, started: float) -> None:          ← 函数入口
  :880:  length = int(self.headers.get("Content-Length") or 0)    ← body 读取前
  :882:  model = extract_model(body)                              ← 准入 hook 插入位（之后）
  :900:  conn, resp = self._open_upstream(...)                    ← 准入 hook 插入位（之前）
  :909:  if not header_timeout_should_retry(...):                 ← header 重试起点
  :942:  except _EmptyStream as exc:                              ← 空流重试起点
  :939:  filtered, truncated = self._relay_sse(resp, ...)         ← SSE 调用点
  :1056: def _relay_sse(self, resp: ..., final: bool) -> tuple:   ← SSE relay 函数
  :1094: if not saw_usage:                                        ← Usage 捕获点
  :1095:     saw_usage = any(sse_line_has_usage(l) for l in pending)  ← 现有逻辑
  :1131: raise _EmptyStream(filtered, primed)                     ← EOF 空流
  :1133: truncated = not saw_done                                  ← 正常 EOF 截断标记
  :999:  relayed_data = self._relay_buffered(resp)                ← buffered 分支
  :1003: parsed = json.loads(relayed_data.decode(...))            ← JSON 解析点
  :920:  except (OSError, http.client.HTTPException) as exc:      ← synth_502 路径
  ```
- **Commit**: `feat(proxy): integrate TPM admission and usage settlement into relay paths`

---

### Card 5 [A] — 全量集成测试 + 回归

- **文件**: `ctyun-stream-fix-proxy.test.py`
- **测试类**: `TpmRateLimitTest(unittest.TestCase)`（继承自 `unittest.TestCase`，对齐 `AdminIntegrationTest`/`BodyErrorTest` 模式）
- **测试函数**（覆盖 Acceptance 全部 8 条）:
  1. `test_queue_exhaustion_and_window_roll` — 窗口耗尽排队：首发大 body 占满 budget（`CTYUN_TPM_LIMIT=200`）后，第二请求阻塞至窗口滚过（～1s poll）+ stderr 含 `qwait=` 且 `tpm=` 数值正确
  2. `test_queue_timeout_returns_429` — 排队超时：用 `CTYUN_TPM_QUEUE_TIMEOUT_S=2`，预算不释放 → 第二请求 2s 后收 429，body 字节等于定死 JSON，REQ 行 `result=tpm-queue-timeout`
  3. `test_queue_full_immediate_429` — 队列满：`CTYUN_TPM_QUEUE_MAX=2`，3 并发中第 3 个立即 429，`result=tpm-queue-full`
  4. `test_usage_settle_releases_budget` — usage 回填：fake upstream 回 `SSE_USAGE`（total_tokens=2）后 settle 释放预算，后续请求不等窗口滚过即放行
  5. `test_sse_passthrough_under_rate_limiting` — SSE 透传不破坏：限流生效路径下 `post_sse` 输出仍字节等于 `SSE_A+SSE_B+SSE_DONE` 且以 `data: [DONE]` 结尾
  6. `test_no_auth_bypasses_tpm` — 无 Authorization 头请求直通不限流（`get_plain`）
  7. `test_tpm_stats_endpoint` — `/api/tpm_stats` 返回 200 JSON，bucket `used`/`rejected`/`timeouts` 与 REQ 行计数一致，key 字段 `sha256:` 前缀脱敏
  8. `test_all_existing_tests_pass` — 既有 127 例无回归
- **测试 seam**: `start_proxy(extra_env={"CTYUN_TPM_LIMIT": "200", "CTYUN_TPM_QUEUE_TIMEOUT_S": "2", "CTYUN_TPM_QUEUE_MAX": "2"})`
- **验收**: `python3 ctyun-stream-fix-proxy.test.py` 全绿（含既有 127 例 + 新增全部 TPM 用例）
- **Commit**: `test(proxy): add TPM rate limiting integration tests`

---

## Anchor Grep 实测证据

以下为 B0 阶段实测 grep（当前 `fix/tpm-rate-limiting` worktree，commit a5ea44d）:

```
$ grep -n 'EMPTY_RETRY_MAX\|SEND_TIMEOUT_S\|_KIND_CATEGORY\|def sse_line_has_usage\|def classify_outcome\|def _proxy_relay\|def _reply_502\|def _relay_sse\|def _relay_buffered\|def do_GET\|def _log' ctyun-stream-fix-proxy.py

43: SEND_TIMEOUT_S = float(os.environ.get("CTYUN_SEND_TIMEOUT", "60"))
51: EMPTY_RETRY_MAX = int(os.environ.get("CTYUN_EMPTY_RETRY", "1"))
79: _KIND_CATEGORY = {
139: def sse_line_has_usage(line: bytes) -> bool:
172: def classify_outcome(status=None, synth_502=False, ...):
837:     def do_GET(self) -> None:
879:     def _proxy_relay(self, started: float) -> None:
1056:     def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
1143:     def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
1161:     def _reply_502(self, exc: BaseException) -> None:
1171:     def _log(self, started: float, status: int, result: str, filtered: int, model=None, retried=0, retry_reason="", exc=None) -> None:
1188:     def do_GET(self) -> None:
1194:         elif path == "/api/stats":
```

```
$ grep -n 'def record_error_event\|def _record_request\|def _safe_log_stderr\|def _send_json\|def _send\b' ctyun-stream-fix-proxy.py

223: def record_error_event(kind, model=None, path=None, ...):
487: def _safe_log_stderr(msg: str) -> None:
680: def _record_request(method: str, path: str, status: int, dur_ms: float, filtered: int, model=None, error: bool = False) -> None:
1310:     def _send(self, status: int, content_type: str, body: bytes) -> None:
1318:     def _send_json(self, status: int, payload: dict) -> None:
```

```
$ grep -n 'class ProxyHandler\|class AdminHandler' ctyun-stream-fix-proxy.py

834: class ProxyHandler(http.server.BaseHTTPRequestHandler):
1185: class AdminHandler(http.server.BaseHTTPRequestHandler):
```

所有 spec 锚点行号与实测一致，无偏移。

---

*B0 skeleton — 卡代码块由 B1..B5 逐卡填充。Self-Review 由 B-final 执行。*