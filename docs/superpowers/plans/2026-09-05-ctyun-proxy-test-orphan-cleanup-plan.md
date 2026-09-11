# ctyun-proxy 测试孤儿进程根修 PLAN（simplified mode）

- **Feature**: 测试代理子进程孤儿根修（stderr drain + atexit 兜底）+ 代理 stderr 写容错
- **Branch**: `fix/ctyun-proxy-test-orphan-cleanup`（worktree `.worktrees/ctyun-proxy-test-orphan-cleanup`）
- **Spec**: `docs/superpowers/specs/2026-09-05-ctyun-proxy-test-orphan-cleanup-design.md`
- **执行者**: 主代理全程亲自执行（本会话用户显式指令延续）
- **Date**: 2026-09-05

## Global Constraints

- 运行/测试一律 `/usr/bin/python3` 绝对路径。
- 现有 28 测试断言语义零变化（仅 stderr 获取方式从 pipe.read 换 drain buffer）；新增测试 ≥3。
- 代理侧只加 `_safe_log_stderr` 并替换 `_log`/`_on_signal` 两处 print——不动转发/剥行/统计逻辑。
- 临时实验脚本（/tmp/ctyun-sigterm-repro*.py、drain 对照版）用后删除。

## Task 1: TDD——drain buffer + kill_registered + _safe_log_stderr

**Step 1 先写失败测试**（`~/.local/bin/ctyun-stream-fix-proxy.test.py`）：

1a. imports 补 `signal`（现有 `import shutil` 行后插 `import signal`——按字母序放 shutil 后 socket 前）。

1b. `ProxyDashboardUnitTest` 的 `test_stats_snapshot_daily_is_copy` 之后追加 3 个单测：

```python
    def test_safe_log_stderr_normal_and_broken(self) -> None:
        mod = self.mod
        captured = []

        class Collect:
            def write(self, s):
                captured.append(s)
                return len(s)

            def flush(self):
                pass

        class Broken:
            def write(self, s):
                raise BrokenPipeError("pipe closed")

            def flush(self):
                pass

        orig = sys.stderr
        try:
            sys.stderr = Collect()
            mod._safe_log_stderr("hello-safe-log")
            self.assertIn("hello-safe-log", "".join(captured))
            sys.stderr = Broken()
            mod._safe_log_stderr("must-not-raise")  # 不抛 = 通过
        finally:
            sys.stderr = orig

    def test_stderr_text_joins_buffer(self) -> None:
        class FakeProc:
            stderr_buf = [b"REQ a\n", b"REQ b\n"]
        self.assertEqual(stderr_text(FakeProc()), "REQ a\nREQ b\n")

    def test_kill_registered_progressive_and_idempotent(self) -> None:
        tmod = sys.modules[__name__]

        class FakeProc:
            def __init__(self, survives_term):
                self.pid = 424242
                self._alive = True
                self._survives_term = survives_term
                self.terminated = False
                self.killed = False

            def poll(self):
                return None if self._alive else 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if self.killed or (self.terminated and not self._survives_term):
                    self._alive = False
                    return 0
                raise subprocess.TimeoutExpired("fake", timeout)

        tmod._PROCS[:] = [FakeProc(False), FakeProc(True)]
        killed = tmod.kill_registered(timeout=0.1)
        self.assertEqual(killed, [424242],
                         "proc surviving SIGTERM must be reported as force-killed")
        self.assertEqual(len(tmod._PROCS), 0, "registry must be cleared")
        self.assertEqual(tmod.kill_registered(), [], "second call is a no-op")
```

**Step 2 红相确认**（`_safe_log_stderr`/`stderr_text`/`kill_registered` 不存在 → 3 error）：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
```

**Step 3 实现**：

3a. `ctyun-stream-fix-proxy.py`——`_prune_daily` 函数后新增：

```python
def _safe_log_stderr(msg: str) -> None:
    # 吞掉的是 stderr 写失败的一切 OSError（BrokenPipeError=管道对端关闭、ENOSPC=磁盘满等）：
    # stderr 是 best-effort 诊断出口，无备用通道，且失败不得传播——传播会炸请求线程，
    # 并使 SIGTERM handler 的 except 分支二次抛出、阻断 SystemExit 退出路径。
    try:
        print(msg, file=sys.stderr, flush=True)
    except OSError:
        pass
```

3b. `ProxyHandler._log` 全函数替换为：

```python
    def _log(self, started: float, status: int, result: str, filtered: int) -> None:
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered))
```

3c. `main()` 内 `_on_signal` 的 except 分支 print 替换：

```python
        except OSError as exc:
            # 退出路径落盘失败仅留痕不阻断退出：计数一致性由日志兜底，
            # 进程此时必须退出（launchd 语义），无其他路径可达。
            _safe_log_stderr("ctyun-stream-fix-proxy: stats save on exit failed: %s" % exc)
        raise SystemExit(0)
```

3d. `ctyun-stream-fix-proxy.test.py`——`_PROCS = []`（`FAKE_SERVERS = []` 行后）：

```python
_PROCS = []  # start_proxy 产物的注册表（atexit 兜底清理，防测试中断遗留孤儿）
```

3e. `stop_fake_upstreams` 函数后新增三个基建函数：

```python
def _drain_pipe(pipe, buf: list) -> None:
    # 吞掉的是 pipe 读取中的 OSError（子进程退出后读端关闭等）：drain 是防"管道满
    # 阻塞子进程 stderr write"的 best-effort，读端异常即终止，无恢复路径。
    try:
        for chunk in iter(pipe.readline, b""):
            buf.append(chunk)
    except OSError:
        return


def stderr_text(proc) -> str:
    """drain buffer 的文本视图（运行期持续收集，替代 terminate 后一次性 pipe.read）。"""
    return b"".join(proc.stderr_buf).decode("utf-8", "replace")


def kill_registered(timeout: float = 2.0) -> list:
    """递进清理全部注册过的代理子进程（terminate → wait → kill）；返回被强杀的 pid。"""
    force_killed = []
    for proc in _PROCS:
        if proc.poll() is not None:
            continue
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # SIGKILL 已发出，内核回收只是调度时序问题，无其他路径可达。
                pass
            force_killed.append(proc.pid)
    del _PROCS[:]
    return force_killed
```

3f. `start_proxy` 的 `proc = subprocess.Popen(...)` 段替换为（wait_port 之前插入 drain + 登记）：

```python
    proc = subprocess.Popen(
        [sys.executable, PROXY_SCRIPT],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout_buf = []
    proc.stderr_buf = []
    for pipe, buf in ((proc.stdout, proc.stdout_buf), (proc.stderr, proc.stderr_buf)):
        threading.Thread(target=_drain_pipe, args=(pipe, buf), daemon=True).start()
    _PROCS.append(proc)
    wait_port(proxy_port)
    wait_port(admin_port)
```

3g. 全部 9 处 stderr 获取点替换（`proc.stderr.read().decode("utf-8", "replace")` → `stderr_text(proc)`；`self.proc.stderr.read()` → `stderr_text(self.proc)`）——涉及 `run_scenario`、`AdminIntegrationTest.tearDown`、`test_client_abort_is_quiet_and_not_error`、`test_daily_bucket_via_sse_and_persist`、`test_upstream_500_counts_into_daily_errors_upstream`（2 处）、`test_daily_buckets_resume_from_persist`、`test_stats_counters_resume_from_persist`、`test_client_abort`（stderr 变量赋值处）。

3h. main-guard 替换为：

```python
if __name__ == "__main__":
    import atexit
    atexit.register(kill_registered)
    # 默认 SIGTERM 直接终止不跑 atexit：转成解释器关闭路径，兜底清理才可执行
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    unittest.main(verbosity=2)
```

（unittest discover 场景 main-guard 不执行、无兜底——当前流程只直接跑本文件，spec Exclusions 已豁免。）

**Step 4 绿相**：

```
/usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
```

通过标准：`Ran 31 tests` + `OK` + exit 0。

## Task 2: 对照实验 + 中断实测 + 归档

2a. drain 版对照实验（R31-S1 同场景 + drain）：预期 6s 内退出（对照 HUNG）。

2b. 中断场景实测：后台起全量测试 → 运行中 SIGTERM 测试进程 → 2s 后 `pgrep -f ctyun-stream-fix-proxy.py` 仅剩 launchd 服务 1 个。

2c. 生产代理验证：`_safe_log_stderr` 改动了代理 py → `launchctl kickstart -k` 重启 → `/api/stats` requests_total 延续。

2d. 归档同步 + cmp byte-identical；删 /tmp 实验脚本。

## 风险与回退

- drain 线程在 EOF 后自然结束；`stderr_text` 与原"terminate 后 read"语义一致。
- 回退：归档目录上一版（v1.2）两文件 cp 回 + launchd 重启。
