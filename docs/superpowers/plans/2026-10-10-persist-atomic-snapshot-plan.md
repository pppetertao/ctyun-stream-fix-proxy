# PLAN — F3 save_stats_counters 快照-写入乱序窗口消除（_PERSIST_LOCK 外扩）

- 日期：2026-10-10；spec：`docs/superpowers/specs/2026-10-10-persist-atomic-snapshot-design.md`（分支 commit b0bad42）
- worktree：`.worktrees/persist-atomic-snapshot`，分支 `fix/persist-atomic-snapshot`，基线 origin/main = ea3fbbc（389 测试全绿）
- Python 一律 `/usr/bin/python3`（本机 `python3` 不可用）
- 规模：小计划（2 卡），单 dispatch 串行执行；卡 1 red 证据 → 卡 2 实现转绿

## Global Constraints（全程有效）

1. **磁盘 schema 键集合零变化**：`json.dump({...})` 字典与 ea3fbbc 逐字一致（卡 2 验证含 diff 守卫）；不加键、不改结构、不加 fsync。
2. **生产 diff 只许两处**：① `save_stats_counters`（:1760，copy/write 两段纯内缩 4 空格 + docstring 替换，行内容零变化）；② `_PERSIST_LOCK` 注释块（:1482-1488）。set_* 四函数（set_probe_enabled :2032 / set_upstream_base :2387 / set_capture_errors :2395 / set_tpm_model_budgets :2403）、flush_stats_if_dirty（:2350）、_on_signal（:5718）、启动迁移（:5681）调用点零改动。
3. **389 既有测试全绿**；新增 1 用例后全量 390。
4. **锁内禁新增其它锁获取边**：`_PERSIST_LOCK` 临界区内按固定顺序 `STATS_LOCK → _CFG_LOCK` 两段短持、两段互不嵌套；禁止 `_PERSIST_LOCK → TPM_LOCK/PROBE_LOCK` 边（spec 锁序分析为边界，无反向边）。
5. TDD：卡 1 先落 `PersistSnapshotOrderTest` 并跑出**确定性 red**；卡 2 实现后转绿。卡 1 不单独 commit（避免分支历史出现红提交）；卡 2 收尾一次 commit（测试+实现同 commit，任意 commit 可绿）。

---

## Task 1 — 新增 PersistSnapshotOrderTest，确定性 red [tier A]

- 文件：`ctyun-stream-fix-proxy.test.py`（插入点 ：6802-6803 之间，`PersistSingleWriterTest` 结束后、`SnapshotV3Test` 之前）
- 操作：单次 Edit，多行文本锚（4 行锚，`class SnapshotV3Test` 全文件唯一）。

**Edit（old_string → new_string）**

old_string（现场 ：6800-6803 实测原文）：

```python
        self.assertEqual(mod.load_stats_counters(path), zeros)  # 损坏 JSON


class SnapshotV3Test(unittest.TestCase):
```

new_string（上 1 行 + 空两行 + 新测试类全文 + 空两行 + 原 class 行；测试类全文 = spec Files 3 逐字）：

```python
        self.assertEqual(mod.load_stats_counters(path), zeros)  # 损坏 JSON


class PersistSnapshotOrderTest(unittest.TestCase):
    """F3（followups:6）：快照-写入乱序。thread A 停在 copy 与写段之间（GateLock），
    thread B 完整 set_upstream_base（mutate+save），放行 A 后断言终态磁盘 == B 的
    新值 == 内存。修复前确定性 FAIL（旧快照覆盖新值），修复后 PASS。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_stale_snapshot_cannot_overwrite_newer_writer(self) -> None:
        mod = self.mod
        tmpdir = tempfile.mkdtemp(prefix="ctyun-f3-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "settings.json")
        orig_path, orig_lock = mod.PERSIST_PATH, mod._PERSIST_LOCK
        orig_base, orig_source = mod.UPSTREAM_BASE, mod._upstream_source
        old_base, new_base = "http://127.0.0.1:9/", "http://127.0.0.1:1/"

        class GateLock:
            def __init__(self, real):
                self._real, self._first = real, True
                self._guard = threading.Lock()
                self.copied = threading.Event()
                self.release = threading.Event()

            def __enter__(self):
                with self._guard:
                    first, self._first = self._first, False
                if first:  # 仅首个进入者停车：老代码=copy 后写段前；新代码=copy 前
                    self.copied.set()
                    assert self.release.wait(30), "gate never released"
                return self._real.__enter__()

            def __exit__(self, *exc):
                return self._real.__exit__(*exc)

        errors = []
        try:
            mod.PERSIST_PATH = path
            mod.set_upstream_base(old_base)   # gate 未装：旧值普通落盘
            gate = GateLock(orig_lock)
            mod._PERSIST_LOCK = gate

            def thread_a():  # flush/SIGTERM 替身：旧快照持有者
                try:
                    mod.save_stats_counters(path)
                except Exception as exc:  # FileNotFoundError 等在此暴露
                    errors.append(exc)

            def thread_b():  # admin POST 替身：新值写入者
                try:
                    mod.set_upstream_base(new_base)  # mutate + 完整 save
                except Exception as exc:
                    errors.append(exc)

            t_a = threading.Thread(target=thread_a, daemon=True)
            t_a.start()
            self.assertTrue(gate.copied.wait(30), "A 未到达 gate")
            t_b = threading.Thread(target=thread_b, daemon=True)
            t_b.start()
            t_b.join(30)
            self.assertFalse(t_b.is_alive(), "B 30s 未结束：疑似死锁")
            gate.release.set()
            t_a.join(30)
            self.assertFalse(t_a.is_alive(), "A 30s 未结束：疑似死锁")
            self.assertEqual(errors, [])                                  # 无 FileNotFoundError
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)                                      # 无损坏 JSON
            self.assertEqual(data["upstream_base"], new_base)             # 终态==最后写入者
            self.assertEqual(data["upstream_base"], mod.UPSTREAM_BASE)    # 盘==内存
            self.assertEqual([n for n in os.listdir(tmpdir) if ".tmp" in n], [])
        finally:
            mod._PERSIST_LOCK = orig_lock
            mod.PERSIST_PATH = orig_path
            mod.UPSTREAM_BASE = orig_base
            mod._upstream_source = orig_source


class SnapshotV3Test(unittest.TestCase):
```

- 依赖已就位（实测）：`json` :19 / `os` :20 / `shutil` :22 / `tempfile` :28 / `threading` :29 / `mock` :32；`load_proxy_module` :637 每测新模块实例。
- 不 commit（见 Global Constraints 5）。

**验证（red 证据）**

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py PersistSnapshotOrderTest -v
test_stale_snapshot_cannot_overwrite_newer_writer ... FAIL
AssertionError: 'http://127.0.0.1:9/' != 'http://127.0.0.1:1/'
Ran 1 test ... FAILED (failures=1)
Exit code: 1
```

确定性 red 机理（spec 时序论证）：A 取完旧快照停在 gate（未持任何锁）→ B mutate+copy+write 落盘 new → 放行 A，A 用**自己的旧局部快照** `os.replace` 覆盖 → `data["upstream_base"] == old_base != mod.UPSTREAM_BASE`。同址回归守卫：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py PersistSingleWriterTest
Ran 4 tests ... OK
Exit code: 0
```

- Gate：red 为 AssertionError（非 error）；PersistSingleWriterTest 4 用例（:6654/:6688/:6767/:6788，PLAN 实测；spec Acceptance 写"3 用例"系少计 :6767，以现场为准）原样通过。

---

## Task 2 — save_stats_counters 外扩实现 + 注释块重写，转绿 [tier A]

- 文件：`ctyun-stream-fix-proxy.py`，两处 Edit（顺序任意，均多行文本锚）。

**Edit A：`_PERSIST_LOCK` 注释块整块替换（现场 ：1482-1488 实测原文）**

old_string：

```python
# E1-A 持久化单写者锁。RLock 为防御选型：_on_signal 在 main 线程执行，而信号可在
# 任意字节码边界打断 main 线程；当前 main 线程注册信号后只进 serve_forever（:5608），
# 同线程重入暂无现实路径，但 main 线程一旦新增 save_stats_counters 调用路径，普通
# Lock 会自死锁挂死退出路径，RLock 则同线程重入安全（跨线程互斥语义不变）。
# 锁序纪律：_PERSIST_LOCK 只包写段；持锁块内禁取 STATS_LOCK/_CFG_LOCK/TPM_LOCK；
# copy 阶段两段锁不并入本锁。
_PERSIST_LOCK = threading.RLock()
```

new_string（spec Files 2 定稿全文；锚修正 :5608→:5765，纪律改为"最外层 + 内取 STATS→_CFG 两段短持"）：

```python
# E1-A 持久化单写者锁（F3 外扩为快照+写盘整体临界区）。RLock 为防御选型：
# _on_signal 在 main 线程执行，信号可在任意字节码边界打断 main 线程；当前 main
# 线程注册信号（:5725）后只进 serve_forever（:5765），同线程重入暂无现实路径，
# 但 main 线程一旦新增 save_stats_counters 调用路径，普通 Lock 会自死锁挂死退出
# 路径，RLock 同线程重入安全（跨线程互斥语义不变）。
# 锁序纪律（F3 起）：_PERSIST_LOCK 包住 copy+write 全程，内部按固定顺序 STATS_LOCK
# → _CFG_LOCK 两段短持（两段互不嵌套）。全库锁序：_PERSIST_LOCK → {STATS_LOCK,
# _CFG_LOCK}；TPM_LOCK → STATS_LOCK（tpm_settle，与 _PERSIST_LOCK 无共享路径，不成环）。
# 禁止在持有 STATS/_CFG/TPM/PROBE 锁时调用 save_stats_counters（同线程死锁或成环）。
_PERSIST_LOCK = threading.RLock()
```

**Edit B：`save_stats_counters` 全函数替换（现场 ：1760-1809 实测原文）**

old_string：

```python
def save_stats_counters(path: str) -> None:
    """把累计计数、按天分桶与当前上游端点全量写入持久化文件（SIGTERM / set_upstream_base 共用）。"""
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total", "eof_without_done_total",
                                          "finish_retries_total", "header_retries_total")}
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_key"])    # v3 T1：value-agnostic prune 复用
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
        daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                          for d, models in STATS["daily_by_model"].items()}
        daily_by_key = {d: {k: dict(v) for k, v in keys.items()}
                        for d, keys in STATS["daily_by_key"].items()}
        events = [dict(e) for e in EVENTS]  # 逐条浅拷贝：磁盘与内存一致（≤100 条）
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
        probe_enabled = PROBE_ENABLED
        budgets = dict(TPM_MODEL_BUDGETS)
        _prune_body_err_samples(PERSISTED_BODY_ERR_SAMPLES)   # 内存态原地 prune（R7，同 _prune_daily）
        _prune_probe_results(PROBE_RESULTS)
        body_err_samples = {m: [dict(s) for s in items]
                            for m, items in PERSISTED_BODY_ERR_SAMPLES.items()}
        probe_results = {m: [dict(r) for r in items]
                         for m, items in PROBE_RESULTS.items()}
    with _PERSIST_LOCK:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"upstream_base": base,
                           "capture_errors": capture_enabled,
                           "probe_enabled": probe_enabled,
                           "model_pricing": MODEL_PRICING,
                           "tpm_model_budgets": budgets,
                           "tpm_body_err_samples": body_err_samples,
                           "tpm_probe_results": probe_results,
                           "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                         daily_by_key=daily_by_key, events=events)},
                          fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # replace 已成功时 tmp 不存在（FileNotFoundError）；清理失败时保留原异常优先
            raise
```

new_string（spec Files 1 定稿全文 54 行；PLAN 作者已与现场做过机械比对：仅 docstring + 外扩 + copy 两段整行 +4 缩进，其余逐字节一致）：

```python
def save_stats_counters(path: str) -> None:
    """把累计计数、按天分桶与当前上游端点全量写入持久化文件（SIGTERM / set_* / flush 共用）。

    F3：copy 两段并入 _PERSIST_LOCK——快照与写盘同一临界区，旧快照无法覆盖
    新落盘值（乱序回退根因见 spec 2026-10-10-persist-atomic-snapshot）。
    """
    with _PERSIST_LOCK:
        with STATS_LOCK:
            counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                              "empty_retries_total", "eof_without_done_total",
                                              "finish_retries_total", "header_retries_total")}
            _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
            _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
            _prune_daily(STATS["daily_by_key"])    # v3 T1：value-agnostic prune 复用
            daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
            daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                              for d, models in STATS["daily_by_model"].items()}
            daily_by_key = {d: {k: dict(v) for k, v in keys.items()}
                            for d, keys in STATS["daily_by_key"].items()}
            events = [dict(e) for e in EVENTS]  # 逐条浅拷贝：磁盘与内存一致（≤100 条）
        with _CFG_LOCK:
            base = UPSTREAM_BASE
            capture_enabled = CAPTURE_ERRORS
            probe_enabled = PROBE_ENABLED
            budgets = dict(TPM_MODEL_BUDGETS)
            _prune_body_err_samples(PERSISTED_BODY_ERR_SAMPLES)   # 内存态原地 prune（R7，同 _prune_daily）
            _prune_probe_results(PROBE_RESULTS)
            body_err_samples = {m: [dict(s) for s in items]
                                for m, items in PERSISTED_BODY_ERR_SAMPLES.items()}
            probe_results = {m: [dict(r) for r in items]
                             for m, items in PROBE_RESULTS.items()}
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"upstream_base": base,
                           "capture_errors": capture_enabled,
                           "probe_enabled": probe_enabled,
                           "model_pricing": MODEL_PRICING,
                           "tpm_model_budgets": budgets,
                           "tpm_body_err_samples": body_err_samples,
                           "tpm_probe_results": probe_results,
                           "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                         daily_by_key=daily_by_key, events=events)},
                          fh, ensure_ascii=False)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass  # replace 已成功时 tmp 不存在（FileNotFoundError）；清理失败时保留原异常优先
            raise
```

- 函数签名 `save_stats_counters(path: str) -> None` 零变化；7 个调用点签名兼容，零改动。

**验证（绿 + 结构守卫 + schema 守卫）**

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py PersistSnapshotOrderTest PersistSingleWriterTest TokenPersistTest SettlePersistV3Test SnapshotV3Test
Ran 19 tests ... OK         # 1 新增 + 4 PersistSingleWriter + 1 Token + 5 Settle + 8 Snapshot
Exit code: 0
```

结构守卫（spec Acceptance 3）：

```
$ sed -n '/def save_stats_counters/,/^def /p' ctyun-stream-fix-proxy.py | grep -n "with _PERSIST_LOCK:\|with STATS_LOCK:\|with _CFG_LOCK:"
8:with _PERSIST_LOCK:
9:    with STATS_LOCK:
19:    with _CFG_LOCK:          # _PERSIST_LOCK 第一且唯一，且先于 copy 两段
$ sed -n '/def save_stats_counters/,/^def /p' ctyun-stream-fix-proxy.py | grep -c "with _PERSIST_LOCK:"
1
Exit code: 0
```

schema 守卫（json.dump 块与 ea3fbbc 逐字一致，diff 空 = exit 0）：

```
$ diff <(git show ea3fbbc:ctyun-stream-fix-proxy.py | sed -n '/json.dump({/,/ensure_ascii=False)/p') \
       <(sed -n '/json.dump({/,/ensure_ascii=False)/p' ctyun-stream-fix-proxy.py) && echo SCHEMA-UNCHANGED
SCHEMA-UNCHANGED
Exit code: 0
```

生产 diff 范围守卫（只许两处）：

```
$ git diff --stat -- ctyun-stream-fix-proxy.py
 ...py | 70 +++++-----   # 仅 save_stats_counters + 注释块两 hunk
$ git diff -- ctyun-stream-fix-proxy.py | grep "^@@"   # hunk 头仅落在 :1482 附近与 :1760 附近
```

**commit（卡 2 收尾，一次提交）**

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
$ git commit -m "fix(persist): save_stats_counters 快照与写盘并入 _PERSIST_LOCK 同一临界区，消除乱序回退窗口（F3）"
```

---

## Whole-branch 收尾节（最后一卡执行者同次 dispatch 完成或主代理单独跑）

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py
Ran 390 tests ... OK        # 389 既有 + 1 新增
Exit code: 0
```

- PersistSingleWriterTest 4 用例断言零改动（`git diff` 不含 :6644-6800 区间）。
- 交 whole-branch reviewer 审 `git diff ea3fbbc..HEAD`。

---

## Plan Self-Review（PLAN 作者 5 项自查）

1. **spec coverage**：spec Files 1→Task 2 Edit B；Files 2→Task 2 Edit A；Files 3→Task 1 测试全文。Acceptance 4 条分别落卡 1 red 验证、卡 2 绿+PersistSingleWriterTest 回归、结构守卫+schema diff、diff 范围守卫。Goal/锁序纪律/Global Constraints 一一对应。**全覆盖。**
2. **placeholder scan**：全文代码块均为定稿逐字文本（生产 54 行/注释块 9 行/测试 75 行），无 `...`/TODO/伪代码/指令替代代码。**0 命中。**
3. **type consistency**：`save_stats_counters(path: str) -> None` 签名不变；测试类名 `PersistSnapshotOrderTest`、方法名 `test_stale_snapshot_cannot_overwrite_newer_writer` 与 spec/验收一致；测试用全局量（PERSIST_PATH/_PERSIST_LOCK/UPSTREAM_BASE/_upstream_source）与 `PersistSingleWriterTest`（:6664-6686）既有 save/restore 口径一致。**一致。**
4. **可落盘性（换视角：executor 拿卡能否零决策执行）**：两卡均为单文件、单/双 Edit、多行文本锚；old_string 已按现场 repr dump 逐字核对缩进（copy 段体 8 空格、写段体 8 空格、注释块行首 `#`）；锚唯一性已核（`class SnapshotV3Test(unittest.TestCase):` 全文 1 次；两处生产 old_string 为完整函数/完整注释块，天然唯一）；验证命令均给出预期输出与 exit code。唯一自由度 = 卡 2 commit 时机（已写死：卡 2 收尾一次提交）。**可零决策执行。**
5. **锚点实测**：`:1760` def 行、`:1762/:1775/:1786` 三段 with、`:1482-1488` 注释块、`:6644/:6800-6803` 测试插入点、`set_upstream_base :2380-2387` mutate+锁外 save、测试 import（:19/:20/:22/:28/:29/:32）、`load_proxy_module :637` 全部 Read/repr 实测；spec 定稿代码与现场做过机械比对 **MATCH（54 行）**。spec R31 行号与现场零漂移；唯一发现 spec 瑕疵 = PersistSingleWriterTest 计为"3 用例"，现场实为 4（:6654/:6688/:6767/:6788），PLAN 已按实测写 4，不影响卡内容。**锚点准。**
