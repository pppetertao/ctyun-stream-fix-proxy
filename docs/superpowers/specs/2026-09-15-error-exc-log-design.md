# REQ 日志行新增 exc 字段（代理合成 502 可定位异常）

## Goal

`result=error`（代理合成 502）的 REQ 日志行携带异常类型与文本，使运维仅凭 stderr 单行日志即可定位上游连接失败根因（如 9-15 上午连续 `dur=5.0s` 的 502 是 timeout 还是 refused）。

## 现状与方案

`_log`（`ctyun-stream-fix-proxy.py:792`）固定 8 字段格式，不含异常；异常文本只写进 502 body（`_reply_502` :783 `"%s" % exc`）。两个 502 产生点：`:612-614`（首次 `_open_upstream` 抛 `OSError`/`http.client.HTTPException`，异常变量 `exc`）与 `:635-638`（空流重试后再失败，异常变量 `retry_exc`）。

**方案（选定的一个）：`_log` 加可选 `exc=None` 参数，渲染为 `exc=<token>` 字段，插在 `retry_reason=` 与 `ts=` 之间（ts 保持末位）。**

- 渲染：`"%s: %s" % (type(exc).__name__, exc)` 后 `re.sub(r"\s+", "_", ...)` 单行化并截断 `[:200]`。空格/换行统一压成 `_`，保证空格分隔字段解析不被破坏；不用 `repr()`——repr 产物仍含空格且截断可能劈开转义序列。
- 无异常时占位 `exc=-`，沿用 `model=-`/`retry_reason=-` 既有惯例，全行保持固定字段形状。`type(exc).__name__` 恒非空，`exc` 非 None 时 token 恒非空。
- `re` 已在 :20 导入，零新依赖。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:792` `ProxyHandler._log()` — 签名加 `exc=None`，格式串插 `exc=%s`。目标代码（完整替换函数体）：

```python
    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0, retry_reason: str = "", exc=None) -> None:
        exc_field = "-"
        if exc is not None:
            exc_field = re.sub(r"\s+", "_",
                               ("%s: %s" % (type(exc).__name__, exc)).strip())[:200]
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d retry_reason=%s exc=%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            retry_reason or "-", exc_field,
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

2. `ctyun-stream-fix-proxy.py:614` `_proxy_relay()` 首次失败分支 — `self._log(started, 502, "error", 0, model=model, exc=exc)`。
3. `ctyun-stream-fix-proxy.py:637` `_proxy_relay()` 重试后失败分支 — 追加 `exc=retry_exc`（保留既有 `retried=1, retry_reason=retry_reason`）。
4. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:1398-1403` `test_req_log_line_has_ts_and_model` — 正则在 `retry_reason=\S+ ` 与 `ts=` 之间插 `exc=\S+ `（TDD 红：先改测试跑失败，再改实现）。
5. `ctyun-stream-fix-proxy.test.py` `ProxyDashboardUnitTest`（:398，插在 ：1130 `test_safe_log_stderr_normal_and_broken` 附近）— 新增 `test_log_exc_field_single_line_and_placeholder`：`load_proxy_module()`（:369）后 `object.__new__(mod.ProxyHandler)` 设 `command`/`path`，`contextlib.redirect_stderr(io.StringIO())` 捕获，调 `_log(0.0, 502, "error", 0, model="m", exc=ValueError("boom word\nnext"))` 断言该行含 `exc=ValueError:boom_word_next` 且整行无 `\n`；另调 `_log(0.0, 200, "ok", 0)` 断言含 `exc=-`。文件头需补 `import contextlib`、`import io`（若缺）。此单测同时覆盖 ：614/:637 两调用点的格式契约。
6. `ctyun-stream-fix-proxy.test.py` `AdminIntegrationTest`（:1198，插在 ：1393 附近）— 新增 `test_req_error_line_carries_exc`：`start_proxy(free_port(), free_port())`（上游=死端口 → ECONNREFUSED 进 ：612 分支），POST 后断言 resp.status==502，terminate 后 `stderr_text` 断言正则 `^REQ POST /v1/chat/completions -> 502 dur=\d+\.\ds result=error ` 且 `exc=(\S+)` 非 `-`。

## Acceptance Criteria

- `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && python3 ctyun-stream-fix-proxy.test.py` 全绿，exit 0（含上述 3 处测试改动）。
- 人造上游不可达场景下，stderr REQ 行形如 `REQ POST /v1/chat/completions -> 502 dur=0.0s result=error filtered=0 model=m retried=0 retry_reason=- exc=ConnectionRefusedError:... ts=...`，单行、无裸空白。
- 非 error 路径（ok/upstream-err/aborted/eof-without-done）REQ 行带 `exc=-`，既有字段序不变（除新增 exc 位）。
- exc 文本含空格/换行时（单测注入 `boom word\nnext`）被压成 `boom_word_next`，grep `exc=\S+` 可整体提取。

## Risks

- 既有按**位置**解析 REQ 行的消费方（awk $N）会因插入字段右移 `ts`；按字段名 grep（现状测试与运维惯例）不受影响。行内字段均为 `key=value` 自描述，风险可接受。
- 异常文本来自上游/OS，可能含任意字符：`_safe_log_stderr`（:253）已兜 OSError；`\s+→_` 保证不裂行，`[:200]` 防超长。
- 集成测试死端口连接在 macOS/Linux 恒为 ConnectionRefusedError（OSError 子类），断言"非 `-`"而非具体类名，避免平台耦合。

## Exclusions

- aborted 路径（:587 `_log(started, 499, "aborted", 0)`）**不加** exc：:581-586 注释已明确三类中断"无法区分也无需区分"为设计意图，加异常类型与该决策冲突。
- `_reply_502`（:782）body 格式不动；STATS/EVENTS/持久化结构不扩展——本改动仅单行 stderr 日志。
- 不为 ：637（重试后再失败）写端到端集成测试：需 fake upstream 首答空流后中途死亡，race-prone；格式契约由 Files#5 单测覆盖。

## R31 Evidence

[R31-S1] 问题现场命中（2026-09-15 采集）：`~/.local/log/ctyun-fwd.err` 中 9-15 上午 09 时段 `result=error` 共 12 条，其中 09:22:11–09:23:21 连续多条恒定 `dur=5.0s`（上游连接 600s 内超时/被断的形态）——仅凭日志无法区分是 connect timeout、TLS 握手失败还是连接被 reset：异常文本只写进了回给客户端的 502 body，REQ 行零异常信息。
```
$ grep -c 'result=error.*ts=2026-09-15T09' ~/.local/log/ctyun-fwd.err
12
$ grep -n 'result=error' ~/.local/log/ctyun-fwd.err | head -3
13841:REQ POST /chat/completions -> 502 dur=5.0s result=error filtered=0 model=deepseek-v4-pro-0813-oc retried=0 retry_reason=- ts=2026-09-15T09:22:11+0800
13842:REQ POST /chat/completions -> 502 dur=5.0s result=error filtered=0 model=deepseek-v4-pro-0813-oc retried=0 retry_reason=- ts=2026-09-15T09:22:17+0800
13843:REQ POST /chat/completions -> 502 dur=5.0s result=error filtered=0 model=deepseek-v4-pro-0813-oc retried=0 retry_reason=- ts=2026-09-15T09:22:25+0800
```
[R31-S2] 根因：两个代理合成 502 的产生点（`_proxy_relay` :612-614 捕获 `OSError`/`http.client.HTTPException` 于 `exc`、:635-638 重试后再失败于 `retry_exc`）调用 `_log` 时均不传异常；`_log`（:792-799）格式串只有 8 字段、无 exc 位，异常文本仅经 `_reply_502`（:783）`"%s" % exc` 进 502 body，客户端不回传即丢。修复面在代理进程内 `_log` 一处，客户端与网关零改动。
```
$ grep -n 'upstream error' ctyun-stream-fix-proxy.py
783:        payload = ("ctyun-stream-fix-proxy: upstream error: %s\n" % exc).encode("utf-8")
$ sed -n '792,799p' ctyun-stream-fix-proxy.py
    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0, retry_reason: str = "") -> None:
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d retry_reason=%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            retry_reason or "-",
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```
