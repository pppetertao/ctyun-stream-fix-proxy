# PLAN — daily_by_model 内存 prune（followup: 内存线性增长）

## Header

- **spec**: `docs/superpowers/specs/2026-09-11-daily-by-model-prune-design.md`（本 worktree，commit 0328f5e）
- **worktree / 分支**: `/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-prune` @ `fix/daily-by-model-prune`
- **baseline（2026-09-11 PLAN-B 实测登记，红清单 = 空）**:

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
...
Ran 45 tests in 19.659s

OK
Exit code: 0
```

  与 spec 预期 45 tests 一致，45/45 全绿。VERIFY 只对"baseline 绿 → 现在红"负责。
- **统一验证命令（全卡）**: `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py`（test:1239 `unittest.main(verbosity=2)`，输出自带逐用例行，全程约 20s）
- **卡数与 tier**: 2 卡 — Task 1 tier B、Task 2 tier A，全卡派 `executor`；**无 C 档**（生产 1 行调用 + 1 行 docstring + 3 个单测 + 1 行 followup 全部在 PLAN 期写死，不命中真机验证 / 运行时数据 / DI 实测三留白）
- **锚点实测（2026-09-11 grep/Read 验证通过）**:
  - 生产 `ctyun-stream-fix-proxy.py`: `BY_MODEL_CAP = 32` @ :45、`DAILY_RETENTION_DAYS = 90` @ :46、`STATS` init 含 `"daily_by_model": {}` @ :103、`_prune_daily` def @ :220、`save_stats_counters` def @ :239、锁块 :241-245、`_record_request` 写入 :344、`_record_empty_retry` 写入 :389、`stats_snapshot` @ :404、`main()` 恢复路径 :1178-1186（只恢复 `daily`，`daily_by_model` 重启即清）
  - 测试 `ctyun-stream-fix-proxy.test.py`: `ProxyDashboardUnitTest` @ :362、`test_daily_by_model_cap` @ :474、`test_daily_prune_on_save` @ :699、`test_stats_snapshot_daily_is_copy` @ :719、`unittest.main(verbosity=2)` @ :1239、`tempfile` 已 import @ :22
  - 3 个 Edit 锚点串唯一性 grep -c = 1（见各卡 old_string）

## Global Constraints

1. **锁内原地（spec D1 核心禁止项）**：`_prune_daily(STATS["daily_by_model"])` 必须在 `save_stats_counters` 的 `with STATS_LOCK:` 块内、对内存态原地执行。禁止移到锁外、禁止只 prune 副本——既有 `_prune_daily(daily)` 作用于锁内浅拷贝的模式（磁盘受控、内存无界）是本 episode 明确不重蹈的。
2. **断言内存态**：3 个新单测的断言对象是 `mod.STATS["daily_by_model"]`（swap 后的内存 dict，断言在 `try` 块内、`finally` 还原之前执行），**不得读回持久化文件**——`daily_by_model` 不落盘，文件里验证不到 prune。
3. **不动清单（spec Exclusions）**：持久化 schema（save 输出 json 仍只含 4 counters + `daily`，:253-254 零改动）、`load_stats_counters`/`load_daily_buckets`/`main()` 恢复路径、`BY_MODEL_CAP=32` 与每日 cap 逻辑（:346-350 / :391-395）、前端 `renderDailyByModel`、`STATS["daily"]` 内存态（独立 followup，Task 2 记录不修）。
4. **零新增面**：不加常量 / env / 依赖；retention 复用 `DAILY_RETENTION_DAYS = 90`（:46），`_prune_daily` 函数体零改动（仅 docstring 补共用说明）。
5. **测试隔离**：swap `mod.STATS["daily_by_model"]` + `try/finally` 还原（test:704-713 同款）；`tempfile.mkdtemp` + `addCleanup(shutil.rmtree)`（test:701-703 同款）；植入 entry 用 5 字段形状（:347-349 同款：`requests/filtered/errors_proxy/errors_upstream/retries`）。
6. **环境**：`/usr/bin/python3`，纯 stdlib；LF 换行；所有文件路径相对 worktree 根。
7. **提交**：Conventional Commits，每卡一个 commit（Task 1 `fix(stats):`、Task 2 `chore(docs):`）；git author 自检 = pppetertao / 178146586+pppetertao@users.noreply.github.com（非 placeholder）。

---

## Task 1 (tier B → executor): daily_by_model 锁内 prune — 3 单测（red）+ 生产 2 处 Edit（green）

**目标**：`STATS["daily_by_model"]` 在每次 `save_stats_counters` 时于 `STATS_LOCK` 内原地 prune 到最近 90 天（spec D1 方案 A / D2 触发点 / D3 retention）。

### Step 1（red）：新增 3 个单测

文件 `ctyun-stream-fix-proxy.test.py`，插入位置：`ProxyDashboardUnitTest`（:362）内、`test_daily_prune_on_save`（:699-717）之后、`test_stats_snapshot_daily_is_copy`（:719）之前。一次 Edit 完成（old_string 为当前 :716-719 原文，已验证唯一）：

**Edit old_string**（当前文件原文）：

```python
        self.assertLessEqual(len(saved), 90, "prune must cap buckets at 90 days")
        self.assertIn("2026-04-11", saved, "most recent bucket must survive prune")

    def test_stats_snapshot_daily_is_copy(self) -> None:
```

**Edit new_string**（原文 + 3 个新方法）：

```python
        self.assertLessEqual(len(saved), 90, "prune must cap buckets at 90 days")
        self.assertIn("2026-04-11", saved, "most recent bucket must survive prune")

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

    def test_stats_snapshot_daily_is_copy(self) -> None:
```

fixture 说明（executor 无需决策，仅背景）：`"2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)` 生成 95 个互异 ISO 日期 key（"2026-01-01".."2026-04-11"，字典序 = 时间序）；`today_key()` 为真实运行日期（> 2026-04-11），与植入 key 无碰撞。已知理论失败窗口（运行日期 ∈ 2026-01-01..06 或 = 2026-04-11）均在过去，不构成现实风险。

### Step 2：red 验证（预期 2 fail / 1 pass — 这是 TDD 红阶段，不是事故，禁止改测试"修绿"）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期输出（关键行）：

```
FAIL: test_daily_by_model_prune_exact_boundary (__main__.ProxyDashboardUnitTest)
AssertionError: 91 != 90 : 91 buckets must prune back to exactly 90
FAIL: test_daily_by_model_prune_on_save (__main__.ProxyDashboardUnitTest)
AssertionError: 96 not less than or equal to 90 : in-memory daily_by_model must be pruned on save
...
Ran 48 tests in <N>s   （<N> 为实际耗时秒数，数值不定）

FAILED (failures=2)
Exit code: 1
```

- `test_daily_by_model_prune_empty` 此阶段**通过**（空 dict 回归守卫，设计如此，不证明 gap）
- 45 个既有用例必须全绿（baseline 红清单为空；若出现其他 FAIL → 停止，RTC 上报）
- 恰好 2 个 FAIL 且失败断言文本如上 → gap 已证实，进 Step 3

### Step 3（green）：生产 2 处 Edit

文件 `ctyun-stream-fix-proxy.py`。

**Edit A — `save_stats_counters`（:239）锁块内加一行**（old_string 为当前 :241-245 原文，已验证唯一；new_string 仅在锁块内插入 1 行调用，行尾注释为 spec 原文）：

```python
# old_string
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total")}
        daily = {k: dict(v) for k, v in STATS["daily"].items()}
    _prune_daily(daily)
```

```python
# new_string
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total")}
        daily = {k: dict(v) for k, v in STATS["daily"].items()}
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
    _prune_daily(daily)
```

**Edit B — `_prune_daily`（:220）docstring 补共用说明**（函数体零改动；old_string 已验证唯一）：

```python
# old_string
def _prune_daily(daily: dict) -> dict:
    """按日期 key 降序保留最近 DAILY_RETENTION_DAYS 天（ISO 日期字符串排序即时间序）。"""
```

```python
# new_string
def _prune_daily(daily: dict) -> dict:
    """按日期 key 降序保留最近 DAILY_RETENTION_DAYS 天（ISO 日期字符串排序即时间序）。
    value 形状无关（平 dict 按 key prune），daily 与 daily_by_model 共用；
    调用方须持 STATS_LOCK（对内存态调用时）。"""
```

### Step 4：green 验证

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期输出（关键行）：

```
Ran 48 tests in <N>s   （<N> 为实际耗时秒数，数值不定）

OK
Exit code: 0
```

含 45 既有零回归（重点：`test_daily_by_model_cap`、`test_daily_prune_on_save`、`test_stats_snapshot_daily_is_copy`）+ 3 新增全绿。

### Step 5：锁内放置目检（Global Constraint 1 的机械验证）

```
$ grep -n -B4 -A1 '_prune_daily(STATS' ctyun-stream-fix-proxy.py
```

预期：新调用行缩进 8 空格（`with STATS_LOCK:` 块内、位于 `daily = {...}` 之后），其下一行 `_prune_daily(daily)` 缩进 4 空格（锁外、作用于副本）。若新调用在锁外 → 违反核心约束，停止上报。

### Step 6：commit

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
$ git commit -m "fix(stats): daily_by_model 锁内内存态 prune（复用 _prune_daily）+ 3 单测"
```

### 验收（spec Acceptance 对应）

- [ ] 全量 48 tests OK，exit 0（3 新增通过 + 45 既有零回归）
- [ ] 超期 key 从**内存** `STATS["daily_by_model"]` 消失（单测断言对象即内存态，非落盘副本）
- [ ] prune 调用位于 `STATS_LOCK` 块内（Step 5 目检）
- [ ] 持久化 schema 不变：save 输出 json 的 `stats` 键仍只含 4 counters + `daily`（save 路径 :253-254 零改动，由既有用例 `test_daily_prune_on_save` 读回文件断言兜底）
- [ ] `_prune_daily` 函数体零改动（仅 docstring；边界分支 `len <= 90` 早返回与 `sorted(daily)[:-90]` 逐个 `del` 不动）

---

## Task 2 (tier A → executor): followups.md 追加 STATS["daily"] 内存态 followup（TDD 豁免：纯文档一行）

**目标**：落实 spec Risks 第 2 条——姊妹问题（`STATS["daily"]` 内存态同样不受副本 prune 保护）出 scope 路由到 followups.md，另开 episode 处理。依赖 Task 1 已完成（引用"已修复的 daily_by_model"表述）。

### Step 1：追加一行

文件 `docs/superpowers/followups.md`（当前仅 1 条 entry，即催生本 episode 的那条）。Edit old_string（现有 entry 尾部，唯一）：

```
 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测
```

Edit new_string（原行 + 新行，格式 `发现位置 | 描述 | 建议处理方式` 与既有 entry 一致）：

```
 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测
- `ctyun-stream-fix-proxy.py` `save_stats_counters`（`_prune_daily(daily)` 仅 prune 锁内浅拷贝副本，内存 `STATS["daily"]` 不受保护） | `STATS["daily"]` 内存态随运行天数线性增长——磁盘副本受 90 天滚动约束但内存无界（与已修复的 daily_by_model 同增长率泄漏） | 建议另开 episode：`daily` 改为锁内对 `STATS["daily"]` 原地 prune（同 daily_by_model 模式），`test_daily_prune_on_save` 断言对象迁到内存态并复核
```

### Step 2：验证（doc-sync：新行存在 + 计数）

```
$ grep -c "^- " docs/superpowers/followups.md
2
$ grep -c "STATS\[\"daily\"\] 不受保护" docs/superpowers/followups.md
1
```

### Step 3：commit

```
$ git add docs/superpowers/followups.md
$ git commit -m "chore(docs): 记录 STATS daily 内存态 prune 缺口 followup"
```

### 验收

- [ ] followups.md 含 2 条 entry，新条目覆盖 spec Risks 第 2 条的姊妹问题（位置 / 描述 / 建议处理方式三段齐全）
- [ ] 不改 followups.md 既有 entry 与其他文件
