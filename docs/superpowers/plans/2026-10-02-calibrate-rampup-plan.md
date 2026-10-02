# PLAN — 校准探测小起步阶梯递增（ramp-up）

- **Feature:** fix/calibrate-rampup
- **Branch:** fix/calibrate-rampup
- **Date:** 2026-10-02
- **Spec:** `docs/superpowers/specs/2026-10-02-calibrate-rampup-design.md`
- **Baseline（本 worktree 实测）:** HEAD `177f8eb`；全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` → `Ran 353 tests in 154.507s — OK`（exit 0）；定向 `-k calibrate` → `Ran 30 tests in 20.031s — OK`（exit 0）
- **分段写作:** 否（spec Files 10 条但全部落在 2 个物理文件（`ctyun-stream-fix-proxy.py` / `.test.py`）上，聚合为 3 张功能卡 < 5；B0/B1..Bn 分段无独立骨架可拆，单次 dispatch 完成全部卡）

## Global Constraints

- 改动文件仅两个：`ctyun-stream-fix-proxy.py`（生产）与 `ctyun-stream-fix-proxy.test.py`（测试）。不改 README/docs/其他文件；不新增依赖（纯 stdlib）。
- 测试解释器固定 `/usr/bin/python3`（实测 Python 3.9.6；`socket.timeout is TimeoutError` 实测为 `False`，故「先 `except socket.timeout` 再 `except (OSError, HTTPException)`」的顺序在 3.9 上成立且必需）。
- 卡序即执行序即提交序：每卡内「先生产编辑（测试此刻必红）→ 测试编辑 → 焦点 green」，green 后才进入下一卡；卡 3 末尾跑全量。TDD 红阶段实测证据见 Anchor Verification。
- PLAN-B 写作时已做全链模拟：把三卡全部 old/new 按本文件顺序作用到临时副本（`/tmp/calib-chain/`），prod 中间态均可 `py_compile`，最终产物与本 PLAN 验证过的绿版逐字节一致；焦点实测卡 1 `Ran 34 — OK` / 卡 2 `Ran 40 — OK` / 卡 3 `Ran 43 — OK`，全量 `Ran 366 tests — OK`。执行者仍须在真实 worktree 重跑并贴 stdout + exit code。
- 每个 old_string 均实测唯一（`str.count == 1`）。spec 中的测试行号（:2973/:2987/:7697/:8254）比实际行号小 1（实际 :2974/:2988/:7698/:8255），**以 old_string 逐字匹配为准，不依赖行号**。
- 生产常量删除：`TPM_CALIBRATE_INPUT_BYTES`（含 env `CTYUN_CALIBRATE_INPUT_BYTES`）与 `_CALIBRATE_BYTES_PER_STEP` 移除；常量区注释保留「已删除」说明，故 `grep -n "_CALIBRATE_BYTES_PER_STEP" ctyun-stream-fix-proxy.py` 完成后预期恰 1 命中（注释行 :117），非代码引用。
- 测试总数 353 → 366（新增 13 个用例 + 1 个用例改名 `..._linear_press_first_reject_stops` → `..._ramp_first_reject_stops`）。全量预期输出 `Ran 366 tests — OK`，exit 0。
- 不做（spec Exclusions 全文）：二分/二分细化；改 `TPM_CALIBRATE_HARD_CAP_TOKENS` 默认；改三源推荐优先级与 `threshold×0.9` 公式；改 settle/凭证/最小间隔/日志机制；改 `classify_outcome`；动 FIFO/`tpm_admit`/`TPM_MODEL_BUDGETS`；自动/定时校准；Dashboard UI；新依赖。
- 风格对齐：中文注释、既有命名习惯（`_safe_log_stderr` 留痕、`sanitize` 式显式分类、纯函数 docstring 说明「纯函数」）。

---

## 卡 1：ramp-up 常量 + 三个纯函数 + 引擎调度替换（tier A）

**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

**目标：** AC1/AC2（除 `hasattr` 断言在卡 1 测试内已含）/AC3/AC4。删除 240KB 固定 body 根源；新调度为「起步 4000 → 几何 ×2 至 32000 → 线性 +20000」；`calibrate_input_bytes` / `calibrate_est_for_batch` 与发送器同源（R5）。

### 生产编辑（按序执行；每步 old_string 实测唯一）

#### 编辑 1.1 — 常量区替换（prod :114-122）

old（9 行）：

```
# TPM 校准常量（主动校准探测；全部 env seam 以便测试加速）
TPM_CALIBRATE_INPUT_BYTES = int(os.environ.get("CTYUN_CALIBRATE_INPUT_BYTES", "240000"))
TPM_CALIBRATE_STEP_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_STEP_TOKENS", "5000"))
TPM_CALIBRATE_MAX_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_MAX_TOKENS", "1"))
TPM_CALIBRATE_BATCH_GAP_S = float(os.environ.get("CTYUN_CALIBRATE_BATCH_GAP_S", "2.0"))
TPM_CALIBRATE_MAX_DURATION_S = float(os.environ.get("CTYUN_CALIBRATE_MAX_DURATION_S", "240"))
TPM_CALIBRATE_MIN_INTERVAL_S = float(os.environ.get("CTYUN_CALIBRATE_MIN_INTERVAL_S", "1800"))
TPM_CALIBRATE_HARD_CAP_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_HARD_CAP_TOKENS", "400000"))
TPM_CALIBRATE_SETTLE_WAIT_S = float(os.environ.get("CTYUN_CALIBRATE_SETTLE_WAIT_S", "60"))
```

new（12 行）：

```
# TPM 校准常量（主动校准探测；全部 env seam 以便测试加速）
# ramp-up（方案 B）：起步 START → ×RAMP_FACTOR 几何递增至 KNEE → 之后每批 +STEP_TOKENS；
# 单批名义 est 超过 MAX_PROBE_TOKENS 即终止（capped）。旧 CTYUN_CALIBRATE_INPUT_BYTES
# 与 _CALIBRATE_BYTES_PER_STEP 已删除（env 失效），输入字节由 calibrate_input_bytes 单一调度。
TPM_CALIBRATE_START_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_START_TOKENS", "4000"))
TPM_CALIBRATE_RAMP_FACTOR = float(os.environ.get("CTYUN_CALIBRATE_RAMP_FACTOR", "2.0"))
TPM_CALIBRATE_KNEE_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_KNEE_TOKENS", "32000"))
TPM_CALIBRATE_STEP_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_STEP_TOKENS", "20000"))
TPM_CALIBRATE_MAX_PROBE_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_MAX_PROBE_TOKENS", "160000"))
TPM_CALIBRATE_TIMEOUT_S = float(os.environ.get("CTYUN_CALIBRATE_TIMEOUT_S", "60"))
TPM_CALIBRATE_MAX_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_MAX_TOKENS", "1"))
TPM_CALIBRATE_BATCH_GAP_S = float(os.environ.get("CTYUN_CALIBRATE_BATCH_GAP_S", "2.0"))
TPM_CALIBRATE_MAX_DURATION_S = float(os.environ.get("CTYUN_CALIBRATE_MAX_DURATION_S", "240"))
TPM_CALIBRATE_MIN_INTERVAL_S = float(os.environ.get("CTYUN_CALIBRATE_MIN_INTERVAL_S", "1800"))
TPM_CALIBRATE_HARD_CAP_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_HARD_CAP_TOKENS", "400000"))
TPM_CALIBRATE_SETTLE_WAIT_S = float(os.environ.get("CTYUN_CALIBRATE_SETTLE_WAIT_S", "60"))
```

#### 编辑 1.2 — `build_calibrate_body` 之后插入三个纯函数，并删引擎 `estimate=` 形参（prod :1000-1006）

old（6 行）：

```
        "messages": [{"role": "user", "content": "x" * max(0, input_bytes)}],
    }


def calibrate_engine(model, send_one, should_abort=None, sleep=None,
                     estimate=estimate_request_tokens):
```

new（40 行）：

```
        "messages": [{"role": "user", "content": "x" * max(0, input_bytes)}],
    }


def calibrate_target_est(batch: int) -> int:
    """第 batch 批（1-based）名义目标 est：几何 ×RAMP_FACTOR 至 KNEE，之后线性 +STEP。

    纯函数；batch ≤ 1 返回起步值（fail-open，不抛出）。
    """
    est = TPM_CALIBRATE_START_TOKENS
    for _ in range(1, max(1, int(batch))):
        if est < TPM_CALIBRATE_KNEE_TOKENS:
            est = min(int(est * TPM_CALIBRATE_RAMP_FACTOR),
                      TPM_CALIBRATE_KNEE_TOKENS)
        else:
            est += TPM_CALIBRATE_STEP_TOKENS
    return est


def calibrate_input_bytes(batch: int) -> int:
    """第 batch 批的 content 填充字节数：由名义目标 est 反推
    （(target - max_tokens) / TPM_TOKEN_RATIO，下限 1）。

    与 calibrate_est_for_batch 共用 calibrate_target_est 单一调度源（R5），
    保证引擎 est 判定与实际发送字节不漂移。纯函数。
    """
    return max(1, int((calibrate_target_est(batch) - TPM_CALIBRATE_MAX_TOKENS)
                      / max(TPM_TOKEN_RATIO, 0.01)))


def calibrate_est_for_batch(model: str, batch: int) -> int:
    """第 batch 批按发送器同源体计算的实际 est（准入口径 estimate_request_tokens）。

    序列化口径与 _calibrate_send 一致：json.dumps(..., ensure_ascii=False).encode("utf-8")。
    纯函数（仅依赖入参与模块常量）。
    """
    body = build_calibrate_body(model, calibrate_input_bytes(batch))
    return estimate_request_tokens(
        json.dumps(body, ensure_ascii=False).encode("utf-8"))


def calibrate_engine(model, send_one, should_abort=None, sleep=None):
```

（注：`estimate=` 形参删除后无调用方受影响——实测全库仅定义点 :1006 与测试调用点，无一处传 `estimate=`。）

#### 编辑 1.3 — 引擎 docstring 重写（prod :1007-1023）

old（18 行）：

```
    """校准引擎（决策 4/5 纯逻辑；发送器/abort/sleep/estimate 全部注入）。

    线性逐批加压：第 batch 批名义 est = base + (batch-1) * TPM_CALIBRATE_STEP_TOKENS，
    base = estimate(序列化后的 build_calibrate_body(model, TPM_CALIBRATE_INPUT_BYTES))——
    首批 est 由准入口径 estimate 实测，保证逐级加压真实作用于限流判定（AC9）；
    序列化口径与真发送器（卡 5）共享：json.dumps(..., ensure_ascii=False).encode("utf-8")。
    每批经 send_one(batch, est) 发送，返回 (status, body_error)；status=None 表示
    连接级异常（无 HTTP 响应）。判定复用 classify_outcome：CLASS_BODY_ERROR /
    CLASS_REQUEST_FAULT（status>=400）→ 拒绝；CLASS_OK / CLASS_POISON_FIXED →
    未拒绝继续加压；CLASS_UPSTREAM_FAULT（5xx/连接异常）→ 同一批重试一次后仍败
    → 中止标 upstream_error（不把上游故障误判为阈值，R5）。
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
    TPM_CALIBRATE_HARD_CAP_TOKENS → capped 且不超发，batches 记已成功发送的批次数
    （被判超顶那一批未发送，不计入）；墙钟超
    TPM_CALIBRATE_MAX_DURATION_S → timeout；should_abort() 为真（批间隙检查）
    → aborted。返回 {"threshold", "batches", "consumed", "outcome", "status", "ts"}：
    threshold = 拒绝批 est（rejected）或最后成功批 est（timeout/aborted），其余 None。
    """
```

new（18 行）：

```
    """校准引擎（决策 4/5 纯逻辑；发送器/abort/sleep 全部注入）。

    阶梯递增（方案 B ramp-up）：第 batch 批实际 est 由 calibrate_est_for_batch
    （与发送器同源的单一调度源）给出——起步 TPM_CALIBRATE_START_TOKENS，
    几何段 ×TPM_CALIBRATE_RAMP_FACTOR 递增至 TPM_CALIBRATE_KNEE_TOKENS，
    之后线性 +TPM_CALIBRATE_STEP_TOKENS（R1/R2 小步逼近阈值）。
    每批经 send_one(batch, est) 发送，返回 (status, body_error)；status=None 表示
    连接级异常（无 HTTP 响应）。判定复用 classify_outcome：CLASS_BODY_ERROR /
    CLASS_REQUEST_FAULT（status>=400）→ 拒绝；CLASS_OK / CLASS_POISON_FIXED →
    未拒绝继续加压；CLASS_UPSTREAM_FAULT（5xx/连接异常）→ 同一批重试一次后仍败
    → 中止标 upstream_error（不把上游故障误判为阈值，R5）。
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
    TPM_CALIBRATE_HARD_CAP_TOKENS → capped 且不超发，batches 记已成功发送的批次数
    （被判超顶那一批未发送，不计入）；墙钟超 TPM_CALIBRATE_MAX_DURATION_S → timeout；
    should_abort() 为真（批间隙检查）→ aborted。返回
    {"threshold", "batches", "consumed", "outcome", "status", "ts"}：
    threshold = 拒绝批 est（rejected）或最后成功批 est（timeout/aborted），
    其余 None。
    """
```

（本卡不含 `probe_timeout`/`MAX_PROBE` 文案——分别由卡 2 编辑 2.2/2.3、卡 3 编辑 3.1 追加。）

#### 编辑 1.4 — 删除一次性 base 计算（prod :1025-1027）

old（4 行）：

```
    started = time.monotonic()
    base = estimate(json.dumps(build_calibrate_body(model, TPM_CALIBRATE_INPUT_BYTES),
                               ensure_ascii=False).encode("utf-8"))
    batch = 0
```

new（2 行）：

```
    started = time.monotonic()
    batch = 0
```

#### 编辑 1.5 — 引擎调度公式替换（prod :1052-1054）

old（3 行）：

```
        batch += 1
        est = base + (batch - 1) * TPM_CALIBRATE_STEP_TOKENS
        if consumed + est > TPM_CALIBRATE_HARD_CAP_TOKENS:
```

new（3 行）：

```
        batch += 1
        est = calibrate_est_for_batch(model, batch)
        if consumed + est > TPM_CALIBRATE_HARD_CAP_TOKENS:
```

#### 编辑 1.6 — 删除 `_CALIBRATE_BYTES_PER_STEP`（prod :1078-1081）

old（4 行）：

```
# v2 P5：校准 daemon 与发送器（决策 9/10）
_CALIBRATE_BYTES_PER_STEP = max(1, int(TPM_CALIBRATE_STEP_TOKENS
                                       / max(TPM_TOKEN_RATIO, 0.01)))
CALIBRATE_LOCK = threading.Lock()
```

new（2 行）：

```
# v2 P5：校准 daemon 与发送器（决策 9/10）
CALIBRATE_LOCK = threading.Lock()
```

#### 编辑 1.7 — `_calibrate_send` docstring body 口径（prod :1155-1157）

old（3 行）：

```
    body 由 build_calibrate_body 按批次放大 input 填充：batch 1=INPUT_BYTES，
    每批 +_CALIBRATE_BYTES_PER_STEP 字节（使 estimate 增量 ≈ TPM_CALIBRATE_STEP_TOKENS）。
    Authorization = auth_token 或 _LAST_AUTH。
```

new（4 行）：

```
    body 由 build_calibrate_body 按批次放大 input 填充：input_bytes 取自
    calibrate_input_bytes(batch)（与引擎 est 同源的单一调度源，R5——ramp-up 起步
    ~4k est，不再首批灌 240KB）。
    Authorization = auth_token 或 _LAST_AUTH。
```

#### 编辑 1.8 — `_calibrate_send` input_bytes 改用调度函数（prod :1164-1165）

old（2 行）：

```
    input_bytes = TPM_CALIBRATE_INPUT_BYTES \
        + (batch - 1) * _CALIBRATE_BYTES_PER_STEP
```

new（1 行）：

```
    input_bytes = calibrate_input_bytes(batch)
```

### 测试编辑（按序执行；生产已改，以下相关断言此刻必红）

#### 编辑 1.9 — 默认值测试（test :2974-2986）

old（13 行）：

```
    def test_tpm_calibrate_constants_defaults(self) -> None:
        mod = self.mod
        self.assertEqual(mod.TPM_CALIBRATE_INPUT_BYTES, 240000)
        self.assertEqual(mod.TPM_CALIBRATE_STEP_TOKENS, 5000)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_TOKENS, 1)
        self.assertEqual(mod.TPM_CALIBRATE_BATCH_GAP_S, 2.0)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_DURATION_S, 240)
        self.assertEqual(mod.TPM_CALIBRATE_MIN_INTERVAL_S, 1800)
        self.assertEqual(mod.TPM_CALIBRATE_HARD_CAP_TOKENS, 400000)
        self.assertEqual(mod.TPM_CALIBRATE_SETTLE_WAIT_S, 60)
        self.assertEqual(mod.TPM_SAMPLES_MAX, 200)
        self.assertEqual(mod.TPM_SAMPLES_RETENTION_DAYS, 30)
        self.assertEqual(mod.TPM_PROBE_RESULTS_MAX, 5)
```

new（15 行）：

```
    def test_tpm_calibrate_constants_defaults(self) -> None:
        mod = self.mod
        self.assertEqual(mod.TPM_CALIBRATE_START_TOKENS, 4000)
        self.assertEqual(mod.TPM_CALIBRATE_RAMP_FACTOR, 2.0)
        self.assertEqual(mod.TPM_CALIBRATE_KNEE_TOKENS, 32000)
        self.assertEqual(mod.TPM_CALIBRATE_STEP_TOKENS, 20000)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_PROBE_TOKENS, 160000)
        self.assertEqual(mod.TPM_CALIBRATE_TIMEOUT_S, 60)
        self.assertFalse(hasattr(mod, "TPM_CALIBRATE_INPUT_BYTES"),
                         "旧常量必须删除（死代码根源）")
        self.assertEqual(mod.TPM_CALIBRATE_MAX_TOKENS, 1)
        self.assertEqual(mod.TPM_CALIBRATE_BATCH_GAP_S, 2.0)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_DURATION_S, 240)
        self.assertEqual(mod.TPM_CALIBRATE_MIN_INTERVAL_S, 1800)
        self.assertEqual(mod.TPM_CALIBRATE_HARD_CAP_TOKENS, 400000)
        self.assertEqual(mod.TPM_CALIBRATE_SETTLE_WAIT_S, 60)
        self.assertEqual(mod.TPM_SAMPLES_MAX, 200)
        self.assertEqual(mod.TPM_SAMPLES_RETENTION_DAYS, 30)
        self.assertEqual(mod.TPM_PROBE_RESULTS_MAX, 5)
```

（`TPM_CALIBRATE_MAX_PROBE_TOKENS`/`TIMEOUT_S` 断言此刻即绿——常量已在卡 1 生产编辑 1.1 落地。AC1 的 `hasattr` 断言在此闭环。）

#### 编辑 1.10 — env seam 键替换（test :2992-2994，子串唯一）

old（3 行）：

```
            "os.environ['CTYUN_CALIBRATE_INPUT_BYTES']='111'; "
            "os.environ['CTYUN_CALIBRATE_STEP_TOKENS']='222'; "
            "os.environ['CTYUN_CALIBRATE_MAX_TOKENS']='3'; "
```

new（7 行）：

```
            "os.environ['CTYUN_CALIBRATE_START_TOKENS']='4001'; "
            "os.environ['CTYUN_CALIBRATE_RAMP_FACTOR']='1.5'; "
            "os.environ['CTYUN_CALIBRATE_KNEE_TOKENS']='30001'; "
            "os.environ['CTYUN_CALIBRATE_MAX_PROBE_TOKENS']='160001'; "
            "os.environ['CTYUN_CALIBRATE_TIMEOUT_S']='6.5'; "
            "os.environ['CTYUN_CALIBRATE_STEP_TOKENS']='222'; "
            "os.environ['CTYUN_CALIBRATE_MAX_TOKENS']='3'; "
```

（首行前导缩进与末行行尾的引号+空格）保持不变；`'CTYUN_CALIBRATE_MAX_TOKENS']='3'; "` 结尾为空格+引号，非行尾空白。）

#### 编辑 1.11 — env seam print 键替换（test :3006-3007）

old（2 行）：

```
            "print(m.TPM_CALIBRATE_INPUT_BYTES, m.TPM_CALIBRATE_STEP_TOKENS, "
            "m.TPM_CALIBRATE_MAX_TOKENS, m.TPM_CALIBRATE_BATCH_GAP_S, "
```

new（4 行）：

```
            "print(m.TPM_CALIBRATE_START_TOKENS, m.TPM_CALIBRATE_RAMP_FACTOR, "
            "m.TPM_CALIBRATE_KNEE_TOKENS, m.TPM_CALIBRATE_MAX_PROBE_TOKENS, "
            "m.TPM_CALIBRATE_TIMEOUT_S, m.TPM_CALIBRATE_STEP_TOKENS, "
            "m.TPM_CALIBRATE_MAX_TOKENS, m.TPM_CALIBRATE_BATCH_GAP_S, "
```

#### 编辑 1.12 — env seam 期望值（test :3014-3016）

old（3 行）：

```
        self.assertEqual(out.split(),
                         ["111", "222", "3", "0.5", "10.0", "9.0", "999", "7.0",
                          "4", "5", "5"])
```

new（4 行）：

```
        self.assertEqual(out.split(),
                         ["4001", "1.5", "30001", "160001", "6.5", "222",
                          "3", "0.5", "10.0", "9.0", "999", "7.0",
                          "4", "5", "5"])
```

（输出顺序与新 print 顺序一致：START / RAMP_FACTOR / KNEE / MAX_PROBE / TIMEOUT / STEP / MAX_TOKENS / GAP / DURATION / MIN_INTERVAL / HARD_CAP / SETTLE / SAMPLES_MAX / RETENTION / PROBE_MAX。）

#### 编辑 1.13 — `test_build_calibrate_body_shape` 重写 + 两个新纯函数用例（test :7716-7736）

old（21 行）：

```
    def test_build_calibrate_body_shape(self) -> None:
        """AC9：max_tokens == TPM_CALIBRATE_MAX_TOKENS、stream == False、
        messages 内容长度 ≥ TPM_CALIBRATE_INPUT_BYTES；estimate 量级 ≥ 大 input 比例。"""
        mod = self.mod
        body = mod.build_calibrate_body("kimi-k3-oc", mod.TPM_CALIBRATE_INPUT_BYTES)
        self.assertEqual(body["model"], "kimi-k3-oc")
        self.assertEqual(body["max_tokens"], mod.TPM_CALIBRATE_MAX_TOKENS)
        self.assertIs(body["stream"], False)
        content = body["messages"][0]["content"]
        self.assertGreaterEqual(len(content), mod.TPM_CALIBRATE_INPUT_BYTES)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        est = mod.estimate_request_tokens(raw)
        self.assertGreaterEqual(est, int(mod.TPM_CALIBRATE_INPUT_BYTES
                                         * mod.TPM_TOKEN_RATIO),
                                "estimate must prove large input magnitude")
        # 输入字节递增 → est 递增（逐级加压真实作用于准入口径）
        bigger = mod.build_calibrate_body("kimi-k3-oc",
                                          mod.TPM_CALIBRATE_INPUT_BYTES + 4000)
        est_bigger = mod.estimate_request_tokens(
            json.dumps(bigger, ensure_ascii=False).encode("utf-8"))
        self.assertGreater(est_bigger, est)
```

new（46 行）：

```
    def test_build_calibrate_body_shape(self) -> None:
        """AC2：max_tokens == TPM_CALIBRATE_MAX_TOKENS、stream == False、
        content 长度 = calibrate_input_bytes(batch)；est 与发送器同源、小起步。"""
        mod = self.mod
        input_bytes = mod.calibrate_input_bytes(2)
        body = mod.build_calibrate_body("kimi-k3-oc", input_bytes)
        self.assertEqual(body["model"], "kimi-k3-oc")
        self.assertEqual(body["max_tokens"], mod.TPM_CALIBRATE_MAX_TOKENS)
        self.assertIs(body["stream"], False)
        content = body["messages"][0]["content"]
        self.assertEqual(len(content), input_bytes)
        # 目标 est 递增 → 输入字节与实际 est 同向递增（单一调度源 R5）
        self.assertGreater(mod.calibrate_input_bytes(3), input_bytes)
        self.assertGreater(mod.calibrate_est_for_batch("kimi-k3-oc", 3),
                           mod.calibrate_est_for_batch("kimi-k3-oc", 2))

    def test_calibrate_target_est_schedule(self) -> None:
        """AC2：两段 ramp-up 目标序列（几何 ×2 至 32k，之后 +20k）。"""
        mod = self.mod
        expected = [4000, 8000, 16000, 32000, 52000, 72000,
                    92000, 112000, 132000, 152000, 172000]
        self.assertEqual([mod.calibrate_target_est(b) for b in range(1, 12)],
                         expected)

    def test_calibrate_est_for_batch_small_start(self) -> None:
        """AC2/R5：input_bytes 由目标 est 反推（(4000-1)/0.25≈15996），
        实际 est 落在 [4000, 4300]——首批小起步，不再是 60k。"""
        mod = self.mod
        self.assertEqual(mod.calibrate_input_bytes(1), 15996)
        est = mod.calibrate_est_for_batch("m", 1)
        self.assertGreaterEqual(est, 4000)
        self.assertLessEqual(est, 4300)
```

#### 编辑 1.14 — 线性首拒用例改名并改调度断言 + 新增单调用例（test :7738-7753）

old（16 行）：

```
    def test_calibrate_engine_linear_press_first_reject_stops(self) -> None:
        """AC1：前 N-1 批 200 正常，第 N 批 429 → 首拒即停（不追加确认批次）；
        threshold = (N-1)*step + base、consumed = 累计 est。"""
        mod = self.mod
        step = mod.TPM_CALIBRATE_STEP_TOKENS
        base = mod.estimate_request_tokens(
            json.dumps(mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES),
                       ensure_ascii=False).encode("utf-8"))
        sender, calls = self._scripted([(200, False)] * 4 + [(429, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(calls["n"], 5, "first reject must stop, no confirm batch")
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 5)
        self.assertEqual(result["threshold"], base + 4 * step)
        self.assertEqual(result["consumed"], base * 5 + step * (0 + 1 + 2 + 3 + 4))
        self.assertEqual(result["status"], 429)
```

new（37 行）：

```
    def test_calibrate_engine_ramp_first_reject_stops(self) -> None:
        """AC4：前 7 批 200、第 8 批 429 → 首拒即停（不追加确认批次）；
        threshold = calibrate_est_for_batch(8)（≈112k 量级），consumed = Σ est(1..8)。"""
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

    def test_calibrate_engine_ramp_small_start_and_monotonic(self) -> None:
        """AC3：首批 est ≤ START×1.1（≈4.4k，不再 60k）；前 8 批严格单调递增。"""
        mod = self.mod
        ests = []

        def sender(batch, est):
            ests.append(est)
            return (200, False) if batch < 8 else (429, False)

        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(len(ests), 8)
        self.assertLessEqual(ests[0], int(mod.TPM_CALIBRATE_START_TOKENS * 1.1),
                             "首批必须小起步（≈4k），不得灌 60k")
        for prev, cur in zip(ests[:7], ests[1:8]):
            self.assertGreater(cur, prev, "前 8 批 est 必须严格递增")
```

（单调用例止步第 8 批：第 9 批目标 172000 > MAX_PROBE 160000 会在卡 1 阶段被 HARD_CAP 口径之外的上限拦下——但 MAX_PROBE 分支卡 3 才落地；第 8 批 429 确保本卡内自洽。）

#### 编辑 1.15 — body-error 用例改调度断言（test :7755-7768 内两处）

old（14 行）：

```
    def test_calibrate_engine_rejects_on_body_error_200(self) -> None:
        """200 + body_error=True → CLASS_BODY_ERROR → 拒绝（R5 判定协同）。"""
        mod = self.mod
        step = mod.TPM_CALIBRATE_STEP_TOKENS
        base = mod.estimate_request_tokens(
            json.dumps(mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES),
                       ensure_ascii=False).encode("utf-8"))
        sender, calls = self._scripted([(200, False), (200, False), (200, True)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["threshold"], base + 2 * step)
        self.assertEqual(result["status"], 200)
        self.assertEqual(calls["n"], 3)
```

new（12 行）：

```
    def test_calibrate_engine_rejects_on_body_error_200(self) -> None:
        """200 + body_error=True → CLASS_BODY_ERROR → 拒绝（R5 判定协同）。"""
        mod = self.mod
        sender, calls = self._scripted([(200, False), (200, False), (200, True)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["threshold"], mod.calibrate_est_for_batch("m", 3))
        self.assertEqual(result["status"], 200)
        self.assertEqual(calls["n"], 3)
```

#### 编辑 1.16 — hard cap 用例改调度断言（test :7770-7784）

old（15 行）：

```
    def test_calibrate_engine_hard_cap_stops_before_oversend(self) -> None:
        """AC6①：下一批将超硬顶 → capped 且不发超限批。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            json.dumps(mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES),
                       ensure_ascii=False).encode("utf-8"))
        orig = mod.TPM_CALIBRATE_HARD_CAP_TOKENS
        mod.TPM_CALIBRATE_HARD_CAP_TOKENS = base + mod.TPM_CALIBRATE_STEP_TOKENS
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_HARD_CAP_TOKENS", orig)
        sender, calls = self._scripted([(200, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "capped")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["consumed"], base)
        self.assertEqual(calls["n"], 1, "must not oversend beyond hard cap")
```

new（16 行）：

```
    def test_calibrate_engine_hard_cap_stops_before_oversend(self) -> None:
        """AC7①：下一批将超硬顶 → capped 且不发超限批。"""
        mod = self.mod
        orig = mod.TPM_CALIBRATE_HARD_CAP_TOKENS
        mod.TPM_CALIBRATE_HARD_CAP_TOKENS = (
            mod.calibrate_est_for_batch("m", 1)
            + mod.calibrate_est_for_batch("m", 2) - 1)
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_HARD_CAP_TOKENS", orig)
        sender, calls = self._scripted([(200, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "capped")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["consumed"], mod.calibrate_est_for_batch("m", 1))
        self.assertEqual(calls["n"], 1, "must not oversend beyond hard cap")
```

#### 编辑 1.17 — 批间 timeout 用例改调度断言（test :7798-7813，两处）

old（13 行）：

```
        """AC6②：第一批成功，批间真实流逝超过墙钟上限 → timeout，
        threshold = 最后成功批 est。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            json.dumps(mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES),
                       ensure_ascii=False).encode("utf-8"))
        orig = mod.TPM_CALIBRATE_MAX_DURATION_S
```

new（9 行）：

```
        """AC7②：第一批成功，批间真实流逝超过墙钟上限 → timeout，
        threshold = 最后成功批 est。"""
        mod = self.mod
        est1 = mod.calibrate_est_for_batch("m", 1)
        orig = mod.TPM_CALIBRATE_MAX_DURATION_S
```

第二处（同方法尾部）：

old（4 行）：

```
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["threshold"], base)
        self.assertEqual(calls["n"], 1)
```

new（4 行）：

```
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["threshold"], est1)
        self.assertEqual(calls["n"], 1)
```

#### 编辑 1.18 — abort 用例改调度断言（test :7815-7833，两处）

第一处 old（7 行）：

```
        """abort 在批间隙生效：第二次 abort 检查后返回，发送器不再调用。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            json.dumps(mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES),
                       ensure_ascii=False).encode("utf-8"))
        checks = {"n": 0}
```

new（4 行）：

```
        """abort 在批间隙生效：第二次 abort 检查后返回，发送器不再调用。"""
        mod = self.mod
        est1 = mod.calibrate_est_for_batch("m", 1)
        checks = {"n": 0}
```

第二处（同方法尾部）：

old（4 行）：

```
        self.assertEqual(result["outcome"], "aborted")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["threshold"], base)
        self.assertEqual(calls["n"], 1)
```

new（4 行）：

```
        self.assertEqual(result["outcome"], "aborted")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["threshold"], est1)
        self.assertEqual(calls["n"], 1)
```

#### 编辑 1.19 — 5xx 重试用例改调度断言（test :7835-7847）

old（13 行）：

```
        """CLASS_UPSTREAM_FAULT（5xx）重试同批一次后仍败 → upstream_error。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            json.dumps(mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES),
                       ensure_ascii=False).encode("utf-8"))
        sender, calls = self._scripted([(503, False), (503, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "upstream_error")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["consumed"], base * 2, "retry re-charges same est")
        self.assertEqual(calls["n"], 2)
```

new（11 行）：

```
        """AC7④：CLASS_UPSTREAM_FAULT（5xx）重试同批一次后仍败 → upstream_error。"""
        mod = self.mod
        est1 = mod.calibrate_est_for_batch("m", 1)
        sender, calls = self._scripted([(503, False), (503, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "upstream_error")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["consumed"], est1 * 2, "retry re-charges same est")
        self.assertEqual(calls["n"], 2)
```

#### 编辑 1.20 — 503 重试成功用例改调度断言（test :7857-7871）

old（15 行）：

```
        """503 重试一次成功（200）→ 未拒绝继续加压，下一批 429 → rejected。"""
        mod = self.mod
        step = mod.TPM_CALIBRATE_STEP_TOKENS
        base = mod.estimate_request_tokens(
            json.dumps(mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES),
                       ensure_ascii=False).encode("utf-8"))
        sender, calls = self._scripted([(503, False), (200, False), (429, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["threshold"], base + step)
        self.assertEqual(result["consumed"],
                         base * 3 + step, "503 retry + batch1 + batch2")
        self.assertEqual(calls["n"], 3)
```

new（15 行）：

```
        """503 重试一次成功（200）→ 未拒绝继续加压，下一批 429 → rejected。"""
        mod = self.mod
        est1 = mod.calibrate_est_for_batch("m", 1)
        est2 = mod.calibrate_est_for_batch("m", 2)
        sender, calls = self._scripted([(503, False), (200, False), (429, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["threshold"], est2)
        self.assertEqual(result["consumed"], est1 * 2 + est2,
                         "503 retry + batch1 + batch2")
        self.assertEqual(calls["n"], 3)
```

#### 编辑 1.21 — `CalibrateSendTest` 新增单一调度源断言（test :8296-8302 之后插入）

old（7 行）：

```
        status, body_error = self.mod._calibrate_send(
            "m", 1, 60013, None)
        self.assertIsNone(status)
        self.assertIs(body_error, False)
```

new（23 行）：

```
        status, body_error = self.mod._calibrate_send(
            "m", 1, 60013, None)
        self.assertIsNone(status)
        self.assertIs(body_error, False)

    def test_calibrate_send_uses_schedule_input_bytes(self) -> None:
        """单一调度源（R5）：发送器 input_bytes 取自 calibrate_input_bytes(batch)，
        不再按固定 240KB + 步进放大。"""
        mod = self.mod
        seen = []
        orig = mod.build_calibrate_body

        def spy(model, input_bytes):
            seen.append((model, input_bytes))
            return orig(model, input_bytes)

        mod.build_calibrate_body = spy
        self.addCleanup(setattr, mod, "build_calibrate_body", orig)
        status, body_error = mod._calibrate_send("m", 3, 16023, "Bearer t")
        self.assertEqual(status, 200)
        self.assertIs(body_error, False)
        self.assertEqual(seen, [("m", mod.calibrate_input_bytes(3))])
```

### 卡 1 验证命令（执行者按序执行，逐条贴 stdout + exit code）

```
# 1) 焦点：新 ramp 用例 + 既有校准用例全绿
/usr/bin/python3 ctyun-stream-fix-proxy.test.py -k calibrate -v
# 预期：Ran 34 tests — OK，exit 0

# 2) 生产残留清点：INPUT_BYTES 0 命中（exit 1）；BYTES_PER_STEP 仅 1 行注释命中
grep -c "TPM_CALIBRATE_INPUT_BYTES" ctyun-stream-fix-proxy.py
grep -n "_CALIBRATE_BYTES_PER_STEP" ctyun-stream-fix-proxy.py
# 预期：第一行输出 0 且 exit 1；第二条仅 :117 注释行（删除说明）
```

### 卡 1 Commit

```
feat(tpm): 校准探测改为小起步两段 ramp-up（4k→32k 几何→+20k 线性）
```

---

## 卡 2：读超时哨兵 + `_calibrate_send` 超时分类（tier A）

**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

**目标：** AC6（含不写 `PROBE_RESULTS`）、AC7（重试路径读超时归 `probe_timeout`）、AC8（loop 层推荐源保持 none）、AC1 的哨兵形态。

### 生产编辑（按序执行）

#### 编辑 2.1 — 新增 `CALIBRATE_STATUS_TIMEOUT` 哨兵（prod :122-123，紧跟常量区）

old（2 行）：

```
TPM_CALIBRATE_SETTLE_WAIT_S = float(os.environ.get("CTYUN_CALIBRATE_SETTLE_WAIT_S", "60"))
# 样本/探测结果持久化上限（R7：防持久化膨胀）
```

new（4 行）：

```
TPM_CALIBRATE_SETTLE_WAIT_S = float(os.environ.get("CTYUN_CALIBRATE_SETTLE_WAIT_S", "60"))
# 校准发送器状态哨兵：读超时（socket.timeout）与 int HTTP 状态码区分（engine 判 probe_timeout）
CALIBRATE_STATUS_TIMEOUT = "timeout"
# 样本/探测结果持久化上限（R7：防持久化膨胀）
```

#### 编辑 2.2 — 引擎 docstring 追加 probe_timeout 语义（prod 卡 1 编辑 1.3 产物内）

old（4 行）：

```
    → 中止标 upstream_error（不把上游故障误判为阈值，R5）。
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
```

new（6 行）：

```
    → 中止标 upstream_error（不把上游故障误判为阈值，R5）；
    send_one 返回 CALIBRATE_STATUS_TIMEOUT（读超时哨兵）→ probe_timeout，
    不重试、不翻倍（同批大请求是读挂上游的根因，R3）。
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
```

#### 编辑 2.3 — 引擎 docstring threshold 口径（prod 卡 1 编辑 1.3 产物内）

old（3 行）：

```
    threshold = 拒绝批 est（rejected）或最后成功批 est（timeout/aborted），
    其余 None。
    """
```

new（3 行）：

```
    threshold = 拒绝批 est（rejected）或最后成功批 est（probe_timeout/timeout/
    aborted），其余 None。
    """
```

#### 编辑 2.4 — `_attempt` 哨兵直通（prod :1040-1046，卡 1 后未变）

old（6 行）：

```
    def _attempt(batch_num, est):
        """发送一次，返回 (category, status)；连接异常 → (CLASS_UPSTREAM_FAULT, None)。"""
        status, body_error = send_one(batch_num, est)
        if status is None:
            return CLASS_UPSTREAM_FAULT, None
        return classify_outcome(status=status, body_error=body_error).category, status
```

new（9 行）：

```
    def _attempt(batch_num, est):
        """发送一次，返回 (category, status)；连接异常 → (CLASS_UPSTREAM_FAULT, None)；
        读超时哨兵（字符串，非 int HTTP 码）直通，不交 classify_outcome 做阈值比较。"""
        status, body_error = send_one(batch_num, est)
        if status is None:
            return CLASS_UPSTREAM_FAULT, None
        if status == CALIBRATE_STATUS_TIMEOUT:
            return CALIBRATE_STATUS_TIMEOUT, status
        return classify_outcome(status=status, body_error=body_error).category, status
```

#### 编辑 2.5 — 首轮 `probe_timeout` 分支（prod :1057-1061，卡 1 编辑 1.5 产物内）

old（3 行）：

```
        category, status = _attempt(batch, est)
        consumed += est
        if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
            return _result("rejected", est, status)
        if category == CLASS_UPSTREAM_FAULT:
```

new（6 行）：

```
        category, status = _attempt(batch, est)
        consumed += est
        if category == CALIBRATE_STATUS_TIMEOUT:
            # 读超时单独分类（R3）：不重试、不翻倍（同批大请求是读挂上游的根因）
            return _result("probe_timeout", last_ok_est, None)
        if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
            return _result("rejected", est, status)
        if category == CLASS_UPSTREAM_FAULT:
```

#### 编辑 2.6 — 重试轮 `probe_timeout` 分支（prod :1066-1069）

old（4 行）：

```
            category, status = _attempt(batch, est)
            consumed += est
            if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
                return _result("rejected", est, status)
```

new（6 行）：

```
            category, status = _attempt(batch, est)
            consumed += est
            if category == CALIBRATE_STATUS_TIMEOUT:
                return _result("probe_timeout", last_ok_est, None)
            if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
                return _result("rejected", est, status)
```

#### 编辑 2.7 — `_calibrate_send` docstring 返回契约（prod :1160-1162，卡 1 编辑 1.7 产物内）

old（3 行）：

```
    返回 (status, body_error)；连接异常（OSError / HTTPException，R6 显式分类）
    → (None, False) 并 _safe_log_stderr 留痕；engine 将连接异常判为
    CLASS_UPSTREAM_FAULT 走重试路径。
```

new（5 行）：

```
    返回 (status, body_error)；读超时（socket.timeout）→ (CALIBRATE_STATUS_TIMEOUT,
    False) 并 _safe_log_stderr 留痕（engine 判 probe_timeout 终止，不重试）；
    其余连接异常（OSError / HTTPException，R6 显式分类）→ (None, False) 并留痕，
    engine 判 CLASS_UPSTREAM_FAULT 走重试路径。socket 超时用
    TPM_CALIBRATE_TIMEOUT_S（整请求读超时，独立于 HEADER_TIMEOUT_S）。
```

#### 编辑 2.8 — `_calibrate_send` 超时独立 + 读超时单独捕获（prod :1174-1186）

old（12 行）：

```
    try:
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        conn.request("POST", upstream_path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        data = resp.read()
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
```

new（20 行）：

```
    try:
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=TPM_CALIBRATE_TIMEOUT_S)
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=TPM_CALIBRATE_TIMEOUT_S)
        conn.request("POST", upstream_path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        data = resp.read()
        conn.close()
    except socket.timeout as exc:
        # 读超时单独分类（R3）：不发重试信号（避免同批翻倍灌大请求），
        # 返回字符串哨兵由 engine 判 probe_timeout 并终止。
        _safe_log_stderr("ctyun-stream-fix-proxy: calibrate read timeout "
                         "model=%s batch=%d: %s" % (model, batch, exc))
        return CALIBRATE_STATUS_TIMEOUT, False
    except (OSError, http.client.HTTPException) as exc:
```

（`except socket.timeout` 必须排在 `except (OSError, ...)` 之前——Python 3.9 中 `socket.timeout` 是 `OSError` 子类；`socket` 已在 prod :25 import。）

### 测试编辑（按序执行）

#### 编辑 2.9 — 哨兵 + 发送器读超时分类（test :8302-8304 之间插入，即卡 1 编辑 1.21 产物之后）

old（4 行）：

```
        self.assertEqual(seen, [("m", mod.calibrate_input_bytes(3))])


class GenSpeedRecentEntryTest(unittest.TestCase):
```

new（24 行）：

```
        self.assertEqual(seen, [("m", mod.calibrate_input_bytes(3))])

    def test_calibrate_status_timeout_sentinel(self) -> None:
        """状态哨兵为字符串，与 int HTTP 状态码区分（engine 判 probe_timeout）。"""
        self.assertEqual(self.mod.CALIBRATE_STATUS_TIMEOUT, "timeout")
        self.assertNotIsInstance(self.mod.CALIBRATE_STATUS_TIMEOUT, int)

    def test_calibrate_send_read_timeout_classified(self) -> None:
        """AC6：上游持连静默超过 TPM_CALIBRATE_TIMEOUT_S → socket.timeout →
        (CALIBRATE_STATUS_TIMEOUT, False)，区别于连接异常的 (None, False)。"""
        mod = self.mod
        stop_fake_upstreams()
        stall_port = make_fake_upstream(False, sleep_stall_all=True)
        mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % stall_port
        orig = mod.TPM_CALIBRATE_TIMEOUT_S
        mod.TPM_CALIBRATE_TIMEOUT_S = 0.5
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_TIMEOUT_S", orig)
        status, body_error = mod._calibrate_send("m", 1, 4023, "Bearer t")
        self.assertEqual(status, mod.CALIBRATE_STATUS_TIMEOUT)
        self.assertIs(body_error, False)


class GenSpeedRecentEntryTest(unittest.TestCase):
```

（`make_fake_upstream(False, sleep_stall_all=True)` 是既有 stall 夹具 :269/:100-104：读 body 后持连静默不写响应，代理侧 `getresponse()` 阻塞到超时抛 `socket.timeout`；`sleep_stall_seconds` 默认 3.0s > 0.5s 测试超时，余量 2.5s 防 flaky。tearDown 的 `stop_fake_upstreams()` 会清理两次注册的服务器。）

#### 编辑 2.10 — 引擎 probe_timeout 三用例（test :7784-7786 之间插入，即 hard cap 用例之后）

old（3 行）：

```
        self.assertEqual(calls["n"], 1, "must not oversend beyond hard cap")

    def test_calibrate_engine_timeout_zero_duration(self) -> None:
```

new（48 行）：

```
        self.assertEqual(calls["n"], 1, "must not oversend beyond hard cap")

    def test_calibrate_engine_probe_timeout_not_retried(self) -> None:
        """AC6：读超时哨兵 → probe_timeout，不重试不翻倍；threshold = last_ok。"""
        mod = self.mod
        est1 = mod.calibrate_est_for_batch("m", 1)
        est2 = mod.calibrate_est_for_batch("m", 2)
        sender, calls = self._scripted([(200, False),
                                        (mod.CALIBRATE_STATUS_TIMEOUT, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "probe_timeout")
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["threshold"], est1)
        self.assertEqual(result["consumed"], est1 + est2)
        self.assertEqual(calls["n"], 2, "timeout must not retry the same batch")

    def test_calibrate_engine_probe_timeout_first_batch_threshold_none(self) -> None:
        """AC6：首批即读超时 → threshold=None、仅 1 次调用。"""
        mod = self.mod
        sender, calls = self._scripted([(mod.CALIBRATE_STATUS_TIMEOUT, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "probe_timeout")
        self.assertEqual(result["batches"], 1)
        self.assertIsNone(result["threshold"])
        self.assertEqual(calls["n"], 1)

    def test_calibrate_engine_retry_timeout_probe_timeout(self) -> None:
        """AC6：503 重试期间读超时 → 同样 probe_timeout（不判 upstream_error）。"""
        mod = self.mod
        est1 = mod.calibrate_est_for_batch("m", 1)
        sender, calls = self._scripted([(503, False),
                                        (mod.CALIBRATE_STATUS_TIMEOUT, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "probe_timeout")
        self.assertEqual(result["batches"], 1)
        self.assertIsNone(result["threshold"])
        self.assertEqual(result["consumed"], est1 * 2)
        self.assertEqual(calls["n"], 2)

    def test_calibrate_engine_timeout_zero_duration(self) -> None:
```

#### 编辑 2.11 — loop 层 probe_timeout 不写 PROBE_RESULTS（test :8079-8081 之间插入）

old（3 行）：

```
        self.assertEqual(result["outcome"], "rejected")

    def test_tpm_last_success_ts(self) -> None:
```

new（76 行）：

```
        self.assertEqual(result["outcome"], "rejected")

    def test_calibrate_loop_probe_timeout_skips_probe_results(self) -> None:
        """AC6/AC8：probe_timeout 终止不写 PROBE_RESULTS；快照推荐源保持 none。"""
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
        mod.RECENT_REQUESTS.append({"model": "m-probe-timeout"})

        def fake_send(model, batch, est, auth_token):
            return mod.CALIBRATE_STATUS_TIMEOUT, False

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

        status, _ = mod.calibrate_start("m-probe-timeout", token="Bearer t")
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
        self.assertEqual(result["outcome"], "probe_timeout")
        self.assertEqual(result["batches"], 1)
        self.assertIsNone(result["threshold"])
        self.assertNotIn("m-probe-timeout", mod.PROBE_RESULTS,
                         "probe_timeout 无可靠阈值，不得写 PROBE_RESULTS")
        snap = mod.tpm_settings_snapshot()
        by_name = {m["name"]: m for m in snap["models"]}
        rec = by_name["m-probe-timeout"]["recommend"]
        self.assertEqual(rec["source"], "none",
                         "无拒绝证据 → 推荐源不得因本次探测变为 probe")
        self.assertIsNone(rec["probe"])

    def test_tpm_last_success_ts(self) -> None:
```

（`RECENT_REQUESTS` 为 `collections.deque(maxlen=100)`（prod :1414），故还原用 `clear()+extend()` 而非切片赋值。）

### 卡 2 验证命令

```
# 焦点：新增读超时/哨兵用例 + 既有校准用例全绿
/usr/bin/python3 ctyun-stream-fix-proxy.test.py -k calibrate -v
# 预期：Ran 40 tests — OK，exit 0

# 超时常量接线确认：HEADER_TIMEOUT_S 在 _calibrate_send 内 0 命中、TPM_CALIBRATE_TIMEOUT_S 2 命中
grep -n "timeout=HEADER_TIMEOUT_S\|timeout=TPM_CALIBRATE_TIMEOUT_S" ctyun-stream-fix-proxy.py
# 预期：仅 HTTP 主通道 :3007/:3010 命中 HEADER_TIMEOUT_S；:1193/:1196 命中 TPM_CALIBRATE_TIMEOUT_S
```

### 卡 2 Commit

```
fix(tpm): 校准读超时单独分类为 probe_timeout（不重试不翻倍，60s 独立超时）
```

---

## 卡 3：单批 est 上限终止 + 摘要日志 `batch=` + 集成验证（tier A）

**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

**目标：** AC5、AC8 集成段、AC9 全量绿；日志可观测 `batch=`。

### 生产编辑（按序执行）

#### 编辑 3.1 — 引擎 docstring 补 MAX_PROBE 安全阀（prod 卡 2 编辑 2.2 产物内）

old（4 行）：

```
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
    TPM_CALIBRATE_HARD_CAP_TOKENS → capped 且不超发，batches 记已成功发送的批次数
    （被判超顶那一批未发送，不计入）；墙钟超 TPM_CALIBRATE_MAX_DURATION_S → timeout；
```

new（5 行）：

```
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
    TPM_CALIBRATE_HARD_CAP_TOKENS 或超 TPM_CALIBRATE_MAX_PROBE_TOKENS
    → capped 且不超发，batches 记已成功发送的批次数（被判超顶那一批未发送，
    不计入）；墙钟超 TPM_CALIBRATE_MAX_DURATION_S → timeout；
```

#### 编辑 3.2 — 单批 est 上限分支（prod :1056-1059，HARD_CAP 分支之后并列）

old（4 行）：

```
        if consumed + est > TPM_CALIBRATE_HARD_CAP_TOKENS:
            # 本批未发送：batches 只报已成功发送的批次数（batch - 1）
            return _result("capped", None, None, batches=batch - 1)
        category, status = _attempt(batch, est)
```

new（7 行）：

```
        if consumed + est > TPM_CALIBRATE_HARD_CAP_TOKENS:
            # 本批未发送：batches 只报已成功发送的批次数（batch - 1）
            return _result("capped", None, None, batches=batch - 1)
        if est > TPM_CALIBRATE_MAX_PROBE_TOKENS:
            # 单批上限（ramp-up 终止条件，R1/R2）：越限批不发送，口径同硬顶
            return _result("capped", None, None, batches=batch - 1)
        category, status = _attempt(batch, est)
```

（两分支共用 `capped` outcome：均无可靠拒绝阈值，`_calibrate_loop` :1342 仅 `rejected` 写 `PROBE_RESULTS`，本卡不改 loop 代码——AC8 无需改生产。）

#### 编辑 3.3 — 摘要日志补 `batch=`（prod :1217-1222）

old（6 行）：

```
            "rid=calibrate-%s host=%s ttfb=- stream=0 outcome=%s "
            "consumed=%d probe=1 ts=%s"
            % (status if status is not None else "-", dur_s, model, task_id,
               urllib.parse.urlparse(UPSTREAM_BASE).netloc,
               result.get("outcome"), result.get("consumed", 0),
```

new（7 行）：

```
            "rid=calibrate-%s host=%s ttfb=- stream=0 outcome=%s "
            "batch=%d consumed=%d probe=1 ts=%s"
            % (status if status is not None else "-", dur_s, model, task_id,
               urllib.parse.urlparse(UPSTREAM_BASE).netloc,
               result.get("outcome"), result.get("batches", 0),
               result.get("consumed", 0),
```

### 测试编辑（按序执行）

#### 编辑 3.4 — 引擎 MAX_PROBE 上限用例（test :7861 附近，`hard cap` 用例之后、probe_timeout 用例之前插入）

old（3 行）：

```
        self.assertEqual(calls["n"], 1, "must not oversend beyond hard cap")

    def test_calibrate_engine_probe_timeout_not_retried(self) -> None:
```

new（18 行）：

```
        self.assertEqual(calls["n"], 1, "must not oversend beyond hard cap")

    def test_calibrate_engine_max_probe_stops_before_oversend(self) -> None:
        """AC5：单批目标 est 超 TPM_CALIBRATE_MAX_PROBE_TOKENS → capped 且不发送该批。"""
        mod = self.mod
        orig = mod.TPM_CALIBRATE_MAX_PROBE_TOKENS
        mod.TPM_CALIBRATE_MAX_PROBE_TOKENS = 100000
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_MAX_PROBE_TOKENS", orig)
        sender, calls = self._scripted([(200, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "capped")
        self.assertEqual(result["batches"], 7, "前 7 批 ≤100k 已发送")
        self.assertEqual(calls["n"], 7, "第 8 批（112k）越限不得发送")
        self.assertEqual(result["consumed"],
                         sum(mod.calibrate_est_for_batch("m", b)
                             for b in range(1, 8)))

    def test_calibrate_engine_probe_timeout_not_retried(self) -> None:
```

#### 编辑 3.5 — loop 层 capped 不写 PROBE_RESULTS（test :8220 附近，probe_timeout loop 用例之后插入）

old（3 行）：

```
        self.assertIsNone(rec["probe"])

    def test_tpm_last_success_ts(self) -> None:
```

new（72 行）：

```
        self.assertIsNone(rec["probe"])

    def test_calibrate_loop_capped_skips_probe_results(self) -> None:
        """AC5/AC8：单批上限 capped 终止不写 PROBE_RESULTS（无拒绝阈值）。"""
        mod = self.mod
        orig_gap = mod.TPM_CALIBRATE_BATCH_GAP_S
        orig_interval = mod.TPM_CALIBRATE_MIN_INTERVAL_S
        orig_probe_max = mod.TPM_CALIBRATE_MAX_PROBE_TOKENS
        mod.TPM_CALIBRATE_BATCH_GAP_S = 0.05
        mod.TPM_CALIBRATE_MIN_INTERVAL_S = 0
        mod.TPM_CALIBRATE_MAX_PROBE_TOKENS = 5000
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_BATCH_GAP_S", orig_gap)
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_MIN_INTERVAL_S", orig_interval)
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_MAX_PROBE_TOKENS", orig_probe_max)
        orig_probe = {m: list(v) for m, v in mod.PROBE_RESULTS.items()}

        def restore():
            mod.PROBE_RESULTS.clear()
            mod.PROBE_RESULTS.update(orig_probe)
        self.addCleanup(restore)
        mod.PROBE_RESULTS.clear()

        calls = []

        def fake_send(model, batch, est, auth_token):
            calls.append(batch)
            return 200, False

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

        status, _ = mod.calibrate_start("m-probe-cap", token="Bearer t")
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
        self.assertEqual(result["outcome"], "capped")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(calls, [1], "第 2 批（8k）越单批上限，不得发送")
        self.assertNotIn("m-probe-cap", mod.PROBE_RESULTS,
                         "capped 无拒绝阈值，不得写 PROBE_RESULTS")

    def test_tpm_last_success_ts(self) -> None:
```

#### 编辑 3.6 — 集成：capped 不产 probe 推荐源（test :5005-5008，`test_calibrate_stats_not_polluted` 之后、`BodyErrorTest` 之前插入）

old（5 行）：

```
        tpm_snap = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(tpm_snap["buckets"], [],
                         "calibration must not touch tpm buckets")


class BodyErrorTest(unittest.TestCase):
```

new（45 行）：

```
        tpm_snap = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(tpm_snap["buckets"], [],
                         "calibration must not touch tpm buckets")

    def test_calibrate_capped_does_not_source_probe(self) -> None:
        """AC5+AC8 集成：真实流量使模型可见 → 单批上限 capped（无 reject）
        → GET /api/tpm_settings 该模型 recommend.source 不得为 probe、probe None。"""
        stop_proxy(self.proc)  # 重启前释放 self.proxy_port（同 class 既有重启写法）
        self.proc = start_proxy(self.upstream_port, self.proxy_port, extra_env={
            "CTYUN_CALIBRATE_BATCH_GAP_S": "0.1",
            "CTYUN_CALIBRATE_MAX_PROBE_TOKENS": "100000",
            "CTYUN_CALIBRATE_SETTLE_WAIT_S": "0.1",
        })
        post_sse_auth(self.proxy_port,
                      b'{"model":"kimi-k3-oc","stream":true,'
                      b'"messages":[{"role":"user","content":"hi"}]}',
                      "Bearer test-cal-token")
        status, body = admin_post(
            self.proc.admin_port, "/api/tpm_calibrate",
            json.dumps({"model": "kimi-k3-oc"}).encode("utf-8"))
        self.assertEqual(status, 200, body)
        deadline = time.time() + 30
        snap = None
        while time.time() < deadline:
            _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_calibrate")
            snap = json.loads(body.decode("utf-8"))
            if not snap["running"]:
                break
            time.sleep(0.2)
        self.assertIsNotNone(snap)
        self.assertFalse(snap["running"], "calibration must finish (capped)")
        self.assertEqual(snap["result"]["outcome"], "capped")
        self.assertEqual(snap["result"]["batches"], 7,
                         "MAX_PROBE=100000 时前 7 批（≤92k）已发送、第 8 批越限跳过")
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
        settings = json.loads(body_bytes.decode("utf-8"))
        by_name = {m["name"]: m for m in settings["models"]}
        self.assertIn("kimi-k3-oc", by_name, "真实流量模型必须出现在 tpm_settings")
        rec = by_name["kimi-k3-oc"]["recommend"]
        self.assertNotEqual(rec["source"], "probe",
                            "capped（无 reject）不得写 PROBE_RESULTS/probe 源")
        self.assertIsNone(rec["probe"])


class BodyErrorTest(unittest.TestCase):
```

#### 编辑 3.7 — 日志断言补 `batch=`（test :4999-5000）

old（2 行）：

```
        self.assertTrue(any("probe=1" in line for line in lines),
                        "calibration log line must carry probe=1")
```

new（5 行）：

```
        self.assertTrue(any("probe=1" in line for line in lines),
                        "calibration log line must carry probe=1")
        self.assertTrue(any("batch=" in line and "probe=1" in line
                            for line in lines),
                        "calibration log line must carry batch= progress field")
```

### 卡 3 验证命令

```
# 1) 焦点：全部校准用例（含新增 capped/集成）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py -k calibrate -v
# 预期：Ran 43 tests — OK，exit 0

# 2) 全量（回归 + 新总数）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
# 预期：Ran 366 tests — OK，exit 0

# 3) 生产残留清点：INPUT_BYTES 0 命中；BYTES_PER_STEP 仅注释 1 行
grep -c "TPM_CALIBRATE_INPUT_BYTES" ctyun-stream-fix-proxy.py
grep -n "_CALIBRATE_BYTES_PER_STEP" ctyun-stream-fix-proxy.py

# 4) 新函数/常量接线清点（预期：4 个 def + 6 个新常量行）
grep -n "def calibrate_target_est\|def calibrate_input_bytes\|def calibrate_est_for_batch\|def calibrate_engine" ctyun-stream-fix-proxy.py
grep -n "TPM_CALIBRATE_START_TOKENS =\|TPM_CALIBRATE_RAMP_FACTOR =\|TPM_CALIBRATE_KNEE_TOKENS =\|TPM_CALIBRATE_MAX_PROBE_TOKENS =\|TPM_CALIBRATE_TIMEOUT_S =\|CALIBRATE_STATUS_TIMEOUT =" ctyun-stream-fix-proxy.py
```

### 卡 3 Commit

```
feat(tpm): 校准单批 est 上限终止 capped + 摘要日志补 batch=
```

---

## Anchor Verification（PLAN-B 写作时实测，非推断）

### 生产锚点（worktree HEAD `177f8eb` 实测行号）

- 常量区 `:114-122`：`:115` 为 `TPM_CALIBRATE_INPUT_BYTES = int(... "240000"))`，`:116` STEP 5000，`:117` MAX_TOKENS 1，`:118` GAP 2.0，`:119` DURATION 240，`:120` MIN_INTERVAL 1800，`:121` HARD_CAP 400000，`:122` SETTLE 60 —— 与 spec Files 一致。
- `build_calibrate_body` `:991-1002`；其尾 3 行（`"messages": ...` / `    }` / 空行）为编辑 1.2 的插入锚（实测唯一）。
- `calibrate_engine` `:1005-1075`：`:1025-1027` 为 `started`/`base`/`batch`；`:1053` 为线性公式 `est = base + (batch - 1) * TPM_CALIBRATE_STEP_TOKENS`；`:1054` HARD_CAP；`:1061-1071` 5xx 单次重试；`:1075` 批间 sleep —— 与 spec 一致。
- `_CALIBRATE_BYTES_PER_STEP` `:1079-1080`；`CALIBRATE_LOCK` `:1081`。
- `_calibrate_send` `:1152-1200`：`:1164-1165` input_bytes 旧公式；`:1177`/`:1180` `timeout=HEADER_TIMEOUT_S`；`:1186-1191` 旧 `except (OSError, HTTPException)` 返回 `(None, False)` —— 与 spec 一致。
- `_calibrate_log` `:1203-1218`：`:1210-1217` 为格式串（编辑 3.3 的 old 覆盖 :1217 起的 6 行，实测唯一）。
- `_calibrate_loop` `:1342-1349`：仅 `result["outcome"] == "rejected"` 才 `record_tpm_probe_result` —— 本 PLAN 不改（`capped`/`probe_timeout` 天然不落网，AC8）。
- `HEADER_TIMEOUT_S` `:43`、`TPM_TOKEN_RATIO` `:111`、`estimate_request_tokens` `:491`、`socket` import `:25`、`RECENT_REQUESTS` deque `:1414`、主通道 `timeout=HEADER_TIMEOUT_S` `:3007`/`:3010`。

### 测试锚点

- 默认值 `:2974-2986`；env seam `:2988-3016`；`TpmCalibrateEngineTest` `:7698-7885`（body shape `:7716-7736`、线性 `:7738-7753`、body error `:7755-7768`、hard cap `:7770-7784`、zero duration `:7786-7796`、timeout between `:7798-7813`、abort `:7815-7833`、5xx `:7835-7847`、连接异常 `:7849-7855`、503 重试 `:7857-7871`、ts 字段 `:7873-7884`）；`TpmCalibrateStateTest` `:7887`，loop 用例 `:7972-8079`；`CalibrateSendTest` `:8255-8302`；`AdminIntegrationTest` `:3447`，`test_calibrate_stats_not_polluted` `:4963-5005`。
- 测试基建：`PROXY_SCRIPT` `:34`、`make_fake_upstream` `:269`（`sleep_stall_all` `:65` + `:100-104`）、`make_scripted_upstream` `:326`、`free_port` `:462`、`start_proxy` `:481`、`load_proxy_module` `:628`、`post_sse_auth` `:664`、`admin_post` `:652`。
- 环境实测：`/usr/bin/python3 --version` → Python 3.9.6；`socket.timeout is TimeoutError` → `False`（`except socket.timeout` 独立分支在 3.9 必需）。

### 全链模拟（PLAN-B 期机械验证，非推断）

- 本 PLAN 全部 old/new 按卡序作用到工作树文件的临时副本：prod 中间态（卡 1 后/卡 2 后）均 `py_compile` 通过；最终 prod/test 与参考实现逐字节相等。
- 红阶段实测（测试先于对应生产）：卡 1 测试 + 原生产 → `Ran 34 tests — FAILED (failures=2, errors=12)`；卡 2 新用例 + 卡 1 生产 → `FAILED (failures=1, errors=5)`；卡 3 新用例 + 卡 2 生产 → `FAILED (failures=4)`（典型报错：`AssertionError: 8 != 7 : 前 7 批 ≤100k 已发送`、`8 != 1`）。绿阶段实测：卡 1 `Ran 34 — OK`、卡 2 `Ran 40 — OK`、卡 3 `Ran 43 — OK`、全量 `Ran 366 tests — OK`。
- 数值实测（`calibrate_target_est(1..11)` 模拟）：`[4000, 8000, 16000, 32000, 52000, 72000, 92000, 112000, 132000, 152000, 172000]`；`calibrate_input_bytes(1)=15996`；`calibrate_est_for_batch("m",1)=4023`（∈[4000,4300]）；前 7 批累计 276161，含第 8 批 388184（< HARD_CAP 400000）——AC4 第 8 批可发；第 9 批 172023 > MAX_PROBE 160000 终止（默认序列）。
- 全部 old_string 实测唯一（`str.count == 1`），含跨编辑依赖锚（编辑 2.9 的 old 依赖编辑 1.21 的 new；编辑 3.4 的 old 依赖编辑 2.10 的 new 等），链式唯一性已在模拟中逐条断言。

## Plan Self-Review（5 项，全卡统查）

1. **spec coverage：** spec Files 条目 → 编辑映射全覆盖——常量区 :114-122→1.1+2.1；`_CALIBRATE_BYTES_PER_STEP` 删除→1.6；`build_calibrate_body` 后三纯函数→1.2；`calibrate_engine` 公式/上限/超时→1.4/1.5/2.4/2.5/2.6/3.2；`CALIBRATE_STATUS_TIMEOUT`→2.1；`_calibrate_send`→1.7/1.8/2.7/2.8；loop 收尾「仅 rejected 写 PROBE_RESULTS」→不改代码、由卡 2/3 的 loop 用例锁定（2.11/3.5）；`_calibrate_log` 可选 `batch=`→3.3；测试常量两用例→1.9-1.12；`TpmCalibrateEngineTest`→1.13-1.20/2.10/3.4；`CalibrateSendTest`→1.21/2.9。AC1→1.9；AC2→1.13；AC3→1.14；AC4→1.14；AC5→3.4+3.5+3.6；AC6→2.9+2.10+2.11；AC7→1.16-1.20（HARD_CAP/timeout/abort/5xx）+2.10（重试期超时）；AC8→2.11+3.6（+3.3 日志不改判定）；AC9→卡 3 全量。Exclusions 均未触碰（无二分、HARD_CAP/推荐公式/优先级/UI 改动）。
2. **placeholder scan：** 全卡 old/new 均为可逐字落盘完整代码，无「略」「TODO」「…」「同前」类占位；无伪代码；每个 Edit 均给出完整方法/块替换，不留内部缺口。
3. **type consistency：** `calibrate_target_est`/`calibrate_input_bytes`/`calibrate_est_for_batch` 返回 `int`（`calibrate_target_est` 对 `batch<=1` fail-open 返回 START，`max(1, int(batch))` 防负/零）；`CALIBRATE_STATUS_TIMEOUT` 为 `str` 哨兵，`_attempt`/engine 用等值比较（与 int 状态码天然隔离，`classify_outcome` 不再收到字符串——在 `_attempt` 内短路）；`TPM_CALIBRATE_TIMEOUT_S` 为 `float`，传入 `HTTPConnection(timeout=...)` 合法；`_result` 的 `threshold` 为 `int|None`，`probe_timeout` 传 `last_ok_est`（可能 None，符合「首批超时 threshold=None」）；测试侧 `mod.calibrate_input_bytes(1)==15996` 为 int 等值断言（`(4000-1)/0.25` 精确）。
4. **可落盘性（换视角：以执行者机械操作复核）：** 所有 old_string 在对应卡的前序状态下实测 count==1；缩进/全角标点/行尾空格逐字核对（编辑 1.10 的 `'3'; "` 结尾保留原空格+引号）；编辑顺序与依赖（1.21→2.9、2.10→3.4、2.2→3.1）已在模拟链中验证；两个目标文件均存在且路径正确；无需跨文件重命名；全链作用后的最终产物可直接编译并通过焦点+全量。
5. **锚点实测：** 上节 Anchor Verification 全部为 grep/py 实测数字（行号、唯一性计数、红绿输出、Python 版本、socket.timeout 判定）；未发现 spec 锚点与代码根本冲突——唯一偏差是 spec 对 4 个测试方法的行号少 1（已在本文件 Global Constraints 显式标注，old_string 为权威锚）。
