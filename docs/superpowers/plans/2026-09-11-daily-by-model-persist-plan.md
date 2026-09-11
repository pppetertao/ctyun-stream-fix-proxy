# daily_by_model 持久化执行计划（2026-09-11）

## Header

- **spec**: `docs/superpowers/specs/2026-09-11-daily-by-model-persist-design.md`（本计划唯一事实源，锚点已全部实测验证）
- **worktree/分支**: `.worktrees/daily-by-model-persist` → `fix/daily-by-model-persist`
- **baseline（2026-09-11 实测）**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 49 tests in 19.555s / OK / exit 0`。README:69 现值 `45 个用例` 为陈旧值（实际 49）；本计划新增 1 个测试，完成后总数 **50**，README:69 同步为 `50 个用例`。
- **运行时**: `/usr/bin/python3` = Python 3.9.6。`datetime.date.fromisoformat` 在 3.9 为严格解析（3.11+ 放宽），loader 的 round-trip 校验（`str(d) != key` → 跳过）与版本无关，**不得省略**。
- **卡数 / tier 分布**: 3 卡（卡 1 tier A、卡 2 tier A、卡 3 tier B），全部派 **executor**；无 C 档卡（save/load/恢复/文案/单测均可在计划阶段写死，不命中真机/运行时数据/DI 实测任一留白）。卡数 ≤4 → 小计划，单次 dispatch，不分段。
- **执行顺序**: 卡 1 → 卡 2 → 卡 3 串行（卡 2 的 roundtrip 测试依赖卡 1 的 save 落盘行为）。

## Global Constraints

1. **测试命令一律 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`**（README:69：Homebrew Python 3.14 的 `http.server.HTTPServer` 构造挂死，禁用其他解释器）。跑单个测试可追加测试名：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_xxx`（unittest.main CLI 选择，已实测可用）。
2. **测试污染防护**：所有改写 `mod.STATS["daily_by_model"]` 的测试必须 `orig_dbm = mod.STATS["daily_by_model"]` 备份 + `finally` 恢复（既有 :762-784 模式），否则污染 cap/matrix 等共享用例。
3. **loader 必须含 ISO round-trip 校验**（`str(d) != key` → 跳过该日期桶）——`_prune_daily` 依赖 ISO 字典序排序，坏 key 必须挡在内存外；`fromisoformat` 宽松行为随 Python 版本不同，round-trip 与版本无关。
4. **不动清单（spec Exclusions，越界=打回）**：`_prune_daily`、`stats_snapshot`、bump 路径（`_record_request` / `_record_empty_retry`）、`load_daily_buckets`（不加 ISO 校验）、`load_stats_counters`、前端 JS（renderDailyByModel 等）、`DAILY_RETENTION_DAYS=90`、`BY_MODEL_CAP=32`、存储形状（日期→模型→5字段，不转置）。
5. **编辑一律内容精确匹配**（old_string 唯一命中），本文行号均为落笔前实测参考——前卡编辑会使后卡行号漂移，禁止按行号盲改。
6. **随卡验证**：每卡 Step 3 跑全量测试并贴实际 stdout/exit code；不跨卡攒验证。
7. **无新依赖、不新建文件**（仅改 `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`、`README.md` 三个既有文件）。
8. 每卡验证全绿后单独 commit（Conventional Commits，信息见各卡）。

---

## 卡 1（tier A）: save_stats_counters 落盘 daily_by_model + 三个 prune 测试补文件断言

**目标**：save 时把 `STATS["daily_by_model"]` 双层深拷贝（prune 后）写入 `stats.daily_by_model`；三个既有 by_model prune 测试（on_save / exact_boundary / empty）删"不落盘"限定、补文件断言——spec acceptance"3 个 prune 测试的文件断言全过"即指 by_model prune 家族这三处。

**锚点（实测）**：`ctyun-stream-fix-proxy.py:241-258`（`save_stats_counters`，STATS_LOCK 块 :243-248，json.dump :256-257）；`ctyun-stream-fix-proxy.test.py:757-784`（`test_daily_by_model_prune_on_save`）、`:786-811`（`test_daily_by_model_prune_exact_boundary`）、`:813-825`（`test_daily_by_model_prune_empty`）。

### Step 1 — 先写失败测试（改三个既有测试，整方法替换）

**Edit 1a** — `ctyun-stream-fix-proxy.test.py` 中 `test_daily_by_model_prune_on_save` 整方法替换。

old_string（现行全文，唯一）：

```python
    def test_daily_by_model_prune_on_save(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            for i in range(95):
                mod.STATS["daily_by_model"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "m1": {"requests": i, "filtered": 0,
                           "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            today = mod.today_key()
            mod.STATS["daily_by_model"][today] = {
                "m1": {"requests": 95, "filtered": 0,
                       "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            # 断言内存态：daily_by_model 不落盘，读回文件验证不到 prune
            dbm = mod.STATS["daily_by_model"]
            self.assertLessEqual(len(dbm), 90,
                                 "in-memory daily_by_model must be pruned on save")
            self.assertNotIn("2026-01-01", dbm,
                             "oldest buckets must be pruned from memory")
            self.assertIn(today, dbm, "today bucket must survive prune")
            self.assertEqual(dbm["2026-04-11"]["m1"]["requests"], 94,
                             "surviving inner entries must be untouched")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
```

new_string（删 :774"不落盘"注释、改内存态注释、finally 后新增 4 条文件断言，对齐 `test_daily_prune_on_save`:719-722 模式）：

```python
    def test_daily_by_model_prune_on_save(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            for i in range(95):
                mod.STATS["daily_by_model"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "m1": {"requests": i, "filtered": 0,
                           "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            today = mod.today_key()
            mod.STATS["daily_by_model"][today] = {
                "m1": {"requests": 95, "filtered": 0,
                       "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            # 内存态断言：prune 必须落到 STATS["daily_by_model"] 原对象
            dbm = mod.STATS["daily_by_model"]
            self.assertLessEqual(len(dbm), 90,
                                 "in-memory daily_by_model must be pruned on save")
            self.assertNotIn("2026-01-01", dbm,
                             "oldest buckets must be pruned from memory")
            self.assertIn(today, dbm, "today bucket must survive prune")
            self.assertEqual(dbm["2026-04-11"]["m1"]["requests"], 94,
                             "surviving inner entries must be untouched")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily_by_model"]
        self.assertLessEqual(len(saved), 90,
                             "file daily_by_model must be pruned to <=90 buckets")
        self.assertNotIn("2026-01-01", saved,
                         "oldest buckets must be pruned from the file")
        self.assertIn(today, saved, "today bucket must survive prune in the file")
        self.assertEqual(saved["2026-04-11"]["m1"]["requests"], 94,
                         "surviving file entries must be untouched")
```

**Edit 1b** — `test_daily_by_model_prune_exact_boundary` 整方法替换。

old_string（现行全文，唯一）：

```python
    def test_daily_by_model_prune_exact_boundary(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            for i in range(90):
                mod.STATS["daily_by_model"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "m1": {"requests": i, "filtered": 0,
                           "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            self.assertEqual(len(mod.STATS["daily_by_model"]), 90,
                             "exactly 90 buckets must hit the <= early-return branch")
            mod.STATS["daily_by_model"]["2026-12-31"] = {
                "m1": {"requests": 90, "filtered": 0,
                       "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            dbm = mod.STATS["daily_by_model"]
            self.assertEqual(len(dbm), 90, "91 buckets must prune back to exactly 90")
            self.assertNotIn("2026-01-01", dbm, "only the oldest bucket must be deleted")
            self.assertIn("2026-01-02", dbm, "second-oldest bucket must survive")
            self.assertIn("2026-12-31", dbm, "newest bucket must survive")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
```

new_string（两次 save 后各补文件断言，对齐 `test_daily_prune_exact_boundary`:741-744 模式并覆盖"91 桶 prune 回 90"的文件侧）：

```python
    def test_daily_by_model_prune_exact_boundary(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            for i in range(90):
                mod.STATS["daily_by_model"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "m1": {"requests": i, "filtered": 0,
                           "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            self.assertEqual(len(mod.STATS["daily_by_model"]), 90,
                             "exactly 90 buckets must hit the <= early-return branch")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)["stats"]["daily_by_model"]
            self.assertEqual(len(saved), 90,
                             "file must keep all 90 buckets when prune early-returns")
            self.assertIn("2026-01-01", saved,
                          "at exactly 90 buckets nothing must be pruned from the file")
            mod.STATS["daily_by_model"]["2026-12-31"] = {
                "m1": {"requests": 90, "filtered": 0,
                       "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            dbm = mod.STATS["daily_by_model"]
            self.assertEqual(len(dbm), 90, "91 buckets must prune back to exactly 90")
            self.assertNotIn("2026-01-01", dbm, "only the oldest bucket must be deleted")
            self.assertIn("2026-01-02", dbm, "second-oldest bucket must survive")
            self.assertIn("2026-12-31", dbm, "newest bucket must survive")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)["stats"]["daily_by_model"]
            self.assertEqual(len(saved), 90,
                             "file must prune back to exactly 90 buckets")
            self.assertNotIn("2026-01-01", saved,
                             "only the oldest bucket must be deleted from the file")
            self.assertIn("2026-12-31", saved,
                          "newest bucket must survive in the file")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
```

**Edit 1c** — `test_daily_by_model_prune_empty` 整方法替换（补空矩阵落盘文件断言，满足 spec acceptance"3 个 prune 测试的文件断言"）。

old_string（现行全文，唯一）：

```python
    def test_daily_by_model_prune_empty(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            mod.save_stats_counters(path)  # {} 不得抛异常
            self.assertEqual(mod.STATS["daily_by_model"], {},
                             "empty daily_by_model must stay empty after save")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
```

new_string（finally 后新增文件断言：空矩阵以空 dict 形状落盘，键存在且为 {}）：

```python
    def test_daily_by_model_prune_empty(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            mod.save_stats_counters(path)  # {} 不得抛异常
            self.assertEqual(mod.STATS["daily_by_model"], {},
                             "empty daily_by_model must stay empty after save")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily_by_model"]
        self.assertEqual(saved, {},
                         "empty daily_by_model must persist as an empty dict")
```

**Step 1 验证（预期 FAIL）**：

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_daily_by_model_prune_on_save ProxyDashboardUnitTest.test_daily_by_model_prune_exact_boundary ProxyDashboardUnitTest.test_daily_by_model_prune_empty
```

预期：3 个测试均 FAIL（`KeyError: 'daily_by_model'`——文件 `stats` 下尚无该键），exit 1。贴实际输出。

### Step 2 — 实现（save_stats_counters 落盘）

**Edit 1d** — `ctyun-stream-fix-proxy.py` 中 `save_stats_counters` 函数体替换。

old_string（现行全文，唯一）：

```python
def save_stats_counters(path: str) -> None:
    """把累计计数、按天分桶与当前上游端点全量写入持久化文件（SIGTERM / set_upstream_base 共用）。"""
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total")}
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
    with _CFG_LOCK:
        base = UPSTREAM_BASE
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base,
                   "stats": dict(counters, daily=daily)}, fh, ensure_ascii=False)
    os.replace(tmp, path)
```

new_string（STATS_LOCK 块内追加双层深拷贝——复用 `stats_snapshot`:411-413 既有拷贝模式；json.dump 扩展 `daily_by_model` 键。落盘的是 :247 prune 后的拷贝，≤90 天天然保证）：

```python
def save_stats_counters(path: str) -> None:
    """把累计计数、按天分桶与当前上游端点全量写入持久化文件（SIGTERM / set_upstream_base 共用）。"""
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total")}
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
        daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                          for d, models in STATS["daily_by_model"].items()}
    with _CFG_LOCK:
        base = UPSTREAM_BASE
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model)},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)
```

### Step 3 — 验证

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 49 tests ... OK`，exit 0（本卡改 3 个既有测试、不新增，总数仍 49；Step 1 的 3 个 FAIL 转 green，其余 46 个无回归）。

**验收**：save 后文件含 `stats.daily_by_model`，内容与内存矩阵一致且 ≤90 日期桶；三个 by_model prune 测试（on_save / exact_boundary / empty）的文件断言全过。

**commit**: `feat(stats): save_stats_counters 落盘 daily_by_model 矩阵`

---

## 卡 2（tier A）: 新增 load_daily_by_model_buckets 嵌套清洗 loader + main() 恢复 + roundtrip 新单测

**目标**：新增独立 loader（不扩展 `load_daily_buckets`，保持其单层职责与既有测试不动）；`main()` 启动恢复一行赋值；新增 roundtrip 单测覆盖 spec 用例矩阵 1-5。

**锚点（实测）**：`ctyun-stream-fix-proxy.py:285-300`（`load_daily_buckets`，其后 :303 为 `flush_stats_if_dirty`）、`:282`（`_DAILY_FIELDS`）、`:45`（`BY_MODEL_CAP = 32`）、`:1177-1189`（`main()` 恢复块）；`ctyun-stream-fix-proxy.test.py:676-697`（`test_daily_persist_roundtrip_legacy_and_corrupt`，新测试插在其后）。

### Step 1 — 先写失败测试（新增整方法）

**Edit 2a** — `ctyun-stream-fix-proxy.test.py` 中，在 `test_daily_persist_roundtrip_legacy_and_corrupt` 与 `test_daily_prune_on_save` 两个方法之间插入新方法。

old_string（两方法边界，唯一）：

```python
        buckets = mod.load_daily_buckets(path)
        self.assertNotIn("2026-01-01", buckets, "non-dict bucket must be skipped")
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(buckets["2026-01-02"]["errors_upstream"], 2)

    def test_daily_prune_on_save(self) -> None:
```

new_string（原边界 + 新方法 `test_daily_by_model_persist_roundtrip`，覆盖 spec 用例矩阵 1-5；orig_dbm 备份 + finally 恢复防污染）：

```python
        buckets = mod.load_daily_buckets(path)
        self.assertNotIn("2026-01-01", buckets, "non-dict bucket must be skipped")
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(buckets["2026-01-02"]["errors_upstream"], 2)

    def test_daily_by_model_persist_roundtrip(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit7-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        try:
            # 用例 1：save→load roundtrip 全等（main() 重启恢复路径的等价操作序列）
            matrix = {
                "2026-01-02": {
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0}}}
            mod.STATS["daily_by_model"] = matrix
            mod.save_stats_counters(path)
            self.assertEqual(mod.load_daily_by_model_buckets(path), matrix,
                             "save->load roundtrip must restore the matrix verbatim")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
        # 用例 2：legacy 文件无 daily_by_model 键 → {}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://x",
                       "stats": {"requests_total": 1}}, fh)
        self.assertEqual(mod.load_daily_by_model_buckets(path), {},
                         "legacy file without daily_by_model key must yield {}")
        # 用例 3：损坏结构逐项容错（顶层非 dict / 日期桶非 dict / entry 非 dict / 坏字段→0）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": "not-a-dict"}}, fh)
        self.assertEqual(mod.load_daily_by_model_buckets(path), {},
                         "non-dict daily_by_model must yield {}")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "2026-01-01": "bad",
                "2026-01-02": {"m1": "bad",
                               "m2": {"requests": 5, "filtered": -1,
                                      "errors_proxy": "x",
                                      "errors_upstream": 2}}}}}, fh)
        self.assertEqual(mod.load_daily_by_model_buckets(path),
                         {"2026-01-02": {"m2": {"requests": 5, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 2, "retries": 0}}},
                         "non-dict bucket/entry must be skipped; bad fields coerced to 0")
        # 用例 4：非 ISO 日期 key 跳过（round-trip 校验，版本无关）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "20260101": {"m1": {"requests": 1, "filtered": 0,
                                    "errors_proxy": 0, "errors_upstream": 0, "retries": 0}},
                "not-a-date": {"m1": {"requests": 1, "filtered": 0,
                                      "errors_proxy": 0, "errors_upstream": 0, "retries": 0}},
                "2026-01-03": {"m1": {"requests": 1}}}}}, fh)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(set(dbm), {"2026-01-03"},
                         "non-ISO date keys must be skipped")
        self.assertEqual(dbm["2026-01-03"]["m1"],
                         {"requests": 1, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0},
                         "missing fields must be filled with 0")
        # 用例 5：单日 40 模型 → 读回恰 32（文件出现序前 32）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {"2026-01-04": {
                "m%02d" % i: {"requests": i} for i in range(40)}}}}, fh)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(len(dbm["2026-01-04"]), 32,
                         "model count must be capped at BY_MODEL_CAP on load")
        self.assertIn("m31", dbm["2026-01-04"],
                      "32nd model in file order must survive the cap")
        self.assertNotIn("m32", dbm["2026-01-04"],
                         "models beyond the cap must be dropped")
        self.assertEqual(dbm["2026-01-04"]["m31"]["requests"], 31,
                         "surviving entries must keep their values")

    def test_daily_prune_on_save(self) -> None:
```

**Step 1 验证（预期 FAIL）**：

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_daily_by_model_persist_roundtrip
```

预期：FAIL（`AttributeError: module 'ctyun_stream_fix_proxy' has no attribute 'load_daily_by_model_buckets'`），exit 1。贴实际输出。

### Step 2 — 实现（loader + main 恢复）

**Edit 2b** — `ctyun-stream-fix-proxy.py` 中，在 `load_daily_buckets` 之后、`flush_stats_if_dirty` 之前插入新函数。

old_string（函数边界，唯一——`clean[field] = value ...` 仅在 load_daily_buckets 出现一次）：

```python
            clean[field] = value if isinstance(value, int) and value >= 0 else 0
        out[key] = clean
    return out


def flush_stats_if_dirty(path: str) -> None:
```

new_string（spec 代码逐字转写；仅注释内行号引用改为函数名引用，防行号漂移，语义零改动）：

```python
            clean[field] = value if isinstance(value, int) and value >= 0 else 0
        out[key] = clean
    return out


def load_daily_by_model_buckets(path: str) -> dict:
    """读日期→模型→_DAILY_FIELDS 矩阵；缺/损坏/legacy 无 daily_by_model 键 → {}。"""
    stats = _load_persist_file(path).get("stats")
    raw = stats.get("daily_by_model") if isinstance(stats, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, models in raw.items():
        # 外层 ISO 日期容错：fromisoformat + round-trip（拒 "20260101"/"2026-1-1"
        # 等非规范形；_prune_daily 依赖 ISO 字典序排序，坏 key 必须挡在内存外）
        try:
            d = datetime.date.fromisoformat(key)
        except (ValueError, TypeError):
            continue
        if str(d) != key or not isinstance(models, dict):
            continue
        bucket = {}
        for model, entry in models.items():
            # 内层逐模型清洗 + BY_MODEL_CAP 截断：按文件出现序保留前 32，与运行时
            # "len < BY_MODEL_CAP 才插新键"（_record_request/_record_empty_retry）语义对齐
            if not isinstance(entry, dict) or len(bucket) >= BY_MODEL_CAP:
                continue
            clean = {}
            for field in _DAILY_FIELDS:  # 同 load_daily_buckets 逐字段规则
                value = entry.get(field)
                clean[field] = value if isinstance(value, int) and value >= 0 else 0
            bucket[model] = clean
        out[key] = bucket
    return out


def flush_stats_if_dirty(path: str) -> None:
```

**Edit 2c** — `ctyun-stream-fix-proxy.py` `main()` 恢复块替换。

old_string（现行，唯一——`STATS["daily"] = daily` 全文仅此一处）：

```python
    counters = load_stats_counters(PERSIST_PATH)  # 累计计数跨重启续算
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["daily"] = daily
        _stats_dirty = False
```

new_string（:1182 后加 load 一行；锁内 :1188 后加赋值一行）：

```python
    counters = load_stats_counters(PERSIST_PATH)  # 累计计数跨重启续算
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    daily_by_model = load_daily_by_model_buckets(PERSIST_PATH)  # 按天×模型矩阵跨重启续算
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["daily"] = daily
        STATS["daily_by_model"] = daily_by_model
        _stats_dirty = False
```

### Step 3 — 验证

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 50 tests ... OK`，exit 0（新增 1 测试；含集成测试——seed_persist 仅含 upstream_base 无 stats 键，loader 返回 {}，无破坏）。

**落盘体积实测（spec Risks 要求，一次性探针，不进测试代码）**：

```sh
/usr/bin/python3 -c "
import importlib.util, os, tempfile
spec = importlib.util.spec_from_file_location('m', 'ctyun-stream-fix-proxy.py')
mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
orig = mod.STATS['daily_by_model']
mod.STATS['daily_by_model'] = {
    ('2026-%02d-%02d' % (1 + i // 28, 1 + i % 28)):
        {'m%02d' % j: {'requests': j, 'filtered': j, 'errors_proxy': 0,
                       'errors_upstream': 0, 'retries': 0} for j in range(32)}
    for i in range(90)}
try:
    p = tempfile.mktemp(suffix='.json')
    mod.save_stats_counters(p)
    print('worst-case persist file size:', os.path.getsize(p), 'bytes')
finally:
    mod.STATS['daily_by_model'] = orig
    os.unlink(p)
"
```

预期：输出 `worst-case persist file size: <N> bytes`，N < 1048576（<1MB；90 天 × 32 模型 × 5 字段上界；PLAN 阶段干跑实测同构造为 276,280 bytes）。贴实际输出。

**验收**：roundtrip 全等（用例 1）；legacy 无键 → {}（用例 2）；各类损坏不崩、逐桶清洗（用例 3-4）；40 模型读回 32（用例 5）；全量 50 tests 绿。

**commit**: `feat(stats): 重启恢复 daily_by_model（嵌套清洗 loader + main 恢复）`

---

## 卡 3（tier B）: 文案同步（py 副表标题 / py footer / README 口径表 + 用例数）

**目标**：三处"重启清零/不持久化"文案改为跨重启保留口径；README:69 用例数同步为 50。纯文案/doc 改动，TDD 豁免，grep + 全量测试验证。

**锚点（实测）**：`ctyun-stream-fix-proxy.py:824`（副表 card-title）、`:851`（footer 行，上下文 :849-852）；`README.md:69`（`45 个用例`）、`:77`（口径表副表行）。

### Edit 3a — py:824 副表标题

old_string（唯一）：

```python
    <div class="card-title">按天 × 模型（<span id="daily-model-title-range">近7天</span>，内存累计，重启清零）</div>
```

new_string：

```python
    <div class="card-title">按天 × 模型（<span id="daily-model-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
```

### Edit 3b — py:851 footer 行

old_string（唯一）：

```python
  按天×模型计数自进程启动累计，不持久化（重启清零）；
```

new_string（与 :849-850、:852 现文案衔接，"见下行"指向 :852 的主副表差值说明）：

```python
  按天×模型计数同 daily 口径跨重启保留（90 天 prune，副表数值 ≤ 主表，差值=当日无 model 请求，见下行）；
```

### Edit 3c — README:77 口径表副表行

old_string（唯一）：

```markdown
| 「按天统计」「按天 × 模型」 | 随所选时间段过滤（最远回溯=上月+当月 ≤62 天 < 90 天 retention） | 主表是（90 天 prune）/ 副表否（重启清零） |
```

new_string：

```markdown
| 「按天统计」「按天 × 模型」 | 随所选时间段过滤（最远回溯=上月+当月 ≤62 天 < 90 天 retention） | 主表/副表均是（90 天 prune） |
```

### Edit 3d — README:69 用例数（45 → 50；45 为陈旧值，实际 baseline 49 + 本计划新增 1 = 50）

old_string（唯一）：

```markdown
45 个用例。**必须用 `/usr/bin/python3`**：Homebrew 的 Python 3.14 `http.server.HTTPServer` 构造会挂死（进程存活但不 LISTEN、零报错）。
```

new_string：

```markdown
50 个用例。**必须用 `/usr/bin/python3`**：Homebrew 的 Python 3.14 `http.server.HTTPServer` 构造会挂死（进程存活但不 LISTEN、零报错）。
```

### 验证

```sh
grep -n "重启清零\|不持久化\|内存累计" ctyun-stream-fix-proxy.py README.md; echo "grep exit: $?"
```

预期：无匹配输出，`grep exit: 1`（三处文案已全部替换；全库仅 py:824、py:851、README:77 含过时口径，已实测确认无第四处）。

```sh
grep -n "个用例" README.md
```

预期：仅一行 `69:50 个用例。...`。

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 50 tests ... OK`，exit 0（文案改动零回归）。

**验收**：三处文案（py:824、py:851、README:77）不再出现"重启清零/不持久化/内存累计"；README:69 = 50 个用例；全量测试绿。

**commit**: `docs: 同步 daily_by_model 持久化口径文案与用例数`

---

## Self-Review（PLAN-B 完成自查，2026-09-11）

1. **spec coverage**：spec Files 三节全覆盖——py 节 4 处改动（save :241-258 / loader 新增 / main :1181-1189 / 文案 :824+:851）→ 卡 1/2/3；test 节 3 处（三个 by_model prune 测试补文件断言 + roundtrip 新测试 5 用例）→ 卡 1 Step 1 / 卡 2 Step 1；README 节 2 处（:77 口径 + :69 用例数）→ 卡 3。Acceptance 6 条逐条映射：文件含键且 ≤90（卡 1）、roundtrip 全等+重启恢复（卡 2 用例 1）、legacy/损坏不崩（卡 2 用例 2-4）、40→32+prune 文件断言（卡 1+卡 2 用例 5）、全绿+用例数同步（各卡 Step 3 + 卡 3）、三处文案（卡 3）。Risks 4 条均有对策落位：体积实测（卡 2 探针）、锁内拷贝同量级（复用 stats_snapshot 模式）、round-trip 版本无关（Global Constraints 3 + 用例 4）、orig_dbm 备份恢复（Global Constraints 2 + 卡 1/卡 2 测试代码）。Exclusions 全部进 Global Constraints 4 不动清单。
2. **placeholder scan**：3 卡共 11 个 Edit（1a/1b/1c/1d/2a/2b/2c/3a/3b/3c/3d，计 11 个 old/new 对）全部为逐字完整代码块，无 TODO/伪代码/指令式描述；grep 复查本文无 "待补"/"略" 类占位（唯一命中为本节自述；"同 load_daily_buckets 逐字段规则" 为代码内注释引用既有实现，非占位）。
3. **type consistency**：loader 签名 `load_daily_by_model_buckets(path: str) -> dict` 与 spec 一致；save 深拷贝产出 `dict[str, dict[str, dict[str, int]]]` 与 `STATS["daily_by_model"]` 初始化形状（:103）及 `_record_request` 写入形状（:350-352 五字段）一致；roundtrip 用例 3 期望值字段序 (requests, filtered, errors_proxy, errors_upstream, retries) 与 `_DAILY_FIELDS`:282 一致；main() 恢复变量名 `daily_by_model` 无遮蔽（函数内无同名）。
4. **可落盘性**：每个 Edit 给出唯一 old_string——已用脚本对 11 个 old_string 逐个 `content.count(old)` 程序化核验（1a/1b/1c/2a → test 文件、1d/2b/2c/3a/3b → py 文件、3c/3d → README），11/11 unique-OK、exit 0；验证命令均为可执行原样命令（CLI 单测选择 `ProxyDashboardUnitTest.test_xxx` 已实测可用）；预期失败模式明确（KeyError: 'daily_by_model' / AttributeError: load_daily_by_model_buckets）。
5. **锚点实测 + 期望值干跑**：全部锚点于 2026-09-11 在 worktree `fix/daily-by-model-persist`（HEAD 3e3a204）实测——baseline `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → Ran 49 tests / OK / exit 0；`/usr/bin/python3` = 3.9.6；BY_MODEL_CAP=32:45、DAILY_RETENTION_DAYS=90:46、_DAILY_FIELDS:282、_prune_daily:220-228、stats_snapshot 拷贝模式:411-413、cap 插键语义 :349/:394、形状同步注释 :383-404、orig_dbm 模式 :762-784/:791-811/:818-825 均已读码确认；集成测试 seed_persist（:1084/:1222/:1329）仅含 upstream_base，新 loader 对其返回 {}，零破坏；全库 grep 确认"重启清零/不持久化/内存累计"仅 3 处（py:824、py:851、README:77）。**期望值干跑**：将 spec loader + plan save 逻辑逐字转写为独立脚本，在目标运行时 3.9.6 上跑通卡 1 三个测试的全部断言（含新文件断言，prune 后 file buckets=90、空矩阵落盘 {}）与卡 2 用例 1-5 的全部 assertEqual 期望值（roundtrip 全等 / legacy {} / 损坏清洗 / 非 ISO key 跳过 / 40→32 cap），体积探针 276,280 bytes <1MB，全部 PASS、exit 0——计划内测试代码的期望值与实现行为已预先对齐。
