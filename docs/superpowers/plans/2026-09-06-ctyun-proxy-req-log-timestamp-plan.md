# ctyun-proxy REQ 日志行加 ts/model PLAN（simplified mode）

- **Feature**: REQ 日志行（stderr）尾插 `model=` 与 `ts=` 两字段——`ts` 为本地时区 ISO 8601 秒级时间戳（`%Y-%m-%dT%H:%M:%S%z`，如 `2026-09-06T14:32:05+0800`），`model` 复用 `extract_model` 结果（取不到为 `-`）；请求转发与统计行为零变更
- **Branch**: `fix/ctyun-proxy-req-log-timestamp`（worktree `.worktrees/ctyun-proxy-req-log-timestamp`，仅承载 docs）
- **Spec**: `docs/superpowers/specs/2026-09-06-ctyun-proxy-req-log-timestamp-design.md`
- **Date**: 2026-09-06
- **实现落点**: `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py` + `ctyun-stream-fix-proxy.test.py`（不在 git 仓库，无 commit；过测后归档 `~/Documents/project/ctyun-stream-fix-proxy/` byte-identical）

## Global Constraints

- 运行/测试一律 `/usr/bin/python3` 绝对路径（Homebrew 3.14.7 `http.server` 挂死坑）。
- REQ 行**字段尾插**：`REQ` 前缀与既有键值序逐字节不变；存量子串断言（test :278 `filtered=1`、:286 `filtered=0`、:763 `result=aborted`）必须零破坏存活。
- 499 aborted 行恒 `model=-`：`_proxy` :321 调用**保持原样不传 model**（abort 时 body 可能未读，与 :322 `_record_request(model=None)` 现状一致）；不做 499 路径 model 回传改造。
- 生产文件零新 import（复用 :24 已有 `time`）；test 文件仅新增 stdlib `re`。
- 请求转发、统计、持久化、dashboard 行为零变更；不碰 09-05 的 stderr drain / kill_registered 测试基建（`stderr_text` :132、`kill_registered` :137）。
- baseline（2026-09-06 实测）：31 用例全绿（`Ran 31 tests / OK / exit 0`）；本计划改后 32 用例。
- 每卡执行者同次 dispatch 内跑完卡内验证命令并贴实际 stdout + exit code，不跑完不 claim 完成。

## Task 1: TDD 红相——test 文件三处改动（tier A → executor）

**文件**: `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`

1a. import 区（:12-25，字母序）`import os` 与 `import shutil` 之间（两行邻接序列全文唯一）插入一行：

```python
import re
```

1b. `AdminIntegrationTest` 新增测试方法：插在 `def test_client_abort_is_quiet_and_not_error(self) -> None:`（:733，全文唯一）def 行之前，与其余方法一致上方留一个空行——

```python
    def test_req_log_line_has_ts_and_model(self) -> None:
        post_sse(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
        self.assertIsNotNone(m, "REQ 行必须带 model= 与 ts= 字段，stderr:\n" + stderr)
        time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z")  # %z 回析：防平台差异静默退化
```

（`post_sse` :210 body 自带 `"model":"deepseek-v4-pro-0813-oc"`；`stderr_text` :132 为 drain buffer 文本视图，terminate+wait 后已收全。）

1c. `test_client_abort_is_quiet_and_not_error` 内（:763-765，四行块全文唯一）：

```python
        self.assertIn("result=aborted", stderr)
        self.assertNotIn("Traceback", stderr,
                         "client abort must not produce handle_error traceback")
```

改为（中间插一行）：

```python
        self.assertIn("result=aborted", stderr)
        self.assertIn("model=-", stderr)  # 499 abort 不传 model → 占位符 -
        self.assertNotIn("Traceback", stderr,
                         "client abort must not produce handle_error traceback")
```

**验证（红相确认）**：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -6
```

通过标准：`Ran 32 tests` + `FAILED (failures=2)`（exit 1），失败恰为 `test_req_log_line_has_ts_and_model`（assertIsNotNone）与 `test_client_abort_is_quiet_and_not_error`（`'model=-' not found`）两用例，其余 30 绿——baseline 绿 → 新红，红点即本计划要修的缺口。

## Task 2: 生产实现——`_log` 尾插 model/ts + 三处调用传参（tier A → executor）

**文件**: `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`

2a. `_log` 全函数替换（:444-447，函数体全文唯一）：

```python
    def _log(self, started: float, status: int, result: str, filtered: int, model=None) -> None:
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d model=%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-",
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

2b. `_proxy_relay` 502 error 路径（:355-357，`_reply_502(exc)` 全文唯一）：

```python
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            self._log(started, 502, "error", 0)
```

改为：

```python
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            self._log(started, 502, "error", 0, model=model)
```

2c. SSE 路径（:365-367，`_relay_sse(resp)` 调用全文唯一）：

```python
        if "text/event-stream" in content_type:
            filtered = self._relay_sse(resp)
            self._log(started, resp.status, result, filtered)
```

改为：

```python
        if "text/event-stream" in content_type:
            filtered = self._relay_sse(resp)
            self._log(started, resp.status, result, filtered, model=model)
```

2d. buffered 非 SSE 路径（:370-372，`_relay_buffered(resp)` 调用全文唯一）：

```python
        else:
            self._relay_buffered(resp)
            self._log(started, resp.status, result, 0)
```

改为：

```python
        else:
            self._relay_buffered(resp)
            self._log(started, resp.status, result, 0, model=model)
```

**不改**：`_proxy` :321 `self._log(started, 499, "aborted", 0)` 保持原样（model 缺省 None → 行内 `model=-`，由 Task 1c 断言锁定）。`model` 变量来自 :331 `extract_model(body)`，2b/2c/2d 三处调用点均在作用域内（:331 先于 :346 try 块）。

**验证（绿相）**：

```
/usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py && echo py-compile-ok
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
```

通过标准：`py-compile-ok` + `Ran 32 tests` + `OK` + exit 0（spec Acceptance ①②）。

## Task 3: 归档同步 byte-identical（tier B → executor）

前置：Task 2 已全绿。两文件字节级复制到归档目录（同名覆盖）并 `cmp` 验证：

```
cp /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/
cmp /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py && cmp /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py && echo archive-identical
```

通过标准：`archive-identical` + exit 0（spec Acceptance ④）。

## DELIVER 步骤（主代理执行，不进 executor 卡）

spec Acceptance ③ 为部署实机验收，归主代理 DELIVER 阶段：

1. `launchctl kickstart -k gui/$UID/com.zcode.ctyun-stream-fix-proxy` 重启常驻代理（stderr 落点按 launchd plist StandardErrorPath，实测时确认）。
2. 发一条经代理请求，grep 最新 REQ 行含 `model=<model> ts=<时间>`，且 ts 与 `date +%Y-%m-%dT%H:%M:%S%z` 秒级吻合。
3. worktree 内 docs commit（PLAN.md）与 ff-merge 回 main 按 DELIVER 铁律执行。

## 风险与回退

- `%z` 平台差异：macOS 系统 Python `time.localtime()` struct 带 tm_gmtoff，`%z` 稳定输出 `+0800`；Task 1b 的 strptime 回析断言即为此设防（回析失败即测试红）。
- 回退：归档目录在 Task 3 cp 之前仍是上版（09-05 daily-stats 版）——`cp ~/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py ~/.local/bin/` + kickstart 即回退。

## R31 Evidence

（SOURCE=spec：`docs/superpowers/specs/2026-09-06-ctyun-proxy-req-log-timestamp-design.md`；以下锚点为 2026-09-06 PLAN-B 阶段实测复核，与 spec 实测一致。）

[R31-S1] 问题现场命中：REQ 行格式串仅 command/path/status/dur/result/filtered 六要素，无时间戳无 model；dashboard recent 为 maxlen=100 滚动 deque（:57），事件滚出后代理侧失联——

```
$ grep -n 'def _log\|REQ %s' /Users/peter/.local/bin/ctyun-stream-fix-proxy.py
444:    def _log(self, started: float, status: int, result: str, filtered: int) -> None:
445:        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d"
$ grep -c 'def test_' /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py
31
```

[R31-S2] 根因与修复面：`_log`（:444-447）格式串从未含时间与模型字段，`extract_model`（:331）结果只进内存统计（`_record_request` :257 → RECENT_REQUESTS 滚出即失）；修复面收敛为 `_log` 签名/格式串 + `_proxy_relay` 三处调用点（:357/:367/:372）传 `model=model`，:321 499 调用不传（占位 `-`）；baseline 31 用例全绿——

```
$ grep -n 'self._log(' /Users/peter/.local/bin/ctyun-stream-fix-proxy.py
321:            self._log(started, 499, "aborted", 0)
357:            self._log(started, 502, "error", 0)
367:            self._log(started, resp.status, result, filtered)
372:            self._log(started, resp.status, result, 0)
$ /usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -3
Ran 31 tests in 11.133s

OK
```
