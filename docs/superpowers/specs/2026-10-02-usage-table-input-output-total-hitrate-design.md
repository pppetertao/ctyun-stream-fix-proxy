R31-EXEMPT: feat-not-bugfix

# Dashboard 用量表「输入/输出/总用量/缓存命中率」扩列 — 设计 spec

## Goal

为天翼云代理 dashboard 用户在两张 token 用量表（表 A 按 API Key、表 B 按天×模型）上新增「输入 / 输出 / 总用量 / 缓存命中率」四个合计口径列，同时保留全部细列（提示/补全/缓存/推理），让用户无需手工心算合计即可直读输入输出总量与缓存效率。

## 实测代码事实（定稿口径的依据，禁止改动本节结论）

1. **usage 五元组解析**（`ctyun-stream-fix-proxy.py:336-381 usage_dict_tokens()`）：
   - `cache_read` 三级别名序 `prompt_cache_hit_tokens → cache_read_input_tokens → prompt_tokens_details.cached_tokens`（:368-373）
   - `reasoning` 两级 `completion_tokens_details.reasoning_tokens → reasoning_tokens`（:375-378）
   - 返回值 `(_int_field("prompt_tokens"), _int_field("completion_tokens"), _int_field("total_tokens"), cache_read, reasoning)`（:380-381）
2. **包含关系无法从代码静态证实**：全仓 grep 无任何注释/测试断言锁定「`prompt_tokens` 是否已含 `cache_read`」「`completion_tokens` 是否已含 `reasoning`」。测试固件 `:5297-5319` 用 `prompt_tokens=1, prompt_cache_hit_tokens=4`（cache_read > prompt_tokens）建模，**不强假设包含关系**。据此定稿口径采用**保守不重复计入**原则（见下「定稿口径」），保证无论上游是否重复，合计列都语义自洽、不随上游口径漂移而失真为负数。
3. **daily_by_key 有 reasoning 字段**（与需求待核实 #2 相反）：`_DAILY_BY_KEY_FIELDS`（:2024-2026）8 字段含 `tokens_reasoning`；落桶代码 `:2468-2471` `entry_k["tokens_reasoning"] += tokens_reasoning` 存在；快照透出 `:2636-2638`。但 `renderByKey`（:4842-4846）当前**未渲染**该列（表 A 现无「推理」列）。本次**按需求拟定的表头不为表 A 新增「推理」细列**（需求表头未含），但表 A「输出」列可直接用 `tokens_completion`（其底层 bucket 与表 B 同源，含 reasoning 与否与表 B 一致），口径差异不存在。
4. **表 B 排序逻辑**（:4451-4453）：按 `(tokens_prompt||0)+(tokens_completion||0)` 降序。本次**维持现状不改**（Exclusions）。
5. **空态 colspan**：表 B `token-daily-body` 现 `colspan="6"`（HTML :3818 + JS :4476）；表 A `bykey-body` 现 `colspan="7"`（HTML :3755 + JS :4824）。两处必须随列数同步。
6. **`fmtTok`**（:4891-4895）：`n==null||!isFinite(n)→"—"`，`n>=1e6→(n/1e6).toFixed(2)+"M"`，否则 `String(Math.round(n))`。合计列复用。
7. **测试现状**：
   - `test_v3_token_table_columns`（test.py:3348-3353）：断言 `<th>缓存</th>` `<th>推理</th>` 在 `_DASH_SECTIONS_V2`——本次保留，需追加新列断言。
   - `test_dashboard_html_full_page`（:3619+）：含 `colspan="6"`（:3645）/`colspan="7"`（:3651）计数断言——`colspan="6"`/`colspan="7"` 在多处出现（见 :3720/:3729/:3837/:3855 等），改动后这些断言的命中数会变，需复核。
   - `test_fmt_tok_call_sites`（:8642-8646）：锁 `fmtTok(tp/tc/cr/rn/mtd)` 调用点——新列调用点需追加。

## 定稿口径（写死，实现不得偏离）

设某行的桶字段：`tp=tokens_prompt||0`、`tc=tokens_completion||0`、`cr=tokens_cache_read||0`、`rn=tokens_reasoning||0`。

- **输入** `inp = max(tp, cr)` —— 若上游 `prompt_tokens` 已含 cache（OpenAI 惯例），`tp>=cr` 取 `tp` 不重复计；若上游 `prompt_tokens` 仅计未缓存部分，则 `cr>tp` 取 `cr` 兜底不丢量。**两种上游口径下均不重复计入、不为负。**
- **输出** `out = max(tc, rn)` —— 同理：若 `completion_tokens` 已含 reasoning（OpenAI 惯例）取 `tc`；若仅计可见补全则 `rn>tc` 取 `rn` 兜底。
- **总用量** `tot = inp + out`。
- **缓存命中率** `hit = cr / inp`，百分比 1 位小数（`(cr/inp*100).toFixed(1)+"%"`）；`inp===0` 或该行无数据显示 `"-"`。`inp===0` 当且仅当 `tp===0 && cr===0`。

> 说明：表 A 与表 B 底层桶同源（`daily_by_key`/`daily_by_model` 的 `tokens_prompt/tokens_completion/tokens_cache_read/tokens_reasoning` 字段名完全一致），故两表共用同一套口径公式，无维度差异。

## Files to Change

### `ctyun-stream-fix-proxy.py`（后端 + 内嵌 dashboard，单文件）

**1. 表 B thead（:3817，`_DASH_SECTIONS_V2`）** — 6 列 → 10 列。
改前：
```html
      <thead><tr><th>日期</th><th>模型</th><th>提示</th><th>补全</th><th>缓存</th><th>推理</th></tr></thead>
```
改后：
```html
      <thead><tr><th>日期</th><th>模型</th><th>输入</th><th>输出</th><th>提示</th><th>补全</th><th>缓存</th><th>推理</th><th>缓存命中率</th><th>总用量</th></tr></thead>
```

**2. 表 B 空态 colspan（:3818）** — `colspan="6"` → `colspan="10"`。
改前：
```html
      <tbody id="token-daily-body"><tr><td class="empty" colspan="6">读取中……</td></tr></tbody>
```
改后：
```html
      <tbody id="token-daily-body"><tr><td class="empty" colspan="10">读取中……</td></tr></tbody>
```

**3. 表 B `renderTokens` td 生成段（:4463-4479）** — 6 td → 10 td + 空态 colSpan 6→10。
改前（:4463-4479）：
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
改后（在 `:4460` `var rn = ...` 之后、`:4463` `var tr` 之前插入 `inp/out/tot/hit` 计算，td 段重写，colSpan 6→10）：
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
（`:4461` 全零跳过守卫 `if (tp + tc + cr + rn === 0) continue;` 与 `:4451-4453` 排序键**保持原样不动**。）

**4. 表 A thead（:3754，`_DASH_SECTIONS_TABLES`）** — 7 列 → 11 列。
改前：
```html
      <thead><tr><th>密钥</th><th>请求</th><th>提示</th><th>补全</th><th>缓存</th><th>出流量</th><th>流式</th></tr></thead>
```
改后：
```html
      <thead><tr><th>密钥</th><th>请求</th><th>输入</th><th>输出</th><th>提示</th><th>补全</th><th>缓存</th><th>缓存命中率</th><th>总用量</th><th>出流量</th><th>流式</th></tr></thead>
```
（表 A 按需求表头**不加「推理」细列**；「输出」=`max(tc,rn)`，`rn` 读 `k.tokens_reasoning||0`，字段在 `_DAILY_BY_KEY_FIELDS` 已存在，无需后端改动。）

**5. 表 A 空态 colspan（:3755）** — `colspan="7"` → `colspan="11"`。
改前：
```html
      <tbody id="bykey-body"><tr><td class="empty" colspan="7">读取中……</td></tr></tbody>
```
改后：
```html
      <tbody id="bykey-body"><tr><td class="empty" colspan="11">读取中……</td></tr></tbody>
```

**6. 表 A `renderByKey` 空态 colSpan + td 生成段（:4824、:4837-4848）** — 7 td → 11 td + 空态 colSpan 7→11。
改前（:4820-4828 空态段 与 :4837-4848 行段）：
```js
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty",
      "暂无 per-key 数据 —— 带 Authorization 的请求经代理后这里会出现按 key 用量");
    td0.colSpan = 7;
```
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
改后（空态 colSpan 7→11；行段提局部变量 + 插入 inp/out/tot/hit + 追加 4 td）：
```js
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty",
      "暂无 per-key 数据 —— 带 Authorization 的请求经代理后这里会出现按 key 用量");
    td0.colSpan = 11;
```
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
（`:4833-4835` 表 A 排序键 `tokens_prompt+tokens_completion` 降序**保持原样不动**。）

### `ctyun-stream-fix-proxy.test.py`（TDD 断言）

**7. `test_v3_token_table_columns`（:3348-3353）** — 追加表 B 新列断言（保留 `<th>缓存</th>` `<th>推理</th>` 原有 2 条）。
在 `:3353` 后追加：
```python
        self.assertIn("<th>输入</th>", v2, "token 按天×模型表缺输入合计列")
        self.assertIn("<th>输出</th>", v2, "token 按天×模型表缺输出合计列")
        self.assertIn("<th>总用量</th>", v2, "token 按天×模型表缺总用量列")
        self.assertIn("<th>缓存命中率</th>", v2, "token 按天×模型表缺缓存命中率列")
```
**8. 新增 `test_v3_bykey_table_columns`（紧邻 :3353 之后）** — 表 A 新列 + 保留列断言：
```python
    def test_v3_bykey_table_columns(self) -> None:
        tables = self.mod._DASH_SECTIONS_TABLES
        for th in ("<th>输入</th>", "<th>输出</th>", "<th>总用量</th>",
                   "<th>缓存命中率</th>", "<th>提示</th>", "<th>补全</th>",
                   "<th>缓存</th>", "<th>出流量</th>", "<th>流式</th>"):
            self.assertIn(th, tables, "token 按 API Key 表缺列 %s" % th)
```
**9. 新增 `test_usage_aggregate_formulas`（同 class 内）** — 口径公式 + 0 除 `-` 断言（锁 JS 段文本）：
```python
    def test_usage_aggregate_formulas(self) -> None:
        js = self.mod._DASH_JS_V2
        self.assertIn("Math.max(tp, cr)", js, "输入必须=max(prompt,cache_read) 防重复计入")
        self.assertIn("Math.max(tc, rn)", js, "输出必须=max(completion,reasoning) 防重复计入")
        self.assertIn('(cr / inp * 100).toFixed(1) + "%"', js, "命中率=cache_read/输入 1位小数")
        self.assertIn('inp > 0 ? (cr / inp * 100).toFixed(1) + "%" : "-"', js,
                      "输入为0命中率必须显示 -（0 除防护）")
        self.assertIn("var inp = Math.max(tp, cr);", js, "表B缺 inp 计算")
        self.assertIn("k.tokens_reasoning || 0", js, "表A输出需读 tokens_reasoning 兜底")
        for sub in ("fmtTok(inp)", "fmtTok(out)", "fmtTok(tot)"):
            self.assertIn(sub, js, "缺 fmtTok 合计列接线 %s" % sub)
```
**10. `colspan` 计数断言复核（:3645、:3651）** — `test_dashboard_html_full_page` 中 `self.assertIn('colspan="6"', html)`（:3645）与 `self.assertIn('colspan="7"', html)`（:3651）：表 B `colspan="6"`→`"10"`、表 A `colspan="7"`→`"11"` 后，`"6"` 仍有 `daily-body`(:3720)/`err-events-body`(:3873) 命中、`"7"` 仍有 `daily-model-body`(:3729)/`phase-model-body`(:3837)/`stability-body`(:3855)/`tpm-obs-body`(:3864) 命中，两条 `assertIn` 仍通过**无需改**；但需**追加**：
```python
        self.assertIn('colspan="10"', html, "表B空态 colspan 必须随 10 列同步")
        self.assertIn('colspan="11"', html, "表A空态 colspan 必须随 11 列同步")
```

## Acceptance Criteria

- [ ] 焦点测试通过：`python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest`（覆盖 :3348 扩列断言 + 新增 8/9 用例）
- [ ] 全量 baseline 绿：`python3 ctyun-stream-fix-proxy.test.py`（366 例全绿，stderr 无意外错误）
- [ ] 表 B 渲染 10 列：启动 dashboard 后 perf tab「Token 用量按天×模型」表头为 `日期|模型|输入|输出|提示|补全|缓存|推理|缓存命中率|总用量`，空态 td `colspan="10"`
- [ ] 表 A 渲染 11 列：overview tab「Token 用量按 API Key」表头为 `密钥|请求|输入|输出|提示|补全|缓存|缓存命中率|总用量|出流量|流式`，空态 td `colspan="11"`
- [ ] 口径正确：任一非零行 `输入=max(提示,缓存)`、`输出=max(补全,推理)`、`总用量=输入+输出`、`缓存命中率=缓存/输入`（1 位小数 %）；`提示=缓存=0` 行命中率列显示 `-`
- [ ] 排序行为不变：表 B 仍按 `tokens_prompt+tokens_completion` 降序、表 A 仍按 `tokens_prompt+tokens_completion` 降序（diff 中 :4451-4453 与 :4833-4835 无改动）

## Risks

- **包含关系上游漂移**：`max()` 口径在「上游 `prompt_tokens` 已含 cache」与「仅计未缓存」两种形态下都不重复计入，但若上游某模型 `prompt_tokens` 语义为「非缓存+缓存分开但 prompt 已含 cache 且 cache 单独再报」的混合形态，`max` 仍取大者不重复——已是最保守安全选择；唯一残余风险是「输入」在「prompt 已含 cache」场景下语义为「总输入」而非「新增输入」，但列名「输入」本身即指总输入口径，语义自洽。
- **`colspan` 断言脆性**：`test_dashboard_html_full_page` 用 `assertIn('colspan="6"')`/`'colspan="7"'` 做存在性断言（非精确计数），多张表共用同值，本次改动后仍通过（见 Files#10）；但若未来其它表列数变化致 `"6"`/`"7"` 全消失，这两条会误报——属既有测试设计，不在本次 scope 修。
- **JS 文本断言耦合**：`test_usage_aggregate_formulas`/`test_fmt_tok_call_sites` 用 `assertIn` 锁 JS 源码文本，格式化/重命名局部变量会破坏断言——实现时变量名必须严格用 `inp/out/tot/hit`，公式文本逐字匹配。
- **表 A 无「推理」细列但「输出」含 rn**：表 A「输出」=`max(补全,推理)` 但不展示「推理」细列，用户无法从表 A 反推 reasoning 占比——这是需求表头的明确取舍（需求表 A 表头未含推理），非缺陷；口径已在表 B 完整呈现。

## Exclusions

- **排序键不改**：表 B `:4451-4453`、表 A `:4833-4835` 排序逻辑（`tokens_prompt+tokens_completion` 降序）维持现状。
- **stat 卡不改**：0-token 聚合卡（`renderZeroToken` :4850）、月末投影卡（`renderQuota` :4864，口径 `prompt+completion`）均不动。
- **卡片标题不改**：`Token 用量按 API Key`/`Token 用量按天×模型` 等标题文本零改动。
- **部署不做**：仅改代码与测试，不触发部署/重启/webhook。
- **列宽/样式不改**：不新增 CSS、不改 `.table-wrap`/`td.num` 样式、不调列宽。
- **cache_write 不解析**：沿用 v3 决策，`tokens_cache_write` 占位字段不在任何表渲染。
- **表 A 不加「推理」细列**：按需求表头定稿，表 A 仅 11 列（无独立推理列）。
- **其它表不改**：`daily-body`/`daily-model-body`/`perf-model-body`/`stability-body` 等其余所有表格零改动。
