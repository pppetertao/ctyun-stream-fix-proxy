# PLAN — E2 TPM 按 (key_id, model) 分桶 FIFO 排队（finding C 跨桶头阻塞修复）

- **Spec**: `docs/superpowers/specs/2026-10-09-tpm-per-bucket-fifo-design.md`（worktree 分支 commit a9f59b5，锚点校准至 main 含 E1 dd8ee63）
- **Worktree**: `.worktrees/tpm-per-bucket-fifo`（分支 `fix/tpm-per-bucket-fifo`）
- **基线**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 377 测试全绿（166s）；本机 `python` 不可用，**一律用 `/usr/bin/python3`**
- **卡数**: 2（均 tier B → executor；每卡内 TDD 顺序强制：先写测试实跑贴红 → 再实现实跑贴绿；卡间串行）
- **改动文件**: `ctyun-stream-fix-proxy.py`（生产，TPM 区 :466-729）、`ctyun-stream-fix-proxy.test.py`（测试，TPM 区）
- **不跑全量**: 每卡验证用针对性测试名；whole-branch 全量验证由主代理另派

## Global Constraints

1. **分桶字典不变量（无空队列键）**：`TPM_WAITERS` 键 `(key_id, model)` 存在 ⟺ 该桶等待队列非空。入队用 `setdefault(..., collections.deque())`；准入 `popleft` 后、`_tpm_remove_waiter` 摘除后队列空即 `del TPM_WAITERS[key]`。任何新代码不得留下空 deque 键。
2. **单桶语义完全不变（兼容承诺）**：同桶 FIFO 顺序、deadline + 1s 粒度轮询超时、429 文案与 rejected/timeout 按桶计数、`tpm_settle` 末尾 `notify_all` 防惊群、超大请求空闲豁免——单桶行为逐字节不变。既有 `TpmRateLimitTest`、`TpmAdmitHookTest`、`TpmPerModelTest` 零改动仍绿即为证。
3. **queue-full/timeout 计数按桶维护**：`_tpm_rejected` / `_tpm_timeouts` 本就是 `(key_id, model) -> int` dict（生产 :478/:479），本 episode 不改其键结构与计数点（:615-616/:623-624/:634-635 保持原样）。
4. **tpm_snapshot 对外 JSON 键集零变化**：`config{limit,window_s,queue_max,model_budgets,enabled_models}`、`queue_total`、`buckets[].{key,model,used,remaining,queued,rejected,timeouts}`；dashboard `loadTpmObs`（`ctyun-stream-fix-proxy.py:5266`，消费点 :5273 `queue_total` chip 与 buckets 字段渲染）零改动。
5. **队满口径变更（唯一行为变更，写清）**：`TPM_QUEUE_MAX` 由全局排队总上限变为**按桶**排队上限；env 键名 `CTYUN_TPM_QUEUE_MAX` 与默认值 20 不变；单桶场景逐字节同语义（既有 `test_queue_full_immediate_429` 不改仍绿即证）。
6. **不加 env seam、不加新依赖；锁序不变**：全部新状态读写都在 `TPM_LOCK` 内；`TPM_LOCK → STATS_LOCK` 链（:674-677 注释）不动。
7. **Edit 全部用文本锚点（多行上下文 old_string→new_string），禁用裸行号定位**。行号仅为人工核对参考（基于 a9f59b5 现场实测）。
8. **TDD 铁律（卡内强制）**：每卡先写/改测试并实跑、原样贴 stdout + exit code（红），再动实现，再实跑贴绿。红必须是实跑结果，不接受"预期会红"。
9. **卡1 绿阶段范围限定**：卡1 生产实现落地后，3 个直触 `TPM_WAITERS` 私有结构的既有测试（`test_tpm_admit_queue_full` / `test_tpm_settle_notifies_waiters` / `test_tpm_waiters_identity_removal`）**预期 ERROR**（`AttributeError: 'collections.deque' object has no attribute 'setdefault'/'append'` 等）——这是卡2 的红，**不算回归**；卡1 绿阶段验证命令不含这 3 个，卡2 修复后跑全量 TPM 针对性集合。
10. **测试文件已有 import**：`collections`(:12)、`threading`(:29)、`time`(:30) 均已存在，新测试代码不加 import。
11. **卡间串行**：卡1 完成（含验证贴证）并 task review 通过后才执行卡2。

---

## 卡1【tier B → executor】跨桶头阻塞：新测试 2 条（红）+ 生产分桶 FIFO 实现（绿）

**改动文件**：`ctyun-stream-fix-proxy.test.py`（插入 2 个新测试）、`ctyun-stream-fix-proxy.py`（11 处 Edit）

### Step 1 — 写失败测试（仅测试文件，一次 Edit 插入两条）

插入位置：`test_tpm_waiters_identity_removal` 结尾（`self._tpm_cleanup(mod)`）之后、`def test_tpm_snapshot_shape_and_masking` 之前。

Edit old_string（文本锚点，实测唯一）：
```python
    def test_tpm_snapshot_shape_and_masking(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
```

Edit new_string：
```python
    def test_tpm_per_bucket_fifo_no_cross_bucket_block(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:cross"] = mod.TPM_LIMIT
        mod.TPM_QUEUE_TIMEOUT_S = 3.0
        try:
            mod.tpm_admit("key:A", mod.TPM_LIMIT, "m:cross")  # 桶A 占满预算
            mod.tpm_admit("key:B", mod.TPM_LIMIT, "m:cross")  # 桶B 占满预算
            results = {}
            ta = threading.Thread(target=lambda: results.setdefault(
                "A2", mod.tpm_admit("key:A", 10, "m:cross")))
            ta.start()
            time.sleep(0.3)  # 结构性断言兜底（下两行），非 sleep 猜时序
            self.assertEqual(sum(len(q) for q in mod.TPM_WAITERS.values()), 1,
                             "A2 must be enqueued (bucket A full)")
            tb = threading.Thread(target=lambda: results.setdefault(
                "B1", mod.tpm_admit("key:B", 10, "m:cross")))
            tb.start()
            time.sleep(0.3)
            self.assertEqual(sum(len(q) for q in mod.TPM_WAITERS.values()), 2,
                             "B1 must be enqueued (bucket B full)")
            mod.tpm_settle("key:B", mod.TPM_LIMIT, 0, "m:cross")  # 只释放 B
            tb.join(timeout=2)
            self.assertFalse(tb.is_alive(), "B1 must admit without waiting for A2")
            self.assertEqual(results["B1"][0], "ok")
            self.assertLess(results["B1"][1] / 1000.0, 1.5,
                            "B1 qwait must not be dragged by bucket A head "
                            "(pre-fix: ~2.7s = A2 full timeout)")
            mod.tpm_settle("key:A", mod.TPM_LIMIT, 0, "m:cross")  # 收尾释放 A
            ta.join(timeout=5)
            self.assertFalse(ta.is_alive())
            self.assertEqual(results["A2"][0], "ok")
        finally:
            mod.TPM_QUEUE_TIMEOUT_S = 120
            self._tpm_cleanup(mod)

    def test_tpm_queue_full_per_bucket(self) -> None:
        """队满按桶：QUEUE_MAX=2 时桶A 队满→同桶第 3 个立即 full 且计 rejected；
        桶B（已占满预算）首个排队请求不受桶A 队满影响——照常入队并在 settle 后准入。
        （修复前全局 cap：桶A 的 2 个假 waiter 占满全局队列，桶B 请求直接 "full"。）"""
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:pf"] = mod.TPM_LIMIT
        mod.TPM_QUEUE_MAX = 2
        mod.TPM_QUEUE_TIMEOUT_S = 5.0
        try:
            mod.tpm_admit("key:A", mod.TPM_LIMIT, "m:pf")  # 桶A 占满预算
            # 桶A 预插 2 个假 waiter（同 test_tpm_admit_queue_full 手法，QUEUE_MAX=2）
            with mod.TPM_LOCK:
                qA = mod.TPM_WAITERS.setdefault(("key:A", "m:pf"),
                                                collections.deque())
                qA.append(mod._TpmWaiter("key:A", 1, 0, "m:pf"))
                qA.append(mod._TpmWaiter("key:A", 1, 0, "m:pf"))
            # 同桶第 3 个：队满立即 full + rejected 计数
            status3, qwait3 = mod.tpm_admit("key:A", 1, "m:pf")
            self.assertEqual(status3, "full")
            self.assertEqual(qwait3, 0)
            self.assertEqual(mod._tpm_rejected.get(("key:A", "m:pf")), 1)
            # 桶B 占满预算；其首个排队请求必须入本桶队列，而非被桶A 队满连坐 429
            mod.tpm_admit("key:B", mod.TPM_LIMIT, "m:pf")
            results = {}
            tb = threading.Thread(target=lambda: results.setdefault(
                "B1", mod.tpm_admit("key:B", 10, "m:pf")))
            tb.start()
            time.sleep(0.3)  # 结构性断言兜底（下两行），非 sleep 猜时序
            self.assertTrue(tb.is_alive(), "B1 must queue, not be rejected full")
            self.assertEqual(len(mod.TPM_WAITERS.get(("key:B", "m:pf"), ())), 1,
                             "B1 must be enqueued in its own bucket")
            mod.tpm_settle("key:B", mod.TPM_LIMIT, 0, "m:pf")  # 全额退款释放 B
            tb.join(timeout=5)
            self.assertFalse(tb.is_alive())
            self.assertEqual(results["B1"][0], "ok")
        finally:
            mod.TPM_QUEUE_MAX = 20
            mod.TPM_QUEUE_TIMEOUT_S = 120
            self._tpm_cleanup(mod)

    def test_tpm_snapshot_shape_and_masking(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
```

### Step 2 — 实跑贴红

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py \
    ProxyDashboardUnitTest.test_tpm_per_bucket_fifo_no_cross_bucket_block \
    ProxyDashboardUnitTest.test_tpm_queue_full_per_bucket
```

预期红（修复前 main 现场，`TPM_WAITERS` 仍是全局 deque）：
- `test_tpm_per_bucket_fifo_no_cross_bucket_block` → **ERROR** `AttributeError: 'collections.deque' object has no attribute 'values'`
- `test_tpm_queue_full_per_bucket` → **ERROR** `AttributeError: 'collections.deque' object has no attribute 'setdefault'`

红语义说明（写入执行报告）：结构性 AttributeError 证明测试已绑定分桶结构（全局 deque 无按桶查询能力 = 缺口本身）；行为级差距由断言界承载——`qwait < 1.5s` 的界是按旧行为 `~2.7s`（A2 整段超时 3.0s − 0.3s 错峰）与修复后 `~0.3-0.6s` 两侧留余量标定的（spec Acceptance 同口径），修复后绿即证明跨桶阻塞消除。exit code 非 0，两条 ERROR 原样贴出。

### Step 3 — 生产实现（`ctyun-stream-fix-proxy.py`，11 处 Edit，全部文本锚点）

**E1 模块状态声明**（原 :477）：
```python
# old
TPM_WAITERS: collections.deque = collections.deque()  # FIFO 排队（of _TpmWaiter）
# new
TPM_WAITERS: dict = {}  # (key_id, model) -> collections.deque[_TpmWaiter]；每桶独立 FIFO，空 deque 即删
```

**E2 惰性驱逐扫描 O(n)→O(1)**（`_tpm_get_bucket` 内，原 :555-557）：
```python
# old
            idle = [bkey for bkey, b in TPM_BUCKETS.items()
                    if b.used <= 0
                    and not any((w.key_id, w.model) == bkey for w in TPM_WAITERS)]
# new
            idle = [bkey for bkey, b in TPM_BUCKETS.items()
                    if b.used <= 0
                    and bkey not in TPM_WAITERS]
```
语义不变依据：约束 1 的不变量（键存在 ⟺ 有排队）使 `bkey not in TPM_WAITERS` 等价于旧的"无该桶 waiter"扫描；仅驱逐窗口空且无排队的桶。

**E3 `_tpm_remove_waiter` 改按桶定位 + 空即删键**（原 :566-576 整函数替换，保持同一性摘除）：
```python
# old
def _tpm_remove_waiter(waiter) -> None:
    """把 waiter 从 TPM_WAITERS 中按**同一性**摘除（不在队列时为 no-op）。

    必须同一性而非值相等删除：_TpmWaiter 是 namedtuple，值全等的两个 waiter
    同队列时，deque.remove 会摘掉别人的那份（超时 waiter 误删仍在排队的请求）。
    仅可在持 TPM_LOCK 时调用。
    """
    for i, w in enumerate(TPM_WAITERS):
        if w is waiter:
            del TPM_WAITERS[i]
            return
# new
def _tpm_remove_waiter(waiter) -> None:
    """把 waiter 从其 (key_id, model) 桶队列按**同一性**摘除（不在队列时为 no-op）。

    必须同一性而非值相等删除：_TpmWaiter 是 namedtuple，值全等的两个 waiter
    同队列时，值匹配删除会摘掉别人的那份（超时 waiter 误删仍在排队的请求）。
    摘后桶队列空即删键（不变量：TPM_WAITERS 无空队列键）。
    仅可在持 TPM_LOCK 时调用。
    """
    q = TPM_WAITERS.get((waiter.key_id, waiter.model))
    if q is None:
        return
    for i, w in enumerate(q):
        if w is waiter:
            del q[i]
            break
    if not q:
        del TPM_WAITERS[(waiter.key_id, waiter.model)]
```

**E4 `tpm_admit` docstring 分桶表述**（两处，原 :591/:594）：
```python
# old
    - 否则入队 FIFO；仅队首 waiter 可被准入（严格 FIFO 防惊群）；
# new
    - 否则入本桶 FIFO；仅本桶队首 waiter 可被准入（按桶 FIFO 防惊群，桶间互不阻塞）；
# old
    - 队满（len(TPM_WAITERS) >= TPM_QUEUE_MAX）→ ("full", 0) 不入队。
# new
    - 队满（本桶排队数 >= TPM_QUEUE_MAX，按桶上限）→ ("full", 0) 不入队。
```

**E5 超大请求豁免**（原 :610-611）：
```python
# old
            if bucket.used <= 0 and not any(
                    (w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS):
# new
            if bucket.used <= 0 and not TPM_WAITERS.get((key_id, model)):
```

**E6 队满口径改按桶**（原 :622）：
```python
# old
        if len(TPM_WAITERS) >= TPM_QUEUE_MAX:
# new
        if len(TPM_WAITERS.get((key_id, model), ())) >= TPM_QUEUE_MAX:
```

**E7 入队走 setdefault**（原 :628）：
```python
# old
        TPM_WAITERS.append(waiter)
# new
        TPM_WAITERS.setdefault((key_id, model), collections.deque()).append(waiter)
```

**E8 准入闸门改本桶队首 + 准入后空即删键**（原 :637-646；spec 记 :637-643，实测 return 至 :646，以文本锚点为准）：
```python
# old
                head = TPM_WAITERS[0] if TPM_WAITERS else None
                if head is waiter:
                    now = time.time()
                    bucket = _tpm_get_bucket(key_id, model)
                    _tpm_prune(bucket, now)
                    if bucket.used + est <= budget:
                        TPM_WAITERS.popleft()
                        bucket.append((now, est))
                        bucket.used += est
                        return ("ok", (now - started) * 1000)
# new
                q = TPM_WAITERS.get((key_id, model))
                head = q[0] if q else None
                if head is waiter:
                    now = time.time()
                    bucket = _tpm_get_bucket(key_id, model)
                    _tpm_prune(bucket, now)
                    if bucket.used + est <= budget:
                        q.popleft()
                        if not q:
                            del TPM_WAITERS[(key_id, model)]
                        bucket.append((now, est))
                        bucket.used += est
                        return ("ok", (now - started) * 1000)
```
不改部分核对：`deadline = started + TPM_QUEUE_TIMEOUT_S`（:629）、超时计数（:634-635）、`TPM_LOCK.wait(timeout=min(1.0, remaining))`（:647）、`finally: _tpm_remove_waiter(waiter)`（:648-651）保持原样。

**E9 快照展示过滤**（`tpm_snapshot` 内，原 :704-708）：
```python
# old
            if bucket.used <= 0 and not any(
                    (w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS) \
                    and not _tpm_rejected.get((key_id, model)) \
                    and not _tpm_timeouts.get((key_id, model)):
# new
            if bucket.used <= 0 and not TPM_WAITERS.get((key_id, model)) \
                    and not _tpm_rejected.get((key_id, model)) \
                    and not _tpm_timeouts.get((key_id, model)):
```

**E10 快照 per-bucket queued**（原 :714-715）：
```python
# old
                "queued": sum(1 for w in TPM_WAITERS
                              if (w.key_id, w.model) == (key_id, model)),
# new
                "queued": len(TPM_WAITERS.get((key_id, model), ())),
```

**E11 快照 queue_total 全量和**（原 :727）：
```python
# old
            "queue_total": len(TPM_WAITERS),
# new
            "queue_total": sum(len(q) for q in TPM_WAITERS.values()),
```

不改部分核对：`tpm_settle`（:654-685）零改动（记账 + notify_all 原样）；`_tpm_rejected`/`_tpm_timeouts`（:478-479）零改动；`/api/tpm_stats` 端点与 dashboard JS 零改动。

### Step 4 — 实跑贴绿（范围限定，见 Global Constraint 9）

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py \
    ProxyDashboardUnitTest.test_tpm_per_bucket_fifo_no_cross_bucket_block \
    ProxyDashboardUnitTest.test_tpm_queue_full_per_bucket \
    ProxyDashboardUnitTest.test_tpm_constants_load \
    ProxyDashboardUnitTest.test_tpm_constants_env_seam \
    ProxyDashboardUnitTest.test_tpm_limit_for_two_level_fallback \
    ProxyDashboardUnitTest.test_tpm_key_id \
    ProxyDashboardUnitTest.test_tpm_prune \
    ProxyDashboardUnitTest.test_tpm_admit_non_enabled_model_bypasses \
    ProxyDashboardUnitTest.test_tpm_admit_enabled_model_goes_budget_path \
    ProxyDashboardUnitTest.test_tpm_admit_ok_immediate \
    ProxyDashboardUnitTest.test_tpm_admit_full_oversized \
    ProxyDashboardUnitTest.test_tpm_snapshot_keeps_rejected_after_window_roll \
    ProxyDashboardUnitTest.test_tpm_settle_corrects_usage \
    ProxyDashboardUnitTest.test_tpm_settle_prune_invariant_no_ghost_tokens \
    ProxyDashboardUnitTest.test_tpm_admit_queue_timeout \
    ProxyDashboardUnitTest.test_tpm_snapshot_shape_and_masking
```

预期：`Ran 16 tests ... OK`，exit code 0。（3 个待适配既有用例**不在本命令内**——它们此刻预期 ERROR，属卡2 的红。）

### 卡1 验收

- Step 2 红、Step 4 绿均实跑贴证（stdout + exit code 原样）。
- 生产 11 处 Edit 与上述 old/new 逐字一致；`git diff ctyun-stream-fix-proxy.py` 只含 TPM 区改动，无其他区域漂移。

---

## 卡2【tier B → executor】按桶队满独立性回归 + 3 个既有测试最小机械适配

**改动文件**：`ctyun-stream-fix-proxy.test.py`（3 处 Edit，语义与断言意图不改）

### Step 1 — 实跑贴红（卡1 实现后的既有用例破损现场）

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py \
    ProxyDashboardUnitTest.test_tpm_admit_queue_full \
    ProxyDashboardUnitTest.test_tpm_settle_notifies_waiters \
    ProxyDashboardUnitTest.test_tpm_waiters_identity_removal
```

预期红（3 个 ERROR，非 FAIL）：`test_tpm_admit_queue_full` 与 `test_tpm_waiters_identity_removal` 报 `AttributeError: 'collections.deque' object has no attribute 'setdefault'`（或 `append`）；`test_tpm_settle_notifies_waiters` 报 `AttributeError: 'collections.deque' object has no attribute 'values'`。exit code 非 0，原样贴出。

### Step 2 — 最小机械适配（3 处 Edit，全部文本锚点）

**T1 `test_tpm_admit_queue_full`**（原 :1215-1223；入队改 `setdefault` + `q.append`，仍在 `with mod.TPM_LOCK:` 内；len 断言改 `len(q)`）：
```python
# old
            # 两个 waiter 已在队（绕过 wait 循环用私有结构直接入队，验证队满拒绝分支）
            with mod.TPM_LOCK:
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
            status3, qwait3 = mod.tpm_admit("key:qfull", 1, "m:qfull")
            self.assertEqual(status3, "full")
            self.assertEqual(qwait3, 0)
            self.assertEqual(mod._tpm_rejected.get(("key:qfull", "m:qfull")), 1)
            self.assertEqual(len(mod.TPM_WAITERS), 2)  # 第三个未入队
# new
            # 两个 waiter 已在队（绕过 wait 循环用私有结构直接入队，验证队满拒绝分支）
            with mod.TPM_LOCK:
                q = mod.TPM_WAITERS.setdefault(("key:qfull", "m:qfull"),
                                               collections.deque())
                q.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
                q.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
            status3, qwait3 = mod.tpm_admit("key:qfull", 1, "m:qfull")
            self.assertEqual(status3, "full")
            self.assertEqual(qwait3, 0)
            self.assertEqual(mod._tpm_rejected.get(("key:qfull", "m:qfull")), 1)
            self.assertEqual(len(q), 2)  # 第三个未入队
```

**T2 `test_tpm_settle_notifies_waiters`**（原 :1278-1279；排队计数改全量口径，语义同为"恰 1 个 waiter 在队"）：
```python
# old
        time.sleep(0.2)  # 给 waiter 入队时间（确定性：settle 后立即 notify）
        self.assertEqual(len(mod.TPM_WAITERS), 1)
# new
        time.sleep(0.2)  # 给 waiter 入队时间（确定性：settle 后立即 notify）
        self.assertEqual(sum(len(q) for q in mod.TPM_WAITERS.values()), 1)
```

**T3 `test_tpm_waiters_identity_removal`**（原 :1317-1331；入队/成员扫描/len 断言改走本桶 `q`；注释与断言意图逐字保留）：
```python
# old
        mod.TPM_WAITERS.append(w1)
        mod.TPM_WAITERS.append(w2)
        # 值相等实现（deque.remove(waiter)）在摘 w2 时会误删队首 w1 —— 本用例必红。
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w2)
        self.assertEqual(len(mod.TPM_WAITERS), 1,
                         "identity removal must drop exactly one entry, got %d"
                         % len(mod.TPM_WAITERS))
        self.assertTrue(any(w is w1 for w in mod.TPM_WAITERS),
                        "head twin w1 must survive removal of w2")
        self.assertFalse(any(w is w2 for w in mod.TPM_WAITERS),
                         "removed waiter w2 must be gone from the queue")
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w1)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
        self._tpm_cleanup(mod)
# new
        q = mod.TPM_WAITERS.setdefault(("k", "m:ident"), collections.deque())
        q.append(w1)
        q.append(w2)
        # 值相等实现（deque.remove(waiter)）在摘 w2 时会误删队首 w1 —— 本用例必红。
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w2)
        self.assertEqual(len(q), 1,
                         "identity removal must drop exactly one entry, got %d"
                         % len(q))
        self.assertTrue(any(w is w1 for w in q),
                        "head twin w1 must survive removal of w2")
        self.assertFalse(any(w is w2 for w in q),
                         "removed waiter w2 must be gone from the queue")
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w1)
        self.assertEqual(len(q), 0)
        self._tpm_cleanup(mod)
```

### Step 3 — 实跑贴绿（TPM 针对性全集合：单测全部 TPM 用例 + 3 个集成类）

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py \
    ProxyDashboardUnitTest.test_tpm_constants_load \
    ProxyDashboardUnitTest.test_tpm_constants_env_seam \
    ProxyDashboardUnitTest.test_parse_tpm_limit_by_model_normal \
    ProxyDashboardUnitTest.test_parse_tpm_limit_by_model_malformed \
    ProxyDashboardUnitTest.test_parse_tpm_limit_by_model_empty \
    ProxyDashboardUnitTest.test_tpm_limit_for_two_level_fallback \
    ProxyDashboardUnitTest.test_tpm_key_id \
    ProxyDashboardUnitTest.test_tpm_prune \
    ProxyDashboardUnitTest.test_tpm_admit_non_enabled_model_bypasses \
    ProxyDashboardUnitTest.test_tpm_admit_enabled_model_goes_budget_path \
    ProxyDashboardUnitTest.test_tpm_admit_ok_immediate \
    ProxyDashboardUnitTest.test_tpm_admit_full_oversized \
    ProxyDashboardUnitTest.test_tpm_snapshot_keeps_rejected_after_window_roll \
    ProxyDashboardUnitTest.test_tpm_admit_queue_full \
    ProxyDashboardUnitTest.test_tpm_settle_corrects_usage \
    ProxyDashboardUnitTest.test_tpm_settle_prune_invariant_no_ghost_tokens \
    ProxyDashboardUnitTest.test_tpm_settle_notifies_waiters \
    ProxyDashboardUnitTest.test_tpm_admit_queue_timeout \
    ProxyDashboardUnitTest.test_tpm_waiters_identity_removal \
    ProxyDashboardUnitTest.test_tpm_per_bucket_fifo_no_cross_bucket_block \
    ProxyDashboardUnitTest.test_tpm_queue_full_per_bucket \
    ProxyDashboardUnitTest.test_tpm_snapshot_shape_and_masking \
    TpmAdmitHookTest.test_oversized_idle_release_then_busy_429 \
    TpmRateLimitTest \
    TpmPerModelTest
```

预期：`Ran 21 + 1 + 8 + N(TpmPerModelTest) tests ... OK`，exit code 0。覆盖核对：
- 单桶语义不变：`TpmRateLimitTest` 全类（含 `test_queue_exhaustion_and_window_roll` / `test_queue_timeout_returns_429` / `test_queue_full_immediate_429` / `test_tpm_stats_endpoint`）、`TpmAdmitHookTest`。
- 跨桶行为：两条新测试（卡1 已绿，此处回归复跑）。
- 按桶队满独立性：`test_tpm_queue_full_per_bucket` + `test_tpm_admit_queue_full`（适配后）+ `test_queue_full_immediate_429`（单桶口径不变证明）。
- 快照口径：`test_tpm_snapshot_shape_and_masking` + `test_tpm_stats_endpoint`。

### 卡2 验收

- Step 1 红、Step 3 绿均实跑贴证（stdout + exit code 原样）。
- 测试文件 `git diff ctyun-stream-fix-proxy.test.py` 中既有用例改动仅 T1-T3 三处，断言意图逐字未变（仅结构适配）；两条新测试零改动。

---

## Self-Review（5 项）

1. **spec coverage（9 条 Files 逐条对映）**：Files 1→E1；2→E2；3→E3；4→E4-E8（四处改动+docstring）；5→E9-E11；6→不改（`dict.clear()` 兼容，卡1/卡2 均未触碰 `_tpm_cleanup`）；7→T1-T3；8→卡1 Step 1 第一条（spec 代码块逐字采用）；9→卡1 Step 1 第二条（spec 给的是构造描述，PLAN 已写死完整代码并复核预修复红路径：全局 cap 使 B1 "full"）。Acceptance 各条均有对应验证命令（单桶不变=卡2 Step 3 集成类；快照键集=shape/stats endpoint 用例；TDD 红→绿=卡1 Step 2/4）。✅ 全覆盖。
2. **placeholder 扫描**：全部代码块可逐字落盘，无 "同上"/"略"/"待补"；卡内无指令式伪代码。grep 自查无 `...`、`TODO`、`<...>` 占位。✅ 0 命中。
3. **type consistency（mod.* 引用实测）**：`mod.TPM_MODEL_BUDGETS`(:1144 用例在用)、`mod.TPM_LIMIT`(:1155)、`mod.TPM_QUEUE_MAX`(:1212)、`mod.TPM_QUEUE_TIMEOUT_S`(:1270)、`mod.TPM_WAITERS`、`mod._TpmWaiter`(:1217)、`mod._tpm_rejected`(:1222)、`mod._tpm_cleanup`(:1074)、`mod.tpm_admit/tpm_settle` 均实测存在；测试文件 `collections`(:12)/`threading`(:29)/`time`(:30) import 实测在。生产侧新代码只用 `collections.deque/dict.get/setdefault/len/sum`，无新符号。✅ 一致。
4. **可落盘性（换视角：以 executor 心智逐卡走查）**：每处 Edit 的 old_string 均为现场实测原文（本 dispatch Read/grep 核过），含足够多行上下文保证唯一；新测试插入锚点 `def test_tpm_snapshot_shape_and_masking` 全文件唯一；验证命令用 unittest.main 位置参数选测试（文件以 `unittest.main(verbosity=2)` 收口 :8984，支持 `Class.method`/`Class` 多名），无需 import 模块（文件名含连字符无法 -m unittest 导入，故用脚本直跑 + 位置参数）；卡1 green 命令 16 测试全为纯单测（秒级），卡2 green 含 3 个集成类预计 ~2 分钟。无执行期决策点，无锚点缺失。✅ 可落盘。
5. **锚点实测（spot-check）**：生产 :477/:557/:566-576/:579/:610-611/:622/:628/:637-646/:647/:651/:685/:704-708/:714-715/:727 全部 Read 核对命中；测试 :1074-1079/:1208-1226/:1266-1286/:1305-1332/:1334/:6714/:6753/:6855/:6955 命中；dashboard :5273 命中。**发现 spec 偏差 3 处（均已在本 PLAN 用文本锚点吸收，不回 PLAN）**：
   - 闸门块 spec 记 `:637-643`，实测准入 `return` 在 `:646`（spec 替换块实际覆盖 :637-646）——E8 文本锚点已按现场写。
   - dashboard bucket 字段渲染 spec 记 `:5289-5295`，实测约 `:5290-5297`（±2 行；只读消费点、零改动，无卡面影响）。
   - spec Risks 称测试侧 `TPM_WAITERS` 直触点仅 3 适配点 + `_tpm_cleanup`：实测另有 5 处 `len(mod.TPM_WAITERS)` 只读断言（:1133/:1160/:1173/:1179/:1300）——全部为 `== 0` 断言，dict 化后空字典 `len()==0` 语义兼容、免改（:1279 的 `== 1` 已在 spec 第 7 条适配清单内）。spec 结论（免改）成立，无需加卡；此 5 处已纳入卡1/卡2 green 命令作回归。
   - 另记（非偏差、执行须知）：spec Acceptance 要求测试 8+9 都在生产改动**前**见红，故两条新测试的编写均在卡1 Step 1；卡2 承接按桶队满独立性回归验证（`test_tpm_queue_full_per_bucket` 复跑 + 既有队满用例）与 3 处适配——与 prompt 的卡2 字面分派略有重组，由 TDD 顺序约束决定。
