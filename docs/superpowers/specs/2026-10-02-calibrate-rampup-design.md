# 校准探测小起步阶梯递增（ramp-up）— 设计文档

状态：待实现（本 spec 落盘不 commit）
范围：单文件纯 stdlib Python 本地反代（主文件 `ctyun-stream-fix-proxy.py`，测试 `ctyun-stream-fix-proxy.test.py`，测试解释器固定 `/usr/bin/python3`）

## Goal

修复 TPM 主动校准探测"第一批就灌大 token 量"导致上游读超时、校准失败的问题：探测从**小 token 批量起步、阶梯递增**，并把读超时与部分失败单独分类，避免首轮 240KB（≈60k est）请求超时后重试翻倍到 120k 把上游读挂。

实测证据（commit 24dbb58）：`calibrate send failed model=kimi-k3-oc batch=1: The read operation timed out (×2)` + `consumed=120054 probe=1 dur=92.3s outcome=upstream_error`。根因：`TPM_CALIBRATE_INPUT_BYTES=240000`（`ctyun-stream-fix-proxy.py:115`）使第 1 批名义 est ≈ 60027（`estimate_request_tokens` :491 的 `len(body)*0.25 + max_tokens`）；`_calibrate_send` 用 `HEADER_TIMEOUT_S=45s`（:43）做整请求 socket 超时，240KB body 上游处理 >45s → 读超时 → `status=None` → `CLASS_UPSTREAM_FAULT` → 引擎对**同批重试一次**（:1061-1071）→ 累计 2×60027=120054，仍超时 → `upstream_error`，校准无结论。

## 候选方案与推荐

- **方案 A — 纯几何 ×2**：`START=4000, FACTOR=2.0`，批序列 4k/8k/16k/32k/64k/128k，6 批、累计 ~252k。最省轮数与时间，但阈值分辨率粗（bracket 最坏 2×），首拒 128k 时推荐值 115k 可能略高于真实 110k 限额。
- **方案 B（推荐）— 几何 ×2 到拐点 + 线性 +20000（两段 ramp-up）**：起步 4000，×2 至 32000，之后每批 +20000，单批上限 160000。批序列 4k/8k/16k/32k/52k/72k/92k/112k/132k/152k，到 112k 累计 ~388k（仍在 400k 硬顶内）。兼顾小起步、可接受轮数（8 批到 112k）与分辨率（bracket ≤20000，≈1.2×）。
- **方案 C — 两段 + 近阈值二分细化**：在 B 基础上首拒后于 [last_ok, first_reject] 二分。分辨率最高，但违背既有 R4"非二分"决策（上游限流为窗口阶跃、非单调连续），实现复杂且累计消耗更高，不推荐。

**推荐方案 B**：首轮仅 ~4k est（≈16KB body，远低于触发读超时的 240KB），几何段快速脱离低区，线性段把近阈值 bracket 压到 20000，推荐值偏差可控。

## Files to Change

所有生产代码进 `ctyun-stream-fix-proxy.py`；测试进 `ctyun-stream-fix-proxy.test.py`。锚点为当前工作树行号。

- `ctyun-stream-fix-proxy.py:114-122`（TPM 校准常量区）——**删除** `TPM_CALIBRATE_INPUT_BYTES`（240000，死代码根源）；**新增** `TPM_CALIBRATE_START_TOKENS`（起步名义 est，env `CTYUN_CALIBRATE_START_TOKENS`，默认 `4000`）、`TPM_CALIBRATE_RAMP_FACTOR`（几何倍率，env `CTYUN_CALIBRATE_RAMP_FACTOR`，默认 `2.0`）、`TPM_CALIBRATE_KNEE_TOKENS`（几何→线性拐点，env `CTYUN_CALIBRATE_KNEE_TOKENS`，默认 `32000`）、`TPM_CALIBRATE_MAX_PROBE_TOKENS`（单批名义 est 上限/终止，env `CTYUN_CALIBRATE_MAX_PROBE_TOKENS`，默认 `160000`）、`TPM_CALIBRATE_TIMEOUT_S`（单批 socket 超时，env `CTYUN_CALIBRATE_TIMEOUT_S`，默认 `60`）；**改默认** `TPM_CALIBRATE_STEP_TOKENS` 5000→`20000`（线性段步长）。`TPM_CALIBRATE_MAX_TOKENS=1` / `BATCH_GAP_S=2.0` / `MAX_DURATION_S=240` / `MIN_INTERVAL_S=1800` / `HARD_CAP_TOKENS=400000` / `SETTLE_WAIT_S=60` 保持不变。
- `ctyun-stream-fix-proxy.py:1079-1080` `_CALIBRATE_BYTES_PER_STEP`——**删除**（被 `calibrate_input_bytes` 取代）。
- `ctyun-stream-fix-proxy.py:991-1002` `build_calibrate_body(model, input_bytes)`——**不改签名**；紧随其后**新增三个纯函数**（单一调度源，保证引擎 est 与发送器字节一致）：
  - `calibrate_target_est(batch: int) -> int`：第 batch 批（1-based）名义目标 est——起步 `TPM_CALIBRATE_START_TOKENS`；`est < TPM_CALIBRATE_KNEE_TOKENS` 时 `est = min(int(est*TPM_CALIBRATE_RAMP_FACTOR), TPM_CALIBRATE_KNEE_TOKENS)`，否则 `est += TPM_CALIBRATE_STEP_TOKENS`。
  - `calibrate_input_bytes(batch: int) -> int`：`max(1, int((calibrate_target_est(batch) - TPM_CALIBRATE_MAX_TOKENS) / max(TPM_TOKEN_RATIO, 0.01)))`。
  - `calibrate_est_for_batch(model, batch) -> int`：`estimate_request_tokens(json.dumps(build_calibrate_body(model, calibrate_input_bytes(batch)), ensure_ascii=False).encode("utf-8"))`（与发送器同源的实际 est）。
- `ctyun-stream-fix-proxy.py:1005-1075` `calibrate_engine(model, send_one, ...)`——**替换线性公式** `est = base + (batch-1)*TPM_CALIBRATE_STEP_TOKENS`（:1053）为 `est = calibrate_est_for_batch(model, batch)`；删除一次性 `base` 计算（:1026-1027）；**新增上限终止**：`est > TPM_CALIBRATE_MAX_PROBE_TOKENS` → 返回 `_result("capped", None, None, batches=batch-1)`（不发送该批，与 HARD_CAP 分支并列）；**新增读超时分支**：`status == CALIBRATE_STATUS_TIMEOUT` → 返回 `_result("probe_timeout", last_ok_est, None)`（不重试、不翻倍）。保留 abort / 墙钟 / HARD_CAP / 首拒即停 / 5xx 单次重试语义。
- `ctyun-stream-fix-proxy.py` 常量区（:114 附近）——新增 `CALIBRATE_STATUS_TIMEOUT = "timeout"`（字符串哨兵，与 int HTTP 状态码区分）。
- `ctyun-stream-fix-proxy.py:1152-1200` `_calibrate_send(model, batch, est, auth_token)`——body 构造改用 `input_bytes = calibrate_input_bytes(batch)`（替换 :1164-1165 的 `TPM_CALIBRATE_INPUT_BYTES + (batch-1)*_CALIBRATE_BYTES_PER_STEP`）；socket 超时改用 `TPM_CALIBRATE_TIMEOUT_S`（替换 :1177/:1180 的 `HEADER_TIMEOUT_S`）；**单独捕获** `socket.timeout`（即 `TimeoutError`，OSError 子类）→ `_safe_log_stderr` 留痕后返回 `(CALIBRATE_STATUS_TIMEOUT, False)`；其余 `(OSError, http.client.HTTPException)` 仍返回 `(None, False)`（连接级异常，引擎走 5xx 重试路径）。
- `ctyun-stream-fix-proxy.py:1342-1349` `_calibrate_loop` 收尾——**保持**仅 `outcome=="rejected"` 才 `record_tpm_probe_result`；新增 `capped` / `probe_timeout` outcome 不写 `PROBE_RESULTS`（无可靠拒绝阈值），仅经 `_calibrate_log` 落摘要。
- `ctyun-stream-fix-proxy.py:1203-1218` `_calibrate_log`——可选在摘要行追加 `batch=%d`（`result["batches"]`）便于观测；不改其余字段。
- `ctyun-stream-fix-proxy.test.py:2973-2985` `test_tpm_calibrate_constants_defaults`——更新为新常量默认值断言（START 4000 / RAMP_FACTOR 2.0 / KNEE 32000 / STEP 20000 / MAX_PROBE 160000 / TIMEOUT 60），并 `assertFalse(hasattr(mod, "TPM_CALIBRATE_INPUT_BYTES"))`。
- `ctyun-stream-fix-proxy.test.py:2987-3009` `test_tpm_calibrate_constants_env_seam`——env 键改为 `CTYUN_CALIBRATE_START_TOKENS` / `CTYUN_CALIBRATE_RAMP_FACTOR` / `CTYUN_CALIBRATE_KNEE_TOKENS` / `CTYUN_CALIBRATE_MAX_PROBE_TOKENS` / `CTYUN_CALIBRATE_TIMEOUT_S` / `CTYUN_CALIBRATE_STEP_TOKENS`。
- `ctyun-stream-fix-proxy.test.py:7697-7883` `TpmCalibrateEngineTest`——把 `base + (batch-1)*step` 线性断言改为基于 `calibrate_est_for_batch`/`calibrate_target_est` 的调度断言；新增 ramp 与超时用例（见 AC）。
- `ctyun-stream-fix-proxy.test.py:8254-8301` `CalibrateSendTest`——`_calibrate_send` 签名不变（`("m", 1, 60013, "Bearer t")`），内部字节推导变化不影响这些断言；新增读超时用例（fake upstream 延迟 > `TPM_CALIBRATE_TIMEOUT_S` 或 monkeypatch socket）。

## Acceptance Criteria

全部测试用 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 运行；定向跑校准相关用 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py -k calibrate -v`。测试基建复用 `load_proxy_module`（:627）/ `make_fake_upstream` / `make_scripted_upstream`（:325）/ `start_proxy`。

验收命令：

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py -k calibrate -v
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

必须全绿（现有用例按上文更新后 + 新增用例，0 failure / 0 error）。

AC1 — 常量默认值（单元级）：`TPM_CALIBRATE_START_TOKENS==4000`、`TPM_CALIBRATE_RAMP_FACTOR==2.0`、`TPM_CALIBRATE_KNEE_TOKENS==32000`、`TPM_CALIBRATE_STEP_TOKENS==20000`、`TPM_CALIBRATE_MAX_PROBE_TOKENS==160000`、`TPM_CALIBRATE_TIMEOUT_S==60`；`TPM_CALIBRATE_INPUT_BYTES` 已删除（`hasattr` 为 False）。

AC2 — 调度纯函数（单元级）：`calibrate_target_est(1..11)` 返回 `[4000, 8000, 16000, 32000, 52000, 72000, 92000, 112000, 132000, 152000, 172000]`；`calibrate_input_bytes(1)` ≈ `(4000-1)/0.25 ≈ 15996`；`calibrate_est_for_batch("m", 1)` 落在 `[4000, 4300]`（证明首批 ≈4k，不再 60k）。

AC3 — 小起步（单元级）：脚本化发送器全 200 时，`calibrate_engine` 第 1 批 `est <= 4400`（START×1.1），且前 8 批 est 严格单调递增。

AC4 — 首拒即停（更新既有用例）：发送器前 7 批 200、第 8 批 429 → `outcome=="rejected"`、`batches==8`、`threshold == calibrate_est_for_batch("m", 8)`（≈112000 量级）、`status==429`、发送器恰好调用 8 次。

AC5 — 单批上限终止（单元级）：`TPM_CALIBRATE_MAX_PROBE_TOKENS` 临时调小（如 `100000`）→ 前 7 批（至 92000）发送后，第 8 批目标 112000>100000 → `outcome=="capped"`、`batches==7`、发送器恰好 7 次（不超发）。

AC6 — 读超时单独分类（单元级）：发送器返回 `(CALIBRATE_STATUS_TIMEOUT, False)` → `outcome=="probe_timeout"`、发送器**只调用 1 次**（不重试、不翻倍）、`threshold == last_ok`（首轮则 None）、不写 `PROBE_RESULTS`。

AC7 — 累计/墙钟/abort 回归（单元级，更新既有用例）：HARD_CAP 超顶仍 `capped` 不超发；`MAX_DURATION_S=0` 首轮即 `timeout`；批间 abort 仍 `aborted`；5xx 重试一次后仍败仍 `upstream_error`。

AC8 — 三源推荐不受影响（单元级 + 集成级）：`tpm_budget_recommend` 的 probe 分支公式不变（`threshold×0.9` 千位向下）；当探测以 `capped`/`probe_timeout` 终止（无 reject）时，`GET /api/tpm_settings` 的 `recommend.source` 不因本次探测变为 `"probe"`（回落 body_err/usage/none）。

AC9 — 回归：全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 绿。

## Risks

- R1 — **累计消耗逼近硬顶**：方案 B 到 112k 累计 ~388k，接近 `HARD_CAP_TOKENS=400000`。若目标模型真实限额 >112k，会在第 9 批被 HARD_CAP 截断（`capped`，无阈值结论）。缓解：保持 400000 默认；如需覆盖更高限额，后续单独评估提高 HARD_CAP（本轮不改）。
- R2 — **阈值分辨率**：几何段 bracket 最坏 2×，线性段 20000（≈1.2×）。`recommended = threshold×0.9` 在最坏情形（真实限额仅略高于 last_ok）可能略高于真实限额（~5-10%）。缓解：KNEE 后线性步长 20000 把 bracket 压到 20000；如需更保守，后续可把推荐公式改为 `min(threshold×0.9, last_ok)`（本轮不改）。
- R3 — **读超时语义**：把 `socket.timeout` 单独分类可避免重试翻倍，但可能把"上游偶发慢"误判为探测上限。缓解：`probe_timeout` 不写 `PROBE_RESULTS`（不污染推荐源）；如需更鲁棒，后续可加"缩小批次重试一次"（本轮不做）。
- R4 — **常量删除兼容面**：删除 `TPM_CALIBRATE_INPUT_BYTES` 影响 4 处测试（:2975/:2991/:3005/:7719-7735）；env `CTYUN_CALIBRATE_INPUT_BYTES` 失效（在常量注释注明）。
- R5 — **调度一致性**：引擎 est 与发送器字节必须同源（`calibrate_input_bytes` / `calibrate_est_for_batch`），否则 HARD_CAP/MAX_PROBE 判定与实际发送错位。缓解：单一调度源 + AC2/AC3 单测锁定。
- R6 — **既有线性用例**：`TpmCalibrateEngineTest` 多个用例断言 `base + (n-1)*step`，不改调度断言会红。缓解：按 Files 条目同步更新。

## Exclusions

本次**不做**：

- 不做二分/二分细化（维持既有 R4"非二分"决策）。
- 不改 `TPM_CALIBRATE_HARD_CAP_TOKENS` 默认值（保持 400000）。
- 不改三源推荐优先级（探测实测 > body-err 样本 > 历史用量）与 `threshold×0.9` 推荐公式。
- 不改 `_calibrate_loop` 的 settle 等待 / 凭证来源（`_LAST_AUTH`）/ 最小间隔 / 日志机制。
- 不改 `classify_outcome`（:406）的判定优先级与类别常量。
- 不动 FIFO 排队 / `tpm_admit` / `TPM_MODEL_BUDGETS`。
- 不做自动/定时校准（仍仅 Dashboard 手动触发）。
- 不改 Dashboard UI（本轮仅后端调度；如需展示 last_ok/bracket 另议）。
- 不新增依赖（纯 stdlib）。

## R31 Evidence

[R31-S1] 现场命中（确认问题存在）：

$ grep 'calibrate' ~/.local/log/ctyun-fwd.err | tail -3

```
ctyun-stream-fix-proxy: calibrate send failed model=kimi-k3-oc batch=1: The read operation timed out
ctyun-stream-fix-proxy: calibrate send failed model=kimi-k3-oc batch=1: The read operation timed out
REQ POST /v1/chat/completions -> - dur=92.3s result=calibrate outcome=upstream_error consumed=120054 probe=1 ts=2026-10-02T12:22:41+0800
```

$ grep -n 'TPM_CALIBRATE_INPUT_BYTES\|HEADER_TIMEOUT_S =\|TPM_TOKEN_RATIO =\|TPM_CALIBRATE_STEP_TOKENS =' ctyun-stream-fix-proxy.py | head -8

```
43:HEADER_TIMEOUT_S = max(1.0, float(os.environ.get("CTYUN_HEADER_TIMEOUT", "45")))
111:TPM_TOKEN_RATIO = float(os.environ.get("CTYUN_TPM_TOKEN_RATIO", "0.25"))
115:TPM_CALIBRATE_INPUT_BYTES = int(os.environ.get("CTYUN_CALIBRATE_INPUT_BYTES", "240000"))
116:TPM_CALIBRATE_STEP_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_STEP_TOKENS", "5000"))
1010:    base = estimate(序列化后的 build_calibrate_body(model, TPM_CALIBRATE_INPUT_BYTES))——
1026:    base = estimate(json.dumps(build_calibrate_body(model, TPM_CALIBRATE_INPUT_BYTES),
1164:    input_bytes = TPM_CALIBRATE_INPUT_BYTES \
```

$ grep -n 'def estimate_request_tokens' ctyun-stream-fix-proxy.py

```
491:def estimate_request_tokens(body):
```

[R31-S2] 根因（带证据）：首批名义 est≈60027——`TPM_CALIBRATE_INPUT_BYTES=240000`（`ctyun-stream-fix-proxy.py:115`）使 `build_calibrate_body` 产出约 240KB body，`estimate_request_tokens`（`ctyun-stream-fix-proxy.py:491`）按 `len(body)*TPM_TOKEN_RATIO(0.25, :111) + max_tokens(1)` 得 ≈60027。`_calibrate_send`（:1152-1200）复用 `HEADER_TIMEOUT_S=45s`（:43，见 :1177/:1180 传入 `HTTPConnection/HTTPSConnection(timeout=...)`）做整请求 socket 超时；240KB body 上游处理 >45s → 读超时 → `status=None` → `classify_outcome` 判 `CLASS_UPSTREAM_FAULT` → `calibrate_engine`（:1061-1071）对**同批重试一次**且 est 不变 → 累计 consumed=2×60027=120054 仍超时 → `upstream_error`，校准无结论。与 Goal 段一致；现场日志 `consumed=120054 probe=1 outcome=upstream_error` 与此推算吻合。
