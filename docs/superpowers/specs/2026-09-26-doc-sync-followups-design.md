# Doc-sync followups（fc88841 whole-branch review 两条 NOTE）

## Goal
为 ctyun-stream-fix-proxy 维护者同步 README 与 docstring 文案至 fc88841 后真实语义，并销账 followups.md 两条已完成条目。

## Files to Change

1. `README.md:56`（`CTYUN_HEADER_RETRY` 行）
   - old：`| \`CTYUN_HEADER_RETRY\` | \`1\` | 头超时自动重试次数（0 禁用） |`
   - new：`| \`CTYUN_HEADER_RETRY\` | \`1\` | 头阶段故障（超时/断连/RST）自动重试次数（0 禁用） |`
2. `README.md:82`（测试用例数）
   - old：`103 个用例。**必须用 \`/usr/bin/python3\`**`
   - new：`115 个用例。**必须用 \`/usr/bin/python3\`**`
   - 依据：`grep -c "def test" ctyun-stream-fix-proxy.test.py` = 115（本 spec R31-S2 实测）。
3. `ctyun-stream-fix-proxy.py:171-175`（`header_timeout_should_retry` docstring）— **方案选定：去行号只留描述**（理由：docstring 扩行导致 :864→:874 漂移已发生一次；行号引用本质不稳，项目内 `empty_stream_should_retry` :166 的 ":656" 同款漂移容忍。本 episode 只动 followup 点名的函数，`empty_stream_should_retry` 不在 scope）
   - old（:171-175 完整 docstring 块）：
     ```python
     def header_timeout_should_retry(budget: int) -> bool:
         """头超时重试决策：budget > 0 时允许重试。调用点 :864（_proxy_relay 首呼
         (socket.timeout, RemoteDisconnected, ConnectionResetError) catch），覆盖
         头阶段三类可重试故障（阻塞超时/上游 FIN-close/上游 RST）。"""
         return budget > 0
     ```
   - new：
     ```python
     def header_timeout_should_retry(budget: int) -> bool:
         """头超时重试决策：budget > 0 时允许重试。调用点在 _proxy_relay 头阶段
         首呼的 (socket.timeout, RemoteDisconnected, ConnectionResetError) catch 分支，
         覆盖头阶段三类可重试故障（阻塞超时/上游 FIN-close/上游 RST）。"""
         return budget > 0
     ```
   - 函数签名、函数体、行宽控制（≤79 col 同现状）不变。
4. `docs/superpowers/followups.md`（删除第 7、8 行两条已完成条目；保留其余 6 条不动）
   - old：第 7 行（`README.md:56`（CTYUN_HEADER_RETRY env 描述）…）+ 第 8 行（`ctyun-stream-fix-proxy.py:172`（`header_timeout_should_retry` docstring…）
   - new：两行整行删除（含行尾换行），其余行内容与顺序不变。

## Acceptance Criteria

1. `grep -n "头阶段故障（超时/断连/RST）自动重试次数" README.md` 命中 1 行且行号为 56。
2. `grep -n "^115 个用例" README.md` 命中 1 行且行号为 82。
3. `grep -n ":864" ctyun-stream-fix-proxy.py` 零命中；`grep -n "_proxy_relay 头阶段" ctyun-stream-fix-proxy.py` 命中 1 行。
4. `grep -n "CTYUN_HEADER_RETRY env 描述\|header_timeout_should_retry.*docstring" docs/superpowers/followups.md` 零命中；`wc -l docs/superpowers/followups.md` = 6（原 8 行 - 2 行）。
5. `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全量退出码 0，stdout 末行 `OK`（确认 .py docstring 编辑无语法破坏，仍 115 用例）。
6. `node ~/.zcode/scripts/validate-r31-evidence.mjs docs/superpowers/specs/2026-09-26-doc-sync-followups-design.md` 退出码 0（见下文 R31 Evidence）。

## Risks

- **docstring 块精确匹配风险**：implementer 必须按 :171-175 五行整体替换（含函数签名与 `return` 行），逐行 Edit 容易漏掉续行缩进；spec 已给完整 old/new 块。
- **README 表格对齐**：改动行在 markdown 表内，new 文本保持 `| ... | ... | ... |` 三列结构，不破坏表宽对齐。
- **followups.md 行号敏感**：本 spec 删除第 7/8 行后，剩余条目行号前移——后续 followup 引用本文件其他条目时用条目首列锚点（file:line），不引用 followups.md 自身行号。
- **验证测试时长**：全量 115 用例含网络夹具，预计 30-60s；卡 brief 需写明 timeout ≥120s。

## Exclusions

- 不动 `empty_stream_should_retry` :166 docstring 的 ":656" 同款漂移（followup 未点名，不在 scope）。
- 不动 `ctyun-stream-fix-proxy.py` 其他 docstring / 注释；不改任何函数体/逻辑/常量。
- 不 redeploy：注释级无行为变化，DELIVER 时仅 `cp` 同步到 `~/.local/bin/ctyun-stream-fix-proxy.py`，**不** `launchctl kickstart`。
- 不加测试：文档/注释豁免 TDD（PLAN 铁律例外条款）。
- 不处理 followups.md 其他 6 条（各自独立 episode）。

## R31 Evidence

[R31-S1] README 现状（改动前）：

```text
$ grep -n "CTYUN_HEADER_RETRY\|103 个用例\|115 个用例" README.md
56:| `CTYUN_HEADER_RETRY` | `1` | 头超时自动重试次数（0 禁用） |
82:103 个用例。**必须用 `/usr/bin/python3`**：Homebrew 的 Python 3.14 `http.server.HTTPServer` 构造会挂死（进程存活但不 LISTEN、零报错）。
```

[R31-S2] docstring 现状 + 测试数实测：

```text
$ sed -n '171,175p' ctyun-stream-fix-proxy.py
def header_timeout_should_retry(budget: int) -> bool:
    """头超时重试决策：budget > 0 时允许重试。调用点 :864（_proxy_relay 首呼
    (socket.timeout, RemoteDisconnected, ConnectionResetError) catch），覆盖
    头阶段三类可重试故障（阻塞超时/上游 FIN-close/上游 RST）。"""
    return budget > 0
$ grep -n "header_timeout_should_retry" ctyun-stream-fix-proxy.py
171:def header_timeout_should_retry(budget: int) -> bool:
874:                if not header_timeout_should_retry(HEADER_RETRY_MAX):
$ grep -c "def test" ctyun-stream-fix-proxy.test.py
115
```

[R31-S2] followups.md 待删条目现状：

```text
$ sed -n '7,8p' docs/superpowers/followups.md
- `README.md:56`（CTYUN_HEADER_RETRY env 描述） | conn-reset episode（fc88841）后实际语义为"头阶段故障（超时/FIN-close/RST）自动重试"，README 描述偏窄只写头超时；whole-branch review NOTE（spec Exclusions 当时排除了 README 改动） | 另开小 episode：改为"头阶段故障（超时/断连/RST）自动重试次数（0 禁用）"，顺带修 `README.md:82` 用例数漂移（写 103，现 115）
- `ctyun-stream-fix-proxy.py:172`（`header_timeout_should_retry` docstring 行号锚点 :864 漂移至 :874） | docstring 自身扩行导致引用偏移（相邻 `empty_stream_should_retry` 同款松散引用，项目容忍模式）；task review NOTE 60 分不阻塞 | 下次触碰该区域时顺手改为实际行号或去行号只留描述
```
