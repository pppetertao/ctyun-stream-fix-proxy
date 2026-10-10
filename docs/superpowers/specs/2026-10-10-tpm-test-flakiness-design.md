## Goal

消除 `TpmRateLimitTest` 两个用例在全量满载运行下的偶发 flaky：`test_queue_timeout_returns_429`（stderr 断言竞态）和 `test_usage_settle_releases_budget`（stderr 断言之墙钟断言竞态）。断言强度不低于现状，不删断言不跳过，不修改生产 TPM 逻辑。

## Files to Change

- **`ctyun-stream-fix-proxy.test.py`** 仅此一个文件
  - `ctyun-stream-fix-proxy.test.py:7207-7209` `test_queue_timeout_returns_429()` -- 将 `stderr = stderr_text(self.proc); assertIn("result=tpm-queue-timeout", stderr)` 替换为有界轮询（3s deadline，50ms 间隔），轮询到即 break，超时后原断言在外层兜底不丢
  - `ctyun-stream-fix-proxy.test.py:7271` `test_usage_settle_releases_budget()` -- 将 `assertLess(elapsed, 1.0)` 替换为 `assertLess(elapsed, 5.0)`（满载下 post_sse_auth 可 >1s，5s 仍远低于窗口滚过 10s，语义等价"无窗口等待"）
  - `ctyun-stream-fix-proxy.test.py:7275-7277` 同测试 -- 将 `stderr = stderr_text(self.proc); assertGreaterEqual(stderr.count("tpm=2"), 2)` 替换为同款有界轮询（3s deadline，50ms 间隔），轮询到即 break

## Acceptance Criteria

1. 两个修改后的测试在满载全量下连续 3 次全量运行全绿，命令：`python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -5`（观察 OK/fail 计数），预期 3 次均 `OK (skipped=0)`，stderr 无 FAILED/ERROR 行。
2. 逻辑真坏（非竞态）场景仍需正确捕获：若生产 TPM 超时逻辑被破坏不返回 429，`test_queue_timeout_returns_429` 应失败（证明断言强度未降级）。
3. 隔离运行两个用例各 10 次全绿：`python3 -m unittest ctyun-stream-fix-proxy.test.TpmRateLimitTest.test_queue_timeout_returns_429 ctyun-stream-fix-proxy.test.TpmRateLimitTest.test_usage_settle_releases_budget 2>&1` 预期 10 次 `OK`。

## Risks

- **轮询上限 3s 仍不足**：极端超载下 drain_pipe 线程延迟 >3s，轮询 timeout → 兜底断言可能仍红。概率极低；真出现则拆 stderr 断言为单独可靠通道（如 /api/diag 日志端点），属独立 episode 范畴。
- **elapsed < 5.0 在极慢环境下与 WINDOW_S=10 接近**：5s 仍远小于 10s，但若 settle 本身延迟 >5s（上游极度 stall），status=200 也会通过。此场景不属于 flaky 修复范围——上游 stall 已有专属超时测试覆盖。

## Exclusions

- 不修改 `ctyun-stream-fix-proxy.py` 任何一行（生产 TPM 逻辑零改）。
- 不修改 `TpmRateLimitTest` 其他 4 个测试（`test_queue_exhaustion_and_window_roll`、`test_queue_full_immediate_429`、`test_sse_passthrough_under_rate_limiting`、`test_no_auth_bypasses_tpm`、`test_tpm_stats_endpoint`）。
- 不修改 `TpmAdmitHookTest` 或任何非 TPM 限流测试。
- 不新增测试框架依赖或 helper 函数（两处轮询均为就地 5 行内联循环，不改测试基建）。

## R31 Evidence

[R31-S1] 两处 flaky 断言现场（grep 实测）

```
$ grep -n "result=tpm-queue-timeout\|assertLess(elapsed, 1\.0)\|stderr\.count(\"tpm=2\")" ctyun-stream-fix-proxy.test.py
ctyun-stream-fix-proxy.test.py:7208:        self.assertIn("result=tpm-queue-timeout", stderr,
ctyun-stream-fix-proxy.test.py:7209:                      "REQ line must carry result=tpm-queue-timeout, stderr:\n" + stderr)
ctyun-stream-fix-proxy.test.py:7271:        self.assertLess(elapsed, 1.0,
ctyun-stream-fix-proxy.test.py:7276:        self.assertGreaterEqual(stderr.count("tpm=2"), 2,
```

```
$ grep -n "queue_timeout_returns_429\|settle_releases_budget" docs/superpowers/followups.md
docs/superpowers/followups.md:2:ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_queue_timeout_returns_429 | 偶发 flaky（全量 6 跑 1 红：queue-timeout seam ~2s 在满载下竞态；隔离 8/8 绿；TPM 限流既有测试，与本 episode 无关） | 建议单独 episode：放宽 seam 或改轮询断言
docs/superpowers/followups.md:5:ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_usage_settle_releases_budget | 偶发 flaky（满载全量下时序竞态：assertLess(elapsed,1.0)+stderr REQ 行计数≥2；隔离复跑绿、baseline 两次全量绿；TPM 限流既有测试，与本 episode 无关） | 建议单独 episode：放宽时序断言或改轮询
```

```
$ grep -n "def _drain_pipe\|def stderr_text\|flush=True" ctyun-stream-fix-proxy.test.py
ctyun-stream-fix-proxy.test.py:410:def _drain_pipe(pipe, buf: list) -> None:
ctyun-stream-fix-proxy.test.py:420:def stderr_text(proc) -> str:
```

```
$ grep -n "def _safe_log_stderr" ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:1682:def _safe_log_stderr(msg: str) -> None:
```

[R31-S2] 根因：stderr pipe drain 线程与测试主线程之间的无锁竞态

生产侧 `_safe_log_stderr`（`ctyun-stream-fix-proxy.py:1682-1693`）用 `print(msg, file=sys.stderr, flush=True)` 写 REQ 行——flush 确保入 OS pipe buffer。测试侧 `start_proxy`（`ctyun-stream-fix-proxy.test.py:513-514`）启 daemon `_drain_pipe` 线程，用 `pipe.readline()` 逐行读入 `proc.stderr_buf`。`stderr_text`（`:420-422`）直接 `b"".join(proc.stderr_buf)` 返回。

满载下 daemon 线程 CPU 调度滞后：`post_sse_auth` 收到 HTTP 响应返回时，生产侧可能已 flush REQ 行到 pipe，但 daemon 尚未执行 `readline()` + `buf.append()`，导致 `stderr_text` 返回旧缓冲。两处 flaky 均为此同一竞态。

第二个测试额外有 `elapsed < 1.0`（`:7271`）的墙钟竞态：满载下即便请求立即准入（无队列等待），TCP 连接 + 线程调度的端到端耗时也可 >1s，`time.time()` 精度叠加系统负载使 1s 阈值过紧。