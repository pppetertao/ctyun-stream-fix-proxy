# PLAN — Episode F2: calibrate 401/403 凭证失败分类 credential_error（立即中止、不持久化）

## Header

- **Spec**: `docs/superpowers/specs/2026-10-10-calibrate-4xx-classification-design.md`（worktree 分支 commit 00d66a2）
- **Worktree**: `.worktrees/calibrate-4xx-classification`，分支 `fix/calibrate-4xx-classification`，merge-base = ea3fbbc
- **Baseline**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 389 全绿（ea3fbbc）。本机无 `python`，一律 `/usr/bin/python3`
- **规模**: 2 实现卡 + whole-branch 收尾节（≤4 卡小计划，单次 dispatch 执行）
- **Tier 路由**: 全卡 B → executor（代码已写死到哨兵/分支级，转写 + 跑测试 + commit；无 C 档留白判据命中——无真机验证、无运行时数据依赖、无 DI 实测，所有 seam 已注入）
- **改动范围**: 仅 2 文件——`ctyun-stream-fix-proxy.py` + `ctyun-stream-fix-proxy.test.py`。新增测试 6 个，全量预期 389 + 6 = **395**
- **命名澄清（spec 措辞 vs 实测）**: 哨兵类别常量 = `CLASS_CREDENTIAL_FAILURE = "credential_failure"`（engine 内部）；对外 result 字符串 = `outcome: "credential_error"`（spec 定名）。两者**不同值**，卡内代码已按此写死，不得混用

## Global Constraints（全卡硬约束）

1. **classify_outcome 本体零改**：`classify_outcome`（:448-483）、`_KIND_CATEGORY` 映射（:212-223）、relay 路径 401/403 语义（`CLASS_REQUEST_FAULT`、log "upstream-err"）一概不动。守卫只加在 `calibrate_engine._attempt` 内。
2. **429 语义零改**：主循环 ：1154 与重试分支 ：1165 的 `category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT) → rejected` 判定原样保留；`test_calibrate_engine_429_still_rejected_regression` 断言逐字复制 ：8391-8404 现状（常绿回归锚）。
3. **磁盘 schema 键集合零变化**：`save_stats_counters` 持久化顶层键与 `tpm_probe_results` 条目键与 ea3fbbc 完全一致（:1793-1801）；credential_error 只存在于内存 `_CALIBRATE_STATE["result"]` 与 stderr REQ 行，不进任何持久化结构。
4. **PROBE_RESULTS 零写入**：credential_error 靠 ：1446 现有条件（`outcome=="rejected" and threshold is not None`）天然排除；`_calibrate_loop` 仅加注释，**无功能改动**。禁止在 loop 加 `elif` 分支。
5. **重试分支对称守卫**：:1154（主循环）与 ：1165（503 重试分支）两处**都必须**插 credential_error 守卫——漏 ：1165 则 503→401 序列落 `last_ok_est = est` 继续加压（比现状更糟）。卡 1 的 `test_calibrate_engine_401_on_retry_after_503_aborts` 专门锁定。
6. **不重试**：credential_error 同凭证重试必然再败，守卫直接 `return _result(...)`，不进 sleep/重试路径。
7. **测试运行方式**：模块名带连字符，`/usr/bin/python3 -m unittest <点路径>` 不可用（已实测 `AttributeError: module 'ctyun-stream-fix-proxy' has no attribute 'test'`）。targeted 运行一律走 `unittest.main` argv 传名：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py <ClassName>[.method] ...`（已实测 30 tests OK / 单方法 OK）。
8. **Edit 全文本锚点**：下述所有 old_string 已实测文件内唯一（:1154 vs :1165 同文异缩进，用多行含缩进块区分；`txt += "。";` 全文件仅 ：5618 一处）。Edit 报 not unique / not found → executor STOP 上报，不得自行改锚。
9. **Git**：每卡一个 commit（卡内测试+实现一体），Conventional Commits；commit 前自检 `git config --get user.name && git config --get user.email`（R32，placeholder 禁入）+ `git status` 仅含本卡文件。本 PLAN.md 落盘不 commit（主代理处理）。
10. **范围**：不跑全量测试（收尾节除外）；不建 spec 外文件；不加依赖；不动 `tpm_admit`/`TPM_MODEL_BUDGETS`/`tpm_budget_recommend`（spec Exclusions）。

## 锚点实测记录（写作时现场 grep/read 回显，全部命中）

| 锚点 | 实测 |
|---|---|
| 常量区 :209-210 | `CLASS_BODY_ERROR = "body_error"` / `ERR_KIND_BODY_ERROR = "body_error"` 配对行 ✓ |
| `_KIND_CATEGORY` :212-223 | 无 credential 条目（现状无任何 401/403 区分，R31-S1 一致）✓ |
| `classify_outcome` :478-480 | `if 400 <= status < 500: return _Outcome(CLASS_REQUEST_FAULT, "upstream-err", ...)` ✓ |
| `calibrate_engine` :1090 / docstring :1098-1111 | def 与 docstring 现文逐字确认 ✓ |
| `_result` :1120-1124 | 签名 `(outcome, threshold, status, batches=None)`，batches 默认闭包 `batch` ✓ |
| `_attempt` :1126-1134 | TIMEOUT 判 ：1132-1133 → classify 调用 ：1134（该行全文件唯一，grep=1）✓ |
| 主循环 ：1154 / 重试分支 ：1165 | 同文 `if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):` 异缩进（8/12 空格），多行块可区分 ✓ |
| `_calibrate_loop` :1446 | `if result["outcome"] == "rejected" and result["threshold"] is not None:` 全文件唯一（grep=1）✓ |
| persist :1793-1801 | `json.dump({...})` 顶层 7 键确认 ✓ |
| `tpm_budget_recommend` :949-963 | probe.threshold>0 → `source="probe"` 0.9×（不改动，仅理解读取端）✓ |
| `_DASH_JS_V2` calibDone :5612-5619 | `txt += "。";`（:5618 全文件唯一）→ `textContent = txt`（:5619）✓ |
| test `_scripted` :8347-8356 | 越界重复最后一项；`(status, body_error)` 二元组 ✓ |
| test 429 先例 ：8391-8404 | 断言集逐字确认 ✓ |
| test daemon 骨架 ：8788-8850 | `TpmCalibrateStateTest`（:8594 起，**spec 写的 "TpmCalibrateLoopTest" 实为该班**）✓ |
| test JS 断言先例 ：3476-3484 | `DashboardV2SkeletonTest`（:3401 起，setUp 已有 `load_proxy_module`）✓ |
| test runner 尾部 | `unittest.main(verbosity=2)` ✓ |
| 类内现有计数 | `TpmCalibrateEngineTest`+`TpmCalibrateStateTest` = 30 绿；`DashboardV2SkeletonTest` = 7 绿 ✓ |

---

## Task 1（tier B）：calibrate_engine 401/403 → credential_error + loop 天然排除锁定

**文件**：`ctyun-stream-fix-proxy.py` + `ctyun-stream-fix-proxy.test.py`
**TDD 顺序**：步骤 1 写 5 个测试实跑红（4 红 + 1 常绿）→ 步骤 2 实现 6 处 Edit → 步骤 3 绿 → commit。

### 步骤 1（红）：先写 5 个失败测试

**Edit 1.1** — `ctyun-stream-fix-proxy.test.py`，在 `TpmCalibrateEngineTest` 班内、既有 429 测试之后插入。锚点（唯一）：

old_string:
```
    def test_calibrate_engine_ramp_small_start_and_monotonic(self) -> None:
```

new_string:
```python
    def test_calibrate_engine_401_stops_credential_error(self) -> None:
        """F2：首批 401 → credential_error 立即中止；threshold=None、不重试、
        不把该批 est 当阈值。"""
        mod = self.mod
        sender, calls = self._scripted([(401, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "credential_error")
        self.assertIsNone(result["threshold"])
        self.assertEqual(result["status"], 401)
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["consumed"],
                         mod.calibrate_est_for_batch("m", 1))
        self.assertEqual(calls["n"], 1, "401 不得重试（同凭证重试必然再败）")

    def test_calibrate_engine_403_mid_ramp_aborts(self) -> None:
        """F2：爬坡中途 403 → credential_error 立即中止，不继续加压。"""
        mod = self.mod
        sender, calls = self._scripted([(200, False)] * 2 + [(403, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "credential_error")
        self.assertIsNone(result["threshold"])
        self.assertEqual(result["status"], 403)
        self.assertEqual(result["batches"], 3)
        self.assertEqual(calls["n"], 3)

    def test_calibrate_engine_429_still_rejected_regression(self) -> None:
        """F2 回归锚：429 判定逐字保持 ramp_first_reject_stops 现状
        （rejected + threshold=本批 est）——守卫只拦 401/403，429 语义零变化。"""
        mod = self.mod
        sender, calls = self._scripted([(200, False)] * 7 + [(429, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(calls["n"], 8, "first reject must stop, no confirm batch")
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 8)
        self.assertEqual(result["threshold"], mod.calibrate_est_for_batch("m", 8))
        self.assertEqual(result["consumed"],
                         sum(mod.calibrate_est_for_batch("m", b)
                             for b in range(1, 9)))
        self.assertEqual(result["status"], 429)

    def test_calibrate_engine_401_on_retry_after_503_aborts(self) -> None:
        """F2 对称守卫：503 重试一次遇 401 → credential_error 立即中止，
        不得落 last_ok_est 继续加压（漏插重试分支守卫即回归本测试）。"""
        mod = self.mod
        sender, calls = self._scripted([(503, False), (401, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "credential_error")
        self.assertIsNone(result["threshold"])
        self.assertEqual(result["status"], 401)
        self.assertEqual(result["batches"], 1)
        self.assertEqual(calls["n"], 2, "503 重试一次后 401 必须立即中止")

    def test_calibrate_engine_ramp_small_start_and_monotonic(self) -> None:
```

**Edit 1.2** — `ctyun-stream-fix-proxy.test.py`，在 `TpmCalibrateStateTest` 班内插入（照抄 ：8788-8850 daemon 骨架，fake_send 恒返 `(401, False)`）。锚点（唯一）：

old_string:
```
    def test_calibrate_loop_capped_skips_probe_results(self) -> None:
```

new_string:
```python
    def test_calibrate_loop_401_skips_probe_results(self) -> None:
        """F2：daemon 收 credential_error 不写 PROBE_RESULTS、不走异常兜底，
        快照推荐源保持 none（recommend.source 不因凭证失败变 probe）。"""
        mod = self.mod
        orig_gap = mod.TPM_CALIBRATE_BATCH_GAP_S
        orig_interval = mod.TPM_CALIBRATE_MIN_INTERVAL_S
        mod.TPM_CALIBRATE_BATCH_GAP_S = 0.05
        mod.TPM_CALIBRATE_MIN_INTERVAL_S = 0
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_BATCH_GAP_S", orig_gap)
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_MIN_INTERVAL_S", orig_interval)
        orig_probe = {m: list(v) for m, v in mod.PROBE_RESULTS.items()}
        orig_recent = list(mod.RECENT_REQUESTS)

        def restore():
            mod.PROBE_RESULTS.clear()
            mod.PROBE_RESULTS.update(orig_probe)
            mod.RECENT_REQUESTS.clear()
            mod.RECENT_REQUESTS.extend(orig_recent)
        self.addCleanup(restore)
        mod.PROBE_RESULTS.clear()
        mod.RECENT_REQUESTS.append({"model": "m-cred"})

        def fake_send(model, batch, est, auth_token):
            return 401, False

        orig_send = mod._calibrate_send
        mod._calibrate_send = fake_send
        self.addCleanup(setattr, mod, "_calibrate_send", orig_send)

        thread = threading.Thread(target=mod._calibrate_loop, daemon=True)
        thread.start()

        def stop_loop():
            with mod.CALIBRATE_LOCK:
                mod._CALIBRATE_STATE["running"] = False
            mod._CALIBRATE_TOKEN = None
            thread.join(2)
        self.addCleanup(stop_loop)

        status, _ = mod.calibrate_start("m-cred", token="Bearer t")
        self.assertEqual(status, 200)

        result = None
        deadline = time.time() + 10
        while time.time() < deadline:
            with mod.CALIBRATE_LOCK:
                result = mod._CALIBRATE_STATE["result"]
            if result is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(
            result, "daemon 10s 内未产出 result（error=%r）"
            % mod._CALIBRATE_STATE["error"])
        self.assertEqual(result["outcome"], "credential_error")
        self.assertIsNone(result["threshold"])
        self.assertEqual(result["status"], 401)
        with mod.CALIBRATE_LOCK:
            self.assertIsNone(mod._CALIBRATE_STATE["error"],
                              "credential_error 是正常终止，不得走异常兜底")
        self.assertNotIn("m-cred", mod.PROBE_RESULTS,
                         "凭证失败不得写 PROBE_RESULTS")
        snap = mod.tpm_settings_snapshot()
        by_name = {m["name"]: m for m in snap["models"]}
        rec = by_name["m-cred"]["recommend"]
        self.assertEqual(rec["source"], "none",
                         "凭证失败 → 推荐源不得因本次探测变为 probe")
        self.assertIsNone(rec["probe"])

    def test_calibrate_loop_capped_skips_probe_results(self) -> None:
```

**红验证**（预期 `Ran 4 tests ... FAILED (failures=4)`——429 回归锚为常绿锚，红证据由其余 4 个承担；现状 401/403 走 ：478 → CLASS_REQUEST_FAULT → rejected）：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py \
    TpmCalibrateEngineTest.test_calibrate_engine_401_stops_credential_error \
    TpmCalibrateEngineTest.test_calibrate_engine_403_mid_ramp_aborts \
    TpmCalibrateEngineTest.test_calibrate_engine_401_on_retry_after_503_aborts \
    TpmCalibrateStateTest.test_calibrate_loop_401_skips_probe_results
Ran 4 tests in ...s
FAILED (failures=4)
```

### 步骤 2（绿）：实现（`ctyun-stream-fix-proxy.py` 6 处 Edit）

**Edit 1.3** — 新增哨兵常量（:209-210 配对行之后，不拆对；不进 `_KIND_CATEGORY`）。

old_string:
```
CLASS_BODY_ERROR = "body_error"
ERR_KIND_BODY_ERROR = "body_error"
```
new_string:
```
CLASS_BODY_ERROR = "body_error"
ERR_KIND_BODY_ERROR = "body_error"
# F2：校准凭证失败哨兵——仅 calibrate_engine 内部使用；不进 classify_outcome 词表、
# 不进 _KIND_CATEGORY 映射（relay 侧 401/403 仍按 R5 口径走 CLASS_REQUEST_FAULT）。
CLASS_CREDENTIAL_FAILURE = "credential_failure"
```

**Edit 1.4** — docstring 追加一行（:1102-1104 之间）。

old_string:
```
    send_one 返回 CALIBRATE_STATUS_TIMEOUT（读超时哨兵）→ probe_timeout，
    不重试、不翻倍（同批大请求是读挂上游的根因，R3）。
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
```
new_string:
```
    send_one 返回 CALIBRATE_STATUS_TIMEOUT（读超时哨兵）→ probe_timeout，
    不重试、不翻倍（同批大请求是读挂上游的根因，R3）。
    401/403（凭证失败）→ credential_error 立即中止、threshold=None、
    不重试（F2）。
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
```

**Edit 1.5** — `_attempt` 守卫（TIMEOUT 判后、classify_outcome 前；401/403 优先于 body_error——带 error JSON 的凭证失败仍是凭证问题）。

old_string:
```
        if status == CALIBRATE_STATUS_TIMEOUT:
            return CALIBRATE_STATUS_TIMEOUT, status
        return classify_outcome(status=status, body_error=body_error).category, status
```
new_string:
```
        if status == CALIBRATE_STATUS_TIMEOUT:
            return CALIBRATE_STATUS_TIMEOUT, status
        if status in (401, 403):
            # F2：凭证失败优先于 body_error——带 error JSON 的 401/403 仍是凭证问题
            return CLASS_CREDENTIAL_FAILURE, status
        return classify_outcome(status=status, body_error=body_error).category, status
```

**Edit 1.6** — 主循环守卫（:1154 前；8 空格缩进块 + 下行注释做唯一性区分）。

old_string:
```
        if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
            return _result("rejected", est, status)
        if category == CLASS_UPSTREAM_FAULT:
            # 上游故障不是限流信号：同一批重试一次（不递增 batch，est 不变）
```
new_string:
```
        if category == CLASS_CREDENTIAL_FAILURE:
            # F2：凭证失效不是限流信号——立即中止不重试（同凭证重试必然再败）
            return _result("credential_error", None, status)
        if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
            return _result("rejected", est, status)
        if category == CLASS_UPSTREAM_FAULT:
            # 上游故障不是限流信号：同一批重试一次（不递增 batch，est 不变）
```

**Edit 1.7** — 重试分支对称守卫（:1165 前；12 空格缩进块，与 Edit 1.6 异缩进互斥）。

old_string:
```
            if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
                return _result("rejected", est, status)
            if category == CLASS_UPSTREAM_FAULT:
                return _result("upstream_error", None, status)
```
new_string:
```
            if category == CLASS_CREDENTIAL_FAILURE:
                # F2：503 重试遇 401/403 同样立即中止——漏判会落 last_ok_est 继续加压
                return _result("credential_error", None, status)
            if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
                return _result("rejected", est, status)
            if category == CLASS_UPSTREAM_FAULT:
                return _result("upstream_error", None, status)
```

**Edit 1.8** — `_calibrate_loop` 补注释（:1446 前；仅注释，零功能改动）。

old_string:
```
        if result["outcome"] == "rejected" and result["threshold"] is not None:
            record_tpm_probe_result(model, {
```
new_string:
```
        # F2：credential_error（401/403 凭证失败）outcome≠"rejected" 且 threshold=None，
        # 天然不进本分支——凭证失败不写 PROBE_RESULTS，recommend.source 不变 probe。
        if result["outcome"] == "rejected" and result["threshold"] is not None:
            record_tpm_probe_result(model, {
```

### 步骤 3（验证 + commit）

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py TpmCalibrateEngineTest TpmCalibrateStateTest
Ran 35 tests in ...s
OK
```
（30 现有 + 5 新增；既有 calibrate 测试零改动零回归。）

schema 零变化核验（AC4 代理检查，diff 不得触及持久化键）：

```
$ git diff -U0 ctyun-stream-fix-proxy.py | grep -cE '^[+-].*(json\.dump|tpm_probe_results|tpm_body_err_samples|tpm_model_budgets|upstream_base|model_pricing)'
0
```

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
$ git commit -m "fix(calibrate): 401/403 凭证失败分类 credential_error 立即中止，不再误判 TPM 阈值拒绝持久化"
```

---

## Task 2（tier B）：dashboard calibDone credential_error 中止文案（textContent 门禁）

**文件**：`ctyun-stream-fix-proxy.py`（`_DASH_JS_V2`）+ `ctyun-stream-fix-proxy.test.py`
**TDD 顺序**：步骤 1 写 1 个测试实跑红 → 步骤 2 实现 1 处 Edit → 步骤 3 绿 → commit。

### 步骤 1（红）：先写失败测试

**Edit 2.1** — `ctyun-stream-fix-proxy.test.py`，`DashboardV2SkeletonTest` 班尾（既有 `test_calib_done_error_display` 同班，:3476 先例句式）。锚点（唯一）：

old_string:
```
class DashboardV3CardsTest(unittest.TestCase):
```

new_string:
```python
    def test_calib_done_credential_error_display(self) -> None:
        """F2：credential_error 上 UI 展示"校准中止"文案，经共享 textContent
        赋值（XSS 门禁，同 test_calib_done_error_display 句式）。"""
        js = self.mod._DASH_JS_V2
        self.assertIn('res.outcome === "credential_error"', js,
                      "calibDone 缺 credential_error 判定分支")
        self.assertIn('"校准中止：上游凭证无效"', js,
                      "calibDone 缺校准中止文案")
        self.assertIn('$("tpm-cali-status-text").textContent = txt;', js,
                      "校准中止文案必须经 textContent 赋值，不得走 innerHTML")


class DashboardV3CardsTest(unittest.TestCase):
```

**红验证**（预期 FAILED——`_DASH_JS_V2` 现状无 credential_error 文案）：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardV2SkeletonTest.test_calib_done_credential_error_display
Ran 1 test in ...s
FAILED (failures=1)
```

### 步骤 2（绿）：实现

**Edit 2.2** — `ctyun-stream-fix-proxy.py` `_DASH_JS_V2` calibDone。插入点定在 `txt += "。";`（:5618，全文件唯一）之后、textContent 赋值（:5619）之前：credential 分支整体替换 `txt`（自带句号），放 `txt += "。"` 之后避免叠加出双句号；`res.threshold` 分支与 `txt +=` 先执行再被覆盖，行为正确；textContent 赋值门禁保持，不动 innerHTML。

old_string:
```
  txt += "。";
  $("tpm-cali-status-text").textContent = txt;
```
new_string:
```
  txt += "。";
  if (res.outcome === "credential_error") {
    txt = "校准中止：上游凭证无效（HTTP " + (res.status || "401/403") +
          "），请检查 API Key 后重试。";
  }
  $("tpm-cali-status-text").textContent = txt;
```

### 步骤 3（验证 + commit）

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py DashboardV2SkeletonTest
Ran 8 tests in ...s
OK
```
（7 现有 + 1 新增。）

```
$ git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
$ git commit -m "fix(dashboard): 校准 credential_error 展示中止文案（textContent 门禁）"
```

---

## Whole-Branch 收尾节（Task 2 执行者同 dispatch 收尾，或主代理单独 dispatch）

1. 全量回归：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py
Ran 395 tests in ...s
OK
```
（389 baseline + 6 新增；零 failed / 零 error。）

2. Scope 核验（merge-base..HEAD 只允许 spec/PLAN/两源文件）：

```
$ git diff --stat ea3fbbc..HEAD
```

3. AC 逐条对照：

| AC | 证据 |
|---|---|
| AC1 全量绿（389+6=395） | 收尾节第 1 步 stdout |
| AC2 401/403 → credential_error/threshold None/不重试；429 → rejected+est | Task 1 步骤 3（`test_calibrate_engine_401_stops_credential_error` / `test_calibrate_engine_401_on_retry_after_503_aborts` / `test_calibrate_engine_429_still_rejected_regression`） |
| AC3 PROBE_RESULTS 无新条目 + REQ 行 `outcome=credential_error -> 401/403` | Task 1 `test_calibrate_loop_401_skips_probe_results`（PROBE_RESULTS/recommend.source/error 三断言）；REQ 行由 `_calibrate_log` :1313-1321 现状自动产出（`outcome=%s`/`-> %s` 直取 result），无新测试（spec 测试清单未列） |
| AC4 磁盘 schema 零变化 | Task 1 步骤 3 grep=0 + 收尾节 diff scope 核验 |
| AC5 dashboard credential_error 中文文案经 textContent | Task 2 步骤 3（`test_calib_done_credential_error_display`） |

---

## Plan Self-Review（作者自查 5 项）

1. **spec coverage**：spec Files §1-5（常量/_attempt/两分支/docstring/loop 注释）→ Task 1 Edit 1.3-1.8；§6（dashboard）→ Task 2 Edit 2.2；测试清单 4 engine + 1 loop + 1 dashboard → Edit 1.1/1.2/2.1 全数覆盖；AC1-5 → 各卡验证 + 收尾节对照表。**两处锚点细化已注明**：① loop 测试班实测为 `TpmCalibrateStateTest`（spec 写 "TpmCalibrateLoopTest" 为描述名）；② JS 插入点细化为 `txt += "。";` 之后（避免双句号），spec 的 "var txt 后" 意图不变。
2. **placeholder scan**：全部 Edit 为完整 old/new 文本，无 `...`/TODO/“仿照既有”指令；测试与实现代码块可直接转写。0 命中。
3. **type consistency**：`_result("credential_error", None, status:int)` 与 ：1120-1124 签名一致（batches 默认闭包 batch——401 首批=1、503→401=1、403 爬坡第 3 批=3，与断言吻合）；哨兵 `"credential_failure"`（category）vs `"credential_error"`（outcome 字符串）已显式区分；JS `(res.status || "401/403")` 字符串拼接合法；测试消费键与 result dict 六键（threshold/batches/consumed/outcome/status/ts）一致。
4. **可落盘性**：所有 old_string 实测文件内唯一（grep=1 或异缩进多行块互斥）；测试插入锚为唯一 def/class 行；targeted 验证命令已实跑打通（30 tests OK / 7 tests OK / 单方法 OK）；红态预期可判定（4 failures=4 / 1 failure=1）。
5. **锚点实测**：≥3 达标（共 17 项，见「锚点实测记录」表，全部现场 grep/read 回显，非 spec 转抄）。
