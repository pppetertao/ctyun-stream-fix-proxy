# ctyun-proxy dashboard 计数口径标注 + 最近请求跨天日期分组（simplified mode）

背景：用户反馈 dashboard 两处展示问题——①「按模型」卡（deepseek-v4-pro-0813-oc 149 请求 / glm-5.3 1 请求）无时间窗口标注，读不出"当天还是累计"；②「最近请求（新在上）」表时间列只有时分秒，跨天后分不清哪条是哪天的。两处均为 `_DASHBOARD_SRC` 内嵌页面问题，Python 统计行为零变更。

## Goal

dashboard「按模型」计数明确标注口径（自上次重启起累计，重启清零），「最近请求」表跨天时按本地日期插入分组行（未跨天不加），使计数窗口与日期归属一眼可读；`/api/stats` 契约与代理转发行为零变更。

## 现场事实（代码级）

- `by_model` 仅内存：`STATS`（ctyun-stream-fix-proxy.py:53-54）含 `by_model`；`save_stats_counters`（:137-152）只持久化 `requests_total/filtered_total/errors_total` + `daily` → **by_model 重启清零，而顶部「请求数」跨重启累计**——两窗口并存无标注即口径混乱根源。
- 前端无标注：「按模型」card-title（:621）纯文本；footer（:648-650）只说明"累计与按天计数跨重启保留"，未提 by_model 窗口。
- 跨天无日期：`fmtTime`（:663-665）`toLocaleTimeString("zh-CN",{hour12:false})` 仅时分秒；`renderRecent`（:698-720）逐行直接用。recent 为 `deque(maxlen=100)`（:57）内存窗口。

## 方案对比

**问题 1（口径）**：A. 标注现状口径——card-title 加 chip 副标注 + footer 补句（零逻辑变更）；B. 改"当日"口径——`STATS` 加 `by_model_today` + `_record_request`（:233-260）跨天清零分支 + snapshot（数据结构变更，丢"自启动累计"维度，测试 +2）；C. by_model 并入持久化全量累计（save/load 改，BY_MODEL_CAP=32 prune 语义复杂化）。**推荐 A**：「按天统计」卡（:625）已提供当日维度，by_model 标注清楚即互补；B/C 变更风险大于收益，若用户实际期望当天口径再开 B episode。

**问题 2（跨天）**：A. 日期分组行——跨天时 tbody 插 `<tr class="date-row">`（colSpan=7）；B. 每行时间前缀日期（fmtTime 加参，每行冗余且列宽变化）。**推荐 A**：行内零冗余，与按天统计卡视觉呼应。

## Files to Change

### 1. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`（`_DASHBOARD_SRC` :522-884 内，零 Python 行为变更）

- **:621 card-title**：`<div class="card-title">按模型</div>` → 标题尾插 chip 副标注（复用 :551 `.chip` 样式，同 :600 upstream-source 用法）：`<span class="chip">自上次重启起累计，重启清零</span>`。
- **:648 footer**：首句后补「按模型计数自进程启动累计，不持久化（重启清零）；」。
- **:665 `fmtTime` 后新增 `fmtDate`**（本地时区，与 `today_key` :113-115 `time.localtime` 同口径；手工拼接避免 locale 差异）：

```js
function fmtDate(ts) {
  var d = new Date(ts * 1000);
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}
```

- **:698 `renderRecent`**：重写为跨天分组版（既有 7 列行构造 :703-711 与空态 :713-719 逐字保留）：

```js
function renderRecent(list) {
  var body = $("req-body");
  body.textContent = "";
  var dateSet = {};
  for (var i = 0; i < list.length; i++) dateSet[fmtDate(list[i].ts)] = true;
  var multiDay = Object.keys(dateSet).length > 1;
  var lastDate = null;
  for (var i = list.length - 1; i >= 0; i--) {
    var r = list[i];
    if (multiDay) {
      var d = fmtDate(r.ts);
      if (d !== lastDate) {           // 新在上遍历：日期切换点即插分组行
        var trd = el("tr", "date-row");
        var tdd = el("td", "", d);
        tdd.colSpan = 7;
        trd.appendChild(tdd);
        body.appendChild(trd);
        lastDate = d;
      }
    }
    var tr = el("tr", r.filtered > 0 ? "hit" : "");
    /* :704-710 既有七列 appendChild 逐字保留 */
    body.appendChild(tr);
  }
  if (list.length === 0) { /* :713-719 空态不变 */ }
}
```

边界条件：multiDay 判定基于本页 100 条内存窗口；单天时 DOM 结构与现状逐字节等价（零分组行）；分组行走 `el`/`textContent`（合规 :672-674 的 innerHTML 禁令）。

- **:583 附近 CSS 追加一行**（:579 既有 `td` border-bottom 自动继承）：`tr.date-row td { color:var(--dim); font-size:12px; padding:3px 8px; }`

### 2. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`

- **`test_dashboard_html_full_page`（:662-686）**：末尾追加 3 个静态断言（前端逻辑 stdlib 不可执行，静态存在性即既有惯例 :672-674 同模式）：

```python
# 口径标注 + 跨天日期分组（纯前端逻辑，静态断言锁定存在性）
self.assertIn("自上次重启起累计", html)
self.assertIn("fmtDate", html)
self.assertIn("date-row", html)
```

用例数不变（32），全量断言清单 :666-686 存量全存活。

### 3. 同步归档

- `/Users/peter/.local/bin/` 两文件过测后字节级复制到 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/`（同名两文件），`cmp` 验证（代码不在 git 仓库，worktree 只承载 docs）。

## Acceptance Criteria

- ① `/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` 全绿 32 用例，贴 stdout 末尾 + exit 0。
- ② `/usr/bin/python3 -m py_compile` 两文件 exit 0。
- ③ `launchctl kickstart -k gui/$UID/com.zcode.ctyun-stream-fix-proxy` 后 `curl -s http://127.0.0.1:7921/` 含"自上次重启起累计"、"fmtDate"、"date-row" 三串。
- ④ 实机目检：浏览器打开，「按模型」chip 副标注可见；跨天窗口出现日期分组行，单天窗口无分组行（零结构变化）。
- ⑤ `cmp` bin vs 归档目录两文件均 exit 0。
- ⑥ `node /Users/peter/.zcode/scripts/validate-r31-evidence.mjs` 对本 spec exit 0。

## Risks

- 跨天渲染逻辑浏览器侧执行，测试只能静态断言（验收④目检兜底）；分组行若漏插仅影响展示不影响数据。
- 常驻进程需重启才生效（`DASHBOARD_HTML` :884 为常量字符串，launchd KeepAlive kickstart 即可）；旧页面未刷新前仍渲染旧版。
- `tr.date-row` 与 `tr.hit` 相邻：分组行 dim 弱化色不与 hit 琥珀冲突，CSS 仅一行追加。
- 归档漂移风险：bin/归档 byte-identical 惯例靠验收⑤ cmp 锁定。
- 用户若真实期望是"按模型当日计数"而非"标注清楚"，方案 A 不满足——已在 Exclusions 显式划出，需另行确认后开 B episode。

## Exclusions

- 不改 by_model 统计口径本身：不加当日分桶、不持久化 by_model、不动 `BY_MODEL_CAP`。
- 不改 `/api/stats` 契约、`_record_request`（:233-260）、`stats_snapshot`（:271-281）、持久化格式。
- 不给「剥行流带」/ sparkline 加日期（`fmtTime` 函数体不动，poison strip :749 复用不受影响）。
- 不做时区配置化（固定本地时区，与 `today_key` 同口径）。
- 零新依赖、零新 import；不动 launchd plist、剥行正则、鉴权、upstream 解析。

## R31 Evidence

[R31-S1] 口径现场命中（2026-09-07 本 episode 定位实测）：footer 只交代累计/按天持久化、无 by_model 窗口说明；持久化函数明确不含 by_model——
```
$ grep -n '按模型\|累计与按天计数' /Users/peter/.local/bin/ctyun-stream-fix-proxy.py
621:    <div class="card-title">按模型</div>
648:  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json）；
$ grep -n 'counters = {k: STATS\[k\]' /Users/peter/.local/bin/ctyun-stream-fix-proxy.py
140:        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total")}
```
[R31-S2] 跨天现场：时间列仅 toLocaleTimeString；recent 为 maxlen=100 内存窗口；baseline 测试数 32（grep 实测）——
```
$ grep -n 'toLocaleTimeString' /Users/peter/.local/bin/ctyun-stream-fix-proxy.py
664:  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
$ grep -c 'def test_' /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py
32
```
