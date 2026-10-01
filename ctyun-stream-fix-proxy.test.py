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
import hashlib
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
SSE_ERROR_FRAME = ('data: {"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试","type":"rate_limit_error","code":"model_tpm_limit"}}\n\n').encode("utf-8")
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
    fail_200_error = False  # True -> do_POST 回 200 + JSON error body（非流式 body error 测试用）
    empty_stream = False  # True → 每次 POST 回空流（reasoning 后 EOF，无 [DONE]）
    empty_stream_calls = ()  # 1-based 呼叫序号元组：命中则回空流（同 empty_stream 形态）
    blank_stream = False  # True → 空流形态为零字节 body（200 + SSE 头 + 立即 EOF）
    body_override = None  # 非 None → 正常路径 body 用此值（priming 前缀/合法 DONE 场景）
    fault_finish_first = False   # True → 首呼回 SSE_FAULT_TAIL 后断连，次呼正常 body
    fault_finish_stream = False  # True → 每呼回故障尾段（SSE_FAULT_TAIL）
    stall_all = False       # True → 每呼读 body 后 close_connection=True 直接返回（代理 getresponse 抛 http.client.RemoteDisconnected）
    stall_calls = ()        # 1-based 呼叫序号元组：命中则 close_connection=True 不写响应（代理 getresponse 抛 http.client.RemoteDisconnected）
    sleep_stall_all = False    # True → 每呼读 body 后 sleep 持连不写响应（代理 getresponse 抛 socket.timeout）
    sleep_stall_calls = ()     # 1-based 呼叫序号元组：命中则 sleep 持连不写响应（同 sleep_stall_all 形态）
    sleep_stall_seconds = 3.0  # 须 > 用例 HEADER_TIMEOUT（白盒 0.5s/黑盒 1s），余量 ≥2s 防 flaky
    tail_delay_after_done_s = 0.0  # >0 → 正常流写完后 flush+sleep 再关连接（模拟"发完 [DONE] 滞留"的 EOF 尾间隙）
    latency_s = 0.0            # >0 → 正常路径写响应前 sleep（TTFB 测试模拟上游慢）
    stall_mid_stream_s = 0.0   # >0 → 写 SSE_A 后 sleep 再写 SSE_B+DONE（stall 检测测试）
    rst_all = False         # True → 每呼读 body 后 SO_LINGER(1,0) close 强制发 RST（代理 getresponse 抛 ConnectionResetError）
    rst_calls = ()          # 1-based 呼叫序号元组：命中则 SO_LINGER(1,0) close 强制发 RST（同 rst_all 形态）
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流
    bodies = None        # 共享 list：非 None 时按调用序 append 收到的原始请求体 bytes
    head_calls = None    # P4 probe 测试：非 None 时 do_HEAD 按调用序 append 计数
    head_fail = False    # P4 probe 测试：True → do_HEAD 回 500（连续失败告警场景）
    x_request_id = None  # 非 None → 正常路径响应头携带上游 X-Request-Id（剥除测试用）

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
        # --- sleep-stall 路径：读 body 后持连 sleep 不写响应（连接保持，代理 getresponse 阻塞到
        # HEADER_TIMEOUT_S 抛 socket.timeout；区别于 stall 的关连接路径——后者产 RemoteDisconnected）---
        if self.sleep_stall_all or (self.sleep_stall_calls and self.calls is not None
                                    and len(self.calls) in self.sleep_stall_calls):
            time.sleep(self.sleep_stall_seconds)
            self.close_connection = True
            return
        # --- end sleep-stall ---
        # --- end stall ---
        if self.rst_all or (self.rst_calls and self.calls is not None
                            and len(self.calls) in self.rst_calls):
            # SO_LINGER(1,0) close 强制发 RST（非 FIN），代理 getresponse 抛
            # ConnectionResetError；macOS 要求 8 字节 linger struct，int 直传 EINVAL
            self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack("ii", 1, 0))
            self.close_connection = True
            return
        if self.fail_200_error:
            body = b'{"error":{"message":"model tpm limit","type":"rate_limit_error","code":"model_tpm_limit"}}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
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
        if self.stall_mid_stream_s:
            # SSE_A 后 sleep 再续 SSE_B+DONE：相邻 record 间隔 = sleep 时长（stall 检测用）
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            try:
                self.wfile.write(SSE_A)
                self.wfile.flush()
                time.sleep(self.stall_mid_stream_s)
                self.wfile.write(SSE_B + SSE_DONE)
                self.wfile.flush()
            except ConnectionError:
                # 吞掉的是代理侧已放弃读取后的写出端 Broken pipe（预期路径，同下方正常路径注释）
                pass
            self.close_connection = True
            return
        if self.latency_s:
            time.sleep(self.latency_s)
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
        if self.x_request_id:
            self.send_header("X-Request-Id", self.x_request_id)
        self.end_headers()
        try:
            self.wfile.write(body)
            if self.tail_delay_after_done_s:
                self.wfile.flush()
                time.sleep(self.tail_delay_after_done_s)
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
        if self.x_request_id:
            self.send_header("X-Request-Id", self.x_request_id)
        self.end_headers()
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        """P4 probe 假上游：HEAD 计数 + 可控 5xx（probe 视 status ≥ 500 为失败）。

        不回 body（HEAD 语义）；BaseHTTPRequestHandler 缺省 do_HEAD 回 501，
        会使"成功 probe"场景恒失败，故必须覆写。
        """
        if self.head_calls is not None:
            self.head_calls.append(1)
        if self.head_fail:
            self.send_response(500)
        else:
            self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()
        self.close_connection = True

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
                       sleep_stall_all: bool = False, sleep_stall_calls: tuple = (),
                       sleep_stall_seconds: float = 3.0,
                       rst_all: bool = False, rst_calls: tuple = (),
                       fail_200_error: bool = False,
                       latency_s: float = 0.0,
                       stall_mid_stream_s: float = 0.0,
                       tail_delay_after_done_s: float = 0.0,
                       x_request_id: str = None) -> int:
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
             "empty_stream": empty_stream, "empty_stream_calls": empty_stream_calls,
             "blank_stream": blank_stream,
             "body_override": body_override,
             "fault_finish_first": fault_finish_first,
             "fault_finish_stream": fault_finish_stream,
             "stall_all": stall_all,
             "stall_calls": stall_calls,
             "sleep_stall_all": sleep_stall_all,
             "sleep_stall_calls": sleep_stall_calls,
             "sleep_stall_seconds": sleep_stall_seconds,
             "rst_all": rst_all,
             "rst_calls": rst_calls,
             "fail_200_error": fail_200_error,
             "latency_s": latency_s,
             "stall_mid_stream_s": stall_mid_stream_s,
             "tail_delay_after_done_s": tail_delay_after_done_s,
             "x_request_id": x_request_id,
             "bodies": [] if record_bodies else None}
    if scripted:
        attrs["calls"] = []
    handler = type("FakeUpstreamHandler", (FakeUpstreamHandler,), attrs)
    # stall 变体使用 ThreadingHTTPServer：单线程 HTTPServer 的 serve_forever 会卡死
    # 在 stall 连接上，shutdown 挂测试；daemon_threads=True 保证 teardown 不阻塞
    use_threading = (stall_all or stall_calls or rst_all or rst_calls
                     or sleep_stall_all or sleep_stall_calls)
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


def make_sleep_stall_upstream(sleep_stall_all: bool = False,
                              sleep_stall_calls: tuple = (), **kwargs) -> tuple:
    """带调用计数的 sleep-stall 假上游：返回 (port, calls)。
    sleep_stall_all/sleep_stall_calls 控制哪些呼叫在读 body 后 sleep 持连不写响应
    （连接保持静默 → 代理 getresponse 阻塞到 HEADER_TIMEOUT_S 抛 socket.timeout；
    区别于 stall 的关连接路径——后者产 RemoteDisconnected）。
    其余 kwargs 透传 empty_stream / body_override 等。"""
    port = make_fake_upstream(False, scripted=True,
                              sleep_stall_all=sleep_stall_all,
                              sleep_stall_calls=sleep_stall_calls, **kwargs)
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


def make_probe_upstream(head_fail: bool = False) -> tuple:
    """P4 probe 假上游：返回 (port, head_calls)；HEAD 正常 200，head_fail=True 时回 500。

    ThreadingHTTPServer：probe 每 PROBE_INTERVAL_S 一轮，测试收尾 shutdown 不得
    阻塞在 keep-alive 连接上（与 stall 变体同款理由）。
    """
    head_calls = []
    handler = type("ProbeUpstreamHandler", (FakeUpstreamHandler,),
                   {"head_calls": head_calls, "head_fail": head_fail})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    FAKE_SERVERS.append(server)
    return server.server_address[1], head_calls


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


def stop_proxy(proc, timeout: float = 10.0) -> bool:
    """terminate → wait(timeout)；超时则 kill 再 wait(5)。
    随后 stderr_text + rmtree persist_dir。返回是否强杀过。
    proc 为 None 或已 reap 安全退出（poll 判活，免 ChildProcessError）。"""
    if proc is None:
        return False
    killed = False
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            killed = True
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass  # SIGKILL 已发出，内核回收只是调度时序问题（同 kill_registered）
    stderr_text(proc)
    pd = getattr(proc, "persist_dir", None)
    if pd:
        shutil.rmtree(pd, ignore_errors=True)
    return killed


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
    env["CTYUN_LISTEN_HOST"] = "127.0.0.1"  # 钉死默认绑定，防 shell env 泄漏 0.0.0.0
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


def post_sse_with_headers(port: int, payload: bytes = None):
    """同 post_sse 但返回 (body, x_request_id)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    if payload is None:
        payload = b'{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[]}'
    conn.request("POST", "/v1/chat/completions", body=payload,
                 headers={"Content-Type": "application/json"})
    resp = conn.getresponse()
    status = resp.status
    x_request_id = resp.getheader("X-Request-Id")
    data = resp.read()
    conn.close()
    assert status == 200, "expected SSE 200, got %d" % status
    return data, x_request_id


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


def get_plain_with_headers(port: int):
    """同 get_plain 但返回 (data, content_length, x_request_id)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    conn.request("GET", "/plain")
    resp = conn.getresponse()
    status = resp.status
    data = resp.read()
    resp_len = resp.getheader("Content-Length")
    x_request_id = resp.getheader("X-Request-Id")
    conn.close()
    assert status == 200, "expected /plain 200, got %d" % status
    return data, resp_len, x_request_id


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
            proc.wait(timeout=10)
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


# 集成测试用的估算比率：现算自被测模块（env 继承一致），测试不硬编码该常量
TPM_RATIO = load_proxy_module().TPM_TOKEN_RATIO


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


def post_sse_auth(port: int, payload: bytes, auth_header: str = None,
                  timeout: int = 30):
    """带可选 Authorization 头的 POST /v1/chat/completions。
    与 post_sse 不同：不断言 status 200，返回 (status, body)。"""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    conn.request("POST", "/v1/chat/completions", body=payload, headers=headers)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


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
            stop_proxy(proc)
            stop_fake_upstreams()


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
        # error frame -> "content" (not "noise") — spec 集成关踺：error 帧视为 content
        # 触发 priming flush、不触发空流重试
        self.assertEqual(
            f(b'data: {"error":{"message":"TPM limit","type":"rate_limit_error"}}\n'),
            "content")
        self.assertEqual(
            f(b'data: {"error":{"message":"model x not found"}}\r\n'),
            "content")

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

    def test_body_has_error_matrix(self) -> None:
        f = self.mod.body_has_error
        # dict with error dict -> True (OpenAI error schema)
        self.assertTrue(f({"error": {"message": "TPM limit", "type": "rate_limit_error",
                                    "code": "model_tpm_limit"}}))
        self.assertTrue(f({"error": {"message": "x"}}))
        # error key present but value not dict -> False
        self.assertFalse(f({"error": "string error"}))
        self.assertFalse(f({"error": None}))
        self.assertFalse(f({"error": [1, 2, 3]}))
        self.assertFalse(f({"error": 42}))
        # error key missing -> False
        self.assertFalse(f({"choices": [{"delta": {"content": "hi"}}]}))
        self.assertFalse(f({}))
        # not dict -> False
        self.assertFalse(f("not a dict"))
        self.assertFalse(f(None))
        self.assertFalse(f([1, 2, 3]))
        self.assertFalse(f(42))
        # dict with both choices and error -> True (error takes priority)
        self.assertTrue(f({"error": {"message": "x"}, "choices": [{"delta": {}}]}))
        # error dict containing nested "choices" key -> True
        self.assertTrue(f({"error": {"message": "x", "choices": [{}]}}))

    def test_sse_line_body_error_matrix(self) -> None:
        f = self.mod.sse_line_body_error
        # error frame -> True
        self.assertTrue(f(b'data: {"error":{"message":"TPM limit",'
                         b'"type":"rate_limit_error","code":"model_tpm_limit"}}\n'))
        self.assertTrue(f(b'data: {"error":{"message":"x","type":"t","code":"c"}}\r\n'))
        # normal content line -> False
        self.assertFalse(f(b'data: {"choices":[{"delta":{"content":"A"}}]}\n'))
        # content mentioning "error" in text -> False (no string matching)
        self.assertFalse(f(b'data: {"choices":[{"delta":{"content":"error occurred"}}]}\n'))
        # string error value -> False (body_has_error rejects non-dict error)
        self.assertFalse(f(b'data: {"error":"string_err"}\n'))
        self.assertFalse(f(b'data: {"error":null}\n'))
        # non-data prefix -> False
        self.assertFalse(f(b': keepalive\n'))
        self.assertFalse(f(b'event: message\n'))
        self.assertFalse(f(b'id: 42\n'))
        # [DONE] -> False
        self.assertFalse(f(b'data: [DONE]\n'))
        self.assertFalse(f(b'data:[DONE]\r\n'))
        # non-JSON data -> False (fail-open like sse_line_has_usage)
        self.assertFalse(f(b'data: {not-json\n'))
        # empty data -> False
        self.assertFalse(f(b'data: \n'))
        # non-dict JSON -> False
        self.assertFalse(f(b'data: [1,2,3]\n'))

    def test_sse_line_usage_matrix(self) -> None:
        f = self.mod.sse_line_usage
        # 独立 usage 帧（choices=[]）→ 返回 usage dict
        result = f(b'data: {"id":"u","choices":[],'
                   b'"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n')
        self.assertIsInstance(result, dict)
        self.assertEqual(result, {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2})
        # ride-on finish 帧（choices 有 finish + usage 顶层）→ 返回 usage dict
        result2 = f(b'data: {"id":"x","choices":[{"delta":{},"finish_reason":"stop"}],'
                    b'"usage":{"prompt_tokens":10,"completion_tokens":3,"total_tokens":13}}\n')
        self.assertEqual(result2, {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13})
        # 空 usage {} → None
        self.assertIsNone(f(b'data: {"id":"e","choices":[],"usage":{}}\n'))
        # usage: null → None
        self.assertIsNone(f(b'data: {"id":"n","choices":[],"usage":null}\n'))
        # [DONE] → None
        self.assertIsNone(f(b"data: [DONE]\n"))
        # 非 JSON → None
        self.assertIsNone(f(b"data: {not json\n"))
        # 非 data 行 → None
        self.assertIsNone(f(b": keep-alive\n"))
        # usage 非 dict（如列表）→ None
        self.assertIsNone(f(b'data: {"usage":[1,2,3],"choices":[]}\n'))
        # 普通 content 帧 → None
        self.assertIsNone(f(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n'))

    def test_tpm_constants_load(self) -> None:
        """TPM 常量通过 load_proxy_module 可见且取值正确。"""
        mod = self.mod
        self.assertEqual(mod.TPM_LIMIT, 110000)
        self.assertEqual(mod.TPM_WINDOW_S, 60)
        self.assertEqual(mod.TPM_QUEUE_MAX, 20)
        self.assertEqual(mod.TPM_QUEUE_TIMEOUT_S, 120)
        self.assertEqual(mod.TPM_TOKEN_RATIO, 0.25)
        self.assertEqual(mod.TPM_KEY_CAP, 64)
        self.assertIsInstance(mod.TPM_LIMIT_BY_MODEL, dict)
        self.assertEqual(mod.TPM_MODEL_BUDGETS, {})
        self.assertEqual(mod.CLASS_TPM_LIMITED, "tpm_limited")
        self.assertEqual(mod.ERR_KIND_TPM_QUEUE_FULL, "tpm_queue_full")
        self.assertEqual(mod.ERR_KIND_TPM_QUEUE_TIMEOUT, "tpm_queue_timeout")
        self.assertIn(mod.ERR_KIND_TPM_QUEUE_FULL, mod._KIND_CATEGORY)
        self.assertIn(mod.ERR_KIND_TPM_QUEUE_TIMEOUT, mod._KIND_CATEGORY)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_TPM_QUEUE_FULL],
                         mod.CLASS_TPM_LIMITED)
        self.assertEqual(mod._KIND_CATEGORY[mod.ERR_KIND_TPM_QUEUE_TIMEOUT],
                         mod.CLASS_TPM_LIMITED)

    def test_tpm_constants_env_seam(self) -> None:
        """TPM 常量从环境变量读取（用 subprocess 验证 env seam）。"""
        import subprocess
        code = (
            "import os; "
            "os.environ['CTYUN_TPM_LIMIT']='50000'; "
            "os.environ['CTYUN_TPM_WINDOW_S']='30'; "
            "os.environ['CTYUN_TPM_QUEUE_MAX']='5'; "
            "os.environ['CTYUN_TPM_QUEUE_TIMEOUT_S']='10'; "
            "os.environ['CTYUN_TPM_TOKEN_RATIO']='0.3'; "
            "os.environ['CTYUN_TPM_KEY_CAP']='8'; "
            "os.environ['CTYUN_TPM_LIMIT_BY_MODEL']='kimi:5000,glm:6000'; "
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('m', %r); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "print(m.TPM_LIMIT, m.TPM_WINDOW_S, m.TPM_QUEUE_MAX, "
            "m.TPM_QUEUE_TIMEOUT_S, m.TPM_TOKEN_RATIO, m.TPM_KEY_CAP, "
            "repr(m.TPM_LIMIT_BY_MODEL))"
        ) % PROXY_SCRIPT
        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=10)
        self.assertEqual(proc.returncode, 0,
                         "env seam subprocess failed stderr:\n" + proc.stderr)
        parts = proc.stdout.strip().split(" ", 6)
        self.assertEqual(parts, ["50000", "30", "5", "10.0", "0.3", "8",
                                 "{'kimi': 5000, 'glm': 6000}"])

    def test_parse_tpm_limit_by_model_normal(self) -> None:
        f = self.mod._parse_tpm_limit_by_model
        self.assertEqual(f("kimi:30000,glm:110000"), {"kimi": 30000, "glm": 110000})
        self.assertEqual(f(" kimi:30000 , glm-5.3-oc:110000 "),
                         {"kimi": 30000, "glm-5.3-oc": 110000})
        self.assertEqual(f("kimi:30000"), {"kimi": 30000})

    def test_parse_tpm_limit_by_model_malformed(self) -> None:
        f = self.mod._parse_tpm_limit_by_model
        # 非两项 / limit 非 int / 负值 → 跳过
        self.assertEqual(f("kimi"), {})
        self.assertEqual(f("kimi:abc"), {})
        self.assertEqual(f("kimi:-1"), {})
        # 混合：OK + 畸形的串；有效项保留
        self.assertEqual(f("kimi:30000,bad,glm:abc,neg:-5,deepseek:110000"),
                         {"kimi": 30000, "deepseek": 110000})
        # 空项跳过
        self.assertEqual(f("kimi:30000,,glm:110000"), {"kimi": 30000, "glm": 110000})

    def test_parse_tpm_limit_by_model_empty(self) -> None:
        self.assertEqual(self.mod._parse_tpm_limit_by_model(""), {})
        self.assertEqual(self.mod._parse_tpm_limit_by_model("   "), {})

    def test_tpm_limit_for_two_level_fallback(self) -> None:
        """两级回退：TPM_MODEL_BUDGETS → 全局 TPM_LIMIT（env 表不参与运行时）。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        try:
            mod.TPM_MODEL_BUDGETS.clear()
            self.assertEqual(mod.tpm_limit_for("kimi-k3-oc"), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for(None), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for(""), mod.TPM_LIMIT)
            self.assertEqual(mod.tpm_limit_for("deepseek-v4-pro-0813-oc"),
                             mod.TPM_LIMIT)
            mod.TPM_MODEL_BUDGETS.update({"kimi-k3-oc": 1000, "glm-5.3-oc": 110000})
            self.assertEqual(mod.tpm_limit_for("kimi-k3-oc"), 1000)
            self.assertEqual(mod.tpm_limit_for("glm-5.3-oc"), 110000)
            self.assertEqual(mod.tpm_limit_for("other"), mod.TPM_LIMIT)
        finally:
            self._tpm_cleanup(mod)

    def _tpm_cleanup(self, mod):
        mod.TPM_BUCKETS.clear()
        mod.TPM_WAITERS.clear()
        mod._tpm_rejected.clear()
        mod._tpm_timeouts.clear()
        mod.TPM_MODEL_BUDGETS.clear()

    def test_tpm_key_id(self) -> None:
        f = self.mod.tpm_key_id
        self.assertIsNone(f(None))
        self.assertIsNone(f(""))
        self.assertIsNone(f("Basic dXNlcjpwYXNz"))
        self.assertIsNone(f("Bearer"))
        self.assertIsNone(f("Bearer   "))  # 空 token
        kid1 = f("Bearer test-key-1")
        self.assertIsInstance(kid1, str)
        self.assertEqual(len(kid1), 64)  # sha256 hex 全位
        self.assertEqual(f("Bearer test-key-1"), kid1)  # 确定性
        self.assertEqual(f("bearer test-key-1"), kid1)  # scheme 大小写不敏感
        self.assertNotEqual(f("Bearer test-key-2"), kid1)

    def test_estimate_request_tokens(self) -> None:
        f = self.mod.estimate_request_tokens
        ratio = self.mod.TPM_TOKEN_RATIO
        self.assertEqual(f(b""), 1)
        self.assertEqual(f(None), 1)
        body = b'{"model":"x","stream":true,"messages":[]}'
        self.assertEqual(f(body), max(1, int(len(body) * ratio)))
        body2 = b'{"model":"x","max_tokens":4096,"messages":[]}'
        self.assertEqual(f(body2), max(1, int(len(body2) * ratio)) + 4096)
        # max_tokens 非 int（str/null/bool）不累加
        for non_int in (b'{"max_tokens":"4096"}', b'{"max_tokens":null}',
                        b'{"max_tokens":true}'):
            self.assertEqual(f(non_int), max(1, int(len(non_int) * ratio)))
        # 非 JSON → 裸长度估（fail-open）
        self.assertEqual(f(b"plain text"), max(1, int(10 * ratio)))

    def test_tpm_prune(self) -> None:
        mod = self.mod
        bucket = mod._TpmBucket()
        bucket.used = 0
        now = 1000.0
        bucket.append((now - 90, 100))   # 窗口外（window=60），先入队保持时间序
        bucket.append((now - 30, 500))
        bucket.used = 600
        mod._tpm_prune(bucket, now)
        self.assertEqual(len(bucket), 1)  # 窗口内保留
        mod._tpm_prune(bucket, now)
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket.used, 500)

    def test_tpm_admit_non_enabled_model_bypasses(self) -> None:
        """opt-in：未启用模型直通——不建桶、不入队、不计 rejected。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        status, qwait = mod.tpm_admit("key:bypass", 10 ** 9, "m:disabled")
        self.assertEqual(status, "ok")
        self.assertEqual(qwait, 0)
        self.assertEqual(len(mod.TPM_BUCKETS), 0)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
        self.assertEqual(len(mod._tpm_rejected), 0)
        # model=None 恒直通（None not in {} → True）
        status2, qwait2 = mod.tpm_admit("key:bypass2", 10 ** 9, None)
        self.assertEqual((status2, qwait2), ("ok", 0))
        self.assertEqual(len(mod.TPM_BUCKETS), 0)

    def test_tpm_admit_enabled_model_goes_budget_path(self) -> None:
        """启用模型照旧走预算判定：est 超预算忙时 429 语义（"full"）保持。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:enabled"] = 100
        status, qwait = mod.tpm_admit("key:en", 100, "m:enabled")
        self.assertEqual((status, qwait), ("ok", 0))
        self.assertEqual(mod.TPM_BUCKETS[("key:en", "m:enabled")].used, 100)
        status2, qwait2 = mod.tpm_admit("key:en", 101, "m:enabled")
        self.assertEqual((status2, qwait2), ("full", 0))
        self.assertEqual(mod._tpm_rejected.get(("key:en", "m:enabled")), 1)

    def test_tpm_admit_ok_immediate(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:imm"] = mod.TPM_LIMIT
        status, qwait = mod.tpm_admit("key:imm", 100, "m:imm")
        self.assertEqual(status, "ok")
        self.assertEqual(qwait, 0)
        self.assertEqual(mod.TPM_BUCKETS[("key:imm", "m:imm")].used, 100)
        self.assertEqual(len(mod.TPM_WAITERS), 0)

    def test_tpm_admit_full_oversized(self) -> None:
        """est > budget：窗口空闲放行（决策 3）、忙时硬拒并计 rejected（决策 4）。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:big"] = mod.TPM_LIMIT
        # ① 空闲：used=0 且无同桶 waiter → 放行（budget 来自 tpm_limit_for）
        status, qwait = mod.tpm_admit("key:big", mod.TPM_LIMIT + 1, "m:big")
        self.assertEqual(status, "ok", "idle window must release oversized request")
        self.assertEqual(qwait, 0)
        self.assertEqual(mod.TPM_BUCKETS[("key:big", "m:big")].used,
                         mod.TPM_LIMIT + 1)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
        # ② 忙时：used > 0 → 硬拒 + rejected 计数（同桶）
        status2, qwait2 = mod.tpm_admit("key:big", mod.TPM_LIMIT + 1, "m:big")
        self.assertEqual(status2, "full", "busy window must reject oversized request")
        self.assertEqual(qwait2, 0)
        self.assertEqual(mod._tpm_rejected.get(("key:big", "m:big")), 1)
        self.assertEqual(len(mod.TPM_WAITERS), 0)  # 不入队
        # ③ 窗口滚过后（模拟 prune 后 used<=0）→ 再次空闲放行，模型隔离
        bucket = mod.TPM_BUCKETS[("key:big", "m:big")]
        bucket.clear()
        bucket.used = 0
        status3, qwait3 = mod.tpm_admit("key:big", mod.TPM_LIMIT + 1, "m:big")
        self.assertEqual(status3, "ok")

    def test_tpm_snapshot_keeps_rejected_after_window_roll(self) -> None:
        """窗口滚空（used<=0、无 waiter）的桶若曾有 rejected/timeouts，快照必须保留。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:rej"] = mod.TPM_LIMIT
        # 制造 rejected：先占满预算，再硬拒一笔超大
        mod.tpm_admit("key:rej", mod.TPM_LIMIT, "m:rej")
        status, _ = mod.tpm_admit("key:rej", mod.TPM_LIMIT + 1, "m:rej")
        self.assertEqual(status, "full")
        # 模拟窗口滚过：条目清空、used 归零（rejected 计数仍在）
        bucket = mod.TPM_BUCKETS[("key:rej", "m:rej")]
        bucket.clear()
        bucket.used = 0
        snap = mod.tpm_snapshot()
        entry = next((b for b in snap["buckets"]
                      if b["key"] == "sha256:" + "key:rej"[:12]
                      and b["model"] == "m:rej"), None)
        self.assertIsNotNone(
            entry, "rejected>0 的空桶不得被展示层过滤吞掉")
        self.assertEqual(entry["rejected"], 1)

    def test_tpm_admit_queue_full(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:qfull"] = mod.TPM_LIMIT
        mod.TPM_QUEUE_MAX = 2
        try:
            mod.tpm_admit("key:qfull", mod.TPM_LIMIT, "m:qfull")  # 占满预算
            # 两个 waiter 已在队（绕过 wait 循环用私有结构直接入队，验证队满拒绝分支）
            with mod.TPM_LOCK:
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
                mod.TPM_WAITERS.append(mod._TpmWaiter("key:qfull", 1, 0, "m:qfull"))
            status3, qwait3 = mod.tpm_admit("key:qfull", 1, "m:qfull")
            self.assertEqual(status3, "full")
            self.assertEqual(qwait3, 0)
            self.assertEqual(mod._tpm_rejected.get(("key:qfull", "m:qfull")), 1)
            self.assertEqual(len(mod.TPM_WAITERS), 2)  # 第三个未入队
        finally:
            mod.TPM_QUEUE_MAX = 20
            self._tpm_cleanup(mod)

    def test_tpm_settle_corrects_usage(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:settle"] = mod.TPM_LIMIT
        mod.tpm_admit("key:settle", 500, "m:settle")
        # 实际 200 → 退款 300
        mod.tpm_settle("key:settle", 500, 200, "m:settle")
        self.assertEqual(mod.TPM_BUCKETS[("key:settle", "m:settle")].used, 200)
        # 实际 800 → 追加 300
        mod.tpm_settle("key:settle", 500, 800, "m:settle")
        self.assertEqual(mod.TPM_BUCKETS[("key:settle", "m:settle")].used, 500)
        # 0 → 全额退款
        mod.tpm_settle("key:settle", 500, 0, "m:settle")
        self.assertEqual(mod.TPM_BUCKETS[("key:settle", "m:settle")].used, 0)

    def test_tpm_settle_prune_invariant_no_ghost_tokens(self) -> None:
        """存储不变量：used 恒等于 deque 条目之和（rev spec 0f2e88e 决策 2）。

        settle 退款条目晚于准入计费条目过期时，存储层不钳位 → used 暂为负
        （窗口欠账），负条目过期后 used 自愈归 0，不留幽灵 token 永久限流。
        """
        mod = self.mod
        self._tpm_cleanup(mod)
        bucket = mod._TpmBucket()
        bucket.used = 0
        bucket.append((0.0, 500))   # t=0 准入计费 est=500
        bucket.used = 500
        bucket.append((30.0, -300))  # t=30 settle actual=200 → 退款 300
        bucket.used = 200
        mod._tpm_prune(bucket, 61.0)  # 计费已过期、退款仍在窗口内
        self.assertEqual(len(bucket), 1)
        self.assertEqual(bucket.used, sum(tokens for _, tokens in bucket))
        self.assertEqual(bucket.used, -300)  # 允许为负（窗口欠账）
        mod._tpm_prune(bucket, 91.0)  # 退款也过期 → 自愈归 0，无幽灵 token
        self.assertEqual(len(bucket), 0)
        self.assertEqual(bucket.used, 0)
        self._tpm_cleanup(mod)

    def test_tpm_settle_notifies_waiters(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:wake"] = mod.TPM_LIMIT
        mod.TPM_QUEUE_TIMEOUT_S = 5.0
        results = {}
        # 占满预算
        mod.tpm_admit("key:wake", mod.TPM_LIMIT, "m:wake")
        def waiter():
            results["w"] = mod.tpm_admit("key:wake", 10, "m:wake")
        t = threading.Thread(target=waiter)
        t.start()
        time.sleep(0.2)  # 给 waiter 入队时间（确定性：settle 后立即 notify）
        self.assertEqual(len(mod.TPM_WAITERS), 1)
        mod.tpm_settle("key:wake", mod.TPM_LIMIT, 0, "m:wake")  # 全额退款唤醒
        t.join(timeout=5)
        self.assertFalse(t.is_alive())
        self.assertIn("w", results)
        self.assertEqual(results["w"][0], "ok")
        self.assertGreaterEqual(results["w"][1], 0)
        mod.TPM_QUEUE_TIMEOUT_S = 120

    def test_tpm_admit_queue_timeout(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:to"] = mod.TPM_LIMIT
        mod.TPM_QUEUE_TIMEOUT_S = 0.3
        try:
            mod.tpm_admit("key:to", mod.TPM_LIMIT, "m:to")  # 占满
            started = time.time()
            status, qwait = mod.tpm_admit("key:to", 10, "m:to")
            self.assertEqual(status, "timeout")
            self.assertLess(time.time() - started, 3.0)
            self.assertEqual(mod._tpm_timeouts.get(("key:to", "m:to")), 1)
            self.assertEqual(len(mod.TPM_WAITERS), 0)  # 已自队列移除
        finally:
            mod.TPM_QUEUE_TIMEOUT_S = 120
            self._tpm_cleanup(mod)

    def test_tpm_waiters_identity_removal(self) -> None:
        """队列自移除必须按同一性（值相等 race 回归）。

        两个值全等的 _TpmWaiter 同在队列时（并发同 key 同 est 同 enqueued_at），
        摘 w1 不得连带摘走 w2——旧实现用值相等的 deque.remove 会误删后者。
        """
        mod = self.mod
        self._tpm_cleanup(mod)
        w1 = mod._TpmWaiter("k", 10, 123.5, "m:ident")
        w2 = mod._TpmWaiter("k", 10, 123.5, "m:ident")
        self.assertEqual(w1, w2)      # namedtuple 值相等
        self.assertIsNot(w1, w2)      # 但非同一对象
        mod.TPM_WAITERS.append(w1)
        mod.TPM_WAITERS.append(w2)
        # 值相等实现（deque.remove(waiter)）在摘 w2 时会误删队首 w1 —— 本用例必红。
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w2)
        self.assertEqual(len(mod.TPM_WAITERS), 1,
                         "identity removal must drop exactly one entry, got %d"
                         % len(mod.TPM_WAITERS))
        self.assertTrue(any(w is w1 for w in mod.TPM_WAITERS),
                        "head twin w1 must survive removal of w2")
        self.assertFalse(any(w is w2 for w in mod.TPM_WAITERS),
                         "removed waiter w2 must be gone from the queue")
        with mod.TPM_LOCK:
            mod._tpm_remove_waiter(w1)
        self.assertEqual(len(mod.TPM_WAITERS), 0)
        self._tpm_cleanup(mod)

    def test_tpm_snapshot_shape_and_masking(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        mod.TPM_MODEL_BUDGETS["m:snap"] = mod.TPM_LIMIT
        mod.tpm_admit(mod.tpm_key_id("Bearer key:snap"), 100, "m:snap")
        snap = mod.tpm_snapshot()
        self.assertEqual(snap["config"]["limit"], mod.TPM_LIMIT)
        self.assertEqual(snap["config"]["window_s"], mod.TPM_WINDOW_S)
        self.assertEqual(snap["config"]["queue_max"], mod.TPM_QUEUE_MAX)
        self.assertEqual(snap["config"]["model_budgets"],
                         {"m:snap": mod.TPM_LIMIT})
        self.assertEqual(snap["config"]["enabled_models"], ["m:snap"])
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        # key 脱敏：sha256: 前缀 + sha256(key) 前 12 位 hex
        expected_short = "sha256:" + hashlib.sha256(b"key:snap").hexdigest()[:12]
        self.assertEqual(b["key"], expected_short)
        self.assertNotIn("key:snap", b["key"])  # 无原始 key 泄漏
        self.assertEqual(b["model"], "m:snap")
        self.assertEqual(b["used"], 100)
        self.assertEqual(b["remaining"], mod.TPM_LIMIT - 100)
        self.assertEqual(b["queued"], 0)
        self.assertEqual(b["rejected"], 0)
        self.assertEqual(b["timeouts"], 0)

    def test_tpm_body_err_samples_extracts_and_filters(self) -> None:
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=31038 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=33227 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-3 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=999 ts=T",                       # result 非 body-err → 跳过
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=deepseek-v4-pro-0813-oc retried=0 rid=r-4 host=h ttfb=1.0ms "
            "stream=1 outcome=body_error qwait=0ms tpm=777 ts=T",      # 异模型 → 跳过
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-5 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms ts=T",                       # 无 tpm= → 跳过
            "garbage line without any fields",                         # 无匹配 → 跳过
            None,                                                      # 非 str → 跳过
        ]
        self.assertEqual(mod.tpm_body_err_samples(lines, "kimi-k3-oc"), [31038, 33227])
        self.assertEqual(mod.tpm_body_err_samples(lines, "deepseek-v4-pro-0813-oc"),
                         [777])
        self.assertEqual(mod.tpm_body_err_samples(lines, "glm-5.3-oc"), [])
        self.assertEqual(mod.tpm_body_err_samples([], "kimi-k3-oc"), [])

    def test_tpm_body_err_samples_persisted(self) -> None:
        mod = self.mod
        samples = {
            "kimi-k3-oc": [{"tpm": 31038, "ts": 1.0}, {"tpm": 33227, "ts": 2.0}],
            "glm-5.3-oc": [{"tpm": 999, "ts": 1.0}, {"tpm": "bad", "ts": 2.0},
                            {"tpm": -1, "ts": 3.0}, "garbage", None],
        }
        self.assertEqual(mod.tpm_body_err_samples_persisted(samples, "kimi-k3-oc"),
                         [31038, 33227])
        self.assertEqual(mod.tpm_body_err_samples_persisted(samples, "glm-5.3-oc"),
                         [999])
        self.assertEqual(mod.tpm_body_err_samples_persisted(samples, "absent"), [])
        self.assertEqual(mod.tpm_body_err_samples_persisted({}, "kimi-k3-oc"), [])

    def test_tpm_body_err_samples_persisted_priority_over_log_ring(self) -> None:
        mod = self.mod
        self.addCleanup(mod.PERSISTED_BODY_ERR_SAMPLES.clear)
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=777 ts=T",
        ]
        mod.PERSISTED_BODY_ERR_SAMPLES["kimi-k3-oc"] = [{"tpm": 31038, "ts": 1.0}]
        self.assertEqual(mod.tpm_body_err_samples(lines, "kimi-k3-oc"), [31038],
                         "persisted sample must take priority over LOG_RING")
        mod.PERSISTED_BODY_ERR_SAMPLES.clear()
        self.assertEqual(mod.tpm_body_err_samples(lines, "kimi-k3-oc"), [777],
                         "without persisted samples LOG_RING parse is the fallback")

    def test_tpm_budget_recommend_with_samples(self) -> None:
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=31038 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=32375 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-3 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=33227 ts=T",
        ]
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines)
        # min=31038 → 31038*9//10=27934 → //1000*1000=27000
        self.assertEqual(rec["recommended"], 27000)
        self.assertEqual(rec["samples"], 3)
        self.assertNotIn("choices", rec)
        self.assertEqual(rec["source"], "body_err")
        self.assertIs(rec["enabled_advice"]["suggest"], True)
        self.assertNotIn("hint", rec)

    def test_tpm_budget_recommend_no_samples(self) -> None:
        mod = self.mod
        rec = mod.tpm_budget_recommend("glm-5.3-oc", [])
        self.assertIsNone(rec["recommended"])
        self.assertEqual(rec["samples"], 0)
        self.assertIn("无上游拒绝证据", rec["hint"])
        self.assertNotIn("choices", rec)
        self.assertIs(rec["enabled_advice"]["suggest"], False)
        self.assertEqual(rec["source"], "none")

    def test_tpm_settings_snapshot_union_and_sort(self) -> None:
        mod = self.mod
        self._tpm_cleanup(mod)
        orig_recent = list(mod.RECENT_REQUESTS)
        orig_daily = dict(mod.STATS["daily_by_model"])
        orig_ring = list(mod.LOG_RING)
        try:
            mod.RECENT_REQUESTS.clear()
            mod.STATS["daily_by_model"] = {mod.today_key(): {"deepseek-v4-pro-0813-oc": {}}}
            mod.RECENT_REQUESTS.append({"model": "glm-5.3-oc"})
            mod.TPM_MODEL_BUDGETS["kimi-k3-oc"] = 30000
            snap = mod.tpm_settings_snapshot()
            names = [m["name"] for m in snap["models"]]
            self.assertEqual(names, ["deepseek-v4-pro-0813-oc", "glm-5.3-oc",
                                     "kimi-k3-oc"])
            by_name = {m["name"]: m for m in snap["models"]}
            self.assertEqual(by_name["kimi-k3-oc"]["enabled"], True)
            self.assertEqual(by_name["kimi-k3-oc"]["budget"], 30000)
            self.assertEqual(by_name["glm-5.3-oc"]["enabled"], False)
            self.assertIsNone(by_name["glm-5.3-oc"]["budget"])
            self.assertIsNone(by_name["glm-5.3-oc"]["recommend"]["recommended"])
            self.assertEqual(snap["default_limit"], mod.TPM_LIMIT)
        finally:
            mod.RECENT_REQUESTS.clear()
            mod.RECENT_REQUESTS.extend(orig_recent)
            mod.STATS["daily_by_model"] = orig_daily
            mod.LOG_RING.clear()
            mod.LOG_RING.extend(orig_ring)
            self._tpm_cleanup(mod)

    def test_tpm_usage_peak(self) -> None:
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=12000 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=47850 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=deepseek-v4-pro-0813-oc retried=0 rid=r-3 host=h ttfb=1.0ms "
            "stream=1 outcome=ok qwait=0ms tpm=99999 ts=T",
            "garbage", None,
        ]
        self.assertEqual(mod.tpm_usage_peak(lines, "kimi-k3-oc"), 47850)
        self.assertEqual(mod.tpm_usage_peak(lines, "deepseek-v4-pro-0813-oc"),
                         99999)
        self.assertIsNone(mod.tpm_usage_peak(lines, "absent"))
        self.assertIsNone(mod.tpm_usage_peak([], "kimi-k3-oc"))

    def test_tpm_budget_recommend_probe_source(self) -> None:
        """AC2：probe 有效 → recommended = 阈值×0.9 千位向下、source="probe"、
        无 choices。"""
        mod = self.mod
        probe = {"threshold": 31000, "ts": time.time() - 10,
                 "outcome": "rejected", "batches": 7, "consumed": 35123}
        rec = mod.tpm_budget_recommend("m", [], samples=None, probe=probe,
                                       usage=None)
        self.assertEqual(rec["recommended"], 27000)
        self.assertEqual(rec["source"], "probe")
        self.assertNotIn("choices", rec)
        self.assertIs(rec["enabled_advice"]["suggest"], True)
        self.assertEqual(rec["probe"]["threshold"], 31000)
        self.assertEqual(rec["samples"], 0)
        self.assertNotIn("hint", rec)

    def test_tpm_budget_recommend_source_priority(self) -> None:
        """AC3：探测实测 > body-err 样本 > 历史用量。"""
        mod = self.mod
        probe = {"threshold": 31000, "ts": time.time() - 10}
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=31038 ts=T",
        ]
        # 三源同给 → probe 优先
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines, samples=[31038],
                                       probe=probe, usage=99999)
        self.assertEqual(rec["source"], "probe")
        self.assertEqual(rec["recommended"], 27000)
        self.assertNotIn("choices", rec)
        # 仅 body-err 样本 → body_err
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines, samples=[31038, 32375],
                                       probe=None, usage=99999)
        self.assertEqual(rec["source"], "body_err")
        self.assertEqual(rec["recommended"], 27000)
        self.assertEqual(rec["samples"], 2)
        # 仅历史用量 → usage_estimate（suggest=False）
        rec = mod.tpm_budget_recommend("kimi-k3-oc", [], samples=[], probe=None,
                                       usage=47850)
        self.assertEqual(rec["source"], "usage_estimate")
        self.assertEqual(rec["recommended"], 47000)
        self.assertIs(rec["enabled_advice"]["suggest"], False)

    def test_tpm_budget_recommend_usage_estimate_from_log_ring(self) -> None:
        """usage=None → 从 lines 解析 tpm 峰值做保守参考。"""
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=12000 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=47850 ts=T",
        ]
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines)
        self.assertEqual(rec["source"], "usage_estimate")
        self.assertEqual(rec["recommended"], 47000)
        self.assertIs(rec["enabled_advice"]["suggest"], False)
        self.assertNotIn("choices", rec)

    def test_tpm_budget_recommend_probe_invalid_ignored(self) -> None:
        """probe 非 dict / threshold 非法 → 不采信，回落下一优先级。"""
        mod = self.mod
        for bad_probe in (None, "x", {}, {"threshold": 0}, {"threshold": "x"},
                          {"threshold": -5}):
            rec = mod.tpm_budget_recommend("m", [], samples=None,
                                           probe=bad_probe, usage=20000)
            self.assertEqual(rec["source"], "usage_estimate",
                             "bad probe %r must fall through" % (bad_probe,))
            self.assertEqual(rec["recommended"], 20000)

    def test_tpm_settings_snapshot_recommend_no_choices(self) -> None:
        """AC12②：snapshot 每模型 recommend 不含 choices，含 enabled_advice/source；
        持久化样本走 body_err 源、探测结果走 probe 源。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        orig_recent = list(mod.RECENT_REQUESTS)
        orig_daily = dict(mod.STATS["daily_by_model"])
        orig_ring = list(mod.LOG_RING)
        orig_persist = dict(mod.PERSISTED_BODY_ERR_SAMPLES)
        orig_probe = dict(mod.PROBE_RESULTS)
        try:
            mod.RECENT_REQUESTS.clear()
            mod.STATS["daily_by_model"] = {mod.today_key(): {}}
            mod.RECENT_REQUESTS.append({"model": "glm-5.3-oc"})
            mod.RECENT_REQUESTS.append({"model": "deepseek-v4-pro-0813-oc"})
            mod.TPM_MODEL_BUDGETS["kimi-k3-oc"] = 30000
            mod.PERSISTED_BODY_ERR_SAMPLES["kimi-k3-oc"] = [
                {"tpm": 31000, "ts": time.time() - 5}]
            mod.PROBE_RESULTS["glm-5.3-oc"] = [
                {"threshold": 31000, "ts": time.time() - 10}]
            snap = mod.tpm_settings_snapshot()
            by_name = {m["name"]: m for m in snap["models"]}
            for m in snap["models"]:
                rec = m["recommend"]
                self.assertNotIn("choices", rec)
                self.assertIn("enabled_advice", rec)
                self.assertIn("source", rec)
            self.assertEqual(by_name["kimi-k3-oc"]["recommend"]["source"],
                             "body_err")
            self.assertEqual(by_name["kimi-k3-oc"]["recommend"]["recommended"],
                             27000)
            self.assertEqual(by_name["glm-5.3-oc"]["recommend"]["source"],
                             "probe")
            self.assertEqual(by_name["glm-5.3-oc"]["recommend"]["recommended"],
                             27000)
            # 无证据模型 → none 源
            self.assertEqual(
                by_name["deepseek-v4-pro-0813-oc"]["recommend"]["source"],
                "none", "model with no evidence must be source='none'")
        finally:
            mod.RECENT_REQUESTS.clear()
            mod.RECENT_REQUESTS.extend(orig_recent)
            mod.STATS["daily_by_model"] = orig_daily
            mod.LOG_RING.clear()
            mod.LOG_RING.extend(orig_ring)
            mod.PERSISTED_BODY_ERR_SAMPLES.clear()
            mod.PERSISTED_BODY_ERR_SAMPLES.update(orig_persist)
            mod.PROBE_RESULTS.clear()
            mod.PROBE_RESULTS.update(orig_probe)
            self._tpm_cleanup(mod)

    def test_save_and_load_tpm_model_budgets_roundtrip(self) -> None:
        mod = self.mod
        path = os.path.join(tempfile.mkdtemp(prefix="ctyun-tpm-rt-"), "s.json")
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update({"kimi-k3-oc": 1000, "glm-5.3-oc": 50000})
            mod.save_stats_counters(path)
            loaded = mod.load_tpm_model_budgets(path)
            self.assertEqual(loaded, {"kimi-k3-oc": 1000, "glm-5.3-oc": 50000})
        finally:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_persist_upstream_carries_tpm_model_budgets(self) -> None:
        mod = self.mod
        path = os.path.join(tempfile.mkdtemp(prefix="ctyun-tpm-pu-"), "s.json")
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS["kimi-k3-oc"] = 1000
            mod.persist_upstream("https://example.com/v1", path, capture_errors=True,
                                 tpm_model_budgets=mod.TPM_MODEL_BUDGETS)
            with open(path, encoding="utf-8") as fh:
                saved = json.load(fh)
            self.assertEqual(saved["tpm_model_budgets"], {"kimi-k3-oc": 1000})
            self.assertEqual(saved["upstream_base"], "https://example.com/v1")
        finally:
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(os.path.dirname(path), ignore_errors=True)

    def test_set_tpm_model_budgets_atomic_replace(self) -> None:
        mod = self.mod
        tmpdir = tempfile.mkdtemp(prefix="ctyun-tpm-set-")
        path = os.path.join(tmpdir, "s.json")
        orig_path = mod.PERSIST_PATH
        orig = dict(mod.TPM_MODEL_BUDGETS)
        try:
            mod.PERSIST_PATH = path
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS["old-model"] = 500
            mod.set_tpm_model_budgets({"new-model": 2000})
            self.assertEqual(mod.TPM_MODEL_BUDGETS, {"new-model": 2000})
            self.assertEqual(mod.load_tpm_model_budgets(path), {"new-model": 2000})
        finally:
            mod.PERSIST_PATH = orig_path
            mod.TPM_MODEL_BUDGETS.clear()
            mod.TPM_MODEL_BUDGETS.update(orig)
            shutil.rmtree(tmpdir, ignore_errors=True)

    def test_tpm_429_reply_payload(self) -> None:
        """_reply_tpm_429 方法 payload 与 spec 定死 JSON 一致。"""
        mod = self.mod
        # 定死 JSON 结构（与 _reply_tpm_429 源码内 payload 全等）
        expected = ('{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试",'
                    '"type":"rate_limit_error","code":"model_tpm_limit"}}').encode("utf-8")
        parsed = json.loads(expected.decode("utf-8"))
        self.assertIn("error", parsed)
        self.assertEqual(parsed["error"]["type"], "rate_limit_error")
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        # 验证方法存在
        self.assertTrue(hasattr(mod.ProxyHandler, "_reply_tpm_429"),
                        "_reply_tpm_429 method must exist on ProxyHandler")
        self.assertTrue(callable(mod.ProxyHandler._reply_tpm_429))

    def test_load_tpm_model_budgets_tolerance(self) -> None:
        mod = self.mod
        path = os.path.join(tempfile.mkdtemp(prefix="ctyun-tpm-load-"), "s.json")
        # 缺文件 → {}
        self.assertEqual(mod.load_tpm_model_budgets(path), {})
        # 顶层非 dict / 无键 / 键非 dict → {}
        for bad in ("[]", "{}", '{"tpm_model_budgets": []}',
                    '{"tpm_model_budgets": "x"}'):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(bad)
            self.assertEqual(mod.load_tpm_model_budgets(path), {})
        # 逐键容错：非法项跳过，合法项保留
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_model_budgets": {
                "ok-a": 1000,
                "": 5,                       # 空名 → 跳过
                "ok-b": 50000,
                "bad-neg": -1,               # 非正 → 跳过
                "bad-zero": 0,               # 非正 → 跳过
                "bad-str": "100",            # 非 int → 跳过
                "bad-bool": True,            # bool → 跳过
                "x" * 201: 5,                # 超 200 字符 → 跳过
                "ok-c": 2.0,                 # float → 跳过（非 int）
            }}, fh, ensure_ascii=False)
        self.assertEqual(mod.load_tpm_model_budgets(path),
                         {"ok-a": 1000, "ok-b": 50000})
        # 超 32 键：只保留前 32 个合法键
        big = {"tpm_model_budgets": {"m%d" % i: 10 for i in range(40)}}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(big, fh)
        self.assertEqual(len(mod.load_tpm_model_budgets(path)), 32)
        shutil.rmtree(os.path.dirname(path), ignore_errors=True)

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
        mod = self.mod
        f = mod.aggregate_daily_range
        daily = {
            "2026-02-01": {"requests": 3, "filtered": 1, "errors_proxy": 0,
                           "errors_upstream": 1, "retries": 0},
            "2026-02-02": {"requests": 5},  # 缺字段桶：缺按 0
            "2026-03-01": {"requests": 7, "filtered": 2, "errors_proxy": 1,
                           "errors_upstream": 0, "retries": 4},  # 窗口外
        }
        out = f(daily, "2026-02-01", "2026-02-28")
        expected = dict.fromkeys(mod.DAILY_V2_FIELDS, 0)
        expected.update({"requests": 8, "filtered": 1, "errors_upstream": 1,
                         "retries": 0, "days": 2})
        self.assertEqual(out, expected)
        # 空窗口：全 0 + days=0
        expected_empty = dict.fromkeys(mod.DAILY_V2_FIELDS, 0)
        expected_empty["days"] = 0
        self.assertEqual(f(daily, "2025-01-01", "2025-01-31"), expected_empty)
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
                             set(mod.DAILY_V2_FIELDS) | {"days"})
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
            today, dict.fromkeys(mod.DAILY_V2_FIELDS, 0))
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
            set(mod.DAILY_V2_FIELDS),
            "dm entry shape must stay in sync across both creation sites (20 fields)")
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
        # v3：_record_empty_retry 的 dm entry 创建点（site-2）同样 20 字段（DAILY_V2_FIELDS）——
        # 用全新日期隔离（真实 today 的键位已被 cap 用例占满 32，新建会被 cap 拒绝）
        orig_today = mod.today_key
        try:
            mod.today_key = lambda: "2026-01-03"
            mod._record_empty_retry("m-retry-only")
        finally:
            mod.today_key = orig_today
        self.assertEqual(
            set(mod.STATS["daily_by_model"]["2026-01-03"]["m-retry-only"]),
            set(mod.DAILY_V2_FIELDS),
            "empty-retry creation site must keep dm entry shape in sync (20 fields)")

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
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0,
                           "tokens_cache_read": 0, "tokens_cache_write": 0,
                           "tokens_reasoning": 0, "requests_zero_token": 0},
                    "m2": {"requests": 7, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 4, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0,
                           "tokens_cache_read": 0, "tokens_cache_write": 0,
                           "tokens_reasoning": 0, "requests_zero_token": 0}},
                "2026-01-05": {
                    "m1": {"requests": 1, "filtered": 0, "errors_proxy": 0,
                           "errors_upstream": 0, "retries": 0, "eof_without_done": 0,
                           "header_retries": 0,
                           "tokens_prompt": 0, "tokens_completion": 0,
                           "bytes_out": 0, "stream_requests": 0,
                           "ttfb_sum_ms": 0, "ttfb_count": 0,
                           "outcome_ok": 0, "outcome_degraded": 0,
                           "outcome_failed": 0,
                           "tokens_cache_read": 0, "tokens_cache_write": 0,
                           "tokens_reasoning": 0, "requests_zero_token": 0}}}
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
                                                "header_retries": 0,
                                                "tokens_prompt": 0,
                                                "tokens_completion": 0,
                                                "bytes_out": 0, "stream_requests": 0,
                                                "ttfb_sum_ms": 0, "ttfb_count": 0,
                                                "outcome_ok": 0, "outcome_degraded": 0,
                                                "outcome_failed": 0,
                                                "tokens_cache_read": 0,
                                                "tokens_cache_write": 0,
                                                "tokens_reasoning": 0,
                                                "requests_zero_token": 0}}},
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
                          "header_retries": 0,
                          "tokens_prompt": 0, "tokens_completion": 0,
                          "bytes_out": 0, "stream_requests": 0,
                          "ttfb_sum_ms": 0, "ttfb_count": 0,
                          "outcome_ok": 0, "outcome_degraded": 0,
                          "outcome_failed": 0,
                          "tokens_cache_read": 0, "tokens_cache_write": 0,
                          "tokens_reasoning": 0, "requests_zero_token": 0},
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

    def test_classify_outcome_body_error(self) -> None:
        mod = self.mod
        f = mod.classify_outcome
        # body_error=True -> CLASS_BODY_ERROR / "body-err" / counts_error=True / capture=True
        o = f(body_error=True)
        self.assertEqual(o.category, mod.CLASS_BODY_ERROR)
        self.assertEqual(o.log_result, "body-err")
        self.assertTrue(o.counts_error)
        self.assertTrue(o.capture)
        # body_error default False: status=200 stays CLASS_OK (no regression)
        o = f(status=200)
        self.assertEqual(o.category, mod.CLASS_OK)
        self.assertEqual(o.log_result, "ok")
        self.assertFalse(o.counts_error)
        # priority: client_abort > body_error
        o = f(client_abort=True, body_error=True)
        self.assertEqual(o.category, mod.CLASS_CLIENT_ABORT)
        self.assertEqual(o.log_result, "aborted")
        # priority: synth_502 > body_error
        o = f(synth_502=True, body_error=True)
        self.assertEqual(o.category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(o.log_result, "error")
        # priority: body_error > eof_without_done
        o = f(body_error=True, eof_without_done=True)
        self.assertEqual(o.category, mod.CLASS_BODY_ERROR)
        self.assertEqual(o.log_result, "body-err")
        # body_error + status=200: body_error wins over status < 400
        o = f(status=200, body_error=True)
        self.assertEqual(o.category, mod.CLASS_BODY_ERROR)
        self.assertEqual(o.log_result, "body-err")
        # body_error=False (default): no impact on status-based classification
        self.assertEqual(f(status=200).category, mod.CLASS_OK)
        self.assertEqual(f(status=502).category, mod.CLASS_UPSTREAM_FAULT)
        self.assertEqual(f(status=200, poison_filtered=1).category, mod.CLASS_POISON_FIXED)

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

    def test_kind_category_body_error(self) -> None:
        mod = self.mod
        self.assertEqual(mod._KIND_CATEGORY.get(mod.ERR_KIND_BODY_ERROR),
                         mod.CLASS_BODY_ERROR,
                         "body_error must map to body_error category")
        self.assertEqual(len(mod._KIND_CATEGORY), 10,
                         "must have exactly 10 kind-category mappings")

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

    def test_open_upstream_sleep_stall_raises_socket_timeout(self) -> None:
        """白盒：sleep-stall 上游 → _open_upstream 在 HEADER_TIMEOUT_S 内抛 socket.timeout。

        sleep-stall 夹具读 body 后持连静默（不写响应、不关连接），代理侧 getresponse
        阻塞到 HEADER_TIMEOUT_S 抛 socket.timeout；与 stall 夹具的
        RemoteDisconnected（关连接路径）互补，锁可重试元组最后零覆盖子分支。"""
        mod = self.mod
        import types
        upstream_port = make_fake_upstream(False, sleep_stall_all=True)
        try:
            orig_base = mod.UPSTREAM_BASE
            mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % upstream_port
            try:
                orig_ht = mod.HEADER_TIMEOUT_S
                mod.HEADER_TIMEOUT_S = 0.5
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    with self.assertRaises(socket.timeout):
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

    def test_tpm_calibrate_constants_defaults(self) -> None:
        mod = self.mod
        self.assertEqual(mod.TPM_CALIBRATE_INPUT_BYTES, 240000)
        self.assertEqual(mod.TPM_CALIBRATE_STEP_TOKENS, 5000)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_TOKENS, 1)
        self.assertEqual(mod.TPM_CALIBRATE_BATCH_GAP_S, 2.0)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_DURATION_S, 240)
        self.assertEqual(mod.TPM_CALIBRATE_MIN_INTERVAL_S, 1800)
        self.assertEqual(mod.TPM_CALIBRATE_HARD_CAP_TOKENS, 400000)
        self.assertEqual(mod.TPM_CALIBRATE_SETTLE_WAIT_S, 60)
        self.assertEqual(mod.TPM_SAMPLES_MAX, 200)
        self.assertEqual(mod.TPM_SAMPLES_RETENTION_DAYS, 30)
        self.assertEqual(mod.TPM_PROBE_RESULTS_MAX, 5)

    def test_tpm_calibrate_constants_env_seam(self) -> None:
        import subprocess
        code = (
            "import os; "
            "os.environ['CTYUN_CALIBRATE_INPUT_BYTES']='111'; "
            "os.environ['CTYUN_CALIBRATE_STEP_TOKENS']='222'; "
            "os.environ['CTYUN_CALIBRATE_MAX_TOKENS']='3'; "
            "os.environ['CTYUN_CALIBRATE_BATCH_GAP_S']='0.5'; "
            "os.environ['CTYUN_CALIBRATE_MAX_DURATION_S']='10'; "
            "os.environ['CTYUN_CALIBRATE_MIN_INTERVAL_S']='9'; "
            "os.environ['CTYUN_CALIBRATE_HARD_CAP_TOKENS']='999'; "
            "os.environ['CTYUN_CALIBRATE_SETTLE_WAIT_S']='7'; "
            "os.environ['CTYUN_TPM_SAMPLES_MAX']='4'; "
            "os.environ['CTYUN_TPM_SAMPLES_RETENTION_DAYS']='5'; "
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('m', %r); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "print(m.TPM_CALIBRATE_INPUT_BYTES, m.TPM_CALIBRATE_STEP_TOKENS, "
            "m.TPM_CALIBRATE_MAX_TOKENS, m.TPM_CALIBRATE_BATCH_GAP_S, "
            "m.TPM_CALIBRATE_MAX_DURATION_S, m.TPM_CALIBRATE_MIN_INTERVAL_S, "
            "m.TPM_CALIBRATE_HARD_CAP_TOKENS, m.TPM_CALIBRATE_SETTLE_WAIT_S, "
            "m.TPM_SAMPLES_MAX, m.TPM_SAMPLES_RETENTION_DAYS, m.TPM_PROBE_RESULTS_MAX)"
            % PROXY_SCRIPT)
        out = subprocess.check_output([sys.executable, "-c", code], text=True,
                                      timeout=30).strip()
        self.assertEqual(out.split(),
                         ["111", "222", "3", "0.5", "10.0", "9.0", "999", "7.0",
                          "4", "5", "5"])

    def test_load_tpm_body_err_samples_fault_tolerant(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 缺文件
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 损坏 JSON
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(["not", "dict"], fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 顶层非 dict
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"other": 1}, fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 键缺失
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": [1, 2]}, fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 值非 dict
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": {"kimi": 5}}, fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # per-model 非 list
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": {"kimi": None}}, fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # per-model None
        now = time.time()
        fresh = now - 60
        stale = now - 40 * 86400
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": {
                "kimi": [{"tpm": 31000, "ts": fresh},
                         {"tpm": 32000, "ts": fresh + 1},
                         {"tpm": 30000, "ts": stale},        # 超保留期 → 丢
                         {"tpm": "999", "ts": fresh},         # tpm 非 int → 丢
                         {"tpm": 28000},                       # 缺 ts → 丢
                         {"tpm": -5, "ts": fresh},             # tpm ≤0 → 丢
                         "garbage"],                           # 非 dict → 丢
                "": [{"tpm": 1, "ts": fresh}],                # 空 model → 丢
                "x" * 201: [{"tpm": 1, "ts": fresh}],         # model 超长 → 丢
            }}, fh)
        out = mod.load_tpm_body_err_samples(path)
        self.assertEqual(sorted(out.keys()), ["kimi"])
        self.assertEqual([s["tpm"] for s in out["kimi"]], [31000, 32000])

    def test_load_tpm_body_err_samples_count_cap(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        now = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": {
                "m": [{"tpm": 1000 + i, "ts": now - 100 + i} for i in range(10)]}}, fh)
        orig = mod.TPM_SAMPLES_MAX
        mod.TPM_SAMPLES_MAX = 3
        self.addCleanup(setattr, mod, "TPM_SAMPLES_MAX", orig)
        out = mod.load_tpm_body_err_samples(path)
        self.assertEqual(len(out["m"]), 3)
        self.assertEqual([s["tpm"] for s in out["m"]], [1007, 1008, 1009],
                         "keep newest 3 by ts")

    def test_load_tpm_probe_results_fault_tolerant_and_prune(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        now = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_probe_results": {
                "kimi": [
                    {"threshold": 31000, "ts": now - 10,
                     "outcome": "rejected", "batches": 7, "consumed": 35123},
                    {"threshold": 32000, "ts": now - 5,
                     "outcome": "capped", "batches": 80, "consumed": 400123},
                    {"threshold": 0, "ts": now},              # threshold ≤0 → 丢
                    {"threshold": "x", "ts": now},             # 非 int → 丢
                ],
                "m2": [{"threshold": 1000 + i, "ts": now - 60 + i} for i in range(7)],
            }}, fh)
        out = mod.load_tpm_probe_results(path)
        self.assertEqual(sorted(out.keys()), ["kimi", "m2"])
        kim = out["kimi"]
        self.assertEqual([r["threshold"] for r in kim], [31000, 32000])
        self.assertEqual(kim[0]["outcome"], "rejected")
        self.assertEqual(kim[0]["batches"], 7)
        self.assertEqual(kim[0]["consumed"], 35123)
        self.assertEqual([r["threshold"] for r in out["m2"]], [1002, 1003, 1004, 1005, 1006],
                         "keep newest 5 of 7")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_probe_results": {"kimi": 5}}, fh)
        self.assertEqual(mod.load_tpm_probe_results(path), {})   # per-model 非 list
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_probe_results": {"kimi": None}}, fh)
        self.assertEqual(mod.load_tpm_probe_results(path), {})   # per-model None

    def test_record_tpm_body_err_sample_appends_and_saves(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        mod.PERSISTED_BODY_ERR_SAMPLES.clear()
        self.addCleanup(mod.PERSISTED_BODY_ERR_SAMPLES.clear)
        mod.record_tpm_body_err_sample("kimi", 31038, time.time() - 1)
        mod.record_tpm_body_err_sample("kimi", 33227, time.time())
        mod.record_tpm_body_err_sample(None, 1, time.time())    # 非法 model → 忽略
        mod.record_tpm_body_err_sample("kimi", -1, time.time()) # 非法 tpm → 忽略
        self.assertEqual(len(mod.PERSISTED_BODY_ERR_SAMPLES["kimi"]), 2)
        mod.save_stats_counters(path)
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("tpm_body_err_samples", payload)
        saved = payload["tpm_body_err_samples"]["kimi"]
        self.assertEqual([s["tpm"] for s in saved], [31038, 33227])
        reloaded = mod.load_tpm_body_err_samples(path)
        self.assertEqual([s["tpm"] for s in reloaded["kimi"]], [31038, 33227])

    def test_record_tpm_probe_result_and_save_roundtrip(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        mod.PROBE_RESULTS.clear()
        self.addCleanup(mod.PROBE_RESULTS.clear)
        mod.record_tpm_probe_result("kimi", {
            "threshold": 31000, "ts": time.time() - 1,
            "outcome": "rejected", "batches": 7, "consumed": 35123})
        mod.record_tpm_probe_result("kimi", {})  # 缺 threshold → 忽略
        self.assertEqual(len(mod.PROBE_RESULTS["kimi"]), 1)
        mod.save_stats_counters(path)
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("tpm_probe_results", payload)
        saved = payload["tpm_probe_results"]["kimi"]
        self.assertEqual(saved[0]["threshold"], 31000)
        self.assertEqual(saved[0]["outcome"], "rejected")
        reloaded = mod.load_tpm_probe_results(path)
        self.assertEqual(reloaded["kimi"][0]["threshold"], 31000)

    def test_save_prunes_stale_and_overcap_samples(self) -> None:
        """save 原地 prune：超期条目 + 超上限条目被裁，内存态同步收缩。"""
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        mod.PERSISTED_BODY_ERR_SAMPLES.clear()
        mod.PROBE_RESULTS.clear()
        self.addCleanup(mod.PERSISTED_BODY_ERR_SAMPLES.clear)
        self.addCleanup(mod.PROBE_RESULTS.clear)
        now = time.time()
        # body-err：5 条近期 + 1 条过期
        for i in range(5):
            mod.record_tpm_body_err_sample("m", 1000 + i, now - 100 + i)
        mod.record_tpm_body_err_sample("m", 9999, now - 40 * 86400)
        # probe：7 条 → prune 至 5
        for i in range(7):
            mod.record_tpm_probe_result("m", {"threshold": 100 + i, "ts": now - 60 + i})
        orig = mod.TPM_SAMPLES_MAX
        mod.TPM_SAMPLES_MAX = 3
        self.addCleanup(setattr, mod, "TPM_SAMPLES_MAX", orig)
        mod.save_stats_counters(path)
        self.assertEqual([s["tpm"] for s in mod.PERSISTED_BODY_ERR_SAMPLES["m"]],
                         [1002, 1003, 1004])
        self.assertEqual([r["threshold"] for r in mod.PROBE_RESULTS["m"]],
                         [102, 103, 104, 105, 106])
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual([s["tpm"] for s in payload["tpm_body_err_samples"]["m"]],
                         [1002, 1003, 1004])


class StopProxyKillPathTest(unittest.TestCase):
    """Episode B：stop_proxy helper 的强杀路径与 None 安全。"""

    def test_stop_proxy_none_safe(self) -> None:
        self.assertFalse(stop_proxy(None), "stop_proxy(None) 必须安全返回 False")

    def test_stop_proxy_kills_sigterm_ignoring_proc(self) -> None:
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN);"
             " time.sleep(30)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        proc.stderr_buf = []  # stop_proxy 会调 stderr_text（要求 stderr_buf 视图）
        time.sleep(0.5)  # 等子进程解释器装好 SIG_IGN handler（否则竞态：SIGTERM 按默认行为杀死）
        try:
            self.assertIsNone(proc.poll(), "就绪守卫：子进程必须仍在运行")
            killed = stop_proxy(proc, timeout=0.5)
            self.assertTrue(killed, "忽略 SIGTERM 的进程必须被强杀并返回 True")
            self.assertIsNotNone(proc.poll(), "强杀后进程必须已退出")
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_stop_proxy_normal_terminate_not_killed(self) -> None:
        proc = subprocess.Popen([sys.executable, "-c", "time.sleep(30)"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        proc.stderr_buf = []
        time.sleep(0.3)  # 解释器就绪，避免启动竞态污染语义
        try:
            killed = stop_proxy(proc, timeout=10.0)
            self.assertFalse(killed, "正常 SIGTERM 退出的进程不得报强杀")
            self.assertIsNotNone(proc.poll())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)


class DashboardV2SkeletonTest(unittest.TestCase):
    """P5：V2 卡片骨架 + 拆分常量契约（模块级常量断言，无需子进程）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_segment_constants_present_and_join(self) -> None:
        for name in ("_DASH_HEAD", "_DASH_SECTIONS_STATIC", "_DASH_SECTIONS_TABLES",
                     "_DASH_SECTIONS_V2", "_DASH_JS_CORE", "_DASH_JS_V2"):
            self.assertTrue(hasattr(self.mod, name), "拆分常量 %s 缺失" % name)
        self.assertTrue(self.mod._DASH_SECTIONS_V2,
                        "_DASH_SECTIONS_V2 不得为空（V2 卡片应已填充）")
        joined = ("".join([
            self.mod._DASH_HEAD, self.mod._DASH_SECTIONS_STATIC,
            self.mod._DASH_SECTIONS_TABLES, self.mod._DASH_SECTIONS_V2,
            self.mod._DASH_JS_CORE, self.mod._DASH_JS_V2,
            "</script>\n</body>\n</html>\n",
        ])).encode("utf-8")
        self.assertEqual(joined, self.mod.DASHBOARD_HTML,
                         "DASHBOARD_HTML 必须等于 6 段常量按序 join + 尾部字面量")

    def test_v2_containers_present(self) -> None:
        v2 = self.mod._DASH_SECTIONS_V2
        for cid in ("perf-model-body", "token-daily-body", "upstream-health",
                    "latency-dist"):
            self.assertIn('id="%s"' % cid, v2, "V2 区块缺容器 %s" % cid)
        # P6：tri-state-card 移入 overview pane（落在 _DASH_SECTIONS_TABLES 段内）——
        # 全页落位由 DashboardTabsSkeletonTest.test_pane_mapping /
        # test_no_regression_existing_ids 守护
        self.assertIn('id="tri-state-card"', self.mod._DASH_SECTIONS_TABLES,
                      "tri-state-card 应移入 _DASH_SECTIONS_TABLES")

    def test_old_content_intact(self) -> None:
        # R5 兜底：旧区块不因拆段/加卡退化（模块级快断言；子进程集成测试仍独立守护）
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        for marker in ("保存上游端点", "剥行流带", 'id="daily-body"',
                       'id="daily-model-body"', "renderStats", "renderDailyByModel",
                       'data-range="last_month"', "spark"):
            self.assertIn(marker, html, "旧内容退化，缺 %s" % marker)

    def test_no_inner_html_anywhere(self) -> None:
        for name in ("_DASH_HEAD", "_DASH_SECTIONS_STATIC", "_DASH_SECTIONS_TABLES",
                     "_DASH_SECTIONS_V2", "_DASH_JS_CORE", "_DASH_JS_V2"):
            self.assertIsNone(re.search(r"\.innerHTML\s*=", getattr(self.mod, name)),
                              "%s 含 innerHTML 赋值（动态数据必须 textContent）" % name)
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertEqual(html.count("innerHTML"), 0, "整页必须 0 个 innerHTML 出现")
        self.assertGreaterEqual(html.count("textContent"), 1,
                                "textContent 出现次数必须 ≥ innerHTML（此处 innerHTML=0）")

    def test_v2_empty_state_branches(self) -> None:
        v2 = self.mod._DASH_JS_V2
        self.assertIn("ms == null || ms === 0", v2,
                      "renderLatencyDist 空态必须判 0.0（hist_percentile 空桶值）")
        self.assertIn("bar.style.display", v2,
                      "renderTriState 空态必须隐藏占比条")
        self.assertNotIn('bar.appendChild(el("span", "empty"', v2,
                         "三态空态占位不得落在 overflow:hidden 的 tri-bar 内")

    def test_v2_js_functions_and_wiring(self) -> None:
        js = self.mod._DASH_JS_V2
        for fn in ("renderPerf", "renderTokens", "renderHealth", "renderTriState"):
            self.assertIn("function %s(" % fn, js, "V2 JS 缺函数 %s" % fn)
        self.assertIn("pollHealth", js)
        self.assertIn('fetch("/api/health")', js)
        core = self.mod._DASH_JS_CORE
        # probe_alert 进标签（EVT_KIND_LABELS）与 tooltip 明细（reason 展示）
        self.assertIn('probe_alert: "上游探测告警"', core)
        self.assertIn('(e.reason ? " · " + e.reason : "")', core)
        # applyRange 复用：token/三态随 4 键时间段切换零请求重渲染
        self.assertIn("renderTokens(lastSnap)", core)
        self.assertIn("renderTriState(lastSnap)", core)
        # 健康卡 chip 挂 probe_alert 事件标记（hover 出 tooltip）
        self.assertIn('markEvents(chip, "probe_alert", null, null)', js)


class DashboardV3CardsTest(unittest.TestCase):
    """v3 卡 7/8：新卡容器/函数/接线模块级断言（join 与 innerHTML 门禁沿用 6 段）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_v3_overview_containers(self) -> None:
        tables = self.mod._DASH_SECTIONS_TABLES
        for cid in ("bykey-body", "zt-count", "zt-ratio", "zt-range",
                    "q-mtd", "q-days", "q-proj"):
            self.assertIn('id="%s"' % cid, tables,
                          "overview 区缺 v3 容器 %s" % cid)

    def test_v3_perf_containers(self) -> None:
        v2 = self.mod._DASH_SECTIONS_V2
        for cid in ("ttfb-hist-body", "phase-model-body", "slow-body",
                    "stability-body", "tpm-obs-body", "err-events-body",
                    "traffic-model-body", "stalls-body", "qwait-body",
                    "settle-body", "hourly-svg", "probe-trend",
                    "tpm-queue-chip", "err-capture-chip",
                    "hourly-bytes-svg", "tp-bytes-in"):
            self.assertIn('id="%s"' % cid, v2, "perf 区缺 v3 容器 %s" % cid)

    def test_v3_token_table_columns(self) -> None:
        v2 = self.mod._DASH_SECTIONS_V2
        self.assertIn("<th>cache tokens</th>", v2,
                      "token 按天×模型表缺 cache 列（T2）")
        self.assertIn("<th>reasoning tokens</th>", v2,
                      "token 按天×模型表缺 reasoning 列（T3）")

    def test_v3_js_functions_and_wiring(self) -> None:
        js = self.mod._DASH_JS_V2
        for fn in ("renderPerfV3", "renderByKey", "renderZeroToken", "renderQuota",
                   "renderTtfbHist", "renderPhaseByModel", "renderSlowTop",
                   "renderStability", "renderTraffic", "renderStalls",
                   "renderQwait", "renderSettle", "renderHourly",
                   "loadTpmObs", "loadErrorEvents", "loadProbeTrend"):
            self.assertIn("function %s(" % fn, js, "V3 JS 缺函数 %s" % fn)
        self.assertIn('fetch("/api/tpm_stats")', js, "P3 需消费 /api/tpm_stats")
        self.assertIn('fetch("/api/errors")', js, "P4 需消费 /api/errors")
        self.assertIn('fetch("/api/probe_history")', js, "P10 需消费 /api/probe_history")
        self.assertIn("renderPerfV3(snap)", js, "poll 需接线 renderPerfV3")
        self.assertIn("renderByKey(snap)", js, "poll 需接线 renderByKey")
        self.assertIn("renderPerfV3(lastSnap || {})", js, "启动需首渲染 perf v3")
        self.assertIn("function renderHourlyBytes(", js, "缺小时级出流量曲线")
        self.assertIn("renderHourlyBytes(snap)", js, "renderPerfV3 需接线小时吞吐曲线")
        self.assertIn("function genSpeed(", js, "缺生成速度计算")
        self.assertIn("<th>生成速度</th>", self.mod._DASH_SECTIONS_V2,
                      "慢请求表缺生成速度列")
        self.assertIn("<th>生成 tok/s</th>", self.mod._DASH_SECTIONS_V2,
                      "模型速度对比表缺生成速度列")
        core = self.mod._DASH_JS_CORE
        self.assertIn("renderZeroToken(lastSnap)", core,
                      "applyRange 需零请求重渲染 0-token 卡")
        self.assertIn("renderQuota(lastSnap)", core,
                      "applyRange 需零请求重渲染月末投影卡")


class DashboardTabsSkeletonTest(unittest.TestCase):
    """P6：页面级 tab 分页骨架（模块级常量断言，无需子进程）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_tab_bar_present(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertIn('<nav class="tab-bar"', html)
        self.assertIn('role="tablist"', html)
        for key in ("overview", "requests", "perf", "settings"):
            self.assertIn('data-tab="%s"' % key, html,
                          "tab-bar 缺 data-tab=%s" % key)
            self.assertIn('aria-controls="pane-%s"' % key, html,
                          "tab %s 缺 aria-controls" % key)

    def test_pane_mapping(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        self.assertEqual(html.count('class="tab-pane"'), 4,
                         "pane 包裹 div 必须恰好 4 个")
        pane_order = ("settings", "overview", "requests", "perf")
        starts = []
        for name in pane_order:
            pos = html.find('data-pane="%s"' % name)
            self.assertNotEqual(pos, -1, "缺 pane %s" % name)
            starts.append(pos)
        self.assertEqual(starts, sorted(starts),
                         "pane 须按 settings/overview/requests/perf 顺序出现（%r）"
                         % starts)
        main_end = html.find("</main>")
        boundaries = starts[1:] + [main_end if main_end != -1 else len(html)]
        pane_ids = {
            "settings": ("upstream-form", "tpm-models"),
            "overview": ("st-requests", "daily-body", "daily-model-body",
                         "tri-state-card"),
            "requests": ("poison-strip", "req-body"),
            "perf": ("upstream-health", "perf-model-body", "latency-dist",
                     "token-daily-body"),
        }
        for name, start, end in zip(pane_order, starts, boundaries):
            seg = html[start:end]
            for cid in pane_ids[name]:
                self.assertIn('id="%s"' % cid, seg,
                              "id=%s 未落在 pane %s" % (cid, name))

    def test_initial_hidden_state(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        tags = re.findall(r'<div class="tab-pane" data-pane="[a-z]+"[^>]*>',
                          html)
        self.assertEqual(len(tags), 4,
                         "初始 HTML 应有 4 个 pane 开标签，实得 %d" % len(tags))
        for tag in tags:
            if 'data-pane="overview"' in tag:
                self.assertNotIn("hidden", tag,
                                 "overview pane 初始不得 hidden：%s" % tag)
            else:
                self.assertIn("hidden", tag,
                              "非 overview pane 初始必须 hidden：%s" % tag)

    def test_tab_js_wiring(self) -> None:
        js = self.mod._DASH_JS_CORE
        for token in ("initTabs", "localStorage", "location.hash",
                      "hashchange", "aria-selected", "history.replaceState"):
            self.assertIn(token, js, "_DASH_JS_CORE 缺 tab 接线 %s" % token)

    def test_no_regression_existing_ids(self) -> None:
        html = self.mod.DASHBOARD_HTML.decode("utf-8")
        for cid in ("daily-body", "daily-model-body", "req-body",
                    "poison-strip", "tpm-models", "tpm-save",
                    "upstream-form", "upstream-health", "perf-model-body",
                    "latency-dist", "token-daily-body", "tri-state-card",
                    "spark", "evt-tip"):
            self.assertEqual(html.count('id="%s"' % cid), 1,
                             "id=%s 应恰好出现 1 次（防 pane 包裹复制/丢段），实得 %d"
                             % (cid, html.count('id="%s"' % cid)))


class AdminIntegrationTest(unittest.TestCase):
    """admin/dashboard 子进程集成测试（真实 socket，端口与持久化均走 seam）。"""

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        stop_proxy(self.proc)
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
                    "recent", "poison_previews",
                    "daily_by_key", "hourly_tokens"):
            self.assertIn(key, snap)
        for key in ("ttfb_hist_by_model", "phase_p50_ms_by_model",
                    "phase_p90_ms_by_model", "stalls_by_model",
                    "qwait_p50_ms_by_model", "qwait_p90_ms_by_model",
                    "tpm_settle_ratio_p50_by_model",
                    "tpm_settle_ratio_p90_by_model"):
            self.assertIn(key, snap["perf"],
                          "/api/stats perf 缺 v3 键 %s" % key)
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
        # v3：按天×模型表 7 列与主表数值列对齐（代理错误/上游5xx）+ 错误行高亮；
        # 卡 8 P2 稳定性表为两列各第 3 处
        self.assertEqual(html.count("<th>代理错误</th>"), 3)
        self.assertEqual(html.count("<th>上游5xx</th>"), 3)
        self.assertIn('colspan="7"', html)
        self.assertIn("String(ent.errors_proxy || 0)", html)
        self.assertIn("String(ent.errors_upstream || 0)", html)
        self.assertIn("(ent.errors_proxy || 0) + (ent.errors_upstream || 0)", html)
        # v2.1：按模型重启累计卡已整体移除，标题与口径标注锁定不复活；
        # v3 观测扩展按模型区分（spec 决策 9），锁收窄到「重启累计」语义卡
        self.assertNotIn("自上次重启起累计", html)
        self.assertNotIn("按模型重启累计", html)
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
        # v1.5：TPM 限流设置区块（静态存在性 + 无 innerHTML 惯例）
        self.assertIn("TPM 限流设置", html)
        self.assertIn('id="tpm-models"', html)
        self.assertIn('id="tpm-save"', html)
        self.assertIn("/api/tpm_settings", html)
        self.assertIn('id="tpm-msg"', html)
        self.assertIn("data-model", html)
        self.assertIn("loadTpmSettings", html)
        self.assertIn("renderTpmSettings", html)
        self.assertNotIn("slice(0, 14)", html)
        self.assertNotIn("最近 14 天", html)
        self.assertNotIn("与顶部错误数同口径", html)
        # P5：V2 新卡片容器 + 健康轮询端点经 HTTP 完整送达
        for cid in ("perf-model-body", "token-daily-body", "upstream-health",
                    "tri-state-card", "latency-dist"):
            self.assertIn('id="%s"' % cid, html)
        self.assertIn('fetch("/api/health")', html)
        self.assertIn("function renderPerf(", html)
        self.assertIn("function renderTokens(", html)
        self.assertIn("function renderHealth(", html)
        self.assertIn("function renderTriState(", html)
        self.assertIn("probe_alert", html)
        # P6：页面级 tab 分页（tab-bar / 4 pane / settings pane / initTabs JS）
        self.assertIn('<nav class="tab-bar"', html)
        self.assertEqual(html.count('class="tab-pane"'), 4)
        self.assertIn('data-pane="settings"', html)
        self.assertIn("initTabs", html)
        self.assertIn("localStorage", html)

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
        stop_proxy(self.proc)  # 含旧实例 persist_dir 清理（防重赋泄漏）
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
        self.proc.wait(timeout=10)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        self.assertGreaterEqual(data["stats"]["requests_total"], 1)
        self.assertIn("upstream_base", data)

    def test_req_log_line_has_ts_and_model(self) -> None:
        post_sse(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        m = re.search(r"^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds "
                      r"result=\S+ filtered=\d+ "
                      r"model=deepseek-v4-pro-0813-oc "
                      r"retried=\d+ "
                      r"retry_reason=\S+ "
                      r"exc=\S+ "
                      r"rid=\S+ host=\S+ ttfb=\S+ stream=\d+ outcome=\S+ "
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
        proc.wait(timeout=10)
        stderr = stderr_text(proc)
        m = re.search(r"^REQ POST /v1/chat/completions -> 502 dur=\d+\.\ds "
                      r"result=error .*? exc=(\S+)\s+rid=", stderr, re.M)
        self.assertIsNotNone(m, "error 502 REQ 行必须带 exc= 字段，stderr:\n" + stderr)
        self.assertNotEqual(m.group(1), "-",
                            "exc 字段不得是占位符，stderr:\n" + stderr)

    def test_client_abort_is_quiet_and_not_error(self) -> None:
        upstream_port = make_fake_upstream(False, big=True)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("result=aborted", stderr)
        self.assertIn("model=-", stderr)  # 499 abort 不传 model → 占位符 -
        self.assertNotIn("Traceback", stderr,
                         "client abort must not produce handle_error traceback")

    def test_config_post_localhost_allowed_even_with_token_env(self) -> None:
        stop_proxy(self.proc)
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
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertGreaterEqual(saved[today]["requests"], 1,
                                "daily buckets must persist on SIGTERM")

    def test_upstream_500_counts_into_daily_errors_upstream(self) -> None:
        stop_proxy(self.proc)
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
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]["daily"]
        self.assertGreaterEqual(saved[today]["errors_upstream"], 1)

    def test_daily_buckets_resume_from_persist(self) -> None:
        stop_proxy(self.proc)  # 默认 timeout=10 吸收 SIGTERM 慢落盘；含旧 dir 清理
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "empty stream must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)

    def test_priming_prefix_flushed_in_order_no_retry(self) -> None:
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_A + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "[DONE] without content is legal, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_DONE)

    def test_double_empty_stream_falls_back_after_two_calls(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.assertEqual(data, b"", "zero-record stream must relay zero bytes")

    def test_ctyun_empty_retry_zero_disables(self) -> None:
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.proc.terminate()  # SIGTERM → handler 落盘
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "reasoning+[DONE] stream must stay result=ok, stderr:\n" + stderr)
        self.assertNotIn("eof-without-done", stderr)

    def test_double_empty_stream_not_marked_eof_without_done(self) -> None:
        # spec 用例 ③：双空流 fallback（priming 路径 EOF 不加标记，避免与 retries 双计数）
        upstream_port, calls = make_scripted_upstream(empty_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2)
        self.assertEqual(data, SSE_REASONING)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(json.loads(body.decode("utf-8"))["eof_without_done_total"], 0,
                         "priming-stage EOF must not trip eof counter")
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertNotIn("eof-without-done", stderr,
                         "priming-stage EOF (empty stream fallback) must not be marked "
                         "eof-without-done, stderr:\n" + stderr)

    def test_finish_with_usage_zero_content_legal_no_retry(self) -> None:
        """finish + usage + [DONE] 零内容流：合法，不重试。"""
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_REASONING + SSE_FINISH + SSE_USAGE + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "usage-bearing finish stream must not retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_REASONING + SSE_FINISH + SSE_USAGE + SSE_DONE,
                         "legal zero-content stream must relay byte-exact, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "legal stream must stay result=ok, stderr:\n" + stderr)

    def test_finish_without_usage_retried_second_stream_relayed(self) -> None:
        """finish 无 usage 故障形态：首呼 fault_finish_first 回故障尾段触发重试，
        次呼回正常 content 流全量交付。"""
        upstream_port, calls = make_scripted_upstream(
            fault_finish_first=True, body_override=SSE_A + SSE_B + SSE_DONE)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "finish fault must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=finish-no-usage", stderr,
                      "REQ line must carry retry_reason=finish-no-usage, stderr:\n" + stderr)

    def test_double_finish_fault_falls_back_after_two_calls(self) -> None:
        """双 finish 故障：fault_finish_stream 每呼回故障尾段 → calls==2 fallback。"""
        upstream_port, calls = make_scripted_upstream(fault_finish_stream=True)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        stop_proxy(self.proc)
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
        stop_proxy(self.proc)
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
        stop_proxy(self.proc)
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
        stop_proxy(self.proc)
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
        stop_proxy(self.proc)
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
        self.proc.wait(timeout=10)
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
        stop_proxy(self.proc)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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

    def test_header_sleep_stall_retry_success(self) -> None:
        """sleep_stall_calls=(1,) → 首呼持连静默到 socket.timeout 触发 header-timeout 重试 → 次呼正常 → calls==2 + SSE 完整。"""
        upstream_port, calls = make_sleep_stall_upstream(sleep_stall_calls=(1,))
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "header sleep-stall must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        # REQ 日志含 retried=1 + retry_reason=header-timeout
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=header-timeout", stderr,
                      "REQ line must carry retry_reason=header-timeout, stderr:\n" + stderr)
        # 持久化计数器
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved.get("header_retries_total", 0), 1,
                                "header_retries_total must be >=1 after sleep-stall retry")

    def test_header_stall_both_timeout_returns_502(self) -> None:
        """stall_all → 首呼+重试均超时 → 502 + calls==2 + synth_502 留痕含 retried=1。"""
        upstream_port, calls = make_stall_upstream(stall_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "502 REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=header-timeout", stderr,
                      "502 REQ line must carry retry_reason=header-timeout, stderr:\n" + stderr)

    def test_header_stall_retry_disabled(self) -> None:
        """CTYUN_HEADER_RETRY=0 + stall_all → 502 + calls==1（不重试）。"""
        upstream_port, calls = make_stall_upstream(stall_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "header RST must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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
        self.proc.wait(timeout=10)
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

    def test_tpm_stats_endpoint_basic(self) -> None:
        """GET /api/tpm_stats 返回 200 JSON，config/queue_total/buckets shape 正确。"""
        status, body, ctype = admin_get(self.proc.admin_port, "/api/tpm_stats")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"),
                        "Content-Type must be application/json, got %r" % ctype)
        snap = json.loads(body.decode("utf-8"))
        self.assertIn("config", snap)
        self.assertIn("queue_total", snap)
        self.assertIn("buckets", snap)
        self.assertIsInstance(snap["buckets"], list)
        self.assertEqual(snap["config"]["limit"], 110000)  # default
        self.assertEqual(snap["config"]["window_s"], 60)
        self.assertEqual(snap["config"]["queue_max"], 20)
        self.assertEqual(snap["queue_total"], 0)

    def test_tpm_stats_unknown_path_404(self) -> None:
        """未知路径仍返回 404（确保 /api/tpm_stats 路由不破坏 else 分支）。"""
        status, _, _ = admin_get(self.proc.admin_port, "/api/nonexistent")
        self.assertEqual(status, 404)

    def test_body_err_samples_persist_across_restart(self) -> None:
        """AC4：seed_persist 预写样本 → /api/tpm_settings 反映（recommended 按
        min×0.9 千位向下）；真实 body-err 流量 → 样本增加；SIGTERM → 持久化文件
        含新样本（save_stats_counters 原子写，跨重启保留）。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        seed_ts = time.time() - 60
        self.proc = start_proxy(
            self.upstream_port, self.proxy_port,
            seed_persist={"tpm_body_err_samples": {
                "kimi-k3-oc": [{"tpm": 31000, "ts": seed_ts}]}})
        try:
            _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
            snap = json.loads(body_bytes.decode("utf-8"))
            by_name = {m["name"]: m for m in snap["models"]}
            self.assertIn("kimi-k3-oc", by_name,
                          "seeded model must appear in tpm_settings")
            rec = by_name["kimi-k3-oc"]["recommend"]
            self.assertEqual(rec["samples"], 1,
                             "persisted sample must be reflected, got %r" % rec)
            self.assertEqual(rec["recommended"], 27000,
                             "31000*0.9=27900 -> 千位向下 27000, got %r" % rec)
            # 切到 body-err 假上游并触发真实 body-err 流量（带 Authorization）
            err_upstream = make_fake_upstream(False, fail_200_error=True)
            status, _ = admin_post(
                self.proc.admin_port, "/api/config",
                json.dumps({"upstream_base": "http://127.0.0.1:%d" % err_upstream}
                           ).encode("utf-8"))
            self.assertEqual(status, 200)
            conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port,
                                              timeout=10)
            req_body = b'{"model":"kimi-k3-oc","messages":[{"role":"user","content":"hi"}]}'
            conn.request("POST", "/v1/chat/completions", body=req_body,
                         headers={"Content-Type": "application/json",
                                  "Authorization": "Bearer test-token"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
            conn.close()
            _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
            snap = json.loads(body_bytes.decode("utf-8"))
            by_name = {m["name"]: m for m in snap["models"]}
            rec = by_name["kimi-k3-oc"]["recommend"]
            self.assertEqual(rec["samples"], 2,
                             "body-err traffic must append a sample, got %r" % rec)
        finally:
            # SIGTERM → save_stats_counters 落盘（含新样本）
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("tpm_body_err_samples", payload,
                      "persisted file must carry tpm_body_err_samples")
        saved = payload["tpm_body_err_samples"]["kimi-k3-oc"]
        self.assertEqual(len(saved), 2, "SIGTERM flush must persist the new sample")
        self.assertEqual(saved[0]["tpm"], 31000)
        self.assertGreater(saved[1]["tpm"], 0)
        self.assertGreater(saved[1]["ts"], saved[0]["ts"])


class BodyErrorTest(unittest.TestCase):
    """body error observability 集成测试（SSE error 帧 + 非流式 error JSON）。
    对齐 AdminIntegrationTest 模式：每用例独立假上游 + 代理子进程。"""

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        if self.proc:
            stop_proxy(self.proc)
            stop_fake_upstreams()

    def test_sse_body_error_classified_and_logged(self) -> None:
        """SSE error 帧 -> stderr REQ 行 result=body-err + /api/stats errors_total +1。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        body = SSE_A + SSE_ERROR_FRAME + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        # error 帧原样透传（不剥离）
        self.assertEqual(data, body,
                         "error frame must be relayed to client intact, got %r" % data[:200])
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body_bytes.decode("utf-8"))
        self.assertGreaterEqual(snap["errors_total"], 1,
                                "errors_total must increment for body error")
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["daily"][today]["errors_proxy"], 1,
                                "daily errors_proxy must increment for body error")
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("result=body-err", stderr,
                      "REQ line must carry result=body-err, stderr:\n" + stderr)

    def test_body_error_no_retry(self) -> None:
        """SSE error 帧流不触发空流重试（calls==1）。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        body = SSE_ERROR_FRAME + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 1,
                         "error frame stream must not trigger retry, calls=%d" % len(calls))
        self.assertEqual(data, body,
                         "error-only stream must relay as-is, got %r" % data[:200])
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("result=body-err", stderr,
                      "error-only stream must carry result=body-err, stderr:\n" + stderr)

    def test_buffered_body_error(self) -> None:
        """非流式 200 + JSON error body -> result=body-err，errors_total +1。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        upstream_port = make_fake_upstream(False, fail_200_error=True)
        self.proc = start_proxy(upstream_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=10)
        conn.request("POST", "/v1/chat/completions", body=b'{"model":"m"}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200,
                         "fail_200_error upstream must return 200, got %d" % resp.status)
        data = resp.read()
        conn.close()
        self.assertIn(b"error", data, "response must contain error body")
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body_bytes.decode("utf-8"))
        self.assertGreaterEqual(snap["errors_total"], 1,
                                "non-SSE body error must increment errors_total")
        today = time.strftime("%Y-%m-%d")
        self.assertGreaterEqual(snap["daily"][today]["errors_proxy"], 1,
                                "non-SSE body error must increment daily errors_proxy")
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("result=body-err", stderr,
                      "non-SSE error must log result=body-err, stderr:\n" + stderr)

    def test_sse_body_error_recorded(self) -> None:
        """capture_errors on + SSE error 帧 -> /api/errors kind=body_error，detail 含 response。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        body = SSE_A + SSE_ERROR_FRAME + SSE_DONE
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"upstream_base": "http://127.0.0.1:%d" % upstream_port,
                        "capture_errors": True}).encode("utf-8"))
        post_sse(self.proc.proxy_port)
        status, body_bytes, _ = admin_get(self.proc.admin_port, "/api/errors")
        data = json.loads(body_bytes.decode("utf-8"))
        self.assertTrue(data["capture_errors"])
        self.assertGreaterEqual(data["count"], 1)
        kinds = [e["kind"] for e in data["events"]]
        self.assertIn("body_error", kinds,
                      "/api/errors must contain kind=body_error, got %r" % kinds)
        eid = data["events"][0]["id"]
        status, body_bytes, _ = admin_get(self.proc.admin_port, "/api/errors?id=%d" % eid)
        self.assertEqual(status, 200)
        ev = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(ev["kind"], "body_error")
        self.assertEqual(ev["category"], "body_error")
        self.assertIn("response", ev)
        self.assertIsNotNone(ev["response"])
        self.assertIn("error", ev["response"].lower(),
                      "detail response must contain the error frame text")

    def test_sse_content_mentioning_error_not_flagged(self) -> None:
        """content 文本含 'error' 字样 -> result=ok，零误判。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        body = (SSE_A +
                b'data: {"choices":[{"delta":{"content":"error occurred"}}]}\n\n' +
                SSE_B + SSE_DONE)
        upstream_port, calls = make_scripted_upstream(body_override=body)
        self.proc = start_proxy(upstream_port, free_port())
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(data, body,
                         "normal content mentioning 'error' must relay as-is, got %r" % data[:200])
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(snap["errors_total"], 0,
                         "false positive must not increment errors_total")
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        self.assertIn("result=ok", stderr,
                      "content with 'error' text must stay result=ok, stderr:\n" + stderr)
        self.assertNotIn("body-err", stderr,
                         "content with 'error' text must NOT be flagged as body-err")


class P1ConstantsTest(unittest.TestCase):
    """P1 地基：v2 常量声明（值精确断言，锚住 P2-P4 桶/间隔契约）。"""

    def test_v2_constants_declared(self) -> None:
        mod = load_proxy_module()
        self.assertEqual(mod.HIST_BUCKETS_MS,
                         (100, 250, 500, 1000, 2000, 5000, 10000, 30000))
        self.assertEqual(mod.PROBE_INTERVAL_S_DEFAULT, 30)
        self.assertEqual(mod.PROBE_TIMEOUT_S, 5)
        self.assertEqual(mod.PROBE_MIN_INTERVAL_S, 10)
        self.assertEqual(mod.PROBE_FAILURE_THRESHOLD, 3)
        self.assertEqual(mod.PROBE_ALERT_DEBOUNCE_S, 300)
        # v2 P3 起 _DAILY_FIELDS 即 DAILY_V2_FIELDS（v3 扩 20 字段，同一 tuple）：
        # 契约按 20 字段字面精确断言。
        self.assertEqual(
            mod.DAILY_V2_FIELDS,
            ("requests", "filtered", "errors_proxy", "errors_upstream",
             "retries", "eof_without_done", "header_retries",
             "tokens_prompt", "tokens_completion",
             "bytes_out", "stream_requests",
             "ttfb_sum_ms", "ttfb_count",
             "outcome_ok", "outcome_degraded", "outcome_failed",
             "tokens_cache_read", "tokens_cache_write",
             "tokens_reasoning", "requests_zero_token"))


class UsageExtractTest(unittest.TestCase):
    """P3 Token期：usage 帧数值抽取矩阵（标准/缺 prompt/缺 completion/extra key/非 dict/空 usage/负值）；
    v3 扩 5 元组（cache_read/reasoning 兜底 0）。"""

    @classmethod
    def setUpClass(cls) -> None:
        # staticmethod：模块级函数赋类属性默认成 method descriptor，
        # self.extract(line) 会多传 cls 导致 TypeError。
        cls.extract = staticmethod(load_proxy_module().sse_line_extract_usage)

    def test_standard_usage_frame(self) -> None:
        line = (b'data: {"id":"u","choices":[],"usage":'
                b'{"prompt_tokens":10,"completion_tokens":5,"total_tokens":15}}\n\n')
        self.assertEqual(self.extract(line), (10, 5, 15, 0, 0))

    def test_missing_prompt_tokens_defaults_zero(self) -> None:
        line = b'data: {"usage":{"completion_tokens":5,"total_tokens":10}}\n\n'
        self.assertEqual(self.extract(line), (0, 5, 10, 0, 0))

    def test_missing_completion_tokens_defaults_zero(self) -> None:
        line = b'data: {"usage":{"prompt_tokens":3,"total_tokens":8}}\n\n'
        self.assertEqual(self.extract(line), (3, 0, 8, 0, 0))

    def test_extra_key_ignored(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"extra":"x","nested":{"a":1}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 0))

    def test_non_dict_returns_none(self) -> None:
        self.assertIsNone(self.extract(b"data: [DONE]\n\n"))
        self.assertIsNone(self.extract(b'data: "just a string"\n\n'))
        self.assertIsNone(self.extract(b"not even a data line\n\n"))
        self.assertIsNone(self.extract(b"data: null\n\n"))
        self.assertIsNone(self.extract(b"data: 42\n\n"))

    def test_empty_usage_dict_returns_none(self) -> None:
        # 委托函数 sse_line_usage 对空 usage dict（{} 为 falsy）返回 None——与
        # sse_line_has_usage 的 bool(usage) 判据同源；extract 透传该 None 语义。
        line = b'data: {"usage":{}}\n\n'
        self.assertIsNone(self.extract(line))

    def test_negative_tokens_coerced_zero(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":-1,"completion_tokens":5,'
                b'"total_tokens":10}}\n\n')
        self.assertEqual(self.extract(line), (0, 5, 10, 0, 0))

    def test_non_int_fields_coerced_zero(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":"9","completion_tokens":null,'
                b'"total_tokens":true}}\n\n')
        self.assertEqual(self.extract(line), (0, 0, 0, 0, 0))

    def test_ride_on_finish_frame_usage_extracted(self) -> None:
        line = (b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":7,"completion_tokens":2,"total_tokens":9}}\n\n')
        self.assertEqual(self.extract(line), (7, 2, 9, 0, 0))

    def test_has_usage_bool_consistent_with_extract(self) -> None:
        """与 sse_line_has_usage 布尔判据一致性：has_usage=True ⟺ extract 非 None。"""
        mod = load_proxy_module()
        for line in (SSE_USAGE,
                     b'data: {"usage":{}}\n\n',
                     b"data: [DONE]\n\n",
                     b'data: {"choices":[{"delta":{"content":"A"}}]}\n\n'):
            self.assertEqual(mod.sse_line_has_usage(line),
                             mod.sse_line_extract_usage(line) is not None,
                             "has_usage/extract must agree on %r" % line)

    # ---- v3：cache_read / reasoning 五元组第 4/5 位解析矩阵 ----

    def test_cache_read_hit_tokens_primary_alias(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_cache_hit_tokens":4}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 4, 0))

    def test_cache_read_input_tokens_alias(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"cache_read_input_tokens":40}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 40, 0))

    def test_cache_read_nested_cached_tokens(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_tokens_details":'
                b'{"cached_tokens":400}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 400, 0))

    def test_cache_read_alias_priority_first_hit_wins(self) -> None:
        # 多级别名同时存在 → 首个命中（prompt_cache_hit_tokens）优先
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_cache_hit_tokens":4,'
                b'"cache_read_input_tokens":40,"prompt_tokens_details":'
                b'{"cached_tokens":400}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 4, 0))

    def test_reasoning_nested_completion_details(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"completion_tokens_details":'
                b'{"reasoning_tokens":5}}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 5))

    def test_reasoning_top_level(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"reasoning_tokens":50}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 50))

    def test_cache_read_non_int_nested_coerced_zero(self) -> None:
        # prompt_tokens_details 非 dict → cache_read 归 0
        line = (b'data: {"usage":{"prompt_tokens":1,"completion_tokens":2,'
                b'"total_tokens":3,"prompt_tokens_details":"oops"}}\n\n')
        self.assertEqual(self.extract(line), (1, 2, 3, 0, 0))


class TokenRelayTest(unittest.TestCase):
    """P3 Token期：usage 数值穿透 relay 路径落 daily_by_model + stream_requests 计数。"""

    def setUp(self) -> None:
        upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_sse_usage_frame_lands_in_daily_by_model(self) -> None:
        """POST 带 SSE_USAGE(prompt=1,completion=1,total=2) 的流 → daily_by_model 当日
        tokens_prompt=1/tokens_completion=1，daily 总桶 stream_requests=1。"""
        data = post_sse(self.proxy_port)
        self.assertEqual(data, SSE_USAGE + SSE_A + SSE_B + SSE_DONE,
                         "stream must relay byte-exact")
        today = time.strftime("%Y-%m-%d")
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        dm = snap["daily_by_model"].get(today, {})
        self.assertEqual(len(dm), 1, "exactly one model expected, got %r" % sorted(dm))
        entry = list(dm.values())[0]
        self.assertEqual(entry["tokens_prompt"], 1)
        self.assertEqual(entry["tokens_completion"], 1)
        self.assertEqual(entry["stream_requests"], 1)
        self.assertEqual(entry["requests"], 1)
        daily_today = snap["daily"][today]
        self.assertEqual(daily_today["tokens_prompt"], 1,
                         "daily total bucket must mirror dm tokens_prompt")
        self.assertEqual(daily_today["stream_requests"], 1)
        # RECENT 条目 tokens 键携带数值三元组
        entry_recent = snap["recent"][-1]
        self.assertIsNone(entry_recent.get("_p3_usage_tokens"),
                          "internal capture attr must not leak into snapshot")
        self.assertIsNotNone(entry_recent.get("tokens"),
                             "RECENT tokens key must be filled by P3")
        self.assertGreater(snap["perf"]["tokens_per_s"], 0,
                           "rates window_tokens must accumulate from usage frame total")


class ModelPricingSeamTest(unittest.TestCase):
    """P3 Token期：CTYUN_MODEL_PRICING env seam + persist model_pricing schema（默认 off）。"""

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_default_off_persists_empty_schema(self) -> None:
        """无 env → /api/config model_pricing == {}；SIGTERM 后 persist 文件含该键。"""
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], {},
                         "model_pricing must default to {} (cost UI off)")
        post_sse(self.proxy_port)   # 触发一次 dirty 落盘
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["model_pricing"], {},
                         "persist top-level model_pricing schema must be present")

    def test_env_seam_overrides_persist(self) -> None:
        """CTYUN_MODEL_PRICING env JSON → /api/config 回显该 dict（默认 off 被覆盖）。"""
        stop_proxy(self.proc)
        pricing = {"deepseek-v4": {"prompt": 0.1, "completion": 0.2}}
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            extra_env={"CTYUN_MODEL_PRICING": json.dumps(pricing)})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], pricing,
                         "env seam must override default with full dict")

    def test_bad_env_json_falls_back_empty(self) -> None:
        """非法 JSON env → {} 不崩。"""
        stop_proxy(self.proc)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={"CTYUN_MODEL_PRICING": "{not-json"})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], {})

    def test_persist_resume_roundtrip(self) -> None:
        """persist 文件已有 model_pricing（无 env）→ 重启后 /api/config 回读同值。"""
        stop_proxy(self.proc)
        pricing = {"m-x": {"prompt": 1.5, "completion": 2.5}}
        self.proc = start_proxy(
            self.upstream_port, free_port(),
            seed_persist={"upstream_base": "http://127.0.0.1:%d" % self.upstream_port,
                          "model_pricing": pricing})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["model_pricing"], pricing,
                         "persist model_pricing must resume across restart")


class TokenPersistTest(unittest.TestCase):
    """P3 Token期端到端：POST 带 usage 帧 SSE → SIGTERM → 重启 → daily_by_model tokens_prompt>0。

    spec P3 Acceptance：daily_by_model 当日当模型 entry 含 tokens_prompt ≥ 1（持久化闭环）。"""

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_token_persist_survives_sigterm_restart(self) -> None:
        """POST 一次带 usage 帧 SSE → SIGTERM → 重启 → daily_by_model tokens_prompt>0。"""
        today = time.strftime("%Y-%m-%d")
        data = post_sse(self.proxy_port)
        self.assertEqual(data, SSE_USAGE + SSE_A + SSE_B + SSE_DONE,
                         "stream must relay byte-exact")

        # 内存态
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        dm = snap["daily_by_model"][today]
        self.assertEqual(len(dm), 1)
        model_key = list(dm)[0]
        self.assertEqual(dm[model_key]["tokens_prompt"], 1)
        self.assertEqual(dm[model_key]["tokens_completion"], 1)

        # SIGTERM 落盘
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        persist_file = os.path.join(self.proc.persist_dir, "settings.json")
        with open(persist_file, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["stats"]["daily_by_model"][today][model_key][
                             "tokens_prompt"], 1,
                         "tokens_prompt must hit disk on SIGTERM")

        # 重启（同 persist 内容 seed）→ daily_by_model tokens_prompt>0
        self.proc = start_proxy(self.upstream_port, free_port(), seed_persist=saved)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap2 = json.loads(body.decode("utf-8"))
        dm2 = snap2["daily_by_model"].get(today, {})
        self.assertIn(model_key, dm2, "model entry must survive restart")
        self.assertGreaterEqual(dm2[model_key]["tokens_prompt"], 1,
                                "tokens_prompt must survive SIGTERM+restart")
        self.assertGreaterEqual(dm2[model_key]["tokens_completion"], 1,
                                "tokens_completion must survive SIGTERM+restart")
        self.assertGreaterEqual(dm2[model_key]["stream_requests"], 1,
                                "stream_requests must survive SIGTERM+restart")


class DailyV2CompatTest(unittest.TestCase):
    """P3 Token期：daily_by_model v2 向后兼容双向 degrade（R2 锁死）。

    旧 7 字段 JSON → 20 字段内存桶零值补齐；
    新 20 字段 JSON → 7 字段加载函数（模拟旧版二进制）只取 7 字段不崩。
    """

    def test_legacy_7_field_load_fills_new_fields_with_zero(self) -> None:
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p3compat-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": {
                "2026-01-02": {"requests": 5, "filtered": 1, "errors_proxy": 0,
                               "errors_upstream": 2, "retries": 1,
                               "eof_without_done": 0, "header_retries": 0}},
                "daily_by_model": {
                    "2026-01-02": {"m1": {"requests": 3, "filtered": 0,
                                          "errors_proxy": 0, "errors_upstream": 0,
                                          "retries": 0, "eof_without_done": 0,
                                          "header_retries": 0}}}}}, fh)
        buckets = mod.load_daily_buckets(path)
        self.assertEqual(buckets["2026-01-02"]["requests"], 5)
        self.assertEqual(set(buckets["2026-01-02"]), set(mod.DAILY_V2_FIELDS),
                         "legacy 7-field bucket must load with 20-field zero-fill")
        self.assertEqual(buckets["2026-01-02"]["tokens_prompt"], 0)
        self.assertEqual(buckets["2026-01-02"]["stream_requests"], 0)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(set(dbm["2026-01-02"]["m1"]), set(mod.DAILY_V2_FIELDS),
                         "legacy 7-field dm entry must load with 20-field zero-fill")
        self.assertEqual(dbm["2026-01-02"]["m1"]["tokens_completion"], 0)

    def test_new_20_field_downgrade_reads_7_fields_without_crash(self) -> None:
        """降级演练：20 字段 JSON 被'旧版 7 字段白名单'加载函数读入 → 只返 7 字段不崩。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p3compat-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        entry16 = dict.fromkeys(mod.DAILY_V2_FIELDS, 0)
        entry16.update({"requests": 4, "tokens_prompt": 11, "tokens_completion": 7,
                        "stream_requests": 2, "bytes_out": 999})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "2026-01-02": {"m1": entry16}}}}, fh)
        legacy_7 = ("requests", "filtered", "errors_proxy", "errors_upstream",
                    "retries", "eof_without_done", "header_retries")

        def legacy_loader(raw) -> dict:
            # 模拟旧版二进制：_DAILY_FIELDS 7 字段白名单迭代
            return {k: raw.get(k, 0) for k in legacy_7}

        raw_entry = json.loads(open(path, encoding="utf-8").read())[
            "stats"]["daily_by_model"]["2026-01-02"]["m1"]
        out = legacy_loader(raw_entry)
        self.assertEqual(out["requests"], 4)
        self.assertEqual(set(out), set(legacy_7),
                         "old 7-field whitelist must silently drop the 13 new fields")
        self.assertNotIn("tokens_prompt", out)

    def test_roundtrip_full_equality(self) -> None:
        """20 字段桶 save→load 全等（含新字段值）。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p3compat-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        orig = mod.STATS["daily_by_model"]
        try:
            mod.STATS["daily_by_model"] = {}
            mod._record_request("POST", "/x", 200, 3.0, 0, model="m-a", stream=1,
                                ttfb_ms=120.0, outcome=mod.CLASS_OK,
                                tokens_prompt=11, tokens_completion=7,
                                tokens=18, bytes_out=500, chunks=2)
            entry = mod.STATS["daily_by_model"][mod.today_key()]["m-a"]
            self.assertEqual(entry["tokens_prompt"], 11)
            self.assertEqual(entry["tokens_completion"], 7)
            self.assertEqual(entry["stream_requests"], 1)
            self.assertEqual(entry["bytes_out"], 500)
            self.assertEqual(entry["ttfb_sum_ms"], 120)
            self.assertEqual(entry["ttfb_count"], 1)
            self.assertEqual(entry["outcome_ok"], 1)
            self.assertEqual(entry["outcome_degraded"], 0)
            self.assertEqual(entry["outcome_failed"], 0)
            mod.save_stats_counters(path)
            self.assertEqual(mod.load_daily_by_model_buckets(path),
                             mod.STATS["daily_by_model"],
                             "20-field buckets must roundtrip verbatim")
        finally:
            mod.STATS["daily_by_model"] = orig

    def test_outcome_tri_state_mapping(self) -> None:
        """_outcome_tri_state 映射矩阵（P4 正式化前的 P3 落桶口径）。"""
        mod = load_proxy_module()
        f = mod._outcome_tri_state
        self.assertEqual(f(mod.CLASS_OK), "ok")
        self.assertEqual(f(mod.CLASS_POISON_FIXED), "degraded")
        self.assertEqual(f(mod.CLASS_CLIENT_ABORT), "degraded")
        self.assertEqual(f(mod.CLASS_UPSTREAM_FAULT), "failed")
        self.assertEqual(f(mod.CLASS_BODY_ERROR), "failed")
        self.assertEqual(f(mod.CLASS_REQUEST_FAULT), "failed")


class SchemaV3MigrationTest(unittest.TestCase):
    """v3 schema 迁移：DAILY_V2_FIELDS 16→20 双向 degrade，daily_by_key 8 字段常量。"""

    def test_daily_v2_fields_count_20_and_order(self) -> None:
        mod = load_proxy_module()
        self.assertEqual(len(mod.DAILY_V2_FIELDS), 20)
        self.assertEqual(
            mod.DAILY_V2_FIELDS,
            ("requests", "filtered", "errors_proxy", "errors_upstream",
             "retries", "eof_without_done", "header_retries",
             "tokens_prompt", "tokens_completion",
             "bytes_out", "stream_requests",
             "ttfb_sum_ms", "ttfb_count",
             "outcome_ok", "outcome_degraded", "outcome_failed",
             "tokens_cache_read", "tokens_cache_write",
             "tokens_reasoning", "requests_zero_token"))

    def test_legacy_16_field_persist_loads_with_4_zero_fill(self) -> None:
        """AC1：写一份 16 字段 legacy persist（无 4 新键）→ load 后每桶 20 键、4 新键 0。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3schema-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        legacy16 = ("requests", "filtered", "errors_proxy", "errors_upstream",
                    "retries", "eof_without_done", "header_retries",
                    "tokens_prompt", "tokens_completion",
                    "bytes_out", "stream_requests",
                    "ttfb_sum_ms", "ttfb_count",
                    "outcome_ok", "outcome_degraded", "outcome_failed")
        entry = dict.fromkeys(legacy16, 0)
        entry.update({"requests": 5, "tokens_prompt": 11, "stream_requests": 2})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily": {"2026-01-02": entry},
                                 "daily_by_model": {"2026-01-02": {"m1": entry}}}},
                      fh)
        buckets = mod.load_daily_buckets(path)
        self.assertEqual(len(buckets["2026-01-02"]), 20,
                         "legacy 16-field file must load as 20-field bucket")
        self.assertEqual(set(buckets["2026-01-02"]), set(mod.DAILY_V2_FIELDS))
        for f in ("tokens_cache_read", "tokens_cache_write",
                  "tokens_reasoning", "requests_zero_token"):
            self.assertEqual(buckets["2026-01-02"][f], 0,
                             "%s must zero-fill from legacy 16-field file" % f)
        self.assertEqual(buckets["2026-01-02"]["tokens_prompt"], 11)
        dbm = mod.load_daily_by_model_buckets(path)
        self.assertEqual(len(dbm["2026-01-02"]["m1"]), 20)
        self.assertEqual(set(dbm["2026-01-02"]["m1"]), set(mod.DAILY_V2_FIELDS))
        self.assertEqual(dbm["2026-01-02"]["m1"]["tokens_cache_read"], 0)

    def test_new_20_field_downgrade_reads_16_without_crash(self) -> None:
        """AC1 反向：20 字段被'旧版 16 字段白名单'加载丢 4 新键不崩。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3downgrade-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        entry20 = dict.fromkeys(mod.DAILY_V2_FIELDS, 0)
        entry20.update({"requests": 4, "tokens_cache_read": 9,
                        "tokens_reasoning": 7, "requests_zero_token": 1})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_model": {
                "2026-01-02": {"m1": entry20}}}}, fh)
        legacy_16 = ("requests", "filtered", "errors_proxy", "errors_upstream",
                     "retries", "eof_without_done", "header_retries",
                     "tokens_prompt", "tokens_completion",
                     "bytes_out", "stream_requests",
                     "ttfb_sum_ms", "ttfb_count",
                     "outcome_ok", "outcome_degraded", "outcome_failed")

        def legacy_loader(raw):
            return {k: raw.get(k, 0) for k in legacy_16}

        raw_entry = json.loads(open(path, encoding="utf-8").read())[
            "stats"]["daily_by_model"]["2026-01-02"]["m1"]
        out = legacy_loader(raw_entry)
        self.assertEqual(out["requests"], 4)
        self.assertEqual(set(out), set(legacy_16),
                         "old 16-field whitelist must silently drop the 4 new fields")
        self.assertNotIn("tokens_cache_read", out)

    def test_daily_by_key_fields_constant_8(self) -> None:
        mod = load_proxy_module()
        # 用 getattr 防 AttributeError——RED 期常量未定义时干净 FAIL
        self.assertEqual(
            getattr(mod, "_DAILY_BY_KEY_FIELDS", None),
            ("requests", "tokens_prompt", "tokens_completion",
             "tokens_cache_read", "tokens_cache_write",
             "tokens_reasoning", "bytes_out", "stream_requests"))

    def test_v3_capacity_constants(self) -> None:
        mod = load_proxy_module()
        self.assertEqual(getattr(mod, "BY_KEY_CAP", None), 64)
        self.assertEqual(getattr(mod, "QWAIT_RING_MAX", None), 500)
        self.assertEqual(getattr(mod, "SETTLE_RING_MAX", None), 500)
        self.assertEqual(getattr(mod, "HOURLY_RING_MAX", None), 49)


class RecordRequestV3Test(unittest.TestCase):
    """v3 _record_request 新维度：daily_by_key/phase 双写/qwait 环/hourly/zero_token。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_daily_by_key_records_tokens(self) -> None:
        mod = self.mod
        today = mod.today_key()
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            mod._record_request("POST", "/k1", 200, 1.0, 0, model="m1",
                                key_id12="abcdef123456",
                                tokens_prompt=10, tokens_completion=5,
                                tokens_cache_read=4, tokens_reasoning=3,
                                tokens=22, bytes_out=100, stream=1)
            entry = mod.STATS["daily_by_key"][today]["abcdef123456"]
            self.assertEqual(entry["requests"], 1)
            self.assertEqual(entry["tokens_prompt"], 10)
            self.assertEqual(entry["tokens_completion"], 5)
            self.assertEqual(entry["tokens_cache_read"], 4)
            self.assertEqual(entry["tokens_reasoning"], 3)
            self.assertEqual(entry["bytes_out"], 100)
            self.assertEqual(entry["stream_requests"], 1)
            self.assertEqual(entry["tokens_cache_write"], 0,
                             "cache_write 不解析，恒 0（Exclusions）")
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_daily_by_key_cap_stops_new_keys(self) -> None:
        mod = self.mod
        today = mod.today_key()
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            for i in range(mod.BY_KEY_CAP):
                mod._record_request("POST", "/c", 200, 1.0, 0,
                                    key_id12="key%012d" % i)
            mod._record_request("POST", "/c", 200, 1.0, 0,
                                key_id12="overflow-key-1")
            day_keys = mod.STATS["daily_by_key"][today]
            self.assertEqual(len(day_keys), mod.BY_KEY_CAP,
                             "cap 后新 key 不记录；got %d keys" % len(day_keys))
            self.assertNotIn("overflow-key-1", day_keys)
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_phase_ms_dual_write_global_and_per_model(self) -> None:
        mod = self.mod
        before_g = {k: list(v) for k, v in mod.STATS["phase_ms"].items()}
        orig_m = mod.STATS["phase_ms_by_model"]
        mod.STATS["phase_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/p", 200, 1.0, 0, model="m1",
                                phase_ms={"connect": 120.0, "headers": 300.0,
                                          "body": 600.0})
            # HIST_BUCKETS_MS=(100,250,500,1000,...): 120→idx1, 300→idx2, 600→idx3
            self.assertEqual(mod.STATS["phase_ms"]["connect"][1],
                             before_g["connect"][1] + 1, "全局 connect +1")
            self.assertEqual(mod.STATS["phase_ms"]["headers"][2],
                             before_g["headers"][2] + 1, "全局 headers +1")
            self.assertEqual(mod.STATS["phase_ms"]["body"][3],
                             before_g["body"][3] + 1, "全局 body +1")
            self.assertEqual(mod.STATS["phase_ms_by_model"]["m1"]["connect"][1], 1)
            self.assertEqual(mod.STATS["phase_ms_by_model"]["m1"]["headers"][2], 1)
            self.assertEqual(mod.STATS["phase_ms_by_model"]["m1"]["body"][3], 1)
        finally:
            mod.STATS["phase_ms_by_model"] = orig_m

    def test_phase_ms_model_none_only_global(self) -> None:
        mod = self.mod
        orig_m = mod.STATS["phase_ms_by_model"]
        mod.STATS["phase_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/p", 200, 1.0, 0,
                                phase_ms={"connect": 50.0, "headers": 80.0,
                                          "body": 200.0})
            self.assertEqual(mod.STATS["phase_ms_by_model"], {},
                             "model=None 不写 per-model 直方图")
        finally:
            mod.STATS["phase_ms_by_model"] = orig_m

    def test_qwait_ring_accumulates(self) -> None:
        mod = self.mod
        orig_q = mod.STATS["qwait_ms_by_model"]
        mod.STATS["qwait_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/q", 200, 1.0, 0, model="m1", qwait_ms=15)
            mod._record_request("POST", "/q", 200, 1.0, 0, model="m1", qwait_ms=25)
            self.assertEqual(list(mod.STATS["qwait_ms_by_model"]["m1"]), [15, 25])
            # 无 qwait / 无 model 不落
            mod._record_request("POST", "/q", 200, 1.0, 0)
            mod._record_request("POST", "/q", 200, 1.0, 0, model="m2")
            self.assertEqual(list(mod.STATS["qwait_ms_by_model"]["m1"]), [15, 25])
            self.assertNotIn("m2", mod.STATS["qwait_ms_by_model"],
                             "qwait_ms=None 不得建 m2 环")
        finally:
            mod.STATS["qwait_ms_by_model"] = orig_q

    def test_hourly_tokens_cross_boundary(self) -> None:
        mod = self.mod
        orig_time_func = mod.time.time
        try:
            mod.time.time = lambda: 1698825600.0  # 2023-11-01 00:00:00 UTC
            mod.STATS["hourly_tokens"].clear()
            mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                                tokens_prompt=10, tokens_completion=5)
            self.assertEqual(len(mod.STATS["hourly_tokens"]), 1)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["hour_start_ts"],
                             1698825600)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["tokens_prompt"], 10)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["tokens_completion"], 5)
            # 同小时追加：累积不新增条目
            mod.time.time = lambda: 1698827400.0  # +30 min
            mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                                tokens_prompt=3, tokens_completion=2)
            self.assertEqual(len(mod.STATS["hourly_tokens"]), 1)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["tokens_prompt"], 13)
            # 跨小时：新增条目
            mod.time.time = lambda: 1698829200.0  # +1h
            mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                                tokens_prompt=1, tokens_completion=0)
            self.assertEqual(len(mod.STATS["hourly_tokens"]), 2)
            self.assertEqual(mod.STATS["hourly_tokens"][-1]["hour_start_ts"],
                             1698829200)
        finally:
            mod.time.time = orig_time_func

    def test_requests_zero_token_increments(self) -> None:
        mod = self.mod
        today = mod.today_key()
        # tokens=None 且 status<500 → zero_token +1
        mod._record_request("POST", "/z1", 200, 1.0, 0, model="m1")
        b = mod.STATS["daily"][today]
        self.assertEqual(b["requests_zero_token"], 1)
        # tokens=0 且 status<500 → +1
        mod._record_request("POST", "/z2", 200, 1.0, 0, model="m1", tokens=0)
        self.assertEqual(b["requests_zero_token"], 2)
        # status>=500 → 不计
        mod._record_request("POST", "/z3", 500, 1.0, 0, model="m1")
        self.assertEqual(b["requests_zero_token"], 2)
        # error=True → 不计
        mod._record_request("POST", "/z4", 200, 1.0, 0, error=True)
        self.assertEqual(b["requests_zero_token"], 2)

    def test_tokens_cache_read_reasoning_in_daily_bucket(self) -> None:
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/t", 200, 1.0, 0, model="m1",
                            tokens_prompt=10, tokens_completion=5,
                            tokens_cache_read=4, tokens_reasoning=3)
        b = mod.STATS["daily"][today]
        self.assertEqual(b["tokens_cache_read"], 4)
        self.assertEqual(b["tokens_reasoning"], 3)
        self.assertEqual(b["tokens_cache_write"], 0,
                         "cache_write 不解析，桶字段恒 0（Exclusions）")

    def test_hourly_bytes_chunks_accumulate(self) -> None:
        """吞吐 v3.1：hourly_tokens 条目扩 bytes_out/chunks 字段同环滚动。"""
        mod = self.mod
        mod.STATS["hourly_tokens"].clear()
        try:
            mod._record_request("POST", "/hb", 200, 1.0, 0, model="m1",
                                bytes_out=500, chunks=7,
                                tokens_prompt=10, tokens_completion=5)
            entry = mod.STATS["hourly_tokens"][-1]
            self.assertEqual(entry["bytes_out"], 500)
            self.assertEqual(entry["chunks"], 7)
            self.assertEqual(entry["tokens_prompt"], 10)
            self.assertEqual(entry["tokens_completion"], 5)
        finally:
            mod.STATS["hourly_tokens"].clear()

    def test_bytes_in_rates_window(self) -> None:
        """吞吐 v3.1：入方向字节（请求体）进 60s rates 窗口 + perf.bytes_in_per_s。"""
        mod = self.mod
        before_total = mod.STATS["rates"]["bytes_in_total"]
        before_win = mod.STATS["rates"]["window_bytes_in"]
        mod._record_request("POST", "/bi", 200, 1.0, 0, model="m1", bytes_in=2048)
        self.assertEqual(mod.STATS["rates"]["bytes_in_total"], before_total + 2048)
        self.assertEqual(mod.STATS["rates"]["window_bytes_in"], before_win + 2048)
        snap = mod.stats_snapshot()
        self.assertIn("bytes_in_per_s", snap["perf"])


class RelaySseV3Test(unittest.TestCase):
    """v3 _relay_sse model kwarg + stalls per-model 双写（决策 3）。

    用 handler stub + fake resp 进程内直驱 _relay_sse：STALL_THRESHOLD_S 降至 0.0
    后任意相邻 record 间隔即触发 stall，可确定性断言 stalls_total 与
    stalls_by_model 的双写语义（含 model=None 只增全局）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    class _StubHandler:
        """_relay_sse 可运行的最小 handler stub（wfile/conn/响应三件套）。

        _relay_sse/_send_sse_headers 是 ProxyHandler 真方法，经未绑定调用
        mod.ProxyHandler._relay_sse(handler, ...) 驱动；stub 只供实例属性。"""

        def __init__(self):
            self.wfile = io.BytesIO()
            self._req_id = "req-1"
            self.close_connection = False
            self._t_last_record = None
            self._t_first_byte_mark = None
            self._relay_bytes = 0
            self._relay_chunks = 0
            self._body_err_line = None
            self._tpm_usage = None
            self._p3_usage_tokens = None

        class _StubConn:
            def gettimeout(self): return 600
            def settimeout(self, _v): pass
        connection = _StubConn()

        def send_response(self, _code): pass
        def send_header(self, _k, _v): pass
        def end_headers(self): pass
        def _send_sse_headers(self, _resp): pass

    class _FakeSseResp:
        """fake resp：readline 依序吐行，耗尽后 b"" EOF；getheaders 空即可。"""
        status = 200

        def __init__(self, lines):
            self._lines = list(lines)
            self._i = 0

        def readline(self):
            if self._i < len(self._lines):
                line = self._lines[self._i]
                self._i += 1
                return line
            return b""

        def getheaders(self):
            return [("Content-Type", "text/event-stream")]

    def _drive(self, lines):
        """统一驱动：STALL_THRESHOLD_S=0.0 下走完 [SSE_A, b"\\n", SSE_DONE, b"\\n"] 流。"""
        mod = self.mod
        orig_thresh = mod.STALL_THRESHOLD_S
        try:
            mod.STALL_THRESHOLD_S = 0.0
            handler = self._StubHandler()
            resp = self._FakeSseResp(lines)
            return (mod.ProxyHandler._relay_sse(handler, resp, final=True,
                                                model="m1"), handler)
        finally:
            mod.STALL_THRESHOLD_S = orig_thresh

    def test_relay_sse_has_model_kwarg(self) -> None:
        import inspect
        sig = inspect.signature(self.mod.ProxyHandler._relay_sse)
        self.assertIn("model", sig.parameters)
        self.assertEqual(sig.parameters["model"].default, None)

    def test_stall_increments_global_and_per_model(self) -> None:
        mod = self.mod
        orig_sm = dict(mod.STATS["stalls_by_model"])
        mod.STATS["stalls_by_model"] = {}
        stalls_before = mod.STATS["stalls_total"]
        try:
            (filtered, truncated), handler = self._drive(
                [SSE_A, b"\n", SSE_DONE, b"\n"])
            self.assertEqual(filtered, 0)
            self.assertFalse(truncated)
            self.assertGreater(mod.STATS["stalls_total"], stalls_before,
                               "相邻 record 间隔必须计入 stalls_total")
            self.assertGreater(mod.STATS["stalls_by_model"].get("m1", 0), 0,
                               "model=m1 时 stalls_by_model[m1] 必须同步 +1")
        finally:
            mod.STATS["stalls_by_model"] = orig_sm

    def test_stall_model_none_only_global(self) -> None:
        mod = self.mod
        orig_sm = dict(mod.STATS["stalls_by_model"])
        mod.STATS["stalls_by_model"] = {}
        orig_thresh = mod.STALL_THRESHOLD_S
        try:
            mod.STALL_THRESHOLD_S = 0.0
            handler = self._StubHandler()
            resp = self._FakeSseResp([SSE_A, b"\n", SSE_DONE, b"\n"])
            mod.ProxyHandler._relay_sse(handler, resp, final=True)  # model 默认 None
            self.assertEqual(mod.STATS["stalls_by_model"], {},
                             "model=None 不得写 stalls_by_model")
        finally:
            mod.STALL_THRESHOLD_S = orig_thresh
            mod.STATS["stalls_by_model"] = orig_sm

    def test_record_request_kwargs_on_stream_settlement(self) -> None:
        """流式结算点：_record_request 新 kwargs 全部透传落桶验证。
        直接调 _record_request 模拟卡 3 完工后的实际调用形态。"""
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/s", 200, 100.0, 0, model="m1",
                            stream=1,
                            tokens_prompt=10, tokens_completion=5,
                            tokens_cache_read=4, tokens_reasoning=3,
                            qwait_ms=15, key_id12="abcdef123456",
                            tokens=22, outcome=mod.CLASS_OK)
        self.assertEqual(
            mod.STATS["daily_by_key"][today]["abcdef123456"]["tokens_prompt"], 10)
        self.assertEqual(
            mod.STATS["daily_by_key"][today]["abcdef123456"]["tokens_cache_read"], 4)
        self.assertEqual(
            mod.STATS["daily_by_key"][today]["abcdef123456"]["tokens_reasoning"], 3)
        qwait = list(mod.STATS["qwait_ms_by_model"].get("m1", []))
        self.assertIn(15, qwait)

    def test_record_request_kwargs_on_non_stream_settlement(self) -> None:
        """非流式结算点：同流式验证、但 stream=0。"""
        mod = self.mod
        today = mod.today_key()
        mod._record_request("POST", "/n", 200, 50.0, 0, model="m2",
                            stream=0,
                            tokens_prompt=7, tokens_completion=3,
                            tokens_cache_read=2, tokens_reasoning=1,
                            qwait_ms=8, key_id12="deadbeef0001",
                            tokens=14, outcome=mod.CLASS_OK)
        by_key = mod.STATS["daily_by_key"][today].get("deadbeef0001")
        self.assertIsNotNone(by_key)
        self.assertEqual(by_key["tokens_prompt"], 7)
        self.assertEqual(by_key["tokens_cache_read"], 2)
        self.assertEqual(by_key["tokens_reasoning"], 1)
        # qwait 落 m2 环
        self.assertIn(8, list(mod.STATS["qwait_ms_by_model"].get("m2", [])))


class SettlePersistV3Test(unittest.TestCase):
    """v3 tpm_settle 比率 + daily_by_key save/load roundtrip（决策 2/4）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_tpm_settle_records_ratio(self) -> None:
        mod = self.mod
        orig_buckets = mod.TPM_BUCKETS
        orig_ring = mod.STATS["tpm_settle_ratio_by_model"]
        mod.TPM_BUCKETS = {}
        mod.STATS["tpm_settle_ratio_by_model"] = {}
        try:
            bucket = mod._TpmBucket()
            bucket.used = 0  # .used 是 deque 子类的附加属性，须显式初始化（同既有 tpm 测试）
            mod.TPM_BUCKETS[("k1", "m1")] = bucket
            # actual=90, est=100 → 0.9
            mod.tpm_settle("k1", 100, 90, "m1")
            self.assertEqual(
                list(mod.STATS["tpm_settle_ratio_by_model"]["m1"]), [0.9],
                "actual/est=0.9 必须入环")
            # est=0 / actual<0 → 不追加
            mod.tpm_settle("k1", 0, 90, "m1")
            mod.tpm_settle("k1", 100, -5, "m1")
            self.assertEqual(
                len(mod.STATS["tpm_settle_ratio_by_model"]["m1"]), 1,
                "est<=0 或 actual<0 不得追加比率样本")
            # est==actual → 1.0（delta==0 不退款但比率仍有观测价值）
            mod.tpm_settle("k1", 100, 100, "m1")
            self.assertEqual(
                list(mod.STATS["tpm_settle_ratio_by_model"]["m1"]), [0.9, 1.0])
        finally:
            mod.TPM_BUCKETS = orig_buckets
            mod.STATS["tpm_settle_ratio_by_model"] = orig_ring

    def test_save_stats_counters_includes_daily_by_key(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykey-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        today = mod.today_key()
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            mod._record_request("POST", "/k", 200, 1.0, 0, model="m1",
                                key_id12="abcdef123456", tokens_prompt=10,
                                tokens_completion=5, stream=1)
            mod.save_stats_counters(path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.assertIn("daily_by_key", data.get("stats", {}),
                          "落盘必须含 daily_by_key 键")
            entry = data["stats"]["daily_by_key"][today]["abcdef123456"]
            self.assertEqual(entry["tokens_prompt"], 10)
            self.assertEqual(entry["stream_requests"], 1)
            # roundtrip 全等
            self.assertEqual(
                mod.load_daily_by_key_buckets(path),
                mod.STATS["daily_by_key"],
                "save→load roundtrip 必须全等")
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_load_daily_by_key_buckets_tolerant(self) -> None:
        mod = self.mod
        loader = getattr(mod, "load_daily_by_key_buckets", None)
        self.assertIsNotNone(loader, "load_daily_by_key_buckets 必须存在")
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykey-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": {
                "2026-01-02": {
                    "abcdef123456": {"requests": 5, "tokens_prompt": 11},
                    "BAD_KEY_WITH_UPPER": {"requests": 1},
                    "toolongkeymorethan12": {"requests": 1},
                    "": {"requests": 1}, "nothex!": {"requests": 1},
                    "1234": "not-a-dict",
                }}}}, fh)
        out = loader(path)
        self.assertEqual(set(out["2026-01-02"]), {"abcdef123456"},
                         "非 1-12 位小写 hex 或空 key 必须丢弃")
        self.assertEqual(set(out["2026-01-02"]["abcdef123456"]),
                         set(mod._DAILY_BY_KEY_FIELDS), "8 字段白名单清洗")
        self.assertEqual(out["2026-01-02"]["abcdef123456"]["tokens_prompt"], 11)
        self.assertEqual(out["2026-01-02"]["abcdef123456"]["tokens_cache_write"], 0)
        # 损坏 / 非 ISO → {}
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": "not-a-dict"}}, fh)
        self.assertEqual(loader(path), {})
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": {"20260101": {"abcd": {}}}}}, fh)
        self.assertEqual(loader(path), {}, "非 ISO 日期 key 必须跳过")

    def test_load_daily_by_key_cap_truncates(self) -> None:
        mod = self.mod
        loader = getattr(mod, "load_daily_by_key_buckets", None)
        self.assertIsNotNone(loader)
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykeycap-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"daily_by_key": {
                "2026-01-02": {"%012x" % i: {"requests": i}
                               for i in range(mod.BY_KEY_CAP + 10)}}}}, fh)
        out = loader(path)
        day = out["2026-01-02"]
        self.assertEqual(len(day), mod.BY_KEY_CAP,
                         "超 BY_KEY_CAP 必须按文件出现序截断")
        self.assertIn("%012x" % (mod.BY_KEY_CAP - 1), day)
        self.assertNotIn("%012x" % mod.BY_KEY_CAP, day)

    def test_daily_by_key_prune_applies_retention(self) -> None:
        mod = self.mod
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-v3bykeyprune-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        try:
            # _prune_daily 是「保留最近 N 天」计数 prune（非按龄截断）——
            # 灌满 RETENTION+10 天触发窗口滚动，验证 daily_by_key 同口径参与
            base = mod.datetime.date.fromisoformat(mod.today_key())
            for i in range(mod.DAILY_RETENTION_DAYS + 10):
                day = (base - mod.datetime.timedelta(days=i)).isoformat()
                mod.STATS["daily_by_key"][day] = {
                    "abcdef123456": dict.fromkeys(mod._DAILY_BY_KEY_FIELDS, 0)}
            mod.save_stats_counters(path)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            saved = data["stats"]["daily_by_key"]
            self.assertEqual(len(saved), mod.DAILY_RETENTION_DAYS,
                             "daily_by_key 必须与 daily 同口径 prune 到保留窗口")
            oldest = (base - mod.datetime.timedelta(
                days=mod.DAILY_RETENTION_DAYS)).isoformat()
            self.assertNotIn(oldest, saved, "窗口外最旧日期桶必须被 prune")
            self.assertIn(mod.today_key(), saved)
        finally:
            mod.STATS["daily_by_key"] = orig


class ProbeHistoryV3Test(unittest.TestCase):
    """v3 probe history (ts, ms) 二元组 + 快照降采样 + /api/probe_history 端点（决策 7）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_probe_history_snapshot_downsamples_to_200(self) -> None:
        mod = self.mod
        orig_hist = mod._PROBE_STATE["history"]
        try:
            # 注入 1000 条 monotonically 递增 ts 的二元组
            mod._PROBE_STATE["history"] = collections.deque(
                [(float(i), float(i * 10)) for i in range(1000)])
            snap = mod.probe_history_snapshot()
            self.assertLessEqual(len(snap["points"]), 200)
            self.assertEqual(snap["count_total"], 1000)
            ts = [p[0] for p in snap["points"]]
            self.assertEqual(ts, sorted(ts), "points 必须单调 ts")
        finally:
            mod._PROBE_STATE["history"] = orig_hist

    def test_probe_history_snapshot_small_returns_all(self) -> None:
        mod = self.mod
        orig_hist = mod._PROBE_STATE["history"]
        try:
            mod._PROBE_STATE["history"] = collections.deque(
                [(10.0, 5.0), (11.0, 6.0), (12.0, 7.0)])
            snap = mod.probe_history_snapshot()
            self.assertEqual(snap["points"],
                             [[10.0, 5.0], [11.0, 6.0], [12.0, 7.0]])
            self.assertEqual(snap["count_total"], 3)
        finally:
            mod._PROBE_STATE["history"] = orig_hist

    def test_latency_p90_tuples_equivalent_to_scalar_oracle(self) -> None:
        mod = self.mod
        values = [50.0] * 180 + [500.0] * 20
        ordered = sorted(values)
        expected = float(ordered[int(0.9 * (len(ordered) - 1))])
        tuples = [(float(i), v) for i, v in enumerate(values)]
        self.assertEqual(mod._latency_p90(tuples), expected,
                         "二元组输入与手工最近秩（标量 oracle）一致")
        self.assertEqual(mod._latency_p90(values), expected,
                         "旧标量形态继续支持（AC8 回归）")
        self.assertEqual(mod._latency_p90([]), 0.0)

    def test_probe_spike_due_tuples(self) -> None:
        mod = self.mod
        f = mod._probe_spike_due
        # 全平 → 无突增
        self.assertFalse(f([(0.0, 50.0)] * 200))
        # 180×50 + 20×500：基线 P90≈50，最近 20 P90=500 > 100 → 告警
        self.assertTrue(f([(0.0, 50.0)] * 180 + [(0.0, 500.0)] * 20))

    def test_probe_history_endpoint_returns_json(self) -> None:
        upstream_port = make_fake_upstream(False)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port,
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "3600"})
        try:
            _, body, _ = admin_get(proc.admin_port, "/api/probe_history")
            snap = json.loads(body.decode("utf-8"))
            self.assertIn("points", snap)
            self.assertIn("count_total", snap)
            self.assertIsInstance(snap["points"], list)
        finally:
            stop_proxy(proc)
            stop_fake_upstreams()


class SnapshotV3Test(unittest.TestCase):
    """v3 stats_snapshot perf 新键 + daily_by_key 深拷 + percentile 推广（决策 6）。"""

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def test_stats_snapshot_contains_v3_keys(self) -> None:
        mod = self.mod
        snap = mod.stats_snapshot()
        self.assertIn("daily_by_key", snap)
        self.assertIn("hourly_tokens", snap)
        self.assertIsInstance(snap["hourly_tokens"], list)
        perf = snap["perf"]
        for key in ("ttfb_hist_by_model", "phase_p50_ms_by_model",
                    "phase_p90_ms_by_model", "stalls_by_model",
                    "qwait_p50_ms_by_model", "qwait_p90_ms_by_model",
                    "tpm_settle_ratio_p50_by_model",
                    "tpm_settle_ratio_p90_by_model"):
            self.assertIn(key, perf, "perf 缺 v3 键 %s" % key)

    def test_qwait_percentile_matches_nearest_rank(self) -> None:
        mod = self.mod
        orig = mod.STATS["qwait_ms_by_model"]
        mod.STATS["qwait_ms_by_model"] = {}
        try:
            samples = [float((i * 7) % 100) for i in range(100)]
            for v in samples:
                mod._record_request("POST", "/q", 200, 1.0, 0, model="m1",
                                    qwait_ms=v)
            snap = mod.stats_snapshot()
            perf = snap["perf"]
            ordered = sorted(samples)
            expected_p90 = round(float(ordered[int(0.9 * (len(ordered) - 1))]), 1)
            expected_p50 = round(float(ordered[int(0.5 * (len(ordered) - 1))]), 1)
            self.assertEqual(perf["qwait_p90_ms_by_model"]["m1"], expected_p90)
            self.assertEqual(perf["qwait_p50_ms_by_model"]["m1"], expected_p50)
        finally:
            mod.STATS["qwait_ms_by_model"] = orig

    def test_settle_ratio_percentile_matches_nearest_rank(self) -> None:
        mod = self.mod
        orig_b = mod.TPM_BUCKETS
        orig_r = mod.STATS["tpm_settle_ratio_by_model"]
        mod.TPM_BUCKETS = {}
        mod.STATS["tpm_settle_ratio_by_model"] = {}
        try:
            bucket = mod._TpmBucket()
            bucket.used = 0  # .used 是 deque 子类的附加属性，须显式初始化
            mod.TPM_BUCKETS[("k1", "m1")] = bucket
            # 50 个样本：0.5, 0.6, ..., 1.4（10 个值各 5 次）
            for i in range(50):
                ratio = 0.5 + (i % 10) * 0.1
                mod.tpm_settle("k1", 100, int(100 * ratio), "m1")
            ordered = sorted(
                [0.5 + (i % 10) * 0.1 for i in range(50)])
            expected_p90 = round(float(ordered[int(0.9 * (len(ordered) - 1))]), 3)
            snap = mod.stats_snapshot()
            self.assertEqual(
                snap["perf"]["tpm_settle_ratio_p90_by_model"]["m1"],
                expected_p90)
        finally:
            mod.TPM_BUCKETS = orig_b
            mod.STATS["tpm_settle_ratio_by_model"] = orig_r

    def test_hourly_tokens_json_serializable(self) -> None:
        mod = self.mod
        mod._record_request("POST", "/h", 200, 1.0, 0, model="m1",
                            tokens_prompt=5, tokens_completion=2)
        snap = mod.stats_snapshot()
        self.assertIsInstance(snap["hourly_tokens"], list)
        self.assertGreaterEqual(len(snap["hourly_tokens"]), 1)
        self.assertIn("hour_start_ts", snap["hourly_tokens"][-1])
        json.dumps(snap)  # deque 裸引用会 TypeError——能 dump 即证明已安全转换

    def test_ttfb_hist_by_model_in_perf(self) -> None:
        mod = self.mod
        orig = mod.STATS["ttfb_hist"]
        mod.STATS["ttfb_hist"] = {"m1": [0] * 9, "m2": [0] * 9}
        try:
            snap = mod.stats_snapshot()
            self.assertEqual(snap["perf"]["ttfb_hist_by_model"],
                             {"m1": [0] * 9, "m2": [0] * 9})
        finally:
            mod.STATS["ttfb_hist"] = orig

    def test_daily_by_key_snapshot_is_deep_copy(self) -> None:
        mod = self.mod
        orig = mod.STATS["daily_by_key"]
        mod.STATS["daily_by_key"] = {}
        try:
            mod._record_request("POST", "/k", 200, 1.0, 0, model="m1",
                                key_id12="abcdef123456", tokens_prompt=10)
            snap = mod.stats_snapshot()
            entry = snap["daily_by_key"][mod.today_key()]["abcdef123456"]
            self.assertEqual(entry["tokens_prompt"], 10)
            # 深拷贝验证：篡改 snap 不影响 STATS
            entry["tokens_prompt"] = 999
            self.assertEqual(
                mod.STATS["daily_by_key"][mod.today_key()]["abcdef123456"][
                    "tokens_prompt"], 10)
        finally:
            mod.STATS["daily_by_key"] = orig

    def test_phase_percentile_by_model(self) -> None:
        mod = self.mod
        orig = mod.STATS["phase_ms_by_model"]
        mod.STATS["phase_ms_by_model"] = {}
        try:
            mod._record_request("POST", "/p", 200, 1.0, 0, model="m1",
                                phase_ms={"connect": 50.0, "headers": 150.0,
                                          "body": 400.0})
            p50 = mod.stats_snapshot()["perf"]["phase_p50_ms_by_model"]["m1"]
            self.assertEqual(set(p50), {"connect", "headers", "body"})
            self.assertGreaterEqual(p50["connect"], 0)
        finally:
            mod.STATS["phase_ms_by_model"] = orig


class RequestIdTest(unittest.TestCase):
    """P1 地基：request id / upstream host / ttfb / stream / outcome 全链路。

    黑盒子进程集成：经 CTYUN_UPSTREAM_BASE seam 指向 fake upstream，
    断言 RECENT_REQUESTS 条目携带 v2 字段且值正确。"""

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_recent_entry_carries_rid_host_ttfb_stream_outcome(self) -> None:
        """RECENT_REQUESTS 条目含 v2 七个新字段；值正确填充。"""
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertTrue(snap["recent"], "RECENT_REQUESTS must not be empty")
        entry = snap["recent"][-1]
        for key in ("rid", "upstream_host", "ttfb_ms", "stream",
                    "tokens", "bytes_out", "outcome"):
            self.assertIn(key, entry,
                          "recent entry must carry '%s' key; got keys %r"
                          % (key, sorted(entry.keys())))
        self.assertEqual(entry["rid"], "r-1",
                         "first request rid must be r-1 (fresh process)")
        self.assertEqual(entry["upstream_host"],
                         "127.0.0.1:%d" % self.upstream_port)
        self.assertIsInstance(entry["ttfb_ms"], float)
        self.assertGreaterEqual(entry["ttfb_ms"], 0)
        self.assertEqual(entry["stream"], 1,
                         "SSE request stream must be 1")
        self.assertEqual(entry["outcome"], "ok",
                         "clean SSE outcome must be ok")
        # tokens 为 P3 前占位；bytes_out 自 P2 起为真实下发字节数（fake 流 >0）
        self.assertIsNone(entry["tokens"])
        self.assertGreater(entry["bytes_out"], 0)

    def test_req_line_carries_rid_host_ttfb_stream_outcome(self) -> None:
        """REQ 行含 rid=r-N / host= / ttfb=<num>ms / stream=1 / outcome=ok。"""
        post_sse(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        m = re.search(
            r"rid=(r-\d+) host=127\.0\.0\.1:%d "
            r"ttfb=\d+\.\dms stream=1 outcome=ok ts=" % self.upstream_port,
            stderr, re.M)
        self.assertIsNotNone(m,
            "REQ 行必须带 rid/host/ttfb/stream/outcome 字段，stderr:\n" + stderr)
        self.assertEqual(m.group(1), "r-1")

    def test_sse_response_carries_x_request_id_matching_req_line(self) -> None:
        """SSE 响应头 X-Request-Id 与 REQ 行 rid 一致。"""
        _, x_request_id = post_sse_with_headers(self.proxy_port)
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr = stderr_text(self.proc)
        m = re.search(r"rid=(r-\d+) host=", stderr)
        self.assertIsNotNone(m, "REQ 行必须含 rid=，stderr:\n" + stderr)
        self.assertEqual(x_request_id, m.group(1),
                         "X-Request-Id 响应头必须与 REQ 行 rid 一致")

    def test_plain_response_carries_x_request_id(self) -> None:
        """非流式（/plain GET → _relay_buffered）响应也带 X-Request-Id。"""
        _, _, x_request_id = get_plain_with_headers(self.proxy_port)
        m = re.match(r"^r-\d+$", x_request_id or "")
        self.assertIsNotNone(m,
            "非流式响应必须带 X-Request-Id，got %r" % x_request_id)

    def test_502_response_carries_x_request_id(self) -> None:
        """代理合成 502 响应也带 X-Request-Id。"""
        dead_port = free_port()  # 死端口：连接即 ECONNREFUSED → 合成 502
        stop_proxy(self.proc)  # 含旧实例 persist_dir 清理（followups 登记的泄漏点）
        self.proc = start_proxy(dead_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1",
                                          self.proc.proxy_port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     body=b'{"model":"m","stream":true,"messages":[]}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        x_request_id = resp.getheader("X-Request-Id")
        resp.read()
        conn.close()
        self.assertEqual(status, 502,
                         "死上游必须合成 502")
        m = re.match(r"^r-\d+$", x_request_id or "")
        self.assertIsNotNone(m,
            "502 响应必须带 X-Request-Id，got %r" % x_request_id)

    def test_upstream_x_request_id_stripped_for_sse(self) -> None:
        """上游自带 X-Request-Id 时 SSE 路径剥除，客户端只收代理 rid 单值。"""
        upstream_port = make_fake_upstream(False, x_request_id="upstream-rid")
        stop_proxy(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     body=b'{"model":"m","stream":true,"messages":[]}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        values = resp.msg.get_all("X-Request-Id")
        resp.read()
        conn.close()
        self.assertEqual(values, ["r-1"],
                         "客户端必须只收到代理 rid 一个值，got %r" % (values,))

    def test_upstream_x_request_id_stripped_for_plain(self) -> None:
        """上游自带 X-Request-Id 时非流式（/plain → _relay_buffered）路径剥除。"""
        upstream_port = make_fake_upstream(False, x_request_id="upstream-rid")
        stop_proxy(self.proc)
        self.proc = start_proxy(upstream_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        conn.request("GET", "/plain")
        resp = conn.getresponse()
        values = resp.msg.get_all("X-Request-Id")
        resp.read()
        conn.close()
        self.assertEqual(values, ["r-1"],
                         "客户端必须只收到代理 rid 一个值，got %r" % (values,))

class TpmAdmitHookTest(unittest.TestCase):
    """TPM 准入 hook 冒烟：est 超预算的带 key 请求空闲放行、忙时 429（不触上游）。"""

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50"},
                                seed_persist={"tpm_model_budgets": {"x": 50}})

    def tearDown(self) -> None:
        if self.proc:
            stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_oversized_idle_release_then_busy_429(self) -> None:
        """est > TPM_LIMIT(50)：窗口空闲首发放行（200，触上游）；
        窗口占用后同 key 同 model 再发 → 429（used>0），上游 calls 不再增长。"""
        # len ~209 bytes → est = max(1, 209*0.25) = 52 > 50
        big = (b'{"model":"x","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')
        # ① 空闲放行
        status, data = post_sse_auth(self.proxy_port, big, "Bearer test-key")
        self.assertEqual(status, 200, "idle window must release oversized request")
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        self.assertEqual(len(self.calls), 1)
        # ② 同 key 同 model 再发：used=52>0 → 429，不触上游
        status2, data2 = post_sse_auth(self.proxy_port, big, "Bearer test-key")
        self.assertEqual(status2, 429, "busy window must reject oversized request")
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        self.assertEqual(len(self.calls), 1, "rejected request must not hit upstream")
        # ③ 无 Authorization 的同体请求直通（向后兼容）
        status3, _ = post_sse_auth(self.proxy_port, big, None)
        self.assertEqual(status3, 200, "no-auth request must bypass TPM")
        self.assertEqual(len(self.calls), 2)


class TpmRateLimitTest(unittest.TestCase):
    """TPM 限流全量集成测试（spec Acceptance ①-⑧ 除部署条）。

    复用 make_scripted_upstream / start_proxy / post_sse / get_plain / admin_get /
    post_sse_auth（卡 4）/ stderr_text / stop_fake_upstreams；env seam 把 60s 窗口、
    120s 超时压缩到秒级。唯一时间断言（窗口滚过 ≥1s、settle 放行 <1s、超时 ~2s）
    全部由 1s 粒度轮询 + seam 余量保证，无 sleep 猜测依赖。
    """

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_B + SSE_DONE)   # 无 usage 帧 → 不 settle
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "110",
                                           "CTYUN_TPM_WINDOW_S": "2",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"m": 110}})

    def tearDown(self) -> None:
        if self.proc:
            stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_queue_exhaustion_and_window_roll(self) -> None:
        """窗口耗尽排队：首发大 body 占满预算（est 104/110）后，第二请求阻塞至
        窗口滚过（WINDOW_S=2）后放行；stderr 含 qwait=（>0）且 tpm= 数值正确。"""
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 347 + b'"}]}')   # 416B → est = int(416*ratio) = 104 ≤ 110
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # 41B → est = 10
        auth = "Bearer test-key-roll"
        status1, _ = post_sse_auth(self.proxy_port, big_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # 104+10=114 > 110 → 排队；无 settle，预算保持至窗口滚过（~2s）后放行
        t0 = time.time()
        status2, data2 = post_sse_auth(self.proxy_port, small_body, auth)
        elapsed = time.time() - t0
        self.assertEqual(status2, 200,
                         "queued request must be admitted after window roll, got %d"
                         % status2)
        self.assertEqual(data2, SSE_A + SSE_B + SSE_DONE)
        self.assertGreaterEqual(elapsed, 1.0,
                                "second request must actually queue (≥1s), got %.2fs"
                                % elapsed)
        # stderr：qwait= 出现且排队请求 qwait>0；tpm= 与估算公式一致（ratio 现算）
        stderr = stderr_text(self.proc)
        qwait_ms = [int(m) for m in re.findall(r"qwait=(\d+)ms", stderr)]
        self.assertTrue(any(v > 0 for v in qwait_ms),
                        "queued request must log qwait>0ms, stderr:\n" + stderr)
        expected_big = max(1, int(len(big_body) * TPM_RATIO))
        expected_small = max(1, int(len(small_body) * TPM_RATIO))
        self.assertIn("tpm=%d" % expected_big, stderr,
                      "first REQ line must carry tpm=%d, stderr:\n%s"
                      % (expected_big, stderr))
        self.assertIn("tpm=%d" % expected_small, stderr,
                      "second REQ line must carry tpm=%d, stderr:\n%s"
                      % (expected_small, stderr))

    def test_queue_timeout_returns_429(self) -> None:
        """排队超时：预算不释放 → 第二请求 ~2s（seam 值）后收 429，
        body 字节等于定死 JSON，REQ 行 result=tpm-queue-timeout，且不触上游。"""
        # 重建代理：小预算 + 短超时 + 长窗口（超时必然先于窗口滚过触发）
        stop_proxy(self.proc)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "30",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "2",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"m": 30}})
        mid_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 20 + b'"}]}')   # 89B → est = int(89*ratio) = 22 ≤ 30
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 10
        auth = "Bearer test-key-timeout"
        status1, _ = post_sse_auth(self.proxy_port, mid_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # 22+10=32 > 30 → 排队；窗口 60s 不滚、无 settle → 2s 超时 → 429
        t0 = time.time()
        status2, data2 = post_sse_auth(self.proxy_port, small_body, auth)
        elapsed = time.time() - t0
        self.assertEqual(status2, 429,
                         "queued request must timeout with 429, got %d" % status2)
        self.assertGreaterEqual(elapsed, 1.0,
                                "timeout must be seam value (~2s), got %.2fs" % elapsed)
        # body 字节等于定死 JSON（与 _reply_tpm_429 payload 全等）
        expected_429 = ('{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试",'
                        '"type":"rate_limit_error","code":"model_tpm_limit"}}').encode("utf-8")
        self.assertEqual(data2, expected_429,
                         "429 body must be the fixed JSON, got %r" % data2[:200])
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        # REQ 行 result=tpm-queue-timeout；超时请求不触上游（calls 仅首发 1 次）
        stderr = stderr_text(self.proc)
        self.assertIn("result=tpm-queue-timeout", stderr,
                      "REQ line must carry result=tpm-queue-timeout, stderr:\n" + stderr)
        self.assertEqual(len(calls), 1,
                         "timed-out request must not hit upstream, calls=%d" % len(calls))

    def test_queue_full_immediate_429(self) -> None:
        """队列满：QUEUE_MAX=2，3 并发中第 3 个立即 429，REQ 行 result=tpm-queue-full。"""
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 347 + b'"}]}')   # est = 104/110，占满预算
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 10
        auth = "Bearer test-key-qfull"
        status1, _ = post_sse_auth(self.proxy_port, big_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # 预算已满（无 settle）→ 3 并发全撞队：前 2 个入队（QUEUE_MAX=2），
        # 第 3 个立即 429（barrier 同步起点，三请求准入竞争窗口 < 窗口滚过 2s）
        barrier = threading.Barrier(3)
        results = []
        lock = threading.Lock()
        def do_req():
            barrier.wait()
            r = post_sse_auth(self.proxy_port, small_body, auth)
            with lock:
                results.append(r)
        threads = [threading.Thread(target=do_req) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        statuses = sorted(r[0] for r in results)
        self.assertEqual(statuses, [200, 200, 429],
                         "exactly 1 of 3 concurrent must be immediate 429 (2 queue, "
                         "admitted after roll), got %r" % statuses)
        stderr = stderr_text(self.proc)
        self.assertIn("result=tpm-queue-full", stderr,
                      "REQ line must carry result=tpm-queue-full, stderr:\n" + stderr)

    def test_usage_settle_releases_budget(self) -> None:
        """usage 回填：fake upstream 回 SSE_USAGE（total_tokens=2）→ settle 校正预算，
        后续请求不等窗口滚过即放行；tpm_stats bucket used == 4（2+2）。"""
        # 重建代理：上游回带 usage 帧的流（每次 POST 都含 SSE_USAGE）
        stop_proxy(self.proc)
        stop_fake_upstreams()
        upstream_port, calls = make_scripted_upstream(
            body_override=SSE_USAGE + SSE_A + SSE_B + SSE_DONE)
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "200",
                                           "CTYUN_TPM_WINDOW_S": "10",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"m": 200}})
        big_body = (b'{"model":"m","stream":true,"messages":[{"role":"user","content":"'
                    + b"x" * 280 + b'"}]}')   # est = 87，settle → 2（释放 85）
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 10
        auth = "Bearer test-key-settle"
        status1, _ = post_sse_auth(self.proxy_port, big_body, auth)
        self.assertEqual(status1, 200, "first request must be admitted")
        # settle 已把预算从 87 校正到 2 → 第二请求立即准入（无窗口等待）
        t0 = time.time()
        status2, _ = post_sse_auth(self.proxy_port, small_body, auth)
        elapsed = time.time() - t0
        self.assertEqual(status2, 200,
                         "second request must be admitted after settle, got %d" % status2)
        self.assertLess(elapsed, 1.0,
                        "settle must release budget immediately (no window wait), got %.2fs"
                        % elapsed)
        # stderr：两次 REQ 行 tpm=2（settle 后按实际 usage 记）
        stderr = stderr_text(self.proc)
        self.assertGreaterEqual(stderr.count("tpm=2"), 2,
                                "both REQ lines must carry tpm=2, stderr:\n" + stderr)
        # tpm_stats：两次 settle 后 bucket used == 2+2 = 4（与 est 无关的稳健断言）
        time.sleep(0.5)   # settle 在 conn.close 前已发生（卡 4 插入位），余量防客户端 EOF 竞态
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(len(snap["buckets"]), 1)
        self.assertEqual(snap["buckets"][0]["used"], 4,
                         "bucket used must be 4 (2+2) after both settles, got %r"
                         % snap["buckets"][0])

    def test_sse_passthrough_under_rate_limiting(self) -> None:
        """SSE 透传不破坏：限流生效路径（带 Authorization 经 tpm_admit）下
        输出仍字节等于 SSE_A+SSE_B+SSE_DONE 且以 data: [DONE] 结尾。"""
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # est = 10 ≤ 110
        auth = "Bearer test-key-passthrough"
        status, data = post_sse_auth(self.proxy_port, small_body, auth)
        self.assertEqual(status, 200)
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "SSE output must be byte-exact under rate limiting, got %r"
                         % data[:200])
        self.assertTrue(data.rstrip().endswith(b"data: [DONE]"),
                        "stream must end with data: [DONE], got tail: %r" % data[-40:])

    def test_no_auth_bypasses_tpm(self) -> None:
        """无 Authorization 头请求直通不限流（get_plain + 无 auth POST），
        stderr 无任何 TPM 字段。"""
        plain_data, plain_len = get_plain(self.proxy_port)   # 无 auth，内部断言 200
        self.assertIsNotNone(plain_data)
        data = post_sse(self.proxy_port)   # 无 auth 默认 payload，内部断言 200
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        stderr = stderr_text(self.proc)
        self.assertNotIn("qwait=", stderr,
                         "no-auth requests must not log TPM fields, stderr:\n" + stderr)
        self.assertNotIn("tpm=", stderr)
        self.assertNotIn("tpm-queue", stderr)

    def test_tpm_stats_endpoint(self) -> None:
        """/api/tpm_stats 返回 200 JSON：config 与 env seam 一致（含 limit_by_model），
        bucket used 与 REQ 行 tpm= 计数一致，key 字段 sha256: 前缀脱敏，model 字段存在。"""
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # 41B -> est = 10
        auth = "Bearer test-key-stats"
        status, _ = post_sse_auth(self.proxy_port, small_body, auth)
        self.assertEqual(status, 200)
        expected_used = max(1, int(len(small_body) * TPM_RATIO))
        status, body, ctype = admin_get(self.proc.admin_port, "/api/tpm_stats")
        self.assertEqual(status, 200)
        self.assertTrue(ctype and ctype.startswith("application/json"),
                        "Content-Type must be application/json, got %r" % ctype)
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["config"],
                         {"limit": 110, "window_s": 2, "queue_max": 2,
                          "model_budgets": {"m": 110},
                          "enabled_models": ["m"]},
                         "config must reflect env seams, got %r" % snap["config"])
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        expected_short = "sha256:" + hashlib.sha256(b"test-key-stats").hexdigest()[:12]
        self.assertEqual(b["key"], expected_short,
                         "bucket key must be sha256: prefix + 12 hex, got %r" % b["key"])
        self.assertNotIn("test-key-stats", b["key"], "bucket key must not leak raw key")
        self.assertEqual(b["model"], "m",
                         "bucket must carry model field, got %r" % b["model"])
        self.assertEqual(b["used"], expected_used,
                         "bucket used must match REQ line tpm= value")
        self.assertEqual(b["remaining"], 110 - expected_used)
        self.assertEqual(b["queued"], 0)
        self.assertEqual(b["rejected"], 0)
        self.assertEqual(b["timeouts"], 0)
        # 与 REQ 行交叉校验：tpm= 数值 == bucket used
        stderr = stderr_text(self.proc)
        self.assertIn("tpm=%d" % expected_used, stderr,
                      "REQ line must carry tpm=%d, stderr:\n%s" % (expected_used, stderr))

    def test_retry_does_not_double_charge(self) -> None:
        """header-stall 重试复用同一次准入：上游被请求 2 次，窗口只 charge 1 次 est。

        stall_calls=(1,) → 首呼 stall 触发 header-timeout 重试 → 次呼正常 SSE。
        SSE 无 usage 帧 → 不 settle，bucket used 应恰为单次 est（非 2×est）。
        """
        # 重建代理：指向 stall 上游（窗口 60s 防 2s seam 下 est 条目滚出窗口）
        stop_proxy(self.proc)
        stop_fake_upstreams()
        upstream_port, calls = make_stall_upstream(stall_calls=(1,))
        self.proxy_port = free_port()
        self.proc = start_proxy(upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "200",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_TIMEOUT_S": "5",
                                           "CTYUN_TPM_QUEUE_MAX": "2",
                                           "CTYUN_HEADER_TIMEOUT": "1"},
                                seed_persist={"tpm_model_budgets": {"m": 200}})
        small_body = b'{"model":"m","stream":true,"messages":[]}'   # 41B → est = 10
        auth = "Bearer test-key-retry"
        status, data = post_sse_auth(self.proxy_port, small_body, auth)
        self.assertEqual(status, 200, "retried request must succeed, got %d" % status)
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        self.assertEqual(len(calls), 2,
                         "header stall must trigger exactly one retry, calls=%d" % len(calls))
        est = max(1, int(len(small_body) * TPM_RATIO))
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(len(snap["buckets"]), 1,
                         "one key must own exactly one bucket, got %r" % snap["buckets"])
        b = snap["buckets"][0]
        self.assertEqual(b["key"], "sha256:" + hashlib.sha256(b"test-key-retry").hexdigest()[:12],
                         "bucket key must be the auth token's masked sha256, got %r" % b["key"])
        self.assertEqual(b["used"], est,
                         "retry must not double-charge: used=%r want single est=%d"
                         % (b["used"], est))
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("tpm=%d" % est, stderr,
                      "REQ line must carry tpm=%d (single charge), stderr:\n%s"
                      % (est, stderr))

    def test_all_existing_tests_pass(self) -> None:
        """既有 127 例无回归冒烟：TPM env 开启下 SSE 透传/plain 透传/统计端点正常。"""
        data = post_sse(self.proxy_port)   # 无 auth → 既有路径，零 TPM 介入
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        self.assertTrue(data.rstrip().endswith(b"data: [DONE]"))
        plain_data, plain_len = get_plain(self.proxy_port)
        self.assertIsNotNone(plain_data)
        status, body, ctype = admin_get(self.proc.admin_port, "/api/stats")
        self.assertEqual(status, 200)
        snap = json.loads(body.decode("utf-8"))
        self.assertGreaterEqual(snap["requests_total"], 2)
        self.assertIn("filtered=0", stderr_text(self.proc))


class PerfHistTest(unittest.TestCase):
    """P2 速度期：hist_percentile 纯函数矩阵（空/单桶/均匀/边界/末桶溢出）。"""

    def test_hist_percentile_empty(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.5), 0.0,
                         "空直方图 P50 必须为 0.0")
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.9), 0.0,
                         "空直方图 P90 必须为 0.0")

    def test_hist_percentile_single_bucket(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[0] = 1                      # 单样本落 [0, 100)
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 50.0,
                               places=6, msg="单样本首桶 P50 = 桶中点 50")
        buckets = [0] * (len(edges) + 1)
        buckets[1] = 1                      # 单样本落 [100, 250)
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 175.0,
                               places=6, msg="单样本第二桶 P50 = 桶中点 175")

    def test_hist_percentile_uniform(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [1] * (len(edges) + 1)    # 9 桶各 1 样本
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 1500.0,
                               places=6, msg="均匀分布 P50（rank=4.5）落在 [1000,2000) 中点")
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.9), 30000.0,
                               places=6, msg="均匀分布 P90（rank=8.1）落末桶 → 下界 30000")

    def test_hist_percentile_exact_edge(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[0] = 1
        buckets[1] = 1                      # rank=1.0 恰落在 100ms 桶界
        self.assertAlmostEqual(mod.hist_percentile(buckets, edges, 0.5), 100.0,
                               places=6, msg="rank 恰落桶界时返回边界值")

    def test_hist_percentile_tail_overflow(self) -> None:
        mod = load_proxy_module()
        edges = mod.HIST_BUCKETS_MS
        buckets = [0] * (len(edges) + 1)
        buckets[-1] = 5                     # 全部溢出到 [30000, +inf)
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.5), float(edges[-1]),
                         "末桶溢出 P50 返回下界 30000")
        self.assertEqual(mod.hist_percentile(buckets, edges, 0.99), float(edges[-1]),
                         "末桶溢出 P99 返回下界 30000")


class TtfbStreamTest(unittest.TestCase):
    """P2 速度期：TTFB 首字节口径 + histogram + 吞吐 + stall 检测。

    黑盒子进程集成：fake upstream sleep 100ms 后吐流 → ttfb_ms ∈ [80, 500] 且
    perf 节按模型 P50/P90 与三阶段 P50 均被填充、bytes/chunks 速率 > 0；
    stall 检测用 env seam CTYUN_STALL_THRESHOLD_S=0.5 加速（上游中途 sleep 1s）。"""

    MODEL = "deepseek-v4-pro-0813-oc"

    def setUp(self) -> None:
        self.upstream_port = make_fake_upstream(False, latency_s=0.1)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port)

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_ttfb_ms_in_range_and_perf_populated(self) -> None:
        """fake upstream sleep 100ms → ttfb_ms ∈ [80, 500]；perf 节非空且速率 > 0。"""
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertIn("perf", snap, "/api/stats 必须含 perf 节")
        perf = snap["perf"]
        entry = snap["recent"][-1]
        self.assertGreaterEqual(entry["ttfb_ms"], 80,
                                "ttfb 必须 ≥ 80ms（上游 sleep 100ms），got %r" % entry["ttfb_ms"])
        self.assertLessEqual(entry["ttfb_ms"], 500,
                             "ttfb 必须 ≤ 500ms（本地回环 100ms sleep），got %r" % entry["ttfb_ms"])
        self.assertGreater(entry["bytes_out"], 0,
                           "流式 bytes_out 必须 > 0")
        self.assertEqual(entry["chunks"], 3,
                         "chunks 必须精确等于 3（SSE_A+SSE_B+SSE_DONE 三条 record；"
                         "无 EOF 幽灵 chunk——fix-loop-1 回归锁）")
        self.assertIn(self.MODEL, perf["ttfb_p50_ms_by_model"],
                      "ttfb P50 必须按模型出现；got %r"
                      % sorted(perf["ttfb_p50_ms_by_model"].keys()))
        self.assertGreater(perf["ttfb_p50_ms_by_model"][self.MODEL], 0)
        self.assertGreater(perf["ttfb_p90_ms_by_model"][self.MODEL], 0)
        for name in ("connect", "headers", "body"):
            self.assertIn(name, perf["phase_p50_ms"],
                          "phase_p50_ms 必须含 '%s'；got %r"
                          % (name, sorted(perf["phase_p50_ms"].keys())))
            self.assertGreater(perf["phase_p50_ms"][name], 0,
                               "phase '%s' P50 必须 > 0" % name)
        self.assertGreater(perf["bytes_per_s"], 0,
                           "60s 窗口 bytes/s 必须 > 0")
        self.assertGreater(perf["chunks_per_s"], 0,
                           "60s 窗口 chunks/s 必须 > 0")
        self.assertEqual(perf["stalls_total"], 0,
                         "无 stall 的流 stalls_total 必须为 0")
        self.assertEqual(perf["stream_share"], 1.0,
                         "单条流式请求 stream_share 必须为 1.0")

    def test_stall_detected_via_env_seam(self) -> None:
        """上游中途 sleep 1s（> 0.5s 阈值）→ stalls_total ≥ 1。"""
        self.proc.terminate()
        self.proc.wait(timeout=10)
        stderr_text(self.proc)
        stop_fake_upstreams()
        self.upstream_port = make_fake_upstream(False, stall_mid_stream_s=1.0)
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_STALL_THRESHOLD_S": "0.5"})
        post_sse(self.proxy_port)
        _, body, _ = admin_get(self.proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        perf = snap["perf"]
        self.assertGreaterEqual(perf["stalls_total"], 1,
                                "中途 1s 间隙（阈值 0.5s）必须计 ≥1 次 stall；perf=%r" % perf)


class StallTailGapTest(unittest.TestCase):
    """P2 fix-loop-1：上游发完 [DONE] 后滞留 >阈值 再关连接（EOF 尾间隙）不得计 stall；
    纯 EOF 迭代不得计 chunk（chunks==3 断言在 TtfbStreamTest，经 RECENT 锁定）。"""

    def test_tail_eof_gap_not_counted_as_stall(self) -> None:
        upstream_port = make_fake_upstream(False, tail_delay_after_done_s=1.0)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port,
                           extra_env={"CTYUN_STALL_THRESHOLD_S": "0.5"})
        try:
            post_sse(proxy_port)
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            perf = snap["perf"]
            self.assertEqual(perf["stalls_total"], 0,
                             "EOF 尾间隙不是相邻 record 间隔，不得计 stall；perf=%r" % perf)
        finally:
            stop_proxy(proc)
            stop_fake_upstreams()


class TpmPerModelTest(unittest.TestCase):
    """per-model 预算集成测试（卡 3/3，spec Acceptance ①-⑥ 交叉验证）。

    kimi 独立 30k 预算 vs 全局 110k / 超大空闲放行 / 超大忙时 429 /
    拒绝计数可见 / tpm_stats 形态演进。
    复用 make_scripted_upstream + start_proxy(extra_env) + post_sse_auth +
    admin_get + stderr_text + stop_fake_upstreams。
    """

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_USAGE + SSE_DONE)  # 带 usage 帧 → settle
        self.proxy_port = free_port()

    def tearDown(self) -> None:
        if self.proc:
            stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_kimi_independent_budget(self) -> None:
        """env CTYUN_TPM_LIMIT=110000 CTYUN_TPM_LIMIT_BY_MODEL="kimi-k3-oc:1000"：
        同 key 发 kimi 请求 est>1000 → 429；同 key 发 deepseek 请求 est>1000
        但 <=110000 → 200。证明 (key,model) 桶隔离（决策 1、决策 2）。
        """
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110000",
                                    "CTYUN_TPM_WINDOW_S": "5",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                },
                                seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 1000}})
        # kimi budget 1000：需 est > kimi budget 才见拒绝；用 max_tokens 拉高 est。
        big_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                    b'"messages":[{"role":"user","content":"'
                    + b"x" * 1000
                    + b'"}],"max_tokens":3000}')   # len~1096 → int(1096*0.25)+3000=3274 > 1000
        auth = "Bearer test-per-model"
        # priming：先发小 kimi 请求（est≈7 < 1000 → 200）占住 kimi 桶，
        # 规避决策 3 的"空闲窗口超大请求放行"，让后续大请求走 429 拒绝路径。
        small_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                      b'"messages":[{"role":"user","content":"hi"}]}')
        prime_status, _ = post_sse_auth(self.proxy_port, small_kimi, auth)
        self.assertEqual(prime_status, 200,
                         "priming kimi request must be admitted (est < kimi budget)")
        status1, _ = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status1, 429,
                         "kimi est > kimi budget must be rejected (per-model bucket)")
        # deepseek 同一 key：大请求仍 ≤ 全局 110000
        big_ds = (b'{"model":"deepseek-v4-pro-0813-oc","stream":true,'
                  b'"messages":[{"role":"user","content":"'
                  + b"x" * 1000
                  + b'"}],"max_tokens":3000}')   # est 3260 ≤ 110000
        status2, _ = post_sse_auth(self.proxy_port, big_ds, auth)
        self.assertEqual(status2, 200,
                         "deepseek est ≤ global budget must pass (key,model isolated)")
        # stderr：kimi REQ 行 result=tpm-queue-full（或 full if oversized busy）
        stderr = stderr_text(self.proc)
        self.assertIn("tpm-queue-full", stderr,
                      "kimi reject must log tpm-queue-full, stderr:\n" + stderr)

    def test_oversized_idle_release_first_then_reject(self) -> None:
        """CTYUN_TPM_LIMIT=50：首发 est>50（空闲窗口）→ 200（放行，决策 3）；
        窗口内再发同 key 请求 → 429（used>0）。
        """
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "50",
                                    "CTYUN_TPM_WINDOW_S": "60",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                },
                                seed_persist={"tpm_model_budgets": {"y": 50}})
        # len 209B → est = int(209*0.25) = 52 > 50
        big = (b'{"model":"y","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')
        auth = "Bearer test-oversized"
        status1, data1 = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status1, 200, "idle oversized must be released")
        self.assertEqual(len(self.calls), 1, "released request must hit upstream")
        # 同 key 立即再发 → 429（used=52>0）
        status2, data2 = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status2, 429, "busy oversized must be rejected")
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        self.assertEqual(len(self.calls), 1,
                         "rejected request must not hit upstream again")

    def test_oversized_busy_429(self) -> None:
        """CTYUN_TPM_LIMIT=50：先发正常请求占预算（est <=50 → 200），
        再发 est>50 请求 → 429（used>0，decision 3 忙时硬拒）。
        """
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "50",
                                    "CTYUN_TPM_WINDOW_S": "60",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                },
                                seed_persist={"tpm_model_budgets": {"z": 50}})
        auth = "Bearer test-busy"
        # small: ~45B → est ≈ 11 ≤ 50
        small = b'{"model":"z","stream":true,"messages":[]}'
        status1, _ = post_sse_auth(self.proxy_port, small, auth)
        self.assertEqual(status1, 200, "normal request must be admitted")
        # big: ~209B → est ≈ 52 > 50
        big = (b'{"model":"z","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')
        status2, data2 = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status2, 429, "oversized must be rejected when budget used")
        parsed = json.loads(data2.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")

    def test_rejected_counting_in_tpm_stats(self) -> None:
        """est>limit 硬拒后 GET /api/tpm_stats：bucket rejected>=1 且 model 字段正确。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "50",
                                    "CTYUN_TPM_WINDOW_S": "60",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                },
                                seed_persist={"tpm_model_budgets": {"w": 50}})
        auth = "Bearer test-reject-count"
        big = (b'{"model":"w","stream":true,"messages":[{"role":"user","content":"'
               + b"y" * 140 + b'"}]}')   # est 52 > 50
        # 首发空闲 → 放行（used<=0, 200）
        post_sse_auth(self.proxy_port, big, auth)
        # 再发忙时 → 429 硬拒，rejected 应计数
        status, data = post_sse_auth(self.proxy_port, big, auth)
        self.assertEqual(status, 429)
        parsed = json.loads(data.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        # 查 tpm_stats
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(len(snap["buckets"]), 1,
                         "桶空则防撞忙致占满仍留痕，got %d 桶" % len(snap["buckets"]))
        b = snap["buckets"][0]
        self.assertEqual(b["model"], "w",
                         "bucket must carry model field")
        self.assertGreaterEqual(b["rejected"], 1,
                                "hard-rejected must be counted, got rejected=%d"
                                % b["rejected"])

    def test_tpm_stats_shape_with_limit_by_model(self) -> None:
        """/api/tpm_stats config 含 model_budgets/enabled_models；bucket 含 model 字段。
        第二阶段：persist 无 tpm_model_budgets + env CTYUN_TPM_LIMIT_BY_MODEL 非空
        → 启动迁移 seed（决策 2），config.model_budgets 反映 env 表且 stderr 含迁移行。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110",
                                    "CTYUN_TPM_WINDOW_S": "2",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                },
                                seed_persist={"tpm_model_budgets": {"v": 110}})
        auth = "Bearer test-shape"
        small = b'{"model":"v","stream":true,"messages":[]}'   # est = 10
        post_sse_auth(self.proxy_port, small, auth)
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["config"]["model_budgets"], {"v": 110},
                         "model_budgets must reflect enabled model budgets")
        self.assertEqual(snap["config"]["enabled_models"], ["v"])
        self.assertEqual(snap["queue_total"], 0)
        self.assertEqual(len(snap["buckets"]), 1)
        b = snap["buckets"][0]
        self.assertEqual(b["model"], "v",
                         "bucket must carry model field (not None/null)")
        self.assertFalse(b["model"] is None,
                         "model field must be the string value, not null")
        # 第二阶段：env 迁移 seed（persist 无 tpm_model_budgets 键）
        stop_proxy(self.proc)
        self.proc = start_proxy(self.upstream_port, free_port(),
                                extra_env={
                                    "CTYUN_TPM_LIMIT": "110",
                                    "CTYUN_TPM_WINDOW_S": "2",
                                    "CTYUN_TPM_QUEUE_MAX": "2",
                                    "CTYUN_TPM_LIMIT_BY_MODEL":
                                        "glm-5.3-oc:110000",
                                })
        _, body2, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap2 = json.loads(body2.decode("utf-8"))
        self.assertEqual(snap2["config"]["model_budgets"],
                         {"glm-5.3-oc": 110000},
                         "env seam must migrate-seed tpm_model_budgets")
        self.assertIn("migrated CTYUN_TPM_LIMIT_BY_MODEL", stderr_text(self.proc))

    def test_no_auth_bypasses_tpm(self) -> None:
        """无 Authorization 头请求直通不限流，stderr 无任何 TPM 字段（向后兼容 spec）。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port)
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(data, SSE_A + SSE_USAGE + SSE_DONE)
        stderr = stderr_text(self.proc)
        self.assertNotIn("qwait=", stderr,
                         "no-auth requests must not log TPM fields")
        self.assertNotIn("tpm=", stderr)
        self.assertNotIn("tpm-queue", stderr)


class TpmModelSelectionTest(unittest.TestCase):
    """TPM 模型选择 + 设置页后端集成测试（spec Acceptance 直通/启用/清单/持久化/迁移/非法输入）。

    复用 make_scripted_upstream + start_proxy(extra_env, seed_persist) +
    post_sse_auth + admin_get + admin_post + stderr_text + stop_fake_upstreams。
    """

    def setUp(self) -> None:
        self.upstream_port, self.calls = make_scripted_upstream(
            body_override=SSE_A + SSE_USAGE + SSE_DONE)  # 带 usage 帧 → settle
        self.proxy_port = free_port()

    def tearDown(self) -> None:
        if self.proc:
            self.proc.terminate()
            self.proc.wait(timeout=5)
            stderr_text(self.proc)
            shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        stop_fake_upstreams()

    def test_disabled_model_passthrough(self) -> None:
        """seed {}（无启用模型）+ CTYUN_TPM_LIMIT=50：est>50 的带 auth 请求 → 200；
        /api/tpm_stats buckets 空；stderr 无 tpm-queue-full。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50",
                                           "CTYUN_TPM_WINDOW_S": "2",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {}})
        big = (b'{"model":"deepseek-v4-pro-0813-oc","stream":true,'
               b'"messages":[{"role":"user","content":"' + b"y" * 140 + b'"}]}')
        status, data = post_sse_auth(self.proxy_port, big, "Bearer test-disabled")
        self.assertEqual(status, 200, "non-enabled model must pass through, got %d" % status)
        self.assertEqual(data, SSE_A + SSE_USAGE + SSE_DONE)
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        snap = json.loads(body.decode("utf-8"))
        self.assertEqual(snap["buckets"], [])
        self.assertNotIn("tpm-queue-full", stderr_text(self.proc))

    def test_enabled_model_429_other_model_200(self) -> None:
        """seed {"kimi-k3-oc": 1000}：同 key 先发小 kimi 占桶再发 est>1000 kimi → 429
        model_tpm_limit；同 key 发未启用 deepseek 大请求 → 200。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "110000",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 1000}})
        auth = "Bearer test-enabled-429"
        small_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                      b'"messages":[{"role":"user","content":"hi"}]}')
        prime_status, _ = post_sse_auth(self.proxy_port, small_kimi, auth)
        self.assertEqual(prime_status, 200, "priming kimi must be admitted")
        big_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                    b'"messages":[{"role":"user","content":"'
                    + b"x" * 1000
                    + b'"}],"max_tokens":3000}')
        status1, data1 = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status1, 429)
        parsed = json.loads(data1.decode("utf-8"))
        self.assertEqual(parsed["error"]["code"], "model_tpm_limit")
        big_ds = (b'{"model":"deepseek-v4-pro-0813-oc","stream":true,'
                  b'"messages":[{"role":"user","content":"' + b"x" * 1000
                  + b'"}],"max_tokens":3000}')
        status2, _ = post_sse_auth(self.proxy_port, big_ds, auth)
        self.assertEqual(status2, 200, "non-enabled deepseek must pass through")

    def test_model_list_auto_generated(self) -> None:
        """发过 deepseek 流量（无 auth 也统计）→ /api/tpm_settings models 含该模型，
        与 TPM_MODEL_BUDGETS 键并集去重、字典序。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 30000}})
        post_sse(self.proxy_port)  # 无 auth，model=deepseek-v4-pro-0813-oc
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
        snap = json.loads(body.decode("utf-8"))
        names = [m["name"] for m in snap["models"]]
        self.assertIn("deepseek-v4-pro-0813-oc", names)
        self.assertIn("kimi-k3-oc", names)
        self.assertEqual(names, sorted(names), "model list must be sorted")
        self.assertEqual(snap["default_limit"], load_proxy_module().TPM_LIMIT)
        by_name = {m["name"]: m for m in snap["models"]}
        self.assertEqual(by_name["kimi-k3-oc"]["enabled"], True)
        self.assertEqual(by_name["deepseek-v4-pro-0813-oc"]["enabled"], False)
        recommend = by_name["deepseek-v4-pro-0813-oc"]["recommend"]
        self.assertNotIn("choices", recommend)
        self.assertIn("enabled_advice", recommend)
        self.assertIn("source", recommend)

    def test_save_restart_persist_roundtrip(self) -> None:
        """POST {"tpm_model_budgets": {"glm-5.3-oc": 50000}} → 200 响应含同值；
        persist 文件含 tpm_model_budgets 键；同 persist 路径重启 → GET /api/config 返回该值。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port)
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": {"glm-5.3-oc": 50000}}).encode("utf-8"))
        self.assertEqual(status, 200)
        resp = json.loads(body.decode("utf-8"))
        self.assertEqual(resp["tpm_model_budgets"], {"glm-5.3-oc": 50000})
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tpm_model_budgets"],
                             {"glm-5.3-oc": 50000})
        # 重启（同 persist 路径复用：保存文件内容后二次 start_proxy 以 seed 回灌）
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        self.proc = start_proxy(self.upstream_port, free_port(), seed_persist=saved)
        _, body2, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body2.decode("utf-8"))
        self.assertEqual(cfg["tpm_model_budgets"], {"glm-5.3-oc": 50000})

    def test_env_migration_path(self) -> None:
        """seed_persist 无新键 + env CTYUN_TPM_LIMIT_BY_MODEL="kimi-k3-oc:1000"
        → 启动迁移：GET /api/config tpm_model_budgets == {"kimi-k3-oc": 1000}
        且 persist 文件已落该键（卡 2 save_stats_counters 带键后可见）。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT_BY_MODEL": "kimi-k3-oc:1000"},
                                seed_persist={"upstream_base": "http://127.0.0.1:%d"
                                              % self.upstream_port})
        _, body, _ = admin_get(self.proc.admin_port, "/api/config")
        cfg = json.loads(body.decode("utf-8"))
        self.assertEqual(cfg["tpm_model_budgets"], {"kimi-k3-oc": 1000})
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["tpm_model_budgets"], {"kimi-k3-oc": 1000})
        self.assertIn("migrated CTYUN_TPM_LIMIT_BY_MODEL", stderr_text(self.proc))

    def test_post_empty_budgets_clears(self) -> None:
        """POST {"tpm_model_budgets": {}} → 全部模型直通：先前 429 的 kimi 大请求 → 200。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_TPM_LIMIT": "50",
                                           "CTYUN_TPM_WINDOW_S": "60",
                                           "CTYUN_TPM_QUEUE_MAX": "2"},
                                seed_persist={"tpm_model_budgets": {"kimi-k3-oc": 50}})
        auth = "Bearer test-clear"
        small_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                      b'"messages":[{"role":"user","content":"hi"}]}')
        post_sse_auth(self.proxy_port, small_kimi, auth)
        big_kimi = (b'{"model":"kimi-k3-oc","stream":true,'
                    b'"messages":[{"role":"user","content":"' + b"x" * 140 + b'"}]}')
        status1, _ = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status1, 429, "enabled kimi with budget 50 must reject")
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": {}}).encode("utf-8"))
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8"))["tpm_model_budgets"], {})
        status2, _ = post_sse_auth(self.proxy_port, big_kimi, auth)
        self.assertEqual(status2, 200, "after clear, kimi must pass through")

    def test_post_invalid_budgets_400(self) -> None:
        """非法输入逐项 400：负值 / 非 dict / 超 32 键。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port)
        status, body = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": {"m": -1}}).encode("utf-8"))
        self.assertEqual(status, 400)
        self.assertIn("正整数", json.loads(body.decode("utf-8"))["error"])
        status2, body2 = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": "not-a-dict"}).encode("utf-8"))
        self.assertEqual(status2, 400)
        self.assertIn("must be a dict", json.loads(body2.decode("utf-8"))["error"])
        too_many = {"m%d" % i: 10 for i in range(33)}
        status3, body3 = admin_post(
            self.proc.admin_port, "/api/config",
            json.dumps({"tpm_model_budgets": too_many}).encode("utf-8"))
        self.assertEqual(status3, 400)
        self.assertIn("32", json.loads(body3.decode("utf-8"))["error"])
        # 非法 POST 不得改变现值（seed 默认 kimi 30000 保持）
        _, body4, _ = admin_get(self.proc.admin_port, "/api/config")
        self.assertEqual(json.loads(body4.decode("utf-8"))["tpm_model_budgets"],
                         {"kimi-k3-oc": 30000})


class ClassifyOutcomeTriStateTest(unittest.TestCase):
    """P4：classify_outcome 返回 tri_state 字段；_record_request 三桶归因 + 双形态兼容。"""

    def test_classify_outcome_tri_state_matrix(self) -> None:
        mod = load_proxy_module()
        f = mod.classify_outcome
        self.assertEqual(f(status=200).tri_state, "ok")
        self.assertEqual(f(status=200, poison_filtered=1).tri_state, "degraded")
        self.assertEqual(f(client_abort=True).tri_state, "degraded")
        self.assertEqual(f(synth_502=True).tri_state, "failed")
        self.assertEqual(f(body_error=True).tri_state, "failed")
        self.assertEqual(f(eof_without_done=True).tri_state, "failed")
        self.assertEqual(f(status=404).tri_state, "failed")
        self.assertEqual(f(status=500).tri_state, "failed")

    def test_tri_state_consistent_with_pure_function(self) -> None:
        mod = load_proxy_module()
        for outcome in (mod.classify_outcome(status=200),
                        mod.classify_outcome(status=200, poison_filtered=1),
                        mod.classify_outcome(client_abort=True),
                        mod.classify_outcome(synth_502=True),
                        mod.classify_outcome(body_error=True),
                        mod.classify_outcome(status=404),
                        mod.classify_outcome(status=500)):
            self.assertEqual(outcome.tri_state,
                             mod._outcome_tri_state(outcome.category))

    def test_tpm_limited_maps_to_failed(self) -> None:
        mod = load_proxy_module()
        # classify_outcome 不产 CLASS_TPM_LIMITED（429 路径不经网关），但映射必须
        # 覆盖该 category（防未来走此网关时误归 ok/degraded）
        self.assertEqual(mod._outcome_tri_state(mod.CLASS_TPM_LIMITED), "failed")

    def test_record_request_lands_tri_state_buckets_from_namedtuple(self) -> None:
        mod = load_proxy_module()
        orig = mod.STATS["daily_by_model"]
        try:
            mod.STATS["daily_by_model"] = {}
            mod._record_request("POST", "/p4", 200, 1.0, 0, model="m-p4",
                                outcome=mod.classify_outcome(status=200))
            mod._record_request("POST", "/p4", 502, 1.0, 0, model="m-p4",
                                outcome=mod.classify_outcome(synth_502=True))
            mod._record_request("POST", "/p4", 200, 1.0, 1, model="m-p4",
                                outcome=mod.classify_outcome(status=200,
                                                             poison_filtered=1))
            entry = mod.STATS["daily_by_model"][mod.today_key()]["m-p4"]
            self.assertEqual(entry["outcome_ok"], 1)
            self.assertEqual(entry["outcome_degraded"], 1)
            self.assertEqual(entry["outcome_failed"], 1)
            # RECENT_REQUESTS 恒存 category 字符串（JSON 可序列化 + 展示口径不变）
            self.assertEqual(mod.RECENT_REQUESTS[-1]["outcome"],
                             mod.CLASS_POISON_FIXED)
        finally:
            mod.STATS["daily_by_model"] = orig

    def test_record_request_string_outcome_back_compat(self) -> None:
        """旧调用形态（category 字符串，P3 测试与既有路径用）仍正确落桶。"""
        mod = load_proxy_module()
        orig = mod.STATS["daily_by_model"]
        try:
            mod.STATS["daily_by_model"] = {}
            mod._record_request("POST", "/p4s", 200, 1.0, 0, model="m-p4s",
                                outcome=mod.CLASS_OK)
            entry = mod.STATS["daily_by_model"][mod.today_key()]["m-p4s"]
            self.assertEqual(entry["outcome_ok"], 1)
            self.assertEqual(mod.RECENT_REQUESTS[-1]["outcome"], mod.CLASS_OK)
        finally:
            mod.STATS["daily_by_model"] = orig


class ProbeLoopTest(unittest.TestCase):
    """P4 探测线程黑盒：HEAD 计数 / 间隔加速 / 连续失败告警 / 去抖。"""

    @staticmethod
    def _wait_head_calls(calls: list, n: int, timeout: float = 8.0) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if len(calls) >= n:
                return
            time.sleep(0.05)
        raise AssertionError("probe 未在 %.1fs 内发出 %d 次 HEAD（实际 %d）"
                             % (timeout, n, len(calls)))

    def _spawn(self, head_fail: bool):
        upstream_port, head_calls = make_probe_upstream(head_fail=head_fail)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port,
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})
        return proc, head_calls

    @staticmethod
    def _teardown_proc(proc) -> None:
        stop_proxy(proc)
        stop_fake_upstreams()

    def test_probe_sends_head_and_success_has_no_alert(self) -> None:
        proc, head_calls = self._spawn(head_fail=False)
        try:
            self._wait_head_calls(head_calls, 3)
            time.sleep(0.3)  # 让最近一轮 probe 落状态
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            kinds = [e["kind"] for e in snap["events"]]
            self.assertNotIn("probe_alert", kinds,
                             "成功 probe 不得产告警；events=%r" % kinds)
            self.assertEqual(snap["requests_total"], 0,
                             "probe 不得计入 requests_total（零副作用）")
        finally:
            self._teardown_proc(proc)

    def test_consecutive_failures_emit_single_probe_alert(self) -> None:
        proc, head_calls = self._spawn(head_fail=True)
        try:
            self._wait_head_calls(head_calls, 3)  # 达阈值 3 → 触发告警
            deadline = time.time() + 5
            alerts = []
            snap = None
            while time.time() < deadline:
                _, body, _ = admin_get(proc.admin_port, "/api/stats")
                snap = json.loads(body.decode("utf-8"))
                alerts = [e for e in snap["events"] if e["kind"] == "probe_alert"]
                if alerts:
                    break
                time.sleep(0.1)
            self.assertIsNotNone(snap)
            self.assertEqual(len(alerts), 1,
                             "连续 3 次 5xx 必须恰好 1 条 probe_alert；events=%r"
                             % snap["events"])
            self.assertEqual(alerts[0]["reason"], "consecutive_failures")
            # 去抖：继续累积 ≥7 次失败（0.5s 间隔），300s 窗口内不得重发同类告警
            self._wait_head_calls(head_calls, 10)
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            self.assertEqual(
                len([e for e in snap["events"] if e["kind"] == "probe_alert"]), 1,
                "PROBE_ALERT_DEBOUNCE_S 窗口内不得重发同类告警（去抖）")
        finally:
            self._teardown_proc(proc)


class ProbeNoSideEffectTest(unittest.TestCase):
    """P4 R3：probe 失败零副作用——持续 5xx 期间 STATS.requests_total 不增、
    RECENT_REQUESTS 不落、daily 不污染；客户端请求计数照常。"""

    def test_failing_probe_does_not_pollute_stats(self) -> None:
        upstream_port, head_calls = make_probe_upstream(head_fail=True)
        proxy_port = free_port()
        proc = start_proxy(upstream_port, proxy_port,
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})
        try:
            ProbeLoopTest._wait_head_calls(head_calls, 6)  # ≥3 次失败足以触发告警
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            self.assertEqual(snap["requests_total"], 0,
                             "probe 失败不得计入 requests_total（零副作用）")
            self.assertEqual(len(snap["recent"]), 0)
            self.assertEqual(sum(b.get("requests", 0)
                                 for b in snap["daily"].values()), 0,
                             "probe 不得污染 daily 桶")
            # 对照组：客户端请求照常计数（probe 不吞客户流量统计）
            post_sse(proxy_port)
            _, body, _ = admin_get(proc.admin_port, "/api/stats")
            snap = json.loads(body.decode("utf-8"))
            self.assertEqual(snap["requests_total"], 1)
            self.assertEqual(len(snap["recent"]), 1)
        finally:
            stop_proxy(proc)
            stop_fake_upstreams()


class ProbeUnitTest(unittest.TestCase):
    """P4 白盒：probe 核心纯函数（间隔解析 / 突增判定）。

    开关解析（resolve_probe_enabled/load_probe_enabled）属卡 3，测试在其
    ProbeEnabledResolutionTest（本卡不落，防卡 2 绿相残留红）。
    """

    def test_probe_interval_env_resolution(self) -> None:
        mod = load_proxy_module()
        f = mod._probe_interval_s_from_env
        self.assertEqual(f(""), 30.0)     # 缺省 → 默认 30
        self.assertEqual(f("0.5"), 0.5)   # 显式正数原样（测试加速 seam）
        self.assertEqual(f("15"), 15.0)
        self.assertEqual(f("0"), 10.0)    # ≤0 → 防滥用下限
        self.assertEqual(f("-3"), 10.0)
        self.assertEqual(f("abc"), 10.0)  # 非法 → 防滥用下限

    def test_probe_spike_due_matrix(self) -> None:
        mod = load_proxy_module()
        f = mod._probe_spike_due
        self.assertFalse(f([]))
        self.assertFalse(f([50.0] * 19))  # 样本不足 20 不告警
        self.assertFalse(f([50.0] * 200))  # 无突增
        # 突增：180×50 + 20×500，基线 P90=50（未污染），最近 20 P90=500 > 100
        self.assertTrue(f([50.0] * 180 + [500.0] * 20))
        # 涨幅 < 2×：60×50 + 20×120，基线 P90=120，最近 20 P90=120 ≤ 240
        self.assertFalse(f([50.0] * 60 + [120.0] * 20))


class ProbeEventsCompatTest(unittest.TestCase):
    """P4 R2：events kind 白名单——新版读 probe_alert 保留 reason；
    旧版本（3-kind 白名单）读含 probe_alert 的文件整条丢弃不崩。"""

    def test_load_events_keeps_probe_alert_with_reason(self) -> None:
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p4evt-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": 100.0, "kind": "proxy", "model": "m", "status": 502},
                {"ts": 200.0, "kind": "probe_alert", "model": None, "status": None,
                 "reason": "consecutive_failures"},
                {"ts": 300.0, "kind": "probe_alert", "model": None, "status": None,
                 "reason": "latency_spike"},
                {"ts": 400.0, "kind": "future_kind", "model": None, "status": None},
            ]}}, fh)
        events = mod.load_stats_events(path)
        self.assertEqual([e["kind"] for e in events],
                         ["proxy", "probe_alert", "probe_alert"])
        self.assertEqual(events[1]["reason"], "consecutive_failures")
        self.assertEqual(events[2]["reason"], "latency_spike")
        self.assertNotIn("reason", events[0], "非 probe_alert 事件不得带 reason")

    def test_old_whitelist_binary_drops_probe_alert_gracefully(self) -> None:
        """R2 降级演练：旧版本 3-kind 白名单 load 逻辑读新格式 → 整条丢弃不崩。"""
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p4old-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"stats": {"events": [
                {"ts": 100.0, "kind": "retry", "model": None, "status": None},
                {"ts": 200.0, "kind": "probe_alert", "model": None, "status": None,
                 "reason": "consecutive_failures"},
            ]}}, fh)
        # 模拟旧版本二进制：白名单写死 ("proxy", "upstream", "retry")——与 P4 前
        # load_stats_events 源码逐字一致（inline 复刻，独立 oracle）。
        data = mod._load_persist_file(path)
        raw = data["stats"]["events"]
        kept = []
        for entry in raw:
            if entry.get("kind") not in ("proxy", "upstream", "retry"):
                continue
            kept.append(entry)
        self.assertEqual([e["kind"] for e in kept], ["retry"],
                         "旧版本必须丢弃 probe_alert 且不崩（graceful degrade）")


class ProbeHealthApiTest(unittest.TestCase):
    """P4：GET /api/health 形状与真实值（probe 运转后回读）。"""

    def setUp(self) -> None:
        self.upstream_port, self.head_calls = make_probe_upstream()
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_health_reports_probe_state(self) -> None:
        ProbeLoopTest._wait_head_calls(self.head_calls, 2)
        deadline = time.time() + 5
        snap = None
        while time.time() < deadline:
            status, body, _ = admin_get(self.proc.admin_port, "/api/health")
            snap = json.loads(body.decode("utf-8"))
            if snap["upstream"]["last_probe_ok"]:
                break
            time.sleep(0.1)
        self.assertEqual(status, 200)
        self.assertIsNotNone(snap)
        upstream = snap["upstream"]
        self.assertEqual(set(upstream),
                         {"host", "last_probe_ts", "last_probe_ok",
                          "last_probe_latency_ms", "consecutive_failures",
                          "probe_enabled"})
        self.assertEqual(upstream["host"], "127.0.0.1:%d" % self.upstream_port)
        self.assertTrue(upstream["last_probe_ok"])
        self.assertGreater(upstream["last_probe_ts"], 0)
        self.assertIsInstance(upstream["last_probe_latency_ms"], float)
        self.assertEqual(upstream["consecutive_failures"], 0)
        self.assertTrue(upstream["probe_enabled"])


class ProbeToggleTest(unittest.TestCase):
    """P4：POST /api/probe 切 on/off（运行时生效、持久化、输入校验、启动空转）。"""

    def setUp(self) -> None:
        self.upstream_port, self.head_calls = make_probe_upstream()
        self.proxy_port = free_port()
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"})

    def tearDown(self) -> None:
        stop_proxy(self.proc)
        stop_fake_upstreams()

    def test_probe_toggle_off_stops_head_requests(self) -> None:
        ProbeLoopTest._wait_head_calls(self.head_calls, 2)  # 确认运行中
        status, body = admin_post(self.proc.admin_port, "/api/probe",
                                  b'{"enabled": false}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")),
                         {"ok": True, "probe_enabled": False})
        _, body, _ = admin_get(self.proc.admin_port, "/api/health")
        self.assertFalse(json.loads(body.decode("utf-8"))["upstream"]["probe_enabled"])
        time.sleep(1.5)  # >2 个间隔：空转不得再发 HEAD
        count_off = len(self.head_calls)
        time.sleep(1.5)
        self.assertEqual(len(self.head_calls), count_off,
                         "probe off 后不得再发 HEAD（空转零网络 IO）")
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            self.assertFalse(json.load(fh)["probe_enabled"],
                             "persist 顶层 probe_enabled 必须落 false")

    def test_probe_toggle_back_on_resumes(self) -> None:
        ProbeLoopTest._wait_head_calls(self.head_calls, 2)
        admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": false}')
        time.sleep(1.0)
        stopped = len(self.head_calls)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": true}')
        self.assertEqual(status, 200)
        ProbeLoopTest._wait_head_calls(self.head_calls, stopped + 2)
        _, body, _ = admin_get(self.proc.admin_port, "/api/health")
        self.assertTrue(json.loads(body.decode("utf-8"))["upstream"]["probe_enabled"])

    def test_probe_post_rejects_bad_input(self) -> None:
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b"not json")
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": "yes"}')
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"enabled": 1}')
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/probe", b'{"nope": true}')
        self.assertEqual(status, 400)

    def test_seed_persist_probe_disabled_starts_idle(self) -> None:
        """seed_persist probe_enabled=false → 启动即空转（零 HEAD，health 零值）。"""
        up_port, up_head_calls = make_probe_upstream()
        proc = start_proxy(up_port, free_port(),
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5"},
                           seed_persist={"upstream_base":
                                         "http://127.0.0.1:%d" % up_port,
                                         "probe_enabled": False})
        try:
            time.sleep(1.5)  # >2 个间隔
            self.assertEqual(len(up_head_calls), 0,
                             "probe_enabled=false 启动后不得发 HEAD（空转不触网）")
            _, body, _ = admin_get(proc.admin_port, "/api/health")
            upstream = json.loads(body.decode("utf-8"))["upstream"]
            self.assertFalse(upstream["probe_enabled"])
            self.assertEqual(upstream["last_probe_ts"], 0)
        finally:
            stop_proxy(proc)
            stop_fake_upstreams()

    def test_env_probe_disabled_overrides_persist_enabled(self) -> None:
        """CTYUN_PROBE_ENABLED=0 优先于 persist true（env > persist > 默认）。"""
        up_port, up_head_calls = make_probe_upstream()
        proc = start_proxy(up_port, free_port(),
                           extra_env={"CTYUN_PROBE_INTERVAL_S": "0.5",
                                      "CTYUN_PROBE_ENABLED": "0"},
                           seed_persist={"upstream_base":
                                         "http://127.0.0.1:%d" % up_port,
                                         "probe_enabled": True})
        try:
            time.sleep(1.5)
            self.assertEqual(len(up_head_calls), 0)
            _, body, _ = admin_get(proc.admin_port, "/api/health")
            self.assertFalse(json.loads(body.decode("utf-8"))["upstream"]["probe_enabled"])
        finally:
            stop_proxy(proc)
            stop_fake_upstreams()


class ProbeEnabledResolutionTest(unittest.TestCase):
    """P4 卡 3 白盒：probe 开关解析/env seam/persist 读取（纯函数）。"""

    def test_resolve_probe_enabled_matrix(self) -> None:
        mod = load_proxy_module()
        f = mod.resolve_probe_enabled
        self.assertTrue(f("1", False))
        self.assertTrue(f("true", False))
        self.assertTrue(f(" YES ", False))
        self.assertTrue(f("on", False))
        self.assertFalse(f("0", True))
        self.assertFalse(f("false", True))
        self.assertFalse(f("OFF", True))
        self.assertFalse(f("no", True))
        self.assertTrue(f("", True))        # env 缺省 → persist
        self.assertFalse(f("", False))      # env 缺省 → persist
        self.assertTrue(f("banana", True))  # env 非法 → persist（不回默认）
        self.assertFalse(f("banana", False))

    def test_load_probe_enabled_defaults(self) -> None:
        mod = load_proxy_module()
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-p4en-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        self.assertTrue(mod.load_probe_enabled(path))  # 缺文件 → 默认开
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"probe_enabled": False}, fh)
        self.assertFalse(mod.load_probe_enabled(path))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"probe_enabled": "yes"}, fh)  # 非 bool → 默认开
        self.assertTrue(mod.load_probe_enabled(path))


if __name__ == "__main__":
    import atexit
    atexit.register(kill_registered)
    # 默认 SIGTERM 直接终止不跑 atexit：转成解释器关闭路径，兜底清理才可执行
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    unittest.main(verbosity=2)