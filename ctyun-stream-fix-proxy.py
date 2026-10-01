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
import bisect
import hashlib
import collections
import datetime
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
LISTEN_HOST = os.environ.get("CTYUN_LISTEN_HOST", "127.0.0.1")
LISTEN_PORT = int(os.environ.get("CTYUN_LISTEN_PORT", "7920"))
ADMIN_HOST = os.environ.get("CTYUN_ADMIN_HOST", "0.0.0.0")
ADMIN_PORT = int(os.environ.get("CTYUN_ADMIN_PORT", "7921"))
PERSIST_PATH = os.environ.get(
    "CTYUN_PERSIST_PATH",
    os.path.expanduser("~/.local/etc/ctyun-stream-fix-proxy.json"))
UPSTREAM_TIMEOUT = 600
HEADER_TIMEOUT_S = max(1.0, float(os.environ.get("CTYUN_HEADER_TIMEOUT", "45")))
# 客户端 send 超时：客户端优雅关闭(FIN)后代理的 send 会无限期阻塞（实测 sample 卡
# __sendto），把线程永久钉死；健康读者不会让 send 阻塞超过一次缓冲排空，阻塞到此
# 阈值即视为对端已死。env 仅作测试 seam（测试用 1s 加速）。
SEND_TIMEOUT_S = float(os.environ.get("CTYUN_SEND_TIMEOUT", "60"))
POST_BODY_LIMIT = 8192
FLUSH_INTERVAL_S = 60
BY_MODEL_CAP = 32
DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）
RANGE_KEYS = ("3d", "7d", "mtd", "last_month")  # 时间维度 tab 键序（快照/dashboard 共用）
POISON_RE = re.compile(rb"^data:\s*null\s*$")
DONE_RE = re.compile(rb"^data:\s*\[DONE\]\s*$")
EMPTY_RETRY_MAX = int(os.environ.get("CTYUN_EMPTY_RETRY", "1"))  # env seam，惯例同 SEND_TIMEOUT_S
HEADER_RETRY_MAX = max(0, int(os.environ.get("CTYUN_HEADER_RETRY", "1")))

# per-model 预算解析（fail-open；模块加载时即调用 → 前置于此，保证常量区 :63 可见）
def _parse_tpm_limit_by_model_log(item: str) -> None:
    # 模块加载时（常量区）_safe_log_stderr（:783）尚未定义，内联其等价实现；
    # 吞掉的是 stderr 写失败的 OSError（BrokenPipeError/ENOSPC 等）：stderr 是
    # best-effort 诊断出口，无备用通道，且失败不得传播——传播会中断模块导入。
    try:
        print("ctyun-stream-fix-proxy: CTYUN_TPM_LIMIT_BY_MODEL: "
              "skip malformed entry %r" % item, file=sys.stderr, flush=True)
    except OSError:
        pass


def _parse_tpm_limit_by_model(env_str: str) -> dict:
    """解析 CTYUN_TPM_LIMIT_BY_MODEL（"model:limit,model:limit"）→ {model: limit}。

    fail-open：逐项 split(":")，非两项 / limit 非 int / limit 负值 → 跳过该项并
    stderr 一行；空串 / 全畸形 → {}。内置默认（kimi-k3-oc:30000）不并入本表，
    由 tpm_limit_for 兜底——保证快照 config.limit_by_model 原样展示 env 表。
    """
    result = {}
    if not env_str or not env_str.strip():
        return result
    for item in env_str.split(","):
        item = item.strip()
        if not item:
            continue
        parts = item.split(":")
        if len(parts) != 2 or not parts[0].strip():
            _parse_tpm_limit_by_model_log(item)
            continue
        model = parts[0].strip()
        try:
            limit = int(parts[1].strip())
        except ValueError:
            # 吞掉的是非整数值（"abc" 等）：fail-open 跳过该项并已 log，无其他路径可达。
            _parse_tpm_limit_by_model_log(item)
            continue
        if limit < 0:
            _parse_tpm_limit_by_model_log(item)
            continue
        result[model] = limit
    return result


# TPM rate limiting（per-key 滚动窗口 + FIFO 排队）
TPM_LIMIT = int(os.environ.get("CTYUN_TPM_LIMIT", "110000"))
TPM_WINDOW_S = int(os.environ.get("CTYUN_TPM_WINDOW_S", "60"))
TPM_QUEUE_MAX = int(os.environ.get("CTYUN_TPM_QUEUE_MAX", "20"))
TPM_QUEUE_TIMEOUT_S = float(os.environ.get("CTYUN_TPM_QUEUE_TIMEOUT_S", "120"))
TPM_TOKEN_RATIO = float(os.environ.get("CTYUN_TPM_TOKEN_RATIO", "0.25"))
TPM_KEY_CAP = int(os.environ.get("CTYUN_TPM_KEY_CAP", "64"))

# per-model 预算（决策 2）：env seam "model:limit,model:limit"（逗号分隔）；
# 解析 fail-open 见 _parse_tpm_limit_by_model；内置默认不并入本表以保快照原样展示。
TPM_LIMIT_BY_MODEL: dict = _parse_tpm_limit_by_model(
    os.environ.get("CTYUN_TPM_LIMIT_BY_MODEL", ""))
_TPM_LIMIT_BY_MODEL_BUILTIN: dict = {"kimi-k3-oc": 30000}  # 实证 31k 触顶留余量；env 表优先覆盖

PRIMED_TAIL_CAP = 262144  # finish hold 尾段缓冲上限（超限 fail-open 防内存膨胀）

# observability v2 常量（P1 先声明；HIST_BUCKETS_MS P2 起用、PROBE_* P4 起用）
HIST_BUCKETS_MS = (100, 250, 500, 1000, 2000, 5000, 10000, 30000)  # 8 桶右开边界，+inf 隐式第 9 桶（对数等比 ≈ ×2.5，覆盖 100ms~30s+）
PROBE_INTERVAL_S_DEFAULT = 30      # v2 P4：probe 线程默认间隔（CTYUN_PROBE_INTERVAL_S env seam 可调）
PROBE_TIMEOUT_S = 5                # v2 P4：probe HEAD 请求超时
PROBE_MIN_INTERVAL_S = 10          # v2 P4：probe 间隔下限（防滥用）
PROBE_FAILURE_THRESHOLD = 3        # v2 P4：连续失败次数达此值触发 EVENTS probe_alert
PROBE_ALERT_DEBOUNCE_S = 300       # v2 P4：同类 probe 告警最小间隔（去抖）
PROBE_LATENCY_SPIKE_FACTOR = 2.0   # v2 P4：延迟突增告警阈值倍数（最近 20 次 P90 > 基线 P90 × 此值）
PROBE_HISTORY_MAX = 20160          # v2 P4：probe 延迟历史 deque 上限（30s 间隔 × 7 天；内存态不持久化）


def _probe_interval_s_from_env(raw: str) -> float:
    """probe 间隔解析（env seam CTYUN_PROBE_INTERVAL_S，模块导入时求值一次）。

    显式正数值原样采用（测试 0.5s 加速 seam）；空串回落默认 30s；≤0 或非法值
    回落 PROBE_MIN_INTERVAL_S=10（防误配忙轮询打上游——"最小 10s 防滥用"只兜
    非法值、不钳显式合法值，否则测试加速 seam 失效，见 PLAN Anchor A1）。
    """
    if not raw.strip():
        return float(PROBE_INTERVAL_S_DEFAULT)
    try:
        value = float(raw)
    except ValueError:
        # 吞掉的是 env 里非法数值字符串：配置错误按防滥用下限回落，无其他路径可达。
        return float(PROBE_MIN_INTERVAL_S)
    if value <= 0:
        return float(PROBE_MIN_INTERVAL_S)
    return value


PROBE_INTERVAL_S = _probe_interval_s_from_env(
    os.environ.get("CTYUN_PROBE_INTERVAL_S", ""))
STALL_THRESHOLD_S = float(os.environ.get("CTYUN_STALL_THRESHOLD_S", "5.0"))  # v2 P2：SSE 相邻 record 间隔 > 此值计一次 stall（env seam，测试降至 0.5 加速）

ERROR_RING_MAX = 50
BODY_SNAPSHOT_CAP = 4096
RESPONSE_SNIPPET_CAP = 2048
LOG_RING_MAX = 1000
LOG_TAIL_DEFAULT = 100
LOG_PAGE_MAX = 500

CLASS_OK = "ok"
CLASS_CLIENT_ABORT = "client_abort"
CLASS_REQUEST_FAULT = "request_fault"
CLASS_UPSTREAM_FAULT = "upstream_fault"
CLASS_POISON_FIXED = "poison_fixed"
CLASS_TPM_LIMITED = "tpm_limited"

ERR_KIND_POISON = "poison_hit"
ERR_KIND_EMPTY_RETRY = "empty_retry"
ERR_KIND_EOF_NO_DONE = "eof_without_done"
ERR_KIND_SYNTH_502 = "synth_502"
ERR_KIND_UPSTREAM_5XX = "upstream_5xx"
ERR_KIND_REQUEST_4XX = "request_4xx"
ERR_KIND_HEADER_TIMEOUT = "header_timeout"
ERR_KIND_TPM_QUEUE_FULL = "tpm_queue_full"
ERR_KIND_TPM_QUEUE_TIMEOUT = "tpm_queue_timeout"

CLASS_BODY_ERROR = "body_error"
ERR_KIND_BODY_ERROR = "body_error"

_KIND_CATEGORY = {
    ERR_KIND_POISON: CLASS_POISON_FIXED,
    ERR_KIND_EMPTY_RETRY: CLASS_OK,
    ERR_KIND_EOF_NO_DONE: CLASS_UPSTREAM_FAULT,
    ERR_KIND_SYNTH_502: CLASS_UPSTREAM_FAULT,
    ERR_KIND_UPSTREAM_5XX: CLASS_UPSTREAM_FAULT,
    ERR_KIND_REQUEST_4XX: CLASS_REQUEST_FAULT,
    ERR_KIND_HEADER_TIMEOUT: CLASS_UPSTREAM_FAULT,
    ERR_KIND_BODY_ERROR: CLASS_BODY_ERROR,
    ERR_KIND_TPM_QUEUE_FULL: CLASS_TPM_LIMITED,
    ERR_KIND_TPM_QUEUE_TIMEOUT: CLASS_TPM_LIMITED,
}


def hist_percentile(buckets, edges, q):
    """直方图线性插值分位数（stdlib 手写，无 numpy；≤20 行）。

    buckets: 桶计数序列，len == len(edges) + 1（末桶为 [edges[-1], +inf)）。
    edges:   升序右开边界（HIST_BUCKETS_MS，8 个）。
    q:       分位数 (0, 1]。
    返回插值毫秒值：桶内均匀分布线性插值；空直方图 → 0.0；
    +inf 末桶命中 → 返回该桶下界 edges[-1]（保守口径，无法对 +inf 插值）。
    """
    total = sum(buckets)
    if total == 0:
        return 0.0
    rank = q * total
    if rank > total:
        rank = total
    cum = 0
    for i, count in enumerate(buckets):
        lo = edges[i - 1] if i > 0 else 0.0
        if i == len(buckets) - 1:          # +inf 末桶
            return float(lo)
        if rank <= cum + count:
            return lo + (rank - cum) / count * (edges[i] - lo)
        cum += count
    return float(edges[-1])  # 数学不可达（rank<=total 且末桶已 return）；防御性兜底


def sse_data_line_kind(line: bytes) -> str:
    """SSE data 行归类："done" / "content" / "finish" / "noise"。判定序：
    非 data: 前缀（注释行/event:/id:）→ noise；[DONE] → done；
    JSON 解析失败 → content（fail-open：宁可不重试，不误判合法流）；
    dict 且顶层 error 对象 → content（body error 帧视为 content，触发 priming flush 不重试）；
    dict + choices 非空 list 时：delta.content 非空 str / delta.tool_calls 真值 →
    content；choice.finish_reason 非 None（且无前两者）→ finish；
    其余（reasoning-only、空 delta、choices 为空的 usage 帧、data:null、非 dict JSON）→ noise。"""
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
    if body_has_error(data):
        return "content"
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
        return "finish"
    return "noise"


def body_has_error(parsed) -> bool:
    """判 JSON 解析值是否含顶层 error 对象（OpenAI 错误 schema）。
    isinstance(parsed, dict) 且 isinstance(parsed.get("error"), dict) → True；
    其余（非 dict、error 键缺失、error 为 str/null/list）→ False。
    不做字符串匹配：content 文本出现 "error" 字样不误判。"""
    if not isinstance(parsed, dict):
        return False
    error_val = parsed.get("error")
    return isinstance(error_val, dict)


def sse_line_has_usage(line: bytes) -> bool:
    """判 SSE data 行是否携带非空 usage 帧（独立 usage 帧 choices=[] 与 ride-on finish
    帧都覆盖）；DONE/非 JSON/非 data 行 → False。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    usage = data.get("usage")
    return isinstance(usage, dict) and bool(usage)


def sse_line_usage(line: bytes):
    """返回 SSE data 行中的 usage dict，无 usage 帧返回 None。

    解析逻辑与 sse_line_has_usage 同序：非 data: 前缀/[DONE]/非 JSON/非 dict →
    None；usage 非空 dict → 返回该 dict；其余 → None。
    独立 usage 帧（choices=[]）与 ride-on finish 帧均覆盖。
    """
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return None
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) and usage else None


def usage_dict_tokens(usage) -> tuple:
    """从已解析的 usage dict 提取 (prompt_tokens, completion_tokens, total_tokens)。

    非 dict → None；各字段取 int ≥0 原值，缺失/非 int/负数 → 0（best-effort）。
    零 IO、零 parse：入参必须是 json.loads 已产出的 dict（复用解析一次，R1）。
    """
    if not isinstance(usage, dict):
        return None

    def _int_field(key):
        value = usage.get(key)
        if isinstance(value, bool):  # bool 是 int 子类：JSON true/false 不是合法计数，归 0
            return 0
        return value if isinstance(value, int) and value >= 0 else 0

    return (_int_field("prompt_tokens"), _int_field("completion_tokens"),
            _int_field("total_tokens"))


def sse_line_extract_usage(line: bytes):
    """SSE data 行 usage 帧数值抽取：返回 (prompt_tokens, completion_tokens, total_tokens)。

    无 usage 帧（非 data: 前缀 / [DONE] / 非 JSON / 非 dict / usage 空）→ None。
    解析委托 sse_line_usage（单次 json.loads，与 sse_line_has_usage 同解析路径）；
    热路径上不重复调用本函数——_relay_sse 直接用 usage_dict_tokens 消费已解析 dict（R1）。
    """
    usage = sse_line_usage(line)
    if usage is None:
        return None
    return usage_dict_tokens(usage)


def sse_line_body_error(line: bytes) -> bool:
    """判 SSE data 行是否为 body error 帧。rstrip → 非 data: 前缀或 [DONE] →
    False；json.loads ValueError → False（fail-open 同 sse_line_has_usage）；
    否则 return body_has_error(data)。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return False
    return body_has_error(data)


_Outcome = collections.namedtuple("_Outcome", "category log_result counts_error capture tri_state")


def classify_outcome(status=None, synth_502=False, client_abort=False,
                     eof_without_done=False, poison_filtered=0,
                     body_error=False) -> _Outcome:
    """错误分类网关：输入场景标志 → 返回 (category, log_result, counts_error, capture, tri_state)。

    判定优先级 client_abort > synth_502 > body_error > eof_without_done > status 阈值；
    status=None 且无 flags → ValueError。
    v2 P4：tri_state 由 category 经 _outcome_tri_state 推导（"ok"|"degraded"|"failed"），
    供 daily outcome_* 三桶归因；纯函数 _outcome_tri_state 保留（字符串回退路径 + P3 测试锚）。
    """
    if client_abort:
        return _Outcome(CLASS_CLIENT_ABORT, "aborted", False, False,
                        _outcome_tri_state(CLASS_CLIENT_ABORT))
    if synth_502:
        return _Outcome(CLASS_UPSTREAM_FAULT, "error", True, True,
                        _outcome_tri_state(CLASS_UPSTREAM_FAULT))
    if body_error:
        return _Outcome(CLASS_BODY_ERROR, "body-err", True, True,
                        _outcome_tri_state(CLASS_BODY_ERROR))
    if eof_without_done:
        return _Outcome(CLASS_UPSTREAM_FAULT, "eof-without-done", False, True,
                        _outcome_tri_state(CLASS_UPSTREAM_FAULT))
    if status is None:
        raise ValueError("classify_outcome: status required when no flag is set")
    if status < 400:
        if poison_filtered > 0:
            return _Outcome(CLASS_POISON_FIXED, "ok", False, True,
                            _outcome_tri_state(CLASS_POISON_FIXED))
        return _Outcome(CLASS_OK, "ok", False, False,
                        _outcome_tri_state(CLASS_OK))
    if 400 <= status < 500:
        return _Outcome(CLASS_REQUEST_FAULT, "upstream-err", False, True,
                        _outcome_tri_state(CLASS_REQUEST_FAULT))
    # status >= 500
    return _Outcome(CLASS_UPSTREAM_FAULT, "upstream-err", False, True,
                    _outcome_tri_state(CLASS_UPSTREAM_FAULT))


def _outcome_tri_state(outcome) -> str:
    """classify_outcome category 字符串 → 三态（"ok"|"degraded"|"failed"）。

    P4 将把此映射内聚进 classify_outcome 返回的 _Outcome 新增 tri_state 字段；
    P3 先行用本函数落 daily outcome_* 桶（字段已由 DAILY_V2_FIELDS 就位）。
    """
    if outcome == CLASS_OK:
        return "ok"
    if outcome in (CLASS_POISON_FIXED, CLASS_CLIENT_ABORT):
        return "degraded"
    return "failed"


# --- TPM rate limiting（module-level state，全部由 TPM_LOCK 保护）---

_TpmWaiter = collections.namedtuple("_TpmWaiter", "key_id est enqueued_at model")

# collections.deque 是 C 类型无 __dict__，不能挂 .used 属性；Python 空子类有
# __dict__ 可承载 .used，而 len()/popleft/append/迭代语义与原 deque 完全一致
class _TpmBucket(collections.deque):
    pass

TPM_LOCK = threading.Condition()
TPM_BUCKETS: dict = {}                 # (key_id, model) -> _TpmBucket（deque 条目 + 附加属性 .used）
TPM_WAITERS: collections.deque = collections.deque()  # FIFO 排队（of _TpmWaiter）
_tpm_rejected: dict = {}               # (key_id, model) -> queue-full 拒绝计数（快照用）
_tpm_timeouts: dict = {}               # (key_id, model) -> queue-timeout 计数（快照用）


def tpm_key_id(auth_header):
    """从 Authorization 头提取 per-key 标识。

    无头 / 非 "Bearer " 前缀 / 空 token → None（= 不限流直通）；
    否则返回 token 的 sha256 hex 全位（内部桶键用全位防碰撞，
    快照对外只显前 12 位脱敏）。
    """
    if not auth_header:
        return None
    parts = auth_header.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    token = parts[1].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def estimate_request_tokens(body):
    """准入估算：max(1, int(len(body) * TPM_TOKEN_RATIO))；
    body 可解析为 JSON 且含 int max_tokens 时累加该值（输出预留）。
    非 JSON body 按裸长度估（fail-open），永不抛出。
    """
    if not body:
        return 1
    base = max(1, int(len(body) * TPM_TOKEN_RATIO))
    try:
        data = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        # 吞掉的是非 JSON 请求体（GET/表单/二进制属常态）：按裸长度估即可。
        return base
    if isinstance(data, dict):
        mt = data.get("max_tokens")
        if isinstance(mt, int) and not isinstance(mt, bool) and mt > 0:
            base += mt
    return base


def tpm_limit_for(model) -> int:
    """per-model 预算查询：env 表 → 内置默认表 → 全局 TPM_LIMIT。

    model 为 None / 空字符串 / 查不到 → 回退 TPM_LIMIT（全局默认）。"""
    if model:
        limit = TPM_LIMIT_BY_MODEL.get(model)
        if limit is not None:
            return limit
        limit = _TPM_LIMIT_BY_MODEL_BUILTIN.get(model)
        if limit is not None:
            return limit
    return TPM_LIMIT


def _tpm_prune(bucket, now: float) -> None:
    """裁剪 bucket 中 now - TPM_WINDOW_S 之前的条目并同步 .used。

    bucket 为 collections.deque（条目 (ts, tokens)，delta 可为负），
    .used 为其增量维护的窗口内 token 合计；popleft 时同步扣减。
    存储不变量：used 恒等于 deque 条目之和，不做下界修正——used 可为负
    （负值 = 窗口欠账，随负 settle 条目过期 prune 自愈）；下界钳位只发生
    在 tpm_snapshot 展示层。仅可在持 TPM_LOCK 时调用。
    """
    cutoff = now - TPM_WINDOW_S
    while bucket and bucket[0][0] < cutoff:
        _, tokens = bucket.popleft()
        bucket.used -= tokens


def _tpm_get_bucket(key_id: str, model):
    """取 (key_id, model) 的桶；不存在则惰性创建（cap 满时先驱逐空桶）。仅在 TPM_LOCK 内调用。"""
    k = (key_id, model)
    bucket = TPM_BUCKETS.get(k)
    if bucket is None:
        if len(TPM_BUCKETS) >= TPM_KEY_CAP:
            # 惰性驱逐：窗口空且无排队的桶（窗口自动过期，最旧优先）
            idle = [bkey for bkey, b in TPM_BUCKETS.items()
                    if b.used <= 0
                    and not any((w.key_id, w.model) == bkey for w in TPM_WAITERS)]
            for bkey in idle:
                del TPM_BUCKETS[bkey]
        bucket = _TpmBucket()
        bucket.used = 0
        TPM_BUCKETS[k] = bucket
    return bucket


def _tpm_remove_waiter(waiter) -> None:
    """把 waiter 从 TPM_WAITERS 中按**同一性**摘除（不在队列时为 no-op）。

    必须同一性而非值相等删除：_TpmWaiter 是 namedtuple，值全等的两个 waiter
    同队列时，deque.remove 会摘掉别人的那份（超时 waiter 误删仍在排队的请求）。
    仅可在持 TPM_LOCK 时调用。
    """
    for i, w in enumerate(TPM_WAITERS):
        if w is waiter:
            del TPM_WAITERS[i]
            return


def tpm_admit(key_id: str, est: int, model):
    """TPM 准入（与入队同原子）。返回 (status, qwait_ms)。

    status: "ok"（准入）/ "full"（队列满或 est 超预算且窗口非空闲，立即 429）/
            "timeout"（排队超时，429）。
    规则：
    - budget = tpm_limit_for(model)（per-model 表 → 内置默认 → TPM_LIMIT）。
    - est > budget → 空闲放行判定：prune 后 bucket.used <= 0 且无同桶 waiter →
      放行（超大请求占满窗口，后续排队至窗口滚过属预期）；否则 ("full", 0)
      并计 rejected（先 touch 建桶，拒绝计数可见）。
    - 窗口内充足 → 入桶 ("ok", 0)。
    - 否则入队 FIFO；仅队首 waiter 可被准入（严格 FIFO 防惊群）；
      wait 循环 TPM_LOCK.wait(timeout=min(1.0, remaining)) + deadline 检查
      （窗口过期靠 1s 粒度轮询，settle 时 notify_all 提前唤醒）。
    - 队满（len(TPM_WAITERS) >= TPM_QUEUE_MAX）→ ("full", 0) 不入队。
    - deadline 到仍未准入 → ("timeout", 0) 并自队列移除。
    排队期间不持有任何上游连接（hook 点在 _open_upstream 之前）。
    """
    budget = tpm_limit_for(model)
    started = time.time()
    with TPM_LOCK:
        bucket = _tpm_get_bucket(key_id, model)
        _tpm_prune(bucket, started)
        if est > budget:
            # 超大请求唯一例外：窗口空闲（used<=0）且无同桶排队 → 放行（决策 3）；
            # 否则硬拒并计 rejected（决策 4：先 touch 建桶，拒绝计数可见）。
            if bucket.used <= 0 and not any(
                    (w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS):
                bucket.append((started, est))
                bucket.used += est
                return ("ok", 0)
            _tpm_rejected[(key_id, model)] = \
                _tpm_rejected.get((key_id, model), 0) + 1
            return ("full", 0)
        if bucket.used + est <= budget:
            bucket.append((started, est))
            bucket.used += est
            return ("ok", 0)
        if len(TPM_WAITERS) >= TPM_QUEUE_MAX:
            _tpm_rejected[(key_id, model)] = \
                _tpm_rejected.get((key_id, model), 0) + 1
            return ("full", 0)
        waiter = _TpmWaiter(key_id=key_id, est=est, enqueued_at=started,
                            model=model)
        TPM_WAITERS.append(waiter)
        deadline = started + TPM_QUEUE_TIMEOUT_S
        try:
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    _tpm_timeouts[(key_id, model)] = \
                        _tpm_timeouts.get((key_id, model), 0) + 1
                    return ("timeout", 0)
                head = TPM_WAITERS[0] if TPM_WAITERS else None
                if head is waiter:
                    now = time.time()
                    bucket = _tpm_get_bucket(key_id, model)
                    _tpm_prune(bucket, now)
                    if bucket.used + est <= budget:
                        TPM_WAITERS.popleft()
                        bucket.append((now, est))
                        bucket.used += est
                        return ("ok", (now - started) * 1000)
                TPM_LOCK.wait(timeout=min(1.0, remaining))
        finally:
            # 超时/异常路径把自己从队列移除；已 popleft 的正常准入不会走到这。
            # 同一性摘除（_tpm_remove_waiter）：值相等的其它 waiter 不受牵连
            _tpm_remove_waiter(waiter)


def tpm_settle(key_id: str, est: int, actual: int, model) -> None:
    """结算：以实际 usage 校正窗口占用。

    prune 后追加校正条目 (now, delta)，delta = actual - est（可为负 = 退款）；
    used 存原始值 used + delta，与追加条目自此保持一致（可为负 = 窗口欠账，
    随条目过期 prune 自愈）。settle 后 notify_all 唤醒排队 waiter 重试准入。
    """
    now = time.time()
    with TPM_LOCK:
        bucket = TPM_BUCKETS.get((key_id, model))
        if bucket is None:
            return  # 该 (key, model) 从未准入（不可能路径，防御处理）
        _tpm_prune(bucket, now)
        delta = actual - est
        if delta != 0:
            bucket.append((now, delta))
        bucket.used = bucket.used + delta
        TPM_LOCK.notify_all()


def tpm_snapshot() -> dict:
    """TPM 状态快照（/api/tpm_stats 端点用）。

    返回 {"config": {"limit", "window_s", "queue_max", "limit_by_model"},
          "queue_total", "buckets": [...]}；
    bucket 条目 {"key"（sha256: 前缀 + 前 12 位 hex）, "model", "used",
                 "remaining", "queued", "rejected", "timeouts"}——key 脱敏，
    无原始 key 泄漏。
    展示层钳位：存储 used 可为负（窗口欠账，见 _tpm_prune 不变量），
    对外 used 取 max(0, used)，remaining 按 per-model 预算计算，避免暴露/展示负值。
    """
    now = time.time()
    with TPM_LOCK:
        buckets = []
        for (key_id, model), bucket in TPM_BUCKETS.items():
            _tpm_prune(bucket, now)
            if bucket.used <= 0 and not any(
                    (w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS):
                continue  # 空桶不展示（噪声）
            buckets.append({
                "key": "sha256:" + key_id[:12],
                "model": model,
                "used": max(0, bucket.used),
                "remaining": max(0, tpm_limit_for(model) - bucket.used),
                "queued": sum(1 for w in TPM_WAITERS
                              if (w.key_id, w.model) == (key_id, model)),
                "rejected": _tpm_rejected.get((key_id, model), 0),
                "timeouts": _tpm_timeouts.get((key_id, model), 0),
            })
        return {
            "config": {
                "limit": TPM_LIMIT,
                "window_s": TPM_WINDOW_S,
                "queue_max": TPM_QUEUE_MAX,
                "limit_by_model": TPM_LIMIT_BY_MODEL,
            },
            "queue_total": len(TPM_WAITERS),
            "buckets": buckets,
        }


def empty_stream_should_retry(budget: int) -> bool:
    """空流重试决策：预算 > 0 时允许重试。调用点 :656 由 final=(EMPTY_RETRY_MAX < 1)
    改为 final=not empty_stream_should_retry(EMPTY_RETRY_MAX)，语义等价。"""
    return budget > 0


def header_timeout_should_retry(budget: int) -> bool:
    """头超时重试决策：budget > 0 时允许重试。调用点在 _proxy_relay 头阶段
    首呼的 (socket.timeout, RemoteDisconnected, ConnectionResetError) catch 分支，
    覆盖头阶段三类可重试故障（阻塞超时/上游 FIN-close/上游 RST）。"""
    return budget > 0


def _snapshot_text(raw: bytes, cap: int) -> str:
    """字节快照截断：utf-8 replace 解码，超 cap 截断追加 trunc 标记。"""
    if raw is None:
        return ""
    text = raw.decode("utf-8", "replace")
    if len(text) <= cap:
        return text
    return text[:cap] + "\u2026[truncated]"


def record_error_event(kind, model=None, path=None, upstream_status=None, exc=None,
                       body=None, response=None, filtered=0, retried=0,
                       retry_reason="") -> None:
    """记录一条错误留痕事件到 ERROR_EVENTS 环。

    CAPTURE_ERRORS 为 False 时直接返回（off=只走现有计数，不留痕不抓 body）。
    条目 schema：{id, ts, kind, category, model, path, upstream_status,
    exc(<=200ch), body(<=4096ch), response(<=2048ch), filtered, retried,
    retry_reason}；id 进程内单调递增（ERROR_LOCK 内）。"""
    if not CAPTURE_ERRORS:
        return
    global _ERROR_EVENT_SEQ
    exc_text = ""
    if exc is not None:
        exc_text = re.sub(r"\s+", "_",
                          ("%s: %s" % (type(exc).__name__, exc)).strip())[:200]
    with ERROR_LOCK:
        _ERROR_EVENT_SEQ += 1
        ERROR_EVENTS.append({
            "id": _ERROR_EVENT_SEQ,
            "ts": time.time(),
            "kind": kind,
            "category": _KIND_CATEGORY.get(kind),
            "model": model,
            "path": path,
            "upstream_status": upstream_status,
            "exc": exc_text or None,
            "body": _snapshot_text(body, BODY_SNAPSHOT_CAP) if body else None,
            "response": _snapshot_text(response, RESPONSE_SNIPPET_CAP) if response else None,
            "filtered": filtered,
            "retried": retried,
            "retry_reason": retry_reason or None,
        })


def logs_snapshot(cursor=None, tail=None) -> dict:
    """日志分页纯函数：从 LOG_RING 取副本后计算，可单测。

    返回 dict：{"lines": [{"seq": int, "line": str}], "next_cursor": int,
    "oldest_seq": int, "ring_max": 1000}。

    参数校验（非法 → ValueError）：
    - tail 非 int / tail < 1 / tail > LOG_RING_MAX → ValueError
    - cursor 非 int → ValueError
    - cursor 与 tail 同给 → ValueError
    """
    if cursor is not None and tail is not None:
        raise ValueError("cursor and tail are mutually exclusive")
    if tail is not None:
        if not isinstance(tail, int) or tail < 1 or tail > LOG_RING_MAX:
            raise ValueError("tail must be 1..%d" % LOG_RING_MAX)
    if cursor is not None:
        if not isinstance(cursor, int):
            raise ValueError("cursor must be an integer")
    with LOG_LOCK:
        ring_snap = list(LOG_RING)
    if not ring_snap:
        return {"lines": [], "next_cursor": 0, "oldest_seq": 0, "ring_max": LOG_RING_MAX}

    oldest_seq = ring_snap[0]["seq"]
    if tail is not None:
        lines = ring_snap[-tail:]
        return {"lines": lines, "next_cursor": lines[-1]["seq"] if lines else 0,
                "oldest_seq": oldest_seq, "ring_max": LOG_RING_MAX}
    if cursor is not None:
        # cursor=C 返回 seq>C 最多 LOG_PAGE_MAX 条 oldest->newest
        page = [l for l in ring_snap if l["seq"] > cursor][:LOG_PAGE_MAX]
        next_cursor = page[-1]["seq"] if page else cursor
        return {"lines": page, "next_cursor": next_cursor,
                "oldest_seq": oldest_seq, "ring_max": LOG_RING_MAX}
    # 缺省 tail=LOG_TAIL_DEFAULT
    lines = ring_snap[-LOG_TAIL_DEFAULT:]
    return {"lines": lines, "next_cursor": lines[-1]["seq"] if lines else 0,
            "oldest_seq": oldest_seq, "ring_max": LOG_RING_MAX}


class _EmptyStream(Exception):
    """priming EOF 仍无 content/[DONE]：携带 filtered 计数与已滤毒缓冲行。"""
    def __init__(self, filtered: int, lines: list, reason: str = "eof-priming"):
        super().__init__("empty upstream sse stream")
        self.filtered = filtered
        self.lines = lines
        self.reason = reason


HOP_HEADERS = {"connection", "keep-alive", "proxy-connection",
               "te", "trailer", "transfer-encoding", "upgrade"}
STRIP_HEADERS = HOP_HEADERS | {"host", "content-length", "accept-encoding"}

# 锁纪律（现状审计，2026-09-30 文档化）：
#   STATS_LOCK — 保护 STATS（含 daily/daily_by_model 桶）/ RECENT_REQUESTS / EVENTS /
#                POISON_PREVIEWS / _stats_dirty
#   _CFG_LOCK  — 保护 UPSTREAM_BASE / _upstream_source / CAPTURE_ERRORS（只护写；
#                UPSTREAM_BASE 读侧无锁：Python 引用赋值原子，读线程看到任一完整旧值）
#   ERROR_LOCK — 保护 ERROR_EVENTS / _ERROR_EVENT_SEQ
#   LOG_LOCK   — 保护 LOG_RING / _LOG_SEQ
#   PROBE_LOCK — P4 新增，保护 _PROBE_STATE；独立于 STATS_LOCK：探测线程与请求线程
#                共享 STATS 时只经 STATS_LOCK 短持锁更新计数器，绝不持锁做网络 IO。
_CFG_LOCK = threading.Lock()
PROBE_LOCK = threading.Lock()
STATS_LOCK = threading.Lock()
STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0,
         "empty_retries_total": 0, "eof_without_done_total": 0,
         "finish_retries_total": 0, "header_retries_total": 0,
         "active": 0, "daily": {}, "daily_by_model": {},
         # v2 P2 速度观测（内存态，不持久化——save/load 白名单均不含此四键，重启清零）
         "ttfb_hist": {},    # {"<model>": [0]*9}，模型 cap 复用 BY_MODEL_CAP（R4：仅按模型，不按阶段）
         "phase_ms": {"connect": [0] * 9, "headers": [0] * 9, "body": [0] * 9},  # 全局三阶段（R4：不按模型）
         "rates": {"bytes_out_total": 0, "chunks_total": 0, "tokens_total": 0,
                   "window_start": time.monotonic(), "window_bytes": 0,
                   "window_chunks": 0, "window_tokens": 0},  # 60s 滑动窗（P2 bytes/chunks；tokens P3 填）
         "stalls_total": 0}
_stats_dirty = False  # STATS_LOCK 保护：计数落盘脏标记（SIGTERM/60s 脏刷消费）

# v2 P4：probe 主动探测状态。独立于 STATS（失败零副作用：不写 STATS 计数、
# 不触发 _record_request、不污染 histogram），仅 EVENTS 告警 append 走 STATS_LOCK。
PROBE_ENABLED = True  # _CFG_LOCK 护写；读侧无锁（bool 引用赋值原子，同 CAPTURE_ERRORS）
_PROBE_STATE = {      # PROBE_LOCK 保护（探测线程写 / probe_state_snapshot 读）
    "last_probe_ts": 0.0,           # time.time() 最近一次 probe 尝试（0=从未）
    "last_probe_ok": False,
    "last_probe_latency_ms": 0.0,
    "consecutive_failures": 0,
    "last_failure_alert_ts": 0.0,   # 连续失败告警去抖时间戳
    "last_spike_alert_ts": 0.0,     # 延迟突增告警去抖时间戳（同类去抖，两桶独立）
    "history": collections.deque(maxlen=PROBE_HISTORY_MAX),  # 成功 probe 延迟（ms）历史
}
STARTED_AT = time.time()
RECENT_REQUESTS = collections.deque(maxlen=100)  # {"ts","method","path","status","dur_ms","filtered","model","rid","upstream_host","ttfb_ms","stream","tokens","bytes_out","chunks","outcome"}
POISON_PREVIEWS = collections.deque(maxlen=20)   # {"ts","preview"} 最近剥除的 record 预览
EVENTS = collections.deque(maxlen=100)  # {"ts","kind":"proxy"|"upstream"|"retry"|"probe_alert","model","status"(,"reason" 仅 probe_alert)}
# 单一全局事件流（kind 区分）而非按 (day,model,kind) 分环：per-key 环形几十个 deque
# 持久化/清洗成本高，单流 maxlen=100 硬上界等价满足"每 key 有界"，tooltip 按需过滤。

CAPTURE_ERRORS = False  # _CFG_LOCK 守护
MODEL_PRICING: dict = {}  # v2 P3：model 价目表（可选 cost 估算），main() 启动时置值，之后只读
ERROR_EVENTS = collections.deque(maxlen=ERROR_RING_MAX)
ERROR_LOCK = threading.Lock()
_ERROR_EVENT_SEQ = 0  # ERROR_LOCK 内递增

LOG_RING = collections.deque(maxlen=LOG_RING_MAX)
LOG_LOCK = threading.Lock()
_LOG_SEQ = 0  # LOG_LOCK 内递增


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


def persist_upstream(base: str, path: str, capture_errors: bool = False) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base, "capture_errors": capture_errors},
                  fh, ensure_ascii=False)
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


def normalize_null_assistant_content(body):
    """把 messages 内 role=assistant 且显式 content:null 的消息归一为 content:""。

    其余一切输入原样返回：空 body、非 JSON（ValueError 系）、顶层非 dict、
    messages 非 list、无改动时返回**原 bytes 对象**（byte-identical 透传，
    保上游 prompt cache）；序列化异常 fail-open 返回原 body，永不抛出。
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return body
    changed = False
    for msg in data["messages"]:
        if (isinstance(msg, dict) and msg.get("role") == "assistant"
                and "content" in msg and msg["content"] is None):
            msg["content"] = ""
            changed = True
    if not changed:
        return body
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body


def today_key() -> str:
    """当日日期桶 key（本地时区）；模块级函数便于测试 patch 模拟跨天。"""
    return time.strftime("%Y-%m-%d", time.localtime())


def range_bounds(today: str, range_key: str) -> tuple:
    """时间维度窗口 [start, end]（ISO 日期闭区间；时区口径由调用方传入的 today 决定）。

    3d/7d = 含今日的滑动窗口；mtd = [当月1日, today]；last_month = 上个自然月整月。
    ISO 日期字符串字典序即时间序（_prune_daily 同性质）。非法 range_key → ValueError。
    """
    d = datetime.date.fromisoformat(today)
    if range_key == "3d":
        start, end = d - datetime.timedelta(days=2), d
    elif range_key == "7d":
        start, end = d - datetime.timedelta(days=6), d
    elif range_key == "mtd":
        start, end = d.replace(day=1), d
    elif range_key == "last_month":
        last_month_end = d.replace(day=1) - datetime.timedelta(days=1)
        start, end = last_month_end.replace(day=1), last_month_end
    else:
        raise ValueError("unknown range_key: %r" % (range_key,))
    return (start.isoformat(), end.isoformat())


def aggregate_daily_range(daily: dict, start: str, end: str) -> dict:
    """窗口 [start, end]（含端点）内 daily 桶逐字段求和；缺字段按 0。

    返回 len(_DAILY_FIELDS)+1 键 dict：_DAILY_FIELDS 全字段（v2 P3 起 16 字段）+ days=命中桶数。
    """
    out = {field: 0 for field in _DAILY_FIELDS}
    days = 0
    for key, bucket in daily.items():
        if not (start <= key <= end):
            continue
        days += 1
        for field in _DAILY_FIELDS:
            out[field] += bucket.get(field, 0)
    out["days"] = days
    return out


def range_stats(daily: dict, today: str = None) -> dict:
    """四个时间维度的聚合计划：{"stats": {key: aggregate}, "bounds": {key: [start, end]}}。

    today 缺省 today_key()（测试可注入固定日期）；stats_snapshot 据此填充加性字段。
    """
    if today is None:
        today = today_key()
    stats, bounds = {}, {}
    for range_key in RANGE_KEYS:
        start, end = range_bounds(today, range_key)
        stats[range_key] = aggregate_daily_range(daily, start, end)
        bounds[range_key] = [start, end]
    return {"stats": stats, "bounds": bounds}


def _prune_daily(daily: dict) -> dict:
    """按日期 key 降序保留最近 DAILY_RETENTION_DAYS 天（ISO 日期字符串排序即时间序）。
    value 形状无关（平 dict 按 key prune），daily 与 daily_by_model 共用；
    调用方须持 STATS_LOCK（对内存态调用时）。"""
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
    global _LOG_SEQ
    with LOG_LOCK:
        _LOG_SEQ += 1
        LOG_RING.append({"seq": _LOG_SEQ, "line": msg})


def save_stats_counters(path: str) -> None:
    """把累计计数、按天分桶与当前上游端点全量写入持久化文件（SIGTERM / set_upstream_base 共用）。"""
    with STATS_LOCK:
        counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                          "empty_retries_total", "eof_without_done_total",
                                          "finish_retries_total", "header_retries_total")}
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
        daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                          for d, models in STATS["daily_by_model"].items()}
        events = [dict(e) for e in EVENTS]  # 逐条浅拷贝：磁盘与内存一致（≤100 条）
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
        probe_enabled = PROBE_ENABLED
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "probe_enabled": probe_enabled,
                   "model_pricing": MODEL_PRICING,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 events=events)},
                  fh, ensure_ascii=False)
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
    for key in ("requests_total", "filtered_total", "errors_total", "empty_retries_total",
                "eof_without_done_total", "finish_retries_total", "header_retries_total"):
        value = stats.get(key) if isinstance(stats, dict) else None
        out[key] = value if isinstance(value, int) and value >= 0 else 0
    return out


def load_capture_errors(path: str) -> bool:
    """从持久化文件读 capture_errors；缺/损坏/非 bool → False。"""
    data = _load_persist_file(path)
    val = data.get("capture_errors") if isinstance(data, dict) else None
    return val if isinstance(val, bool) else False


def load_model_pricing(path: str) -> dict:
    """从持久化文件顶层读 model_pricing；缺/损坏/非 dict → {}。
    env seam CTYUN_MODEL_PRICING（JSON 字符串）优先级更高，由 main() 覆盖。"""
    data = _load_persist_file(path)
    val = data.get("model_pricing") if isinstance(data, dict) else None
    return val if isinstance(val, dict) else {}


def resolve_probe_enabled(env_val: str, persisted: bool) -> bool:
    """probe_enabled 解析：env > persist > 默认（True）。

    env_val（CTYUN_PROBE_ENABLED）："1"/"true"/"yes"/"on" → True；
    "0"/"false"/"no"/"off" → False；其余（含空串/垃圾值）→ 落 persisted 值。
    """
    if env_val:
        normalized = env_val.strip().lower()
        if normalized in ("1", "true", "yes", "on"):
            return True
        if normalized in ("0", "false", "no", "off"):
            return False
    return persisted


def load_probe_enabled(path: str) -> bool:
    """从持久化文件顶层读 probe_enabled；缺/损坏/非 bool → True（默认开）。"""
    data = _load_persist_file(path)
    val = data.get("probe_enabled") if isinstance(data, dict) else None
    return val if isinstance(val, bool) else True


def set_probe_enabled(enabled: bool) -> None:
    """POST /api/probe 落库：切 PROBE_ENABLED + 立即全量落盘（跨重启保留）。"""
    global PROBE_ENABLED
    with _CFG_LOCK:
        PROBE_ENABLED = enabled
    # save_stats_counters 内部也要拿 _CFG_LOCK：必须在锁外调用（同 set_capture_errors
    # 死锁注释——非重入锁，嵌套即同线程死锁）
    save_stats_counters(PERSIST_PATH)


_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream",
                 "retries", "eof_without_done", "header_retries")

# v2 P3 起用（P1 先声明）：daily/daily_by_model 16 字段 schema。
# P3 切换前零引用；load/save 循环各按迭代时的 _DAILY_FIELDS 白名单，
# 切换后旧格式自动补 0，新格式被旧版加载时自动丢新字段。
DAILY_V2_FIELDS = _DAILY_FIELDS + ("tokens_prompt", "tokens_completion",
                                   "bytes_out", "stream_requests",
                                   "ttfb_sum_ms", "ttfb_count",
                                   "outcome_ok", "outcome_degraded",
                                   "outcome_failed")

# v2 P3：daily/daily_by_model 切换 16 字段 schema（DAILY_V2_FIELDS 已在 P1 声明，见下）。
# load/save 循环与四处 entry 创建均按 _DAILY_FIELDS 迭代 → 切换后旧 7 字段桶自动补 0，
# 新 16 字段桶被旧版二进制读入自动丢新字段（R2 双向 degrade，无需版本号）。
_DAILY_FIELDS = DAILY_V2_FIELDS


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


def load_daily_by_model_buckets(path: str) -> dict:
    """读日期→模型→_DAILY_FIELDS 矩阵；缺/损坏/legacy 无 daily_by_model 键 → {}。"""
    stats = _load_persist_file(path).get("stats")
    raw = stats.get("daily_by_model") if isinstance(stats, dict) else None
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, models in raw.items():
        # 外层 ISO 日期容错：fromisoformat + round-trip（拒 "20260101"/"2026-1-1"
        # 等非规范形；_prune_daily 依赖 ISO 字典序排序，坏 key 必须挡在内存外）
        try:
            d = datetime.date.fromisoformat(key)
        except (ValueError, TypeError):
            continue
        if str(d) != key or not isinstance(models, dict):
            continue
        bucket = {}
        for model, entry in models.items():
            # 内层逐模型清洗 + BY_MODEL_CAP 截断：按文件出现序保留前 32，与运行时
            # "len < BY_MODEL_CAP 才插新键"（_record_request/_record_empty_retry）语义对齐
            if not isinstance(entry, dict) or len(bucket) >= BY_MODEL_CAP:
                continue
            clean = {}
            for field in _DAILY_FIELDS:  # 同 load_daily_buckets 逐字段规则
                value = entry.get(field)
                clean[field] = value if isinstance(value, int) and value >= 0 else 0
            bucket[model] = clean
        out[key] = bucket
    return out


def load_stats_events(path: str) -> list:
    """读 stats.events（oldest→newest，≤100 条）；缺/损坏/legacy 无键 → []。

    kind 白名单含 probe_alert（v2 P4）：旧版本二进制读含 probe_alert 的文件时
    白名单不命中 → 整条 continue 丢弃（graceful degrade，R2），不会崩。
    reason 键仅 probe_alert 携带：新版本读入保留（合法 str 且 ≤200 字符），
    其余 kind 不输出该键。
    """
    stats = _load_persist_file(path).get("stats")
    raw = stats.get("events") if isinstance(stats, dict) else None
    if not isinstance(raw, list):
        return []
    out = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind, ts = entry.get("kind"), entry.get("ts")
        model, status = entry.get("model"), entry.get("status")
        if kind not in ("proxy", "upstream", "retry", "probe_alert"):
            continue
        if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts < 0:
            continue
        if model is not None and (not isinstance(model, str) or not model or len(model) > 200):
            continue
        if status is not None and (isinstance(status, bool) or not isinstance(status, int)
                                   or not 100 <= status <= 599):
            continue
        item = {"ts": ts, "kind": kind, "model": model, "status": status}
        reason = entry.get("reason")
        if kind == "probe_alert" and isinstance(reason, str) and reason \
                and len(reason) <= 200:
            item["reason"] = reason
        out.append(item)
    return out[-100:]  # 与 deque maxlen 对齐，只留最新


def _latency_p90(samples) -> float:
    """延迟样本（ms，可迭代）的 P90（最近秩法：idx = int(0.9 * (n - 1))）。
    空序列 → 0.0。样本上限 PROBE_HISTORY_MAX，排序成本可忽略（每成功 probe 一次）。"""
    if not samples:
        return 0.0
    ordered = sorted(samples)
    return float(ordered[int(0.9 * (len(ordered) - 1))])


def _probe_spike_due(history, factor: float = PROBE_LATENCY_SPIKE_FACTOR) -> bool:
    """延迟突增判定：最近 20 次成功 probe 的 P90 > 全历史 P90 × factor 且样本 ≥ 20。

    history：成功 probe 延迟（ms）deque（新在尾）；len < 20 → False（样本不足不告警）。
    """
    if len(history) < 20:
        return False
    recent = list(history)[-20:]
    baseline_p90 = _latency_p90(history)
    if baseline_p90 <= 0:
        return False
    return _latency_p90(recent) > baseline_p90 * factor


def _probe_once(host_base: str) -> tuple:
    """单轮 probe：向 host_base 根 path 发 HEAD（独立连接，超时 PROBE_TIMEOUT_S）。

    返回 (ok, latency_ms)：ok = HTTP status < 500；网络异常（OSError/http.client.HTTPException）
    视为失败，latency 取整个尝试耗时。不解析响应体（spec Exclusions）。
    零副作用：不写 STATS、不触发 _record_request、不持任何锁做网络 IO。
    """
    parsed = urllib.parse.urlsplit(host_base)
    path = parsed.path.rstrip("/") or "/"
    t_start = time.monotonic()
    conn = None
    try:
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(parsed.hostname, parsed.port,
                                               timeout=PROBE_TIMEOUT_S)
        else:
            conn = http.client.HTTPConnection(parsed.hostname, parsed.port,
                                              timeout=PROBE_TIMEOUT_S)
        conn.request("HEAD", path)
        resp = conn.getresponse()
        resp.read()  # 排空（HEAD 无 body；保证连接可复用语义完整）
        ok = resp.status < 500
    except (OSError, http.client.HTTPException):
        # 吞掉的是连接失败/超时/对端 RST——probe 语义即"不可达=失败"，
        # 单条 except 已覆盖全部可预期异常，无其他路径可达。
        ok = False
    finally:
        if conn is not None:
            try:
                conn.close()
            except OSError:
                # 吞掉的是连接关闭阶段对端已断开（close 二次清理）：probe 结果已定，
                # 关闭失败不影响成败判定，无其他路径可达。
                pass
    latency_ms = (time.monotonic() - t_start) * 1000
    return ok, latency_ms


def _probe_loop() -> None:
    """probe daemon 线程（main() 恒启动一次；开关由每轮 flag 检查控制）。

    probe_enabled=False 时空转：sleep 间隔 + 检查开关，零网络 IO 零副作用——
    运行时 POST /api/probe 切 on/off 无需管理线程生命周期（裁决见 Anchor A4）。
    每轮：_probe_once → PROBE_LOCK 内更新 _PROBE_STATE；连续失败 ≥
    PROBE_FAILURE_THRESHOLD 且距上次同类告警 ≥ PROBE_ALERT_DEBOUNCE_S → EVENTS
    追加 probe_alert（STATS_LOCK，与 _record_empty_retry 同款模式）；成功清零
    连续失败并累积延迟历史；延迟突增（_probe_spike_due）同类去抖告警。
    告警之外不碰 STATS（R3 零副作用，ProbeNoSideEffectTest 锚死）。
    """
    global _stats_dirty
    while True:
        time.sleep(PROBE_INTERVAL_S)
        if not PROBE_ENABLED:
            continue
        ok, latency_ms = _probe_once(UPSTREAM_BASE)
        alert_reason = None
        with PROBE_LOCK:
            state = _PROBE_STATE
            state["last_probe_ts"] = time.time()
            state["last_probe_ok"] = ok
            state["last_probe_latency_ms"] = round(latency_ms, 1)
            if ok:
                state["consecutive_failures"] = 0
                state["history"].append(round(latency_ms, 1))
                if _probe_spike_due(state["history"]):
                    now = time.time()
                    if now - state["last_spike_alert_ts"] >= PROBE_ALERT_DEBOUNCE_S:
                        state["last_spike_alert_ts"] = now
                        alert_reason = "latency_spike"
            else:
                state["consecutive_failures"] += 1
                if state["consecutive_failures"] >= PROBE_FAILURE_THRESHOLD:
                    now = time.time()
                    if now - state["last_failure_alert_ts"] >= PROBE_ALERT_DEBOUNCE_S:
                        state["last_failure_alert_ts"] = now
                        alert_reason = "consecutive_failures"
        if alert_reason is not None:
            with STATS_LOCK:
                EVENTS.append({"ts": time.time(), "kind": "probe_alert",
                               "model": None, "status": None,
                               "reason": alert_reason})
                _stats_dirty = True


def probe_state_snapshot() -> dict:
    """/api/health 用：spec 定死 6 键（host/last_probe_ts/last_probe_ok/
    last_probe_latency_ms/consecutive_failures/probe_enabled）。
    UPSTREAM_BASE 读侧无锁（引用赋值原子，同 _proxy 惯例）。"""
    with PROBE_LOCK:
        state = _PROBE_STATE
        snap = {
            "host": urllib.parse.urlparse(UPSTREAM_BASE).netloc,
            "last_probe_ts": state["last_probe_ts"],
            "last_probe_ok": state["last_probe_ok"],
            "last_probe_latency_ms": state["last_probe_latency_ms"],
            "consecutive_failures": state["consecutive_failures"],
            "probe_enabled": PROBE_ENABLED,
        }
    return snap


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
        _safe_log_stderr("ctyun-stream-fix-proxy: stats flush failed: %s" % exc)


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
        persist_upstream(base, PERSIST_PATH, capture_errors=CAPTURE_ERRORS)
    # save_stats_counters 内部也要拿 _CFG_LOCK：必须在锁外调用，否则同线程
    # 非重入死锁（admin 线程挂死且 SIGTERM 退出时同样卡锁）。
    save_stats_counters(PERSIST_PATH)


def set_capture_errors(enabled: bool) -> None:
    global CAPTURE_ERRORS
    with _CFG_LOCK:
        CAPTURE_ERRORS = enabled
        persist_upstream(UPSTREAM_BASE, PERSIST_PATH, capture_errors=enabled)
    # save_stats_counters 内部也要拿 _CFG_LOCK：必须在锁外调用（同 :449-451 死锁注释）
    save_stats_counters(PERSIST_PATH)


def _record_request(method: str, path: str, status: int, dur_ms: float,
                    filtered: int, model=None, error: bool = False,
                    rid=None, upstream_host=None, ttfb_ms=None, stream=None,
                    tokens=None, bytes_out: int = 0, outcome=None,
                    chunks: int = 0,
                    tokens_prompt: int = 0, tokens_completion: int = 0,
                    phase_ms: dict = None) -> None:
    global _stats_dirty
    # v2 P4：outcome 兼容两形态——_Outcome namedtuple（落桶用 tri_state 字段）或
    # category 字符串（旧调用点/测试，回退 _outcome_tri_state 纯函数）。
    # RECENT_REQUESTS 恒存 category 字符串（JSON 可序列化 + 展示口径不变）。
    tri_state = None
    outcome_category = None
    if outcome is not None:
        if hasattr(outcome, "tri_state"):
            tri_state = outcome.tri_state
            outcome_category = outcome.category
        else:
            tri_state = _outcome_tri_state(outcome)
            outcome_category = outcome
    with STATS_LOCK:
        if error:
            STATS["errors_total"] += 1
        STATS["filtered_total"] += filtered
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                # v2 P3：16 字段 schema（DAILY_V2_FIELDS），与另三处创建点形状同步
                entry_dm = day_models[model] = dict.fromkeys(_DAILY_FIELDS, 0)
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["requests"] += 1
                entry_dm["filtered"] += filtered
                if error:
                    entry_dm["errors_proxy"] += 1
                elif status >= 500:
                    entry_dm["errors_upstream"] += 1
            # v2 P3：dm entry 增量（tokens/bytes/stream/延迟/三态归因）；
            # 模型数达 BY_MODEL_CAP 时 entry_dm 为 None——增量块必须整体守卫
            if entry_dm is not None:
                if tokens_prompt > 0:
                    entry_dm["tokens_prompt"] += tokens_prompt
                if tokens_completion > 0:
                    entry_dm["tokens_completion"] += tokens_completion
                if bytes_out > 0:
                    entry_dm["bytes_out"] += bytes_out
                if stream:
                    entry_dm["stream_requests"] += 1
                if ttfb_ms is not None:
                    entry_dm["ttfb_sum_ms"] += round(ttfb_ms)
                    entry_dm["ttfb_count"] += 1
                if tri_state is not None:
                    entry_dm["outcome_" + tri_state] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), dict.fromkeys(_DAILY_FIELDS, 0))
        bucket["requests"] += 1
        bucket["filtered"] += filtered
        if error:
            bucket["errors_proxy"] += 1
        elif status >= 500:
            bucket["errors_upstream"] += 1
        # v2 P3：daily 总桶增量（与 dm entry 同口径）
        if tokens_prompt > 0:
            bucket["tokens_prompt"] += tokens_prompt
        if tokens_completion > 0:
            bucket["tokens_completion"] += tokens_completion
        if bytes_out > 0:
            bucket["bytes_out"] += bytes_out
        if stream:
            bucket["stream_requests"] += 1
        if ttfb_ms is not None:
            bucket["ttfb_sum_ms"] += round(ttfb_ms)
            bucket["ttfb_count"] += 1
        if tri_state is not None:
            bucket["outcome_" + tri_state] += 1
        if error or status >= 500:
            # 分类优先级与计数一致（error 分支胜过 status>=500）：error=True → proxy，
            # 其余 status>=500 → upstream；499 中断两边都不入流（同计数口径）。
            EVENTS.append({"ts": time.time(), "kind": "proxy" if error else "upstream",
                           "model": model, "status": status})
        RECENT_REQUESTS.append({"ts": time.time(), "method": method, "path": path,
                                "status": status, "dur_ms": round(dur_ms, 1),
                                "filtered": filtered, "model": model,
                                "rid": rid, "upstream_host": upstream_host,
                                "ttfb_ms": ttfb_ms, "stream": stream,
                                "tokens": tokens, "bytes_out": bytes_out,
                                "chunks": chunks, "outcome": outcome_category})
        # v2 P2：histogram 桶更新与 60s rates 窗口滚动（每请求一次，均在本锁内；R1/R4）
        if ttfb_ms is not None and model:
            ttfb_hist = STATS["ttfb_hist"]
            if model not in ttfb_hist and len(ttfb_hist) < BY_MODEL_CAP:
                ttfb_hist[model] = [0] * 9
            if model in ttfb_hist:
                ttfb_hist[model][bisect.bisect_right(HIST_BUCKETS_MS, ttfb_ms)] += 1
        if phase_ms:
            for name in ("connect", "headers", "body"):
                STATS["phase_ms"][name][
                    bisect.bisect_right(HIST_BUCKETS_MS, phase_ms[name])] += 1
        now_mono = time.monotonic()
        rates = STATS["rates"]
        if now_mono - rates["window_start"] >= 60.0:
            # 60s 滑动窗滚动：仅新请求进入且窗口过期时重置一次（spec P2）
            rates["window_start"] = now_mono
            rates["window_bytes"] = 0
            rates["window_chunks"] = 0
            rates["window_tokens"] = 0
        rates["bytes_out_total"] += bytes_out
        rates["chunks_total"] += chunks
        rates["window_bytes"] += bytes_out
        rates["window_chunks"] += chunks
        if tokens:  # P3 填 tokens；P2 阶段恒 None → 不累计
            rates["tokens_total"] += tokens
            rates["window_tokens"] += tokens
        _stats_dirty = True


def _record_poison_preview(raw: bytes) -> None:
    preview = raw.decode("utf-8", "replace")
    if len(preview) > 200:
        preview = preview[:200]
    with STATS_LOCK:
        POISON_PREVIEWS.append({"ts": time.time(), "preview": preview})


def _record_empty_retry(model=None, reason: str = "eof-priming") -> None:
    """空流重试计数：STATS 总量 + 当日桶 + daily_by_model。
    finish-no-usage 形态额外累加 finish_retries_total。
    entry/桶形状必须与 _record_request 同步：dm entry 同为 16 字段（DAILY_V2_FIELDS，本函数无
    error/status 参数，errors_* 仅保形状不归因）；旧持久化桶经 load_daily_buckets
    的 _DAILY_FIELDS 清洗已补键。形状不同步时 += 直接 KeyError。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["empty_retries_total"] += 1
        if reason == "finish-no-usage":
            STATS["finish_retries_total"] += 1
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                # v2 P3：16 字段 schema（与 _record_request 创建点形状同步）
                entry_dm = day_models[model] = dict.fromkeys(_DAILY_FIELDS, 0)
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["retries"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), dict.fromkeys(_DAILY_FIELDS, 0))
        bucket["retries"] += 1
        EVENTS.append({"ts": time.time(), "kind": "retry", "model": model, "status": None})
        _stats_dirty = True


def _record_eof_without_done(model=None) -> None:
    """EOF-without-done 计数：STATS 总量 + 当日桶 + daily_by_model。
    逐行镜像 _record_empty_retry（entry/桶形状同步），仅去掉 EVENTS 追加——
    eof 信号由计数器 + result 标记承载（spec Exclusions：事件流不扩展）。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["eof_without_done_total"] += 1
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                # v2 P3：16 字段 schema（与 _record_request 创建点形状同步）
                entry_dm = day_models[model] = dict.fromkeys(_DAILY_FIELDS, 0)
            if entry_dm is not None:  # 每日独立 cap：键数达上限后新模型不记录
                entry_dm["eof_without_done"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), dict.fromkeys(_DAILY_FIELDS, 0))
        bucket["eof_without_done"] += 1
        _stats_dirty = True


def _record_header_retry(model=None) -> None:
    """头超时重试计数：STATS 总量 + 当日桶 + daily_by_model。
    逐行镜像 _record_eof_without_done（entry/桶形状同步），仅去掉 EVENTS 追加——
    头重试信号由计数器 + 错误留痕环承载（spec Exclusions：事件流不扩展）。"""
    global _stats_dirty
    with STATS_LOCK:
        STATS["header_retries_total"] += 1
        if model:
            day_models = STATS["daily_by_model"].setdefault(today_key(), {})
            entry_dm = day_models.get(model)
            if entry_dm is None and len(day_models) < BY_MODEL_CAP:
                # v2 P3：16 字段 schema（与 _record_request 创建点形状同步）
                entry_dm = day_models[model] = dict.fromkeys(_DAILY_FIELDS, 0)
            if entry_dm is not None:
                entry_dm["header_retries"] += 1
        bucket = STATS["daily"].setdefault(
            today_key(), dict.fromkeys(_DAILY_FIELDS, 0))
        bucket["header_retries"] += 1
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
        snap["events"] = [dict(e) for e in EVENTS]  # 逐条浅拷贝（对齐 daily 模式），oldest→newest
        ttfb_hist = {m: list(h) for m, h in STATS["ttfb_hist"].items()}
        phase_hist = {k: list(v) for k, v in STATS["phase_ms"].items()}
        rates = dict(STATS["rates"])
        stalls_total = STATS["stalls_total"]
        recent_len = len(RECENT_REQUESTS)
        recent_stream = sum(1 for r in RECENT_REQUESTS if r.get("stream"))
    snap["uptime_s"] = int(time.time() - STARTED_AT)
    snap["upstream_base"] = UPSTREAM_BASE
    snap["upstream_source"] = _upstream_source
    plan = range_stats(snap["daily"])  # 锁外基于副本计算（4×≤31 桶求和 <1ms），不拉长持锁
    snap["range_stats"] = plan["stats"]
    snap["range_bounds"] = plan["bounds"]
    # v2 P2：perf 节（锁外基于浅拷贝计算；≤32 模型 × 9 桶 + 3 阶段 × 9 桶 <1ms）
    perf = {"ttfb_p50_ms_by_model": {}, "ttfb_p90_ms_by_model": {},
            "phase_p50_ms": {}, "bytes_per_s": 0.0, "chunks_per_s": 0.0,
            "tokens_per_s": 0.0, "stalls_total": stalls_total,
            "stream_share": 0.0}
    for model, hist in ttfb_hist.items():
        perf["ttfb_p50_ms_by_model"][model] = round(
            hist_percentile(hist, HIST_BUCKETS_MS, 0.5), 1)
        perf["ttfb_p90_ms_by_model"][model] = round(
            hist_percentile(hist, HIST_BUCKETS_MS, 0.9), 1)
    for name in ("connect", "headers", "body"):
        perf["phase_p50_ms"][name] = round(
            hist_percentile(phase_hist[name], HIST_BUCKETS_MS, 0.5), 1)
    elapsed = time.monotonic() - rates["window_start"]
    if elapsed < 60.0 and elapsed > 0:
        # 窗口过期（≥60s 无请求）时速率报 0：无近期流量（Anchor Reconciliation）
        perf["bytes_per_s"] = round(rates["window_bytes"] / elapsed, 1)
        perf["chunks_per_s"] = round(rates["window_chunks"] / elapsed, 1)
        perf["tokens_per_s"] = round(rates["window_tokens"] / elapsed, 1)
    if recent_len:
        perf["stream_share"] = round(recent_stream / recent_len, 4)
    snap["perf"] = perf
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
            rid = "r-%d" % STATS["requests_total"]
            # 一次解析，转发路径全程复用；UPSTREAM_BASE 读侧无锁（
            # _CFG_LOCK 只护写，Python 引用赋值原子 → 读线程看到任一完整旧值）
            upstream_host = urllib.parse.urlparse(UPSTREAM_BASE).netloc
        self._req_id = rid
        self._upstream_host = upstream_host
        try:
            self._proxy_relay(started)
        except (ConnectionError, socket.timeout):
            # 吞掉的是 relay 阶段任一端断流：客户端 EPIPE/reset（RST）、客户端优雅
            # 关闭后 send 阻塞到 SEND_TIMEOUT_S、上游中途 stall 到 600s 超时——三者
            # 都是"turn 中断、非代理故障"（客户端取消是常态；后两者无法与前者区分
            # 处也无需区分）。不重抛：再抛只进 handle_error 打 20+ 行 traceback 且
            # 无 status 记录；不计 errors_total。status 取 499（nginx 客户端中断惯例）。
            outcome = classify_outcome(client_abort=True)
            self._log(started, 499, outcome.log_result, 0,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      outcome=outcome.category)
            _record_request(self.command, self.path, 499,
                            (time.time() - started) * 1000, 0, model=None,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome)
        finally:
            with STATS_LOCK:
                STATS["active"] -= 1

    def _proxy_relay(self, started: float) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        model = extract_model(body)
        body = normalize_null_assistant_content(body)

        # --- TPM 准入 hook（_open_upstream 之前；重试复用本次准入不重复 charge）---
        tpm_key = tpm_key_id(self.headers.get("Authorization"))
        tpm_est = 0
        tpm_qwait_ms = None
        tpm_final_used = None
        if tpm_key is not None:
            tpm_est = estimate_request_tokens(body)
            tpm_status, tpm_qwait_ms = tpm_admit(tpm_key, tpm_est, model)
            if tpm_status in ("full", "timeout"):
                # 锁外记录（tpm_admit 已释放 TPM_LOCK，锁序安全）
                self._reply_tpm_429(tpm_status)
                result = ("tpm-queue-full" if tpm_status == "full"
                          else "tpm-queue-timeout")
                self._log(started, 429, result, 0, model=model, qwait_ms=tpm_qwait_ms)
                _record_request(self.command, self.path, 429,
                                (time.time() - started) * 1000, 0,
                                model=model, error=True)
                record_error_event(
                    ERR_KIND_TPM_QUEUE_FULL if tpm_status == "full"
                    else ERR_KIND_TPM_QUEUE_TIMEOUT,
                    model=model, path=self.path, body=body)
                return

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

        header_retried = 0
        header_retry_reason = ""
        t_conn_start = time.monotonic()  # TTFB 起点：每次 _open_upstream 尝试前重记（重试等待不混入 TTFB）
        try:
            try:
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                t_headers_done = time.monotonic()
            except (socket.timeout, http.client.RemoteDisconnected, ConnectionResetError) as first_exc:
                # socket.timeout = 响应头阶段阻塞到 HEADER_TIMEOUT_S；RemoteDisconnected
                # = 上游在响应头阶段直接关连接（未发任何响应字节）；ConnectionResetError =
                # connect/TLS/request 发送/getresponse 头读阶段被 RST。三者同为"响应头阶段
                # 未收到任何响应字节"，priming 不可见论证对三者同样成立：重试一次不损伤
                # 客户端交付。retry_reason 有意偏离阶段命名：RST 指向上游 LB 健康、timeout
                # 指向上游慢，运维 grep 需区分；kind 与计数器仍按阶段复用 header_timeout，
                # 避免 9 触点统计形状改动。
                if not header_timeout_should_retry(HEADER_RETRY_MAX):
                    raise
                header_retried = 1
                header_retry_reason = ("header-timeout"
                                       if isinstance(first_exc, (socket.timeout,
                                                                 http.client.RemoteDisconnected))
                                       else "conn-reset")
                record_error_event(ERR_KIND_HEADER_TIMEOUT, model=model, path=self.path,
                                   exc=first_exc, body=body, retry_reason=header_retry_reason)
                _record_header_retry(model)
                t_conn_start = time.monotonic()
                conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                t_headers_done = time.monotonic()
        except (OSError, http.client.HTTPException) as exc:
            self._reply_502(exc)
            # TPM 退款：上游不可达/响应头阶段异常 → 全额退款
            if tpm_key is not None:
                tpm_settle(tpm_key, tpm_est, 0, model)
            outcome = classify_outcome(synth_502=True)
            self._log(started, 502, outcome.log_result, 0, model=model, exc=exc,
                      retried=header_retried, retry_reason=header_retry_reason,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      outcome=outcome.category,
                      qwait_ms=tpm_qwait_ms,
                      tpm_used=0 if tpm_key is not None else None)
            _record_request(self.command, self.path, 502,
                            (time.time() - started) * 1000, 0,
                            model=model, error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            outcome=outcome)
            record_error_event(ERR_KIND_SYNTH_502, model=model, path=self.path,
                               exc=exc, body=body,
                               retried=header_retried, retry_reason=header_retry_reason)
            return

        ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
        content_type = (resp.getheader("Content-Type") or "").lower()
        if "text/event-stream" in content_type:
            retried = header_retried
            retry_reason = header_retry_reason
            try:
                self._body_err_line = None
                self._tpm_usage = None
                self._p3_usage_tokens = None
                filtered, truncated = self._relay_sse(resp,
                    final=not empty_stream_should_retry(EMPTY_RETRY_MAX))
                body_error = self._body_err_line is not None
            except _EmptyStream as exc:
                filtered = exc.filtered  # attempt-1 已滤毒缓冲随重试丢弃，filtered 只计交付流
                retried = header_retried + 1
                retry_reason = exc.reason
                _record_empty_retry(model, exc.reason)
                record_error_event(ERR_KIND_EMPTY_RETRY, model=model, path=self.path,
                                   response=b"".join(exc.lines),
                                   retry_reason=exc.reason)
                conn.close()
                t_conn_start = time.monotonic()
                try:
                    conn, resp = self._open_upstream(self.command, self.path, body, fwd_headers)
                    t_headers_done = time.monotonic()
                except (OSError, http.client.HTTPException) as retry_exc:
                    self._reply_502(retry_exc)  # 客户端尚未收到字节，502 语义与既有路径一致
                    # TPM 退款：重试仍失败 → 全额退款
                    if tpm_key is not None:
                        tpm_settle(tpm_key, tpm_est, 0, model)
                    outcome = classify_outcome(synth_502=True)
                    self._log(started, 502, outcome.log_result, 0, model=model, retried=1,
                              retry_reason=retry_reason, exc=retry_exc,
                              rid=self._req_id, upstream_host=self._upstream_host,
                              outcome=outcome.category,
                              qwait_ms=tpm_qwait_ms,
                              tpm_used=0 if tpm_key is not None else None)
                    _record_request(self.command, self.path, 502,
                                    (time.time() - started) * 1000, 0, model=model,
                                    error=outcome.counts_error,
                                    rid=self._req_id, upstream_host=self._upstream_host,
                                    outcome=outcome)
                    record_error_event(ERR_KIND_SYNTH_502, model=model, path=self.path,
                                       exc=retry_exc, body=body,
                                       retried=1, retry_reason=retry_reason)
                    return
                self._body_err_line = None
                self._tpm_usage = None
                self._p3_usage_tokens = None
                filtered, truncated = self._relay_sse(resp, final=True)
                body_error = self._body_err_line is not None
                ttfb_ms = round((t_headers_done - t_conn_start) * 1000, 1)
            # v2 P2：首字节口径覆盖 headers 口径（流式 = 首个非毒 record 交付时刻；
            # 空流重试路径 _relay_sse 入口已重置 mark，此处读到的始终是交付流的值）
            if self._t_first_byte_mark is not None:
                ttfb_ms = round((self._t_first_byte_mark - t_conn_start) * 1000, 1)
            t_relay_done = time.monotonic()
            connect_ms = round((self._t_request_sent - t_conn_start) * 1000, 1)
            headers_ms = round((t_headers_done - self._t_request_sent) * 1000, 1)
            body_ms = round((t_relay_done - t_headers_done) * 1000, 1)
            outcome = classify_outcome(status=resp.status, poison_filtered=filtered,
                                       body_error=body_error)
            result = outcome.log_result  # 重试后按实际 resp 重算
            if truncated and result == "ok":
                # 仅覆盖 ok：body-err/upstream-err（status>=400 更有信息量）与 aborted
                # （异常路径不经此处）不误标；priming EOF 由 retries 计数承载，避免双计数
                result = "eof-without-done"
                outcome = classify_outcome(eof_without_done=True)
                _record_eof_without_done(model)
            if outcome.capture:
                if body_error:
                    kind = ERR_KIND_BODY_ERROR
                elif outcome.category == CLASS_POISON_FIXED:
                    kind = ERR_KIND_POISON
                elif result == "eof-without-done":
                    kind = ERR_KIND_EOF_NO_DONE
                elif resp.status >= 500:
                    kind = ERR_KIND_UPSTREAM_5XX
                else:
                    kind = ERR_KIND_REQUEST_4XX
                record_error_event(kind, model=model, path=self.path,
                                   upstream_status=(resp.status
                                                    if resp.status >= 400 else None),
                                   body=body, filtered=filtered,
                                   response=self._body_err_line if body_error else None)
            # TPM settle：用上游实际 usage 校正窗口占用（无 usage 帧不校正）
            tpm_final_used = tpm_est if tpm_key is not None else None
            if tpm_key is not None and self._tpm_usage is not None:
                total = self._tpm_usage.get("total_tokens")
                if isinstance(total, int) and total >= 0:
                    tpm_settle(tpm_key, tpm_est, total, model)
                    tpm_final_used = total
            self._log(started, resp.status, result, filtered, model=model, retried=retried,
                      retry_reason=retry_reason,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      ttfb_ms=ttfb_ms, stream=1, outcome=outcome.category,
                      qwait_ms=tpm_qwait_ms,
                      tpm_used=tpm_final_used if tpm_key is not None else None)
            p3_tokens = self._p3_usage_tokens
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, filtered, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=1, outcome=outcome,
                            tokens=p3_tokens[2] if p3_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_tokens[0] if p3_tokens else 0,
                            tokens_completion=p3_tokens[1] if p3_tokens else 0,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
        else:
            relayed_data = self._relay_buffered(resp)
            # v2 P2：非流式 TTFB = t_body_done - t_conn_start（_relay_buffered 已置 mark）
            ttfb_ms = round((self._t_first_byte_mark - t_conn_start) * 1000, 1)
            connect_ms = round((self._t_request_sent - t_conn_start) * 1000, 1)
            headers_ms = round((t_headers_done - self._t_request_sent) * 1000, 1)
            body_ms = round((self._t_first_byte_mark - t_headers_done) * 1000, 1)
            body_error = False
            tpm_final_used = tpm_est if tpm_key is not None else None
            p3_buf_tokens = None
            if relayed_data:
                try:
                    parsed = json.loads(relayed_data.decode("utf-8", "replace"))
                except ValueError:
                    pass  # non-JSON body -> no body error, fail-open
                else:
                    body_error = body_has_error(parsed)
                    # TPM settle：解析 usage.total_tokens 校正（非 dict / 无 usage 不校正）；
                    # v2 P3：同一 parsed dict 顺手抽 usage 数值——零额外 json.loads（R1）
                    if isinstance(parsed, dict):
                        usage = parsed.get("usage")
                        if isinstance(usage, dict):
                            p3_buf_tokens = usage_dict_tokens(usage)
                            if tpm_key is not None:
                                total = usage.get("total_tokens")
                                if isinstance(total, int) and total >= 0:
                                    tpm_settle(tpm_key, tpm_est, total, model)
                                    tpm_final_used = total
            outcome = classify_outcome(status=resp.status, body_error=body_error)
            self._log(started, resp.status, outcome.log_result, 0, model=model,
                      rid=self._req_id, upstream_host=self._upstream_host,
                      ttfb_ms=ttfb_ms, stream=0, outcome=outcome.category,
                      qwait_ms=tpm_qwait_ms,
                      tpm_used=tpm_final_used if tpm_key is not None else None)
            _record_request(self.command, self.path, resp.status,
                            (time.time() - started) * 1000, 0, model=model,
                            error=outcome.counts_error,
                            rid=self._req_id, upstream_host=self._upstream_host,
                            ttfb_ms=ttfb_ms, stream=0, outcome=outcome,
                            tokens=p3_buf_tokens[2] if p3_buf_tokens else None,
                            bytes_out=self._relay_bytes, chunks=self._relay_chunks,
                            tokens_prompt=p3_buf_tokens[0] if p3_buf_tokens else 0,
                            tokens_completion=p3_buf_tokens[1] if p3_buf_tokens else 0,
                            phase_ms={"connect": connect_ms, "headers": headers_ms,
                                      "body": body_ms})
            if outcome.capture:
                if body_error:
                    kind = ERR_KIND_BODY_ERROR
                elif resp.status >= 500:
                    kind = ERR_KIND_UPSTREAM_5XX
                else:
                    kind = ERR_KIND_REQUEST_4XX
                record_error_event(
                    kind, model=model, path=self.path,
                    upstream_status=resp.status if resp.status >= 400 else None,
                    body=body,
                    response=relayed_data[:RESPONSE_SNIPPET_CAP]
                    if body_error or resp.status >= 500 else None)
        conn.close()

    def _open_upstream(self, method: str, path: str, body, fwd_headers: dict):
        parsed = urllib.parse.urlparse(UPSTREAM_BASE)
        upstream_path = parsed.path.rstrip("/") + path
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        conn.request(method, upstream_path, body=body, headers=fwd_headers)
        self._t_request_sent = time.monotonic()  # v2 P2：connect+TLS+请求发送完成时刻（phase connect/headers 分界）
        # getresponse() 成功返回后 conn.sock 会被置 None（socket 移交 HTTPResponse 的
        # fp.raw），因此必须在此之前保留自己的引用；该引用与 HTTPResponse 包装的是
        # 同一 socket 对象，settimeout 作用于体阶段读（不 poke resp.fp.raw._sock）。
        sock = conn.sock
        resp = conn.getresponse()
        sock.settimeout(UPSTREAM_TIMEOUT)
        return conn, resp

    def _send_sse_headers(self, resp) -> None:
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("X-Request-Id", self._req_id)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool) -> tuple:
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        # v2 P2 打点状态：每 record 一次 monotonic + 整数加法，零锁；stall 自增
        # <0.1% record 命中才持 STATS_LOCK（R1 热路径纪律）。重试二次进入时此处重置，
        # 空流尝试的残留值不会漏进交付流。
        self._t_first_byte_mark = None  # 首个非毒 record 交付时刻（TTFB 首字节口径）
        self._relay_bytes = 0
        self._relay_chunks = 0
        self._t_last_record = None      # 相邻 record 间隔基线（stall 检测）
        try:
            filtered = 0
            saw_done = False   # 全程（priming+streaming）是否见过 [DONE] record
            truncated = False  # streaming 阶段 EOF 且全程无 done → 上游截断标记
            pending = []      # 当前 SSE record 的行缓冲（不含终结空行）
            poisoned = False  # 当前 record 内是否命中毒行
            primed = []       # priming 阶段已滤毒缓冲的完整 record 行（含终结空行）
            priming = True    # True = 客户端尚未收到任何字节
            finish_hold = False  # finish record 触发 hold：缓冲尾段至 EOF 判 usage
            saw_usage = False    # 整流是否出现过非空 usage 帧
            hold_bytes = 0       # finish hold 期已缓冲的字节数

            def _flush_primed() -> None:
                """补发 SSE 头 + primed 缓冲并 flush，切换出 priming。"""
                self._send_sse_headers(resp)
                primed_bytes = b"".join(primed)
                self.wfile.write(primed_bytes)
                self.wfile.flush()
                now = time.monotonic()
                if self._t_first_byte_mark is None:
                    self._t_first_byte_mark = now  # v2 P2：TTFB 首字节口径
                self._t_last_record = now           # v2 P2：stall 间隔基线
                self._relay_bytes += len(primed_bytes)

            while True:
                line = resp.readline()
                if line in (b"\n", b"\r\n", b""):
                    # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
                    saw_done = saw_done or ("done" in kinds)
                    # TPM usage 捕获：本 record 内所有行取最后非空 usage 帧；
                    # v2 P3 同循环顺手消费已解析 dict——零额外 json.loads（R1）
                    for buf_line in pending:
                        usage_hit = sse_line_usage(buf_line)
                        if usage_hit is not None:
                            self._tpm_usage = usage_hit
                            self._p3_usage_tokens = usage_dict_tokens(usage_hit)
                    if self._body_err_line is None:
                        hit = next((l for l in pending if sse_line_body_error(l)), None)
                        if hit is not None:
                            self._body_err_line = hit
                    if poisoned:
                        filtered += 1  # 整 record（含终结空行）丢弃，不损伤相邻字节（priming 期不进 primed）
                        _record_poison_preview(b"".join(pending))
                    elif priming:
                        primed.extend(pending)
                        if line:
                            primed.append(line)
                        if pending or line:
                            self._relay_chunks += 1  # v2 P2：每非毒 record 计一 chunk（毒 record 已在 filtered 分支；纯 EOF 迭代不计——fix-loop-1）
                        if not saw_usage:
                            saw_usage = any(sse_line_has_usage(l) for l in pending)
                        if finish_hold:
                            hold_bytes += sum(len(l) for l in pending) + len(line)
                            if "content" in kinds:              # fail-open：finish 后反常 content
                                _flush_primed()
                                priming = False
                                finish_hold = False
                            elif hold_bytes > PRIMED_TAIL_CAP:  # 恶意/异常长尾 fail-open
                                _flush_primed()
                                priming = False
                                finish_hold = False
                            # done/finish/noise（含 usage）：继续缓冲尾段
                        elif "content" in kinds or "done" in kinds:
                            _flush_primed()
                            priming = False       # v1.4 原语义不变
                        elif "finish" in kinds:
                            finish_hold = True    # hold 至 EOF 判 usage
                    else:
                        if pending or line:  # 纯 EOF 迭代（pending 空 + line 空）不计 chunk、不做 stall 检测：
                            # 否则"最后 record→EOF 尾间隙"会被当相邻 record 间隔误报 stall（fix-loop-1）
                            now = time.monotonic()
                            if self._t_last_record is not None \
                                    and now - self._t_last_record > STALL_THRESHOLD_S:
                                # v2 P2：stall 自增是热循环内唯一持锁点，且仅跨阈值间隙命中
                                # （正常流相邻 record 间隔 << 阈值，<0.1% 命中；R1 纪律）
                                with STATS_LOCK:
                                    STATS["stalls_total"] += 1
                            self._t_last_record = now
                            for buf_line in pending:
                                self.wfile.write(buf_line)
                                self._relay_bytes += len(buf_line)
                            if line:
                                self.wfile.write(line)
                                self._relay_bytes += len(line)
                            self.wfile.flush()
                            self._relay_chunks += 1
                    pending = []
                    poisoned = False
                    if line == b"":
                        if priming:  # EOF 仍 priming = 空流（零 record / reasoning-only 断流）
                            if finish_hold:
                                if saw_usage or final:
                                    _flush_primed()   # 合法零内容流（有 usage）或预算已尽 fail-open
                                else:
                                    raise _EmptyStream(filtered, primed,
                                                      reason="finish-no-usage")
                            elif final:
                                _flush_primed()       # v1.4 原语义：switch off / 重试流原样下发
                            else:
                                raise _EmptyStream(filtered, primed)
                        else:
                            truncated = not saw_done  # streaming EOF 无 done = 上游截断
                        break
                else:
                    if POISON_RE.match(line.rstrip(b"\r\n")):
                        poisoned = True
                    pending.append(line)
            return filtered, truncated
        finally:
            self.connection.settimeout(old_timeout)

    def _relay_buffered(self, resp: http.client.HTTPResponse) -> bytes:
        data = resp.read()
        t_body_done = time.monotonic()   # v2 P2：非流式无首字节事件，TTFB 口径 = t_body_done - t_conn_start（spec P2）
        self._t_first_byte_mark = t_body_done
        self._relay_bytes = len(data)
        self._relay_chunks = 1           # 非流式 = 单 chunk
        self.send_response(resp.status)
        for name, value in resp.getheaders():
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-Id", self._req_id)
        self.end_headers()
        old_timeout = self.connection.gettimeout()
        self.connection.settimeout(SEND_TIMEOUT_S)
        try:
            if data:
                self.wfile.write(data)
        finally:
            self.connection.settimeout(old_timeout)
        return data

    def _reply_502(self, exc: BaseException) -> None:
        payload = ("ctyun-stream-fix-proxy: upstream error: %s\n" % exc).encode("utf-8")
        self.send_response(502)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("X-Request-Id", self._req_id)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def _reply_tpm_429(self, reason: str) -> None:
        """合成 429 响应（镜像 _reply_502 定死 body + close_connection=True）。

        reason: "full"（队列满/est 超预算）或 "timeout"（排队超时）——用于日志分类。
        """
        payload = ('{"error":{"message":"模型请求 TPM 超限，请减少 tokens 后重试",'
                   '"type":"rate_limit_error","code":"model_tpm_limit"}}').encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Request-Id", self._req_id)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(payload)
        self.close_connection = True

    def _log(self, started: float, status: int, result: str, filtered: int, model=None,
             retried: int = 0, retry_reason: str = "", exc=None,
             rid=None, upstream_host=None, ttfb_ms=None, stream=None,
             outcome=None, qwait_ms=None, tpm_used=None) -> None:
        exc_field = "-"
        if exc is not None:
            exc_field = re.sub(r"\s+", "_",
                               ("%s: %s" % (type(exc).__name__, exc)).strip())[:200]
        ttfb_field = "%.1fms" % ttfb_ms if ttfb_ms is not None else "-"
        extra = ""
        if qwait_ms is not None:
            extra += " qwait=%dms" % qwait_ms
        if tpm_used is not None:
            extra += " tpm=%d" % tpm_used
        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "
                         "model=%s retried=%d retry_reason=%s exc=%s "
                         "rid=%s host=%s ttfb=%s stream=%s outcome=%s%s ts=%s"
                         % (self.command, self.path, status, time.time() - started,
                            result, filtered, model or "-", retried,
                            retry_reason or "-", exc_field,
                            rid or "-", upstream_host or "-", ttfb_field,
                            "-" if stream is None else ("1" if stream else "0"),
                            outcome or "-", extra,
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
        elif path == "/api/tpm_stats":
            self._send_json(200, tpm_snapshot())
        elif path == "/api/config":
            with _CFG_LOCK:
                payload = {"upstream_base": UPSTREAM_BASE, "source": _upstream_source,
                           "capture_errors": CAPTURE_ERRORS,
                           "model_pricing": MODEL_PRICING}
            self._send_json(200, payload)
        elif path == "/api/health":
            self._send_json(200, {"upstream": probe_state_snapshot()})
        elif path == "/api/errors":
            id_str = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query).get("id", [None])[0]
            if id_str is not None:
                # 单条详情（含 body/response），需鉴权
                try:
                    eid = int(id_str)
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "id must be an integer"})
                    return
                if not write_allowed(self.client_address[0],
                                     self.headers.get("X-Admin-Token") or "",
                                     os.environ.get("CTYUN_ADMIN_TOKEN", "")):
                    self._send_json(403, {"error": "detail requires X-Admin-Token"})
                    return
                with ERROR_LOCK:
                    match = None
                    for ev in ERROR_EVENTS:
                        if ev["id"] == eid:
                            match = dict(ev)
                            break
                if match is None:
                    self._send_json(404, {"error": "event not found"})
                else:
                    self._send_json(200, match)
            else:
                # 列表（不含 body/response），无鉴权
                with ERROR_LOCK:
                    events = [{"id": e["id"], "ts": e["ts"], "kind": e["kind"],
                               "category": e["category"], "model": e["model"],
                               "path": e["path"],
                               "upstream_status": e["upstream_status"],
                               "exc": e["exc"], "filtered": e["filtered"],
                               "retried": e["retried"],
                               "retry_reason": e["retry_reason"]}
                              for e in ERROR_EVENTS]
                events.reverse()  # newest-first（锁外反转：events 已是新 list）
                with _CFG_LOCK:
                    cap_enabled = CAPTURE_ERRORS
                self._send_json(200, {"capture_errors": cap_enabled,
                                      "count": len(events),
                                      "events": events})
        elif path == "/api/logs":
            qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            cursor = qs.get("cursor", [None])[0]
            tail = qs.get("tail", [None])[0]
            if cursor is not None:
                try:
                    cursor = int(cursor)
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "cursor must be an integer"})
                    return
            if tail is not None:
                try:
                    tail = int(tail)
                except (ValueError, TypeError):
                    self._send_json(400, {"error": "tail must be an integer"})
                    return
            try:
                snap = logs_snapshot(cursor=cursor, tail=tail)
            except ValueError as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(200, snap)
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
        if path == "/api/probe":
            self._handle_probe_post(raw)
            return
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
        cap = data.get("capture_errors") if isinstance(data, dict) else None  # 可选键
        if cap is not None and not isinstance(cap, bool):
            self._send_json(400, {"error": "capture_errors must be a boolean"})
            return
        # upstream_base 缺失/为 None 时允许单独 POST capture_errors；给出但非法仍 400。
        if base is not None and (not isinstance(base, str) or not valid_upstream_url(base)):
            self._send_json(400, {"error": "upstream_base 需为 "
                                           "http(s)://host[:port]/path 形式的合法 URL"})
            return
        if base is not None:
            set_upstream_base(base)
        if cap is not None:
            set_capture_errors(cap)
        with _CFG_LOCK:
            resp = {"ok": True, "upstream_base": UPSTREAM_BASE, "source": "api",
                    "capture_errors": CAPTURE_ERRORS}
        self._send_json(200, resp)

    def _handle_probe_post(self, raw: bytes) -> None:
        """POST /api/probe：切 probe 开关 {"enabled": bool}（鉴权同 /api/config）。"""
        if not write_allowed(self.client_address[0],
                             self.headers.get("X-Admin-Token") or "",
                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            self._send_json(403, {"error": "非本机切换 probe 开关需要 X-Admin-Token 头"
                                           "（值 = 服务器环境变量 CTYUN_ADMIN_TOKEN）"})
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体不是合法 JSON——"
                                           "请发 {\"enabled\": true|false}"})
            return
        enabled = data.get("enabled") if isinstance(data, dict) else None
        if not isinstance(enabled, bool):
            self._send_json(400, {"error": "enabled 必须为布尔值"})
            return
        set_probe_enabled(enabled)
        self._send_json(200, {"ok": True, "probe_enabled": enabled})

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
# P5：按页面顺序拆 6 段常量 + 尾部字面量 join 成 DASHBOARD_HTML。join 顺序 = 页面
# 实际顺序，漏段/错序 → 白屏但后端 200（R5），DashboardV2SkeletonTest 用 join 等式
# 与容器/函数名断言兜底。_DASH_JS_CORE 前导 </main>/footer/evt-tip/<script> 胶水，
# 使 _DASH_SECTIONS_V2 的卡片落在 </main> 之前的主网格内；_DASH_SECTIONS_V2 初始空串。
_DASH_HEAD = """<!doctype html>
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
.range-tabs { display:flex; gap:8px; flex-wrap:wrap; }
.range-tab { font-weight:400; font-size:12.5px; background:transparent; color:var(--dim);
  border:1px solid var(--line); border-radius:99px; padding:4px 14px; }
.range-tab:hover { filter:none; color:var(--text); }
.range-tab.active { background:var(--amber); color:var(--ink); border-color:var(--amber); font-weight:700; }
svg#spark { width:100%; height:64px; display:block; }
.strip { display:flex; gap:8px; overflow-x:auto; padding-bottom:4px; }
.chip-poison { flex:0 0 auto; border:1px solid var(--amber); border-left:4px solid var(--amber);
  border-radius:4px; padding:2px 8px; color:var(--amber); font-size:12px;
  text-decoration:line-through; white-space:nowrap; }
    .chip-poison .t { color:var(--dim); text-decoration:none; margin-right:6px; }
.empty { color:var(--dim); }
.table-wrap { overflow-x:auto; }
.evt-tip { position:fixed; z-index:9; display:none; max-width:340px; max-height:260px;
  overflow-y:auto; background:var(--panel); border:1px solid var(--amber); border-radius:6px;
  padding:6px 10px; font-size:12px; pointer-events:none;
  box-shadow:0 4px 16px rgba(0,0,0,.5); }
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
"""

_DASH_SECTIONS_STATIC = """<main>
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
  <section class="card range-tabs" role="tablist" aria-label="统计时间维度">
    <button type="button" class="range-tab" role="tab" data-range="3d">近3天</button>
    <button type="button" class="range-tab" role="tab" data-range="7d">近7天</button>
    <button type="button" class="range-tab" role="tab" data-range="mtd">本月</button>
    <button type="button" class="range-tab" role="tab" data-range="last_month">上月</button>
  </section>
  <section class="stats-row">
    <div class="card stat"><div class="num" id="st-requests">--</div><div class="label">请求数</div></div>
    <div class="card stat"><div class="num amber" id="st-filtered">--</div><div class="label">剥行（所选时段）</div></div>
    <div class="card stat"><div class="num" id="st-rate">--</div><div class="label">毒行率</div></div>
    <div class="card stat"><div class="num" id="st-active">--</div><div class="label">活跃连接·实时</div></div>
    <div class="card stat"><div class="num" id="st-errors">--</div><div class="label">错误数</div></div>
  </section>
  <section class="card">
    <div class="card-title">请求节奏 · 最近 10 分钟（琥珀=请求，红=剥行）</div>
    <svg id="spark" viewBox="0 0 600 64" preserveAspectRatio="none" role="img" aria-label="最近 10 分钟请求柱状图"></svg>
  </section>
"""

_DASH_SECTIONS_TABLES = """  <section class="card">
    <div class="card-title">按天统计（<span id="daily-title-range">近7天</span>，新在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-body"><tr><td class="empty" colspan="6">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">按天 × 模型（<span id="daily-model-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th><th>重试</th></tr></thead>
      <tbody id="daily-model-body"><tr><td class="empty" colspan="7">读取中……</td></tr></tbody>
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
"""

_DASH_SECTIONS_V2 = """  <section class="card">
    <div class="card-title">上游健康<span class="chip" id="health-status">--</span></div>
    <table>
      <tbody id="upstream-health"><tr><td class="empty" colspan="2">读取中……</td></tr></tbody>
    </table>
  </section>
  <section class="card">
    <div class="card-title">模型速度对比 · TTFB P50/P90（进程内累计，最快在上）</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>模型</th><th>P50</th><th>P90</th></tr></thead>
      <tbody id="perf-model-body"><tr><td class="empty" colspan="3">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card">
    <div class="card-title">延迟分布 · 三阶段 P50（连接 / 响应头 / 数据体）</div>
    <svg id="latency-dist" viewBox="0 0 600 96" preserveAspectRatio="none" role="img"
         aria-label="阶段延迟 P50 条形图"></svg>
  </section>
  <section class="card">
    <div class="card-title">Token 用量按天 × 模型（<span id="token-title-range">近7天</span>，跨重启保留（每 60s 落盘））</div>
    <div class="table-wrap">
    <table>
      <thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th></tr></thead>
      <tbody id="token-daily-body"><tr><td class="empty" colspan="4">读取中……</td></tr></tbody>
    </table>
    </div>
  </section>
  <section class="card" id="tri-state-card">
    <div class="card-title">三态可用率 · 所选时段（<span id="tri-state-range">近7天</span>）</div>
    <div class="tri-bar" style="display:flex;height:14px;border-radius:4px;overflow:hidden;gap:2px"
         role="img" aria-label="ok / degraded / failed 占比条"></div>
    <div id="tri-state-legend" style="color:var(--dim);font-size:12px;margin-top:8px"></div>
  </section>
"""

_DASH_JS_CORE = """</main>
<footer><div class="inner">
  顶部统计卡按所选时间段聚合（近3/近7天为含今日的滑动窗口，今日为部分数据；本月/上月为自然月，本地时区）；
  错误数=该时段内「代理错误+上游5xx」合计；活跃连接恒为实时值，不随时间段变化；
  累计与按天计数跨重启保留（每 60s 落盘，持久化于 ~/.local/etc/ctyun-stream-fix-proxy.json，
  日桶保留 90 天，覆盖上月+当月最远 62 天回溯）；
  按天×模型计数同 daily 口径跨重启保留（90 天 prune，副表数值 ≤ 主表，差值=当日无 model 请求，见下行）；
  按天主表含无 model 请求，各行数值 ≥「按天 × 模型」副表合计，差值即当日无 model 请求；
  最近请求/剥行流带为内存数据；「代理错误」=代理自身错误（含 body 内嵌错误：HTTP 200 但 body 顶层 error 对象），「上游5xx」=上游透传 status≥500
  （499 中断两边都不计）。页面每 2s 轮询 /api/stats，切换时间段用缓存零请求重渲染；非本机修改上游需 X-Admin-Token。
</div></footer>
<div class="evt-tip" id="evt-tip"></div>
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
var RANGE_LABELS = { "3d": "近3天", "7d": "近7天", "mtd": "本月", "last_month": "上月" };
var selectedRange = "7d";  // 刷新不记忆（无 localStorage），默认近7天
var lastSnap = null;       // poll 缓存：tab 点击零请求重渲染

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
var EVT_KIND_LABELS = { proxy: "代理错误", upstream: "上游5xx", retry: "空流重试", probe_alert: "上游探测告警" };
function evtFilter(kind, day, model) {
  // 按 dataset 重新过滤 lastSnap.events：kind="errors" = proxy+upstream（顶部错误数
  // 口径 = rs.errors_proxy + rs.errors_upstream）；day/model 给定时精确匹配。
  var evts = (lastSnap && lastSnap.events) || [];
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var out = [];
  for (var i = 0; i < evts.length; i++) {
    var e = evts[i];
    if (kind === "errors") {
      if (e.kind !== "proxy" && e.kind !== "upstream") continue;
    } else if (e.kind !== kind) {
      continue;
    }
    if (bounds && !(bounds[0] <= fmtDate(e.ts) && fmtDate(e.ts) <= bounds[1])) continue;
    if (day && fmtDate(e.ts) !== day) continue;
    if (model && e.model !== model) continue;
    out.push(e);
  }
  return out;
}
function markEvents(n, kind, day, model) {
  // 匹配数 >0 才设 data-evt：计数 0 → 无属性 → 无 tooltip（数字与明细同 key）
  delete n.dataset.evt;
  delete n.dataset.day;
  delete n.dataset.model;
  if (evtFilter(kind, day, model).length > 0) {
    n.dataset.evt = kind;
    if (day) n.dataset.day = day;
    if (model) n.dataset.model = model;
  }
}
function showEvtTip(target, x, y) {
  var tip = $("evt-tip");
  tip.textContent = "";
  var evts = evtFilter(target.dataset.evt, target.dataset.day, target.dataset.model);
  var rangeRow = !target.dataset.day;  // 顶部范围卡行（无 day）：时间含日期
  if (evts.length === 0) {
    tip.appendChild(el("div", "", "无记录"));
  } else {
    for (var i = evts.length - 1; i >= 0; i--) {  // 最新在上（renderRecent 模式）
      var e = evts[i];
      tip.appendChild(el("div", "",
        (EVT_KIND_LABELS[e.kind] || e.kind) + " " +
        (rangeRow ? fmtDate(e.ts) + " " + fmtTime(e.ts) : fmtTime(e.ts)) +
        (e.model ? " · " + e.model : "") +
        (e.status ? " · " + e.status : "") +
        (e.reason ? " · " + e.reason : "")));
    }
    if (evts.length >= 100) {
      tip.appendChild(el("div", "", "共 " + evts.length + " 次，仅保留最近 100 条事件记录"));
    }
  }
  tip.style.display = "block";
  tip.style.left = "0px";
  tip.style.top = "0px";
  var tw = tip.offsetWidth, th = tip.offsetHeight;
  var left = x + 14, top = y + 14;
  if (left + tw > window.innerWidth - 8) left = x - tw - 14;  // 视口边缘翻转
  if (top + th > window.innerHeight - 8) top = y - th - 14;
  tip.style.left = Math.max(8, left) + "px";
  tip.style.top = Math.max(8, top) + "px";
}
function hideEvtTip() {
  var tip = $("evt-tip");
  tip.style.display = "none";
  tip.textContent = "";
}
document.addEventListener("mouseover", function (e) {
  var t = e.target && e.target.closest ? e.target.closest("[data-evt]") : null;
  if (t) showEvtTip(t, e.clientX, e.clientY);
});
document.addEventListener("mousemove", function (e) {
  var t = e.target && e.target.closest ? e.target.closest("[data-evt]") : null;
  if (t) showEvtTip(t, e.clientX, e.clientY);
});
document.addEventListener("mouseout", function (e) {
  var t = e.target && e.target.closest ? e.target.closest("[data-evt]") : null;
  if (t) hideEvtTip();
});
function renderStats(snap) {
  var rs = snap.range_stats && snap.range_stats[selectedRange];
  if (rs) {
    $("st-requests").textContent = rs.requests;
    $("st-filtered").textContent = rs.filtered;
    $("st-rate").textContent = rs.requests > 0
      ? ((rs.filtered / rs.requests) * 100).toFixed(1) + "%" : "—";
    $("st-errors").textContent = rs.errors_proxy + rs.errors_upstream;
    markEvents($("st-errors"), "errors", null, null);
  }
  $("st-active").textContent = snap.active;
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
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var keys = Object.keys(daily).sort().reverse().filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
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
    var tdEp = el("td", "num", String(b.errors_proxy));
    var tdEu = el("td", "num", String(b.errors_upstream));
    var tdRt = el("td", "num", String(b.retries || 0));
    markEvents(tdEp, "proxy", keys[i], null);
    markEvents(tdEu, "upstream", keys[i], null);
    markEvents(tdRt, "retry", keys[i], null);
    tr.appendChild(tdEp);
    tr.appendChild(tdEu);
    tr.appendChild(tdRt);
    body.appendChild(tr);
  }
}
function renderDailyByModel(dbm) {
  var body = $("daily-model-body");
  body.textContent = "";
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var days = Object.keys(dbm).sort().reverse().filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
  if (days.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无按天 × 模型统计");
    td0.colSpan = 7;
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
      var tr = el("tr", ((ent.errors_proxy || 0) + (ent.errors_upstream || 0)) > 0 ? "hit" : "");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(ent.requests || 0)));
      tr.appendChild(el("td", "num", String(ent.filtered || 0)));
      var tdEp = el("td", "num", String(ent.errors_proxy || 0));
      var tdEu = el("td", "num", String(ent.errors_upstream || 0));
      var tdRt = el("td", "num", String(ent.retries || 0));
      markEvents(tdEp, "proxy", days[i], names[j]);
      markEvents(tdEu, "upstream", days[i], names[j]);
      markEvents(tdRt, "retry", days[i], names[j]);
      tr.appendChild(tdEp);
      tr.appendChild(tdEu);
      tr.appendChild(tdRt);
      body.appendChild(tr);
    }
  }
}
function renderRangeTabs() {
  var tabs = document.querySelectorAll(".range-tab");
  for (var i = 0; i < tabs.length; i++) {
    var on = tabs[i].getAttribute("data-range") === selectedRange;
    tabs[i].classList.toggle("active", on);
    tabs[i].setAttribute("aria-selected", on ? "true" : "false");
  }
}
function applyRange() {
  renderRangeTabs();
  var label = RANGE_LABELS[selectedRange] || selectedRange;
  $("daily-title-range").textContent = label;
  $("daily-model-title-range").textContent = label;
  $("token-title-range").textContent = label;
  $("tri-state-range").textContent = label;
  if (lastSnap) {
    renderStats(lastSnap);
    renderDaily(lastSnap.daily || {});
    renderDailyByModel(lastSnap.daily_by_model || {});
    renderTokens(lastSnap);
    renderTriState(lastSnap);
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
"""

_DASH_JS_V2 = """function renderPerf(snap) {
  var perf = snap.perf || {};
  var p50 = perf.ttfb_p50_ms_by_model || {};
  var p90 = perf.ttfb_p90_ms_by_model || {};
  var body = $("perf-model-body");
  body.textContent = "";
  var names = Object.keys(p50).sort(function (a, b) { return p50[a] - p50[b]; });
  if (names.length === 0) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无速度数据 —— 有请求经过代理后这里会出现 P50/P90");
    td0.colSpan = 3;
    tr0.appendChild(td0);
    body.appendChild(tr0);
  }
  for (var i = 0; i < names.length; i++) {
    var tr = el("tr");
    tr.appendChild(el("td", "", names[i]));
    tr.appendChild(el("td", "num", fmtDur(p50[names[i]])));
    tr.appendChild(el("td", "num", fmtDur(p90[names[i]])));
    body.appendChild(tr);
  }
  renderLatencyDist(perf.phase_p50_ms || {});
}
function renderLatencyDist(phases) {
  var svg = $("latency-dist");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var defs = [["connect", "连接"], ["headers", "响应头"], ["body", "数据体"]];
  var cap = 5000;
  var svgns = "http://www.w3.org/2000/svg";
  for (var i = 0; i < defs.length; i++) {
    var name = defs[i][0];
    var label = defs[i][1];
    var ms = phases[name];
    var w = ms === undefined ? 0 : Math.min(ms / cap, 1) * 460;
    var g = document.createElementNS(svgns, "g");
    var t = document.createElementNS(svgns, "text");
    t.setAttribute("x", 0);
    t.setAttribute("y", 24 + i * 30);
    t.setAttribute("fill", "var(--dim)");
    t.setAttribute("font-size", 12);
    t.textContent = label;
    var r = document.createElementNS(svgns, "rect");
    r.setAttribute("x", 90);
    r.setAttribute("y", 12 + i * 30);
    r.setAttribute("width", Math.max(2, w).toFixed(1));
    r.setAttribute("height", 14);
    r.setAttribute("fill", "var(--amber)");
    var v = document.createElementNS(svgns, "text");
    v.setAttribute("x", 96 + Math.max(2, w));
    v.setAttribute("y", 24 + i * 30);
    v.setAttribute("fill", "var(--text)");
    v.setAttribute("font-size", 12);
    v.textContent = ms === undefined ? "—" : fmtDur(ms);
    g.appendChild(t);
    g.appendChild(r);
    g.appendChild(v);
    svg.appendChild(g);
  }
}
function renderTokens(snap) {
  var body = $("token-daily-body");
  body.textContent = "";
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var dbm = snap.daily_by_model || {};
  var days = Object.keys(dbm).sort().reverse().filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
  var hasData = false;
  for (var i = 0; i < days.length; i++) {
    var models = dbm[days[i]];
    var names = Object.keys(models).sort(function (a, b) {
      return ((models[b].tokens_prompt || 0) + (models[b].tokens_completion || 0)) -
             ((models[a].tokens_prompt || 0) + (models[a].tokens_completion || 0));
    });
    for (var j = 0; j < names.length; j++) {
      var ent = models[names[j]];
      var tp = ent.tokens_prompt || 0;
      var tc = ent.tokens_completion || 0;
      if (tp + tc === 0) continue;  // 0-token 行（502/剥行等）不展示
      hasData = true;
      var tr = el("tr");
      tr.appendChild(el("td", "num", days[i]));
      tr.appendChild(el("td", "", names[j]));
      tr.appendChild(el("td", "num", String(tp)));
      tr.appendChild(el("td", "num", String(tc)));
      body.appendChild(tr);
    }
  }
  if (!hasData) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "暂无 token 用量 —— 上游返回 usage 帧后这里会出现记录");
    td0.colSpan = 4;
    tr0.appendChild(td0);
    body.appendChild(tr0);
  }
}
function renderTriState(snap) {
  var bar = document.querySelector("#tri-state-card .tri-bar");
  var legend = $("tri-state-legend");
  bar.textContent = "";
  legend.textContent = "";
  var bounds = lastSnap && lastSnap.range_bounds && lastSnap.range_bounds[selectedRange];
  var dbm = snap.daily_by_model || {};
  var days = Object.keys(dbm).filter(function (k) {
    return !bounds || (bounds[0] <= k && k <= bounds[1]);
  });
  var ok = 0, deg = 0, fail = 0;
  for (var i = 0; i < days.length; i++) {
    var models = dbm[days[i]];
    var names = Object.keys(models);
    for (var j = 0; j < names.length; j++) {
      var ent = models[names[j]];
      ok += ent.outcome_ok || 0;
      deg += ent.outcome_degraded || 0;
      fail += ent.outcome_failed || 0;
    }
  }
  var total = ok + deg + fail;
  if (total === 0) {
    bar.appendChild(el("span", "empty", "暂无 outcome 数据 —— 有请求经过代理后这里会出现三态占比"));
    return;
  }
  var wOk = Math.round(ok / total * 100);
  var wDeg = Math.round(deg / total * 100);
  var wFail = 100 - wOk - wDeg;
  var segs = [["ok", wOk, "var(--ok)"], ["degraded", wDeg, "var(--amber)"], ["failed", wFail, "var(--err)"]];
  for (var s = 0; s < segs.length; s++) {
    var seg = el("span", "");
    seg.style.width = segs[s][1] + "%";
    seg.style.background = segs[s][2];
    bar.appendChild(seg);
  }
  function pct(n) { return (n / total * 100).toFixed(1) + "%"; }
  legend.appendChild(el("span", "", "ok " + ok + "（" + pct(ok) + "）"));
  legend.appendChild(el("span", "", " · degraded " + deg + "（" + pct(deg) + "）"));
  legend.appendChild(el("span", "", " · failed " + fail + "（" + pct(fail) + "）"));
}
function renderHealth(h) {
  var body = $("upstream-health");
  var chip = $("health-status");
  body.textContent = "";
  if (!h) {
    var tr0 = el("tr");
    var td0 = el("td", "empty", "健康数据读取中……（/api/health 未响应）");
    td0.colSpan = 2;
    tr0.appendChild(td0);
    body.appendChild(tr0);
    chip.textContent = "读取中";
    return;
  }
  var rows = [
    ["上游主机", h.host || "—"],
    ["最近探测", h.last_probe_ts ? fmtDate(h.last_probe_ts) + " " + fmtTime(h.last_probe_ts) : "从未探测"],
    ["探测延迟", h.last_probe_ts ? fmtDur(h.last_probe_latency_ms) : "—"],
    ["连续失败", String(h.consecutive_failures || 0)],
    ["主动探测", h.probe_enabled ? "开" : "关"]
  ];
  for (var i = 0; i < rows.length; i++) {
    var tr = el("tr");
    tr.appendChild(el("td", "num", rows[i][0]));
    tr.appendChild(el("td", "", rows[i][1]));
    body.appendChild(tr);
  }
  if (!h.last_probe_ts) {
    chip.textContent = "待首探";
  } else if (h.last_probe_ok) {
    chip.textContent = "正常";
  } else {
    chip.textContent = "异常";
  }
  markEvents(chip, "probe_alert", null, null);
}
function pollHealth() {
  fetch("/api/health")
    .then(function (resp) {
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      return resp.json();
    })
    .then(function (data) {
      renderHealth(data && data.upstream ? data.upstream : null);
    })
    .catch(function () {
      renderHealth(null);  // 健康卡独立降级显示"读取中"，不触发 setConn（连通性以 /api/stats 为准）
    });
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
      lastSnap = snap;
      renderStats(snap);
      renderRecent(snap.recent || []);
      renderPoison(snap.poison_previews || []);
      renderDaily(snap.daily || {});
      renderDailyByModel(snap.daily_by_model || {});
      renderSpark(snap.recent || []);
      renderPerf(snap);
      renderTokens(snap);
      renderTriState(snap);
      setConn(true);
      hideEvtTip();  // 重渲染后旧 tooltip 指向已换的 DOM，防悬空
    })
    .catch(function (err) {
      clearTimeout(timer);
      setConn(false, err && err.name === "AbortError" ? "轮询超时(4s)" : String(err));
    });
  pollHealth();
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
document.querySelector(".range-tabs").addEventListener("click", function (e) {
  var btn = e.target && e.target.closest ? e.target.closest(".range-tab") : null;
  if (!btn) return;
  var key = btn.getAttribute("data-range");
  if (!key || key === selectedRange) return;
  selectedRange = key;
  applyRange();  // 零请求重渲染：直接消费 lastSnap 缓存
});
applyRange();
poll();
setInterval(poll, 2000);
"""

DASHBOARD_HTML = ("".join([
    _DASH_HEAD, _DASH_SECTIONS_STATIC, _DASH_SECTIONS_TABLES,
    _DASH_SECTIONS_V2, _DASH_JS_CORE, _DASH_JS_V2,
    "</script>\n</body>\n</html>\n",
])).encode("utf-8")

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
    global UPSTREAM_BASE, _upstream_source, _stats_dirty, CAPTURE_ERRORS, MODEL_PRICING
    global PROBE_ENABLED
    UPSTREAM_BASE, _upstream_source = resolve_upstream_base(
        os.environ.get("CTYUN_UPSTREAM_BASE"), PERSIST_PATH)
    # v2 P4：probe 开关解析（env > persist > 默认 True），_probe_loop 启动前定值
    PROBE_ENABLED = resolve_probe_enabled(
        os.environ.get("CTYUN_PROBE_ENABLED", ""), load_probe_enabled(PERSIST_PATH))
    counters = load_stats_counters(PERSIST_PATH)  # 累计计数跨重启续算
    daily = load_daily_buckets(PERSIST_PATH)      # 按天分桶跨重启续算
    daily_by_model = load_daily_by_model_buckets(PERSIST_PATH)  # 按天×模型矩阵跨重启续算
    events = load_stats_events(PERSIST_PATH)      # 错误/重试事件流跨重启续算
    CAPTURE_ERRORS = load_capture_errors(PERSIST_PATH)  # 启动时回填开关
    # v2 P3：model_pricing 装载（env seam 优先，否则持久化文件；解析失败回落 {}）
    pricing_env = os.environ.get("CTYUN_MODEL_PRICING", "")
    if pricing_env:
        try:
            MODEL_PRICING = json.loads(pricing_env)
        except ValueError:
            # 吞掉的是 env 里非法 JSON 字符串：价目表 best-effort，回落 {} 即可，无其他路径可达。
            MODEL_PRICING = {}
        if not isinstance(MODEL_PRICING, dict):
            MODEL_PRICING = {}
    else:
        MODEL_PRICING = load_model_pricing(PERSIST_PATH)
    with STATS_LOCK:
        STATS["requests_total"] = counters["requests_total"]
        STATS["filtered_total"] = counters["filtered_total"]
        STATS["errors_total"] = counters["errors_total"]
        STATS["empty_retries_total"] = counters["empty_retries_total"]
        STATS["eof_without_done_total"] = counters["eof_without_done_total"]
        STATS["finish_retries_total"] = counters["finish_retries_total"]
        STATS["header_retries_total"] = counters["header_retries_total"]
        STATS["daily"] = daily
        STATS["daily_by_model"] = daily_by_model
        EVENTS.clear()
        EVENTS.extend(events)  # 先 clear 后 extend：防 deque 残留叠加
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
        _safe_log_stderr("ctyun-stream-fix-proxy: admin bind failed on %s:%d: %s — exit 1，"
                         "交由 launchd KeepAlive 重试" % (ADMIN_HOST, ADMIN_PORT, exc))
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

    # v2 P4：probe daemon（恒启动一次；enabled=False 时空转，见 _probe_loop docstring）
    threading.Thread(target=_probe_loop, daemon=True,
                     name="upstream-probe").start()

    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), ProxyHandler)
    server.daemon_threads = True
    _safe_log_stderr("ctyun-stream-fix-proxy listening on %s:%d -> %s (admin dashboard on %s:%d)"
                     % (LISTEN_HOST, LISTEN_PORT, UPSTREAM_BASE, ADMIN_HOST, ADMIN_PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        admin_server.server_close()


if __name__ == "__main__":
    main()
