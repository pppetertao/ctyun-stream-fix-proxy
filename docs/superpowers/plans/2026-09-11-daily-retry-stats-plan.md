# PLAN — ctyun-proxy 按天统计「重试」列 + 按天×模型副表（v2）

## Header

- **Spec**: `docs/superpowers/specs/2026-09-11-daily-retry-stats-design.md`（已 commit 于 472bfdd；锚点行号与当前 worktree 文件逐一实测核对，**无漂移**，无需 CHANGED 段）
- **分支/工作树**: `fix/daily-retry-stats` @ `.worktrees/daily-retry-stats/`
- **改动文件（仅两个，其他零触碰）**:
  - `ctyun-stream-fix-proxy.py`
  - `ctyun-stream-fix-proxy.test.py`
- **基线（2026-09-11 实测）**: `cd .worktrees/daily-retry-stats && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 41 tests in 20.233s / OK / exit 0`；两文件 `py_compile` exit 0
- **PLAN 自验（2026-09-11 /tmp 沙箱 dry-run）**: 见文末「PLAN 自验记录」——全部 17 组 OLD/NEW 机械抽取应用（OLD 唯一命中）→ Task 1 red 精确 4 errors → green 42 OK → Task 2 red 精确 1 failure → green 42 OK + py_compile exit 0 + 冒烟脚本 SMOKE OK
- **卡数/tier**: 3 卡 = Task 1 **[tier A]** 后端计数链 / Task 2 **[tier A]** 前端双表 / Task 3 **[tier B]** 部署同步+冒烟；全部 A/B → executor；**无 C 卡**（全部基于本地 fake upstream / scratch 实例，无真机、运行时生产数据、DI 集成边界留白）
- **TDD 结构说明**: prompt 建议按「后端 / 前端 / 测试+部署」分卡；为满足 TDD 铁律（每卡先红后绿），测试断言分别并入对应实现卡（Task 1 含 4 处测试编辑、Task 2 含 1 处），Task 3 无新代码（纯部署同步 + 冒烟验证）
- **部署边界**: `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy` 重启生效 = **主代理 DELIVER 职责，不进任何卡**；卡内只做复制 + `cmp` + scratch 实例冒烟（不动 launchd 服务、不动真实上游）

## Global Constraints

1. 解释器固定 `/usr/bin/python3`（3.9.6 实测）；纯 stdlib，不加依赖；禁 3.10+ 语法（无 `match/case`、无 PEP 604 `X | Y` 类型联合、无括号化 context managers）。
2. 前端 ES5：`var`/`function`，无箭头函数/`let`/`const`/模板字面量；动态数据一律 `textContent`（禁 `innerHTML`）。
3. **持久化零变更**：`save_stats_counters`/`load_stats_counters`/`load_daily_buckets`/`_DAILY_FIELDS` 不碰；`daily_by_model` 仅内存，重启清零（与 `by_model` 语义一致）。
4. **cap**：`daily_by_model` 每日独立 cap = `BY_MODEL_CAP`(32)，`len(day_models) < BY_MODEL_CAP` 才新建（与 :291/:329 同构），防膨胀。
5. **fallback 恒等式**：model=None（非 JSON body / body 无 model / 499 中断 :391-392）只进 daily 总桶、不进 `daily_by_model`（`if model:` 与 :288/:326 同构）；对 k∈{requests,filtered,retries}：`daily[d][k] ≥ Σ_m daily_by_model[d][m][k]`，差值 = 该日无 model 请求；前端**不造「未知」键**。
6. 所有命令 cwd = worktree 根：`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats`。
7. **No Placeholders**：每处 Edit 给出唯一命中的 OLD 块 + 完整 NEW 块，用 Edit 工具逐字转写（含缩进与注释）。OLD 定位失败或不唯一 → **STOP 报主代理**，不得自行适配。
8. **红/绿清单精确**（各卡验证节写死）；红/绿漂移出清单 → STOP。claim 完成前贴实际命令 stdout + exit code。
9. **语义零变更区**（不得顺手改）：`_relay_sse`/`_relay_buffered`/`_open_upstream`/`_reply_502`/499 路径/毒行判定/admin 鉴权/`renderRecent`/`renderSpark`/`renderModelChips`。
10. Conventional Commits；commit 前自检：分支 = `fix/daily-retry-stats`、`git config user.name/user.email` 非 agent placeholder、`git status` 只含预期文件。

---

## Task 1 [tier A] — 后端计数链 + snapshot 深拷贝（TDD red→green）

**文件**: 先 `ctyun-stream-fix-proxy.test.py`（red），后 `ctyun-stream-fix-proxy.py`（green）。

### 1.1 baseline 登记

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 41 tests in xx.xxx s

OK
Exit code: 0
```

非 41 绿 → 基线漂移，STOP 报主代理。

### 1.2 Edit T1 — 新用例 `test_daily_by_model_matrix_fallback`（`test_daily_bucket_spans_days` 末尾后，锚点 :531-533）

OLD:
```python
        self.assertIn("2026-01-02", mod.STATS["daily"])
        self.assertIn(today, mod.STATS["daily"])
        self.assertIsNot(mod.STATS["daily"]["2026-01-02"], mod.STATS["daily"][today])
```

NEW:
```python
        self.assertIn("2026-01-02", mod.STATS["daily"])
        self.assertIn(today, mod.STATS["daily"])
        self.assertIsNot(mod.STATS["daily"]["2026-01-02"], mod.STATS["daily"][today])

    def test_daily_by_model_matrix_fallback(self) -> None:
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/dm", 200, 1.0, 1, model="m-a")
        before = set(mod.STATS["daily_by_model"].get(today, {}))
        mod._record_request("POST", "/dm", 200, 1.0, 0)  # model=None：只进 daily 总桶
        self.assertEqual(set(mod.STATS["daily_by_model"].get(today, {})), before,
                         "model=None requests must not enter daily_by_model")
        mod._record_empty_retry("m-a")
        dm_today = mod.STATS["daily_by_model"][today]
        self.assertGreaterEqual(dm_today["m-a"]["requests"], 1)
        self.assertGreaterEqual(dm_today["m-a"]["filtered"], 1)
        self.assertGreaterEqual(dm_today["m-a"]["retries"], 1,
                                "retry attribution must land in daily_by_model")
        # 恒等式：daily 总桶 ≥ 分模型合计（差值 = 当日无 model 请求）
        for k in ("requests", "retries"):
            total = mod.STATS["daily"][today][k]
            summed = sum(m[k] for m in dm_today.values())
            self.assertGreaterEqual(total, summed,
                                    "daily[%r][%r]=%d < Σ daily_by_model=%d"
                                    % (today, k, total, summed))
```

### 1.3 Edit T2 — cap 用例追加 daily_by_model 断言（锚点 :474-481）

OLD:
```python
    def test_by_model_accumulation_and_cap(self) -> None:
        mod = self.mod
        mod._record_request("POST", "/c", 200, 1.0, 1, model="m-a")
        self.assertGreaterEqual(mod.STATS["by_model"]["m-a"]["requests"], 1)
        for i in range(40):
            mod._record_request("POST", "/c", 200, 1.0, 0, model="m-%02d" % i)
        self.assertLessEqual(len(mod.STATS["by_model"]), 32)
        self.assertNotIn("m-39", mod.STATS["by_model"])
```

NEW:
```python
    def test_by_model_accumulation_and_cap(self) -> None:
        mod = self.mod
        mod._record_request("POST", "/c", 200, 1.0, 1, model="m-a")
        self.assertGreaterEqual(mod.STATS["by_model"]["m-a"]["requests"], 1)
        for i in range(40):
            mod._record_request("POST", "/c", 200, 1.0, 0, model="m-%02d" % i)
        self.assertLessEqual(len(mod.STATS["by_model"]), 32)
        self.assertNotIn("m-39", mod.STATS["by_model"])
        # v2：daily_by_model 每日独立 cap（口径与 by_model 全局 cap 略异，见 spec Risks）
        self.assertLessEqual(len(mod.STATS["daily_by_model"][mod.today_key()]), 32)
        self.assertNotIn("m-39", mod.STATS["daily_by_model"][mod.today_key()])
```

### 1.4 Edit T3 — snapshot 拷贝用例追加 daily_by_model 双层断言（锚点 :578-585）

OLD:
```python
    def test_stats_snapshot_daily_is_copy(self) -> None:
        mod = self.mod
        snap = mod.stats_snapshot()
        self.assertIn("daily", snap)
        snap["daily"]["mutation-test"] = {"requests": 1, "filtered": 0,
                                          "errors_proxy": 0, "errors_upstream": 0}
        self.assertNotIn("mutation-test", mod.STATS["daily"],
                         "snapshot must hand out copies, not internal refs")
```

NEW:
```python
    def test_stats_snapshot_daily_is_copy(self) -> None:
        mod = self.mod
        snap = mod.stats_snapshot()
        self.assertIn("daily", snap)
        snap["daily"]["mutation-test"] = {"requests": 1, "filtered": 0,
                                          "errors_proxy": 0, "errors_upstream": 0}
        self.assertNotIn("mutation-test", mod.STATS["daily"],
                         "snapshot must hand out copies, not internal refs")
        # v2：daily_by_model 双层深拷贝（外层日期 dict + 内层模型 entry）
        mod.STATS["daily_by_model"].setdefault(mod.today_key(), {})["snap-m"] = {
            "requests": 1, "filtered": 0, "retries": 0}
        snap = mod.stats_snapshot()
        self.assertIn("daily_by_model", snap)
        snap["daily_by_model"]["mutation-test"] = {}
        self.assertNotIn("mutation-test", mod.STATS["daily_by_model"])
        snap["daily_by_model"][mod.today_key()]["snap-m"]["requests"] = 999
        self.assertEqual(
            mod.STATS["daily_by_model"][mod.today_key()]["snap-m"]["requests"], 1,
            "inner model entries must be copies, not internal refs")
```

### 1.5 Edit T4 — 集成断言并入 `test_recent_entries_carry_model`（锚点 :771-779）

OLD:
```python
    def test_recent_entries_carry_model(self) -> None:
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertTrue(snap["recent"])
        self.assertEqual(snap["recent"][-1]["model"], "deepseek-v4-pro-0813-oc")
        self.assertIn("deepseek-v4-pro-0813-oc", snap["by_model"])
        self.assertGreaterEqual(
            snap["by_model"]["deepseek-v4-pro-0813-oc"]["requests"], 1)
```

NEW:
```python
    def test_recent_entries_carry_model(self) -> None:
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertTrue(snap["recent"])
        self.assertEqual(snap["recent"][-1]["model"], "deepseek-v4-pro-0813-oc")
        self.assertIn("deepseek-v4-pro-0813-oc", snap["by_model"])
        self.assertGreaterEqual(
            snap["by_model"]["deepseek-v4-pro-0813-oc"]["requests"], 1)
        # v2：daily_by_model 集成（/api/stats 顶层键透出；retries 归因见单测 matrix_fallback）
        today = time.strftime("%Y-%m-%d")
        self.assertIn(today, snap["daily_by_model"])
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["requests"], 1,
            "per-model daily matrix must record the SSE request")
```

### 1.6 验证（red）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

FAILED (errors=4)
Exit code: 1
```

**精确红清单（4 红 = 4 errors，其余 38 绿）**:

| # | 用例（执行序） | 红法 |
|---|------|------|
| 1 | `AdminIntegrationTest.test_recent_entries_carry_model` | ERROR: KeyError 'daily_by_model'（/api/stats JSON 缺顶层键） |
| 2 | `ProxyDashboardUnitTest.test_by_model_accumulation_and_cap` | ERROR: KeyError 'daily_by_model'（新增 cap 断言行） |
| 3 | `ProxyDashboardUnitTest.test_daily_by_model_matrix_fallback` | ERROR: KeyError 'daily_by_model'（新用例） |
| 4 | `ProxyDashboardUnitTest.test_stats_snapshot_daily_is_copy` | ERROR: KeyError 'daily_by_model'（新增 seed 行） |

红名单外任何红/绿变化 = 锚点漂移 → STOP。

```
$ /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.test.py
Exit code: 0
```

### 1.7 Edit P1 — `STATS` init 加 `daily_by_model`（锚点 :99-101）

OLD:
```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0,
         "active": 0, "by_model": {}, "daily": {}}
```

NEW:
```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0,
         "active": 0, "by_model": {}, "daily": {}, "daily_by_model": {}}
```

### 1.8 Edit P2 — `_record_request` 分模型日桶自增（锚点 :288-295，`if model:` 块内）

OLD:
```python
        if model:
            by_model = STATS["by_model"]
            entry = by_model.get(model)
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry is not None:  # 键数达上限后新模型不记录，防内存膨胀
                entry["requests"] += 1
                entry["filtered"] += filtered
```

NEW:
```python
        if model:
            by_model = STATS["by_model"]
            entry = by_model.get(model)
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry is not None:  # 键数达上限后新模型不记录，防内存膨胀
                entry["requests"] += 1
                entry["filtered"] += filtered
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["requests"] += 1
                entry_dm["filtered"] += filtered
```

### 1.9 Edit P3 — `_record_empty_retry` docstring 补 daily_by_model（锚点 :320-322）

OLD:
```python
    """空流重试计数：STATS 总量 + by_model retries 维度 + 当日桶。
    entry/桶形状必须与 _record_request 同步含 retries 键（旧持久化桶经
    load_daily_buckets 的 _DAILY_FIELDS 清洗已补键），否则 += 直接 KeyError。"""
```

NEW:
```python
    """空流重试计数：STATS 总量 + by_model retries 维度 + 当日桶 + daily_by_model。
    entry/桶形状必须与 _record_request 同步含 retries 键（旧持久化桶经
    load_daily_buckets 的 _DAILY_FIELDS 清洗已补键），否则 += 直接 KeyError。"""
```

### 1.10 Edit P4 — `_record_empty_retry` 分模型日桶 retries 自增（锚点 :326-332，`if model:` 块内）

OLD:
```python
        if model:
            by_model = STATS["by_model"]
            entry = by_model.get(model)
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry is not None:  # 键数达上限后新模型不记录，防内存膨胀
                entry["retries"] += 1
```

NEW:
```python
        if model:
            by_model = STATS["by_model"]
            entry = by_model.get(model)
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry is not None:  # 键数达上限后新模型不记录，防内存膨胀
                entry["retries"] += 1
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["retries"] += 1
```

（注意：P2 与 P4 的 OLD 前 5 行相同，区分在末行——P2 以 `entry["filtered"] += filtered` 结尾（`_record_request`），P4 以 `entry["retries"] += 1` 结尾（`_record_empty_retry`）；两块各自全文唯一。）

### 1.11 Edit P5 — `stats_snapshot` 双层深拷贝（锚点 :342-344）

OLD:
```python
        snap = dict(STATS)
        snap["by_model"] = {k: dict(v) for k, v in STATS["by_model"].items()}
        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
```

NEW:
```python
        snap = dict(STATS)
        snap["by_model"] = {k: dict(v) for k, v in STATS["by_model"].items()}
        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
        snap["daily_by_model"] = {
            d: {m: dict(v) for m, v in models.items()}
            for d, models in STATS["daily_by_model"].items()}
```

### 1.12 验证（green）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
Exit code: 0
```

```
$ /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
Exit code: 0
```

任何用例红 → 贴原文诊断，最多修 3 轮；仍红 → STOP 报主代理。

### 1.13 commit（两条，顺序固定）

```
$ git status   # 自检：只含 ctyun-stream-fix-proxy.py 与 ctyun-stream-fix-proxy.test.py 两个改动文件
$ git add ctyun-stream-fix-proxy.test.py
$ git commit -m "test(proxy): daily_by_model 矩阵/cap/快照拷贝用例 + 集成断言"
$ git add ctyun-stream-fix-proxy.py
$ git commit -m "feat(proxy): 按天×模型计数链（daily_by_model 内存矩阵 + snapshot 双层深拷贝）"
```

注：第一条 commit 单独检出时是 TDD red 中间态（新测试 + 旧实现），预期；**分支 tip 必须 42 绿**（1.12 验证后才 commit）。commit 前自检：分支 = `fix/daily-retry-stats`，author 非 agent placeholder。

---

## Task 2 [tier A] — 前端双表：主表重试列 + 按天×模型副表（TDD red→green）

**文件**: 先 `ctyun-stream-fix-proxy.test.py`（red），后 `ctyun-stream-fix-proxy.py`（green）。起始态 = Task 1 绿（42 OK，2 commits）。

### 2.1 Edit T5 — dashboard 断言并入 `test_dashboard_html_full_page`（锚点 :752-755）

OLD:
```python
        # v1.2：按天统计卡（日期表 + 双口径错误列）
        self.assertIn("按天统计", html)
        self.assertIn("<th>上游5xx</th>", html)
        self.assertIn('id="daily-body"', html)
```

NEW:
```python
        # v1.2：按天统计卡（日期表 + 双口径错误列）
        self.assertIn("按天统计", html)
        self.assertIn("<th>上游5xx</th>", html)
        self.assertIn('id="daily-body"', html)
        # v2：按天表「重试」列（6 列）+ 按天×模型副表
        self.assertIn("<th>重试</th>", html)
        self.assertIn('colspan="6"', html)
        self.assertIn('id="daily-model-body"', html)
```

### 2.2 验证（red）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

FAILED (failures=1)
Exit code: 1
```

**精确红清单（1 红 = 1 failure，其余 41 绿）**:

| # | 用例 | 红法 |
|---|------|------|
| 1 | `AdminIntegrationTest.test_dashboard_html_full_page` | FAIL: `'<th>重试</th>' not found in html`（首条新断言即挂，后续断言未达） |

红名单外任何红/绿变化 = 锚点漂移 → STOP。

### 2.3 Edit F1 — thead 补「重试」列 + colspan 5→6（锚点 :742-743）

OLD:
```html
      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th></tr></thead>
      <tbody id="daily-body"><tr><td class="empty" colspan="5">读取中……</td></tr></tbody>
```

NEW:
```html
      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-body"><tr><td class="empty" colspan="6">读取中……</td></tr></tbody>
```

### 2.4 Edit F2 — 按天×模型副表 section（「按天统计」卡后插入，锚点 :744-748）

OLD:
```html
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">剥行流带 · 最近剥除的毒 record</div>
```

NEW:
```html
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">按天 × 模型（内存累计，重启清零）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>请求</th><th>剥行</th><th>重试</th></tr></thead>
      <tbody id="daily-model-body"><tr><td class="empty" colspan="5">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">剥行流带 · 最近剥除的毒 record</div>
```

### 2.5 Edit F3 — footer 补两句口径（锚点 :762-764）

OLD:
```html
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json）；
  按模型计数自进程启动累计，不持久化（重启清零）；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（与顶部错误数同口径），
```

NEW:
```html
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json）；
  按模型与按天×模型计数自进程启动累计，不持久化（重启清零）；
  按天主表含无 model 请求，各行数值 ≥「按天 × 模型」副表合计，差值即当日无 model 请求；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（与顶部错误数同口径），
```

### 2.6 Edit F4 — `renderDaily` 空态 colSpan 5→6（锚点 :893-896）

OLD:
```javascript
  if (keys.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天统计");
    td0.colSpan = 5;
```

NEW:
```javascript
  if (keys.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天统计");
    td0.colSpan = 6;
```

### 2.7 Edit F5 — `renderDaily` 行尾补重试 td（锚点 :903-909）

OLD:
```javascript
    var tr = el("tr", (b.errors_proxy + b.errors_upstream) > 0 ? "hit" : "");
    tr.appendChild(el("td", "num", keys[i]));
    tr.appendChild(el("td", "num", String(b.requests)));
    tr.appendChild(el("td", "num", String(b.filtered)));
    tr.appendChild(el("td", "num", String(b.errors_proxy)));
    tr.appendChild(el("td", "num", String(b.errors_upstream)));
    body.appendChild(tr);
```

NEW:
```javascript
    var tr = el("tr", (b.errors_proxy + b.errors_upstream) > 0 ? "hit" : "");
    tr.appendChild(el("td", "num", keys[i]));
    tr.appendChild(el("td", "num", String(b.requests)));
    tr.appendChild(el("td", "num", String(b.filtered)));
    tr.appendChild(el("td", "num", String(b.errors_proxy)));
    tr.appendChild(el("td", "num", String(b.errors_upstream)));
    tr.appendChild(el("td", "num", String(b.retries || 0)));
    body.appendChild(tr);
```

### 2.8 Edit F6 — 新函数 `renderDailyByModel`（`renderDaily` 后、`renderSpark` 前插入，锚点 :909-912；**须在 F5 之后应用**）

OLD:
```javascript
    body.appendChild(tr);
  }
}
function renderSpark(recent) {
```

NEW:
```javascript
    body.appendChild(tr);
  }
}
function renderDailyByModel(dbm) {
  var body = $("daily-model-body");
  body.textContent = "";
  var days = Object.keys(dbm).sort().reverse().slice(0, 14);
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天 × 模型统计");
    td0.colSpan = 5;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    return;
  }
  for (var i = 0; i < days.length; i++) {
    var models = dbm[days[i]];
    var names = Object.keys(models);
    names.sort(function (a, b) {
      return (models[b].requests || 0) - (models[a].requests || 0);
    });
    for (var j = 0; j < names.length; j++) {
      var ent = models[names[j]];
      var tr = el("tr", (ent.retries || 0) > 0 ? "hit" : "");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(ent.requests || 0)));
      tr.appendChild(el("td", "num", String(ent.filtered || 0)));
      tr.appendChild(el("td", "num", String(ent.retries || 0)));
      body.appendChild(tr);
    }
  }
}
function renderSpark(recent) {
```

### 2.9 Edit F7 — `poll` 追加副表渲染（锚点 :972-973）

OLD:
```javascript
      renderDaily(snap.daily || {});
      renderSpark(snap.recent || []);
```

NEW:
```javascript
      renderDaily(snap.daily || {});
      renderDailyByModel(snap.daily_by_model || {});
      renderSpark(snap.recent || []);
```

### 2.10 验证（green）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
Exit code: 0
```

```
$ /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
Exit code: 0
```

任何用例红 → 贴原文诊断，最多修 3 轮；仍红 → STOP 报主代理。

### 2.11 commit（两条，顺序固定）

```
$ git status   # 自检：只含两个改动文件
$ git add ctyun-stream-fix-proxy.test.py
$ git commit -m "test(dashboard): 按天表重试列与按天×模型副表断言"
$ git add ctyun-stream-fix-proxy.py
$ git commit -m "feat(dashboard): 按天表补重试列 + 按天×模型副表 + footer 口径"
```

---

## Task 3 [tier B] — 部署副本同步 + 冒烟验证（无新代码，无 commit）

**起始态** = Task 2 绿（42 OK，4 commits on top of 472bfdd）。本卡不改仓库内任何文件 → **无 commit**。

### 3.1 全量回归（worktree 内）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats && /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py && echo PY_COMPILE_OK
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
Exit code: 0
```

```
$ git status   # 应干净（4 commits 已落）
$ git log --oneline -6
```

### 3.2 部署副本复制 + cmp 双向

```
$ cp ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py
$ cp ctyun-stream-fix-proxy.test.py ~/.local/bin/ctyun-stream-fix-proxy.test.py
$ cmp ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py && cmp ~/.local/bin/ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.py && echo CMP_PY_OK
$ cmp ctyun-stream-fix-proxy.test.py ~/.local/bin/ctyun-stream-fix-proxy.test.py && cmp ~/.local/bin/ctyun-stream-fix-proxy.test.py ctyun-stream-fix-proxy.test.py && echo CMP_TEST_OK
CMP_PY_OK
CMP_TEST_OK
Exit code: 0
```

（cmp 字节级对称，双向各跑一次按 spec ④ 字面执行；任一非 0 → 复制失败，STOP。）

### 3.3 冒烟：部署副本 + scripted 上游 + curl 断言（spec ③）

完整脚本逐字执行（scratch 实例走 env seam，不碰 launchd 服务与真实上游；`curl` 拉 `/api/stats` 与 dashboard）：

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-retry-stats && /usr/bin/python3 - <<'EOF'
import http.client, json, os, shutil, socket, subprocess, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer

SSE_A = b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
SSE_DONE = b"data: [DONE]\n\n"
SSE_REASONING = b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}\n\n'


class SmokeUpstream(BaseHTTPRequestHandler):
    calls = 0

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            self.rfile.read(length)
        SmokeUpstream.calls += 1
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        if SmokeUpstream.calls == 1:
            self.wfile.write(SSE_REASONING)  # 空流签名 → 代理触发一次重试
        else:
            self.wfile.write(SSE_A + SSE_DONE)
        self.close_connection = True

    def do_GET(self):
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_port(port):
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            return
        except OSError:
            time.sleep(0.05)
    raise SystemExit("port %d not listening" % port)


upstream = HTTPServer(("127.0.0.1", 0), SmokeUpstream)
threading.Thread(target=upstream.serve_forever, daemon=True).start()
tmp = tempfile.mkdtemp(prefix="ctyun-smoke-")
proxy_port, admin_port = free_port(), free_port()
env = dict(os.environ,
           CTYUN_UPSTREAM_BASE="http://127.0.0.1:%d" % upstream.server_address[1],
           CTYUN_LISTEN_PORT=str(proxy_port),
           CTYUN_ADMIN_HOST="127.0.0.1",
           CTYUN_ADMIN_PORT=str(admin_port),
           CTYUN_PERSIST_PATH=os.path.join(tmp, "settings.json"))
proc = subprocess.Popen(
    ["/usr/bin/python3", os.path.expanduser("~/.local/bin/ctyun-stream-fix-proxy.py")],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    wait_port(proxy_port)
    wait_port(admin_port)
    # ① 带 model 请求（上游首呼空流 → 透明重试一次）
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    conn.request("POST", "/v1/chat/completions",
                 body=b'{"model":"smoke-model","stream":true,"messages":[]}',
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    assert resp.status == 200 and data == SSE_A + SSE_DONE, (resp.status, data)
    assert SmokeUpstream.calls == 2, SmokeUpstream.calls
    # ② 无 model 请求（GET 无 body → extract_model None，只进 daily 总桶）
    conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=10)
    conn.request("GET", "/plain")
    resp = conn.getresponse()
    resp.read()
    conn.close()
    assert resp.status == 200
    # ③ curl /api/stats：daily_by_model + 恒等式（fresh 实例，数值确定）
    snap = json.loads(subprocess.run(
        ["curl", "-s", "http://127.0.0.1:%d/api/stats" % admin_port],
        capture_output=True, check=True).stdout)
    today = time.strftime("%Y-%m-%d")
    dm = snap["daily_by_model"]
    assert today in dm and "smoke-model" in dm[today], dm
    assert dm[today]["smoke-model"]["retries"] >= 1, dm[today]
    assert dm[today]["smoke-model"]["requests"] == 1, dm[today]
    sum_req = sum(m["requests"] for m in dm[today].values())
    no_model = snap["daily"][today]["requests"] - sum_req
    assert no_model == 1, (snap["daily"][today], dm[today])
    assert snap["daily"][today]["retries"] >= sum(
        m["retries"] for m in dm[today].values())
    # ④ curl dashboard：主表 6 列 + 副表容器 + JS 接线
    html = subprocess.run(
        ["curl", "-s", "http://127.0.0.1:%d/" % admin_port],
        capture_output=True, check=True).stdout.decode("utf-8")
    assert "<th>上游5xx</th><th>重试</th></tr></thead>" in html
    assert 'id="daily-body"><tr><td class="empty" colspan="6">' in html
    assert "按天 × 模型" in html and 'id="daily-model-body"' in html
    assert "renderDailyByModel" in html
    proc.terminate()
    proc.wait(timeout=5)
    # ⑤ 持久化零变更：SIGTERM 落盘文件不含 daily_by_model（内存矩阵重启清零）
    with open(os.path.join(tmp, "settings.json"), encoding="utf-8") as fh:
        saved = json.load(fh)
    assert "daily_by_model" not in saved["stats"], sorted(saved["stats"])
    assert today in saved["stats"]["daily"], sorted(saved["stats"]["daily"])
    print("SMOKE OK: dm=%s no_model_diff=%d daily=%s"
          % (dm[today], no_model, saved["stats"]["daily"][today]))
finally:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    upstream.shutdown()
    upstream.server_close()
    shutil.rmtree(tmp, ignore_errors=True)
EOF
SMOKE OK: dm={'smoke-model': {'requests': 1, 'filtered': 0, 'retries': 1}} no_model_diff=1 daily={'requests': 2, 'filtered': 0, 'errors_proxy': 0, 'errors_upstream': 0, 'retries': 1}
Exit code: 0
```

**冒烟断言要点（curl 侧）**：
- `/api/stats` 顶层 `daily_by_model[today]["smoke-model"]`：`retries >= 1`（空流重试归因）、`requests == 1`（重试不双计）。
- 恒等式：`daily[today]["requests"] == Σ_m daily_by_model[today][m]["requests"] + 1`（差值 1 = 无 model 的 GET /plain）。
- dashboard HTML：`<th>上游5xx</th><th>重试</th></tr></thead>`（主表 6 列收尾）、`colspan="6"`、`id="daily-model-body"`、`renderDailyByModel` 接线存在。
- 落盘文件：`stats` 无 `daily_by_model` 键（持久化零变更）。

任一断言挂 → 贴 AssertionError 原文，STOP 报主代理（不得改脚本迁就）。

### 3.4 边界说明（不进本卡）

- `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy` 重启 launchd 服务 = **主代理 DELIVER**（spec Files §3 明示）。
- spec ③ 的「主表 6 td/行、副表每活跃模型一行且数值与 JSON 一致」DOM 渲染目检 = 部署重启后浏览器确认（主代理/用户；同 v1.3 fmtDate/date-row 目检惯例）。本卡 ④ 已锁 HTML 结构 + JS 接线 + JSON 数值。

---

## Spec 覆盖对照

| spec 条目（Files to Change） | 落点 |
|---|---|
| 1. `:99-101` STATS init 加 `daily_by_model` | Task 1 P1 |
| 1. `_record_request` :288 块内分模型日桶自增 | Task 1 P2 |
| 1. `_record_empty_retry` docstring + :326 块内 retries 自增 | Task 1 P3, P4 |
| 1. `stats_snapshot` :344 后双层深拷贝 | Task 1 P5 |
| 1. thead :742 重试列 + :743 colspan 6 | Task 2 F1 |
| 1. 副表 section（card-title + table-wrap + daily-model-body） | Task 2 F2 |
| 1. footer :762-765 补两句 | Task 2 F3 |
| 1. renderDaily :896 colSpan / :908 后 retries td | Task 2 F4, F5 |
| 1. 新函数 renderDailyByModel（:888 前后） | Task 2 F6 |
| 1. poll :972 后追加 | Task 2 F7 |
| 2. 新用例 test_daily_by_model_matrix_fallback（:522 后） | Task 1 T1 |
| 2. cap 并入 test_by_model_accumulation_and_cap（:474-481） | Task 1 T2 |
| 2. snapshot 并入 test_stats_snapshot_daily_is_copy（:578-585） | Task 1 T3 |
| 2. dashboard 断言（:752-755） | Task 2 T5 |
| 2. 集成并入 :777 附近 SSE 用例 | Task 1 T4 |
| 3. 部署副本复制 + cmp 双向 | Task 3 3.2 |
| 3. launchctl kickstart 重启生效 | 不进卡（主代理 DELIVER） |
| 验收 ① py_compile / ② 全量绿（41+1）贴末尾 | Task 1/2/3 各验证节 |
| 验收 ③ 冒烟 curl 断言 | Task 3 3.3 |
| 验收 ④ cmp exit 0 | Task 3 3.2 |

## Spec 偏差记录（PLAN-B 裁定，生产行为零偏差）

1. **dashboard 断言落点**：spec 测试条目 4 写「并入 `test_dashboard_and_stats_served` :752-755」，但 :752-755 实际位于 `test_dashboard_html_full_page`（v1.2 按天断言块；`test_dashboard_and_stats_served` 在 :670-693）。按行号锚点 + 既有断言块惯例落 `test_dashboard_html_full_page`；`test_dashboard_and_stats_served` 的 assertIn 键循环天然兼容新顶层键，不改。
2. **cap 用例加一条 `assertNotIn("m-39", daily_by_model[today])`**：spec 只要求 `assertLessEqual(..., 32)`；补同模式断言证明 cap 真拒绝了第 33+ 个模型键而非字典碰巧小（与相邻 by_model 断言完全同构），口径不变。
3. **纯文字层面**（语义零变更）：`stats_snapshot` 深拷贝 spec 一行式拆为 3 行字面量（控行宽，拼接后语义等价）；`_record_empty_retry` docstring 首行补 `+ daily_by_model`（spec 明示）。
4. **卡结构**：prompt 建议「后端 / 前端 / 测试+部署」三卡；为满足 TDD 铁律（每卡先红后绿），测试断言随实现卡分布（Task 1 四处、Task 2 一处），Task 3 = 部署同步 + 冒烟（无新代码）。测试内容与 spec 五条一一对应，无增删。
5. **冒烟 ③ DOM 目检项**：「主表 6 td/行、副表每活跃模型一行且数值与 JSON 一致」为浏览器渲染目检；冒烟卡覆盖 HTML 结构 + JS 接线 + JSON 数值/恒等式（同 v1.3 fmtDate/date-row 目检兜底惯例），目检由部署后主代理/用户执行。

## PLAN 自验记录（2026-09-11 /tmp 沙箱 dry-run 实测）

- 17 组 OLD/NEW（T1-T5 / P1-P5 / F1-F7）机械抽取自本 PLAN.md 并依序应用：每组 OLD 在目标文件唯一命中（含 P2/P4 共享前缀的区分验证）。
- Task 1 red：`Ran 42 tests / FAILED (errors=4)`，红名单与 1.6 表逐条一致（4 个 KeyError: 'daily_by_model'）。
- Task 1 green：`Ran 42 tests / OK / exit 0`。
- Task 2 red：`Ran 42 tests / FAILED (failures=1)`，红名单与 2.2 表一致（test_dashboard_html_full_page）。
- Task 2 green：`Ran 42 tests / OK / exit 0` + 两文件 `py_compile` exit 0。
- 冒烟脚本（部署路径指向沙箱副本）：`SMOKE OK: dm={'smoke-model': {'requests': 1, 'filtered': 0, 'retries': 1}} no_model_diff=1 daily={'requests': 2, 'filtered': 0, 'errors_proxy': 0, 'errors_upstream': 0, 'retries': 1}`，exit 0。
