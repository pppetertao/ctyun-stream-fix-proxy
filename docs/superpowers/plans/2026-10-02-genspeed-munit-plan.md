# PLAN — 生成速度虚高修复 + token 用量 M 单位

- **Feature:** fix/gen-speed-munit
- **Branch:** fix/gen-speed-munit
- **Date:** 2026-10-02
- **Spec:** `docs/superpowers/specs/2026-10-02-genspeed-munit-design.md`

## Global Constraints

- 改动文件仅两个：`ctyun-stream-fix-proxy.py`（生产）+ `ctyun-stream-fix-proxy.test.py`（追加测试）。其余文件一律不碰。
- 不加新依赖（纯 stdlib Python + 原生 JS）。
- 不改后端聚合/daily/daily_by_key/hourly 字段定义（DAILY_V2_FIELDS :1941、_DAILY_BY_KEY_FIELDS :1959 原样）。
- 总览卡 tokens/s（`perf.tokens_per_s`）不动；`renderHourlyBytes`/`renderTraffic` 的 fmtBytes 用法不动。
- 非流式路径不传 gen_ms（默认 None），前端 `!r.stream` 返回 null 的行为不变。
- JS 源码断言的子串已按 `_DASH_JS_V2` 实际落盘格式（本文档写作时 Read 实测 :4286-4291 / :4816-4825 / :5072-5085）逐字对齐，执行者不得凭 spec 缩进臆测改写。
- 验证命令：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（baseline 已登记：Ran 350 tests — OK，全绿）。收尾全量跑预期 353 绿。
- tier：卡 1、卡 2 均为 A（全部代码已在本文档写死，无 C 档留白——无真机依赖、无运行时数据依赖、无 DI 边界实测）。

---

## 卡 1：后端 `gen_ms` 打点 + `tokens_completion`/`gen_ms` 进 RECENT_REQUESTS 条目（tier A）

**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

**目标：** 流式请求收尾计算"首字节交付 → relay 结束"的 monotonic 窗口 `gen_ms`（毫秒，1 位小数），连同 `tokens_completion` 一起写入 recent 条目；新增黑盒集成测试锚死两键的口径。

### 生产代码改动（`ctyun-stream-fix-proxy.py`，全部 Edit 逐字执行）

**Edit 1.1 — `_record_request()` 签名新增 `gen_ms`（当前 :2298-2309）**

old:

```
                    # v3.1：入方向字节（请求体 Content-Length）
                    bytes_in: int = 0) -> None:
```

new:

```
                    # v3.1：入方向字节（请求体 Content-Length）
                    bytes_in: int = 0,
                    gen_ms=None) -> None:
```

**Edit 1.2 — RECENT_REQUESTS 条目追加两键（当前 :2415-2421）**

old:

```
                                "tokens": tokens, "bytes_out": bytes_out,
```

new:

```
                                "tokens": tokens, "tokens_completion": tokens_completion,
                                "gen_ms": gen_ms, "bytes_out": bytes_out,
```

**Edit 1.3 — 流式收尾调用点前计算 gen_ms（当前 :2910-2911，`stream=1` 分支）**

old:

```
            p3_tokens = self._p3_usage_tokens
            _record_request(self.command, self.path, resp.status,
```

new:

```
            p3_tokens = self._p3_usage_tokens
            gen_ms = (round((t_relay_done - self._t_first_byte_mark) * 1000, 1)
                      if self._t_first_byte_mark is not None else None)
            _record_request(self.command, self.path, resp.status,
```

**Edit 1.4 — 流式 `_record_request` 调用传 `gen_ms=gen_ms`（当前 :2923-2926）**

（old_string 缩进与邻近行一致；锚点 `p3_tokens[4]` 保证只命中流式分支，不被非流式 `p3_buf_tokens` 版本误击。）

old:

```
                            tokens_reasoning=p3_tokens[4] if p3_tokens else 0,
                            qwait_ms=tpm_qwait_ms,
                            key_id12=tpm_key[:12] if tpm_key else None,
                            bytes_in=length,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

new:

```
                            tokens_reasoning=p3_tokens[4] if p3_tokens else 0,
                            qwait_ms=tpm_qwait_ms,
                            key_id12=tpm_key[:12] if tpm_key else None,
                            bytes_in=length,
                            gen_ms=gen_ms,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

（非流式 `_record_request` 调用点 :2966-2981 不传 gen_ms，默认 None，零改动。）

### 测试代码（追加到 `ctyun-stream-fix-proxy.test.py`，插入位 `if __name__ == "__main__":`（:8304）之前）

```python
class GenSpeedRecentEntryTest(unittest.TestCase):
    """生成速度修复：RECENT 条目含 tokens_completion + gen_ms 两键且口径正确。

    黑盒子进程集成：scripted 上游回 SSE_USAGE(prompt=1,completion=1,total=2)
    的流 → recent[-1] 的 tokens_completion 必须取 completion（=1 而非 total=2），
    gen_ms 为流式交付窗口毫秒数（float 且 >= 0）。"""

    def setUp(self) -> None:
        upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_recent_entry_carries_completion_and_gen_ms(self) -> None:
        data = post_sse(self.proxy_port)
        self.assertEqual(data, SSE_USAGE + SSE_A + SSE_B + SSE_DONE,
                         "stream must relay byte-exact")
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertTrue(snap["recent"], "RECENT_REQUESTS must not be empty")
        entry = snap["recent"][-1]
        for key in ("tokens_completion", "gen_ms"):
            self.assertIn(key, entry,
                          "recent entry must carry '%s' key; got keys %r"
                          % (key, sorted(entry.keys())))
        self.assertEqual(entry["tokens_completion"], 1,
                         "completion must be usage completion_tokens (=1), "
                         "not total_tokens (=2)")
        self.assertIsInstance(entry["gen_ms"], float,
                              "gen_ms must be a float ms window")
        self.assertGreaterEqual(entry["gen_ms"], 0,
                                "gen_ms must be >= 0 (monotonic window)")
```

### 验证命令

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedRecentEntryTest -v
```

预期：`Ran 1 test — OK`（先跑一遍确认测试失败——断言 `tokens_completion`/`gen_ms` 缺键，TDD red；改完生产代码转绿）。

---

## 卡 2：前端 `genSpeed` 新公式 + `fmtTok` helper 及接线（tier A）

**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

**目标：** `_DASH_JS_V2` 内 genSpeed 换新公式（completion tokens / monotonic gen_ms，≥100ms 下界），新增 `fmtTok` helper（≥1e6 才缩 M），六处 token 数字展示接线 fmtTok；新增模块级 JS 字符串断言锚死公式与接线。

### 生产代码改动（`ctyun-stream-fix-proxy.py`，全部 Edit 逐字执行）

**Edit 2.1 — `genSpeed` 整函数替换（当前 :4286-4291）**

old:

```
_DASH_JS_V2 = """// v3.1：单请求生成速度（流式且 tokens>0 且耗时>首字节时有效，tokens/s 综合）
function genSpeed(r) {
  if (!r || !r.stream || !r.tokens || !(r.dur_ms > 0) || r.ttfb_ms == null ||
      r.dur_ms <= r.ttfb_ms) return null;
  return r.tokens / ((r.dur_ms - r.ttfb_ms) / 1000);
}
function median(xs) {
```

new:

```
_DASH_JS_V2 = """// v3.1：单请求生成速度（流式，completion tokens / monotonic 生成窗口 >=100ms 时有效，tokens/s）
function genSpeed(r) {
  if (!r || !r.stream || !r.tokens_completion || r.gen_ms == null ||
      !(r.gen_ms >= 100)) return null;
  return r.tokens_completion / (r.gen_ms / 1000);
}
function median(xs) {
```

（两个调用点 :4314 与 :4900-4901 共用此函数，`gs != null ? ... : "—"` 展示逻辑已在位，零改动。）

**Edit 2.2 — 新增 `fmtTok` helper（插入 fmtBytes 之后、fmtHour 之前，当前 :4816-4825）**

old:

```
function fmtBytes(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + "KB";
  return String(Math.round(n)) + "B";
}
function fmtHour(ts) {
```

new:

```
function fmtBytes(n) {
  if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB";
  if (n >= 1024) return (n / 1024).toFixed(1) + "KB";
  return String(Math.round(n)) + "B";
}
function fmtTok(n) {
  if (n == null || !isFinite(n)) return "—";
  if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
  return String(Math.round(n));
}
function fmtHour(ts) {
```

**Edit 2.3 — `renderDailyByModel` 按天×模型表四处接线（当前 :4396-4399）**

old:

```
      tr.appendChild(el("td", "num", String(tp)));
      tr.appendChild(el("td", "num", String(tc)));
      tr.appendChild(el("td", "num", String(cr)));
      tr.appendChild(el("td", "num", String(rn)));
```

new:

```
      tr.appendChild(el("td", "num", fmtTok(tp)));
      tr.appendChild(el("td", "num", fmtTok(tc)));
      tr.appendChild(el("td", "num", fmtTok(cr)));
      tr.appendChild(el("td", "num", fmtTok(rn)));
```

（排序键 :4382-4383 保持原始数值，不动。）

**Edit 2.4 — `renderByKey` per-key 用量表三处接线（当前 :4772-4774）**

old:

```
    tr.appendChild(el("td", "num", String(k.tokens_prompt || 0)));
    tr.appendChild(el("td", "num", String(k.tokens_completion || 0)));
    tr.appendChild(el("td", "num", String(k.tokens_cache_read || 0)));
```

new:

```
    tr.appendChild(el("td", "num", fmtTok(k.tokens_prompt || 0)));
    tr.appendChild(el("td", "num", fmtTok(k.tokens_completion || 0)));
    tr.appendChild(el("td", "num", fmtTok(k.tokens_cache_read || 0)));
```

（`requests`/`bytes_out`/`stream_requests` 列不动。）

**Edit 2.5 — `renderQuota` 月末投影两处接线（当前 :4808, :4810-4811）**

old:

```
  $("q-mtd").textContent = String(mtd);
  $("q-days").textContent = daysSeen + "/" + daysInMonth;
  $("q-proj").textContent = daysSeen > 0
    ? String(Math.round(mtd / daysSeen * daysInMonth)) : "—";
```

new:

```
  $("q-mtd").textContent = fmtTok(mtd);
  $("q-days").textContent = daysSeen + "/" + daysInMonth;
  $("q-proj").textContent = daysSeen > 0
    ? fmtTok(Math.round(mtd / daysSeen * daysInMonth)) : "—";
```

**Edit 2.6 — `renderHourly` 小时峰值文案接线（当前 :5081-5082）**

old:

```
    " · 峰值 " + max + " tokens/h";
```

new:

```
    " · 峰值 " + fmtTok(max) + " tokens/h";
```

（柱体几何 :5052-5067 保持原始数值，不动；`renderHourlyBytes` 的 fmtBytes 不动。）

### 测试代码（追加到 `ctyun-stream-fix-proxy.test.py`，插入位 `if __name__ == "__main__":`（:8304）之前，放在卡 1 的类之后）

```python
class GenSpeedDashJSV2Test(unittest.TestCase):
    """生成速度修复：_DASH_JS_V2 中 genSpeed 新公式 + fmtTok helper 及接线
    （模块级字符串断言，无需子进程）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_genspeed_js_formula_and_fmt_tok(self) -> None:
        js = self.mod._DASH_JS_V2
        self.assertIn("function genSpeed(", js, "缺生成速度计算")
        self.assertIn("r.tokens_completion / (r.gen_ms / 1000)",
                      js, "genSpeed 分子分母必须为 completion tokens / gen_ms")
        self.assertIn("r.gen_ms >= 100", js,
                      "genSpeed 必须保留 >=100ms 下界 guard")
        self.assertNotIn("r.dur_ms - r.ttfb_ms", js,
                         "旧公式 tokens/(dur_ms-ttfb_ms) 必须移除")
        self.assertIn("function fmtTok(", js, "缺 token M 单位格式化 helper")

    def test_fmt_tok_call_sites(self) -> None:
        js = self.mod._DASH_JS_V2
        for sub in ("fmtTok(tp)", "fmtTok(tc)", "fmtTok(cr)", "fmtTok(rn)",
                    "fmtTok(mtd)", 'fmtTok(max) + " tokens/h"'):
            self.assertIn(sub, js, "缺 fmtTok 接线 %s" % sub)
```

### 验证命令

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedDashJSV2Test -v
```

预期：`Ran 2 tests — OK`（先跑一遍确认 red——旧公式 `r.dur_ms - r.ttfb_ms` 存在且新子串缺失；改完转绿）。

---

## 收尾验证（最后一卡完成后同次 dispatch 跑全量）

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 353 tests — OK`（既有 350 全绿 + 新增 3 绿；baseline 已登记全绿，新增红一律本卡修）。
