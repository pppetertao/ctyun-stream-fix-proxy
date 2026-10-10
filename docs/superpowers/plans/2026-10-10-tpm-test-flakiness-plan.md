# PLAN: TpmRateLimitTest 两个 flaky 用例确定性化（stderr 轮询 + 墙钟放宽）

## Header

| 项 | 值 |
|---|---|
| 日期 | 2026-10-10 |
| spec | `docs/superpowers/specs/2026-10-10-tpm-test-flakiness-design.md`（分支 commit 356a439） |
| worktree / 分支 | `.worktrees/tpm-test-flakiness` / `fix/tpm-test-flakiness`（main = ea3fbbc） |
| 执行者 | executor（3 卡全 tier A，逐卡串行） |
| Python | `/usr/bin/python3`（本机无 `python`，一律用它） |
| Baseline | `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿 389 用例；两目标用例隔离实跑绿（queue_timeout 3.1s / settle 1.6s） |
| whole-branch 收尾 | 3 卡完成后跑「whole-branch 收尾验证」节（spec Acceptance #1 全量 3 连跑 + #3 隔离 10 连跑），由最后一卡执行者同次 dispatch 收尾 |
| 提交 | 执行者不 commit，改动留工作树由主代理审查后统一提交 |

锚点口径：本计划行号为当前 HEAD（356a439）实测行号；前卡落地后测试文件行号会漂移，**所有 Edit 一律用卡内给出的文本锚点（old_string 全文唯一匹配），不按行号定位**。锚点唯一性已逐一 grep 实测（各 count=1）。

## Global Constraints

1. **只动一个文件**：`ctyun-stream-fix-proxy.test.py`；生产 `ctyun-stream-fix-proxy.py` 零改（spec Exclusions）。不 skip 用例、不删断言、除 spec 三处改动点外不放松任何断言。
2. **断言强度不低于现状**：两处 stderr 断言加轮询后，**原 assert 语句原文保留**作兜底（轮询只增加等待，不改变判定条件）；`elapsed` 1.0→5.0 是 spec 唯一批准的放宽（语义等价论证见卡 2）。判定标准：把轮询循环删掉，剩余断言与改前逐字等价。
3. **轮询惯例**：`deadline = time.time() + 3.0` + `time.sleep(0.05)` 的有界轮询，与文件既有 `wait_port`（:479-487）的 deadline+50ms 惯例一致（`deadline = time.time()` 形态文件内已有 17 处）。禁裸 `sleep` 固定等待替代轮询；不加 helper 函数、不加 import（`time` 已在 :30 导入，`deadline`/`stderr` 变量名在两方法作用域内无冲突）。
4. **TDD 卡内顺序（强制）**：每卡五步——RED 守卫（sabotage Edit → 跑目标用例 → 预期红）→ REVERT（Edit 还原 → 预期绿）→ FIX（真实改动）→ GREEN（跑目标用例 → 预期绿）→ STRENGTH 复验（对改动后代码再 sabotage → 预期红 → 还原 → 预期绿）。每步跑命令贴实际 stdout/exit code；**预期不符即 STOP 报告主代理**（sabotage 跑出绿 / green 跑出红 / Edit 锚点未命中 / 失败点不是本卡目标断言——都不自行改预期、不 blind-retry）。
5. **运行形态纠偏（实测）**：目标用例统一用 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_xxx`（unittest.main argv 形态，已实测绿）。**spec Acceptance #3 原文写的 `-m unittest ctyun-stream-fix-proxy.test.TpmRateLimitTest.…` 实测 ImportError（`FAILED (errors=1)`——连字符目录名不是合法模块路径，且仓库无该包 `__init__.py`）**，全 PLAN 按 argv 形态执行，语义等同（同一用例、同一断言路径）。
6. **spec Exclusions 冻结**：不动 `TpmRateLimitTest` 其余 4 个用例（:7136 window_roll、:7213 queue_full、:7287 passthrough、:7300 no_auth、:7313 stats_endpoint——spec 排除清单 5 个）、不动 settle 用例既有的 `time.sleep(0.5)`（:7279）、不动 `TpmAdmitHookTest` 及任何非 TPM 限流测试。
7. 只改本计划列出的锚点；禁顺手改其它代码/注释/格式。

---

## 卡1【tier A】`test_queue_timeout_returns_429` stderr 断言改有界轮询

- **文件**：`ctyun-stream-fix-proxy.test.py`
- **位置**：`TpmRateLimitTest.test_queue_timeout_returns_429`，现 :7207-7209（方法体尾段，`assertEqual(len(calls), 1)` 之前）
- **目标**：消除 stderr drain 线程与测试主线程的无锁竞态（spec R31-S2）：满载下 daemon `_drain_pipe`（:410）可能滞后 `readline()`，`stderr_text()`（:420）返回旧缓冲导致 `assertIn("result=tpm-queue-timeout", …)` 偶发红。改为有界轮询等该行进入 drain 缓冲，轮询不到由原 assert 兜底。
- **针对性验证命令**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_queue_timeout_returns_429`（预期尾行 `OK`，rc=0，约 3s；RED/STRENGTH 步预期 `FAILED`，rc=1）
- **spec 对应**：Files §2 第 1 条；Acceptance #2 的强度守卫在本卡 Step 5 落地。

### Step 1 RED（等价守卫，改动前）——sabotage 断言目标串，预期红

证明"该行缺席时此断言必红"这一性质当前成立（也是改动后必须保住的性质）。Edit（old_string 唯一，grep count=1）：

old_string：

```python
        self.assertIn("result=tpm-queue-timeout", stderr,
                      "REQ line must carry result=tpm-queue-timeout, stderr:\n" + stderr)
```

new_string：

```python
        self.assertIn("result=tpm-queue-timeout-SABOTAGE", stderr,
                      "REQ line must carry result=tpm-queue-timeout, stderr:\n" + stderr)
```

（message 行不动：失败信息仍打印真实 stderr，失败形态与"行缺席"完全同径。）跑验证命令，预期 `FAILED`、rc=1。

### Step 2 REVERT——还原 Step 1 的 Edit（new/old 对调），跑命令预期 `OK`、rc=0

### Step 3 FIX——真实改动

old_string（实测 :7206-7209，唯一）：

```python
        # REQ 行 result=tpm-queue-timeout；超时请求不触上游（calls 仅首发 1 次）
        stderr = stderr_text(self.proc)
        self.assertIn("result=tpm-queue-timeout", stderr,
                      "REQ line must carry result=tpm-queue-timeout, stderr:\n" + stderr)
```

new_string：

```python
        # REQ 行 result=tpm-queue-timeout；超时请求不触上游（calls 仅首发 1 次）。
        # 满载下 drain 线程调度可能滞后于 HTTP 响应返回（spec R31-S2 竞态）：
        # 有界轮询 3s/50ms 等该行进入 drain 缓冲（同 wait_port 的 deadline+50ms 惯例），
        # 轮询不到则由下方 assertIn 兜底失败——断言强度不降。
        deadline = time.time() + 3.0
        stderr = stderr_text(self.proc)
        while "result=tpm-queue-timeout" not in stderr and time.time() < deadline:
            time.sleep(0.05)
            stderr = stderr_text(self.proc)
        self.assertIn("result=tpm-queue-timeout", stderr,
                      "REQ line must carry result=tpm-queue-timeout, stderr:\n" + stderr)
```

（轮询命中即退出 = "轮询到即 break"；原 assert 保留为超时兜底，判定条件逐字未动。）

### Step 4 GREEN——跑验证命令，预期 `OK`、rc=0

### Step 5 STRENGTH 复验（spec Acceptance #2 等价，不改生产文件）——对改动后代码重做 Step 1 的同一条 sabotage Edit（assert 两行未被 Step 3 改动，锚点仍有效），跑命令预期 `FAILED`、rc=1（轮询条件用真实串会立即命中退出，随即 sabotage 的 assert 红——“期望行缺席/不匹配 → 必红”性质保住）；再 REVERT 还原，跑命令预期 `OK`、rc=0。

---

## 卡2【tier A】`test_usage_settle_releases_budget` 墙钟断言 1.0s→5.0s

- **文件**：`ctyun-stream-fix-proxy.test.py`
- **位置**：`TpmRateLimitTest.test_usage_settle_releases_budget`，现 :7271-7273（第二请求 `assertEqual(status2, 200)` 之后）
- **目标**：满载下第二请求即便立即准入（settle 已释放预算，无窗口等待），TCP 连接 + 线程调度的端到端耗时也可 >1s，`time.time()` 精度叠加负载使 1s 阈值过紧（spec R31-S2 第二竞态）。放宽到 5.0s：本测试 `CTYUN_TPM_WINDOW_S=10`（:7255），真排队等窗口滚过的路径必然 >5s，故"无窗口等待即放行"语义不变。
- **针对性验证命令**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_usage_settle_releases_budget`（预期尾行 `OK`，rc=0，约 1.6s；RED/STRENGTH 步预期 `FAILED`，rc=1）
- **spec 对应**：Files §2 第 2 条；spec Risks 第 2 条（settle 本身 stall 的场景不属本卡，已有专属超时测试覆盖，不加断言）。

### Step 1 RED（等价守卫，改动前）——把阈值打到必红值，预期红

证明 elapsed 断言当前是活的（真实耗时远大于 0.001s，实测隔离跑 1.6s）。Edit（old_string 唯一，grep count=1）：

old_string：

```python
        self.assertLess(elapsed, 1.0,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
```

new_string：

```python
        self.assertLess(elapsed, 0.001,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
```

跑验证命令，预期 `FAILED`、rc=1。

### Step 2 REVERT——还原 Step 1 的 Edit（new/old 对调），跑命令预期 `OK`、rc=0

### Step 3 FIX——真实改动

old_string（同 Step 1 的 old_string，逐字）：

```python
        self.assertLess(elapsed, 1.0,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
```

new_string：

```python
        # 满载下端到端耗时（TCP 连接 + 线程调度）可 >1s（spec R31-S2 墙钟竞态）；
        # 放宽到 5s 仍远小于本测试窗口 10s（WINDOW_S=10），排队等待路径必然 >5s，
        # "settle 后无窗口等待即放行"语义不变——断言强度不低于现状。
        self.assertLess(elapsed, 5.0,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
```

### Step 4 GREEN——跑验证命令，预期 `OK`、rc=0

### Step 5 STRENGTH 复验——对改动后代码 sabotage：

old_string：

```python
        self.assertLess(elapsed, 5.0,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
```

new_string：

```python
        self.assertLess(elapsed, 0.001,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
```

跑命令预期 `FAILED`、rc=1；再 REVERT（new/old 对调还原到 5.0 版），跑命令预期 `OK`、rc=0。

---

## 卡3【tier A】`test_usage_settle_releases_budget` stderr tpm=2 计数断言改有界轮询

- **文件**：`ctyun-stream-fix-proxy.test.py`
- **位置**：`TpmRateLimitTest.test_usage_settle_releases_budget`，现 :7274-7277（elapsed 断言之后、`time.sleep(0.5)` 之前）
- **目标**：与卡 1 同一竞态（spec R31-S2）：满载下 drain 滞后使 `stderr.count("tpm=2") < 2` 偶发红。改为有界轮询等两行 tpm=2 齐备，轮询不到由原 assert 兜底。
- **前置依赖**：卡 2 已落地（两卡同方法串行执行；本卡锚点与卡 2 改动区不重叠，文本锚点不受影响）。
- **针对性验证命令**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_usage_settle_releases_budget`（预期尾行 `OK`，rc=0，约 1.6s；RED/STRENGTH 步预期 `FAILED`，rc=1）
- **spec 对应**：Files §2 第 3 条。

### Step 1 RED（等价守卫，改动前）——sabotage 计数目标串，预期红

Edit（old_string 唯一，grep count=1）：

old_string：

```python
        self.assertGreaterEqual(stderr.count("tpm=2"), 2,
                                "both REQ lines must carry tpm=2, stderr:\n" + stderr)
```

new_string：

```python
        self.assertGreaterEqual(stderr.count("tpm=9"), 2,
                                "both REQ lines must carry tpm=2, stderr:\n" + stderr)
```

（message 行不动。`tpm=2` 在本文件仅出现在本卡锚点区域 3 行内【grep 实测】，sabotage 不波及其他用例。）跑验证命令，预期 `FAILED`、rc=1。

### Step 2 REVERT——还原 Step 1 的 Edit（new/old 对调），跑命令预期 `OK`、rc=0

### Step 3 FIX——真实改动

old_string（实测 :7274-7277，唯一）：

```python
        # stderr：两次 REQ 行 tpm=2（settle 后按实际 usage 记）
        stderr = stderr_text(self.proc)
        self.assertGreaterEqual(stderr.count("tpm=2"), 2,
                                "both REQ lines must carry tpm=2, stderr:\n" + stderr)
```

new_string：

```python
        # stderr：两次 REQ 行 tpm=2（settle 后按实际 usage 记）。满载下 drain 线程调度
        # 可能滞后于 HTTP 响应返回（spec R31-S2 竞态，同 queue-timeout 用例）：
        # 有界轮询 3s/50ms 等两行齐（同 wait_port 的 deadline+50ms 惯例），
        # 轮询不到则由下方 assert 兜底失败——断言强度不降。
        deadline = time.time() + 3.0
        stderr = stderr_text(self.proc)
        while stderr.count("tpm=2") < 2 and time.time() < deadline:
            time.sleep(0.05)
            stderr = stderr_text(self.proc)
        self.assertGreaterEqual(stderr.count("tpm=2"), 2,
                                "both REQ lines must carry tpm=2, stderr:\n" + stderr)
```

（`stderr.count("tpm=2")` 判定语义与原 assert 完全一致；settle 后 REQ 行记 `tpm=2`，预算 200 不产生 `tpm=200` 类子串误判——REQ 行 tpm= 字段记的是每请求 est/settle 值，非 limit 值。）

### Step 4 GREEN——跑验证命令，预期 `OK`、rc=0

### Step 5 STRENGTH 复验——对改动后代码重做 Step 1 的同一条 sabotage Edit（assert 两行未被 Step 3 改动，锚点仍有效；此时 while 条件仍用真实串 `tpm=2` 会命中退出，sabotage 的 assert 随即红），跑命令预期 `FAILED`、rc=1；再 REVERT 还原，跑命令预期 `OK`、rc=0。

---

## whole-branch 收尾验证（3 卡全绿后，最后一卡执行者同次 dispatch 收尾；结果贴 stdout/exit code）

1. **spec Acceptance #1——全量 3 连跑**（每轮约 150-160s，共约 8 分钟）：

```bash
for i in 1 2 3; do
  /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/tpmfix_full_$i.log 2>&1
  echo "run$i rc=$?"
  tail -3 /tmp/tpmfix_full_$i.log
  grep -E "^(FAILED|ERROR)" /tmp/tpmfix_full_$i.log
done
```

预期：3 轮均 `rc=0`、各 `Ran 389 tests`、尾行 `OK`；grep 无 FAILED/ERROR 行（无输出即过）。

2. **spec Acceptance #3——两用例隔离 10 连跑**（按 Global Constraints #5 的 argv 形态纠偏执行；每轮约 5s）：

```bash
for i in $(seq 1 10); do
  /usr/bin/python3 ctyun-stream-fix-proxy.test.py \
    TpmRateLimitTest.test_queue_timeout_returns_429 \
    TpmRateLimitTest.test_usage_settle_releases_budget > /tmp/tpmfix_iso_$i.log 2>&1
  echo "run$i rc=$? $(tail -1 /tmp/tpmfix_iso_$i.log)"
done
```

预期：10 轮均 `rc=0` 且尾行 `OK`。

3. **失败处置**：任一轮红 → 保留对应 /tmp 日志全文，STOP 报告主代理（按 spec Risks 第 1 条：轮询 3s 仍不足属独立 episode 范畴，不在卡内续修）。

4. **spec Acceptance #2 不在本节重跑**：已由三卡各自的 Step 5（改动后 sabotage → 红）在测试侧等价落地——期望行/阈值缺席时断言必红的性质，与"生产逻辑破坏 → 测试红"是同一条 assert 路径，且避免了本 episode 禁改的生产文件。

---

## Plan Self-Review（作者自查 5 项）

1. **spec coverage**：spec Files 3 处改动点 → 卡 1/2/3 一一对应（锚点文本与 spec R31-S1 grep 行号逐一核对，Read 实测 :7206-7209 / :7271-7273 / :7274-7277 逐字一致）；Acceptance #1/#3 → whole-branch 收尾节，#2 → 三卡 Step 5（口径注记见收尾第 4 条）；Risks 两条 → 收尾第 3 条失败处置 + 卡 2 目标节；Exclusions → Global Constraints #1/#6。无遗漏。
2. **placeholder 0**：三卡所有 Edit 的 old_string/new_string 均为逐字代码，无指令式占位；所有验证命令可直接复制执行（argv 形态已实跑验证）。
3. **type consistency**：`deadline`（float）/`stderr`（str）/`.count`（int）均为局部变量，两方法作用域内无同名冲突（Read 实测方法全文体）；`time` 已在 :30 导入，零新增 import/helper。
4. **可落盘性**：9 个 Edit 锚点全部 grep 实测唯一（count=1：`超时请求不触上游` / `assertLess(elapsed, 1.0` / `settle 后按实际 usage 记` / `REQ line must carry result=tpm-queue-timeout` / `both REQ lines must carry tpm=2` / `settle must release budget immediately` / 三个 FIX 的多行块）；卡间依赖仅卡 3 依赖卡 2（同方法串行），锚点区不重叠互不失效。
5. **锚点实测**：`_drain_pipe` :410 / `stderr_text` :420 / `wait_port` :479-487（deadline+50ms 惯例）/ `start_proxy` drain 线程 :513-514 / 两目标用例实跑绿（3.1s、1.6s）/ `-m unittest` 形态实测 ImportError——共 6 处 spot-check，全部与 spec 一致或已纠偏注记。
