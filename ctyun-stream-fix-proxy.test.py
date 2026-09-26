#!/usr/bin/env python3
"""Tests for ctyun-stream-fix-proxy.py（纯 stdlib，Python >= 3.9）。

起 stdlib 假上游（127.0.0.1:0 随机端口），经 CTYUN_UPSTREAM_BASE / CTYUN_LISTEN_PORT
两个测试 seam 把代理指向假上游，断言:
  ①毒流输出不含 data:null  ②以 data: [DONE] 结尾  ③输出精确等于 SSE_A+SSE_B+SSE_DONE 逐字节
  ④/plain（body 含 data:null 毒行变体、Content-Type 非 SSE）经代理字节一致且 Content-Length
    不变——覆盖"仅 SSE 分支剥行、非 SSE 原样透传"分支
  ⑤对照组 poison=False filtered=0 且与直连一致
"""

import collections
import contextlib
import http.client
import importlib.util
import io
import json
import os
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
PROXY_SCRIPT = os.path.join(HERE, "ctyun-stream-fix-proxy.py")

SSE_A = b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'
SSE_B = b'data: {"choices":[{"delta":{"content":"B"}}]}\n\n'
SSE_POISON = b"data:null\n\n"
SSE_DONE = b"data: [DONE]\n\n"
SSE_REASONING = b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}\n\n'
SSE_FINISH = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
SSE_FAULT_TAIL = SSE_REASONING + SSE_FINISH + SSE_DONE

FAKE_SERVERS = []
_PROCS = []  # start_proxy 产物的注册表（atexit 兜底清理，防测试中断遗留孤儿）


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    poison = False
    tag = "/plain"  # /plain 响应携带的路径标记，供"上游热切换后路由命中"断言区分
    big = False     # True → 1.2MB 大 SSE 流，供 client-abort 测试把代理写缓冲打穿
    fail_500 = False  # True → do_POST 回 500 JSON（上游 5xx 透传计数测试用）
    empty_stream = False  # True → 每次 POST 回空流（reasoning 后 EOF，无 [DONE]）
    empty_stream_calls = ()  # 1-based 呼叫序号元组：命中则回空流（同 empty_stream 形态）
    blank_stream = False  # True → 空流形态为零字节 body（200 + SSE 头 + 立即 EOF）
    body_override = None  # 非 None → 正常路径 body 用此值（priming 前缀/合法 DONE 场景）
    fault_finish_first = False   # True → 首呼回 SSE_FAULT_TAIL 后断连，次呼正常 body
    fault_finish_stream = False  # True → 每呼回故障尾段（SSE_FAULT_TAIL）
    stall_all = False       # True → 每呼读 body 后 close_connection=True 直接返回（代理 getresponse 抛 http.client.RemoteDisconnected）
    stall_calls = ()        # 1-based 呼叫序号元组：命中则 close_connection=True 不写响应（代理 getresponse 抛 http.client.RemoteDisconnected）
    rst_all = False         # True → 每呼读 body 后 SO_LINGER(1,0) close 强制发 RST（代理 getresponse 抛 ConnectionResetError）
    rst_calls = ()          # 1-based 呼叫序号元组：命中则 SO_LINGER(1,0) close 强制发 RST（同 rst_all 形态）
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流
    bodies = None        # 共享 list：非 None 时按调用序 append 收到的原始请求体 bytes

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            raw = self.rfile.read(length)
            if self.bodies is not None:
                self.bodies.append(raw)
        # --- calls 计上游被请求总次数（含 stall 呼）：append 提前到 stall 检查之前 ---
        if self.calls is not None:
            self.calls.append(1)
        # --- stall 路径：读 body 后静默返回不写响应（close_connection=True，代理 getresponse 抛 RemoteDisconnected）---
        if self.stall_all:
            # 每呼均 stall：读 body 后直接返回，不写任何响应不关连接
            self.close_connection = True
            return
        if self.stall_calls and self.calls is not None:
            call_num = len(self.calls)  # 当前呼叫序号（1-based，append 之后 len 即为序号）
            if call_num in self.stall_calls:
                self.close_connection = True
                return
        # --- end stall ---
        if self.rst_all or (self.rst_calls and self.calls is not None
                            and len(self.calls) in self.rst_calls):
            # SO_LINGER(1,0) close 强制发 RST（非 FIN），代理 getresponse 抛
            # ConnectionResetError；macOS 要求 8 字节 linger struct，int 直传 EINVAL
            self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack("ii", 1, 0))
            self.close_connection = True
            return
        if self.fail_500:
            body = b'{"error":"upstream exploded"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        if self.fault_finish_first:
            if self.calls is not None and len(self.calls) == 1:
                # 首呼：故障尾段（reasoning + finish + [DONE]，无 content/usage）
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    self.wfile.write(SSE_FAULT_TAIL)
                except ConnectionError:
                    pass
                self.close_connection = True
                return
            # 次呼（len(calls) > 1）fall through 到既有正常路径：
            # body_override 非 None 用 override（用例 B/D），否则默认 SSE_A+SSE_B+SSE_DONE
        if self.fault_finish_stream:
            # 每呼回故障尾段（故意无限重试→第二次 final=True fail-open）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(SSE_FAULT_TAIL)
            except ConnectionError:
                pass
            self.close_connection = True
            return
        # 空流路径：empty_stream=True 每呼全空；empty_stream_calls 序号命中则单呼空
        empty_hit = (self.empty_stream_calls and self.calls is not None
                     and len(self.calls) in self.empty_stream_calls)
        if self.empty_stream or empty_hit or (self.calls is not None and len(self.calls) == 1
                                              and self.body_override is None):
            # 空流签名：200 + SSE 头 + 少量 reasoning delta 后无 [DONE] 即 EOF
            # （blank_stream 则零字节）。body_override 场景首呼走正常路径：
            # spec ②③ 的 calls==1 断言要求首响应即 override 内容、不触发重试。
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            if not self.blank_stream:
                try:
                    self.wfile.write(SSE_REASONING)
                except ConnectionError:
                    # 吞掉的是代理侧已放弃读取后的写出端 Broken pipe（预期路径）：
                    # 测试假上游无需留痕，无其他路径可达。
                    pass
            self.close_connection = True
            return
        if self.big:
            self._respond_big_sse()
            return
        if self.body_override is not None:
            body = self.body_override  # 完整 body 替换：调用方自带整段流，不再追加 poison/DONE
        else:
            body = SSE_A + SSE_B
            if self.poison:
                body += SSE_POISON
            body += SSE_DONE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
            self.wfile.write(body)
        except ConnectionError:
            # 吞掉的是 client-abort 测试里代理线程 EPIPE 死掉后不再读上游、本假上游
            # 写出端随之 Broken pipe 的预期路径：测试假上游无需留痕，无其他路径可达。
            pass
        self.close_connection = True

    def _respond_big_sse(self) -> None:
        # 分块慢速流（~64KB/20ms）：一次性大 write 会被环回内核缓冲整体吞掉，
        # 代理感知不到 RST；慢速写保证客户端断开后代理必然在后续块上撞 EPIPE。
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        chunk = b'data: {"chunk":true}\n\n' * 4000
        try:
            for _ in range(60):
                self.wfile.write(chunk)
                self.wfile.flush()
                time.sleep(0.02)
        except ConnectionError:
            # 同上：客户端断开后写出的预期路径，测试假上游无需留痕。
            pass
        self.close_connection = True

    def do_GET(self) -> None:
        body = ('data:null\n{"ok":true,"path":"%s"}' % self.tag).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
                       fail_500: bool = False, empty_stream: bool = False,
                       empty_stream_calls: tuple = (),
                       blank_stream: bool = False, body_override=None,
                       fault_finish_first: bool = False,
                       fault_finish_stream: bool = False,
                       scripted: bool = False, record_bodies: bool = False,
                       stall_all: bool = False, stall_calls: tuple = (),
                       rst_all: bool = False, rst_calls: tuple = ()) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
             "empty_stream": empty_stream, "empty_stream_calls": empty_stream_calls,
             "blank_stream": blank_stream,
             "body_override": body_override,
             "fault_finish_first": fault_finish_first,
             "fault_finish_stream": fault_finish_stream,
             "stall_all": stall_all,
             "stall_calls": stall_calls,
             "rst_all": rst_all,
             "rst_calls": rst_calls,
             "bodies": [] if record_bodies else None}
    if scripted:
        attrs["calls"] = []
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,), attrs)
    # stall 变体使用 ThreadingHTTPServer：单线程 HTTPServer 的 serve_forever 会卡死
    # 在 stall 连接上，shutdown 挂测试；daemon_threads=True 保证 teardown 不阻塞
    use_threading = stall_all or stall_calls or rst_all or rst_calls
    if use_threading:
        # RST 变体走 RstUpstreamServer：跳过 std shutdown_request 的 SHUT_WR 阶段，
        # 防止 FIN 在 SO_LINGER(1,0) close 的 RST 之前先到客户端诱发 RemoteDisconnected。
        if rst_all or rst_calls:
            server = RstUpstreamServer(("127.0.0.1", 0), handler)
        else:
            server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        server.daemon_threads = True
    else:
        server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1]


def make_scripted_upstream(**kwargs) -> tuple:
    """带调用计数的假上游：返回 (port, calls)；calls 按上游被请求次数 append。
    kwargs 透传 empty_stream / blank_stream / body_override /
    fault_finish_first / fault_finish_stream（勿传 scripted/poison）。"""
    port = make_fake_upstream(False, scripted=True, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls


def make_stall_upstream(stall_all: bool = False, stall_calls: tuple = (), **kwargs) -> tuple:
    """带调用计数的 stall 假上游：返回 (port, calls)。
    stall_all/stall_calls 控制哪些呼叫不写响应直接返回。
    其余 kwargs 透传 empty_stream / body_override 等。"""
    port = make_fake_upstream(False, scripted=True,
                              stall_all=stall_all, stall_calls=stall_calls, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls


def make_rst_upstream(rst_all: bool = False, rst_calls: tuple = (), **kwargs) -> tuple:
    """带调用计数的 RST 假上游：返回 (port, calls)。
    rst_all/rst_calls 控制哪些呼叫在读 body 后 SO_LINGER(1,0) close 强制发 RST。
    其余 kwargs 透传 empty_stream / body_override 等。"""
    port = make_fake_upstream(False, scripted=True,
                              rst_all=rst_all, rst_calls=rst_calls, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls


class RstUpstreamServer(ThreadingHTTPServer):
    def shutdown_request(self, request):
        # std 流程 shutdown(SHUT_WR) 的 FIN 先于 SO_LINGER(1,0) close 的 RST，
        # 客户端可能先读 EOF 抛 RemoteDisconnected；跳过 shutdown 保纯 RST。
        self.close_request(request)


def make_body_recording_upstream(**kwargs) -> tuple:
    """记录上游收到的原始请求体：返回 (port, bodies)；正常路径响应 SSE_A+SSE_B+DONE。"""
    port = make_fake_upstream(False, record_bodies=True, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.bodies


def stop_fake_upstreams() -> None:
    for server in FAKE_SERVERS:
        server.shutdown()
        server.server_close()
    del FAKE_SERVERS[:]


def _drain_pipe(pipe, buf: list) -> None:
    # 吞掉的是 pipe 读取中的 OSError（子进程退出后读端关闭等）：drain 是防"管道满
    # 阻塞子进程 stderr write"的 best-effort，读端异常即终止，无恢复路径。
    try:
        for chunk in iter(pipe.readline, b""):
            buf.append(chunk)
    except OSError:
        return


def stderr_text(proc) -> str:
    """drain buffer 的文本视图（运行期持续收集，替代 terminate 后一次性 pipe.read）。"""
    return b"".join(proc.stderr_buf).decode("utf-8", "replace")


def kill_registered(timeout: float = 2.0) -> list:
    """递进清理全部注册过的代理子进程（terminate → wait → kill）；返回被强杀的 pid。"""
    force_killed = []
    for proc in _PROCS:
        if proc.poll() is not None:
            continue
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                # SIGKILL 已发出，内核回收只是调度时序问题，无其他路径可达。
                pass
            force_killed.append(proc.pid)
    del _PROCS[:]
    return force_killed


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


def start_proxy(upstream_port: int, proxy_port: int, extra_env: dict = None,
                seed_persist: dict = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["CTYUN_UPSTREAM_BASE"] = "http://127.0.0.1:%d" % upstream_port
    env["CTYUN_LISTEN_PORT"] = str(proxy_port)
    # admin/persist seam：与真实 7921 端口、真实持久化文件（~/.local/etc/）完全隔离
    env["CTYUN_ADMIN_HOST"] = "127.0.0.1"
    admin_port = free_port()
    env["CTYUN_ADMIN_PORT"] = str(admin_port)
    persist_dir = tempfile.mkdtemp(prefix="ctyun-proxy-test-")
    env["CTYUN_PERSIST_PATH"] = os.path.join(persist_dir, "settings.json")
    if extra_env:
        env.update(extra_env)
    if seed_persist is not None:
        # 在进程启动前预写持久化文件（计数续算测试用）
        with open(env["CTYUN_PERSIST_PATH"], "w", encoding="utf-8") as fh:
            json.dump(seed_persist, fh)
    proc = subprocess.Popen(
        [sys.executable, PROXY_SCRIPT],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout_buf = []
    proc.stderr_buf = []
    for pipe, buf in ((proc.stdout, proc.stdout_buf), (proc.stderr, proc.stderr_buf)):
        threading.Thread(target=_drain_pipe, args=(pipe, buf), daemon=True).start()
    _PROCS.append(proc)
    wait_port(proxy_port)
    wait_port(admin_port)
    proc.proxy_port = proxy_port
    proc.admin_port = admin_port
    proc.persist_dir = persist_dir
    return proc


def post_sse(port: int, payload: bytes = None) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    if payload is None:
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
            stderr = stderr_text(proc)
            shutil.rmtree(proc.persist_dir, ignore_errors=True)
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


def load_proxy_module():
    """in-process 载入代理模块做纯函数单测（模块顶层只定义/读 env，无副作用）。"""
    spec = importlib.util.spec_from_file_location("ctyun_stream_fix_proxy", PROXY_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def admin_get(port: int, path: str, headers: dict = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {}
    if headers:
        hdrs.update(headers)
    conn.request("GET", path, headers=hdrs)
    resp = conn.getresponse()
    out = (resp.status, resp.read(), resp.getheader("Content-Type"))
    conn.close()
    return out


def admin_post(port: int, path: str, body: bytes, headers: dict = None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    conn.request("POST", path, body=body, headers=hdrs)
    resp = conn.getresponse()
    out = (resp.status, resp.read())
    conn.close()
    return out


class ProxyDashboardUnitTest(unittest.TestCase):
    """纯函数单测：in-process 载入模块，不经 socket。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_proxy_module()

    def test_valid_upstream_url(self) -> None:
        f = self.mod.valid_upstream_url
        self.assertTrue(f("https://eaichat.ctyun.cn/ai/platform/v2/cp"))
        self.assertTrue(f("http://127.0.0.1:8000"))
        self.assertFalse(f("ftp://example.com/x"))
        self.assertFalse(f("https://"))
        self.assertFalse(f(""))
        self.assertFalse(f("not a url"))

    def test_resolve_upstream_base_precedence(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        good = os.path.join(tmp, "good.json")
        mod.persist_upstream("http://127.0.0.1:9999", good)
        self.assertEqual(mod.resolve_upstream_base("http://env:1", good),
                         ("http://env:1", "env"))
        self.assertEqual(mod.resolve_upstream_base("", good),
                         ("http://127.0.0.1:9999", "file"))
        bad = os.path.join(tmp, "bad.json")
        with open(bad, "w", encoding="utf-8") as fh:
            fh.write("{corrupt json")
        self.assertEqual(mod.resolve_upstream_base("", bad),
                         (mod.DEFAULT_UPSTREAM_BASE, "default"))
        self.assertEqual(mod.resolve_upstream_base("", os.path.join(tmp, "missing.json")),
                         (mod.DEFAULT_UPSTREAM_BASE, "default"))
        invalid = os.path.join(tmp, "invalid.json")
        with open(invalid, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "ftp://nope"}, fh)
        self.assertEqual(mod.resolve_upstream_base("", invalid),
                         (mod.DEFAULT_UPSTREAM_BASE, "default"))

    def test_write_allowed_matrix(self) -> None:
        f = self.mod.write_allowed
        self.assertTrue(f("127.0.0.1", "", ""))
        self.assertTrue(f("::1", "", ""))
        self.assertFalse(f("192.168.1.5", "", ""))
        self.assertFalse(f("192.168.1.5", "", "tok"))
        self.assertFalse(f("192.168.1.5", "tok", ""))
        self.assertTrue(f("192.168.1.5", "tok", "tok"))
        self.assertFalse(f("192.168.1.5", "wrong", "tok"))

    def test_extract_model(self) -> None:
        f = self.mod.extract_model
        self.assertEqual(f(b'{"model":"deepseek-v4-pro-0813-oc","stream":true}'),
                         "deepseek-v4-pro-0813-oc")
        self.assertIsNone(f(b'{"messages":[]}'))
        self.assertIsNone(f(b"not json"))
        self.assertIsNone(f(b""))
        self.assertIsNone(f(b"[1,2,3]"))
        self.assertIsNone(f(b'{"model":123}'))
        self.assertIsNone(f(b'{"model":""}'))

    def test_normalize_null_assistant_content_basic(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = json.dumps({"model": "glm-5.3-oc", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        ]}).encode("utf-8")
        data = json.loads(f(body).decode("utf-8"))
        self.assertEqual(data["messages"][1]["content"], "")
        self.assertEqual(data["messages"][1]["tool_calls"], [{"id": "c1"}])
        self.assertEqual(data["messages"][0]["content"], "hi")

    def test_normalize_identity_without_null(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = b'{"model":"glm-5.3-oc","messages":[{"role":"assistant","content":"x"}]}'
        self.assertIs(f(body), body)  # 同一对象：字节级透传

    def test_normalize_mixed_only_assistant_null(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = json.dumps({"messages": [
            {"role": "user", "content": None},        # user null 不动
            {"role": "assistant", "content": None},   # 唯一改动点
            {"role": "assistant", "content": "done"},  # 非 null 不动
            "not-a-dict", 42,                          # 非 dict 元素跳过
        ]}).encode("utf-8")
        msgs = json.loads(f(body).decode("utf-8"))["messages"]
        self.assertIsNone(msgs[0]["content"])
        self.assertEqual(msgs[1]["content"], "")
        self.assertEqual(msgs[2]["content"], "done")
        self.assertEqual(msgs[3], "not-a-dict")
        self.assertEqual(msgs[4], 42)

    def test_normalize_invalid_json_passthrough(self) -> None:
        f = self.mod.normalize_null_assistant_content
        for body in (b"not json", b"[1,2,3]", b'{"messages":"oops"}'):
            self.assertIs(f(body), body)

    def test_normalize_none_body_passthrough(self) -> None:
        self.assertIsNone(self.mod.normalize_null_assistant_content(None))

    def test_normalize_missing_messages_passthrough(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = b'{"model":"glm-5.3-oc","stream":true}'
        self.assertIs(f(body), body)

    def test_normalize_missing_content_key_untouched(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = b'{"messages":[{"role":"assistant","tool_calls":[{"id":"c1"}]}]}'
        self.assertIs(f(body), body)  # 键不存在 != content:null

    def test_normalize_non_assistant_null_untouched(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = json.dumps({"messages": [
            {"role": "tool", "content": None},
            {"role": "user", "content": None},
        ]}).encode("utf-8")
        self.assertIs(f(body), body)  # 无改动 -> 原 bytes 对象

    def test_sse_data_line_kind_matrix(self) -> None:
        f = self.mod.sse_data_line_kind
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\n'), "content")
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\r\n'), "content")
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":""}}]}\n'), "noise")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1"}]},"index":0}]}\n'),
            "content")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'), "finish")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}\n'), "noise")
        self.assertEqual(f(b"data: [DONE]\n"), "done")
        self.assertEqual(f(b"data:[DONE]\r\n"), "done")
        self.assertEqual(f(b": keep-alive comment\n"), "noise")
        self.assertEqual(f(b"event: message\n"), "noise")
        self.assertEqual(f(b"id: 42\n"), "noise")
        self.assertEqual(f(b"data: null\n"), "noise")
        self.assertEqual(f(b'data: {"choices":[]}\n'), "noise")
        self.assertEqual(f(b'data: {"id":"x","choices":[{"delta":{}}]}\n'), "noise")
        self.assertEqual(f(b'data: {"usage":{"total_tokens":9},"choices":[]}\n'), "noise")
        self.assertEqual(f(b"data: [1,2,3]\n"), "noise")
        self.assertEqual(f(b"data: {not-json\n"), "content",
                         "非 JSON data 行 fail-open 归 content（宁漏不误）")
        # content + finish 同帧 → "content"（delta.content 优先）
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"content":"x"},"finish_reason":"stop"}]}\n'),
            "content")
        # tool_calls + finish 同帧 → "content"（delta.tool_calls 优先）
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"tool_calls":[{"id":"t1"}]},'
              b'"finish_reason":"stop"}]}\n'), "content")

    def test_sse_line_has_usage_matrix(self) -> None:
        f = self.mod.sse_line_has_usage
        # 真：独立 usage 帧（choices=[]）
        self.assertTrue(f(b'data: {"id":"u","choices":[],'
                          b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n'))
        # 真：ride-on finish 帧（choices 有 finish + usage 顶层）
        self.assertTrue(f(b'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}],'
                          b'"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n'))
        # 假：空 usage {}
        self.assertFalse(f(b'data: {"id":"e","choices":[],"usage":{}}\n'))
        # 假：usage: null
        self.assertFalse(f(b'data: {"id":"n","choices":[],"usage":null}\n'))
        # 假：[DONE]
        self.assertFalse(f(b"data: [DONE]\n"))
        # 假：非 JSON
        self.assertFalse(f(b"data: {not json\n"))
        # 假：非 data 行
        self.assertFalse(f(b": keep-alive\n"))
        # 假：空 data
        self.assertFalse(f(b"data:\n"))
        # 假：usage 非 dict（如列表）
        self.assertFalse(f(b'data: {"usage":[1,2,3],"choices":[]}\n'))

    def test_stats_persist_roundtrip_and_defaults(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0, "eof_without_done_total": 0,
                          "finish_retries_total": 0, "header_retries_total": 0})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0, "eof_without_done_total": 0,
                          "finish_retries_total": 0, "header_retries_total": 0})
        mod._record_request("POST", "/x", 200, 1.0, 2)
        mod.save_stats_counters(path)
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertIn("upstream_base", data)
        # 断言与内存态一致（模块级计数被同类其他测试累积，不能断绝对值）
        expected = mod.STATS["filtered_total"]
        self.assertEqual(data["stats"]["filtered_total"], expected)
        self.assertEqual(mod.load_stats_counters(path)["filtered_total"], expected)
        legacy = os.path.join(tmp, "legacy.json")
        with open(legacy, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://x"}, fh)
        self.assertEqual(mod.load_stats_counters(legacy)["requests_total"], 0)

    def test_daily_by_model_cap(self) -> None:
        mod = self.mod
        for i in range(40):
            mod._record_request("POST", "/c", 200, 1.0, 0, model="m-%02d" % i)
        self.assertLessEqual(len(mod.STATS["daily_by_model"][mod.today_key()]), 32)
        self.assertNotIn("m-39", mod.STATS["daily_by_model"][mod.today_key()])

    def test_stats_snapshot_shape(self) -> None:
        mod = self.mod
        mod._record_request("POST", "/x-err", 502, 1.0, 0, error=True)
        mod._record_request("POST", "/x", 200, 12.0, 1)
        mod._record_poison_preview(b"data:null")
        snap = mod.stats_snapshot()
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "eof_without_done_total",
                    "finish_retries_total",
                    "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews", "events"):
            self.assertIn(key, snap)
        self.assertIsInstance(snap["uptime_s"], int)
        self.assertIsInstance(snap["recent"], list)
        self.assertIsInstance(snap["poison_previews"], list)
        self.assertIsInstance(snap["events"], list)
        self.assertGreaterEqual(snap["filtered_total"], 1)
        self.assertGreaterEqual(snap["recent"][-1]["filtered"], 1)
        self.assertIn("data:null", snap["poison_previews"][-1]["preview"])
        # events：error 请求必入流；元素含 ts/kind；oldest→newest 与 recent 同序
        self.assertGreaterEqual(len(snap["events"]), 1)
        for e in snap["events"]:
            self.assertIn("ts", e)
            self.assertIn("kind", e)
        # 副本断言（对齐 test_stats_snapshot_daily_is_copy 模式）：
        # 改 snap["events"] 不得影响模块级 EVENTS
        n_before = len(mod.EVENTS)
        snap["events"].append({"ts": 0, "kind": "proxy", "model": None, "status": 0})
        self.assertEqual(len(mod.EVENTS), n_before,
                         "snapshot events must be a copy, not the live deque")

    def test_range_bounds_calendar_edges(self) -> None:
        f = self.mod.range_bounds
        # 月初切片：mtd 仅当日；上月=完整 2 月（2026 非闰年 28 天）
        self.assertEqual(f("2026-03-01", "mtd"), ("2026-03-01", "2026-03-01"))
        self.assertEqual(f("2026-03-01", "last_month"), ("2026-02-01", "2026-02-28"))
        # 跨年：1 月初的上月 = 上年 12 月整月
        self.assertEqual(f("2026-01-01", "last_month"), ("2025-12-01", "2025-12-31"))
        # 闰日 today：mtd 含 02-29；闰年 2 月整月（2024 闰）
        self.assertEqual(f("2024-02-29", "mtd"), ("2024-02-01", "2024-02-29"))
        self.assertEqual(f("2024-03-31", "last_month"), ("2024-02-01", "2024-02-29"))
        # 滑动窗口含今日
        self.assertEqual(f("2026-03-01", "3d"), ("2026-02-27", "2026-03-01"))
        self.assertEqual(f("2026-03-01", "7d"), ("2026-02-23", "2026-03-01"))
        with self.assertRaises(ValueError):
            f("2026-03-01", "30d")

    def test_aggregate_daily_range_sums_and_days(self) -> None:
        f = self.mod.aggregate_daily_range
        daily = {
            "2026-02-01": {"requests": 3, "filtered": 1, "errors_proxy": 0,
                           "errors_upstream": 1, "retries": 0},
            "2026-02-02": {"requests": 5},  # 缺字段桶：缺按 0
            "2026-03-01": {"requests": 7, "filtered": 2, "errors_proxy": 1,
                           "errors_upstream": 0, "retries": 4},  # 窗口外
        }
        out = f(daily, "2026-02-01", "2026-02-28")
        self.assertEqual(out, {"requests": 8, "filtered": 1, "errors_proxy": 0,
                               "errors_upstream": 1, "retries": 0,
                               "eof_without_done": 0, "header_retries": 0,
                               "days": 2})
        # 空窗口：全 0 + days=0
        self.assertEqual(f(daily, "2025-01-01", "2025-01-31"),
                         {"requests": 0, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0,
                          "eof_without_done": 0, "header_retries": 0,
                          "days": 0})
        # 端点闭合：start/end 当天都计入
        self.assertEqual(f(daily, "2026-02-02", "2026-02-02")["days"], 1)
        self.assertEqual(f(daily, "2026-02-02", "2026-02-02")["requests"], 5)

    def test_range_stats_all_four_keys(self) -> None:
        mod = self.mod
        daily = {"2026-02-28": {"requests": 2, "filtered": 1, "errors_proxy": 0,
                                "errors_upstream": 0, "retries": 0},
                 "2026-03-01": {"requests": 4, "filtered": 0, "errors_proxy": 1,
                                "errors_upstream": 0, "retries": 0}}
        plan = mod.range_stats(daily, today="2026-03-01")
        self.assertEqual(set(plan), {"stats", "bounds"})
        self.assertEqual(set(plan["stats"]), set(mod.RANGE_KEYS),
                         "range_stats must cover exactly the four RANGE_KEYS")
        self.assertEqual(set(plan["bounds"]), set(mod.RANGE_KEYS))
        for key in mod.RANGE_KEYS:
            self.assertEqual(set(plan["stats"][key]),
                             {"requests", "filtered", "errors_proxy",
                              "errors_upstream", "retries", "eof_without_done",
                              "header_retries", "days"})
            self.assertEqual(len(plan["bounds"][key]), 2)
        # 3d 窗口 = [02-27, 03-01]：两天桶都在窗内
        self.assertEqual(plan["stats"]["3d"]["requests"], 6)
        self.assertEqual(plan["stats"]["3d"]["days"], 2)
        # mtd 窗口 = [03-01, 03-01]：仅当日桶
        self.assertEqual(plan["stats"]["mtd"]["requests"], 4)
        self.assertEqual(plan["stats"]["mtd"]["days"], 1)
        # last_month = [02-01, 02-28]：仅 02-28 桶
        self.assertEqual(plan["stats"]["last_month"]["requests"], 2)
        self.assertEqual(plan["bounds"]["7d"], ["2026-02-23", "2026-03-01"])

    def test_stats_snapshot_includes_range_stats(self) -> None:
        mod = self.mod
        today = mod.today_key()
        bucket = mod.STATS["daily"].setdefault(
            today, {"requests": 0, "filtered": 0, "errors_proxy": 0,
                    "errors_upstream": 0, "retries": 0})
        base = dict(bucket)
        mod._record_request("POST", "/rs", 200, 1.0, 1)
        snap = mod.stats_snapshot()
        self.assertEqual(bucket["requests"], base["requests"] + 1)
        self.assertEqual(set(snap["range_stats"]), set(mod.RANGE_KEYS))
        self.assertEqual(set(snap["range_bounds"]), set(mod.RANGE_KEYS))
        # 当日桶落在含今日的窗口内：7d/mtd 的 requests 恰等于窗内桶求和（独立 oracle：
        # 测试侧自行按 bounds 字符串过滤 snap["daily"] 求和，不复用被测聚合实现）
        for key in ("7d", "mtd"):
            start, end = snap["range_bounds"][key]
            self.assertTrue(start <= today <= end,
                            "%s window must include today" % key)
            expected = sum(b.get("requests", 0)
                           for k, b in snap["daily"].items() if start <= k <= end)
            self.assertEqual(snap["range_stats"][key]["requests"], expected)
            self.assertGreaterEqual(snap["range_stats"][key]["requests"],
                                    base["requests"] + 1)

    def test_daily_bucket_accumulation_and_dual_error_semantics(self) -> None:
        mod = self.mod
        today = mod.today_key()
        bucket = mod.STATS["daily"].setdefault(
            today, {"requests": 0, "filtered": 0, "errors_proxy": 0,
                    "errors_upstream": 0, "retries": 0})
        base = dict(bucket)
        err_base = mod.STATS["errors_total"]
        mod._record_request("POST", "/d1", 200, 1.0, 2)
        mod._record_request("POST", "/d2", 502, 1.0, 0, error=True)
        mod._record_request("POST", "/d3", 500, 1.0, 0)
        mod._record_request("POST", "/d4", 499, 1.0, 0)
        self.assertEqual(bucket["requests"], base["requests"] + 4)
        self.assertEqual(bucket["filtered"], base["filtered"] + 2)
        self.assertEqual(bucket["errors_proxy"], base["errors_proxy"] + 1,
                         "error=True (proxy-made 502) must land in errors_proxy only")
        self.assertEqual(bucket["errors_upstream"], base["errors_upstream"] + 1,
                         "upstream 500 passthrough must land in errors_upstream only")
        self.assertEqual(mod.STATS["errors_total"], err_base + 1,
                         "errors_total semantics unchanged (proxy errors only)")
        self.assertEqual(bucket["errors_upstream"], base["errors_upstream"] + 1,
                         "499 aborted must not inflate errors_upstream")

    def test_daily_bucket_spans_days(self) -> None:
        mod = self.mod
        today = mod.today_key()
        orig = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-02"
            mod._record_request("POST", "/span", 200, 1.0, 1)
        finally:
            mod.today_key = orig
        self.assertIn("2026-01-02", mod.STATS["daily"])
        self.assertIn(today, mod.STATS["daily"])
        self.assertIsNot(mod.STATS["daily"]["2026-01-02"], mod.STATS["daily"][today])

    def test_daily_by_model_matrix_fallback(self) -> None:
        mod = self.mod
        today = mod.today_key()
        # 清当日桶自洽化：setUpClass 共享 mod，cap 测试可能已把当日 daily_by_model
        # 填满 32 键，m-a 会被每日 cap 挡掉（旧序靠先跑测试放入 m-a 的跨测试
        # 隐式耦合，改名后暴露）。
        mod.STATS["daily_by_model"][today] = {}
        mod._record_request("POST", "/dm", 200, 1.0, 1, model="m-a")
        before = set(mod.STATS["daily_by_model"].get(today, {}))
        mod._record_request("POST", "/dm", 200, 1.0, 0)  # model=None：只进 daily 总桶
        self.assertEqual(set(mod.STATS["daily_by_model"].get(today, {})), before,
                         "model=None requests must not enter daily_by_model")
        mod._record_empty_retry("m-a")
        dm_today = mod.STATS["daily_by_model"][today]
        self.assertGreaterEqual(dm_today["m-a"]["requests"], 1)
        self.assertGreaterEqual(dm_today["m-a"]["filtered"], 1)
        self.assertGreaterEqual(dm_today["m-a"]["retries"], 1,
                                "retry attribution must land in daily_by_model")
        # v3：dm entry 恒 7 字段 + 错误归因与 daily 总桶同口径（error→proxy，5xx→upstream）
        self.assertEqual(
            set(dm_today["m-a"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries",
             "eof_without_done", "header_retries"},
            "dm entry shape must stay in sync across both creation sites")
        mod._record_request("POST", "/dm", 502, 1.0, 0, model="m-a", error=True)
        self.assertEqual(dm_today["m-a"]["errors_proxy"], 1,
                         "error=True (proxy-made 502) must land in dm errors_proxy")
        mod._record_request("POST", "/dm", 500, 1.0, 0, model="m-a")
        self.assertEqual(dm_today["m-a"]["errors_upstream"], 1,
                         "upstream 500 passthrough must land in dm errors_upstream")
        before_499 = set(dm_today)
        mod._record_request("POST", "/dm", 499, 1.0, 0)  # 499 中断 model=None
        self.assertEqual(set(mod.STATS["daily_by_model"][today]), before_499,
                         "499 aborted (model=None) must not enter daily_by_model")
        self.assertEqual(dm_today["m-a"]["errors_proxy"], 1,
                         "499 must inflate neither dm error column")
        self.assertEqual(dm_today["m-a"]["errors_upstream"], 1)
        # 恒等式：daily 总桶 ≥ 分模型合计（差值 = 当日无 model 请求）
        for k in ("requests", "retries"):
            total = mod.STATS["daily"][today][k]
            summed = sum(m[k] for m in dm_today.values())
            self.assertGreaterEqual(total, summed,
                                    "daily[%r][%r]=%d < Σ daily_by_model=%d"
                                    % (today, k, total, summed))
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 6 字段——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries",
             "eof_without_done", "header_retries"},
            "empty-retry creation site must keep dm entry shape in sync")

    def test_events_record_and_cap(self) -> None:
        mod = self.mod
        orig_events = mod.EVENTS
        mod.EVENTS = collections.deque(maxlen=100)
        try:
            # 分类优先级与计数口径一致：error=True → proxy（status≥500 时 error 胜出）；
            # 无 error 的 500/502 → upstream；_record_empty_retry → retry
            mod._record_request("POST", "/e1", 502, 1.0, 0, error=True)
            mod._record_request("POST", "/e2", 500, 1.0, 0)
            mod._record_empty_retry("m-a")
            snap = mod.stats_snapshot()
            self.assertEqual([e["kind"] for e in snap["events"]],
                             ["proxy", "upstream", "retry"])
            self.assertEqual(snap["events"][0]["status"], 502)
            self.assertIsNone(snap["events"][1]["model"])
            self.assertEqual(snap["events"][2]["model"], "m-a")
            self.assertIsNone(snap["events"][2]["status"])
            for e in snap["events"]:
                self.assertIn("ts", e)
                self.assertIsInstance(e["ts"], float)
            # cap：再记 120 条 → 恰留最新 100，最老（proxy/upstream）被丢
            for _ in range(120):
                mod._record_empty_retry()
            snap = mod.stats_snapshot()
            self.assertEqual(len(snap["events"]), 100)
            self.assertEqual(len(mod.EVENTS), 100)
            kinds = [e["kind"] for e in snap["events"]]
            self.assertNotIn("proxy", kinds, "oldest events must be dropped by maxlen")
            self.assertNotIn("upstream", kinds)
        finally:
            mod.EVENTS = orig_events

    def test_daily_persist_roundtrip_legacy_and_corrupt(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit3-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://x",
                       "stats": {"requests_total": 1}}, fh)
        self.assertEqual(mod.load_daily_buckets(path), {},
                         "legacy file without daily key must yield {}")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": "not-a-dict"}}, fh)
        self.assertEqual(mod.load_daily_buckets(path), {})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": {
                "2026-01-01": "bad",
                "2026-01-02": {"requests": 5, "filtered": 1,
                               "errors_proxy": 0, "errors_upstream": 2}}}}, fh)
        buckets = mod.load_daily_buckets(path)
        self.assertNotIn("2026-01-01", buckets, "non-dict bucket must be skipped")
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(buckets["2026-01-02"]["errors_upstream"], 2)

    def test_events_persist_roundtrip(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit8-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_events = mod.EVENTS
        try:
            # 用例 1：save→load roundtrip 全等（oldest→newest 保序；main() 恢复路径的
            # 等价操作序列）
            mod.EVENTS = collections.deque([
                {"ts": 1757654300.1, "kind": "proxy", "model": None, "status": 502},
                {"ts": 1757654301.2, "kind": "upstream", "model": "m-1", "status": 500},
                {"ts": 1757654302.3, "kind": "retry", "model": "m-2", "status": None}],
                maxlen=100)
            mod.save_stats_counters(path)
            self.assertEqual(mod.load_stats_events(path), list(mod.EVENTS),
                             "save->load roundtrip must restore events verbatim")
        finally:
            mod.EVENTS = orig_events
        # 用例 2：legacy 文件无 events 键 → []
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://x",
                       "stats": {"requests_total": 1}}, fh)
        self.assertEqual(mod.load_stats_events(path), [],
                         "legacy file without events key must yield []")
        # 用例 3：坏 entry 逐项跳过（kind 非法 / ts<0 / model 空 / model>200 /
        # status 越界 / 非 dict entry）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": 1.0, "kind": "bogus", "model": None, "status": None},
                {"ts": -1.0, "kind": "retry", "model": None, "status": None},
                {"ts": 2.0, "kind": "retry", "model": "", "status": None},
                {"ts": 3.0, "kind": "retry", "model": "x" * 201, "status": None},
                {"ts": 4.0, "kind": "proxy", "model": None, "status": 99},
                {"ts": 5.0, "kind": "proxy", "model": None, "status": 600},
                "not-a-dict",
                {"ts": 6.0, "kind": "upstream", "model": "m-ok", "status": 503}]}}, fh)
        self.assertEqual(mod.load_stats_events(path),
                         [{"ts": 6.0, "kind": "upstream", "model": "m-ok", "status": 503}],
                         "malformed entries must be skipped individually")
        # 用例 4：150 条 → 读回最新 100（与 deque maxlen 对齐）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": float(i), "kind": "retry", "model": None, "status": None}
                for i in range(150)]}}, fh)
        loaded = mod.load_stats_events(path)
        self.assertEqual(len(loaded), 100)
        self.assertEqual(loaded[0]["ts"], 50.0, "only the newest 100 must survive")
        self.assertEqual(loaded[-1]["ts"], 149.0)

    def test_daily_by_model_persist_roundtrip(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit7-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        try:
            # 用例 1：save→load roundtrip 全等（main() 重启恢复路径的等价操作序列）
            matrix = {
                "2026-01-02": {
                    "m1": {"requests": 3, "filtered": 5, "errors_proxy": 1,
                           "errors_upstream": 2, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4, "eof_without_done": 0,
                           "header_retries": 0}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0}}}
            mod.STATS["daily_by_model"] = matrix
            mod.save_stats_counters(path)
            self.assertEqual(mod.load_daily_by_model_buckets(path), matrix,
                             "save->load roundtrip must restore the matrix verbatim")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
        # 用例 2：legacy 文件无 daily_by_model 键 → {}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"upstream_base": "http://x",
                       "stats": {"requests_total": 1}}, fh)
        self.assertEqual(mod.load_daily_by_model_buckets(path), {},
                         "legacy file without daily_by_model key must yield {}")
        # 用例 3：损坏结构逐项容错（顶层非 dict / 日期桶非 dict / entry 非 dict / 坏字段→0）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": "not-a-dict"}}, fh)
        self.assertEqual(mod.load_daily_by_model_buckets(path), {},
                         "non-dict daily_by_model must yield {}")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "2026-01-01": "bad",
                "2026-01-02": {"m1": "bad",
                               "m2": {"requests": 5, "filtered": -1,
                                      "errors_proxy": "x",
                                      "errors_upstream": 2}}}}}, fh)
        self.assertEqual(mod.load_daily_by_model_buckets(path),
                         {"2026-01-02": {"m2": {"requests": 5, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 2, "retries": 0,
                                                "eof_without_done": 0,
                                                "header_retries": 0}}},
                         "non-dict bucket/entry must be skipped; bad fields coerced to 0")
        # 用例 4：非 ISO 日期 key 跳过（round-trip 校验，版本无关）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "20260101": {"m1": {"requests": 1, "filtered": 0,
                                    "errors_proxy": 0, "errors_upstream": 0, "retries": 0}},
                "not-a-date": {"m1": {"requests": 1, "filtered": 0,
                                      "errors_proxy": 0, "errors_upstream": 0, "retries": 0}},
                "2026-01-03": {"m1": {"requests": 1}}}}}, fh)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(set(dbm), {"2026-01-03"},
                         "non-ISO date keys must be skipped")
        self.assertEqual(dbm["2026-01-03"]["m1"],
                         {"requests": 1, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0, "eof_without_done": 0,
                          "header_retries": 0},
                         "missing fields must be filled with 0")
        # 用例 5：单日 40 模型 → 读回恰 32（文件出现序前 32）
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {"2026-01-04": {
                "m%02d" % i: {"requests": i} for i in range(40)}}}}, fh)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(len(dbm["2026-01-04"]), 32,
                         "model count must be capped at BY_MODEL_CAP on load")
        self.assertIn("m31", dbm["2026-01-04"],
                      "32nd model in file order must survive the cap")
        self.assertNotIn("m32", dbm["2026-01-04"],
                         "models beyond the cap must be dropped")
        self.assertEqual(dbm["2026-01-04"]["m31"]["requests"], 31,
                         "surviving entries must keep their values")

    def test_daily_prune_on_save(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit4-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_daily = mod.STATS["daily"]
        mod.STATS["daily"] = {}
        try:
            for i in range(95):
                mod.STATS["daily"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "requests": i, "filtered": 0,
                    "errors_proxy": 0, "errors_upstream": 0}
            mod.save_stats_counters(path)
            d = mod.STATS["daily"]  # 断言内存态：prune 必须落到 STATS["daily"] 原对象
            self.assertLessEqual(len(d), 90, "in-memory daily must be pruned on save")
            self.assertNotIn("2026-01-01", d, "oldest buckets must be pruned from memory")
            self.assertIn("2026-04-11", d, "most recent bucket must survive prune")
            self.assertEqual(d["2026-04-11"]["requests"], 94, "surviving buckets untouched")
        finally:
            mod.STATS["daily"] = orig_daily
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertLessEqual(len(saved), 90, "prune must cap buckets at 90 days")
        self.assertIn("2026-04-11", saved, "most recent bucket must survive prune")

    def test_daily_prune_exact_boundary(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit6-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_daily = mod.STATS["daily"]
        mod.STATS["daily"] = {}
        try:
            for i in range(90):
                mod.STATS["daily"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "requests": i, "filtered": 0,
                    "errors_proxy": 0, "errors_upstream": 0, "retries": 0}
            mod.save_stats_counters(path)
            self.assertEqual(len(mod.STATS["daily"]), 90,
                             "exactly 90 buckets must hit the <= early-return branch")
            self.assertIn("2026-01-01", mod.STATS["daily"],
                          "at exactly 90 buckets nothing must be pruned")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)["stats"]["daily"]
            self.assertEqual(len(saved), 90,
                             "file must keep all 90 buckets when prune early-returns")
            mod.STATS["daily"]["2026-12-31"] = {
                "requests": 90, "filtered": 0,
                "errors_proxy": 0, "errors_upstream": 0, "retries": 0}
            mod.save_stats_counters(path)
            d = mod.STATS["daily"]
            self.assertEqual(len(d), 90, "91 buckets must prune back to exactly 90")
            self.assertNotIn("2026-01-01", d, "only the oldest bucket must be deleted")
            self.assertIn("2026-01-02", d, "second-oldest bucket must survive")
            self.assertIn("2026-12-31", d, "newest bucket must survive")
        finally:
            mod.STATS["daily"] = orig_daily

    def test_daily_by_model_prune_on_save(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            for i in range(95):
                mod.STATS["daily_by_model"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "m1": {"requests": i, "filtered": 0,
                           "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            today = mod.today_key()
            mod.STATS["daily_by_model"][today] = {
                "m1": {"requests": 95, "filtered": 0,
                       "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            # 内存态断言：prune 必须落到 STATS["daily_by_model"] 原对象
            dbm = mod.STATS["daily_by_model"]
            self.assertLessEqual(len(dbm), 90,
                                 "in-memory daily_by_model must be pruned on save")
            self.assertNotIn("2026-01-01", dbm,
                             "oldest buckets must be pruned from memory")
            self.assertIn(today, dbm, "today bucket must survive prune")
            self.assertEqual(dbm["2026-04-11"]["m1"]["requests"], 94,
                             "surviving inner entries must be untouched")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily_by_model"]
        self.assertLessEqual(len(saved), 90,
                             "file daily_by_model must be pruned to <=90 buckets")
        self.assertNotIn("2026-01-01", saved,
                         "oldest buckets must be pruned from the file")
        self.assertIn(today, saved, "today bucket must survive prune in the file")
        self.assertEqual(saved["2026-04-11"]["m1"]["requests"], 94,
                         "surviving file entries must be untouched")

    def test_daily_by_model_prune_exact_boundary(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            for i in range(90):
                mod.STATS["daily_by_model"]["2026-%02d-%02d" % (1 + i // 28, 1 + i % 28)] = {
                    "m1": {"requests": i, "filtered": 0,
                           "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            self.assertEqual(len(mod.STATS["daily_by_model"]), 90,
                             "exactly 90 buckets must hit the <= early-return branch")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)["stats"]["daily_by_model"]
            self.assertEqual(len(saved), 90,
                             "file must keep all 90 buckets when prune early-returns")
            self.assertIn("2026-01-01", saved,
                          "at exactly 90 buckets nothing must be pruned from the file")
            mod.STATS["daily_by_model"]["2026-12-31"] = {
                "m1": {"requests": 90, "filtered": 0,
                       "errors_proxy": 0, "errors_upstream": 0, "retries": 0}}
            mod.save_stats_counters(path)
            dbm = mod.STATS["daily_by_model"]
            self.assertEqual(len(dbm), 90, "91 buckets must prune back to exactly 90")
            self.assertNotIn("2026-01-01", dbm, "only the oldest bucket must be deleted")
            self.assertIn("2026-01-02", dbm, "second-oldest bucket must survive")
            self.assertIn("2026-12-31", dbm, "newest bucket must survive")
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)["stats"]["daily_by_model"]
            self.assertEqual(len(saved), 90,
                             "file must prune back to exactly 90 buckets")
            self.assertNotIn("2026-01-01", saved,
                             "only the oldest bucket must be deleted from the file")
            self.assertIn("2026-12-31", saved,
                          "newest bucket must survive in the file")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm

    def test_daily_by_model_prune_empty(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit5-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig_dbm = mod.STATS["daily_by_model"]
        mod.STATS["daily_by_model"] = {}
        try:
            mod.save_stats_counters(path)  # {} 不得抛异常
            self.assertEqual(mod.STATS["daily_by_model"], {},
                             "empty daily_by_model must stay empty after save")
        finally:
            mod.STATS["daily_by_model"] = orig_dbm
        with open(path, encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily_by_model"]
        self.assertEqual(saved, {},
                         "empty daily_by_model must persist as an empty dict")

    def test_stats_snapshot_daily_is_copy(self) -> None:
        mod = self.mod
        snap = mod.stats_snapshot()
        self.assertIn("daily", snap)
        snap["daily"]["mutation-test"] = {"requests": 1, "filtered": 0,
                                          "errors_proxy": 0, "errors_upstream": 0}
        self.assertNotIn("mutation-test", mod.STATS["daily"],
                         "snapshot must hand out copies, not internal refs")
        # v2：daily_by_model 双层深拷贝（外层日期 dict + 内层模型 entry）
        mod.STATS["daily_by_model"].setdefault(mod.today_key(), {})["snap-m"] = {
            "requests": 1, "filtered": 0, "retries": 0}
        snap = mod.stats_snapshot()
        self.assertIn("daily_by_model", snap)
        snap["daily_by_model"]["mutation-test"] = {}
        self.assertNotIn("mutation-test", mod.STATS["daily_by_model"])
        snap["daily_by_model"][mod.today_key()]["snap-m"]["requests"] = 999
        self.assertEqual(
            mod.STATS["daily_by_model"][mod.today_key()]["snap-m"]["requests"], 1,
            "inner model entries must be copies, not internal refs")

    def test_dashboard_tooltip_skeleton(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertIn('id="evt-tip"', html)
        self.assertIn("position:fixed", html)
        self.assertIn("markEvents", html)
        self.assertIn("showEvtTip", html)
        self.assertIn("hideEvtTip", html)
        # model 名来自上游请求体：动态数据禁走 innerHTML，必须 textContent
        self.assertNotIn("innerHTML", html)

    def test_safe_log_stderr_normal_and_broken(self) -> None:
        mod = self.mod
        captured = []

        class Collect:
            def write(self, s):
                captured.append(s)
                return len(s)

            def flush(self):
                pass

        class Broken:
            def write(self, s):
                raise BrokenPipeError("pipe closed")

            def flush(self):
                pass

        orig = sys.stderr
        try:
            sys.stderr = Collect()
            mod._safe_log_stderr("hello-safe-log")
            self.assertIn("hello-safe-log", "".join(captured))
            sys.stderr = Broken()
            mod._safe_log_stderr("must-not-raise")  # 不抛 = 通过
        finally:
            sys.stderr = orig

    def test_log_exc_field_single_line_and_placeholder(self) -> None:
        mod = self.mod
        handler = object.__new__(mod.ProxyHandler)
        handler.command = "POST"
        handler.path = "/v1/chat/completions"
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            handler._log(0.0, 502, "error", 0, model="m",
                         exc=ValueError("boom word\nnext"))
        lines = buf.getvalue().splitlines()
        self.assertEqual(len(lines), 1,
                         "异常内嵌换行不得把 REQ 行裂成多行")
        self.assertIn("exc=ValueError:_boom_word_next", lines[0])
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            handler._log(0.0, 200, "ok", 0)
        self.assertIn("exc=-", buf.getvalue(),
                      "无异常时必须占位 exc=-")

    def test_stderr_text_joins_buffer(self) -> None:
        class FakeProc:
            stderr_buf = [b"REQ a\n", b"REQ b\n"]
        self.assertEqual(stderr_text(FakeProc()), "REQ a\nREQ b\n")

    def test_kill_registered_progressive_and_idempotent(self) -> None:
        tmod = sys.modules[__name__]

        class FakeProc:
            def __init__(self, survives_term):
                self.pid = 424242
                self._alive = True
                self._survives_term = survives_term
                self.terminated = False
                self.killed = False

            def poll(self):
                return None if self._alive else 0

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

            def wait(self, timeout=None):
                if self.killed or (self.terminated and not self._survives_term):
                    self._alive = False
                    return 0
                raise subprocess.TimeoutExpired("fake", timeout)

        tmod._PROCS[:] = [FakeProc(False), FakeProc(True)]
        killed = tmod.kill_registered(timeout=0.1)
        self.assertEqual(killed, [424242],
                         "proc surviving SIGTERM must be reported as force-killed")
        self.assertEqual(len(tmod._PROCS), 0, "registry must be cleared")
        self.assertEqual(tmod.kill_registered(), [], "second call is a no-op")

    def test_classify_outcome_full_matrix(self) -> None:
        mod = self.mod
        f = mod.classify_outcome

        # client_abort
        o = f(client_abort=True)
        self.assertEqual(o.category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(o.log_result, "aborted")
        self.assertFalse(o.counts_error)
        self.assertFalse(o.capture)

        # synth_502
        o = f(synth_502=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "error")
        self.assertTrue(o.counts_error)
        self.assertTrue(o.capture)

        # eof_without_done
        o = f(eof_without_done=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "eof-without-done")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # status < 400, no poison
        o = f(status=200)
        self.assertEqual(o.category, mod.CLASS_OK)
        self.assertEqual(o.log_result, "ok")
        self.assertFalse(o.counts_error)
        self.assertFalse(o.capture)

        # status < 400, poison_filtered > 0
        o = f(status=200, poison_filtered=1)
        self.assertEqual(o.category, mod.CLASS_POISON_FIXED)
        self.assertEqual(o.log_result, "ok")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # 400 <= status < 500
        o = f(status=404)
        self.assertEqual(o.category, mod.CLASS_REQUEST_FAULT)
        self.assertEqual(o.log_result, "upstream-err")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # status >= 500
        o = f(status=502)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "upstream-err")
        self.assertFalse(o.counts_error)
        self.assertTrue(o.capture)

        # priority: client_abort > synth_502 (flags 优先级验证)
        o = f(client_abort=True, synth_502=True)
        self.assertEqual(o.category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(o.log_result, "aborted")

        # priority: synth_502 > eof_without_done
        o = f(synth_502=True, eof_without_done=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "error")
        self.assertTrue(o.counts_error)

        # ValueError: status=None and no flags
        with self.assertRaises(ValueError):
            f()
        with self.assertRaises(ValueError):
            f(status=None)

    def test_empty_stream_should_retry(self) -> None:
        f = self.mod.empty_stream_should_retry
        self.assertTrue(f(1))
        self.assertTrue(f(10))
        self.assertFalse(f(0))
        self.assertFalse(f(-1))
        # 等价性验证：final = not f(budget) 必须与直接比较一致
        self.assertEqual(not f(0), not 0 > 0)
        self.assertEqual(not f(1), not 1 > 0)

    def test_snapshot_text_normal_and_truncation(self) -> None:
        f = self.mod._snapshot_text
        self.assertEqual(f(b"hello", 100), "hello")
        self.assertEqual(f(b"", 10), "")
        self.assertEqual(f(None, 10), "")
        long_bytes = b"x" * 5000
        result = f(long_bytes, 100)
        self.assertLessEqual(len(result), 100 + len("\u2026[truncated]"))
        self.assertTrue(result.endswith("[truncated]"))
        # 恰等于 cap 时不截断
        exact = b"a" * 50
        self.assertEqual(f(exact, 50), "a" * 50)
        # unicode 替换字符
        invalid = b"\xff\xfe"
        self.assertIn("\ufffd", f(invalid, 50))

    def test_record_error_event_off_no_capture(self) -> None:
        mod = self.mod
        orig_cap = mod.CAPTURE_ERRORS
        orig_events = mod.ERROR_EVENTS
        orig_seq = mod._ERROR_EVENT_SEQ
        mod.ERROR_EVENTS = collections.deque(maxlen=mod.ERROR_RING_MAX)
        mod._ERROR_EVENT_SEQ = 0
        try:
            mod.CAPTURE_ERRORS = False
            mod.record_error_event(mod.ERR_KIND_SYNTH_502, model="m", exc=ValueError("boom"))
            self.assertEqual(len(mod.ERROR_EVENTS), 0,
                             "CAPTURE_ERRORS=False must not add events")
            self.assertEqual(mod._ERROR_EVENT_SEQ, 0,
                             "seq must not advance when capture is off")
            mod.CAPTURE_ERRORS = True
            mod.record_error_event(mod.ERR_KIND_SYNTH_502, model="m", exc=ValueError("boom"))
            self.assertEqual(len(mod.ERROR_EVENTS), 1)
            self.assertEqual(mod._ERROR_EVENT_SEQ, 1)
            ev = mod.ERROR_EVENTS[0]
            self.assertEqual(ev["kind"], mod.ERR_KIND_SYNTH_502)
            self.assertEqual(ev["model"], "m")
            self.assertIsInstance(ev["id"], int)
            self.assertIsInstance(ev["ts"], float)
            self.assertIn("ValueError", ev["exc"])
        finally:
            mod.CAPTURE_ERRORS = orig_cap
            mod.ERROR_EVENTS = orig_events
            mod._ERROR_EVENT_SEQ = orig_seq

    def test_record_error_event_ring_eviction_and_id_monotonic(self) -> None:
        mod = self.mod
        orig_cap = mod.CAPTURE_ERRORS
        orig_events = mod.ERROR_EVENTS
        orig_seq = mod._ERROR_EVENT_SEQ
        mod.ERROR_EVENTS = collections.deque(maxlen=mod.ERROR_RING_MAX)
        mod._ERROR_EVENT_SEQ = 0
        try:
            mod.CAPTURE_ERRORS = True
            cap = mod.ERROR_RING_MAX
            for i in range(cap + 10):
                mod.record_error_event(mod.ERR_KIND_SYNTH_502)
            self.assertEqual(len(mod.ERROR_EVENTS), cap,
                             "ring must cap at ERROR_RING_MAX")
            self.assertEqual(mod.ERROR_EVENTS[-1]["id"], cap + 10,
                             "id must stay monotonic across eviction")
            # 最老 10 条已被淘汰
            self.assertGreater(mod.ERROR_EVENTS[0]["id"], 10)
        finally:
            mod.CAPTURE_ERRORS = orig_cap
            mod.ERROR_EVENTS = orig_events
            mod._ERROR_EVENT_SEQ = orig_seq

    def test_record_error_event_body_response_snapshot(self) -> None:
        mod = self.mod
        orig_cap = mod.CAPTURE_ERRORS
        orig_events = mod.ERROR_EVENTS
        orig_seq = mod._ERROR_EVENT_SEQ
        mod.ERROR_EVENTS = collections.deque(maxlen=mod.ERROR_RING_MAX)
        mod._ERROR_EVENT_SEQ = 0
        try:
            mod.CAPTURE_ERRORS = True
            body = b"x" * 5000
            resp = b"y" * 3000
            mod.record_error_event(
                mod.ERR_KIND_UPSTREAM_5XX, model="m",
                upstream_status=500, body=body, response=resp,
                filtered=3, retried=1, retry_reason="test")
            ev = mod.ERROR_EVENTS[0]
            self.assertLessEqual(len(ev["body"]), mod.BODY_SNAPSHOT_CAP + len("\u2026[truncated]"))
            self.assertLessEqual(len(ev["response"]), mod.RESPONSE_SNIPPET_CAP + len("\u2026[truncated]"))
            self.assertTrue(ev["body"].endswith("[truncated]"))
            self.assertTrue(ev["response"].endswith("[truncated]"))
            self.assertEqual(ev["filtered"], 3)
            self.assertEqual(ev["retried"], 1)
            self.assertEqual(ev["retry_reason"], "test")
            self.assertEqual(ev["upstream_status"], 500)
        finally:
            mod.CAPTURE_ERRORS = orig_cap
            mod.ERROR_EVENTS = orig_events
            mod._ERROR_EVENT_SEQ = orig_seq

    def test_logs_snapshot_empty_ring(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(maxlen=mod.LOG_RING_MAX)
        try:
            snap = mod.logs_snapshot()
            self.assertEqual(snap["lines"], [])
            self.assertEqual(snap["next_cursor"], 0)
            self.assertEqual(snap["oldest_seq"], 0)
            self.assertEqual(snap["ring_max"], mod.LOG_RING_MAX)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_tail_default_and_custom(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(
            [{"seq": i + 1, "line": "msg-%d" % (i + 1)} for i in range(200)],
            maxlen=mod.LOG_RING_MAX)
        try:
            # 缺省 tail=100
            snap = mod.logs_snapshot()
            self.assertEqual(len(snap["lines"]), 100)
            self.assertEqual(snap["lines"][0]["seq"], 101)
            self.assertEqual(snap["lines"][-1]["seq"], 200)
            self.assertEqual(snap["next_cursor"], 200)
            self.assertEqual(snap["oldest_seq"], 1)

            # tail=20
            snap = mod.logs_snapshot(tail=20)
            self.assertEqual(len(snap["lines"]), 20)
            self.assertEqual(snap["lines"][-1]["seq"], 200)
            self.assertEqual(snap["next_cursor"], 200)

            # tail 边界：1 和 LOG_RING_MAX
            snap = mod.logs_snapshot(tail=1)
            self.assertEqual(len(snap["lines"]), 1)
            snap = mod.logs_snapshot(tail=mod.LOG_RING_MAX)
            self.assertEqual(len(snap["lines"]), 200)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_cursor_incremental(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(
            [{"seq": i + 1, "line": "msg-%d" % (i + 1)} for i in range(50)],
            maxlen=mod.LOG_RING_MAX)
        try:
            # cursor 在中间
            snap = mod.logs_snapshot(cursor=20)
            lines = snap["lines"]
            self.assertEqual(len(lines), 30)  # seq 21..50
            self.assertEqual(lines[0]["seq"], 21)
            self.assertEqual(lines[-1]["seq"], 50)
            self.assertEqual(snap["next_cursor"], 50)
            self.assertEqual(snap["oldest_seq"], 1)

            # cursor 在最新 → 空结果
            snap = mod.logs_snapshot(cursor=50)
            self.assertEqual(snap["lines"], [])
            self.assertEqual(snap["next_cursor"], 50)

            # cursor 在最新之后（未来 cursor）
            snap = mod.logs_snapshot(cursor=99)
            self.assertEqual(snap["lines"], [])
            self.assertEqual(snap["next_cursor"], 99)

            # cursor 在 oldest 之前（淘汰可检测）
            snap = mod.logs_snapshot(cursor=0)
            self.assertEqual(len(snap["lines"]), 50)
            self.assertEqual(snap["oldest_seq"], 1)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_cursor_page_limit(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        mod.LOG_RING = collections.deque(
            [{"seq": i + 1, "line": "msg-%d" % (i + 1)} for i in range(800)],
            maxlen=mod.LOG_RING_MAX)
        try:
            snap = mod.logs_snapshot(cursor=0)
            # page 上限 LOG_PAGE_MAX=500
            self.assertLessEqual(len(snap["lines"]), mod.LOG_PAGE_MAX)
            self.assertEqual(snap["lines"][0]["seq"], 1)
            self.assertEqual(snap["lines"][-1]["seq"], mod.LOG_PAGE_MAX)
        finally:
            mod.LOG_RING = orig_ring

    def test_logs_snapshot_invalid_params(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        # 子测试 1：空环状态下非法参数仍抛 ValueError（参数校验先于空环早退）
        mod.LOG_RING = collections.deque(maxlen=mod.LOG_RING_MAX)
        try:
            # cursor 和 tail 同给
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor=1, tail=10)
            # tail 越界
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=0)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=mod.LOG_RING_MAX + 1)
            # cursor 非 int
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor="abc")
            # tail 非 int
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail="abc")
        finally:
            mod.LOG_RING = orig_ring
        # 子测试 2：非空环状态下同样非法参数也抛 ValueError（防回归掩盖）
        mod.LOG_RING = collections.deque(
            [{"seq": 1, "line": "x"}], maxlen=mod.LOG_RING_MAX)
        try:
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor=1, tail=10)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=0)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail=mod.LOG_RING_MAX + 1)
            with self.assertRaises(ValueError):
                mod.logs_snapshot(cursor="abc")
            with self.assertRaises(ValueError):
                mod.logs_snapshot(tail="abc")
        finally:
            mod.LOG_RING = orig_ring

    def test_safe_log_stderr_writes_to_log_ring(self) -> None:
        mod = self.mod
        orig_ring = mod.LOG_RING
        orig_seq = mod._LOG_SEQ
        mod.LOG_RING = collections.deque(maxlen=mod.LOG_RING_MAX)
        mod._LOG_SEQ = 0
        try:
            mod._safe_log_stderr("hello-ring")
            self.assertEqual(len(mod.LOG_RING), 1)
            self.assertEqual(mod.LOG_RING[0]["line"], "hello-ring")
            self.assertEqual(mod.LOG_RING[0]["seq"], 1)
            mod._safe_log_stderr("msg-2")
            self.assertEqual(len(mod.LOG_RING), 2)
            self.assertEqual(mod.LOG_RING[1]["seq"], 2)
        finally:
            mod.LOG_RING = orig_ring
            mod._LOG_SEQ = orig_seq

    def test_header_timeout_should_retry(self) -> None:
        f = self.mod.header_timeout_should_retry
        self.assertTrue(f(1))
        self.assertTrue(f(10))
        self.assertFalse(f(0))
        self.assertFalse(f(-1))
        self.assertEqual(not f(0), not 0 > 0)
        self.assertEqual(not f(1), not 1 > 0)

    def test_header_timeout_kind_category_and_classify_regression(self) -> None:
        mod = self.mod
        # 新 kind 映射正确
        self.assertEqual(mod._KIND_CATEGORY.get(mod.ERR_KIND_HEADER_TIMEOUT),
                         mod.CLASS_UPSTREAM_FAULT,
                         "header_timeout must be classified as upstream_fault")
        # 既有的 6 个 kind 映射不变（逐条锁死）
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_POISON], mod.CLASS_POISON_FIXED)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_EMPTY_RETRY], mod.CLASS_OK)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_EOF_NO_DONE], mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_SYNTH_502], mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_UPSTREAM_5XX], mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_REQUEST_4XX], mod.CLASS_REQUEST_FAULT)
        # classify_outcome 矩阵不受影响（关键路径逐条锁死）
        self.assertEqual(mod.classify_outcome(client_abort=True).category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(mod.classify_outcome(synth_502=True).category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod.classify_outcome(eof_without_done=True).category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(mod.classify_outcome(status=200).category, mod.CLASS_OK)
        self.assertEqual(mod.classify_outcome(status=200, poison_filtered=1).category, mod.CLASS_POISON_FIXED)
        self.assertEqual(mod.classify_outcome(status=404).category, mod.CLASS_REQUEST_FAULT)
        self.assertEqual(mod.classify_outcome(status=502).category, mod.CLASS_UPSTREAM_FAULT)

    def test_open_upstream_header_timeout_then_body_long_timeout(self) -> None:
        """白盒：patch 假上游，调 _open_upstream 直连，断言 getresponse 后 sock timeout 已恢复 UPSTREAM_TIMEOUT。"""
        mod = self.mod
        import types
        # 用真实假上游验证：正常上游无 stall，直调 _open_upstream
        upstream_port = make_fake_upstream(False)
        try:
            # Patch UPSTREAM_BASE 指向假上游
            orig_base = mod.UPSTREAM_BASE
            mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % upstream_port
            try:
                # 临时设短超时
                orig_ht = mod.HEADER_TIMEOUT_S
                mod.HEADER_TIMEOUT_S = 0.5
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    conn, resp = mod.ProxyHandler._open_upstream(
                        types.SimpleNamespace(), "POST", "/v1/chat/completions",
                        body, {"Content-Type": "application/json"})
                    # 断言 getresponse 后 sock timeout 已恢复 UPSTREAM_TIMEOUT。
                    # 实测 getresponse() 返回后 conn.sock 已被置 None（socket 移交
                    # HTTPResponse 的 fp.raw）；生产实现保留 getresponse 前的 sock
                    # 引用并在其上 settimeout，与 resp.fp.raw._sock 为同一对象，
                    # 故经此断言体阶段超时已恢复。
                    self.assertIsNone(conn.sock)
                    self.assertEqual(resp.fp.raw._sock.gettimeout(), mod.UPSTREAM_TIMEOUT,
                                     "after getresponse, sock timeout must be UPSTREAM_TIMEOUT (body phase)")
                    resp.read()
                    conn.close()
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()

    def test_open_upstream_stall_raises_remote_disconnected(self) -> None:
        """白盒：stall 上游 → _open_upstream 在 HEADER_TIMEOUT_S 内抛 RemoteDisconnected。

        实测：stall 上游读 body 后不写响应直接关连接，代理侧 getresponse() 抛
        http.client.RemoteDisconnected（"响应头阶段未收到任何响应字节"），而非
        socket.timeout（后者仅当上游保持连接静默到超时阈值才出现）。"""
        mod = self.mod
        import types
        upstream_port = make_fake_upstream(False, stall_all=True)
        try:
            orig_base = mod.UPSTREAM_BASE
            mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % upstream_port
            try:
                orig_ht = mod.HEADER_TIMEOUT_S
                mod.HEADER_TIMEOUT_S = 0.5
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    with self.assertRaises(http.client.RemoteDisconnected):
                        mod.ProxyHandler._open_upstream(
                            types.SimpleNamespace(), "POST", "/v1/chat/completions",
                            body, {"Content-Type": "application/json"})
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()

    def test_open_upstream_rst_raises_connection_reset_error(self) -> None:
        """白盒：RST 上游 → _open_upstream 在 HEADER_TIMEOUT_S 内抛 ConnectionResetError。

        SO_LINGER(1,0) close 强制发 RST（非 FIN-close），代理侧抛 ConnectionResetError
        且类型恰为 ConnectionResetError（非其子类 RemoteDisconnected——后者是 FIN 竞态
        产物，此处锁死真 RST 以证夹具确定性）。"""
        mod = self.mod
        import types
        upstream_port = make_fake_upstream(False, rst_all=True)
        try:
            orig_base = mod.UPSTREAM_BASE
            mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % upstream_port
            try:
                orig_ht = mod.HEADER_TIMEOUT_S
                mod.HEADER_TIMEOUT_S = 0.5
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    with self.assertRaises(ConnectionResetError) as cm:
                        mod.ProxyHandler._open_upstream(
                            types.SimpleNamespace(), "POST", "/v1/chat/completions",
                            body, {"Content-Type": "application/json"})
                    self.assertIs(type(cm.exception), ConnectionResetError,
                                  "RST fixture must raise exactly ConnectionResetError,"
                                  " not a subclass like RemoteDisconnected")
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()


class AdminIntegrationTest(unittest.TestCase):
    """admin/dashboard 子进程集成测试（真实 socket，端口与持久化均走 seam）。"""

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_dashboard_and_stats_served(self) -> None:
        status, body, ctype = admin_get(self.proc.admin_port, "/")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("text/html"),
                        "dashboard must be text/html, got %r" % ctype)
        self.assertTrue(body.startswith(b"<!doctype html"), body[:60])
        self.assertIn("/api/stats", body.decode("utf-8"))
        status, body, ctype = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"))
        snap = json.loads(body.decode("utf-8"))
        for key in ("requests_total", "filtered_total", "errors_total",
                    "empty_retries_total", "eof_without_done_total",
                    "finish_retries_total",
                    "active",
                    "uptime_s", "upstream_base", "upstream_source",
                    "recent", "poison_previews"):
            self.assertIn(key, snap)
        self.assertEqual(snap["upstream_base"],
                         "http://127.0.0.1:%d" % self.upstream_port)
        self.assertEqual(snap["upstream_source"], "env")
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(snap["requests_total"], 1)
        self.assertGreaterEqual(len(snap["recent"]), 1)

    def test_config_get_reports_env_source(self) -> None:
        status, body, ctype = admin_get(self.proc.admin_port, "/api/config")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"))
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["upstream_base"],
                         "http://127.0.0.1:%d" % self.upstream_port)
        self.assertEqual(cfg["source"], "env")

    def test_config_post_swaps_upstream_and_persists(self) -> None:
        upstream2_port = make_fake_upstream(False, tag="/second-upstream")
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream2_port}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["ok"], True)
        data, _ = get_plain(self.proxy_port)
        self.assertIn(b"/second-upstream", data,
                      "after swap /plain must hit second upstream, got %r" % data)
        status, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["upstream_base"], "http://127.0.0.1:%d" % upstream2_port)
        self.assertEqual(cfg["source"], "api")
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["upstream_base"],
                             "http://127.0.0.1:%d" % upstream2_port)

    def test_config_post_rejects_bad_input(self) -> None:
        status, _ = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "ftp://nope"}).encode("utf-8"))
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/config", b"not json")
        self.assertEqual(status, 400)

    def test_capture_errors_alone_post(self) -> None:
        """单独 POST capture_errors（无 upstream_base）应 200，并维持上游。"""
        original_base = "http://127.0.0.1:%d" % self.upstream_port
        # 单独 POST capture_errors:true
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"capture_errors": True}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertTrue(resp.get("ok"))
        self.assertTrue(resp.get("capture_errors"))
        self.assertEqual(resp.get("upstream_base"), original_base)
        # GET /api/config 回读一致
        status, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertTrue(cfg["capture_errors"])
        self.assertEqual(cfg["upstream_base"], original_base)
        # 再单独 POST capture_errors:false
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"capture_errors": False}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertFalse(resp.get("capture_errors"))
        self.assertEqual(resp.get("upstream_base"), original_base)

    def test_dashboard_html_full_page(self) -> None:
        status, body, ctype = admin_get(self.proc.admin_port, "/")
        self.assertEqual(status, 200)
        html = body.decode("utf-8")
        self.assertIn("保存上游端点", html)
        self.assertIn("剥行流带", html)
        self.assertIn("剥行（所选时段）", html)
        self.assertIn('name="viewport"', html)
        self.assertIn("prefers-reduced-motion", html)
        self.assertIn("focus-visible", html)
        # 毒行预览来自上游原始字节：动态数据禁走 innerHTML，必须 textContent
        self.assertIn("textContent", html)
        self.assertNotIn("innerHTML", html)
        # v1.1：sparkline 剥行红柱叠加 / 模型列 / 计数持久化文案
        self.assertIn("var(--err)", html)
        self.assertIn("<th>模型</th>", html)
        self.assertIn("跨重启保留", html)
        # favicon + 标题配套 meta
        self.assertIn('rel="icon"', html)
        self.assertIn("theme-color", html)
        # v1.2：按天统计卡（日期表 + 双口径错误列）
        self.assertIn("按天统计", html)
        self.assertIn("<th>上游5xx</th>", html)
        self.assertIn('id="daily-body"', html)
        # v2：按天表「重试」列（6 列）+ 按天×模型副表
        self.assertIn("<th>重试</th>", html)
        self.assertIn('colspan="6"', html)
        self.assertIn('id="daily-model-body"', html)
        # v3：按天×模型表 7 列与主表数值列对齐（代理错误/上游5xx）+ 错误行高亮
        self.assertEqual(html.count("<th>代理错误</th>"), 2)
        self.assertEqual(html.count("<th>上游5xx</th>"), 2)
        self.assertIn('colspan="7"', html)
        self.assertIn("String(ent.errors_proxy || 0)", html)
        self.assertIn("String(ent.errors_upstream || 0)", html)
        self.assertIn("(ent.errors_proxy || 0) + (ent.errors_upstream || 0)", html)
        # v2.1：按模型重启累计卡已整体移除，标题与口径标注锁定不复活
        self.assertNotIn("自上次重启起累计", html)
        self.assertNotIn("按模型", html)
        # v1.3：跨天日期分组（纯前端逻辑，静态断言锁定存在性，目检兜底见 Task 3）
        self.assertIn("fmtDate", html)
        self.assertIn("date-row", html)
        # 时间维度切换：4 tab / 默认 7d / bounds 字符串过滤 / 零请求重渲染 / 新错误口径
        self.assertEqual(html.count('class="range-tab"'), 4)
        for rk in ("3d", "7d", "mtd", "last_month"):
            self.assertIn('data-range="%s"' % rk, html)
        self.assertIn("活跃连接·实时", html)
        self.assertIn('var selectedRange = "7d"', html)
        self.assertIn("range_bounds[selectedRange]", html)
        self.assertIn("lastSnap = snap", html)
        self.assertIn("rs.errors_proxy + rs.errors_upstream", html)
        self.assertIn("renderRangeTabs", html)
        self.assertIn('id="daily-title-range"', html)
        self.assertIn('id="daily-model-title-range"', html)
        self.assertNotIn("slice(0, 14)", html)
        self.assertNotIn("最近 14 天", html)
        self.assertNotIn("与顶部错误数同口径", html)

    def test_favicon_served(self) -> None:
        status, body, ctype = admin_get(self.proc.admin_port, "/favicon.ico")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("image/x-icon"),
                        "favicon must be image/x-icon, got %r" % ctype)
        self.assertTrue(body.startswith(b"\x00\x00\x01\x00"),
                        "body must start with ICO magic, got %r" % body[:6])
        self.assertGreater(len(body), 100)

    def test_recent_entries_carry_model(self) -> None:
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertTrue(snap["recent"])
        self.assertEqual(snap["recent"][-1]["model"], "deepseek-v4-pro-0813-oc")
        self.assertNotIn("by_model", snap,
                         "by_model must be removed from /api/stats snapshot")
        # v2：daily_by_model 集成（/api/stats 顶层键透出；retries 归因见单测 matrix_fallback）
        today = time.strftime("%Y-%m-%d")
        self.assertIn(today, snap["daily_by_model"])
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["requests"], 1,
            "per-model daily matrix must record the SSE request")
        # v3：dm entry 6 键经 /api/stats 透出（纯新增键，旧客户端只读 3 键不受影响）
        dm_entry = snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]
        for key in ("requests", "filtered", "errors_proxy", "errors_upstream",
                    "retries", "eof_without_done"):
            self.assertIn(key, dm_entry)
            self.assertIsInstance(dm_entry[key], int)
            self.assertGreaterEqual(dm_entry[key], 0)

    def test_stats_counters_resume_from_persist(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                          "stats": {"requests_total": 7, "filtered_total": 3,
                                    "errors_total": 1}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["requests_total"], 7)
        self.assertEqual(snap["filtered_total"], 3)
        self.assertEqual(snap["errors_total"], 1)
        self.assertEqual(snap["eof_without_done_total"], 0,
                         "legacy persist without eof key must load as 0")
        post_sse(self.proc.proxy_port)  # 重启后的新代理端口
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["requests_total"], 8)
    def test_sigterm_persists_counters(self) -> None:
        post_sse(self.proxy_port)
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=5)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertGreaterEqual(data["stats"]["requests_total"], 1)
        self.assertIn("upstream_base", data)

    def test_req_log_line_has_ts_and_model(self) -> None:
        post_sse(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"retry_reason=\S+ "
                      r"exc=\S+ "
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
        self.assertIsNotNone(m, "REQ 行必须带 model= / retry_reason= / exc= / ts= 字段，stderr:\n" + stderr)
        time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z")  # %z 回析：防平台差异静默退化

    def test_req_error_line_carries_exc(self) -> None:
        upstream_port = free_port()  # 死端口：连接即 ECONNREFUSED，进 :612 首次失败分支
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port)
        conn = http.client.HTTPConnection("127.0.0.1", proxy_port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     body=b'{"model":"m","stream":true,"messages":[]}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 502, "上游不可达必须由代理合成 502")
        proc.terminate()
        proc.wait(timeout=5)
        stderr = stderr_text(proc)
        m = re.search(r"^REQ POST /v1/chat/completions -> 502 dur=\d+\.\ds "
                      r"result=error .*? exc=(\S+)\s+ts=", stderr, re.M)
        self.assertIsNotNone(m, "error 502 REQ 行必须带 exc= 字段，stderr:\n" + stderr)
        self.assertNotEqual(m.group(1), "-",
                            "exc 字段不得是占位符，stderr:\n" + stderr)

    def test_client_abort_is_quiet_and_not_error(self) -> None:
        upstream_port = make_fake_upstream(False, big=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_SEND_TIMEOUT": "1"})
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        payload = b'{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[]}'
        conn.request("POST", "/v1/chat/completions", body=payload,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        resp.read(64)
        conn.close()  # 带未读数据关闭 → 内核回 RST → 代理后续写 EPIPE
        deadline = time.time() + 5
        saw499 = False
        snap = {}
        while time.time() < deadline:
            _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            if any(r["status"] == 499 for r in snap["recent"]):
                saw499 = True
                break
            time.sleep(0.2)
        self.assertTrue(saw499, "aborted request must be recorded with status 499")
        self.assertEqual(snap["errors_total"], 0,
                         "client abort must not count into errors_total")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=aborted", stderr)
        self.assertIn("model=-", stderr)  # 499 abort 不传 model → 占位符 -
        self.assertNotIn("Traceback", stderr,
                         "client abort must not produce handle_error traceback")

    def test_config_post_localhost_allowed_even_with_token_env(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port = make_fake_upstream(False)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={"CTYUN_ADMIN_TOKEN": "sekret"})
        # token 已设：本机来源仍走白名单，无需 X-Admin-Token 头
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream_port}).encode("utf-8"))
        self.assertEqual(status, 200)

    def test_daily_bucket_via_sse_and_persist(self) -> None:
        today = time.strftime("%Y-%m-%d")
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertIn(today, snap["daily"])
        self.assertGreaterEqual(snap["daily"][today]["requests"], 1)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertGreaterEqual(saved[today]["requests"], 1,
                                "daily buckets must persist on SIGTERM")

    def test_upstream_500_counts_into_daily_errors_upstream(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        bad_port = make_fake_upstream(False, fail_500=True)
        self.proc = start_proxy(bad_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 500)
        resp.read()
        conn.close()
        today = time.strftime("%Y-%m-%d")
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(snap["daily"][today]["errors_upstream"], 1)
        self.assertEqual(snap["errors_total"], 0,
                         "upstream 5xx passthrough must not touch errors_total")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertGreaterEqual(saved[today]["errors_upstream"], 1)

    def test_daily_buckets_resume_from_persist(self) -> None:
        self.proc.terminate()
        # 系统忙时 SIGTERM 落盘 + server_close 偶发超过 5s（实测两连挂、复刻秒退），
        # 放宽到 10s 只吸收慢、不掩盖死锁
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                          "stats": {"requests_total": 7, "filtered_total": 3,
                                    "errors_total": 1,
                                    "daily": {"2026-01-01": {
                                        "requests": 5, "filtered": 1,
                                        "errors_proxy": 0, "errors_upstream": 2}}}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["daily"]["2026-01-01"]["requests"], 5)
        self.assertEqual(snap["daily"]["2026-01-01"]["errors_upstream"], 2)

    def test_events_resume_from_persist(self) -> None:
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        seeded = [{"ts": 1757654300.5, "kind": "proxy", "model": None, "status": 502},
                  {"ts": 1757654301.5, "kind": "retry", "model": "m-a", "status": None}]
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                          "stats": {"requests_total": 1, "events": seeded}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["events"], seeded,
                         "main() must restore EVENTS from persist verbatim")

    def test_empty_stream_retried_and_second_attempt_relayed(self) -> None:
        upstream_port, calls = make_scripted_upstream()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "empty stream must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)

    def test_priming_prefix_flushed_in_order_no_retry(self) -> None:
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_A + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "content-bearing stream must not retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_A + SSE_DONE,
                         "priming prefix must flush first and in order, got %r" % data)

    def test_done_without_content_is_legal_no_retry(self) -> None:
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "[DONE] without content is legal, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_DONE)

    def test_double_empty_stream_falls_back_after_two_calls(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "retry cap is 1: second empty stream ends the attempt, calls=%d"
                         % len(calls))
        self.assertEqual(data, SSE_REASONING,
                         "attempt-2 buffer must be delivered as-is, got %r" % data)

    def test_zero_record_empty_200_retried(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True, blank_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.assertEqual(data, b"", "zero-record stream must relay zero bytes")

    def test_ctyun_empty_retry_zero_disables(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_EMPTY_RETRY": "0"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "CTYUN_EMPTY_RETRY=0 must disable retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING)

    def test_empty_retry_counter_persists_and_resumes(self) -> None:
        upstream_port, calls = make_scripted_upstream()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=5)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertGreaterEqual(saved["stats"]["empty_retries_total"], 1)
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(saved["stats"]["daily"][today]["retries"], 1)
        upstream_port2, calls2 = make_scripted_upstream()
        self.proc = start_proxy(
            upstream_port2, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % upstream_port2,
                          "stats": {"empty_retries_total": 5}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["empty_retries_total"], 5,
                         "seeded counter must resume from persist")
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls2), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["empty_retries_total"], 6)

    def test_eof_without_done_marked_counted_and_persisted(self) -> None:
        # spec 用例 ①：有 content 无 [DONE] 即 EOF（上游截断签名，R31 现场复刻）
        upstream_port, calls = make_scripted_upstream(body_override=SSE_A + SSE_B)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "content-bearing stream must not retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B,
                         "truncated stream must still relay byte-exact, got %r" % data)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["eof_without_done_total"], 1,
                                "eof-without-done must count into STATS total")
        self.assertGreaterEqual(snap["daily"][today]["eof_without_done"], 1,
                                "eof-without-done must count into daily bucket")
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["eof_without_done"], 1,
            "eof-without-done must count into daily_by_model")
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=eof-without-done", stderr,
                      "REQ line must carry result=eof-without-done, stderr:\n" + stderr)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved["eof_without_done_total"], 1)
        self.assertGreaterEqual(saved["daily"][today]["eof_without_done"], 1)
        self.assertGreaterEqual(
            saved["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["eof_without_done"], 1)
        # 重启续算：seed 含新计数键 → /api/stats 透出（仿 empty_retries resume 段）
        self.proc = start_proxy(
            upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                          "stats": {"eof_without_done_total": 5}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 5,
                         "seeded eof counter must resume from persist")

    def test_done_streams_stay_ok_no_eof_counter(self) -> None:
        # spec 用例 ② 对照组 A：默认正常流（SSE_A+SSE_B+[DONE]，setUp 假上游）
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 0,
                         "normal done-terminated stream must not trip eof counter")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "normal stream must stay result=ok, stderr:\n" + stderr)
        # spec 用例 ② 对照组 B：reasoning 前缀 + 合法 [DONE]（priming 前缀场景）
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_DONE)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1)
        self.assertEqual(data, SSE_REASONING + SSE_DONE)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 0,
                         "reasoning+[DONE] stream must not trip eof counter")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "reasoning+[DONE] stream must stay result=ok, stderr:\n" + stderr)
        self.assertNotIn("eof-without-done", stderr)

    def test_double_empty_stream_not_marked_eof_without_done(self) -> None:
        # spec 用例 ③：双空流 fallback（priming 路径 EOF 不加标记，避免与 retries 双计数）
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.assertEqual(data, SSE_REASONING)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 0,
                         "priming-stage EOF must not trip eof counter")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertNotIn("eof-without-done", stderr,
                         "priming-stage EOF (empty stream fallback) must not be marked "
                         "eof-without-done, stderr:\n" + stderr)

    def test_finish_with_usage_zero_content_legal_no_retry(self) -> None:
        """finish + usage + [DONE] 零内容流：合法，不重试。"""
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_FINISH + SSE_USAGE + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "usage-bearing finish stream must not retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_FINISH + SSE_USAGE + SSE_DONE,
                         "legal zero-content stream must relay byte-exact, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "legal stream must stay result=ok, stderr:\n" + stderr)

    def test_finish_without_usage_retried_second_stream_relayed(self) -> None:
        """finish 无 usage 故障形态：首呼 fault_finish_first 回故障尾段触发重试，
        次呼回正常 content 流全量交付。"""
        upstream_port, calls = make_scripted_upstream(
            fault_finish_first=True, body_override=SSE_A + SSE_B + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "finish fault must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=finish-no-usage", stderr,
                      "REQ line must carry retry_reason=finish-no-usage, stderr:\n" + stderr)

    def test_double_finish_fault_falls_back_after_two_calls(self) -> None:
        """双 finish 故障：fault_finish_stream 每呼回故障尾段 → calls==2 fallback。"""
        upstream_port, calls = make_scripted_upstream(fault_finish_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "retry cap is 1: second fault finish ends the attempt, calls=%d"
                         % len(calls))
        self.assertEqual(data, SSE_FAULT_TAIL,
                         "attempt-2 buffer must be delivered as-is, got %r" % data)

    def test_finish_fault_counters_and_req_line(self) -> None:
        """finish 故障计数器 + SIGTERM 落盘 + seed resume 续算。"""
        upstream_port, calls = make_scripted_upstream(
            fault_finish_first=True, body_override=SSE_A + SSE_B + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["empty_retries_total"], 1)
        self.assertGreaterEqual(snap["finish_retries_total"], 1,
                                "finish fault must increment finish_retries_total")
        self.assertGreaterEqual(snap["daily"][today]["retries"], 1)
        self.assertGreaterEqual(
            snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["retries"], 1)
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=5)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved["finish_retries_total"], 1)
        # seed resume：finish_retries_total 跨重启续算
        upstream_port2, calls2 = make_scripted_upstream(
            fault_finish_first=True, body_override=SSE_A + SSE_B + SSE_DONE)
        self.proc = start_proxy(
            upstream_port2, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % upstream_port2,
                          "stats": {"finish_retries_total": 5}})
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap2 = json.loads(body.decode("utf-8"))
        self.assertEqual(snap2["finish_retries_total"], 5,
                         "seeded finish_retries_total must resume from persist")
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls2), 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["finish_retries_total"], 6)

    def test_finish_then_content_fails_open_no_retry(self) -> None:
        """finish 后反常跟 content：fail-open 即刻 flush，不重试。"""
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_FINISH + SSE_A + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "finish-then-content must fail-open no retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_FINISH + SSE_A + SSE_DONE,
                         "finish-then-content must relay byte-exact, got %r" % data)

    def test_finish_fault_retry_zero_disables(self) -> None:
        """CTYUN_EMPTY_RETRY=0：finish 故障不重试，原样下发。"""
        upstream_port, calls = make_scripted_upstream(fault_finish_first=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_EMPTY_RETRY": "0"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "CTYUN_EMPTY_RETRY=0 must disable retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_FAULT_TAIL)

    def test_finish_tail_over_cap_fails_open(self) -> None:
        """finish 后尾段超 PRIMED_TAIL_CAP(256KB) → fail-open 即刻 flush。"""
        # 构造 >256KB 尾段：11000 条噪音 data 行（26B/行 ≈ 286KB）无空行分隔，
        # 与 [DONE] 行合成单 record 后一次性计入 hold_bytes 超限。
        big_noise = b'data: {"comment":"noise"}\n' * 11000  # 286,000B > 262,144B
        body = SSE_REASONING + SSE_FINISH + big_noise + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "finish tail over cap must fail-open no retry, calls=%d" % len(calls))
        self.assertEqual(len(data), len(body),
                         "fail-open must deliver all bytes, expected %d got %d"
                         % (len(body), len(data)))

    def test_upstream_receives_normalized_null_content(self) -> None:
        """端到端：assistant content:null 经代理后上游收到 content:"" 归一体；
        无 null 的请求上游收到字节级一致 body。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        upstream_port, bodies = make_body_recording_upstream()
        self.proc = start_proxy(upstream_port, free_port())
        payload = json.dumps({
            "model": "glm-5.3-oc", "stream": True,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}]},
            ],
        }).encode("utf-8")
        data = post_sse(self.proc.proxy_port, payload)
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        self.assertEqual(len(bodies), 1)
        sent = json.loads(bodies[0].decode("utf-8"))
        self.assertEqual(sent["messages"][1]["content"], "")
        self.assertEqual(sent["messages"][1]["tool_calls"][0]["id"], "c1")
        clean = json.dumps({"model": "glm-5.3-oc", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")
        post_sse(self.proc.proxy_port, clean)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[1], clean)  # 无 null 请求字节级透传

    def test_errors_endpoint_default_off(self) -> None:
        """默认 CAPTURE_ERRORS=False：/api/errors 返回 capture_errors:false + 空列表。"""
        status, body, ctype = admin_get(self.proc.admin_port, "/api/errors")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"))
        data = json.loads(body.decode("utf-8"))
        self.assertFalse(data["capture_errors"])
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["events"], [])
        # 列表项不应含 body/response
        for ev in data["events"]:
            self.assertNotIn("body", ev)
            self.assertNotIn("response", ev)

    def test_capture_errors_post_get_roundtrip(self) -> None:
        """POST capture_errors:true → GET /api/config 回读 → 持久化文件含键。"""
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertTrue(resp["ok"])
        self.assertTrue(resp.get("capture_errors"))

        # GET /api/config 回读
        status, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertTrue(cfg["capture_errors"])

        # GET /api/errors 报告 capture_errors:true
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["capture_errors"])

        # 持久化文件含 capture_errors
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertTrue(saved.get("capture_errors"))

    def test_capture_errors_bad_input(self) -> None:
        """capture_errors 非 bool → 400。"""
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                        "capture_errors": "yes"}).encode("utf-8"))
        self.assertEqual(status, 400)

    def test_errors_endpoint_kind_upstream_5xx(self) -> None:
        """上游 500 + capture_errors on → /api/errors kind=upstream_5xx 且 ?id= 含 response。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        bad_port = make_fake_upstream(False, fail_500=True)
        self.proc = start_proxy(bad_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % bad_port,
                        "capture_errors": True}).encode("utf-8"))
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m-500"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 500)
        resp.read()
        conn.close()
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        self.assertTrue(data["capture_errors"])
        self.assertGreaterEqual(data["count"], 1)
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("upstream_5xx", kinds)
        # 详情含 response 快照（newest-first → events[0] 为最新 500 事件）
        eid = data["events"][0]["id"]
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=%d" % eid)
        self.assertEqual(status, 200)
        ev = json.loads(body.decode("utf-8"))
        self.assertEqual(ev["kind"], "upstream_5xx")
        self.assertIn("response", ev)
        self.assertIsNotNone(ev["response"])

    def test_errors_detail_by_id_and_bad_params(self) -> None:
        """?id= 详情（含 body/response/exc）；404/400 分支。"""
        dead_port = free_port()
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        self.proc = start_proxy(dead_port, free_port(),
                                extra_env={"CTYUN_ADMIN_TOKEN": "sekret"})
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % dead_port,
                        "capture_errors": True}).encode("utf-8"))
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502)
        resp.read()
        conn.close()
        # ?id=1（本机 + 带 token 头）→ 200 含 body/exc
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=1",
                                    headers={"X-Admin-Token": "sekret"})
        self.assertEqual(status, 200)
        ev = json.loads(body.decode("utf-8"))
        self.assertEqual(ev["id"], 1)
        self.assertEqual(ev["kind"], "synth_502")
        self.assertIn("body", ev)
        self.assertIsNotNone(ev["body"])
        self.assertIn("exc", ev)
        self.assertIsNotNone(ev["exc"])
        # ?id=9999 → 404
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=9999",
                                    headers={"X-Admin-Token": "sekret"})
        self.assertEqual(status, 404)
        # ?id=abc → 400
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors?id=abc")
        self.assertEqual(status, 400)
        # 列表不含 body/response 键
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        for event in data["events"]:
            self.assertNotIn("body", event)
            self.assertNotIn("response", event)

    def test_errors_poison_hit(self) -> None:
        """poison=True 上游 + capture_errors on → kind=poison_hit。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        poison_port = make_fake_upstream(True)
        self.proc = start_proxy(poison_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % poison_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("poison_hit", kinds)

    def test_errors_eof_without_done(self) -> None:
        """截断流 + capture_errors on → kind=eof_without_done。"""
        # spec 测试列表写 "fault_finish_stream → eof_without_done"，但按 relay 代码实测
        # fault_finish_stream（reasoning+finish+DONE 尾段）attempt-2 final=True 走
        # _flush_primed fail-open，产出 EMPTY_RETRY(finish-no-usage) 而非 eof 标记；
        # eof_without_done 的真实签名是"有 content 无 [DONE] 即 EOF"（与既有
        # test_eof_without_done_marked_counted_and_persisted 同场景），故本测试用
        # body_override=SSE_A+SSE_B 复刻。五种 kind 的集成覆盖不受影响。
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream(body_override=SSE_A + SSE_B)
        self.proc = start_proxy(upstream_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("eof_without_done", kinds)

    def test_errors_empty_retry(self) -> None:
        """空流重试 + capture_errors on → kind=empty_retry。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream()
        self.proc = start_proxy(upstream_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body.decode("utf-8"))
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("empty_retry", kinds)

    def test_capture_errors_restart_roundtrip(self) -> None:
        """capture_errors 经 POST→GET→重启（seed_persist）roundtrip 保持。"""
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        # 重启并 seed 持久化文件
        persist_path = os.path.join(self.proc.persist_dir, "settings.json")
        with open(persist_path, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist=saved)
        status, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertTrue(cfg["capture_errors"],
                        "capture_errors must survive restart via seed_persist")

    def test_logs_endpoint_tail(self) -> None:
        """GET /api/logs?tail=20 返回 <=20 行。"""
        # 先发一个请求确保 stderr 有内容
        post_sse(self.proxy_port)
        status, body, ctype = admin_get(self.proc.admin_port, "/api/logs?tail=20")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"))
        data = json.loads(body.decode("utf-8"))
        self.assertLessEqual(len(data["lines"]), 20)
        self.assertIn("lines", data)
        self.assertIn("next_cursor", data)
        self.assertIn("oldest_seq", data)
        self.assertIn("ring_max", data)
        for line in data["lines"]:
            self.assertIn("seq", line)
            self.assertIn("line", line)
            self.assertIsInstance(line["seq"], int)

    def test_logs_endpoint_cursor_incremental(self) -> None:
        """cursor 增量拉取至 lines=[]。"""
        post_sse(self.proxy_port)
        # 先取 tail 获取 cursor
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=10")
        data = json.loads(body.decode("utf-8"))
        cursor = data["next_cursor"]
        # cursor 增量拉取
        status, body, _ = admin_get(
            self.proc.admin_port, "/api/logs?cursor=%d" % cursor)
        data = json.loads(body.decode("utf-8"))
        self.assertEqual(len(data["lines"]), 0,
                         "cursor at latest should return empty lines")
        self.assertEqual(data["next_cursor"], cursor)

    def test_logs_endpoint_invalid_params(self) -> None:
        """cursor+tail 同给 / 非 int → 400。"""
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?cursor=1&tail=10")
        self.assertEqual(status, 400)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?cursor=abc")
        self.assertEqual(status, 400)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=0")
        self.assertEqual(status, 400)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=2000")
        self.assertEqual(status, 400)

    def test_logs_line_contains_req(self) -> None:
        """日志行含 "REQ POST" 等请求记录。"""
        post_sse(self.proxy_port)
        status, body, _ = admin_get(self.proc.admin_port, "/api/logs?tail=50")
        data = json.loads(body.decode("utf-8"))
        lines_text = " ".join(l["line"] for l in data["lines"])
        self.assertIn("REQ POST", lines_text)

    def test_classifier_stats_regression_errors_total(self) -> None:
        """统计口径回归：errors_total 仅 502 合成路径 +1；现有断言不变。"""
        # 正常 SSE 请求不应增 errors_total
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        before = json.loads(body.decode("utf-8"))["errors_total"]
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        after = json.loads(body.decode("utf-8"))["errors_total"]
        self.assertEqual(after, before,
                         "normal SSE must not increment errors_total")
        # 502 合成错误应 +1
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()
        dead_port = free_port()
        self.proc = start_proxy(dead_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502)
        resp.read()
        conn.close()
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["errors_total"], 1)

    def test_header_stall_retry_success(self) -> None:
        """stall_calls=(1,) → 首呼 stall 触发 header-timeout 重试 → 次呼正常 → calls==2 + SSE 完整。"""
        upstream_port, calls = make_stall_upstream(stall_calls=(1,))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "header stall must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        # header_timeout 留痕事件
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("header-timeout", stderr,
                      "REQ line must carry retry_reason=header-timeout, stderr:\n" + stderr)
        # /api/errors 含 kind=header_timeout（需先开启 capture_errors）
        # 检查计数器
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved.get("header_retries_total", 0), 1,
                                "header_retries_total must be >=1 after stall retry")

    def test_header_stall_both_timeout_returns_502(self) -> None:
        """stall_all → 首呼+重试均超时 → 502 + calls==2 + synth_502 留痕含 retried=1。"""
        upstream_port, calls = make_stall_upstream(stall_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        body = b'{"model":"m","stream":true,"messages":[]}'
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 502, "double stall must synthesize 502, got %d" % status)
        self.assertEqual(len(calls), 2,
                         "stall_all must trigger exactly one retry then fail, calls=%d" % len(calls))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "502 REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=header-timeout", stderr,
                      "502 REQ line must carry retry_reason=header-timeout, stderr:\n" + stderr)

    def test_header_stall_retry_disabled(self) -> None:
        """CTYUN_HEADER_RETRY=0 + stall_all → 502 + calls==1（不重试）。"""
        upstream_port, calls = make_stall_upstream(stall_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1",
                                          "CTYUN_HEADER_RETRY": "0"})
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        body = b'{"model":"m","stream":true,"messages":[]}'
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 502,
                         "stall_all with retry disabled must return 502, got %d" % resp.status)
        resp.read()
        conn.close()
        self.assertEqual(len(calls), 1,
                         "CTYUN_HEADER_RETRY=0 must disable retry, calls=%d" % len(calls))

    def test_header_rst_retry_success(self) -> None:
        """rst_calls=(1,) → 首呼 RST 触发 conn-reset 重试 → 次呼正常 → calls==2 + SSE 完整。"""
        upstream_port, calls = make_rst_upstream(rst_calls=(1,))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "header RST must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=conn-reset", stderr,
                      "REQ line must carry retry_reason=conn-reset, stderr:\n" + stderr)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved.get("header_retries_total", 0), 1,
                                "header_retries_total must be >=1 after RST retry")

    def test_header_rst_both_timeout_returns_502(self) -> None:
        """rst_all → 首呼+重试均 RST → 502 + calls==2 + synth_502 留痕含 retried=1 / conn-reset。"""
        upstream_port, calls = make_rst_upstream(rst_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        body = b'{"model":"m","stream":true,"messages":[]}'
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 502, "double RST must synthesize 502, got %d" % status)
        self.assertEqual(len(calls), 2,
                         "rst_all must trigger exactly one retry then fail, calls=%d" % len(calls))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "502 REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=conn-reset", stderr,
                      "502 REQ line must carry retry_reason=conn-reset, stderr:\n" + stderr)

    def test_header_timeout_does_not_leak_into_body_phase(self) -> None:
        """滴流上游（chunk 间隔 1.5s > HEADER_TIMEOUT_S=1）→ 完整收流含 [DONE]。
        证明 getresponse 后的 settimeout(UPSTREAM_TIMEOUT) 生效，短超时未漏进体阶段。"""
        # 使用 body_override 构造慢速流：分块写，间隔 > HEADER_TIMEOUT_S
        import time as _time
        class SlowHandler(FakeUpstreamHandler):
            slow_body = SSE_A + SSE_B + SSE_DONE

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length > 0:
                    self.rfile.read(length)
                if self.calls is not None:
                    self.calls.append(1)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    # chunk 1: SSE_A
                    self.wfile.write(SSE_A)
                    self.wfile.flush()
                    _time.sleep(1.5)  # > HEADER_TIMEOUT_S=1
                    # chunk 2: SSE_B
                    self.wfile.write(SSE_B)
                    self.wfile.flush()
                    _time.sleep(1.5)
                    # chunk 3: SSE_DONE
                    self.wfile.write(SSE_DONE)
                    self.wfile.flush()
                except ConnectionError:
                    pass
                self.close_connection = True

            def log_message(self, format, *args):
                pass

        calls = []
        handler = type("SlowHandler", (SlowHandler,), {"calls": calls})
        server = HTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        FAKE_SERVERS.append(server)
        upstream_port = server.server_address[1]

        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        # 滴流各 chunk 间隔 1.5s > HEADER_TIMEOUT_S=1，若短超时泄漏到体阶段
        # 读 SSE_B 时必超时断开；完整收到即证明体阶段仍为 UPSTREAM_TIMEOUT=600s
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "drip stream must relay completely despite chunks spaced > HEADER_TIMEOUT_S, "
                         "proving body-phase timeout remains UPSTREAM_TIMEOUT. Got: %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "drip stream must stay result=ok, stderr:\n" + stderr)
        self.assertNotIn("header-timeout", stderr,
                         "body phase must not trigger header-timeout, stderr:\n" + stderr)

    def test_header_stall_then_empty_stream_compound(self) -> None:
        """stall_calls=(1,) + empty_stream_calls=(2,) → 首呼 header-stall 重试 →
        次呼（空流）触发空流重试 → 三呼正常 → calls==3 + retried=2。"""
        upstream_port, calls = make_stall_upstream(
            stall_calls=(1,), empty_stream_calls=(2,))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        # 呼 1: stall → header-timeout retry
        # 呼 2: empty_stream → 空流重试
        # 呼 3: 正常 body_override=None 走默认 SSE_A+SSE_B+SSE_DONE
        self.assertEqual(len(calls), 3,
                         "header stall + empty stream compound must yield 3 calls, got %d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-3 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=2", stderr,
                      "compound scenario must carry retried=2, stderr:\n" + stderr)
        self.assertIn("eof-priming", stderr,
                      "compound scenario must mention eof-priming, stderr:\n" + stderr)
        # header-timeout 事件仅入 ERROR_EVENTS 环（非 stderr），计数器可证 header retry 已发生
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved.get("header_retries_total", 0), 1,
                                "compound scenario must count header retry")


if __name__ == "__main__":
    import atexit
    atexit.register(kill_registered)
    # 默认 SIGTERM 直接终止不跑 atexit：转成解释器关闭路径，兜底清理才可执行
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    unittest.main(verbosity=2)