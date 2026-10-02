# PLAN — Dashboard Token 用量两张表表头中文化

- **Feature:** dashboard-token-header-zh
- **Branch:** fix/dashboard-token-header-zh（worktree `.worktrees/dashboard-token-header-zh`）
- **Date:** 2026-10-02
- **Spec:** `docs/superpowers/specs/2026-10-02-dashboard-token-header-zh-design.md`
- **Baseline:** worktree HEAD `f7cacfa`（spec commit 之后，`git status --short` 干净）；全量 `python3 ctyun-stream-fix-proxy.test.py` 实测 `Ran 366 tests in 166.802s` — `OK`（2026-10-02；unittest 成功路径 exit 0）
- **Tier:** 单卡 tier A（4 处均为纯字面量转写，spec 已把改前/改后文本逐字定死，无 TDD 决策空间）→ 派 `executor`

## Global Constraints

- 改动仅限 2 个文件、4 处：`ctyun-stream-fix-proxy.py` 两处表头行（:3754、:3817），`ctyun-stream-fix-proxy.test.py` 两条测试断言（:3350-3351、:3352-3353）。禁止改其他任何文件、其他行、其他列。
- 提交：Conventional Commits，单卡单 commit，信息 `feat(dash): token 用量表头中文化`；`git add` 只加上述 2 个文件（提交前 `git status --short` 自查仅这 2 文件）；不 push、不 merge、不碰 main。
- 不改 spec Exclusions 内容：JS 渲染/排序逻辑（`_DASH_JS_V2` 及段内脚本不动）、卡片标题（`ctyun-stream-fix-proxy.py:3751`、`:3814`）、其他表（模型速度对比、最近请求、月末投影、TPM 队列表 `:3863`）、列顺序/列数/`colspan`、部署/webhook/systemd。
- 两个表头 old_string 必须用含 `<thead>` 的整行（实测整行全库唯一）；不得只替换 `<th>cache</th>` 之类片段（裸 `<th>Key</th>` 在 `:3863` 另一表另有 1 次命中，片段替换会引入越界改动）。
- 缩进逐字保持：表头行前导 6 空格；测试断言行前导 8 空格、续行前导 22 空格（repr 实测，见 Anchor Verification）。
- 锚点修正（实测，非推断）：spec Files#1 称 `:3754` 属 `_DASH_SECTIONS_STATIC`，实测该行位于 `_DASH_SECTIONS_TABLES`（字面量 3715–3788）；`:3754` 行号与改前文本与 spec 完全一致，仅常量名描述偏差，不影响本卡任何 Edit 与测试（测试只断言 `_DASH_SECTIONS_V2`；表 B `:3817` 实测位于 `_DASH_SECTIONS_V2` 字面量 3790 起）。
- AC#1 的“源码中无 `<th>Key</th>` 残留”按整行核验：裸 `<th>Key</th>` 在 `:3863` 另一表仍存在且属 Exclusions 保留项；表 A 残留核验用“完整旧表头行缺席 + 完整新表头行在场”（见验证命令第 3、4 步）。
- 验证命令：全量 `python3 ctyun-stream-fix-proxy.test.py`（预期 `Ran 366 tests` — `OK`，exit 0，总数与 baseline 不变）；焦点 `python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest.test_v3_token_table_columns -v`（改测试断言后必红、改表头后必绿）。

---

## 卡 1：两张 token 表表头中文化 + 测试断言同步（tier A）

**Files:** `ctyun-stream-fix-proxy.py`、`ctyun-stream-fix-proxy.test.py`

**目标：** 表 A 7 列与表 B 6 列的英文 `<th>` 表头改中文，同步两条引用旧文本的测试断言。

### 步骤 0 — baseline（先跑，登记初始状态）

```
python3 ctyun-stream-fix-proxy.test.py
```

预期：`Ran 366 tests in ~167s` — `OK`，exit 0（与 Header 登记一致）。贴实际 stdout 摘要 + exit code。若此步非全绿 → STOP 报告，不进入后续步骤。

### Edit 1.1（TDD 红步骤）— `ctyun-stream-fix-proxy.test.py:3350-3351`

old（2 行；实测唯一：`<th>cache tokens</th>` 在本文件仅此 1 处命中）：

```
        self.assertIn("<th>cache tokens</th>", v2,
                      "token 按天×模型表缺 cache 列（T2）")
```

new（2 行）：

```
        self.assertIn("<th>缓存</th>", v2,
                      "token 按天×模型表缺缓存列（T2）")
```

### Edit 1.2（TDD 红步骤）— `ctyun-stream-fix-proxy.test.py:3352-3353`

old（2 行；实测唯一：`<th>reasoning tokens</th>` 在本文件仅此 1 处命中）：

```
        self.assertIn("<th>reasoning tokens</th>", v2,
                      "token 按天×模型表缺 reasoning 列（T3）")
```

new（2 行）：

```
        self.assertIn("<th>推理</th>", v2,
                      "token 按天×模型表缺推理列（T3）")
```

**红验证（改完 Edit 1.1 + 1.2 后立即跑，改表头之前）：**

```
python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest.test_v3_token_table_columns -v
```

预期：FAIL — `AssertionError: '<th>缓存</th>' not found in ...`（生产 `:3817` 仍为 `cache tokens`），exit 1。贴实际输出与 exit code。

### Edit 1.3（TDD 绿步骤）— `ctyun-stream-fix-proxy.py:3754` 表 A（`_DASH_SECTIONS_TABLES`）

old（整行；前导 6 空格；实测全库唯一）：

```
      <thead><tr><th>Key</th><th>请求</th><th>prompt</th><th>completion</th><th>cache</th><th>出流量</th><th>流式</th></tr></thead>
```

new（整行）：

```
      <thead><tr><th>密钥</th><th>请求</th><th>提示</th><th>补全</th><th>缓存</th><th>出流量</th><th>流式</th></tr></thead>
```

### Edit 1.4（TDD 绿步骤）— `ctyun-stream-fix-proxy.py:3817` 表 B（`_DASH_SECTIONS_V2`）

old（整行；前导 6 空格；实测全库唯一）：

```
      <thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th><th>cache tokens</th><th>reasoning tokens</th></tr></thead>
```

new（整行）：

```
      <thead><tr><th>日期</th><th>模型</th><th>提示</th><th>补全</th><th>缓存</th><th>推理</th></tr></thead>
```

### 绿验证 + 残留核验（逐条执行，贴实际 stdout + exit code）

```
# 1) 焦点复跑：必绿
python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest.test_v3_token_table_columns -v
# 预期：Ran 1 test — OK，exit 0

# 2) 全量：366 tests 全绿（总数与 baseline 一致）
python3 ctyun-stream-fix-proxy.test.py
# 预期：Ran 366 tests — OK，exit 0

# 3) 旧表头整行缺席（两条命令均预期输出 ABSENT、exit 0）
! grep -qF '<thead><tr><th>Key</th><th>请求</th><th>prompt</th><th>completion</th><th>cache</th><th>出流量</th><th>流式</th></tr></thead>' ctyun-stream-fix-proxy.py && echo ABSENT
! grep -qF '<thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th><th>cache tokens</th><th>reasoning tokens</th></tr></thead>' ctyun-stream-fix-proxy.py && echo ABSENT

# 4) 新表头整行在场（两条命令均预期输出 1、exit 0）
grep -cF '<thead><tr><th>密钥</th><th>请求</th><th>提示</th><th>补全</th><th>缓存</th><th>出流量</th><th>流式</th></tr></thead>' ctyun-stream-fix-proxy.py
grep -cF '<thead><tr><th>日期</th><th>模型</th><th>提示</th><th>补全</th><th>缓存</th><th>推理</th></tr></thead>' ctyun-stream-fix-proxy.py

# 5) 改动范围自查（预期仅这 2 个文件被修改）
git status --short
```

### Commit

```
git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
git commit -m "feat(dash): token 用量表头中文化"
```

---

## Anchor Verification（PLAN-B 写作时实测，非推断）

- `:3754` repr 逐字 = `'      <thead><tr><th>Key</th><th>请求</th><th>prompt</th><th>completion</th><th>cache</th><th>出流量</th><th>流式</th></tr></thead>'`；整行全库 count = 1。
- `:3817` repr 逐字 = `'      <thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th><th>cache tokens</th><th>reasoning tokens</th></tr></thead>'`；整行全库 count = 1。
- `:3350-3351` 与 `:3352-3353` repr 逐字与 spec 改前文本一致；`<th>cache tokens</th>` 与 `<th>reasoning tokens</th>` 在测试文件各恰 1 命中（即两条断言本体）；测试文件对 `Key</th>`/`prompt</th>`/`completion</th>`/`cache</th>` 子串均 0 命中（表 A 改动无其他断言依赖）。
- 常量归属实测：`_DASH_SECTIONS_STATIC` 自 3652 起、`_DASH_SECTIONS_TABLES` 3715–3788、`_DASH_SECTIONS_V2` 自 3790 起；`:3754` ∈ TABLES（spec 描述偏差已在 Global Constraints 修正）；`:3817` ∈ V2。
- 生产文件命中计数：`Key</th>` = 2（`:3754` 待改、`:3863` 另一表保留）；`prompt</th>`/`completion</th>`/`cache</th>` 各 1（均在 `:3754`）。
- 焦点命令语法实测通过（改前）：`python3 ctyun-stream-fix-proxy.test.py DashboardV3CardsTest.test_v3_token_table_columns -v` → `Ran 1 test ... OK`，exit 0。
- 全量 baseline 实测：`Ran 366 tests in 166.802s` — `OK`（2026-10-02）。
- worktree HEAD `f7cacfa`（含 spec commit），`git status --short` 干净。

## Plan Self-Review

1. **spec coverage：** spec Files 4 处 → Edit 1.1（Files#3，:3350）、Edit 1.2（Files#4，:3352）、Edit 1.3（Files#1，:3754）、Edit 1.4（Files#2，:3817），零遗漏。AC#1 → 验证 3+4 表 A 旧行缺席/新行在场；AC#2 → 验证 3+4 表 B 同法；AC#3 → Edit 1.1/1.2 断言文本 + 验证 1；AC#4 → 验证 1+2。Exclusions 未触碰（JS 逻辑、卡片标题、`:3863` 表、列结构、部署）。
2. **placeholder scan：** 卡内 4 个 Edit 的 old/new 均为逐字可落盘文本；扫描“类似”“等”“TODO”“……”零占位命中（Edit 块与验证命令中无省略描述）；验证命令为可直接复制执行的完整命令。
3. **type consistency：** 改动全部为 str 字面量内部替换，无类型或接口变化；测试 `assertIn(str, str)` 两侧类型不变（`v2 = self.mod._DASH_SECTIONS_V2` 为 str）；断言消息仍为 str。
4. **可落盘性：** 4 个 old_string 实测唯一（两整行表头各 count 1；测试两处 `<th>` 字符串在测试文件各仅 1 命中），缩进经 repr 逐字核对（表头 6 空格、断言行 8 空格、续行 22 空格）；目标文件存在、工作区干净、无跨文件重命名或移动。
5. **锚点实测：** 行号 3754/3817/3350/3352 与 spec 改前文本逐字一致（无漂移）；发现并修正 1 处 spec 描述偏差（`:3754` 常量归属为 `_DASH_SECTIONS_TABLES` 而非 `_DASH_SECTIONS_STATIC`）与 1 处 AC 核验口径问题（裸 `<th>Key</th>` 在 `:3863` 仍存在，残留核验改为整行匹配），两条均已写入 Global Constraints，不改变 4 处 Edit 的最终文本。
