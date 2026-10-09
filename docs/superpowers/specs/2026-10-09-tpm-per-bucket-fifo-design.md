# E2 — TPM 按桶 FIFO 排队（finding C）设计

## Goal

消除 TPM 排队中单一全局队首对跨桶请求的阻塞：队首 (key_id, model) 桶持续超预算时，其他桶已排队请求不再被拖到 TPM_QUEUE_TIMEOUT_S（默认 120s）超时 429，本桶预算一释放即准入；单桶场景语义（同桶 FIFO、超时、429、notify_all 防惊群）完全不变。

## Files to Change

全部锚点为当前 main 实测（2026-10-09，E1 已合入 dd8ee63）。

**ctyun-stream-fix-proxy.py**

1. `:477 TPM_WAITERS` 模块状态：单条全局 deque → 分桶字典；不变量"无空队列键"（入队 setdefault、准入/摘除后空即删）：
```python
TPM_WAITERS: dict = {}  # (key_id, model) -> collections.deque[_TpmWaiter]；每桶独立 FIFO，空 deque 即删
```
2. `:557 _tpm_get_bucket()` 惰性驱逐扫描：`not any((w.key_id, w.model) == bkey for w in TPM_WAITERS)` → `bkey not in TPM_WAITERS`（O(n)→O(1)，语义不变：仅驱逐窗口空且无排队的桶）。
3. `:566 _tpm_remove_waiter(waiter) -> None` 改按桶定位，保持同一性摘除（防值相等误删回归）：
```python
def _tpm_remove_waiter(waiter) -> None:
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
4. `:579 tpm_admit(key_id, est, model)` 四处改动 + docstring（:583-597 全局 FIFO 表述改分桶 FIFO）：
   - `:610-611` 超大请求豁免：`not any((w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS)` → `not TPM_WAITERS.get((key_id, model))`；
   - `:622` 队满：`len(TPM_WAITERS) >= TPM_QUEUE_MAX` → `len(TPM_WAITERS.get((key_id, model), ())) >= TPM_QUEUE_MAX`（口径变按桶上限，见 Acceptance）；
   - `:628` 入队：`TPM_WAITERS.append(waiter)` → `TPM_WAITERS.setdefault((key_id, model), collections.deque()).append(waiter)`；
   - `:637-643` 闸门改为本桶队首判定：
```python
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
   `:634-636` 超时计数、`:647` 1s 粒度 wait、`:651` finally 摘除均不改（timeout/rejected 计数本就是按桶 dict :478/:479）。
5. `:688 tpm_snapshot()` 三处：
   - `:704-707` 展示过滤：`not any((w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS)` → `not TPM_WAITERS.get((key_id, model))`；
   - `:714-715` `"queued"` → `len(TPM_WAITERS.get((key_id, model), ()))`；
   - `:727` `"queue_total"` → `sum(len(q) for q in TPM_WAITERS.values())`。
   对外 JSON 键名与逐键含义均不变。

**ctyun-stream-fix-proxy.test.py**

6. `:1074 ProxyDashboardUnitTest._tpm_cleanup()`：`TPM_WAITERS.clear()` 对 dict 天然可用，不改。
7. 三个直触 TPM_WAITERS 私有结构的既有测试最小机械适配（语义与断言意图不改）：
   - `:1208 test_tpm_admit_queue_full`：`:1217-1218` 两次 append 改 `q = mod.TPM_WAITERS.setdefault(("key:qfull", "m:qfull"), collections.deque())` + 两次 `q.append(...)`（仍在 `:1216 with mod.TPM_LOCK:` 内）；`:1223` `len(mod.TPM_WAITERS)` → `len(q)`；
   - `:1266 test_tpm_settle_notifies_waiters`：`:1279` `len(mod.TPM_WAITERS)` → `sum(len(q) for q in mod.TPM_WAITERS.values())`；
   - `:1305 test_tpm_waiters_identity_removal`：`:1317-1322/:1325-1328` 入队/成员扫描改走 `q = mod.TPM_WAITERS.setdefault(("k", "m:ident"), collections.deque())`（`len(q)` 替代 `len(mod.TPM_WAITERS)`，`any(w is w1 for w in q)`）；`:1331` len 断言同理。
8. 新增 `ProxyDashboardUnitTest.test_tpm_per_bucket_fifo_no_cross_bucket_block`（插在 :1332 后、:1334 `test_tpm_snapshot_shape_and_masking` 前）：两桶确定性复现跨桶头阻塞。
```python
def test_tpm_per_bucket_fifo_no_cross_bucket_block(self):
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
```
9. 新增 `ProxyDashboardUnitTest.test_tpm_queue_full_per_bucket`：QUEUE_MAX=2，桶A 预插 2 个假 waiter（同既有 :1208 手法）→ 同桶第 3 个 `("full", 0)` 且 `_tpm_rejected[("key:A","m:pf")] == 1`；桶B（占满预算）首个请求入队（线程 0.3s 后仍存活 + 队长断言）而非 full，settle B 后 `("ok", ...)`——修复前必红（旧全局 cap 使 B 桶请求也 "full"）。

## Acceptance Criteria

- `python ctyun-stream-fix-proxy.test.py` 全绿；`TpmAdmitHookTest`(:6714)、`TpmRateLimitTest`(:6753)、`ProxyDashboardUnitTest`(:725) 其余 TPM 用例不改语义仍绿（仅第 7 条 3 个结构适配）。
- 新测试 8 在修复前 main 上必红（旧代码 B1 qwait ≈ A2 全量超时 ~2.7s > 1.5s 界），修复后绿；新测试 9 修复前必红（B 桶被全局队满 429），修复后绿。TDD：先写这两测试见红，再改生产代码。
- 队满口径变更（写清）：`TPM_QUEUE_MAX` 由全局排队上限变为按桶排队上限，env 键名 `CTYUN_TPM_QUEUE_MAX` 与默认值 20 不变；单桶场景逐字节同语义——既有 `test_queue_full_immediate_429`(:6855，同 key 3 并发 [200,200,429]) 不改仍绿即证。
- 快照口径不变：`queue_total` == 全量排队 waiter 数（= 各桶 queued 之和）；桶B 准入后其 queued 归 0。
- tpm_snapshot 对外 JSON 键集零变化：`config{limit,window_s,queue_max,model_budgets,enabled_models}`、`queue_total`、`buckets[].{key,model,used,remaining,queued,rejected,timeouts}`——dashboard `loadTpmObs` 消费点 :5273 queue_total / :5289-5295 bucket 字段不改；既有 `test_tpm_stats_endpoint`(:6955) 与 `test_tpm_snapshot_shape_and_masking`(:1334) 不改仍绿即证。
- 不加 env seam、不加依赖；锁序不变（全部新状态在 TPM_LOCK 内；TPM_LOCK→STATS_LOCK 链 :674-677 未动）。

## Risks

- 三个既有测试直触 TPM_WAITERS 私有结构，需第 7 条最小机械适配；不改语义、不重写断言意图。全仓测试 grep `TPM_WAITERS` 已核实直触点仅此 + `_tpm_cleanup`（dict.clear() 兼容，免改）。若有遗漏引用，TypeError/AttributeError 会在测试期显式暴露，不会静默漂移。
- 队满按桶后排队线程上限放大：多桶同时超预算时，排队线程数上限从 TPM_QUEUE_MAX(20) 变为 20 × 排队桶数（≤ TPM_KEY_CAP 64）。每排队请求占一线程；无 auth/未启用模型连接本就不受 TPM 约束。如需全局硬顶，另行加全局 sum guard（见 Exclusions，需回 PLAN）。
- 时序测试确定性：0.3s 等待后紧跟队列长度/存活结构性断言兜底（同 TpmRateLimitTest :6756-6759 风格），无裸 sleep 竞态；qwait 断言界 1.5s 距旧行为 2.7s 与修复后 ~1s 两侧均留余量。
- 与其他 episode 无冲突：E2 只动生产 :466-729 与测试 TPM 区；tpm_snapshot 只读 TPM_LOCK 状态，不碰 E1 的 stats_snapshot 直方图（:2619+）与 E4 的 SSE 热路径（:3117+）。

## Exclusions

- 不加全局队列总上限（sum guard）、不改 TPM_QUEUE_MAX 默认值与 env 键名。
- 不采纳备选方案 B（保留全局 deque + 准入时扫描"本桶最靠前 waiter"）：测试零改动，但队满仍是全局上限（跨桶队满饿死仍在）、每次唤醒 O(n) 扫描、结构语义更难推理——留档不实现。
- 不改 `_tpm_rejected`/`_tpm_timeouts` 计数键（已是按桶 dict :478/:479）、不改 settle 记账（负 delta 窗口欠账不变量）、不改超大请求空闲豁免语义、不改 `tpm_limit_for`/预算逻辑、不改 admin 端点与 dashboard JS。

## R31 Evidence

[R31-S1] 问题存在现场证据——全局单 deque 及其所有操作（修复前 main 实测 grep 回显）：

```
$ grep -n "TPM_WAITERS: collections.deque" ctyun-stream-fix-proxy.py
477:TPM_WAITERS: collections.deque = collections.deque()  # FIFO 排队（of _TpmWaiter）
$ grep -n "TPM_WAITERS.append(waiter)" ctyun-stream-fix-proxy.py
628:        TPM_WAITERS.append(waiter)
$ grep -n "TPM_LOCK.notify_all()" ctyun-stream-fix-proxy.py
685:        TPM_LOCK.notify_all()
```

```
$ grep -n "TPM_WAITERS\[0\]" ctyun-stream-fix-proxy.py
637:                head = TPM_WAITERS[0] if TPM_WAITERS else None
$ grep -n "if head is waiter" ctyun-stream-fix-proxy.py
638:                if head is waiter:
$ grep -n "deadline = started + TPM_QUEUE_TIMEOUT_S" ctyun-stream-fix-proxy.py
629:        deadline = started + TPM_QUEUE_TIMEOUT_S
```

[R31-S2] 根因：TPM_WAITERS 是单条全局 deque（ctyun-stream-fix-proxy.py:477），桶键只存在于 waiter 元组（:468 `_TpmWaiter` namedtuple）；准入闸门仅比对全局队首同一性（:637-638 `head = TPM_WAITERS[0]...; if head is waiter:`），队首桶持续超预算时后方其他 (key_id, model) 的 waiter 永轮不到 head，deadline = 入队时间 + TPM_QUEUE_TIMEOUT_S（:629，默认 120s 见 :110）到点 429；tpm_settle 末尾 notify_all（:685）唤醒后仍撞同一闸门（:637）。唯一按桶豁免在入队前（:610-611 空闲放行），排队中无绕过。