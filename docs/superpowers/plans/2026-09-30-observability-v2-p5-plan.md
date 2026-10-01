# observability v2 P5 — dashboard 重构 PLAN（最终期）

- **spec（唯一权威源）**：`docs/superpowers/specs/2026-09-30-ctyun-proxy-observability-v2-design.md`（P5 节 :75-87 + 测试 :96 + Risks R5 :116 + Acceptance :107）
- **作业目录（worktree）**：`/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p5`，分支 `fix/observability-v2-p5`，base `main@bd2554f`（P1-P4 已合入）
- **卡数**：4（Card 1/2/3/4 全串行；tier：A / B / B / B）
- **改动文件**：`ctyun-stream-fix-proxy.py`（`_DASHBOARD_SRC` 区域拆段 + V2 内容）、`ctyun-stream-fix-proxy.test.py`（新增 `DashboardV2SkeletonTest` 等）、`README.md`（计数口径/功能清单补行，Card 4）
- **baseline**：`main@bd2554f` 实测 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` **219 全绿**（117.078s）；卡内验证预期 220 → 223 → 224 → 224

## Global Constraints

1. **等价性策略（Card 1 专责）**：拆分 6 段常量 + join 后 `DASHBOARD_HTML` 必须与重构前**逐字节等价**。验证手段：新增 `DashboardRefactorEquivalenceTest`，把重构前 HTML 的 **sha256 快照**（`2e4dfdb4f602fddb1a7f9484b2c14eb73a97bc6ef1d618874d9acb9276c9539c`，本 PLAN 实测自 bd2554f，24466 字节）写死进断言 + join 等式断言 + 6 常量存在断言。该测试是**一次性守门测试**：Card 2 起 HTML 有意变化（加 V2 卡片），此测试退役，旧内容不退化改由 `test_dashboard_html_full_page`（既有集成测试）+ `DashboardV2SkeletonTest.test_old_content_intact` 守护。
2. **防 XSS 铁律**：新卡片渲染一律 `el(tag, cls, text)`（textContent）；`innerHTML` 整页 0 出现（正则 `r"\.innerHTML\s*="` 在 6 段常量中 0 命中）；数值走 `style.width`/`setAttribute` 设置（仅数字与常量颜色，无 markup 注入面）。
3. **join 顺序 = 页面顺序（R5）**：`DASHBOARD_HTML = ("".join([_DASH_HEAD, _DASH_SECTIONS_STATIC, _DASH_SECTIONS_TABLES, _DASH_SECTIONS_V2, _DASH_JS_CORE, _DASH_JS_V2, "</script>\n</body>\n</html>\n"])).encode("utf-8")`。漏段/错序 → 白屏但后端 200；`DashboardV2SkeletonTest.test_segment_constants_present_and_join` 用 join 等式兜底。
4. **单测命令纪律**：一律 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py <TestClass>[.<method> ...]`（支持多参数，实测可用）；**禁用 `-m unittest`**（README:118 明示 Homebrew 3.14 挂死）。exit code 取命令真值，测试输出贴 stdout。
5. **TDD**：每卡先写失败测试（贴 RED stdout）→ 实现 → GREEN。Card 4 为纯文档卡，豁免 TDD（AGENTS.md 例外清单：Documentation）。
6. **零新依赖、单文件部署不变**：不拆静态文件（spec 已拒绝方案 b），CSS/JS 全内嵌。
7. **范围**：只动 P5 涉及的文件；不改 `stats_snapshot`（P2 契约）、不改 `/api/health`（P4 契约）、不改转发语义；README 仅补 Card 4 两处缺口，无新 API。
8. **executor 纪律**：所有 Edit 为精确 old_string/new_string 转写（old_string 均实测自 bd2554f 文件原文）；遇到锚点不符 → STOP 上报主代理，不得边改边设计。

## Anchor Reconciliation（spec ↔ 代码现状实测）

| spec 锚点 | 实测结果 | 裁决 |
|---|---|---|
| `ctyun-stream-fix-proxy.py:1332-1886 _DASHBOARD_SRC` | P1-P4 合入后整体下移：`_DASHBOARD_SRC` 现居 **:2307-2862**，`DASHBOARD_HTML` 在 :2862，admin serve 点 :2137 | 按实测行号作业；拆段边界实测为字符串行索引 0-82 / 83-110 / 111-141 / 142-482 / 483-550 / 551-553（见下卡） |
| spec 拆 6 段 + join 尾部 `"</script></body></html>"` | 现有 HTML 尾部是 `</script>\n</body>\n</html>\n`（含换行与结尾换行）。照抄 spec 字面量会丢换行 → **不逐字节等价** | 尾部字面量改为 `"</script>\n</body>\n</html>\n"`（唯一偏离 spec 字面量处；字节等价优先，页面 HTML 与重构前完全相同） |
| spec 6 段清单未覆盖 `</main>`/footer/evt-tip/`<script>` 归属 | 这些页面闭合元素必须在 V2 卡片之后、JS 之前 | 归入 `_DASH_JS_CORE` **前导胶水**（常量注释说明）；`_DASH_SECTIONS_V2` 卡片落在 `</main>` 之前的主网格内，join 顺序保持 spec 字面量 |
| `_DASH_SECTIONS_V2` 内容 | 重构期须为空串才能逐字节等价 | Card 1 `_DASH_SECTIONS_V2 = ""`；Card 2 填 5 个新卡片区块 |
| `/api/health` 合并进 lastSnap 还是独立 fetch | spec 留白"你裁决" | **独立并行 fetch（`pollHealth()`）**：`/api/stats` 快照在 STATS_LOCK 下产出、`/api/health` 在 PROBE_LOCK 下产出，合并会迫使改 `stats_snapshot` 契约（违背 P2/P4 已定接口）；独立 fetch 在 `poll()` 体内同步发起、与 stats 请求并发在途，健康卡失败只降级显示"读取中"，**不触发 setConn**（连通性信号仍以 /api/stats 为准）；2s 轮询复用（pollHealth 由 poll 每 2s 携带触发） |
| probe_alert 进 events 过滤与标签 | 现有 `evtFilter` 的 kind 匹配是通用等式分支（非白名单），`probe_alert` 天然可过滤；但 EVT_KIND_LABELS 无标签、全页无 probe_alert 附着点 → 告警不可见 | 三处接线：① EVT_KIND_LABELS 加 `probe_alert: "上游探测告警"`；② `showEvtTip` 明细行加 `reason` 展示；③ `renderHealth` 对健康卡 chip `markEvents(chip, "probe_alert", null, null)`（hover 出 tooltip）。"errors" 桶**刻意不含** probe_alert——顶部错误数口径 = errors_proxy + errors_upstream（footer 明示），不因 P5 变动 |
| range tabs 复用 | 现有 4 键（3d/7d/mtd/last_month）+ `applyRange()` 零请求重渲染模式 | `applyRange()` 追加 token/tri-state 标题标签更新 + `renderTokens(lastSnap)`/`renderTriState(lastSnap)`；`renderPerf` 为进程内累计不随 range 变，只在 poll 时刷 |
| main 侧 TPM 对 dashboard 的改动 | `_DASHBOARD_SRC` 区域内 grep `tpm\|TPM` **0 命中**（exit 1）；`/api/tpm_stats` 仅后端存在，无前端 | 无冲突；保持 `/api/tpm_stats` 不动 |
| `stats_snapshot` perf 节 / daily_by_model 16 字段 | 实测 :1564-1584 perf 节（ttfb_p50/90_ms_by_model、phase_p50_ms、bytes/chunks/tokens_per_s、stalls_total、stream_share）；`_DAILY_FIELDS` 16 字段 :1078-1093 | JS 直接消费，零后端改动 |
| `/api/health` 形状 | 实测 :2150-2151 返回 `{"upstream": probe_state_snapshot()}`，:1288-1302 定死 6 键（host/last_probe_ts/last_probe_ok/last_probe_latency_ms/consecutive_failures/probe_enabled） | `renderHealth` 按 6 键消费 |
| spec 测试 `DashboardV2SkeletonTest` | 现有 `test_dashboard_tooltip_skeleton` 在 `ProxyDashboardUnitTest`（test.py:1912）；集成 `test_dashboard_html_full_page` :2634；`load_proxy_module` :595；test.py 已 import `hashlib`(:15)/`re`(:21)；runner `unittest.main(verbosity=2)` :5609（多测试名参数实测可用） | 新测试类置于 `AdminIntegrationTest`（:2530）之前 |
| README spec:97「计数口径表追加三态可用率/模型速度/token 用量三行」 | **实测缺口**：README.md:90-98 计数口径表 5 行，无 P5 新卡片行；功能清单 :16-19 亦无新卡片 bullet | 追加 Card 4（docs）补齐（含上游健康行与 hover 明细事件集扩充 probe 告警） |

## 实测锚点表（bd2554f worktree 实测）

| 位置 | 内容 | 用途 |
|---|---|---|
| ctyun-stream-fix-proxy.py:2307 | `_DASHBOARD_SRC = """<!doctype html>` | Card 1 Edit 1.1 锚点（改名 `_DASH_HEAD`） |
| :2304-2306 | 防 XSS 注释块 | Card 1 Edit 1.1 注释扩展 |
| :2389 / :2390 | `</div></header>` / `<main>` | HEAD↔STATIC 边界（Edit 1.2） |
| :2417 / :2418-2419 | sparkline `</section>` / 按天表开头 | STATIC↔TABLES 边界（Edit 1.3，含 daily-title-range 行保唯一） |
| :2448 / :2449 | recent `</section>` / `</main>` | TABLES↔V2↔JS_CORE 边界（Edit 1.4；V2 卡片插入点） |
| :2460-2461 | `<div class="evt-tip"...>` / `<script>` | JS_CORE 胶水（含 `<script>` 开标签） |
| :2502 | `var EVT_KIND_LABELS = { proxy: ..., upstream: ..., retry: ... };` | Card 3 Edit 3.1 锚点 |
| :2547-2548 | showEvtTip 的 model/status 拼接两行 | Card 3 Edit 3.2 锚点（加 reason） |
| :2733-2743 | `function applyRange()` 全文 | Card 3 Edit 3.3 锚点 |
| :2789 / :2790-2791 | `}`（bar 结束）/ `function poll() {` | JS_CORE↔JS_V2 边界（Edit 1.5）；Card 3 Edit 3.4 新函数插入点 |
| :2806-2807 | `renderSpark(snap.recent || []);` / `setConn(true);` | Card 3 Edit 3.5 锚点 |
| :2812-2814 | poll catch 尾部 `});` + `}` | Card 3 Edit 3.6 锚点（pollHealth 调用） |
| :2855-2862 | `applyRange(); poll(); setInterval(...); </script>...</html>""" DASHBOARD_HTML = ...` | Card 1 Edit 1.6 锚点（尾部字面量 + join） |
| :2137 | `self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML)` | 无改动，服务点不变 |
| :1541-1585 | `stats_snapshot()` perf 节 | 只读消费，无改动 |
| :1288-1302 / :2150-2151 | `probe_state_snapshot()` 6 键 / `/api/health` handler | 只读消费，无改动 |
| test.py:15/21 | `import hashlib` / `import re` | 新测试直接可用，无需加 import |
| test.py:595 | `def load_proxy_module()` | 新测试类 setUp 复用 |
| test.py:1912 | `ProxyDashboardUnitTest.test_dashboard_tooltip_skeleton` | Card 1 回归锚 |
| test.py:2530 / :2634 / :2687-2689 | `AdminIntegrationTest` / `test_dashboard_html_full_page` / 尾部三断言 | 新测试类插入点 / Card 3 扩展锚 |
| README.md:16-19 / :90-98 | 功能清单 dashboard bullets / 计数口径表 | Card 4 锚 |
| sha256 实测 | `2e4dfdb4f602fddb1a7f9484b2c14eb73a97bc6ef1d618874d9acb9276c9539c`（24466 bytes，553 内容行） | Card 1 守门断言 |
| baseline | `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → **Ran 219 tests, OK**（117.078s） | 各卡回归基线 |

---

## Card 1（tier A）拆分 `_DASHBOARD_SRC` 为 6 段常量，逐字节等价

**目标**：现有 dashboard 拆成 `_DASH_HEAD` / `_DASH_SECTIONS_STATIC` / `_DASH_SECTIONS_TABLES` / `_DASH_SECTIONS_V2`(=空串) / `_DASH_JS_CORE` / `_DASH_JS_V2` + 尾部字面量 join；`DASHBOARD_HTML` 输出与重构前**逐字节相同**（页面零变化）。

### 步骤（TDD）

1. **测试先行**（`ctyun-stream-fix-proxy.test.py`）：在 `class AdminIntegrationTest(unittest.TestCase):`（含其后 docstring 行）之前插入新测试类：

```python
class DashboardRefactorEquivalenceTest(unittest.TestCase):
    """P5 Card 1 一次性守门：拆分前/后 DASHBOARD_HTML 逐字节等价（sha256 快照）。

    Card 2 起 HTML 有意变化（V2 卡片），本类退役——旧内容不退化改由
    DashboardV2SkeletonTest.test_old_content_intact + 既有 test_dashboard_html_full_page 守护。
    """

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_split_join_byte_equivalent(self) -> None:
        for name in ("_DASH_HEAD", "_DASH_SECTIONS_STATIC", "_DASH_SECTIONS_TABLES",
                     "_DASH_SECTIONS_V2", "_DASH_JS_CORE", "_DASH_JS_V2"):
            self.assertTrue(hasattr(self.mod, name), "拆分常量 %s 缺失" % name)
        self.assertEqual(self.mod._DASH_SECTIONS_V2, "",
                         "Card 1 阶段 _DASH_SECTIONS_V2 必须为空串（纯重构不加内容）")
        joined = ("".join([
            self.mod._DASH_HEAD, self.mod._DASH_SECTIONS_STATIC,
            self.mod._DASH_SECTIONS_TABLES, self.mod._DASH_SECTIONS_V2,
            self.mod._DASH_JS_CORE, self.mod._DASH_JS_V2,
            "</script>\n</body>\n</html>\n",
        ])).encode("utf-8")
        self.assertEqual(joined, self.mod.DASHBOARD_HTML,
                         "DASHBOARD_HTML 必须等于 6 段常量按序 join + 尾部字面量")
        # 重构前 HTML 快照 sha256（main@bd2554f 实测，24466 字节）
        self.assertEqual(
            hashlib.sha256(self.mod.DASHBOARD_HTML).hexdigest(),
            "2e4dfdb4f602fddb1a7f9484b2c14eb73a97bc6ef1d618874d9acb9276c9539c",
            "拆分后 HTML 与重构前快照逐字节不一致")
```

   验证 RED（此时 6 常量不存在）：
   `/usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardRefactorEquivalenceTest` → 预期 `FAILED (errors=1)`（hasattr 断言失败），exit code 1。

2. **实现**（`ctyun-stream-fix-proxy.py`，按序 6 个 Edit；old_string 均实测自 bd2554f 原文）：

   **Edit 1.1**（注释扩展 + 常量改名）：
   old:
```
# dashboard（单文件零外部依赖，局域网离线可用）：str 常量而非 f-string，CSS/JS 大括号
# 零冲突；bytes 字面量放不下中文，故 str + 一次 encode。动态数据一律 textContent，
# 禁 innerHTML（毒行预览来自上游原始字节，防注入）。
_DASHBOARD_SRC = """<!doctype html>
```
   new:
```
# dashboard（单文件零外部依赖，局域网离线可用）：str 常量而非 f-string，CSS/JS 大括号
# 零冲突；bytes 字面量放不下中文，故 str + 一次 encode。动态数据一律 textContent，
# 禁 innerHTML（毒行预览来自上游原始字节，防注入）。
# P5：按页面顺序拆 6 段常量 + 尾部字面量 join 成 DASHBOARD_HTML。join 顺序 = 页面
# 实际顺序，漏段/错序 → 白屏但后端 200（R5），DashboardV2SkeletonTest 用 join 等式
# 与容器/函数名断言兜底。_DASH_JS_CORE 前导 </main>/footer/evt-tip/<script> 胶水，
# 使 _DASH_SECTIONS_V2 的卡片落在 </main> 之前的主网格内；_DASH_SECTIONS_V2 初始空串。
_DASH_HEAD = """<!doctype html>
```

   **Edit 1.2**（HEAD↔STATIC 边界）：
   old:
```
</div></header>
<main>
```
   new:
```
</div></header>
"""

_DASH_SECTIONS_STATIC = """<main>
```

   **Edit 1.3**（STATIC↔TABLES 边界；daily-title-range 行保证 old 唯一）：
   old:
```
  </section>
  <section class="card">
    <div class="card-title">按天统计（<span id="daily-title-range">近7天</span>，新在上）</div>
```
   new:
```
  </section>
"""

_DASH_SECTIONS_TABLES = """  <section class="card">
    <div class="card-title">按天统计（<span id="daily-title-range">近7天</span>，新在上）</div>
```

   **Edit 1.4**（TABLES↔V2↔JS_CORE 边界）：
   old:
```
  </section>
</main>
```
   new:
```
  </section>
"""

_DASH_SECTIONS_V2 = ""

_DASH_JS_CORE = """</main>
```

   **Edit 1.5**（JS_CORE↔JS_V2 边界）：
   old:
```
}
function poll() {
```
   new:
```
}
"""

_DASH_JS_V2 = """function poll() {
```

   **Edit 1.6**（尾部字面量 + join 常量）：
   old:
```
setInterval(poll, 2000);
</script>
</body>
</html>
"""
DASHBOARD_HTML = _DASHBOARD_SRC.encode("utf-8")
```
   new:
```
setInterval(poll, 2000);
"""

DASHBOARD_HTML = ("".join([
    _DASH_HEAD, _DASH_SECTIONS_STATIC, _DASH_SECTIONS_TABLES,
    _DASH_SECTIONS_V2, _DASH_JS_CORE, _DASH_JS_V2,
    "</script>\n</body>\n</html>\n",
])).encode("utf-8")
```

3. **验证**（逐条贴 stdout/exit code）：
   - `/usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardRefactorEquivalenceTest` → `Ran 1 test, OK`
   - `/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_dashboard_tooltip_skeleton AdminIntegrationTest.test_dashboard_html_full_page` → `Ran 2 tests, OK`
   - 全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → **`Ran 220 tests, OK`**（219 + 1 新增；拆分无回归）

**验收**：sha256 断言绿；既有 dashboard 两测试绿；全量 220 绿。

---

## Card 2（tier B）`_DASH_SECTIONS_V2` 新卡片 HTML + 骨架测试

**目标**：5 个新卡片区块落入主网格（tables 之后、`</main>` 之前）：上游健康 / 模型速度对比（P50/P90）/ 延迟分布 SVG / Token 用量按天×模型 / 三态可用率。容器 id：`upstream-health` / `perf-model-body` / `latency-dist` / `token-daily-body` / `tri-state-card`。同时退役 Card 1 守门测试。

### 步骤（TDD）

1. **测试先行**（`ctyun-stream-fix-proxy.test.py`）：用 `DashboardV2SkeletonTest` **整体替换** Card 1 的 `DashboardRefactorEquivalenceTest` 类（old_string = Card 1 类全文，new_string = 下类全文）：

```python
class DashboardV2SkeletonTest(unittest.TestCase):
    """P5：V2 卡片骨架 + 拆分常量契约（模块级常量断言，无需子进程）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_segment_constants_present_and_join(self) -> None:
        for name in ("_DASH_HEAD", "_DASH_SECTIONS_STATIC", "_DASH_SECTIONS_TABLES",
                     "_DASH_SECTIONS_V2", "_DASH_JS_CORE", "_DASH_JS_V2"):
            self.assertTrue(hasattr(self.mod, name), "拆分常量 %s 缺失" % name)
        self.assertTrue(self.mod._DASH_SECTIONS_V2,
                        "_DASH_SECTIONS_V2 不得为空（V2 卡片应已填充）")
        joined = ("".join([
            self.mod._DASH_HEAD, self.mod._DASH_SECTIONS_STATIC,
            self.mod._DASH_SECTIONS_TABLES, self.mod._DASH_SECTIONS_V2,
            self.mod._DASH_JS_CORE, self.mod._DASH_JS_V2,
            "</script>\n</body>\n</html>\n",
        ])).encode("utf-8")
        self.assertEqual(joined, self.mod.DASHBOARD_HTML,
                         "DASHBOARD_HTML 必须等于 6 段常量按序 join + 尾部字面量")

    def test_v2_containers_present(self) -> None:
        v2 = self.mod._DASH_SECTIONS_V2
        for cid in ("perf-model-body", "token-daily-body", "upstream-health",
                    "tri-state-card", "latency-dist"):
            self.assertIn('id="%s"' % cid, v2, "V2 区块缺容器 %s" % cid)

    def test_old_content_intact(self) -> None:
        # R5 兜底：旧区块不因拆段/加卡退化（模块级快断言；子进程集成测试仍独立守护）
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        for marker in ("保存上游端点", "剥行流带", 'id="daily-body"',
                       'id="daily-model-body"', "renderStats", "renderDailyByModel",
                       'data-range="last_month"', "spark"):
            self.assertIn(marker, html, "旧内容退化，缺 %s" % marker)

    def test_no_inner_html_anywhere(self) -> None:
        for name in ("_DASH_HEAD", "_DASH_SECTIONS_STATIC", "_DASH_SECTIONS_TABLES",
                     "_DASH_SECTIONS_V2", "_DASH_JS_CORE", "_DASH_JS_V2"):
            self.assertIsNone(re.search(r"\.innerHTML\s*=", getattr(self.mod, name)),
                              "%s 含 innerHTML 赋值（动态数据必须 textContent）" % name)
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertEqual(html.count("innerHTML"), 0, "整页必须 0 个 innerHTML 出现")
        self.assertGreaterEqual(html.count("textContent"), 1,
                                "textContent 出现次数必须 ≥ innerHTML（此处 innerHTML=0）")
```

   验证 RED（此时 `_DASH_SECTIONS_V2 == ""`）：
   `/usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardV2SkeletonTest` → 预期 `FAILED (failures=2)`（join 与 v2_containers 两方法红），exit code 1。

2. **实现**（`ctyun-stream-fix-proxy.py` 单个 Edit；old_string 为 Card 1 产物）：
   old:
```
_DASH_SECTIONS_V2 = ""

_DASH_JS_CORE = """</main>
```
   new（卡片顺序：健康 → 速度 → 延迟分布 → token → 三态）:
```
_DASH_SECTIONS_V2 = """  <section class="card">
    <div class="card-title">上游健康<span class="chip" id="health-status">--</span></div>
    <table>
      <tbody id="upstream-health"><tr><td class="empty" colspan="2">读取中……</td></tr></tbody>
    </table>
  </section>
  <section class="card">
    <div class="card-title">模型速度对比 · TTFB P50/P90（进程内累计，最快在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>模型</th><th>P50</th><th>P90</th></tr></thead>
      <tbody id="perf-model-body"><tr><td class="empty" colspan="3">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">延迟分布 · 三阶段 P50（连接 / 响应头 / 数据体）</div>
    <svg id="latency-dist" viewBox="0 0 600 96" preserveAspectRatio="none" role="img"
         aria-label="阶段延迟 P50 条形图"></svg>
  </section>
  <section class="card">
    <div class="card-title">Token 用量按天 × 模型（<span id="token-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th></tr></thead>
      <tbody id="token-daily-body"><tr><td class="empty" colspan="4">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card" id="tri-state-card">
    <div class="card-title">三态可用率 · 所选时段（<span id="tri-state-range">近7天</span>）</div>
    <div class="tri-bar" style="display:flex;height:14px;border-radius:4px;overflow:hidden;gap:2px"
         role="img" aria-label="ok / degraded / failed 占比条"></div>
    <div id="tri-state-legend" style="color:var(--dim);font-size:12px;margin-top:8px"></div>
  </section>
"""

_DASH_JS_CORE = """</main>
```

3. **验证**（逐条贴 stdout/exit code）：
   - `/usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardV2SkeletonTest` → `Ran 4 tests, OK`
   - `/usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_dashboard_html_full_page` → OK（旧断言不受影响；V2 未引入 innerHTML/新 range-tab/代理错误表头）
   - 全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → **`Ran 223 tests, OK`**（219 - 1 退役 + 4 新增）

**验收**：骨架测试 4 绿；全量 223 绿。

---

## Card 3（tier B）`_DASH_JS_V2` 渲染函数 + 健康轮询 + probe_alert 接线 + 集成断言

**目标**：`_DASH_JS_V2` 新增 `renderPerf`（模型速度表 + 延迟分布 SVG）/ `renderTokens` / `renderTriState` / `renderHealth` / `pollHealth`（独立并行 fetch `/api/health`）；`poll()` 装配 V2 渲染 + 携带 pollHealth；`_DASH_JS_CORE` 内 `applyRange` 复用 4 键切换重渲染 token/三态；probe_alert 三处接线（标签 / reason 明细 / 健康卡 chip hover）。

### 步骤（TDD）

1. **测试先行**（`ctyun-stream-fix-proxy.test.py` 两处 Edit）：

   **Edit T3.1**（`DashboardV2SkeletonTest` 追加 1 方法；锚点 = 类内 `test_no_inner_html_anywhere` 末尾两行断言之后）：
   old:
```
        self.assertGreaterEqual(html.count("textContent"), 1,
                                "textContent 出现次数必须 ≥ innerHTML（此处 innerHTML=0）")
```
   new:
```
        self.assertGreaterEqual(html.count("textContent"), 1,
                                "textContent 出现次数必须 ≥ innerHTML（此处 innerHTML=0）")

    def test_v2_js_functions_and_wiring(self) -> None:
        js = self.mod._DASH_JS_V2
        for fn in ("renderPerf", "renderTokens", "renderHealth", "renderTriState"):
            self.assertIn("function %s(" % fn, js, "V2 JS 缺函数 %s" % fn)
        self.assertIn("pollHealth", js)
        self.assertIn('fetch("/api/health")', js)
        core = self.mod._DASH_JS_CORE
        # probe_alert 进标签（EVT_KIND_LABELS）与 tooltip 明细（reason 展示）
        self.assertIn('probe_alert: "上游探测告警"', core)
        self.assertIn('(e.reason ? " · " + e.reason : "")', core)
        # applyRange 复用：token/三态随 4 键时间段切换零请求重渲染
        self.assertIn("renderTokens(lastSnap)", core)
        self.assertIn("renderTriState(lastSnap)", core)
        # 健康卡 chip 挂 probe_alert 事件标记（hover 出 tooltip）
        self.assertIn('markEvents(chip, "probe_alert", null, null)', js)
```

   **Edit T3.2**（`AdminIntegrationTest.test_dashboard_html_full_page` 尾部追加 P5 断言）：
   old:
```
        self.assertNotIn("slice(0, 14)", html)
        self.assertNotIn("最近 14 天", html)
        self.assertNotIn("与顶部错误数同口径", html)
```
   new:
```
        self.assertNotIn("slice(0, 14)", html)
        self.assertNotIn("最近 14 天", html)
        self.assertNotIn("与顶部错误数同口径", html)
        # P5：V2 新卡片容器 + 健康轮询端点经 HTTP 完整送达
        for cid in ("perf-model-body", "token-daily-body", "upstream-health",
                    "tri-state-card", "latency-dist"):
            self.assertIn('id="%s"' % cid, html)
        self.assertIn('fetch("/api/health")', html)
        self.assertIn("function renderPerf(", html)
        self.assertIn("function renderTokens(", html)
        self.assertIn("function renderHealth(", html)
        self.assertIn("function renderTriState(", html)
        self.assertIn("probe_alert", html)
```

   验证 RED：
   `/usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardV2SkeletonTest.test_v2_js_functions_and_wiring` → 预期 FAILED，exit code 1。

2. **实现**（`ctyun-stream-fix-proxy.py` 6 个 Edit，按序）：

   **Edit 3.1**（EVT_KIND_LABELS 加标签，位于 `_DASH_JS_CORE`）：
   old:
```
var EVT_KIND_LABELS = { proxy: "代理错误", upstream: "上游5xx", retry: "空流重试" };
```
   new:
```
var EVT_KIND_LABELS = { proxy: "代理错误", upstream: "上游5xx", retry: "空流重试", probe_alert: "上游探测告警" };
```

   **Edit 3.2**（showEvtTip 明细加 reason）：
   old:
```
        (e.model ? " · " + e.model : "") +
        (e.status ? " · " + e.status : "")));
```
   new:
```
        (e.model ? " · " + e.model : "") +
        (e.status ? " · " + e.status : "") +
        (e.reason ? " · " + e.reason : "")));
```

   **Edit 3.3**（applyRange 复用 4 键切换，位于 `_DASH_JS_CORE`）：
   old:
```
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
   new:
```
function applyRange() {
  renderRangeTabs();
  var label = RANGE_LABELS[selectedRange] || selectedRange;
  $("daily-title-range").textContent = label;
  $("daily-model-title-range").textContent = label;
  $("token-title-range").textContent = label;
  $("tri-state-range").textContent = label;
  if (lastSnap) {
    renderStats(lastSnap);
    renderDaily(lastSnap.daily || {});
    renderDailyByModel(lastSnap.daily_by_model || {});
    renderTokens(lastSnap);
    renderTriState(lastSnap);
  }
}
```

   **Edit 3.4**（`_DASH_JS_V2` 顶部插入 5 个新函数）：
   old:
```
function poll() {
  var ctrl = new AbortController();
```
   new:
```
function renderPerf(snap) {
  var perf = snap.perf || {};
  var p50 = perf.ttfb_p50_ms_by_model || {};
  var p90 = perf.ttfb_p90_ms_by_model || {};
  var body = $("perf-model-body");
  body.textContent = "";
  var names = Object.keys(p50).sort(function (a, b) { return p50[a] - p50[b]; });
  if (names.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无速度数据 —— 有请求经过代理后这里会出现 P50/P90");
    td0.colSpan = 3;
    tr0.appendChild(td0);
    body.appendChild(tr0);
  }
  for (var i = 0; i < names.length; i++) {
    var tr = el("tr");
    tr.appendChild(el("td", "", names[i]));
    tr.appendChild(el("td", "num", fmtDur(p50[names[i]])));
    tr.appendChild(el("td", "num", fmtDur(p90[names[i]])));
    body.appendChild(tr);
  }
  renderLatencyDist(perf.phase_p50_ms || {});
}
function renderLatencyDist(phases) {
  var svg = $("latency-dist");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var defs = [["connect", "连接"], ["headers", "响应头"], ["body", "数据体"]];
  var cap = 5000;
  var svgns = "http://www.w3.org/2000/svg";
  for (var i = 0; i < defs.length; i++) {
    var name = defs[i][0];
    var label = defs[i][1];
    var ms = phases[name];
    var w = ms === undefined ? 0 : Math.min(ms / cap, 1) * 460;
    var g = document.createElementNS(svgns, "g");
    var t = document.createElementNS(svgns, "text");
    t.setAttribute("x", 0);
    t.setAttribute("y", 24 + i * 30);
    t.setAttribute("fill", "var(--dim)");
    t.setAttribute("font-size", 12);
    t.textContent = label;
    var r = document.createElementNS(svgns, "rect");
    r.setAttribute("x", 90);
    r.setAttribute("y", 12 + i * 30);
    r.setAttribute("width", Math.max(2, w).toFixed(1));
    r.setAttribute("height", 14);
    r.setAttribute("fill", "var(--amber)");
    var v = document.createElementNS(svgns, "text");
    v.setAttribute("x", 96 + Math.max(2, w));
    v.setAttribute("y", 24 + i * 30);
    v.setAttribute("fill", "var(--text)");
    v.setAttribute("font-size", 12);
    v.textContent = ms === undefined ? "—" : fmtDur(ms);
    g.appendChild(t);
    g.appendChild(r);
    g.appendChild(v);
    svg.appendChild(g);
  }
}
function renderTokens(snap) {
  var body = $("token-daily-body");
  body.textContent = "";
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var dbm = snap.daily_by_model || {};
  var days = Object.keys(dbm).sort().reverse().filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
  var hasData = false;
  for (var i = 0; i < days.length; i++) {
    var models = dbm[days[i]];
    var names = Object.keys(models).sort(function (a, b) {
      return ((models[b].tokens_prompt || 0) + (models[b].tokens_completion || 0)) -
             ((models[a].tokens_prompt || 0) + (models[a].tokens_completion || 0));
    });
    for (var j = 0; j < names.length; j++) {
      var ent = models[names[j]];
      var tp = ent.tokens_prompt || 0;
      var tc = ent.tokens_completion || 0;
      if (tp + tc === 0) continue;  // 0-token 行（502/剥行等）不展示
      hasData = true;
      var tr = el("tr");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(tp)));
      tr.appendChild(el("td", "num", String(tc)));
      body.appendChild(tr);
    }
  }
  if (!hasData) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无 token 用量 —— 上游返回 usage 帧后这里会出现记录");
    td0.colSpan = 4;
    tr0.appendChild(td0);
    body.appendChild(tr0);
  }
}
function renderTriState(snap) {
  var bar = document.querySelector("#tri-state-card .tri-bar");
  var legend = $("tri-state-legend");
  bar.textContent = "";
  legend.textContent = "";
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var dbm = snap.daily_by_model || {};
  var days = Object.keys(dbm).filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
  var ok = 0, deg = 0, fail = 0;
  for (var i = 0; i < days.length; i++) {
    var models = dbm[days[i]];
    var names = Object.keys(models);
    for (var j = 0; j < names.length; j++) {
      var ent = models[names[j]];
      ok += ent.outcome_ok || 0;
      deg += ent.outcome_degraded || 0;
      fail += ent.outcome_failed || 0;
    }
  }
  var total = ok + deg + fail;
  if (total === 0) {
    bar.appendChild(el("span", "empty", "暂无 outcome 数据 —— 有请求经过代理后这里会出现三态占比"));
    return;
  }
  var wOk = Math.round(ok / total * 100);
  var wDeg = Math.round(deg / total * 100);
  var wFail = 100 - wOk - wDeg;
  var segs = [["ok", wOk, "var(--ok)"], ["degraded", wDeg, "var(--amber)"], ["failed", wFail, "var(--err)"]];
  for (var s = 0; s < segs.length; s++) {
    var seg = el("span", "");
    seg.style.width = segs[s][1] + "%";
    seg.style.background = segs[s][2];
    bar.appendChild(seg);
  }
  function pct(n) { return (n / total * 100).toFixed(1) + "%"; }
  legend.appendChild(el("span", "", "ok " + ok + "（" + pct(ok) + "）"));
  legend.appendChild(el("span", "", " · degraded " + deg + "（" + pct(deg) + "）"));
  legend.appendChild(el("span", "", " · failed " + fail + "（" + pct(fail) + "）"));
}
function renderHealth(h) {
  var body = $("upstream-health");
  var chip = $("health-status");
  body.textContent = "";
  if (!h) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "健康数据读取中……（/api/health 未响应）");
    td0.colSpan = 2;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    chip.textContent = "读取中";
    return;
  }
  var rows = [
    ["上游主机", h.host || "—"],
    ["最近探测", h.last_probe_ts ? fmtDate(h.last_probe_ts) + " " + fmtTime(h.last_probe_ts) : "从未探测"],
    ["探测延迟", h.last_probe_ts ? fmtDur(h.last_probe_latency_ms) : "—"],
    ["连续失败", String(h.consecutive_failures || 0)],
    ["主动探测", h.probe_enabled ? "开" : "关"]
  ];
  for (var i = 0; i < rows.length; i++) {
    var tr = el("tr");
    tr.appendChild(el("td", "num", rows[i][0]));
    tr.appendChild(el("td", "", rows[i][1]));
    body.appendChild(tr);
  }
  if (!h.last_probe_ts) {
    chip.textContent = "待首探";
  } else if (h.last_probe_ok) {
    chip.textContent = "正常";
  } else {
    chip.textContent = "异常";
  }
  markEvents(chip, "probe_alert", null, null);
}
function pollHealth() {
  fetch("/api/health")
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (data) {
      renderHealth(data && data.upstream ? data.upstream : null);
    })
    .catch(function () {
      renderHealth(null);  // 健康卡独立降级显示"读取中"，不触发 setConn（连通性以 /api/stats 为准）
    });
}
function poll() {
  var ctrl = new AbortController();
```

   **Edit 3.5**（poll 装配 V2 渲染）：
   old:
```
      renderSpark(snap.recent || []);
      setConn(true);
```
   new:
```
      renderSpark(snap.recent || []);
      renderPerf(snap);
      renderTokens(snap);
      renderTriState(snap);
      setConn(true);
```

   **Edit 3.6**（poll 尾部携带 pollHealth——与 stats 请求并发在途）：
   old:
```
      setConn(false, err && err.name === "AbortError" ? "轮询超时(4s)" : String(err));
    });
}
```
   new:
```
      setConn(false, err && err.name === "AbortError" ? "轮询超时(4s)" : String(err));
    });
  pollHealth();
}
```

3. **验证**（逐条贴 stdout/exit code）：
   - `/usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardV2SkeletonTest` → `Ran 5 tests, OK`
   - `/usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_dashboard_html_full_page` → OK（含新 P5 断言）
   - 全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → **`Ran 224 tests, OK`**

**验收**：骨架测试 5 绿；集成 full-page 绿；全量 224 绿（whole-branch）。

---

## Card 4（tier B，docs 豁免 TDD）README 计数口径 + 功能清单补行

**目标**：补 spec:97 遗留的 README 文档缺口（实测计数口径表缺 P5 新卡片行、功能清单缺新卡片 bullet）。无新 API，只补文档。

### 实现（`README.md` 两个 Edit）

**Edit 4.1**（功能清单 dashboard bullets）：
old:
```
  - 「剥行流带」最近 20 条毒 record 预览 + 累计 sparkline
  - 「按天统计」「按天 × 模型」随所选时间段过滤日期（上月最多 31 行；daily 分桶，双口径错误列）
```
new:
```
  - 「剥行流带」最近 20 条毒 record 预览 + 累计 sparkline
  - 「按天统计」「按天 × 模型」随所选时间段过滤日期（上月最多 31 行；daily 分桶，双口径错误列）
  - 「模型速度对比」各模型 TTFB P50/P90（进程内直方图分位数）+「延迟分布」连接/响应头/数据体三阶段 P50
  - 「Token 用量按天 × 模型」prompt/completion tokens（usage 帧抽取，随所选时间段过滤）
  - 「三态可用率」ok/degraded/failed 占比条（随所选时间段过滤）
  - 「上游健康」主动探测 HEAD 结果（最近探测时间/延迟/连续失败/开关；探测告警 hover 明细）
```

**Edit 4.2**（计数口径表）：
old:
```
| 「最近请求」「剥行流带」 | 最近 100 / 20 条内存窗口 | 否 |
| 错误/重试数字 hover 明细 | 事件流最近 100 条（代理错误+上游5xx+空流重试） | 是（stats.events 随 60s 周期落盘） |
```
new:
```
| 「最近请求」「剥行流带」 | 最近 100 / 20 条内存窗口 | 否 |
| 「模型速度对比」「延迟分布」 | 进程内 TTFB 直方图（9 桶）分位数：按模型 P50/P90 + 三阶段全局 P50 | 否（重启清零） |
| 「Token 用量按天 × 模型」 | usage 帧 prompt/completion tokens，随所选时间段过滤 | 是（daily_by_model 90 天 prune） |
| 「三态可用率」 | outcome ok/degraded/failed 按天×模型计数聚合，随所选时间段过滤 | 是（daily_by_model 90 天 prune） |
| 「上游健康」 | 最近一次 probe HEAD 的延迟/成败 + 连续失败计数 | 否（内存态 probe 状态） |
| 错误/重试数字 hover 明细 | 事件流最近 100 条（代理错误+上游5xx+空流重试+探测告警） | 是（stats.events 随 60s 周期落盘） |
```

### 验证

- 文档校验（grep）：`grep -n '模型速度对比\|三态可用率\|上游健康\|Token 用量按天 × 模型' README.md` → 4 处命中（功能清单 4 bullet + 表 4 行，grep 计数 ≥8 命中线），exit code 0
- **whole-branch 收尾全量**（VERIFY 铁律：最后一卡执行者跑全量）：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → **`Ran 224 tests, OK`**

**验收**：README 两处补行落盘；全量 224 绿。

---

## DELIVER-期手测清单（非卡 gate，主代理 DELIVER 后浏览器目检兜底）

1. `view-source:http://127.0.0.1:7921/` 全文 0 个 `innerHTML=`。
2. 有流量后：模型速度表按 P50 升序出各模型行；延迟分布三根条有数值；token 表随 4 键 range 切换变化；三态占比条三色且 legend 百分比合计 100%。
3. 健康卡显示"正常/异常/待首探"与最近探测时间；把上游指向假 5xx（或 POST /api/probe 关开）后连续失败计数增长、EVENTS 出 probe_alert 时 hover 健康卡 chip 出 tooltip 含 reason。
4. 切 4 键 range：顶部卡/按天/按天×模型/token/三态同步重渲染且零网络请求（Network 面板确认）。

## 自查（Plan Self-Review 5 项）

1. **spec coverage**：spec P5 :75-87 全部落卡——6 段拆分（Card 1）、V2 五类卡片容器（Card 2）、renderPerf/renderTokens/renderHealth/renderTriState + poll 装配（Card 3）、range tabs 复用（Edit 3.3）、textContent 模式（Global Constraints 2 + test_no_inner_html_anywhere）、probe_alert 过滤与标签（Edit 3.1/3.2 + renderHealth markEvents）；spec :96 DashboardV2SkeletonTest 全断言落地；spec :97 README 计数口径三行（Card 4，实测缺口补 4 行）；Acceptance :107 dashboard 全量口径覆盖。
2. **placeholder scan**：无 TODO/待办/占位符；所有 Edit 的 old/new 字符串与测试代码全文写死。
3. **type consistency**：JS 消费的 snap 字段与 `stats_snapshot` perf 节（:1564-1584）及 `/api/health` 6 键（:1288-1302）逐一比对一致；`daily_by_model` 16 字段（tokens_prompt/tokens_completion/outcome_*）与 `_DAILY_FIELDS`（:1078-1093）一致。
4. **可落盘性**：6 段拆分按字符串行索引 0-82/83-110/111-141/142-482/483-550/551-553 实测验证 join 逐字节等价（脚本实测 `Byte-identical: True`）；尾部字面量含换行的裁决已入 Anchor Reconciliation。
5. **锚点实测**：全部锚点来自 worktree bd2554f 实读（行号、sha256 `2e4dfdb4...9539c`、baseline 219 绿、unittest 多参数运行实测可用）；无推测锚点。
