# PLAN — E3 管理面 GET 读门禁（read_allowed）+ 启动暴露警告 + dashboard JS token 引导

- **spec**: `docs/superpowers/specs/2026-10-09-admin-read-auth-design.md`（commit 1a3b37a）
- **分支/worktree**: `fix/admin-read-auth` @ `.worktrees/admin-read-auth`（基于 main E1 dd8ee63 之后）
- **日期**: 2026-10-09
- **执行者**: executor（3 卡全 tier B——代码已在卡内写死，无设计决策）
- **baseline**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` = 377 测试全绿（E1 后）
- **卡数**: 3（串行执行，每卡 TDD：先失败测试实跑红 → 再实现实跑绿）

## Global Constraints

1. **不改 ADMIN_HOST 默认**：`ctyun-stream-fix-proxy.py:37` `ADMIN_HOST = os.environ.get("CTYUN_ADMIN_HOST", "0.0.0.0")` 一字不动（用户硬约束：局域网可访问）。
2. **写端点语义零变化**：`write_allowed`（:2319-2323）不动；4 处 POST 门禁（:3442 / :3495 / :3516 / :3538）不动；JS 内 4 处 POST fetch（:4643 / :4821 / :5424 / :5481）不动。
3. **GET 门禁只加不减**：loopback 行为不变（直通）；未配 token 非 loopback 放行 + 启动警告。`/`（:3332）、`/favicon.ico`（:3334）、`/api/errors?id=` 详情（:3365 既有 write_allowed）不门禁。
4. **Python 一律 `/usr/bin/python3`**（本机 `python` 不可用）。每卡验证用针对性测试（`/usr/bin/python3 ctyun-stream-fix-proxy.test.py <Class>.<method>`），不跑全量；whole-branch 全量见文末「Whole-branch 收尾」。
5. **Edit 全部用文本锚点**（本文各行号均为 2026-10-09 在分支 HEAD 实测，E1 后最新；行号仅定位参考，Edit 以唯一文本子串为准）。
6. **风格**：JS 走既有 ES5 模式（`var`/`function`/`try{...}catch(e){}` localStorage 降级、`history.replaceState` 特性检测，参照 ：4304-4343 tab 记忆段）；空 catch 必须自证注释；do_GET 分支体缩进 12 空格、门禁体 16 空格；不引新依赖（`hmac` 已在用）。
7. **测试计数**：完成后预期 377 + 5 = **382** 全绿（新增 5 个测试方法）。VERIFY 只对「baseline 绿 → 现红」负责。
8. **scope 裁定**：spec Exclusions 3（不改 DASHBOARD_HTML JS）被主代理裁定覆盖——dashboard JS token 引导纳入本期（Card 3），其余 Exclusions 照旧。

---

## Card 1 — `read_allowed` 纯函数 + 矩阵单测（tier B）

**Files**: `ctyun-stream-fix-proxy.py`（新增函数）、`ctyun-stream-fix-proxy.test.py`（新增 1 方法）

### 1a. 失败测试（先写，实跑红）

**Edit 锚点**（`ctyun-stream-fix-proxy.test.py`，`test_write_allowed_matrix` 结尾与 `test_extract_model` 之间，:773 附近；锚点文本实测唯一）：

old:
```python
        self.assertTrue(f("192.168.1.5", "tok", "tok"))
        self.assertFalse(f("192.168.1.5", "wrong", "tok"))

    def test_extract_model(self) -> None:
```

new:
```python
        self.assertTrue(f("192.168.1.5", "tok", "tok"))
        self.assertFalse(f("192.168.1.5", "wrong", "tok"))

    def test_read_allowed_matrix(self) -> None:
        """E3 读门禁矩阵：loopback 无条件放行；未配 token 非 loopback 放行（与
        write_allowed 的关键差异，write_allowed 同参 False）；已配 token 按
        X-Admin-Token 精确匹配。"""
        f = self.mod.read_allowed
        # loopback 直通（IPv4/IPv6）
        self.assertTrue(f("127.0.0.1", "", ""))
        self.assertTrue(f("::1", "", ""))
        # 未配 token：非 loopback 放行（E3 关键差异）
        self.assertTrue(f("192.168.1.5", "", ""))
        self.assertTrue(f("192.168.1.5", "tok", ""))
        self.assertTrue(f("192.168.1.5", "wrong", ""))
        # 已配 token：精确匹配判定
        self.assertTrue(f("192.168.1.5", "tok", "tok"))
        self.assertFalse(f("192.168.1.5", "wrong", "tok"))

    def test_extract_model(self) -> None:
```

**红证命令**（`read_allowed` 尚不存在 → AttributeError 红）：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_read_allowed_matrix
```
预期 exit 1，报 `AttributeError: ... has no attribute 'read_allowed'`。

### 1b. 实现（实跑绿）

**Edit 锚点**（`ctyun-stream-fix-proxy.py`，`write_allowed` 函数体后、`set_upstream_base` 前，:2323-2325；锚点文本实测唯一）：

old:
```python
def write_allowed(peer_ip: str, token_header: str, token_env: str) -> bool:
    if peer_ip in ("127.0.0.1", "::1"):
        return True
    if not token_env:
        return False
    return hmac.compare_digest(token_header.encode("utf-8"), token_env.encode("utf-8"))


def set_upstream_base(base: str) -> None:
```

new（spec 原文逐字）:
```python
def write_allowed(peer_ip: str, token_header: str, token_env: str) -> bool:
    if peer_ip in ("127.0.0.1", "::1"):
        return True
    if not token_env:
        return False
    return hmac.compare_digest(token_header.encode("utf-8"), token_env.encode("utf-8"))


def read_allowed(peer_ip: str, token_header: str, token_env: str) -> bool:
    """非 loopback 来源的 GET 读门禁。

    语义：loopback 无条件放行；非 loopback 在 CTYUN_ADMIN_TOKEN 已配置时要求
    X-Admin-Token 头匹配，未配置时保持放行（可选加固——启动日志会显式警告暴露面）。
    与 write_allowed 的差别：token_env 为空时 write_allowed 拒绝，read_allowed 放行。
    """
    if peer_ip in ("127.0.0.1", "::1"):
        return True
    if not token_env:  # 未配 token：保持 LAN 明文读（可选加固），启动日志已警告
        return True
    return hmac.compare_digest(token_header.encode("utf-8"), token_env.encode("utf-8"))


def set_upstream_base(base: str) -> None:
```

**绿证命令**：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest
```
预期 exit 0（该纯函数类全绿，含新矩阵用例 + 既有用例无回归）。

### 1c. 验收

- `test_read_allowed_matrix` 覆盖 spec AC1 七行矩阵（loopback×2 / 无 token 非 loopback×3 / 匹配×2）。
- `write_allowed` 矩阵用例（:765）不受影响（同类回归）。

---

## Card 2 — do_GET 九端点读门禁 + 启动暴露警告 + 3 个集成测试（tier B）

**Files**: `ctyun-stream-fix-proxy.py`（do_GET :3330-3420、main 段 :5613 后）、`ctyun-stream-fix-proxy.test.py`（AdminIntegrationTest :3499 内新增 3 方法）

### 2a. 失败测试（先写，实跑红）

**Edit 锚点**（`ctyun-stream-fix-proxy.test.py`，`test_config_post_localhost_allowed_even_with_token_env` 结尾与 `test_daily_bucket_via_sse_and_persist` 之间，:3890 附近；实测 `AdminIntegrationTest` 类起于 ：3499，spec 写 ：3890 区域——以文本锚点为准，行漂移吸收）：

old:
```python
        self.assertEqual(status, 200)

    def test_daily_bucket_via_sse_and_persist(self) -> None:
```

new（3 个新方法整体插入；复用 `start_proxy` :490（extra_env 覆盖 CTYUN_ADMIN_HOST）、`admin_get` :649、`stderr_text` :420、`make_fake_upstream` :278；`lan_ip` 探测用 `gethostbyname_ex` 枚举 + 三段过滤 + 私网优先（环境稳健版，见偏差记录 5 与测试内注释）；无私网候选 → `self.skipTest`（spec 指定 SkipTest 口径，防御沙箱/离线机/VPN 环境））：

```python
        self.assertEqual(status, 200)

    def test_admin_get_read_gate_non_loopback(self) -> None:
        """E3：CTYUN_ADMIN_TOKEN 已配时，非 loopback GET 读端点 无/错 token → 403、
        对 token → 200（无/带/错三形态）；loopback 无 token → 200（读门禁直通）。"""
        stop_proxy(self.proc)  # setUp 默认 127.0.0.1 绑定；本测需 0.0.0.0 + token，重建
        stop_fake_upstreams()
        upstream_port = make_fake_upstream(False)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={"CTYUN_ADMIN_HOST": "0.0.0.0",
                                           "CTYUN_ADMIN_TOKEN": "sekret"})
        # 本机候选地址枚举（gethostbyname_ex 不发包、不依赖默认路由——弃用 UDP
        # connect 探测：VPN 全流量代理会把默认路由压在 TUN 段，取到的是黑洞地址），
        # 按序过滤丢弃：
        #   ① 127.0.0.0/8 loopback；② 169.254.0.0/16 链路本地；
        #   ③ 198.18.0.0/15 —— VPN/TUN 默认路由基准段（RFC 2544 benchmarking 段），
        #     对 0.0.0.0 监听是 TCP 黑洞：SYN 进 TUN 有去无回、只超时不拒绝，
        #     连上它本用例恒红且无法 SkipTest（本卡执行期实测坑），必须显式排除。
        try:
            candidates = list(socket.gethostbyname_ex(socket.gethostname())[2])
        except socket.gaierror:
            # 吞 socket.gaierror（沙箱/离线机本机主机名不可解析）：枚举失败等价于
            # 无候选、走末尾 skipTest；单测环境内无其他主机名解析路径可达。
            candidates = []
        candidates = [ip for ip in candidates
                      if not ip.startswith("127.")
                      and not ip.startswith("169.254.")
                      and not (ip.startswith("198.18.") or ip.startswith("198.19."))]
        lan_ip = None
        for ip in candidates:  # 优先取私网段第一个（按枚举序）：192.168/16、10/8、172.16/12
            if (ip.startswith("192.168.") or ip.startswith("10.")
                    or (ip.startswith("172.") and 16 <= int(ip.split(".")[1]) <= 31)):
                lan_ip = ip
                break
        if lan_ip is None:
            self.skipTest("no routable LAN IP in sandbox")
        # 无 token → 403
        conn = http.client.HTTPConnection(lan_ip, self.proc.admin_port, timeout=10)
        conn.request("GET", "/api/stats")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        resp.read()
        conn.close()
        # 错 token → 403
        conn = http.client.HTTPConnection(lan_ip, self.proc.admin_port, timeout=10)
        conn.request("GET", "/api/stats", headers={"X-Admin-Token": "wrong"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 403)
        resp.read()
        conn.close()
        # 对 token → 200
        conn = http.client.HTTPConnection(lan_ip, self.proc.admin_port, timeout=10)
        conn.request("GET", "/api/stats", headers={"X-Admin-Token": "sekret"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        resp.read()
        conn.close()
        # loopback 无 token → 200（admin_get 固定连 127.0.0.1，读门禁直通）
        status, _, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(status, 200)

    def test_admin_get_read_allowed_no_token_env(self) -> None:
        """E3：未配 CTYUN_ADMIN_TOKEN 时读端点保持开放（loopback 200，不受门禁影响）。
        非 loopback 行为不在此测（依赖网络拓扑），由 test_read_allowed_matrix 兜底。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        upstream_port = make_fake_upstream(False)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={"CTYUN_ADMIN_HOST": "0.0.0.0"})
        status, _, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(status, 200)
        status, _, _ = admin_get(self.proc.admin_port, "/api/health")
        self.assertEqual(status, 200)

    def test_startup_log_warns_exposure(self) -> None:
        """E3：ADMIN_HOST 非 loopback 且未配 token → 启动 stderr 含 WARNING +
        CTYUN_ADMIN_TOKEN 指引；loopback 绑定 → 不含 WARNING。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        upstream_port = make_fake_upstream(False)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={"CTYUN_ADMIN_HOST": "0.0.0.0"})
        # stderr 异步 drain：轮询等 WARNING 行（同 test_listen_host_env_binds_lan while/else 模式）
        deadline = time.time() + 5
        while time.time() < deadline:
            if "WARNING" in stderr_text(self.proc):
                break
            time.sleep(0.05)
        else:
            self.fail("0.0.0.0 绑定未配 token，stderr 未见暴露面 WARNING: %s"
                      % stderr_text(self.proc))
        self.assertIn("CTYUN_ADMIN_TOKEN", stderr_text(self.proc))
        # 反例：loopback 绑定不告警（start_proxy 默认钉死 CTYUN_ADMIN_HOST=127.0.0.1）
        stop_proxy(self.proc)
        stop_fake_upstreams()
        self.proc = start_proxy(self.upstream_port, free_port())
        self.assertNotIn("WARNING", stderr_text(self.proc))

    def test_daily_bucket_via_sse_and_persist(self) -> None:
```

**红证命令**（逐个跑；门禁缺失 → 403 断言拿到 200 红；WARNING 缺失 → fail 红）：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_admin_get_read_gate_non_loopback
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_startup_log_warns_exposure
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest.test_admin_get_read_allowed_no_token_env
```
预期：前两条 exit 1（红）；第三条**设计即绿**（无 token 配置行为不变的回归护栏，非缺口证明者，缺口由前两条证明）。注：`test_admin_get_read_gate_non_loopback` 在无 LAN IP 沙箱会 SkipTest（非红）——红证依赖联网环境；纯函数矩阵（Card 1）为逻辑兜底。

### 2b. 实现（实跑绿）

**do_GET 门禁**（`ctyun-stream-fix-proxy.py`，9 处插入；每处 old_string 均实测唯一）。门禁块模板（4 行，缩进 12/16）：

```python
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
```

**(1) /api/stats**（:3336-3337）old:
```python
        elif path == "/api/stats":
            self._send_json(200, stats_snapshot())
```
new:
```python
        elif path == "/api/stats":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, stats_snapshot())
```

**(2) /api/tpm_stats**（:3338-3339）old:
```python
        elif path == "/api/tpm_stats":
            self._send_json(200, tpm_snapshot())
```
new:
```python
        elif path == "/api/tpm_stats":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, tpm_snapshot())
```

**(3) /api/tpm_settings**（:3340-3341）old:
```python
        elif path == "/api/tpm_settings":
            self._send_json(200, tpm_settings_snapshot())
```
new:
```python
        elif path == "/api/tpm_settings":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, tpm_settings_snapshot())
```

**(4) /api/tpm_calibrate**（:3342-3343）old:
```python
        elif path == "/api/tpm_calibrate":
            self._send_json(200, calibrate_state_snapshot())
```
new:
```python
        elif path == "/api/tpm_calibrate":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, calibrate_state_snapshot())
```

**(5) /api/config**（:3344，门禁插在 `with _CFG_LOCK:` 前）old:
```python
        elif path == "/api/config":
            with _CFG_LOCK:
```
new:
```python
        elif path == "/api/config":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            with _CFG_LOCK:
```
（do_GET 内此形态唯一——POST /api/config 在 do_POST，锚点不同。）

**(6) /api/health**（:3351-3352）old:
```python
        elif path == "/api/health":
            self._send_json(200, {"upstream": probe_state_snapshot()})
```
new:
```python
        elif path == "/api/health":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, {"upstream": probe_state_snapshot()})
```

**(7) /api/probe_history**（:3353-3354）old:
```python
        elif path == "/api/probe_history":
            self._send_json(200, probe_history_snapshot())
```
new:
```python
        elif path == "/api/probe_history":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            self._send_json(200, probe_history_snapshot())
```

**(8) /api/errors 列表分支**（:3380-3382，插在 else 体内 `with ERROR_LOCK:` 前；同时更新 ：3381 过时注释「无鉴权」）old:
```python
            else:
                # 列表（不含 body/response），无鉴权
                with ERROR_LOCK:
```
new:
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

**(9) /api/logs**（:3397-3398，门禁插在分支顶、参数解析前）old:
```python
        elif path == "/api/logs":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
```
new:
```python
        elif path == "/api/logs":
            if not read_allowed(self.client_address[0],
                                self.headers.get("X-Admin-Token") or "",
                                os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                self._send_json(403, {"error": "read requires X-Admin-Token"})
                return
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
```

**启动暴露警告**（:5612-5613 listening 行后、:5614 `try:` 前；实测锚点唯一）old:
```python
    _safe_log_stderr("ctyun-stream-fix-proxy listening on %s:%d -> %s (admin dashboard on %s:%d)"
                     % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE, ADMIN_HOST, ADMIN_PORT))
    try:
```
new:
```python
    _safe_log_stderr("ctyun-stream-fix-proxy listening on %s:%d -> %s (admin dashboard on %s:%d)"
                     % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE, ADMIN_HOST, ADMIN_PORT))
    if ADMIN_HOST not in ("127.0.0.1", "::1") and not os.environ.get("CTYUN_ADMIN_TOKEN"):
        _safe_log_stderr("ctyun-stream-fix-proxy: WARNING — admin dashboard bound to %s:%d "
                         "without CTYUN_ADMIN_TOKEN; LAN read endpoints (stats/logs/errors "
                         "list/config/health/calibrate states) are served without "
                         "authentication. Set CTYUN_ADMIN_TOKEN to require X-Admin-Token "
                         "header on non-loopback reads." % (ADMIN_HOST, ADMIN_PORT))
    try:
```

**绿证命令**：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py AdminIntegrationTest
```
预期 exit 0（3 个新用例 + 类内既有 admin 用例全绿——含 AC8 回归护栏 `test_config_post_localhost_allowed_even_with_token_env` :3878、`test_dashboard_and_stats_served` 等：既有用例全走 127.0.0.1，loopback 直通不受门禁影响）。

### 2c. 验收

- AC2/AC3/AC4 → `test_admin_get_read_gate_non_loopback`；AC5 → `test_admin_get_read_allowed_no_token_env`；AC6/AC7 → `test_startup_log_warns_exposure`。
- `/`、`/favicon.ico`、`/api/errors?id=`（write_allowed）、4 处 POST、404 分支零改动（Edit 未触及其文本）。

---

## Card 3 — DASHBOARD_HTML JS token 引导 + adminFetch 包裹 7 个 GET fetch（tier B）

**Files**: `ctyun-stream-fix-proxy.py`（`_DASH_JS_CORE` 字符串常量 ：3968-5491 内）、`ctyun-stream-fix-proxy.test.py`（ProxyDashboardUnitTest 新增 1 方法）

**背景（主代理 scope 裁定，覆盖 spec Exclusions 3）**：token 一旦配置，LAN 浏览器 dashboard 的 7 个 GET 轮询（:4587 / :4602 / :4676 / :5267 / :5302 / :5336 / :5448）会 403 → 修复不可用。最小可用版：URL `?token=` 引导进 localStorage，此后所有 admin GET fetch 自动带 `X-Admin-Token`；未引导过 token 时不带头部（现状可用）。

### 3a. 失败测试（先写，实跑红）

**Edit 锚点**（`ctyun-stream-fix-proxy.test.py`，Card 1 插入的 `test_read_allowed_matrix` 结尾与 `test_extract_model` 之间；锚点文本唯一）：

old:
```python
        self.assertTrue(f("192.168.1.5", "tok", "tok"))
        self.assertFalse(f("192.168.1.5", "wrong", "tok"))

    def test_extract_model(self) -> None:
```

new:
```python
        self.assertTrue(f("192.168.1.5", "tok", "tok"))
        self.assertFalse(f("192.168.1.5", "wrong", "tok"))

    def test_dashboard_js_token_bootstrap(self) -> None:
        """E3 dashboard JS token 引导（主代理裁定纳入本期）：页面含 ?token= →
        localStorage 持久化引导与 adminFetch 包裹；7 个 GET 端点全部经 adminFetch
        发出（服务端配置 token 后 LAN dashboard 轮询不失能）。JS 运行时行为无法在
        python unittest 内执行，按既有 dashboard 测试惯例做 DASHBOARD_HTML 子串断言；
        服务端 403/200 行为由 Card 2 集成测试锁定。"""
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        # 引导与包裹函数落地
        self.assertIn('localStorage.setItem("ctyun-admin-token"', html)
        self.assertIn('function adminFetch(url, opts)', html)
        self.assertIn('"X-Admin-Token"', html)
        # 7 个 GET 端点全部经 adminFetch 发出
        for ep in ("/api/health", "/api/stats", "/api/tpm_settings", "/api/tpm_stats",
                   "/api/errors", "/api/probe_history", "/api/tpm_calibrate"):
            self.assertIn('adminFetch("%s"' % ep, html)
        # 旧裸 fetch 形态零残留（tpm_calibrate 以 `")` 收尾，与同端点 POST 形态 `", {` 区分）
        for stale in ('fetch("/api/health")',
                      'fetch("/api/stats", { signal: ctrl.signal })',
                      'fetch("/api/tpm_settings")',
                      'fetch("/api/tpm_stats")',
                      'fetch("/api/errors")',
                      'fetch("/api/probe_history")',
                      'fetch("/api/tpm_calibrate")'):
            self.assertNotIn(stale, html)

    def test_extract_model(self) -> None:
```

**红证命令**：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_dashboard_js_token_bootstrap
```
预期 exit 1（首个 assertIn 未命中——adminFetch/localStorage 引导尚不存在；既有 HTML 无 `ctyun-admin-token`、无 `adminFetch`，已 grep 核实无假阳性）。

### 3b. 实现（实跑绿）

**(A) 插入引导块 + adminFetch**（`_DASH_JS_CORE`，:3979-3980 `var lastSnap` 行后；ES5、沿用 ：4304-4343 tab 记忆段的 localStorage try/catch 与 `history.replaceState` 特性检测既有模式；字符串常量内无 `"""`/`\` 冲突，已核）old:
```python
var selectedRange = "7d";  // 刷新不记忆（无 localStorage），默认近7天
var lastSnap = null;       // poll 缓存：tab 点击零请求重渲染
```
new:
```python
var selectedRange = "7d";  // 刷新不记忆（无 localStorage），默认近7天
var lastSnap = null;       // poll 缓存：tab 点击零请求重渲染

// ===== E3 读门禁 token 引导：URL ?token= 一次性写入 localStorage，此后所有 admin GET
// fetch 经 adminFetch 自动带 X-Admin-Token；未引导过 token 时保持裸 fetch——服务端未配
// CTYUN_ADMIN_TOKEN 时 LAN 可读不受影响（写端点行为与此前一致，不由本段改动）。
function adminToken() {
  try { return localStorage.getItem("ctyun-admin-token") || ""; }
  catch (e) { return ""; }  // SecurityError：localStorage 读被禁（隐私模式），按无 token 降级
}
try {
  var urlToken = new URL(window.location.href).searchParams.get("token");
  if (urlToken) {
    localStorage.setItem("ctyun-admin-token", urlToken);
    if (history.replaceState) history.replaceState(null, "",
        window.location.pathname + window.location.hash);  // 地址栏抹掉 token，防误存误转发（hash 保留防丢 tab 态）
  }
} catch (e) { /* 单语句引导块：URL/localStorage 不可用时跳过引导，按无 token 裸请求 */ }
function adminFetch(url, opts) {
  var tok = adminToken();
  var o = opts || {};
  if (tok) {
    var h = {};
    for (var k in (o.headers || {})) h[k] = o.headers[k];
    h["X-Admin-Token"] = tok;
    o.headers = h;
  }
  return fetch(url, o);
}
```

**(B)-(H) 包裹 7 个 GET fetch**（每处 old_string 实测唯一；POST 形态 `fetch("/api/tpm_calibrate", {` 等不含收尾 `")`，不受影响）：

| # | old（唯一） | new |
|---|------------|-----|
| B :4587 | `  fetch("/api/health")` | `  adminFetch("/api/health")` |
| C :4602 | `  fetch("/api/stats", { signal: ctrl.signal })` | `  adminFetch("/api/stats", { signal: ctrl.signal })` |
| D :4676 | `  fetch("/api/tpm_settings")` | `  adminFetch("/api/tpm_settings")` |
| E :5267 | `  fetch("/api/tpm_stats")` | `  adminFetch("/api/tpm_stats")` |
| F :5302 | `  fetch("/api/errors")` | `  adminFetch("/api/errors")` |
| G :5336 | `  fetch("/api/probe_history")` | `  adminFetch("/api/probe_history")` |
| H :5448 | `  fetch("/api/tpm_calibrate")` | `  adminFetch("/api/tpm_calibrate")` |

**绿证命令**：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest
```
预期 exit 0（含 3a 新用例 + `test_dashboard_and_stats_served`/`test_dashboard_tpm_two_choice_and_calibrate_ui` 对应的 HTML 断言不受影响——本卡不动布局/容器/文案，且不引入 innerHTML，XSS 门禁保持）。

### 3c. 验收

- 7 个 GET fetch 全走 `adminFetch`；token 未引导时不改任何请求头（`adminFetch` 直通 `fetch`）。
- POST fetch 4 处（:4643 / :4821 / :5424 / :5481）与布局/CSS 零改动。
- AC9（全量绿）归入下方 Whole-branch 收尾，不计入卡内验证。

---

## Whole-branch 收尾（非卡内验证）

全部卡完成后由最后一卡执行者同次 dispatch 或主代理单独跑（AGENTS.md VERIFY 铁律）：

```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```
预期 382 tests 全绿（377 baseline + 5 新增），exit 0。

## Spec 偏差记录

1. **JS token 引导纳入本期**（Card 3）：主代理裁定覆盖 spec Exclusions 3 与 Risk 1 的「E3b follow-up」处理；spec Risk 1 描述的 403 降级由本卡消除。
2. **`test_admin_get_read_gate_non_loopback` 增加错 token → 403 形态**：spec 测试 (1) 为 3 断言（无/带/loopback），主代理裁定要求 无/带/错三形态——本卡为超集。
3. **行号漂移**：spec 写 AdminIntegrationTest 新增方法「约 ：3890 区域」，实测类定义在 ：3499、插入锚点在 ：3890（`test_config_post_localhost_allowed_even_with_token_env` 之后）——语义一致，文本锚点吸收，不回 PLAN。
4. 其余 spec 锚点全部实测命中：`write_allowed` :2319、do_GET :3330-3420 九端点、POST 门禁 ：3442/:3495/:3516/:3538、listening :5612-5613、`stderr_text` :420、`start_proxy` :490、`admin_get` :649、`test_write_allowed_matrix` :765、JS fetch :4587/:4602/:4676/:5267/:5302/:5336/:5448、`ADMIN_HOST` :37。
5. **卡 2 LAN IP 探测环境缺陷回补（2026-10-09 执行期，PLAN 修订）**：卡 2 测试落盘后实测发现，原 UDP connect（192.0.2.1）探测在本机（VPN 环境）取到 TUN 段地址 198.18.0.1——198.18.0.0/15 是 VPN 默认路由基准段，对 0.0.0.0 监听为 TCP 黑洞（SYN 进 TUN 有去无回、只超时不拒绝），导致 `test_admin_get_read_gate_non_loopback` 恒红且无法走 SkipTest 口径。本修订仅改该测试的 `lan_ip` 探测代码块：`socket.gethostbyname_ex(socket.gethostname())[2]` 枚举候选 → 按序过滤 127/8、169.254/16、198.18/15 → 私网段（192.168/16、10/8、172.16/12）优先取第一个 → 无私网候选 `self.skipTest("no routable LAN IP in sandbox")`。实现本身已带外实证正确（真实 LAN 192.168.5.234 下：9 端点 无 token→403、错 token→403、对 token→200、loopback→200），不需回改生产代码。另两条卡 2 测试（`test_admin_get_read_allowed_no_token_env` / `test_startup_log_warns_exposure`）不经 LAN IP 探测（纯 loopback `admin_get` / 纯 stderr 断言，无真实 LAN 依赖），无需共用探测 helper，保持原状。
