# PLAN: REQ 日志行新增 exc 字段（代理合成 502 可定位异常）

## Header

- **目标**：`result=error`（代理合成 502）的 REQ stderr 行携带 `exc=<异常类型>:<压平文本>` 字段，运维凭单行日志定位上游连接失败根因（timeout vs refused vs reset）。
- **Spec**：`docs/superpowers/specs/2026-09-15-error-exc-log-design.md`
- **规模**：简化路径，2 卡，全 A 档（代码 PLAN 阶段写死 → executor 转写）。
- **路径映射**：spec 中 Files/acceptance 引用的绝对路径指向 main 工作树，本 PLAN 实现均在 worktree `.worktrees/req-log-exc-field/` 内进行，验证命令对应使用 worktree 路径。

## Global Constraints

- **TDD 铁律**：先改测试跑红 → 再改实现跑绿。卡 1 只改测试文件并跑出预期红（3 个测试不通过，exit code 1），卡 2 改生产代码后全量绿（exit 0）。生产代码不得先于失败测试出现。
- **验证命令**（每卡必跑，贴 stdout + exit code）：
  ```
  cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/req-log-exc-field && python3 ctyun-stream-fix-proxy.test.py
  ```
- **零新依赖**：生产代码仅用已有 `re`（ctyun-stream-fix-proxy.py:20 已导入）；测试文件加 `import contextlib`、`import io`（stdlib 已有）。
- **不动 aborted 路径**：`_log(started, 499, "aborted", 0)`（:587）不加 exc——spec Exclusions 已确认三类中断无法区分是设计意图。
- **不改**：`_reply_502` body、STATS/EVENTS、持久化结构、非 error 的 `_log` 调用点（ok/upstream-err/eof-without-done 走 exc 默认 `-`）。
- **executor STOP 守则**：执行中遇到本计划外决策或代码与计划不符 → STOP 上报，不自行裁决。

---

## 卡 1（tier A）：测试先行 — 3 处测试改动，跑红

**文件**：`ctyun-stream-fix-proxy.test.py`（worktree 内）

### 改动清单（死代码）

**① import 块补 `contextlib` 和 `io`**

位置：文件头 import 块（行 12 起），保持字母序。

- `import collections` 之后加一行：`import contextlib`
- `import importlib.util` 之后加一行：`import io`

**② `test_req_log_line_has_ts_and_model` — 正则加 `exc=\S+` 字段**

锚点：`AdminIntegrationTest`（:1198）内，函数 `test_req_log_line_has_ts_and_model`（:1393–:1406）。

完全替换如下代码块（old）：

```python
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"retry_reason=\S+ "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
        self.assertIsNotNone(m, "REQ 行必须带 model= / retry_reason= / ts= 字段，stderr:\n" + stderr)
```

New：

```python
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"retry_reason=\S+ "
                      r"exc=\S+ "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
        self.assertIsNotNone(m, "REQ 行必须带 model= / retry_reason= / exc= / ts= 字段，stderr:\n" + stderr)
```

→ 红期断言：实现尚未加 exc 字段时，REQ 行无 `exc=`，正则 `exc=\S+ ` 无匹配 → `assertIsNotNone` FAIL。

**③ `ProxyDashboardUnitTest` 新增单测 `test_log_exc_field_single_line_and_placeholder`**

锚点：类 `ProxyDashboardUnitTest`（:398），插在 `test_safe_log_stderr_normal_and_broken`（结束于 :1157）之后、`test_stderr_text_joins_buffer`（:1159）之前。

完整新增如下：

```python
    def test_log_exc_field_single_line_and_placeholder(self) -> None:
        mod = self.mod
        handler = object.__new__(mod.ProxyHandler)
        handler.command = "POST"
        handler.path = "/v1/chat/completions"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            handler._log(0.0, 502, "error", 0, model="m",
                         exc=ValueError("boom word\nnext"))
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1,
                         "异常内嵌换行不得把 REQ 行裂成多行")
        self.assertIn("exc=ValueError:_boom_word_next", lines[0])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            handler._log(0.0, 200, "ok", 0)
        self.assertIn("exc=-", buf.getvalue(),
                      "无异常时必须占位 exc=-")
```

> **校订注**：spec Files#5 原文断言串写为 `exc=ValueError:boom_word_next`，与 Files#1 死代码的实际产出 `exc=ValueError:_boom_word_next` 不一致（`%s: %s` 冒号后的空格也被 `\s+→_` 压平——这是目标代码的必然行为）。本 PLAN 按 Files#1 死代码的真实行为写死断言串 `exc=ValueError:_boom_word_next`；spec 的验收意图（类型名出现 + 空白压平可 grep `exc=\S+`）完全满足。executor 按此精确串执行，不得改回 spec 的原串。

→ 红期断言：`_log` 尚无 `exc` 形参 → `handler._log(..., exc=ValueError(...))` → **TypeError**（第一调用即炸）。

**④ `AdminIntegrationTest` 新增集成测试 `test_req_error_line_carries_exc`**

锚点：类 `AdminIntegrationTest`（:1198），插在 `test_req_log_line_has_ts_and_model`（结束于 :1406）之后、`test_client_abort_is_quiet_and_not_error`（:1408）之前。

完整新增如下：

```python
    def test_req_error_line_carries_exc(self) -> None:
        upstream_port = free_port()  # 死端口：连接即 ECONNREFUSED，进 :612 首次失败分支
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port)
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     body=b'{"model":"m","stream":true,"messages":[]}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 502, "上游不可达必须由代理合成 502")
        proc.terminate()
        proc.wait(timeout=5)
        stderr = stderr_text(proc)
        m = re.search(r"^REQ POST /v1/chat/completions -> 502 dur=\d+\.\ds "
                      r"result=error .*? exc=(\S+)\s+ts=", stderr, re.M)
        self.assertIsNotNone(m, "error 502 REQ 行必须带 exc= 字段，stderr:\n" + stderr)
        self.assertNotEqual(m.group(1), "-",
                            "exc 字段不得是占位符，stderr:\n" + stderr)
```

→ 红期断言：代理进程仍是旧代码走 :614 `_log(started, 502, "error", 0, model=model)`，REQ 行不带 `exc=` → `assertIsNotNone` FAIL。

### 验收（卡 1 结束）

```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/req-log-exc-field && python3 ctyun-stream-fix-proxy.test.py
```

**预期红**：恰好下列 3 个测试不通过，exit code 1（非 0）：
1. `ProxyDashboardUnitTest.test_log_exc_field_single_line_and_placeholder` → **ERROR**（TypeError：`_log` 不接受 `exc` kwarg）
2. `AdminIntegrationTest.test_req_log_line_has_ts_and_model` → **FAIL**（正则 `exc=\S+ ` 无匹配）
3. `AdminIntegrationTest.test_req_error_line_carries_exc` → **FAIL**（REQ 行无 `exc=` 字段）

**其余测试必须全绿。** 若失败集合不匹配（多/少/名字不同）→ STOP，上报主代理。

---

## 卡 2（tier A）：实现 — `_log` 加 exc 字段 + 两处 502 调用点传异常，跑绿

**文件**：`ctyun-stream-fix-proxy.py`（worktree 内）

### 改动清单（死代码）

**① `_log` 签名 + 格式串完整替换**（:792–:799 → spec Files#1）

Old：

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

New：

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

**② `_proxy_relay` 首次失败分支传 exc**（:614）

Old：

```python
            self._log(started, 502, "error", 0, model=model)
```

New：

```python
            self._log(started, 502, "error", 0, model=model, exc=exc)
```

**③ `_proxy_relay` 重试后失败分支传 `exc=retry_exc`**（:637–:638）

Old：

```python
                    self._log(started, 502, "error", 0, model=model, retried=1,
                              retry_reason=retry_reason)
```

New：

```python
                    self._log(started, 502, "error", 0, model=model, retried=1,
                              retry_reason=retry_reason, exc=retry_exc)
```

### 验收（卡 2 结束）

```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/req-log-exc-field && python3 ctyun-stream-fix-proxy.test.py
```

**预期绿**：全部测试通过，exit code 0。贴 stdout 末尾几行（含 `OK` 行 / `Ran N tests` 行）及 exit code。