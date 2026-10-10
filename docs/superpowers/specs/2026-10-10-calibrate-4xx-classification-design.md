# Episode F2 — calibrate 401/403 凭证失败不再误判为 TPM 阈值拒绝（simplified spec）

## Goal

校准探测遇上游 401/403（凭证失效）时立即中止且不把该批次 est 当"TPM 阈值拒绝"持久化（当前 status>=400 统一判拒绝并写 PROBE_RESULTS，污染预算推荐），429 语义完全不变。

## Files to Change

### ctyun-stream-fix-proxy.py（1 文件，全部函数级锚点）

1. **`calibrate_engine` :1090（新增内部哨兵常量）** — 模块常量区 :209 后新增 `CLASS_CREDENTIAL_FAILURE = "credential_failure"`（仅 engine 内部使用，不进 classify_outcome 词表、不进 ERR_KIND 映射）。
2. **`calibrate_engine._attempt` :1126** — 在 `classify_outcome` 调用（:1134）之前插入守卫：
   ```python
   if status in (401, 403):
       return CLASS_CREDENTIAL_FAILURE, status
   ```
   放 CALIBRATE_STATUS_TIMEOUT 判后、classify_outcome 前（401/403 优先于 body_error 判定——凭证失败即使带 error JSON 也是凭证问题）。
3. **`calibrate_engine` 主循环 :1154 与重试分支 :1165** — 两处 `if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT)` 之前各插入：
   ```python
   if category == CLASS_CREDENTIAL_FAILURE:
       return _result("credential_error", None, status)
   ```
   两处都必须插：重试分支若漏判，401/403 会落到 `last_ok_est = est` 继续加压（比现状更糟）。不重试——同凭证重试必然再败。
4. **`calibrate_engine` docstring :1098-1111** — 追加一行："401/403（凭证失败）→ credential_error 立即中止、threshold=None、不重试（F2）"。
5. **`_calibrate_loop` :1446** — 现状 `if result["outcome"] == "rejected" and result["threshold"] is not None:` 天然排除 credential_error（threshold=None 且 outcome≠rejected），`record_tpm_probe_result` **不改**；`_calibrate_log` :1454 无条件执行，credential_error 自动落 REQ 行（`outcome=credential_error`、`-> 401/403`）。仅补注释说明。
6. **`_DASH_JS_V2` `calibDone` :5612-5617** — `var txt = "校准完成：" + ...` 后插入：
   ```js
   if (res.outcome === "credential_error") {
     txt = "校准中止：上游凭证无效（HTTP " + (res.status || "401/403") +
           "），请检查 API Key 后重试。";
   }
   ```
   保持 textContent 赋值门禁（同 :5619 现状），不动 innerHTML。

### ctyun-stream-fix-proxy.test.py（沿用 `_scripted` 手法 :8347 与 `test_calibrate_loop_*` daemon 手法 :8679-8910）

1. **`TpmCalibrateEngineTest`（:8391 同班）新增**：
   - `test_calibrate_engine_401_stops_credential_error`：`self._scripted([(401, False)])` → outcome=="credential_error"、threshold None、status 401、batches 1、consumed=est(1)、calls 1（无重试）。
   - `test_calibrate_engine_403_mid_ramp_aborts`：`_scripted([(200, False)]*2 + [(403, False)])` → outcome=="credential_error"、threshold None、status 403、batches 3。
   - `test_calibrate_engine_429_still_rejected_regression`：`_scripted([(200, False)]*7 + [(429, False)])` → 断言与 :8391 现状逐字相同（outcome=="rejected"、threshold=est(8)、status 429）——429 语义不变的可确定性锚。
   - `test_calibrate_engine_401_on_retry_after_503_aborts`：`_scripted([(503, False), (401, False)])` → outcome=="credential_error"、calls 2（503 重试一次后 401 立即中止，不得继续加压）。
2. **`TpmCalibrateLoopTest`（:8788 同班）新增** `test_calibrate_loop_401_skips_probe_results`：照抄 :8788-8842 daemon 骨架，fake_send 恒返 `(401, False)` → 轮询取 result 断言 outcome=="credential_error"、`self.assertNotIn("m-cred", mod.PROBE_RESULTS)`、`mod._CALIBRATE_STATE["error"] is None`（不走异常兜底）。
3. **Dashboard JS 断言（:3476 同班）** 扩展 `test_calib_done_credential_error_display`：`self.assertIn('"校准中止：上游凭证无效"', self.mod._DASH_JS_V2)` + textContent 门禁断言（同 :3483 句式）。

## Acceptance Criteria

1. `python3 -m unittest ctyun-stream-fix-proxy.test.py` 全量绿（baseline 389 个，本 episode 新增约 5 个；既有 calibrate 测试零改动零回归）。
2. 401/403 探测：`calibrate_engine` 返回 outcome=="credential_error"、threshold None、不重试；429 探测返回 outcome=="rejected" + threshold=该批 est（回归锚）。
3. `_calibrate_loop` 收 401/403 结果后 `PROBE_RESULTS` 无新条目（`/api/tpm_settings` recommend.source 不因凭证失败变成 "probe"）；REQ 行含 `probe=1 outcome=credential_error -> 401/403`。
4. 磁盘 schema 零变更：persist 文件顶层键集合与 tpm_probe_results 条目键集合与 ea3fbbc 完全一致（grep save_stats_counters :1793-1801 验证）。
5. Dashboard `/` JS 含 credential_error 中文文案且经 textContent 赋值。

## Risks

1. **磁盘 schema（E1 前科）**：本设计**零 schema 变更**——credential_error 只出现在内存 state.result 与 LOG_RING（不持久化）；PROBE_RESULTS 对 credential_error 根本不写条目。E1 spec（docs/superpowers/specs/2026-10-09-persist-race-snapshot-design.md:74）明令"键增删需独立 episode"，任何想在 tpm_probe_results 条目加 kind 字段的方案一律否决。
2. **漏插重试分支 :1165**：若只在主循环 :1154 插守卫，503→401 序列会继续加压（实现时必须两处对称，AC1 的 `test_calibrate_engine_401_on_retry_after_503_aborts` 专门锁这个）。
3. **classify_outcome 共享路径**：:448 是 relay 与 calibrate 共用网关，relay 侧 4xx→CLASS_REQUEST_FAULT（counts_error=False、log "upstream-err"）是 R5 既定口径，**不得改**——守卫只加在 calibrate 的 `_attempt` 内。401/403 之外的 4xx（400/404/422）在 calibrate 中仍按现状判 rejected（followups 范围仅 401/403）。
4. **Dashboard 文案测试脆性**：:3476-3484 已有 JS 字符串断言先例，新增断言须沿用 `assertIn` 精确子串，避免匹配 `_DASH_JS_V2` 拼接差异。

## Exclusions

- 不动 `classify_outcome` :448-483、不动 ERR_KIND→CLASS 映射 :213-222、不动 relay 路径 4xx 语义。
- 不动 TPM 限流准入（`tpm_admit` :618 / `TPM_MODEL_BUDGETS` / `tpm_budget_recommend` :932）——只堵住错误证据的入口。
- 不做 400/404/408/422 等其他 4xx 的分类细化（独立 followup）。
- 不改磁盘 schema、不加 env seam、不加依赖、不建 worktree 外文件。

## R31 Evidence

[R31-S1] 现状判定分支（grep 回显）：

```
$ grep -n "400 <= status < 500" ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:478:    if 400 <= status < 500:

$ grep -n "category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT)" ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:1154:        if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
ctyun-stream-fix-proxy.py:1165:            if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):

$ grep -n "status in (401, 403)" ctyun-stream-fix-proxy.py || echo "NO MATCH — 现状无任何 401/403 区分"
NO MATCH — 现状无任何 401/403 区分
```

```
$ grep -n "record_tpm_probe_result\|tpm_probe_results" ctyun-stream-fix-proxy.py | head -8
ctyun-stream-fix-proxy.py:1447:            record_tpm_probe_result(model, {
ctyun-stream-fix-proxy.py:1784:        probe_results = {m: [dict(r) for r in items]
ctyun-stream-fix-proxy.py:1969:def record_tpm_probe_result(model, result) -> None:
```

[R31-S2] 根因（带 file:line）：`classify_outcome` :478-480 把 `400 <= status < 500` 一律归 `CLASS_REQUEST_FAULT`；`calibrate_engine` :1154（主循环）与 :1165（503 重试分支）把该类别直接判 `rejected` 且 threshold=本批 est；`_calibrate_loop` :1446-1453 对 outcome=="rejected" 无条件 `record_tpm_probe_result` 落 PROBE_RESULTS → :1784-1799 持久化进磁盘；`tpm_budget_recommend` :949-963 拿该阈值出 `source="probe"` 的 0.9× 推荐（401 首批评测 est≈4000 → 推荐 3000 tokens）。凭证失效被当成"上游 4000 tokens 即限流"的实证并持久化——后续按推荐启用模型即被无谓限流。
