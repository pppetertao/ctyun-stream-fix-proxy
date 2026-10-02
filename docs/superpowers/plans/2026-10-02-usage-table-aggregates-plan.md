# PLAN — Dashboard 用量表「输入/输出/总用量/缓存命中率」扩列

- Spec: `docs/superpowers/specs/2026-10-02-usage-table-input-output-total-hitrate-design.md`
- Worktree: `.worktrees/usage-table-aggregates`，分支 `fix/usage-table-aggregates`
- 日期: 2026-10-02
- 卡数: 2（全部 tier A/B → executor）
- 目标文件: `ctyun-stream-fix-proxy.py`（单文件后端+dashboard）、`ctyun-stream-fix-proxy.test.py`

## 卡序与范围

| 卡 | tier | 范围（spec 改动点编号） | 生产改动 | 测试改动 |
|----|------|------------------------|----------|----------|
| 卡 1 | A（executor） | 表 B：spec #1 thead、#2 空态 colspan、#3 renderTokens、#7 扩列断言、#9 公式断言（表 B 子集）、#10 表 B colspan 断言 | py ×3 Edit | test ×2 Edit |
| 卡 2 | A（executor） | 表 A：spec #4 thead、#5 空态 colspan、#6 renderByKey、#8 新列用例、#9 表 A 子集断言（`k.tokens_reasoning`）、#10 表 A colspan 断言 | py ×4 Edit | test ×3 Edit |

spec 10 个改动点全部落卡；#9 按「卡内独立红→绿」拆为卡 1 子集（表 B 公式）+ 卡 2 追加 1 条（表 A `k.tokens_reasoning`）；#10 按表拆为卡 1（表 B colspan）+ 卡 2（表 A colspan）。

## Global Constraints（全卡强制）

1. **口径写死（不得偏离）**：`inp = Math.max(tp, cr)`、`out = Math.max(tc, rn)`、`tot = inp + out`、`hit = inp > 0 ? (cr / inp * 100).toFixed(1) + "%" : "-"`。JS 局部变量名严格用 `inp/out/tot/hit`（spec Risks：JS 源码文本断言耦合，重命名会破坏新测试）。
2. **锚点实测结论（本 PLAN 已核实，执行时以此为准）**：
   - **2a 落点**：`renderTokens`（py:4440）与 `renderByKey`（py:4815）**均在 `_DASH_JS_V2`**（常量区间 py:4356–5452；`_DASH_JS_CORE` 为 3937–4354，不含这两函数）。故 spec `test_usage_aggregate_formulas` 断言目标 `self.mod._DASH_JS_V2` **正确，无需按函数拆分**。
   - **2b 逐字核对**：spec Files#3 改前块 = 实际 py:4463–4476，Files#6 空态块 = py:4822–4824、行段 = py:4837–4848，用 repr 逐字核对缩进一致，无行号漂移（thead B=3817、tbody B=3818、thead A=3754、tbody A=3755）。改后卡 1 会给 py 增加 4 行，卡 2 锚点全部为内容锚定，不受漂移影响。
   - **测试落点修正（TDD 有效性，不回炉 spec）**：spec Files#10 的 `self.assertIn('colspan="10"', html)` 在改前**已绿**——全页 HTML 中 `colspan="10"` 已由 `ttfb-hist-body`（py:3828）命中（实测 built page count=1），不能作 red 断言。改为 id 作用域精确断言 `<tbody id="token-daily-body"><tr><td class="empty" colspan="10">`（改前实测 count=0）与 `<tbody id="bykey-body"><tr><td class="empty" colspan="11">`（改前实测 count=0）；spec #10 原有两条 `colspan="6"`/`colspan="7"` assertIn 保持不动（改后分别仍有 2/4 处命中，实测确认仍绿）。
   - **插入点澄清**：表 B `inp/out/tot/hit` 四行插在 `hasData = true;` 之后、`var tr = el("tr");` 之前（全零守卫 `if (tp + tc + cr + rn === 0) continue;` 保持在其前）；表 A 四行插在 `var rn = k.tokens_reasoning || 0;` 之后、`var tr` 之前。
3. **TDD 纪律（每卡独立红→绿）**：先写该卡全部测试 → 跑焦点命令确认**按预期失败**（贴失败断言名）→ 再改生产 → 跑焦点命令全绿 → 卡末跑全量并贴 stdout+exit code。禁止先改生产再补测试。
4. **Baseline 已登记（本 PLAN 实测）**：`python3 ctyun-stream-fix-proxy.test.py` → `Ran 366 tests in 157.149s` / `OK`（本机 `python3` = `/opt/homebrew/bin/python3` 3.14.7）。已知 flaky：`test_usage_settle_releases_budget`（git log b977d0b 记录，本轮通过）；若全量出现该用例偶发红，单独重跑确认，不计入本次 regression。
5. **预期全量计数**：新增 2 个测试方法（`test_v3_bykey_table_columns`、`test_usage_aggregate_formulas`）→ 卡 1 末全量 **367 例**（366+1，`test_usage_aggregate_formulas`）、卡 2 末全量 **368 例**（366+2），两次均须全绿 exit 0。
6. **Exclusions 全继承（不得越界）**：排序键不改（py:4451-4453、py:4833-4835）；`renderZeroToken`/`renderQuota` stat 卡不改；卡片标题不改；不部署/不重启/webhook；不加 CSS/不改列宽样式；`tokens_cache_write` 不解析不渲染；表 A **不加「推理」细列**；`daily-body`/`daily-model-body`/`perf-model-body`/`stability-body` 等其它表零改动。
7. **不改后端 Python 逻辑**：表 A `rn` 读 `k.tokens_reasoning || 0`，字段已在 `_DAILY_BY_KEY_FIELDS`；本次零后端/零 schema 改动。
8. **提交**：只 `git add` 当卡涉及的两个文件；Conventional Commits，卡 1 `feat(dash): 用量表B输入输出总用量命中率列`，卡 2 `feat(dash): 用量表A输入输出总用量命中率列`。不 push。
9. **不新增依赖、不建文档、不清理无关文件。**

---

## 卡 1 — 表 B（按天×模型）10 列

**tier: A（executor）**；预计工具调用 <40。

### Step 1：测试先行（写 test.py 两处，改前先验证锚点唯一）

#### Edit T1 — 扩列断言 + 新增公式用例（`DashboardV3CardsTest`）

Edit 锚点（old_string，py 测试文件 :3352-3353，实测唯一）：

```python
        self.assertIn("<th>推理</th>", v2,
                      "token 按天×模型表缺推理列（T3）")
```

new_string（原样替换为）：

```python
        self.assertIn("<th>推理</th>", v2,
                      "token 按天×模型表缺推理列（T3）")
        self.assertIn("<th>输入</th>", v2, "token 按天×模型表缺输入合计列")
        self.assertIn("<th>输出</th>", v2, "token 按天×模型表缺输出合计列")
        self.assertIn("<th>总用量</th>", v2, "token 按天×模型表缺总用量列")
        self.assertIn("<th>缓存命中率</th>", v2, "token 按天×模型表缺缓存命中率列")

    def test_usage_aggregate_formulas(self) -> None:
        js = self.mod._DASH_JS_V2
        self.assertIn("Math.max(tp, cr)", js, "输入必须=max(prompt,cache_read) 防重复计入")
        self.assertIn("Math.max(tc, rn)", js, "输出必须=max(completion,reasoning) 防重复计入")
        self.assertIn('(cr / inp * 100).toFixed(1) + "%"', js, "命中率=cache_read/输入 1位小数")
        self.assertIn('inp > 0 ? (cr / inp * 100).toFixed(1) + "%" : "-"', js,
                      "输入为0命中率必须显示 -（0 除防护）")
        self.assertIn("var inp = Math.max(tp, cr);", js, "表B缺 inp 计算")
        for sub in ("fmtTok(inp)", "fmtTok(out)", "fmtTok(tot)"):
            self.assertIn(sub, js, "缺 fmtTok 合计列接线 %s" % sub)
```

（卡 2 会在此方法尾部追加表 A 断言、并在其后插入 `test_v3_bykey_table_columns`。）

#### Edit T2 — 全页 colspan 断言（`AdminIntegrationTest.test_dashboard_html_full_page`）

Edit 锚点（old_string，测试文件 :3651，实测唯一）：

```python
        self.assertIn('colspan="7"', html)
```

new_string：

```python
        self.assertIn('colspan="7"', html)
        self.assertIn('<tbody id="token-daily-body"><tr><td class="empty" colspan="10">', html,
                      "表B空态 colspan 必须随 10 列同步")
```

（spec #10 的 `assertIn('colspan="10"', html)` 改前已绿，按 Global Constraints 第 2 条改用 id 作用域精确断言。）

### Step 1b：红验证（贴原文）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest AdminIntegrationTest.test_dashboard_html_full_page 2>&1 | tail -30
```

预期：`FAILED (failures=3)` —— `test_v3_token_table_columns`（首个新断言 `<th>输入</th>`）、`test_usage_aggregate_formulas`（首个断言 `Math.max(tp, cr)`）、`test_dashboard_html_full_page`（tbody colspan=10）。若失败数或失败点不符 → STOP 报告，不得继续。

> PLAN-B 实测记录（红相已在 PLAN 阶段用内存镜像验证，执行者按上表复现即可）：failures=3 / errors=0，失败点与上述逐字一致。

### Step 2：生产改动（`ctyun-stream-fix-proxy.py` 三处 Edit）

#### Edit P1 — 表 B thead（py:3817，实测唯一）

old_string：

```html
      <thead><tr><th>日期</th><th>模型</th><th>提示</th><th>补全</th><th>缓存</th><th>推理</th></tr></thead>
```

new_string：

```html
      <thead><tr><th>日期</th><th>模型</th><th>输入</th><th>输出</th><th>提示</th><th>补全</th><th>缓存</th><th>推理</th><th>缓存命中率</th><th>总用量</th></tr></thead>
```

#### Edit P2 — 表 B 空态 colspan（py:3818，实测唯一）

old_string：

```html
      <tbody id="token-daily-body"><tr><td class="empty" colspan="6">读取中……</td></tr></tbody>
```

new_string：

```html
      <tbody id="token-daily-body"><tr><td class="empty" colspan="10">读取中……</td></tr></tbody>
```

#### Edit P3 — `renderTokens` td 段（py:4463-4476，实测唯一）

old_string：

```js
      var tr = el("tr");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", fmtTok(tp)));
      tr.appendChild(el("td", "num", fmtTok(tc)));
      tr.appendChild(el("td", "num", fmtTok(cr)));
      tr.appendChild(el("td", "num", fmtTok(rn)));
      body.appendChild(tr);
    }
  }
  if (!hasData) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无 token 用量 —— 上游返回 usage 帧后这里会出现记录");
    td0.colSpan = 6;
```

new_string：

```js
      var inp = Math.max(tp, cr);
      var out = Math.max(tc, rn);
      var tot = inp + out;
      var hit = inp > 0 ? (cr / inp * 100).toFixed(1) + "%" : "-";
      var tr = el("tr");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", fmtTok(inp)));
      tr.appendChild(el("td", "num", fmtTok(out)));
      tr.appendChild(el("td", "num", fmtTok(tp)));
      tr.appendChild(el("td", "num", fmtTok(tc)));
      tr.appendChild(el("td", "num", fmtTok(cr)));
      tr.appendChild(el("td", "num", fmtTok(rn)));
      tr.appendChild(el("td", "num", hit));
      tr.appendChild(el("td", "num", fmtTok(tot)));
      body.appendChild(tr);
    }
  }
  if (!hasData) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无 token 用量 —— 上游返回 usage 帧后这里会出现记录");
    td0.colSpan = 10;
```

不动项（自查 diff 确认）：`py:4451-4453` 排序键、`py:4461` 全零守卫。

### Step 3：焦点绿验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest AdminIntegrationTest.test_dashboard_html_full_page 2>&1 | tail -10
```

预期 `OK`（0 failures）。

### Step 4：卡末全量（贴 stdout 尾 + exit code）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
python3 ctyun-stream-fix-proxy.test.py > /tmp/usage-table-card1-full.log 2>&1; echo "EXIT=$?"; tail -6 /tmp/usage-table-card1-full.log
```

预期：`Ran 367 tests` / `OK` / `EXIT=0`（366 baseline + 卡 1 新增 `test_usage_aggregate_formulas` 共 367；`test_v3_bykey_table_columns` 属卡 2，届时升 368。executor 必须在报告中贴实际数字）。

### Step 5：提交

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
git commit -m "feat(dash): 用量表B输入输出总用量命中率列"
```

---

## 卡 2 — 表 A（按 API Key）11 列

**tier: A（executor）**；依赖卡 1 已合并（同 worktree 顺序执行）。预计工具调用 <40。

### Step 1：测试先行（三处 Edit）

#### Edit T3 — 表 A colspan 断言（接在卡 1 新增断言之后）

Edit 锚点（old_string，卡 1 写入后唯一）：

```python
        self.assertIn('<tbody id="token-daily-body"><tr><td class="empty" colspan="10">', html,
                      "表B空态 colspan 必须随 10 列同步")
```

new_string：

```python
        self.assertIn('<tbody id="token-daily-body"><tr><td class="empty" colspan="10">', html,
                      "表B空态 colspan 必须随 10 列同步")
        self.assertIn('<tbody id="bykey-body"><tr><td class="empty" colspan="11">', html,
                      "表A空态 colspan 必须随 11 列同步")
```

#### Edit T4 — 新增表 A 列用例（`DashboardV3CardsTest`，插在 `test_usage_aggregate_formulas` 之后）

Edit 锚点（old_string，测试文件 `test_v3_js_functions_and_wiring` 定义行，实测唯一）：

```python
    def test_v3_js_functions_and_wiring(self) -> None:
```

new_string：

```python
    def test_v3_bykey_table_columns(self) -> None:
        tables = self.mod._DASH_SECTIONS_TABLES
        for th in ("<th>输入</th>", "<th>输出</th>", "<th>总用量</th>",
                   "<th>缓存命中率</th>", "<th>提示</th>", "<th>补全</th>",
                   "<th>缓存</th>", "<th>出流量</th>", "<th>流式</th>"):
            self.assertIn(th, tables, "token 按 API Key 表缺列 %s" % th)

    def test_v3_js_functions_and_wiring(self) -> None:
```

#### Edit T5 — 追加表 A 公式断言（`test_usage_aggregate_formulas` 尾部）

Edit 锚点（old_string，卡 1 写入后唯一）：

```python
        for sub in ("fmtTok(inp)", "fmtTok(out)", "fmtTok(tot)"):
            self.assertIn(sub, js, "缺 fmtTok 合计列接线 %s" % sub)
```

new_string：

```python
        for sub in ("fmtTok(inp)", "fmtTok(out)", "fmtTok(tot)"):
            self.assertIn(sub, js, "缺 fmtTok 合计列接线 %s" % sub)
        self.assertIn("k.tokens_reasoning || 0", js, "表A输出需读 tokens_reasoning 兜底")
```

### Step 1b：红验证（贴原文）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest AdminIntegrationTest.test_dashboard_html_full_page 2>&1 | tail -30
```

预期：`FAILED (failures=3)` —— `test_v3_bykey_table_columns`（首个断言 `<th>输入</th>`）、`test_usage_aggregate_formulas`（`k.tokens_reasoning || 0`，表 B 断言已在卡 1 变绿）、`test_dashboard_html_full_page`（tbody bykey colspan=11，表 B 断言已绿）。不符 → STOP 报告。

> PLAN-B 实测记录（红相已在 PLAN 阶段用内存镜像验证）：卡 1 已应用 + 卡 2 测试写入后，单元两例 failures=2（失败点与上述一致），集成例的 `colspan="11"` 在卡 1 后全文件 0 命中（静态确认将红）。

### Step 2：生产改动（`ctyun-stream-fix-proxy.py` 四处 Edit；卡 1 后行号 +4，以下为内容锚点）

#### Edit P4 — 表 A thead（原 py:3754，实测唯一）

old_string：

```html
      <thead><tr><th>密钥</th><th>请求</th><th>提示</th><th>补全</th><th>缓存</th><th>出流量</th><th>流式</th></tr></thead>
```

new_string：

```html
      <thead><tr><th>密钥</th><th>请求</th><th>输入</th><th>输出</th><th>提示</th><th>补全</th><th>缓存</th><th>缓存命中率</th><th>总用量</th><th>出流量</th><th>流式</th></tr></thead>
```

#### Edit P5 — 表 A 空态 colspan（原 py:3755，实测唯一）

old_string：

```html
      <tbody id="bykey-body"><tr><td class="empty" colspan="7">读取中……</td></tr></tbody>
```

new_string：

```html
      <tbody id="bykey-body"><tr><td class="empty" colspan="11">读取中……</td></tr></tbody>
```

#### Edit P6 — `renderByKey` 空态 colSpan（原 py:4822-4824，实测唯一；`td0.colSpan = 7;` 全文件 6 处，必须带前两行上下文）

old_string：

```js
    var td0 = el("td", "empty",
      "暂无 per-key 数据 —— 带 Authorization 的请求经代理后这里会出现按 key 用量");
    td0.colSpan = 7;
```

new_string：

```js
    var td0 = el("td", "empty",
      "暂无 per-key 数据 —— 带 Authorization 的请求经代理后这里会出现按 key 用量");
    td0.colSpan = 11;
```

#### Edit P7 — `renderByKey` 行生成段（原 py:4837-4848，实测唯一）

old_string：

```js
  for (var i = 0; i < names.length; i++) {
    var k = keys[names[i]];
    var tr = el("tr");
    tr.appendChild(el("td", "", names[i]));
    tr.appendChild(el("td", "num", String(k.requests || 0)));
    tr.appendChild(el("td", "num", fmtTok(k.tokens_prompt || 0)));
    tr.appendChild(el("td", "num", fmtTok(k.tokens_completion || 0)));
    tr.appendChild(el("td", "num", fmtTok(k.tokens_cache_read || 0)));
    tr.appendChild(el("td", "num", String(k.bytes_out || 0)));
    tr.appendChild(el("td", "num", String(k.stream_requests || 0)));
    body.appendChild(tr);
  }
```

new_string：

```js
  for (var i = 0; i < names.length; i++) {
    var k = keys[names[i]];
    var tp = k.tokens_prompt || 0;
    var tc = k.tokens_completion || 0;
    var cr = k.tokens_cache_read || 0;
    var rn = k.tokens_reasoning || 0;
    var inp = Math.max(tp, cr);
    var out = Math.max(tc, rn);
    var tot = inp + out;
    var hit = inp > 0 ? (cr / inp * 100).toFixed(1) + "%" : "-";
    var tr = el("tr");
    tr.appendChild(el("td", "", names[i]));
    tr.appendChild(el("td", "num", String(k.requests || 0)));
    tr.appendChild(el("td", "num", fmtTok(inp)));
    tr.appendChild(el("td", "num", fmtTok(out)));
    tr.appendChild(el("td", "num", fmtTok(tp)));
    tr.appendChild(el("td", "num", fmtTok(tc)));
    tr.appendChild(el("td", "num", fmtTok(cr)));
    tr.appendChild(el("td", "num", hit));
    tr.appendChild(el("td", "num", fmtTok(tot)));
    tr.appendChild(el("td", "num", String(k.bytes_out || 0)));
    tr.appendChild(el("td", "num", String(k.stream_requests || 0)));
    body.appendChild(tr);
  }
```

不动项（自查 diff 确认）：`renderByKey` 排序键（原 py:4833-4835）、`renderZeroToken`/`renderQuota` 及其余表。

### Step 3：焦点绿验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest AdminIntegrationTest.test_dashboard_html_full_page 2>&1 | tail -10
```

预期 `OK`。

### Step 4：卡末全量（贴 stdout 尾 + exit code）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
python3 ctyun-stream-fix-proxy.test.py > /tmp/usage-table-card2-full.log 2>&1; echo "EXIT=$?"; tail -6 /tmp/usage-table-card2-full.log
```

预期：`Ran 368 tests` / `OK` / `EXIT=0`。

### Step 5：提交

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/usage-table-aggregates
git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
git commit -m "feat(dash): 用量表A输入输出总用量命中率列"
```

---

## 卡内自查清单（每卡收尾必查，executor 在报告 Observations 中逐条确认）

- [ ] diff 中无 `tokens_cache_write`、无排序键改动、无 stat 卡/标题/样式改动（Exclusions）
- [ ] `inp/out/tot/hit` 命名与文本逐字一致；`td0.colSpan` 与 thead 列数一致（B=10、A=11；行为 td 数 B=10、A=11）
- [ ] 焦点命令输出与预期失败点/通过点一致，已贴原文
- [ ] 全量 368 例 + exit code 已贴
- [ ] 只 add 当卡两个文件，commit message 与卡对应

## PLAN Self-Review（5 项，PLAN-B 实测）

1. **spec coverage**：10/10 全部落卡——#1/#2/#3→卡 1，均含逐字代码；#4/#5/#6→卡 2；#7→卡 1 T1；#8→卡 2 T4；#9→卡 1 T1 主体 + 卡 2 T5 追加；#10→卡 1 T2（表 B）+ 卡 2 T3（表 A）。
2. **placeholder scan**：0 命中——12 个 Edit 各自给出完整 old_string + new_string，无待办标记、无「略」式占位、无缩写省略。
3. **type consistency**：测试断言文本与生产改后文本逐字互校通过——`Math.max(tp, cr)`、`Math.max(tc, rn)`、`var inp = Math.max(tp, cr);`、`(cr / inp * 100).toFixed(1) + "%"`、`inp > 0 ? (cr / inp * 100).toFixed(1) + "%" : "-"`、`fmtTok(inp)`/`fmtTok(out)`/`fmtTok(tot)`、`k.tokens_reasoning || 0` 均已核对；thead 列序与 td 追加顺序一一对应（B：输入/输出/提示/补全/缓存/推理/缓存命中率/总用量；A：输入/输出/提示/补全/缓存/缓存命中率/总用量）。
4. **可落盘性（换视角：以「Edit 执行者」视角逐条验证 old_string 唯一性，实测 count）**：theadB=1、tbodyB=1、renderTokens 块=1（`td0.colSpan = 6;` 全文件 3 处，块含唯一「暂无 token 用量」行）、theadA=1、tbodyA=1、bykey 空态块=1（`td0.colSpan = 7;` 全文件 6 处，块含唯一「暂无 per-key 数据」行）、bykey 行段=1；测试锚点：推理断言块=1、`assertIn('colspan="7"', html)`=1、`def test_v3_js_functions_and_wiring`=1、卡 1 新写入的两条锚点在卡 2 时唯一（无重复文本）。同构文本 `Math.max(tp, cr)` 会同时出现在表 A/B，但本卡 Edit 全部锚定于各自唯一上下文块，不依赖该短文本定位。
5. **锚点实测（2a/2b 专项）**：2a 见 Global Constraints 第 2 条（两函数均在 `_DASH_JS_V2`，断言目标无需拆）；2b 用 repr 逐字核对，spec 改前块与实际完全一致（仅 spec 文中标注的区间端点略有出入，实际块边界 = py:4463-4476 / 4822-4824 / 4837-4848）；另发现并修正 spec #10 的 `colspan="10"` 断言改前已绿问题（改为 id 作用域断言，实测改前 count=0）。
   - 附：baseline 实测 `Ran 366 tests in 157.149s / OK`；全页实测 `colspan="6"`=3、`colspan="7"`=5、`colspan="10"`=1、`colspan="11"`=0，改动后分别余 2/4/2/1，spec #10 保留的两条 assertIn 仍绿。
   - 另附（PLAN 阶段全量落盘性模拟，内存执行未写盘）：12 个 Edit 锚点逐一实测 count=1；模拟改后两文件 `compile()` 通过、全零守卫/排序键/`cache_write` 计数（5）不变；模拟改后测试套 `loadTestsFromModule` = **368 例**（366+2，卡 1 中间态为 367）。
