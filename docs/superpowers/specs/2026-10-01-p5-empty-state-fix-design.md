# 2026-10-01 P5 两空态展示瑕疵修复 — design spec

## Goal

修复 dashboard V2 两张卡的空态：延迟分布卡空态显示 "—" 而非 3 个 "0ms"；三态卡空态占位文字移出 14px 高 overflow:hidden 的占比条，不再被裁切。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:2867` `renderLatencyDist(phases)`（`_DASH_JS_V2` 内，实测 :2867-2902）：

现状：:2876 `var ms = phases[name];` → :2877 `var w = ms === undefined ? 0 : Math.min(ms / cap, 1) * 460;` → :2888 rect width `Math.max(2, w)` → :2896 `v.textContent = ms === undefined ? "—" : fmtDur(ms)`。后端 `stats_snapshot`（实测 :1573-1575）恒填三键 `connect/headers/body`；空直方图时 `hist_percentile`（实测 :200-202）返回 0.0 → `ms === 0`，`undefined` 分支不可达，`fmtDur(0)` = "0ms"。

修法（纯前端，后端契约不动）：

```js
var has = ms == null || ms === 0;   // ms==null 兜 undefined/null；0.0 与空直方图一一对应
var w = has ? 0 : Math.min(ms / cap, 1) * 460;
```

:2888 rect width 改 `r.setAttribute("width", (has ? 0 : Math.max(2, w)).toFixed(1));`（空态不留 2px 假条）；:2896 改 `v.textContent = has ? "—" : fmtDur(ms);`。

不变量依据（写进代码旁注释）：`hist_percentile` q=0.5 下非空直方图最小值 ≥25ms（首桶 [0,50) 插值：rank≥0.5 且 count≥1 → 0.5/count×50 ≥ 25），故 `ms === 0` ⟺ 空直方图，无误判"真实 0ms"。

2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:2940` `renderTriState(snap)`（实测 :2940-2980）：

现状 :2962-2964 空态 `bar.appendChild(el("span", "empty", "暂无 outcome 数据 …"))` 落在 :2490-2491 `.tri-bar`（`height:14px; overflow:hidden`）内 → 文字裁切不可读。

修法：

```js
if (total === 0) {
  bar.style.display = "none";
  legend.appendChild(el("span", "empty", "暂无 outcome 数据 —— 有请求经过代理后这里会出现三态占比"));
  return;
}
bar.style.display = "flex";   // 非空分支恢复内联样式默认值，防上次空态残留 none
```

占位移入 `#tri-state-legend`（:2492，`margin-top:8px;font-size:12px`，无 overflow 限制）；`bar.textContent = ""` 清空语义保留。

3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:2530` `DashboardV2SkeletonTest` 加 1 个模块级断言测试（沿用 :2557 `test_old_content_intact` 字符串断言风格；实现代码字样须与断言一致）：

```python
def test_v2_empty_state_branches(self) -> None:
    v2 = self.mod._DASH_JS_V2
    self.assertIn("ms == null || ms === 0", v2,
                  "renderLatencyDist 空态必须判 0.0（hist_percentile 空桶值）")
    self.assertIn("bar.style.display", v2,
                  "renderTriState 空态必须隐藏占比条")
    self.assertNotIn('bar.appendChild(el("span", "empty"', v2,
                     "三态空态占位不得落在 overflow:hidden 的 tri-bar 内")
```

## Acceptance Criteria

- 上述 3 断言通过；既有 `test_segment_constants_present_and_join`（:2536 常量 join 校验）与 `test_no_inner_html_anywhere`（:2565，改动不得引入 innerHTML）保持绿。
- 全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 224 用例全绿（贴 stdout/exit code）。
- 目检（DELIVER 阶段，无流量冷启动代理开 dashboard）：延迟分布卡三行显示 "—"；三态卡空态文字完整一行显示在条下方，不被裁切。
- followups.md:15-16 两条目随本 episode 消化删除（DELIVER 处理）。

## Risks

- `ms === 0` 语义依赖 `hist_percentile` 空桶→0.0 现状（:191-202 docstring 已写明）；若未来直方图首桶边界改动需复查该不变量（low）。
- `_DASH_JS_V2` 是字符串常量，编辑易错引号/转义；join 常量测试（:2536）兜底。
- `renderTriState` 多渲染路径（:3048 等处 poll 调用）：`bar.style.display = "flex"` 恢复行必须放非空分支开头，否则空态渲染一次后条永久隐藏。
- 前端判空分支与模块级断言字样耦合：实现改动措辞（如改 `ms <= 0`）会挂断言——PLAN-B 写死代码块与断言一致。

## Exclusions

- 不改后端 stats_snapshot 契约（P2 已冻结：`phase_p50_ms` 三键恒在、空直方图 0.0）。
- 不改 `renderPerf`/`renderTokens`/`renderHealth` 的既有空态样式（它们空态已可读）。
- 不做真浏览器 e2e（无 browser 依赖引入）；视觉验收走 DELIVER 目检。

## R31 Evidence

[R31-S1] 现状证据（本会话实测）：两处空态分支的现状锚点——后端恒填三键且空直方图返 0.0；前端空态 span 落在 14px 高 overflow:hidden 的 tri-bar 内：

```
$ grep -n 'renderLatencyDist\|renderTriState\|hist_percentile' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
191:def hist_percentile(buckets, edges, q):
1570:            hist_percentile(hist, HIST_BUCKETS_MS, 0.5), 1)
1572:            hist_percentile(hist, HIST_BUCKETS_MS, 0.9), 1)
1575:            hist_percentile(phase_hist[name], HIST_BUCKETS_MS, 0.5), 1)
2793:  renderTriState(lastSnap);
2865:  renderLatencyDist(perf.phase_p50_ms || {});
2867:function renderLatencyDist(phases) {
2940:function renderTriState(snap) {
3048:      renderTriState(snap);
```

```
$ grep -n 'tri-bar\|tri-state-legend\|function fmtDur\|function el(' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
2476:    <svg id="latency-dist" viewBox="0 0 600 96" preserveAspectRatio="none" role="img"
2490:    <div class="tri-bar" style="display:flex;height:14px;border-radius:4px;overflow:hidden;gap:2px"
2492:    <div id="tri-state-legend" style="color:var(--dim);font-size:12px;margin-top:8px"></div>
2511:function el(tag, cls, text) {
2530:function fmtDur(ms) {
```

[R31-S2] 根因：hist_percentile 空直方图返回 0.0（实测 :191-202，docstring 明示"空直方图 → 0.0"），stats_snapshot :1573-1575 恒填三键 0.0 → renderLatencyDist :2896 的 `ms === undefined` 分支不可达，空态渲染 fmtDur(0) = "0ms"；renderTriState :2962-2964 空态 span 落在 :2490-2491 .tri-bar（height:14px;overflow:hidden）内被裁切。需求/裁决来源：followups.md:15-16 + 用户 2026-10-01 "开 task 处理本会话的遗留问题"，并明确裁决不改后端契约（P2 stats_snapshot 已冻结）。裁决：前端判 `has = (ms == null || ms === 0)`（不变量：q=0.5 下非空直方图返回值 ≥25ms，0.0 与空直方图一一对应）；三态空态隐藏 .tri-bar、占位移入 legend。
