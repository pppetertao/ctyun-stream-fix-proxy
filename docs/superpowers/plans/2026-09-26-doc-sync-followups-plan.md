# PLAN.md — doc-sync followups

**Spec：** `docs/superpowers/specs/2026-09-26-doc-sync-followups-design.md`  
**Plan type：** 简化（纯文档/注释，免 TDD）  
**Cards：** 1 卡（tier A → executor）

---

## Global Constraints

- 纯文档/注释任务，豁免 TDD（PLAN 铁律例外条款）。
- 所有改动在 worktree `fix/doc-sync-followups` 分支执行。
- 改后 `ctyun-stream-fix-proxy.py` 的 `.py` 语法须通过 `py_compile`，全量测试须 115 条 OK + exit 0。
- commit：一处一逻辑，本卡 4 处改动合并一个 commit：`docs(readme): 头重试语义/用例数同步 + docstring 锚点去行号 + followups 清偿`。

---

## 任务卡 1（tier A，executor）：4 处文档/注释编辑

**文件：** `README.md`, `ctyun-stream-fix-proxy.py`, `docs/superpowers/followups.md`

### 改动 1：README.md:56 — CTYUN_HEADER_RETRY 描述扩写

**Edit：**

```
old_string:
| `CTYUN_HEADER_RETRY` | `1` | 头超时自动重试次数（0 禁用） |

new_string:
| `CTYUN_HEADER_RETRY` | `1` | 头阶段故障（超时/断连/RST）自动重试次数（0 禁用） |
```

### 改动 2：README.md:82 — 测试用例数 103→115

**Edit：**

```
old_string:
103 个用例。**必须用 `/usr/bin/python3`**

new_string:
115 个用例。**必须用 `/usr/bin/python3`**
```

### 改动 3：ctyun-stream-fix-proxy.py:171-175 — header_timeout_should_retry docstring 去行号

**整体替换 5 行（含函数签名与 return）：**

```
old_string:
def header_timeout_should_retry(budget: int) -> bool:
    """头超时重试决策：budget > 0 时允许重试。调用点 :864（_proxy_relay 首呼
    (socket.timeout, RemoteDisconnected, ConnectionResetError) catch），覆盖
    头阶段三类可重试故障（阻塞超时/上游 FIN-close/上游 RST）。"""
    return budget > 0

new_string:
def header_timeout_should_retry(budget: int) -> bool:
    """头超时重试决策：budget > 0 时允许重试。调用点在 _proxy_relay 头阶段
    首呼的 (socket.timeout, RemoteDisconnected, ConnectionResetError) catch 分支，
    覆盖头阶段三类可重试故障（阻塞超时/上游 FIN-close/上游 RST）。"""
    return budget > 0
```

### 改动 4：docs/superpowers/followups.md — 删除第 7、8 行（两条已清偿条目）

**Edit（Replace 替换整行为空）：**

```
old_string:
- `README.md:56`（CTYUN_HEADER_RETRY env 描述） | conn-reset episode（fc88841）后实际语义为"头阶段故障（超时/FIN-close/RST）自动重试"，README 描述偏窄只写头超时；whole-branch review NOTE（spec Exclusions 当时排除了 README 改动） | 另开小 episode：改为"头阶段故障（超时/断连/RST）自动重试次数（0 禁用）"，顺带修 `README.md:82` 用例数漂移（写 103，现 115）
- `ctyun-stream-fix-proxy.py:172`（`header_timeout_should_retry` docstring 行号锚点 :864 漂移至 :874） | docstring 自身扩行导致引用偏移（相邻 `empty_stream_should_retry` 同款松散引用，项目容忍模式）；task review NOTE 60 分不阻塞 | 下次触碰该区域时顺手改为实际行号或去行号只留描述

new_string:

```

### 验证命令

```bash
# 1. grep 四处改动落位
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/doc-sync-followups

echo "=== [1] README:56 header-retry 描述 ==="
grep -n "头阶段故障（超时/断连/RST）自动重试次数" README.md

echo "=== [2] README:82 115 个用例 ==="
grep -n "^115 个用例" README.md

echo "=== [3] py docstring 无 :864 行号 ==="
grep -n ":864" ctyun-stream-fix-proxy.py && echo "FAIL: :864 still present" || echo "PASS: :864 removed"
grep -n "_proxy_relay 头阶段" ctyun-stream-fix-proxy.py

echo "=== [4] followups.md 条目 8→6 ==="
grep -n "CTYUN_HEADER_RETRY env 描述\|header_timeout_should_retry.*docstring" docs/superpowers/followups.md && echo "FAIL: still present" || echo "PASS: removed"
wc -l docs/superpowers/followups.md

# 2. Python 语法检查
/usr/bin/python3 -c "import py_compile; py_compile.compile('ctyun-stream-fix-proxy.py', doraise=True)" && echo "PASS: py_compile OK" || echo "FAIL: py_compile error"

# 3. 全量测试（timeout ≥120s，网络夹具）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**验收标准（grep 行号以文件现状为准；漂移时以文本匹配优先）：**

| # | grep 命令 | 预期结果 |
|---|-----------|---------|
| 1 | `grep -n "头阶段故障（超时/断连/RST）自动重试次数" README.md` | 命中 1 行（行号 56 或附近漂移可行） |
| 2 | `grep -n "^115 个用例" README.md` | 命中 1 行（行号 82 或附近漂移可行） |
| 3 | `grep -n ":864" ctyun-stream-fix-proxy.py` | 零命中 |
| 3 | `grep -n "_proxy_relay 头阶段" ctyun-stream-fix-proxy.py` | 命中 1 行 |
| 4 | `grep -n "CTYUN_HEADER_RETRY env 描述\|header_timeout_should_retry.*docstring" docs/superpowers/followups.md` | 零命中 |
| 4 | `wc -l docs/superpowers/followups.md` | 6 |
| 5 | py_compile | exit 0 |
| 6 | full test | exit 0, stdout 末行 `OK`，`Ran 115 tests` |

### Commit 指令

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/doc-sync-followups
git add README.md ctyun-stream-fix-proxy.py docs/superpowers/followups.md
git commit -m "docs(readme): 头重试语义/用例数同步 + docstring 锚点去行号 + followups 清偿"
```

---

## Plan Self-Review

### 1. Spec Coverage
spec 4 处改动全部覆盖：README:56（行内 old→new）、README:82（行内 old→new）、py:171-175（5 行整体 old→new）、followups.md:7-8（2 行整行删除）。Exclusions 无遗漏——不动 empty_stream_should_retry、不动部署、不加测试。

### 2. Placeholder Scan
全文 0 `TODO`/`FIXME`/`TKTK`/`...` placeholder。卡内每处 Edit 均提供完整 old_string/new_string 对。

### 3. Type Consistency
纯文本编辑，无类型/接口/签名变更。Python docstring 仅调整描述文字，函数签名与 return 行不变；README markdown 表格仍三列 `| ... | ... | ... |`。

### 4. 可落盘性
4 处 Edit 均为单文件单处替换，old_string 与 worktree 现状核对如下：
- README:56 — `grep -n "CTYUN_HEADER_RETRY" README.md` → 56 行文本与 old_string 完全一致。
- README:82 — `sed -n '82p' README.md` → `103 个用例。**必须用 \`/usr/bin/python3\`**：...`，old_string 截取 `103 个用例。**必须用 \`/usr/bin/python3\`**` 不包含尾部中文，可唯一定位（该子串在文件中仅出现一次）。
- py:171-175 — `sed -n '171,175p' ctyun-stream-fix-proxy.py` → 5 行文本与 old_string 完全一致（含正确缩进）。
- followups.md:7-8 — `sed -n '7,8p' docs/superpowers/followups.md` → 2 行文本与 old_string 完全一致。
全量 `replace_all: false` 不设。

### 5. 锚点实测
执行以下实测（worktree 内）：
- `grep -c "def test " ctyun-stream-fix-proxy.test.py` → 115（验证 spec 的用例数 115 非臆造）。
- `grep -n "CTYUN_HEADER_RETRY" README.md` → 56 行。
- `grep -n "def header_timeout_should_retry" ctyun-stream-fix-proxy.py` → 171 行。
- `wc -l docs/superpowers/followups.md` → 8 行。
以上与 spec R31 Evidence 一致，无漂移。