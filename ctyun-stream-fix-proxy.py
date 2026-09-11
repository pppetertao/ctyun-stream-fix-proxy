#!/usr/bin/env python3
"""ctyun-stream-fix-proxy — 本地阻塞式反代，剥除天翼云网关 SSE 流中的非法 data:null 行。

数据流: SSE 客户端 -> http://127.0.0.1:7920 (本代理, 剥行) -> UPSTREAM_BASE (原 path prepend)。
旁路:   http://<ADMIN_HOST>:7921 (admin/dashboard：只读统计 + 受限写上游端点)。
纯 stdlib（http.server + http.client + re），全阻塞，Python >= 3.9。
生产默认: UPSTREAM_BASE=https://eaichat.ctyun.cn/ai/platform/v2/cp, 端口 7920;
CTYUN_UPSTREAM_BASE / CTYUN_LISTEN_PORT / CTYUN_ADMIN_HOST / CTYUN_ADMIN_PORT /
CTYUN_PERSIST_PATH / CTYUN_ADMIN_TOKEN 环境变量为测试与部署 seam。
"""

import base64
import collections
import hmac
import http.client
import http.server
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import urllib.parse

DEFAULT_UPSTREAM_BASE = "https://eaichat.ctyun.cn/ai/platform/v2/cp"
UPSTREAM_BASE = DEFAULT_UPSTREAM_BASE  # 运行时可变：main() 启动解析 / POST /api/config 热切换
_upstream_source = "default"           # "env" | "file" | "default" | "api"
LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = int(os.environ.get("CTYUN_LISTEN_PORT", "7920"))
ADMIN_HOST = os.environ.get("CTYUN_ADMIN_HOST", "0.0.0.0")
ADMIN_PORT = int(os.environ.get("CTYUN_ADMIN_PORT", "7921"))
PERSIST_PATH = os.environ.get(
    "CTYUN_PERSIST_PATH",
    os.path.expanduser("~/.local/etc/ctyun-stream-fix-proxy.json"))
UPSTREAM_TIMEOUT = 600
# 客户端 send 超时：客户端优雅关闭(FIN)后代理的 send 会无限期阻塞（实测 sample 卡
# __sendto），把线程永久钉死；健康读者不会让 send 阻塞超过一次缓冲排空，阻塞到此
# 阈值即视为对端已死。env 仅作测试 seam（测试用 1s 加速）。
SEND_TIMEOUT_S = float(os.environ.get("CTYUN_SEND_TIMEOUT", "60"))
POST_BODY_LIMIT = 8192
FLUSH_INTERVAL_S = 60
BY_MODEL_CAP = 32
DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）
POISON_RE = re.compile(rb"^data:\s*null\s*$")
DONE_RE = re.compile(rb"^data:\s*\[DONE\]\s*$")
EMPTY_RETRY_MAX = int(os.environ.get("CTYUN_EMPTY_RETRY", "1"))  # env seam，惯例同 SEND_TIMEOUT_S


def sse_data_line_kind(line: bytes) -> str:
    """SSE data 行归类："done" / "content" / "noise"。判定序：
    非 data: 前缀（注释行/event:/id:）→ noise；[DONE] → done；
    JSON 解析失败 → content（fail-open：宁可不重试，不误判合法流）；
    dict + choices 非空 list 时：delta.content 非空 str / delta.tool_calls 真值 /
    choice.finish_reason 非 None 任一 → content；其余（reasoning-only、空 delta、
    choices 为空的 usage 帧、data:null、非 dict JSON）→ noise。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:"):
        return "noise"
    if DONE_RE.match(stripped):
        return "done"
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return "content"
    if not isinstance(data, dict):
        return "noise"
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "noise"
    choice = choices[0] if isinstance(choices[0], dict) else {}
    delta = choice.get("delta")
    delta = delta if isinstance(delta, dict) else {}
    content = delta.get("content")
    if isinstance(content, str) and content:
        return "content"
    if delta.get("tool_calls"):
        return "content"
    if choice.get("finish_reason") is not None:
        return "content"
    return "noise"


class _EmptyStream(Exception):
    """priming EOF 仍无 content/[DONE]：携带 filtered 计数与已滤毒缓冲行。"""
    def __init__(self, filtered: int, lines: list):
        super().__init__("empty upstream sse stream")
        self.filtered = filtered
        self.lines = lines


HOP_HEADERS = {"connection", "keep-alive", "proxy-connection",
               "te", "trailer", "transfer-encoding", "upgrade"}
STRIP_HEADERS = HOP_HEADERS | {"host", "content-length", "accept-encoding"}

_CFG_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {}}
_stats_dirty = False  # STATS_LOCK 保护：计数落盘脏标记（SIGTERM/60s 脏刷消费）
STARTED_AT = time.time()
RECENT_REQUESTS = collections.deque(maxlen=100)  # {"ts","method","path","status","dur_ms","filtered","model"}
POISON_PREVIEWS = collections.deque(maxlen=20)   # {"ts","preview"} 最近剥除的 record 预览


def valid_upstream_url(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        # 吞掉的是 urlsplit 对畸形括号 IPv6 抛的 ValueError：本函数语义即"合法 True /
        # 其余 False"，非法输入按校验不过处理，无其他路径可达。
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def resolve_upstream_base(env_base: str, persist_path: str) -> tuple:
    """上游端点解析，优先级 env > 持久化文件 > 内置默认；返回 (base, source)。"""
    if env_base:
        return env_base, "env"
    try:
        with open(persist_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # 吞掉的是持久化文件缺失（OSError，首次运行常态）与损坏 JSON（ValueError）：
        # 该文件是"网页上次设置"的缓存，缺失/损坏回落 default 即可，无其他路径可达。
        pass
    else:
        base = data.get("upstream_base") if isinstance(data, dict) else None
        if isinstance(base, str) and valid_upstream_url(base):
            return base, "file"
    return DEFAULT_UPSTREAM_BASE, "default"


def persist_upstream(base: str, path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base}, fh, ensure_ascii=False)
    os.replace(tmp, path)  # 同目录原子替换，读侧不会见到半截文件


def extract_model(body):
    """从请求体 JSON 提取 model 字段（仅统计展示用）；非 JSON/缺失/非字符串 → None。"""
    if not body:
        return None
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        # 吞掉的是非 JSON 请求体（GET/表单/二进制属常态）：统计字段 best-effort，
        # 解析失败按无 model 处理即可，无其他路径可达。
        return None
    model = data.get("model") if isinstance(data, dict) else None
    return model if isinstance(model, str) and model else None


def today_key() -> str:
    """当日日期桶 key（本地时区）；模块级函数便于测试 patch 模拟跨天。"""
    return time.strftime("%Y-%m-%d", time.localtime())


def _prune_daily(daily: dict) -> dict:
    """按日期 key 降序保留最近 DAILY_RETENTION_DAYS 天（ISO 日期字符串排序即时间序）。"""
    if len(daily) <= DAILY_RETENTION_DAYS:
        return daily
    for key in sorted(daily)[:-DAILY_RETENTION_DAYS]:
        del daily[key]
    return daily


def _safe_log_stderr(msg: str) -> None:
    # 吞掉的是 stderr 写失败的一切 OSError（BrokenPipeError=管道对端关闭、ENOSPC=磁盘满等）：
    # stderr 是 best-effort 诊断出口，无备用通道，且失败不得传播——传播会炸请求线程，
    # 并使 SIGTERM handler 的 except 分支二次抛出、阻断 SystemExit 退出路径。
    try:
        print(msg, file=sys.stderr, flush=True)
    except OSError:
        pass


def save_stats_counters(path: str) -> None:
    """把累计计数、按天分桶与当前上游端点全量写入持久化文件（SIGTERM / set_upstream_base 共用）。"""
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total")}
        daily = {k: dict(v) for k, v in STATS["daily"].items()}
    _prune_daily(daily)
    with _CFG_LOCK:
        base = UPSTREAM_BASE
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base,
                   "stats": dict(counters, daily=daily)}, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _load_persist_file(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        # 吞掉的是持久化文件缺失（OSError，首次运行常态）与损坏 JSON（ValueError）：
        # 该文件是"网页上次设置 + 计数缓存"，损坏等同首次运行回落零值，无其他路径可达。
        return {}
    return data if isinstance(data, dict) else {}


def load_stats_counters(path: str) -> dict:
    """从持久化文件读累计计数；缺文件/损坏/legacy 无 stats 键 → 各键零值。"""
    stats = _load_persist_file(path).get("stats")
    out = {}
    for key in ("requests_total", "filtered_total", "errors_total", "empty_retries_total"):
        value = stats.get(key) if isinstance(stats, dict) else None
        out[key] = value if isinstance(value, int) and value >= 0 else 0
    return out


_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream", "retries")


def load_daily_buckets(path: str) -> dict:
    """从持久化文件读按天分桶；缺/损坏/非法结构 → {}，逐桶容错（手工改坏文件不崩）。"""
    stats = _load_persist_file(path).get("stats")
    raw = stats.get("daily") if isinstance(stats, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, bucket in raw.items():
        if not isinstance(bucket, dict):
            continue
        clean = {}
        for field in _DAILY_FIELDS:
            value = bucket.get(field)
            clean[field] = value if isinstance(value, int) and value >= 0 else 0
        out[key] = clean
    return out


def flush_stats_if_dirty(path: str) -> None:
    global _stats_dirty
    with STATS_LOCK:
        dirty = _stats_dirty
        _stats_dirty = False
    if not dirty:
        return
    try:
        save_stats_counters(path)
    except OSError as exc:
        # 落盘失败（磁盘满/权限）：回置脏标记等下轮重试，代理继续服务不因统计
        # 持久化受阻，stderr 留痕。
        _stats_dirty = True
        print("ctyun-stream-fix-proxy: stats flush failed: %s" % exc,
              file=sys.stderr, flush=True)


def write_allowed(peer_ip: str, token_header: str, token_env: str) -> bool:
    if peer_ip in ("127.0.0.1", "::1"):
        return True
    if not token_env:
        return False
    return hmac.compare_digest(token_header.encode("utf-8"), token_env.encode("utf-8"))


def set_upstream_base(base: str) -> None:
    global UPSTREAM_BASE, _upstream_source
    with _CFG_LOCK:
        UPSTREAM_BASE = base
        _upstream_source = "api"
        persist_upstream(base, PERSIST_PATH)
    # save_stats_counters 内部也要拿 _CFG_LOCK：必须在锁外调用，否则同线程
    # 非重入死锁（admin 线程挂死且 SIGTERM 退出时同样卡锁）。
    save_stats_counters(PERSIST_PATH)


def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False) -> None:
    global _stats_dirty
    with STATS_LOCK:
        if error:
            STATS["errors_total"] += 1
        STATS["filtered_total"] += filtered
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0,
                                                "errors_proxy": 0,
                                                "errors_upstream": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["requests"] += 1
                entry_dm["filtered"] += filtered
                if error:
                    entry_dm["errors_proxy"] += 1
                elif status >= 500:
                    entry_dm["errors_upstream"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0})
        bucket["requests"] += 1
        bucket["filtered"] += filtered
        if error:
            bucket["errors_proxy"] += 1
        elif status >= 500:
            bucket["errors_upstream"] += 1
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model})
        _stats_dirty = True


def _record_poison_preview(raw: bytes) -> None:
    preview = raw.decode("utf-8", "replace")
    if len(preview) > 200:
        preview = preview[:200]
    with STATS_LOCK:
        POISON_PREVIEWS.append({"ts": time.time(), "preview": preview})


def _record_empty_retry(model=None) -> None:
    """空流重试计数：STATS 总量 + 当日桶 + daily_by_model。
    entry/桶形状必须与 _record_request 同步含 retries 键（旧持久化桶经
    load_daily_buckets 的 _DAILY_FIELDS 清洗已补键），否则 += 直接 KeyError。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["empty_retries_total"] += 1
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                entry_dm = day_models[model] = {"requests": 0, "filtered": 0, "retries": 0}
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["retries"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), {"requests": 0, "filtered": 0,
                          "errors_proxy": 0, "errors_upstream": 0, "retries": 0})
        bucket["retries"] += 1
        _stats_dirty = True


def stats_snapshot() -> dict:
    with STATS_LOCK:
        snap = dict(STATS)
        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
        snap["daily_by_model"] = {
            d: {m: dict(v) for m, v in models.items()}
            for d, models in STATS["daily_by_model"].items()}
        snap["recent"] = list(RECENT_REQUESTS)
        snap["poison_previews"] = list(POISON_PREVIEWS)
    snap["uptime_s"] = int(time.time() - STARTED_AT)
    snap["upstream_base"] = UPSTREAM_BASE
    snap["upstream_source"] = _upstream_source
    return snap


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
        with STATS_LOCK:
            STATS["requests_total"] += 1
            STATS["active"] += 1
        try:
            self._proxy_relay(started)
        except (ConnectionError, socket.timeout):
            # 吞掉的是 relay 阶段任一端断流：客户端 EPIPE/reset（RST）、客户端优雅
            # 关闭后 send 阻塞到 SEND_TIMEOUT_S、上游中途 stall 到 600s 超时——三者
            # 都是"turn 中断、非代理故障"（客户端取消是常态；后两者无法与前者区分
            # 处也无需区分）。不重抛：再抛只进 handle_error 打 20+ 行 traceback 且
            # 无 status 记录；不计 errors_total。status 取 499（nginx 客户端中断惯例）。
            self._log(started, 499, "aborted", 0)
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None)
        finally:
            with STATS_LOCK:
                STATS["active"] -= 1

    def _proxy_relay(self, started: float) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        model = extract_model(body)

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

        try:
            conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            self._log(started, 502, "error", 0, model=model)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=True)
            return

        content_type = (resp.getheader("Content-Type") or "").lower()
        result = "ok" if resp.status < 400 else "upstream-err"
        if "text/event-stream" in content_type:
            retried = 0
            try:
                filtered = self._relay_sse(resp, final=(EMPTY_RETRY_MAX < 1))
            except _EmptyStream as exc:
                filtered = exc.filtered  # attempt-1 已滤毒缓冲随重试丢弃，filtered 只计交付流
                retried = 1
                _record_empty_retry(model)
                conn.close()
                try:
                    conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                except (OSError, http.client.HTTPException) as retry_exc:
                    self._reply_502(retry_exc)  # 客户端尚未收到字节，502 语义与既有路径一致
                    self._log(started, 502, "error", 0, model=model, retried=1)
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model, error=True)
                    return
                filtered = self._relay_sse(resp, final=True)
            result = "ok" if resp.status < 400 else "upstream-err"  # 重试后按实际 resp 重算
            self._log(started, resp.status, result, filtered, model=model, retried=retried)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model)
        else:
            self._relay_buffered(resp)
            self._log(started, resp.status, result, 0, model=model)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model)
        conn.close()

    def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):
        parsed = urllib.parse.urlparse(UPSTREAM_BASE)
        upstream_path = parsed.path.rstrip("/") + path
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
        conn.request(method, upstream_path, body=body, headers=fwd_headers)
        return conn, conn.getresponse()

    def _send_sse_headers(self, resp) -> None:
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> int:
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            filtered = 0
            pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
            poisoned = False  # 当前 record 内是否命中毒行
            primed = []       # priming 阶段已滤毒缓冲的完整 record 行（含终结空行）
            priming = True    # True = 客户端尚未收到任何字节
            while True:
                line = resp.readline()
                if line in (b"\n", b"\r\n", b""):
                    # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    if poisoned:
                        filtered += 1  # 整 record（含终结空行）丢弃，不损伤相邻字节（priming 期不进 primed）
                        _record_poison_preview(b"".join(pending))
                    elif priming:
                        primed.extend(pending)
                        if line:
                            primed.append(line)
                        if "content" in kinds or "done" in kinds:
                            # 首个信号 record：补发头 + 整段前缀，转 streaming
                            self._send_sse_headers(resp)
                            self.wfile.write(b"".join(primed))
                            self.wfile.flush()
                            priming = False
                    else:
                        for buf_line in pending:
                            self.wfile.write(buf_line)
                        if line:
                            self.wfile.write(line)
                        self.wfile.flush()
                    pending = []
                    poisoned = False
                    if line == b"":
                        if priming:  # EOF 仍 priming = 空流（零 record / reasoning-only 断流）
                            if final:  # 按现状语义收尾：缓冲原样下发（含合法 [DONE] 零内容流）
                                self._send_sse_headers(resp)
                                self.wfile.write(b"".join(primed))
                                self.wfile.flush()
                            else:
                                raise _EmptyStream(filtered, primed)
                        break
                else:
                    if POISON_RE.match(line.rstrip(b"\r\n")):
                        poisoned = True
                    pending.append(line)
            return filtered
        finally:
            self.connection.settimeout(old_timeout)

    def _relay_buffered(self, resp: http.client.HTTPResponse) -> None:
        data = resp.read()
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            if data:
                self.wfile.write(data)
        finally:
            self.connection.settimeout(old_timeout)

    def _reply_502(self, exc: BaseException) -> None:
        payload = ("ctyun-stream-fix-proxy: upstream error: %s\n" % exc).encode("utf-8")
        self.send_response(502)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0) -> None:
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))


class AdminHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:
        path = urllib.parse.urlsplit(self.path).path
        if path == "/":
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML)
        elif path == "/favicon.ico":
            self._send(200, "image/x-icon", FAVICON_ICO)
        elif path == "/api/stats":
            self._send_json(200, stats_snapshot())
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source}
            self._send_json(200, payload)
        else:
            self._send(404, "text/plain; charset=utf-8", b"not found")

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > POST_BODY_LIMIT:
            self._send_json(413, {"error": "请求体超过 %d 字节上限" % POST_BODY_LIMIT})
            self.close_connection = True
            return
        raw = self.rfile.read(length) if length > 0 else b""
        path = urllib.parse.urlsplit(self.path).path
        if path != "/api/config":
            self._send(404, "text/plain; charset=utf-8", b"not found")
            return
        if not write_allowed(self.client_address[0],
                             self.headers.get("X-Admin-Token") or "",
                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            self._send_json(403, {"error": "非本机修改上游端点需要 X-Admin-Token 头"
                                           "（值 = 服务器环境变量 CTYUN_ADMIN_TOKEN）"})
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体不是合法 JSON——"
                                           "请发 {\"upstream_base\": \"https://...\"}"})
            return
        base = data.get("upstream_base") if isinstance(data, dict) else None
        if not isinstance(base, str) or not valid_upstream_url(base):
            self._send_json(400, {"error": "upstream_base 需为 "
                                           "http(s)://host[:port]/path 形式的合法 URL"})
            return
        set_upstream_base(base)
        self._send_json(200, {"ok": True, "upstream_base": base, "source": "api"})

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, payload: dict) -> None:
        self._send(status, "application/json; charset=utf-8",
                   json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    def log_message(self, format: str, *args: object) -> None:
        # 吞掉的是默认访问日志行：dashboard 2s 轮询会把它刷成纯噪音，访问事实由
        # /api/stats 自身承载。handler 内异常 traceback 走 handle_error 直接打
        # stderr，不经本方法，不会被吞。
        pass


# dashboard（单文件零外部依赖，局域网离线可用）：str 常量而非 f-string，CSS/JS 大括号
# 零冲突；bytes 字面量放不下中文，故 str + 一次 encode。动态数据一律 textContent，
# 禁 innerHTML（毒行预览来自上游原始字节，防注入）。
_DASHBOARD_SRC = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/x-icon" href="/favicon.ico">
<meta name="theme-color" content="#0F141D">
<meta name="apple-mobile-web-app-title" content="剥行代理">
<title>CTYUN 剥行代理 · 运行台</title>
<style>
:root {
  --bg:#0F141D; --panel:#161E2B; --line:#24304A; --text:#D7DEEA; --dim:#7C8AA5;
  --amber:#F5A623; --ok:#4CC38A; --err:#E5646C; --ink:#14100A;
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size:14px; line-height:1.55; }
header { border-bottom:1px solid var(--line); background:var(--panel); }
header .inner, main, footer .inner { max-width:1060px; margin:0 auto; padding:12px 16px; }
header .inner { display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; }
.brand { font-weight:700; letter-spacing:.08em; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%;
  background:var(--dim); margin-left:8px; vertical-align:baseline; }
.dot.ok { background:var(--ok); } .dot.err { background:var(--err); }
.uptime { color:var(--dim); font-size:12px; }
main { display:grid; gap:12px; padding:16px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:14px 16px; }
.card-title { color:var(--dim); font-size:12px; letter-spacing:.06em; margin-bottom:8px; }
.chip { display:inline-block; border:1px solid var(--line); border-radius:99px;
  padding:0 8px; font-size:11px; color:var(--amber); margin-left:6px; }
.upstream-url { color:var(--amber); font-size:15px; word-break:break-all; margin-bottom:10px; }
.upstream-url.dimmed { color:var(--dim); }
form { display:flex; gap:8px; flex-wrap:wrap; }
input { flex:1 1 320px; font:inherit; background:#0B0F17; color:var(--text);
  border:1px solid var(--line); border-radius:6px; padding:8px 12px; }
button { font:inherit; font-weight:700; background:var(--amber); color:var(--ink);
  border:none; border-radius:6px; padding:8px 16px; cursor:pointer; }
button:hover { filter:brightness(1.08); }
.msg { margin:8px 0 0; font-size:12.5px; min-height:1.2em; }
.msg.ok { color:var(--ok); } .msg.err { color:var(--err); } .msg.wait { color:var(--dim); }
.stats-row { display:grid; grid-template-columns:repeat(auto-fit, minmax(150px, 1fr)); gap:12px; }
.stat .num { font-size:30px; font-weight:600; font-variant-numeric:tabular-nums; }
.stat .num.amber { color:var(--amber); }
.stat .label { color:var(--dim); font-size:12px; }
svg#spark { width:100%; height:64px; display:block; }
.strip { display:flex; gap:8px; overflow-x:auto; padding-bottom:4px; }
.chip-poison { flex:0 0 auto; border:1px solid var(--amber); border-left:4px solid var(--amber);
  border-radius:4px; padding:2px 8px; color:var(--amber); font-size:12px;
  text-decoration:line-through; white-space:nowrap; }
    .chip-poison .t { color:var(--dim); text-decoration:none; margin-right:6px; }
.empty { color:var(--dim); }
.table-wrap { overflow-x:auto; }
table { width:100%; border-collapse:collapse; font-size:13px; }
th, td { text-align:left; padding:5px 8px; border-bottom:1px solid var(--line); white-space:nowrap; }
th { color:var(--dim); font-weight:400; font-size:12px; }
td.num { font-variant-numeric:tabular-nums; }
tr.hit td { color:var(--amber); }
tr.hit td.path, td.path { color:inherit; }
tr.date-row td { color:var(--dim); font-size:12px; padding:3px 8px; }
footer .inner { color:var(--dim); font-size:12px; padding-top:4px; padding-bottom:20px; }
:focus-visible { outline:2px solid var(--amber); outline-offset:2px; }
@media (prefers-reduced-motion: reduce) { * { transition:none !important; animation:none !important; } }
@media (max-width:720px) {
  .stat .num { font-size:24px; }
  main { padding:12px; }
}
</style>
</head>
<body>
<header><div class="inner">
  <span class="brand">CTYUN 剥行代理 · 运行台<span class="dot" id="conn-dot"></span></span>
  <span class="uptime" id="uptime">运行时长 --</span>
</div></header>
<main>
  <section class="card">
    <div class="card-title">上游端点<span class="chip" id="upstream-source">--</span></div>
    <div class="upstream-url dimmed" id="upstream-base">读取中……</div>
    <form id="upstream-form">
      <input id="upstream-input" type="text" autocomplete="off" spellcheck="false"
             placeholder="https://eaichat.ctyun.cn/ai/platform/v2/cp" aria-label="新的上游端点 URL">
      <button type="submit">保存上游端点</button>
    </form>
    <p class="msg" id="upstream-msg" role="status"></p>
  </section>
  <section class="stats-row">
    <div class="card stat"><div class="num" id="st-requests">--</div><div class="label">请求数</div></div>
    <div class="card stat"><div class="num amber" id="st-filtered">--</div><div class="label">剥行累计</div></div>
    <div class="card stat"><div class="num" id="st-rate">--</div><div class="label">毒行率</div></div>
    <div class="card stat"><div class="num" id="st-active">--</div><div class="label">活跃连接</div></div>
    <div class="card stat"><div class="num" id="st-errors">--</div><div class="label">错误数</div></div>
  </section>
  <section class="card">
    <div class="card-title">请求节奏 · 最近 10 分钟（琥珀=请求，红=剥行）</div>
    <svg id="spark" viewBox="0 0 600 64" preserveAspectRatio="none" role="img" aria-label="最近 10 分钟请求柱状图"></svg>
  </section>
  <section class="card">
    <div class="card-title">按天统计（最近 14 天，新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-body"><tr><td class="empty" colspan="6">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">按天 × 模型（内存累计，重启清零）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>请求</th><th>剥行</th><th>重试</th></tr></thead>
      <tbody id="daily-model-body"><tr><td class="empty" colspan="5">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">剥行流带 · 最近剥除的毒 record</div>
    <div class="strip" id="poison-strip"><span class="empty">读取中……</span></div>
  </section>
  <section class="card">
    <div class="card-title">最近请求（新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>时间</th><th>方法</th><th>路径</th><th>模型</th><th>状态</th><th>耗时</th><th>剥行</th></tr></thead>
      <tbody id="req-body"></tbody>
    </table>
    </div>
  </section>
</main>
<footer><div class="inner">
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json）；
  按天×模型计数自进程启动累计，不持久化（重启清零）；
  按天主表含无 model 请求，各行数值 ≥「按天 × 模型」副表合计，差值即当日无 model 请求；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（与顶部错误数同口径），
  「上游5xx」=上游透传 status≥500（499 中断两边都不计）。页面每 2s 轮询 /api/stats；非本机修改上游需 X-Admin-Token。
</div></footer>
<script>
"use strict";
var $ = function (id) { return document.getElementById(id); };
function el(tag, cls, text) {
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}
var SOURCE_LABEL = { env: "环境变量", file: "持久化文件", api: "网页设置", default: "内置默认" };

function fmtTime(ts) {
  return new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false });
}
function fmtDate(ts) {
  var d = new Date(ts * 1000);
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate());
}
function fmtDur(ms) {
  return ms >= 1000 ? (ms / 1000).toFixed(1) + "s" : Math.round(ms) + "ms";
}
function fmtUptime(s) {
  var d = Math.floor(s / 86400), h = Math.floor(s % 86400 / 3600),
      m = Math.floor(s % 3600 / 60), sec = s % 60;
  return (d ? d + "d " : "") + (h ? h + "h " : "") + (m ? m + "m " : "") + sec + "s";
}
function setConn(ok, errText) {
  var dot = $("conn-dot");
  dot.className = "dot " + (ok ? "ok" : "err");
  if (!ok) {
    var strip = $("poison-strip");
    strip.textContent = "";
    strip.appendChild(el("span", "empty",
      "无法连接管理接口（/api/stats）：" + errText +
      " —— 确认代理进程存活：launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy"));
  }
}
function renderStats(snap) {
  $("st-requests").textContent = snap.requests_total;
  $("st-filtered").textContent = snap.filtered_total;
  $("st-rate").textContent = snap.requests_total > 0
    ? ((snap.filtered_total / snap.requests_total) * 100).toFixed(1) + "%" : "—";
  $("st-active").textContent = snap.active;
  $("st-errors").textContent = snap.errors_total;
  $("uptime").textContent = "运行时长 " + fmtUptime(snap.uptime_s);
  var base = $("upstream-base");
  base.textContent = snap.upstream_base;
  base.classList.remove("dimmed");
  $("upstream-source").textContent = SOURCE_LABEL[snap.upstream_source] || snap.upstream_source;
}
function renderRecent(list) {
  var body = $("req-body");
  body.textContent = "";
  var dateSet = {};
  for (var i = 0; i < list.length; i++) dateSet[fmtDate(list[i].ts)] = true;
  var multiDay = Object.keys(dateSet).length > 1;
  var lastDate = null;
  for (var i = list.length - 1; i >= 0; i--) {
    var r = list[i];
    if (multiDay) {
      var d = fmtDate(r.ts);
      if (d !== lastDate) {  // 新在上遍历：日期切换点即插分组行
        var trd = el("tr", "date-row");
        var tdd = el("td", "", d);
        tdd.colSpan = 7;
        trd.appendChild(tdd);
        body.appendChild(trd);
        lastDate = d;
      }
    }
    var tr = el("tr", r.filtered > 0 ? "hit" : "");
    tr.appendChild(el("td", "num", fmtTime(r.ts)));
    tr.appendChild(el("td", "", r.method));
    tr.appendChild(el("td", "path", r.path));
    tr.appendChild(el("td", "path", r.model || "—"));
    tr.appendChild(el("td", "num", String(r.status)));
    tr.appendChild(el("td", "num", fmtDur(r.dur_ms)));
    tr.appendChild(el("td", "num", r.filtered > 0 ? String(r.filtered) : "0"));
    body.appendChild(tr);
  }
  if (list.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无请求 —— 有 SSE 流经过代理后这里会出现记录");
    td0.colSpan = 7;
    tr0.appendChild(td0);
    body.appendChild(tr0);
  }
}
function renderPoison(list) {
  var strip = $("poison-strip");
  strip.textContent = "";
  if (list.length === 0) {
    strip.appendChild(el("span", "empty", "暂无剥行记录 —— 上游干净，或尚未有 SSE 流经过"));
    return;
  }
  for (var i = list.length - 1; i >= 0; i--) {
    var p = list[i];
    var chip = el("span", "chip-poison");
    chip.appendChild(el("span", "t", fmtTime(p.ts)));
    chip.appendChild(document.createTextNode(p.preview));
    strip.appendChild(chip);
  }
}
function renderDaily(daily) {
  var body = $("daily-body");
  body.textContent = "";
  var keys = Object.keys(daily).sort().reverse().slice(0, 14);
  if (keys.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天统计");
    td0.colSpan = 6;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    return;
  }
  for (var i = 0; i < keys.length; i++) {
    var b = daily[keys[i]];
    var tr = el("tr", (b.errors_proxy + b.errors_upstream) > 0 ? "hit" : "");
    tr.appendChild(el("td", "num", keys[i]));
    tr.appendChild(el("td", "num", String(b.requests)));
    tr.appendChild(el("td", "num", String(b.filtered)));
    tr.appendChild(el("td", "num", String(b.errors_proxy)));
    tr.appendChild(el("td", "num", String(b.errors_upstream)));
    tr.appendChild(el("td", "num", String(b.retries || 0)));
    body.appendChild(tr);
  }
}
function renderDailyByModel(dbm) {
  var body = $("daily-model-body");
  body.textContent = "";
  var days = Object.keys(dbm).sort().reverse().slice(0, 14);
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天 × 模型统计");
    td0.colSpan = 5;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    return;
  }
  for (var i = 0; i < days.length; i++) {
    var models = dbm[days[i]];
    var names = Object.keys(models);
    names.sort(function (a, b) {
      return (models[b].requests || 0) - (models[a].requests || 0);
    });
    for (var j = 0; j < names.length; j++) {
      var ent = models[names[j]];
      var tr = el("tr", (ent.retries || 0) > 0 ? "hit" : "");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(ent.requests || 0)));
      tr.appendChild(el("td", "num", String(ent.filtered || 0)));
      tr.appendChild(el("td", "num", String(ent.retries || 0)));
      body.appendChild(tr);
    }
  }
}
function renderSpark(recent) {
  var svg = $("spark");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 600, H = 64, N = 30, SPAN = 600;
  var now = Date.now() / 1000;
  var buckets = [], filtered = [];
  for (var i = 0; i < N; i++) { buckets.push(0); filtered.push(0); }
  for (var j = 0; j < recent.length; j++) {
    var age = now - recent[j].ts;
    if (age < 0 || age > SPAN) continue;
    var idx = N - 1 - Math.floor(age / (SPAN / N));
    if (idx >= 0 && idx < N) {
      buckets[idx] += 1;
      filtered[idx] += recent[j].filtered || 0;
    }
  }
  var max = 1;
  for (var k = 0; k < N; k++) {
    if (buckets[k] > max) max = buckets[k];
    if (filtered[k] > max) max = filtered[k];
  }
  var bw = W / N;
  for (var m = 0; m < N; m++) {
    // 先画红色剥行柱再叠琥珀请求柱：同桶两者都 >0 时琥珀在上、红色露出底部
    if (filtered[m] > 0) {
      var fh = Math.max(4, filtered[m] / max * (H - 8));
      svg.appendChild(bar(m, bw, fh, H, "var(--err)"));
    }
    if (buckets[m] > 0) {
      var h = Math.max(4, buckets[m] / max * (H - 8));
      svg.appendChild(bar(m, bw, h, H, "var(--amber)"));
    }
    if (buckets[m] === 0 && filtered[m] === 0) {
      svg.appendChild(bar(m, bw, 2, H, "var(--line)"));
    }
  }
}
function bar(idx, bw, h, H, fill) {
  var rect = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  rect.setAttribute("x", (idx * bw + 1).toFixed(1));
  rect.setAttribute("y", (H - h).toFixed(1));
  rect.setAttribute("width", Math.max(1, bw - 2).toFixed(1));
  rect.setAttribute("height", h.toFixed(1));
  rect.setAttribute("fill", fill);
  return rect;
}
function poll() {
  var ctrl = new AbortController();
  var timer = setTimeout(function () { ctrl.abort(); }, 4000);
  fetch("/api/stats", { signal: ctrl.signal })
    .then(function (resp) {
      clearTimeout(timer);
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (snap) {
      renderStats(snap);
      renderRecent(snap.recent || []);
      renderPoison(snap.poison_previews || []);
      renderDaily(snap.daily || {});
      renderDailyByModel(snap.daily_by_model || {});
      renderSpark(snap.recent || []);
      setConn(true);
    })
    .catch(function (err) {
      clearTimeout(timer);
      setConn(false, err && err.name === "AbortError" ? "轮询超时(4s)" : String(err));
    });
}
$("upstream-form").addEventListener("submit", function (e) {
  e.preventDefault();
  var msg = $("upstream-msg");
  var value = $("upstream-input").value.trim();
  if (!value) {
    msg.className = "msg err";
    msg.textContent = "先填入新的上游端点，再点保存。";
    return;
  }
  msg.className = "msg wait";
  msg.textContent = "保存中……";
  fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ upstream_base: value })
  }).then(function (resp) {
    return resp.json().then(function (data) { return { status: resp.status, data: data }; });
  }).then(function (r) {
    if (r.status === 200) {
      msg.className = "msg ok";
      msg.textContent = "已保存，立即生效（当前 " + r.data.upstream_base + "）";
      $("upstream-input").value = "";
      poll();
    } else {
      msg.className = "msg err";
      msg.textContent = (r.data && r.data.error) ? r.data.error : ("保存失败：HTTP " + r.status);
    }
  }).catch(function (err) {
    msg.className = "msg err";
    msg.textContent = "保存失败：" + String(err) + " —— 确认能访问管理接口 /api/config。";
  });
});
poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""
DASHBOARD_HTML = _DASHBOARD_SRC.encode("utf-8")

# favicon：32×32 深石板底 + 三根琥珀 SSE 行、中根被删除线剥除（与页面"剥行流带"
# 同一视觉语言）。构建期用纯 zlib/struct 生成 ICO（内嵌 PNG 格式），运行期只解码。
_FAVICON_B64 = (
    "AAABAAEAICAAAAEAIADGAAAAFgAAAIlQTkcNChoKAAAADUlIRFIAAAAgAAAAIAgGAAAAc3p69AAA"
    "AI1JREFUeNpjYMADxOS0/1MDM5ACqGUpWY6hteV4HUEvy7E6gt6WYzhiZDtgoCyHOwKXxNdlylTF"
    "ow4YdQDJDhjwXDCgDugP4cWJgcGZRw6mmgPIdRxdHIAPk5MLsPqE2o4g2QH4MFUdQG084A4YLYoH"
    "rwNwtYro4QC8bcIBd8Cw7xsMvq7ZoOicDkT3HADt2Gg9Gd0R2gAAAABJRU5ErkJggg=="
)
FAVICON_ICO = base64.b64decode(_FAVICON_B64)


def main() -> None:
    global UPSTREAM_BASE, _upstream_source, _stats_dirty
    UPSTREAM_BASE, _upstream_source = resolve_upstream_base(
        os.environ.get("CTYUN_UPSTREAM_BASE"), PERSIST_PATH)
    counters = load_stats_counters(PERSIST_PATH)  # 累计计数跨重启续算
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["daily"] = daily
        _stats_dirty = False

    def _on_signal(signum, frame):
        try:
            save_stats_counters(PERSIST_PATH)
        except OSError as exc:
            # 退出路径落盘失败仅留痕不阻断退出：计数一致性由日志兜底，
            # 进程此时必须退出（launchd 语义），无其他路径可达。
            _safe_log_stderr("ctyun-stream-fix-proxy: stats save on exit failed: %s" % exc)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    try:
        admin_server = http.server.ThreadingHTTPServer((ADMIN_HOST, ADMIN_PORT), AdminHandler)
    except OSError as exc:
        print("ctyun-stream-fix-proxy: admin bind failed on %s:%d: %s — exit 1，"
              "交由 launchd KeepAlive 重试" % (ADMIN_HOST, ADMIN_PORT, exc),
              file=sys.stderr, flush=True)
        raise SystemExit(1)
    admin_server.daemon_threads = True
    threading.Thread(target=admin_server.serve_forever, daemon=True,
                     name="admin-dashboard").start()

    def _dirty_flush_loop():
        while True:
            time.sleep(FLUSH_INTERVAL_S)
            flush_stats_if_dirty(PERSIST_PATH)

    threading.Thread(target=_dirty_flush_loop, daemon=True,
                     name="stats-flush").start()

    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.daemon_threads = True
    print("ctyun-stream-fix-proxy listening on %s:%d -> %s (admin dashboard on %s:%d)"
          % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE, ADMIN_HOST, ADMIN_PORT),
          file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        admin_server.server_close()


if __name__ == "__main__":
    main()
