# ctyun-proxy dashboard 计数口径标注 + 最近请求跨天日期分组 PLAN（simplified mode）

- **Feature**: dashboard 两处纯前端展示修复——①「按模型」card-title 加 chip 副标注「自上次重启起累计，重启清零」+ footer 补一句口径说明；②「最近请求（新在上）」表跨天时按本地日期插 `<tr class="date-row">` 分组行（单天零结构变化）。Python 统计行为、`/api/stats` 契约、代理转发零变更。
- **Branch**: `fix/ctyun-dashboard-window`（worktree `.worktrees/ctyun-dashboard-window`，仅承载 docs）
- **Spec**: `docs/superpowers/specs/2026-09-07-ctyun-proxy-dashboard-window-and-crossday-design.md`
- **Date**: 2026-09-07
- **实现落点**: `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py` + `ctyun-stream-fix-proxy.test.py`（不在 git 仓库，无 commit；过测后归档 `~/Documents/project/ctyun-stream-fix-proxy/` byte-identical）

## Global Constraints

- 运行/测试一律 `/usr/bin/python3` 绝对路径（Homebrew 3.14.7 `http.server` 挂死坑）。
- **零 Python 行为变更**：生产文件只改 `_DASHBOARD_SRC` 内嵌字符串（:522-883）内的 HTML/CSS/JS；`STATS`（:53-54）、`today_key`（:113-115）、`save_stats_counters`（:137-152）、`_record_request`、`stats_snapshot`、持久化格式一律不动。
- **存量子串断言零破坏**：`test_dashboard_html_full_page`（test :662-686）既有 12 条断言全存活，特别是 `assertNotIn("innerHTML", html)`（test :674）——新增 JS 只准走 `el()`/`textContent`/`colSpan`。
- 用例数恒 32：新断言全部追加在既有 `test_dashboard_html_full_page` 方法尾部，不新增测试方法。
- 零新 import、零新依赖；不动 launchd plist、剥行正则、鉴权、upstream 解析、`fmtTime` 函数体。
- baseline（2026-09-07 实测）：`Ran 32 tests in 12.312s / OK / exit 0`；本计划全程维持 32 用例。
- 每卡执行者同次 dispatch 内跑完卡内验证命令并贴实际 stdout + exit code，不跑完不 claim 完成。

## Task 1: 「按模型」口径 chip 副标注 + footer 补句（tier B → executor）

**文件**: `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` + `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`

**1a. 先写失败测试**——`test_dashboard_html_full_page` 方法尾部（test :683-686 四行块全文唯一）之后追加：

改前（锚点，:683-686）：

```python
        # v1.2：按天统计卡（日期表 + 双口径错误列）
        self.assertIn("按天统计", html)
        self.assertIn("<th>上游5xx</th>", html)
        self.assertIn('id="daily-body"', html)
```

改后：

```python
        # v1.2：按天统计卡（日期表 + 双口径错误列）
        self.assertIn("按天统计", html)
        self.assertIn("<th>上游5xx</th>", html)
        self.assertIn('id="daily-body"', html)
        # v1.3：按模型口径标注（自上次重启起累计，重启清零）
        self.assertIn("自上次重启起累计", html)
```

**红相确认**：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -8
```

通过标准：`Ran 32 tests` + `FAILED (failures=1)`（exit 1），失败恰为 `test_dashboard_html_full_page`（`'自上次重启起累计' not found`），其余 31 绿。

**1b. 生产实现（两处，均在 `_DASHBOARD_SRC` 内）**：

改前①（生产 :621，全文唯一）：

```html
    <div class="card-title">按模型</div>
```

改后①（chip 复用 :551 `.chip` 样式，同 :600 upstream-source 用法）：

```html
    <div class="card-title">按模型<span class="chip">自上次重启起累计，重启清零</span></div>
```

改前②（生产 :647-649，footer 首句，三行块全文唯一）：

```html
<footer><div class="inner">
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json）；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（与顶部错误数同口径），
```

改后②（首句后纯插入一行，其余行不动）：

```html
<footer><div class="inner">
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json）；
  按模型计数自进程启动累计，不持久化（重启清零）；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（与顶部错误数同口径），
```

**绿相验证**：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
/usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py && echo COMPILE_OK
```

通过标准：`Ran 32 tests` + `OK`（exit 0）；`COMPILE_OK`（exit 0）。

## Task 2: fmtDate + renderRecent 跨天日期分组 + 分组行 CSS（tier B → executor）

**文件**: `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` + `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`

**2a. 先写失败测试**——Task 1 追加的两行之后继续追加：

改前（锚点，Task 1 后的方法尾部）：

```python
        # v1.3：按模型口径标注（自上次重启起累计，重启清零）
        self.assertIn("自上次重启起累计", html)
```

改后：

```python
        # v1.3：按模型口径标注（自上次重启起累计，重启清零）
        self.assertIn("自上次重启起累计", html)
        # v1.3：跨天日期分组（纯前端逻辑，静态断言锁定存在性，目检兜底见 Task 3）
        self.assertIn("fmtDate", html)
        self.assertIn("date-row", html)
```

**红相确认**：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -8
```

通过标准：`Ran 32 tests` + `FAILED (failures=1)`（exit 1），失败恰为 `test_dashboard_html_full_page`（`'fmtDate' not found`），其余 31 绿。

**2b. 生产实现（三处，均在 `_DASHBOARD_SRC` 内）**：

改前①（生产 :582-583，两行块全文唯一）：

```css
tr.hit td { color:var(--amber); }
tr.hit td.path, td.path { color:inherit; }
```

改后①（追加一行；border-bottom 由 :579 `th, td` 规则自动继承）：

```css
tr.hit td { color:var(--amber); }
tr.hit td.path, td.path { color:inherit; }
tr.date-row td { color:var(--dim); font-size:12px; padding:3px 8px; }
```

改前②（生产 :663-666，四行块全文唯一）：

```js
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function fmtDur(ms) {
```

改后②（`fmtTime` 函数体逐字不动，仅其后插入 `fmtDate`；手工拼接避免 locale 差异，与 `today_key` :113-115 `time.localtime` 同本地时区口径）：

```js
function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function fmtDate(ts) {
  var d = new Date(ts * 1000);
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}
function fmtDur(ms) {
```

改前③（生产 :698-720，`renderRecent` 整函数替换，函数体全文唯一）：

```js
function renderRecent(list) {
  var body = $("req-body");
  body.textContent = "";
  for (var i = list.length - 1; i >= 0; i--) {
    var r = list[i];
    var tr = el("tr", r.filtered > 0 ? "hit" : "");
    tr.appendChild(el("td", "num", fmtTime(r.ts)));
    tr.appendChild(el("td", "", r.method));
    tr.appendChild(el("td", "path", r.path));
    tr.appendChild(el("td", "path", r.model || "—"));
    tr.appendChild(el("td", "num", String(r.status)));
    tr.appendChild(el("td", "num", fmtDur(r.dur_ms)));
    tr.appendChild(el("td", "num", r.filtered > 0 ? String(r.filtered) : "0"));
    body.appendChild(tr);
  }
  if (list.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无请求 —— 有 SSE 流经过代理后这里会出现记录");
    td0.colSpan = 7;
    tr0.appendChild(td0);
    body.appendChild(tr0);
  }
}
```

改后③（七列行构造与空态逐字保留；`multiDay` 基于本页 100 条内存窗口判定，单天时 DOM 与现状逐字节等价——零分组行；新在上遍历中日期切换点插分组行）：

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
      if (d !== lastDate) {  // 新在上遍历：日期切换点即插分组行
        var trd = el("tr", "date-row");
        var tdd = el("td", "", d);
        tdd.colSpan = 7;
        trd.appendChild(tdd);
        body.appendChild(trd);
        lastDate = d;
      }
    }
    var tr = el("tr", r.filtered > 0 ? "hit" : "");
    tr.appendChild(el("td", "num", fmtTime(r.ts)));
    tr.appendChild(el("td", "", r.method));
    tr.appendChild(el("td", "path", r.path));
    tr.appendChild(el("td", "path", r.model || "—"));
    tr.appendChild(el("td", "num", String(r.status)));
    tr.appendChild(el("td", "num", fmtDur(r.dur_ms)));
    tr.appendChild(el("td", "num", r.filtered > 0 ? String(r.filtered) : "0"));
    body.appendChild(tr);
  }
  if (list.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无请求 —— 有 SSE 流经过代理后这里会出现记录");
    td0.colSpan = 7;
    tr0.appendChild(td0);
    body.appendChild(tr0);
  }
}
```

**绿相验证**：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
/usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py && echo COMPILE_OK
```

通过标准：`Ran 32 tests` + `OK`（exit 0，含既有 `assertNotIn("innerHTML")` 存活）；`COMPILE_OK`（exit 0）。

## Task 3: 常驻进程生效验证 + 跨天运行时判据 + 归档同步（tier C → implementer）

**C 档留白理由（命中两条）**：
1. **真机验证**：`DASHBOARD_HTML`（生产 :884）是模块加载期常量，须 `launchctl kickstart -k` 重启常驻进程才生效；chip 可见性与分组行渲染需真机浏览器目检（spec 验收③④）。
2. **运行时数据**：跨天分组行是否出现取决于 `RECENT_REQUESTS`（deque maxlen=100，生产 :57）当前是否真含两天数据——运行时窗口内容 PLAN 阶段不可预知，需 `/api/stats` 实测 `recent[].ts` 判定，预期 DOM 无法静态写死。

**执行序列**（implementer 同次 dispatch 内完成，除标注"主代理/用户"两步）：

3a. 重跑全量（绿相复核）：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
```

通过标准：`Ran 32 tests` + `OK`（exit 0）。

3b. **重启常驻进程（主代理执行；implementer 硬约束禁进程管理命令）**：

```
launchctl kickstart -k gui/$UID/com.zcode.ctyun-stream-fix-proxy
```

3c. 新进程生效 + 三串静态检查（implementer 执行，sleep 2 等 KeepAlive 拉起）：

```
sleep 2 && curl -s http://127.0.0.1:7921/ | grep -o -e '自上次重启起累计' -e 'fmtDate' -e 'date-row' | sort | uniq -c
```

通过标准：三串各 ≥1 次命中（spec 验收③）；零命中 = 重启未生效或改错位置。

3d. **跨天运行时数据判定**（implementer 执行，C 档判断主体）：

```
curl -s http://127.0.0.1:7921/api/stats | /usr/bin/python3 -c "import json,sys,time; r=json.load(sys.stdin)['recent']; days={time.strftime('%Y-%m-%d', time.localtime(x['ts'])) for x in r}; print('entries=%d days=%s multiDay=%s' % (len(r), sorted(days), len(days)>1))"
```

判定分支：
- `multiDay=True` → 浏览器（用户目检，spec 验收④）应见日期分组行，每组首行上方一条 dim 弱化日期行；
- `multiDay=False` → 当前窗口单天，分组行**不出现**（零结构变化，与现状 DOM 等价）——这是正确行为而非缺陷；跨天目检顺延至窗口自然跨天后由用户确认，不阻塞本卡（spec Risks 第 1 条：分组行漏插仅影响展示不影响数据，静态断言 + 三串检查已锁存在性）。

3e. **归档同步**（implementer 执行）：

```
cp -p /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
cp -p /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py
cmp /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py && echo PY_IDENTICAL
cmp /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py && echo TEST_IDENTICAL
```

通过标准：`PY_IDENTICAL` + `TEST_IDENTICAL`（spec 验收⑤，cmp exit 0）。baseline（2026-09-07 实测）两文件 cmp 已一致，改后同步应恢复一致。

**验证命令补充（主代理 spec 完整性用，非 implementer 步骤）**：

```
node /Users/peter/.zcode/scripts/validate-r31-evidence.mjs docs/superpowers/specs/2026-09-07-ctyun-proxy-dashboard-window-and-crossday-design.md
```

通过标准：exit 0（spec 验收⑥）。
