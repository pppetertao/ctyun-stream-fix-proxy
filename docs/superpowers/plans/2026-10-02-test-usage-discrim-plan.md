# PLAN — GenSpeedRecentEntryTest 判别力增强（SSE_USAGE_DISTINCT）

- **Feature:** fix/test-usage-discrim
- **Branch:** fix/test-usage-discrim
- **Date:** 2026-10-02
- **Spec:** `docs/superpowers/specs/2026-10-02-test-usage-discrimination-design.md`
- **Baseline:** worktree HEAD `a4b6d83`；全量 `Ran 353 tests in 156.050s — OK`（exit 0，2026-10-02 实测）

## Global Constraints

- 改动文件仅两个：`ctyun-stream-fix-proxy.test.py`（新增 1 常量 + 改 1 测试类）与 `docs/superpowers/followups.md`（删 1 行）。`ctyun-stream-fix-proxy.py` 生产代码零改动——反向判别力自查的临时翻转必须还原，并以 `git diff --quiet ctyun-stream-fix-proxy.py` 证零。
- 不动 `SSE_USAGE` 常量本体（test.py:43）及其全部既有消费点（:4107 / :4115 / :5231 / :5288 / :5297 / :5300 / :5396 / :5408 / :6602 / :6604 / :6608 / :6923 / :7100 / :7117 / :7140）。
- 不引入 pytest / 第三方断言库；维持 stdlib unittest。不新增/删除测试方法 → 全量总数保持 353。
- 本卡为纯测试增强卡（无生产行为改动）：TDD「先写失败测试」不适用；验证 = 全量 353 绿 + 焦点绿 + 反向判别力实测红后还原。
- 卡内 5 个 Edit 的 old_string 均已在写作时实测唯一（`str.count(old) == 1`，含中英文标点逐字比对）；执行者不得凭缩进臆测改写。
- 计数口径提醒：改后 `grep -n "SSE_USAGE_DISTINCT" ctyun-stream-fix-proxy.test.py` 命中 **4 行**（1 定义 + 1 docstring 文本 + 2 代码引用）。spec AC#4 写的「3 处」未计 docstring 文本命中；**代码引用仍恰为 2 处**，与 AC#4 实质要求一致，属 spec 内部计数口径差异，非代码问题。
- 验证命令：全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（预期 `Ran 353 tests — OK`，exit 0）；焦点 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedRecentEntryTest -v`（预期 `Ran 1 test — OK`，exit 0）。
- tier：卡 1 为 A（全部代码已写死；无真机依赖、无运行时数据依赖、无 DI 边界实测）。

---

## 卡 1：新增 `SSE_USAGE_DISTINCT` 常量并改 `GenSpeedRecentEntryTest` 断言（tier A）

**Files:** `ctyun-stream-fix-proxy.test.py`、`docs/superpowers/followups.md`

**目标：** fake upstream 回流的 usage 帧 prompt/completion/total 取 5/3/8（三者两两互异），使 `entry["tokens_completion"] == 3` 能同时识别 `p3_tokens[0]`（prompt）/`p3_tokens[2]`（total）索引错取；闭环删除 followups.md:5 登记项。

### Edit 1.1 — 插入模块级常量（test.py:43 `SSE_USAGE` 之后、:44 `SSE_FAULT_TAIL` 之前）

old（2 行，实测唯一）：

```
SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
SSE_FAULT_TAIL = SSE_REASONING + SSE_FINISH + SSE_DONE
```

new（3 行）：

```
SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
SSE_USAGE_DISTINCT = b'data: {"id":"ud","choices":[],"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":8}}\n\n'
SSE_FAULT_TAIL = SSE_REASONING + SSE_FINISH + SSE_DONE
```

（保持模块级 bytes 常量聚簇；`SSE_USAGE` 本体一字不动。）

### Edit 1.2 — 类 docstring + `setUp` 改用新常量（test.py:8307-8313）

old（7 行，实测唯一）：

```
    黑盒子进程集成：scripted 上游回 SSE_USAGE(prompt=1,completion=1,total=2)
    的流 → recent[-1] 的 tokens_completion 必须取 completion（=1 而非 total=2），
    gen_ms 为流式交付窗口毫秒数（float 且 >= 0）。"""

    def setUp(self) -> None:
        upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
```

new（7 行）：

```
    黑盒子进程集成：scripted 上游回 SSE_USAGE_DISTINCT(prompt=5,completion=3,total=8)
    的流 → recent[-1] 的 tokens_completion 必须取 completion（=3 而非 prompt=5/total=8），
    gen_ms 为流式交付窗口毫秒数（float 且 >= 0）。"""

    def setUp(self) -> None:
        upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_USAGE_DISTINCT + SSE_A + SSE_B + SSE_DONE)
```

（`tearDown` 及类外结构不动；新增常量行使本块由 :8307-8313 顺移至 :8308-8314。注意 `SSE_USAGE(prompt=1,completion=1,total=2)` 字样在 `TokenRelayTest` docstring :5297 亦存在——属 Exclusions 内的既有消费点，本卡不动；Edit 1.2 的 7 行整块 old_string 已实测唯一，不会误击。）

### Edit 1.3 — 逐字节回显断言改用新常量（test.py:8321-8323）

old（4 行，实测唯一；`def` 行保证不被 :5300 / :5408 的同形断言误击）：

```
    def test_recent_entry_carries_completion_and_gen_ms(self) -> None:
        data = post_sse(self.proxy_port)
        self.assertEqual(data, SSE_USAGE + SSE_A + SSE_B + SSE_DONE,
                         "stream must relay byte-exact")
```

new（4 行）：

```
    def test_recent_entry_carries_completion_and_gen_ms(self) -> None:
        data = post_sse(self.proxy_port)
        self.assertEqual(data, SSE_USAGE_DISTINCT + SSE_A + SSE_B + SSE_DONE,
                         "stream must relay byte-exact")
```

### Edit 1.4 — 核心断言改锚 completion=3（test.py:8333-8335，改后顺移 :8334-8336）

old（3 行，实测唯一）：

```
        self.assertEqual(entry["tokens_completion"], 1,
                         "completion must be usage completion_tokens (=1), "
                         "not total_tokens (=2)")
```

new（3 行）：

```
        self.assertEqual(entry["tokens_completion"], 3,
                         "completion must be usage completion_tokens (=3), "
                         "not prompt_tokens (=5) or total_tokens (=8)")
```

（断言值 3 必须与新常量 `completion_tokens` 严格一致；写成 1 即判别力增强失效，task review 需 diff 核对。）

### Edit 1.5 — 删除 followups.md:5 登记行（cross-file 小改动）

old（2 行，实测唯一；保留 :4 不动，避免误删邻行）：

```
ctyun-stream-fix-proxy.py | spec Files 的 tpm_probe_charges_budget 纯文档函数未实现（零影响，声明性条目） | 确认弃用或补一个断言用函数
ctyun-stream-fix-proxy.test.py:43 | SSE_USAGE 的 prompt_tokens 与 completion_tokens 同为 1，GenSpeedRecentEntryTest 的 ==1 断言无法区分 p3_tokens 索引互换（[0]/[1]） | 把 fake upstream 的 completion_tokens 改为异于 prompt 的值（如 3），增强 recent 条目取值判别力
```

new（1 行）：

```
ctyun-stream-fix-proxy.py | spec Files 的 tpm_probe_charges_budget 纯文档函数未实现（零影响，声明性条目） | 确认弃用或补一个断言用函数
```

（同文件 :2/:3 其余登记项不动；文件 `wc -l` 由 5 → 4。）

### 判别力矩阵（写进本卡的原因：本卡无生产行为改动，断言价值必须自证）

生产取值点 `ctyun-stream-fix-proxy.py:2923` `tokens_completion=p3_tokens[1] if p3_tokens else 0`；五元组序由 `usage_dict_tokens`（:327-371 实测）定义为 `(prompt, completion, total, cache_read, reasoning)`。

| 假想生产实现 | `entry["tokens_completion"]` | 新断言 `== 3` | 旧断言 `== 1`（旧 SSE_USAGE 1/1/2） |
|---|---|---|---|
| `p3_tokens[1]`（现实现，正确） | 3 | pass | pass |
| `p3_tokens[0]`（错取 prompt） | 5 | **fail（5 != 3）** | **pass（盲区，1 != 1 不成立）** |
| `p3_tokens[2]`（错取 total） | 8 | **fail（8 != 3）** | fail（2 != 1） |

三者两两互异（5/3/8）保证任一索引错取在新断言下必红；旧常量 1/1/2 对 [0]/[1] 互换完全无判别力（本 episode 动机）。

解析链路已实测（PLAN-B 期机械验证，非推断）：`sse_line_usage(新 usage 行)` → `{'prompt_tokens': 5, 'completion_tokens': 3, 'total_tokens': 8}` → `usage_dict_tokens(...)` → `(5, 3, 8, 0, 0)`，即 `p3_tokens[1] == 3`。

### 验证命令（执行者按序执行，逐条贴 stdout + exit code）

```
# 1) 焦点：改后必绿
/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedRecentEntryTest -v
# 预期：Ran 1 test — OK，exit 0

# 2) 反向判别力实测（spec AC#3，临时改生产代码后必须还原）
#    2a. 临时把 ctyun-stream-fix-proxy.py:2923 的 p3_tokens[1] 改为 p3_tokens[0]
/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedRecentEntryTest -v
#    预期：FAIL，assertEqual 报文 expected 3 / actual 5（断言失败信息锚定 completion=3）
#    2b. 再改为 p3_tokens[2]
/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedRecentEntryTest -v
#    预期：FAIL，actual 8

# 3) 还原生产代码并证零改动
git checkout -- ctyun-stream-fix-proxy.py
git diff --quiet ctyun-stream-fix-proxy.py && echo "production clean"

# 4) 焦点复跑：还原后必绿
/usr/bin/python3 ctyun-stream-fix-proxy.test.py GenSpeedRecentEntryTest -v

# 5) 全量：改后仍 353 绿（总数不变）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
# 预期：Ran 353 tests — OK，exit 0

# 6) 常量引用清点（预期 4 行：44 定义 / 8308 docstring / 8314 setUp / 8324 回显断言）
grep -n "SSE_USAGE_DISTINCT" ctyun-stream-fix-proxy.test.py

# 7) followups 闭环（预期 0 命中，grep exit 1）
grep -c "SSE_USAGE 的 prompt_tokens" docs/superpowers/followups.md
```

### Commit

一个逻辑改动一个 commit，两个文件同 commit：

```
test(dashboard): recent 条目断言改用 SSE_USAGE_DISTINCT（prompt≠completion）增强 p3_tokens 索引判别力
```

---

## Anchor Verification（PLAN-B 写作时实测，非推断）

- `test.py:43` 常量定义 repr 逐字：`SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'`；:44 为 `SSE_FAULT_TAIL = SSE_REASONING + SSE_FINISH + SSE_DONE` → 插入位成立。
- `GenSpeedRecentEntryTest` 位于 :8304-8339，`setUp` :8311-8315，`test_recent_entry_carries_completion_and_gen_ms` :8321-8339；docstring :8307-8309 — 与 spec 行号锚点一致。
- `ctyun-stream-fix-proxy.py:2923` 实测为 `tokens_completion=p3_tokens[1] if p3_tokens else 0,`（spec 锚点一致）；`usage_dict_tokens` :327-371 五元组序 `(prompt, completion, total, cache_read, reasoning)` 实测。
- `docs/superpowers/followups.md` 现 5 个内容行，:5 为待删行（repr 实测），文件以 `\n` 结尾。
- 全量 baseline 实测：`Ran 353 tests in 156.050s — OK`，exit 0。
- 5 个 old_string 实测唯一（`str.count == 1`）；`SSE_USAGE_DISTINCT` 在 test.py 现为 0 命中。

## Plan Self-Review

1. **spec coverage：** spec Files 全部 3 条 → Edit 1.1（常量）、Edit 1.2-1.4（类内 docstring/回显/核心断言）、Edit 1.5（followups 删行），无遗漏；AC#1 → 验证 5；AC#2 → 验证 1；AC#3 → 验证 2（含还原）；AC#4 → 验证 6 + Global Constraints 口径说明（4 行 vs spec 写的 3 处，差值为 docstring 文本命中）；AC#5 → 验证 7。Exclusions 均未触碰。
2. **placeholder scan：** 卡内 5 个 Edit 的 old/new 均为可逐字落盘的完整代码/文本，无「略」「TODO」「…」占位；无伪代码。
3. **type consistency：** 新常量为 module-level `bytes`，与 `SSE_A`/`SSE_B`/`SSE_DONE` 及 `body_override` 的 bytes 拼接类型一致；断言左侧 `entry["tokens_completion"]` 为 int（生产元组 `p3_tokens[1]` 取自 `int`），右侧字面量 `3` 为 int，且等于新常量 `completion_tokens` 值，三处一致。
4. **可落盘性：** 5 个 old_string 均实测唯一且逐字（含全角括号/箭头/中文标点）；目标路径 `docs/superpowers/plans/` 与 `docs/superpowers/followups.md` 均存在；工作树 git 状态干净（仅本计划文件待写）；无跨文件重命名/移动。
5. **锚点实测：** 见上节 Anchor Verification——行号、函数名、取值索引、baseline 数字全部实测复核；未发现 spec 锚点与代码冲突（唯一差异为 AC#4 的 grep 计数口径，已在 Global Constraints 显式标注，不影响实质验收）。
