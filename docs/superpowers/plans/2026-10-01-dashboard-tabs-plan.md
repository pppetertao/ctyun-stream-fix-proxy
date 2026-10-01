# PLAN — Dashboard Tab 分页重构（P6）

> **来源 spec**：`docs/superpowers/specs/2026-10-01-dashboard-tabs-design.md`
> **worktree**：`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/dashboard-tabs`（分支 `fix/dashboard-tabs`）
> **总卡数**：2（全 tier A——代码全部在本计划写死，executor 直接转写）
> **卡序**：卡 1 失败测试（RED）→ 卡 2 实现（GREEN + whole-branch verify）

## Global Constraints

1. **TDD 铁律**：卡 1 只编辑 `ctyun-stream-fix-proxy.test.py`，卡 2 只编辑 `ctyun-stream-fix-proxy.py`。卡 1 所有新增测试必须先 RED，卡 2 让它们 GREEN + 无回归。
2. **baseline**：本 PLAN 写作时已实测 baseline：
   ```
   $ python3 ctyun-stream-fix-proxy.test.py
   Ran 248 tests in 121.185s
   OK
   ```
   卡 1 验证前 executor 复跑一次 baseline 确认 248 OK（若 baseline 本身 RED 则暂停上报，不继续卡 1）。
3. **verify=全量测试**：项目无 lint/build 配置文件 — verify 就是 `python3 ctyun-stream-fix-proxy.test.py`。每卡验证跑全量。
4. **文件边界**：worktree 内唯一修改两个文件：
   - `ctyun-stream-fix-proxy.py`（生产）
   - `ctyun-stream-fix-proxy.test.py`（测试）
   不碰其他任何文件。
5. **卡 1 RED 预期**：253 测试，6 失败（具体清单见卡 1），其余 247 绿。exit code 1 **是预期结果**，不是卡点。
6. **卡 2 GREEN 预期**：全部 253 测试通过，exit code 0。

## 开放决策（spec 留给 implementer 的唯一开放项，本计划定死）

spec 决策 7「implementer 决定是新增两常量还是合并进相邻段」→ **选「合并进相邻段」，不新增常量**。

| 内容 | 合并目标 | 理由 |
|------|---------|------|
| tab CSS（`.tab-bar` / `.tab` / `.tab-pane` / `[hidden]`） | `_DASH_HEAD`（`</style>` 之前） | 同段已含全部 CSS，保持 `_DASH_HEAD` 为单一样式入口；独立 `_DASH_CSS_TABS` 需 join 时 split/insert `</style>` 破坏纯拼接模式 |
| tab-bar nav（4 按钮） | `_DASH_SECTIONS_STATIC`（`<main>` 后第一条） | nav 是 main 直接子元素，与 settings/overview pane 同段天然内聚 |
| initTabs JS | `_DASH_JS_CORE`（`loadTpmSettings();` 之后） | `_DASH_JS_CORE` 已含全部页面初始化逻辑 |

**影响**：`DASHBOARD_HTML` join（6 段顺序）不变 → `test_segment_constants_present_and_join` 和 `test_no_inner_html_anywhere` 常量 tuple 零改动。spec 原文「join 等式断言必须同步更新」的前置条件是「若 implementer 选新增常量路径」——选合并路径则等式不变。

**连带锁定决策**（spec 未显式开放但需本计划定调）：

- **tri-state HTML 物理移入 `_DASH_SECTIONS_TABLES`**（overview pane 内，daily×model 之后）：overview pane 在 TABLES 段闭合（spec 决策 2「闭合 `</div>` 落在 TABLES 段内」），且总 pane 数必须 = 4（一个数据组一个 div），无法让 tri-state 留在 V2 又共享 overview pane。`test_v2_containers_present` 的 V2 断言 tuple 去掉 `tri-state-card`，改在 TABLES 中落位断言。
- **Pane 容器 id**：`pane-overview` / `pane-requests` / `pane-perf` / `pane-settings`；tab 按钮 id：`tab-overview` 等同名。aria-controls / aria-labelledby 互指。
- **新增 `.tab-pane { display:grid; gap:12px; }`**：原来 `<main>` 是 grid、section 是 grid item 享 12px 间距；包 pane 后 section 落入 pane 块流→间距丢失。让 pane 也是 grid 保持 12px 间距—视觉零回归。
- **初始 tab 应用不写 hash/localStorage**：`applyTab(key, persist)` 的 `persist` 参数区分；初始调 `applyTab(initial, false)`，切换/click/keydown/hashchange → `true`。符合 spec 决策 4「切换 tab 时同步写」。
- **localStorage 写失败 try/catch + 会话内存降级**：`readStoredTab()` → `dashTabMem` 回退；`storeTab()` → catch 内 `dashTabMem = key` + 注释自证三要件（SecurityError 命名 + try 单语句 + 用户无持久化诉求可容忍）。

---

## Task 1: TDD 失败测试卡（tier A）

### 目标

在 `ctyun-stream-fix-proxy.test.py` 中新增 `DashboardTabsSkeletonTest` 类（5 个骨架测试），更新 `test_v2_containers_present`（tri-state 移入 TABLES），在 `test_dashboard_html_full_page` 尾部追加 tab 断言。全部新断言对应 spec Acceptance Criteria，当前基线不含任何 tab 结构→新增测试必 RED。

### 文件

`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/dashboard-tabs/ctyun-stream-fix-proxy.test.py`

### 改动 1 — 新增 `DashboardTabsSkeletonTest` 类（在 `DashboardV2SkeletonTest` 之后、`AdminIntegrationTest` 之前插入）

**old_string**（锚：`DashboardV2SkeletonTest` 末行 + 空白 + `AdminIntegrationTest` 类头）：
```
        self.assertIn('markEvents(chip, "probe_alert", null, null)', js)


class AdminIntegrationTest(unittest.TestCase):
```

**new_string**：
```
        self.assertIn('markEvents(chip, "probe_alert", null, null)', js)


class DashboardTabsSkeletonTest(unittest.TestCase):
    """P6：页面级 tab 分页骨架（模块级常量断言，无需子进程）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_tab_bar_present(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertIn('<nav class="tab-bar"', html)
        self.assertIn('role="tablist"', html)
        for key in ("overview", "requests", "perf", "settings"):
            self.assertIn('data-tab="%s"' % key, html,
                          "tab-bar 缺 data-tab=%s" % key)
            self.assertIn('aria-controls="pane-%s"' % key, html,
                          "tab %s 缺 aria-controls" % key)

    def test_pane_mapping(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertEqual(html.count('class="tab-pane"'), 4,
                         "pane 包裹 div 必须恰好 4 个")
        pane_order = ("settings", "overview", "requests", "perf")
        starts = []
        for name in pane_order:
            pos = html.find('data-pane="%s"' % name)
            self.assertNotEqual(pos, -1, "缺 pane %s" % name)
            starts.append(pos)
        self.assertEqual(starts, sorted(starts),
                         "pane 须按 settings/overview/requests/perf 顺序出现（%r）"
                         % starts)
        main_end = html.find("</main>")
        boundaries = starts[1:] + [main_end if main_end != -1 else len(html)]
        pane_ids = {
            "settings": ("upstream-form", "tpm-models"),
            "overview": ("st-requests", "daily-body", "daily-model-body",
                         "tri-state-card"),
            "requests": ("poison-strip", "req-body"),
            "perf": ("upstream-health", "perf-model-body", "latency-dist",
                     "token-daily-body"),
        }
        for name, start, end in zip(pane_order, starts, boundaries):
            seg = html[start:end]
            for cid in pane_ids[name]:
                self.assertIn('id="%s"' % cid, seg,
                              "id=%s 未落在 pane %s" % (cid, name))

    def test_initial_hidden_state(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        tags = re.findall(r'<div class="tab-pane" data-pane="[a-z]+"[^>]*>',
                          html)
        self.assertEqual(len(tags), 4,
                         "初始 HTML 应有 4 个 pane 开标签，实得 %d" % len(tags))
        for tag in tags:
            if 'data-pane="overview"' in tag:
                self.assertNotIn("hidden", tag,
                                 "overview pane 初始不得 hidden：%s" % tag)
            else:
                self.assertIn("hidden", tag,
                              "非 overview pane 初始必须 hidden：%s" % tag)

    def test_tab_js_wiring(self) -> None:
        js = self.mod._DASH_JS_CORE
        for token in ("initTabs", "localStorage", "location.hash",
                      "hashchange", "aria-selected", "history.replaceState"):
            self.assertIn(token, js, "_DASH_JS_CORE 缺 tab 接线 %s" % token)

    def test_no_regression_existing_ids(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        for cid in ("daily-body", "daily-model-body", "req-body",
                    "poison-strip", "tpm-models", "tpm-save",
                    "upstream-form", "upstream-health", "perf-model-body",
                    "latency-dist", "token-daily-body", "tri-state-card",
                    "spark", "evt-tip"):
            self.assertEqual(html.count('id="%s"' % cid), 1,
                             "id=%s 应恰好出现 1 次（防 pane 包裹复制/丢段），实得 %d"
                             % (cid, html.count('id="%s"' % cid)))


class AdminIntegrationTest(unittest.TestCase):
```

### 改动 2 — `test_v2_containers_present`：tri-state 迁出 V2、断言落位 TABLES

**old_string**：
```
        for cid in ("perf-model-body", "token-daily-body", "upstream-health",
                    "tri-state-card", "latency-dist"):
            self.assertIn('id="%s"' % cid, v2, "V2 区块缺容器 %s" % cid)
```

**new_string**：
```
        for cid in ("perf-model-body", "token-daily-body", "upstream-health",
                    "latency-dist"):
            self.assertIn('id="%s"' % cid, v2, "V2 区块缺容器 %s" % cid)
        # P6：tri-state-card 移入 overview pane（落在 _DASH_SECTIONS_TABLES 段内）——
        # 全页落位由 DashboardTabsSkeletonTest.test_pane_mapping /
        # test_no_regression_existing_ids 守护
        self.assertIn('id="tri-state-card"', self.mod._DASH_SECTIONS_TABLES,
                      "tri-state-card 应移入 _DASH_SECTIONS_TABLES")
```

### 改动 3 — `test_dashboard_html_full_page` 尾部追加 tab 断言

**old_string**（方法末行 + 空行 + 下一个方法）：
```
        self.assertIn("probe_alert", html)

    def test_favicon_served(self) -> None:
```

**new_string**：
```
        self.assertIn("probe_alert", html)
        # P6：页面级 tab 分页（tab-bar / 4 pane / settings pane / initTabs JS）
        self.assertIn('<nav class="tab-bar"', html)
        self.assertEqual(html.count('class="tab-pane"'), 4)
        self.assertIn('data-pane="settings"', html)
        self.assertIn("initTabs", html)
        self.assertIn("localStorage", html)

    def test_favicon_served(self) -> None:
```

### 验证命令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/dashboard-tabs
python3 ctyun-stream-fix-proxy.test.py
```

### 预期 RED 结果

- exit code：1
- 总测试数：253（248 原有 + 5 新增）
- **失败：6**
  1. `DashboardTabsSkeletonTest.test_tab_bar_present` — 当前 HTML 无 `<nav class="tab-bar"`
  2. `DashboardTabsSkeletonTest.test_pane_mapping` — `count('class="tab-pane"')` = 0 ≠ 4
  3. `DashboardTabsSkeletonTest.test_initial_hidden_state` — `re.findall` 匹配 0 个 pane 开标签 ≠ 4
  4. `DashboardTabsSkeletonTest.test_tab_js_wiring` — `_DASH_JS_CORE` 无 `initTabs` 等
  5. `DashboardV2SkeletonTest.test_v2_containers_present` — `tri-state-card` 不在 `_DASH_SECTIONS_TABLES`（基线仍在 V2）
  6. `AdminIntegrationTest.test_dashboard_html_full_page` — `<nav class="tab-bar"` 断言失败
- **通过：247**（含 `DashboardTabsSkeletonTest.test_no_regression_existing_ids` 绿——基线 id 出现次数均为 1）

---

## Task 2: 实现卡（tier A）

### 目标

修改 `ctyun-stream-fix-proxy.py` 的 5 个常量（HEAD/STATIC/TABLES/V2/JS_CORE），实现全部 8 项设计决策。所有卡 1 的 RED 测试变 GREEN，全量 253 测试 0 失败。DASHBOARD_HTML join 不变。

### 文件

`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/dashboard-tabs/ctyun-stream-fix-proxy.py`

### 改动 P1 — `_DASH_HEAD` 在 `</style>` 前追加 tab CSS

**old_string**：
```
@media (max-width:720px) {
  .stat .num { font-size:24px; }
  main { padding:12px; }
}
</style>
```

**new_string**：
```
@media (max-width:720px) {
  .stat .num { font-size:24px; }
  main { padding:12px; }
}
/* P6：页面级 tab 分页——tab-bar 横向条 / pane 网格间距 / 显隐 / 移动端防压缩 */
.tab-bar { display:flex; gap:8px; overflow-x:auto; flex-wrap:nowrap;
  -webkit-overflow-scrolling:touch; padding-bottom:4px; }
.tab { font-weight:400; font-size:12.5px; background:transparent; color:var(--dim);
  border:1px solid var(--line); border-radius:99px; padding:4px 14px;
  cursor:pointer; white-space:nowrap; }
.tab:hover { filter:none; color:var(--text); }
.tab.active { background:var(--amber); color:var(--ink); border-color:var(--amber); font-weight:700; }
.tab-pane { display:grid; gap:12px; }
[hidden] { display:none !important; }
@media (max-width:720px) {
  .tab { flex:0 0 auto; }
}
</style>
```

### 改动 P2 — `_DASH_SECTIONS_STATIC`：tab-bar nav + settings pane + overview pane 开

**old_string**（完整常量替换）：
```
_DASH_SECTIONS_STATIC = """<main>
  <section class="card">
    <div class="card-title">上游端点<span class="chip" id="upstream-source">--</span></div>
    <div class="upstream-url dimmed" id="upstream-base">读取中……</div>
    <form id="upstream-form">
      <input id="upstream-input" type="text" autocomplete="off" spellcheck="false"
             placeholder="https://eaichat.ctyun.cn/ai/platform/v2/cp" aria-label="新的上游端点 URL">
      <button type="submit">保存上游端点</button>
    </form>
    <p class="msg" id="upstream-msg" role="status"></p>
  </section>
  <section class="card" id="tpm-settings-card">
    <div class="card-title">TPM 限流设置（勾选启用的模型）</div>
    <p class="dimmed">仅勾选的模型走限流（桶/排队/429），其余模型直通；预算档位由上游拒绝实证推荐。加载中……</p>
    <div id="tpm-models"></div>
    <div class="actions">
      <button id="tpm-save" type="button">保存限流设置</button>
      <p class="msg" id="tpm-msg" role="status"></p>
    </div>
  </section>
  <section class="card range-tabs" role="tablist" aria-label="统计时间维度">
    <button type="button" class="range-tab" role="tab" data-range="3d">近3天</button>
    <button type="button" class="range-tab" role="tab" data-range="7d">近7天</button>
    <button type="button" class="range-tab" role="tab" data-range="mtd">本月</button>
    <button type="button" class="range-tab" role="tab" data-range="last_month">上月</button>
  </section>
  <section class="stats-row">
    <div class="card stat"><div class="num" id="st-requests">--</div><div class="label">请求数</div></div>
    <div class="card stat"><div class="num amber" id="st-filtered">--</div><div class="label">剥行（所选时段）</div></div>
    <div class="card stat"><div class="num" id="st-rate">--</div><div class="label">毒行率</div></div>
    <div class="card stat"><div class="num" id="st-active">--</div><div class="label">活跃连接·实时</div></div>
    <div class="card stat"><div class="num" id="st-errors">--</div><div class="label">错误数</div></div>
  </section>
  <section class="card">
    <div class="card-title">请求节奏 · 最近 10 分钟（琥珀=请求，红=剥行）</div>
    <svg id="spark" viewBox="0 0 600 64" preserveAspectRatio="none" role="img" aria-label="最近 10 分钟请求柱状图"></svg>
  </section>
"""
```

**new_string**：
```
_DASH_SECTIONS_STATIC = """<main>
<!-- P6 pane 包裹说明：settings 在本段开闭；overview 在本段开、在 _DASH_SECTIONS_TABLES
     段内闭（跨段闭合，改排布前先核对配对注释 "pane X 开/闭"）。 -->
<nav class="tab-bar" role="tablist" aria-label="运行台分页">
  <button type="button" class="tab active" role="tab" data-tab="overview" id="tab-overview"
          aria-selected="true" aria-controls="pane-overview" tabindex="0">监控概览</button>
  <button type="button" class="tab" role="tab" data-tab="requests" id="tab-requests"
          aria-selected="false" aria-controls="pane-requests" tabindex="-1">请求与剥行</button>
  <button type="button" class="tab" role="tab" data-tab="perf" id="tab-perf"
          aria-selected="false" aria-controls="pane-perf" tabindex="-1">性能与健康</button>
  <button type="button" class="tab" role="tab" data-tab="settings" id="tab-settings"
          aria-selected="false" aria-controls="pane-settings" tabindex="-1">设置</button>
</nav>
  <!-- pane settings 开 -->
  <div class="tab-pane" data-pane="settings" role="tabpanel" id="pane-settings"
       aria-labelledby="tab-settings" hidden>
  <section class="card">
    <div class="card-title">上游端点<span class="chip" id="upstream-source">--</span></div>
    <div class="upstream-url dimmed" id="upstream-base">读取中……</div>
    <form id="upstream-form">
      <input id="upstream-input" type="text" autocomplete="off" spellcheck="false"
             placeholder="https://eaichat.ctyun.cn/ai/platform/v2/cp" aria-label="新的上游端点 URL">
      <button type="submit">保存上游端点</button>
    </form>
    <p class="msg" id="upstream-msg" role="status"></p>
  </section>
  <section class="card" id="tpm-settings-card">
    <div class="card-title">TPM 限流设置（勾选启用的模型）</div>
    <p class="dimmed">仅勾选的模型走限流（桶/排队/429），其余模型直通；预算档位由上游拒绝实证推荐。加载中……</p>
    <div id="tpm-models"></div>
    <div class="actions">
      <button id="tpm-save" type="button">保存限流设置</button>
      <p class="msg" id="tpm-msg" role="status"></p>
    </div>
  </section>
  </div>
  <!-- pane settings 闭 -->
  <!-- pane overview 开（闭合见 _DASH_SECTIONS_TABLES 段尾） -->
  <div class="tab-pane" data-pane="overview" role="tabpanel" id="pane-overview"
       aria-labelledby="tab-overview">
  <section class="card range-tabs" role="tablist" aria-label="统计时间维度">
    <button type="button" class="range-tab" role="tab" data-range="3d">近3天</button>
    <button type="button" class="range-tab" role="tab" data-range="7d">近7天</button>
    <button type="button" class="range-tab" role="tab" data-range="mtd">本月</button>
    <button type="button" class="range-tab" role="tab" data-range="last_month">上月</button>
  </section>
  <section class="stats-row">
    <div class="card stat"><div class="num" id="st-requests">--</div><div class="label">请求数</div></div>
    <div class="card stat"><div class="num amber" id="st-filtered">--</div><div class="label">剥行（所选时段）</div></div>
    <div class="card stat"><div class="num" id="st-rate">--</div><div class="label">毒行率</div></div>
    <div class="card stat"><div class="num" id="st-active">--</div><div class="label">活跃连接·实时</div></div>
    <div class="card stat"><div class="num" id="st-errors">--</div><div class="label">错误数</div></div>
  </section>
  <section class="card">
    <div class="card-title">请求节奏 · 最近 10 分钟（琥珀=请求，红=剥行）</div>
    <svg id="spark" viewBox="0 0 600 64" preserveAspectRatio="none" role="img" aria-label="最近 10 分钟请求柱状图"></svg>
  </section>
"""
```

**注意**：
1. STATIC new_string 尾部（spark section 后）**不带** `</div>`——overview pane 跨段，闭合落在 TABLES 段内（P3）。
2. 按「不改 section 本体」原则：所有既有 section 保持与 old 完全相同的字节（含 2-space 缩进），仅在其上下插入 pane div（2-space）与 `<!-- pane X 开/闭 -->` 配对注释。pane div 与 section 同级缩进不影响 HTML 合法性，且保证 section 零内部回归面。

### 改动 P3 — `_DASH_SECTIONS_TABLES`：overview 续 + 闭合 + tri-state 移入 + requests pane

**old_string**（完整常量替换）：
```
_DASH_SECTIONS_TABLES = """  <section class="card">
    <div class="card-title">按天统计（<span id="daily-title-range">近7天</span>，新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-body"><tr><td class="empty" colspan="6">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">按天 × 模型（<span id="daily-model-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-model-body"><tr><td class="empty" colspan="7">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">剥行流带 · 最近剥除的毒 record</div>
    <div class="strip" id="poison-strip"><span class="empty">读取中……</span></div>
  </section>
  <section class="card">
    <div class="card-title">最近请求（新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>时间</th><th>方法</th><th>路径</th><th>模型</th><th>状态</th><th>耗时</th><th>剥行</th></tr></thead>
      <tbody id="req-body"></tbody>
    </table>
    </div>
  </section>
"""
```

**new_string**：
```
_DASH_SECTIONS_TABLES = """  <section class="card">
    <div class="card-title">按天统计（<span id="daily-title-range">近7天</span>，新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-body"><tr><td class="empty" colspan="6">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">按天 × 模型（<span id="daily-model-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-model-body"><tr><td class="empty" colspan="7">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <!-- P6：tri-state-card 自 _DASH_SECTIONS_V2 移入 overview pane（pane 开自 _DASH_SECTIONS_STATIC） -->
  <section class="card" id="tri-state-card">
    <div class="card-title">三态可用率 · 所选时段（<span id="tri-state-range">近7天</span>）</div>
    <div class="tri-bar" style="display:flex;height:14px;border-radius:4px;overflow:hidden;gap:2px"
         role="img" aria-label="ok / degraded / failed 占比条"></div>
    <div id="tri-state-legend" style="color:var(--dim);font-size:12px;margin-top:8px"></div>
  </section>
  </div>
  <!-- pane overview 闭（开自 _DASH_SECTIONS_STATIC，跨段闭合勿误切配对） -->
  <!-- pane requests 开 -->
  <div class="tab-pane" data-pane="requests" role="tabpanel" id="pane-requests"
       aria-labelledby="tab-requests" hidden>
  <section class="card">
    <div class="card-title">剥行流带 · 最近剥除的毒 record</div>
    <div class="strip" id="poison-strip"><span class="empty">读取中……</span></div>
  </section>
  <section class="card">
    <div class="card-title">最近请求（新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>时间</th><th>方法</th><th>路径</th><th>模型</th><th>状态</th><th>耗时</th><th>剥行</th></tr></thead>
      <tbody id="req-body"></tbody>
    </table>
    </div>
  </section>
  </div>
  <!-- pane requests 闭 -->
"""
```

**注意**：tri-state HTML（共 5 行：section 头 + tri-bar + legend + div 闭 + section 尾）与原有 `_DASH_SECTIONS_V2` 中的 tri-state 内容逐字相同；仅上下文从 V2 常量物理移入 TABLES。

### 改动 P4 — `_DASH_SECTIONS_V2`：perf pane 包裹 + 移除 tri-state

**old_string**（完整常量替换）：
```
_DASH_SECTIONS_V2 = """  <section class="card">
    <div class="card-title">上游健康<span class="chip" id="health-status">--</span></div>
    <table>
      <tbody id="upstream-health"><tr><td class="empty" colspan="2">读取中……</td></tr></tbody>
    </table>
  </section>
  <section class="card">
    <div class="card-title">模型速度对比 · TTFB P50/P90（进程内累计，最快在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>模型</th><th>P50</th><th>P90</th></tr></thead>
      <tbody id="perf-model-body"><tr><td class="empty" colspan="3">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">延迟分布 · 三阶段 P50（连接 / 响应头 / 数据体）</div>
    <svg id="latency-dist" viewBox="0 0 600 96" preserveAspectRatio="none" role="img"
         aria-label="阶段延迟 P50 条形图"></svg>
  </section>
  <section class="card">
    <div class="card-title">Token 用量按天 × 模型（<span id="token-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th></tr></thead>
      <tbody id="token-daily-body"><tr><td class="empty" colspan="4">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card" id="tri-state-card">
    <div class="card-title">三态可用率 · 所选时段（<span id="tri-state-range">近7天</span>）</div>
    <div class="tri-bar" style="display:flex;height:14px;border-radius:4px;overflow:hidden;gap:2px"
         role="img" aria-label="ok / degraded / failed 占比条"></div>
    <div id="tri-state-legend" style="color:var(--dim);font-size:12px;margin-top:8px"></div>
  </section>
"""
```

**new_string**：
```
_DASH_SECTIONS_V2 = """  <!-- pane perf 开 -->
  <div class="tab-pane" data-pane="perf" role="tabpanel" id="pane-perf"
       aria-labelledby="tab-perf" hidden>
  <section class="card">
    <div class="card-title">上游健康<span class="chip" id="health-status">--</span></div>
    <table>
      <tbody id="upstream-health"><tr><td class="empty" colspan="2">读取中……</td></tr></tbody>
    </table>
  </section>
  <section class="card">
    <div class="card-title">模型速度对比 · TTFB P50/P90（进程内累计，最快在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>模型</th><th>P50</th><th>P90</th></tr></thead>
      <tbody id="perf-model-body"><tr><td class="empty" colspan="3">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">延迟分布 · 三阶段 P50（连接 / 响应头 / 数据体）</div>
    <svg id="latency-dist" viewBox="0 0 600 96" preserveAspectRatio="none" role="img"
         aria-label="阶段延迟 P50 条形图"></svg>
  </section>
  <section class="card">
    <div class="card-title">Token 用量按天 × 模型（<span id="token-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th></tr></thead>
      <tbody id="token-daily-body"><tr><td class="empty" colspan="4">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  </div>
  <!-- pane perf 闭 -->
"""
```

### 改动 P5 — `_DASH_JS_CORE`：initTabs JS 块

**old_string**：
```
  rect.style.fill = fill;  // 展示属性不支持 var()，必须走 CSSOM style
  return rect;
}
"""
```

**new_string**：
```
  rect.style.fill = fill;  // 展示属性不支持 var()，必须走 CSSOM style
  return rect;
}
/* ===== P6: 页面级 tab 分页（tab-bar ↔ pane 显隐 ↔ aria ↔ hash ↔ localStorage） ===== */
var DASH_TAB_KEYS = ["overview", "requests", "perf", "settings"];
var dashTabMem = "overview";  // localStorage 不可用（隐私模式等）时的会话内降级
function initTabs() {
  var bar = document.querySelector(".tab-bar");
  if (!bar) return;
  function readStoredTab() {
    try {
      var v = localStorage.getItem("ctyun-dash-tab");
      if (v) return v;
    } catch (e) { /* SecurityError：localStorage 读被禁，走内存降级 */ }
    return dashTabMem;
  }
  function storeTab(key) {
    try { localStorage.setItem("ctyun-dash-tab", key); }
    catch (e) { /* SecurityError（隐私模式/禁用 cookie）：吞掉降级内存变量。
                    该 try 仅包 setItem 一条语句，除安全策略阻断无其他失败路径；
                    用户无跨会话持久化诉求，会话内记忆已是合理降级 */ }
    dashTabMem = key;
  }
  function hashTab() {
    var h = location.hash ? location.hash.slice(1) : "";
    return DASH_TAB_KEYS.indexOf(h) !== -1 ? h : null;
  }
  function applyTab(key, persist) {
    var panes = document.querySelectorAll(".tab-pane");
    for (var i = 0; i < panes.length; i++) {
      if (panes[i].getAttribute("data-pane") === key) panes[i].removeAttribute("hidden");
      else panes[i].setAttribute("hidden", "");
    }
    var tabs = bar.querySelectorAll(".tab");
    for (var j = 0; j < tabs.length; j++) {
      var on = tabs[j].getAttribute("data-tab") === key;
      tabs[j].classList.toggle("active", on);
      tabs[j].setAttribute("aria-selected", on ? "true" : "false");
      tabs[j].setAttribute("tabindex", on ? "0" : "-1");
    }
    if (persist) {
      storeTab(key);
      if (history.replaceState) history.replaceState(null, "", "#" + key);
    }
  }
  bar.addEventListener("click", function (e) {
    var btn = e.target && e.target.closest ? e.target.closest(".tab") : null;
    if (!btn) return;
    applyTab(btn.getAttribute("data-tab"), true);
  });
  // 左/右方向键循环切换（WAI-APG 简化版）；keydown 挂在 tab-bar 上，不劫持 range-tab
  bar.addEventListener("keydown", function (e) {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    var tabs = bar.querySelectorAll(".tab");
    var idx = -1;
    for (var i = 0; i < tabs.length; i++) {
      if (tabs[i] === document.activeElement) { idx = i; break; }
    }
    if (idx === -1) return;
    e.preventDefault();
    var next = e.key === "ArrowRight" ? (idx + 1) % tabs.length
                                       : (idx - 1 + tabs.length) % tabs.length;
    tabs[next].focus();
    applyTab(tabs[next].getAttribute("data-tab"), true);
  });
  window.addEventListener("hashchange", function () {
    var key = hashTab();
    if (key) applyTab(key, true);
  });
  applyTab(hashTab() || readStoredTab(), false);
}
initTabs();
"""
```

### DASHBOARD_HTML join

**不改动**。6 段顺序保持 `_DASH_HEAD, _DASH_SECTIONS_STATIC, _DASH_SECTIONS_TABLES, _DASH_SECTIONS_V2, _DASH_JS_CORE, _DASH_JS_V2, "</script>\n</body>\n</html>\n"`。

### 验证命令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/dashboard-tabs
python3 ctyun-stream-fix-proxy.test.py
```

### 预期 GREEN 结果

- exit code：0
- 253 tests，OK（全绿）
- 输出末尾：
  ```
  ----------------------------------------------------------------------
  Ran 253 tests in ...s
  OK
  ```

### 手动验证（DELIVER 阶段，非卡 2 自动化验证内容）

1. `curl -s http://localhost:<port>/ | grep -c 'class="tab-pane"'` == 4
2. 浏览器：默认 overview → 点 settings 显示 upstream form + TPM → 刷新仍 settings → `/​#requests` 进 requests → 窄屏 tab 条横向滚动

---

## Self-Review（5 项）

> **已执行**（PLAN-B 作者自查，写 Plan 时同步完成，不另 dispatch）。

### 1. spec coverage

| spec AC | PLAN 卡 | 状态 |
|---------|---------|------|
| `DashboardTabsSkeletonTest` 5 方法 | 卡 1 改动 1 | 全覆盖，每方法 spec 逐行对应 |
| `test_dashboard_html_full_page` 5 条追加 | 卡 1 改动 3 | 全覆盖 |
| join 等式同步 | 开放决策→合并路径→不变 | 明确写死 |
| `test_no_inner_html_anywhere` 常量 tuple | 合并路径→tuple 不变 | 明确写死 |
| 手动验证 curl + 浏览器 | 卡 2 末尾 observation | 非自动化 |
| 8 项设计决策 | 卡 2 改动 P1-P5 | 全覆盖 |
| 所有风险处置 | P1-P5 内嵌（跨段注释、keydown 作用域、try/catch、grid pane 等） | 全覆盖 |

### 2. placeholder scan

全文搜索 "TODO"/"待实现"/"在此添加"/"xxx"/"..."/"实现如下"/"依 spec"→ 0 命中。所有卡内代码块为逐字可粘贴的完整 Python/HTML/CSS/JS，无指令描述替代代码。

### 3. type consistency

- 所有 Edit old_string 均唯一锚定（已在 PLAN-B 阶段通过 Read/grep 实测验证）
- P5 锚 `  rect.style.fill = fill; ...\n"""`（4 行）全文件唯一：`grep -n "rect.style.fill = fill"` 计数=1（:3054）；该块是 `_DASH_JS_CORE` 真实尾部（:3054-3057，`"""` 闭合于 :3057）。原稿误锚的 `loadTpmSettings();\n"""` 实为 `_DASH_JS_V2` 段尾（:3416-3417），V2 常量起始于 :3059——不可用作 P5 锚点（已修订）
- `@media (max-width:720px) { ... }\n</style>` 唯一（唯一 `</style>` 在 HEAD 内）
- 所有测试使用 `self.mod` / `load_proxy_module()` 模式与既有 `DashboardV2SkeletonTest` 一致
- 所有新测试方法签名 `def test_*(self) -> None:` 与既有签名风格一致
- JS 使用 `var` + `function`（ES5），与既有 JS_CORE 风格一致

### 4. 可落盘性

每卡指定：
- 精确文件路径（绝对路径）
- 精确 old_string（逐字节与 Read 输出一致，可直接用于 Edit 工具）
- 精确 new_string（完整块，无省略）
- 验证命令 + 预期输出 + exit code
卡 1 共 3 次 Edit（新增类 + v2_containers + full_page）；卡 2 共 5 次 Edit（P1-P5）。

### 5. 锚点实测（PLAN-B 阶段已执行 grep/Read 验证）

| spec 锚点 | 文件 | 实测 line | 结果 |
|-----------|------|-----------|------|
| `_DASH_HEAD` CSS 尾部 `</style>` | proxy.py | :2547 | 准确 |
| `_DASH_SECTIONS_STATIC` 起始 (2556) 至结尾 (2593) | proxy.py | :2556-2593 | 准确 |
| `_DASH_SECTIONS_TABLES` 起始 (2595) 至结尾 (2626) | proxy.py | :2595-2626 | 准确 |
| `_DASH_SECTIONS_V2` 起始 (2628) 至结尾 (2663) | proxy.py | :2628-2663 | 准确 |
| `_DASH_JS_CORE` 起始 (2711)，尾部 bar()（`rect.style.fill = fill`，:3054-3057） | proxy.py | :2711, 3054-3057 | 准确（`rect.style.fill = fill` grep -n 计数=1，`"""` 闭合于 :3057） |
| `DASHBOARD_HTML` join (3373) | proxy.py | :3373-3377 | 准确 |
| `DashboardV2SkeletonTest` (2828) | test.py | :2828 | 准确 |
| `test_segment_constants_present_and_join` (2834) | test.py | :2834 | 准确 |
| `test_no_inner_html_anywhere` (2863) | test.py | :2863 | 准确 |
| `test_dashboard_html_full_page` (3000) | test.py | :3000 | 准确 |
| `test_v2_containers_present` (2849) | test.py | :2849 | 准确（plan 需改此行，spec 未显式列但为 tri-state 移位必然连带） |
| range-tab click handler 挂载方式 (:3264) | proxy.py | :3264 | `e.target.closest(".range-tab")`，与我 initTabs 的 `.tab` 不冲突 |
| `load_proxy_module` 辅助 (627) | test.py | :627 | 新测试复用 |