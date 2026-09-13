# PLAN — EOF-without-done 检测（ctyun-stream-fix-proxy）

## Header

- **目标**：`_relay_sse` streaming 阶段 EOF 且全程未见 `[DONE]` 时，把该请求 result 标记为 `eof-without-done` 并经 `_record_eof_without_done()` 计数落盘（全局 + 按天 + 按模型），使上游截断故障可见；字节透传行为零改动。
- **spec**：`docs/superpowers/specs/2026-09-13-eof-without-done-detection-design.md`
- **分支**：`fix/eof-without-done-detection`（worktree `.worktrees/eof-without-done-detection`）
- **baseline**：54 tests OK（`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`，exit 0），初始红清单为空。

## Global Constraints

- 只改 2 文件：`ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`。**不碰** `_DASHBOARD_SRC`、`load_stats_events` 白名单、EVENTS 事件流、滤毒/priming/flush/`_EmptyStream` 重试/超时/499 路径（spec Exclusions 全部维持）。
- 测试必须用 `/usr/bin/python3` 跑（README:69）；最终预期 `Ran 57 tests ... OK`（54 既有 + 3 新增），0 failure 0 error。
- LF 换行；无新依赖（纯 stdlib）；错误处理显式；无死代码。
- TDD 顺序强制：Step 1 测试文件全部改动 → Step 2 跑红（登记红清单，须与卡内预期红集一致）→ Step 3 主文件全部改动 → Step 4 跑绿 → Step 5 commit。红集出现预期外的失败 = 锚点漂移，STOP 上报。
- priming 阶段 EOF（空流）**不**标 eof-without-done（已由 retries 计数承载，spec Exclusions：避免同一失败双计数）。

---

## Task 1 — eof-without-done 检测：测试先行 + 主文件实现（TDD 单循环）

**Tier: A**（executor 转写卡。spec 已到代码级，本卡全部代码写死；无真机/运行时/DI 留白，不标 C。）

**文件**：`ctyun-stream-fix-proxy.test.py`（Step 1）、`ctyun-stream-fix-proxy.py`（Step 3）

### Step 1 — 测试文件改动（ctyun-stream-fix-proxy.test.py）

以下 E1–E14 逐条用 Edit 工具执行（old → new 逐字替换）。E1 与 E2 各有 2 处相同文本，须用 `replace_all`。

**E1**（2 处相同，`:453-455` 与 `:458-460`，`test_stats_persist_roundtrip_and_defaults`；replace_all: true）：

```python
# OLD
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0})
# NEW
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0, "eof_without_done_total": 0})
```

**E2**（`:488-491`，`test_stats_snapshot_shape` assertIn 键清单，加性）：

```python
# OLD
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews", "events"):
# NEW
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "eof_without_done_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews", "events"):
```

**E3**（`:538-539`，`test_aggregate_daily_range_sums_and_days` 窗口内精确 dict）：

```python
# OLD
        self.assertEqual(out, {"requests": 8, "filtered": 1, "errors_proxy": 0,
                               "errors_upstream": 1, "retries": 0, "days": 2})
# NEW
        self.assertEqual(out, {"requests": 8, "filtered": 1, "errors_proxy": 0,
                               "errors_upstream": 1, "retries": 0,
                               "eof_without_done": 0, "days": 2})
```

**E4**（`:541-543`，同测试空窗口精确 dict）：

```python
# OLD
        self.assertEqual(f(daily, "2025-01-01", "2025-01-31"),
                         {"requests": 0, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0, "days": 0})
# NEW
        self.assertEqual(f(daily, "2025-01-01", "2025-01-31"),
                         {"requests": 0, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "days": 0})
```

**E5**（`:560-562`，`test_range_stats_all_four_keys` 键集合）：

```python
# OLD
            self.assertEqual(set(plan["stats"][key]),
                             {"requests", "filtered", "errors_proxy",
                              "errors_upstream", "retries", "days"})
# NEW
            self.assertEqual(set(plan["stats"][key]),
                             {"requests", "filtered", "errors_proxy",
                              "errors_upstream", "retries", "eof_without_done", "days"})
```

**E6**（`:652-656`，`test_daily_by_model_matrix_fallback` site-1 形状断言 + 注释同步。锚点实测补充：spec 未列此站，`_DAILY_FIELDS`/创建字面量加第 6 键后此集合断言必红）：

```python
# OLD
        # v3：dm entry 恒 5 字段 + 错误归因与 daily 总桶同口径（error→proxy，5xx→upstream）
        self.assertEqual(
            set(dm_today["m-a"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
            "dm entry shape must stay in sync across both creation sites")
# NEW
        # v3：dm entry 恒 6 字段 + 错误归因与 daily 总桶同口径（error→proxy，5xx→upstream）
        self.assertEqual(
            set(dm_today["m-a"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries",
             "eof_without_done"},
            "dm entry shape must stay in sync across both creation sites")
```

**E7**（`:677-688`，同测试 site-2 形状断言 + 注释同步。锚点实测补充：spec 未列此站）：

```python
# OLD
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 5 字段——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
            "empty-retry creation site must keep dm entry shape in sync")
# NEW
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 6 字段——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries",
             "eof_without_done"},
            "empty-retry creation site must keep dm entry shape in sync")
```

**E8**（`:803-811`，`test_daily_by_model_persist_roundtrip` 用例 1 的 matrix 字面量。锚点实测补充：spec 未列此站，save 原样拷贝 + load 按 6 字段清洗后 roundtrip 全等断言必红）：

```python
# OLD
            matrix = {
                "2026-01-02": {
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0}}}
# NEW
            matrix = {
                "2026-01-02": {
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0, "eof_without_done": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4, "eof_without_done": 0}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0, "eof_without_done": 0}}}
```

**E9**（`:836-840`，同测试用例 3 期望 dict。锚点实测补充）：

```python
# OLD
        self.assertEqual(mod.load_daily_by_model_buckets(path),
                         {"2026-01-02": {"m2": {"requests": 5, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 2, "retries": 0}}},
                         "non-dict bucket/entry must be skipped; bad fields coerced to 0")
# NEW
        self.assertEqual(mod.load_daily_by_model_buckets(path),
                         {"2026-01-02": {"m2": {"requests": 5, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 2, "retries": 0,
                                                "eof_without_done": 0}}},
                         "non-dict bucket/entry must be skipped; bad fields coerced to 0")
```

**E10**（`:852-855`，同测试用例 4 期望 dict。锚点实测补充）：

```python
# OLD
        self.assertEqual(dbm["2026-01-03"]["m1"],
                         {"requests": 1, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0},
                         "missing fields must be filled with 0")
# NEW
        self.assertEqual(dbm["2026-01-03"]["m1"],
                         {"requests": 1, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0, "eof_without_done": 0},
                         "missing fields must be filled with 0")
```

**E11**（`:1149-1152`，`test_dashboard_and_stats_served` assertIn 键清单，加性）：

```python
# OLD
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews"):
# NEW
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "eof_without_done_total", "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews"):
```

**E12**（`:1279-1281`，`test_recent_entries_carry_model` 注释 + dm 键清单，加性）：

```python
# OLD
        # v3：dm entry 5 键经 /api/stats 透出（纯新增键，旧客户端只读 3 键不受影响）
        dm_entry = snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]
        for key in ("requests", "filtered", "errors_proxy", "errors_upstream", "retries"):
# NEW
        # v3：dm entry 6 键经 /api/stats 透出（纯新增键，旧客户端只读 3 键不受影响）
        dm_entry = snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]
        for key in ("requests", "filtered", "errors_proxy", "errors_upstream",
                    "retries", "eof_without_done"):
```

**E13**（`:1296-1299`，`test_stats_counters_resume_from_persist` 加 legacy 无键加载为 0 断言，对应 spec Acceptance「旧文件加载为 0 不崩」）：

```python
# OLD
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["requests_total"], 7)
        self.assertEqual(snap["filtered_total"], 3)
        self.assertEqual(snap["errors_total"], 1)
# NEW
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["requests_total"], 7)
        self.assertEqual(snap["filtered_total"], 3)
        self.assertEqual(snap["errors_total"], 1)
        self.assertEqual(snap["eof_without_done_total"], 0,
                         "legacy persist without eof key must load as 0")
```

**E14**（文件尾 `:1561-1564`，`AdminIntegrationTest` 末尾追加 3 个新用例。锚点为 `test_empty_retry_counter_persists_and_resumes` 最后一行 + `if __name__` 块）：

```python
# OLD
        self.assertEqual(json.loads(body.decode("utf-8"))["empty_retries_total"], 6)


if __name__ == "__main__":
# NEW
        self.assertEqual(json.loads(body.decode("utf-8"))["empty_retries_total"], 6)

    def test_eof_without_done_marked_counted_and_persisted(self) -> None:
        # spec 用例 ①：有 content 无 [DONE] 即 EOF（上游截断签名，R31 现场复刻）
        upstream_port, calls = make_scripted_upstream(body_override=SSE_A + SSE_B)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "content-bearing stream must not retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B,
                         "truncated stream must still relay byte-exact, got %r" % data)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["eof_without_done_total"], 1,
                                "eof-without-done must count into STATS total")
        self.assertGreaterEqual(snap["daily"][today]["eof_without_done"], 1,
                                "eof-without-done must count into daily bucket")
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["eof_without_done"], 1,
            "eof-without-done must count into daily_by_model")
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=eof-without-done", stderr,
                      "REQ line must carry result=eof-without-done, stderr:\n" + stderr)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved["eof_without_done_total"], 1)
        self.assertGreaterEqual(saved["daily"][today]["eof_without_done"], 1)
        self.assertGreaterEqual(
            saved["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["eof_without_done"], 1)
        # 重启续算：seed 含新计数键 → /api/stats 透出（仿 empty_retries resume 段）
        self.proc = start_proxy(
            upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                          "stats": {"eof_without_done_total": 5}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 5,
                         "seeded eof counter must resume from persist")

    def test_done_streams_stay_ok_no_eof_counter(self) -> None:
        # spec 用例 ② 对照组 A：默认正常流（SSE_A+SSE_B+[DONE]，setUp 假上游）
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 0,
                         "normal done-terminated stream must not trip eof counter")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "normal stream must stay result=ok, stderr:\n" + stderr)
        # spec 用例 ② 对照组 B：reasoning 前缀 + 合法 [DONE]（priming 前缀场景）
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_DONE)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1)
        self.assertEqual(data, SSE_REASONING + SSE_DONE)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 0,
                         "reasoning+[DONE] stream must not trip eof counter")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "reasoning+[DONE] stream must stay result=ok, stderr:\n" + stderr)
        self.assertNotIn("eof-without-done", stderr)

    def test_double_empty_stream_not_marked_eof_without_done(self) -> None:
        # spec 用例 ③：双空流 fallback（priming 路径 EOF 不加标记，避免与 retries 双计数）
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.assertEqual(data, SSE_REASONING)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 0,
                         "priming-stage EOF must not trip eof counter")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertNotIn("eof-without-done", stderr,
                         "priming-stage EOF (empty stream fallback) must not be marked "
                         "eof-without-done, stderr:\n" + stderr)


if __name__ == "__main__":
```

### Step 2 — 跑红（TDD 证据）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期红集（恰好这 12 个方法失败/报错，其余 45 个绿；出现预期外红 = 锚点漂移，STOP）：**

1. `test_stats_persist_roundtrip_and_defaults`（E1 dict 不匹配）
2. `test_stats_snapshot_shape`（E2 assertIn 缺键）
3. `test_aggregate_daily_range_sums_and_days`（E3/E4）
4. `test_range_stats_all_four_keys`（E5）
5. `test_daily_by_model_matrix_fallback`（E6/E7）
6. `test_daily_by_model_persist_roundtrip`（E8/E9/E10）
7. `test_dashboard_and_stats_served`（E11）
8. `test_recent_entries_carry_model`（E12）
9. `test_stats_counters_resume_from_persist`（E13 KeyError）
10. `test_eof_without_done_marked_counted_and_persisted`（新，KeyError：`result=ok` 且计数缺位）
11. `test_done_streams_stay_ok_no_eof_counter`（新，KeyError）
12. `test_double_empty_stream_not_marked_eof_without_done`（新，KeyError）

登记实际 stdout（Ran 57 tests, FAILED/ERRORS 计数与失败清单）作为 TDD 红相证据。

### Step 3 — 主文件改动（ctyun-stream-fix-proxy.py）

以下 M1–M16 逐条用 Edit 执行（按文件行序排列；M5/M6 各 2 处相同文本用 `replace_all`）。

**M1**（`:101-103` STATS 初始化，spec 项 1）：

```python
# OLD
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {}}
# NEW
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0, "eof_without_done_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {}}
```

**M2**（`:194` `aggregate_daily_range` docstring 键数同步，注释准确性）：

```python
# OLD
    返回 6 键 dict：_DAILY_FIELDS 五字段 + days=命中桶数。
# NEW
    返回 7 键 dict：_DAILY_FIELDS 六字段 + days=命中桶数。
```

**M3**（`:247-248` `save_stats_counters` counter 元组，spec 项 5）：

```python
# OLD
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total")}
# NEW
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total", "eof_without_done_total")}
```

**M4**（`:284` `load_stats_counters` key 元组，spec 项 5；旧文件缺键 → 0）：

```python
# OLD
    for key in ("requests_total", "filtered_total", "errors_total", "empty_retries_total"):
# NEW
    for key in ("requests_total", "filtered_total", "errors_total", "empty_retries_total",
                "eof_without_done_total"):
```

**M5**（`:290` `_DAILY_FIELDS` 追加第 6 字段，spec 项 2；loader 按 `_DAILY_FIELDS` 迭代清洗自动补键零迁移）：

```python
# OLD
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream", "retries")
# NEW
_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream",
                 "retries", "eof_without_done")
```

**M6**（2 处相同，`:414-416`（`_record_request` dm entry）与 `:464-466`（`_record_empty_retry` dm entry）；replace_all: true。spec 项 3）：

```python
# OLD
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0}
# NEW
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0}
```

**M7**（2 处相同，`:424-426` 与 `:469-471`（两处 daily 桶 setdefault）；replace_all: true。spec 项 3）：

```python
# OLD
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0})
# NEW
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0})
```

**M8**（`:454` `_record_empty_retry` docstring 字段数同步，注释准确性）：

```python
# OLD
    entry/桶形状必须与 _record_request 同步：dm entry 同为 5 字段（本函数无
# NEW
    entry/桶形状必须与 _record_request 同步：dm entry 同为 6 字段（本函数无
```

**M9**（`_record_empty_retry` 之后、`stats_snapshot` 之前插入新函数，spec 项 4。锚点：`_record_empty_retry` 尾部 + `def stats_snapshot`）：

```python
# OLD
        bucket["retries"] += 1
        EVENTS.append({"ts": time.time(), "kind": "retry", "model": model, "status": None})
        _stats_dirty = True


def stats_snapshot() -> dict:
# NEW
        bucket["retries"] += 1
        EVENTS.append({"ts": time.time(), "kind": "retry", "model": model, "status": None})
        _stats_dirty = True


def _record_eof_without_done(model=None) -> None:
    """EOF-without-done 计数：STATS 总量 + 当日桶 + daily_by_model。
    逐行镜像 _record_empty_retry（entry/桶形状同步），仅去掉 EVENTS 追加——
    eof 信号由计数器 + result 标记承载（spec Exclusions：事件流不扩展）。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["eof_without_done_total"] += 1
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0,
                                                "eof_without_done": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["eof_without_done"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0})
        bucket["eof_without_done"] += 1
        _stats_dirty = True


def stats_snapshot() -> dict:
```

**M10**（`:571` 调用点改元组解包，spec 项 7）：

```python
# OLD
                filtered = self._relay_sse(resp, final=(EMPTY_RETRY_MAX < 1))
# NEW
                filtered, truncated = self._relay_sse(resp, final=(EMPTY_RETRY_MAX < 1))
```

**M11**（`:585` 调用点改元组解包，spec 项 7）：

```python
# OLD
                filtered = self._relay_sse(resp, final=True)
# NEW
                filtered, truncated = self._relay_sse(resp, final=True)
```

**M12**（`:586-587` result 重算后加截断标记，spec 项 8。仅覆盖 `"ok"`；`upstream-err` 与 `aborted`（`:527` 异常路径不经此处）不误标；`:567` 初值只服务非 SSE 分支不动）：

```python
# OLD
            result = "ok" if resp.status < 400 else "upstream-err"  # 重试后按实际 resp 重算
            self._log(started, resp.status, result, filtered, model=model, retried=retried)
# NEW
            result = "ok" if resp.status < 400 else "upstream-err"  # 重试后按实际 resp 重算
            if result == "ok" and truncated:
                # 仅覆盖 ok：upstream-err（status≥400 更有信息量）与 aborted（异常
                # 路径不经此处）不误标；priming EOF 由 retries 计数承载，避免双计数
                result = "eof-without-done"
                _record_eof_without_done(model)
            self._log(started, resp.status, result, filtered, model=model, retried=retried)
```

（`truncated` 无 unbound 路径：`_EmptyStream` 异常时 `:571` 不赋值，但 `:585` 必然先于 `:586` 重赋值；重开上游失败的 return 路径不引用 `truncated`。）

**M13**（`:619` 签名改 `-> tuple`，spec 项 6）：

```python
# OLD
    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> int:
# NEW
    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
```

**M14**（`:623-627` 循环前初始化 `saw_done`/`truncated`，spec 项 6）：

```python
# OLD
            filtered = 0
            pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
            poisoned = False  # 当前 record 内是否命中毒行
            primed = []       # priming 阶段已滤毒缓冲的完整 record 行（含终结空行）
            priming = True    # True = 客户端尚未收到任何字节
# NEW
            filtered = 0
            saw_done = False   # 全程（priming+streaming）是否见过 [DONE] record
            truncated = False  # streaming 阶段 EOF 且全程无 done → 上游截断标记
            pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
            poisoned = False  # 当前 record 内是否命中毒行
            primed = []       # priming 阶段已滤毒缓冲的完整 record 行（含终结空行）
            priming = True    # True = 客户端尚未收到任何字节
```

**M15a**（`:632` kinds 计算后累计 saw_done，spec 项 6；覆盖 priming 与 streaming 两阶段所有 record）：

```python
# OLD
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
# NEW
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    saw_done = saw_done or ("done" in kinds)
```

**M15b**（`:654-662` EOF 分支加 `else: truncated = not saw_done`，spec 项 6。priming 内部逻辑不动：final flush / raise `_EmptyStream` 均保持 `truncated=False`）：

```python
# OLD
                    if line == b"":
                        if priming:  # EOF 仍 priming = 空流（零 record / reasoning-only 断流）
                            if final:  # 按现状语义收尾：缓冲原样下发（含合法 [DONE] 零内容流）
                                self._send_sse_headers(resp)
                                self.wfile.write(b"".join(primed))
                                self.wfile.flush()
                            else:
                                raise _EmptyStream(filtered, primed)
                        break
# NEW
                    if line == b"":
                        if priming:  # EOF 仍 priming = 空流（零 record / reasoning-only 断流）
                            if final:  # 按现状语义收尾：缓冲原样下发（含合法 [DONE] 零内容流）
                                self._send_sse_headers(resp)
                                self.wfile.write(b"".join(primed))
                                self.wfile.flush()
                            else:
                                raise _EmptyStream(filtered, primed)
                        else:
                            truncated = not saw_done  # streaming EOF 无 done = 上游截断
                        break
```

**M15c**（`:667` 返回元组，spec 项 6）：

```python
# OLD
            return filtered
# NEW
            return filtered, truncated
```

**M16**（`:1358` `main()` 恢复块加新计数键，spec 项 5）：

```python
# OLD
        STATS["empty_retries_total"] = counters["empty_retries_total"]
# NEW
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["eof_without_done_total"] = counters["eof_without_done_total"]
```

### Step 4 — 跑绿（VERIFY）

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期**：`Ran 57 tests in <N>s` + `OK`，exit 0。任何失败 → 对照 Step 2 红集逐个确认已转绿；仍有红 → fix loop（修 → 复跑 → 贴证）。

### Step 5 — commit

```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py && git commit -m "feat(stats): 上游 SSE 截断（EOF 无 [DONE]）检测与计数"
```

（单 commit：测试 + 实现同属一个逻辑改动；Conventional Commits `feat:`；commit 前 `git config --get user.name && git config --get user.email` 自检身份。）

---

## Plan Self-Review（5 项自查）

1. **spec coverage**：主文件 spec 8 项全部落卡——项 1→M1、项 2→M5（loader 零迁移由 `_DAILY_FIELDS` 迭代清洗既有机制承载，无需改 loader 代码）、项 3→M6/M7（4 处字面量，2 组 replace_all 各命中 2 处，grep 实测计数=2/2）、项 4→M9、项 5→M3/M4/M16、项 6→M13/M14/M15a/b/c、项 7→M10/M11、项 8→M12。测试侧 spec 3 类用例→E14（①含 SIGTERM 落盘 + resume 续算、②双对照组、③双空流）；spec 列出的 5 处既有断言更新→E1-E5/E11；**锚点实测发现 spec 漏列 5 处必红站**（dm 形状集合 :653-656/:685-688、dbm roundtrip :814-815/:836-840/:852-855）→ E6-E10 补齐，否则 executor 会在 Step 4 撞预期外红；E13 对应 spec Acceptance「旧文件加载为 0 不崩」。Exclusions 零触碰：EVENTS/白名单/dashboard/透传/重试机制均不动（M9 明确不加 EVENTS.append）。
2. **placeholder scan**：0 命中。全部 30 个编辑操作（E1-E14、M1-M16）均为逐字 old/new 代码块，无"参照 spec/同上"类指令；新函数与新测试完整写死。
3. **type consistency**：`_relay_sse -> tuple` 返回 `(filtered: int, truncated: bool)`，两个调用点均元组解包，无第三个调用点（grep 实测仅 :571/:585/:619）；`truncated` 无 unbound 路径（M12 注明）；计数键命名全局一致——总量 `"eof_without_done_total"`、分桶 `"eof_without_done"`、result 字符串 `"eof-without-done"`（与 `empty_retries_total`/`retries` 惯例对齐）；M6/M7/M9 三处创建字面量与 `_DAILY_FIELDS` 第 6 字段同键，`+=` 无 KeyError 路径。
4. **可落盘性**：所有 old_string 锚点经 grep 实测唯一（或 replace_all 且出现次数写死）；预期红集 12 方法逐一列出（executor 可机械核对）；验证命令固定 `/usr/bin/python3`；预期终态 57 tests OK；commit message 给死。无运行时决策点。
5. **锚点实测**：spec 主文件行号全部核对无误（:101-103/:290/:414-416/:424-426/:456/:464-466/:469-471/:474/:247-248/:284/:1355-1358/:619/:623/:632/:654-662/:667/:571/:585/:586/:567/:527/:698-704/:354/:973 均与实际代码一致，无漂移）。测试文件行号核对：spec 所列 :453-460/:538-543/:560-562/:488-491/:1149-1152/:1456/:1486/:1498/:1533 准确；**spec 的既有断言受波及清单不全**（漏 5 处，见第 1 项），已按实际代码补入卡内并在 E6-E10 标注"锚点实测补充"。
