## Goal

为非 loopback 来源的管理面 GET 端点增加可选 token 门禁（`read_allowed`），配合启动日志暴露面警告，关闭 finding E（管理面零鉴权），同时保持 ADMIN_HOST 默认 0.0.0.0 不破坏用户现有局域网 dashboard 使用。

## Files to Change

### 1. `ctyun-stream-fix-proxy.py` — 新增 `read_allowed` + do_GET 门禁 + 启动警告

**`read_allowed` 新函数（位于 `write_allowed` 之后，约 :2325）**
```python
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
```
- **锚点**：`write_allowed` 函数定义（`ctyun-stream-fix-proxy.py:2319`）后紧跟。
- **接口契约**：入参 `peer_ip: str, token_header: str, token_env: str`，出参 `bool`。
- **边界条件**：（1）loopback IPv4/IPv6 → True；（2）非 loopback + token_env 空 → True；（3）非 loopback + token_env 非空 + header 匹配 → True；（4）非 loopback + token_env 非空 + header 不匹配 → False。

**`AdminHandler.do_GET` 加装 read 门禁（:3330-3420）**
在 `self._send_json(200, ...)` 调用前，对以下 GET 端点插入 `read_allowed` 判定：
- `/api/stats` :3336 → 门禁后 `:3337`
- `/api/tpm_stats` :3338 → 门禁后 `:3339`
- `/api/tpm_settings` :3340 → 门禁后 `:3341`
- `/api/tpm_calibrate` :3342 → 门禁后 `:3343`
- `/api/config` :3344 → 门禁后 `:3351`
- `/api/health` :3351 → 门禁后 `:3353`
- `/api/probe_history` :3353 → 门禁后 `:3355`
- `/api/errors` 列表（`id_str is None` 分支 `:3380-3396`）→ 门禁后 `:3395`
- `/api/logs` :3397 → 门禁后 `:3419`

不门禁：`/`（:3332 dashboard HTML）、`/favicon.ico`（:3334）、`/api/errors?id=` 详情（:3358 已有 `write_allowed` 门禁）、未知路径 404（:3419）。

门禁模板（以 /api/stats 为例）：
```python
elif path == "/api/stats":
    if not read_allowed(self.client_address[0],
                        self.headers.get("X-Admin-Token") or "",
                        os.environ.get("CTYUN_ADMIN_TOKEN", "")):
        self._send_json(403, {"error": "read requires X-Admin-Token"})
        return
    self._send_json(200, stats_snapshot())
```

**启动日志暴露面警告（`main` 段，:5612-5613 区域）**
在 `_safe_log_stderr` 宣告监听地址后，追加条件警告行。形式：
```python
if ADMIN_HOST not in ("127.0.0.1", "::1") and not os.environ.get("CTYUN_ADMIN_TOKEN"):
    _safe_log_stderr("ctyun-stream-fix-proxy: WARNING — admin dashboard bound to %s:%d "
                     "without CTYUN_ADMIN_TOKEN; LAN read endpoints (stats/logs/errors "
                     "list/config/health/calibrate states) are served without "
                     "authentication. Set CTYUN_ADMIN_TOKEN to require X-Admin-Token "
                     "header on non-loopback reads." % (ADMIN_HOST, ADMIN_PORT))
```
- **锚点**：`ctyun-stream-fix-proxy.py:5613`（listening 行之后，`server = ...` 创建 ProxyHandler 之前或之后均可，紧随 listening 行最清晰）。
- **边界条件**：仅当 ADMIN_HOST 非 loopback 且 CTYUN_ADMIN_TOKEN 为空/未设时打印；loopback 绑定或已配 token 时不打印。

### 2. `ctyun-stream-fix-proxy.test.py` — 新增读门禁测试

**`test_read_allowed_matrix`（`ProxyDashboardUnitTest` 类内，约 :765 之后）**
`test_write_allowed_matrix` 的同构测试。覆盖矩阵：
```
loopback 127.0.0.1 / "", "" → True
loopback ::1 / "", "" → True
非 loopback / "", "" → True   （与 write_allowed 关键差异）
非 loopback / "", "tok" → True
非 loopback / "tok", "tok" → True
非 loopback / "wrong", "tok" → False
非 loopback / "wrong", "" → True   （无 token 配，header 不检查）
```
函数级锚点：`ProxyDashboardUnitTest.test_write_allowed_matrix`（:765）后新增 `test_read_allowed_matrix`。

**`AdminIntegrationTest` 新增 3 个测试方法（约 :3890 区域）**

(1) `test_admin_get_read_gate_non_loopback`
```
# 用 lan_ip + socket.create_connection（既有模式：test_listen_host_env_binds_lan :692）
# extra_env={"CTYUN_ADMIN_HOST": "0.0.0.0", "CTYUN_ADMIN_TOKEN": "sekret"}
# admin_get via lan_ip, no header → 403
# admin_get via lan_ip, X-Admin-Token: sekret → 200
# admin_get via 127.0.0.1, no header → 200（loopback 直通）
```
关键锚点：复用 `lan_ip` 探测技巧（:706-716）、`admin_get(port, path, headers={...})`（:649）、`start_proxy(extra_env={...})`（:490）。当无法获取非 loopback 地址（`lan_ip is None or lan_ip.startswith("127.")`）时 `raise unittest.SkipTest`——与 :717 的防御模式一致。

(2) `test_admin_get_read_allowed_no_token_env`
```
# extra_env={"CTYUN_ADMIN_HOST": "0.0.0.0"}（不设 CTYUN_ADMIN_TOKEN）
# admin_get via 127.0.0.1 → 200（loopback 直通）
```
不测试非 loopback（会让测试依赖网络拓扑），仅验证 loopback 不受 token 配置影响。

(3) `test_startup_log_warns_exposure`
```
# extra_env={"CTYUN_ADMIN_HOST": "0.0.0.0"}（不设 token）
# 断言 stderr_text(proc) 含 "WARNING" 和 "CTYUN_ADMIN_TOKEN"
# 反例：extra_env={"CTYUN_ADMIN_HOST": "127.0.0.1"} → stderr 不含 WARNING
```
关键锚点：`stderr_text(proc)`（:420）、`start_proxy(extra_env={...})`（:490）。`stderr_text` 实时 drain，启动日志在 `wait_port(admin_port)` 后即可读。

## Acceptance Criteria

| # | 断言 | 验证方式 |
|---|------|----------|
| 1 | `read_allowed` 矩阵：loopback 无条件 True；无 token_env 非 loopback True；token 已配匹配判定正确 | `test_read_allowed_matrix` |
| 2 | 非 loopback 无 token GET /api/stats → 403（CTYUN_ADMIN_TOKEN 已配时） | `test_admin_get_read_gate_non_loopback`（跳过无 LAN IP） |
| 3 | 非 loopback 正确 token GET /api/stats → 200 | 同上 |
| 4 | loopback GET /api/stats → 200（不受门禁影响） | 同上 |
| 5 | CTYUN_ADMIN_TOKEN 未配时 loopback GET → 200 | `test_admin_get_read_allowed_no_token_env` |
| 6 | ADMIN_HOST 非 loopback + 无 token → stderr 含 `WARNING` + `CTYUN_ADMIN_TOKEN` | `test_startup_log_warns_exposure` |
| 7 | ADMIN_HOST=127.0.0.1 → stderr 不含 WARNING | 同上反例 |
| 8 | POST 写端点行为零变化（现有 POST 测试全绿） | `test_config_post_localhost_allowed_even_with_token_env` 等 |
| 9 | `python ctyun-stream-fix-proxy.test.py` 全绿（含既有 1600+ 测试） | `python ctyun-stream-fix-proxy.test.py` exit 0 |

## Risks

1. **Dashboard 浏览器端 token 传递**：当前 DASHBOARD_HTML 的 7 个 GET fetch 调用（:4587 / :4602 / :4676 / :5267 / :5302 / :5336 / :5448）均未发送 `X-Admin-Token`。用户配置 `CTYUN_ADMIN_TOKEN` 后，局域网浏览器内 dashboard 的 API 轮询会收到 403 → 页面降级（connectivity chip 显示"读取失败"）。**缓解**：dashoard JS 的 token 支持作为独立跟进 episode（E3b），本期 spec 不在 Files 内。
2. **LAN IP 探测不可靠**：`test_listen_host_env_binds_lan` 使用的 UDP 探测（:706-716）在无路由/沙箱环境中 `lan_ip` 为 None。新增非 loopback 门禁测试同此模式，`lan_ip is None` → `SkipTest`。部分 CI 环境永远不会执行非 loopback 分支——由纯函数 `test_read_allowed_matrix` 兜底覆盖。
3. **与 E1 持久化变更无冲突**：`read_allowed`/`do_GET` 门禁只碰 AdminHandler 的请求调度，不涉及 STATS_LOCK / _CFG_LOCK / _PERSIST_LOCK 任一条锁链。E1 已落地，E3 基于最新行号。

## Exclusions

1. 不改 `ADMIN_HOST` 默认值（:37 保持 `"0.0.0.0"`）——硬约束。
2. 不削弱现有写端点门禁（`write_allowed` 语义零变化）。
3. 不修改 DASHBOARD_HTML 的 JS（dashboard 浏览器端 token 传递不在本期 scope，开单独 episode）。
4. `/`（dashboard HTML）和 `/favicon.ico` 不门禁——前者为静态无秘密内容，门禁会使 token 配置后 dashboard 页面完全不可达（浏览器无 JS 运行环境），与 LAN 访问约束矛盾。
5. `/api/errors?id=` 详情保持现有 `write_allowed` 门禁，不替换为 `read_allowed`（详情含 body/response 属写级敏感度）。

## R31 Evidence

[R31-S1] 证据：do_GET 全端点裸奔（除 /errors?id 已挂 write_allowed）

```
$ grep -n 'elif path == "/api/' ctyun-stream-fix-proxy.py | head -12
3336:        elif path == "/api/stats":
3338:        elif path == "/api/tpm_stats":
3340:        elif path == "/api/tpm_settings":
3342:        elif path == "/api/tpm_calibrate":
3344:        elif path == "/api/config":
3351:        elif path == "/api/health":
3353:        elif path == "/api/probe_history":
3355:        elif path == "/api/errors":
3397:        elif path == "/api/logs":
```

```
$ grep -n 'write_allowed\|read_allowed\|无鉴权\|X-Admin-Token' ctyun-stream-fix-proxy.py | head -15
2319:def write_allowed(peer_ip: str, token_header: str, token_env: str) -> bool:
3365:                if not write_allowed(self.client_address[0],
3366:                                     self.headers.get("X-Admin-Token") or "",
3381:                # 列表（不含 body/response），无鉴权
3442:        if not write_allowed(self.client_address[0],
3443:                             self.headers.get("X-Admin-Token") or "",
3495:        if not write_allowed(self.client_address[0],
3496:                             self.headers.get("X-Admin-Token") or "",
3516:        if not write_allowed(self.client_address[0],
3517:                             self.headers.get("X-Admin-Token") or "",
3538:        if not write_allowed(self.client_address[0],
3539:                             self.headers.get("X-Admin-Token") or "",
```

```
$ grep -n 'ADMIN_HOST\|CTYUN_ADMIN_TOKEN' ctyun-stream-fix-proxy.py | head -6
37:ADMIN_HOST = os.environ.get("CTYUN_ADMIN_HOST", "0.0.0.0")
3367:                                     os.environ.get("CTYUN_ADMIN_TOKEN", "")):
3444:                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
3497:                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
3518:                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
3540:                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
```

[R31-S2] 根因：`AdminHandler.do_GET`（:3330-3420）对除 `/api/errors?id=` （:3365 `write_allowed`）和 404 外的 9 个 GET 端点均未做任何鉴权判定；`/api/errors` 列表分支（:3381）注释 "无鉴权" 确认是有意设计。ADMIN_HOST 默认绑定 0.0.0.0（:37）使该面在局域网完全暴露。`_safe_log_stderr`（:1643）仅在监听宣告行输出地址（:5612-5613），未打印暴露面警告。写端点（POST /api/config :3442、/api/probe :3495、/api/tpm_calibrate :3516、/api/tpm_calibrate_abort :3538）均已通过 `write_allowed`（:2319）正确门禁，读通路没有等效机制。