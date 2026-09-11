# PLAN — 移除 dashboard「按模型（自上次重启起累计）」卡片

## Header

| 项 | 值 |
|---|---|
| 日期 | 2026-09-11 |
| Spec | `docs/superpowers/specs/2026-09-11-remove-by-model-design.md`（commit 900006d） |
| Worktree | `/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/remove-by-model`（分支 `fix/remove-by-model`） |
| 改动文件 | `ctyun-stream-fix-proxy.py`（10 处 Edit）、`ctyun-stream-fix-proxy.test.py`（7 处 Edit） |
| 卡数 / tier | 2 卡，全部 tier A（纯机械删除/改写，old_string/new_string 已逐字写死，无决策点）→ executor |
| 方案 | Spec 方案 B：前后端全删，零死代码；`stats_snapshot()` 不再返回 `by_model` 键 |
| Baseline | `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → Ran 42 tests, OK, exit 0（2026-09-11 实测） |
| 测试命令 | `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/remove-by-model && /usr/bin/python3 ctyun-stream-fix-proxy.test.py`（README:66 口径） |

## Global Constraints

1. **TDD 顺序铁律**：Task 1（测试改写，红）必须先于 Task 2（生产删除，绿）落地。Task 1 验证预期**恰好 2 个失败**（见卡内清单），出现其他失败 = 锚点不符，STOP 上报。
2. **保留清单（禁碰）**：
   - `BY_MODEL_CAP = 32`（`:44`）—— daily_by_model 分支（改后 `:288`/`:333` 附近）仍消费；
   - `daily_by_model` 全链路：STATS 初始化、`_record_request`/`_record_empty_retry` 写入块、`stats_snapshot` 双层深拷贝、`renderDailyByModel`、`#daily-model-body`；
   - 两函数的 `if model:` 外层守卫（删 by_model 块后守卫保留，包裹 daily_by_model 写入）；
   - `RECENT_REQUESTS` 的 `model` 字段与最近请求表模型列（`renderRecent` 的 `<th>模型</th>`）；
   - `.strip` CSS（`:697`，仍被 `#poison-strip` 使用）、`.chip-poison` CSS、按天主表（`renderDaily`/`#daily-body`）、顶部汇总卡、sparkline、剥行流带；
   - 持久化格式不变（`save_stats_counters` 从未持久化 by_model，老文件无需迁移）。
3. **footer 口径句**（`:789`）「按天主表含无 model 请求，各行数值 ≥…」不动；只改 `:788` 一行。
4. **无新依赖、无新文件**；LF 换行；Edit 的 old_string 必须逐字匹配（含缩进/全角字符/`·` 间隔符）。
5. **历史文档不碰**：`docs/superpowers/plans/2026-09-05-*`、`2026-09-11-*empty-stream-retry*` 等旧 plan 中的 `by_model` 引用是历史记录，不在 scope。
6. 每卡完成后 commit（Conventional Commits，见各卡尾）；commit 前自检 `git config --get user.name && git config --get user.email` 为期望身份（R32）。

## 锚点实测记录（PLAN-B 写作时验证，commit 900006d）

- `grep -n "by_model\|model-chips\|renderModelChips\|chip-model\|自上次重启起累计\|按模型" ctyun-stream-fix-proxy.py` → 命中行：101, 289-292, 296(daily), 327(docstring), 334-337, 340(daily), 356, 358/360(daily), 702, 704, 751, 752, 788, 882-899(函数体), 1027, 1030(daily)。
- 同 pattern grep 测试文件 → 命中行：474-484, 538-557(daily), 611-620(daily), 780-782, 796-797, 817-819, 820-824(daily), 1089-1103。
- Baseline 全量测试 42 个全绿（exit 0）。

## Spec 偏差与补充（同根因，非 scope 扩张）

Spec Files 列出主文件 8 处 + 测试 3 处；锚点 grep 实测发现以下 4 点为**验收必红的同根因遗漏**，已并入本 PLAN（不解决则 Acceptance「测试全绿 / grep 零命中」不达成）：

| # | 位置 | spec 状态 | 本 PLAN 处理 |
|---|---|---|---|
| S1 | `ctyun-stream-fix-proxy.test.py:780-782` `assertIn("按模型", html)` | 未列（删卡后必红） | Task 1 Edit T2：删该断言并改注释 |
| S2 | `ctyun-stream-fix-proxy.test.py:796-797` `assertIn("自上次重启起累计", html)` | 未列（删卡后必红） | Task 1 Edit T3：翻转为 `assertNotIn` 锁定不复活 |
| S3 | `ctyun-stream-fix-proxy.py:327` docstring「STATS 总量 + by_model retries 维度 + …」 | 未列（Acceptance grep `by_model` 零命中的独立命中） | Task 2 Edit P3：删「by_model retries 维度 +」片段 |
| S4 | `ctyun-stream-fix-proxy.test.py:476` `mod._record_request(..., model="m-a")` | spec 只说删断言 477-481 | Task 1 Edit T1 连同删除——该记录只为被删断言服务，保留即死测试代码（铁律「无死代码」） |
| S5 | `ctyun-stream-fix-proxy.test.py:506-507` daily 桶 setdefault 模板缺 `retries` 键 | 未列（T1 改名改变字母序后暴露：`test_daily_by_model_matrix_fallback` KeyError 'retries'，沙盒模拟实测复现） | Task 1 Edit T6：模板补 `"retries": 0` 与生产桶形状对齐 |
| S6 | `ctyun-stream-fix-proxy.test.py:538-548` matrix_fallback 依赖先跑测试放入 `m-a` 的跨测试隐式耦合 | 未列（T1 删 `m-a` 记录行 + 改名后暴露：`m-a` 被当日 32 cap 挡 → KeyError 'm-a'，沙盒模拟实测复现；T6 修复后浮出水面） | Task 1 Edit T7：开头清当日 `daily_by_model` 桶，测试自洽化 |

S5/S6 同根因（T1 改名引发），不修则 Task 2 验收「测试全绿」不达成。

另两处主动强化（不改行为，仅锁定回归）：Edit T3 增加 `assertNotIn("按模型", html)`；Edit T4 将被删的 `snap["by_model"]` 断言原位替换为 `assertNotIn("by_model", snap)`（对应 Acceptance「snapshot 无 by_model 键」）。

---

## Task 1 [tier A] — 测试先行：改写测试锁定目标状态（预期红）

**文件**：`ctyun-stream-fix-proxy.test.py`（仅此一个文件，7 组 Edit）
**执行者**：executor（纯转写）
**目标**：测试表达「by_model 卡片/键已移除」的目标状态；生产未动 → 恰好 2 个集成测试失败（红证据）。

### Edit T1 — `ProxyDashboardUnitTest.test_by_model_accumulation_and_cap` 改名瘦身（:474-484）

old_string:
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

new_string:
```python
    def test_daily_by_model_cap(self) -> None:
        mod = self.mod
        for i in range(40):
            mod._record_request("POST", "/c", 200, 1.0, 0, model="m-%02d" % i)
        self.assertLessEqual(len(mod.STATS["daily_by_model"][mod.today_key()]), 32)
        self.assertNotIn("m-39", mod.STATS["daily_by_model"][mod.today_key()])
```

### Edit T2 — `AdminIntegrationTest` dashboard 断言块 1：删「按模型」存在性断言（:780-783）

old_string:
```python
        # v1.1：sparkline 剥行红柱叠加 / 按模型卡 / 模型列 / 计数持久化文案
        self.assertIn("var(--err)", html)
        self.assertIn("按模型", html)
        self.assertIn("<th>模型</th>", html)
```

new_string:
```python
        # v1.1：sparkline 剥行红柱叠加 / 模型列 / 计数持久化文案
        self.assertIn("var(--err)", html)
        self.assertIn("<th>模型</th>", html)
```

### Edit T3 — dashboard 断言块 2：口径标注断言翻转为 assertNotIn（:796-797）

old_string:
```python
        # v1.3：按模型口径标注（自上次重启起累计，重启清零）
        self.assertIn("自上次重启起累计", html)
```

new_string:
```python
        # v2.1：按模型重启累计卡已整体移除，标题与口径标注锁定不复活
        self.assertNotIn("自上次重启起累计", html)
        self.assertNotIn("按模型", html)
```

### Edit T4 — `test_recent_entries_carry_model`：by_model 集成断言原位替换为键缺失断言（:817-819）

old_string:
```python
        self.assertIn("deepseek-v4-pro-0813-oc", snap["by_model"])
        self.assertGreaterEqual(
            snap["by_model"]["deepseek-v4-pro-0813-oc"]["requests"], 1)
```

new_string:
```python
        self.assertNotIn("by_model", snap,
                         "by_model must be removed from /api/stats snapshot")
```

（紧随其后的 `# v2：daily_by_model 集成…` 起的 daily_by_model 断言块 :821-825 原样保留，禁碰。）

### Edit T5 — 整体删除 `AdminIntegrationTest.test_by_model_retries_dimension`（:1089-1103）

old_string:
```python
        self.assertEqual(json.loads(body.decode("utf-8"))["empty_retries_total"], 6)

    def test_by_model_retries_dimension(self) -> None:
        upstream_port, calls = make_scripted_upstream()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        entry = snap["by_model"]["deepseek-v4-pro-0813-oc"]
        self.assertGreaterEqual(entry["retries"], 1)
        self.assertEqual(entry["requests"], 1,
                         "retry must not double-count model requests")
        self.assertGreaterEqual(snap["empty_retries_total"], 1)


if __name__ == "__main__":
```

new_string:
```python
        self.assertEqual(json.loads(body.decode("utf-8"))["empty_retries_total"], 6)


if __name__ == "__main__":
```

（retries 归因覆盖由 `test_daily_by_model_matrix_fallback` 的 daily_by_model retries 断言承担，spec 已核实不损失覆盖。）

### Edit T6 — `test_daily_bucket_accumulation_and_dual_error_semantics` 桶模板补 `retries` 键（:505-507）

**根因（沙盒模拟实测发现）**：该测试的 setdefault 模板缺 `"retries": 0`，与生产 `_record_request`/`_record_empty_retry` 的桶模板（`ctyun-stream-fix-proxy.py:303-305/346-348` 均含 retries）形状不一致。Baseline 下该不一致被字母序执行顺序掩盖（旧名 `test_by_model_accumulation_and_cap` 先跑，经 `_record_request` 先建出带 retries 的当日桶）；Edit T1 改名 `test_daily_by_model_cap` 后本测试变为先跑，setdefault 建出无 retries 桶，后续 `test_daily_by_model_matrix_fallback` 的 `bucket["retries"] += 1` 即 KeyError。对齐生产桶形状，消除对测试顺序的隐式依赖。

old_string:
```python
        today = mod.today_key()
        bucket = mod.STATS["daily"].setdefault(
            today, {"requests": 0, "filtered": 0, "errors_proxy": 0, "errors_upstream": 0})
```

new_string:
```python
        today = mod.today_key()
        bucket = mod.STATS["daily"].setdefault(
            today, {"requests": 0, "filtered": 0, "errors_proxy": 0,
                    "errors_upstream": 0, "retries": 0})
```

（`base = dict(bucket)` 起的断言只读 requests/filtered/errors_*，补键不影响；该 setdefault 是测试文件中 `STATS["daily"]` 唯一一处，old_string 全文件唯一。）

### Edit T7 — `test_daily_by_model_matrix_fallback` 开头清当日桶，消除跨测试顺序依赖（:538-541）

**根因（沙盒模拟实测发现，与 T6 同源）**：`ProxyDashboardUnitTest.setUpClass` 共享一个 `mod` 实例，STATS 跨测试累积。旧序下 `test_by_model_accumulation_and_cap`（字母序最前）先放入 `m-a` 再以 40 个 `m-XX` 填满当日 32 cap，matrix_fallback 的 `_record_request(model="m-a")` 靠命中**已有** entry 通过；T1 改名 `test_daily_by_model_cap` 且删 `m-a` 记录行后，当日桶 = `{m-00..m-31}` 无 `m-a`，matrix_fallback 的 `m-a` 被每日 cap 挡掉 → `dm_today["m-a"]` KeyError。开头清当日桶，使该测试不依赖任何其他测试的残留。

old_string:
```python
    def test_daily_by_model_matrix_fallback(self) -> None:
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/dm", 200, 1.0, 1, model="m-a")
```

new_string:
```python
    def test_daily_by_model_matrix_fallback(self) -> None:
        mod = self.mod
        today = mod.today_key()
        # 清当日桶自洽化：setUpClass 共享 mod，cap 测试可能已把当日 daily_by_model
        # 填满 32 键，m-a 会被每日 cap 挡掉（旧序靠先跑测试放入 m-a 的跨测试
        # 隐式耦合，改名后暴露）。
        mod.STATS["daily_by_model"][today] = {}
        mod._record_request("POST", "/dm", 200, 1.0, 1, model="m-a")
```

（后续断言全部兼容：`before` 集合在 m-a record 后取值仍为 `{"m-a"}`；恒等式 `daily 总桶 ≥ Σ daily_by_model` 因 summed 只含 m-a 而更易成立；清空只影响本测试内状态，后续测试不依赖当日 m-XX 残留。）

### 验证命令（Task 1，预期红）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/remove-by-model && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期**：exit code 1，`FAILED (failures=2)`，失败**恰好**为：
- `AdminIntegrationTest.test_dashboard_html_full_page` — `assertNotIn("自上次重启起累计", html)` 失败（生产尚未删卡）；
- `AdminIntegrationTest.test_recent_entries_carry_model` — `assertNotIn("by_model", snap)` 失败（snapshot 尚含键）。

其余 39 个测试全 ok（含改名的 `test_daily_by_model_cap`、T6 修复的 `test_daily_bucket_accumulation_and_dual_error_semantics`、T7 自洽化的 `test_daily_by_model_matrix_fallback`——生产 daily_by_model 链路未动，均应绿）。**出现上述 2 个之外的任何失败/错误 → STOP 上报主代理，不得继续 Task 2。**

### Commit

```
test(dashboard): 锁定按模型重启累计卡移除（断言翻转+cap 用例改名瘦身）
```

---

## Task 2 [tier A] — 生产删除：by_model 前后端链路全删（预期绿）

**文件**：`ctyun-stream-fix-proxy.py`（仅此一个文件，10 组 Edit；依赖 Task 1 已落地）
**执行者**：executor（纯转写）
**目标**：HTML 卡片、renderModelChips、poll 调用、STATS 键、两处写入块、snapshot 深拷贝、死 CSS、footer 文案、docstring 全部清除，测试全绿。

### Edit P1 — STATS 初始化删 `"by_model": {}` 键（:99-101）

old_string:
```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0,
         "active": 0, "by_model": {}, "daily": {}, "daily_by_model": {}}
```

new_string:
```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {}}
```

### Edit P2 — `_record_request` 删 by_model 写入块，保留守卫与 daily 块（:288-296）

old_string:
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
```

new_string:
```python
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
```

（old_string 因含 `entry["requests"] += 1` / `entry["filtered"] += filtered` 与 P4 区分，全文件唯一。后续 `entry_dm` 的 daily_by_model 块原样保留，禁碰。）

### Edit P3 — `_record_empty_retry` docstring 删「by_model retries 维度 +」片段（:327）

old_string:
```python
    """空流重试计数：STATS 总量 + by_model retries 维度 + 当日桶 + daily_by_model。
```

new_string:
```python
    """空流重试计数：STATS 总量 + 当日桶 + daily_by_model。
```

（docstring 其余两行不动。）

### Edit P4 — `_record_empty_retry` 删 by_model 写入块，保留守卫与 daily 块（:333-340）

old_string:
```python
        if model:
            by_model = STATS["by_model"]
            entry = by_model.get(model)
            if entry is None and len(by_model) < BY_MODEL_CAP:
                entry = by_model[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry is not None:  # 键数达上限后新模型不记录，防内存膨胀
                entry["retries"] += 1
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
```

new_string:
```python
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
```

### Edit P5 — `stats_snapshot` 删 by_model 深拷贝行（:355-357）

old_string:
```python
        snap = dict(STATS)
        snap["by_model"] = {k: dict(v) for k, v in STATS["by_model"].items()}
        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
```

new_string:
```python
        snap = dict(STATS)
        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
```

（紧随的 `daily_by_model` 双层深拷贝 :358-360 原样保留，禁碰。）

### Edit P6 — CSS 删 `.chip-model` 两条规则（:702-704）

old_string:
```css
.chip-model { flex:0 0 auto; border:1px solid var(--line); border-radius:4px;
  padding:2px 8px; font-size:12px; white-space:nowrap; color:var(--text); }
.chip-model .n { color:var(--amber); font-weight:700; }
.empty { color:var(--dim); }
```

new_string:
```css
.empty { color:var(--dim); }
```

（`.strip` :697 与 `.chip-poison` 系列保留——`#poison-strip` 仍用。）

### Edit P7 — HTML 删「按模型」卡片整个 section（:750-755）

old_string:
```html
  <section class="card">
    <div class="card-title">按模型<span class="chip">自上次重启起累计，重启清零</span></div>
    <div class="strip" id="model-chips"><span class="empty">暂无按模型统计</span></div>
  </section>
  <section class="card">
    <div class="card-title">按天统计（最近 14 天，新在上）</div>
```

new_string:
```html
  <section class="card">
    <div class="card-title">按天统计（最近 14 天，新在上）</div>
```

（old_string 经「按模型」标题行保证唯一；删后 sparkline 卡与按天统计卡直接相邻。）

### Edit P8 — footer 文案同步（:788）

old_string:
```html
  按模型与按天×模型计数自进程启动累计，不持久化（重启清零）；
```

new_string:
```html
  按天×模型计数自进程启动累计，不持久化（重启清零）；
```

（`:787` 跨重启保留句与 `:789` 主表≥副表口径句不动。）

### Edit P9 — JS 删 `renderModelChips` 全函数（:882-899）

old_string:
```javascript
function renderModelChips(byModel) {
  var wrap = $("model-chips");
  wrap.textContent = "";
  var names = Object.keys(byModel);
  if (names.length === 0) {
    wrap.appendChild(el("span", "empty", "暂无按模型统计"));
    return;
  }
  names.sort(function (a, b) { return byModel[b].requests - byModel[a].requests; });
  for (var i = 0; i < names.length; i++) {
    var m = byModel[names[i]];
    var chip = el("span", "chip-model");
    chip.appendChild(el("span", "", names[i] + " "));
    chip.appendChild(el("span", "", m.requests + " 请求"));
    if (m.filtered > 0) chip.appendChild(el("span", "n", " · " + m.filtered + " 剥行"));
    wrap.appendChild(chip);
  }
}
function renderPoison(list) {
```

new_string:
```javascript
function renderPoison(list) {
```

### Edit P10 — `poll()` 删 `renderModelChips` 调用行（:1026-1028）

old_string:
```javascript
      renderRecent(snap.recent || []);
      renderModelChips(snap.by_model || {});
      renderPoison(snap.poison_previews || []);
```

new_string:
```javascript
      renderRecent(snap.recent || []);
      renderPoison(snap.poison_previews || []);
```

### 验证命令（Task 2，预期全绿）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/remove-by-model && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期**：`Ran 41 tests ... OK`，exit code 0，stderr 无 Traceback（42 - 删除的 `test_by_model_retries_dimension` = 41）。

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/remove-by-model && /usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py && echo COMPILE_OK
```

**预期**：输出 `COMPILE_OK`，exit 0。

Acceptance grep（spec 验收口径，主文件零命中）：

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/remove-by-model && grep -n "model-chips\|renderModelChips\|by_model\|chip-model" ctyun-stream-fix-proxy.py | grep -v daily_by_model; echo "grep_rc=$?"
```

**预期**：无输出行，`grep_rc=1`（管道末 grep 无命中）。

补充核对（非 spec 硬门禁，但应零命中）：

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/remove-by-model && grep -n "自上次重启起累计\|按模型" ctyun-stream-fix-proxy.py; echo "grep_rc=$?"
```

**预期**：无输出行，`grep_rc=1`。

（测试文件 grep `by_model` 会命中 Edit T4 的 `assertNotIn("by_model", snap)` 字面量——这是刻意的回归锁，非死代码，属预期。）

### Commit

```
refactor(dashboard): 移除按模型重启累计卡（前后端链路全删零死代码）
```

---

## Acceptance Criteria 映射

| Spec 验收 | 由谁证明 |
|---|---|
| 主文件 grep `model-chips\|renderModelChips\|by_model\|chip-model` 零命中（daily_by_model 除外） | Task 2 验证命令 3（P1-P10 全落地后） |
| `stats_snapshot()` 无 `by_model` 键、`daily_by_model` 仍在 | Edit P5（删）+ Task 1 Edit T4 `assertNotIn`（锁）+ 既有 `test_stats_snapshot_daily_is_copy`（daily 仍在） |
| dashboard 无「按模型」独立卡片；按天×模型副表正常出数 | Edit P7（删卡）+ Task 1 Edit T3 `assertNotIn`（锁）+ 既有 `test_recent_entries_carry_model` daily_by_model 集成断言（副表出数） |
| 测试全绿、stderr 无意外错误 | Task 2 验证命令 1（41 tests OK） |
| 无未使用变量/import/死 CSS；footer 口径一致 | Edit P6（CSS）、P9（函数）、P1/P2/P4/P5（变量/键）、P8（footer）；Task 2 验证命令 3/4 grep 兜底 |

## Plan Self-Review（5 项）

1. **Spec coverage**：spec 主文件 8 项 → P1-P10 一一对应（spec 第 8 项拆为 P6+P8 两 Edit）；spec 测试 3 项 → T1/T4/T5 对应；另补 6 处同根因遗漏（S1-S6，见偏差表）与 2 处回归锁强化。spec Exclusions 全部落入 Global Constraints 保留清单。无遗漏。
2. **Placeholder scan**：全文 0 个「此处省略/类似/同上/TODO/按 spec 执行」类占位；17 组 Edit 的 old_string/new_string 均为逐字完整代码（含缩进与全角字符）。
3. **Type consistency**：纯删除任务，无新类型/签名。`stats_snapshot()` 返回 dict 少一个键（向后兼容风险 spec Risks 已接受）；`_record_request`/`_record_empty_retry` 的 `if model:` 守卫保留，`today_key()`/`BY_MODEL_CAP` 引用不悬空；JS 侧 `snap.by_model` 唯一消费者（poll :1027）随 P10 同步删除，无悬空引用。
4. **可落盘性**：每卡 = 文件 + Edit 对 + 预期输出 + commit message，executor 可零决策转写；Task 1 红清单（恰好 2 失败）与 Task 2 绿清单（41 tests）给出精确判停条件。
5. **锚点实测**：全部 old_string 取自 commit 900006d 的 Read 实测输出（行号见各 Edit 标题）；baseline 42 tests OK 已跑；grep 命中清单见「锚点实测记录」；唯一性已核对（P2/P4 靠 `entry["requests"]` vs `entry["retries"]` 区分，P7 靠「按模型」标题行，P9 靠函数名）。**沙盒模拟全链验证**（/tmp 副本按 PLAN 顺序机械应用全部 17 组 Edit）：Task 1 → exit 1、恰好 2 failures 且 0 errors（`test_dashboard_html_full_page`/`test_recent_entries_carry_model`）、Ran 41；Task 2 → Ran 41 tests OK、exit 0、stderr 无 Traceback；py_compile 双文件通过；验收 grep 主文件零命中。模拟过程用后已删，未触碰工作树源文件。
