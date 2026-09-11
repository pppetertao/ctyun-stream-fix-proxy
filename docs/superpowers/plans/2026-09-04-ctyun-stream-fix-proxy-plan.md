# ctyun-stream-fix-proxy 实施计划（simplified PLAN-B）

- 日期：2026-09-04
- Spec（唯一需求源）：`docs/superpowers/specs/2026-09-04-ctyun-stream-fix-proxy-design.md`
- Worktree：`/Users/peter/.zcode/.worktrees/ctyun-stream-fix-proxy`（branch `fix/ctyun-stream-fix-proxy`）
- 卡数：4（Task 1/2 = B 档 executor 转写；Task 3/4 = C 档 implementer 真机留白），串行执行，单次 dispatch
- 落盘说明：4 张卡的生产/测试文件全部落真实路径（`/Users/peter/.local/bin/`、`/Users/peter/Library/LaunchAgents/`、`/Users/peter/.zcode/v2/config.json`），不在 worktree 内；worktree 分支只收 spec/PLAN 文档。spec 未要求生产脚本入库，不设 commit 步骤。

## 锚点实测（PLAN-B 自查，2026-09-04 实跑）

- `/Users/peter/.zcode/v2/config.json` `:608` = provider 块 `"57b84b95-a300-4b97-9e05-bc71c8fa6096"` 起始，`:612` = `apiKey`，`:613` = `baseURL`（`sed -n '605,620p'` 逐行核对命中）。
- baseURL 字符串 `https://eaichat.ctyun.cn/ai/platform/v2/cp` 在 config 中出现 2 处：`:277`（其他 provider 块，不碰）与 `:613`（TY Pro）——**禁止单行唯一替换，Task 4 必须用 `:612`-`:613` 两行作 Edit 锚**。
- provider 定位于 `cfg["provider"]["57b84b95-a300-4b97-9e05-bc71c8fa6096"]["options"]`（JSON walk 实测）。
- `/usr/bin/python3 --version` → `Python 3.9.6`（≥3.9 达标；代码全部按 3.9 兼容写死，无 match/无 `|` type union）。
- PATH `python3` = Homebrew 3.14.7（实测其 `http.server.HTTPServer` 构造挂死，禁用）；launchd plist `ProgramArguments` 固定 `/usr/bin/python3`——本计划所有验证命令的 python 调用统一写死 `/usr/bin/python3` 绝对路径（测试内代理经 `sys.executable` 拉起，随启动解释器，与 plist 同源）。
- 端口 7920 空闲（`lsof -iTCP:7920 -sTCP:LISTEN` 无输出）。
- `/Users/peter/.local/bin` 与 `/Users/peter/.local/log` 实际已存在（spec「两目录现均不存在」陈述已过时）；`mkdir -p` 幂等照跑，无害。
- plist 风格参照 `/Users/peter/Library/LaunchAgents/com.zcode.aliyun-env.plist`（XML header + doctype 同款、2 空格缩进，已读原文件核对）。
- `/v2/` 整目录 git-ignored（`.gitignore:33`），config 备份 `.bak` 不污染 `git status`。

## Global Constraints

1. **TDD 先红后绿**：Task 1 先落测试并实跑，贴红相 stderr 原文；红相没贴，不许进 Task 2 写实现。
2. **报错贴原文**：任何命令失败，先贴完整 stdout/stderr 原文，不概括、不转述；不自动重试相同命令，先诊断根因再改。
3. **claim 完成必须贴证据**：每卡验收前贴实际命令 stdout + exit code（`echo "exit=$?"` 跟在验收命令后）；"应该能过"不算完成。
4. **executor STOP 纪律**：executor 遇清单不清 / 文件锚点与实测不符（如 `:613` 不是 baseURL）/ 卡内代码与实际冲突 → 立即 STOP 退报告，不得边设计边执行，不得自行改卡。
5. **纯 stdlib、Python 3.9 兼容**：只用 `http.server` / `http.client` / `re` / `os` / `sys` / `time` / `socket` / `subprocess` / `threading` / `unittest` / `urllib.parse`；禁第三方包、禁 3.10+ 语法（match、`X | Y` type union）。
6. **scope 边界**：不动旧 TY（`:277` 所在 provider 块）及其他任何 provider；不动 `~/.zcode/agents/*.md`；不做鉴权/加密（仅绑 127.0.0.1）；不碰 opencode-forward-proxy（7898）。
7. **日志格式**：代理每请求 stderr 固定单行 `REQ {method} {path} -> {status} dur={n}s result={ok|upstream-err|error} filtered={n}`。
8. **端口联动**：仅当 7920 被占才整体换 7921，且必须两处同步——代理 `CTYUN_LISTEN_PORT` 默认值 + config `:613` baseURL；7920 可用则不动。

---

## Task 1 (tier B, executor) — 测试先行（TDD 红相）

**目标**：新建 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`（完整代码如下），实跑确认红相（代理文件尚不存在）。

**死清单**：仅创建这 1 个文件，内容逐字照抄下方代码块；不改其他任何文件。

```python
#!/usr/bin/env python3
"""Tests for ctyun-stream-fix-proxy.py（纯 stdlib，Python >= 3.9）。

起 stdlib 假上游（127.0.0.1:0 随机端口），经 CTYUN_UPSTREAM_BASE / CTYUN_LISTEN_PORT
两个测试 seam 把代理指向假上游，断言:
  ①毒流输出不含 data:null  ②以 data: [DONE] 结尾  ③输出精确等于 SSE_A+SSE_B+SSE_DONE 逐字节
  ④/plain（body 含 data:null 毒行变体、Content-Type 非 SSE）经代理字节一致且 Content-Length
    不变——覆盖"仅 SSE 分支剥行、非 SSE 原样透传"分支
  ⑤对照组 poison=False filtered=0 且与直连一致
"""

import http.client
import os
import socket
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_SCRIPT = os.path.join(HERE, "ctyun-stream-fix-proxy.py")

SSE_A = b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
SSE_B = b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
SSE_POISON = b"data:null\n\n"
SSE_DONE = b"data: [DONE]\n\n"

FAKE_SERVERS = []


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    poison = False

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            self.rfile.read(length)
        body = SSE_A + SSE_B
        if self.poison:
            body += SSE_POISON
        body += SSE_DONE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        body = b'data:null\n{"ok":true,"path":"/plain"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def make_fake_upstream(poison: bool) -> int:
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,), {"poison": poison})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1]


def stop_fake_upstreams() -> None:
    for server in FAKE_SERVERS:
        server.shutdown()
        server.server_close()
    del FAKE_SERVERS[:]


def free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def wait_port(port: int, timeout: float = 5.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.05)
    raise AssertionError("port %d not listening within %.1fs" % (port, timeout))


def start_proxy(upstream_port: int, proxy_port: int) -> subprocess.Popen:
    env = dict(os.environ)
    env["CTYUN_UPSTREAM_BASE"] = "http://127.0.0.1:%d" % upstream_port
    env["CTYUN_LISTEN_PORT"] = str(proxy_port)
    proc = subprocess.Popen(
        [sys.executable, PROXY_SCRIPT],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    wait_port(proxy_port)
    return proc


def post_sse(port: int) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    payload = b'{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[]}'
    conn.request("POST", "/v1/chat/completions", body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    status = resp.status
    data = resp.read()
    conn.close()
    assert status == 200, "expected SSE 200, got %d" % status
    return data


def get_plain(port: int):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/plain")
    resp = conn.getresponse()
    status = resp.status
    data = resp.read()
    resp_len = resp.getheader("Content-Length")
    conn.close()
    assert status == 200, "expected /plain 200, got %d" % status
    return data, resp_len


class ProxyLifecycleTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.assertTrue(os.path.exists(PROXY_SCRIPT),
                        "proxy script missing: %s（TDD 红相：先建代理后重跑）" % PROXY_SCRIPT)

    def run_scenario(self, poison: bool):
        upstream_port = make_fake_upstream(poison)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port)
        try:
            sse_via_proxy = post_sse(proxy_port)
            plain_via_proxy, plain_len = get_plain(proxy_port)
            plain_direct, plain_direct_len = get_plain(upstream_port)
            sse_direct = post_sse(upstream_port)
        finally:
            proc.terminate()
            proc.wait(timeout=5)
            stderr = proc.stderr.read().decode("utf-8", "replace")
            stop_fake_upstreams()
        return {
            "sse_via_proxy": sse_via_proxy,
            "sse_direct": sse_direct,
            "plain_via_proxy": plain_via_proxy,
            "plain_len": plain_len,
            "plain_direct": plain_direct,
            "plain_direct_len": plain_direct_len,
            "stderr": stderr,
        }


class PoisonStreamTest(ProxyLifecycleTestCase):
    def test_poison_line_removed_and_stream_intact(self) -> None:
        r = self.run_scenario(poison=True)
        self.assertNotIn(b"data:null", r["sse_via_proxy"])
        self.assertNotIn(b"data: null", r["sse_via_proxy"])
        self.assertTrue(r["sse_via_proxy"].rstrip().endswith(b"data: [DONE]"),
                        "stream must end with data: [DONE], got tail: %r"
                        % r["sse_via_proxy"][-40:])
        self.assertEqual(r["sse_via_proxy"], SSE_A + SSE_B + SSE_DONE,
                         "SSE output must be byte-exact SSE_A+SSE_B+SSE_DONE after "
                         "filtering (adjacent-byte damage forbidden), got: %r"
                         % r["sse_via_proxy"])
        self.assertIn("filtered=1", r["stderr"],
                      "poison scenario must log filtered=1, stderr:\n" + r["stderr"])


class PassThroughTest(ProxyLifecycleTestCase):
    def test_control_stream_and_plain_passthrough(self) -> None:
        r = self.run_scenario(poison=False)
        self.assertEqual(r["sse_via_proxy"], r["sse_direct"])
        self.assertIn("filtered=0", r["stderr"],
                      "clean stream must log filtered=0, stderr:\n" + r["stderr"])
        self.assertEqual(r["plain_via_proxy"], r["plain_direct"])
        self.assertEqual(r["plain_len"], r["plain_direct_len"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
```

**验证命令（红相）**：

```bash
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py; echo "exit=$?"
```

**预期**：2 个测试全失败（`AssertionError: proxy script missing: ...` 或 `FileNotFoundError`），`exit=1`。贴完整 stderr 原文后进 Task 2。

---

## Task 2 (tier B, executor) — 代理实现（TDD 绿相）

**目标**：新建 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`（完整代码如下），重跑 Task 1 测试至全绿。

**死清单**：仅创建这 1 个文件，内容逐字照抄下方代码块；不改其他任何文件。

```python
#!/usr/bin/env python3
"""ctyun-stream-fix-proxy — 本地阻塞式反代，剥除天翼云网关 SSE 流中的非法 data:null 行。

数据流: ZCode -> http://127.0.0.1:7920 (本代理, 剥行) -> UPSTREAM_BASE (原 path prepend)。
纯 stdlib（http.server + http.client + re），全阻塞，Python >= 3.9。
生产默认: UPSTREAM_BASE=https://eaichat.ctyun.cn/ai/platform/v2/cp, 端口 7920;
CTYUN_UPSTREAM_BASE / CTYUN_LISTEN_PORT 两个环境变量仅作测试 seam。
"""

import http.client
import http.server
import os
import re
import sys
import time
import urllib.parse

UPSTREAM_BASE = os.environ.get("CTYUN_UPSTREAM_BASE", "https://eaichat.ctyun.cn/ai/platform/v2/cp")
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("CTYUN_LISTEN_PORT", "7920"))
UPSTREAM_TIMEOUT = 600
POISON_RE = re.compile(rb"^data:\s*null\s*$")
HOP_HEADERS = {"connection", "keep-alive", "proxy-connection",
               "te", "trailer", "transfer-encoding", "upgrade"}
STRIP_HEADERS = HOP_HEADERS | {"host", "content-length", "accept-encoding"}


class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        self._proxy()

    def do_POST(self) -> None:
        self._proxy()

    def do_PUT(self) -> None:
        self._proxy()

    def do_DELETE(self) -> None:
        self._proxy()

    def do_PATCH(self) -> None:
        self._proxy()

    def log_message(self, format: str, *args: object) -> None:
        # 吞掉的是 BaseHTTPRequestHandler 默认访问日志行（send_response 每请求触发一次）：
        # spec 固定单行日志由 _log 输出，默认行属重复噪音。handler 内异常 traceback 走
        # socketserver.handle_error 直接打 stderr，不经本方法，不会被吞。
        pass

    def _proxy(self) -> None:
        started = time.time()
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None

        fwd_headers = {}
        for key, value in self.headers.items():
            lk = key.lower()
            if lk in STRIP_HEADERS:
                continue
            if lk in fwd_headers:
                fwd_headers[lk] = fwd_headers[lk] + ", " + value
            else:
                fwd_headers[lk] = value
        fwd_headers["accept-encoding"] = "identity"

        parsed = urllib.parse.urlparse(UPSTREAM_BASE)
        upstream_path = parsed.path.rstrip("/") + self.path
        try:
            if parsed.scheme == "https":
                conn = http.client.HTTPSConnection(
                    parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
            else:
                conn = http.client.HTTPConnection(
                    parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
            conn.request(self.command, upstream_path, body=body, headers=fwd_headers)
            resp = conn.getresponse()
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            self._log(started, 502, "error", 0)
            return

        content_type = (resp.getheader("Content-Type") or "").lower()
        result = "ok" if resp.status < 400 else "upstream-err"
        if "text/event-stream" in content_type:
            filtered = self._relay_sse(resp)
            self._log(started, resp.status, result, filtered)
        else:
            self._relay_buffered(resp)
            self._log(started, resp.status, result, 0)
        conn.close()

    def _relay_sse(self, resp: http.client.HTTPResponse) -> int:
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        filtered = 0
        pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
        poisoned = False  # 当前 record 内是否命中毒行
        while True:
            line = resp.readline()
            if line in (b"\n", b"\r\n", b""):
                # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                if poisoned:
                    filtered += 1  # 整 record（含终结空行）丢弃，不损伤相邻字节
                else:
                    for buf_line in pending:
                        self.wfile.write(buf_line)
                    if line:
                        self.wfile.write(line)
                    self.wfile.flush()
                pending = []
                poisoned = False
                if line == b"":
                    break
            else:
                if POISON_RE.match(line.rstrip(b"\r\n")):
                    poisoned = True
                pending.append(line)
        return filtered

    def _relay_buffered(self, resp: http.client.HTTPResponse) -> None:
        data = resp.read()
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _reply_502(self, exc: BaseException) -> None:
        payload = ("ctyun-stream-fix-proxy: upstream error: %s\n" % exc).encode("utf-8")
        self.send_response(502)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def _log(self, started: float, status: int, result: str, filtered: int) -> None:
        print("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d"
              % (self.command, self.path, status, time.time() - started, result, filtered),
              file=sys.stderr, flush=True)


def main() -> None:
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.daemon_threads = True
    print("ctyun-stream-fix-proxy listening on %s:%d -> %s"
          % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE), file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
```

**实现要点（与 spec 锚点一一对应，executor 不需另行设计）**：SSE 判定 = 响应 Content-Type 含 `text/event-stream`（大小写不敏感子串）；`_relay_sse` 按 SSE record 级剥除——逐行 `readline()` 缓冲，空行（`b"\n"`/`b"\r\n"`）为 record 终结；record 内任一行 `rstrip(b"\r\n")` 后命中 `POISON_RE` → 整 record（含终结空行）丢弃并计数，否则原字节写出缓冲 + 终结空行并逐 record `flush()`；EOF 残留 record 同规则（含毒行丢弃计数，否则写出）；SSE 响应头剥 `Content-Length`/`Transfer-Encoding`/`Connection`，加 `Connection: close` 并 `close_connection = True`（close-delimited 替代 Content-Length）；非 SSE `_relay_buffered` 读全量按实际字节数重设 `Content-Length`；转发头剥 `HOP_HEADERS` + `host` + `content-length` + `accept-encoding`，强制 `Accept-Encoding: identity`，重复头按 RFC 7230 逗号合并；上游连接失败/超时 → 502 纯文本 + `result=error`；上游 ≥400 照常转发 + `result=upstream-err`；scheme 按 `UPSTREAM_BASE` 解析（生产默认 https→`HTTPSConnection`，测试 seam http→`HTTPConnection`）。

**验证命令（绿相）**：

```bash
/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py; echo "exit=$?"
```

**预期**：`Ran 2 tests ... OK`，`exit=0`。贴完整 stdout。

---

## Task 3 (tier C, implementer) — launchd plist 落盘 + 真机加载

**C 档理由**：plist 内容已写死，但 `launchctl load` / `KeepAlive` 自愈 / 端口监听属真机 launchd 运行时行为，无法在 PLAN 阶段写死验证结果。

**死清单**：仅创建 `/Users/peter/Library/LaunchAgents/com.zcode.ctyun-stream-fix-proxy.plist`，内容逐字照抄；然后按验证命令序列实跑。

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.zcode.ctyun-stream-fix-proxy</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/peter/.local/bin/ctyun-stream-fix-proxy.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>10</integer>
  <key>ProcessType</key>
  <string>Background</string>
  <key>StandardOutPath</key>
  <string>/Users/peter/.local/log/ctyun-fwd.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/peter/.local/log/ctyun-fwd.err</string>
</dict>
</plist>
```

**验证命令（真机）**：

```bash
mkdir -p /Users/peter/.local/bin /Users/peter/.local/log
/usr/bin/python3 --version
plutil -lint /Users/peter/Library/LaunchAgents/com.zcode.ctyun-stream-fix-proxy.plist; echo "lint_exit=$?"
launchctl unload /Users/peter/Library/LaunchAgents/com.zcode.ctyun-stream-fix-proxy.plist 2>/dev/null; echo "unload_exit=$?"
launchctl load -w /Users/peter/Library/LaunchAgents/com.zcode.ctyun-stream-fix-proxy.plist; echo "load_exit=$?"
sleep 2
lsof -iTCP:7920 -sTCP:LISTEN; echo "lsof_exit=$?"
tail -3 /Users/peter/.local/log/ctyun-fwd.err
```

**预期**：`Python 3.9.6`；`plutil -lint` → `...: OK`，`lint_exit=0`；`unload_exit=1`（首次未加载，非错误）或 0（重跑）；`load_exit=0`；`lsof` 输出含 `Python` 且本地地址 `127.0.0.1:7920 (LISTEN)`；`ctyun-fwd.err` 末行含 `ctyun-stream-fix-proxy listening on 127.0.0.1:7920`。验收 ② 达成。

**红线**：`lsof` 无输出或端口被占 → 贴原文，STOP 报告主代理（端口换 7921 属 Global Constraint 8 联动，由主代理决定，不自行改卡）。

---

## Task 4 (tier C, implementer) — config 切换 + 真实上游验证

**C 档理由**：Edit 锚已写死，但真实上游 curl 验证与 ZCode TY Pro 出流确认依赖运行时数据（真实网关、真实 key、真实生成），留白实测。

**死清单**：仅改 `/Users/peter/.zcode/v2/config.json` 的 `:613` 一行；备份 + 验证命令照抄。

**Step 1 — 备份**：

```bash
cp /Users/peter/.zcode/v2/config.json /Users/peter/.zcode/v2/config.json.bak.20260904-ctyun-proxy
ls -la /Users/peter/.zcode/v2/config.json.bak.20260904-ctyun-proxy
```

**Step 2 — 读锚行**（把两行原文贴进报告，用作 Edit old_string）：

```bash
sed -n '612,613p' /Users/peter/.zcode/v2/config.json
```

预期：`:612` 为 TY Pro 的 `apiKey`（JWT，含唯一 `cpToken` 随机段），`:613` 为 `"baseURL": "https://eaichat.ctyun.cn/ai/platform/v2/cp",`。**注意**：baseURL 单行在 `:277` 重复出现，禁止只拿 `:613` 单行做 old_string；必须用 `:612`-`:613` 两行整体做 Edit 锚（apiKey JWT 全局唯一，两行组合唯一）。

**Step 3 — Edit**：old_string = Step 2 读到的两行原文；new_string = 仅第二行 `https://eaichat.ctyun.cn/ai/platform/v2/cp` → `http://127.0.0.1:7920`，第一行原样保留。`:612` apiKey 不动。

**Step 4 — 编辑验证**：

```bash
/usr/bin/python3 -c "import json;json.load(open('/Users/peter/.zcode/v2/config.json'))"; echo "json_exit=$?"
diff /Users/peter/.zcode/v2/config.json.bak.20260904-ctyun-proxy /Users/peter/.zcode/v2/config.json; echo "diff_exit=$?"
grep -n 'eaichat.ctyun.cn' /Users/peter/.zcode/v2/config.json
grep -n '"baseURL": "http://127.0.0.1:7920"' /Users/peter/.zcode/v2/config.json
```

**预期**：`json_exit=0`；diff 仅 `613c613` 一个 hunk（其余 0 差异 → 旧 TY `:277` 及全部其他 provider 未动）；`eaichat` 仅剩 `:277` 一处；新 baseURL 命中 `:613`。

**Step 5 — 真实上游 curl（验收 ③④）**：

```bash
TY_KEY=$(/usr/bin/python3 -c "import json;print(json.load(open('/Users/peter/.zcode/v2/config.json'))['provider']['57b84b95-a300-4b97-9e05-bc71c8fa6096']['options']['apiKey'])")
curl -sN --max-time 120 -X POST http://127.0.0.1:7920/v1/chat/completions -H "Authorization: Bearer $TY_KEY" -H "Content-Type: application/json" -d '{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[{"role":"user","content":"只回复两个字：正常"}]}' | tee /tmp/ctyun-proxy-deepseek.sse | tail -5
grep -c '^data:null' /tmp/ctyun-proxy-deepseek.sse; echo "null_count_exit=$?"
tail -1 /Users/peter/.local/log/ctyun-fwd.err
curl -sN --max-time 120 -X POST http://127.0.0.1:7920/v1/chat/completions -H "Authorization: Bearer $TY_KEY" -H "Content-Type: application/json" -d '{"model":"glm-5.3","stream":true,"messages":[{"role":"user","content":"只回复两个字：正常"}]}' | tee /tmp/ctyun-proxy-glm.sse | tail -3
grep -c '^data:null' /tmp/ctyun-proxy-glm.sse; echo "glm_null_exit=$?"
tail -1 /Users/peter/.local/log/ctyun-fwd.err
curl -sN --max-time 120 -X POST https://eaichat.ctyun.cn/ai/platform/v2/cp/v1/chat/completions -H "Authorization: Bearer $TY_KEY" -H "Content-Type: application/json" -d '{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[{"role":"user","content":"只回复两个字：正常"}]}' | tail -4
```

**预期与判定口径**：deepseek 经代理流 `grep -c '^data:null'` → `0`（exit 1，grep 计数为 0 的正常返回码），末行 `data: [DONE]`，`delta.content` 非空，日志行 `filtered=1`；glm-5.3 经代理 `grep -c` → `0`，日志行 `filtered=0`；直连对照仍复现 `data:null`（证明剥行来自代理而非上游自愈）。采样生成两次，两次流文本不逐字节相同属正常，验收口径 = 结构一致（`[DONE]` 收尾、chunk schema 合法）+ 内容正常，最终行为验收归 Step 6。

**Step 6 — ZCode TY Pro 确认（验收 ⑤）**：实现者不重启 ZCode 进程外服务（config 由 ZCode 读盘），在完成报告中提示用户：重启/新开 ZCode 会话后用 TY Pro（deepseek-v4-pro-0813-oc）发一轮消息确认正常出流、旧 TY 不受影响；用户确认前本卡标 partial。

**Step 7 — 清理临时文件**：

```bash
rm -f /tmp/ctyun-proxy-deepseek.sse /tmp/ctyun-proxy-glm.sse
```

**红线**：curl 连接失败 / 502 → 贴 stdout/stderr 原文 + `tail -20 /Users/peter/.local/log/ctyun-fwd.err`，STOP 报告；`grep -c` 结果非 0 → 立即回滚（`cp` 备份回 config），贴原文，STOP 报告。

---

## Acceptance 对照（spec 5 条 → 卡映射）

- ① 测试全绿 + stdout + exit 0 → Task 2
- ② launchctl load 后 `lsof -iTCP:7920 -sTCP:LISTEN` 有 python3 → Task 3
- ③ 真实 curl 经代理 deepseek 流无 `data:null`、`[DONE]` 收尾 → Task 4 Step 5
- ④ glm-5.3 经代理行为不变（`filtered=0`）→ Task 4 Step 5
- ⑤ ZCode TY Pro 正常出流、旧 TY 不受影响 → Task 4 Step 6（用户确认）

## R31 Evidence

[R31-S1] 问题现场命中：2026-09-04 原始 curl 复现——`deepseek-v4-pro-0813-oc` 的 SSE 流在 `data:[DONE]` 前出现非法 `data:null` 行；ZCode 每 turn 报 `AI_TypeValidationError: expected object, received null (path [])`，retryable=false，turn 必失败（与 spec 同源证据）。

```
$ curl -sN -X POST https://eaichat.ctyun.cn/ai/platform/v2/cp/v1/chat/completions -H "Authorization: Bearer $TY_KEY" -d '{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[...]}' | tail -5
data: {"id":"...","choices":[{"delta":{"content":"...done"}}]}
data:null

data: [DONE]
```

[R31-S2] 根因：天翼云网关仅对该模型流末尾多发一行 `data:null`（同网关 `glm-5.3` 无此行）；AI SDK openai-compatible 解析器对每个 `data:` 负载按 chunk schema 校验，`null` 非法即抛不可重试错误，整 turn 失败。修复面在本地反代剥行，客户端与网关零改动（与 spec 同源证据）。

```
$ curl -sN -X POST https://eaichat.ctyun.cn/ai/platform/v2/cp/v1/chat/completions -H "Authorization: Bearer $TY_KEY" -d '{"model":"glm-5.3","stream":true,"messages":[...]}' | tail -3
data: {"id":"...","choices":[{"delta":{}}]}

data: [DONE]
```
