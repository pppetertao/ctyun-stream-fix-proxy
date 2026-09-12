# 错误/重试数字 hover 时间明细 tooltip 设计（2026-09-12）

R31-EXEMPT: feat-not-bugfix

## Goal
面板中错误/重试类计数支持鼠标 hover 浮动框，列出该计数对应事件的具体时间（新在上，附原因/模型），数字与明细同 key、同重置语义。

## Files to Change

### 1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`

**采集（模块级有界事件流）**

- 常量区 `RECENT_REQUESTS`:106 / `POISON_PREVIEWS`:107 旁新增：
  ```python
  EVENTS = collections.deque(maxlen=100)  # {"ts","kind":"proxy"|"upstream"|"retry","model","status"}
  ```
  单一全局流（kind 区分）而非按 (day,model,kind) 分环：per-key 环形几十个 deque 持久化/清洗成本高，单流 maxlen=100 硬上界等价满足"每 key 有界"，tooltip 按需过滤。
- `_record_request(method, path, status, dur_ms, filtered, model=None, error=False)`:373 — `:403 RECENT_REQUESTS.append` 前（同一 `with STATS_LOCK` 块内）加单次分类 append：
  ```python
  if error or status >= 500:
      # 分类优先级与计数一致（:390-393 error 分支胜过 status>=500）
      EVENTS.append({"ts": time.time(), "kind": "proxy" if error else "upstream",
                     "model": model, "status": status})
  ```
- `_record_empty_retry(model=None)`:417 — `:437 bucket["retries"] += 1` 后、`:438 _stats_dirty = True` 前加：
  ```python
  EVENTS.append({"ts": time.time(), "kind": "retry", "model": model, "status": None})
  ```
  两函数已有 `_stats_dirty = True`，事件随既有 60s flush（`flush_stats_if_dirty`:337）落盘，不加新脏标记。计数逻辑零改动。

**API（扩展现有端点，不新建）**

- `stats_snapshot()`:441 — 锁内 `:449 snap["poison_previews"] = ...` 后加 `snap["events"] = [dict(e) for e in EVENTS]`（逐条浅拷贝，对齐 `snap["daily"]`:444 模式）。`/api/stats`:679-680 handler 零改动。payload 契约（oldest→newest，与 `recent` 同序）：
  ```json
  "events": [
    {"ts": 1757654321.2, "kind": "retry", "model": "glm-4.6", "status": null},
    {"ts": 1757654300.1, "kind": "proxy", "model": null, "status": 502}
  ]
  ```

**持久化（决定：随 daily_by_model 持久化）**

理由：tooltip 注释的 daily 桶计数本身跨重启保留，session-only 会出现"计数 12 / 明细无记录"的对不上；代价 ≤100 entry ≈ <20KB（现文件 <1MB），且清洗 loader 与既有 `_load_persist_file`:264 模式同构，老文件无键 → `[]`，双向零迁移。

- `save_stats_counters()`:241 — 锁内（:243-248）加 `events = [dict(e) for e in EVENTS]`；`:259 "stats": dict(counters, daily=..., daily_by_model=...)` 追加 `events=events`。
- 新增 `load_stats_events(path: str) -> list`（置于 `load_daily_by_model_buckets` 尾 :334 与 `flush_stats_if_dirty`:337 之间）：
  ```python
  def load_stats_events(path: str) -> list:
      """读 stats.events（oldest→newest，≤100 条）；缺/损坏/legacy 无键 → []。"""
      stats = _load_persist_file(path).get("stats")
      raw = stats.get("events") if isinstance(stats, dict) else None
      if not isinstance(raw, list):
          return []
      out = []
      for entry in raw:
          if not isinstance(entry, dict):
              continue
          kind, ts = entry.get("kind"), entry.get("ts")
          model, status = entry.get("model"), entry.get("status")
          if kind not in ("proxy", "upstream", "retry"):
              continue
          if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts < 0:
              continue
          if model is not None and (not isinstance(model, str) or not model or len(model) > 200):
              continue
          if status is not None and (isinstance(status, bool) or not isinstance(status, int)
                                     or not 100 <= status <= 599):
              continue
          out.append({"ts": ts, "kind": kind, "model": model, "status": status})
      return out[-100:]  # 与 deque maxlen 对齐，只留最新
  ```
- `main()`:1211 — `:1217 daily_by_model = load_...` 后锁外加 `events = load_stats_events(PERSIST_PATH)`；锁内 `:1224` 后加 `EVENTS.clear(); EVENTS.extend(events)`（先 clear 防 deque 残留叠加）。

**前端（DASHBOARD_HTML 内，零依赖、fixed 定位防裁剪）**

- CSS（:797-806 区追加）：`.evt-tip { position:fixed; z-index:9; display:none; max-width:340px; max-height:260px; overflow-y:auto; background:var(--panel); border:1px solid var(--amber); border-radius:6px; padding:6px 10px; font-size:12px; pointer-events:none; box-shadow:0 4px 16px rgba(0,0,0,.5); }` — `position:fixed` 逃出 `.table-wrap{overflow-x:auto}`:798 的裁剪上下文。
- HTML：body 尾、`<script>`（`function el`:893 所在块）前加 `<div class="evt-tip" id="evt-tip"></div>`。
- JS 工具区（:893-930 旁）新增：
  - `function markEvents(n, kind, day, model)` — 用 `lastSnap.events` 过滤（kind 为 `"errors"` 时=proxy+upstream 且 `fmtDate(e.ts)` 落在 `lastSnap.range_bounds[selectedRange]`:455 区间，比较法同 :1004-1006；day 给定时 `fmtDate(e.ts)===day`；model 给定时 `e.model===model`），匹配数 >0 才设 `n.dataset.evt/n.dataset.day/n.dataset.model`；计数 0 不设属性 → 无 tooltip。
  - `function showEvtTip(target, x, y)` — document 级委托 `mouseover/mousemove`（`e.target.closest("[data-evt]")`）触发：按 dataset 重新过滤 `lastSnap.events`，倒序遍历（:954 模式）最新在上，每行一个 `el()` 行（kind 标签映射 `{proxy:"代理错误", upstream:"上游5xx", retry:"空流重试"}` + `fmtTime`:904；范围卡行用 `fmtDate+": "+fmtTime`），全部 textContent（:739-740 禁 innerHTML，model 名来自上游请求体）；显示数 < 匹配数时尾部加"共 N 次，仅保留最近 100 条事件记录"；防御空列表显示"无记录"；fixed 定位贴近光标并按视口翻转。
  - `function hideEvtTip()` — `mouseout` 及 `poll()`:1127 每轮重渲染后调用。
- 挂点：`renderStats`:938 `$("st-errors")` 赋值后 → `markEvents(..., "errors", null, null)`；`renderDaily`:1021-1023 三个 td → `markEvents(td, "proxy"/"upstream"/"retry", keys[i], null)`；`renderDailyByModel`:1055-1057 三个 td → `markEvents(td, kind, days[i], names[j])`。

### 2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`

- `test_stats_snapshot_shape`:481 — 补断言 `snap["events"]` 为 list、元素含 ts/kind，且为副本（对齐 `test_stats_snapshot_daily_is_copy`:929 模式）。
- 新增 `test_events_record_and_cap`（`ProxyDashboardUnitTest`:362 内，:620-672 用例旁）：先备份 `mod.EVENTS` 并换新 deque（:474-479 STATS 备份/finally 恢复模式）→ `_record_request(...,502,...,error=True)`→kind=="proxy"；`_record_request(...,500,...)`→"upstream"；`_record_empty_retry("m-a")`→kind=="retry" 且 model=="m-a"；循环 120 条 → `len(snap["events"])==100` 且最老被丢。
- 新增 `test_events_persist_roundtrip`（`test_daily_persist_roundtrip_legacy_and_corrupt`:676 旁）：save→`load_stats_events` 全等；legacy 无键→[]；坏 entry（kind 非法/ts<0/model 空>200/status 越界/非 dict）逐项跳过；150 条读回最新 100。
- 新增 `test_dashboard_tooltip_skeleton`：`'id="evt-tip"'` 与 `position:fixed` 均在 `mod.DASHBOARD_HTML` 中。

### 3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/README.md`

- `:69` 用例数 50 → 新总数；`:75-78` 口径表追加一行：错误/重试数字 hover 明细 | 事件流最近 100 条（代理错误+上游5xx+空流重试） | 是（stats.events 随 60s 周期落盘）。

## Acceptance Criteria
- 采集：error=True→proxy（含 status≥500 时优先级同计数）；500/502 无 error→upstream；`_record_empty_retry`→retry；全部含 ts/model；120 条后 snapshot 恰 100 且丢最老。
- `/api/stats` 响应含 `events`（oldest→newest），改 snap 不影响 `EVENTS`（副本断言过）。
- save→load roundtrip 全等；legacy 无键→`[]`；5 类坏 entry 跳过；150 条取最新 100。
- `main()` 恢复后 `EVENTS` 与落盘一致且无残留叠加（先 clear 后 extend）。
- 前端：计数 0 → 无 `data-evt` 属性、无 tooltip；>0 hover 显示列表（最新在上），表格行 HH:MM:SS、范围卡行含日期；截断时显示 footer；tooltip 不被 `.table-wrap`:798 裁剪（fixed 定位）；poll 重渲染后旧 tooltip 消失。
- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` exit 0 全绿（README:69 同步）。

## Risks
- 锁内 append 为 deque O(1)，与 `RECENT_REQUESTS.append`:403 同量级，无新竞争面。
- 单日事件 >100 时明细仅覆盖最近窗口（deque 全局上限），tooltip footer 明示——设计内限制，非 bug；不为历史天数回填。
- JS 行为无自动化测试（纯 Python 测试栈）：行为验收靠浏览器手测 + 骨架断言兜底；注入面由 textContent-only 规则（:739-740）封死，实现不得用 innerHTML。
- `main()`/测试若漏 clear/备份恢复会残留跨用例污染（deque 为模块级可变全局）。
- 老持久化文件双向兼容论证同 daily_by_model spec：`load_stats_counters`:275 只取 4 键，多余 `events` 键无感知。

## Exclusions
- 点击弹框方案不实现（用户原话备选，主代理已拍板 hover）。
- 剥行/毒行数字不加 tooltip：剥行流带（`POISON_PREVIEWS`:107、`renderPoison`:985-999）已带时间+预览。
- 不新建 HTTP 端点；不加任何前端依赖/框架；不改计数口径、prune、retention、`_record_request`/`_record_empty_retry` 既有计数逻辑。
- 不做事件的服务端按 (day,model) 预聚合——过滤在客户端完成。
