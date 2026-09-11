# PLAN — 按天×模型表与按天统计对齐（14 天 / 新在上 / 请求·剥行·代理错误·上游5xx·重试）

## Header

- **Spec**: `docs/superpowers/specs/2026-09-11-daily-by-model-14d-align-design.md`（commit e276da6；spec 全部锚点行号已在当前 worktree 文件逐一实测核对，**无漂移**，无需 CHANGED 段）
- **分支/工作树**: `fix/daily-by-model-14d-align` @ `.worktrees/daily-by-model-14d-align/`
- **改动文件（仅两个，其他零触碰）**:
  - `ctyun-stream-fix-proxy.py`
  - `ctyun-stream-fix-proxy.test.py`
- **基线（2026-09-11 实测）**: `cd .worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 42 tests in 20.275s / OK / exit 0`
- **PLAN 自验（2026-09-11 /tmp 沙箱 dry-run）**: 见附录——全部 10 组 OLD/NEW 机械抽取应用（OLD 唯一命中）→ 三轮 red 均精确 1 failure 且方法名吻合 → 各 green 42 OK → 终态 42 OK + py_compile exit 0
- **卡数/tier**: 4 卡 = Task 1 **[tier A]** 后端 `_record_request` / Task 2 **[tier A]** 后端 `_record_empty_retry` / Task 3 **[tier A]** 前端表头+渲染 / Task 4 **[tier B]** 集成断言+全量回归；全部 A/B → executor；**无 C 卡**（全部验证基于 in-process 模块加载 + scratch 子进程 + 本地 fake upstream，无真机、无运行时生产数据、无 DI/集成边界留白）
- **TDD 结构说明**: 为满足 TDD 铁律（先红后绿），Task 1/2/3 的测试断言分别并入对应实现卡（先 Edit 测试 → 跑出精确红 → Edit 生产 → 绿）；Task 4 为纯测试追加（行为已由 Task 1-3 落地、红先义务已在各卡单测履行，预期直接绿）+ 全量回归收尾。**不新增测试方法**（spec 要求追加断言到既有用例），全程测试数恒为 42。
- **部署边界**: spec Files 仅列两个源文件、未含部署步骤 → 本 plan 无部署卡；安装副本同步与服务重启归主代理 DELIVER 阶段处置，卡内禁止触碰。

## Global Constraints

1. 解释器固定 `/usr/bin/python3`（3.9.6 实测）；纯 stdlib，不加依赖；禁 3.10+ 语法（无 `match/case`、无 PEP 604 `X | Y`、无括号化 context managers）。
2. 前端 ES5：`var`/`function`，无箭头函数/`let`/`const`/模板字面量；动态数据一律 `textContent`（禁 `innerHTML`）。
3. **持久化零变更**：`save_stats_counters`/`load_stats_counters`/`load_daily_buckets`/`_DAILY_FIELDS`/`DAILY_RETENTION_DAYS`/`_prune_daily` 不碰；`daily_by_model` 仍纯内存、重启清零；`stats_snapshot`（:353-366）双层深拷贝零改动，新字段自动透出。
4. **`by_model` entry 形状不变**（仍 `{"requests","filtered","retries"}` 3 字段，:292/:337 两处）——本任务只动 **dm entry**（`daily_by_model` 内层）为 5 字段。
5. **cap 语义不变**：`daily_by_model` 每日独立 cap = `BY_MODEL_CAP`(32)，`len(day_models) < BY_MODEL_CAP` 才新建；cap 拒绝后 `entry_dm is None` → 不累加也不归因（与 requests/filtered 同守卫）。
6. **No Placeholders**：每处 Edit 给出唯一命中的 OLD 块 + 完整 NEW 块，逐字转写（含缩进与注释）。OLD 定位失败或不唯一 → **STOP 报主代理**，不得自行适配。
7. **红/绿清单精确**（各卡验证节写死）；红/绿漂移出清单 → STOP。claim 完成前贴实际命令 stdout + exit code。
8. **语义零变更区**（不得顺手改）：499/502 记账调用点（:406-408、:433-436、:454-456）、`renderDaily`（:915-938）、`renderRecent`/`renderSpark`/`renderModelChips`、footer 文案（:786-792）、`/api/stats` 既有键、按模型 chips 卡、最近请求表。
9. 所有命令 cwd = worktree 根：`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align`。
10. Conventional Commits；commit 前自检：分支 = `fix/daily-by-model-14d-align`、`git config user.name/user.email` 非 agent placeholder、`git status` 只含预期文件。

---

## Task 1 [tier A] — 后端 `_record_request`：dm entry 5 字段 + 错误归因（TDD red→green）

**文件**: 先 `ctyun-stream-fix-proxy.test.py`（red），后 `ctyun-stream-fix-proxy.py`（green）。

### 1.0 锚点与边界（已实测）

- dm entry 创建点 site-1 在 `_record_request` py:298-299，累加在 :300-302；daily 总桶错误归因口径 :308-311（`if error → errors_proxy; elif status>=500 → errors_upstream`）——dm 按同口径复制。
- 归因必须放在 `if entry_dm is not None:` 块内（cap 拒绝的模型无 entry 可归）。
- 边界（生产代码无需改、由现有调用形状保证）：499 aborted 调用（:407-408）传 `model=None` → 不进 `if model:` 块，dm 不归因；502 两条 `error=True` 带 model 路径（:434-436、:455-456）→ 归入该模型 `errors_proxy`。
- 测试精确等值（`==1`）安全依据：unittest 按方法名字母序执行，`test_by_model_accumulation_and_cap`（仅 200、无 error 写 m-a）先于 `test_daily_by_model_matrix_fallback`；m-a 由本方法首行调用保证存在。

### 1.1 baseline 登记

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t0-base.log 2>&1; echo "exit=$?"; tail -4 /tmp/t0-base.log
exit=0
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
```

非 42 绿 → 基线漂移，STOP 报主代理。

### 1.2 Edit T1 — `test_daily_by_model_matrix_fallback` 追加断言（锚点 :550-552）

OLD（唯一命中，grep `retry attribution must land in daily_by_model` = 1）:
```python
        self.assertGreaterEqual(dm_today["m-a"]["retries"], 1,
                                "retry attribution must land in daily_by_model")
        # 恒等式：daily 总桶 ≥ 分模型合计（差值 = 当日无 model 请求）
```

NEW:
```python
        self.assertGreaterEqual(dm_today["m-a"]["retries"], 1,
                                "retry attribution must land in daily_by_model")
        # v3：dm entry 恒 5 字段 + 错误归因与 daily 总桶同口径（error→proxy，5xx→upstream）
        self.assertEqual(
            set(dm_today["m-a"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
            "dm entry shape must stay in sync across both creation sites")
        mod._record_request("POST", "/dm", 502, 1.0, 0, model="m-a", error=True)
        self.assertEqual(dm_today["m-a"]["errors_proxy"], 1,
                         "error=True (proxy-made 502) must land in dm errors_proxy")
        mod._record_request("POST", "/dm", 500, 1.0, 0, model="m-a")
        self.assertEqual(dm_today["m-a"]["errors_upstream"], 1,
                         "upstream 500 passthrough must land in dm errors_upstream")
        before_499 = set(dm_today)
        mod._record_request("POST", "/dm", 499, 1.0, 0)  # 499 中断 model=None
        self.assertEqual(set(mod.STATS["daily_by_model"][today]), before_499,
                         "499 aborted (model=None) must not enter daily_by_model")
        self.assertEqual(dm_today["m-a"]["errors_proxy"], 1,
                         "499 must inflate neither dm error column")
        self.assertEqual(dm_today["m-a"]["errors_upstream"], 1)
        # 恒等式：daily 总桶 ≥ 分模型合计（差值 = 当日无 model 请求）
```

### 1.3 red 验证（精确 1 failure）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t1-red.log 2>&1; echo "exit=$?"; grep -E "^(FAIL:|ERROR:)" /tmp/t1-red.log; tail -4 /tmp/t1-red.log
exit=1
FAIL: test_daily_by_model_matrix_fallback (__main__.ProxyDashboardUnitTest)
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

FAILED (failures=1)
```

红清单：仅 `test_daily_by_model_matrix_fallback` 一条 failure，首因为 `AssertionError: ... != {'requests', 'filtered', 'errors_proxy', 'errors_upstream', 'retries'}`（dm entry shape 断言，3 字段 ≠ 5 字段）。出现其他 FAIL/ERROR 或 failure 数 ≠ 1 → STOP。

### 1.4 Edit P1 — `_record_request` dm 块（锚点 :298-302）

OLD（唯一命中：`entry_dm` 创建 + `requests`/`filtered` 尾行组合仅此一处；site-2 的同形创建以 `retries` 尾行区分）:
```python
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["requests"] += 1
                entry_dm["filtered"] += filtered
```

NEW:
```python
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["requests"] += 1
                entry_dm["filtered"] += filtered
                if error:
                    entry_dm["errors_proxy"] += 1
                elif status >= 500:
                    entry_dm["errors_upstream"] += 1
```

### 1.5 green 验证

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t1-green.log 2>&1; echo "exit=$?"; tail -4 /tmp/t1-green.log
exit=0
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
```

cap 回归（test_by_model_accumulation_and_cap）与深拷贝回归（test_stats_snapshot_daily_is_copy，其 :612-613 直接注入 3 字段 entry 只读写 `requests` 键，与本改动无冲突）须同绿。

### 1.6 commit

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py && git commit -m "feat(proxy): daily_by_model 错误归因（errors_proxy/errors_upstream）+ entry 5 字段"
```

---

## Task 2 [tier A] — 后端 `_record_empty_retry`：dm entry 创建点 5 字段 + docstring 同步（TDD red→green）

**文件**: 先 `ctyun-stream-fix-proxy.test.py`（red），后 `ctyun-stream-fix-proxy.py`（green）。

### 2.0 锚点与边界（已实测）

- dm entry 创建点 site-2 在 `_record_empty_retry` py:342-343，`entry_dm["retries"] += 1`（:345）不变；本函数**无 error/status 参数，不新增归因**，仅同步 5 字段形状（否则 Task 1 后两创建点形状分叉）。
- docstring（:327-329）是形状同步约定，随形状一起更新。
- **cap 陷阱（测试设计依据）**: `test_by_model_accumulation_and_cap`（字母序先跑）会把真实 today 的 dm 键位占满 32（m-a + m-00..m-30），此时对**新模型名**调 `_record_empty_retry` 会被 cap 拒绝创建 → KeyError。故 site-2 形状断言必须用**全新日期**隔离（沿用 `test_daily_bucket_spans_days` :528-533 的 today_key monkeypatch 模式，日期取 `2026-01-03` 避开已用的 `2026-01-02`）。

### 2.1 Edit T2 — `test_daily_by_model_matrix_fallback` 末尾追加 site-2 断言（锚点 :552-558）

OLD（唯一命中，grep `for k in ("requests", "retries")` = 1）:
```python
        # 恒等式：daily 总桶 ≥ 分模型合计（差值 = 当日无 model 请求）
        for k in ("requests", "retries"):
            total = mod.STATS["daily"][today][k]
            summed = sum(m[k] for m in dm_today.values())
            self.assertGreaterEqual(total, summed,
                                    "daily[%r][%r]=%d < Σ daily_by_model=%d"
                                    % (today, k, total, summed))
```

NEW:
```python
        # 恒等式：daily 总桶 ≥ 分模型合计（差值 = 当日无 model 请求）
        for k in ("requests", "retries"):
            total = mod.STATS["daily"][today][k]
            summed = sum(m[k] for m in dm_today.values())
            self.assertGreaterEqual(total, summed,
                                    "daily[%r][%r]=%d < Σ daily_by_model=%d"
                                    % (today, k, total, summed))
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 5 字段——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
            "empty-retry creation site must keep dm entry shape in sync")
```

### 2.2 red 验证（精确 1 failure）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t2-red.log 2>&1; echo "exit=$?"; grep -E "^(FAIL:|ERROR:)" /tmp/t2-red.log; tail -4 /tmp/t2-red.log
exit=1
FAIL: test_daily_by_model_matrix_fallback (__main__.ProxyDashboardUnitTest)
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

FAILED (failures=1)
```

红清单：仅 `test_daily_by_model_matrix_fallback` 一条 failure，首因为 site-2 形状断言（3 字段 ≠ 5 字段）。Task 1 已落地区块须全绿。漂移 → STOP。

### 2.3 Edit P2a — docstring 同步（锚点 :326-329）

OLD（唯一命中，grep `空流重试计数：STATS 总量` = 1）:
```python
    """空流重试计数：STATS 总量 + by_model retries 维度 + 当日桶 + daily_by_model。
    entry/桶形状必须与 _record_request 同步含 retries 键（旧持久化桶经
    load_daily_buckets 的 _DAILY_FIELDS 清洗已补键），否则 += 直接 KeyError。"""
```

NEW:
```python
    """空流重试计数：STATS 总量 + by_model retries 维度 + 当日桶 + daily_by_model。
    entry/桶形状必须与 _record_request 同步：dm entry 同为 5 字段（本函数无
    error/status 参数，errors_* 仅保形状不归因）；旧持久化桶经 load_daily_buckets
    的 _DAILY_FIELDS 清洗已补键。形状不同步时 += 直接 KeyError。"""
```

### 2.4 Edit P2b — dm entry 创建点 5 字段（锚点 :340-345）

OLD（唯一命中：Task 1 已把 site-1 改为 5 字段 + requests/filtered 尾行，此 3 字段 + retries 尾行组合仅剩 site-2 一处；grep `entry_dm["retries"] += 1` = 1）:
```python
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["retries"] += 1
```

NEW:
```python
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["retries"] += 1
```

### 2.5 green 验证

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t2-green.log 2>&1; echo "exit=$?"; tail -4 /tmp/t2-green.log
exit=0
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
```

### 2.6 commit

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py && git commit -m "feat(proxy): 空流重试 dm entry 创建点同步 5 字段形状 + docstring"
```

---

## Task 3 [tier A] — 前端：按天×模型表 7 列对齐 + 错误行高亮（TDD red→green）

**文件**: 先 `ctyun-stream-fix-proxy.test.py`（red），后 `ctyun-stream-fix-proxy.py`（green）。

### 3.0 锚点与边界（已实测）

- card HTML :763-771：thead :767 现 5 列（日期/模型/请求/剥行/重试）→ 7 列，数值列顺序与主表 :758 一致（请求/剥行/代理错误/上游5xx/重试）；tbody :768 空态 colspan 5→7；card-title :764「（内存累计，重启清零）」保留。
- `renderDailyByModel`（:939-968）：14 天截断/新在上（:942）与模型按 requests 降序（:954-956）**不变**；空态 `td0.colSpan` 5→7（:946）；循环体 :962-964 后插 2 个 cell（`|| 0` 防御热更新过渡态的旧 3 字段 entry）；行高亮 :959 由 `(ent.retries||0)>0` 改为与 `renderDaily` :929 一致的错误口径。
- 高亮口径变更是 spec 明示接受的行为变更（Risks）：空流重试活跃但无错误的行不再高亮。
- 既有断言不受影响的依据（grep 实测）：`<th>重试</th>` 现为 2 处（主表 + dm 表）改后仍 2 处；`colspan="6"`（主表 tbody :759）不动；`<th>代理错误</th>`/`<th>上游5xx</th>` 现各 1 处 → 改后各 2 处；`colspan="5"` 全库仅 :768 一处。

### 3.1 Edit T3 — `test_dashboard_html_full_page` 追加静态断言（锚点 :792-795）

OLD（唯一命中，grep `v2：按天表「重试」列（6 列）+ 按天×模型副表` = 1）:
```python
        # v2：按天表「重试」列（6 列）+ 按天×模型副表
        self.assertIn("<th>重试</th>", html)
        self.assertIn('colspan="6"', html)
        self.assertIn('id="daily-model-body"', html)
```

NEW:
```python
        # v2：按天表「重试」列（6 列）+ 按天×模型副表
        self.assertIn("<th>重试</th>", html)
        self.assertIn('colspan="6"', html)
        self.assertIn('id="daily-model-body"', html)
        # v3：按天×模型表 7 列与主表数值列对齐（代理错误/上游5xx）+ 错误行高亮
        self.assertEqual(html.count("<th>代理错误</th>"), 2)
        self.assertEqual(html.count("<th>上游5xx</th>"), 2)
        self.assertIn('colspan="7"', html)
        self.assertIn("String(ent.errors_proxy || 0)", html)
        self.assertIn("String(ent.errors_upstream || 0)", html)
        self.assertIn("(ent.errors_proxy || 0) + (ent.errors_upstream || 0)", html)
```

### 3.2 red 验证（精确 1 failure）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t3-red.log 2>&1; echo "exit=$?"; grep -E "^(FAIL:|ERROR:)" /tmp/t3-red.log; tail -4 /tmp/t3-red.log
exit=1
FAIL: test_dashboard_html_full_page (__main__.AdminIntegrationTest)
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

FAILED (failures=1)
```

红清单：仅 `test_dashboard_html_full_page` 一条 failure，首因为 `AssertionError: 1 != 2`（`<th>代理错误</th>` 计数）。漂移 → STOP。

### 3.3 Edit P3a — card HTML 表头 + 空态 colspan（锚点 :767-768）

OLD（唯一命中，含 `daily-model-body`）:
```html
      <thead><tr><th>日期</th><th>模型</th><th>请求</th><th>剥行</th><th>重试</th></tr></thead>
      <tbody id="daily-model-body"><tr><td class="empty" colspan="5">读取中……</td></tr></tbody>
```

NEW:
```html
      <thead><tr><th>日期</th><th>模型</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-model-body"><tr><td class="empty" colspan="7">读取中……</td></tr></tbody>
```

### 3.4 Edit P3b — `renderDailyByModel` 空态 colSpan（锚点 :943-949）

OLD（唯一命中，grep `暂无按天 × 模型统计` = 1）:
```javascript
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天 × 模型统计");
    td0.colSpan = 5;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    return;
  }
```

NEW:
```javascript
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天 × 模型统计");
    td0.colSpan = 7;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    return;
  }
```

### 3.5 Edit P3c — `renderDailyByModel` 循环体：高亮口径 + 2 个新 cell（锚点 :957-965）

OLD（唯一命中，grep `var tr = el("tr", (ent.retries || 0) > 0 ? "hit" : "");` = 1）:
```javascript
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
```

NEW:
```javascript
    for (var j = 0; j < names.length; j++) {
      var ent = models[names[j]];
      var tr = el("tr", ((ent.errors_proxy || 0) + (ent.errors_upstream || 0)) > 0 ? "hit" : "");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(ent.requests || 0)));
      tr.appendChild(el("td", "num", String(ent.filtered || 0)));
      tr.appendChild(el("td", "num", String(ent.errors_proxy || 0)));
      tr.appendChild(el("td", "num", String(ent.errors_upstream || 0)));
      tr.appendChild(el("td", "num", String(ent.retries || 0)));
      body.appendChild(tr);
    }
```

### 3.6 green 验证

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t3-green.log 2>&1; echo "exit=$?"; tail -4 /tmp/t3-green.log
exit=0
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
```

### 3.7 commit

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py && git commit -m "feat(dashboard): 按天×模型表 7 列对齐主表数值列 + 错误行琥珀高亮"
```

---

## Task 4 [tier B] — 集成断言加固 + 全量回归（纯测试追加，预期直接绿）

**文件**: `ctyun-stream-fix-proxy.test.py`（唯一）。无生产代码——red 先义务已由 Task 1-3 各卡单测履行，本卡断言预期直接绿（漂移出绿 → STOP 报主代理）。

### 4.0 锚点与边界（已实测）

- `test_recent_entries_carry_model`（:811-825）经真实 scratch 进程 + fake upstream 走完整 SSE 链路，`post_sse` 为 200 → dm entry 的 `errors_proxy`/`errors_upstream` 为 0、`requests ≥ 1`；断言只锁「5 键存在且 int ≥ 0」（键形状透出），不断言错误值。
- `/api/stats`（:589-590）经 `stats_snapshot` 双层深拷贝透出 dm 新键，纯新增、向后兼容。

### 4.1 Edit T4 — 集成断言追加（锚点 :820-825）

OLD（唯一命中，grep `per-model daily matrix must record the SSE request` = 1）:
```python
        # v2：daily_by_model 集成（/api/stats 顶层键透出；retries 归因见单测 matrix_fallback）
        today = time.strftime("%Y-%m-%d")
        self.assertIn(today, snap["daily_by_model"])
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["requests"], 1,
            "per-model daily matrix must record the SSE request")
```

NEW:
```python
        # v2：daily_by_model 集成（/api/stats 顶层键透出；retries 归因见单测 matrix_fallback）
        today = time.strftime("%Y-%m-%d")
        self.assertIn(today, snap["daily_by_model"])
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["requests"], 1,
            "per-model daily matrix must record the SSE request")
        # v3：dm entry 5 键经 /api/stats 透出（纯新增键，旧客户端只读 3 键不受影响）
        dm_entry = snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]
        for key in ("requests", "filtered", "errors_proxy", "errors_upstream", "retries"):
            self.assertIn(key, dm_entry)
            self.assertIsInstance(dm_entry[key], int)
            self.assertGreaterEqual(dm_entry[key], 0)
```

### 4.2 全量回归（whole-branch verify）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 ctyun-stream-fix-proxy.test.py > /tmp/t4-full.log 2>&1; echo "exit=$?"; tail -4 /tmp/t4-full.log
exit=0
----------------------------------------------------------------------
Ran 42 tests in xx.xxx s

OK
```

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/daily-by-model-14d-align && /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py; echo "exit=$?"
exit=0
```

绿清单：42 tests OK（含 test_recent_entries_carry_model 新断言）+ py_compile exit 0。任何 FAIL/ERROR → STOP。

### 4.3 commit

```
$ git add ctyun-stream-fix-proxy.test.py && git commit -m "test(dashboard): daily_by_model 5 键经 /api/stats 透出的集成断言"
```

---

## 附录：锚点实测记录（2026-09-11，PLAN-B 写作时）

| 锚点 | 实测结果 |
|---|---|
| `_record_request` dm 块 :296-302 / daily 归因 :308-311 | 逐行核对一致（Read） |
| `_record_empty_retry` :326-350（docstring :327-329、创建 :342-343） | 逐行核对一致（Read） |
| `stats_snapshot` dm 双层深拷贝 :358-360 | 核对一致，零改动确认 |
| card HTML :763-771（thead :767 / tbody :768 / title :764） | 逐行核对一致（Read） |
| 主表 thead :758（6 列顺序）/ footer :786-792 | 核对一致，不动确认 |
| `renderDaily` :915-938（:929 高亮口径）/ `renderDailyByModel` :939-968 | 逐行核对一致（Read） |
| 499 调用 :407-408（model=None）/ 502 调用 :434-436、:455-456（error=True + model） | 核对一致，不改确认 |
| 测试 :538-558（matrix_fallback）/ :769-800（dashboard_html）/ :811-825（recent_entries） | 逐行核对一致（Read） |
| OLD 块唯一性 grep | `retry attribution...`=1、`for k in ("requests", "retries")`=1、`空流重试计数：STATS 总量`=1、`entry_dm["retries"] += 1`=1、`v2：按天表「重试」列...`=1、`暂无按天 × 模型统计`=1、`(ent.retries \|\| 0) > 0 ? "hit"`=1、`per-model daily matrix...`=1；3 字段创建字面量=2（两 site，靠尾行区分，各 OLD 块整体唯一） |
| th/colspan 计数 | `<th>代理错误</th>`=1→改后 2；`<th>上游5xx</th>`=1→2；`<th>重试</th>`=2 不变；`colspan="5"` 仅 :768；`colspan="6"` 主表保留 |
| 基线 | `Ran 42 tests in 20.275s / OK / exit 0`（2026-09-11 实测） |
| /tmp 沙箱 dry-run（2026-09-11） | 10 组 OLD/NEW 按卡序机械应用（脚本抽取自本 PLAN，OLD 各唯一命中）：red1(T1)=1 failure `matrix_fallback` → green1(P1)=42 OK → red2(T2)=1 failure `matrix_fallback` → green2(P2a+P2b)=42 OK → red3(T3)=1 failure `dashboard_html_full_page` → green3(P3a+P3b+P3c)=42 OK → final(T4)=42 OK + py_compile exit 0。**全部与各卡红/绿清单逐项吻合**（沙箱已清理，worktree 源码零触碰） |
