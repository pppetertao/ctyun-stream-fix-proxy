# PLAN — observability v2 P2（速度期：TTFB 首字节口径 + 阶段拆分 + histogram + 吞吐 + stall）

- **Spec 权威源**：`docs/superpowers/specs/2026-09-30-ctyun-proxy-observability-v2-design.md`（P2 节 + Risks R1/R4；P1 节为地基现状）
- **分支/worktree**：fix/observability-v2-p2 @ 4772648（P1 已合入）
- **生产文件**：`ctyun-stream-fix-proxy.py`；**测试文件**：`ctyun-stream-fix-proxy.test.py`；README 不动（P2 无 API 面变化）
- **P3-P5 不进本计划**：tokens/usage 抽取、daily v2 字段、probe、dashboard 均属后续期次
- **Baseline**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 133 tests ... OK`（61.1s），133 全绿
- **卡数/tier**：3 卡，全部 tier A（纯转写，无真机/运行时判据留白）
- **单测命令形式（P1 教训，全程沿用）**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py <TestClass>.<method>`；文件名带连字符，**禁 `-m unittest` 模块路径**；exit code 用 `cd <worktree> && cmd ... ; echo "exit=$?"` 重定向取真值

---

## Global Constraints

1. **热路径纪律（R1，最高）**：`_relay_sse` 每 record 只允许一次 `time.monotonic()` + 整数加法；stall 自增是热循环内唯一持锁点且仅 >阈值 间隙命中（正常流 0 命中）；禁止 per-record 日志/落盘/分配大对象。histogram 桶更新与 rates 滚动全部在 `_record_request` 的 STATS_LOCK 临界区内完成（每请求一次，非每 record）。
2. **维度纪律（R4）**：`ttfb_hist` 按模型分桶（BY_MODEL_CAP=32 复用）；`phase_ms` 全局三阶段 **不按模型**——禁止实现成 32×9×3。
3. **内存态不持久化**：`ttfb_hist`/`phase_ms`/`rates`/`stalls_total` 只进 STATS 内存态；`save_stats_counters`（:518-543 白名单拷贝）/`load_stats_counters`（:557-565）**零改动**；`main()` 的计数器加载循环（:1980-1990）只摸 7 个累计键，新键保持模块初始值（重启清零，spec 明示）。
4. **计时口径**：热路径一律 `time.monotonic()`（防 NTP 跳变污染直方图）；`time.time()` 仅保留在既有 dur/ts 展示字段。
5. **Edit 精确匹配纪律**：每卡 old_string 取自本 PLAN 写盘时的实测文件内容（锚点表见文末）；executor 转写时若 old_string 匹配失败 → STOP 上报（P1 合并后行号漂移的防御），不自行发挥。
6. **测试命令**：baseline 前先实跑；每卡验证命令只跑本卡涉及测试类（+ 全量收尾在卡 3）；`assert` 失败输出必须贴原文。
7. **不加依赖**：全部 stdlib；`import bisect` 是新增的唯一 import（stdlib）。
8. **无死代码**：新增函数/属性必须被生产代码或测试引用。

---

## Anchor Reconciliation（spec 锚点 vs P1 合入后实测）

| 项 | spec 锚 | 实测现状（P1 后） | 裁决 |
|---|---|---|---|
| STATS 定义 | :314 | :331-334 | 用实测行 |
| `_record_request` | :680（7 参数版） | :706（P1 已扩 12 参数版） | 用实测签名，P2 在其上追加 2 参数 |
| `_relay_sse` | :1056 | :1123 | 用实测行 |
| `_relay_buffered` | :1143 | :1210 | 用实测行 |
| `_proxy_relay` | :879 | :921 | 用实测行 |
| `stats_snapshot` | :815 | :847 | 用实测行 |
| `_open_upstream` | :1028 | :1094 | 用实测行 |
| **P1 已存在的 headers 口径 ttfb** | P1 只记 headers 口径 | :983 已有 `ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)`，:1025 重试路径重复一次 | P2 在 `_relay_sse`/`_relay_buffered` 返回后用首字节/体读完口径**覆盖**（spec 明示 TTFB 口径升级）；headers 口径保留为 phase `headers` 桶数据源 |
| 阶段拆分数据源 | spec 未点名 `_open_upstream` 内部打点 | `_open_upstream` :1103 `conn.request(...)` → :1108 `resp = conn.getresponse()` 是 connect 与 headers 的天然分界 | 在 :1103 后插入 `self._t_request_sent = time.monotonic()`；connect = `t_request_sent - t_conn_start`，headers = `t_headers_done - t_request_sent`，body = `t_relay_done - t_headers_done` |
| `hist_percentile` 末桶插值 | spec 未规定 | HIST_BUCKETS_MS 末桶 [30000, +inf) 无法线性插值 | 末桶命中返回下界 `edges[-1]`（保守口径），PLAN 写死并有测试锁定 |
| `stream_share` 数据源 | spec 未规定 | RECENT_REQUESTS（maxlen 100）条目带 `stream` 字段（P1 已交付） | 用最近 ≤100 条请求的 stream 占比（无需新计数器，不扩 STATS schema）；测试锁定 |
| rates 60s 窗口滚动 | "只在新请求进入且 now - window_start >= 60 时发生一次" | — | 严格照 spec：`_record_request` 内先滚动后累加；snapshot 中 elapsed ≥ 60 时速率报 0（窗口过期无近期流量），PLAN 写死并测试锁定 |
| `PerfHistTest`/`TtfbStreamTest` | spec 测试节 | 测试文件不存在这两类 | 本 PLAN 新增，沿用 `load_proxy_module`/`start_proxy`/`make_fake_upstream`/`post_sse`/`admin_get` 基建；stall 阈值 env seam `CTYUN_STALL_THRESHOLD_S`（默认 5.0）为 spec 测试节"用 env seam 把 stall 阈值降到 0.5s 加速"的落点 |

---

## 卡 1（tier A）— `hist_percentile` 纯函数 + `PerfHistTest`

**目标**：stdlib 手写直方图线性插值分位数（≤20 行，无 numpy），供 stats_snapshot P50/P90 使用。

### TDD 步骤

1. 在 `ctyun-stream-fix-proxy.test.py` **文件末尾**（`if __name__ == "__main__":` 之前，实测 :3652 `m = re.match(r"^r-\d+$", x_request_id or "")` 断言块之后）追加 `PerfHistTest` 测试类（完整代码见下）。
2. 跑 `PerfHistTest` → 红（`AttributeError: module ... has no attribute 'hist_percentile'`）。
3. 在 `ctyun-stream-fix-proxy.py` 加 `import bisect` 与 `hist_percentile` 函数。
4. 跑 `PerfHistTest` → 绿。

### Edit 1.1 — 生产：import bisect

file: `ctyun-stream-fix-proxy.py`

old_string:
```
import base64
import collections
```

new_string:
```
import base64
import bisect
import collections
```

### Edit 1.2 — 生产：`hist_percentile` 纯函数（放在 `_KIND_CATEGORY` 之后、`sse_data_line_kind` 之前）

file: `ctyun-stream-fix-proxy.py`

old_string:
```
    ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR,
}


def sse_data_line_kind(line: bytes) -> str:
```

new_string:
```
    ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR,
}


def hist_percentile(buckets, edges, q):
    """直方图线性插值分位数（stdlib 手写，无 numpy；≤20 行）。

    buckets: 桶计数序列，len == len(edges) + 1（末桶为 [edges[-1], +inf)）。
    edges:   升序右开边界（HIST_BUCKETS_MS，8 个）。
    q:       分位数 (0, 1]。
    返回插值毫秒值：桶内均匀分布线性插值；空直方图 → 0.0；
    +inf 末桶命中 → 返回该桶下界 edges[-1]（保守口径，无法对 +inf 插值）。
    """
    total = sum(buckets)
    if total == 0:
        return 0.0
    rank = q * total
    if rank > total:
        rank = total
    cum = 0
    for i, count in enumerate(buckets):
        lo = edges[i - 1] if i > 0 else 0.0
        if i == len(buckets) - 1:          # +inf 末桶
            return float(lo)
        if rank <= cum + count:
            return lo + (rank - cum) / count * (edges[i] - lo)
        cum += count
    return float(edges[-1])  # 数学不可达（rank<=total 且末桶已 return）；防御性兜底


def sse_data_line_kind(line: bytes) -> str:
```

### Edit 1.3 — 测试：`PerfHistTest` 追加到测试文件末尾

file: `ctyun-stream-fix-proxy.test.py`

old_string:
```
        self.assertEqual(status, 502,
                         "死上游必须合成 502")
        m = re.match(r"^r-\d+$", x_request_id or "")
        self.assertIsNotNone(m,
            "502 响应必须带 X-Request-Id，got %r" % x_request_id)


if __name__ == "__main__":
```

new_string:
```
        self.assertEqual(status, 502,
                         "死上游必须合成 502")
        m = re.match(r"^r-\d+$", x_request_id or "")
        self.assertIsNotNone(m,
            "502 响应必须带 X-Request-Id，got %r" % x_request_id)


class PerfHistTest(unittest.TestCase):
    """P2 速度期：hist_percentile 纯函数矩阵（空/单桶/均匀/边界/末桶溢出）。"""

    def test_hist_percentile_empty(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.5), 0.0,
                         "空直方图 P50 必须为 0.0")
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.9), 0.0,
                         "空直方图 P90 必须为 0.0")

    def test_hist_percentile_single_bucket(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[0] = 1                      # 单样本落 [0, 100)
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 50.0,
                               places=6, msg="单样本首桶 P50 = 桶中点 50")
        buckets = [0] * (len(edges) + 1)
        buckets[1] = 1                      # 单样本落 [100, 250)
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 175.0,
                               places=6, msg="单样本第二桶 P50 = 桶中点 175")

    def test_hist_percentile_uniform(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [1] * (len(edges) + 1)    # 9 桶各 1 样本
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 1500.0,
                               places=6, msg="均匀分布 P50（rank=4.5）落在 [1000,2000) 中点")
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.9), 30000.0,
                               places=6, msg="均匀分布 P90（rank=8.1）落末桶 → 下界 30000")

    def test_hist_percentile_exact_edge(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[0] = 1
        buckets[1] = 1                      # rank=1.0 恰落在 100ms 桶界
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 100.0,
                               places=6, msg="rank 恰落桶界时返回边界值")

    def test_hist_percentile_tail_overflow(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[-1] = 5                     # 全部溢出到 [30000, +inf)
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.5), float(edges[-1]),
                         "末桶溢出 P50 返回下界 30000")
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.99), float(edges[-1]),
                         "末桶溢出 P99 返回下界 30000")


if __name__ == "__main__":
```

### 验证命令（卡 1）

```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p2 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py PerfHistTest 2>&1 ; echo "exit=$?"
```
期望：5 用例全绿，exit=0。

---

## 卡 2（tier A）— STATS 内存键 + STALL_THRESHOLD_S + `_open_upstream`/`_relay_sse`/`_relay_buffered` 打点

**目标**：数据采集层——STATS 四内存键、stall 阈值 env seam、connect/headers 分界时刻、SSE 首字节标记/字节/chunk 累计/stall 检测、非流式字节累计。本卡无独立集成断言（数据要经卡 3 的 `_record_request`/snapshot 才能从外部观测），验证命令跑现有全量测试防回归 + 卡 3 的 `TtfbStreamTest` 收口。

### Edit 2.1 — 生产：STALL_THRESHOLD_S 常量

file: `ctyun-stream-fix-proxy.py`

old_string:
```
PROBE_ALERT_DEBOUNCE_S = 300       # v2 P4：同类 probe 告警最小间隔（去抖）
```

new_string:
```
PROBE_ALERT_DEBOUNCE_S = 300       # v2 P4：同类 probe 告警最小间隔（去抖）
STALL_THRESHOLD_S = float(os.environ.get("CTYUN_STALL_THRESHOLD_S", "5.0"))  # v2 P2：SSE 相邻 record 间隔 > 此值计一次 stall（env seam，测试降至 0.5 加速）
```

### Edit 2.2 — 生产：STATS 新增内存态键

file: `ctyun-stream-fix-proxy.py`

old_string:
```
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0, "eof_without_done_total": 0,
         "finish_retries_total": 0, "header_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {}}
```

new_string:
```
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0, "eof_without_done_total": 0,
         "finish_retries_total": 0, "header_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {},
         # v2 P2 速度观测（内存态，不持久化——save/load 白名单均不含此四键，重启清零）
         "ttfb_hist": {},    # {"<model>": [0]*9}，模型 cap 复用 BY_MODEL_CAP（R4：仅按模型，不按阶段）
         "phase_ms": {"connect": [0] * 9, "headers": [0] * 9, "body": [0] * 9},  # 全局三阶段（R4：不按模型）
         "rates": {"bytes_out_total": 0, "chunks_total": 0, "tokens_total": 0,
                   "window_start": time.monotonic(), "window_bytes": 0,
                   "window_chunks": 0, "window_tokens": 0},  # 60s 滑动窗（P2 bytes/chunks；tokens P3 填）
         "stalls_total": 0}
```

### Edit 2.3 — 生产：`_open_upstream` connect/headers 分界

file: `ctyun-stream-fix-proxy.py`

old_string:
```
        conn.request(method, upstream_path, body=body, headers=fwd_headers)
        # getresponse() 成功返回后 conn.sock 会被置 None（socket 移交 HTTPResponse 的
```

new_string:
```
        conn.request(method, upstream_path, body=body, headers=fwd_headers)
        self._t_request_sent = time.monotonic()  # v2 P2：connect+TLS+请求发送完成时刻（phase connect/headers 分界）
        # getresponse() 成功返回后 conn.sock 会被置 None（socket 移交 HTTPResponse 的
```

### Edit 2.4 — 生产：`_relay_sse` 打点状态初始化

file: `ctyun-stream-fix-proxy.py`

old_string:
```
    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
```

new_string:
```
    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        # v2 P2 打点状态：每 record 一次 monotonic + 整数加法，零锁；stall 自增
        # <0.1% record 命中才持 STATS_LOCK（R1 热路径纪律）。重试二次进入时此处重置，
        # 空流尝试的残留值不会漏进交付流。
        self._t_first_byte_mark = None  # 首个非毒 record 交付时刻（TTFB 首字节口径）
        self._relay_bytes = 0
        self._relay_chunks = 0
        self._t_last_record = None      # 相邻 record 间隔基线（stall 检测）
        try:
```

### Edit 2.5 — 生产：`_flush_primed` 首字节标记 + 字节/record 基线

file: `ctyun-stream-fix-proxy.py`

old_string:
```
            def _flush_primed() -> None:
                """补发 SSE 头 + primed 缓冲并 flush，切换出 priming。"""
                self._send_sse_headers(resp)
                self.wfile.write(b"".join(primed))
                self.wfile.flush()
```

new_string:
```
            def _flush_primed() -> None:
                """补发 SSE 头 + primed 缓冲并 flush，切换出 priming。"""
                self._send_sse_headers(resp)
                primed_bytes = b"".join(primed)
                self.wfile.write(primed_bytes)
                self.wfile.flush()
                now = time.monotonic()
                if self._t_first_byte_mark is None:
                    self._t_first_byte_mark = now  # v2 P2：TTFB 首字节口径
                self._t_last_record = now           # v2 P2：stall 间隔基线
                self._relay_bytes += len(primed_bytes)
```

### Edit 2.6 — 生产：priming 分支 chunk 计数

file: `ctyun-stream-fix-proxy.py`

old_string:
```
                    elif priming:
                        primed.extend(pending)
                        if line:
                            primed.append(line)
```

new_string:
```
                    elif priming:
                        primed.extend(pending)
                        if line:
                            primed.append(line)
                        self._relay_chunks += 1  # v2 P2：每非毒 record 计一 chunk（毒 record 已在上面 filtered 分支）
```

### Edit 2.7 — 生产：非 priming 写路径 stall 检测 + 字节/chunk 累计

file: `ctyun-stream-fix-proxy.py`

old_string:
```
                    else:
                        for buf_line in pending:
                            self.wfile.write(buf_line)
                        if line:
                            self.wfile.write(line)
                        self.wfile.flush()
                    pending = []
                    poisoned = False
```

new_string:
```
                    else:
                        now = time.monotonic()
                        if self._t_last_record is not None \
                                and now - self._t_last_record > STALL_THRESHOLD_S:
                            # v2 P2：stall 自增是热循环内唯一持锁点，且仅跨阈值间隙命中
                            # （正常流相邻 record 间隔 << 阈值，<0.1% 命中；R1 纪律）
                            with STATS_LOCK:
                                STATS["stalls_total"] += 1
                        self._t_last_record = now
                        for buf_line in pending:
                            self.wfile.write(buf_line)
                            self._relay_bytes += len(buf_line)
                        if line:
                            self.wfile.write(line)
                            self._relay_bytes += len(line)
                        self.wfile.flush()
                        self._relay_chunks += 1
                    pending = []
                    poisoned = False
```

### Edit 2.8 — 生产：`_relay_buffered` 体读完时刻 + 字节/chunk

file: `ctyun-stream-fix-proxy.py`

old_string:
```
    def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
        data = resp.read()
```

new_string:
```
    def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
        data = resp.read()
        t_body_done = time.monotonic()   # v2 P2：非流式无首字节事件，TTFB 口径 = t_body_done - t_conn_start（spec P2）
        self._t_first_byte_mark = t_body_done
        self._relay_bytes = len(data)
        self._relay_chunks = 1           # 非流式 = 单 chunk
```

### 验证命令（卡 2）

```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p2 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py PoisonStreamTest PassThroughTest BodyErrorTest RequestIdTest 2>&1 | tail -3 ; echo "exit=$?"
```
期望：四个现有测试类全绿（回归防护；本卡改动路径覆盖 SSE 剥行/透传/body-error/rid 全链路），exit=0。

---

## 卡 3（tier A）— `_proxy_relay` 汇总 + `_record_request` histogram/rates + `stats_snapshot` perf 节 + `TtfbStreamTest`

**目标**：把卡 2 采集的原始数据汇总成 TTFB（首字节口径覆盖）、三阶段 ms、bytes/chunks，喂给 `_record_request` 完成 histogram 桶更新与 60s rates 窗口滚动；`stats_snapshot` 暴露 `perf` 节。集成测试 `TtfbStreamTest` 黑盒验证。

### TDD 步骤

1. 追加 `TtfbStreamTest` 测试类（完整代码见 Edit 3.8）与 `FakeUpstreamHandler`/`make_fake_upstream` 的 latency/stall 支撑（Edit 3.6/3.7）。
2. 跑 `TtfbStreamTest` → 红（`/api/stats` 无 `perf` 键 → `KeyError`）。
3. 依次落 Edit 3.1-3.5（生产代码）。
4. 跑 `TtfbStreamTest` → 绿。
5. 全量收尾。

### Edit 3.1 — 生产：`_proxy_relay` 流式分支 TTFB 首字节覆盖 + 阶段 ms 计算

file: `ctyun-stream-fix-proxy.py`

old_string:
```
                self._body_err_line = None
                filtered, truncated = self._relay_sse(resp, final=True)
                body_error = self._body_err_line is not None
                ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
            outcome = classify_outcome(status=resp.status, poison_filtered=filtered,
                                       body_error=body_error)
```

new_string:
```
                self._body_err_line = None
                filtered, truncated = self._relay_sse(resp, final=True)
                body_error = self._body_err_line is not None
                ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
            # v2 P2：首字节口径覆盖 headers 口径（流式 = 首个非毒 record 交付时刻；
            # 空流重试路径 _relay_sse 入口已重置 mark，此处读到的始终是交付流的值）
            if self._t_first_byte_mark is not None:
                ttfb_ms = round((self._t_first_byte_mark - t_conn_start) * 1000, 1)
            t_relay_done = time.monotonic()
            connect_ms = round((self._t_request_sent - t_conn_start) * 1000, 1)
            headers_ms = round((t_headers_done - self._t_request_sent) * 1000, 1)
            body_ms = round((t_relay_done - t_headers_done) * 1000, 1)
            outcome = classify_outcome(status=resp.status, poison_filtered=filtered,
                                       body_error=body_error)
```

### Edit 3.2 — 生产：`_proxy_relay` 流式 `_record_request` 调用追加 bytes/chunks/phase

file: `ctyun-stream-fix-proxy.py`

old_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category)
        else:
            relayed_data = self._relay_buffered(resp)
```

new_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
        else:
            relayed_data = self._relay_buffered(resp)
```

### Edit 3.3 — 生产：`_proxy_relay` 非流式分支 TTFB/阶段计算与 `_record_request` 参数

file: `ctyun-stream-fix-proxy.py`

old_string:
```
        else:
            relayed_data = self._relay_buffered(resp)
            body_error = False
```

new_string:
```
        else:
            relayed_data = self._relay_buffered(resp)
            # v2 P2：非流式 TTFB = t_body_done - t_conn_start（_relay_buffered 已置 mark）
            ttfb_ms = round((self._t_first_byte_mark - t_conn_start) * 1000, 1)
            connect_ms = round((self._t_request_sent - t_conn_start) * 1000, 1)
            headers_ms = round((t_headers_done - self._t_request_sent) * 1000, 1)
            body_ms = round((self._t_first_byte_mark - t_headers_done) * 1000, 1)
            body_error = False
```

file: `ctyun-stream-fix-proxy.py`

old_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category)
```

new_string:
```
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
```

### Edit 3.4 — 生产：`_record_request` 签名 + histogram/rates 更新

file: `ctyun-stream-fix-proxy.py`

old_string:
```
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None) -> None:
    global _stats_dirty
    with STATS_LOCK:
```

new_string:
```
def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None,
                    chunks: int = 0, phase_ms: dict = None) -> None:
    global _stats_dirty
    with STATS_LOCK:
```

file: `ctyun-stream-fix-proxy.py`

old_string:
```
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model,
                                "rid": rid, "upstream_host": upstream_host,
                                "ttfb_ms": ttfb_ms, "stream": stream,
                                "tokens": tokens, "bytes_out": bytes_out,
                                "outcome": outcome})
        _stats_dirty = True
```

new_string:
```
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model,
                                "rid": rid, "upstream_host": upstream_host,
                                "ttfb_ms": ttfb_ms, "stream": stream,
                                "tokens": tokens, "bytes_out": bytes_out,
                                "outcome": outcome})
        # v2 P2：histogram 桶更新与 60s rates 窗口滚动（每请求一次，均在本锁内；R1/R4）
        if ttfb_ms is not None and model:
            ttfb_hist = STATS["ttfb_hist"]
            if model not in ttfb_hist and len(ttfb_hist) < BY_MODEL_CAP:
                ttfb_hist[model] = [0] * 9
            if model in ttfb_hist:
                ttfb_hist[model][bisect.bisect_right(HIST_BUCKETS_MS, ttfb_ms)] += 1
        if phase_ms:
            for name in ("connect", "headers", "body"):
                STATS["phase_ms"][name][
                    bisect.bisect_right(HIST_BUCKETS_MS, phase_ms[name])] += 1
        now_mono = time.monotonic()
        rates = STATS["rates"]
        if now_mono - rates["window_start"] >= 60.0:
            # 60s 滑动窗滚动：仅新请求进入且窗口过期时重置一次（spec P2）
            rates["window_start"] = now_mono
            rates["window_bytes"] = 0
            rates["window_chunks"] = 0
            rates["window_tokens"] = 0
        rates["bytes_out_total"] += bytes_out
        rates["chunks_total"] += chunks
        rates["window_bytes"] += bytes_out
        rates["window_chunks"] += chunks
        if tokens:  # P3 填 tokens；P2 阶段恒 None → 不累计
            rates["tokens_total"] += tokens
            rates["window_tokens"] += tokens
        _stats_dirty = True
```

### Edit 3.5 — 生产：`stats_snapshot` perf 节

file: `ctyun-stream-fix-proxy.py`

old_string:
```
        snap["recent"] = list(RECENT_REQUESTS)
        snap["poison_previews"] = list(POISON_PREVIEWS)
        snap["events"] = [dict(e) for e in EVENTS]  # 逐条浅拷贝（对齐 daily 模式），oldest→newest
    snap["uptime_s"] = int(time.time() - STARTED_AT)
```

new_string:
```
        snap["recent"] = list(RECENT_REQUESTS)
        snap["poison_previews"] = list(POISON_PREVIEWS)
        snap["events"] = [dict(e) for e in EVENTS]  # 逐条浅拷贝（对齐 daily 模式），oldest→newest
        ttfb_hist = {m: list(h) for m, h in STATS["ttfb_hist"].items()}
        phase_hist = {k: list(v) for k, v in STATS["phase_ms"].items()}
        rates = dict(STATS["rates"])
        stalls_total = STATS["stalls_total"]
        recent_len = len(RECENT_REQUESTS)
        recent_stream = sum(1 for r in RECENT_REQUESTS if r.get("stream"))
    snap["uptime_s"] = int(time.time() - STARTED_AT)
```

file: `ctyun-stream-fix-proxy.py`

old_string:
```
    plan = range_stats(snap["daily"])  # 锁外基于副本计算（4×≤31 桶求和 <1ms），不拉长持锁
    snap["range_stats"] = plan["stats"]
    snap["range_bounds"] = plan["bounds"]
    return snap
```

new_string:
```
    plan = range_stats(snap["daily"])  # 锁外基于副本计算（4×≤31 桶求和 <1ms），不拉长持锁
    snap["range_stats"] = plan["stats"]
    snap["range_bounds"] = plan["bounds"]
    # v2 P2：perf 节（锁外基于浅拷贝计算；≤32 模型 × 9 桶 + 3 阶段 × 9 桶 <1ms）
    perf = {"ttfb_p50_ms_by_model": {}, "ttfb_p90_ms_by_model": {},
            "phase_p50_ms": {}, "bytes_per_s": 0.0, "chunks_per_s": 0.0,
            "tokens_per_s": 0.0, "stalls_total": stalls_total,
            "stream_share": 0.0}
    for model, hist in ttfb_hist.items():
        perf["ttfb_p50_ms_by_model"][model] = round(
            hist_percentile(hist, HIST_BUCKETS_MS, 0.5), 1)
        perf["ttfb_p90_ms_by_model"][model] = round(
            hist_percentile(hist, HIST_BUCKETS_MS, 0.9), 1)
    for name in ("connect", "headers", "body"):
        perf["phase_p50_ms"][name] = round(
            hist_percentile(phase_hist[name], HIST_BUCKETS_MS, 0.5), 1)
    elapsed = time.monotonic() - rates["window_start"]
    if elapsed < 60.0 and elapsed > 0:
        # 窗口过期（≥60s 无请求）时速率报 0：无近期流量（Anchor Reconciliation）
        perf["bytes_per_s"] = round(rates["window_bytes"] / elapsed, 1)
        perf["chunks_per_s"] = round(rates["window_chunks"] / elapsed, 1)
        perf["tokens_per_s"] = round(rates["window_tokens"] / elapsed, 1)
    if recent_len:
        perf["stream_share"] = round(recent_stream / recent_len, 4)
    snap["perf"] = perf
    return snap
```

### Edit 3.6 — 测试：`FakeUpstreamHandler` 类属性

file: `ctyun-stream-fix-proxy.test.py`

old_string:
```
    rst_all = False         # True → 每呼读 body 后 SO_LINGER(1,0) close 强制发 RST（代理 getresponse 抛 ConnectionResetError）
```

new_string:
```
    latency_s = 0.0            # >0 → 正常路径写响应前 sleep（TTFB 测试模拟上游慢）
    stall_mid_stream_s = 0.0   # >0 → 写 SSE_A 后 sleep 再写 SSE_B+DONE（stall 检测测试）
    rst_all = False         # True → 每呼读 body 后 SO_LINGER(1,0) close 强制发 RST（代理 getresponse 抛 ConnectionResetError）
```

### Edit 3.7 — 测试：`FakeUpstreamHandler.do_POST` stall-mid-stream 分支与 latency sleep；`make_fake_upstream` 透传

file: `ctyun-stream-fix-proxy.test.py`

old_string:
```
            self.close_connection = True
            return
        if self.big:
            self._respond_big_sse()
            return
```

new_string:
```
            self.close_connection = True
            return
        if self.stall_mid_stream_s:
            # SSE_A 后 sleep 再续 SSE_B+DONE：相邻 record 间隔 = sleep 时长（stall 检测用）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(SSE_A)
                self.wfile.flush()
                time.sleep(self.stall_mid_stream_s)
                self.wfile.write(SSE_B + SSE_DONE)
                self.wfile.flush()
            except ConnectionError:
                # 吞掉的是代理侧已放弃读取后的写出端 Broken pipe（预期路径，同下方正常路径注释）
                pass
            self.close_connection = True
            return
        if self.latency_s:
            time.sleep(self.latency_s)
        if self.big:
            self._respond_big_sse()
            return
```

file: `ctyun-stream-fix-proxy.test.py`

old_string:
```
                       sleep_stall_all: bool = False, sleep_stall_calls: tuple = (),
                       sleep_stall_seconds: float = 3.0,
                       rst_all: bool = False, rst_calls: tuple = (),
                       fail_200_error: bool = False) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
```

new_string:
```
                       sleep_stall_all: bool = False, sleep_stall_calls: tuple = (),
                       sleep_stall_seconds: float = 3.0,
                       rst_all: bool = False, rst_calls: tuple = (),
                       fail_200_error: bool = False,
                       latency_s: float = 0.0,
                       stall_mid_stream_s: float = 0.0) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
```

file: `ctyun-stream-fix-proxy.test.py`

old_string:
```
             "rst_all": rst_all,
             "rst_calls": rst_calls,
             "fail_200_error": fail_200_error,
             "bodies": [] if record_bodies else None}
```

new_string:
```
             "rst_all": rst_all,
             "rst_calls": rst_calls,
             "fail_200_error": fail_200_error,
             "latency_s": latency_s,
             "stall_mid_stream_s": stall_mid_stream_s,
             "bodies": [] if record_bodies else None}
```

### Edit 3.8 — 测试：`TtfbStreamTest` 追加到测试文件末尾（`PerfHistTest` 之后）

file: `ctyun-stream-fix-proxy.test.py`

old_string:
```
    def test_hist_percentile_tail_overflow(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[-1] = 5                     # 全部溢出到 [30000, +inf)
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.5), float(edges[-1]),
                         "末桶溢出 P50 返回下界 30000")
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.99), float(edges[-1]),
                         "末桶溢出 P99 返回下界 30000")


if __name__ == "__main__":
```

new_string:
```
    def test_hist_percentile_tail_overflow(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[-1] = 5                     # 全部溢出到 [30000, +inf)
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.5), float(edges[-1]),
                         "末桶溢出 P50 返回下界 30000")
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.99), float(edges[-1]),
                         "末桶溢出 P99 返回下界 30000")


class TtfbStreamTest(unittest.TestCase):
    """P2 速度期：TTFB 首字节口径 + histogram + 吞吐 + stall 检测。

    黑盒子进程集成：fake upstream sleep 100ms 后吐流 → ttfb_ms ∈ [80, 500] 且
    perf 节按模型 P50/P90 与三阶段 P50 均被填充、bytes/chunks 速率 > 0；
    stall 检测用 env seam CTYUN_STALL_THRESHOLD_S=0.5 加速（上游中途 sleep 1s）。"""

    MODEL = "deepseek-v4-pro-0813-oc"

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False, latency_s=0.1)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_ttfb_ms_in_range_and_perf_populated(self) -> None:
        """fake upstream sleep 100ms → ttfb_ms ∈ [80, 500]；perf 节非空且速率 > 0。"""
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertIn("perf", snap, "/api/stats 必须含 perf 节")
        perf = snap["perf"]
        entry = snap["recent"][-1]
        self.assertGreaterEqual(entry["ttfb_ms"], 80,
                                "ttfb 必须 ≥ 80ms（上游 sleep 100ms），got %r" % entry["ttfb_ms"])
        self.assertLessEqual(entry["ttfb_ms"], 500,
                             "ttfb 必须 ≤ 500ms（本地回环 100ms sleep），got %r" % entry["ttfb_ms"])
        self.assertGreater(entry["bytes_out"], 0,
                           "流式 bytes_out 必须 > 0")
        self.assertIn(self.MODEL, perf["ttfb_p50_ms_by_model"],
                      "ttfb P50 必须按模型出现；got %r"
                      % sorted(perf["ttfb_p50_ms_by_model"].keys()))
        self.assertGreater(perf["ttfb_p50_ms_by_model"][self.MODEL], 0)
        self.assertGreater(perf["ttfb_p90_ms_by_model"][self.MODEL], 0)
        for name in ("connect", "headers", "body"):
            self.assertIn(name, perf["phase_p50_ms"],
                          "phase_p50_ms 必须含 '%s'；got %r"
                          % (name, sorted(perf["phase_p50_ms"].keys())))
            self.assertGreater(perf["phase_p50_ms"][name], 0,
                               "phase '%s' P50 必须 > 0" % name)
        self.assertGreater(perf["bytes_per_s"], 0,
                           "60s 窗口 bytes/s 必须 > 0")
        self.assertGreater(perf["chunks_per_s"], 0,
                           "60s 窗口 chunks/s 必须 > 0")
        self.assertEqual(perf["stalls_total"], 0,
                         "无 stall 的流 stalls_total 必须为 0")
        self.assertEqual(perf["stream_share"], 1.0,
                         "单条流式请求 stream_share 必须为 1.0")

    def test_stall_detected_via_env_seam(self) -> None:
        """上游中途 sleep 1s（> 0.5s 阈值）→ stalls_total ≥ 1。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        stop_fake_upstreams()
        self.upstream_port = make_fake_upstream(False, stall_mid_stream_s=1.0)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_STALL_THRESHOLD_S": "0.5"})
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        perf = snap["perf"]
        self.assertGreaterEqual(perf["stalls_total"], 1,
                                "中途 1s 间隙（阈值 0.5s）必须计 ≥1 次 stall；perf=%r" % perf)


if __name__ == "__main__":
```

### 验证命令（卡 3）

单类：
```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p2 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py PerfHistTest TtfbStreamTest 2>&1 | tail -3 ; echo "exit=$?"
```
全量收尾：
```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p2 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -3 ; echo "exit=$?"
```
期望：`Ran 139 tests ... OK`（133 baseline + 5 PerfHistTest + 2 TtfbStreamTest − 已有 1 个重名风险 0 = 140；实际以运行输出为准，卡 3 报告贴实际数字），exit=0。

---

## 实测锚点表（PLAN-B 写盘时 grep 实测，P1 合并后）

```
$ grep -nE '^(import base64|STATS = |def hist_percentile|def _record_request|def stats_snapshot|    def _proxy_relay|    def _open_upstream|    def _relay_sse|    def _relay_buffered)' ctyun-stream-fix-proxy.py
12:import base64
331:STATS = {"requests_total": 0, ...
706:def _record_request(method: str, path: str, ...
847:def stats_snapshot() -> dict:
921:    def _proxy_relay(self, started: float) -> None:
1094:    def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):
1123:    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
1210:    def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
```

```
$ grep -nE '^(class PerfHistTest|class TtfbStreamTest|class RequestIdTest|def load_proxy_module|def make_fake_upstream|def start_proxy)' ctyun-stream-fix-proxy.test.py
220:def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
383:def start_proxy(upstream_port: int, proxy_port: int, extra_env: dict = None,
530:def load_proxy_module():
3548:class RequestIdTest(unittest.TestCase):
```
（`PerfHistTest`/`TtfbStreamTest` 本计划新增，追加于测试文件末尾 `if __name__` 之前；`RequestIdTest` 尾部锚点为 `"502 响应必须带 X-Request-Id，got %r" % x_request_id` 断言块。）

## 卡间依赖与串行序

卡 1 → 卡 2 → 卡 3 严格串行：
- 卡 2 的 Edit 2.2 依赖卡 1 的 `import bisect`？**否**——Edit 2.x 均不引用 `bisect`/`hist_percentile`（卡 3 才引用），但按序执行避免 import 缺失期。
- 卡 3 的 Edit 3.4 引用 `bisect`（卡 1）与 `HIST_BUCKETS_MS`（P1 已交付），Edit 3.1-3.3 引用 `self._t_request_sent`（卡 2 Edit 2.3）、`self._t_first_byte_mark`/`_relay_bytes`/`_relay_chunks`（卡 2 Edit 2.4-2.8）。
- 跨卡 Edit 顺序模拟已核：卡 2 与卡 3 的 old_string 不重叠（卡 2 改 `_relay_sse`/`_relay_buffered`/`_open_upstream` 内部；卡 3 改 `_proxy_relay`/`_record_request`/`stats_snapshot`），顺序执行无冲突；卡 3 内部 Edit 3.1-3.5 的 old_string 互不重叠且按文件自上而下顺序排列。
- `_proxy` 的 ConnectionError 异常路径（:913-916）与两处 synth-502 `_record_request` 调用（:973、:1013）不传 chunks/phase_ms → 依赖默认值 `chunks=0, phase_ms=None`，histogram/rates 对失败路径自然跳过（`ttfb_ms=None`/`phase_ms=None`），符合 spec"失败零副作用"精神（P2 只观测成功交付）。

## Self-Review 结论（PLAN-B 自查 5 项）

1. **spec coverage**：spec P2 节 5 个锚点（STATS 四键 / `_relay_sse` 打点约束 / `_relay_buffered` / `_proxy_relay` 汇总 / `stats_snapshot` perf 节）全部落卡；R1（热路径无锁纪律）与 R4（phase 不按模型）在 Global Constraints + 代码注释双重重申。P2 测试节两类（`PerfHistTest`/`TtfbStreamTest`）全覆盖。
2. **placeholder scan**：全卡生产/测试代码块为完整 Edit old/new_string 与完整类/方法体，无 TODO/待补/省略号占位。
3. **type consistency**：`ttfb_ms` 全链路 float（round 1 位）；`bytes_out`/`chunks` int；`phase_ms` dict 三键 connect/headers/body；`hist_percentile(buckets, edges, q)` 签名与调用点（snapshot :3 处）一致；`_record_request` 新参数默认值保持所有旧调用点（7 处）兼容。
4. **可落盘性（含跨卡 Edit 模拟）**：每卡 old_string 均为实测文件原文；卡 2/卡 3 Edit 互不重叠；`stats_snapshot` 两处 Edit（锁内浅拷贝 + 锁外 perf 计算）old_string 互异（一处带 `snap["uptime_s"]`、一处带 `plan = range_stats`）；测试文件 3 处追加锚（PerfHistTest 尾部、TtfbStreamTest 尾部、`if __name__` 前）均为唯一匹配。`import bisect` 置于字母序正确位。
5. **锚点实测**：上方锚点表为写盘时 grep 实测值；baseline 实跑 `Ran 133 tests ... OK`。
