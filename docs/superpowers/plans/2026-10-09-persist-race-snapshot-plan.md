# PLAN: E1 持久化单写者 + 快照冻结（finding A + B）

## Header

| 项 | 值 |
|---|---|
| 日期 | 2026-10-09 |
| spec | `docs/superpowers/specs/2026-10-09-persist-race-snapshot-design.md`（分支 commit b77f4e6） |
| worktree / 分支 | `.worktrees/persist-race-snapshot` / `fix/persist-race-snapshot` |
| 执行者 | executor（4 卡全 tier A，逐卡串行） |
| Python | `/usr/bin/python3`（本机无 `python`，生产与测试统一用它） |
| Baseline | `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿 372 用例（157.6s），登记为回归基线 |
| 交付后全量回归 | 4 卡完成后由主代理 whole-branch 跑全量（预计 376 用例），不在卡内跑全量 |
| 提交 | 执行者不 commit，改动留工作树由主代理审查后统一提交 |

锚点口径：本计划行号为**当前 HEAD（b77f4e6）实测行号**；卡 1/卡 2 落地后生产与测试文件行号会漂移，**所有 Edit 一律用卡内给出的文本锚点（old_string 唯一匹配），不按行号定位**。

## Global Constraints

1. **TDD 顺序（强制）**：卡 1（测试，红）→ 卡 2（实现，绿）→ 卡 3（测试，红）→ 卡 4（实现，绿）。每卡先跑验证命令贴实际 stdout/exit code，通过才进下一卡。
2. **锁序纪律（A 卡核心约束）**：`_PERSIST_LOCK` 只包 `save_stats_counters` 的写段（`directory`/`makedirs`/`tmp`/`open`+`json.dump`/`os.replace`）；持锁块内**零**其他锁获取（禁 STATS_LOCK/_CFG_LOCK/TPM_LOCK）；copy 阶段两段锁（:1735-1747 STATS_LOCK、:1748-1758 _CFG_LOCK）保持原状、不并入持久化锁——违反则与 `tpm_settle` :674-684 的 TPM_LOCK→STATS_LOCK 链形成潜在死锁。**必须 RLock**：SIGTERM 处理器 `_on_signal` :5577 在 main 线程执行，若恰逢 main 线程正持锁写段（如启动迁移 :5542）时同线程重入 `save_stats_counters`，普通 Lock 自死锁挂死退出路径；RLock 同线程重入安全，跨线程互斥语义与 Lock 相同。
3. **磁盘 schema 逐字不动**：顶层 8 键 `{upstream_base, capture_errors, probe_enabled, model_pricing, tpm_model_budgets, tpm_body_err_samples, tpm_probe_results, stats}` 与 stats 子树 11 键（7 计数器 + daily/daily_by_model/daily_by_key/events）键集合与字面顺序均不变——load_* 白名单（:1792-1795 等）与升级历史依赖它。**口径注记**：spec 写"11 个顶层键"，实测 :1764-1773 顶层为 8 键，11 恰为 stats 子树键数；卡 1 测试对**两层键集合都断死**（严格强于 spec 断言，意图不变）。
4. **B 口径纠偏**：本次是**快照一致性**修复，不是崩溃修复（现状 json.dumps 无 indent 走 C encoder，不抛 RuntimeError 不 500）。验收只设"5 键冻结副本"断言，**禁止**写"响应不再 500"类断言。
5. **竞态测试构造纪律（2026-10-09 主代理决策转写，修订原"必过性"口径）**：`set_*` 三函数窄写后**紧接着** `save_stats_counters` 全量写，落盘终态从无窄 schema 中间态；卡1 的 red 由并发用例承担（确定性 FileNotFoundError——`persist_upstream` :1535 与 `save_stats_counters` :1762 共用 `path + ".tmp"`，并发下 `os.replace` 互摘 tmp 抛 FileNotFoundError，即 finding A 的确定性表现，非 flaky），另两用例为回归守卫（修复前后均绿）。
6. **不加依赖、不加 env seam**；不动 FLUSH_INTERVAL_S 与 flush 触发策略、不动 `_stats_dirty` 语义（`flush_stats_if_dirty` :2307-2320 原样）、不删 `sse_line_extract_usage`（test-only seam）。
7. **tmp 残留边界**：kill -9 于写段中会留 `.tmp.<pid>` 残留（不损坏主文件），可接受，不加启动清理逻辑。
8. 只改本计划列出的文件与锚点；禁止顺手改其它代码/注释/格式。

---

## 卡1【tier A】测试先行：PersistSingleWriterTest + 重写 :1633 用例 + 修 :744 fixture

- **文件**：`ctyun-stream-fix-proxy.test.py`
- **目标**（2026-10-09 主代理决策转写）：为卡 2 提供确定性 red（并发用例：共享 `.tmp` 被 `os.replace` 互摘 → 确定性 FileNotFoundError）+ 回归守卫网（崩溃窗口 + 计数不丢）。
- **预期 red 形态**：卡 2 未做时，**唯一 red** 是 `test_three_way_concurrent_writes_produce_valid_full_schema`——确定性 FAIL（`errors` 捕获 2 条 FileNotFoundError，finding A 的确定性表现，非 flaky）；`test_persist_upstream_carries_tpm_model_budgets` 与 `test_set_upstream_base_keeps_stats_counters` 为回归守卫，修复前后均绿（`set_*` 窄写后紧接着全量写，落盘终态从无窄 schema 中间态）。

### Edit 1a：重写 `ProxyDashboardUnitTest.test_persist_upstream_carries_tpm_model_budgets`（现 :1633）

old_string（逐字，实测锚点）：

```python
    def test_persist_upstream_carries_tpm_model_budgets(self) -> None:
        mod = self.mod
        path = os.path.join(tempfile.mkdtemp(prefix="ctyun-tpm-pu-"), "s.json")
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS["kimi-k3-oc"] = 1000
            mod.persist_upstream("https://example.com/v1", path, capture_errors=True,
                                 tpm_model_budgets=mod.TPM_MODEL_BUDGETS)
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved["tpm_model_budgets"], {"kimi-k3-oc": 1000})
            self.assertEqual(saved["upstream_base"], "https://example.com/v1")
        finally:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)
```

new_string（逐字替换）：

```python
    def test_persist_upstream_carries_tpm_model_budgets(self) -> None:
        # E1-A 回归守卫（修复前后均绿）：persist_upstream 已删，窄出口改走
        # set_tpm_model_budgets → save_stats_counters 全量 schema；预算读回相等 +
        # 文件含 stats 键，防止再引入窄 schema 中间态落盘（崩溃窗口回归）。
        mod = self.mod
        tmpdir = tempfile.mkdtemp(prefix="ctyun-tpm-pu-")
        path = os.path.join(tmpdir, "s.json")
        orig_path = mod.PERSIST_PATH
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.PERSIST_PATH = path
            mod.TPM_MODEL_BUDGETS.clear()
            mod.set_tpm_model_budgets({"kimi-k3-oc": 1000})
            self.assertEqual(mod.load_tpm_model_budgets(path),
                             {"kimi-k3-oc": 1000})
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertIn("stats", saved)  # 全量 schema 直写，无窄 schema 窗口
        finally:
            mod.PERSIST_PATH = orig_path
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(tmpdir, ignore_errors=True)
```

### Edit 1b：修 `test_resolve_upstream_base_precedence` 的 :744 fixture（spec 漏盘的额外引用，实测 `grep -n persist_upstream ctyun-stream-fix-proxy.test.py` 命中 ：744/:1633/:1640 三处）

old_string：

```python
        good = os.path.join(tmp, "good.json")
        mod.persist_upstream("http://127.0.0.1:9999", good)
```

new_string：

```python
        good = os.path.join(tmp, "good.json")
        with open(good, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://127.0.0.1:9999"}, fh)
```

（fixture 直写 JSON，与写者函数解耦；`resolve_upstream_base` 只经 `_load_persist_file` 读 `upstream_base` 键，行为不变。）

### Edit 1c：在 `class SnapshotV3Test(unittest.TestCase):` 前插入新测试类

old_string：

```python
class SnapshotV3Test(unittest.TestCase):
    """v3 stats_snapshot perf 新键 + daily_by_key 深拷 + percentile 推广（决策 6）。"""
```

new_string：

```python
class PersistSingleWriterTest(unittest.TestCase):
    """E1-finding A：save_stats_counters 单写者（_PERSIST_LOCK 串行 + tmp 带 pid
    + set_* 去窄写）。test_three_way_concurrent_writes_produce_valid_full_schema
    为本卡唯一 red-before-green 用例（修复前确定性 FAIL：共享 .tmp 被 os.replace
    互摘 → 2 条 FileNotFoundError，finding A 的确定性表现，非 flaky）；另两用例
    为回归守卫（修复前后均绿）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_set_upstream_base_keeps_stats_counters(self) -> None:
        # 回归守卫（修复前后均绿）：set_* 窄写后紧接着 save_stats_counters 全量写，
        # 落盘终态从无窄 schema 中间态——本用例守护"崩溃窗口 + 计数不丢"，
        # 而非确定性丢失。造数用 filtered_total：_record_request（:2363 起）只加
        # STATS["filtered_total"]（:2392），requests_total 仅 _proxy（:2759）自增，
        # 单测造不出 requests_total。
        mod = self.mod
        tmpdir = tempfile.mkdtemp(prefix="ctyun-psw-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "settings.json")
        orig_path = mod.PERSIST_PATH
        orig_base = mod.UPSTREAM_BASE
        orig_source = mod._upstream_source
        try:
            mod.PERSIST_PATH = path
            mod._record_request("POST", "/x", 200, 1.0, 0)
            self.assertGreaterEqual(mod.STATS["filtered_total"], 1)
            mod.save_stats_counters(path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertGreaterEqual(data["stats"]["filtered_total"], 1)
            filtered_before = data["stats"]["filtered_total"]
            mod.set_upstream_base("http://127.0.0.1:1/")
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("stats", data)
            self.assertEqual(data["stats"]["filtered_total"], filtered_before)
        finally:
            mod.PERSIST_PATH = orig_path
            mod.UPSTREAM_BASE = orig_base
            mod._upstream_source = orig_source

    def test_three_way_concurrent_writes_produce_valid_full_schema(self) -> None:
        """三路并发：SIGTERM :5577 替身 / admin POST :3486-3491 替身
        （set_capture_errors）/ flush :5602 替身；Barrier(3) 同步后各 50 轮。

        卡 1 唯一 red-before-green 用例：修复前确定性 FAIL——persist_upstream :1535
        与 save_stats_counters :1762 共用 path + ".tmp"，并发下 os.replace 互摘 tmp
        抛 FileNotFoundError（errors 捕获 2 条），即 finding A 的确定性表现，
        非 flaky；卡 2 后必须全过：文件恒为全量 schema、filtered_total 单调不减、
        无 tmp 残留。
        """
        mod = self.mod
        tmpdir = tempfile.mkdtemp(prefix="ctyun-psw-conc-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "settings.json")
        orig_path = mod.PERSIST_PATH
        orig_capture = mod.CAPTURE_ERRORS
        mod.PERSIST_PATH = path
        mod.save_stats_counters(path)  # 先落一次全量文件
        barrier = threading.Barrier(3)
        errors = []

        def sigterm_writer():  # SIGTERM 处理器替身：直调 save
            try:
                barrier.wait()
                for _ in range(50):
                    mod.save_stats_counters(path)
            except Exception as exc:
                errors.append(exc)

        def admin_toggle():  # admin POST set_capture_errors 替身
            try:
                barrier.wait()
                for i in range(50):
                    mod.set_capture_errors(bool(i % 2))
            except Exception as exc:
                errors.append(exc)

        def flush_writer():  # flush 定时器替身：直调 save
            try:
                barrier.wait()
                for _ in range(50):
                    mod.save_stats_counters(path)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=sigterm_writer),
                   threading.Thread(target=admin_toggle),
                   threading.Thread(target=flush_writer)]
        try:
            for t in threads:
                t.start()
            for t in threads:
                t.join(60)
            for t in threads:
                self.assertFalse(t.is_alive(), "线程 60s 未结束：疑似锁自死锁")
            self.assertEqual(errors, [])
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)  # 写坏 JSON 在此抛 ValueError
            self.assertEqual(
                set(data),
                {"upstream_base", "capture_errors", "probe_enabled", "model_pricing",
                 "tpm_model_budgets", "tpm_body_err_samples", "tpm_probe_results",
                 "stats"})
            self.assertEqual(
                set(data["stats"]),
                {"requests_total", "filtered_total", "errors_total",
                 "empty_retries_total", "eof_without_done_total",
                 "finish_retries_total", "header_retries_total",
                 "daily", "daily_by_model", "daily_by_key", "events"})
            before = mod.load_stats_counters(path)
            mod.save_stats_counters(path)
            after = mod.load_stats_counters(path)
            self.assertGreaterEqual(after["filtered_total"], before["filtered_total"])
            self.assertEqual(
                [n for n in os.listdir(tmpdir) if ".tmp" in n], [])  # 无 tmp 残留
        finally:
            mod.PERSIST_PATH = orig_path
            mod.CAPTURE_ERRORS = orig_capture

    def test_load_stats_counters_corrupt_or_missing_falls_back_to_zero(self) -> None:
        # 损坏/缺失回落全零（对齐既有 :1727-1732 口径）。
        mod = self.mod
        tmpdir = tempfile.mkdtemp(prefix="ctyun-psw-bad-")
        self.addCleanup(shutil.rmtree, tmpdir, ignore_errors=True)
        path = os.path.join(tmpdir, "settings.json")
        zeros = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                 "empty_retries_total": 0, "eof_without_done_total": 0,
                 "finish_retries_total": 0, "header_retries_total": 0}
        self.assertEqual(mod.load_stats_counters(path), zeros)  # 文件缺失
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertEqual(mod.load_stats_counters(path), zeros)  # 损坏 JSON


class SnapshotV3Test(unittest.TestCase):
    """v3 stats_snapshot perf 新键 + daily_by_key 深拷 + percentile 推广（决策 6）。"""
```

### 卡 1 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/persist-race-snapshot
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_persist_upstream_carries_tpm_model_budgets PersistSingleWriterTest
```

预期：`test_three_way_concurrent_writes_produce_valid_full_schema` FAIL（`errors` 非空、捕获 2 条 FileNotFoundError——确定性，非 flaky，本卡唯一 red）；`test_persist_upstream_carries_tpm_model_budgets`、`test_set_upstream_base_keeps_stats_counters`、`test_load_stats_counters_corrupt_or_missing_falls_back_to_zero` 均 PASS（回归守卫，修复前后均绿）。整体 exit code 1。
Baseline 后测试总数按 372 + 4 计（1 重写 + 3 新增）。

---

## 卡2【tier A】实现：_PERSIST_LOCK + tmp 唯一化 + 删 persist_upstream + set_* 去窄写

- **文件**：`ctyun-stream-fix-proxy.py`
- **前置**：卡 1 已落地。**卡内 Edit 顺序强制：2a → 2b → 2c → 2d/2e/2f**（2c 的 old_string 在 persist_upstream 存活时不唯一，必须先删 2b）。

### Edit 2a：STATS_LOCK :1442 旁新增模块级锁

old_string：

```python
STATS_LOCK = threading.Lock()
```

new_string：

```python
STATS_LOCK = threading.Lock()
# E1-A 持久化单写者锁：必须 RLock——SIGTERM 处理器 _on_signal 在 main 线程执行，
# 若恰逢 main 线程正持锁跑 save_stats_counters 写段（如启动迁移），同线程重入
# 需放行，普通 Lock 自死锁挂死退出路径；RLock 同线程重入安全，跨线程互斥不变。
# 锁序纪律：_PERSIST_LOCK 只包写段；持锁块内禁取 STATS_LOCK/_CFG_LOCK/TPM_LOCK；
# copy 阶段两段锁不并入本锁。
_PERSIST_LOCK = threading.RLock()
```

### Edit 2b：整函数删除 `persist_upstream`（:1524-1540）

old_string（含下随两个空行与 `def extract_model` 首行，保证删后空行规整）：

```python
def persist_upstream(base: str, path: str, capture_errors: bool = False,
                     tpm_model_budgets: dict = None) -> None:
    """持久化上游端点 + capture_errors + tpm_model_budgets（窄出口原子替换）。

    注意：本函数不持 _CFG_LOCK——调用方（set_upstream_base / set_capture_errors /
    set_tpm_model_budgets）在各自的 _CFG_LOCK 区内调用，由调用方保证 TPM_MODEL_BUDGETS
    读一致性。避免嵌套获取非重入锁的死锁（决策：_CFG_LOCK 是 threading.Lock）。
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base, "capture_errors": capture_errors,
                   "tpm_model_budgets": tpm_model_budgets or {}},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)  # 同目录原子替换，读侧不会见到半截文件


def extract_model(body):
```

new_string：

```python
def extract_model(body):
```

### Edit 2c：`save_stats_counters` 写段整体包 `_PERSIST_LOCK` + tmp 带 pid（:1759-1774）

old_string（2b 完成后唯一）：

```python
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
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
```

new_string（json.dump 11 键字面量逐字保留，仅整体缩进 +4；`"%s.tmp.%d"` 的 pid 后缀防多进程重启交叠撞名，进程内并发由 _PERSIST_LOCK 串行）：

```python
    with _PERSIST_LOCK:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
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
```

### Edit 2d：`set_upstream_base` 删窄写（:2336-2337）

old_string：

```python
        _upstream_source = "api"
        persist_upstream(base, PERSIST_PATH, capture_errors=CAPTURE_ERRORS,
                         tpm_model_budgets=TPM_MODEL_BUDGETS)
```

new_string：

```python
        _upstream_source = "api"
```

（:2338-2339 锁外调用注释保留原状——实测该注释无"persist"表述可删。）

### Edit 2e：`set_capture_errors` 删窄写（:2347-2348）

old_string：

```python
        CAPTURE_ERRORS = enabled
        persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=enabled,
                         tpm_model_budgets=TPM_MODEL_BUDGETS)
```

new_string：

```python
        CAPTURE_ERRORS = enabled
```

### Edit 2f：`set_tpm_model_budgets` 删窄写（:2357-2358）

old_string：

```python
        TPM_MODEL_BUDGETS = dict(budgets)
        persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=CAPTURE_ERRORS,
                         tpm_model_budgets=TPM_MODEL_BUDGETS)
```

new_string：

```python
        TPM_MODEL_BUDGETS = dict(budgets)
```

### 卡 2 验证（针对性测试 + spec AC 的 grep 断言）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/persist-race-snapshot
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_persist_upstream_carries_tpm_model_budgets ProxyDashboardUnitTest.test_stats_persist_roundtrip_and_defaults ProxyDashboardUnitTest.test_resolve_upstream_base_precedence ProxyDashboardUnitTest.test_set_tpm_model_budgets_atomic_replace PersistSingleWriterTest
grep -n 'persist_upstream' ctyun-stream-fix-proxy.py; echo "grep1 rc=$? (期望 1，无输出)"
grep -n '\.tmp' ctyun-stream-fix-proxy.py
```

预期：测试全绿 exit 0（卡 1 的 red 转绿 + 3 个既有持久化用例无回归）；grep1 无输出 exit 1（定义+调用 = 0）；grep2 仅 1 行命中，形如 `tmp = "%s.tmp.%d" % (path, os.getpid())`。`open(`/`os.replace` 位于 `with _PERSIST_LOCK:` 块内由主代理/reviewer 人工 review 确认（spec AC 原文）。

---

## 卡3【tier A】测试先行：SnapshotV3Test 5 键冻结断言

- **文件**：`ctyun-stream-fix-proxy.test.py`
- **预期 red 形态**：卡 4 未做时 `assertIsNot` **必 FAIL**（`snap = dict(STATS)` 浅拷贝，5 键均为活引用），exit 1。

### Edit 3：在 `test_ttfb_hist_by_model_in_perf` 之后插入新用例

old_string（实测唯一锚点，位于 :6358-6363）：

```python
        finally:
            mod.STATS["ttfb_hist"] = orig

    def test_daily_by_key_snapshot_is_deep_copy(self) -> None:
```

new_string：

```python
        finally:
            mod.STATS["ttfb_hist"] = orig

    def test_snapshot_v3_hist_keys_are_frozen(self) -> None:
        # E1-B：5 个直方图键写回冻结副本，响应内不再混合快照+活读。
        # 造数：_record_request(model="m", ttfb_ms=120.0) → STATS["ttfb_hist"]["m"]
        # 第 2 桶（bisect_right(HIST_BUCKETS_MS, 120.0)=1）+1。
        mod = self.mod
        orig_hist = mod.STATS["ttfb_hist"]
        orig_rates = mod.STATS["rates"]
        mod.STATS["ttfb_hist"] = {}
        try:
            mod._record_request("POST", "/t", 200, 1.0, 0, model="m", ttfb_ms=120.0)
            snap = mod.stats_snapshot()
            for key in ("ttfb_hist", "phase_ms", "phase_ms_by_model",
                        "stalls_by_model", "rates"):
                self.assertIsNot(snap[key], mod.STATS[key],
                                 "%s 仍是活引用，未写回冻结副本" % key)
            self.assertEqual(snap["ttfb_hist"]["m"],
                             list(mod.STATS["ttfb_hist"]["m"]))
            baseline_bytes = mod.STATS["rates"]["bytes_out_total"]
            baseline_bucket0 = mod.STATS["ttfb_hist"]["m"][0]
            snap["rates"]["bytes_out_total"] += 999
            snap["ttfb_hist"]["m"][0] += 1
            self.assertEqual(mod.STATS["rates"]["bytes_out_total"], baseline_bytes)
            self.assertEqual(mod.STATS["ttfb_hist"]["m"][0], baseline_bucket0)
        finally:
            mod.STATS["ttfb_hist"] = orig_hist
            mod.STATS["rates"] = orig_rates

    def test_daily_by_key_snapshot_is_deep_copy(self) -> None:
```

### 卡 3 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/persist-race-snapshot
/usr/bin/python3 ctyun-stream-fix-proxy.test.py SnapshotV3Test
```

预期：`test_snapshot_v3_hist_keys_are_frozen` FAIL（`AssertionError: 仍是活引用...`，首个 assertIsNot 即 `ttfb_hist`）；SnapshotV3Test 其余用例 PASS。整体 exit code 1。

---

## 卡4【tier A】实现：stats_snapshot 锁内写回 5 键冻结副本

- **文件**：`ctyun-stream-fix-proxy.py`
- **前置**：卡 3 已落地（red 在案）。

### Edit 4：`stats_snapshot` 在 `rates = dict(STATS["rates"])`（:2658）之后、STATS_LOCK 块内追加 5 行写回

old_string（实测唯一）：

```python
        stalls_by_model = dict(STATS["stalls_by_model"])
        rates = dict(STATS["rates"])
```

new_string（5 行写回与既有本地副本同名，8 空格缩进 = 仍在 `with STATS_LOCK:` 块内；后续 `stalls_total = STATS["stalls_total"]` 等行不动；:2653-2656 的 `qwait_by_model`/`settle_by_model` 本地副本仍被 perf 分位 :2697-2703 消费，勿删；qwait/settle 两键既有写回 :2645-2648 不动）：

```python
        stalls_by_model = dict(STATS["stalls_by_model"])
        rates = dict(STATS["rates"])
        snap["ttfb_hist"] = ttfb_hist
        snap["phase_ms"] = phase_hist
        snap["phase_ms_by_model"] = phase_by_model
        snap["stalls_by_model"] = stalls_by_model
        snap["rates"] = rates
```

### 卡 4 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/persist-race-snapshot
/usr/bin/python3 ctyun-stream-fix-proxy.test.py SnapshotV3Test
```

预期：SnapshotV3Test 全绿（含卡 3 red 转绿、既有 `json.dumps(snap)` 序列化冒烟用例）exit 0。

---

## Self-Review（PLAN-B 完成自查 5 项）

1. **spec coverage**：spec Files to Change 逐条对映——模块锁（spec §1.1）→ 卡 2a；save 写段重构+tmp 唯一化（§1.2）→ 卡 2c；删 persist_upstream（§1.3）→ 卡 2b + 卡 1a/1b（spec 只列了 :1633，实测补 :744 fixture，见第 5 项）；set_* 三函数（§1.4）→ 卡 2d/2e/2f；stats_snapshot 写回（§1.5）→ 卡 4；测试侧三件套（§2）→ 卡 1a/1c + 卡 3。AC 五条均有对应卡内验证命令。**全覆盖。**
2. **placeholder scan**：全文无"参考 spec"/"同上"/省略号替代代码；生产与测试代码块均逐字可落盘（Edit old/new 成对给出）。**0 命中。**
3. **type consistency**：`_PERSIST_LOCK = threading.RLock()` 模块级；`save_stats_counters(path: str) -> None`、`stats_snapshot() -> dict` 签名不变；删 `persist_upstream` 后其 4 处引用（生产 3 + 测试 2）全部随卡 1/卡 2 处理；测试用到的 `mod.*` 名（load_stats_counters :1788 / load_tpm_model_budgets :1806 / set_* :2331-2360 / _record_request :2363 / PERSIST_PATH / UPSTREAM_BASE / _upstream_source / CAPTURE_ERRORS / STATS）逐一实测存在。**一致。**
4. **可落盘性 + 锚点实测**（换视角：以 executor 抄写视角逐卡核对 old_string 唯一性）——实测 ≥3 锚点全命中：`STATS_LOCK = threading.Lock()` :1442 唯一；`persist_upstream` def :1524 + 调用 :2336/:2347/:2357 与 spec 一致；`tmp = path + ".tmp"` 恰 :1535/:1762 两处（删 2b 后 2c old_string 唯一，卡内已强制 Edit 顺序）；`stalls_by_model = dict(STATS["stalls_by_model"])` :2657 唯一；测试侧 `mod.STATS["ttfb_hist"] = orig` 仅 :6360（卡 3 锚点唯一）；`class SnapshotV3Test` :6277 唯一。生产 json.dump 8 键字面量与 :1764-1773 逐字一致。**可落盘。**
5. **锚点偏差与 spec 修正记录**（均已回写进卡内，不阻塞）：① spec"11 个顶层键"实测为顶层 8 键 + stats 子树 11 键，卡 1 并发用例两层键集合都断死；② spec":2338-2339 注释删 persist 表述"实测该注释无 persist 字样，维持原状；③ spec"persist_upstream 全仓调用点仅 :2336/:2347/:2357"实测测试文件另有 :744 fixture 引用，卡 1b 以直写 JSON 修复（行为不变）；④ 卡 1b/1c/3 的插入锚点采用 4 行上下文文本而非行号， immune to 行号漂移。

**卡数/tier 汇总**：4 卡全 tier A（卡 1/卡 3 测试转写卡，卡 2/卡 4 生产转写卡），executor 串行执行，无 C 档。
