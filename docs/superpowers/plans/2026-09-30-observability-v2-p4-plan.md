# PLAN — observability v2 P4（三态 outcome 归因 + 主动探测）

- **spec（唯一权威源）**：`docs/superpowers/specs/2026-09-30-ctyun-proxy-observability-v2-design.md`（P4 节 + Risks R2/R3）
- **分支/worktree**：`fix/observability-v2-p4` @ main 7348be7（P1-P3 已合入），作业目录 `.worktrees/observability-v2-p4/`
- **baseline（2026-10-01 实测）**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 189 tests in 84.370s / OK`，exit=0。VERIFY 只对「baseline 绿 → 现在红」负责。
- **单测命令铁律**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py <TestClass>[.<method>]`（禁 `-m unittest`；禁 Homebrew python3.14——README.md:85 明示其 HTTPServer 挂死）。每卡验证贴 stdout + exit code 原文。
- **预期卡数**：4 卡（≤4 → 小计划，一次 PLAN-B 完成）。卡 1/2/3 tier A（完整代码+测试写死，executor 转写）；卡 4 tier B（README 文档，无 TDD 豁免，验证=grep 证据）。
- **卡序依赖**：卡 1（纯函数+落桶，无依赖）→ 卡 2（probe 核心 + loop 启动，黑盒测试不依赖 /api/health，经 /api/stats events 断言）→ 卡 3（admin API + persist + env，依赖卡 2 的 loop/快照函数）→ 卡 4（文档）。卡间严格串行，前卡全绿再开下卡。

---

## Global Constraints

1. **TDD 铁律**：每卡先落失败测试（跑测试类贴红证据），再写生产代码，再跑绿。卡 4 为文档改动，豁免 TDD（验证 = grep 证据 + 全量回归不红）。
2. **失败零副作用（R3）**：probe 线程**绝不**走 `_record_request`、绝不写 STATS 请求计数/histogram/rates/daily；唯一跨 STATS 共享点是 EVENTS 告警 append（持 STATS_LOCK，与 `_record_empty_retry` :1179 同款模式）。`ProbeNoSideEffectTest` 锚死。
3. **锁纪律**：PROBE_LOCK 只保护 `_PROBE_STATE`；`PROBE_ENABLED` 由 `_CFG_LOCK` 护写、读侧无锁（bool 引用赋值原子，同 CAPTURE_ERRORS 惯例）；probe 线程**绝不持锁做网络 IO**（网络调用 `_probe_once` 全部锁外）。
4. **R2 双向降级**：events kind 白名单加 `probe_alert`；旧二进制读含新 kind 的文件整条丢弃不崩（`ProbeEventsCompatTest` 用旧白名单 inline 复刻锁死）。persist 顶层新增 `probe_enabled` 布尔键：旧版读入只取认识的键自动忽略；缺键回落默认 True。
5. **不改转发语义**：剥行/重试/finish-hold/TPM 路径零行为变化；`_log` 与 RECENT_REQUESTS 展示口径不变（outcome 恒存 category 字符串）。
6. **env seam 纪律**：`CTYUN_PROBE_INTERVAL_S` 显式正数**原样采用**（测试 0.5s 加速 seam），≤0/非法值回落 `PROBE_MIN_INTERVAL_S`（防滥用下限兜底，见 Anchor Reconciliation A1）；`CTYUN_PROBE_ENABLED` 优先级 env > persist > 默认 True（同 MODEL_PRICING 解析模式）。
7. **单文件部署**：零新依赖、零新文件（生产）；测试只扩 `ctyun-stream-fix-proxy.test.py`。
8. **验证随卡**：每卡验证命令贴实际 stdout/stderr + exit code；最终 whole-branch 全量 `test.py` 由卡 4 收尾跑（README 改动不影响测试，但作为终验跑全量并贴证据）。

---

## Anchor Reconciliation（实测锚点 + 裁决）

> 2026-10-01 在 worktree 内实测（Read/grep 取证），行号以 `ctyun-stream-fix-proxy.py` 当前内容为准。

| # | spec 表述 | 实测现状 | 裁决 |
|---|-----------|----------|------|
| A1 | `CTYUN_PROBE_INTERVAL_S` 最小 10s 防滥用，但测试节要求 `=0.5` 加速 | 两者在 spec 内矛盾：钳 10s 下限则 0.5s 加速 seam 失效 | **显式正数原样采用；≤0/非法回落 PROBE_MIN_INTERVAL_S=10**。防滥用只兜非法配置（0/负数/垃圾字符串 → 忙轮询风险），显式小值视为操作者/测试有意配置。写成纯函数 `_probe_interval_s_from_env(raw)` 白盒可测 |
| A2 | `_record_request` 落桶改用 `outcome.tri_state` | 实测：`:1085`/`:1107` 现用 `_outcome_tri_state(outcome)`（纯函数，P3 seam，`:303-313`）；调用点 499（`:1322`）、502 两处（`:1411`/`:1458`）、SSE（`:1520`）、buffered（`:1565`）全传 `outcome.category` **字符串**；测试 `:4254` 传 `mod.CLASS_OK` 字符串 | `_Outcome` namedtuple 扩第 5 位 `tri_state`（由 `_outcome_tri_state(category)` 推导）；**保留** `_outcome_tri_state` 纯函数（P3 测试 `test_outcome_tri_state_mapping` :4275 锁死 + 字符串回退路径需要）。`_record_request` 入参兼容两形态：`hasattr(outcome, "tri_state")` → 用字段；否则回退纯函数。5 个生产调用点全部改传 namedtuple；RECENT_REQUESTS 恒存 category 字符串（namedtuple 非 JSON 可序列化，且 `RequestIdTest` :4325 断言 `entry["outcome"] == "ok"` 字符串） |
| A3 | PROBE_LOCK / _PROBE_STATE | 实测：`:644-645` 仅注释声明（P1 锁纪律注释块），**无**实际 Lock 对象；`_PROBE_STATE` 不存在；`PROBE_LATENCY_SPIKE_FACTOR` 不存在 | 卡 2 真正创建 `PROBE_LOCK = threading.Lock()`（插在注释块后、`_CFG_LOCK` 上）与 `_PROBE_STATE`（插在 `_stats_dirty` 后） |
| A4 | probe_enabled=False 时「不启动循环或空转」（spec 让你裁决） | 实测：POST /api/probe 可运行时切开关 | **裁决：main() 恒启动一个 `_probe_loop` daemon 线程；enabled=False 时空转（sleep 间隔 + 查开关，零网络 IO 零副作用）**。理由：运行时 on/off 切换无需线程生命周期管理（不 start/stop 线程），空转成本=每间隔一次 sleep；`ProbeToggleTest` 断言 off 后 HEAD 计数停增、seed_persist/enabled=false 启动后零 HEAD。 |
| A5 | 延迟突增告警「最近 20 次 P90 > 历史 7 天 P90 × 2」 | 无现成历史存储；persist 逐 probe 延迟需 7 天 × 2880 样本/天落盘，违背失败零副作用与 R3 简洁性 | 内存态 deque `history`（`maxlen=PROBE_HISTORY_MAX=20160` ≈ 30s 间隔 × 7 天），仅成功 probe append，重启清零（内存态观测，同 P2 ttfb_hist 口径）。基线 P90 = 全 history P90；最近 20 = history 尾 20；`len < 20` 不告警。写成纯函数 `_probe_spike_due(history, factor)` 白盒可测 |
| A6 | EVENTS `{"kind":"probe_alert","reason":...}` vs `load_stats_events` :983 白名单 `("proxy","upstream","retry")` + 输出只含 ts/kind/model/status | 实测：`:986-998` 白名单写死 3 kind；输出丢弃未知键 | 白名单加 `probe_alert`；`load_stats_events` 对 `kind=="probe_alert"` 且 `reason` 为合法 str（≤200ch）时在输出中保留 `reason` 键。旧二进制读新文件：kind 不识别 → 整条 continue 丢弃（R2 正确降级，测试锁死） |
| A7 | `/api/health` 返回 `{"upstream": {host, last_probe_ts, last_probe_ok, last_probe_latency_ms, consecutive_failures, probe_enabled}}` | 实测：AdminHandler.do_GET :1818 无此分支；do_POST :1909 只放行 `/api/config` | 卡 3 在 do_GET 的 `/api/config` 分支后加 `/api/health`（经 `probe_state_snapshot()`，卡 2 已定义，返回**恰好** spec 6 键）；do_POST 开头加 `/api/probe` 分流到新 `_handle_probe_post`（鉴权/结构完全镜像 `/api/config` 的 write_allowed + 400 文案风格） |
| A8 | persist 顶层 `probe_enabled`；env > persist > 默认 | 实测：`save_stats_counters` :862-868 dump 键 `upstream_base/capture_errors/model_pricing/stats`；`persist_upstream` :712 只写 2 键（其后必有 save_stats_counters 补全量写，沿用该模式）；`load_model_pricing` :901 为「顶层键读取」样板 | `save_stats_counters` dump 加 `probe_enabled`（`_CFG_LOCK` 块内同读）；新增 `load_probe_enabled(path)`（缺/非 bool → True）与 `resolve_probe_enabled(env_val, persisted)` 纯函数；`set_probe_enabled` 镜像 `set_capture_errors` :1036（锁外 save）；main() 启动时解析覆盖 `PROBE_ENABLED`。`persist_upstream` 不动（其后紧跟全量 save，与 model_pricing 同一既存模式） |
| A9 | test 基建 | 实测：`start_proxy(upstream_port, proxy_port, extra_env, seed_persist)` :414、`make_fake_upstream` :245、`admin_get` :573、`admin_post` :585、`FakeUpstreamHandler.do_GET` :233、`stop_fake_upstreams` :352、`FAKE_SERVERS` :46 | 新增 `FakeUpstreamHandler.do_HEAD`（计数 + 可控 500）+ 独立 `make_probe_upstream(head_fail=False) -> (port, head_calls)`。`make_fake_upstream` 签名**不动**（避免扰动既有 40+ 调用点） |
| A10 | CLASS_TPM_LIMITED 的 tri_state | 实测：`classify_outcome` 不产 CLASS_TPM_LIMITED（429 路径 :1343 直接 log 不经网关）；`_KIND_CATEGORY` :111 映射 TPM → CLASS_TPM_LIMITED；`_outcome_tri_state` 对该值落 failed（else 分支） | 测试矩阵经 `_outcome_tri_state(CLASS_TPM_LIMITED) == "failed"` 锁死（main agent 事实 3：tri_state 归 failed），并注释说明 classify_outcome 不产该类别的原因 |
| A11 | spec 测试名 vs 现有测试风格 | 实测：新增测试类均独立 `unittest.TestCase` 放文件尾部区（:3914 起 P1-P3 类如此） | 新类插入 `StallTailGapTest`（:4851）之后、`if __name__ == "__main__"`（:4875）之前 |

---

## 卡 1（tier A）— `_Outcome` 增 tri_state + `_record_request` 落桶兼容

**目标**：`classify_outcome` 返回 5 字段 namedtuple（+`tri_state`）；`_record_request` 落 daily 三桶改用 `outcome.tri_state`（namedtuple 形态），字符串形态回退 `_outcome_tri_state`；5 个生产调用点改传 namedtuple；RECENT_REQUESTS 恒存 category 字符串。

### TDD 红相

在 `ctyun-stream-fix-proxy.test.py` 尾部（`if __name__ == "__main__"` 前）追加：

```python
class ClassifyOutcomeTriStateTest(unittest.TestCase):
    """P4：classify_outcome 返回 tri_state 字段；_record_request 三桶归因 + 双形态兼容。"""

    def test_classify_outcome_tri_state_matrix(self) -> None:
        mod = load_proxy_module()
        f = mod.classify_outcome
        self.assertEqual(f(status=200).tri_state, "ok")
        self.assertEqual(f(status=200, poison_filtered=1).tri_state, "degraded")
        self.assertEqual(f(client_abort=True).tri_state, "degraded")
        self.assertEqual(f(synth_502=True).tri_state, "failed")
        self.assertEqual(f(body_error=True).tri_state, "failed")
        self.assertEqual(f(eof_without_done=True).tri_state, "failed")
        self.assertEqual(f(status=404).tri_state, "failed")
        self.assertEqual(f(status=500).tri_state, "failed")

    def test_tri_state_consistent_with_pure_function(self) -> None:
        mod = load_proxy_module()
        for outcome in (mod.classify_outcome(status=200),
                        mod.classify_outcome(status=200, poison_filtered=1),
                        mod.classify_outcome(client_abort=True),
                        mod.classify_outcome(synth_502=True),
                        mod.classify_outcome(body_error=True),
                        mod.classify_outcome(status=404),
                        mod.classify_outcome(status=500)):
            self.assertEqual(outcome.tri_state,
                             mod._outcome_tri_state(outcome.category))

    def test_tpm_limited_maps_to_failed(self) -> None:
        mod = load_proxy_module()
        # classify_outcome 不产 CLASS_TPM_LIMITED（429 路径不经网关），但映射必须
        # 覆盖该 category（防未来走此网关时误归 ok/degraded）
        self.assertEqual(mod._outcome_tri_state(mod.CLASS_TPM_LIMITED), "failed")

    def test_record_request_lands_tri_state_buckets_from_namedtuple(self) -> None:
        mod = load_proxy_module()
        orig = mod.STATS["daily_by_model"]
        try:
            mod.STATS["daily_by_model"] = {}
            mod._record_request("POST", "/p4", 200, 1.0, 0, model="m-p4",
                                outcome=mod.classify_outcome(status=200))
            mod._record_request("POST", "/p4", 502, 1.0, 0, model="m-p4",
                                outcome=mod.classify_outcome(synth_502=True))
            mod._record_request("POST", "/p4", 200, 1.0, 1, model="m-p4",
                                outcome=mod.classify_outcome(status=200,
                                                             poison_filtered=1))
            entry = mod.STATS["daily_by_model"][mod.today_key()]["m-p4"]
            self.assertEqual(entry["outcome_ok"], 1)
            self.assertEqual(entry["outcome_degraded"], 1)
            self.assertEqual(entry["outcome_failed"], 1)
            # RECENT_REQUESTS 恒存 category 字符串（JSON 可序列化 + 展示口径不变）
            self.assertEqual(mod.RECENT_REQUESTS[-1]["outcome"],
                             mod.CLASS_POISON_FIXED)
        finally:
            mod.STATS["daily_by_model"] = orig

    def test_record_request_string_outcome_back_compat(self) -> None:
        """旧调用形态（category 字符串，P3 测试与既有路径用）仍正确落桶。"""
        mod = load_proxy_module()
        orig = mod.STATS["daily_by_model"]
        try:
            mod.STATS["daily_by_model"] = {}
            mod._record_request("POST", "/p4s", 200, 1.0, 0, model="m-p4s",
                                outcome=mod.CLASS_OK)
            entry = mod.STATS["daily_by_model"][mod.today_key()]["m-p4s"]
            self.assertEqual(entry["outcome_ok"], 1)
            self.assertEqual(mod.RECENT_REQUESTS[-1]["outcome"], mod.CLASS_OK)
        finally:
            mod.STATS["daily_by_model"] = orig
```

验证（红，预期 AttributeError/tuple 长度错）：

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p4 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py ClassifyOutcomeTriStateTest; echo "EXIT=$?"
```

### 生产代码（`ctyun-stream-fix-proxy.py`）

**Edit 1.1** — namedtuple 扩位（:272）：

old_string:
```python
_Outcome = collections.namedtuple("_Outcome", "category log_result counts_error capture")
```
new_string:
```python
_Outcome = collections.namedtuple("_Outcome", "category log_result counts_error capture tri_state")
```

**Edit 1.2** — `classify_outcome` 整体替换（:275-300）：

old_string:
```python
def classify_outcome(status=None, synth_502=False, client_abort=False,
                     eof_without_done=False, poison_filtered=0,
                     body_error=False) -> _Outcome:
    """错误分类网关：输入场景标志 → 返回 (category, log_result, counts_error, capture)。

    判定优先级 client_abort > synth_502 > body_error > eof_without_done > status 阈值；
    status=None 且无 flags → ValueError。
    """
    if client_abort:
        return _Outcome(CLASS_CLIENT_ABORT, "aborted", False, False)
    if synth_502:
        return _Outcome(CLASS_UPSTREAM_FAULT, "error", True, True)
    if body_error:
        return _Outcome(CLASS_BODY_ERROR, "body-err", True, True)
    if eof_without_done:
        return _Outcome(CLASS_UPSTREAM_FAULT, "eof-without-done", False, True)
    if status is None:
        raise ValueError("classify_outcome: status required when no flag is set")
    if status < 400:
        if poison_filtered > 0:
            return _Outcome(CLASS_POISON_FIXED, "ok", False, True)
        return _Outcome(CLASS_OK, "ok", False, False)
    if 400 <= status < 500:
        return _Outcome(CLASS_REQUEST_FAULT, "upstream-err", False, True)
    # status >= 500
    return _Outcome(CLASS_UPSTREAM_FAULT, "upstream-err", False, True)
```
new_string:
```python
def classify_outcome(status=None, synth_502=False, client_abort=False,
                     eof_without_done=False, poison_filtered=0,
                     body_error=False) -> _Outcome:
    """错误分类网关：输入场景标志 → 返回 (category, log_result, counts_error, capture, tri_state)。

    判定优先级 client_abort > synth_502 > body_error > eof_without_done > status 阈值；
    status=None 且无 flags → ValueError。
    v2 P4：tri_state 由 category 经 _outcome_tri_state 推导（"ok"|"degraded"|"failed"），
    供 daily outcome_* 三桶归因；纯函数 _outcome_tri_state 保留（字符串回退路径 + P3 测试锚）。
    """
    if client_abort:
        return _Outcome(CLASS_CLIENT_ABORT, "aborted", False, False,
                        _outcome_tri_state(CLASS_CLIENT_ABORT))
    if synth_502:
        return _Outcome(CLASS_UPSTREAM_FAULT, "error", True, True,
                        _outcome_tri_state(CLASS_UPSTREAM_FAULT))
    if body_error:
        return _Outcome(CLASS_BODY_ERROR, "body-err", True, True,
                        _outcome_tri_state(CLASS_BODY_ERROR))
    if eof_without_done:
        return _Outcome(CLASS_UPSTREAM_FAULT, "eof-without-done", False, True,
                        _outcome_tri_state(CLASS_UPSTREAM_FAULT))
    if status is None:
        raise ValueError("classify_outcome: status required when no flag is set")
    if status < 400:
        if poison_filtered > 0:
            return _Outcome(CLASS_POISON_FIXED, "ok", False, True,
                            _outcome_tri_state(CLASS_POISON_FIXED))
        return _Outcome(CLASS_OK, "ok", False, False,
                        _outcome_tri_state(CLASS_OK))
    if 400 <= status < 500:
        return _Outcome(CLASS_REQUEST_FAULT, "upstream-err", False, True,
                        _outcome_tri_state(CLASS_REQUEST_FAULT))
    # status >= 500
    return _Outcome(CLASS_UPSTREAM_FAULT, "upstream-err", False, True,
                    _outcome_tri_state(CLASS_UPSTREAM_FAULT))
```

**Edit 1.3** — `_record_request` 双形态归一（:1052-1053 之前）：

old_string:
```python
                    chunks: int = 0,
                    tokens_prompt: int = 0, tokens_completion: int = 0,
                    phase_ms: dict = None) -> None:
    global _stats_dirty
    with STATS_LOCK:
```
new_string:
```python
                    chunks: int = 0,
                    tokens_prompt: int = 0, tokens_completion: int = 0,
                    phase_ms: dict = None) -> None:
    global _stats_dirty
    # v2 P4：outcome 兼容两形态——_Outcome namedtuple（落桶用 tri_state 字段）或
    # category 字符串（旧调用点/测试，回退 _outcome_tri_state 纯函数）。
    # RECENT_REQUESTS 恒存 category 字符串（JSON 可序列化 + 展示口径不变）。
    tri_state = None
    outcome_category = None
    if outcome is not None:
        if hasattr(outcome, "tri_state"):
            tri_state = outcome.tri_state
            outcome_category = outcome.category
        else:
            tri_state = _outcome_tri_state(outcome)
            outcome_category = outcome
    with STATS_LOCK:
```

**Edit 1.4** — dm entry 落桶（:1084-1085）：

old_string:
```python
                if outcome is not None:
                    entry_dm["outcome_" + _outcome_tri_state(outcome)] += 1
```
new_string:
```python
                if tri_state is not None:
                    entry_dm["outcome_" + tri_state] += 1
```

**Edit 1.5** — daily 总桶落桶（:1106-1107）：

old_string:
```python
        if outcome is not None:
            bucket["outcome_" + _outcome_tri_state(outcome)] += 1
```
new_string:
```python
        if tri_state is not None:
            bucket["outcome_" + tri_state] += 1
```

**Edit 1.6** — RECENT_REQUESTS 存 category（:1119）：

old_string:
```python
                                "chunks": chunks, "outcome": outcome})
```
new_string:
```python
                                "chunks": chunks, "outcome": outcome_category})
```

**Edit 1.7** — 499 路径传 namedtuple（:1322；`_log` 行保持 category 不动）：

old_string:
```python
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome.category)
```
new_string:
```python
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome)
```

**Edit 1.8** — 502 路径①（:1407-1411）：

old_string:
```python
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome.category)
```
new_string:
```python
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome)
```

**Edit 1.9** — 502 路径②（:1454-1458，缩进不同故 old_string 唯一）：

old_string:
```python
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model,
                                    error=outcome.counts_error,
                                    rid=self._req_id, upstream_host=self._upstream_host,
                                    outcome=outcome.category)
```
new_string:
```python
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model,
                                    error=outcome.counts_error,
                                    rid=self._req_id, upstream_host=self._upstream_host,
                                    outcome=outcome)
```

**Edit 1.10** — SSE 主路径（:1520，`stream=1` 使 old_string 唯一）：

old_string:
```python
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category,
```
new_string:
```python
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome,
```

**Edit 1.11** — buffered 路径（:1565，`stream=0` 使 old_string 唯一）：

old_string:
```python
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category,
```
new_string:
```python
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome,
```

### 验证（绿）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p4 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py ClassifyOutcomeTriStateTest; echo "EXIT=$?"
```

回归（既有三桶/分类/快照测试不得红）：

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_classify_outcome_full_matrix ProxyDashboardUnitTest.test_classify_outcome_body_error DailyV2CompatTest.test_outcome_tri_state_mapping DailyV2CompatTest.test_roundtrip_16_field_full_equality RequestIdTest; echo "EXIT=$?"
```

---

## 卡 2（tier A）— probe 核心：常量/锁/状态/loop/告警 + events 白名单 + 测试基建

**目标**：`PROBE_LATENCY_SPIKE_FACTOR`/`PROBE_HISTORY_MAX`/`PROBE_INTERVAL_S` 常量与解析；真正创建 `PROBE_LOCK` + `_PROBE_STATE` + `PROBE_ENABLED`；`_latency_p90`/`_probe_spike_due`/`_probe_once`/`_probe_loop`/`probe_state_snapshot`；`load_stats_events` 白名单加 `probe_alert` + reason 保留；main() 启动 loop 线程；测试基建 `do_HEAD` + `make_probe_upstream`。

### TDD 红相（测试基建 + 测试类）

**Edit T2.1** — FakeUpstreamHandler 类属性（:73 后）：

old_string:
```python
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流
    bodies = None        # 共享 list：非 None 时按调用序 append 收到的原始请求体 bytes
```
new_string:
```python
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流
    bodies = None        # 共享 list：非 None 时按调用序 append 收到的原始请求体 bytes
    head_calls = None    # P4 probe 测试：非 None 时 do_HEAD 按调用序 append 计数
    head_fail = False    # P4 probe 测试：True → do_HEAD 回 500（连续失败告警场景）
```

**Edit T2.2** — do_HEAD 方法（do_GET 之后、log_message 之前，:239-241 之间）：

old_string:
```python
    def log_message(self, format: str, *args: object) -> None:
        pass


def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
```
new_string:
```python
    def do_HEAD(self) -> None:
        """P4 probe 假上游：HEAD 计数 + 可控 5xx（probe 视 status ≥ 500 为失败）。

        不回 body（HEAD 语义）；BaseHTTPRequestHandler 缺省 do_HEAD 回 501，
        会使"成功 probe"场景恒失败，故必须覆写。
        """
        if self.head_calls is not None:
            self.head_calls.append(1)
        if self.head_fail:
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:
        pass


def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
```

**Edit T2.3** — make_probe_upstream 助手（make_body_recording_upstream 之后、stop_fake_upstreams 之前，:349-352 之间）：

old_string:
```python
def stop_fake_upstreams() -> None:
```
new_string:
```python
def make_probe_upstream(head_fail: bool = False) -> tuple:
    """P4 probe 假上游：返回 (port, head_calls)；HEAD 正常 200，head_fail=True 时回 500。

    ThreadingHTTPServer：probe 每 PROBE_INTERVAL_S 一轮，测试收尾 shutdown 不得
    阻塞在 keep-alive 连接上（与 stall 变体同款理由）。
    """
    head_calls = []
    handler = type("ProbeUpstreamHandler", (FakeUpstreamHandler,),
                   {"head_calls": head_calls, "head_fail": head_fail})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1], head_calls


def stop_fake_upstreams() -> None:
```

**Edit T2.4** — 新测试类（文件尾、`if __name__ == "__main__"` 前）：

```python
class ProbeLoopTest(unittest.TestCase):
    """P4 探测线程黑盒：HEAD 计数 / 间隔加速 / 连续失败告警 / 去抖。"""

    @staticmethod
    def _wait_head_calls(calls: list, n: int, timeout: float = 8.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(calls) >= n:
                return
            time.sleep(0.05)
        raise AssertionError("probe 未在 %.1fs 内发出 %d 次 HEAD（实际 %d）"
                             % (timeout, n, len(calls)))

    def _spawn(self, head_fail: bool):
        upstream_port, head_calls = make_probe_upstream(head_fail=head_fail)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port,
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})
        return proc, head_calls

    @staticmethod
    def _teardown_proc(proc) -> None:
        proc.terminate()
        proc.wait(timeout=5)
        stderr_text(proc)
        shutil.rmtree(proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_probe_sends_head_and_success_has_no_alert(self) -> None:
        proc, head_calls = self._spawn(head_fail=False)
        try:
            self._wait_head_calls(head_calls, 3)
            time.sleep(0.3)  # 让最近一轮 probe 落状态
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            kinds = [e["kind"] for e in snap["events"]]
            self.assertNotIn("probe_alert", kinds,
                             "成功 probe 不得产告警；events=%r" % kinds)
            self.assertEqual(snap["requests_total"], 0,
                             "probe 不得计入 requests_total（零副作用）")
        finally:
            self._teardown_proc(proc)

    def test_consecutive_failures_emit_single_probe_alert(self) -> None:
        proc, head_calls = self._spawn(head_fail=True)
        try:
            self._wait_head_calls(head_calls, 3)  # 达阈值 3 → 触发告警
            deadline = time.time() + 5
            alerts = []
            snap = None
            while time.time() < deadline:
                _, body, _ = admin_get(proc.admin_port, "/api/stats")
                snap = json.loads(body.decode("utf-8"))
                alerts = [e for e in snap["events"] if e["kind"] == "probe_alert"]
                if alerts:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(snap)
            self.assertEqual(len(alerts), 1,
                             "连续 3 次 5xx 必须恰好 1 条 probe_alert；events=%r"
                             % snap["events"])
            self.assertEqual(alerts[0]["reason"], "consecutive_failures")
            # 去抖：继续累积 ≥7 次失败（0.5s 间隔），300s 窗口内不得重发同类告警
            self._wait_head_calls(head_calls, 10)
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            self.assertEqual(
                len([e for e in snap["events"] if e["kind"] == "probe_alert"]), 1,
                "PROBE_ALERT_DEBOUNCE_S 窗口内不得重发同类告警（去抖）")
        finally:
            self._teardown_proc(proc)


class ProbeNoSideEffectTest(unittest.TestCase):
    """P4 R3：probe 失败零副作用——持续 5xx 期间 STATS.requests_total 不增、
    RECENT_REQUESTS 不落、daily 不污染；客户端请求计数照常。"""

    def test_failing_probe_does_not_pollute_stats(self) -> None:
        upstream_port, head_calls = make_probe_upstream(head_fail=True)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port,
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})
        try:
            ProbeLoopTest._wait_head_calls(head_calls, 6)  # ≥3 次失败足以触发告警
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            self.assertEqual(snap["requests_total"], 0,
                             "probe 失败不得计入 requests_total（零副作用）")
            self.assertEqual(len(snap["recent"]), 0)
            self.assertEqual(sum(b.get("requests", 0)
                                 for b in snap["daily"].values()), 0,
                             "probe 不得污染 daily 桶")
            # 对照组：客户端请求照常计数（probe 不吞客户流量统计）
            post_sse(proxy_port)
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            self.assertEqual(snap["requests_total"], 1)
            self.assertEqual(len(snap["recent"]), 1)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            stderr_text(proc)
            shutil.rmtree(proc.persist_dir, ignore_errors=True)
            stop_fake_upstreams()


class ProbeUnitTest(unittest.TestCase):
    """P4 白盒：probe 核心纯函数（间隔解析 / 突增判定）。

    开关解析（resolve_probe_enabled/load_probe_enabled）属卡 3，测试在其
    ProbeEnabledResolutionTest（本卡不落，防卡 2 绿相残留红）。
    """

    def test_probe_interval_env_resolution(self) -> None:
        mod = load_proxy_module()
        f = mod._probe_interval_s_from_env
        self.assertEqual(f(""), 30.0)     # 缺省 → 默认 30
        self.assertEqual(f("0.5"), 0.5)   # 显式正数原样（测试加速 seam）
        self.assertEqual(f("15"), 15.0)
        self.assertEqual(f("0"), 10.0)    # ≤0 → 防滥用下限
        self.assertEqual(f("-3"), 10.0)
        self.assertEqual(f("abc"), 10.0)  # 非法 → 防滥用下限

    def test_probe_spike_due_matrix(self) -> None:
        mod = load_proxy_module()
        f = mod._probe_spike_due
        self.assertFalse(f([]))
        self.assertFalse(f([50.0] * 19))  # 样本不足 20 不告警
        self.assertFalse(f([50.0] * 200))  # 无突增
        # 突增：180×50 + 20×500，基线 P90=50（未污染），最近 20 P90=500 > 100
        self.assertTrue(f([50.0] * 180 + [500.0] * 20))
        # 涨幅 < 2×：60×50 + 20×120，基线 P90=120，最近 20 P90=120 ≤ 240
        self.assertFalse(f([50.0] * 60 + [120.0] * 20))


class ProbeEventsCompatTest(unittest.TestCase):
    """P4 R2：events kind 白名单——新版读 probe_alert 保留 reason；
    旧版本（3-kind 白名单）读含 probe_alert 的文件整条丢弃不崩。"""

    def test_load_events_keeps_probe_alert_with_reason(self) -> None:
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p4evt-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": 100.0, "kind": "proxy", "model": "m", "status": 502},
                {"ts": 200.0, "kind": "probe_alert", "model": None, "status": None,
                 "reason": "consecutive_failures"},
                {"ts": 300.0, "kind": "probe_alert", "model": None, "status": None,
                 "reason": "latency_spike"},
                {"ts": 400.0, "kind": "future_kind", "model": None, "status": None},
            ]}}, fh)
        events = mod.load_stats_events(path)
        self.assertEqual([e["kind"] for e in events],
                         ["proxy", "probe_alert", "probe_alert"])
        self.assertEqual(events[1]["reason"], "consecutive_failures")
        self.assertEqual(events[2]["reason"], "latency_spike")
        self.assertNotIn("reason", events[0], "非 probe_alert 事件不得带 reason")

    def test_old_whitelist_binary_drops_probe_alert_gracefully(self) -> None:
        """R2 降级演练：旧版本 3-kind 白名单 load 逻辑读新格式 → 整条丢弃不崩。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p4old-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": 100.0, "kind": "retry", "model": None, "status": None},
                {"ts": 200.0, "kind": "probe_alert", "model": None, "status": None,
                 "reason": "consecutive_failures"},
            ]}}, fh)
        # 模拟旧版本二进制：白名单写死 ("proxy", "upstream", "retry")——与 P4 前
        # load_stats_events 源码逐字一致（inline 复刻，独立 oracle）。
        data = mod._load_persist_file(path)
        raw = data["stats"]["events"]
        kept = []
        for entry in raw:
            if entry.get("kind") not in ("proxy", "upstream", "retry"):
                continue
            kept.append(entry)
        self.assertEqual([e["kind"] for e in kept], ["retry"],
                         "旧版本必须丢弃 probe_alert 且不崩（graceful degrade）")
```

验证（红：`mod._probe_interval_s_from_env` 等不存在 → AttributeError；ProbeLoopTest 无 probe → HEAD 计数 0 超时失败）：

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p4 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProbeUnitTest ProbeEventsCompatTest; echo "EXIT=$?"
```

### 生产代码（`ctyun-stream-fix-proxy.py`）

**Edit 2.1** — 常量追加（:72 之后）：

old_string:
```python
PROBE_ALERT_DEBOUNCE_S = 300       # v2 P4：同类 probe 告警最小间隔（去抖）
```
new_string:
```python
PROBE_ALERT_DEBOUNCE_S = 300       # v2 P4：同类 probe 告警最小间隔（去抖）
PROBE_LATENCY_SPIKE_FACTOR = 2.0   # v2 P4：延迟突增告警阈值倍数（最近 20 次 P90 > 基线 P90 × 此值）
PROBE_HISTORY_MAX = 20160          # v2 P4：probe 延迟历史 deque 上限（30s 间隔 × 7 天；内存态不持久化）


def _probe_interval_s_from_env(raw: str) -> float:
    """probe 间隔解析（env seam CTYUN_PROBE_INTERVAL_S，模块导入时求值一次）。

    显式正数值原样采用（测试 0.5s 加速 seam）；空串回落默认 30s；≤0 或非法值
    回落 PROBE_MIN_INTERVAL_S=10（防误配忙轮询打上游——"最小 10s 防滥用"只兜
    非法值、不钳显式合法值，否则测试加速 seam 失效，见 PLAN Anchor A1）。
    """
    if not raw.strip():
        return float(PROBE_INTERVAL_S_DEFAULT)
    try:
        value = float(raw)
    except ValueError:
        # 吞掉的是 env 里非法数值字符串：配置错误按防滥用下限回落，无其他路径可达。
        return float(PROBE_MIN_INTERVAL_S)
    if value <= 0:
        return float(PROBE_MIN_INTERVAL_S)
    return value


PROBE_INTERVAL_S = _probe_interval_s_from_env(
    os.environ.get("CTYUN_PROBE_INTERVAL_S", ""))
```

**Edit 2.2** — PROBE_LOCK 实体（注释块后、_CFG_LOCK 前，:645-646）：

old_string:
```python
#   PROBE_LOCK — P4 新增，保护 _PROBE_STATE；独立于 STATS_LOCK：探测线程与请求线程
#                共享 STATS 时只经 STATS_LOCK 短持锁更新计数器，绝不持锁做网络 IO。
_CFG_LOCK = threading.Lock()
```
new_string:
```python
#   PROBE_LOCK — P4 新增，保护 _PROBE_STATE；独立于 STATS_LOCK：探测线程与请求线程
#                共享 STATS 时只经 STATS_LOCK 短持锁更新计数器，绝不持锁做网络 IO。
_CFG_LOCK = threading.Lock()
PROBE_LOCK = threading.Lock()
```

**Edit 2.3** — PROBE_ENABLED + _PROBE_STATE（`_stats_dirty` 行后，:659 之后）：

old_string:
```python
_stats_dirty = False  # STATS_LOCK 保护：计数落盘脏标记（SIGTERM/60s 脏刷消费）
```
new_string:
```python
_stats_dirty = False  # STATS_LOCK 保护：计数落盘脏标记（SIGTERM/60s 脏刷消费）

# v2 P4：probe 主动探测状态。独立于 STATS（失败零副作用：不写 STATS 计数、
# 不触发 _record_request、不污染 histogram），仅 EVENTS 告警 append 走 STATS_LOCK。
PROBE_ENABLED = True  # _CFG_LOCK 护写；读侧无锁（bool 引用赋值原子，同 CAPTURE_ERRORS）
_PROBE_STATE = {      # PROBE_LOCK 保护（探测线程写 / probe_state_snapshot 读）
    "last_probe_ts": 0.0,           # time.time() 最近一次 probe 尝试（0=从未）
    "last_probe_ok": False,
    "last_probe_latency_ms": 0.0,
    "consecutive_failures": 0,
    "last_failure_alert_ts": 0.0,   # 连续失败告警去抖时间戳
    "last_spike_alert_ts": 0.0,     # 延迟突增告警去抖时间戳（同类去抖，两桶独立）
    "history": collections.deque(maxlen=PROBE_HISTORY_MAX),  # 成功 probe 延迟（ms）历史
}
```

**Edit 2.4** — probe 函数块（`load_stats_events` 之后、`flush_stats_if_dirty` 之前，:998 之后）：

old_string:
```python
    return out[-100:]  # 与 deque maxlen 对齐，只留最新


def flush_stats_if_dirty(path: str) -> None:
```
new_string:
```python
    return out[-100:]  # 与 deque maxlen 对齐，只留最新


def _latency_p90(samples) -> float:
    """延迟样本（ms，可迭代）的 P90（最近秩法：idx = int(0.9 * (n - 1))）。
    空序列 → 0.0。样本上限 PROBE_HISTORY_MAX，排序成本可忽略（每成功 probe 一次）。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return float(ordered[int(0.9 * (len(ordered) - 1))])


def _probe_spike_due(history, factor: float = PROBE_LATENCY_SPIKE_FACTOR) -> bool:
    """延迟突增判定：最近 20 次成功 probe 的 P90 > 全历史 P90 × factor 且样本 ≥ 20。

    history：成功 probe 延迟（ms）deque（新在尾）；len < 20 → False（样本不足不告警）。
    """
    if len(history) < 20:
        return False
    recent = list(history)[-20:]
    baseline_p90 = _latency_p90(history)
    if baseline_p90 <= 0:
        return False
    return _latency_p90(recent) > baseline_p90 * factor


def _probe_once(host_base: str) -> tuple:
    """单轮 probe：向 host_base 根 path 发 HEAD（独立连接，超时 PROBE_TIMEOUT_S）。

    返回 (ok, latency_ms)：ok = HTTP status < 500；网络异常（OSError/http.client.HTTPException）
    视为失败，latency 取整个尝试耗时。不解析响应体（spec Exclusions）。
    零副作用：不写 STATS、不触发 _record_request、不持任何锁做网络 IO。
    """
    parsed = urllib.parse.urlsplit(host_base)
    path = parsed.path.rstrip("/") or "/"
    t_start = time.monotonic()
    conn = None
    try:
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port,
                                               timeout=PROBE_TIMEOUT_S)
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port,
                                              timeout=PROBE_TIMEOUT_S)
        conn.request("HEAD", path)
        resp = conn.getresponse()
        resp.read()  # 排空（HEAD 无 body；保证连接可复用语义完整）
        ok = resp.status < 500
    except (OSError, http.client.HTTPException):
        # 吞掉的是连接失败/超时/对端 RST——probe 语义即"不可达=失败"，
        # 单条 except 已覆盖全部可预期异常，无其他路径可达。
        ok = False
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                # 吞掉的是连接关闭阶段对端已断开（close 二次清理）：probe 结果已定，
                # 关闭失败不影响成败判定，无其他路径可达。
                pass
    latency_ms = (time.monotonic() - t_start) * 1000
    return ok, latency_ms


def _probe_loop() -> None:
    """probe daemon 线程（main() 恒启动一次；开关由每轮 flag 检查控制）。

    probe_enabled=False 时空转：sleep 间隔 + 检查开关，零网络 IO 零副作用——
    运行时 POST /api/probe 切 on/off 无需管理线程生命周期（裁决见 Anchor A4）。
    每轮：_probe_once → PROBE_LOCK 内更新 _PROBE_STATE；连续失败 ≥
    PROBE_FAILURE_THRESHOLD 且距上次同类告警 ≥ PROBE_ALERT_DEBOUNCE_S → EVENTS
    追加 probe_alert（STATS_LOCK，与 _record_empty_retry 同款模式）；成功清零
    连续失败并累积延迟历史；延迟突增（_probe_spike_due）同类去抖告警。
    告警之外不碰 STATS（R3 零副作用，ProbeNoSideEffectTest 锚死）。
    """
    global _stats_dirty
    while True:
        time.sleep(PROBE_INTERVAL_S)
        if not PROBE_ENABLED:
            continue
        ok, latency_ms = _probe_once(UPSTREAM_BASE)
        alert_reason = None
        with PROBE_LOCK:
            state = _PROBE_STATE
            state["last_probe_ts"] = time.time()
            state["last_probe_ok"] = ok
            state["last_probe_latency_ms"] = round(latency_ms, 1)
            if ok:
                state["consecutive_failures"] = 0
                state["history"].append(round(latency_ms, 1))
                if _probe_spike_due(state["history"]):
                    now = time.time()
                    if now - state["last_spike_alert_ts"] >= PROBE_ALERT_DEBOUNCE_S:
                        state["last_spike_alert_ts"] = now
                        alert_reason = "latency_spike"
            else:
                state["consecutive_failures"] += 1
                if state["consecutive_failures"] >= PROBE_FAILURE_THRESHOLD:
                    now = time.time()
                    if now - state["last_failure_alert_ts"] >= PROBE_ALERT_DEBOUNCE_S:
                        state["last_failure_alert_ts"] = now
                        alert_reason = "consecutive_failures"
        if alert_reason is not None:
            with STATS_LOCK:
                EVENTS.append({"ts": time.time(), "kind": "probe_alert",
                               "model": None, "status": None,
                               "reason": alert_reason})
                _stats_dirty = True


def probe_state_snapshot() -> dict:
    """/api/health 用：spec 定死 6 键（host/last_probe_ts/last_probe_ok/
    last_probe_latency_ms/consecutive_failures/probe_enabled）。
    UPSTREAM_BASE 读侧无锁（引用赋值原子，同 _proxy 惯例）。"""
    with PROBE_LOCK:
        state = _PROBE_STATE
        snap = {
            "host": urllib.parse.urlparse(UPSTREAM_BASE).netloc,
            "last_probe_ts": state["last_probe_ts"],
            "last_probe_ok": state["last_probe_ok"],
            "last_probe_latency_ms": state["last_probe_latency_ms"],
            "consecutive_failures": state["consecutive_failures"],
            "probe_enabled": PROBE_ENABLED,
        }
    return snap


def flush_stats_if_dirty(path: str) -> None:
```

**Edit 2.5** — `load_stats_events` 白名单 + reason（:976-998 整体替换）：

old_string:
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
new_string:
```python
def load_stats_events(path: str) -> list:
    """读 stats.events（oldest→newest，≤100 条）；缺/损坏/legacy 无键 → []。

    kind 白名单含 probe_alert（v2 P4）：旧版本二进制读含 probe_alert 的文件时
    白名单不命中 → 整条 continue 丢弃（graceful degrade，R2），不会崩。
    reason 键仅 probe_alert 携带：新版本读入保留（合法 str 且 ≤200 字符），
    其余 kind 不输出该键。
    """
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
        if kind not in ("proxy", "upstream", "retry", "probe_alert"):
            continue
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts < 0:
            continue
        if model is not None and (not isinstance(model, str) or not model or len(model) > 200):
            continue
        if status is not None and (isinstance(status, bool) or not isinstance(status, int)
                                   or not 100 <= status <= 599):
            continue
        item = {"ts": ts, "kind": kind, "model": model, "status": status}
        reason = entry.get("reason")
        if kind == "probe_alert" and isinstance(reason, str) and reason \
                and len(reason) <= 200:
            item["reason"] = reason
        out.append(item)
    return out[-100:]  # 与 deque maxlen 对齐，只留最新
```

**Edit 2.6** — main() 启动 loop（`_dirty_flush_loop` 启动后，:2595-2596）：

old_string:
```python
    threading.Thread(target=_dirty_flush_loop, daemon=True,
                     name="stats-flush").start()
```
new_string:
```python
    threading.Thread(target=_dirty_flush_loop, daemon=True,
                     name="stats-flush").start()

    # v2 P4：probe daemon（恒启动一次；enabled=False 时空转，见 _probe_loop docstring）
    threading.Thread(target=_probe_loop, daemon=True,
                     name="upstream-probe").start()
```

### 验证（绿）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p4 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProbeLoopTest ProbeNoSideEffectTest ProbeUnitTest ProbeEventsCompatTest; echo "EXIT=$?"
```

回归（events/快照/常量既有锚不得红）：

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_events_record_and_cap ProxyDashboardUnitTest.test_events_persist_roundtrip ProxyDashboardUnitTest.test_stats_snapshot_shape P1ConstantsTest; echo "EXIT=$?"
```

---

## 卡 3（tier A）— `/api/health` + `POST /api/probe` + persist/env 开关

**目标**：`resolve_probe_enabled`/`load_probe_enabled`/`set_probe_enabled`；`save_stats_counters` dump 加 `probe_enabled`；AdminHandler `GET /api/health` + `POST /api/probe`（鉴权同 /api/config）；main() 启动时解析 `PROBE_ENABLED`（env > persist > 默认）。

### TDD 红相（文件尾追加）

```python
class ProbeHealthApiTest(unittest.TestCase):
    """P4：GET /api/health 形状与真实值（probe 运转后回读）。"""

    def setUp(self) -> None:
        self.upstream_port, self.head_calls = make_probe_upstream()
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})

    def tearDown(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_health_reports_probe_state(self) -> None:
        ProbeLoopTest._wait_head_calls(self.head_calls, 2)
        deadline = time.time() + 5
        snap = None
        while time.time() < deadline:
            status, body, _ = admin_get(self.proc.admin_port, "/api/health")
            snap = json.loads(body.decode("utf-8"))
            if snap["upstream"]["last_probe_ok"]:
                break
            time.sleep(0.1)
        self.assertEqual(status, 200)
        self.assertIsNotNone(snap)
        upstream = snap["upstream"]
        self.assertEqual(set(upstream),
                         {"host", "last_probe_ts", "last_probe_ok",
                          "last_probe_latency_ms", "consecutive_failures",
                          "probe_enabled"})
        self.assertEqual(upstream["host"], "127.0.0.1:%d" % self.upstream_port)
        self.assertTrue(upstream["last_probe_ok"])
        self.assertGreater(upstream["last_probe_ts"], 0)
        self.assertIsInstance(upstream["last_probe_latency_ms"], float)
        self.assertEqual(upstream["consecutive_failures"], 0)
        self.assertTrue(upstream["probe_enabled"])


class ProbeToggleTest(unittest.TestCase):
    """P4：POST /api/probe 切 on/off（运行时生效、持久化、输入校验、启动空转）。"""

    def setUp(self) -> None:
        self.upstream_port, self.head_calls = make_probe_upstream()
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})

    def tearDown(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_probe_toggle_off_stops_head_requests(self) -> None:
        ProbeLoopTest._wait_head_calls(self.head_calls, 2)  # 确认运行中
        status, body = admin_post(self.proc.admin_port, "/api/probe",
                                  b'{"enabled": false}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")),
                         {"ok": True, "probe_enabled": False})
        _, body, _ = admin_get(self.proc.admin_port, "/api/health")
        self.assertFalse(json.loads(body.decode("utf-8"))["upstream"]["probe_enabled"])
        time.sleep(1.5)  # >2 个间隔：空转不得再发 HEAD
        count_off = len(self.head_calls)
        time.sleep(1.5)
        self.assertEqual(len(self.head_calls), count_off,
                         "probe off 后不得再发 HEAD（空转零网络 IO）")
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertFalse(json.load(fh)["probe_enabled"],
                             "persist 顶层 probe_enabled 必须落 false")

    def test_probe_toggle_back_on_resumes(self) -> None:
        ProbeLoopTest._wait_head_calls(self.head_calls, 2)
        admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": false}')
        time.sleep(1.0)
        stopped = len(self.head_calls)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": true}')
        self.assertEqual(status, 200)
        ProbeLoopTest._wait_head_calls(self.head_calls, stopped + 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/health")
        self.assertTrue(json.loads(body.decode("utf-8"))["upstream"]["probe_enabled"])

    def test_probe_post_rejects_bad_input(self) -> None:
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b"not json")
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": "yes"}')
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": 1}')
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"nope": true}')
        self.assertEqual(status, 400)

    def test_seed_persist_probe_disabled_starts_idle(self) -> None:
        """seed_persist probe_enabled=false → 启动即空转（零 HEAD，health 零值）。"""
        up_port, up_head_calls = make_probe_upstream()
        proc = start_proxy(up_port, free_port(),
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"},
                           seed_persist={"upstream_base":
                                         "http://127.0.0.1:%d" % up_port,
                                         "probe_enabled": False})
        try:
            time.sleep(1.5)  # >2 个间隔
            self.assertEqual(len(up_head_calls), 0,
                             "probe_enabled=false 启动后不得发 HEAD（空转不触网）")
            _, body, _ = admin_get(proc.admin_port, "/api/health")
            upstream = json.loads(body.decode("utf-8"))["upstream"]
            self.assertFalse(upstream["probe_enabled"])
            self.assertEqual(upstream["last_probe_ts"], 0)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            stderr_text(proc)
            shutil.rmtree(proc.persist_dir, ignore_errors=True)
            stop_fake_upstreams()

    def test_env_probe_disabled_overrides_persist_enabled(self) -> None:
        """CTYUN_PROBE_ENABLED=0 优先于 persist true（env > persist > 默认）。"""
        up_port, up_head_calls = make_probe_upstream()
        proc = start_proxy(up_port, free_port(),
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5",
                                      "CTYUN_PROBE_ENABLED": "0"},
                           seed_persist={"upstream_base":
                                         "http://127.0.0.1:%d" % up_port,
                                         "probe_enabled": True})
        try:
            time.sleep(1.5)
            self.assertEqual(len(up_head_calls), 0)
            _, body, _ = admin_get(proc.admin_port, "/api/health")
            self.assertFalse(json.loads(body.decode("utf-8"))["upstream"]["probe_enabled"])
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            stderr_text(proc)
            shutil.rmtree(proc.persist_dir, ignore_errors=True)
            stop_fake_upstreams()
```

此外，`ProbeEnabledResolutionTest`（白盒，`resolve_probe_enabled`/`load_probe_enabled` 纯函数）与卡 3 生产代码一同落地：

```python
class ProbeEnabledResolutionTest(unittest.TestCase):
    """P4 卡 3 白盒：probe 开关解析/env seam/persist 读取（纯函数）。"""

    def test_resolve_probe_enabled_matrix(self) -> None:
        mod = load_proxy_module()
        f = mod.resolve_probe_enabled
        self.assertTrue(f("1", False))
        self.assertTrue(f("true", False))
        self.assertTrue(f(" YES ", False))
        self.assertTrue(f("on", False))
        self.assertFalse(f("0", True))
        self.assertFalse(f("false", True))
        self.assertFalse(f("OFF", True))
        self.assertFalse(f("no", True))
        self.assertTrue(f("", True))        # env 缺省 → persist
        self.assertFalse(f("", False))      # env 缺省 → persist
        self.assertTrue(f("banana", True))  # env 非法 → persist（不回默认）
        self.assertFalse(f("banana", False))

    def test_load_probe_enabled_defaults(self) -> None:
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p4en-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        self.assertTrue(mod.load_probe_enabled(path))  # 缺文件 → 默认开
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"probe_enabled": False}, fh)
        self.assertFalse(mod.load_probe_enabled(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"probe_enabled": "yes"}, fh)  # 非 bool → 默认开
        self.assertTrue(mod.load_probe_enabled(path))
```

验证（红：`/api/health` 404、`/api/probe` 404、`mod.resolve_probe_enabled` AttributeError）：

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p4 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProbeHealthApiTest ProbeToggleTest ProbeEnabledResolutionTest; echo "EXIT=$?"
```

### 生产代码（`ctyun-stream-fix-proxy.py`）

**Edit 3.1** — 开关解析/装载/落库（`load_model_pricing` 之后，:906 之后）：

old_string:
```python
def load_model_pricing(path: str) -> dict:
    """从持久化文件顶层读 model_pricing；缺/损坏/非 dict → {}。
    env seam CTYUN_MODEL_PRICING（JSON 字符串）优先级更高，由 main() 覆盖。"""
    data = _load_persist_file(path)
    val = data.get("model_pricing") if isinstance(data, dict) else None
    return val if isinstance(val, dict) else {}
```
new_string:
```python
def load_model_pricing(path: str) -> dict:
    """从持久化文件顶层读 model_pricing；缺/损坏/非 dict → {}。
    env seam CTYUN_MODEL_PRICING（JSON 字符串）优先级更高，由 main() 覆盖。"""
    data = _load_persist_file(path)
    val = data.get("model_pricing") if isinstance(data, dict) else None
    return val if isinstance(val, dict) else {}


def resolve_probe_enabled(env_val: str, persisted: bool) -> bool:
    """probe_enabled 解析：env > persist > 默认（True）。

    env_val（CTYUN_PROBE_ENABLED）："1"/"true"/"yes"/"on" → True；
    "0"/"false"/"no"/"off" → False；其余（含空串/垃圾值）→ 落 persisted 值。
    """
    if env_val:
        normalized = env_val.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return persisted


def load_probe_enabled(path: str) -> bool:
    """从持久化文件顶层读 probe_enabled；缺/损坏/非 bool → True（默认开）。"""
    data = _load_persist_file(path)
    val = data.get("probe_enabled") if isinstance(data, dict) else None
    return val if isinstance(val, bool) else True


def set_probe_enabled(enabled: bool) -> None:
    """POST /api/probe 落库：切 PROBE_ENABLED + 立即全量落盘（跨重启保留）。"""
    global PROBE_ENABLED
    with _CFG_LOCK:
        PROBE_ENABLED = enabled
    # save_stats_counters 内部也要拿 _CFG_LOCK：必须在锁外调用（同 set_capture_errors
    # 死锁注释——非重入锁，嵌套即同线程死锁）
    save_stats_counters(PERSIST_PATH)
```

**Edit 3.2** — `save_stats_counters` 读开关（:855-857）：

old_string:
```python
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
```
new_string:
```python
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
        probe_enabled = PROBE_ENABLED
```

**Edit 3.3** — `save_stats_counters` dump 加键（:862-868）：

old_string:
```python
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "model_pricing": MODEL_PRICING,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
```
new_string:
```python
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "probe_enabled": probe_enabled,
                   "model_pricing": MODEL_PRICING,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
```

**Edit 3.4** — do_GET 加 `/api/health`（`/api/config` 分支之后、`/api/errors` 之前，:1833-1834 之间）：

old_string:
```python
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS,
                           "model_pricing": MODEL_PRICING}
            self._send_json(200, payload)
        elif path == "/api/errors":
```
new_string:
```python
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS,
                           "model_pricing": MODEL_PRICING}
            self._send_json(200, payload)
        elif path == "/api/health":
            self._send_json(200, {"upstream": probe_state_snapshot()})
        elif path == "/api/errors":
```

**Edit 3.5** — do_POST 分流 `/api/probe`（:1908-1917）：

old_string:
```python
        path = urllib.parse.urlsplit(self.path).path
        if path != "/api/config":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        if not write_allowed(self.client_address[0],
                             self.headers.get("X-Admin-Token") or "",
                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            self._send_json(403, {"error": "非本机修改上游端点需要 X-Admin-Token 头"
                                           "（值 = 服务器环境变量 CTYUN_ADMIN_TOKEN）"})
            return
```
new_string:
```python
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/probe":
            self._handle_probe_post(raw)
            return
        if path != "/api/config":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        if not write_allowed(self.client_address[0],
                             self.headers.get("X-Admin-Token") or "",
                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            self._send_json(403, {"error": "非本机修改上游端点需要 X-Admin-Token 头"
                                           "（值 = 服务器环境变量 CTYUN_ADMIN_TOKEN）"})
            return
```

**Edit 3.6** — `_handle_probe_post` 方法（do_POST 之后、`_send` 之前，:1941-1943 之间）：

old_string:
```python
        self._send_json(200, resp)

    def _send(self, status: int, content_type: str, body: bytes) -> None:
```
new_string:
```python
        self._send_json(200, resp)

    def _handle_probe_post(self, raw: bytes) -> None:
        """POST /api/probe：切 probe 开关 {"enabled": bool}（鉴权同 /api/config）。"""
        if not write_allowed(self.client_address[0],
                             self.headers.get("X-Admin-Token") or "",
                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            self._send_json(403, {"error": "非本机切换 probe 开关需要 X-Admin-Token 头"
                                           "（值 = 服务器环境变量 CTYUN_ADMIN_TOKEN）"})
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体不是合法 JSON——"
                                           "请发 {\"enabled\": true|false}"})
            return
        enabled = data.get("enabled") if isinstance(data, dict) else None
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "enabled 必须为布尔值"})
            return
        set_probe_enabled(enabled)
        self._send_json(200, {"ok": True, "probe_enabled": enabled})

    def _send(self, status: int, content_type: str, body: bytes) -> None:
```

**Edit 3.7** — main() 启动解析（:2533-2536）：

old_string:
```python
def main() -> None:
    global UPSTREAM_BASE, _upstream_source, _stats_dirty, CAPTURE_ERRORS, MODEL_PRICING
    UPSTREAM_BASE, _upstream_source = resolve_upstream_base(
        os.environ.get("CTYUN_UPSTREAM_BASE"), PERSIST_PATH)
```
new_string:
```python
def main() -> None:
    global UPSTREAM_BASE, _upstream_source, _stats_dirty, CAPTURE_ERRORS, MODEL_PRICING
    global PROBE_ENABLED
    UPSTREAM_BASE, _upstream_source = resolve_upstream_base(
        os.environ.get("CTYUN_UPSTREAM_BASE"), PERSIST_PATH)
    # v2 P4：probe 开关解析（env > persist > 默认 True），_probe_loop 启动前定值
    PROBE_ENABLED = resolve_probe_enabled(
        os.environ.get("CTYUN_PROBE_ENABLED", ""), load_probe_enabled(PERSIST_PATH))
```

### 验证（绿）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p4 && /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProbeHealthApiTest ProbeToggleTest ProbeEnabledResolutionTest; echo "EXIT=$?"
```

回归（config POST 分流/持久化/快照不得红）：

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest ProxyDashboardUnitTest.test_stats_persist_roundtrip_and_defaults ModelPricingSeamTest; echo "EXIT=$?"
```

---

## 卡 4（tier B）— README 同步（无 TDD 豁免：文档）

**目标**：API 节两行（`/api/health`、`POST /api/probe`）+ env 表一行（`CTYUN_PROBE_INTERVAL_S`）。`CTYUN_PROBE_ENABLED` 在 `/api/probe` 行文内说明（不另开 env 行——主代理 brief 限定 env 表恰一行）。

**Edit 4.1** — env 表（:59 后）：

old_string:
```markdown
| `CTYUN_MODEL_PRICING` | 空 | 模型价目表 JSON 字符串（可选 cost 估算数据源）；仅落 persist 顶层 `model_pricing` schema，不实现任何计费 UI |
```
new_string:
```markdown
| `CTYUN_MODEL_PRICING` | 空 | 模型价目表 JSON 字符串（可选 cost 估算数据源）；仅落 persist 顶层 `model_pricing` schema，不实现任何计费 UI |
| `CTYUN_PROBE_INTERVAL_S` | `30` | 主动探测 HEAD 间隔（秒）；显式正数原样采用（测试加速 seam），≤0/非法值回落 `10` |
```

**Edit 4.2** — API 节两行（:67 后）：

old_string:
```markdown
- `POST /api/config` `{"upstream_base": "..."}` — 热切上游（本机免鉴权，LAN 需 `X-Admin-Token`）
```
new_string:
```markdown
- `POST /api/config` `{"upstream_base": "..."}` — 热切上游（本机免鉴权，LAN 需 `X-Admin-Token`）
- `GET /api/health` — 上游健康（最近 probe 时间/延迟/连续失败/开关 `probe_enabled`）
- `POST /api/probe` `{"enabled": true|false}` — 切主动探测开关（本机免鉴权，LAN 需 `X-Admin-Token`；env `CTYUN_PROBE_ENABLED`（`1/true` 开、`0/false` 关）优先，否则持久化顶层 `probe_enabled`，缺省开）
```

### 验证

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/observability-v2-p4 && grep -n 'api/health\|api/probe\|CTYUN_PROBE_INTERVAL_S\|CTYUN_PROBE_ENABLED' README.md; echo "EXIT=$?"
```

终验（whole-branch 全量 + baseline 对照）：

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py; echo "EXIT=$?"
```
预期：`Ran 189+20=209 tests ... OK`（新增：卡1×5 + 卡2×7 + 卡3×8 = 20 用例——精确新增数以实际 Ran 数为准，只许增不许减）。

---

## 卡清单汇总

| 卡 | tier | 生产改动 | 测试改动 | 新增用例 |
|----|------|----------|----------|----------|
| 1 | A | `_Outcome` 5 字段；`classify_outcome` 重写；`_record_request` 双形态归一 + 3 处落桶/存储；5 个调用点传 namedtuple | `ClassifyOutcomeTriStateTest`（5） | 5 |
| 2 | A | probe 常量+`PROBE_INTERVAL_S` 解析；`PROBE_LOCK`/`_PROBE_STATE`/`PROBE_ENABLED`；`_latency_p90`/`_probe_spike_due`/`_probe_once`/`_probe_loop`/`probe_state_snapshot`；`load_stats_events` 白名单+reason；main() 启线程 | FakeUpstreamHandler.do_HEAD + `make_probe_upstream`；`ProbeLoopTest`（2）/`ProbeNoSideEffectTest`（1）/`ProbeUnitTest`（2）/`ProbeEventsCompatTest`（2） | 7 |
| 3 | A | `resolve_probe_enabled`/`load_probe_enabled`/`set_probe_enabled`；`save_stats_counters` +probe_enabled；do_GET `/api/health`；do_POST `/api/probe` 分流 + `_handle_probe_post`；main() 解析开关 | `ProbeHealthApiTest`（1）/`ProbeToggleTest`（5）/`ProbeEnabledResolutionTest`（2） | 8 |
| 4 | B | README env 表 1 行 + API 节 2 行 | 无 | 0 |

---

## Spec 冲突裁决汇总（已在 Anchor Reconciliation 展开）

1. **A1 间隔下限 vs 测试加速**：显式正数 env 原样采用；≤0/非法回落 10s。防滥用只兜非法配置。
2. **A2 `_outcome_tri_state` 保留或收编**：保留纯函数（P3 测试锁死 + 字符串回退路径）；`_Outcome` 字段值由它推导。
3. **A4 禁用时空转 vs 不启动**：裁决空转（恒启动单 daemon，flag 检查零 IO），运行时 on/off 免线程生命周期管理。
4. **A5 7 天基线存储**：内存 deque（20160 上限）只记成功 probe，重启清零；不持久化（失败零副作用 + R3 简洁性）。
5. **A8 CTYUN_PROBE_ENABLED 文档位置**：env 表只加 brief 要求的一行（CTYUN_PROBE_INTERVAL_S），新 env 在 `/api/probe` API 行文内说明。

---

## 自查 5 项（Plan Self-Review）

1. **spec coverage**：P4 节全部落地——tri_state 归因（卡1）、probe 循环/间隔/超时/阈值/去抖/突增（卡2）、/api/health + POST /api/probe + persist probe_enabled + env 优先（卡3）、events 白名单 + R2 降级测试（卡2）、README（卡4）。P5 零进入。
2. **placeholder scan**：每卡 Edit 均含精确 old/new_string 与完整测试类代码；无「TODO/按现状实现/同现有风格」类指令残留。
3. **type consistency**：`_Outcome` 5 字段全构造点（classify_outcome 7 分支）逐一核对；`_record_request` 双形态归一后 `entry_dm`/`bucket`/`RECENT_REQUESTS` 三处引用一致性核对；`probe_state_snapshot` 返回键与 spec 6 键逐字一致；EVENTS 条目 schema（ts/kind/model/status/reason）与 `load_stats_events` 输出键一致。
4. **可落盘性**：全部 old_string 取自 worktree 实测内容（含缩进/换行）；9 处生产 Edit + 4 处测试基建 Edit 锚点唯一性核对（重复文本已用上下文区分：卡1 两处 502 以缩进区分、SSE/buffered 以 stream=1/0 区分）。
5. **锚点实测**：PROBE_LOCK 仅注释（grep 证）、`_PROBE_STATE`/`probe_enabled`/`PROBE_LATENCY_SPIKE_FACTOR` 零命中（grep 证）、`load_stats_events` 白名单 :986 现为 3 kind、`classify_outcome` :272-300 现为 4 字段、main() :2533 现无 probe 线程、do_GET :1818 无 health、do_POST :1909 只放行 /api/config、baseline 189 绿实测。全部与 spec 锚点一致，无「锚点失配需回 architect」项。
