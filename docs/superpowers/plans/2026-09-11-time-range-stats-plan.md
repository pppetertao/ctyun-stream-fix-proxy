# PLAN — 时间维度统计（近3天/近7天/本月/上月）

## Header

- **Spec**: `docs/superpowers/specs/2026-09-11-time-range-stats-design.md`（db6002d 已 commit 于本 worktree 分支）
- **Worktree**: `/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/time-range-stats`，分支 `fix/time-range-stats`
- **日期**: 2026-09-11
- **卡数**: 4（≤4，小计划，单次 PLAN-B dispatch 完成全文）
- **Tier 分布**: Task 1 / 2 / 3 = A/B（完整代码已写死，派 `executor` 转写）；Task 4 = C（派 `implementer`，命中留白：① 真机/运行时验证依赖——dashboard 浏览器手动验证；② 运行时数据依赖——真进程 `/api/stats` curl 新旧键集 diff。JS 无测试基建（spec D4 明示），浏览器交互行为无法在 PLAN 阶段写死自动化断言）
- **目标一句话**: 顶卡 + 按天主表 + 按天×模型副表按四个时间维度聚合切换，后端复用 daily 桶下发 `range_stats`/`range_bounds` 加性字段，前端零日历运算。

### Baseline（写失败测试前登记）

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py
----------------------------------------------------------------------
Ran 41 tests in 20.548s

OK
Exit code: 0
```

- 初始红清单：空（41/41 全绿；stderr 仅有既有 ResourceWarning（unclosed FakeProc 管道），非本计划引入，不处理）。
- `/usr/bin/python3` = Python 3.9.6（`datetime.date.fromisoformat` 3.7+ 可用）。
- 测试计数演进：baseline 41 → Task 1 后 44（+3）→ Task 2 后 45（+1）→ Task 3 后 45（扩展既有用例，不加方法）。

## Global Constraints

1. **Python 解释器锁死 `/usr/bin/python3`**：Homebrew Python 3.14 的 `http.server.HTTPServer` 构造挂死（README:69 已记载）。所有测试/运行命令一律 `/usr/bin/python3`。
2. **零新依赖**：仅新增 stdlib `import datetime`；docstring:6 "纯 stdlib" 仍成立。
3. **持久化 schema 零改动**：`save_stats_counters`/`load_stats_counters`/`load_daily_buckets`/`_prune_daily`/`DAILY_RETENTION_DAYS` 一律不动；`range_stats`/`range_bounds` 是 snapshot 内存字段，**不落盘**。
4. **/api/stats 严格加性**：原 12 键（requests_total/filtered_total/errors_total/empty_retries_total/active/daily/daily_by_model/recent/poison_previews/uptime_s/upstream_base/upstream_source）键名与语义零变动，仅新增 `range_stats`/`range_bounds` 两键。
5. **前端禁日历运算**（spec D4/Risks）：JS 只做 bounds 字符串比较（`bounds[0] <= k && k <= bounds[1]`），不得在 JS 内写 date 计算；动态数据一律 `textContent`，禁 `innerHTML`。
6. **TDD**：Task 1/2/3 严格红→绿（先落测试代码跑出失败，再落生产代码）；Task 4 的 README 为文档（TDD 豁免），运行时验证按卡内脚本执行并贴 stdout。
7. **提交纪律**：每卡一个 commit，Conventional Commits；commit 前自检 `git config --get user.name && git config --get user.email` 为期望身份、分支为 `fix/time-range-stats`、`git status` 仅含本卡文件。
8. **LF 换行**；中文与半角符号排版沿用现有文件风格。
9. **每卡 VERIFY**：跑卡内验证命令并贴实际 stdout + exit code；全绿才可 claim 完成。

---

## Task 1 — 后端纯函数三件套（tier A/B → executor）

**改动文件**: `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
**Commit**: `feat(stats): 时间维度纯函数 range_bounds/aggregate_daily_range/range_stats + RANGE_KEYS`

### 锚点（已实测）

- import 区 :12-25（`import collections` 与 `import hmac` 之间插入；grep 确认当前无 `import datetime`）
- :45 `DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）`（其后插入 RANGE_KEYS）
- :160-162 `today_key()` 与 :165 `def _prune_daily` 之间的双空行区（三个纯函数插入点）
- `_DAILY_FIELDS` 定义于 :224（晚于插入点；`aggregate_daily_range` 函数体内引用，调用期求值，模块级定义顺序无碍）
- 测试插入点：`ctyun-stream-fix-proxy.test.py` `test_stats_snapshot_shape` 方法结束（:496 `self.assertIn("data:null", snap["poison_previews"][-1]["preview"])`）与 :498 `def test_daily_bucket_accumulation_and_dual_error_semantics` 之间

### 步骤 1：测试代码先行（红）

在 `ProxyDashboardUnitTest` 内、`test_stats_snapshot_shape` 之后插入以下三个完整方法（缩进 4 空格，与类内既有方法一致）：

```python
    def test_range_bounds_calendar_edges(self) -> None:
        f = self.mod.range_bounds
        # 月初切片：mtd 仅当日；上月=完整 2 月（2026 非闰年 28 天）
        self.assertEqual(f("2026-03-01", "mtd"), ("2026-03-01", "2026-03-01"))
        self.assertEqual(f("2026-03-01", "last_month"), ("2026-02-01", "2026-02-28"))
        # 跨年：1 月初的上月 = 上年 12 月整月
        self.assertEqual(f("2026-01-01", "last_month"), ("2025-12-01", "2025-12-31"))
        # 闰日 today：mtd 含 02-29；闰年 2 月整月（2024 闰）
        self.assertEqual(f("2024-02-29", "mtd"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(f("2024-03-31", "last_month"), ("2024-02-01", "2024-02-29"))
        # 滑动窗口含今日
        self.assertEqual(f("2026-03-01", "3d"), ("2026-02-27", "2026-03-01"))
        self.assertEqual(f("2026-03-01", "7d"), ("2026-02-23", "2026-03-01"))
        with self.assertRaises(ValueError):
            f("2026-03-01", "30d")

    def test_aggregate_daily_range_sums_and_days(self) -> None:
        f = self.mod.aggregate_daily_range
        daily = {
            "2026-02-01": {"requests": 3, "filtered": 1, "errors_proxy": 0,
                           "errors_upstream": 1, "retries": 0},
            "2026-02-02": {"requests": 5},  # 缺字段桶：缺按 0
            "2026-03-01": {"requests": 7, "filtered": 2, "errors_proxy": 1,
                           "errors_upstream": 0, "retries": 4},  # 窗口外
        }
        out = f(daily, "2026-02-01", "2026-02-28")
        self.assertEqual(out, {"requests": 8, "filtered": 1, "errors_proxy": 0,
                               "errors_upstream": 1, "retries": 0, "days": 2})
        # 空窗口：全 0 + days=0
        self.assertEqual(f(daily, "2025-01-01", "2025-01-31"),
                         {"requests": 0, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0, "days": 0})
        # 端点闭合：start/end 当天都计入
        self.assertEqual(f(daily, "2026-02-02", "2026-02-02")["days"], 1)
        self.assertEqual(f(daily, "2026-02-02", "2026-02-02")["requests"], 5)

    def test_range_stats_all_four_keys(self) -> None:
        mod = self.mod
        daily = {"2026-02-28": {"requests": 2, "filtered": 1, "errors_proxy": 0,
                                "errors_upstream": 0, "retries": 0},
                 "2026-03-01": {"requests": 4, "filtered": 0, "errors_proxy": 1,
                                "errors_upstream": 0, "retries": 0}}
        plan = mod.range_stats(daily, today="2026-03-01")
        self.assertEqual(set(plan), {"stats", "bounds"})
        self.assertEqual(set(plan["stats"]), set(mod.RANGE_KEYS),
                         "range_stats must cover exactly the four RANGE_KEYS")
        self.assertEqual(set(plan["bounds"]), set(mod.RANGE_KEYS))
        for key in mod.RANGE_KEYS:
            self.assertEqual(set(plan["stats"][key]),
                             {"requests", "filtered", "errors_proxy",
                              "errors_upstream", "retries", "days"})
            self.assertEqual(len(plan["bounds"][key]), 2)
        # 3d 窗口 = [02-27, 03-01]：两天桶都在窗内
        self.assertEqual(plan["stats"]["3d"]["requests"], 6)
        self.assertEqual(plan["stats"]["3d"]["days"], 2)
        # mtd 窗口 = [03-01, 03-01]：仅当日桶
        self.assertEqual(plan["stats"]["mtd"]["requests"], 4)
        self.assertEqual(plan["stats"]["mtd"]["days"], 1)
        # last_month = [02-01, 02-28]：仅 02-28 桶
        self.assertEqual(plan["stats"]["last_month"]["requests"], 2)
        self.assertEqual(plan["bounds"]["7d"], ["2026-02-23", "2026-03-01"])
```

跑红（预期 3 个新用例 ERROR/FAIL：`AttributeError: module has no attribute 'range_bounds'`）：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py -v 2>&1 | grep -E "test_(range_bounds|aggregate_daily_range|range_stats)"
```

### 步骤 2：生产代码（绿）

**2a. import 区**（:13-14 之间插入一行）：

```python
import collections
import datetime
import hmac
```

**2b. 常量区**（:45 之后新增一行）：

```python
DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）
RANGE_KEYS = ("3d", "7d", "mtd", "last_month")  # 时间维度 tab 键序（快照/dashboard 共用）
```

**2c. 三个纯函数**（插入 `today_key()` 与 `_prune_daily` 之间的双空行区）：

```python
def range_bounds(today: str, range_key: str) -> tuple:
    """时间维度窗口 [start, end]（ISO 日期闭区间；时区口径由调用方传入的 today 决定）。

    3d/7d = 含今日的滑动窗口；mtd = [当月1日, today]；last_month = 上个自然月整月。
    ISO 日期字符串字典序即时间序（_prune_daily 同性质）。非法 range_key → ValueError。
    """
    d = datetime.date.fromisoformat(today)
    if range_key == "3d":
        start, end = d - datetime.timedelta(days=2), d
    elif range_key == "7d":
        start, end = d - datetime.timedelta(days=6), d
    elif range_key == "mtd":
        start, end = d.replace(day=1), d
    elif range_key == "last_month":
        last_month_end = d.replace(day=1) - datetime.timedelta(days=1)
        start, end = last_month_end.replace(day=1), last_month_end
    else:
        raise ValueError("unknown range_key: %r" % (range_key,))
    return (start.isoformat(), end.isoformat())


def aggregate_daily_range(daily: dict, start: str, end: str) -> dict:
    """窗口 [start, end]（含端点）内 daily 桶逐字段求和；缺字段按 0。

    返回 6 键 dict：_DAILY_FIELDS 五字段 + days=命中桶数。
    """
    out = {field: 0 for field in _DAILY_FIELDS}
    days = 0
    for key, bucket in daily.items():
        if not (start <= key <= end):
            continue
        days += 1
        for field in _DAILY_FIELDS:
            out[field] += bucket.get(field, 0)
    out["days"] = days
    return out


def range_stats(daily: dict, today: str = None) -> dict:
    """四个时间维度的聚合计划：{"stats": {key: aggregate}, "bounds": {key: [start, end]}}。

    today 缺省 today_key()（测试可注入固定日期）；stats_snapshot 据此填充加性字段。
    """
    if today is None:
        today = today_key()
    stats, bounds = {}, {}
    for range_key in RANGE_KEYS:
        start, end = range_bounds(today, range_key)
        stats[range_key] = aggregate_daily_range(daily, start, end)
        bounds[range_key] = [start, end]
    return {"stats": stats, "bounds": bounds}
```

### 验证命令

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py -v 2>&1 | grep -E "test_(range_bounds|aggregate_daily_range|range_stats)"
```

预期：`Ran 44 tests ... OK`，exit 0；3 个新用例各 `... ok`；既有 41 用例零回归。

### 验收

- `range_bounds("2026-03-01","mtd") == ("2026-03-01","2026-03-01")`；`last_month` 跨年/闰年边界全过；非法 key ValueError。
- `aggregate_daily_range` 缺字段按 0、空窗口全 0、端点闭合。
- `range_stats` 恰覆盖 4 键，每键 6 字段。

---

## Task 2 — stats_snapshot 透出 range_stats/range_bounds（tier A/B → executor）

**改动文件**: `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
**Commit**: `feat(stats): stats_snapshot 透出 range_stats/range_bounds 加性字段`
**依赖**: Task 1 已合入（`range_stats` 可用）。

### 锚点（已实测）

- `ctyun-stream-fix-proxy.py:349-361` `stats_snapshot()` 整函数替换（STATS_LOCK 块零改动，新增代码在锁外）
- 测试插入点：Task 1 落盘后 `test_range_stats_all_four_keys` 方法的最后一行（`self.assertEqual(plan["bounds"]["7d"], ["2026-02-23", "2026-03-01"])`）之后

### 步骤 1：测试代码先行（红）

在 `ProxyDashboardUnitTest` 内、`test_range_stats_all_four_keys` 之后插入：

```python
    def test_stats_snapshot_includes_range_stats(self) -> None:
        mod = self.mod
        today = mod.today_key()
        bucket = mod.STATS["daily"].setdefault(
            today, {"requests": 0, "filtered": 0, "errors_proxy": 0,
                    "errors_upstream": 0, "retries": 0})
        base = dict(bucket)
        mod._record_request("POST", "/rs", 200, 1.0, 1)
        snap = mod.stats_snapshot()
        self.assertEqual(bucket["requests"], base["requests"] + 1)
        self.assertEqual(set(snap["range_stats"]), set(mod.RANGE_KEYS))
        self.assertEqual(set(snap["range_bounds"]), set(mod.RANGE_KEYS))
        # 当日桶落在含今日的窗口内：7d/mtd 的 requests 恰等于窗内桶求和（独立 oracle：
        # 测试侧自行按 bounds 字符串过滤 snap["daily"] 求和，不复用被测聚合实现）
        for key in ("7d", "mtd"):
            start, end = snap["range_bounds"][key]
            self.assertTrue(start <= today <= end,
                            "%s window must include today" % key)
            expected = sum(b.get("requests", 0)
                           for k, b in snap["daily"].items() if start <= k <= end)
            self.assertEqual(snap["range_stats"][key]["requests"], expected)
            self.assertGreaterEqual(snap["range_stats"][key]["requests"],
                                    base["requests"] + 1)
```

跑红（预期 `KeyError: 'range_stats'`）：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py -v 2>&1 | grep "test_stats_snapshot_includes_range_stats"
```

### 步骤 2：生产代码（绿）

`stats_snapshot()` 整函数替换为（唯一差异：末尾 3 行新增，锁内零改动）：

```python
def stats_snapshot() -> dict:
    with STATS_LOCK:
        snap = dict(STATS)
        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
        snap["daily_by_model"] = {
            d: {m: dict(v) for m, v in models.items()}
            for d, models in STATS["daily_by_model"].items()}
        snap["recent"] = list(RECENT_REQUESTS)
        snap["poison_previews"] = list(POISON_PREVIEWS)
    snap["uptime_s"] = int(time.time() - STARTED_AT)
    snap["upstream_base"] = UPSTREAM_BASE
    snap["upstream_source"] = _upstream_source
    plan = range_stats(snap["daily"])  # 锁外基于副本计算（4×≤31 桶求和 <1ms），不拉长持锁
    snap["range_stats"] = plan["stats"]
    snap["range_bounds"] = plan["bounds"]
    return snap
```

### 验证命令

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
```

预期：`Ran 45 tests ... OK`，exit 0。既有 `test_stats_snapshot_shape`/`test_stats_snapshot_daily_is_copy`/`test_stats_persist_roundtrip_and_defaults`（持久化 schema 零改动证据）与 `AdminIntegrationTest.test_dashboard_and_stats_served`（assertIn 键集，加性不破坏）零回归。

### 验收

- snapshot 含 `range_stats`/`range_bounds`，各恰 4 键；`7d`/`mtd` 窗口含今日且聚合值=窗内桶求和。
- 持久化文件键集不变（`save_stats_counters` 未触碰）。

---

## Task 3 — dashboard 四时间维度切换（tier A/B → executor）

**改动文件**: `ctyun-stream-fix-proxy.py`（`_DASHBOARD_SRC` 内 CSS/HTML/JS + footer）、`ctyun-stream-fix-proxy.test.py`
**Commit**: `feat(dashboard): 顶卡四时间维度 tab + 主/副表 bounds 过滤 + 新错误口径 footer`
**依赖**: Task 2 已合入（snapshot 下发 bounds）。

### 锚点（已实测，全部在 `_DASHBOARD_SRC` 内）

- CSS：`.stat .label { color:var(--dim); font-size:12px; }`（:690）之后插入
- HTML 顶卡：`<section class="stats-row">`（:731）之前插入 tabs；`#st-active` label 行 :735
- 主表标题 :743 `按天统计（最近 14 天，新在上）`（grep 全文件唯一）；副表标题 :752
- footer :774-780（旧句"与顶部错误数同口径" grep 全文件唯一，:778）
- JS：`var SOURCE_LABEL = ...`（:790）之后；`renderStats` :819-831；`renderDaily` :885-908（:888 `slice(0, 14)`）；`renderDailyByModel` :909-940（:912 `slice(0, 14)`）；`poll()` then 块 :996-1004；`poll();\nsetInterval(poll, 2000);` :1042-1043
- 测试：`ctyun-stream-fix-proxy.test.py` `test_dashboard_html_full_page` 内 `self.assertIn("date-row", html)`（:837，方法末行）之后插入断言
- 已确认：`slice(0, 14)` 全文件恰 2 处（:888/:912）；`最近 14 天` 在 .py 恰 1 处（:743）；测试文件无 "与顶部错误数同口径"/"最近 14 天" 断言

### 步骤 1：测试代码先行（红）

`test_dashboard_html_full_page` 方法末尾（`self.assertIn("date-row", html)` 之后）追加：

```python
        # 时间维度切换：4 tab / 默认 7d / bounds 字符串过滤 / 零请求重渲染 / 新错误口径
        self.assertEqual(html.count('class="range-tab"'), 4)
        for rk in ("3d", "7d", "mtd", "last_month"):
            self.assertIn('data-range="%s"' % rk, html)
        self.assertIn("活跃连接·实时", html)
        self.assertIn('var selectedRange = "7d"', html)
        self.assertIn("range_bounds[selectedRange]", html)
        self.assertIn("lastSnap = snap", html)
        self.assertIn("rs.errors_proxy + rs.errors_upstream", html)
        self.assertIn("renderRangeTabs", html)
        self.assertIn('id="daily-title-range"', html)
        self.assertIn('id="daily-model-title-range"', html)
        self.assertNotIn("slice(0, 14)", html)
        self.assertNotIn("最近 14 天", html)
        self.assertNotIn("与顶部错误数同口径", html)
```

跑红（预期多条 assertIn 失败 / count=0）：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py -v 2>&1 | grep -A3 "test_dashboard_html_full_page"
```

### 步骤 2：生产代码（绿）

**2a. CSS**（`.stat .label { ... }` 行后插入）：

```css
.range-tabs { display:flex; gap:8px; flex-wrap:wrap; }
.range-tab { font-weight:400; font-size:12.5px; background:transparent; color:var(--dim);
  border:1px solid var(--line); border-radius:99px; padding:4px 14px; }
.range-tab:hover { filter:none; color:var(--text); }
.range-tab.active { background:var(--amber); color:var(--ink); border-color:var(--amber); font-weight:700; }
```

**2b. HTML tabs**（`<section class="stats-row">` 之前插入；`#st-active` 卡 label 改"活跃连接·实时"）：

```html
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
```

（注：初始 HTML 不预置 `active` 类，由启动时 `applyRange()` 设置——保证静态断言 `count('class="range-tab"') == 4` 稳定。）

**2c. 两表标题**（range 词加 span 供 JS 联动）：

```html
    <div class="card-title">按天统计（<span id="daily-title-range">近7天</span>，新在上）</div>
```

```html
    <div class="card-title">按天 × 模型（<span id="daily-model-title-range">近7天</span>，内存累计，重启清零）</div>
```

**2d. JS 状态变量**（`var SOURCE_LABEL = ...;` 行后插入）：

```js
var RANGE_LABELS = { "3d": "近3天", "7d": "近7天", "mtd": "本月", "last_month": "上月" };
var selectedRange = "7d";  // 刷新不记忆（无 localStorage），默认近7天
var lastSnap = null;       // poll 缓存：tab 点击零请求重渲染
```

**2e. renderStats 整函数替换**（:819-831 → 下述；active 仍读实时 gauge，五值改读 `range_stats[selectedRange]`，错误数=维度内两口径合计，requests=0 毒行率"—"）：

```js
function renderStats(snap) {
  var rs = snap.range_stats && snap.range_stats[selectedRange];
  if (rs) {
    $("st-requests").textContent = rs.requests;
    $("st-filtered").textContent = rs.filtered;
    $("st-rate").textContent = rs.requests > 0
      ? ((rs.filtered / rs.requests) * 100).toFixed(1) + "%" : "—";
    $("st-errors").textContent = rs.errors_proxy + rs.errors_upstream;
  }
  $("st-active").textContent = snap.active;
  $("uptime").textContent = "运行时长 " + fmtUptime(snap.uptime_s);
  var base = $("upstream-base");
  base.textContent = snap.upstream_base;
  base.classList.remove("dimmed");
  $("upstream-source").textContent = SOURCE_LABEL[snap.upstream_source] || snap.upstream_source;
}
```

**2f. renderDaily 整函数替换**（:885-908 → 下述；废除 `slice(0, 14)`，改 bounds 字符串比较过滤，行数上限=窗口自然上界）：

```js
function renderDaily(daily) {
  var body = $("daily-body");
  body.textContent = "";
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var keys = Object.keys(daily).sort().reverse().filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
  if (keys.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天统计");
    td0.colSpan = 6;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    return;
  }
  for (var i = 0; i < keys.length; i++) {
    var b = daily[keys[i]];
    var tr = el("tr", (b.errors_proxy + b.errors_upstream) > 0 ? "hit" : "");
    tr.appendChild(el("td", "num", keys[i]));
    tr.appendChild(el("td", "num", String(b.requests)));
    tr.appendChild(el("td", "num", String(b.filtered)));
    tr.appendChild(el("td", "num", String(b.errors_proxy)));
    tr.appendChild(el("td", "num", String(b.errors_upstream)));
    tr.appendChild(el("td", "num", String(b.retries || 0)));
    body.appendChild(tr);
  }
}
```

**2g. renderDailyByModel 整函数替换**（:909-940 → 下述；days 键同一 bounds 过滤，模型行渲染逻辑不变）：

```js
function renderDailyByModel(dbm) {
  var body = $("daily-model-body");
  body.textContent = "";
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var days = Object.keys(dbm).sort().reverse().filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天 × 模型统计");
    td0.colSpan = 7;
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
  }
}
```

**2h. renderRangeTabs / applyRange / click 委托**（renderDailyByModel 之后、`renderSpark` 之前插入前两个函数；click 委托插在 upstream-form 监听器块之后）：

```js
function renderRangeTabs() {
  var tabs = document.querySelectorAll(".range-tab");
  for (var i = 0; i < tabs.length; i++) {
    var on = tabs[i].getAttribute("data-range") === selectedRange;
    tabs[i].classList.toggle("active", on);
    tabs[i].setAttribute("aria-selected", on ? "true" : "false");
  }
}
function applyRange() {
  renderRangeTabs();
  var label = RANGE_LABELS[selectedRange] || selectedRange;
  $("daily-title-range").textContent = label;
  $("daily-model-title-range").textContent = label;
  if (lastSnap) {
    renderStats(lastSnap);
    renderDaily(lastSnap.daily || {});
    renderDailyByModel(lastSnap.daily_by_model || {});
  }
}
```

```js
document.querySelector(".range-tabs").addEventListener("click", function (e) {
  var btn = e.target && e.target.closest ? e.target.closest(".range-tab") : null;
  if (!btn) return;
  var key = btn.getAttribute("data-range");
  if (!key || key === selectedRange) return;
  selectedRange = key;
  applyRange();  // 零请求重渲染：直接消费 lastSnap 缓存
});
```

**2i. poll then 块**（:996-1004 → 下述；首行缓存 lastSnap）：

```js
    .then(function (snap) {
      lastSnap = snap;
      renderStats(snap);
      renderRecent(snap.recent || []);
      renderPoison(snap.poison_previews || []);
      renderDaily(snap.daily || {});
      renderDailyByModel(snap.daily_by_model || {});
      renderSpark(snap.recent || []);
      setConn(true);
    })
```

**2j. 启动序列**（文件尾 `poll();\nsetInterval(poll, 2000);` → 下述；`applyRange()` 先行设置默认 tab 高亮与标题，此时 lastSnap=null 不触发渲染）：

```js
applyRange();
poll();
setInterval(poll, 2000);
```

**2k. footer 整块替换**（:774-780；旧句"与顶部错误数同口径"同 commit 废除——spec Risks 点名的口径漂移风险）：

```html
<footer><div class="inner">
  顶部统计卡按所选时间段聚合（近3/近7天为含今日的滑动窗口，今日为部分数据；本月/上月为自然月，本地时区）；
  错误数=该时段内「代理错误+上游5xx」合计；活跃连接恒为实时值，不随时间段变化；
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json，
  日桶保留 90 天，覆盖上月+当月最远 62 天回溯）；
  按天×模型计数自进程启动累计，不持久化（重启清零）；
  按天主表含无 model 请求，各行数值 ≥「按天 × 模型」副表合计，差值即当日无 model 请求；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误，「上游5xx」=上游透传 status≥500
  （499 中断两边都不计）。页面每 2s 轮询 /api/stats，切换时间段用缓存零请求重渲染；非本机修改上游需 X-Admin-Token。
</div></footer>
```

### 验证命令

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
```

预期：`Ran 45 tests ... OK`，exit 0。重点零回归：`test_dashboard_html_full_page`（含新增 v4 断言块全过）、`test_recent_entries_carry_model`（`assertNotIn("by_model")` 不受影响）、既有 `colspan="6"`/`colspan="7"`/`按天统计`/`按天 × 模型` 断言仍命中。

### 验收

- HTML 含 4 个 range-tab（无预置 active）、`活跃连接·实时`、默认 `selectedRange = "7d"`；`slice(0, 14)` 与"最近 14 天"清零。
- renderStats 五值改读 range_stats、错误数两口径合计、毒行率空窗"—"；renderDaily/renderDailyByModel 按 bounds 过滤；poll 缓存 lastSnap；click 委托零请求重渲染。
- footer 新口径文案落地，旧句废除。

---

## Task 4 — README 口径同步 + 运行时验证（tier C → implementer）

**改动文件**: `README.md`（文档，TDD 豁免，文案已写死）
**Commit**: `docs(readme): 时间维度统计口径与 /api/stats 加性字段说明`
**依赖**: Task 1-3 全部合入。
**C 档留白理由**: ① 真机/运行时验证依赖——dashboard 浏览器交互（tab 切换、devtools network 零请求确认）无法在 PLAN 阶段写死自动化断言（JS 无测试基建，spec D4 明示）；② 运行时数据依赖——`/api/stats` 新旧响应键集 diff 需起真进程 + 种子持久化文件实测。PLAN 已把可脚本化部分（curl 断言脚本）与不可脚本化部分（浏览器目检清单）全部写死，implementer 照跑 + 记录，无设计决策空间。

### 步骤 1：README 文案（已写死，逐行替换）

**1a. :16 实时统计行**：

```markdown
  - 实时统计：按所选时间段（近3天/近7天/本月/上月）聚合的请求数 / 剥行 / 毒行率 / 错误数；活跃连接恒实时
```

**1b. :19 按天统计行**（spec Files 未列此行，但旧文"最近 14 天"与实现矛盾，按验收标准"README 口径描述与实现一致"一并修正）：

```markdown
  - 「按天统计」「按天 × 模型」随所选时间段过滤日期（上月最多 31 行；daily 分桶，双口径错误列）
```

**1c. :59 /api/stats 行**：

```markdown
- `GET /api/stats` — 全量统计 JSON（计数 / daily 分桶 / daily_by_model / recent 100 / 毒行流带 / range_stats 四维度聚合 / range_bounds 四维度窗口闭区间；后两键为加性新增，旧消费者零破坏）
```

**1d. :69 用例数事实性纠错**（`32 个用例` → `45 个用例`，同文件现状纠错，其余文字不动）。

**1e. :73-77 口径表整表替换**：

```markdown
| 页面区块 | 口径 | 持久化 |
|----------|------|--------|
| 顶部「请求数 / 剥行 / 错误」 | 按所选时间段聚合（近3/近7天含今日滑动窗口；本月/上月自然月；错误=代理错误+上游5xx 合计） | 是（daily 桶跨重启） |
| 「活跃连接」 | 实时 gauge，不随时间段变化 | 否 |
| 「按天统计」「按天 × 模型」 | 随所选时间段过滤（最远回溯=上月+当月 ≤62 天 < 90 天 retention） | 主表是（90 天 prune）/ 副表否（重启清零） |
| 「最近请求」「剥行流带」 | 最近 100 / 20 条内存窗口 | 否 |
```

**:28 保持不动**（"daily 桶 90 天 prune"描述仍准确，spec 明示保持）。

### 步骤 2：/api/stats curl 实测（运行时，脚本已写死）

在 worktree 根目录执行（端口/持久化全走 seam，不碰真实 7921 与 `~/.local/etc/`；种子含今日/昨日/上月首日三桶——上月首日距今日 ≥28 天，恒在 7d/3d/mtd 窗外，断言与运行日期无关）：

```sh
set -e
TMPD=$(mktemp -d /tmp/ctyun-range-stats-XXXX)
D0=$(/usr/bin/python3 -c 'import datetime; print(datetime.date.today().isoformat())')
D1=$(/usr/bin/python3 -c 'import datetime; print((datetime.date.today()-datetime.timedelta(days=1)).isoformat())')
DLM=$(/usr/bin/python3 -c 'import datetime; d=datetime.date.today().replace(day=1); print((d-datetime.timedelta(days=1)).replace(day=1).isoformat())')
cat > "$TMPD/settings.json" <<EOF
{"upstream_base": "http://127.0.0.1:1",
 "stats": {"requests_total": 9, "filtered_total": 4, "errors_total": 2,
           "empty_retries_total": 0,
           "daily": {"$DLM": {"requests": 5, "filtered": 2, "errors_proxy": 0, "errors_upstream": 0, "retries": 0},
                     "$D1": {"requests": 3, "filtered": 1, "errors_proxy": 0, "errors_upstream": 1, "retries": 0},
                     "$D0": {"requests": 6, "filtered": 3, "errors_proxy": 1, "errors_upstream": 0, "retries": 0}}}}
EOF
CTYUN_LISTEN_PORT=17920 CTYUN_ADMIN_HOST=127.0.0.1 CTYUN_ADMIN_PORT=17921 \
CTYUN_UPSTREAM_BASE=http://127.0.0.1:1 CTYUN_PERSIST_PATH="$TMPD/settings.json" \
/usr/bin/python3 ctyun-stream-fix-proxy.py > "$TMPD/server.log" 2>&1 &
PID=$!
sleep 1
curl -s http://127.0.0.1:17921/api/stats -o "$TMPD/new.json"
kill $PID; wait $PID 2>/dev/null || true
/usr/bin/python3 - "$TMPD/new.json" <<'PYEOF'
import json, sys
snap = json.load(open(sys.argv[1]))
RK = {"3d", "7d", "mtd", "last_month"}
assert set(snap) == {"requests_total", "filtered_total", "errors_total",
                     "empty_retries_total", "active", "daily", "daily_by_model",
                     "recent", "poison_previews", "uptime_s", "upstream_base",
                     "upstream_source", "range_stats", "range_bounds"}, \
       ("key set must be old 12 keys + exactly 2 additive", set(snap))
assert set(snap["range_stats"]) == RK and set(snap["range_bounds"]) == RK
for k, agg in snap["range_stats"].items():
    assert set(agg) == {"requests", "filtered", "errors_proxy",
                        "errors_upstream", "retries", "days"}, (k, set(agg))
    assert isinstance(snap["range_bounds"][k], list) and len(snap["range_bounds"][k]) == 2
assert snap["range_stats"]["7d"]["requests"] == 9, snap["range_stats"]["7d"]
assert snap["range_stats"]["3d"]["requests"] == 9, snap["range_stats"]["3d"]
assert snap["range_stats"]["7d"]["filtered"] == 4
assert snap["range_stats"]["7d"]["errors_proxy"] == 1
assert snap["range_stats"]["7d"]["errors_upstream"] == 1
assert snap["range_stats"]["7d"]["days"] == 2
lm_start, lm_end = snap["range_bounds"]["last_month"]
lm_expected = sum(b.get("requests", 0) for k, b in snap["daily"].items() if lm_start <= k <= lm_end)
assert snap["range_stats"]["last_month"]["requests"] == lm_expected, (lm_expected, snap["range_stats"]["last_month"]["requests"])
assert snap["range_stats"]["last_month"]["days"] == sum(1 for k in snap["daily"] if lm_start <= k <= lm_end)
start, end = snap["range_bounds"]["mtd"]
expect_mtd = sum(b["requests"] for k, b in snap["daily"].items() if start <= k <= end)
assert snap["range_stats"]["mtd"]["requests"] == expect_mtd, (start, end, expect_mtd)
assert snap["requests_total"] == 9 and snap["errors_total"] == 2 and snap["filtered_total"] == 4
print("curl /api/stats range assertions: OK")
PYEOF
rm -rf "$TMPD"
```

预期 stdout 末行：`curl /api/stats range assertions: OK`（即 spec 验收"恰 4 键 × 6 字段、与旧响应 diff 仅新增两键、原字段值不变"的运行时证据）。

### 步骤 3：dashboard 手动验证（浏览器目检，清单已写死）

重新起隔离进程（同步骤 2 环境变量，保持前台后台可控），浏览器打开 `http://127.0.0.1:17921/`，逐项核对并记录结果：

| # | 检查项 | 预期 |
|---|--------|------|
| 1 | 默认 tab | 「近7天」高亮；顶部卡：请求数 9、剥行 4、错误数 2（=代理 1+上游 1） |
| 2 | 切「上月」 | 按天主表仅显示上月种子行（1 行，≤31）；标题变"按天统计（上月，新在上）" |
| 3 | 副表同步 | 「按天 × 模型」同步过滤（种子无模型桶 → 空态文案）；标题 range 词联动 |
| 4 | 活跃连接 | 任意 tab 下恒为实时值（空闲 0），不随维度变 0/变空 |
| 5 | 空窗口毒行率 | 切「上月」等无请求窗口时毒行率显示"—" |
| 6 | 零请求切换 | devtools Network：tab 点击不产生新 `/api/stats` 请求（仅既有 2s 轮询） |
| 7 | footer | 新口径文案（含"错误数=该时段内「代理错误+上游5xx」合计"），无"与顶部错误数同口径" |

验证完 `kill` 进程并清理临时目录。若 dispatch 环境无浏览器，步骤 1-2 照跑、步骤 3 如实标注"待用户目检"并 `STATUS: partial` 回报，不伪造目检结果。

### 验证命令

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
$ grep -c "range_stats" README.md
$ grep -n "62 天" README.md
```

预期：45 tests OK；README `range_stats` ≥1 处（:59 API 行）；62<90 覆盖论证一行存在。

### 验收

- README :16/:19/:59/:69/:73-77 与实现一致；:28 未动。
- curl 脚本全断言通过（键集=旧 12+新 2、4 键 × 6 字段、窗口聚合数值正确、原字段值不变）。
- 浏览器清单 7 项逐项核对（或如实标注待目检）。

---

## Plan Self-Review（5 项）

1. **spec coverage**: spec Files to Change 12 条逐一映射——import datetime/RANGE_KEYS/三纯函数→Task 1；stats_snapshot→Task 2；顶卡 HTML/renderStats/renderDaily/renderDailyByModel/两表标题/poll+renderRangeTabs/footer→Task 3；4 新单测→Task 1(3)+Task 2(1)；README→Task 4。Acceptance 5 条：单测绿+零回归→各卡验证命令；curl 键集 diff→Task 4 步骤 2；日历边界注入断言→Task 1 测试；dashboard 手动验证→Task 4 步骤 3；footer/README 口径一致→Task 3 验收+Task 4 步骤 1。Exclusions 未越界（无月桶/无压缩/无第五 tab/无 localStorage/最近请求表与流带与 sparkline 未动）。
2. **placeholder scan**: 全文无 TODO/待定/伪代码/“此处省略”；所有生产与测试代码均为完整可落盘文本。
3. **type consistency**: `range_bounds -> tuple[str, str]`、`aggregate_daily_range -> 6 键 dict`、`range_stats -> {"stats","bounds"}` 与 snapshot 消费端（`snap["range_stats"]`/`snap["range_bounds"]`）及 JS 读取端（`rs.requests` 等 6 字段、`bounds[0]/bounds[1]`）字段名逐一核对一致；`today: str = None` 与代码库既有 `extra_env: dict = None` 风格一致。
4. **可落盘性**: 每处改动给出精确插入/替换锚点（file:line + 唯一上下文，grep 已验证唯一性：slice(0,14) 恰 2 处、"最近 14 天".py 恰 1 处、"与顶部错误数同口径"恰 1 处、无既有 import datetime）；测试计数演进 41→44→45→45 可机械核对；验证命令均为可直接执行的完整命令行。
5. **锚点实测**: 本 PLAN 撰写前已 Read 核对 :12-25/:45/:160-242/:349-361/:640-780/:819-940/:987-1044 主文件、test :333-362/:362-644/:721-890、README 全文 81 行；baseline 41 tests OK 已登记。两处 spec 邻接微修已显式标注理由（README:19 矛盾修正、:69 用例数纠错），供 reviewer 裁量。
