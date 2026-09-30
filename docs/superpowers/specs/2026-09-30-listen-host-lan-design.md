# 转发代理开放局域网监听（CTYUN_LISTEN_HOST）设计

## Goal

让局域网机器可使用转发代理（7920）：`LISTEN_HOST` 由硬编码 `127.0.0.1` 改为 env seam `CTYUN_LISTEN_HOST`（默认仍 `127.0.0.1` 向后兼容），仓库 launchd plist 模板内置 `0.0.0.0` 使实际部署开箱 LAN 可达。

## Files to Change

1. `ctyun-stream-fix-proxy.py:31` — OLD `LISTEN_HOST = "127.0.0.1"` → NEW：
   ```python
   LISTEN_HOST = os.environ.get("CTYUN_LISTEN_HOST", "127.0.0.1")
   ```
   一行，仿 :33 `ADMIN_HOST` 同款模式（无尾注释，与邻行风格一致）；下游 :1893 `ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)` 与 :1896 启动日志零改动即生效。

2. `ctyun-stream-fix-proxy.test.py` 两处：
   - `:374` 后（`start_proxy` 内，必须在 `:381 if extra_env: env.update(extra_env)` 之前，保证 extra_env 可覆盖）加一行钉死默认，防开发机 shell 已 export 时泄漏进全量用例：
     ```python
     env["CTYUN_LISTEN_HOST"] = "127.0.0.1"  # 钉死默认绑定，防 shell env 泄漏 0.0.0.0
     ```
   - `:518` `class ProxyDashboardUnitTest` 前插新黑盒用例类（helpers 全部现成：`make_fake_upstream` :209、`start_proxy` :370、`free_port` :351、`stderr_text` :325、`stop_fake_upstreams` :308）：
     ```python
     class ListenHostEnvTest(unittest.TestCase):
         """CTYUN_LISTEN_HOST seam：0.0.0.0 时非 loopback 本机地址 TCP 可达。
         默认 127.0.0.1 回归 = 既有全量用例（start_proxy 已显式钉死）。"""

         def test_listen_host_env_binds_lan(self) -> None:
             upstream_port = make_fake_upstream(False)
             proxy_port = free_port()
             proc = start_proxy(upstream_port, proxy_port,
                                extra_env={"CTYUN_LISTEN_HOST": "0.0.0.0"})
             try:
                 # 证据 1（主）：启动日志宣告实际绑定地址（py 启动行在 serve_forever 前输出）
                 deadline = time.time() + 5
                 while time.time() < deadline:
                     if ("listening on 0.0.0.0:%d" % proxy_port) in stderr_text(proc):
                         break
                     time.sleep(0.05)
                 else:
                     self.fail("stderr 未见 0.0.0.0 绑定宣告: %s" % stderr_text(proc))
                 # 证据 2：非 loopback 地址 TCP 可达（UDP connect 不发包，仅取路由源地址）
                 probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                 try:
                     probe.connect(("192.0.2.1", 80))  # TEST-NET-1；UDP connect 零流量
                 except OSError:
                     # 吞掉"无非 loopback 接口/无路由"（沙箱/离线机）：证据 1 已锁绑定
                     # 地址，连通性断言可跳过；try 内仅此一条语句可能抛 OSError。
                     lan_ip = None
                 else:
                     lan_ip = probe.getsockname()[0]
                 probe.close()
                 if lan_ip and not lan_ip.startswith("127."):
                     with socket.create_connection((lan_ip, proxy_port), timeout=5):
                         pass
             finally:
                 proc.terminate()
                 proc.wait(timeout=5)
                 shutil.rmtree(proc.persist_dir, ignore_errors=True)
                 stop_fake_upstreams()
     ```

3. `com.ctyun-stream-fix-proxy.plist:11`（`</array>` 之后）— 插入：
   ```xml
     <key>EnvironmentVariables</key>
     <dict>
       <key>CTYUN_LISTEN_HOST</key>
       <string>0.0.0.0</string>
     </dict>
   ```

4. `README.md` 四处：
   - `:14` OLD：`- **双端口**：转发代理 `127.0.0.1:7920` + 同进程内嵌监控台 `0.0.0.0:7921`` → NEW：`- **双端口**：转发代理 `:7920`（默认绑 `127.0.0.1`；`CTYUN_LISTEN_HOST=0.0.0.0` 开放局域网）+ 同进程内嵌监控台 `0.0.0.0:7921``
   - 环境变量表 `:49`（`CTYUN_ADMIN_HOST` 行之前）插一行：`| `CTYUN_LISTEN_HOST` | `127.0.0.1` | 转发代理监听地址；`0.0.0.0` 开放局域网（转发端口无鉴权，仅可信网段） |`
   - 部署段 `:41` 代码块后加一行：`仓库 plist 模板已内置 `EnvironmentVariables.CTYUN_LISTEN_HOST=0.0.0.0`：live plist（~/Library/LaunchAgents/）同步该键并 kickstart -k 后转发端口 LAN 可达；不需要时删键或改回 `127.0.0.1`。`
   - `:81` `117 个用例` → `118 个用例`。

## Acceptance Criteria

- TDD 红绿：先落卡 2 测试（py 未改）焦点跑 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py ListenHostEnvTest` 红（stderr 断言失败），改 py:31 后同命令绿（贴实际 stdout/exit code）。
- 全量回归：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（必须系统 Python，README:81 警告 Homebrew 3.14 挂死）118 用例 OK——既有 117 用例零改动即绿，即默认值 `127.0.0.1` 回归证据。
- `plutil -lint com.ctyun-stream-fix-proxy.plist` 输出 OK。
- 手工验收（部署机，DELIVER 后）：live plist 同步该键 + `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`，`lsof -nP -iTCP:7920 -sTCP:LISTEN` 显示 `*:7920`，局域网他机 `nc -z <部署机IP> 7920` 通。

## Risks

- **安全（用户已知情接受）**：转发端口无鉴权，绑 `0.0.0.0` 后同 LAN 任何机器可经它向上游转发流量（开放中继面）；链路为 LAN 明文 HTTP。仅可信网段使用；不信任即从 live plist 删键回 `127.0.0.1`。
- **macOS 防火墙**：`/usr/bin/python3` 首次接收入向连接可能弹「允许传入连接」，用户拒绝则 LAN 仍不通——需在系统设置手动允许，非 bug。
- **plist 模板 vs live 实例**：仓库 plist 是模板（README:34 已声明），本改动不触 live `~/Library/LaunchAgents/` 实例；不同步则部署行为零变化（静默不生效），手工验收步已覆盖。
- **测试环境无 LAN 接口**：沙箱/离线机证据 2 跳过，证据 1（stderr 绑定宣告，源自 py:1896 实际 bind 参数）仍锁定行为，非全跳过。
- launchd `EnvironmentVariables` 仅进程启动时读取：改键必须 `kickstart -k`，对运行中进程不生效。

## Exclusions

- 转发端口鉴权 / IP 白名单 / TLS（用户未要求，属后续 episode）。
- plist 模板 `/usr/local/bin` 路径修正（README:34「模板按目标机调整」属现状设计）。
- `CTYUN_LISTEN_PORT` / `CTYUN_UPSTREAM_BASE` 补登 README 环境变量表（历史缺口，另开 followup）。
- admin 侧（7921）监听地址与鉴权模型任何改动。
- live plist 实例的直接编辑（不属仓库改动，由部署者按 README 同步）。

## R31 Evidence

[R31-S1] 问题现场（硬编码 + seam 缺失）：

```
$ grep -n 'LISTEN_HOST\|ADMIN_HOST' ctyun-stream-fix-proxy.py
31:LISTEN_HOST = "127.0.0.1"
33:ADMIN_HOST = os.environ.get("CTYUN_ADMIN_HOST", "0.0.0.0")
1893:    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
1896:                     % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE, ADMIN_HOST, ADMIN_PORT))
```

```
$ grep -n 'EnvironmentVariables\|ProgramArguments' com.ctyun-stream-fix-proxy.plist
7:  <key>ProgramArguments</key>
（EnvironmentVariables 零命中 → 部署侧 seam 未接通）
```

[R31-S2] 根因陈述：py:31 硬编码 loopback 是 LAN 不可达的唯一代码根因；admin 侧 :33 已有同款 env 模式可仿，测试 seam 现成（start_proxy 的 extra_env 在 :381 覆盖 env）：

```
$ grep -n 'CTYUN_LISTEN_PORT\|extra_env' ctyun-stream-fix-proxy.test.py | head -5
4:起 stdlib 假上游（127.0.0.1:0 随机端口），经 CTYUN_UPSTREAM_BASE / CTYUN_LISTEN_PORT
370:def start_proxy(upstream_port: int, proxy_port: int, extra_env: dict = None,
374:    env["CTYUN_LISTEN_PORT"] = str(proxy_port)
381:    if extra_env:
382:        env.update(extra_env)
```
