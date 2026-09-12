# 错误/重试数字 hover 时间明细 tooltip 执行计划（2026-09-12）

## Header

- **spec**: `docs/superpowers/specs/2026-09-12-error-retry-time-tooltip-design.md`（本计划唯一事实源，锚点已全部实测验证）
- **worktree/分支**: `.worktrees/error-retry-time-tooltip` → `fix/error-retry-time-tooltip`（HEAD 37a3d12 = spec commit）
- **baseline（2026-09-12 实测）**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 50 tests in 18.956s / OK / exit 0`。本计划新增 4 个测试（卡 1 ×1、卡 2 ×2、卡 3 ×1），完成后总数 **54**，README:69 同步为 `54 个用例`。VERIFY 只对"baseline 绿 → 现在红"负责。
- **运行时**: `/usr/bin/python3` = Python 3.9.6。
- **卡数 / tier 分布**: 4 卡（卡 1 tier A、卡 2 tier A、卡 3 tier B、卡 4 tier B），全部派 **executor**；无 C 档卡——四卡的生产代码与测试代码均已在计划阶段写死（含前端 JS 全文），不命中真机/运行时数据/DI 实测任一留白。卡数 ≤4 → 小计划，单次 dispatch，不分段。
- **执行顺序**: 卡 1 → 卡 2 → 卡 3 → 卡 4 串行（卡 2 依赖卡 1 的 EVENTS 采集与 snapshot 键；卡 3 的 evtFilter 消费卡 1/2 落定的 `snap["events"]` 契约；卡 4 的用例数依赖卡 1-3 的新增测试计数）。
- **干跑验证（2026-09-12，PLAN 阶段）**: 全部 23 个 Edit（1a-1g/2a-2f/3a-3h/4a-4b 计 7+6+8+2 个 old/new 对）已在 `/tmp/plan-dryrun` 副本上按卡序完整执行——每卡 Step 1 红（exit 1）→ Step 2 目标测试绿 → 全量绿（50→51→53→54），README grep 通过，dashboard JS 经 `node --check` 语法校验通过。期望值与实现行为已预先对齐。

## Global Constraints

1. **测试命令一律 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`**（README:69：Homebrew Python 3.14 的 `http.server.HTTPServer` 构造挂死，禁用其他解释器）。跑单个测试可追加测试名（unittest.main CLI 选择，已实测可用）。
2. **Baseline（regression 防护）**：写失败测试前先跑全量测试登记初始状态（2026-09-12 实测 `Ran 50 tests / OK / exit 0`）；VERIFY 只对"baseline 绿 → 现在红"负责，baseline 就红转新 episode。
3. **测试污染防护**：所有换写 `mod.EVENTS` 的测试必须 `orig_events = mod.EVENTS` 备份 + `finally` 恢复（对齐既有 `orig_dbm` 备份模式 test:704-721），否则污染共享模块级 deque。
4. **不加新依赖**：零新包、零前端框架；不新建文件（仅改 `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`、`README.md` 三个既有文件）。
5. **禁 innerHTML**：前端动态数据（model 名来自上游请求体）一律 `textContent` / `el()` 构造节点（py:738-740 既有规则，集成测试 `assertNotIn("innerHTML")` 锁定）。
6. **LF 换行**；**禁沉默 catch**（本计划全部新增代码无 try/except，不涉及）。
7. **不改既有计数口径**：`_record_request` / `_record_empty_retry` 的计数逻辑零改动（只加 `EVENTS.append`）；499 中断不入事件流（同计数口径：error 与 status≥500 才入）；`_prune_daily` / `load_stats_counters` / retention / `BY_MODEL_CAP` 不动。
8. **不动清单（spec Exclusions，越界=打回）**：不实现点击弹框；剥行/毒行数字不加 tooltip（`POISON_PREVIEWS` / `renderPoison` 不动）；不新建 HTTP 端点（`/api/stats` handler 零改动）；不做服务端按 (day,model) 预聚合（过滤在客户端）。
9. **编辑一律内容精确匹配**（old_string 唯一命中，23 个锚点已程序化核验 count==1），本文行号均为落笔前实测参考——前卡编辑会使后卡行号漂移，禁止按行号盲改。
10. **随卡验证**：每卡 Step 1 贴红相（exit 1）、Step 3 跑全量测试贴实际 stdout/exit code；不跨卡攒验证。每卡全绿后单独 commit（Conventional Commits，信息见各卡）。
11. **浏览器手测（用户验收项，非卡内 gate）**：JS 行为无自动化测试（纯 Python 测试栈，spec Risks 明示），骨架断言兜底；全部卡完成后由用户浏览器手测：hover 出列表（最新在上）、表格行 HH:MM:SS / 范围卡行含日期、计数 0 无 tooltip、tooltip 不被 `.table-wrap` 裁剪、poll 重渲染后旧 tooltip 消失。

---

## 卡 1（tier A）: EVENTS 事件流采集 + stats_snapshot 透出

**tier 理由**: spec 已给出全部代码块（常量/两处 append/snapshot 键），逐字转写 + 既有模式对齐，无设计决策 → A（转写卡，派 executor）。

**涉及文件**: `ctyun-stream-fix-proxy.py`（4 处）、`ctyun-stream-fix-proxy.test.py`（3 处）。

**锚点（实测）**: py:106-107（`RECENT_REQUESTS`/`POISON_PREVIEWS` 常量区）、py:403（`_record_request` 内 `RECENT_REQUESTS.append`）、py:437-438（`_record_empty_retry` 尾部）、py:448-449（`stats_snapshot` 锁内 recent/poison 行）；test:12-13（import 区）、test:481-496（`test_stats_snapshot_shape`）、test:671-676（`test_daily_by_model_matrix_fallback` 尾与下一方法边界）。

### Step 1 — 先写失败测试

**Edit 1a** — `ctyun-stream-fix-proxy.test.py` import 区加 `collections`（字母序首位）。

old_string（唯一）:

```python
import http.client
import importlib.util
```

new_string:

```python
import collections
import http.client
import importlib.util
```

**Edit 1b** — `test_stats_snapshot_shape` 整方法替换（补 events 键断言 + 副本断言 + 自足的 error 请求；注意 `/x-err` 必须在 `/x` **之前**记录——`snap["recent"][-1]["filtered"] >= 1` 断言依赖 `/x`（filtered=1）是最后一条 recent）。

old_string（现行全文，唯一）:

```python
    def test_stats_snapshot_shape(self) -> None:
        mod = self.mod
        mod._record_request("POST", "/x", 200, 12.0, 1)
        mod._record_poison_preview(b"data:null")
        snap = mod.stats_snapshot()
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews"):
            self.assertIn(key, snap)
        self.assertIsInstance(snap["uptime_s"], int)
        self.assertIsInstance(snap["recent"], list)
        self.assertIsInstance(snap["poison_previews"], list)
        self.assertGreaterEqual(snap["filtered_total"], 1)
        self.assertGreaterEqual(snap["recent"][-1]["filtered"], 1)
        self.assertIn("data:null", snap["poison_previews"][-1]["preview"])
```

new_string:

```python
    def test_stats_snapshot_shape(self) -> None:
        mod = self.mod
        mod._record_request("POST", "/x-err", 502, 1.0, 0, error=True)
        mod._record_request("POST", "/x", 200, 12.0, 1)
        mod._record_poison_preview(b"data:null")
        snap = mod.stats_snapshot()
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews", "events"):
            self.assertIn(key, snap)
        self.assertIsInstance(snap["uptime_s"], int)
        self.assertIsInstance(snap["recent"], list)
        self.assertIsInstance(snap["poison_previews"], list)
        self.assertIsInstance(snap["events"], list)
        self.assertGreaterEqual(snap["filtered_total"], 1)
        self.assertGreaterEqual(snap["recent"][-1]["filtered"], 1)
        self.assertIn("data:null", snap["poison_previews"][-1]["preview"])
        # events：error 请求必入流；元素含 ts/kind；oldest→newest 与 recent 同序
        self.assertGreaterEqual(len(snap["events"]), 1)
        for e in snap["events"]:
            self.assertIn("ts", e)
            self.assertIn("kind", e)
        # 副本断言（对齐 test_stats_snapshot_daily_is_copy 模式）：
        # 改 snap["events"] 不得影响模块级 EVENTS
        n_before = len(mod.EVENTS)
        snap["events"].append({"ts": 0, "kind": "proxy", "model": None, "status": 0})
        self.assertEqual(len(mod.EVENTS), n_before,
                         "snapshot events must be a copy, not the live deque")
```

**Edit 1c** — 在 `test_daily_by_model_matrix_fallback` 与 `test_daily_persist_roundtrip_legacy_and_corrupt` 之间插入新方法 `test_events_record_and_cap`。

old_string（两方法边界，唯一）:

```python
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
            "empty-retry creation site must keep dm entry shape in sync")

    def test_daily_persist_roundtrip_legacy_and_corrupt(self) -> None:
```

new_string（原边界 + 新方法；orig_events 备份 + finally 恢复防污染）:

```python
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
            "empty-retry creation site must keep dm entry shape in sync")

    def test_events_record_and_cap(self) -> None:
        mod = self.mod
        orig_events = mod.EVENTS
        mod.EVENTS = collections.deque(maxlen=100)
        try:
            # 分类优先级与计数口径一致：error=True → proxy（status≥500 时 error 胜出）；
            # 无 error 的 500/502 → upstream；_record_empty_retry → retry
            mod._record_request("POST", "/e1", 502, 1.0, 0, error=True)
            mod._record_request("POST", "/e2", 500, 1.0, 0)
            mod._record_empty_retry("m-a")
            snap = mod.stats_snapshot()
            self.assertEqual([e["kind"] for e in snap["events"]],
                             ["proxy", "upstream", "retry"])
            self.assertEqual(snap["events"][0]["status"], 502)
            self.assertIsNone(snap["events"][1]["model"])
            self.assertEqual(snap["events"][2]["model"], "m-a")
            self.assertIsNone(snap["events"][2]["status"])
            for e in snap["events"]:
                self.assertIn("ts", e)
                self.assertIsInstance(e["ts"], float)
            # cap：再记 120 条 → 恰留最新 100，最老（proxy/upstream）被丢
            for _ in range(120):
                mod._record_empty_retry()
            snap = mod.stats_snapshot()
            self.assertEqual(len(snap["events"]), 100)
            self.assertEqual(len(mod.EVENTS), 100)
            kinds = [e["kind"] for e in snap["events"]]
            self.assertNotIn("proxy", kinds, "oldest events must be dropped by maxlen")
            self.assertNotIn("upstream", kinds)
        finally:
            mod.EVENTS = orig_events

    def test_daily_persist_roundtrip_legacy_and_corrupt(self) -> None:
```

**Step 1 验证（预期 FAIL，exit 1；干跑实测 `FAILED (failures=1, errors=1)`——shape 在 assertIn("events") 失败、record_and_cap 在 `snap["events"]` KeyError）**:

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_events_record_and_cap ProxyDashboardUnitTest.test_stats_snapshot_shape
```

### Step 2 — 实现（采集 + 透出）

**Edit 1d** — `ctyun-stream-fix-proxy.py` 常量区 `POISON_PREVIEWS` 后新增 `EVENTS`。

old_string（唯一）:

```python
RECENT_REQUESTS = collections.deque(maxlen=100)  # {"ts","method","path","status","dur_ms","filtered","model"}
POISON_PREVIEWS = collections.deque(maxlen=20)   # {"ts","preview"} 最近剥除的 record 预览
```

new_string:

```python
RECENT_REQUESTS = collections.deque(maxlen=100)  # {"ts","method","path","status","dur_ms","filtered","model"}
POISON_PREVIEWS = collections.deque(maxlen=20)   # {"ts","preview"} 最近剥除的 record 预览
EVENTS = collections.deque(maxlen=100)  # {"ts","kind":"proxy"|"upstream"|"retry","model","status"}
# 单一全局事件流（kind 区分）而非按 (day,model,kind) 分环：per-key 环形几十个 deque
# 持久化/清洗成本高，单流 maxlen=100 硬上界等价满足"每 key 有界"，tooltip 按需过滤。
```

**Edit 1e** — `_record_request` 锁内 `RECENT_REQUESTS.append` 前加分类 append（同一 `with STATS_LOCK` 块内）。

old_string（唯一）:

```python
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model})
        _stats_dirty = True
```

new_string:

```python
        if error or status >= 500:
            # 分类优先级与计数一致（error 分支胜过 status>=500）：error=True → proxy，
            # 其余 status>=500 → upstream；499 中断两边都不入流（同计数口径）。
            EVENTS.append({"ts": time.time(), "kind": "proxy" if error else "upstream",
                           "model": model, "status": status})
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model})
        _stats_dirty = True
```

**Edit 1f** — `_record_empty_retry` 锁内 `bucket["retries"] += 1` 后、`_stats_dirty = True` 前加 append（两函数已有 `_stats_dirty = True`，事件随既有 60s flush 落盘，不加新脏标记）。

old_string（唯一）:

```python
        bucket["retries"] += 1
        _stats_dirty = True
```

new_string:

```python
        bucket["retries"] += 1
        EVENTS.append({"ts": time.time(), "kind": "retry", "model": model, "status": None})
        _stats_dirty = True
```

**Edit 1g** — `stats_snapshot` 锁内 `snap["poison_previews"]` 后加 events（逐条浅拷贝，对齐 `snap["daily"]` 模式；`/api/stats` handler 零改动）。

old_string（唯一）:

```python
        snap["recent"] = list(RECENT_REQUESTS)
        snap["poison_previews"] = list(POISON_PREVIEWS)
```

new_string:

```python
        snap["recent"] = list(RECENT_REQUESTS)
        snap["poison_previews"] = list(POISON_PREVIEWS)
        snap["events"] = [dict(e) for e in EVENTS]  # 逐条浅拷贝（对齐 daily 模式），oldest→newest
```

### Step 3 — 验证

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 51 tests ... OK`，exit 0（新增 1 测试；Step 1 两 FAIL 转 green，其余 49 无回归；干跑实测 `Ran 51 tests in 18.826s / OK`）。

**验收**：error=True→proxy（含 status≥500 时优先级同计数）；500/502 无 error→upstream；`_record_empty_retry`→retry 且带 model；全部含 ts；120 条后 snapshot 恰 100 且丢最老；改 snap 不影响 EVENTS（副本断言）。

**commit**: `feat(stats): 错误/重试事件流采集（EVENTS deque + snapshot 透出）`

---

## 卡 2（tier A）: events 随计数落盘 + load_stats_events loader + main() 重启恢复

**tier 理由**: spec 给出 loader 逐字代码，save/main 挂点明确（对齐 daily_by_model 既有模式），无设计决策 → A（转写卡，派 executor）。

**涉及文件**: `ctyun-stream-fix-proxy.py`（4 处）、`ctyun-stream-fix-proxy.test.py`（2 处）。

**锚点（实测）**: py:249-251（`save_stats_counters` 锁块尾 `daily_by_model` 拷贝 + `with _CFG_LOCK`）、py:258-259（json.dump）、py:333-337（`load_daily_by_model_buckets` 尾与 `flush_stats_if_dirty` 边界）、py:1215-1225（`main()` 恢复块）；test:694-699（`test_daily_persist_roundtrip_legacy_and_corrupt` 尾与 `test_daily_by_model_persist_roundtrip` 边界）、test:1330-1335（`test_daily_buckets_resume_from_persist` 尾与 `test_empty_stream_retried_and_second_attempt_relayed` 边界）。

### Step 1 — 先写失败测试

**Edit 2a** — 在 `test_daily_persist_roundtrip_legacy_and_corrupt` 与 `test_daily_by_model_persist_roundtrip` 之间插入新方法 `test_events_persist_roundtrip`。

old_string（两方法边界，唯一）:

```python
        buckets = mod.load_daily_buckets(path)
        self.assertNotIn("2026-01-01", buckets, "non-dict bucket must be skipped")
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(buckets["2026-01-02"]["errors_upstream"], 2)

    def test_daily_by_model_persist_roundtrip(self) -> None:
```

new_string（原边界 + 新方法，覆盖 spec 用例矩阵：roundtrip 全等 / legacy 无键 / 5 类坏 entry 逐项跳过 / 150 条取最新 100）:

```python
        buckets = mod.load_daily_buckets(path)
        self.assertNotIn("2026-01-01", buckets, "non-dict bucket must be skipped")
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(buckets["2026-01-02"]["errors_upstream"], 2)

    def test_events_persist_roundtrip(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit8-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_events = mod.EVENTS
        try:
            # 用例 1：save→load roundtrip 全等（oldest→newest 保序；main() 恢复路径的
            # 等价操作序列）
            mod.EVENTS = collections.deque([
                {"ts": 1757654300.1, "kind": "proxy", "model": None, "status": 502},
                {"ts": 1757654301.2, "kind": "upstream", "model": "m-1", "status": 500},
                {"ts": 1757654302.3, "kind": "retry", "model": "m-2", "status": None}],
                maxlen=100)
            mod.save_stats_counters(path)
            self.assertEqual(mod.load_stats_events(path), list(mod.EVENTS),
                             "save->load roundtrip must restore events verbatim")
        finally:
            mod.EVENTS = orig_events
        # 用例 2：legacy 文件无 events 键 → []
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://x",
                       "stats": {"requests_total": 1}}, fh)
        self.assertEqual(mod.load_stats_events(path), [],
                         "legacy file without events key must yield []")
        # 用例 3：坏 entry 逐项跳过（kind 非法 / ts<0 / model 空 / model>200 /
        # status 越界 / 非 dict entry）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": 1.0, "kind": "bogus", "model": None, "status": None},
                {"ts": -1.0, "kind": "retry", "model": None, "status": None},
                {"ts": 2.0, "kind": "retry", "model": "", "status": None},
                {"ts": 3.0, "kind": "retry", "model": "x" * 201, "status": None},
                {"ts": 4.0, "kind": "proxy", "model": None, "status": 99},
                {"ts": 5.0, "kind": "proxy", "model": None, "status": 600},
                "not-a-dict",
                {"ts": 6.0, "kind": "upstream", "model": "m-ok", "status": 503}]}}, fh)
        self.assertEqual(mod.load_stats_events(path),
                         [{"ts": 6.0, "kind": "upstream", "model": "m-ok", "status": 503}],
                         "malformed entries must be skipped individually")
        # 用例 4：150 条 → 读回最新 100（与 deque maxlen 对齐）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": float(i), "kind": "retry", "model": None, "status": None}
                for i in range(150)]}}, fh)
        loaded = mod.load_stats_events(path)
        self.assertEqual(len(loaded), 100)
        self.assertEqual(loaded[0]["ts"], 50.0, "only the newest 100 must survive")
        self.assertEqual(loaded[-1]["ts"], 149.0)

    def test_daily_by_model_persist_roundtrip(self) -> None:
```

**Edit 2b** — 在 `AdminIntegrationTest.test_daily_buckets_resume_from_persist` 与 `test_empty_stream_retried_and_second_attempt_relayed` 之间插入新方法 `test_events_resume_from_persist`（spec acceptance "main() 恢复后 EVENTS 与落盘一致" 的直接验证，对齐 `test_stats_counters_resume_from_persist` 既有 seed_persist 模式）。

old_string（两方法边界，唯一）:

```python
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["daily"]["2026-01-01"]["requests"], 5)
        self.assertEqual(snap["daily"]["2026-01-01"]["errors_upstream"], 2)

    def test_empty_stream_retried_and_second_attempt_relayed(self) -> None:
```

new_string:

```python
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["daily"]["2026-01-01"]["requests"], 5)
        self.assertEqual(snap["daily"]["2026-01-01"]["errors_upstream"], 2)

    def test_events_resume_from_persist(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        seeded = [{"ts": 1757654300.5, "kind": "proxy", "model": None, "status": 502},
                  {"ts": 1757654301.5, "kind": "retry", "model": "m-a", "status": None}]
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                          "stats": {"requests_total": 1, "events": seeded}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["events"], seeded,
                         "main() must restore EVENTS from persist verbatim")

    def test_empty_stream_retried_and_second_attempt_relayed(self) -> None:
```

**Step 1 验证（预期 FAIL，exit 1；干跑实测 `FAILED (failures=1, errors=1)`——unit 在 `AttributeError: load_stats_events`、integration 在 `snap["events"]==[]` 不等 seeded）**:

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_events_persist_roundtrip AdminIntegrationTest.test_events_resume_from_persist
```

### Step 2 — 实现（落盘 + loader + 恢复）

**Edit 2c** — `save_stats_counters` 锁块内 `daily_by_model` 拷贝后加 events 拷贝。

old_string（唯一）:

```python
        daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                          for d, models in STATS["daily_by_model"].items()}
    with _CFG_LOCK:
```

new_string:

```python
        daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                          for d, models in STATS["daily_by_model"].items()}
        events = [dict(e) for e in EVENTS]  # 逐条浅拷贝：磁盘与内存一致（≤100 条）
    with _CFG_LOCK:
```

**Edit 2d** — `save_stats_counters` 的 json.dump 追加 `events` 键（老文件无键 → loader 回 `[]`，双向零迁移；`load_stats_counters` 只取 4 键，多余键无感知）。

old_string（唯一）:

```python
        json.dump({"upstream_base": base,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model)},
                  fh, ensure_ascii=False)
```

new_string:

```python
        json.dump({"upstream_base": base,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
                  fh, ensure_ascii=False)
```

**Edit 2e** — 在 `load_daily_by_model_buckets` 与 `flush_stats_if_dirty` 之间插入新函数（spec 代码逐字转写）。

old_string（函数边界，唯一）:

```python
            bucket[model] = clean
        out[key] = bucket
    return out


def flush_stats_if_dirty(path: str) -> None:
```

new_string:

```python
            bucket[model] = clean
        out[key] = bucket
    return out


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


def flush_stats_if_dirty(path: str) -> None:
```

**Edit 2f** — `main()` 恢复块：锁外加 load 一行，锁内 `STATS["daily_by_model"]` 赋值后加 clear+extend（先 clear 防 deque 残留叠加）。

old_string（唯一）:

```python
    counters = load_stats_counters(PERSIST_PATH)  # 累计计数跨重启续算
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    daily_by_model = load_daily_by_model_buckets(PERSIST_PATH)  # 按天×模型矩阵跨重启续算
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["daily"] = daily
        STATS["daily_by_model"] = daily_by_model
        _stats_dirty = False
```

new_string:

```python
    counters = load_stats_counters(PERSIST_PATH)  # 累计计数跨重启续算
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    daily_by_model = load_daily_by_model_buckets(PERSIST_PATH)  # 按天×模型矩阵跨重启续算
    events = load_stats_events(PERSIST_PATH)      # 错误/重试事件流跨重启续算
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["daily"] = daily
        STATS["daily_by_model"] = daily_by_model
        EVENTS.clear()
        EVENTS.extend(events)  # 先 clear 后 extend：防 deque 残留叠加
        _stats_dirty = False
```

### Step 3 — 验证

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 53 tests ... OK`，exit 0（新增 2 测试；干跑实测 `Ran 53 tests in 19.474s / OK`）。

**验收**：save→load roundtrip 全等；legacy 无键→`[]`；5 类坏 entry 逐项跳过；150 条取最新 100；`main()` 恢复后 `/api/stats` 的 events 与落盘一致（integration 直证）。

**commit**: `feat(stats): events 随计数落盘与重启恢复（save/loader/main）`

---

## 卡 3（tier B）: 前端 hover 时间明细 tooltip（CSS/HTML/JS + 三处挂点）

**tier 理由**: 8 个 Edit 的机械转写（CSS/HTML/JS 全文已写死，零设计决策）；JS 行为验收靠骨架断言 + 用户浏览器手测（spec Risks 明示，手测为用户验收项非卡内 gate），测试已写死不升 C → B（机械卡，派 executor）。

**涉及文件**: `ctyun-stream-fix-proxy.py`（7 处，均在 `_DASHBOARD_SRC` 内）、`ctyun-stream-fix-proxy.test.py`（1 处）。

**锚点（实测）**: py:798（CSS `.table-wrap`）、py:889-890（`</div></footer>` + `<script>`）、py:931-932（`function renderStats` 头）、py:938（`$("st-errors")` 赋值）、py:1018-1024（`renderDaily` 行构造）、py:1051-1057（`renderDailyByModel` 行构造）、py:1143-1144（`poll` 内 `renderSpark`/`setConn`）；test:944-949（`test_stats_snapshot_daily_is_copy` 尾与 `test_safe_log_stderr_normal_and_broken` 边界）。

### Step 1 — 先写失败测试

**Edit 3a** — 在 `test_stats_snapshot_daily_is_copy` 与 `test_safe_log_stderr_normal_and_broken` 之间插入新方法 `test_dashboard_tooltip_skeleton`。

old_string（两方法边界，唯一）:

```python
        snap["daily_by_model"][mod.today_key()]["snap-m"]["requests"] = 999
        self.assertEqual(
            mod.STATS["daily_by_model"][mod.today_key()]["snap-m"]["requests"], 1,
            "inner model entries must be copies, not internal refs")

    def test_safe_log_stderr_normal_and_broken(self) -> None:
```

new_string:

```python
        snap["daily_by_model"][mod.today_key()]["snap-m"]["requests"] = 999
        self.assertEqual(
            mod.STATS["daily_by_model"][mod.today_key()]["snap-m"]["requests"], 1,
            "inner model entries must be copies, not internal refs")

    def test_dashboard_tooltip_skeleton(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertIn('id="evt-tip"', html)
        self.assertIn("position:fixed", html)
        self.assertIn("markEvents", html)
        self.assertIn("showEvtTip", html)
        self.assertIn("hideEvtTip", html)
        # model 名来自上游请求体：动态数据禁走 innerHTML，必须 textContent
        self.assertNotIn("innerHTML", html)

    def test_safe_log_stderr_normal_and_broken(self) -> None:
```

**Step 1 验证（预期 FAIL，exit 1；干跑实测 `FAILED (failures=1)`——`'id="evt-tip"' not found`）**:

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_dashboard_tooltip_skeleton
```

### Step 2 — 实现（前端）

**Edit 3b** — CSS：`.table-wrap` 后追加 `.evt-tip`（`position:fixed` 逃出 `.table-wrap{overflow-x:auto}` 的裁剪上下文；`pointer-events:none` 防 tooltip 自身截获鼠标）。

old_string（唯一）:

```css
.table-wrap { overflow-x:auto; }
```

new_string:

```css
.table-wrap { overflow-x:auto; }
.evt-tip { position:fixed; z-index:9; display:none; max-width:340px; max-height:260px;
  overflow-y:auto; background:var(--panel); border:1px solid var(--amber); border-radius:6px;
  padding:6px 10px; font-size:12px; pointer-events:none;
  box-shadow:0 4px 16px rgba(0,0,0,.5); }
```

**Edit 3c** — HTML：body 尾、footer 后 `<script>` 前加 tooltip 容器。

old_string（唯一）:

```html
</div></footer>
<script>
```

new_string:

```html
</div></footer>
<div class="evt-tip" id="evt-tip"></div>
<script>
```

**Edit 3d** — JS：`setConn` 之后、`renderStats` 之前插入 tooltip 工具区（`evtFilter`/`markEvents`/`showEvtTip`/`hideEvtTip` + document 级委托）。

old_string（唯一）:

```javascript
function renderStats(snap) {
  var rs = snap.range_stats && snap.range_stats[selectedRange];
```

new_string:

```javascript
var EVT_KIND_LABELS = { proxy: "代理错误", upstream: "上游5xx", retry: "空流重试" };
function evtFilter(kind, day, model) {
  // 按 dataset 重新过滤 lastSnap.events：kind="errors" = proxy+upstream（顶部错误数
  // 口径 = rs.errors_proxy + rs.errors_upstream）；day/model 给定时精确匹配。
  var evts = (lastSnap && lastSnap.events) || [];
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var out = [];
  for (var i = 0; i < evts.length; i++) {
    var e = evts[i];
    if (kind === "errors") {
      if (e.kind !== "proxy" && e.kind !== "upstream") continue;
    } else if (e.kind !== kind) {
      continue;
    }
    if (bounds && !(bounds[0] <= fmtDate(e.ts) && fmtDate(e.ts) <= bounds[1])) continue;
    if (day && fmtDate(e.ts) !== day) continue;
    if (model && e.model !== model) continue;
    out.push(e);
  }
  return out;
}
function markEvents(n, kind, day, model) {
  // 匹配数 >0 才设 data-evt：计数 0 → 无属性 → 无 tooltip（数字与明细同 key）
  delete n.dataset.evt;
  delete n.dataset.day;
  delete n.dataset.model;
  if (evtFilter(kind, day, model).length > 0) {
    n.dataset.evt = kind;
    if (day) n.dataset.day = day;
    if (model) n.dataset.model = model;
  }
}
function showEvtTip(target, x, y) {
  var tip = $("evt-tip");
  tip.textContent = "";
  var evts = evtFilter(target.dataset.evt, target.dataset.day, target.dataset.model);
  var rangeRow = !target.dataset.day;  // 顶部范围卡行（无 day）：时间含日期
  if (evts.length === 0) {
    tip.appendChild(el("div", "", "无记录"));
  } else {
    for (var i = evts.length - 1; i >= 0; i--) {  // 最新在上（renderRecent 模式）
      var e = evts[i];
      tip.appendChild(el("div", "",
        (EVT_KIND_LABELS[e.kind] || e.kind) + " " +
        (rangeRow ? fmtDate(e.ts) + " " + fmtTime(e.ts) : fmtTime(e.ts)) +
        (e.model ? " · " + e.model : "") +
        (e.status ? " · " + e.status : "")));
    }
    if (evts.length >= 100) {
      tip.appendChild(el("div", "", "共 " + evts.length + " 次，仅保留最近 100 条事件记录"));
    }
  }
  tip.style.display = "block";
  tip.style.left = "0px";
  tip.style.top = "0px";
  var tw = tip.offsetWidth, th = tip.offsetHeight;
  var left = x + 14, top = y + 14;
  if (left + tw > window.innerWidth - 8) left = x - tw - 14;  // 视口边缘翻转
  if (top + th > window.innerHeight - 8) top = y - th - 14;
  tip.style.left = Math.max(8, left) + "px";
  tip.style.top = Math.max(8, top) + "px";
}
function hideEvtTip() {
  var tip = $("evt-tip");
  tip.style.display = "none";
  tip.textContent = "";
}
document.addEventListener("mouseover", function (e) {
  var t = e.target && e.target.closest ? e.target.closest("[data-evt]") : null;
  if (t) showEvtTip(t, e.clientX, e.clientY);
});
document.addEventListener("mousemove", function (e) {
  var t = e.target && e.target.closest ? e.target.closest("[data-evt]") : null;
  if (t) showEvtTip(t, e.clientX, e.clientY);
});
document.addEventListener("mouseout", function (e) {
  var t = e.target && e.target.closest ? e.target.closest("[data-evt]") : null;
  if (t) hideEvtTip();
});
function renderStats(snap) {
  var rs = snap.range_stats && snap.range_stats[selectedRange];
```

**Edit 3e** — `renderStats`：`$("st-errors")` 赋值后挂 markEvents（顶部错误数 = 范围卡行，kind="errors"，无 day/model）。

old_string（唯一）:

```javascript
    $("st-errors").textContent = rs.errors_proxy + rs.errors_upstream;
  }
```

new_string:

```javascript
    $("st-errors").textContent = rs.errors_proxy + rs.errors_upstream;
    markEvents($("st-errors"), "errors", null, null);
  }
```

**Edit 3f** — `renderDaily`：代理错误/上游5xx/重试三个 td 改为先构造、挂 markEvents 再 append（day=行日期 key）。

old_string（唯一）:

```javascript
    tr.appendChild(el("td", "num", keys[i]));
    tr.appendChild(el("td", "num", String(b.requests)));
    tr.appendChild(el("td", "num", String(b.filtered)));
    tr.appendChild(el("td", "num", String(b.errors_proxy)));
    tr.appendChild(el("td", "num", String(b.errors_upstream)));
    tr.appendChild(el("td", "num", String(b.retries || 0)));
    body.appendChild(tr);
```

new_string:

```javascript
    tr.appendChild(el("td", "num", keys[i]));
    tr.appendChild(el("td", "num", String(b.requests)));
    tr.appendChild(el("td", "num", String(b.filtered)));
    var tdEp = el("td", "num", String(b.errors_proxy));
    var tdEu = el("td", "num", String(b.errors_upstream));
    var tdRt = el("td", "num", String(b.retries || 0));
    markEvents(tdEp, "proxy", keys[i], null);
    markEvents(tdEu, "upstream", keys[i], null);
    markEvents(tdRt, "retry", keys[i], null);
    tr.appendChild(tdEp);
    tr.appendChild(tdEu);
    tr.appendChild(tdRt);
    body.appendChild(tr);
```

**Edit 3g** — `renderDailyByModel`：代理错误/上游5xx/重试三个 td 改为先构造、挂 markEvents 再 append（day=行日期，model=行模型名，双过滤）。

old_string（唯一）:

```javascript
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(ent.requests || 0)));
      tr.appendChild(el("td", "num", String(ent.filtered || 0)));
      tr.appendChild(el("td", "num", String(ent.errors_proxy || 0)));
      tr.appendChild(el("td", "num", String(ent.errors_upstream || 0)));
      tr.appendChild(el("td", "num", String(ent.retries || 0)));
      body.appendChild(tr);
```

new_string:

```javascript
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(ent.requests || 0)));
      tr.appendChild(el("td", "num", String(ent.filtered || 0)));
      var tdEp = el("td", "num", String(ent.errors_proxy || 0));
      var tdEu = el("td", "num", String(ent.errors_upstream || 0));
      var tdRt = el("td", "num", String(ent.retries || 0));
      markEvents(tdEp, "proxy", days[i], names[j]);
      markEvents(tdEu, "upstream", days[i], names[j]);
      markEvents(tdRt, "retry", days[i], names[j]);
      tr.appendChild(tdEp);
      tr.appendChild(tdEu);
      tr.appendChild(tdRt);
      body.appendChild(tr);
```

**Edit 3h** — `poll`：每轮重渲染后调 `hideEvtTip`（DOM 已换，防旧 tooltip 悬空）。

old_string（唯一）:

```javascript
      renderSpark(snap.recent || []);
      setConn(true);
    })
```

new_string:

```javascript
      renderSpark(snap.recent || []);
      setConn(true);
      hideEvtTip();  // 重渲染后旧 tooltip 指向已换的 DOM，防悬空
    })
```

### Step 3 — 验证

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 54 tests ... OK`，exit 0（新增 1 测试；含 `test_dashboard_html_full_page` 集成断言——`assertNotIn("innerHTML")` / `assertNotIn("按模型")` 等既有锁定全部不受影响，干跑实测 `Ran 54 tests in 19.651s / OK`）。

可选语法自查（非 gate，node 不存在则跳过；干跑实测通过）:

```sh
/usr/bin/python3 -c "
import re
src = open('ctyun-stream-fix-proxy.py', encoding='utf-8').read()
html = re.search(r'_DASHBOARD_SRC = \"\"\"(.*?)\"\"\"', src, re.S).group(1)
open('/tmp/dashboard-check.js', 'w', encoding='utf-8').write(
    re.search(r'<script>(.*?)</script>', html, re.S).group(1))
" && node --check /tmp/dashboard-check.js && echo "JS SYNTAX OK"
```

**验收**：`id="evt-tip"` 与 `position:fixed` 均在 DASHBOARD_HTML；markEvents/showEvtTip/hideEvtTip 骨架存在；innerHTML 零出现；全量 54 绿。行为验收（hover 列表/裁剪/poll 消失）见 Global Constraints 11 用户手测清单。

**commit**: `feat(dashboard): 错误/重试数字 hover 时间明细 tooltip`

---

## 卡 4（tier B）: README 同步（用例数 + 口径表追加行）

**tier 理由**: 纯文案两处替换 + grep 验证，零设计决策 → B（机械卡，派 executor）。TDD 豁免（文档改动）。

**涉及文件**: `README.md`（2 处）。

**锚点（实测）**: README:69（`50 个用例`）、README:78（口径表末行「最近请求」「剥行流带」）。

### Edit 4a — 用例数 50 → 54（baseline 50 + 卡 1 ×1 + 卡 2 ×2 + 卡 3 ×1）

old_string（唯一）:

```markdown
50 个用例。**必须用 `/usr/bin/python3`**：Homebrew 的 Python 3.14 `http.server.HTTPServer` 构造会挂死（进程存活但不 LISTEN、零报错）。
```

new_string:

```markdown
54 个用例。**必须用 `/usr/bin/python3`**：Homebrew 的 Python 3.14 `http.server.HTTPServer` 构造会挂死（进程存活但不 LISTEN、零报错）。
```

### Edit 4b — 口径表追加事件流明细行（spec 给定文案逐字）

old_string（唯一）:

```markdown
| 「最近请求」「剥行流带」 | 最近 100 / 20 条内存窗口 | 否 |
```

new_string:

```markdown
| 「最近请求」「剥行流带」 | 最近 100 / 20 条内存窗口 | 否 |
| 错误/重试数字 hover 明细 | 事件流最近 100 条（代理错误+上游5xx+空流重试） | 是（stats.events 随 60s 周期落盘） |
```

### 验证

```sh
grep -n "个用例" README.md
```

预期：仅一行 `69:54 个用例。...`（干跑实测一致）。

```sh
grep -n "hover 明细" README.md
```

预期：仅一行 `79:| 错误/重试数字 hover 明细 | 事件流最近 100 条（代理错误+上游5xx+空流重试） | 是（stats.events 随 60s 周期落盘） |`（干跑实测一致）。

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 54 tests ... OK`，exit 0（文案改动零回归）。

**验收**：README:69 = 54 个用例；口径表含 hover 明细行；全量测试绿。

**commit**: `docs: 同步事件流 tooltip 口径与用例数`

---

## Self-Review（PLAN-B 完成自查，2026-09-12）

1. **spec coverage**：spec Files 三节全覆盖——py 节：EVENTS 常量（卡 1 Edit 1d）、`_record_request`/`_record_empty_retry` 采集挂点（卡 1 Edit 1e/1f）、`stats_snapshot` events 键（卡 1 Edit 1g）、save 落盘（卡 2 Edit 2c/2d）、`load_stats_events` loader（卡 2 Edit 2e）、main() 恢复（卡 2 Edit 2f）、前端 CSS/HTML/JS/三挂点/poll（卡 3 Edit 3b-3h）→ 卡 1/2/3；test 节：shape 补断言（卡 1 Edit 1b）、record_and_cap（卡 1 Edit 1c）、persist_roundtrip（卡 2 Edit 2a）、tooltip_skeleton（卡 3 Edit 3a）→ 各卡 Step 1，另加 `test_events_resume_from_persist`（卡 2 Edit 2b）直证 acceptance 第 4 条 "main() 恢复后 EVENTS 与落盘一致"（spec acceptance 有此条而 test 节未列测试，按 acceptance 补齐，模式对齐既有 `test_stats_counters_resume_from_persist`，不违反 Exclusions）；README 节：:69 用例数 + :78 口径表行（卡 4）。Acceptance 7 条逐条映射：采集分类/cap（卡 1）、events 键+副本（卡 1）、roundtrip/legacy/坏 entry/150→100（卡 2）、main 恢复（卡 2 integration）、前端骨架+行为（卡 3 骨架断言 + Global Constraints 11 用户手测）、全绿+README 同步（各卡 Step 3 + 卡 4）。Risks 5 条均有对策：锁内 O(1)（卡 1 同 RECENT_REQUESTS 模式）、>100 仅覆盖最近窗口（卡 3 footer 明示）、JS 无自动化测试（骨架断言 + 手测清单）、deque 污染（Global Constraints 3 orig_events 备份恢复）、老文件兼容（卡 2 Edit 2d 说明）。Exclusions 全部进 Global Constraints 8 不动清单。
2. **placeholder scan**：4 卡共 23 个 Edit（卡 1 七个 1a-1g、卡 2 六个 2a-2f、卡 3 八个 3a-3h、卡 4 两个 4a-4b）全部为逐字完整 old/new 代码块，无 TODO/TBD/"按 spec 实现"类指令式占位；JS 块为完整可落盘函数全文（含事件委托绑定），非签名 stub。grep 复查 `TODO|TBD|FIXME|待补|按 spec 实现|参考 spec|同上|此处略|省略` 全文仅 1 处命中=本行自述引用（初稿 Edit 3g 描述语含"同上"二字，已改写消除——代码块本身始终完整）。
3. **type consistency**：`EVENTS` 元素形状 `{"ts","kind","model","status"}` 在采集（1e/1f）、snapshot（1g）、save（2c/2d）、loader（2e 输出同 4 键 dict）、main 恢复（2f）、前端消费（3d `e.ts/e.kind/e.model/e.status`）六处一致；`load_stats_events(path: str) -> list` 签名与 spec 逐字一致；kind 枚举 `("proxy","upstream","retry")` 与前端 `EVT_KIND_LABELS` 三键 + `evtFilter` 的 `"errors"` 聚合口径一致（errors = proxy+upstream，对齐 `rs.errors_proxy + rs.errors_upstream`）；`markEvents(n, kind, day, model)` 与三处挂点调用签名一致；测试引用 `mod.EVENTS`/`mod.load_stats_events`/`snap["events"]` 与实现标识符一致（全库 grep 确认新标识符零既有冲突）。
4. **可落盘性**：23 个 old_string 已用脚本对 worktree 现行文件逐个 `content.count(old)` 程序化核验，23/23 unique-OK；每块 new_string 含完整上下文（import/缩进/相邻行），贴到目标位置即可用；验证命令均为可执行原样命令（unittest CLI 单测选择已实测可用）；各卡 Step 1 预期失败模式明确且干跑实测吻合（卡 1 `FAILED (failures=1, errors=1)`、卡 2 `FAILED (failures=1, errors=1)`、卡 3 `FAILED (failures=1)`）。
5. **锚点实测 + 期望值干跑**：spec 全部 file:line 锚点于 2026-09-12 在 worktree `fix/error-retry-time-tooltip`（HEAD 37a3d12）实测核对——py:106/107/373/403/417/437/438/441/449/444/679-680/258-259/333-337/1211/1217/1224/798/889-890/931/938/1018-1024/1051-1057/1143-1144 与 test:481/676/674/929/694-699/1330-1335、README:69/78 全部命中；两处 spec 行号微漂已按实际锚点修正：① spec "save 锁内 :243-248" 实际锁块为 :243-250（daily_by_model 拷贝占两行，卡 2 锚点改用精确文本）；② spec ":474-479 STATS 备份/finally 恢复模式" 实际该区间是 `test_daily_by_model_cap`，备份模式真身在 :704-721（卡 1 测试代码按 orig_dbm 模式实现，语义一致）。baseline 实测 `Ran 50 tests / OK / exit 0`；`/usr/bin/python3` = 3.9.6。**期望值干跑（两轮）**：第一轮在 `/tmp/plan-dryrun` 副本上手写脚本按卡序执行全部 23 个 Edit——卡 1 红（2 测试 FAIL）→ 绿 → 全量 `Ran 51 tests / OK`；卡 2 红（2 测试 FAIL）→ 绿 → `Ran 53 tests / OK`；卡 3 红（1 测试 FAIL）→ 绿 → `Ran 54 tests / OK`（含 `test_dashboard_html_full_page` 既有断言无回归）；卡 4 grep 两处命中且仅一处；dashboard JS 提取后 `node --check` 语法通过。第二轮为**文档回放**——脚本直接解析本 PLAN.md 的 23 个 Edit 代码块、对 worktree 干净副本按文档序逐个 `count(old)==1` 校验并替换，23/23 全部应用，全量 `Ran 54 tests in 19.409s / OK / exit 0`，README grep（:69 用例数、:79 口径行）命中，且三个产物文件与第一轮终态逐字节一致（filecmp shallow=False）——计划文档自含、可逐字转写即绿。干跑期间发现并修正一处计划缺陷：shape 测试的 error 请求若插在 `/x` 之后会破坏既有 `snap["recent"][-1]["filtered"] >= 1` 断言（recent 末条变为 filtered=0 的 error 请求），已调整为 `/x-err` 先记、`/x` 后记。
