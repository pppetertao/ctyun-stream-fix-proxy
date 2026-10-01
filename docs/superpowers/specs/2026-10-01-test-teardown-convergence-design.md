# 2026-10-01 测试 teardown flake 收敛 + tempdir 泄漏修复 — design spec

## Goal

消除全量套件约 1/6 概率 `proc.wait(timeout=5)` 抛 `subprocess.TimeoutExpired` 的 flake（proxy SIGTERM handler 落盘 stats 慢），收敛约 70 处 terminate/wait/stderr_text/rmtree 块到单一 helper；同时修复 3 处 `self.proc` 重赋型 persist_dir 泄漏（实测 followups.md:5 登记）。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py` — 唯一文件，三组改动：

### 1.1 新增模块级 helper（:408 `kill_registered` 之后，其注释风格对齐）

```python
def stop_proxy(proc, timeout: float = 10.0) -> bool:
    """terminate → wait(timeout)；超时则 kill 再 wait(5)。
    随后 stderr_text + rmtree persist_dir。返回是否强杀过。
    proc 为 None 或已 reap 安全退出（poll 判活，免 ChildProcessError）。"""
    if proc is None:
        return False
    killed = False
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            killed = True
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # SIGKILL 已发出，内核回收只是调度时序问题（同 kill_registered:420-423）
    stderr_text(proc)
    pd = getattr(proc, "persist_dir", None)
    if pd:
        shutil.rmtree(pd, ignore_errors=True)
    return killed
```

默认 timeout=10 覆盖 SIGTERM 慢落盘余量（现存唯一 wait=10 实例在 :2961，落地后其 10 秒值由 helper 默认继承，:2959-2960 注释可保留）。

### 1.2 tearDown 方法批量替换（12 处）

分两型精确 Edit（old_string 含上下文行，确保唯一性）：

**标准款（8 处）——实测行号 :2600-2605, :4185-4190, :4308-4313, :4469-4474, :4969-4974, :5515-5520, :5556-5561；还有一处 :4230 区域（ModelPricingSeamTest）待 implementer 确认：**

```python
self.proc.terminate()
self.proc.wait(timeout=5)
stderr_text(self.proc)
shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
```

四行整体替换为：

```python
stop_proxy(self.proc)
```

每处 `stop_fake_upstreams()` 紧跟保留。

**`if self.proc:` 卫款（4 处）——实测 :3937-3943, :4567-4573, :4617-4623, :5069-5075：**

卫段整体替换为 `stop_proxy(self.proc)`（helper 容忍 None，卫语义内化）；`stop_fake_upstreams()` 保留于原缩进位（这些类里的 stop 在卫外侧 :4573/:4623/:5075 或卫内侧 :3943——implementer 逐处核对缩进）。

### 1.3 内联 terminate-wait 块分流策略

约 60 处 test body 内出现 `self.proc.terminate(); self.proc.wait(timeout=5)` 后接 stderr_text/rmtree。分三型：

- **型 A（wait 后不读旧 persist_dir、直接重启）**：替换为 `stop_proxy(self.proc)`。典型——BodyErrorTest :3947-3951（已有 rmtree 但可收敛）与 restart-with-seed 型（如 :2825-2827？implementer 依 anchor 判断）。
- **型 B（wait 后要读 settings.json——约 5 处）**：保留手工 `self.proc.terminate(); self.proc.wait(timeout=10)`（timeout 改 10 吸慢落盘），读完数据后 `shutil.rmtree(self.proc.persist_dir, ignore_errors=True)` + 再重赋 `self.proc = start_proxy(...)`。锚点：:2815-2819、:2949-2955、:2958-2963（wait 已是 10）、:3076-3079、:3120-3125 等——implementer 逐处确认旧 dir 读取行后补 rmtree。
- **型 C（helper 函数内的 proc 形参，非 self）**：:414-423 已有 `kill_registered`（atexit 清理，不动）；:553-557 `make_proxy_wait` 内部块（:553-557）——保留或委托 `stop_proxy(proc, timeout=10)`；implementer 依 anchor 判断。

### 1.4 重启泄漏修复（3 处）

在 `self.proc = start_proxy(...)` 重赋之前补 `shutil.rmtree`：

- :2795-2798（test_stats_counters_resume_from_persist）：terminate→wait→stderr_text → 重赋前加 `shutil.rmtree(self.proc.persist_dir, ignore_errors=True)`。
- :2958-2963（test_daily_buckets_resume_from_persist）：同款，wait 已是 10 保留，重赋前加 rmtree。
- :4537-4540（test_502_response_carries_x_request_id）：:4537-4539 现有 terminate+wait+stderr_text，重赋 :4540 前加 `shutil.rmtree(self.proc.persist_dir, ignore_errors=True)`（followups.md:5 登记的泄漏）。

### 1.5 ProbeLoopTest._teardown_proc 收敛

实测 :5341-5347 `_teardown_proc` 静态方法 body 三行委托 `stop_proxy(proc)`（替换 terminate+wait+stderr_text+rmtree）+ `stop_fake_upstreams()`；staticmethod 签名与 :5349-5362/:5364-5380 等 try/finally 调用点不变。

### 1.6 新增单测

模块级（近 :1970 `test_stderr_text_joins_buffer` 区域）加 `StopProxyKillPathTest`：spawn `sys.executable -c "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"`，`stop_proxy(proc, timeout=0.5)` 断言返回 True 且 `proc.poll()` 非 None；以及 `stop_proxy(None)` 返回 False。Mock 测试不需要；FakeProc 可用于无遗害验证。

## Acceptance Criteria

- 连续 3 次全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（贴 3 次 stdout/exit code）；flake 不得复现。
- 新增 stop_proxy 单测通过。
- 全量后 `ls -d /var/folders/*/T/ctyun-proxy-test-* 2>/dev/null | wc -l` 跑前跑后不增（macOS 环境近似验证；非确定性路径若不可靠可跳过）。
- grep `def tearDown` 方法体内无残留裸 `wait(timeout=5)`（helper 内 kill 后 `wait(5)` + 型 B 手工块 `wait(10)` 除外）。
- followups.md:3 与 :5 两条目删除（DELIVER 消化）。

## Risks

- 替换面 ~70 块、唯一性靠上下文行锚定：错锚会改坏相邻测试；implementer 每块编辑后逐卡 `python3 ctyun-stream-fix-proxy.test.py <ClassName>` 验证。
- helper 内 `rmtree` 对型 B（wait 后读 dir）有破坏性——必须走手工保留路径；implementer grep `settings.json` 确认所有读旧 dir 的行均已识别。
- kill 路径下 `stderr_text` 可能丢 SIGTERM 慢落盘的最后几行——仅 kill 兜底场景受影响；正常路径断言 stderr 含特定字符串的测试（如 :4519-4525 REQ 行匹配）走 10s wait 不移到 kill 分支。
- ProbeLoopTest._teardown_proc 收敛后 try/finally 仍调 stop_fake_upstreams 两次（helper 内不含 stop）——此次重复是 idempotent（`del FAKE_SERVERS[:]` 二次调无副作用），可保留。

## Exclusions

- 不改生产代码 `ctyun-stream-fix-proxy.py`（SIGTERM 落盘慢是 flake 根因之一，但属持久化正确性行为，不在本 spec）。
- 不动仅含 `stop_fake_upstreams()` 的无 proc 模块级测试（:2440/:2468/:2496/:2527）；不动 setUpClass。
- TPM 相关 followups.md:9-13 条目不消化。
- 不引入 pytest 或新依赖。

## R31 Evidence

[R31-S1] 现状证据（本会话实测）：teardown 清理逻辑零散分布——12 个 tearDown 方法 + 1 个静态 _teardown_proc，另有大量 test body 内联 terminate/wait 块：

```
$ grep -n 'def tearDown\|def _teardown_proc' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py
2600:    def tearDown(self) -> None:
3937:    def tearDown(self) -> None:
4185:    def tearDown(self) -> None:
4230:    def tearDown(self) -> None:
4308:    def tearDown(self) -> None:
4469:    def tearDown(self) -> None:
4567:    def tearDown(self) -> None:
4617:    def tearDown(self) -> None:
4969:    def tearDown(self) -> None:
5069:    def tearDown(self) -> None:
5342:    def _teardown_proc(proc) -> None:
5515:    def tearDown(self) -> None:
5556:    def tearDown(self) -> None:
```

```
$ grep -c '\.terminate()' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py
102
$ grep -c 'wait(timeout=5)' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py
100
```

[R31-S2] 根因：102 处 terminate 中 100 处配 wait(timeout=5) 等待 proxy SIGTERM 落盘 stats，慢落盘时超时抛 subprocess.TimeoutExpired（followups.md:3："全量套件约 1/6 概率……复跑即绿"）；3 处 test body 重赋 self.proc 前不清理旧实例 persist_dir——followups.md:5 登记 RequestIdTest.test_502_response_carries_x_request_id（实测 :4537-4540 无 rmtree 即重赋），同型泄漏实测 :2795-2798（test_stats_counters_resume_from_persist）与 :2958-2963（test_daily_buckets_resume_from_persist）。需求/裁决来源：followups.md:3/:5 + 用户 2026-10-01 "开 task 处理本会话的遗留问题"。裁决：新增 stop_proxy helper（terminate → wait(10) → 超时 kill 再 wait → stderr_text → rmtree，返回是否强杀），12 处 tearDown 分标准款/卫款精确替换；读 settings.json 的内联块走型 B 手工保留（wait 改 10、读后 rmtree）。
