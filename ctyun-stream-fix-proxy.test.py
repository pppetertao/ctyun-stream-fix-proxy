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
import http.client
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
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
SSE_REASONING = b'data: {"choices":[{"delta":{"reasoning_content":"th"}}]}\n\n'

FAKE_SERVERS = []
_PROCS = []  # start_proxy 产物的注册表（atexit 兜底清理，防测试中断遗留孤儿）


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    poison = False
    tag = "/plain"  # /plain 响应携带的路径标记，供"上游热切换后路由命中"断言区分
    big = False     # True → 1.2MB 大 SSE 流，供 client-abort 测试把代理写缓冲打穿
    fail_500 = False  # True → do_POST 回 500 JSON（上游 5xx 透传计数测试用）
    empty_stream = False  # True → 每次 POST 回空流（reasoning 后 EOF，无 [DONE]）
    blank_stream = False  # True → 空流形态为零字节 body（200 + SSE 头 + 立即 EOF）
    body_override = None  # 非 None → 正常路径 body 用此值（priming 前缀/合法 DONE 场景）
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            self.rfile.read(length)
        if self.fail_500:
            body = b'{"error":"upstream exploded"}'
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.close_connection = True
            return
        if self.calls is not None:
            self.calls.append(1)
        if self.empty_stream or (self.calls is not None and len(self.calls) == 1
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
                       blank_stream: bool = False, body_override=None,
                       scripted: bool = False) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
             "empty_stream": empty_stream, "blank_stream": blank_stream,
             "body_override": body_override}
    if scripted:
        attrs["calls"] = []
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,), attrs)
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1]


def make_scripted_upstream(**kwargs) -> tuple:
    """带调用计数的假上游：返回 (port, calls)；calls 按上游被请求次数 append。
    kwargs 透传 empty_stream / blank_stream / body_override（勿传 scripted/poison）。"""
    port = make_fake_upstream(False, scripted=True, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls


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


def admin_get(port: int, path: str):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", path)
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

    def test_sse_data_line_kind_matrix(self) -> None:
        f = self.mod.sse_data_line_kind
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\n'), "content")
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\r\n'), "content")
        self.assertEqual(f(b'data: {"choices":[{"delta":{"content":""}}]}\n'), "noise")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{"tool_calls":[{"id":"c1"}]},"index":0}]}\n'),
            "content")
        self.assertEqual(
            f(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'), "content")
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

    def test_stats_persist_roundtrip_and_defaults(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit2-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0})
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{corrupt")
        self.assertEqual(mod.load_stats_counters(path),
                         {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
                          "empty_retries_total": 0})
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
                    "empty_retries_total", "active",
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
                               "errors_upstream": 1, "retries": 0, "days": 2})
        # 空窗口：全 0 + days=0
        self.assertEqual(f(daily, "2025-01-01", "2025-01-31"),
                         {"requests": 0, "filtered": 0, "errors_proxy": 0,
                          "errors_upstream": 0, "retries": 0, "days": 0})
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
                              "errors_upstream", "retries", "days"})
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
        # v3：dm entry 恒 5 字段 + 错误归因与 daily 总桶同口径（error→proxy，5xx→upstream）
        self.assertEqual(
            set(dm_today["m-a"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
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
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 5 字段——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            {"requests", "filtered", "errors_proxy", "errors_upstream", "retries"},
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
                           "errors_upstream": 2, "retries": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0}}}
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
                                                "errors_upstream": 2, "retries": 0}}},
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
                          "errors_upstream": 0, "retries": 0},
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
                    "empty_retries_total", "active",
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
        # v3：dm entry 5 键经 /api/stats 透出（纯新增键，旧客户端只读 3 键不受影响）
        dm_entry = snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]
        for key in ("requests", "filtered", "errors_proxy", "errors_upstream", "retries"):
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
                      r"ts=(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4})$",
                      stderr, re.M)
        self.assertIsNotNone(m, "REQ 行必须带 model= 与 ts= 字段，stderr:\n" + stderr)
        time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z")  # %z 回析：防平台差异静默退化

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


if __name__ == "__main__":
    import atexit
    atexit.register(kill_registered)
    # 默认 SIGTERM 直接终止不跑 atexit：转成解释器关闭路径，兜底清理才可执行
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    unittest.main(verbosity=2)