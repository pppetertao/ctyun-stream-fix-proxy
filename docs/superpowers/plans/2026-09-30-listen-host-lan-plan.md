# PLAN — 转发代理开放局域网监听（CTYUN_LISTEN_HOST）

- **spec**：`docs/superpowers/specs/2026-09-30-listen-host-lan-design.md`
- **branch**：`fix/listen-host-lan`
- **tier**：全卡 A（spec 代码写死，锚点实测，执行阶段派 executor 转写）
- **date**：2026-09-30

## Global Constraints

1. **系统 Python** — 全部测试必须用 `/usr/bin/python3`（README:81 警告 Homebrew 3.14 `HTTPSServer` 挂死）。
2. **Conventional Commits** — `feat:` / `test:` 格式，一卡一 commit。
3. **每卡完成后 task review** — spec 合规 + 质量双审（executor RTC 返回后主代理执行，通过才进入下一卡）。
4. **TDD** — 卡 1 严格执行：先落测试（红）→ 改生产代码（绿）→ 焦点验证。
5. **commit 在 worktree 分支 `fix/listen-host-lan`**，不进 main。

---

## 卡 1（tier A）— TDD：py:31 env seam + 测试先行

**文件**：
- `ctyun-stream-fix-proxy.py`
- `ctyun-stream-fix-proxy.test.py`

**执行顺序（关键）**：

### Step 1 — 落测试（不改生产代码）

**插入 1**：`ctyun-stream-fix-proxy.test.py` — 在 `start_proxy` 内 :374 后、:381 `if extra_env:` 之前，加一行钉死默认：

```
old:     env["CTYUN_LISTEN_PORT"] = str(proxy_port)
new:     env["CTYUN_LISTEN_PORT"] = str(proxy_port)
    env["CTYUN_LISTEN_HOST"] = "127.0.0.1"  # 钉死默认绑定，防 shell env 泄漏 0.0.0.0
```

**插入 2**：`ctyun-stream-fix-proxy.test.py` — 在 :518 `class ProxyDashboardUnitTest(unittest.TestCase):` 之前插入新测试类：

```
old: class ProxyDashboardUnitTest(unittest.TestCase):

new: class ListenHostEnvTest(unittest.TestCase):
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

class ProxyDashboardUnitTest(unittest.TestCase):
```

### Step 2 — 焦点跑新测试（预期红）

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ListenHostEnvTest
```

**预期**：stderr 断言失败（py:31 仍硬编码 `127.0.0.1`，`0.0.0.0` 绑定宣告不会出现）。

### Step 3 — 改生产代码

`ctyun-stream-fix-proxy.py`：:31 一行改 env seam

```
old: LISTEN_HOST = "127.0.0.1"
new: LISTEN_HOST = os.environ.get("CTYUN_LISTEN_HOST", "127.0.0.1")
```

（`os` 已在模块顶部导入；:33 `ADMIN_HOST` 同款模式，无尾注释保持邻行风格。）

### Step 4 — 焦点验证（预期绿）

```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ListenHostEnvTest
```

**预期**：OK，0 失败。贴实际 stdout 与 exit code。

### Step 5 — commit

```bash
git add ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py
git commit -m "feat(proxy): CTYUN_LISTEN_HOST env seam 替换硬编码 127.0.0.1

LISTEN_HOST 由 os.environ.get('CTYUN_LISTEN_HOST', '127.0.0.1') 替代，
默认值不改；新增 ListenHostEnvTest（0.0.0.0 绑定宣告 + LAN 可达）。"
```

---

## 卡 2（tier A）— plist + README 四处 + plutil lint + 全量回归收尾

**文件**：
- `com.ctyun-stream-fix-proxy.plist`
- `README.md`

### 编辑 1 — plist 模板内置 env

`com.ctyun-stream-fix-proxy.plist`：在 `</array>` 之后插入 `EnvironmentVariables` dict

```
old:   </array>
  <key>RunAtLoad</key>

new:   </array>
    <key>EnvironmentVariables</key>
    <dict>
      <key>CTYUN_LISTEN_HOST</key>
      <string>0.0.0.0</string>
    </dict>
  <key>RunAtLoad</key>
```

### 编辑 2 — README :14 特性行

```
old: - **双端口**：转发代理 `127.0.0.1:7920` + 同进程内嵌监控台 `0.0.0.0:7921`

new: - **双端口**：转发代理 `:7920`（默认绑 `127.0.0.1`；`CTYUN_LISTEN_HOST=0.0.0.0` 开放局域网）+ 同进程内嵌监控台 `0.0.0.0:7921`
```

### 编辑 3 — README env 表插新行（:49 `CTYUN_ADMIN_HOST` 之前）

```
old: | `CTYUN_ADMIN_HOST` | `0.0.0.0` | 监控台监听地址 |

new: | `CTYUN_LISTEN_HOST` | `127.0.0.1` | 转发代理监听地址；`0.0.0.0` 开放局域网（转发端口无鉴权，仅可信网段） |
| `CTYUN_ADMIN_HOST` | `0.0.0.0` | 监控台监听地址 |
```

### 编辑 4 — README 部署段代码块后加一行

在 `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`（:40，代码块最后一行）+ 其后 closing fence（:41）之后插入新行。old/new 如下（用 4-backtick fence 包裹，区分内层 3-backtick fence）：

````text
OLD:
launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy
```

NEW:
launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy
```
仓库 plist 模板已内置 `EnvironmentVariables.CTYUN_LISTEN_HOST=0.0.0.0`：live plist（~/Library/LaunchAgents/）同步该键并 kickstart -k 后转发端口 LAN 可达；不需要时删键或改回 `127.0.0.1`。
````

### 编辑 5 — README :81 用例计数

```
old: 117 个用例

new: 118 个用例
```

### 验证

```bash
# plist 语法检查
plutil -lint com.ctyun-stream-fix-proxy.plist
# 预期输出：com.ctyun-stream-fix-proxy.plist: OK

# 全量回归（系统 Python）
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
# 预期：118 用例全部 OK，0 失败，exit code 0
```

### commit

```bash
git add com.ctyun-stream-fix-proxy.plist README.md
git commit -m "feat(plist): CTYUN_LISTEN_HOST=0.0.0.0 内置 plist 模板，README 同步

plist 模板内置 EnvironmentVariables → 部署机同步该键后转发端口 LAN 可达。
README 特性行/环境变量表/部署段/用例计数四处同步更新。"
```

---

## Self-Review（5 项）

### 1. spec coverage
spec 5 文件（py:31 / test.py 两处 / plist:11 / README 四处）全部覆盖，逐卡逐编辑点一一映射。AC 全部代码化。

### 2. placeholder scan
全文 grep `TODO` / `FIXME` / `...` / `INSERT` / `PLACEHOLDER` — 零命中。所有代码块、验证命令、commit message 均写死。

### 3. type consistency
- py:31 `os.environ.get("CTYUN_LISTEN_HOST", "127.0.0.1")` → `str`，与下游 :1893 `ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ...)` 签名一致（`host: str`）。
- test: `extra_env={"CTYUN_LISTEN_HOST": "0.0.0.0"}` → `dict[str, str]`，与 `start_proxy` :370 `extra_env: dict` 兼容。
- plist: `<key>CTYUN_LISTEN_HOST</key><string>0.0.0.0</string>` → launchd 字符串 env 注入，类型一致。

### 4. 可落盘性
每卡 old_string / new_string 均取自 worktree 文件实测行号（py:31、test.py:374/518、plist:11、README:14/40-41/49/81），Edit 锚点唯一。验证命令均给出确切命令 + 预期输出，executor 可直接执行无需设计决策。

### 5. 锚点实测
抽样 3 处实测通过（worktree 当前状态）：
- py:31 — `grep` 确认 `LISTEN_HOST = "127.0.0.1"` 唯一命中
- test.py:374,381 — `grep` 确认 `env["CTYUN_LISTEN_PORT"]` 与 `if extra_env:` 行号吻合
- README:81 — `grep` 确认 `117 个用例` 唯一命中