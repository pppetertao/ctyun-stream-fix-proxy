# 测试判别力增强：GenSpeedRecentEntryTest 用差异化 usage 常量

## Goal

消除 GenSpeedRecentEntryTest 对 `p3_tokens[0]`（prompt）与 `p3_tokens[1]`（completion）索引互换的盲区：fake upstream 的 usage 帧 prompt/completion 同为 1 时 `== 1` 断言无法区分两者，本 spec 让该测试能真实锚死"取 completion 而非 prompt/total"。

## Files to Change

- `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:43`（常量区 `SSE_USAGE` 定义行之后插入新行）— 新增模块级常量：
  ```python
  SSE_USAGE_DISTINCT = b'data: {"id":"ud","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
  ```
  取值 5/3/8 三者互不相同，可同时区分 [0]/[1]/[2] 索引互换。**不改 SSE_USAGE 本体**（方案 B，零 blast radius）。
- `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:8304-8339`（`class GenSpeedRecentEntryTest(unittest.TestCase)`）— 仅改 `setUp`（:8311-8315）与 `test_recent_entry_carries_completion_and_gen_ms`（:8321-8339）中的常量引用与断言，类外结构不动：
  1. :8313 `body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE` → `body_override=SSE_USAGE_DISTINCT + SSE_A + SSE_B + SSE_DONE`。
  2. :8323 逐字节回显断言 `self.assertEqual(data, SSE_USAGE + SSE_A + SSE_B + SSE_DONE, ...)` → 同样改用 `SSE_USAGE_DISTINCT`。
  3. :8333-8335 核心断言改为：
     ```python
     self.assertEqual(entry["tokens_completion"], 3,
                      "completion must be usage completion_tokens (=3), "
                      "not prompt_tokens (=5) or total_tokens (=8)")
     ```
  4. :8307-8308 类 docstring 中 `SSE_USAGE(prompt=1,completion=1,total=2)` 字样更新为 `SSE_USAGE_DISTINCT(prompt=5,completion=3,total=8)`，`:8308` 的"=1 而非 total=2"更新为"=3 而非 prompt=5/total=8"。
- `/Users/peter/Documents/project/ctyun-stream-fix-proxy/docs/superpowers/followups.md:5` — 删除该条 followup（本 episode 闭环处理，行原文：`ctyun-stream-fix-proxy.test.py:43 | SSE_USAGE 的 prompt_tokens 与 completion_tokens 同为 1，...`）。

## Acceptance Criteria

- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全量 353 个测试全绿（baseline 353 绿；改后总数不变仍 353）。
- 焦点验证：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedRecentEntryTest -v` 通过，且断言失败信息锚定 completion=3。
- 反向判别力验证（PLAN/实现期自查，不留代码）：把生产代码 `ctyun-stream-fix-proxy.py:2923` 的 `p3_tokens[1]` 临时改为 `p3_tokens[0]` 跑该测试必须红（expected 3, actual 5）；改 `p3_tokens[2]` 必须红（expected 3, actual 8）。三种取值互异保证任一索引错取都挂。
- `grep -n "SSE_USAGE_DISTINCT" ctyun-stream-fix-proxy.test.py` 命中 3 处（1 定义 + 2 引用），`SSE_USAGE` 其余消费点（:4107/:4115/:5231/:5288/:5300/:5396/:5408/:6608/:6923/:7100/:7117/:7140 及 followups.md 外计划文档）零改动。
- followups.md:5 条目已删除，文件内不再出现 `SSE_USAGE 的 prompt_tokens` 字样。

## Risks

- SSE_USAGE 共享面大（生产路径 7 处测试 + TPM settle 断言 `bucket used == 4`（test.py:6640）与 `stderr.count("tpm=2")`（:6633）依赖 total=2，TokenRelayTest :5308/:5309 与 TokenPersistTest :5417/:5418 断言 prompt=1/completion=1）：方案 B 不动 SSE_USAGE 即规避全部此面；若误用方案 A 需同步改 ≥6 处数值断言。
- 新常量插入位置须在 `SSE_USAGE`（:43）之后、`SSE_FAULT_TAIL`（:44）之前的常量区，保持模块级 bytes 常量聚簇；插错位置（如类内部）不影响行为但破坏既有组织约定。
- `entry["tokens_completion"]` 断言值 3 与新常量 completion_tokens 必须一致；实现时手滑写回 1 会让判别力增强形同虚设——task review 需 diff 核对。
- followups.md 行删除属跨文件小改动，须确认只删 :5 一条，不动同文件 :2/:3/:4 其余登记项。

## Exclusions

- 不改 `ctyun-stream-fix-proxy.py` 任何生产代码（p3_tokens 取值逻辑上一 episode 已修复，本 episode 仅增强测试判别力）。
- 不动 SSE_USAGE 常量本体及其全部既有消费测试（TokenRelayTest / TokenPersistTest / TpmRateLimitTest / TpmPerModelTest / TpmModelSelectionTest / finish-retry 集成等）。
- 不处理 followups.md 其余条目（queue-timeout flaky、calibrate 401/403 误判、tpm_probe_charges_budget 声明性条目）。
- 不引入 pytest / 第三方断言库，维持 stdlib unittest。

## R31 Evidence

[R31-S1] 问题存在：docs/superpowers/followups.md:5 登记行原文 `ctyun-stream-fix-proxy.test.py:43 | SSE_USAGE 的 prompt_tokens 与 completion_tokens 同为 1，GenSpeedRecentEntryTest 的 ==1 断言无法区分 p3_tokens 索引互换（[0]/[1]） | 把 fake upstream 的 completion_tokens 改为异于 prompt 的值（如 3），增强 recent 条目取值判别力`。SSE_USAGE 真实定义（Read test.py:43 逐字）：

```
$ grep -n "SSE_USAGE" ctyun-stream-fix-proxy.test.py
43:SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
```

prompt_tokens=1 与 completion_tokens=1 同值：生产侧 recent 条目取自 `ctyun-stream-fix-proxy.py:2923` `tokens_completion=p3_tokens[1] if p3_tokens else 0`；若实现错取 `p3_tokens[0]`（prompt），测试断言 `entry["tokens_completion"] == 1` 照样通过——判别力缺陷确认。仅 `p3_tokens[2]`（total=2）能被现断言区分，[0]/[1] 互换不可区分。

[R31-S2] 方案：方案 B（新增独立常量，零 blast radius）。GenSpeedRecentEntryTest 现断言真实代码（Read test.py:8333-8335 逐字）：

```
        self.assertEqual(entry["tokens_completion"], 1,
                         "completion must be usage completion_tokens (=1), "
                         "not total_tokens (=2)")
```

消费点证据（grep 全部命中，断言数值面决定方案）：TokenRelayTest test.py:5308/:5309 `entry["tokens_prompt"] == 1` / `entry["tokens_completion"] == 1`；TokenPersistTest :5417/:5418 同样断言 ==1、:5428 落盘断言 ==1；TpmRateLimitTest.test_usage_settle_releases_budget :6633 `stderr.count("tpm=2") >= 2` 与 :6640 `buckets[0]["used"] == 4`（2+2）硬依赖 SSE_USAGE total_tokens=2；TpmPerModelTest :6923 与 TpmModelSelectionTest :7117 的 `SSE_A + SSE_USAGE + SSE_DONE` body 参与逐字节回显断言（:7100/:7140）。方案 A 需同步改 ≥6 处数值断言且动 TPM settle 口径，方案 B 只加 1 常量 + 改 1 测试类——选 B。

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -3
Ran 353 tests in ~150s — OK（baseline 全绿）
```
