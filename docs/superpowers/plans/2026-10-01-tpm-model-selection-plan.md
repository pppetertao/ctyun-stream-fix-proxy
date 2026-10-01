# PLAN: TPM 限流模型选择 + 设置页（代码级执行计划）

**spec:** `docs/superpowers/specs/2026-10-01-tpm-model-selection-design.md`
**worktree:** `.worktrees/tpm-model-selection`（base bd2554f，spec 锚点基于 b1de8b3，全部行号已按盘上实测校正）
**baseline:** `python3 ctyun-stream-fix-proxy.test.py` = 219 例全绿（主代理于 bd2554f 实测）
**phase:** 完整 PLAN（4 卡全 A/B，单次 dispatch 落盘；每卡含完整生产代码块 + 完整测试代码块 + 验证命令 + commit message，No Placeholders）

---

## Global Constraints

1. **Language / runtime.** 单文件 Python 3 代理 + 单文件 unittest。不加新依赖，不改 `_log`、`classify_outcome` 签名。
2. **验证命令（每卡必跑）.** `python3 ctyun-stream-fix-proxy.test.py`（全量，219 + 新增）；baseline 全绿在先，卡后全绿 = 无回归。
3. **Tier.** 全卡 tier A——所有代码块在 PLAN 阶段写死（纯函数 + in-process import 单测 + socket 级集成测试 + tempfile 持久化均可离线验证），无运行时留白。
4. **TDD 顺序（每卡内）.** 先写失败测试（改存量测试先按新语义改断言行）→ 跑测试证红 → 写生产代码 → 跑测试证绿 → 全量回归 → commit。每卡独立 commit。
5. **测试确定性.** 单测复用 `load_proxy_module()`（:595）；TPM 全局态清理复用 `_tpm_cleanup(mod)`（:1035，本计划扩展为含 `TPM_MODEL_BUDGETS` 重置）；集成测试复用 `make_scripted_upstream`（:318）/ `start_proxy(extra_env, seed_persist)`（:448）/ `admin_get`（:607）/ `admin_post`（:619）/ `post_sse_auth`（:631）/ `stderr_text`（:403）/ `free_port`（:429）/ `stop_fake_upstreams`。持久化全部走 `start_proxy` 内置 `tempfile.mkdtemp`（:458），不触真实 ~/.local/etc。
6. **卡序与依赖.** 4 卡串行执行：卡 1（运行时状态+准入+迁移 seed）→ 卡 2（persist 写键+setter+推荐纯函数组）→ 卡 3（HTTP API+集成测试类）→ 卡 4（Dashboard v1.5 UI+HTML 断言）。每卡结束必须全量绿才能 commit 并进下一卡。
7. **漂移对齐声明（锚点实测 2026-10-01，盘上 bd2554f）.** spec 锚点 → 本计划实测行号：常量区 :100-112 → :101-112；tpm_limit_for :420 → :455；tpm_admit :480 → :515；tpm_settle :550 → :585；tpm_snapshot :570/593/604 → :605/:639/:640-641；logs_snapshot 之后插入点 :669 → :742（logs_snapshot 尾）与 :745（_EmptyStream 之前）；persist_upstream :792 → :841；save_stats_counters :929 → :978；load_capture_errors :980 → :1031（其后插 load_tpm_model_budgets）；set_capture_errors :1122 → :1340（其后插 set_tpm_model_budgets）；do_GET :1912-1918 → :2142-2149；do_POST :1987-2023 → :2219-2262；main() :2627 → :2875；dashboard 上游 section :2144 → :2391-2400；JS poll() :2600 区域 → :2855-2857（applyRange/poll/setInterval 前插入）。语义不变，仅行号漂移。
8. **DASHBOARD_HTML 改动集中成单块.** 并行会话 P5（dashboard 重构）与本 episode 均动 `_DASHBOARD_SRC`；本计划卡 4 的 HTML/JS 改动全部集中在上游 section（:2400）之后与 `<script>` 尾（:2857）之前两处单块插入，降低 rebase 冲突面。
9. **版本语义口径.** `TPM_LIMIT_BY_MODEL` env 常量保留解析（`_parse_tpm_limit_by_model` 原样），仅作一次性迁移 seed；`_TPM_LIMIT_BY_MODEL_BUILTIN` 移除；运行时唯一权威 = `TPM_MODEL_BUDGETS`（模块态 dict，`_CFG_LOCK` 护写，读侧无锁引用原子）。

---

## Task Cards

### Card 1: opt-in 运行时状态与准入 + 迁移 seed 回填 + 存量测试对齐

**tier:** A
**TDD order:** 1
**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
**Commit:** `feat(proxy): opt-in per-model TPM budgets with migration seed`

**改动总览（生产）:**
1. 常量区：删 `_TPM_LIMIT_BY_MODEL_BUILTIN`，新增 `TPM_MODEL_BUDGETS: dict = {}`；`_parse_tpm_limit_by_model` docstring 更新。
2. `tpm_limit_for` 两级回退：`TPM_MODEL_BUDGETS.get(model)` → `TPM_LIMIT`。
3. `tpm_admit` 函数体首行（`budget = tpm_limit_for(model)` 之前）加 opt-in 短路。
4. `tpm_settle` 仅 docstring 补句（签名不动）。
5. `tpm_snapshot` config：`limit_by_model` → `model_budgets`（`dict(TPM_MODEL_BUDGETS)`）+ 新增 `enabled_models`。
6. 新增 `load_tpm_model_budgets(path)`（`load_capture_errors` 之后）。
7. `main()` 启动迁移 seed（`CAPTURE_ERRORS = load_capture_errors(...)` 之后）。

**改动总览（测试）:** 新增 5 个单测；迁移/对齐存量测试 10 处（含 `_tpm_cleanup` 扩展与集成测试 seed）。

---

**生产代码块 1-1：常量区（替换现 :108-112）**

现盘上（:108-112）：
```python
# per-model 预算（决策 2）：env seam "model:limit,model:limit"（逗号分隔）；
# 解析 fail-open 见 _parse_tpm_limit_by_model；内置默认不并入本表以保快照原样展示。
TPM_LIMIT_BY_MODEL: dict = _parse_tpm_limit_by_model(
    os.environ.get("CTYUN_TPM_LIMIT_BY_MODEL", ""))
_TPM_LIMIT_BY_MODEL_BUILTIN: dict = {"kimi-k3-oc": 30000}  # 实证 31k 触顶留余量；env 表优先覆盖
```

替换为：
```python
# per-model 预算 env seam（决策 2）：仅作一次性迁移 seed 用（main() 启动时解析并落盘
# tpm_model_budgets）；运行时限流预算唯一权威 = TPM_MODEL_BUDGETS。
TPM_LIMIT_BY_MODEL: dict = _parse_tpm_limit_by_model(
    os.environ.get("CTYUN_TPM_LIMIT_BY_MODEL", ""))
# 启用模型集合（决策 2）：{model: int budget}，_CFG_LOCK 护写（读侧无锁靠引用赋值原子）。
# 仅集合内模型走 tpm_admit 预算判定（opt-in）；main() 启动由持久化 / env 迁移 / 默认 seed 回填。
TPM_MODEL_BUDGETS: dict = {}
```

**生产代码块 1-2：`_parse_tpm_limit_by_model` docstring 尾段（现 :72-73）**

现盘上：
```python
    fail-open：逐项 split(":")，非两项 / limit 非 int / limit 负值 → 跳过该项并
    stderr 一行；空串 / 全畸形 → {}。内置默认（kimi-k3-oc:30000）不并入本表，
    由 tpm_limit_for 兜底——保证快照 config.limit_by_model 原样展示 env 表。
```

替换为：
```python
    fail-open：逐项 split(":")，非两项 / limit 非 int / limit 负值 → 跳过该项并
    stderr 一行；空串 / 全畸形 → {}。本表仅供 main() 一次性迁移 seed
    （tpm_model_budgets 持久化缺失且 env 非空时），运行时限流不消费它。
```

**生产代码块 1-3：`tpm_limit_for`（替换现 :455-466）**

```python
def tpm_limit_for(model) -> int:
    """per-model 预算查询（两级回退）：TPM_MODEL_BUDGETS → 全局 TPM_LIMIT。

    仅启用模型（TPM_MODEL_BUDGETS 键内）会消费本函数——tpm_admit 已按 opt-in
    短路，未启用模型不走到 budget 判定；model 为 None / 空字符串 / 查不到 →
    回退 TPM_LIMIT（全局默认）。"""
    if model:
        limit = TPM_MODEL_BUDGETS.get(model)
        if limit is not None:
            return limit
    return TPM_LIMIT
```

**生产代码块 1-4：`tpm_admit` docstring 规则行 + 短路（现 :521、:533）**

现盘上 :521：
```python
    - budget = tpm_limit_for(model)（per-model 表 → 内置默认 → TPM_LIMIT）。
```

替换为：
```python
    - 未启用模型（不在 TPM_MODEL_BUDGETS）→ 直接直通 ("ok", 0)，不建桶/不排队/不 429。
    - budget = tpm_limit_for(model)（TPM_MODEL_BUDGETS → TPM_LIMIT）。
```

现盘上 :533：
```python
    budget = tpm_limit_for(model)
    started = time.time()
```

替换为：
```python
    # opt-in 短路（决策 1）：未启用模型直通——不建桶、不排队、不 429、不 touch rejected。
    # model 为 None 时 `None not in {...}` 恒真 → 直通（无 key 请求由上游调用点过滤，天然符合）。
    if model not in TPM_MODEL_BUDGETS:
        return ("ok", 0)
    budget = tpm_limit_for(model)
    started = time.time()
```

**生产代码块 1-5：`tpm_settle` docstring（现 :586-591，仅 docstring 加句）**

现盘上：
```python
    """结算：以实际 usage 校正窗口占用。

    prune 后追加校正条目 (now, delta)，delta = actual - est（可为负 = 退款）；
    used 存原始值 used + delta，与追加条目自此保持一致（可为负 = 窗口欠账，
    随条目过期 prune 自愈）。settle 后 notify_all 唤醒排队 waiter 重试准入。
    """
```

替换为：
```python
    """结算：以实际 usage 校正窗口占用。

    prune 后追加校正条目 (now, delta)，delta = actual - est（可为负 = 退款）；
    used 存原始值 used + delta，与追加条目自此保持一致（可为负 = 窗口欠账，
    随条目过期 prune 自愈）。settle 后 notify_all 唤醒排队 waiter 重试准入。
    未启用模型桶不存在 → 现有 `bucket is None` no-op 分支命中，直通模型天然免疫；
    模型从启用→取消遗留的桶继续校正并 notify，在途 waiter 走完当次判定（决策 6）。
    """
```

**生产代码块 1-6：`tpm_snapshot` config（替换现 :634-641 的 config dict；docstring :608 同步）**

现盘上 :608：
```python
    返回 {"config": {"limit", "window_s", "queue_max", "limit_by_model"},
```

替换为：
```python
    返回 {"config": {"limit", "window_s", "queue_max", "model_budgets", "enabled_models"},
```

现盘上 :635-640：
```python
        return {
            "config": {
                "limit": TPM_LIMIT,
                "window_s": TPM_WINDOW_S,
                "queue_max": TPM_QUEUE_MAX,
                "limit_by_model": TPM_LIMIT_BY_MODEL,
            },
```

替换为：
```python
        return {
            "config": {
                "limit": TPM_LIMIT,
                "window_s": TPM_WINDOW_S,
                "queue_max": TPM_QUEUE_MAX,
                "model_budgets": dict(TPM_MODEL_BUDGETS),
                "enabled_models": sorted(TPM_MODEL_BUDGETS.keys()),
            },
```

**生产代码块 1-7：新增 `load_tpm_model_budgets`（插在 `load_capture_errors` 之后，现 :1036 与 `load_model_pricing` 之间）**

```python
def load_tpm_model_budgets(path: str) -> dict:
    """从持久化文件读 tpm_model_budgets；缺/损坏/非 dict → {}。

    逐键校验（容错同 load_daily_by_model_buckets 风格）：model 名非空 str
    且 ≤200 字符、limit 为 int 且 >0、键数 ≤32；非法项跳过不报错。
    """
    data = _load_persist_file(path)
    val = data.get("tpm_model_budgets") if isinstance(data, dict) else None
    if not isinstance(val, dict):
        return {}
    out = {}
    for model, limit in val.items():
        if len(out) >= 32:
            break
        if not isinstance(model, str) or not model or len(model) > 200:
            continue
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            continue
        out[model] = limit
    return out
```

**生产代码块 1-8：`main()` 迁移 seed（插在现 :2887 `CAPTURE_ERRORS = load_capture_errors(PERSIST_PATH)` 之后、:2888 model_pricing 注释之前）**

现盘上 :2876-2877 的 global 行：
```python
    global UPSTREAM_BASE, _upstream_source, _stats_dirty, CAPTURE_ERRORS, MODEL_PRICING
    global PROBE_ENABLED
```

替换为：
```python
    global UPSTREAM_BASE, _upstream_source, _stats_dirty, CAPTURE_ERRORS, MODEL_PRICING
    global PROBE_ENABLED, TPM_MODEL_BUDGETS
```

现盘上 :2887：
```python
    CAPTURE_ERRORS = load_capture_errors(PERSIST_PATH)  # 启动时回填开关
```

替换为（在其后插入迁移块）：
```python
    CAPTURE_ERRORS = load_capture_errors(PERSIST_PATH)  # 启动时回填开关
    # 迁移 seed（决策 2）：持久化 tpm_model_budgets 为运行时唯一权威；
    # 键**缺失**（不是空 dict——用户显式保存 {} = 全部直通，必须尊重）且
    # env CTYUN_TPM_LIMIT_BY_MODEL 非空 → env 解析 seed 一次并立即落盘；
    # 两者皆无 → 默认 {"kimi-k3-oc": 30000}（生产实证线，31k 触顶留余量）。
    persist_data = _load_persist_file(PERSIST_PATH)
    if "tpm_model_budgets" in persist_data:
        TPM_MODEL_BUDGETS = load_tpm_model_budgets(PERSIST_PATH)
    else:
        env_budgets = TPM_LIMIT_BY_MODEL
        if env_budgets:
            TPM_MODEL_BUDGETS = dict(env_budgets)
            _safe_log_stderr("ctyun-stream-fix-proxy: migrated CTYUN_TPM_LIMIT_BY_MODEL"
                             " to tpm_model_budgets: %r" % (TPM_MODEL_BUDGETS,))
            save_stats_counters(PERSIST_PATH)
        else:
            TPM_MODEL_BUDGETS = {"kimi-k3-oc": 30000}
```

> 注：`save_stats_counters` 此刻尚未带 `tpm_model_budgets` 键（卡 2 加）——迁移落盘需卡 2 完成后才在持久化文件可见；卡 1 期间 `/api/tpm_stats` 的 `config.model_budgets` 已反映内存态。卡 3 的 env 迁移集成测试（断言 persist 文件已落键）排在卡 2 之后，无时序矛盾。

---

**测试代码块 1-A：`_tpm_cleanup` 扩展（现 :1035-1039）**

```python
    def _tpm_cleanup(self, mod):
        mod.TPM_BUCKETS.clear()
        mod.TPM_WAITERS.clear()
        mod._tpm_rejected.clear()
        mod._tpm_timeouts.clear()
        mod.TPM_MODEL_BUDGETS.clear()
```

**测试代码块 1-B：常量断言迁移（现 :952）**

现盘上：
```python
        self.assertEqual(mod._TPM_LIMIT_BY_MODEL_BUILTIN, {"kimi-k3-oc": 30000})
        self.assertIsInstance(mod.TPM_LIMIT_BY_MODEL, dict)
```

替换为：
```python
        self.assertIsInstance(mod.TPM_LIMIT_BY_MODEL, dict)
        self.assertEqual(mod.TPM_MODEL_BUDGETS, {})
```

**测试代码块 1-C：`test_tpm_limit_for_fallback_and_env_override` 重写（替换现 :1015-1033 整段）**

```python
    def test_tpm_limit_for_two_level_fallback(self) -> None:
        """两级回退：TPM_MODEL_BUDGETS → 全局 TPM_LIMIT（env 表不参与运行时）。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        try:
            mod.TPM_MODEL_BUDGETS.clear()
            self.assertEqual(mod.tpm_limit_for("kimi-k3-oc"), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for(None), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for(""), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for("deepseek-v4-pro-0813-oc"),
                             mod.TPM_LIMIT)
            mod.TPM_MODEL_BUDGETS.update({"kimi-k3-oc": 1000, "glm-5.3-oc": 110000})
            self.assertEqual(mod.tpm_limit_for("kimi-k3-oc"), 1000)
            self.assertEqual(mod.tpm_limit_for("glm-5.3-oc"), 110000)
            self.assertEqual(mod.tpm_limit_for("other"), mod.TPM_LIMIT)
        finally:
            self._tpm_cleanup(mod)
```

**测试代码块 1-D：新增 opt-in 准入单测（插在 `test_tpm_admit_ok_immediate` 之前，现 :1085 前）**

```python
    def test_tpm_admit_non_enabled_model_bypasses(self) -> None:
        """opt-in：未启用模型直通——不建桶、不入队、不计 rejected。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        status, qwait = mod.tpm_admit("key:bypass", 10 ** 9, "m:disabled")
        self.assertEqual(status, "ok")
        self.assertEqual(qwait, 0)
        self.assertEqual(len(mod.TPM_BUCKETS), 0)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
        self.assertEqual(len(mod._tpm_rejected), 0)
        # model=None 恒直通（None not in {} → True）
        status2, qwait2 = mod.tpm_admit("key:bypass2", 10 ** 9, None)
        self.assertEqual((status2, qwait2), ("ok", 0))
        self.assertEqual(len(mod.TPM_BUCKETS), 0)

    def test_tpm_admit_enabled_model_goes_budget_path(self) -> None:
        """启用模型照旧走预算判定：est 超预算忙时 429 语义（"full"）保持。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:enabled"] = 100
        status, qwait = mod.tpm_admit("key:en", 100, "m:enabled")
        self.assertEqual((status, qwait), ("ok", 0))
        self.assertEqual(mod.TPM_BUCKETS[("key:en", "m:enabled")].used, 100)
        status2, qwait2 = mod.tpm_admit("key:en", 101, "m:enabled")
        self.assertEqual((status2, qwait2), ("full", 0))
        self.assertEqual(mod._tpm_rejected.get(("key:en", "m:enabled")), 1)
```

**测试代码块 1-E：存量准入单测加启用行（6 处，`_tpm_cleanup(mod)` 之后各加一行 `mod.TPM_MODEL_BUDGETS["m:X"] = mod.TPM_LIMIT`）**

| 测试（现行号） | 加行 |
|---|---|
| `test_tpm_admit_ok_immediate`（:1085） | 在 :1087 `self._tpm_cleanup(mod)` 后加 `mod.TPM_MODEL_BUDGETS["m:imm"] = mod.TPM_LIMIT` |
| `test_tpm_admit_full_oversized`（:1094） | 在 :1097 后加 `mod.TPM_MODEL_BUDGETS["m:big"] = mod.TPM_LIMIT` |
| `test_tpm_admit_queue_full`（:1118） | 在 :1120 后加 `mod.TPM_MODEL_BUDGETS["m:qfull"] = mod.TPM_LIMIT` |
| `test_tpm_settle_corrects_usage`（:1137） | 在 :1139 后加 `mod.TPM_MODEL_BUDGETS["m:settle"] = mod.TPM_LIMIT` |
| `test_tpm_settle_notifies_waiters`（:1174） | 在 :1176 后加 `mod.TPM_MODEL_BUDGETS["m:wake"] = mod.TPM_LIMIT` |
| `test_tpm_admit_queue_timeout`（:1195） | 在 :1197 后加 `mod.TPM_MODEL_BUDGETS["m:to"] = mod.TPM_LIMIT` |

**测试代码块 1-F：`test_tpm_snapshot_shape_and_masking` 对齐（现 :1240-1248）**

在 :1242 `self._tpm_cleanup(mod)` 之后加：
```python
        mod.TPM_MODEL_BUDGETS["m:snap"] = mod.TPM_LIMIT
```

现盘上 :1248：
```python
        self.assertEqual(snap["config"]["limit_by_model"], mod.TPM_LIMIT_BY_MODEL)
```

替换为：
```python
        self.assertEqual(snap["config"]["model_budgets"],
                         {"m:snap": mod.TPM_LIMIT})
        self.assertEqual(snap["config"]["enabled_models"], ["m:snap"])
```

**测试代码块 1-G：新增 `load_tpm_model_budgets` 容错单测（插在 `load_capture_errors` 相关单测附近）**

```python
    def test_load_tpm_model_budgets_tolerance(self) -> None:
        mod = self.mod
        path = os.path.join(tempfile.mkdtemp(prefix="ctyun-tpm-load-"), "s.json")
        # 缺文件 → {}
        self.assertEqual(mod.load_tpm_model_budgets(path), {})
        # 顶层非 dict / 无键 / 键非 dict → {}
        for bad in ("[]", "{}", '{"tpm_model_budgets": []}',
                    '{"tpm_model_budgets": "x"}'):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(bad)
            self.assertEqual(mod.load_tpm_model_budgets(path), {})
        # 逐键容错：非法项跳过，合法项保留
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_model_budgets": {
                "ok-a": 1000,
                "": 5,                       # 空名 → 跳过
                "ok-b": 50000,
                "bad-neg": -1,               # 非正 → 跳过
                "bad-zero": 0,               # 非正 → 跳过
                "bad-str": "100",            # 非 int → 跳过
                "bad-bool": True,            # bool → 跳过
                "x" * 201: 5,                # 超 200 字符 → 跳过
                "ok-c": 2.0,                 # float → 跳过（非 int）
            }}, fh, ensure_ascii=False)
        self.assertEqual(mod.load_tpm_model_budgets(path),
                         {"ok-a": 1000, "ok-b": 50000})
        # 超 32 键：只保留前 32 个合法键
        big = {"tpm_model_budgets": {"m%d" % i: 10 for i in range(40)}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(big, fh)
        self.assertEqual(len(mod.load_tpm_model_budgets(path)), 32)
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)
```

**测试代码块 1-H：集成测试迁移（opt-in 后未启用模型直通，存量限流集成测试必须 seed 启用其模型）**

`TpmAdmitHookTest.setUp`（现 :4492-4493）：
```python
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50"})
```
替换为：
```python
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50"},
                                seed_persist={"tpm_model_budgets": {"x": 50}})
```

`TpmRateLimitTest.setUp`（现 :4539-4543）：
```python
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "110",
                                           "CTYUN_TPM_WINDOW_S": "2",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2"})
```
替换为（加 seed）：
```python
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "110",
                                           "CTYUN_TPM_WINDOW_S": "2",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"m": 110}})
```

`test_queue_timeout_returns_429` 重建代理（现 :4599-4603）：
```python
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "30",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "2",
                                           "CTYUN_TPM_QUEUE_MAX": "2"})
```
替换为（加 seed）：
```python
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "30",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "2",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"m": 30}})
```

`test_retry_does_not_double_charge` 重建代理（现 :4785-4790）：
```python
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "200",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2",
```
替换为（加 seed；注 :4790 后的续行 env 保持原样）：
```python
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "200",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2",
```
（续行结构不变，仅在此 start_proxy 调用追加 `seed_persist={"tpm_model_budgets": {"m": 200}}` 参数；执行者按现文件实际续行结尾加）

`test_tpm_stats_endpoint` config 严格相等（现 :4747-4750）：
```python
        self.assertEqual(snap["config"],
                         {"limit": 110, "window_s": 2, "queue_max": 2,
                          "limit_by_model": {}},
                         "config must reflect env seams, got %r" % snap["config"])
```
替换为：
```python
        self.assertEqual(snap["config"],
                         {"limit": 110, "window_s": 2, "queue_max": 2,
                          "model_budgets": {"m": 110},
                          "enabled_models": ["m"]},
                         "config must reflect env seams, got %r" % snap["config"])
```

`TpmPerModelTest` 五处 seed 迁移（env 表语义改 seed_persist；卡 1 只迁移，env 迁移专门测试由卡 3 集成类承载）：

| 测试（现行号） | 迁移 |
|---|---|
| `test_kimi_independent_budget`（:5010-5017） | `extra_env` 中删 `"CTYUN_TPM_LIMIT_BY_MODEL": "kimi-k3-oc:1000",`，改加 `seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 1000}}` |
| `test_oversized_idle_release_first_then_reject`（:5061） | 该测试模型 "w"（:5104），在 start_proxy 加 `seed_persist={"tpm_model_budgets": {"w": 50}}` |
| `test_oversized_busy_429`（:5085） | 同模型 "w"，加 `seed_persist={"tpm_model_budgets": {"w": 50}}` |
| `test_rejected_counting_in_tpm_stats`（:5107） | 同模型 "w"，加 `seed_persist={"tpm_model_budgets": {"w": 50}}` |
| `test_tpm_stats_shape_with_limit_by_model`（:5127-5165） | 见下方完整替换 |

`test_tpm_stats_shape_with_limit_by_model`（现 :5125-5165 整段替换；模型 "v" 预算 110；第二阶段保留 env 作迁移 seed 断言——卡 1 已落地 main() 迁移）：
```python
    def test_tpm_stats_shape_with_limit_by_model(self) -> None:
        """/api/tpm_stats config 含 model_budgets/enabled_models；bucket 含 model 字段。
        第二阶段：persist 无 tpm_model_budgets + env CTYUN_TPM_LIMIT_BY_MODEL 非空
        → 启动迁移 seed（决策 2），config.model_budgets 反映 env 表且 stderr 含迁移行。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110",
                                    "CTYUN_TPM_WINDOW_S": "2",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                },
                                seed_persist={"tpm_model_budgets": {"v": 110}})
        auth = "Bearer test-shape"
        small = b'{"model":"v","stream":true,"messages":[]}'   # est = 10
        post_sse_auth(self.proxy_port, small, auth)
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["config"]["model_budgets"], {"v": 110},
                         "model_budgets must reflect enabled model budgets")
        self.assertEqual(snap["config"]["enabled_models"], ["v"])
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        self.assertEqual(snap["buckets"][0]["model"], "v")
        # 第二阶段：env 迁移 seed（persist 无 tpm_model_budgets 键）
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110",
                                    "CTYUN_TPM_WINDOW_S": "2",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                    "CTYUN_TPM_LIMIT_BY_MODEL":
                                        "glm-5.3-oc:110000",
                                })
        _, body2, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap2 = json.loads(body2.decode("utf-8"))
        self.assertEqual(snap2["config"]["model_budgets"],
                         {"glm-5.3-oc": 110000},
                         "env seam must migrate-seed tpm_model_budgets")
        self.assertIn("migrated CTYUN_TPM_LIMIT_BY_MODEL", stderr_text(self.proc))
```

> 注意：第二阶段模型 "glm-5.3-oc" 未发请求，`enabled_models == ["glm-5.3-oc"]` 不额外断言 buckets（此测试只验证 config 形态与迁移 seed）。执行者按现文件该测试的后续断言（若另有 used/remaining 断言）一并保留对齐。

---

**验证命令（卡 1）:**
```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/tpm-model-selection && python3 ctyun-stream-fix-proxy.test.py
```
预期：全绿（219 存量对齐后 + 新增 5 例 ≈ 224）。TDD 红点：先改测试（1-A~1-H）跑 → 存量限流相关用例红（opt-in 直通致 429 断言失败）；再落生产（1-1~1-8）跑 → 全绿。

**Commit:** `feat(proxy): opt-in per-model TPM budgets with migration seed`

---

### Card 2: 设置数据层——persist 写键 + `set_tpm_model_budgets` + 推荐纯函数组

**tier:** A
**TDD order:** 2（依赖卡 1 的 `TPM_MODEL_BUDGETS` 常量）
**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
**Commit:** `feat(proxy): tpm settings data layer — persist keys, setter, recommend functions`

**改动总览（生产）:**
1. `persist_upstream`（:841）签名加可选形参 `tpm_model_budgets`，落盘 dict 加键（防改上游端点丢限流设置）。
2. `set_upstream_base`（:1329）与 `set_capture_errors`（:1340）两处调用点同步传 `tpm_model_budgets=TPM_MODEL_BUDGETS`（不回写覆盖丢键）。
3. `save_stats_counters`（:978）落盘 dict 加 `tpm_model_budgets` 键。
4. 新增 `set_tpm_model_budgets(budgets)`（`set_capture_errors` 之后，:1346 后）。
5. 新增三个纯函数（`logs_snapshot` 之后，:742 后）：`tpm_body_err_samples`、`tpm_budget_recommend`、`tpm_settings_snapshot`。

---

**生产代码块 2-1：`persist_upstream`（替换现 :841-849）**

```python
def persist_upstream(base: str, path: str, capture_errors: bool = False,
                     tpm_model_budgets: dict = None) -> None:
    """持久化上游端点 + capture_errors + tpm_model_budgets（窄出口原子替换）。

    注意：本函数不持 _CFG_LOCK——调用方（set_upstream_base / set_capture_errors /
    set_tpm_model_budgets）在各自的 _CFG_LOCK 区内调用，由调用方保证 TPM_MODEL_BUDGETS
    读一致性。避免嵌套获取非重入锁的死锁（决策：_CFG_LOCK 是 threading.Lock）。
    """
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base, "capture_errors": capture_errors,
                   "tpm_model_budgets": tpm_model_budgets or {}},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)  # 同目录原子替换，读侧不会见到半截文件
```

> **死锁预防**：`_CFG_LOCK` 是 `threading.Lock`（非重入）。`persist_upstream` 被 `set_upstream_base`/:1329、`set_capture_errors`/:1340 在其 `with _CFG_LOCK` 区块内调用——若 `persist_upstream` 内部再取 `_CFG_LOCK` → 同线程死锁。故 `persist_upstream` 不持锁，`tpm_model_budgets` 由调用方在锁内传入。以下三处调用点一并更新签名。

**生产代码块 2-1b：`set_upstream_base` 调用点更新（现 :1329-1334）**

现盘上：
```python
def set_upstream_base(base: str) -> None:
    global UPSTREAM_BASE, _upstream_source
    with _CFG_LOCK:
        UPSTREAM_BASE = base
        _upstream_source = "api"
        persist_upstream(base, PERSIST_PATH, capture_errors=CAPTURE_ERRORS)
```

替换为：
```python
def set_upstream_base(base: str) -> None:
    global UPSTREAM_BASE, _upstream_source
    with _CFG_LOCK:
        UPSTREAM_BASE = base
        _upstream_source = "api"
        persist_upstream(base, PERSIST_PATH, capture_errors=CAPTURE_ERRORS,
                         tpm_model_budgets=TPM_MODEL_BUDGETS)
```

**生产代码块 2-1c：`set_capture_errors` 调用点更新（现 :1340-1346）**

现盘上：
```python
def set_capture_errors(enabled: bool) -> None:
    global CAPTURE_ERRORS
    with _CFG_LOCK:
        CAPTURE_ERRORS = enabled
        persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=enabled)
```

替换为：
```python
def set_capture_errors(enabled: bool) -> None:
    global CAPTURE_ERRORS
    with _CFG_LOCK:
        CAPTURE_ERRORS = enabled
        persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=enabled,
                         tpm_model_budgets=TPM_MODEL_BUDGETS)
```

**生产代码块 2-2：`save_stats_counters`（现 :990-993 与 :999-1005 两处）**

现盘上 :990-993：
```python
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
        probe_enabled = PROBE_ENABLED
```

替换为：
```python
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
        probe_enabled = PROBE_ENABLED
        budgets = dict(TPM_MODEL_BUDGETS)
```

现盘上 :999-1005：
```python
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "probe_enabled": probe_enabled,
                   "model_pricing": MODEL_PRICING,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
                  fh, ensure_ascii=False)
```

替换为：
```python
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "probe_enabled": probe_enabled,
                   "model_pricing": MODEL_PRICING,
                   "tpm_model_budgets": budgets,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
                  fh, ensure_ascii=False)
```

**生产代码块 2-3：新增 `set_tpm_model_budgets`（插在 `set_capture_errors` 之后，现 :1346 与 `_record_request` 之间）**

```python
def set_tpm_model_budgets(budgets: dict) -> None:
    global TPM_MODEL_BUDGETS
    with _CFG_LOCK:
        TPM_MODEL_BUDGETS = dict(budgets)
        persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=CAPTURE_ERRORS,
                         tpm_model_budgets=TPM_MODEL_BUDGETS)
    # save_stats_counters 内部也要拿 _CFG_LOCK：必须在锁外调用（同 :1335-1337 死锁注释）
    save_stats_counters(PERSIST_PATH)
```

**生产代码块 2-4：新增三个纯函数（插在 `logs_snapshot` 之后、`_EmptyStream` 之前，现 :742 后）**

```python
def tpm_body_err_samples(lines: list, model: str) -> list[int]:
    """从 REQ 行列表提取指定模型 body-err 行的 tpm= 值（决策 4 实证样本源）。

    正则锚定字段序（REQ 行 result 在 model 前、tpm 在 model 后，见 _log :2119-2122）：
    `result=body-err\b.*\bmodel=X\b.*\btpm=(\d+)`；不匹配/无 tpm 字段的行跳过。
    纯函数：无锁无 IO。
    """
    pattern = re.compile(r"result=body-err\b.*\bmodel=%s\b.*\btpm=(\d+)" % re.escape(model))
    out = []
    for line in lines:
        if not isinstance(line, str):
            continue
        match = pattern.search(line)
        if match:
            out.append(int(match.group(1)))
    return out


def tpm_budget_recommend(model: str, lines: list) -> dict:
    """预算推荐（决策 4）：body-err tpm= 样本 min×0.9 千位向下取整；无样本给 hint。

    有样本：recommended = (min(samples) * 9 // 10) // 1000 * 1000（最小 1000），
    choices = [recommended, recommended*3//2//1000*1000, recommended*2] 去重保序，
    标签「推荐/宽松×1.5/宽松×2」。无样本：recommended=None + hint + 默认三档
    [TPM_LIMIT//2, TPM_LIMIT, TPM_LIMIT*2]。纯函数（TPM_LIMIT 为模块常量只读）。
    """
    samples = tpm_body_err_samples(lines, model)
    if not samples:
        return {
            "recommended": None,
            "samples": 0,
            "hint": "无上游拒绝证据，建议不限流",
            "choices": [
                {"value": TPM_LIMIT // 2, "label": "默认/2"},
                {"value": TPM_LIMIT, "label": "默认"},
                {"value": TPM_LIMIT * 2, "label": "默认×2"},
            ],
        }
    base = min(samples)
    recommended = max(1000, (base * 9 // 10) // 1000 * 1000)
    values = [recommended, recommended * 3 // 2 // 1000 * 1000, recommended * 2]
    labels = ["推荐", "宽松×1.5", "宽松×2"]
    choices = []
    seen = set()
    for value, label in zip(values, labels):
        if value in seen:
            continue
        seen.add(value)
        choices.append({"value": value, "label": label})
    return {"recommended": recommended, "samples": len(samples), "choices": choices}


def tpm_settings_snapshot() -> dict:
    """TPM 设置快照（GET /api/tpm_settings 用，决策 3/5）。

    模型清单三源并集去重、字典序：RECENT_REQUESTS 模型名 ∪
    STATS["daily_by_model"][今日] 键 ∪ TPM_MODEL_BUDGETS 键。
    每模型 {name, enabled, budget, recommend}；budget 仅启用模型有值。
    锁纪律：LOG_LOCK / STATS_LOCK / _CFG_LOCK 各短持一次取副本，不嵌套、不做 IO。
    """
    with LOG_LOCK:
        lines = [entry["line"] for entry in LOG_RING]
    with STATS_LOCK:
        names = set(r.get("model") for r in RECENT_REQUESTS)
        day = STATS["daily_by_model"].get(today_key(), {})
        names.update(k for k in day.keys())
    with _CFG_LOCK:
        budgets = dict(TPM_MODEL_BUDGETS)
    names.update(budgets.keys())
    models = []
    for name in sorted(n for n in names if n):
        models.append({
            "name": name,
            "enabled": name in budgets,
            "budget": budgets.get(name),
            "recommend": tpm_budget_recommend(name, lines),
        })
    return {"models": models, "default_limit": TPM_LIMIT}
```

---

**测试代码块 2-A：纯函数单测（插在 ProxyDashboardUnitTest 内，`test_tpm_snapshot_shape_and_masking` 之后）**

```python
    def test_tpm_body_err_samples_extracts_and_filters(self) -> None:
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=31038 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=33227 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-3 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=999 ts=T",                       # result 非 body-err → 跳过
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=deepseek-v4-pro-0813-oc retried=0 rid=r-4 host=h ttfb=1.0ms "
            "stream=1 outcome=body_error qwait=0ms tpm=777 ts=T",      # 异模型 → 跳过
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-5 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms ts=T",                       # 无 tpm= → 跳过
            "garbage line without any fields",                         # 无匹配 → 跳过
            None,                                                      # 非 str → 跳过
        ]
        self.assertEqual(mod.tpm_body_err_samples(lines, "kimi-k3-oc"), [31038, 33227])
        self.assertEqual(mod.tpm_body_err_samples(lines, "deepseek-v4-pro-0813-oc"),
                         [777])
        self.assertEqual(mod.tpm_body_err_samples(lines, "glm-5.3-oc"), [])
        self.assertEqual(mod.tpm_body_err_samples([], "kimi-k3-oc"), [])

    def test_tpm_budget_recommend_with_samples(self) -> None:
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=31038 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=32375 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-3 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=33227 ts=T",
        ]
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines)
        # min=31038 → 31038*9//10=27934 → //1000*1000=27000
        self.assertEqual(rec["recommended"], 27000)
        self.assertEqual(rec["samples"], 3)
        # choices: [27000, 27000*3//2//1000*1000=40000, 54000]
        self.assertEqual([c["value"] for c in rec["choices"]], [27000, 40000, 54000])
        self.assertEqual(rec["choices"][0]["label"], "推荐")
        self.assertNotIn("hint", rec)

    def test_tpm_budget_recommend_no_samples(self) -> None:
        mod = self.mod
        rec = mod.tpm_budget_recommend("glm-5.3-oc", [])
        self.assertIsNone(rec["recommended"])
        self.assertEqual(rec["samples"], 0)
        self.assertIn("无上游拒绝证据", rec["hint"])
        self.assertEqual([c["value"] for c in rec["choices"]],
                         [mod.TPM_LIMIT // 2, mod.TPM_LIMIT, mod.TPM_LIMIT * 2])

    def test_tpm_settings_snapshot_union_and_sort(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        orig_recent = list(mod.RECENT_REQUESTS)
        orig_daily = dict(mod.STATS["daily_by_model"])
        orig_ring = list(mod.LOG_RING)
        try:
            mod.RECENT_REQUESTS.clear()
            mod.STATS["daily_by_model"] = {mod.today_key(): {"deepseek-v4-pro-0813-oc": {}}}
            mod.RECENT_REQUESTS.append({"model": "glm-5.3-oc"})
            mod.TPM_MODEL_BUDGETS["kimi-k3-oc"] = 30000
            snap = mod.tpm_settings_snapshot()
            names = [m["name"] for m in snap["models"]]
            self.assertEqual(names, ["deepseek-v4-pro-0813-oc", "glm-5.3-oc",
                                     "kimi-k3-oc"])
            by_name = {m["name"]: m for m in snap["models"]}
            self.assertEqual(by_name["kimi-k3-oc"]["enabled"], True)
            self.assertEqual(by_name["kimi-k3-oc"]["budget"], 30000)
            self.assertEqual(by_name["glm-5.3-oc"]["enabled"], False)
            self.assertIsNone(by_name["glm-5.3-oc"]["budget"])
            self.assertIsNone(by_name["glm-5.3-oc"]["recommend"]["recommended"])
            self.assertEqual(snap["default_limit"], mod.TPM_LIMIT)
        finally:
            mod.RECENT_REQUESTS.clear()
            mod.RECENT_REQUESTS.extend(orig_recent)
            mod.STATS["daily_by_model"] = orig_daily
            mod.LOG_RING.clear()
            mod.LOG_RING.extend(orig_ring)
            self._tpm_cleanup(mod)
```

> 注：`RECENT_REQUESTS`/`STATS`/`LOG_RING` 为模块级共享态，测试以 try/finally 恢复；三锁短持在 `tpm_settings_snapshot` 函数内部实现，测试不持锁。

**测试代码块 2-B：persist 写键 + setter 单测（插在 ProxyDashboardUnitTest 内）**

```python
    def test_save_and_load_tpm_model_budgets_roundtrip(self) -> None:
        mod = self.mod
        path = os.path.join(tempfile.mkdtemp(prefix="ctyun-tpm-rt-"), "s.json")
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update({"kimi-k3-oc": 1000, "glm-5.3-oc": 50000})
            mod.save_stats_counters(path)
            loaded = mod.load_tpm_model_budgets(path)
            self.assertEqual(loaded, {"kimi-k3-oc": 1000, "glm-5.3-oc": 50000})
        finally:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_persist_upstream_carries_tpm_model_budgets(self) -> None:
        mod = self.mod
        path = os.path.join(tempfile.mkdtemp(prefix="ctyun-tpm-pu-"), "s.json")
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS["kimi-k3-oc"] = 1000
            mod.persist_upstream("https://example.com/v1", path, capture_errors=True,
                                 tpm_model_budgets=mod.TPM_MODEL_BUDGETS)
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved["tpm_model_budgets"], {"kimi-k3-oc": 1000})
            self.assertEqual(saved["upstream_base"], "https://example.com/v1")
        finally:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_set_tpm_model_budgets_atomic_replace(self) -> None:
        mod = self.mod
        tmpdir = tempfile.mkdtemp(prefix="ctyun-tpm-set-")
        path = os.path.join(tmpdir, "s.json")
        orig_path = mod.PERSIST_PATH
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.PERSIST_PATH = path
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS["old-model"] = 500
            mod.set_tpm_model_budgets({"new-model": 2000})
            self.assertEqual(mod.TPM_MODEL_BUDGETS, {"new-model": 2000})
            self.assertEqual(mod.load_tpm_model_budgets(path), {"new-model": 2000})
        finally:
            mod.PERSIST_PATH = orig_path
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(tmpdir, ignore_errors=True)
```

> 注：`mod.PERSIST_PATH` 在模块中是模块级变量（`PERSIST_PATH = ...` 由 env 决定），单测直接赋模块属性可换临时路径；测试终态恢复。

---

**验证命令（卡 2）:**
```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/tpm-model-selection && python3 ctyun-stream-fix-proxy.test.py
```
预期：全绿（219 + 卡 1 新增 + 卡 2 新增 6 例 ≈ 230）。TDD 红点：先落 2-A/2-B 测试跑 → `AttributeError: module has no attribute 'tpm_body_err_samples'` 等红；再落 2-1~2-4 生产跑 → 全绿。

**Commit:** `feat(proxy): tpm settings data layer — persist keys, setter, recommend functions`

---

### Card 3: HTTP API——`GET /api/tpm_settings` + `GET/POST /api/config` 扩展 + 集成测试类

**tier:** A
**TDD order:** 3（依赖卡 1/2）
**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`
**Commit:** `feat(proxy): GET /api/tpm_settings and POST /api/config tpm_model_budgets`

**改动总览（生产）:**
1. `do_GET`：`/api/tpm_stats` 分支后新增 `/api/tpm_settings`；`/api/config` GET payload 加 `tpm_model_budgets`。
2. `do_POST /api/config`：新增可选键 `tpm_model_budgets` 校验 + 调用 setter + 响应带键。

---

**生产代码块 3-1：`do_GET` 新端点 + config payload（现 :2142-2149）**

现盘上：
```python
        elif path == "/api/tpm_stats":
            self._send_json(200, tpm_snapshot())
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS,
                           "model_pricing": MODEL_PRICING}
            self._send_json(200, payload)
```

替换为：
```python
        elif path == "/api/tpm_stats":
            self._send_json(200, tpm_snapshot())
        elif path == "/api/tpm_settings":
            self._send_json(200, tpm_settings_snapshot())
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS,
                           "model_pricing": MODEL_PRICING,
                           "tpm_model_budgets": dict(TPM_MODEL_BUDGETS)}
            self._send_json(200, payload)
```

**生产代码块 3-2：`do_POST /api/config` 校验 + 调用（现 :2245-2262）**

现盘上：
```python
        base = data.get("upstream_base") if isinstance(data, dict) else None
        cap = data.get("capture_errors") if isinstance(data, dict) else None  # 可选键
        if cap is not None and not isinstance(cap, bool):
            self._send_json(400, {"error": "capture_errors must be a boolean"})
            return
```

替换为（cap 校验后插入 tpm 校验）：
```python
        base = data.get("upstream_base") if isinstance(data, dict) else None
        cap = data.get("capture_errors") if isinstance(data, dict) else None  # 可选键
        tpm = data.get("tpm_model_budgets") if isinstance(data, dict) else None  # 可选键
        if cap is not None and not isinstance(cap, bool):
            self._send_json(400, {"error": "capture_errors must be a boolean"})
            return
        if tpm is not None:
            if not isinstance(tpm, dict):
                self._send_json(400, {"error": "tpm_model_budgets must be a dict"})
                return
            if len(tpm) > 32:
                self._send_json(400, {"error": "tpm_model_budgets 最多 32 个模型"})
                return
            for model, limit in tpm.items():
                if not isinstance(model, str) or not model or len(model) > 200:
                    self._send_json(400, {"error": "tpm_model_budgets 键需为非空字符串"
                                                   "（≤200 字符），非法项：%r" % (model,)})
                    return
                if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                    self._send_json(400, {"error": "tpm_model_budgets 值需为正整数，"
                                                   "非法项 %r=%r" % (model, limit)})
                    return
```

现盘上 :2255-2262：
```python
        if base is not None:
            set_upstream_base(base)
        if cap is not None:
            set_capture_errors(cap)
        with _CFG_LOCK:
            resp = {"ok": True, "upstream_base": UPSTREAM_BASE, "source": "api",
                    "capture_errors": CAPTURE_ERRORS}
        self._send_json(200, resp)
```

替换为：
```python
        if base is not None:
            set_upstream_base(base)
        if cap is not None:
            set_capture_errors(cap)
        if tpm is not None:
            set_tpm_model_budgets(tpm)
        with _CFG_LOCK:
            resp = {"ok": True, "upstream_base": UPSTREAM_BASE, "source": "api",
                    "capture_errors": CAPTURE_ERRORS,
                    "tpm_model_budgets": dict(TPM_MODEL_BUDGETS)}
        self._send_json(200, resp)
```

---

**测试代码块 3-A：新增集成测试类 `TpmModelSelectionTest`（插在 `TpmPerModelTest` 之后，现 :5179 `ClassifyOutcomeTriStateTest` 之前）**

```python
class TpmModelSelectionTest(unittest.TestCase):
    """TPM 模型选择 + 设置页后端集成测试（spec Acceptance 直通/启用/清单/持久化/迁移/非法输入）。

    复用 make_scripted_upstream + start_proxy(extra_env, seed_persist) +
    post_sse_auth + admin_get + admin_post + stderr_text + stop_fake_upstreams。
    """

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_USAGE + SSE_DONE)  # 带 usage 帧 → settle
        self.proxy_port = free_port()

    def tearDown(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            stderr_text(self.proc)
            shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_disabled_model_passthrough(self) -> None:
        """seed {}（无启用模型）+ CTYUN_TPM_LIMIT=50：est>50 的带 auth 请求 → 200；
        /api/tpm_stats buckets 空；stderr 无 tpm-queue-full。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50",
                                           "CTYUN_TPM_WINDOW_S": "2",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {}})
        big = (b'{"model":"deepseek-v4-pro-0813-oc","stream":true,'
               b'"messages":[{"role":"user","content":"' + b"y" * 140 + b'"}]}')
        status, data = post_sse_auth(self.proxy_port, big, "Bearer test-disabled")
        self.assertEqual(status, 200, "non-enabled model must pass through, got %d" % status)
        self.assertEqual(data, SSE_A + SSE_USAGE + SSE_DONE)
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["buckets"], [])
        self.assertNotIn("tpm-queue-full", stderr_text(self.proc))

    def test_enabled_model_429_other_model_200(self) -> None:
        """seed {"kimi-k3-oc": 1000}：同 key 先发小 kimi 占桶再发 est>1000 kimi → 429
        model_tpm_limit；同 key 发未启用 deepseek 大请求 → 200。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "110000",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 1000}})
        auth = "Bearer test-enabled-429"
        small_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                      b'"messages":[{"role":"user","content":"hi"}]}')
        prime_status, _ = post_sse_auth(self.proxy_port, small_kimi, auth)
        self.assertEqual(prime_status, 200, "priming kimi must be admitted")
        big_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                    b'"messages":[{"role":"user","content":"'
                    + b"x" * 1000
                    + b'"}],"max_tokens":3000}')
        status1, data1 = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status1, 429)
        parsed = json.loads(data1.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        big_ds = (b'{"model":"deepseek-v4-pro-0813-oc","stream":true,'
                  b'"messages":[{"role":"user","content":"' + b"x" * 1000
                  + b'"}],"max_tokens":3000}')
        status2, _ = post_sse_auth(self.proxy_port, big_ds, auth)
        self.assertEqual(status2, 200, "non-enabled deepseek must pass through")

    def test_model_list_auto_generated(self) -> None:
        """发过 deepseek 流量（无 auth 也统计）→ /api/tpm_settings models 含该模型，
        与 TPM_MODEL_BUDGETS 键并集去重、字典序。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 30000}})
        post_sse(self.proxy_port)  # 无 auth，model=deepseek-v4-pro-0813-oc
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
        snap = json.loads(body.decode("utf-8"))
        names = [m["name"] for m in snap["models"]]
        self.assertIn("deepseek-v4-pro-0813-oc", names)
        self.assertIn("kimi-k3-oc", names)
        self.assertEqual(names, sorted(names), "model list must be sorted")
        self.assertEqual(snap["default_limit"], load_proxy_module().TPM_LIMIT)
        by_name = {m["name"]: m for m in snap["models"]}
        self.assertEqual(by_name["kimi-k3-oc"]["enabled"], True)
        self.assertEqual(by_name["deepseek-v4-pro-0813-oc"]["enabled"], False)
        self.assertIsInstance(by_name["deepseek-v4-pro-0813-oc"]["recommend"]["choices"],
                              list)

    def test_save_restart_persist_roundtrip(self) -> None:
        """POST {"tpm_model_budgets": {"glm-5.3-oc": 50000}} → 200 响应含同值；
        persist 文件含 tpm_model_budgets 键；同 persist 路径重启 → GET /api/config 返回该值。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port)
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": {"glm-5.3-oc": 50000}}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["tpm_model_budgets"], {"glm-5.3-oc": 50000})
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tpm_model_budgets"],
                             {"glm-5.3-oc": 50000})
        # 重启（同 persist 路径复用：保存文件内容后二次 start_proxy 以 seed 回灌）
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        self.proc = start_proxy(self.upstream_port, free_port(), seed_persist=saved)
        _, body2, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body2.decode("utf-8"))
        self.assertEqual(cfg["tpm_model_budgets"], {"glm-5.3-oc": 50000})

    def test_env_migration_path(self) -> None:
        """seed_persist 无新键 + env CTYUN_TPM_LIMIT_BY_MODEL="kimi-k3-oc:1000"
        → 启动迁移：GET /api/config tpm_model_budgets == {"kimi-k3-oc": 1000}
        且 persist 文件已落该键（卡 2 save_stats_counters 带键后可见）。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT_BY_MODEL": "kimi-k3-oc:1000"},
                                seed_persist={"upstream_base": "http://127.0.0.1:%d"
                                              % self.upstream_port})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["tpm_model_budgets"], {"kimi-k3-oc": 1000})
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tpm_model_budgets"], {"kimi-k3-oc": 1000})
        self.assertIn("migrated CTYUN_TPM_LIMIT_BY_MODEL", stderr_text(self.proc))

    def test_post_empty_budgets_clears(self) -> None:
        """POST {"tpm_model_budgets": {}} → 全部模型直通：先前 429 的 kimi 大请求 → 200。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 50}})
        auth = "Bearer test-clear"
        small_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                      b'"messages":[{"role":"user","content":"hi"}]}')
        post_sse_auth(self.proxy_port, small_kimi, auth)
        big_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                    b'"messages":[{"role":"user","content":"' + b"x" * 140 + b'"}]}')
        status1, _ = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status1, 429, "enabled kimi with budget 50 must reject")
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": {}}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["tpm_model_budgets"], {})
        status2, _ = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status2, 200, "after clear, kimi must pass through")

    def test_post_invalid_budgets_400(self) -> None:
        """非法输入逐项 400：负值 / 非 dict / 超 32 键。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port)
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": {"m": -1}}).encode("utf-8"))
        self.assertEqual(status, 400)
        self.assertIn("正整数", json.loads(body.decode("utf-8"))["error"])
        status2, body2 = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": "not-a-dict"}).encode("utf-8"))
        self.assertEqual(status2, 400)
        self.assertIn("must be a dict", json.loads(body2.decode("utf-8"))["error"])
        too_many = {"m%d" % i: 10 for i in range(33)}
        status3, body3 = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": too_many}).encode("utf-8"))
        self.assertEqual(status3, 400)
        self.assertIn("32", json.loads(body3.decode("utf-8"))["error"])
        # 非法 POST 不得改变现值（seed 默认 kimi 30000 保持）
        _, body4, _ = admin_get(self.proc.admin_port, "/api/config")
        self.assertEqual(json.loads(body4.decode("utf-8"))["tpm_model_budgets"],
                         {"kimi-k3-oc": 30000})
```

> 注意：`start_proxy` 缺省无 seed 时 main() 迁移默认 seed `{"kimi-k3-oc": 30000}`——`test_post_invalid_budgets_400` 末断言依赖此默认值（生产实证线）。

---

**验证命令（卡 3）:**
```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/tpm-model-selection && python3 ctyun-stream-fix-proxy.test.py
```
预期：全绿（≈230 + 7 例 ≈ 237）。TDD 红点：先落 3-A 测试类跑 → `/api/tpm_settings` 404 红、POST tpm_model_budgets 被忽略红；再落 3-1/3-2 生产跑 → 全绿。

**Commit:** `feat(proxy): GET /api/tpm_settings and POST /api/config tpm_model_budgets`

---

### Card 4: Dashboard v1.5——TPM 限流设置区块（HTML + JS 单块插入）

**tier:** A
**TDD order:** 4（依赖卡 3 端点）
**Files:** `ctyun-stream-fix-proxy.py`（`_DASHBOARD_SRC`）、`ctyun-stream-fix-proxy.test.py`
**Commit:** `feat(dashboard): TPM 限流设置 section v1.5`

**改动总览（生产，两处单块插入，全部在 `_DASHBOARD_SRC` 内）:**
1. HTML section：上游端点 section（现 :2400 `</section>`）之后插入「TPM 限流设置」卡片。
2. JS：`<script>` 尾（现 :2857 `setInterval(poll, 2000);` 之后）插入 `loadTpmSettings` / `renderTpmSettings` / 保存 handler + 启动挂载。

---

**生产代码块 4-1：HTML section（现 :2400 `</section>` 之后、:2401 range-tabs section 之前插入）**

```html
  <section class="card" id="tpm-settings-card">
    <div class="card-title">TPM 限流设置（按模型启用）</div>
    <p class="dimmed">仅勾选的模型走限流（桶/排队/429），其余模型直通；预算档位由上游拒绝实证推荐。加载中……</p>
    <div id="tpm-models"></div>
    <div class="actions">
      <button id="tpm-save" type="button">保存限流设置</button>
      <p class="msg" id="tpm-msg" role="status"></p>
    </div>
  </section>
```

**生产代码块 4-2：JS（现 :2857 `setInterval(poll, 2000);` 之后、`</script>` 之前插入）**

```javascript
function loadTpmSettings() {
  fetch("/api/tpm_settings")
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (data) { renderTpmSettings(data); })
    .catch(function (err) {
      var box = $("tpm-models");
      box.textContent = "";
      box.appendChild(el("p", "msg err", "读取限流设置失败：" + String(err)));
    });
}
function renderTpmSettings(data) {
  var box = $("tpm-models");
  box.textContent = "";
  var models = data.models || [];
  if (!models.length) {
    box.appendChild(el("p", "empty", "暂无模型流量记录"));
    return;
  }
  for (var i = 0; i < models.length; i++) {
    var m = models[i];
    var row = el("div", "tpm-row");
    var check = document.createElement("input");
    check.type = "checkbox";
    check.id = "tpm-check-" + i;
    check.setAttribute("data-model", m.name);
    check.checked = !!m.enabled;
    var label = el("label", "", "");
    label.setAttribute("for", "tpm-check-" + i);
    label.textContent = m.name;
    row.appendChild(check);
    row.appendChild(label);
    if (m.recommend && m.recommend.choices && m.recommend.choices.length) {
      var sel = document.createElement("select");
      sel.id = "tpm-select-" + i;
      sel.setAttribute("data-model", m.name);
      sel.setAttribute("aria-label", "预算档位");
      for (var j = 0; j < m.recommend.choices.length; j++) {
        var opt = document.createElement("option");
        opt.value = String(m.recommend.choices[j].value);
        opt.textContent = m.recommend.choices[j].label + "（" +
                          m.recommend.choices[j].value + "）";
        if (m.budget !== null && m.budget !== undefined &&
            m.recommend.choices[j].value === m.budget) {
          opt.selected = true;
        }
        sel.appendChild(opt);
      }
      row.appendChild(sel);
    }
    if (m.recommend && m.recommend.hint) {
      row.appendChild(el("span", "hint", m.recommend.hint));
    } else if (m.recommend && m.recommend.recommended !== null &&
               m.recommend.recommended !== undefined) {
      row.appendChild(el("span", "hint", "推荐 " + m.recommend.recommended +
                         "（样本 " + m.recommend.samples + "）"));
    }
    box.appendChild(row);
  }
}
$("tpm-save").addEventListener("click", function () {
  var msg = $("tpm-msg");
  var box = $("tpm-models");
  var checks = box.querySelectorAll("input[type=checkbox][data-model]");
  var budgets = {};
  for (var i = 0; i < checks.length; i++) {
    if (!checks[i].checked) continue;
    var model = checks[i].getAttribute("data-model");
    var sel = box.querySelector("select[data-model='" + model + "']");
    budgets[model] = sel ? parseInt(sel.value, 10) : 0;
  }
  msg.className = "msg wait";
  msg.textContent = "保存中……";
  fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tpm_model_budgets: budgets })
  }).then(function (resp) {
    return resp.json().then(function (data) { return { status: resp.status, data: data }; });
  }).then(function (r) {
    if (r.status === 200) {
      msg.className = "msg ok";
      msg.textContent = "已保存，立即生效";
      loadTpmSettings();
    } else {
      msg.className = "msg err";
      msg.textContent = (r.data && r.data.error) ? r.data.error : ("保存失败：HTTP " + r.status);
    }
  }).catch(function (err) {
    msg.className = "msg err";
    msg.textContent = "保存失败：" + String(err) + " —— 确认能访问管理接口 /api/config。";
  });
});
loadTpmSettings();
```

> 禁 innerHTML：上列全部用 `el()`/`textContent`/DOM API（沿用 :2464-2469 惯例）。`loadTpmSettings()` 只在加载时与保存成功后各挂一次，不进 2s poll。

---

**测试代码块 4-A：`test_dashboard_html_full_page` 追加断言（现 :2683 区域之后追加）**

```python
        # v1.5：TPM 限流设置区块（静态存在性 + 无 innerHTML 惯例）
        self.assertIn("TPM 限流设置", html)
        self.assertIn('id="tpm-models"', html)
        self.assertIn('id="tpm-save"', html)
        self.assertIn("/api/tpm_settings", html)
        self.assertIn('id="tpm-msg"', html)
        self.assertIn('data-model', html)
        self.assertIn("loadTpmSettings", html)
        self.assertIn("renderTpmSettings", html)
```

（既有 `self.assertNotIn("innerHTML", html)` 断言 :2646 天然覆盖新 JS 块——执行者不改该行。）

---

**验证命令（卡 4）:**
```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/tpm-model-selection && python3 ctyun-stream-fix-proxy.test.py
```
预期：全绿（≈237 + 1 例 ≈ 238）。TDD 红点：先落 4-A 断言跑 → 4 个 assertIn 红（HTML 尚无区块）；再落 4-1/4-2 生产跑 → 全绿。

**Commit:** `feat(dashboard): TPM 限流设置 section v1.5`

---

## Card Execution Order

| Order | Card | Depends On | Commit Message |
|-------|------|-----------|----------------|
| 1 | opt-in 运行时状态与准入 + 迁移 seed + 存量测试对齐 | — | `feat(proxy): opt-in per-model TPM budgets with migration seed` |
| 2 | 设置数据层（persist 写键 + setter + 推荐纯函数组） | 卡 1 | `feat(proxy): tpm settings data layer — persist keys, setter, recommend functions` |
| 3 | HTTP API（tpm_settings 端点 + config 扩展 + 集成测试类） | 卡 1/2 | `feat(proxy): GET /api/tpm_settings and POST /api/config tpm_model_budgets` |
| 4 | Dashboard v1.5 UI（HTML + JS 单块插入） | 卡 3 | `feat(dashboard): TPM 限流设置 section v1.5` |

---

## Spec Acceptance 覆盖映射

| Spec Acceptance | 卡 |
|---|---|
| 未启用模型直通（est 超默认也 200、buckets 空、无 tpm-queue-full） | 卡 1（单测+存量对齐）、卡 3（`test_disabled_model_passthrough`） |
| 启用模型照旧 429（kimi 链路）+ 未启用 deepseek 200 | 卡 3（`test_enabled_model_429_other_model_200`） |
| TPM_MODEL_BUDGETS 唯一权威 + env 一次性迁移 seed（persist 缺键+env 非空 → seed+落盘；皆无 → kimi 30000） | 卡 1（生产+main()）、卡 2（save 带键）、卡 3（`test_env_migration_path`） |
| tpm_limit_for 两级化 | 卡 1（生产 1-3 + 测试 1-C） |
| 模型清单三源并集（RECENT ∪ daily_by_model 今日 ∪ budgets） | 卡 2（`tpm_settings_snapshot`）、卡 3（`test_model_list_auto_generated`） |
| 推荐算法（min×0.9 千位取整、choices 推荐/×1.5/×2、无样本 hint） | 卡 2（2-4 + 2-A 三例；27000/40000/54000 精确断言） |
| GET /api/tpm_settings | 卡 3（3-1 + 3-A） |
| POST /api/config 扩展（≤32 键 int>0 校验 400 逐项） | 卡 3（3-2 + `test_post_invalid_budgets_400`） |
| persist_upstream 窄出口带新键（改上游不丢限流设置） | 卡 2（2-1 + `test_persist_upstream_carries_tpm_model_budgets`） |
| dashboard v1.5 设置区块（checkbox+select、el()/textContent 禁 innerHTML） | 卡 4 |
| TpmPerModelTest env 用例迁移 seed_persist | 卡 1（1-H） |
| 部署与生产验证（DELIVER 阶段，主代理执行，非 implementer） | 不在卡内 |

---

## Plan Self-Review（5 项）

1. **Spec coverage.** 上表 12 条 Acceptance 全覆盖；`_log`/`classify_outcome` 未触碰；Exclusions 未违反（不改全局常量语义/429 响应体/ERROR_EVENTS schema；不扫历史日志文件；推荐仅基于内存 LOG_RING）。
2. **Placeholder scan.** 无「TODO/FIXME/待补/implementer 自行决定」类指令；每卡生产与测试代码均行级写死。所有代码块含 before/after 对照，无抽象描述。Card 2 测试 2-A `tpm_settings_snapshot_union_and_sort` 使用 try/finally 恢复模块共享态——属测试技术惯例非占位。
3. **Type consistency.** `TPM_MODEL_BUDGETS: dict`（str→int）；`tpm_limit_for -> int`；`tpm_admit -> ("ok"|"full"|"timeout", int)`；`tpm_budget_recommend -> dict`（choices 元素 `{"value": int, "label": str}`）；`load_tpm_model_budgets -> dict[str,int]`；API JSON 键名 `model_budgets`/`enabled_models`/`tpm_model_budgets` 三处口径与 spec 决策 2/5 一致；`limit_by_model` 旧键全仓移除（生产 :639 + 测试 :952/:1248/:4747-4750/:5125-5165 共 5 处已列替换）。
4. **可落盘性.** 所有锚点按盘上 bd2554f grep 实测（见 Global Constraints 7 行号对照表）；每块替换给出「现盘上/替换为」对照，执行者无歧义。TDD 红点路径明确：卡 1 先改测试 → 存量限流用例红；卡 2/3/4 先加测试 → AttributeError/404 红。
5. **锚点实测声明.** 本计划行号在 2026-10-01 于 worktree 盘上 grep/Read 验证（常量 :101-112、tpm_limit_for :455、tpm_admit :515、tpm_settle :585、tpm_snapshot :605-643、logs_snapshot 尾 :742、persist_upstream :841、save_stats_counters :978-1006、load_capture_errors :1031、set_capture_errors :1340-1346、do_GET :2142-2149、do_POST :2245-2262、main :2875-2887、上游 section :2391-2400、JS 尾 :2855-2857、测试工具 :318/:403/:429/:448/:595/:607/:619/:631/:1035、TpmPerModelTest :4983）。执行时若漂移（P5 并行合入），按语义锚点（函数名/结构）定位，行号仅作参照。

## 漂移对齐声明

spec 基于源码 b1de8b3，本 worktree 基点 bd2554f（P4 probe+health API 已合入）：全部锚点行号已按盘上实测校正（对照表见 Global Constraints 7），语义与 spec 完全一致（tpm_admit/settle/snapshot 函数体、persist 结构、do_GET/do_POST 分支、main() 启动序列、dashboard 上游 section 与 JS 尾均逐点核验）。P5 并行会话为 dashboard 重构，本计划 DASHBOARD 改动集中为两处单块插入（HTML section :2400 后 + JS :2857 后），冲突面最小化。
