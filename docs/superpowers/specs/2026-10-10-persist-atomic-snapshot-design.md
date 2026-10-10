# F3 设计 — save_stats_counters 快照-写入乱序窗口（followups.md:6，E1 遗留）

- 日期：2026-10-10；基线：origin/main = ea3fbbc（389 测试全绿）；单生产文件 `ctyun-stream-fix-proxy.py`。
- 事实修正：set_* 家族实为 **4 个**（set_probe_enabled :2032 / set_upstream_base :2387 / set_capture_errors :2395 / set_tpm_model_budgets :2403），brief 中"三个"少计 set_probe_enabled；save_stats_counters 调用点共 **7 个**（上述 4 + flush_stats_if_dirty :2350 + 启动迁移 :5681 + _on_signal :5718），全部锁外直调。
- 本版覆盖同路径旧稿：旧稿缺方案对比、生产代码用 `...` 占位、R31 回显含 grep 不产出的伪造行，均不可用。

## Goal

消除 save_stats_counters"锁外取快照 → _PERSIST_LOCK 内写盘"的乱序窗口，杜绝线程 A 的旧快照覆盖线程 B 已落盘的新值（内存新、磁盘旧、重启回退），磁盘 schema 键集合与 flush/SIGTERM/admin POST 并发语义零变化。

## 根因（时序详见 R31-S2）

copy 两段（STATS_LOCK :1762 / _CFG_LOCK :1775）在 _PERSIST_LOCK（:1786）**之外**执行：A 取完旧快照、尚未进写段时，B 可完成完整 mutate+copy+write，随后 A 的写段用旧快照 `os.replace`（:1803）覆盖 B 的新文件。

## 方案对比

| 维度 | A：_PERSIST_LOCK 外扩包住 copy+write（推荐） | B：乐观 generation 计数 | C：专用持久化单写线程（enqueue+join） |
|---|---|---|---|
| 乱序消除 | 彻底（快照与写盘同临界区，所有持久化变更经同一锁串行） | 依赖"每个持久化 mutator 都 bump gen"的**完备枚举**，漏一个静默回归；STATS 计数高频变更需排除在 gen 外 → 又回到不完备 | 彻底（copy+write 单线程顺序执行） |
| 死锁面 | 新边 _PERSIST_LOCK→{STATS,_CFG}；7 调用点全锁外 → 无反向边，图无环（下节论证） | 锁序零变化 | 新线程生命周期；SIGTERM 需同步 join（超时语义难定） |
| 持锁时长 | 临界区增 copy（µs 级）+ 既有写盘（ms 级）；savers 低频（60s flush / 手动 POST / 一次性 SIGTERM）→ 无感知 | 最短 | 最短（调用方只入队） |
| 复杂度 | 最小（两段 `with` 内缩一层 + 注释重写） | 高（gen 读点、限次重试、回退分支） | 高（线程 + 退出时序 + 启动旁路） |
| 测试确定性 | GateLock seam（patch `_PERSIST_LOCK`）确定构造 red/green（见 Files 3） | 同 seam 但断言弱、重试分支多 | 需 mock 线程时序，最繁 |
| 可观测行为 | 零变化 | 零变化 | **有**（SIGTERM 等待时序、启动旁路）→ 违反硬约束 |

**推荐 A**。C 对低频场景过度工程且违反"无可观测行为变化"；B 的正确性靠枚举不变式，脆弱。

## 锁序与信号重入分析（结论）

1. 新边：`_PERSIST_LOCK → STATS_LOCK`、`_PERSIST_LOCK → _CFG_LOCK`（copy 段入内，两段短持不嵌套）。
2. 既有边：`TPM_LOCK → STATS_LOCK`（tpm_settle :705 持锁 → :720 取 STATS_LOCK）。TPM 路径**从不调用 save_stats_counters**，_PERSIST_LOCK 与 TPM_LOCK 无共享路径 → 两链不交叉、不成环。PROBE_LOCK 独立（probe 告警的 STATS_LOCK :2304 在 PROBE_LOCK 块 :2281-2302 释放**之后**、同层 :2303 处获取，无嵌套边）。
3. 反向边审计：7 个调用点全部锁外（:2030/:2385/:2394/:2402 四处"必须在锁外调用"死锁注释锚死；:2350 flush 在 STATS_LOCK :2346 释放后直调；:5681 单线程启动段，线程 :5735+ 才 spawn；:5718 main 线程 handler 无锁）→ **无 X→_PERSIST_LOCK 边，锁图无环，无死锁**。
4. 信号重入（E1 RLock 前因，:1482-1485）：CPython 信号 handler 在 main 线程执行；main 注册信号（:5725-5726）后只进 serve_forever（:5765；E1 注释中的 :5608 锚已过期，本次修正），main 线程不存在持 _PERSIST_LOCK 被信号打断重入的现实路径——RLock（:1488）纯防御。方案 A 保留 RLock：跨线程互斥不变；未来 main 线程新增 save 调用路径时同线程重入仍安全（信号在 STATS_LOCK/_CFG_LOCK 内打断的理论自死锁窗口与现结构完全相同，非本次引入、无现实路径，见 Exclusions）。
5. 新纪律：**禁止在持有 STATS/_CFG/TPM/PROBE 锁时调用 save_stats_counters**（内层为非重入 Lock，同线程自死锁或成环）——与既有 set_* 四处注释一致，零新增负担。

## Files to Change

1. `ctyun-stream-fix-proxy.py:1760` `save_stats_counters(path: str) -> None` — copy 两段并入 `_PERSIST_LOCK` 临界区（纯内缩 4 空格，行内容零变化），docstring 补 F3 一句。定稿全文：

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

（json.dump 字典与基线 :1793-1801 逐字相同——schema 键集合零变化。）

2. `ctyun-stream-fix-proxy.py:1482-1488` `_PERSIST_LOCK` 定义处纪律注释 — 整块替换（保留 RLock 防御论证，锚修正 :5608→:5765，纪律改为"最外层 + 内取 STATS→_CFG 两段短持"）：

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

3. `ctyun-stream-fix-proxy.test.py:6802`（`PersistSingleWriterTest` 结束与 `SnapshotV3Test` :6803 之间）— 新增 `class PersistSnapshotOrderTest(unittest.TestCase)`，**本卡唯一 red-before-green 用例**。测试基建：`load_proxy_module()`（:637，每测新模块实例）+ `tempfile.mkdtemp` + `mock`/`threading`/`json`（:32/:29/:19 均已 import），全局量 save/restore 仿 `PersistSingleWriterTest`（:6664-6686）。**确定性构造 seam = GateLock**：patch `mod._PERSIST_LOCK` 为包装器，首次 `__enter__` 发 copied 信号并阻塞等 release（未取真锁）。老代码下该点恰是"copy 完成、未进写段"的乱序窗口；新代码下在 copy 之前——同一测试 red-before/green-after：

```python
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
```

时序论证——老代码（red）：A 取完旧快照停在 gate（未持任何锁）→ B mutate+copy+write 落盘 new → 放行 A 写 old 覆盖 → 断言 FAIL（确定性）。新代码（green）：A 停在 copy 之前 → B 完整落盘 new → 放行 A，A 的 copy 取到 B 已 mutate 的 new → 写 new → 断言 PASS。

## Acceptance Criteria

- `PersistSnapshotOrderTest.test_stale_snapshot_cannot_overwrite_newer_writer`：修复前（当前 main）确定性 FAIL（`data["upstream_base"] == old_base != mod.UPSTREAM_BASE`）；方案 A 落地后 PASS——两线程交错快照-写入，终态磁盘 == 最后写入者内存值、json 可解析、errors 空（无 FileNotFoundError）、无 .tmp 残留。
- 全量 `python3 ctyun-stream-fix-proxy.test.py` 389+1 全绿；`PersistSingleWriterTest`（:6644）3 用例**原样**通过（不改其断言）——:6746-6756 键集断言即"磁盘 schema 键集合零变化"守卫。
- 结构守卫：`sed -n '/def save_stats_counters/,/^def /p' ctyun-stream-fix-proxy.py` 区间内 `with _PERSIST_LOCK:` 出现且先于 `with STATS_LOCK:`，函数内 `_PERSIST_LOCK` 仅一个 with 块；json.dump 字典与基线逐字一致（`git show ea3fbbc:ctyun-stream-fix-proxy.py` 对应段落 diff 零差异）。
- 生产 diff 仅两处：save_stats_counters 重排（:1760，行内容零变化纯内缩）+ :1482-1488 注释块；set_* 四函数 / flush_stats_if_dirty / _on_signal / 启动迁移调用点零改动。

## Risks

- **锁序**（完整结论见上节）：新边 _PERSIST_LOCK→{STATS,_CFG} 与 TPM_LOCK→STATS_LOCK（:705→:720）不交叉——TPM 代码无 save 调用路径；无任何 X→_PERSIST_LOCK 反向边（7 调用点全锁外）→ 无环无死锁。未来在持内层锁处新增 save 调用 = 同线程死锁，靠新 :1486 纪律 + set_* 四处既有注释双锚定。
- **信号重入**：handler 在 main 线程（:5716），main 注册后仅 serve_forever（:5765）——无现实重入路径；RLock 防御保留（跨线程语义不变）。
- **持锁时长**：临界区含 json.dump+os.replace（ms 级，量级不变）；savers 低频（flush 60s :5740、手动 POST、一次性 SIGTERM）→ 无感知；请求热路径仅短碰 STATS_LOCK（copy 段持有时长与现状相同），`_record_request` 不受影响。
- GateLock seam 只在单测内 patch/restore 模块属性，`load_proxy_module` 每测隔离（:637），无跨用例泄漏；join 超时 30s + is_alive 断言防挂死静默。

## Exclusions

- 不改磁盘 schema 键集/字段/JSON 结构（os.replace 原子写沿用，不加 fsync）。
- 不处理崩溃窗口内计数丢失（_stats_dirty 60s 脏刷既有语义；本 spec 只管乱序回退）。
- 不动 set_* 家族函数体、_on_signal/flush/启动迁移逻辑、`PersistSingleWriterTest` 既有断言。
- 不修"信号在 STATS_LOCK/_CFG_LOCK 内打断 main 线程"的理论自死锁窗口（现结构同样存在、无现实路径，见分析第 4 条）。
- followups.md 其余各行（flaky 测试 :2/:5、用例改名 :7、GET 门禁收敛 :8 等）不在本 episode。

## R31 Evidence

[R31-S1] copy/write 两段锁结构现状（main = ea3fbbc 实测回显；`…` 为同命令其余命中行省略）：

```
$ rg -n "save_stats_counters|_PERSIST_LOCK|_CFG_LOCK|STATS_LOCK|TPM_LOCK" ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:1488:_PERSIST_LOCK = threading.RLock()
ctyun-stream-fix-proxy.py:1762:    with STATS_LOCK:
ctyun-stream-fix-proxy.py:1775:    with _CFG_LOCK:
ctyun-stream-fix-proxy.py:1786:    with _PERSIST_LOCK:
…
$ rg -n "save_stats_counters\(PERSIST_PATH\)" ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:2032:    save_stats_counters(PERSIST_PATH)
ctyun-stream-fix-proxy.py:2387:    save_stats_counters(PERSIST_PATH)
ctyun-stream-fix-proxy.py:2395:    save_stats_counters(PERSIST_PATH)
ctyun-stream-fix-proxy.py:2403:    save_stats_counters(PERSIST_PATH)
ctyun-stream-fix-proxy.py:5681:            save_stats_counters(PERSIST_PATH)
ctyun-stream-fix-proxy.py:5718:            save_stats_counters(PERSIST_PATH)
```

（另 :2350 为 `save_stats_counters(path)` 形参调用——flush_stats_if_dirty，其 STATS_LOCK 于 :2346 释放后锁外直调。）

[R31-S2] 乱序时序根因（file:line 均为 main 现场实测）：

```
T1  线程A: STATS_LOCK copy 旧值（:1762-1774）→ _CFG_LOCK copy 旧值（:1775-1785）
T2  线程A: 尚未执行 `with _PERSIST_LOCK:`（:1786）——乱序窗口敞开
T3  线程B: set_upstream_base（:2380）在 _CFG_LOCK 内改 UPSTREAM_BASE=新值（:2383）
          → 锁外调 save_stats_counters（:2387）→ copy 新值 → 写段 os.replace（:1803）
T4  线程A: 进入 :1786 写段 → json.dump 旧快照（:1793-1802）→ os.replace（:1803）
          覆盖 B 的新文件
终态: 内存 UPSTREAM_BASE=新值，磁盘=旧值；进程退出后重启 _load_persist_file
（:1812）读回旧值 → 用户 admin POST 设置静默回退。
```

自检（Grep 同构）：`^## R31 Evidence` 1 命中、`^\[R31-S1\]` 1、`^\[R31-S2\]` 1、`^\$ ` 2（≥2）、围栏行 4（≥4，两对）；块内无行首 `## `。
