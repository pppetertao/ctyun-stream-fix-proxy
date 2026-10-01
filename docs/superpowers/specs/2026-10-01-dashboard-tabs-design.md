# Dashboard Tab 分页重构设计（ctyun-stream-fix-proxy）

## Goal

把当前单页 12+ 卡片纵向堆叠的 dashboard（`GET /` 返回的 `DASHBOARD_HTML`）改成「顶部 tab 条 + 单 tab 内容区」形态，缓解页面过长过挤；零新依赖、零外部 CDN、零后端改动，服务所有经浏览器访问本代理运行台的运维/开发者。

## 设计决策（定死）

1. **Tab 分组（4 个）**，顺序固定：`overview`（默认）/ `requests` / `perf` / `settings`。映射表（源行号见 R31-S2）：
   - **overview（监控概览）**：range-tabs 时间维度条（`_DASH_SECTIONS_STATIC` :2576-2581）+ stats-row 五格统计（:2582-2588）+ 请求节奏 spark（:2589-2592）+ 按天统计（`_DASH_SECTIONS_TABLES` :2595-2603）+ 按天×模型（:2604-2612）+ 三态可用率（`_DASH_SECTIONS_V2` :2657-2662）。
   - **requests（请求与剥行）**：剥行流带（`_DASH_SECTIONS_TABLES` :2613-2616）+ 最近请求（:2617-2625）。
   - **perf（性能与健康）**：上游健康（`_DASH_SECTIONS_V2` :2628-2633）+ 模型速度对比（:2634-2642）+ 延迟分布（:2643-2647）+ Token 用量按天×模型（:2648-2656）。
   - **settings（设置）**：上游端点 form（`_DASH_SECTIONS_STATIC` :2557-2566）+ TPM 限流设置（:2567-2575）。

   **被否备选**：①「监控 / 设置」二分法——监控内仍 10+ 卡片过长；②按「V1/V2」分——用户无 V2 概念，违背心智模型；③每卡片一 tab——tab 条爆炸且割裂相关卡片（如 daily 与 daily-by-model）。

2. **DOM 结构**：在 `<main>` 内最顶部插入 `<nav class="tab-bar" role="tablist" aria-label="运行台分页">`（4 个 `<button type="button" class="tab" role="tab" data-tab="overview|requests|perf|settings">`）。每个既有 `<section class="card">` 不动 id/内部结构，仅按上表包一层 `<div class="tab-pane" data-pane="overview|requests|perf|settings">`（不改 section 本体——零内部回归面）。range-tabs 那节比较特殊（其本身是 `role="tablist"` 的 section），改为嵌进 overview pane 内第一个位置，aria 标签不变；stats-row 同理进 overview。

3. **显隐机制**：CSS `[hidden] { display:none !important; }` + JS 给非激活 pane 加 `hidden` 属性；激活 tab 按钮加 `.active`（复用现有 `.range-tab.active` 琥珀填充样式语言）。不重建/不卸载 DOM，所有 fetch/render 逻辑照旧对所有 id 生效——**所有现有 render 函数（renderStats/renderRecent/renderDaily/renderDailyByModel/renderSpark/renderPerf/renderTokens/renderTriState/renderHealth/renderTpmSettings）零改动**，display:none 的容器接受 textContent 更新无副作用。

4. **状态持久化**：`localStorage["ctyun-dash-tab"]` 读写当前 tab key；启动时按 `location.hash`（`#overview|#requests|#perf|#settings`）> localStorage > `"overview"` 回退。切换 tab 时同步写 localStorage + `history.replaceState(null, "", "#"+key)`（不触发 hashchange 跳滚动）。`window.addEventListener("hashchange", ...)` 监听外部改 hash 也切 tab。

5. **移动端**：`.tab-bar { display:flex; gap:8px; overflow-x:auto; flex-wrap:nowrap; -webkit-overflow-scrolling:touch; }`，窄屏可横向滑；`@media (max-width:720px)` 内追加 `.tab { flex:0 0 auto; }` 防压缩。

6. **可访问性**：tab 按钮 `aria-selected`、`aria-controls="<pane-id>"`；pane `role="tabpanel"` `aria-labelledby="<tab-id>"`；激活 tab `tabindex=0`、非激活 `tabindex="-1"`；左/右方向键循环切换（WAI-APG 简化版）。

7. **常量拆分**：现有 `_DASH_HEAD` / `_DASH_SECTIONS_STATIC` / `_DASH_SECTIONS_TABLES` / `_DASH_SECTIONS_V2` / `_DASH_JS_CORE` / `_DASH_JS_V2` 六段保留；新增 `_DASH_TAB_BAR`（nav 片段）与 `_DASH_CSS_TABS`（tab 样式）两个常量，样式并入 `_DASH_HEAD` 尾部 `</style>` 前；`DASHBOARD_HTML` join 顺序改为 `_DASH_HEAD_CSS_TABS_MERGED, _DASH_TAB_BAR+改造后 STATIC/TABLES/V2, _DASH_JS_CORE+TAB 初始化, _DASH_JS_V2, 尾部`。implementer 决定是新增两常量还是合并进相邻段——**join 等式断言（`DashboardV2SkeletonTest.test_segment_constants_present_and_join` :2840-2847）必须同步更新**。

8. **后端零改动**：`do_GET /` (:2274) 仍返回 `DASHBOARD_HTML`，所有 `/api/*` 端点不动。

## Files to Change

- `ctyun-stream-fix-proxy.py:2497`（`.card` CSS 块后）— 在 `_DASH_HEAD` 的 `</style>` (:2547) 之前追加 tab 样式：`.tab-bar` / `.tab` / `.tab.active` / `[hidden]{display:none!important}` / `@media (max-width:720px) .tab{flex:0 0 auto}`；沿用现有 `--amber`/`--ink`/`--line` 变量与 `.range-tab` 视觉语言。
- `ctyun-stream-fix-proxy.py:2556`（`_DASH_SECTIONS_STATIC` 起始 `<main>` 后）— 注入 `_DASH_TAB_BAR` nav 片段（4 个 button，data-tab/role=tab/aria-selected/tabindex）；并在原文档流上把 :2557-2575 两张 settings 卡包进 `<div class="tab-pane" data-pane="settings" role="tabpanel" hidden>`，:2576-2592 range-tabs + stats-row + spark 包进 `<div class="tab-pane" data-pane="overview" role="tabpanel">`（默认激活不 hidden）。
- `ctyun-stream-fix-proxy.py:2595`（`_DASH_SECTIONS_TABLES`）— :2595-2612 两段（daily、daily×model）追加进 overview pane 尾部；:2613-2625 两段（poison-strip、req-body）包进 `<div class="tab-pane" data-pane="requests" role="tabpanel" hidden>`。注意 overview pane 跨 STATIC/TABLES 两常量，闭合 `</div>` 落在 TABLES 段内——在常量头部注释里点明跨段闭合，防后续维护误切。
- `ctyun-stream-fix-proxy.py:2628`（`_DASH_SECTIONS_V2`）— :2628-2656 四段（health/perf/latency/token-daily）+ :2657-2662 tri-state 拆分：tri-state 移入 overview pane（放 daily×model 之后），其余四段包进 `<div class="tab-pane" data-pane="perf" role="tabpanel" hidden>`。
- `ctyun-stream-fix-proxy.py:2665`（`_DASH_JS_CORE` 起始 `</main>` 前）— 在 `</main>` 之前闭合所有 pane；JS 尾部 (:3370 `loadTpmSettings();` 之后、`</script>` 之前) 新增 `initTabs()`：`document.querySelectorAll(".tab-bar .tab")` 绑 click/keydown，读 localStorage+location.hash 决定初始 tab，调 `activateTab(key)`（切 hidden/aria/tabindex/hash/localStorage 五项同步）；`window.addEventListener("hashchange", ...)`。
- `ctyun-stream-fix-proxy.py:3373`（`DASHBOARD_HTML` join 列表）— 按决策 7 更新 join 顺序与常量名集合；若新增 `_DASH_TAB_BAR`/`_DASH_CSS_TABS`，同步加入 join。
- `ctyun-stream-fix-proxy.test.py:2834` `test_segment_constants_present_and_join` — join 等式同步加新常量；常量存在性 tuple 加 `"_DASH_TAB_BAR"`（若 implementer 选新增常量路径）。
- `ctyun-stream-fix-proxy.test.py:3000` `test_dashboard_html_full_page` — 追加 tab 断言（见 Acceptance Criteria §3）。
- `ctyun-stream-fix-proxy.test.py:2863` `test_no_inner_html_anywhere` — 检查常量名 tuple 同步（新常量也得过 innerHTML=0 扫描）。

## Acceptance Criteria

- `python3 ctyun-stream-fix-proxy.test.py` 全绿（含既有 199+ 例与下方新增断言）。
- **新单测 `DashboardTabsSkeletonTest`**（模块级，无需子进程，与 `DashboardV2SkeletonTest` :2828 同款模式）：
  - `test_tab_bar_present`：HTML 含 `<nav class="tab-bar"`、`role="tablist"`、4 个 `data-tab="overview|requests|perf|settings"` 按钮、每按钮有 `aria-controls`。
  - `test_pane_mapping`：每个 `<div class="tab-pane" data-pane="X"` 数量 = 4；按 data-pane 取值分组，断言关键容器 id 落位——overview 含 `id="st-requests"`(:2583)/`id="daily-body"`(:2600)/`id="daily-model-body"`(:2609)/`id="tri-state-card"`(:2657)；requests 含 `id="poison-strip"`(:2615)/`id="req-body"`(:2622)；perf 含 `id="upstream-health"`(:2631)/`id="perf-model-body"`(:2639)/`id="latency-dist"`(:2645)/`id="token-daily-body"`(:2653)；settings 含 `id="upstream-form"`(:2560)/`id="tpm-models"`(:2570)。
  - `test_initial_hidden_state`：`data-pane="overview"` 所在 div 不带 `hidden`；其余 3 个 pane 均带 `hidden`。
  - `test_tab_js_wiring`：`_DASH_JS_CORE` 含 `initTabs`、`localStorage`、`location.hash`、`hashchange`、`aria-selected`、`history.replaceState`。
  - `test_no_regression_existing_ids`：所有既有断言 id（`daily-body`/`daily-model-body`/`req-body`/`poison-strip`/`tpm-models`/`tpm-save`/`upstream-form`/`upstream-health`/`perf-model-body`/`latency-dist`/`token-daily-body`/`tri-state-card`/`spark`/`evt-tip`）仍各恰好出现 1 次（防 pane 包裹时复制/丢段）。
- **集成测试追加**（`test_dashboard_html_full_page` :3000 内追加）：`assertIn('<nav class="tab-bar"', html)`、`assertEqual(html.count('class="tab-pane"'), 4)`、`assertIn('data-pane="settings"', html)`、`assertIn('initTabs', html)`、`assertIn('localStorage', html)`。
- **手动验证（DELIVER 阶段）**：起代理后 `curl -s http://localhost:<port>/ | grep -c 'class="tab-pane"'` = 4；浏览器开 `/` → 默认 overview、点 settings 显示上游端点 + TPM 两张卡、刷新仍 settings、`/​​#requests` 直链进 requests；窗口宽 <720px tab 条可横向滚动。

## Risks

- **跨常量闭合 `<div>`**：overview pane 跨 `_DASH_SECTIONS_STATIC`/`_DASH_SECTIONS_TABLES` 两常量，requests/perf/settings pane 也可能跨段。若后续维护按段常量切分/重排时漏改闭合，`DASHBOARD_HTML` 仍是合法 str 但浏览器解析出嵌套错位——`test_segment_constants_present_and_join` 等式断言 + `test_initial_hidden_state` 的 pane 计数=4 可兜住明显错误；implementer 必须在每个 pane 开/闭处加 HTML 注释 `<!-- pane overview 开/闭 -->` 便于 grep 校验配对。
- **hidden pane 内 SVG/CSS layout**：`renderSpark`(:2965) 与 `renderLatencyDist`(:3036) 用 `viewBox` + `preserveAspectRatio="none"`，无 JS 量宽度，display:none → 显示时浏览器自动重排，无需监听 resize；但**激活 tab 的瞬间**若浏览器尚未 layout 完成就触发 render（极罕见），svg 内容会按上次 width 绘——可接受，2s 后 poll 自动重绘。
- **range-tabs 嵌套 tablist**：overview pane 内的 range-tabs 自身是 `role="tablist"`，与外层页面 tab-bar 的 tablist 嵌套。WAI-APG 允许嵌套 tablist（aria 标签已区分：`aria-label="统计时间维度"` vs `aria-label="运行台分页"`）；方向键 handler 必须只在自身 `.tab-bar` 容器内生效，不得劫持 range-tab 的左右键——`initTabs` 内 `e.currentTarget` 限定为 `.tab-bar`，keydown 监听挂在 tab-bar 而非 document。
- **localStorage 异常隐身**：隐私模式/禁用 cookie 时 `localStorage.setItem` 抛 `SecurityError`。`initTabs`/`activateTab` 必须用 try/catch 包 localStorage 写，catch 内静默 fallback 到「本次会话内存变量」（命名异常 + 注释说明为何可吞：用户无持久化诉求，内存态已是合理降级）；不得空 catch。
- **既有 e2e 断言可能失效**：`test_dashboard_html_full_page` 已断言的字段若因 pane 包裹被移位（例如 `id="tpm-save"` 现在嵌两层 div），字符串断言本身仍命中（`assertIn` 不查嵌套深度），但任何依赖 DOM 顺序的断言（grep 未发现）需 implementer 执行阶段复核。
- **旧浏览器**：`history.replaceState` / `Array.prototype.forEach` / `Element.closest` 在 ES5+ 浏览器全支持；项目已用 `fetch`/`AbortController`/`classList`，无新增兼容面。

## Exclusions

- 不改任何 `/api/*` 端点签名、不改 `do_GET`/`do_POST` 后端逻辑、不改 `DASHBOARD_HTML` 的 `Content-Type`/缓存头。
- 不做单卡片视觉重设计（配色/字号/间距沿用现状）；不动 `.range-tab` 的样式与行为。
- 不引入新依赖（无 npm 包、无外部 CSS/JS CDN、无字体）。
- 不做卡片拖拽/重排、不做用户自定义 tab 顺序。
- 不做 tab 内二次分页/子 tab；不在 tab 间共享 fetch（仍由现有 `poll()` 每 2s 全量拉）。
- 不做服务端渲染的激活 tab（首屏永远是 overview + JS  hydrate 后切到 hash/localStorage 指向的 tab——可能闪一下 overview，接受）。
- 不处理 followups.md 既有遗留项（若有 dashboard 相关 followup，另开 episode）。
- 不改 plist/launchd/deploy；不改 `~/.local/etc/ctyun-stream-fix-proxy.json` schema。

## R31 Evidence

[R31-S1] 问题存在：dashboard 单页纵向堆叠 12+ 卡片，滚动过长

证据①（页面 12 个 `<section class="card">` 全部纵排在 `<main>` 内，无任何分组导航）：

```
$ grep -nE '<section class="card' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
2557:  <section class="card">
2567:  <section class="card" id="tpm-settings-card">
2576:  <section class="card range-tabs" role="tablist" aria-label="统计时间维度">
2582:  <section class="stats-row">
2589:  <section class="card">
2595:_DASH_SECTIONS_TABLES = """  <section class="card">
2604:  <section class="card">
2613:  <section class="card">
2617:  <section class="card">
2628:_DASH_SECTIONS_V2 = """  <section class="card">
2634:  <section class="card">
2643:  <section class="card">
2648:  <section class="card">
2657:  <section class="card" id="tri-state-card">
```

证据②（页面骨架跨 3 段常量、约 110 行 HTML，全部一次性输出到 `<main>`）：

```
$ grep -nE '_DASH_SECTIONS_(STATIC|TABLES|V2)|DASHBOARD_HTML = ' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
2556:_DASH_SECTIONS_STATIC = """<main>
2595:_DASH_SECTIONS_TABLES = """  <section class="card">
2628:_DASH_SECTIONS_V2 = """  <section class="card">
3373:DASHBOARD_HTML = ("".join([
```

[R31-S2] 根因：架构上从未引入分组/导航层——所有 section 平铺直叙进 `<main>`，仅有一个针对时间维度的局部 tablist（range-tabs），无页面级 tab 机制

```
$ grep -nE 'role="tablist"|role="tab"|class="tab|localStorage|location\.hash|hashchange' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
2516:.range-tabs { display:flex; gap:8px; flex-wrap:wrap; }
2517:.range-tab { font-weight:400; font-size:12.5px; background:transparent; color:var(--dim);
2519:.range-tab:hover { filter:none; color:var(--text); }
2520:.range-tab.active { background:var(--amber); color:var(--ink); border-color:var(--amber); font-weight:700; }
2576:  <section class="card range-tabs" role="tablist" aria-label="统计时间维度">
2577:    <button type="button" class="range-tab" role="tab" data-range="3d">近3天</button>
2578:    <button type="button" class="range-tab" role="tab" data-range="7d">近7天</button>
2579:    <button type="button" class="range-tab" role="tab" data-range="mtd">本月</button>
2580:    <button type="button" class="range-tab" role="tab" data-range="last_month">上月</button>
2688:var selectedRange = "7d";  // 刷新不记忆（无 localStorage），默认近7天
2943:  var tabs = document.querySelectorAll(".range-tab");
```

`.range-tab` 仅用于「时间维度」二级切换，作用域局限在统计卡片；页面级无 `localStorage`/`location.hash` 持久化（:2688 注释明示「刷新不记忆（无 localStorage）」），也没有 `<nav>`/页面级 `role="tablist"` 元素。卡片按 STATIC → TABLES → V2 顺序无脑拼接（:3373-3377），后续每加一张卡（如 v1.5 的 TPM 设置 :2567）就更长一屏——结构性问题，只能靠引入页面级 tab 解决。
