# PLAN: followups 积压清偿（prune 验证 / README 部署段 / socket.timeout 覆盖）

- **Spec**: `docs/superpowers/specs/2026-09-27-followups-backlog-clear-design.md`
- **Branch**: `fix/followups-backlog-clear`（worktree `.worktrees/followups-backlog-clear`，base eab5ae4）
- **Tier summary**: 3 卡全 A → executor
- **验证基线**: `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 当前 115 用例 OK（anchor 实测：3 prune 用例 `OK` exit=0）

## Global Constraints

1. **生产代码零改动**：`ctyun-stream-fix-proxy.py` 不在本批次修改范围（锚点:865 socket.timeout 已在可重试元组；:473 _prune_daily(daily_by_model) 已落地）。
2. **Python 路径**：`/usr/bin/python3`（系统 Python 3.9.6；Homebrew Python 3.14 的 HTTPServer 挂死）。
3. **卡 3 覆盖型用例非回归修复**：行为已正确 → 直接绿，不经红灯阶段（spec「关键结论」已裁）。验证命令：全量 `117 tests OK`。
4. **sleep 时长**：夹具 `sleep_stall_seconds=3.0` > 用例 HEADER_TIMEOUT（白盒 0.5s/黑盒 1s），余量 >=2s。
5. **use_threading 必改**：漏改会致单线程 HTTPServer 在 sleep 连接上卡死测试挂死。

---

## 卡 1（验证型 tir A）— prune 已落地验证 + followups 销账

**Tier**: A（executor — todo: zero prod/test code change; verify existing tests + delete one doc line）

### 生产代码变更：无

### 测试代码变更：无

### 文档变更

1. `docs/superpowers/followups.md` — 删条目 1（line 3）

```
old_string = "- `ctyun-stream-fix-proxy.py` `save_stats_counters`（仅 prune `daily`，`daily_by_model` 不 prune，+1 天 key/天，重启清） | `daily_by_model` 内存随运行天数线性增长（键为日期串，值矩阵受 `BY_MODEL_CAP=32` 约束），长期运行进程内存缓慢膨胀 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测\n"

new_string = ""
```

### 验证命令

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py -k test_daily_by_model_prune
```

期望输出: `Ran 3 tests ... OK`，exit=0。焦点用例锚点已实测通过（同次验证基线）。

### Commit

```
test(proxy): 验证 daily_by_model prune 已落地并销账 followup
```

---

## 卡 2（免 TDD tier A）— README 部署段改实际路径

**Tier**: A（executor — todo: README 部署段替换 + followups 删条目 2；纯文档，TDD 豁免）

### 生产代码变更：无

### 测试代码变更：无

### 文档变更

1. `README.md:34-44` — 旧部署段 → 新部署段

```
old_string = "1. 将 `ctyun-stream-fix-proxy.py` 放到部署路径（plist 模板默认 `/usr/local/bin/`），`chmod +x`。\n2. 按需调整 `com.ctyun-stream-fix-proxy.plist` 中的路径（脚本位置与日志位置），放入 `~/Library/LaunchAgents/`。\n3. 加载：\n\n```sh\nlaunchctl load ~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist\n# 或加载后重启生效：\nlaunchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy\n```\n\n4. 浏览器打开 `http://127.0.0.1:7921/` 查看监控台。"

new_string = "本机实际部署（2026-09-18 验收口径）：脚本 `~/.local/bin/ctyun-stream-fix-proxy.py`，plist `~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist`（仓库内 plist 为模板，路径按目标机调整；日志路径见本机 plist 的 StandardOutPath/StandardErrorPath）。\n\n日常 deploy（改代码后生效）：\n\n```sh\ncp ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py\nlaunchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy\n```\n\n首次安装：1. `mkdir -p ~/.local/bin` + cp + `chmod +x`；2. 按目标机调整仓库 plist 模板（脚本/日志路径）拷入 `~/Library/LaunchAgents/`；3. `launchctl load ~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist`；4. 浏览器打开 `http://127.0.0.1:7921/`。"
```

2. `docs/superpowers/followups.md` — 删条目 2（原 line 4，卡 1 执行后为第一个条目）

```
old_string = "- `README.md:31-41`（部署段写 `/usr/local/bin/`） | 实际 plist（com.ctyun-stream-fix-proxy.plist）与部署副本在 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`，README 部署路径与事实不符（2026-09-18 部署验收时实测发现） | 另开小 episode：README 部署段改为 `~/.local/bin/` 实际路径 + 写明 deploy 步骤（cp + `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`）\n"

new_string = ""
```

### 验证命令

```sh
grep '~/.local/bin/ctyun-stream-fix-proxy.py' README.md
grep 'kickstart -k gui/$UID/com.ctyun-stream-fix-proxy' README.md
grep '/usr/local/bin/' README.md; [ $? -eq 1 ] && echo "PASS: no /usr/local/bin/ reference" || echo "FAIL: stale /usr/local/bin/ reference found"
```

### Commit

```
docs(readme): 部署段改实际路径 ~/.local/bin 并写明 cp+kickstart 步骤
```

---

## 卡 3（覆盖型 tir A）— socket.timeout 头超时分支补覆盖

**Tier**: A（executor — todo: 完整生产+测试代码已写死；覆盖型用例直接绿，验证全量 117 OK）

### 变更点总览

| # | 文件:行（锚点） | 变更 |
|---|----------------|------|
| 1 | `test.py:60`（stall_calls 后） | 加 3 行 sleep-stall 类属性 |
| 2 | `test.py:85`（`# --- end stall ---` 前） | 插 sleep-stall do_POST 分支 |
| 3 | `test.py:206`（make_fake_upstream 签名） | 加 3 参数 |
| 4 | `test.py:217`（attrs dict） | 透传 3 键 |
| 5 | `test.py:223`（use_threading） | 加 sleep_stall_all or sleep_stall_calls |
| 6 | `test.py:254`（make_stall_upstream 后） | 加 make_sleep_stall_upstream helper |
| 7 | `test.py:1789`（stall 白盒后） | 加 test_open_upstream_sleep_stall_raises_socket_timeout |
| 8 | `test.py:2901`（stall 黑盒后） | 加 test_header_sleep_stall_retry_success |
| 9 | `docs/superpowers/followups.md` | 删条目 3（该文件只剩标题行） |
| 10 | `README.md:82` | `115 个用例` → `117 个用例` |

### 编辑 1: 类属性（test.py:60 后插入）

```
old_string = "    stall_calls = ()        # 1-based 呼叫序号元组：命中则 close_connection=True 不写响应（代理 getresponse 抛 http.client.RemoteDisconnected）\n    rst_all = False         # True → 每呼读 body 后 SO_LINGER(1,0) close 强制发 RST（代理 getresponse 抛 ConnectionResetError）"

new_string = "    stall_calls = ()        # 1-based 呼叫序号元组：命中则 close_connection=True 不写响应（代理 getresponse 抛 http.client.RemoteDisconnected）\n    sleep_stall_all = False    # True → 每呼读 body 后 sleep 持连不写响应（代理 getresponse 抛 socket.timeout）\n    sleep_stall_calls = ()     # 1-based 呼叫序号元组：命中则 sleep 持连不写响应（同 sleep_stall_all 形态）\n    sleep_stall_seconds = 3.0  # 须 > 用例 HEADER_TIMEOUT（白盒 0.5s/黑盒 1s），余量 ≥2s 防 flaky\n    rst_all = False         # True → 每呼读 body 后 SO_LINGER(1,0) close 强制发 RST（代理 getresponse 抛 ConnectionResetError）"
```

### 编辑 2: do_POST sleep-stall 分支（test.py:85 前插入）

```
old_string = "        # --- end stall ---\n        if self.rst_all"

new_string = "        # --- sleep-stall 路径：读 body 后持连 sleep 不写响应（连接保持，代理 getresponse 阻塞到\n        # HEADER_TIMEOUT_S 抛 socket.timeout；区别于 stall 的关连接路径——后者产 RemoteDisconnected）---\n        if self.sleep_stall_all or (self.sleep_stall_calls and self.calls is not None\n                                    and len(self.calls) in self.sleep_stall_calls):\n            time.sleep(self.sleep_stall_seconds)\n            self.close_connection = True\n            return\n        # --- end sleep-stall ---\n        # --- end stall ---\n        if self.rst_all"
```

### 编辑 3: make_fake_upstream 签名（test.py:206 末尾加参）

```
old_string = "                       stall_all: bool = False, stall_calls: tuple = (),\n                       rst_all: bool = False, rst_calls: tuple = ()) -> int:"

new_string = "                       stall_all: bool = False, stall_calls: tuple = (),\n                       sleep_stall_all: bool = False, sleep_stall_calls: tuple = (),\n                       sleep_stall_seconds: float = 3.0,\n                       rst_all: bool = False, rst_calls: tuple = ()) -> int:"
```

### 编辑 4: attrs dict 透传（test.py:217 attrs 末加 3 键）

```
old_string = "             \"stall_all\": stall_all,\n             \"stall_calls\": stall_calls,\n             \"rst_all\": rst_all,"

new_string = "             \"stall_all\": stall_all,\n             \"stall_calls\": stall_calls,\n             \"sleep_stall_all\": sleep_stall_all,\n             \"sleep_stall_calls\": sleep_stall_calls,\n             \"sleep_stall_seconds\": sleep_stall_seconds,\n             \"rst_all\": rst_all,"
```

### 编辑 5: use_threading 扩展（test.py:223）

```
old_string = "    use_threading = stall_all or stall_calls or rst_all or rst_calls"

new_string = "    use_threading = (stall_all or stall_calls or rst_all or rst_calls\n                     or sleep_stall_all or sleep_stall_calls)"
```

### 编辑 6: make_sleep_stall_upstream（test.py:254 插入）

```
old_string = "def make_rst_upstream(rst_all: bool = False, rst_calls: tuple = (), **kwargs) -> tuple:"

new_string = "def make_sleep_stall_upstream(sleep_stall_all: bool = False,\n                              sleep_stall_calls: tuple = (), **kwargs) -> tuple:\n    \"\"\"带调用计数的 sleep-stall 假上游：返回 (port, calls)。\n    sleep_stall_all/sleep_stall_calls 控制哪些呼叫在读 body 后 sleep 持连不写响应\n    （连接保持静默 → 代理 getresponse 阻塞到 HEADER_TIMEOUT_S 抛 socket.timeout；\n    区别于 stall 的关连接路径——后者产 RemoteDisconnected）。\n    其余 kwargs 透传 empty_stream / body_override 等。\"\"\"\n    port = make_fake_upstream(False, scripted=True,\n                              sleep_stall_all=sleep_stall_all,\n                              sleep_stall_calls=sleep_stall_calls, **kwargs)\n    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls\n\n\ndef make_rst_upstream(rst_all: bool = False, rst_calls: tuple = (), **kwargs) -> tuple:"
```

### 编辑 7: 白盒用例 test_open_upstream_sleep_stall_raises_socket_timeout（test.py:1789 插入）

```
old_string = "    def test_open_upstream_rst_raises_connection_reset_error(self) -> None:"

new_string = "    def test_open_upstream_sleep_stall_raises_socket_timeout(self) -> None:\n        \"\"\"白盒：sleep-stall 上游 → _open_upstream 在 HEADER_TIMEOUT_S 内抛 socket.timeout。\n\n        sleep-stall 夹具读 body 后持连静默（不写响应、不关连接），代理侧 getresponse\n        阻塞到 HEADER_TIMEOUT_S 抛 socket.timeout；与 stall 夹具的\n        RemoteDisconnected（关连接路径）互补，锁可重试元组最后零覆盖子分支。\"\"\"\n        mod = self.mod\n        import types\n        upstream_port = make_fake_upstream(False, sleep_stall_all=True)\n        try:\n            orig_base = mod.UPSTREAM_BASE\n            mod.UPSTREAM_BASE = \"http://127.0.0.1:%d\" % upstream_port\n            try:\n                orig_ht = mod.HEADER_TIMEOUT_S\n                mod.HEADER_TIMEOUT_S = 0.5\n                try:\n                    body = b'{\"model\":\"test\",\"stream\":true,\"messages\":[]}'\n                    with self.assertRaises(socket.timeout):\n                        mod.ProxyHandler._open_upstream(\n                            types.SimpleNamespace(), \"POST\", \"/v1/chat/completions\",\n                            body, {\"Content-Type\": \"application/json\"})\n                finally:\n                    mod.HEADER_TIMEOUT_S = orig_ht\n            finally:\n                mod.UPSTREAM_BASE = orig_base\n        finally:\n            stop_fake_upstreams()\n\n    def test_open_upstream_rst_raises_connection_reset_error(self) -> None:"
```

### 编辑 8: 黑盒用例 test_header_sleep_stall_retry_success（test.py:2901 插入）

```
old_string = "    def test_header_stall_both_timeout_returns_502(self) -> None:"

new_string = "    def test_header_sleep_stall_retry_success(self) -> None:\n        \"\"\"sleep_stall_calls=(1,) → 首呼持连静默到 socket.timeout 触发 header-timeout 重试 → 次呼正常 → calls==2 + SSE 完整。\"\"\"\n        upstream_port, calls = make_sleep_stall_upstream(sleep_stall_calls=(1,))\n        self.proc.terminate()\n        self.proc.wait(timeout=5)\n        stderr_text(self.proc)\n        self.proc = start_proxy(upstream_port, free_port(),\n                                extra_env={\"CTYUN_HEADER_TIMEOUT\": \"1\"})\n        data = post_sse(self.proc.proxy_port)\n        self.assertEqual(len(calls), 2,\n                         \"header sleep-stall must trigger exactly one retry, calls=%d\" % len(calls))\n        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,\n                         \"attempt-2 must relay byte-exact stream, got %r\" % data)\n        # REQ 日志含 retried=1 + retry_reason=header-timeout\n        self.proc.terminate()\n        self.proc.wait(timeout=5)\n        stderr = stderr_text(self.proc)\n        self.assertIn(\"retried=1\", stderr,\n                      \"REQ line must carry retried=1, stderr:\\n\" + stderr)\n        self.assertIn(\"retry_reason=header-timeout\", stderr,\n                      \"REQ line must carry retry_reason=header-timeout, stderr:\\n\" + stderr)\n        # 持久化计数器\n        with open(os.path.join(self.proc.persist_dir, \"settings.json\"),\n                  encoding=\"utf-8\") as fh:\n            saved = json.load(fh)[\"stats\"]\n        self.assertGreaterEqual(saved.get(\"header_retries_total\", 0), 1,\n                                \"header_retries_total must be >=1 after sleep-stall retry\")\n\n    def test_header_stall_both_timeout_returns_502(self) -> None:"
```

### 编辑 9: followups.md 销账（删最后条目 + 前置空行 → 文件只剩标题行）

卡 1、卡 2 已删前两个条目，执行时文件内容为:

```
# Follow-ups（跨 episode 独立问题）

- `ctyun-stream-fix-proxy.py:865`（header-stall 可重试 except 元组） | `(socket.timeout, RemoteDisconnected)` 中 socket.timeout 子分支零测试覆盖——现有 stall 夹具走关连接路径只产出 RemoteDisconnected（review r1 建议可选项） | 另开小 episode：加真·持连 stall 夹具（handler 读 body 后 sleep 到超时）覆盖 socket.timeout 分支 + 单测
```

```
old_string = "\n- `ctyun-stream-fix-proxy.py:865`（header-stall 可重试 except 元组） | `(socket.timeout, RemoteDisconnected)` 中 socket.timeout 子分支零测试覆盖——现有 stall 夹具走关连接路径只产出 RemoteDisconnected（review r1 建议可选项） | 另开小 episode：加真·持连 stall 夹具（handler 读 body 后 sleep 到超时）覆盖 socket.timeout 分支 + 单测\n"

new_string = ""
```

### 编辑 10: README 用例数同步

```
old_string = "115 个用例。"

new_string = "117 个用例。"
```

### 验证命令

```sh
# 焦点跑 2 新用例
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_open_upstream_sleep_stall_raises_socket_timeout AdminIntegrationTest.test_header_sleep_stall_retry_success

# 全量 117 用例 OK
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

期望: 焦点 2 用例 `OK` exit=0；全量 `Ran 117 tests in ... OK` exit=0。

### Commit

```
test(proxy): socket.timeout 头超时分支补覆盖（持连 stall 夹具）
```

---

## Plan Self-Review（implementer 自查 5 项）

1. **Spec coverage** — 3 卡全覆盖 spec Goal/Acceptance/Files。卡 1: prune 验证 + followups 销账（spec Acceptance 第 1 条，Files 条目 1）。卡 2: README 部署段改 actual path + followups 销账（spec Acceptance 第 2 条，Files:2 条目:32-44 → NEW 写死，README:82 留卡 3）。卡 3: sleep-stall 夹具全量代码 + 白盒/黑盒 2 用例 + use_threading 扩展 + 用例数 115→117 + followups 终销账（spec Acceptance 第 3-4 条，Files:4-10 全部覆盖）。**生产代码零改动符合 Exclusion**。

2. **Placeholder scan** — 全文搜索 `TODO|FIXME|...` 无命中。所有 old_string/new_string 均为完整代码块，无"同上"/"参见"等占位符。

3. **Type consistency** — `sleep_stall_all: bool`、`sleep_stall_calls: tuple`、`sleep_stall_seconds: float` 三个参数在 4 处引用（类属性、do_POST、make_fake_upstream 签名、attrs dict）类型一致。`make_sleep_stall_upstream` 返回 `tuple[port, calls]` 与 `make_stall_upstream` 同型。白盒用 `assertRaises(socket.timeout)`，黑盒断言 `retry_reason=header-timeout`（`_log` 格式 `retried=%d retry_reason=%s` 产出 `retried=1 retry_reason=header-timeout`，精确匹配）。

4. **可落盘性** — 所有 Edit old_string 均锚定唯一行（实测 grep 确认）：
   - `stall_calls = ()` + `rst_all = False` 组合 → 文件内唯一
   - `# --- end stall ---` → 单一 occurrence（test.py:85）
   - `stall_all: bool = False, stall_calls: tuple = (),\n                       rst_all: bool = False` → 唯一签名尾部
   - `"stall_calls": stall_calls,\n             "rst_all": rst_all,` → attrs dict 尾部唯一
   - `use_threading = stall_all or stall_calls or rst_all or rst_calls` → 唯一（test.py:223）
   - `def make_rst_upstream(rst_all` → 唯一（test.py:256）
   - `def test_open_upstream_rst_raises_connection_reset_error` → 唯一（test.py:1790）
   - `def test_header_stall_both_timeout_returns_502` → 唯一（test.py:2902）
   - `- \`ctyun-stream-fix-proxy.py:865\`...` 完整行 → followups.md 唯一（仅剩该条目）
   文件无 trailing whitespace 污染。Python 3.9.6 unittest.main 支持 `-k` 选择器与多个 positional test name。

5. **锚点实测** — prune 3 用例焦点已跑 `OK`（exit=0）。`ctyun-stream-fix-proxy.py:865` socket.timeout 在元组内已 grep 确认。`test.py:223 use_threading` 当前不含 sleep_stall 条件 — 卡 3 编辑 5 必改。黑盒 `extra_env={"CTYUN_HEADER_TIMEOUT": "1"}` 符合 README "最小值 1"（:55）。`stop_fake_upstreams()` 线程安全（daemon_threads=True + shutdown 不阻塞，spec Risk 已分析）。