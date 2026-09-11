# ctyun-proxy 按天统计 PLAN（simplified mode）

- **Feature**: 7920 剥行代理 daily 分桶统计（requests/filtered/errors_proxy/errors_upstream 按本地日期持久化）+ dashboard 按天卡
- **Branch**: `fix/ctyun-proxy-daily-stats`（worktree `.worktrees/ctyun-proxy-daily-stats`）
- **Spec**: `docs/superpowers/specs/2026-09-05-ctyun-proxy-daily-stats-design.md`
- **执行者**: 主代理全程亲自执行（用户显式指令：本会话不派子代理）
- **Date**: 2026-09-05

## Global Constraints

- 运行/测试一律 `/usr/bin/python3` 绝对路径（Homebrew 3.14.7 `http.server` 挂死坑）。
- 499 aborted 不进任何错误桶（v1.1 中断静默化语义不变）；`errors_total` 语义零改动。
- `errors_proxy` 与 `errors_upstream` 互斥：`error=True`（代理合成 502）→ 仅 errors_proxy；`error=False` 且 `status>=500`（上游透传 5xx）→ 仅 errors_upstream。
- 既有 20 测试除 `test_dashboard_html_full_page` 尾部追加断言外零改动全绿；`make_fake_upstream` 只允许追加带默认值的 `fail_500` 形参（向后兼容）。
- 实现落点 `~/.local/bin/`，与测试同目录跑；完成后归档 `~/Documents/project/ctyun-stream-fix-proxy/` byte-identical。
- 不记 token；不做 daily.by_model；不动 plist/剥行逻辑/鉴权。

## Task 1: TDD——daily 分桶 + 双口径错误 + 持久化 + dashboard 按天卡

**Step 1 先写失败测试**（`~/.local/bin/ctyun-stream-fix-proxy.test.py`）：

1a. `FakeUpstreamHandler` 类属性追加（`big = False` 行后）：

```python
    fail_500 = False  # True → do_POST 回 500 JSON（上游 5xx 透传计数测试用）
```

1b. `do_POST` 开头（`if self.big:` 之前）插入：

```python
        if self.fail_500:
            body = b'{"error":"upstream exploded"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
```

1c. `make_fake_upstream` 签名与 type() 调用改为：

```python
def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
                       fail_500: bool = False) -> int:
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,),
                   {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500})
```

1d. `ProxyDashboardUnitTest` 追加 5 个单测（`test_stats_snapshot_shape` 方法之后）：

```python
    def test_daily_bucket_accumulation_and_dual_error_semantics(self) -> None:
        mod = self.mod
        today = mod.today_key()
        bucket = mod.STATS["daily"].setdefault(
            today, {"requests": 0, "filtered": 0, "errors_proxy": 0, "errors_upstream": 0})
        base = dict(bucket)
        err_base = mod.STATS["errors_total"]
        mod._record_request("POST", "/d1", 200, 1.0, 2)
        mod._record_request("POST", "/d2", 502, 1.0, 0, error=True)
        mod._record_request("POST", "/d3", 500, 1.0, 0)
        mod._record_request("POST", "/d4", 499, 1.0, 0)
        self.assertEqual(bucket["requests"], base["requests"] + 4)
        self.assertEqual(bucket["filtered"], base["filtered"] + 2)
        self.assertEqual(bucket["errors_proxy"], base["errors_proxy"] + 1,
                         "error=True (proxy-made 502) must land in errors_proxy only")
        self.assertEqual(bucket["errors_upstream"], base["errors_upstream"] + 1,
                         "upstream 500 passthrough must land in errors_upstream only")
        self.assertEqual(mod.STATS["errors_total"], err_base + 1,
                         "errors_total semantics unchanged (proxy errors only)")
        self.assertEqual(bucket["errors_upstream"], base["errors_upstream"] + 1,
                         "499 aborted must not inflate errors_upstream")

    def test_daily_bucket_spans_days(self) -> None:
        mod = self.mod
        today = mod.today_key()
        orig = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-02"
            mod._record_request("POST", "/span", 200, 1.0, 1)
        finally:
            mod.today_key = orig
        self.assertIn("2026-01-02", mod.STATS["daily"])
        self.assertIn(today, mod.STATS["daily"])
        self.assertIsNot(mod.STATS["daily"]["2026-01-02"], mod.STATS["daily"][today])

    def test_daily_persist_roundtrip_legacy_and_corrupt(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit3-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://x",
                       "stats": {"requests_total": 1}}, fh)
        self.assertEqual(mod.load_daily_buckets(path), {},
                         "legacy file without daily key must yield {}")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": "not-a-dict"}}, fh)
        self.assertEqual(mod.load_daily_buckets(path), {})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": {
                "2026-01-01": "bad",
                "2026-01-02": {"requests": 5, "filtered": 1,
                               "errors_proxy": 0, "errors_upstream": 2}}}}, fh)
        buckets = mod.load_daily_buckets(path)
        self.assertNotIn("2026-01-01", buckets, "non-dict bucket must be skipped")
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(buckets["2026-01-02"]["errors_upstream"], 2)

    def test_daily_prune_on_save(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit4-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_daily = mod.STATS["daily"]
        mod.STATS["daily"] = {}
        try:
            for i in range(95):
                mod.STATS["daily"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "requests": i, "filtered": 0,
                    "errors_proxy": 0, "errors_upstream": 0}
            mod.save_stats_counters(path)
        finally:
            mod.STATS["daily"] = orig_daily
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertLessEqual(len(saved), 90, "prune must cap buckets at 90 days")
        self.assertIn("2026-04-11", saved, "most recent bucket must survive prune")

    def test_stats_snapshot_daily_is_copy(self) -> None:
        mod = self.mod
        snap = mod.stats_snapshot()
        self.assertIn("daily", snap)
        snap["daily"]["mutation-test"] = {"requests": 1, "filtered": 0,
                                          "errors_proxy": 0, "errors_upstream": 0}
        self.assertNotIn("mutation-test", mod.STATS["daily"],
                         "snapshot must hand out copies, not internal refs")
```

1e. `AdminIntegrationTest` 追加 3 个集成测试（`test_config_post_localhost_allowed_even_with_token_env` 之后）：

```python
    def test_daily_bucket_via_sse_and_persist(self) -> None:
        today = time.strftime("%Y-%m-%d")
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertIn(today, snap["daily"])
        self.assertGreaterEqual(snap["daily"][today]["requests"], 1)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stderr.read()
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertGreaterEqual(saved[today]["requests"], 1,
                                "daily buckets must persist on SIGTERM")

    def test_upstream_500_counts_into_daily_errors_upstream(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stderr.read()
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        bad_port = make_fake_upstream(False, fail_500=True)
        self.proc = start_proxy(bad_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 500)
        resp.read()
        conn.close()
        today = time.strftime("%Y-%m-%d")
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(snap["daily"][today]["errors_upstream"], 1)
        self.assertEqual(snap["errors_total"], 0,
                         "upstream 5xx passthrough must not touch errors_total")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stderr.read()
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertGreaterEqual(saved[today]["errors_upstream"], 1)

    def test_daily_buckets_resume_from_persist(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        self.proc.stderr.read()
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                          "stats": {"requests_total": 7, "filtered_total": 3,
                                    "errors_total": 1,
                                    "daily": {"2026-01-01": {
                                        "requests": 5, "filtered": 1,
                                        "errors_proxy": 0, "errors_upstream": 2}}}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["daily"]["2026-01-01"]["requests"], 5)
        self.assertEqual(snap["daily"]["2026-01-01"]["errors_upstream"], 2)
```

1f. `test_dashboard_html_full_page` 尾部（`self.assertIn("theme-color", html)` 行后）追加：

```python
        # v1.2：按天统计卡（日期表 + 双口径错误列）
        self.assertIn("按天统计", html)
        self.assertIn("<th>上游5xx</th>", html)
        self.assertIn('id="daily-body"', html)
```

**Step 2 跑测试确认红相**（新增 8 红 / 既有 20 绿；红点 = `today_key`/`daily` 属性缺失、`load_daily_buckets` 不存在、HTML 无按天卡）：

```
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -5
```

**Step 3 实现**（`~/.local/bin/ctyun-stream-fix-proxy.py`）：

3a. 常量（`BY_MODEL_CAP = 32` 行后）：

```python
DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）
```

3b. `extract_model` 函数后新增两个纯函数：

```python
def today_key() -> str:
    """当日日期桶 key（本地时区）；模块级函数便于测试 patch 模拟跨天。"""
    return time.strftime("%Y-%m-%d", time.localtime())


def _prune_daily(daily: dict) -> dict:
    """按日期 key 降序保留最近 DAILY_RETENTION_DAYS 天（ISO 日期字符串排序即时间序）。"""
    if len(daily) <= DAILY_RETENTION_DAYS:
        return daily
    for key in sorted(daily)[:-DAILY_RETENTION_DAYS]:
        del daily[key]
    return daily
```

3c. `STATS` 初始 dict（:52-53）改为：

```python
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "active": 0, "by_model": {}, "daily": {}}
```

3d. `_record_request` 内（`if entry is not None:` 块之后、`RECENT_REQUESTS.append` 之前）插入：

```python
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0})
        bucket["requests"] += 1
        bucket["filtered"] += filtered
        if error:
            bucket["errors_proxy"] += 1
        elif status >= 500:
            bucket["errors_upstream"] += 1
```

3e. `load_stats_counters` 重构为共享读 + 新增 `load_daily_buckets`（替换原 `load_stats_counters` 全函数）：

```python
def _load_persist_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # 吞掉的是持久化文件缺失（OSError，首次运行常态）与损坏 JSON（ValueError）：
        # 该文件是"网页上次设置 + 计数缓存"，损坏等同首次运行回落零值，无其他路径可达。
        return {}
    return data if isinstance(data, dict) else {}


def load_stats_counters(path: str) -> dict:
    """从持久化文件读累计计数；缺文件/损坏/legacy 无 stats 键 → 三零值。"""
    stats = _load_persist_file(path).get("stats")
    out = {}
    for key in ("requests_total", "filtered_total", "errors_total"):
        value = stats.get(key) if isinstance(stats, dict) else None
        out[key] = value if isinstance(value, int) and value >= 0 else 0
    return out


_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream")


def load_daily_buckets(path: str) -> dict:
    """从持久化文件读按天分桶；缺/损坏/非法结构 → {}，逐桶容错（手工改坏文件不崩）。"""
    stats = _load_persist_file(path).get("stats")
    raw = stats.get("daily") if isinstance(stats, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, bucket in raw.items():
        if not isinstance(bucket, dict):
            continue
        clean = {}
        for field in _DAILY_FIELDS:
            value = bucket.get(field)
            clean[field] = value if isinstance(value, int) and value >= 0 else 0
        out[key] = clean
    return out
```

（独立 `load_daily_buckets` 而非在 `load_stats_counters` 返回值加键：保既有 `test_stats_persist_roundtrip_and_defaults` 的精确相等断言零改动。）

3f. `save_stats_counters` 全函数替换为：

```python
def save_stats_counters(path: str) -> None:
    """把累计计数、按天分桶与当前上游端点全量写入持久化文件（SIGTERM / set_upstream_base 共用）。"""
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total")}
        daily = {k: dict(v) for k, v in STATS["daily"].items()}
    _prune_daily(daily)
    with _CFG_LOCK:
        base = UPSTREAM_BASE
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base,
                   "stats": dict(counters, daily=daily)}, fh, ensure_ascii=False)
    os.replace(tmp, path)
```

3g. `stats_snapshot` 的 `with STATS_LOCK:` 块内（`snap["by_model"] = ...` 行后）加：

```python
        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
```

3h. `main()` 启动加载段（`counters = load_stats_counters(...)` 至 `with STATS_LOCK:` 块）改为：

```python
    counters = load_stats_counters(PERSIST_PATH)  # 累计计数跨重启续算
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["daily"] = daily
        _stats_dirty = False
```

3i. `DASHBOARD_SRC`：「按模型」section 之后插入按天卡：

```html
  <section class="card">
    <div class="card-title">按天统计（最近 14 天，新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th></tr></thead>
      <tbody id="daily-body"><tr><td class="empty" colspan="5">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
```

JS `renderPoison` 函数后新增：

```javascript
function renderDaily(daily) {
  var body = $("daily-body");
  body.textContent = "";
  var keys = Object.keys(daily).sort().reverse().slice(0, 14);
  if (keys.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天统计");
    td0.colSpan = 5;
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
    body.appendChild(tr);
  }
}
```

`poll()` 成功回调 `renderPoison(snap.poison_previews || []);` 行后加：

```javascript
      renderDaily(snap.daily || {});
```

footer 文案改为：

```html
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json）；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（errors_total 同口径），
  「上游5xx」=上游透传 status≥500，两者互斥。页面每 2s 轮询 /api/stats；非本机修改上游需 X-Admin-Token。
```

**Step 4 验证绿相**：

```
/usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
```

通过标准：`Ran 28 tests` + `OK` + exit 0。

## Task 2: 部署实机验证 + 归档同步

2a. 重启常驻代理（用户已确认本会话不派子代理，重启不断本会话链路——主代理走 BigModel）：

```
launchctl kickstart -k gui/$UID/com.zcode.ctyun-stream-fix-proxy
sleep 2 && launchctl print gui/$UID/com.zcode.ctyun-stream-fix-proxy | grep -E "state|pid"
```

2b. 实机验证（贴 stdout）：

```
curl -s http://127.0.0.1:7921/api/stats | jq '.daily | {today: .["'"$(date +%F)"'"]}'
curl -s http://127.0.0.1:7920/chat/completions -H "Authorization: Bearer $(jq -r '[.. | objects | select((.baseURL // "") | contains("7920"))][0].apiKey' /Users/peter/.zcode/v2/config.json)" -H "Content-Type: application/json" -d '{"model":"glm-5.3","messages":[{"role":"user","content":"say ok"}],"max_tokens":16,"stream":true}' -o /dev/null -w "%{http_code}\n"
curl -s http://127.0.0.1:7921/api/stats | jq '.daily | to_entries | max_by(.key)'
jq '.stats.daily | keys' ~/.local/etc/ctyun-stream-fix-proxy.json
```

通过标准：① 重启后 `/api/stats` daily 含重启前同日桶（计数延续）；② 经代理真实请求后当天 requests 增 1；③ persist 文件 daily 与 API 一致。

2c. 归档同步（byte-identical）：

```
cp /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/
cmp /Users/peter/.local/bin/ctyun-stream-fix-proxy.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py && cmp /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py && echo archive-identical
```

## 风险与回退

- 部署后 dashboard 若异常：launchd KeepAlive 自愈；回退 = `cp ~/Documents/project/ctyun-stream-fix-proxy/` 上版文件回 `~/.local/bin/` + kickstart（归档在本次 cp 前仍是 v1.1 版）。
- persist 文件 schema 前向兼容（v1.1 文件无 daily → `{}` 起步）；手工改坏 → 逐桶容错。
