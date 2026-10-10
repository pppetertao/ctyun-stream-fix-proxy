# PLAN — Episode F4: followups 维护三件套（改名 / do_GET 读门禁收敛 / 声明性死条目处置）

- **Spec**: `docs/superpowers/specs/2026-10-10-maintenance-battery-design.md`（分支 commit 0973b23）
- **Worktree**: `.worktrees/maintenance-battery`（分支 `fix/maintenance-battery`）
- **Baseline**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 389 全绿（main = ea3fbbc）
- **执行者**: executor（4 卡全 tier B；卡内代码已写死，照抄转写 + 跑验证）
- **锚点实测**: 所有 file:line 均于 2026-10-10 在本 worktree 实测核对（spec 锚点零漂移）

## Global Constraints

1. **安全 gate 行为逐字节等价（最高优先）**：403 状态码、响应体 `{"error": "read requires X-Admin-Token"}` 逐字节不变。生成链：`_send_json`（py:3669，`json.dumps(payload, ensure_ascii=False)` 默认分隔符 `", "`/`": "`）→ `_send`（py:3661，`wfile.write(body)` 原样写、无尾换行）。门禁位置不变：每端点在敏感数据构造**之前**；`read_allowed` 三参数来源零变化（`self.client_address[0]` / `self.headers.get("X-Admin-Token") or ""` / `os.environ.get("CTYUN_ADMIN_TOKEN", "")`）。
2. **`_read_gate` helper 形态固定 = bool 返回 + 拒绝时 send 副作用**：`_send_json` 返回 `None`（py:3669 `-> None`），拒绝路径由 helper 内发送 403；`return` 留在 do_GET 调用点（`if not self._read_gate(): return`）。禁止 response-tuple 形态（spec Risks 已否决）。
3. **零改动区**：`read_allowed` 本体（py:2366-2371）、`write_allowed` 本体、`/api/errors` 详情分支（py:3462-3483，含 py:3469-3473 `write_allowed` 门禁与 `"detail requires X-Admin-Token"` 文案）、do_POST 全部、其余任何生产逻辑。
4. **改名只动 def 行**（test:1782）；test:1783-1785 三行注释保留（解释守卫来历，改名后仍准确）；旧名 `test_persist_upstream_carries_tpm_model_budgets` 在 test.py 零残留（活代码仅 def 一处，无引用，spec R31 证据 2）。
5. **文档外科手术**：2026-10-01 spec 只删 py:30 句尾声明分句（分号起至句号前），该行其余与文件其余零改动；followups.md 只删 :4/:7/:8 三行。
6. **TDD 序（强制）**：卡 1 矩阵 characterization 先写先跑 = **绿**（提取前行为守卫网）→ 卡 2 结构测试 = **red 留证**（literal 计数 9≠1）→ 卡 3 提取后转绿。卡 2 的 pin 测试 born-green（函数从未存在，无 red 可言，断言钉死弃用决策）。
7. **Edit 全文本锚点**：每卡给出完整 old/new 文本块；执行者照抄，遇文本不匹配（锚点漂移）→ STOP 报主代理，不得自行改写卡内代码。
8. **验证纪律**：一律 `/usr/bin/python3`（本机 python 不可用）；定向跑用 unittest.main argv 传名形态 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py <Class>[.<test>]`（模块名带连字符，**禁** `-m unittest` 点路径）；每卡只跑针对性验证不跑全量；全量放 whole-branch 收尾节。

---

## 卡 1 (tier B) — test.py 基建：admin_get host 参数 + 读门禁 characterization 矩阵 + 用例改名

**文件**: `ctyun-stream-fix-proxy.test.py`（3 处 Edit）

### E1: admin_get 加第四参 host（:649-650）

old:
```python
def admin_get(port: int, path: str, headers: dict = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
```
new:
```python
def admin_get(port: int, path: str, headers: dict = None, host: str = "127.0.0.1"):
    conn = http.client.HTTPConnection(host, port, timeout=10)
```
默认值 `"127.0.0.1"` 保持既有调用方零影响（admin_get 调用点遍布 test.py，均不传 host）。

### E2: 用例改名（:1782，仅 def 行；:1783-1785 注释不动）

old:
```python
    def test_persist_upstream_carries_tpm_model_budgets(self) -> None:
```
new:
```python
    def test_set_tpm_model_budgets_persists_full_schema(self) -> None:
```

### E3: 插入 characterization 矩阵测试（AdminIntegrationTest 内，:4093 之后、:4095 `def test_admin_get_read_allowed_no_token_env` 之前）

old:
```python
        # loopback 无 token → 200（admin_get 固定连 127.0.0.1，读门禁直通）
        status, _, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(status, 200)

    def test_admin_get_read_allowed_no_token_env(self) -> None:
```
new:
```python
        # loopback 无 token → 200（admin_get 固定连 127.0.0.1，读门禁直通）
        status, _, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(status, 200)

    def test_admin_read_gate_matrix_all_endpoints(self) -> None:
        """F4 characterization pin：9 个 GET 读端点 × 4 形态行为矩阵。提取 _read_gate
        前后均须绿（行为守卫网）；red 由 AdminReadGateStructureTest 承担。
        /api/errors 不带 id 参数——命中列表分支（read_allowed 门禁）；带 id 会误入
        write_allowed 详情分支（403 文案不同，不属本矩阵）。"""
        stop_proxy(self.proc)  # setUp 默认 127.0.0.1 绑定；本测需 0.0.0.0 + token，重建
        stop_fake_upstreams()
        upstream_port = make_fake_upstream(False)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={"CTYUN_ADMIN_HOST": "0.0.0.0",
                                           "CTYUN_ADMIN_TOKEN": "sekret"})
        # 本机候选地址枚举与过滤沿用 test_admin_get_read_gate_non_loopback（:4045-4069）：
        # ① 127.0.0.0/8；② 169.254.0.0/16；③ 198.18.0.0/15 VPN/TUN 黑洞段显式排除。
        try:
            candidates = list(socket.gethostbyname_ex(socket.gethostname())[2])
        except socket.gaierror:
            candidates = []
        candidates = [ip for ip in candidates
                      if not ip.startswith("127.")
                      and not ip.startswith("169.254.")
                      and not (ip.startswith("198.18.") or ip.startswith("198.19."))]
        lan_ip = None
        for ip in candidates:  # 优先取私网段第一个：192.168/16、10/8、172.16/12
            if (ip.startswith("192.168.") or ip.startswith("10.")
                    or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)):
                lan_ip = ip
                break
        if lan_ip is None:
            self.skipTest("no routable LAN IP in sandbox")
        forbidden = b'{"error": "read requires X-Admin-Token"}'
        endpoints = ["/api/stats", "/api/tpm_stats", "/api/tpm_settings",
                     "/api/tpm_calibrate", "/api/config", "/api/health",
                     "/api/probe_history", "/api/errors", "/api/logs"]
        for path in endpoints:
            with self.subTest(endpoint=path, leg="lan-no-token"):
                status, body, _ = admin_get(self.proc.admin_port, path, host=lan_ip)
                self.assertEqual(status, 403)
                self.assertEqual(body, forbidden)
            with self.subTest(endpoint=path, leg="lan-wrong-token"):
                status, body, _ = admin_get(self.proc.admin_port, path,
                                            headers={"X-Admin-Token": "wrong"},
                                            host=lan_ip)
                self.assertEqual(status, 403)
                self.assertEqual(body, forbidden)
            with self.subTest(endpoint=path, leg="lan-right-token"):
                status, _, _ = admin_get(self.proc.admin_port, path,
                                         headers={"X-Admin-Token": "sekret"},
                                         host=lan_ip)
                self.assertEqual(status, 200)
            with self.subTest(endpoint=path, leg="loopback-no-token"):
                status, _, _ = admin_get(self.proc.admin_port, path)
                self.assertEqual(status, 200)

    def test_admin_get_read_allowed_no_token_env(self) -> None:
```

注：候选过滤三行与既有 :4058-4061 逐字一致（`198.18.0.0/15` 覆盖 198.18.* 与 198.19.* 两段）。

### 卡 1 验证（针对性，不跑全量）

```bash
# 1) 改名后定向跑（旧名已不存在）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_set_tpm_model_budgets_persists_full_schema
# 期望: OK（1 test）
# 2) 矩阵测试定向跑（characterization 提取前必须绿；沙箱无 LAN IP → SKIP，不算失败）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_admin_read_gate_matrix_all_endpoints
# 期望: OK 或 SKIPPED ("no routable LAN IP in sandbox")
# 3) 旧名零残留
grep -c 'test_persist_upstream_carries_tpm_model_budgets' ctyun-stream-fix-proxy.test.py
# 期望: 0（grep exit 1）
```

**Gate**: 三条全过 → 卡 1 complete。矩阵测试此刻必须绿（这是提取前的行为基线，卡 3 依赖它守卫等价性）。

---

## 卡 2 (tier B) — 结构测试（red 留证）+ 弃用 pin 测试（born-green）

**文件**: `ctyun-stream-fix-proxy.test.py`（3 处 Edit）

### E1: 补 `import inspect`（顶部 import 区，:17 与 :18 之间；实测当前无 inspect 导入）

old:
```python
import importlib.util
import io
```
new:
```python
import importlib.util
import inspect
import io
```

### E2: 新增 AdminReadGateStructureTest（:5352 `class BodyErrorTest` 之前）

old:
```python
        self.assertIsNone(rec["probe"])


class BodyErrorTest(unittest.TestCase):
```
new:
```python
        self.assertIsNone(rec["probe"])


class AdminReadGateStructureTest(unittest.TestCase):
    """F4 结构测试：_read_gate 收敛后 do_GET 内 9 处门禁统一走 helper、
    read_allowed( 与 403 文案零内联。提炼前 do_GET 内 self._read_gate() 计数 0 != 9 → red；
    提炼后 9 处统一 helper、read_allowed(/403 文案零内联；hasattr 钉 helper 存在
    （TDD red 由本测试承担；行为等价由 AdminIntegrationTest 矩阵测试守卫）。"""

    def test_read_gate_helper_converges_nine_inline_blocks(self) -> None:
        mod = load_proxy_module()          # :637 既有基建
        src = inspect.getsource(mod.AdminHandler.do_GET)
        self.assertEqual(src.count("self._read_gate()"), 9,
                         "9 个读端点应统一走 _read_gate helper")
        self.assertEqual(src.count("read_allowed("), 0,
                         "do_GET 不应再内联 read_allowed")
        self.assertNotIn('"read requires X-Admin-Token"', src,
                         "403 文案应收敛到 helper 内")
        self.assertTrue(hasattr(mod.AdminHandler, "_read_gate"),
                        "AdminHandler 必须有 _read_gate helper")


class BodyErrorTest(unittest.TestCase):
```

**修正（首轮 STOP 后由主代理裁定）**：原 E2 块用 `inspect.getsource(mod.AdminHandler)`（class 级），在 `load_proxy_module()` 样板下恒抛 `TypeError: <class 'ctyun_stream_fix_proxy.AdminHandler'> is a built-in class`——`spec_from_file_location` + `exec_module` 不写 `sys.modules`，class 级 `getsource` 依赖 `sys.modules.get(obj.__module__)` 反查文件。函数级 `getsource` 走 `__code__.co_filename`，无此依赖（既有 `RelaySseV3Test.test_calibrate_probe_dead_guard_removed` :6496 即函数级先例，10 OK）。故断言口径改为取 `mod.AdminHandler.do_GET` 函数级，断言内容=do_GET 内联门禁块收敛；red 形态由 TypeError(error) 改为 `AssertionError: 0 != 9`(failure)，符合本卡 gate「red 由计数不等承担、非意外错误」。

**修正二（同坑复发，第四行改 hasattr）**：上一轮只把前三断言迁到函数级 `getsource`，第四行仍留 class 级 `inspect.getsource(mod.AdminHandler)`，红态下虽被前三断言短路、不实际触发，但一旦卡3 提炼完成即会命中同一 `TypeError`。第四行断言的语义只是「helper 存在」，不需要源码文本，故改用 `self.assertTrue(hasattr(mod.AdminHandler, "_read_gate"), ...)`——不依赖 `getsource`，也不读文件。

### E3: TpmCalibrateEngineTest（:8340）内加 pin 测试（setUpClass 之后、`def _scripted` 之前；复用 `cls.mod = load_proxy_module()`）

old:
```python
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_proxy_module()

    def _scripted(self, responses):
```
new:
```python
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_proxy_module()

    def test_tpm_probe_charges_budget_stays_disposed(self) -> None:
        """F4 弃用 pin：tpm_probe_charges_budget 从未实现（spec R31 证据 3），决策为
        弃用（2026-10-01 spec :30 声明句删除）。生而绿——无 red 可言，此断言钉死
        弃用决策，防僵尸半实现回归。"""
        self.assertFalse(hasattr(self.mod, "tpm_probe_charges_budget"))

    def _scripted(self, responses):
```

### 卡 2 验证（red 留证）

```bash
# 1) 结构测试此刻必须 FAIL（literal 计数 9≠1 —— TDD red 有据）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminReadGateStructureTest
# 期望: FAIL — AssertionError: 9 != 1（'"read requires X-Admin-Token"' 计数）；
#       且 'def _read_gate(self) -> bool:' not found。Exit code 1。此 red 是预期交付物，
#       原样贴 stdout 作为 red 证据，不算卡失败。
# 2) pin 测试 born-green
/usr/bin/python3 ctyun-stream-fix-proxy.test.py TpmCalibrateEngineTest.test_tpm_probe_charges_budget_stays_disposed
# 期望: OK（1 test）
```

**Gate**: 结构测试 FAIL（且失败原因恰为 literal 计数/缺 helper，而非 import 错误等意外）+ pin 测试 OK → 卡 2 complete。

---

## 卡 3 (tier B) — 生产提取：AdminHandler._read_gate + 9 处调用点退化（red→green）

**文件**: `ctyun-stream-fix-proxy.py`（10 处 Edit：1 插入 + 9 退化）

### E0: 插入 helper（py:3397 与 py:3399 之间）

old:
```python
class AdminHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
```
new:
```python
class AdminHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _read_gate(self) -> bool:
        """do_GET 读端点统一读门禁：read_allowed 通过返回 True；拒绝时已发送
        403（文案与原 9 处逐字一致）并返回 False。调用方：`if not self._read_gate(): return`。"""
        if read_allowed(self.client_address[0],
                        self.headers.get("X-Admin-Token") or "",
                        os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            return True
        self._send_json(403, {"error": "read requires X-Admin-Token"})
        return False

    def do_GET(self) -> None:
```

### E1-E8: 8 处顶层 elif 门禁退化（12 空格缩进；各 old 块含前导 elif 行保证唯一）

E1 `/api/stats`（py:3405-3411）— old:
```python
        elif path == "/api/stats":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, stats_snapshot())
```
new:
```python
        elif path == "/api/stats":
            if not self._read_gate():
                return
            self._send_json(200, stats_snapshot())
```

E2 `/api/tpm_stats`（py:3412-3418）— old:
```python
        elif path == "/api/tpm_stats":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, tpm_snapshot())
```
new:
```python
        elif path == "/api/tpm_stats":
            if not self._read_gate():
                return
            self._send_json(200, tpm_snapshot())
```

E3 `/api/tpm_settings`（py:3419-3425）— old:
```python
        elif path == "/api/tpm_settings":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, tpm_settings_snapshot())
```
new:
```python
        elif path == "/api/tpm_settings":
            if not self._read_gate():
                return
            self._send_json(200, tpm_settings_snapshot())
```

E4 `/api/tpm_calibrate`（py:3426-3432）— old:
```python
        elif path == "/api/tpm_calibrate":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, calibrate_state_snapshot())
```
new:
```python
        elif path == "/api/tpm_calibrate":
            if not self._read_gate():
                return
            self._send_json(200, calibrate_state_snapshot())
```

E5 `/api/config`（py:3433-3438 部分；后随 `with _CFG_LOCK:` 不动）— old:
```python
        elif path == "/api/config":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            with _CFG_LOCK:
```
new:
```python
        elif path == "/api/config":
            if not self._read_gate():
                return
            with _CFG_LOCK:
```

E6 `/api/health`（py:3445-3451）— old:
```python
        elif path == "/api/health":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, {"upstream": probe_state_snapshot()})
```
new:
```python
        elif path == "/api/health":
            if not self._read_gate():
                return
            self._send_json(200, {"upstream": probe_state_snapshot()})
```

E7 `/api/probe_history`（py:3452-3458）— old:
```python
        elif path == "/api/probe_history":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, probe_history_snapshot())
```
new:
```python
        elif path == "/api/probe_history":
            if not self._read_gate():
                return
            self._send_json(200, probe_history_snapshot())
```

E8 `/api/logs`（py:3506-3512 部分；后随 `qs = ...` 不动）— old:
```python
        elif path == "/api/logs":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
```
new:
```python
        elif path == "/api/logs":
            if not self._read_gate():
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
```

### E9: /api/errors 列表分支门禁退化（py:3484-3491，16 空格缩进；注释保留——解释本分支用读门禁、详情分支维持写门禁，提取后仍准确）

old:
```python
            else:
                # 列表（不含 body/response）：挂 read_allowed 读门禁（详情分支维持 write_allowed）
                if not read_allowed(self.client_address[0],
                                    self.headers.get("X-Admin-Token") or "",
                                    os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                    self._send_json(403, {"error": "read requires X-Admin-Token"})
                    return
                with ERROR_LOCK:
```
new:
```python
            else:
                # 列表（不含 body/response）：挂 read_allowed 读门禁（详情分支维持 write_allowed）
                if not self._read_gate():
                    return
                with ERROR_LOCK:
```

### 卡 3 验证（red→green + 零改动区核对）

```bash
# 1) 结构测试转绿
/usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminReadGateStructureTest
# 期望: OK（1 test）。Exit code 0。
# 2) 行为等价守卫：矩阵（卡 1）+ 既有 0.0.0.0 门禁测试 + 既有 read_allowed 单测矩阵
/usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_admin_read_gate_matrix_all_endpoints AdminIntegrationTest.test_admin_get_read_gate_non_loopback ProxyDashboardUnitTest.test_read_allowed_matrix
# 期望: OK（3 tests；前两者沙箱无 LAN IP 时 SKIPPED，不算失败）
# 3) grep 复核（生产文件）
grep -c 'self._read_gate()' ctyun-stream-fix-proxy.py
# 期望: 10（9 调用点 + helper docstring 内 1 处示例）
grep -c '"read requires X-Admin-Token"' ctyun-stream-fix-proxy.py
# 期望: 1（仅 helper）
grep -c 'read_allowed(' ctyun-stream-fix-proxy.py
# 期望: 2（:2366 def + helper 内 1 处调用）
# 4) 零改动区负验证（-U0 下未改动行不出现）
git diff -U0 -- ctyun-stream-fix-proxy.py | grep -c '^[+-].*def read_allowed'
# 期望: 0
git diff -U0 -- ctyun-stream-fix-proxy.py | grep -c '^[+-].*write_allowed'
# 期望: 0
git diff -U0 -- ctyun-stream-fix-proxy.py | grep -c '^[+-].*detail requires'
# 期望: 0
```

**Gate**: 结构测试 OK + 行为守卫全绿/SKIP + grep 四组全命中期望 + 零改动区三负验证 = 0 → 卡 3 complete。

---

## 卡 4 (tier B) — 声明性死条目文档收尾（2026-10-01 spec 删句 + followups 删 3 行）

**文件**: `docs/superpowers/specs/2026-10-01-tpm-auto-calibrate-design.md`（1 处 Edit）+ `docs/superpowers/followups.md`（3 行删除）

### E1: 2026-10-01 spec :30 删句尾声明分句（该行其余、文件其余零改动）

old:
```markdown
- `ctyun-stream-fix-proxy.py:516` `tpm_admit(key_id, est, model)` —— 不改签名。校准请求**不**调用 `tpm_admit`（见 Risk 2 的校准豁免论证）；新增纯函数 `tpm_probe_charges_budget(model)` 仅用于文档/断言目的，返回 `False`。
```
new:
```markdown
- `ctyun-stream-fix-proxy.py:516` `tpm_admit(key_id, est, model)` —— 不改签名。校准请求**不**调用 `tpm_admit`（见 Risk 2 的校准豁免论证）。
```

### E2: followups.md 删 :4/:7/:8 三行（整行删除，其余行不动；删除后无空行残留）

删：
- `:4` `ctyun-stream-fix-proxy.py | spec Files 的 tpm_probe_charges_budget 纯文档函数未实现（零影响，声明性条目） | 确认弃用或补一个断言用函数`
- `:7` `ctyun-stream-fix-proxy.test.py:1634 用例名引用已删函数（E1 OCR #6） | test_persist_upstream_carries_tpm_model_budgets 名中的 persist_upstream 已随 E1 删除，误导维护者 | 顺手改名为 test_set_tpm_model_budgets_persists_full_schema（低价值维护项，不阻塞）`
- `:8` `ctyun-stream-fix-proxy.py:3357-3458 do_GET 读门禁 5 行块逐字复制 9 处（E3 OCR #3） | 纯 maintainability：新增 GET 端点时漏挂风险真实 | 独立 episode 提 AdminHandler._read_gate() helper 收敛 9 处（行为等价零风险，但交付前不宜动安全 gate 位）`

### 卡 4 验证

```bash
# 1) 生产文件零命中
grep -c 'tpm_probe_charges_budget' ctyun-stream-fix-proxy.py
# 期望: 0（grep exit 1，无输出）
# 2) 测试文件恰 1 命中（仅卡 2 pin 断言一行）
grep -c 'tpm_probe_charges_budget' ctyun-stream-fix-proxy.test.py
# 期望: 1
# 3) 两个文档零命中
grep -rc 'tpm_probe_charges_budget' docs/superpowers/specs/2026-10-01-tpm-auto-calibrate-design.md docs/superpowers/followups.md
# 期望: 两文件均 0
# 4) followups 已解决三关键词零命中（改名行与门禁行确认移除）
grep -c 'test_persist_upstream_carries_tpm_model_budgets' docs/superpowers/followups.md; grep -c '_read_gate()' docs/superpowers/followups.md
# 期望: 均为 0
# 5) pin 测试复跑（钉死弃用决策）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py TpmCalibrateEngineTest.test_tpm_probe_charges_budget_stays_disposed
# 期望: OK（1 test）
```

**注（spec 验收措辞澄清）**：spec Acceptance 写"`tpm_probe_charges_budget` 在两个 .py 内零命中"，但卡 2 pin 断言本身含该字符串（`hasattr(self.mod, "tpm_probe_charges_budget")`）——两句不可同时成立。唯一自洽解读：**生产 .py 零命中 + test.py 恰 1 命中（pin 断言行）**，本卡验证按此执行。

**Gate**: 5 条验证全过 → 卡 4 complete。

---

## Whole-branch 收尾验证（最后一卡完成后、task review 前）

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
# 期望: Ran 392 tests（389 既有 + 3 新增：矩阵/结构/pin 各 1）— OK。
#       沙箱无 LAN IP 时矩阵测试 SKIPPED：显示 OK (skipped=1)，不算失败。
git status --short
# 期望: 仅本 episode 4 文件改动（2 .py + 2 .md），无临时文件残留。
```

全绿后由主代理走 task review → whole-branch review（merge-base..HEAD diff，重点核 Global Constraints 1/3 零改动区）。

---

## Plan Self-Review（5 项）

1. **Spec coverage**：spec Files 1-4 全覆盖——py helper+9 退化（卡 3 E0-E9）、test 改名（卡 1 E2）/admin_get host（卡 1 E1）/矩阵（卡 1 E3）/结构（卡 2 E2）/pin（卡 2 E3）/inspect import（卡 2 E1）、2026-10-01 spec :30 删句（卡 4 E1）、followups :4/:7/:8（卡 4 E2）。Acceptance 4 条全部映射到卡内验证（全绿→收尾节；提取前绿+red 有据→卡 1/卡 2 gate；grep 复核→卡 3/卡 4；零 diff→卡 3 负验证）。
2. **Placeholder scan**：全部卡内代码为可逐字落盘的完整文本（无 TODO/省略号/"类似地"指令）；9 处退化块逐一给出 old/new 全文。
3. **Type consistency**：`_read_gate() -> bool` 与调用点 `if not self._read_gate(): return` 匹配；`admin_get(port, path, headers=None, host="127.0.0.1")` 与既有 3 元组返回 `(status, body, content_type)` 解包一致（矩阵测试用 `status, body, _`/`status, _, _`）；结构测试断言的 literal 计数与卡 3 实际产出一致（`self._read_gate()` 计 10≥9，docstring 内 "read_allowed 通过" 无括号不误计数）。
4. **可落盘性**：所有 old 锚点均于本 worktree 实测读取（py:3396-3520、py:3661-3672、test:637-658、test:1782-1785、test:4036-4097、test:5344-5352、test:8340-8348、spec:30、followups:1-8），文本逐字取自现场；卡 1 E3 候选过滤三行与既有 :4058-4061 逐字一致。
5. **锚点实测**：spec 三项 R31 证据（9 处门禁行号、:1782 唯一命中、tpm_probe_charges_budget 零存在）在本 worktree 复测全部吻合，行号零漂移；`inspect` 确认未导入（grep rc=1）；`unittest.main(verbosity=2)` 确认 argv 传名可用。
