# PLAN — TPM_TOKEN_RATIO 0.3 → 0.25 微调（简化单卡）

- Spec: `docs/superpowers/specs/2026-09-30-tpm-rate-limiting-design.md`（决策 1 rev#2 + Risks）
- 工作树: `.worktrees/tpm-ratio-recal`（分支 `fix/tpm-ratio-recal`）
- 规模: 2 文件（prod 1 字面量 + 测试断言/est 注释/seam 跟随），1 卡 A 档

## Global Constraints

- TDD 顺序：先改测试断言跑红 → 改生产字面量 → 全量绿；验证 `python3 ctyun-stream-fix-proxy.test.py`（基线 161 例全绿）。
- 范围锁定：只动 `TPM_TOKEN_RATIO` 默认值及其测试跟随；不碰 env seam 测试的显式 `"0.3"` 覆盖值（证明 env 覆盖仍有效，与默认解耦）、不碰 `TPM_QUEUE_TIMEOUT_S=0.3`（超时 seam，与比率无关）。
- 场景保语义：est 数值关系被 0.25 打破的用例（队列耗尽/队列满）按 a90623d 同法调 body seam（x 数 280→347，est 104 不变），不调 LIMIT。

## 任务卡 1（tier A）：recalibrate TPM_TOKEN_RATIO 0.3 → 0.25

**锚点**（已实测）：
- `ctyun-stream-fix-proxy.py:60` — `TPM_TOKEN_RATIO = float(os.environ.get("CTYUN_TPM_TOKEN_RATIO", "0.3"))`（全文件唯一 0.3 字面量）
- `ctyun-stream-fix-proxy.test.py:886` — `self.assertEqual(mod.TPM_TOKEN_RATIO, 0.3)`
- est 期望值断言已全部现算（:945-957 用 `mod.TPM_TOKEN_RATIO`、:4071-4072/4234/4288 用模块级 `TPM_RATIO`），无需改断言逻辑

**执行顺序与代码（全部写死）**：

1) 测试断言改 0.25（红）— `ctyun-stream-fix-proxy.test.py:886`：

```python
        self.assertEqual(mod.TPM_TOKEN_RATIO, 0.25)
```

红证明命令：`python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_tpm_constants_load`
预期红：`AssertionError: 0.3 != 0.25`（旧默认 0.3 对断言 0.25）。

2) 生产字面量改 0.25 — `ctyun-stream-fix-proxy.py:60`：

```python
TPM_TOKEN_RATIO = float(os.environ.get("CTYUN_TPM_TOKEN_RATIO", "0.25"))
```

（该行无 0.3 注释；全文件其余 0.3 字面量经 grep 确认仅此一处。）

3) 测试 seam/注释跟随（保场景语义 + 数值同步）：

3a. `ctyun-stream-fix-proxy.test.py:4004`（TpmAdmitHookTest，LIMIT=50；209B est 52 > 50 仍拒，注释同步）：

```python
        # len ~209 bytes → est = max(1, 209*0.25) = 52 > 50
```

3b. `test_queue_exhaustion_and_window_roll`（:4049-4051、:4055）— est@0.25 下 87+10=97 ≤ 110 不再排队，body seam x 280→347（416B，est 104 不变）保场景：

```python
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 347 + b'"}]}')   # 416B → est = int(416*ratio) = 104 ≤ 110
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # 41B → est = 10
```

```python
        # 104+10=114 > 110 → 排队；无 settle，预算保持至窗口滚过（~2s）后放行
```

（docstring "est 104/110" 仍成立，不动。）

3c. `test_queue_timeout_returns_429`（:4098-4099、:4103）— 22+10=32 > 30 仍触发，仅注释数值同步：

```python
                    + b"x" * 20 + b'"}]}')   # 89B → est = int(89*ratio) = 22 ≤ 30
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 10
```

```python
        # 22+10=32 > 30 → 排队；窗口 60s 不滚、无 settle → 2s 超时 → 429
```

3d. `test_queue_full_immediate_429`（:4128-4129）— est@0.25 下 87 只占 110 的 79%，3 并发小请求可直接准入不再撞满队列，body seam x 280→347（est 104，剩余 6 < 10）保场景：

```python
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 347 + b'"}]}')   # est = 104/110，占满预算
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 10
```

3e. `test_usage_settle_releases_budget`（:4174-4175、:4179）— 自建代理 LIMIT=200 场景仍触发，仅注释同步（该测试 body 保持 280）：

```python
                    + b"x" * 280 + b'"}]}')   # est = 87，settle → 2（释放 85）
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 10
```

```python
        # settle 已把预算从 87 校正到 2 → 第二请求立即准入（无窗口等待）
```

3f. 纯注释数值同步（场景均不受影响）：:4204 `# est = 12 ≤ 110` → `# est = 10 ≤ 110`；:4230 `# 41B → est = 12` → `# 41B → est = 10`；:4281 `# 41B → est = 12` → `# 41B → est = 10`。

**不改**（已核实不因 0.25 失效）：
- :907/:921 env seam 测试显式 `"0.3"` 覆盖 → 与默认解耦，仍绿（覆盖值 ≠ 新默认，seam 证明更强）。
- :1071 `mod.TPM_QUEUE_TIMEOUT_S = 0.3` → 超时 seam，非比率。

**验证命令**（同次 dispatch 内跑，贴 stdout + exit code）：
1. `python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_tpm_constants_load` → 先红后绿
2. `python3 ctyun-stream-fix-proxy.test.py` → 161 例全绿（`OK`，无 FAILED/ERROR）

**验收**：全量 161 例 OK；`git diff` 仅含上述 2 文件改动；无新 0.3/0.25 字面量遗留。

**commit**: `fix(proxy): recalibrate TPM_TOKEN_RATIO 0.3 -> 0.25 (production evidence 5/7 hard-rejected)`
