# 2026-10-02 — 生成速度虚高修复 + token 用量 M 单位

## Goal

为 dashboard 用户修复两个数据显示缺陷：(1) 单请求生成速度 t/s 因分子误用 total_tokens（含 prompt）且分母跨 wall/monotonic 两时钟相减，在长上下文请求上虚高两个数量级（线上 66k–100k t/s）；(2) token 用量裸数字无单位，长上下文场景下按天×模型 / per-key / 月末投影 / 小时峰值等表格可读性差。改动集中于单文件 `ctyun-stream-fix-proxy.py`（后端 Python + 内嵌 dashboard JS）。

## Files to Change

唯一改动文件：`/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`；测试追加到 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`。

### 后端 Python（锚点均已经 Read 复核）

- **`_record_request()` 签名（ctyun-stream-fix-proxy.py:2298-2309）** — 新增一个关键字参数 `gen_ms=None`（放在既有 `bytes_in: int = 0` 之后，保持关键字-only 调用风格不变）。该参数语义：流式请求"首字节交付 → relay 结束"的 monotonic 窗口毫秒数；非流式 / 无意义时保持 None。

- **RECENT_REQUESTS append 条目（ctyun-stream-fix-proxy.py:2415-2421）** — dict 追加两个键：`"tokens_completion": tokens_completion`（`_record_request` 已有同名入参，见 :2303，当前只进 daily 桶未进 recent 条目）与 `"gen_ms": gen_ms`。RECENT_REQUESTS 是 `collections.deque(maxlen=100)`（:1414），纯内存、重启即清，旧条目不存在兼容问题——但 guard 仍需对缺键/None 健壮（防御性，见前端 genSpeed）。

- **流式收尾 `_record_request` 调用点（ctyun-stream-fix-proxy.py:2865-2926）** — `t_relay_done = time.monotonic()`（:2865）已存在且与 `_t_first_byte_mark`（同为 monotonic，:3059 写入）同时钟。在调用 `_record_request` 前新增：
  ```python
  gen_ms = (round((t_relay_done - self._t_first_byte_mark) * 1000, 1)
            if self._t_first_byte_mark is not None else None)
  ```
  并把 `gen_ms=gen_ms` 传入 :2911 起的 `_record_request(...)` 调用（`stream=1` 分支）。注意 `_t_first_byte_mark` 在 priming 交付时打点（:3059），是"客户端视角首字节"；空流重试路径 _relay_sse 入口已重置 mark（:3035），读到的是最终交付流的值，口径正确。

- **非流式收尾 `_record_request` 调用点（ctyun-stream-fix-proxy.py:2966-2981）** — `stream=0` 分支不传 `gen_ms`（默认 None）。非流式 `_relay_buffered` 里 `_t_first_byte_mark = t_body_done`（:3162）语义是"整个 body 读完时刻"，不是首字节，无生成窗口可言，gen_ms 恒 None，前端 genSpeed 对非流式本就 return null（`!r.stream`），行为不变。

### 前端 dashboard JS（内嵌 `_DASH_JS_V2`，锚点均已复核）

- **`genSpeed(r)`（ctyun-stream-fix-proxy.py:4287-4291）** — 整函数替换：
  ```javascript
  function genSpeed(r) {
    if (!r || !r.stream || !r.tokens_completion || r.gen_ms == null ||
        !(r.gen_ms >= 100)) return null;
    return r.tokens_completion / (r.gen_ms / 1000);
  }
  ```
  决策与理由：
  - 分子改 `tokens_completion`（只算生成侧，剔除 prompt 部分——线上 66k 上下文请求的 prompt 占比 >95% 是虚高主因）。
  - 分母改后端同时钟 `gen_ms`，杜绝 wall/monotonic 跨时钟相减。
  - 阈值 `gen_ms >= 100`（ms）：线上虚高样本分母仅 89.8ms；低于 100ms 的"生成窗口"对长 completion 无统计意义，且 monotonic 打点 ±ms 级误差在 <100ms 窗口里占比过大。100ms 是"足够排除打点噪声 + 排除首字节即 EOF 的近零生成"的下限，比 89.8ms 高出一档余量。低于此值显示 "—" 而非一个不可信的数字。
  - 两个调用点（慢请求 Top10 :4900-4901、模型对比中位 :4325-4326）共用此函数，改一处全覆盖，"—" 展示逻辑已在调用点就位（`gs != null ? ... : "—"`），无需改。

- **新增 `fmtTok(n)` helper（放在 `fmtBytes` 之后，ctyun-stream-fix-proxy.py:4820 之后）** —
  ```javascript
  function fmtTok(n) {
    if (n == null || !isFinite(n)) return "—";
    if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
    return String(Math.round(n));
  }
  ```
  决策：只做 M 一档（>=1e6 才缩写），不做 k。理由：token 计数典型值在 10^2–10^7 区间，k 档（1e3–1e6）数字最长 6 位仍可读；只有长上下文日/月累计稳定 ≥1e6 才需要缩写，过度分档反而让同一列单位忽 k 忽 M 不可比。与 fmtBytes 的 B/KB/MB 三档不同（bytes 跨 6 个数量级）。

- **`renderDailyByModel` 按天×模型表（ctyun-stream-fix-proxy.py:4396-4399）** — 四处 `String(tp)/String(tc)/String(cr)/String(rn)` 改 `fmtTok(tp)/fmtTok(tc)/fmtTok(cr)/fmtTok(rn)`（变量在 :4387-4390 定义）。排序键（:4382-4383）保持原始数值不动。

- **`renderByKey` per-key 用量表（ctyun-stream-fix-proxy.py:4772-4774）** — `String(k.tokens_prompt||0)/String(k.tokens_completion||0)/String(k.tokens_cache_read||0)` 三处改 `fmtTok(...)`。`requests`/`bytes_out`/`stream_requests` 列不动（bytes_out 非 token 语义，保留裸数）。

- **`renderQuota` 月末投影（ctyun-stream-fix-proxy.py:4808, 4810-4811）** — `$("q-mtd").textContent = String(mtd)` 改 `fmtTok(mtd)`；`q-proj` 的 `String(Math.round(...))` 改 `fmtTok(Math.round(...))`。`q-days`（"N/31"）不动。

- **`renderHourly` 小时 token 柱状图峰值文案（ctyun-stream-fix-proxy.py:5081-5082）** — `" · 峰值 " + max + " tokens/h"` 改 `" · 峰值 " + fmtTok(max) + " tokens/h"`。柱体几何（:5052-5067）保持原始数值不动。`renderHourlyBytes`（:5086-5135）用 fmtBytes 是对的，不动。

## Acceptance Criteria

测试命令（README.md:98 定死，unittest 非 pytest，测试文件 350 个 `test_` 入口在 `ctyun-stream-fix-proxy.test.py:8310 unittest.main`）：

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

预期：既有 350 个测试全绿（baseline 先跑一遍登记；若有 baseline 红按 AGENTS.md 规则另开 episode，本卡不背），新增测试全绿。

新增测试（追加到 `ctyun-stream-fix-proxy.test.py`，遵循既有先例）：

1. **RECENT_REQUESTS 条目含新字段且口径正确** — 仿 `RequestIdTest.test_recent_entry_carries_rid_host_ttfb_stream_outcome`（test.py:6320-6344，黑盒子进程集成：fake upstream + admin `/api/stats`）。fake upstream 需返回带 usage 的 SSE（既有 `SSE_USAGE` 常量 test.py:43 可复用，prompt=1/completion=1/total=2）。断言 `snap["recent"][-1]`：①含 `"tokens_completion"` 与 `"gen_ms"` 两键；②`entry["tokens_completion"] == 1`（来自 SSE_USAGE 的 completion_tokens，不是 total=2——锚死"用 completion 而非 total"）；③`entry["gen_ms"]` 是 float 且 >= 0。

2. **genSpeed JS 源码断言** — 仿既有 `_DASH_JS_V2` 字符串断言先例（test.py:3357 `self.assertIn("function genSpeed(", js, ...)`，同 class `load_proxy_module()` 取 `self.mod._DASH_JS_V2`）。断言 js 字符串：①含 `r.tokens_completion / (r.gen_ms / 1000)`；②含 `r.gen_ms >= 100`；③**不含** `r.dur_ms - r.ttfb_ms`（锚死旧 bug 公式已移除）。同时断言含 `function fmtTok(`。

3. **fmtTok 调用点覆盖** — 同 class 内断言 js 字符串含 `fmtTok(tp)`、`fmtTok(mtd)`、`fmtTok(max) + " tokens/h"`，锚死三处关键调用点已接线（按天×模型 / 月末投影 / 小时峰值）。

4. **手动验证（非自动测试，验收时做）** — 起一个代理，经它发一个带长 prompt + usage 帧的流式请求，打开 dashboard：慢请求 Top10 与模型对比中位的 t/s 应落在个位到三位数（不再是五位数）；按天×模型表 token 列在日累计 ≥1e6 时显示如 `1.23M`。

## Risks

- **`gen_ms` 打点口径**：`t_relay_done`（:2865）在 `_record_request` 调用前、`_log` 之后取，位于流式分支收尾；`_t_first_byte_mark` 在 priming flush 时打点（:3059）。若流式请求在 priming 阶段即 EOF 且无 usage（`_EmptyStream` 路径 :3142/3147 raise），不会走到 :2865 的收尾，该请求进 502 分支 `_record_request`（:2845-2849）——该调用点不传 `gen_ms`，recent 条目 `gen_ms=None`，前端显示 "—"。这是期望行为（无交付流就无生成速度可言），测试 1 的正常流不触发。
- **`tokens_completion` 进 recent 条目的内存成本**：deque maxlen=100，每条目多两个键（int + float/None），内存增量可忽略；不进 daily 桶、不落盘，无持久化 schema 变更。
- **JS 字符串断言的脆弱性**：测试 2/3 是源码子串断言，对函数体格式敏感（如 `r.gen_ms >= 100` 的空格）。implementer 写测试时按实际落盘的 JS 代码对齐子串，不要凭 spec 里的缩进臆测；spec 给的是语义锚点。
- **fmtTok 只改前端**：daily/daily_by_key 后端聚合字段（`DAILY_V2_FIELDS` :1941、`_DAILY_BY_KEY_FIELDS` :1959）与 `/api/stats` snap 输出零改动，下游消费方（若有脚本扒 API）拿到的仍是原始整数，无兼容风险。
- **baseline 红**：350 个测试全跑耗时不明，先跑一遍 baseline 登记初始红清单；本卡只对"baseline 绿→现在红"负责。

## Exclusions

- **不改总览卡 tokens/s**（`perf.tokens_per_s`，JS :4942；总吞吐口径含 prompt+completion 是有意的，与单请求生成速度语义不同）。
- **不改任何后端聚合/daily/daily_by_key/hourly 字段定义与落桶逻辑**（:1941-1959、:2362-2408 的桶增量原样保留）。
- **不加 k 档格式化**，不做 fmtBytes 的三档扩展。
- **不改非流式请求的展示**（非流式 recent 条目 `gen_ms=None`，genSpeed 对 `!r.stream` 本就 return null）。
- **不引入新依赖**（纯 stdlib Python + 原生 JS，与仓库现状一致）。
- **不动 `renderHourlyBytes` / `renderTraffic` 的 fmtBytes 用法**（bytes 语义正确，不在本 episode）。
- **不处理 RECENT_REQUESTS 之外的速度展示**（如 REQ 行 stderr 日志 :2904-2909 不含 t/s，不新增）。

## R31 Evidence

[R31-S1] 确认问题存在：线上 dashboard「生成速度」出现 66439 t/s 量级读数。拉取生产 /api/stats 的 recent[]，按前端现公式 tokens/((dur_ms-ttfb_ms)/1000) 复算命中同类虚高样本：

```
$ curl -s http://127.0.0.1:7921/api/stats | python3 -c "...按 genSpeed 公式复算 recent[]..."
{"rid": "r-26520", "model": "deepseek-v4-pro-0813-oc", "stream": 1, "tokens": 84359, "dur_ms": 4758.7, "ttfb_ms": 3758.7} -> gen t/s=84359
{"rid": "r-26511", "model": "deepseek-v4-pro-0813-oc", "stream": 1, "tokens": 79790, "dur_ms": 7706.8, "ttfb_ms": 7617.0} -> gen t/s=888530
{"rid": "r-26500", "model": "deepseek-v4-pro-0813-oc", "stream": 1, "tokens": 66576, "dur_ms": 12252.8, "ttfb_ms": 11272.8} -> gen t/s=67935
```

「生成速度」数值恰好等于该请求 total_tokens（r-26520：84359 tokens ÷ 恰好 1000.0ms 窗口 = 84359 t/s），且存在分母仅 89.8ms 的记录（r-26511 → 888530 t/s）；该批均为 66k–100k 长上下文请求，prompt 占比 >95%。确认虚高非真实生成速度。

[R31-S2] 确定根因：dashboard 内嵌 JS `genSpeed(r)`（ctyun-stream-fix-proxy.py:4287-4291）分子取 `r.tokens`——由 `_record_request` 的 `tokens` 入参写入 RECENT_REQUESTS 条目（:2415-2421），流式路径传 `p3_tokens[2]` 即 total_tokens（含 prompt）；分母 `dur_ms - ttfb_ms` 是 wall clock（time.time()）与 monotonic（`_t_first_byte_mark`）跨时钟相减，且仅挡 `dur<=ttfb`、无毫秒级下限 guard。现状代码：

```
function genSpeed(r) {
  if (!r || !r.stream || !r.tokens || !(r.dur_ms > 0) || r.ttfb_ms == null ||
      r.dur_ms <= r.ttfb_ms) return null;
  return r.tokens / ((r.dur_ms - r.ttfb_ms) / 1000);
}
```

$ node ~/.zcode/scripts/validate-r31-evidence.mjs docs/superpowers/specs/2026-10-02-genspeed-munit-design.md
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -3

Ran 350 tests in 153.497s — OK（baseline 全绿）
