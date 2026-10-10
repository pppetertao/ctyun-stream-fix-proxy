# Episode F4 Design: followups 维护三件套（改名 / do_GET 读门禁收敛 / 声明性死条目处置）

## Goal

收口 followups.md 三条纯维护项：误导性测试改名（行 7）、do_GET 9 处逐字复制的读门禁 5 行块收敛为 1 个 helper（行 8）、从未实现的 `tpm_probe_charges_budget` 声明性条目弃用删除（行 4）——行为逐字节等价，389 既有测试全绿。

## Files to Change

### 1. /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py

**`AdminHandler`（:3396）新增 `_read_gate`（插在 `protocol_version` :3397 与 `do_GET` :3399 之间）：**

```python
    def _read_gate(self) -> bool:
        """do_GET 读端点统一读门禁：read_allowed 通过返回 True；拒绝时已发送
        403（文案与原 9 处逐字一致）并返回 False。调用方：`if not self._read_gate(): return`。"""
        if read_allowed(self.client_address[0],
                        self.headers.get("X-Admin-Token") or "",
                        os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            return True
        self._send_json(403, {"error": "read requires X-Admin-Token"})
        return False
```

**9 处调用点退化**（各删 5 行换 2 行，位置不动、仍先于敏感数据构造）：`do_GET`（:3399）内 :3406（/api/stats）、:3413（/api/tpm_stats）、:3420（/api/tpm_settings）、:3427（/api/tpm_calibrate）、:3434（/api/config）、:3446（/api/health）、:3453（/api/probe_history）、:3486（/api/errors 列表分支）、:3507（/api/logs）：

```python
            if not self._read_gate():
                return
```

硬约束：403 状态码+响应体 `{"error": "read requires X-Admin-Token"}` 逐字节不变（`_send_json` :3669 → `json.dumps(..., ensure_ascii=False)` 默认分隔符）；`read_allowed` :2366 本体、`write_allowed` 及 /api/errors 详情分支 :3469-3473 零改动。

### 2. /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py

- **:1782** `ProxyDashboardUnitTest.test_persist_upstream_carries_tpm_model_budgets` 改名 `test_set_tpm_model_budgets_persists_full_schema`（仅 def 行；:1783-1785 注释保留——它解释守卫来历，改名后仍准确）。
- **:649** `admin_get(port, path, headers=None)` 加第四参 `host: str = "127.0.0.1"`，:650 `HTTPConnection("127.0.0.1", ...)` 改用 `host`（既有调用方零影响）。
- **AdminIntegrationTest（:3645，:4036 旁）新增矩阵测试**：复用 :4039-4069 的 0.0.0.0+token 重建与 LAN IP 枚举/skipTest 手法，端点表 `["/api/stats","/api/tpm_stats","/api/tpm_settings","/api/tpm_calibrate","/api/config","/api/health","/api/probe_history","/api/errors","/api/logs"]`（/api/errors **不带** id 参数命中列表分支），每端点 subTest 四形态：
  - LAN 无 token → `(403, b'{"error": "read requires X-Admin-Token"}')`（status+body 逐字节）；
  - LAN 错 token → 同上 403；
  - LAN 对 token（`{"X-Admin-Token": "sekret"}`）→ 200；
  - loopback（admin_get 默认 host）无 token → 200。
  此测试为 characterization pin：**提取前先跑必须绿**（行为守卫网）；red 由下一条结构测试承担。
- **:5352（BodyErrorTest 前）新增 `AdminReadGateStructureTest`**（`import inspect` 补顶部 import）：

```python
class AdminReadGateStructureTest(unittest.TestCase):
    def test_read_gate_helper_converges_nine_inline_blocks(self) -> None:
        mod = load_proxy_module()          # :637 既有基建
        src = inspect.getsource(mod.AdminHandler)
        self.assertIn("def _read_gate(self) -> bool:", src)
        self.assertEqual(src.count('"read requires X-Admin-Token"'), 1)   # 提取前 9 → red
        self.assertGreaterEqual(src.count("self._read_gate()"), 9)
        self.assertEqual(src.count("read_allowed("), 1)                   # 仅 helper 内一处
```

- **TpmCalibrateEngineTest（:8340）新增 pin 测试** `test_tpm_probe_charges_budget_stays_disposed`：`self.assertFalse(hasattr(mod, "tpm_probe_charges_budget"))`（mod 经 load_proxy_module）。生而绿——该函数从未存在于代码（见 R31 证据 3），无 red 可言；此断言钉死弃用决策，防僵尸半实现。

### 3. /Users/peter/Documents/project/ctyun-stream-fix-proxy/docs/superpowers/specs/2026-10-01-tpm-auto-calibrate-design.md

**:30** 删除句尾声明 `；新增纯函数 \`tpm_probe_charges_budget(model)\` 仅用于文档/断言目的，返回 \`False\``（处置选 a：纯声明、零调用、零实现，符合无死代码规则；使该 spec 回到真实现状）。

### 4. /Users/peter/Documents/project/ctyun-stream-fix-proxy/docs/superpowers/followups.md

**:4/:7/:8** 删除三条已解决行（卡 3 收尾时统一执行）。

## Acceptance Criteria

- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿：既有 389 全过 + 新增 3 用例过（矩阵 subTest 全过；沙箱无 LAN IP 时按 :4069 惯例 skipTest，不判失败）。
- 提取前：矩阵测试跑通（绿）+ 结构测试确定性 FAIL（literal 计数 9≠1）——TDD red 有据。
- grep 复核（生产文件）：`grep -c 'self._read_gate()'` ≥9；`grep -c '"read requires X-Admin-Token"'` == 1；`grep -c 'read_allowed('`（AdminHandler 内）== 1。测试文件：旧用例名零命中。`tpm_probe_charges_budget` 在两个 .py 内零命中；followups.md 三行移除。
- write_allowed 分支 :3469-3473 与 read_allowed :2366-2371 零 diff（review 确认）。

## Risks

- **helper 形态**：选 bool+副作用 send（`_send_json` :3669 返回 None、既有 9 处即 send+return 惯例，`return` 留在 do_GET 调用点）；response-tuple 形态需改 do_GET 返回链/控制流，风险更高，否决。
- **403 legs 依赖 LAN IP**：沙箱/离线机 skipTest（同 :4036 既有局限）；兜底=loopback 200 legs + 结构测试 + 既有 `test_read_allowed_matrix` 单测（:4097 docstring 引证）。
- **逐字节 body 断言**绑定 `json.dumps` 默认分隔符（`", "`/`": "`）——payload 任何改动即红，属预期守卫。
- **编辑已 commit 的 2026-10-01 spec**：仅删一句从未落地的声明（外科手术）；若 DELIVER 阶段判定不可改历史 spec，fallback=保留原句、弃用决策以本 spec+followups 行解决为准。
- /api/logs 200 leg 走无 cursor 默认 tail 分支；/api/errors 必须无 id——带 id 会误入 write_allowed 详情分支。

## Exclusions

- 不动 `read_allowed`/`write_allowed` 本体、do_POST 及其它任何生产逻辑；不收敛 do_POST 侧 write 门禁重复（如需另开 episode）。
- 不修 followups 其余行（queue-timeout flaky、calibrate 401/403 误判、快照-写入乱序）。
- 不补 `tpm_probe_charges_budget` 实现（选 a 弃用）；历史 plans/specs 中 persist_upstream 字样不改写（执行记录）。

## R31 Evidence

[R31-S1] 问题存在证据（仓库根实测回显）：

证据 1——do_GET 9 处读门禁 5 行块逐字复制（:3406-3507）：

```text
$ grep -n 'read_allowed' ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:2366:def read_allowed(peer_ip: str, token_header: str, token_env: str) -> bool:
ctyun-stream-fix-proxy.py:2371:    与 write_allowed 的差别：token_env 为空时 write_allowed 拒绝，read_allowed 放行。
ctyun-stream-fix-proxy.py:3406:            if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3413:            if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3420:            if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3427:            if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3434:            if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3446:            if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3453:            if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3486:                if not read_allowed(self.client_address[0],
ctyun-stream-fix-proxy.py:3507:            if not read_allowed(self.client_address[0],
```

证据 2——误导用例名现状（活代码仅 def 一处，无 CI/其它用例引用；.github/workflows 不存在）：

```text
$ grep -n 'test_persist_upstream_carries_tpm_model_budgets' ctyun-stream-fix-proxy.test.py
ctyun-stream-fix-proxy.test.py:1782:    def test_persist_upstream_carries_tpm_model_budgets(self) -> None:
```

证据 3——`tpm_probe_charges_budget` 生产文件零存在（纯 spec 声明，无 def 无 docstring）：

```text
$ grep -n 'tpm_probe_charges_budget' ctyun-stream-fix-proxy.py
（无输出，exit 1）
$ grep -rn 'tpm_probe_charges_budget' --include='*.py' .
（无输出，exit 1）
```

[R31-S2] 根因/误导陈述（file:line）：① test.py:1782 用例名引用的 `persist_upstream` 已随 E1 删除（2026-10-09-ocr-findings-fix-split-plan.md:14 记录删除 :1524 旧函数），名实不符误导维护者；② py:3406-3507 同一 5 行门禁块（条件+参数来源+403 文案完全相同）逐字复制 9 处，新增 GET 端点漏挂风险真实（followups.md:8）；③ 2026-10-01-tpm-auto-calibrate-design.md:30 声明"新增纯函数 `tpm_probe_charges_budget(model)`…返回 False"，实际从未实现、全仓零调用——声明性死条目（followups.md:4）。
