# PLAN — STATS["daily"] 内存态 prune（save_stats_counters 副本 prune 修复）

## Header

- **spec**: `docs/superpowers/specs/2026-09-11-daily-memory-prune-design.md`（本 worktree，commit f53f530）
- **worktree / 分支**: `/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-memory-prune` @ `fix/daily-memory-prune`
- **baseline（2026-09-11 PLAN-B 实测登记，红清单 = 空）**:

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-memory-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
...
Ran 48 tests in 19.544s

OK
Exit code: 0
```

  与 spec 预期 48 tests 一致，48/48 全绿。VERIFY 只对"baseline 绿 → 现在红"负责。
- **统一验证命令（全卡）**: `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-memory-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py`（test:1309 `unittest.main(verbosity=2)`，输出自带逐用例行，全程约 20s）
- **卡数与 tier**: 2 卡 — Task 1 tier B、Task 2 tier A，全卡派 `executor`；**无 C 档**（生产锁内 3 行重排 + 1 个测试迁移 + 1 个新单测 + followup 删 1 行，全部在 PLAN 期写死，不命中真机验证 / 运行时数据 / DI 实测三留白）
- **锚点实测（2026-09-11 grep/Read 验证通过）**:
  - 生产 `ctyun-stream-fix-proxy.py`: `DAILY_RETENTION_DAYS = 90` @ :46、`aggregate_daily_range` def @ :188、`range_stats` def @ :205、`_prune_daily` def @ :220（函数体零改动）、`save_stats_counters` def @ :241、锁块 :243-248（:246 daily 拷贝行 / :247 daily_by_model prune 行 / :248 副本 prune 行）、`load_daily_buckets` @ :285、`stats_snapshot` @ :407（:410 另有一处 `snap["daily"] = {k: dict(v) ...}` 拷贝行，**不在本卡改动范围**，grep 验证模式已设计为只命中 save 块）
  - 测试 `ctyun-stream-fix-proxy.test.py`: `test_daily_prune_on_save` @ :699-717、`test_daily_by_model_prune_on_save` @ :719-746（内存断言结构参照）、`test_daily_by_model_prune_exact_boundary` @ :748-773（边界测试结构参照）、`json`/`os`/`shutil`/`tempfile` 已 import @ :13/:15/:17/:22、`unittest.main(verbosity=2)` @ :1309、当前 48 个 `def test_`
  - `docs/superpowers/followups.md`: 共 4 行（1 标题 + 2 entry + 1 空行），待删行 = :4（`仅 prune 锁内浅拷贝副本`）
  - 4 个 Edit 锚点串唯一性 grep -c = 1（见各卡 old_string；测试侧 save+finally 串在文件中出现 4 次，故 old_string 向上扩展至 fixture 行保证唯一）

## Global Constraints

1. **顺序敏感（spec 方案 A 核心）**：锁内必须**先** `_prune_daily(STATS["daily"])`（原地 prune）**再** `daily = {...}` 浅拷贝。禁止实现成先拷贝后 prune（磁盘将含 95 天，文件断言 `len(saved) <= 90` 兜底捕获）；禁止保留锁外 `_prune_daily(daily)` 副本调用（prune 后副本再 prune 是 len≤90 早退的永久空转 = 死代码，违反无死代码铁律；spec 方案 B 否决）。
2. **断言内存态为主 + 文件断言保留**：迁移后的 `test_daily_prune_on_save` 内存断言（`mod.STATS["daily"]`）在 `save_stats_counters(path)` 之后、`finally` 还原之前执行；既有文件断言（读回 settings.json）**原样保留**——`daily` 落盘（:257），磁盘契约是真实约束，且文件断言同时守护"先拷贝后 prune"顺序 bug。
3. **不动清单（spec Exclusions）**：持久化 schema（`json.dump` :256-257 键结构零改动）、`_prune_daily` 函数体（:220-228 零改动，含 docstring）、`load_daily_buckets`（:285）/ `main()` 恢复路径（:1183-1189）/ `stats_snapshot`（:407-422）/ `range_stats`（:205）/ `aggregate_daily_range`（:188）零改动、`DAILY_RETENTION_DAYS=90`（:46）不变、前端零改动。
4. **零新增面**：不加常量 / env / 依赖 / 新文件；`daily_by_model` 既有锁内 prune 行（:247）原样保留（仅对称参照，spec 已落地）。
5. **测试隔离**：swap `mod.STATS["daily"]` + `try/finally` 还原（test:704-713 既有模式，兼容原地 prune 零改动）；`tempfile.mkdtemp` + `addCleanup(shutil.rmtree)`（:701-703 同款）；植入桶用 5 字段形状（`requests/filtered/errors_proxy/errors_upstream/retries`）。
6. **环境**：`/usr/bin/python3` 3.9.6 纯 stdlib，无新语法；LF 换行；所有文件路径相对 worktree 根。
7. **提交**：Conventional Commits，每卡一个 commit（Task 1 `fix(stats):`、Task 2 `chore(docs):`）；git author 自检 = pppetertao / 178146586+pppetertao@users.noreply.github.com（非 placeholder）。

---

## Task 1 (tier B → executor): save_stats_counters 锁内重排（先原地 prune 再拷贝）— 测试迁移 + 新边界单测（red）+ 生产 3 行替换（green）

**目标**：`STATS["daily"]` 在每次 `save_stats_counters` 时于 `STATS_LOCK` 内原地 prune 到最近 90 天，磁盘副本 = prune 后内存态（spec Files 1 方案 A + Files 2）。

### Step 1（red 前置）：迁移 `test_daily_prune_on_save` + 新增 `test_daily_prune_exact_boundary`

文件 `ctyun-stream-fix-proxy.test.py`，一次 Edit 完成（old_string 为当前 :707-713 原文，已验证唯一——save+finally 串在文件中出现 4 次，向上扩展至 `for i in range(95):` 与 daily 专有 fixture 行后仅此一处）：

**Edit old_string**（当前文件原文，:707-713）：

```python
            for i in range(95):
                mod.STATS["daily"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "requests": i, "filtered": 0,
                    "errors_proxy": 0, "errors_upstream": 0}
            mod.save_stats_counters(path)
        finally:
            mod.STATS["daily"] = orig_daily
```

**Edit new_string**（save 调用后插入内存断言 + finally 还原 + 新增边界测试方法；内存断言结构镜像 :735-744 dbm 测试，桶形状改 daily 平桶）：

```python
            for i in range(95):
                mod.STATS["daily"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "requests": i, "filtered": 0,
                    "errors_proxy": 0, "errors_upstream": 0}
            mod.save_stats_counters(path)
            d = mod.STATS["daily"]  # 断言内存态：prune 必须落到 STATS["daily"] 原对象
            self.assertLessEqual(len(d), 90, "in-memory daily must be pruned on save")
            self.assertNotIn("2026-01-01", d, "oldest buckets must be pruned from memory")
            self.assertIn("2026-04-11", d, "most recent bucket must survive prune")
            self.assertEqual(d["2026-04-11"]["requests"], 94, "surviving buckets untouched")
        finally:
            mod.STATS["daily"] = orig_daily

    def test_daily_prune_exact_boundary(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit6-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_daily = mod.STATS["daily"]
        mod.STATS["daily"] = {}
        try:
            for i in range(90):
                mod.STATS["daily"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "requests": i, "filtered": 0,
                    "errors_proxy": 0, "errors_upstream": 0, "retries": 0}
            mod.save_stats_counters(path)
            self.assertEqual(len(mod.STATS["daily"]), 90,
                             "exactly 90 buckets must hit the <= early-return branch")
            self.assertIn("2026-01-01", mod.STATS["daily"],
                          "at exactly 90 buckets nothing must be pruned")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)["stats"]["daily"]
            self.assertEqual(len(saved), 90,
                             "file must keep all 90 buckets when prune early-returns")
            mod.STATS["daily"]["2026-12-31"] = {
                "requests": 90, "filtered": 0,
                "errors_proxy": 0, "errors_upstream": 0, "retries": 0}
            mod.save_stats_counters(path)
            d = mod.STATS["daily"]
            self.assertEqual(len(d), 90, "91 buckets must prune back to exactly 90")
            self.assertNotIn("2026-01-01", d, "only the oldest bucket must be deleted")
            self.assertIn("2026-01-02", d, "second-oldest bucket must survive")
            self.assertIn("2026-12-31", d, "newest bucket must survive")
        finally:
            mod.STATS["daily"] = orig_daily
```

fixture 说明（executor 无需决策，仅背景）：`"2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)` 生成互异 ISO 日期 key（"2026-01-01".."2026-04-11"，字典序 = 时间序）；`2026-12-31` 字典序最大（最新），与植入 key 无碰撞。既有文件断言（原 :714-717）在 Edit 范围之外，原样保留。

### Step 2：red 验证（预期 2 fail / 47 pass — 这是 TDD 红阶段，不是事故，禁止改测试"修绿"）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-memory-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期输出（关键行；unittest 按方法名字典序执行，`exact_boundary` 先于 `on_save`）：

```
FAIL: test_daily_prune_exact_boundary (__main__.ProxyDashboardUnitTest)
AssertionError: 91 != 90 : 91 buckets must prune back to exactly 90
FAIL: test_daily_prune_on_save (__main__.ProxyDashboardUnitTest)
AssertionError: 95 not less than or equal to 90 : in-memory daily must be pruned on save
...
Ran 49 tests in <N>s   （<N> 为实际耗时秒数，数值不定）

FAILED (failures=2)
Exit code: 1
```

- 本步即 spec Acceptance「迁移测试证伪旧实现」的实证：内存断言在旧实现（只 prune 副本）下必 fail（95 > 90），无需事后还原旧代码重跑。
- 47 个既有用例必须全绿（baseline 红清单为空；若出现其他 FAIL → 停止，RTC 上报）。
- 恰好 2 个 FAIL 且失败断言文本如上 → gap 已证实，进 Step 3。

### Step 3（green）：生产锁内 3 行替换

文件 `ctyun-stream-fix-proxy.py`。一次 Edit 完成（old_string 为当前 :246-248 原文，已验证唯一；new_string = spec Files 1 目标代码逐字）：

**Edit old_string**（当前文件原文，:246-248）：

```python
        daily = {k: dict(v) for k, v in STATS["daily"].items()}
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
    _prune_daily(daily)
```

**Edit new_string**（先原地 prune 再拷贝，:248 副本 prune 行删除）：

```python
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
```

### Step 4：green 验证

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-memory-prune && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期输出（关键行）：

```
Ran 49 tests in <N>s   （<N> 为实际耗时秒数，数值不定）

OK
Exit code: 0
```

含 47 既有零回归（重点：`test_daily_by_model_prune_on_save` / `test_daily_by_model_prune_exact_boundary` / `test_daily_by_model_prune_empty`——:247 dbm 行原样保留；`test_stats_snapshot_daily_is_copy` / `test_stats_snapshot_includes_range_stats`——:407-422 消费路径零改动）+ 迁移后 `test_daily_prune_on_save` + 新增 `test_daily_prune_exact_boundary` 全绿。

### Step 5：调用点清点 + 顺序目检（Global Constraint 1 的机械验证）

```
$ grep -n "_prune_daily" ctyun-stream-fix-proxy.py
```

预期输出（恰好 4 行：:171 docstring 引用 + :220 定义 + 锁内 2 次调用；**不得再出现 4 空格缩进的独立 `_prune_daily(daily)` 行**）：

```
171:    ISO 日期字符串字典序即时间序（_prune_daily 同性质）。非法 range_key → ValueError。
220:def _prune_daily(daily: dict) -> dict:
246:        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
247:        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
```

```
$ grep -n -E '_prune_daily\(STATS|daily = \{k: dict' ctyun-stream-fix-proxy.py
```

预期输出（恰好 3 行，行号 246 < 247 < 248 递增即证 prune 先于拷贝；模式不命中 :410 `snap["daily"] = {...}`，因该行 "daily" 后跟 `"]` 非空格）：

```
246:        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
247:        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
248:        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
```

若 prune 行行号大于拷贝行（顺序颠倒）、或出现 4 空格独立 `_prune_daily(daily)` → 违反核心约束，停止上报。

### Step 6：commit

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
$ git commit -m "fix(stats): daily 锁内内存态 prune（先 prune 后拷贝）+ 单测迁内存态 + 边界单测"
```

### 验收（spec Acceptance 对应）

- [ ] 全量 49 tests OK，exit 0（AC1：迁移后 `test_daily_prune_on_save` + 新增 `test_daily_prune_exact_boundary` 全绿 + 47 既有零回归）
- [ ] 迁移测试已证伪旧实现（AC2：Step 2 red 输出 2 FAIL，内存断言 95 > 90）
- [ ] `grep -n "_prune_daily"` 仅剩 :171 docstring + :220 定义 + 锁内 2 处调用，独立副本调用消失（AC3，Step 5 第一条 grep）
- [ ] prune 在拷贝之前（Step 5 第二条 grep 行号递增 246 < 248）
- [ ] `stats_snapshot`（:407）/ `range_stats`（:205）消费路径零改动且测试全绿（AC4：Step 4 全量绿，含 `test_stats_snapshot_daily_is_copy` / `test_stats_snapshot_includes_range_stats`）
- [ ] 持久化 schema 不变（save 输出 json 的 `stats` 键仍只含 4 counters + `daily`，:256-257 零改动）

---

## Task 2 (tier A → executor): followups.md 删除已消化条目（TDD 豁免：纯文档一行）

**目标**：spec Files 3——本 episode 已消化 `docs/superpowers/followups.md:4` 的 followup（STATS["daily"] 内存态 prune），留行即陈旧引用。依赖 Task 1 已完成。

### Step 1：删除第 4 行

文件 `docs/superpowers/followups.md`（当前共 4 行：标题 / 空行 / 2 条 entry）。Edit old_string（现有 :3-4 两行，唯一；new_string 仅保留第 3 行——daily_by_model entry 是另一 episode 的历史记录，不动）：

**Edit old_string**：

```
- `ctyun-stream-fix-proxy.py` `save_stats_counters`（仅 prune `daily`，`daily_by_model` 不 prune，+1 天 key/天，重启清） | `daily_by_model` 内存随运行天数线性增长（键为日期串，值矩阵受 `BY_MODEL_CAP=32` 约束），长期运行进程内存缓慢膨胀 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测
- `ctyun-stream-fix-proxy.py` `save_stats_counters`（`_prune_daily(daily)` 仅 prune 锁内浅拷贝副本，内存 `STATS["daily"]` 不受保护） | `STATS["daily"]` 内存态随运行天数线性增长——磁盘副本受 90 天滚动约束但内存无界（与已修复的 daily_by_model 同增长率泄漏） | 建议另开 episode：`daily` 改为锁内对 `STATS["daily"]` 原地 prune（同 daily_by_model 模式），`test_daily_prune_on_save` 断言对象迁到内存态并复核
```

**Edit new_string**：

```
- `ctyun-stream-fix-proxy.py` `save_stats_counters`（仅 prune `daily`，`daily_by_model` 不 prune，+1 天 key/天，重启清） | `daily_by_model` 内存随运行天数线性增长（键为日期串，值矩阵受 `BY_MODEL_CAP=32` 约束），长期运行进程内存缓慢膨胀 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测
```

### Step 2：验证（doc-sync：条目计数 + 已删行不存在）

```
$ grep -c "^- " docs/superpowers/followups.md
1
$ ! grep -q "仅 prune 锁内浅拷贝副本" docs/superpowers/followups.md && echo LINE-4-DELETED
LINE-4-DELETED
```

### Step 3：commit

```
$ git add docs/superpowers/followups.md
$ git commit -m "chore(docs): 删除已消化的 daily 内存态 prune followup"
```

### 验收

- [ ] followups.md 仅剩 1 条 entry（daily_by_model 历史条目原样保留）
- [ ] 不改其他文件（Task 2 触碰面 = followups.md 单文件）
