# ctyun-proxy 测试孤儿进程根修 spec（simplified mode）

背景：followups.md 2026-09-05 条目——测试中断时代理子进程成 PPID=1 孤儿（实测清理 11 个），部分 SIGTERM 不退需 SIGKILL，疑似致全量测试 resume 用例偶发 wait(5s) 超时。本 episode 根修：复现实验实锤根因 → 测试基建加固 + 代理退出路径小加固。全程主代理执行（本会话用户显式指令延续）。

## Goal

① 根因修复（测试侧）：`start_proxy` 对 stdout/stderr PIPE 起 daemon drain 线程读入内存 buffer——消灭"stderr 管道满 + 无人读 → 代理线程卡 write → 解释器退出被拖死 → SIGTERM 不退"链条；② 兜底（测试侧）：子进程全局注册表 + `atexit` 递进清理（terminate → wait(2s) → kill），测试主入口注册 SIGTERM/SIGINT handler 走 sys.exit 触发 atexit——测试异常退出/被信号终止时不再遗留孤儿；③ 代理侧小加固：`_safe_log_stderr(msg)` 统一 stderr 写失败容错（BrokenPipeError/ENOSPC 等 OSError 不炸线程、不阻断 SIGTERM 退出路径）；④ 复现实验结论与根因机制入档（spec R31 Evidence + 记忆），followups 条目标记已处理。

## Files to Change

### 1. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`（现状 ~770 行）

- **常量区（`FAKE_SERVERS = []` 旁）**：`_PROCS = []`（全部 start_proxy 产物的注册表）。
- **`start_proxy`（:128 附近）改造**：`proc.stderr_buf: list[bytes]`、`proc.stdout_buf: list[bytes]` 清空 + 起 2 个 daemon drain 线程（`threading.Thread(target=_drain_pipe, args=(proc.stderr, proc.stderr_buf), daemon=True).start()`，stdout 同理）；`_PROCS.append(proc)`；返回前无其他变化（wait_port 等保留）。
- **新增 `_drain_pipe(pipe, buf: list)`**：`for chunk in iter(pipe.readline, b""): buf.append(chunk)`；OSError 时 return（pipe 死即终，注释自证：drain 是防满 best-effort，读端异常无恢复路径）。
- **新增 `stderr_text(proc) -> str`**：`"".join(...)` 对 `proc.stderr_buf` decode utf-8 replace——替换全部 9 处 `proc.stderr.read().decode("utf-8", "replace")` 调用点（`run_scenario` 1 处 + AdminIntegrationTest 8 处），断言语义零变化（原 read 发生在 terminate 后，buffer 此时已含全部输出；drain 线程读到 EOF 后自然结束）。
- **新增 `kill_registered(timeout: float = 2.0) -> list[int]`**：遍历 `_PROCS`，对 `poll() is None` 的存活 proc 递进 `terminate()` → `wait(timeout)` → 超时则 `kill()` + `wait(timeout)`；返回被强杀（走到 kill）的 pid 列表；`_PROCS.clear()`。幂等（已死 proc 直接跳过）。
- **`if __name__ == "__main__":` 区**：`import atexit` + `atexit.register(kill_registered)`；`signal.signal(signal.SIGTERM/SIGINT, lambda *_: sys.exit(0))`（默认 SIGTERM 直接终止不跑 atexit，转成解释器关闭路径）；再 `unittest.main(verbosity=2)`。注意 signal 注册放 main-guard（import 本模块做单测的场景 `load_proxy_module` 加载的是代理脚本不是测试脚本，不受影响；unittest discover 下 main-guard 不执行，discover 场景无兜底——当前流程只直接跑本文件，可接受，注释说明）。

### 2. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`（现状 ~930 行）

- **新增 `_safe_log_stderr(msg: str) -> None`（`today_key` 旁）**：`try: print(msg, file=sys.stderr, flush=True) except OSError: pass`，注释自证三件事（吞：stderr 写失败的一切 OSError——BrokenPipeError[管道对端关闭]/ENOSPC[磁盘满] 等；为何无其他路径：stderr 是 best-effort 诊断出口，写失败时无备用通道且失败不得传播——传播会炸请求线程并使 SIGTERM handler 的 except 分支二次抛出阻断退出；限一条语句：仅一个 print）。
- **`_log`（:455 附近）**：print 改 `_safe_log_stderr(...)`。
- **`main()` 内 `_on_signal`（:855 附近）**：except 分支的 print 改 `_safe_log_stderr(...)`——否则磁盘满场景下"落盘失败留痕"自身再抛 OSError，`SystemExit(0)` 不可达，进程卡死（launchd exit-timeout 兜底才 SIGKILL）。
- **不做**：管道满阻塞（write 永久阻塞非异常）无法在代理侧单方面根治——根修在测试 drain；生产 launchd stderr 为文件（`~/.local/log/ctyun-fwd.err`）无管道满风险，Risks 说明。

### 3. 新增 `/tmp` 一次性验证脚本（不留存）：复现实验对照

drain 版对照实验（同 R31-S1 实验，stderr 换 drain 线程消费）预期 SIGTERM 秒退——写进 Acceptance，脚本用后删。

## Acceptance Criteria

- ① `/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` 全绿（28 + 新增 ≥3），贴 stdout + exit 0。
- ② 复现实验对照（drain 版）：同一 1200×502 灌流 + SIGTERM → 6s 内退出（对照 R31-S1 的 HUNG）。
- ③ 中断场景实测：后台起全量测试进程，运行中 SIGTERM 杀测试进程 → 2s 后 `pgrep -f ctyun-stream-fix-proxy.py` 仅剩 launchd 服务 1 个。
- ④ `/usr/bin/python3 -m py_compile` 两文件 exit 0；归档 `~/Documents/project/ctyun-stream-fix-proxy/` cmp byte-identical。
- ⑤ 生产代理不受本 episode 影响：launchd 服务无需重启（改动仅测试文件 + `_safe_log_stderr` 小加固）；若动了代理 py 则 kickstart 重启并确认 `/api/stats` 计数延续。

## Risks

- drain 线程读 EOF 后退出：terminate 后 buffer 定格——`stderr_text` 语义与原"terminate 后 read"一致（原 read 也是 EOF 语义），竞态无新增。
- `_PROCS` 全局表跨测试累积：kill_registered 清空；单测试套件 ≤30 个 proc，内存可忽略。
- SIGTERM→sys.exit 路径下 unittest 不打印结果摘要（被信号打断的运行本来就没有可靠摘要）——可接受。
- CPython 退出路径为何被卡 write 的 daemon 线程拖死（主线程栈停 `PyRun_SimpleFileExFlags` 的锁等待）未精确考古到 CPython 源码行级——不影响修复：drain 消灭卡 write 线程后整条链条不可达（对照实验验证）。
- `_safe_log_stderr` 吞 OSError 的范围：print 仅一条语句，无资源清理逻辑依赖其成功。

## Exclusions

- 不改代理转发/剥行/统计逻辑、不改 launchd plist、不做 stderr 复用/轮转。
- 不考古 CPython finalize 源码行级机制（Risks 已说明不影响修复）。
- 不为 discover 模式补兜底（当前流程只直接执行本文件）。

## R31 Evidence

[R31-S1] 根因复现实验（2026-09-05，/usr/bin/python3 直跑）：起代理（stderr=PIPE 全程无人读）→ 4 线程并发灌 1200 个 502 请求（上游指到死端口，每请求产生一行 stderr error 日志 ≈110KB > 64KB 管道缓冲）→ SIGTERM → **HUNG，wait(6s) 超时需 SIGKILL**；对照（stderr 仅 banner 一行未满）SIGTERM 1s 内退出。挂死时 sample 抓栈：工作线程停 `write` syscall（stderr 管道满），主线程停 `PyRun_SimpleFileExFlags → PyThread_acquire_lock_timed → _pthread_cond_wait`（解释器退出阶段被拖死）。历史后果：PPID=1 孤儿 11 个（含旧版生产代理 SIGTERM 不退，已 -9 清理）。
```
$ /usr/bin/python3 /tmp/ctyun-sigterm-repro.py
blast done, sending SIGTERM
RESULT: HUNG after SIGTERM with unread full stderr -> hypothesis CONFIRMED
$ sample <hung-pid> 1 | grep -c 'write  (in libsystem_kernel'
2
$ lsof -nP -iTCP:7920 -iTCP:7921 -sTCP:LISTEN   # 生产链路不受影响：launchd stderr=文件
Python  40573 peter    7u  IPv4  ... TCP 127.0.0.1:7920 (LISTEN)
```
[R31-S2] 根因：测试基建 `start_proxy` 用 `stderr=subprocess.PIPE` 但只在 terminate 后才 read——运行期间无人消费，请求日志写满 64KB 内核缓冲后代理线程的 stderr write 永久阻塞（PEP 475 对 EINTR 自动重试，无异常路径）；阻塞线程使解释器退出阶段（threading shutdown/finalize）的锁等待不可完成，SIGTERM handler 落盘后 SystemExit 无法走完 → 进程滞留成孤儿。测试无 atexit/信号兜底，unittest 异常退出时 tearDown 不执行 → 孤儿长期存活。
```
$ /usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -3
Ran 28 tests in 11.166s

OK
$ /usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py && echo compile-ok
compile-ok
```
