# PLAN — Anthropic Messages API 支持与默认上游切换

- 日期: 2026-10-20
- Spec: `docs/superpowers/specs/2026-10-20-anthropic-messages-support-design.md`（同 worktree）
- 分支: `fix/anthropic-messages-support`（worktree `.worktrees/anthropic-messages-support`）
- Tier: 全卡 **C**（implementer 执行）——卡 2/3 命中"集成边界实测"（SSE relay 热路径 priming/finish_hold 状态机行为、loopback 全链路 flush 实时性），卡 1 命中"运行时数据依赖"（x-api-key > Bearer 的计费分桶口径需按真实网关凭证形态判断）；卡内代码已全部写死，C 仅为执行期判断与真机验证留白，非占位。
- 执行顺序: Card 1 → Card 2 → Card 3（串行，每卡 TDD 红→绿）

## Global Constraints

1. **测试先行（TDD）**：每卡先落测试 → 跑出本卡"红态举证"所列失败 → 再落生产代码 → 跑绿。禁止先写实现。
2. **字节透传不变**：转发层（`_relay_sse`/`_relay_buffered` 写路径）不新增任何改写；归一只发生在"判定与统计"边界（kind 查表 / usage 归一函数），帧字节原样下发。
3. **OpenAI 回归零改动**：现有 OpenAI 用例零修改通过；`choices` 非空路径逐行为等价（Card 2 重构仅为分支反转，判定体原样保留）。
4. **不加依赖**：纯 stdlib（http.server + http.client + re + hashlib），Python >= 3.9。
5. **单文件**：生产改动仅 `ctyun-stream-fix-proxy.py`；测试改动仅 `ctyun-stream-fix-proxy.test.py`。
6. **锚点行号**为 2026-10-20 worktree 实测值；执行时以 grep 重定位为准，漂移 ±10 行内不改卡。
7. **验证命令**：项目用 stdlib unittest（`:9749` `unittest.main(verbosity=2)`）。焦点验证 `python3 ctyun-stream-fix-proxy.test.py <ClassName> -v`；每卡收尾与最终验收跑全量 `python3 ctyun-stream-fix-proxy.test.py`（spec 验收 1：现有 OpenAI 用例零修改全绿）。
8. **Baseline**：开工前先跑一次全量 `python3 ctyun-stream-fix-proxy.test.py` 记录初始红清单（若有）；VERIFY 只对"baseline 绿 → 现在红"负责。

## 锚点实测（2026-10-20 worktree grep/Read 复核）

```
生产 ctyun-stream-fix-proxy.py:
  :7     docstring "生产默认: UPSTREAM_BASE=https://eaichat.ctyun.cn/ai/platform/v2/cp, 端口 7920;"
  :32    DEFAULT_UPSTREAM_BASE = "https://eaichat.ctyun.cn/ai/platform/v2/cp"
  :255   _SSE_NO_PARSED = object()
  :259   def sse_data_line_kind(line: bytes, parsed=_SSE_NO_PARSED) -> str:
  :306   def body_has_error(parsed) -> bool:
  :317   def sse_line_has_usage(line: bytes, parsed=_SSE_NO_PARSED) -> bool:
  :340   def sse_line_usage(line: bytes, parsed=_SSE_NO_PARSED):
  :367   def usage_dict_tokens(usage) -> tuple:      # 尾部 return 在 :411-412
  :415   def sse_line_extract_usage(line: bytes):
  :518   def tpm_key_id(auth_header):
  :2892  tpm_key = tpm_key_id(self.headers.get("Authorization"))
  :3062-3068 流式 TPM settle（:3065 total = self._tpm_usage.get("total_tokens")）
  :3117-3125 非流式 settle（:3122 total = usage.get("total_tokens")）
  :3199  def _relay_sse(self, resp, final: bool, model=None) -> tuple:
  :3222  def _flush_primed() -> None:                # :3291 elif "content" in kinds or "done" in kinds
  :3257-3261 usage 捕获（:3260 self._tpm_usage = usage_hit）
  :3819-3820 dashboard placeholder="https://eaichat.ctyun.cn/ai/platform/v2/cp"
测试 ctyun-stream-fix-proxy.test.py:
  :13 import hashlib  :336 make_scripted_upstream  :447 stop_proxy  :472 free_port
  :491 start_proxy  :525 post_sse  :650 admin_get
  :726 class ProxyDashboardUnitTest（含 :892 kind 矩阵 / :933 has_usage 矩阵 /
        :1007 usage 矩阵 / :1143 test_tpm_key_id / :742 resolve 优先级）
  :5598 class UsageExtractTest（extract = sse_line_extract_usage）
  :6255 class RelaySseV3Test（:6265 _StubHandler / :6293 _FakeSseResp / :6311 _drive）
  :6514 class SettlePersistV3Test（AnthropicSseTest 插入点 = 6255 类尾与 6514 之间）
  :9749 unittest.main(verbosity=2)
```

---

## Card 1 — 默认上游切换 + tpm_key_id 双头签名（x-api-key 优先）

- **Goal**: 默认 base 切到 Anthropic 端点（docstring/常量/dashboard placeholder 三处同步）；`tpm_key_id` 增 `api_key_header` 参数，x-api-key > Bearer 优先级，调用点透传 `x-api-key` 头。
- **Files**: `ctyun-stream-fix-proxy.py`（:7 / :32 / :3819-3820 / :518-533 / :2892）；`ctyun-stream-fix-proxy.test.py`（ProxyDashboardUnitTest 内追加 2 处）。
- **Tier**: C — 留白理由：双头优先级的计费分桶口径依赖真实网关凭证形态（运行时数据），执行期需按线上 header 组合复核。

### 生产代码（全部写死）

**1.1 `:7` docstring（整行替换）**
```python
# 旧
生产默认: UPSTREAM_BASE=https://eaichat.ctyun.cn/ai/platform/v2/cp, 端口 7920;
# 新
生产默认: UPSTREAM_BASE=https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic, 端口 7920;
```

**1.2 `:32`（整行替换）**
```python
# 旧
DEFAULT_UPSTREAM_BASE = "https://eaichat.ctyun.cn/ai/platform/v2/cp"
# 新
DEFAULT_UPSTREAM_BASE = "https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic"
```

**1.3 `:3819-3820` dashboard placeholder（整行替换）**
```html
# 旧
             placeholder="https://eaichat.ctyun.cn/ai/platform/v2/cp" aria-label="新的上游端点 URL">
# 新
             placeholder="https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic" aria-label="新的上游端点 URL">
```

**1.4 `:518-533` `tpm_key_id` 整函数替换**
```python
def tpm_key_id(auth_header, api_key_header=None):
    """从 x-api-key / Authorization 头提取 per-key 标识（x-api-key 优先）。

    x-api-key 为非空 str（strip 后非空）→ 对该值取 sha256；
    否则 Authorization "Bearer <token>" → token 的 sha256；
    两者皆无 → None（= 不限流直通）。返回 sha256 hex 全位（内部桶键用全位防碰撞，
    快照对外只显前 12 位脱敏）。
    优先级依据：x-api-key 是 Anthropic 原生计费凭证；网关 Bearer 可能多客户端共享，
    优先 Bearer 会让不同 x-api-key 撞同桶。空/非 str x-api-key 回落 Bearer。
    """
    token = api_key_header.strip() if isinstance(api_key_header, str) else ""
    if not token and auth_header:
        parts = auth_header.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
```

**1.5 `:2892` 调用点（整行替换）**
```python
# 旧
        tpm_key = tpm_key_id(self.headers.get("Authorization"))
# 新
        tpm_key = tpm_key_id(self.headers.get("Authorization"),
                             self.headers.get("x-api-key"))
```
（email.message 头查大小写不敏感；`x-api-key` 不在 `STRIP_HEADERS`，头透传不受影响。）

### 测试代码（全部写死，落 `ProxyDashboardUnitTest` 类内）

**1.T1 追加到 `test_tpm_key_id`（:1143）方法体末尾**（现有 10 行断言原样保留）：
```python
        # --- x-api-key 双头（Anthropic 原生计费凭证）：确定性、优先级、空值回落 ---
        kid_api = f(None, "k")  # 仅 x-api-key
        self.assertIsInstance(kid_api, str)
        self.assertEqual(len(kid_api), 64)
        self.assertEqual(f(None, "k"), kid_api)      # x-api-key 确定性
        # 双头优先级：x-api-key > Bearer（同 x-api-key 不同 Bearer 同桶）
        self.assertEqual(f("Bearer b", "k"), kid_api)
        self.assertNotEqual(f("Bearer b"), kid_api)  # 无 x-api-key 回落 Bearer → 不同桶
        # 空/None/非 str/纯空白 x-api-key → 回落 Bearer
        self.assertEqual(f("Bearer b", ""), f("Bearer b"))
        self.assertEqual(f("Bearer b", None), f("Bearer b"))
        self.assertEqual(f("Bearer b", 42), f("Bearer b"))
        self.assertEqual(f("Bearer b", "   "), f("Bearer b"))
        # 双双缺失 / 非 Bearer Authorization → None
        self.assertIsNone(f(None, None))
        self.assertIsNone(f("", ""))
        self.assertIsNone(f("Basic dXNlcjpwYXNz", None))
        # x-api-key 空白 token + 无 Bearer → None
        self.assertIsNone(f(None, "   "))
```

**1.T2 `ProxyDashboardUnitTest` 内新增方法**（放在 `test_resolve_upstream_base_precedence` 之后）：
```python
    def test_default_upstream_base_anthropic(self) -> None:
        """默认 base 已切 Anthropic 端点；resolve 优先级 env > file > default 由
        test_resolve_upstream_base_precedence 以 mod.DEFAULT_UPSTREAM_BASE 符号断言，不受值变更影响。"""
        self.assertEqual(
            self.mod.DEFAULT_UPSTREAM_BASE,
            "https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic")
```

### 红态举证
- 1.T1 首个双头断言 `TypeError: tpm_key_id() takes from 1 to 2 positional arguments but 2 were given`（现签名无第二参）。
- 1.T2 `AssertionError: 'https://eaichat.ctyun.cn/ai/platform/v2/cp/anthropic' != 'https://eaichat.ctyun.cn/ai/platform/v2/cp'`。
- 回归护栏：`test_valid_upstream_base`（:735 断言旧 URL 通过 `valid_upstream_url` 校验）不感知默认值，保持通过；`test_resolve_upstream_base_precedence` 用符号 `mod.DEFAULT_UPSTREAM_BASE` 断言，保持通过。

### 验证命令
```
$ python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest -v   # 焦点绿
$ python3 ctyun-stream-fix-proxy.test.py                             # 全量绿
```

---

## Card 2 — SSE 事件表 + Anthropic usage 归一 + usage_merge + TPM settle 用归一 total

- **Goal**: `sse_data_line_kind` 增 Anthropic 事件类型表（choices 空时查表）；`anthropic_usage_dict` 归一 message_start/message_delta usage；`sse_line_has_usage`/`sse_line_usage` 先走归一分支；`usage_dict_tokens` 增 input/output 别名与 total 兜底；新增 `usage_merge` 并改 `_relay_sse` 捕获点为按键合并；流式/非流式 TPM settle 改用 `usage_dict_tokens(...)[2]`。
- **Files**: `ctyun-stream-fix-proxy.py`（:255 后新增常量 / :259 / :315 后新增函数 / :317 / :340 / :367 / :412 后新增函数 / :3257-3261 / :3062-3068 / :3117-3125）；`ctyun-stream-fix-proxy.test.py`（:892 / :933 / :1007 / UsageExtractTest 追加）。
- **Tier**: C — 留白理由：事件表归类驱动 relay priming/finish_hold 状态机，正确性依赖真实 SSE 流实测（集成边界）；usage 归一键序依赖上游真实帧形态。

### 生产代码（全部写死）

**2.1 `:256`（`_SSE_NO_PARSED` 注释行之后、`def sse_data_line_kind` 之前）插入常量**
```python
# Anthropic Messages API 事件类型 → SSE 归类（spec 方案 A 边界归一化）：
# message_start/content_block_start/content_block_delta → content（首帧即 priming flush）；
# message_delta → finish（尾帧带 usage，走 finish_hold 缓冲判 usage——仅 priming 期生效）；
# message_stop → done（saw_done 判定，避免空流重试）；error → content（body error 同语义）；
# content_block_stop / ping → noise。
_ANTHROPIC_EVENT_KIND = {
    "message_start": "content", "content_block_start": "content",
    "content_block_delta": "content", "content_block_stop": "noise",
    "message_delta": "finish", "message_stop": "done",
    "error": "content", "ping": "noise",
}
```

**2.2 `:259-300` `sse_data_line_kind` 整函数替换**（docstring 同步；choices 分支反转为非空守卫，判定体原样；查表在 choices 空/缺失后、最终 noise 前）
```python
def sse_data_line_kind(line: bytes, parsed=_SSE_NO_PARSED) -> str:
    """SSE data 行归类："done" / "content" / "finish" / "noise"。判定序：
    非 data: 前缀（注释行/event:/id:）→ noise；[DONE] → done；
    JSON 解析失败 → content（fail-open：宁可不重试，不误判合法流）；
    dict 且顶层 error 对象 → content（body error 帧视为 content，触发 priming flush 不重试）；
    dict + choices 非空 list 时：delta.content 非空 str / delta.tool_calls 真值 →
    content；choice.finish_reason 非 None（且无前两者）→ finish；
    choices 缺失/为空且 type 命中 _ANTHROPIC_EVENT_KIND → 查表归类
    （message_start/content_block_start/content_block_delta/error → content；
    message_delta → finish；message_stop → done；content_block_stop/ping → noise）；
    其余（reasoning-only、空 delta、choices 为空的 usage 帧、data:null、非 dict JSON）→ noise。
    parsed 为 _SSE_NO_PARSED（默认）时保持原自行解析路径；显式传值（含 data:null 的 None）
    直接复用，不重复 json.loads。sentinel _SSE_PARSE_FAILED 等价解析失败 → content。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:"):
        return "noise"
    if DONE_RE.match(stripped):
        return "done"
    if parsed is _SSE_PARSE_FAILED:
        return "content"
    if parsed is _SSE_NO_PARSED:
        try:
            data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
        except ValueError:
            return "content"
    else:
        data = parsed
    if not isinstance(data, dict):
        return "noise"
    if body_has_error(data):
        return "content"
    choices = data.get("choices")
    if isinstance(choices, list) and choices:
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
    etype = data.get("type")
    if isinstance(etype, str) and etype in _ANTHROPIC_EVENT_KIND:
        return _ANTHROPIC_EVENT_KIND[etype]
    return "noise"
```

**2.3 `:315`（`body_has_error` 函数尾与 `def sse_line_has_usage` 之间）插入新函数**
```python
def anthropic_usage_dict(data):
    """从 Anthropic Messages SSE 帧提取 usage 并归一为 OpenAI 键。

    type == "message_start" → data["message"]["usage"]；type == "message_delta" →
    data["usage"]；其余 type / 非 dict → None。usage 非空 dict 才继续。
    归一映射：input_tokens → prompt_tokens；output_tokens → completion_tokens；
    cache_read_input_tokens 原名保留（usage_dict_tokens 既有第二别名直认）。
    值须 int ≥0 且非 bool 才收录；收录结果为空 → None。
    零 IO 零 parse：入参须为 json.loads 已产出的 dict。
    """
    if not isinstance(data, dict):
        return None
    etype = data.get("type")
    if etype == "message_start":
        message = data.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
    elif etype == "message_delta":
        usage = data.get("usage")
    else:
        return None
    if not isinstance(usage, dict) or not usage:
        return None
    out = {}
    for src, dst in (("input_tokens", "prompt_tokens"),
                     ("output_tokens", "completion_tokens")):
        value = usage.get(src)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[dst] = value
    cache_read = usage.get("cache_read_input_tokens")
    if isinstance(cache_read, int) and not isinstance(cache_read, bool) and cache_read >= 0:
        out["cache_read_input_tokens"] = cache_read
    return out or None
```

**2.4 `:317-337` `sse_line_has_usage` 整函数替换**（仅 docstring 补一句 + dict 守卫后插归一分支）
```python
def sse_line_has_usage(line: bytes, parsed=_SSE_NO_PARSED) -> bool:
    """判 SSE data 行是否携带非空 usage 帧（独立 usage 帧 choices=[] 与 ride-on finish
    帧都覆盖；Anthropic message_start/message_delta 帧经 anthropic_usage_dict 归一判定）；
    DONE/非 JSON/非 data 行 → False。
    _SSE_NO_PARSED（默认）自行解析；显式传值（含 None）复用跳过 json.loads；
    sentinel/解析失败 → False。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    if parsed is _SSE_PARSE_FAILED:
        return False
    if parsed is _SSE_NO_PARSED:
        try:
            data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
        except ValueError:
            return False
    else:
        data = parsed
    if not isinstance(data, dict):
        return False
    if anthropic_usage_dict(data) is not None:
        return True
    usage = data.get("usage")
    return isinstance(usage, dict) and bool(usage)
```

**2.5 `:340-364` `sse_line_usage` 整函数替换**（同位置插归一分支；必须先于顶层 usage 分支——message_delta 的 usage 原生是 Anthropic 键，先走原分支会把 `output_tokens` 原样返回，合并时无法覆盖 `completion_tokens`）
```python
def sse_line_usage(line: bytes, parsed=_SSE_NO_PARSED):
    """返回 SSE data 行中的 usage dict，无 usage 帧返回 None。

    解析逻辑与 sse_line_has_usage 同序：非 data: 前缀/[DONE]/非 JSON/非 dict →
    None；Anthropic message_start/message_delta 帧先经 anthropic_usage_dict 归一
    （须先于顶层 usage 分支，否则 Anthropic 原生键原样漏出）；其余顶层 usage
    非空 dict → 返回该 dict（非流式 Anthropic body 的顶层 usage 由此分支兜底，
    数值化靠 usage_dict_tokens 的 input/output 别名）；其余 → None。
    独立 usage 帧（choices=[]）与 ride-on finish 帧均覆盖。
    _SSE_NO_PARSED（默认）自行解析；显式传值（含 None）复用跳过 json.loads；
    sentinel → None（同解析失败）。
    """
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return None
    if parsed is _SSE_PARSE_FAILED:
        return None
    if parsed is _SSE_NO_PARSED:
        try:
            data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
        except ValueError:
            return None
    else:
        data = parsed
    if not isinstance(data, dict):
        return None
    anth = anthropic_usage_dict(data)
    if anth is not None:
        return anth
    usage = data.get("usage")
    return usage if isinstance(usage, dict) and usage else None
```

**2.6 `:367-412` `usage_dict_tokens`**：docstring 增一行（"Anthropic 别名"段，插在 reasoning 两级描述之后），并把尾部 `:411-412` 的 `return (...)` 替换为别名 + total 兜底块。docstring 追加：
```
    Anthropic 别名：input_tokens/output_tokens 作 prompt/completion 的次级别名
    （OpenAI 键优先）；total_tokens 缺失时兜底 prompt+completion（Anthropic 无 total）。
```
尾部代码替换（`# cache_read：三级别名序…` 至 `return` 前的所有既有行原样保留）：
```python
    # 旧（:411-412）
    return (_int_field("prompt_tokens"), _int_field("completion_tokens"),
            _int_field("total_tokens"), cache_read, reasoning)
    # 新
    prompt = _int_field("prompt_tokens") or _int_field("input_tokens")
    completion = _int_field("completion_tokens") or _int_field("output_tokens")
    total = _int_field("total_tokens")
    if total == 0 and (prompt or completion):
        total = prompt + completion
    return (prompt, completion, total, cache_read, reasoning)
```
（`cache_read_input_tokens` 已是既有第二别名，无需改。）

**2.7 `:412`（`usage_dict_tokens` 函数尾与 `def sse_line_extract_usage` 之间）插入新函数**
```python
def usage_merge(acc, new):
    """usage 按键合并：Anthropic 的 message_start 只带 input、message_delta 只带
    output，分帧到达须合并累积；OpenAI 单 usage 帧 last-wins 语义等价（键冲突新值胜）。
    new 非空 dict 才合并；acc 非 dict 视为空表；恒返回新 dict，不改入参。"""
    if not isinstance(new, dict) or not new:
        return acc
    merged = dict(acc) if isinstance(acc, dict) else {}
    merged.update(new)
    return merged
```

**2.8 `:3255-3261` `_relay_sse` usage 捕获点替换**（注释 + 循环体）
```python
# 旧
                    # TPM usage 捕获：本 record 内所有行取最后非空 usage 帧；
                    # 复用预解析 dict（D）——全程每 data 行恰 1 次 json.loads（R1）
                    for i, buf_line in enumerate(pending):
                        usage_hit = sse_line_usage(buf_line, parsed=pre_parsed[i])
                        if usage_hit is not None:
                            self._tpm_usage = usage_hit
                            self._p3_usage_tokens = usage_dict_tokens(usage_hit)
# 新
                    # TPM usage 捕获：按键合并累积（Anthropic message_start 带
                    # input、message_delta 带 output，分帧合两为一）；OpenAI 单帧
                    # last-wins 语义不变。复用预解析 dict（D）——全程每 data 行
                    # 恰 1 次 json.loads（R1）
                    for i, buf_line in enumerate(pending):
                        usage_hit = sse_line_usage(buf_line, parsed=pre_parsed[i])
                        if usage_hit is not None:
                            self._tpm_usage = usage_merge(self._tpm_usage, usage_hit)
                            self._p3_usage_tokens = usage_dict_tokens(self._tpm_usage)
```

**2.9 `:3062-3068` 流式 TPM settle 替换**
```python
# 旧
            # TPM settle：用上游实际 usage 校正窗口占用（无 usage 帧不校正）
            tpm_final_used = tpm_est if tpm_key is not None else None
            if tpm_key is not None and self._tpm_usage is not None:
                total = self._tpm_usage.get("total_tokens")
                if isinstance(total, int) and total >= 0:
                    tpm_settle(tpm_key, tpm_est, total, model)
                    tpm_final_used = total
# 新
            # TPM settle：用归一五元组的 total（含 prompt+completion 兜底）校正
            # 窗口占用；无 usage 帧（_tpm_usage None）不校正
            tpm_final_used = tpm_est if tpm_key is not None else None
            tokens = usage_dict_tokens(self._tpm_usage) if self._tpm_usage is not None else None
            if tpm_key is not None and tokens is not None:
                tpm_settle(tpm_key, tpm_est, tokens[2], model)
                tpm_final_used = tokens[2]
```

**2.10 `:3117-3125` 非流式 settle 替换**（`p3_buf_tokens` 之后用其 `[2]` 结算，不再 `usage.get("total_tokens")`）
```python
# 旧
                    if isinstance(parsed, dict):
                        usage = parsed.get("usage")
                        if isinstance(usage, dict):
                            p3_buf_tokens = usage_dict_tokens(usage)
                            if tpm_key is not None:
                                total = usage.get("total_tokens")
                                if isinstance(total, int) and total >= 0:
                                    tpm_settle(tpm_key, tpm_est, total, model)
                                    tpm_final_used = total
# 新
                    if isinstance(parsed, dict):
                        usage = parsed.get("usage")
                        if isinstance(usage, dict):
                            p3_buf_tokens = usage_dict_tokens(usage)
                            if tpm_key is not None and p3_buf_tokens is not None:
                                total = p3_buf_tokens[2]
                                if isinstance(total, int) and total >= 0:
                                    tpm_settle(tpm_key, tpm_est, total, model)
                                    tpm_final_used = total
```

### 测试代码（全部写死）

**2.T1 `test_sse_data_line_kind_matrix`（:892）方法体末尾追加**：
```python
        # --- Anthropic Messages 事件类型表（8 type + event: 行 + 未知 type）---
        self.assertEqual(
            f(b'data: {"type":"message_start","message":{"usage":{}}}\n'), "content")
        self.assertEqual(
            f(b'data: {"type":"content_block_start","index":0}\n'), "content")
        self.assertEqual(
            f(b'data: {"type":"content_block_delta",'
              b'"delta":{"type":"text_delta","text":"Hi"}}\n'), "content")
        self.assertEqual(
            f(b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n'),
            "finish")
        self.assertEqual(f(b'data: {"type":"message_stop"}\n'), "done")
        self.assertEqual(f(b'data: {"type":"content_block_stop","index":0}\n'), "noise")
        self.assertEqual(f(b'data: {"type":"ping"}\n'), "noise")
        # Anthropic error 帧（type=error 且 error 为 dict）→ body_has_error 最优先 → content
        self.assertEqual(
            f(b'data: {"type":"error","error":{"type":"overloaded_error",'
              b'"message":"Overloaded"}}\n'), "content")
        # event: 前缀行（Anthropic 流每 record 自带）→ 仍 noise
        self.assertEqual(f(b"event: content_block_delta\n"), "noise")
        # 未知 type 且无 choices → noise
        self.assertEqual(f(b'data: {"type":"unknown_event"}\n'), "noise")
        # choices 非空时 OpenAI 路径完全不变（type 键共存不抢判定）
        self.assertEqual(
            f(b'data: {"type":"x","choices":[{"delta":{"content":"A"}}]}\n'), "content")
```

**2.T2 `test_sse_line_has_usage_matrix`（:933）方法体末尾追加**：
```python
        # --- Anthropic 归一布尔判定 ---
        self.assertTrue(f(b'data: {"type":"message_start","message":'
                          b'{"usage":{"input_tokens":10}}}\n'))
        self.assertTrue(f(b'data: {"type":"message_delta","usage":{"output_tokens":7}}\n'))
        self.assertFalse(f(b'data: {"type":"message_start","message":{"usage":{}}}\n'))
        self.assertFalse(f(b'data: {"type":"message_delta","usage":{}}\n'))
        self.assertFalse(f(b'data: {"type":"ping"}\n'))
        self.assertFalse(f(b'data: {"type":"message_stop"}\n'))
```

**2.T3 `test_sse_line_usage_matrix`（:1007）方法体末尾追加**：
```python
        # --- Anthropic 归一：message_start（嵌套 message.usage）→ OpenAI 键 ---
        result3 = f(b'data: {"type":"message_start","message":{'
                    b'"id":"msg_1","usage":{"input_tokens":10,'
                    b'"output_tokens":1}}}\n')
        self.assertEqual(result3, {"prompt_tokens": 10, "completion_tokens": 1})
        # message_delta（顶层 usage，Anthropic 原生键）→ 归一（不得原样漏出 output_tokens）
        result4 = f(b'data: {"type":"message_delta",'
                    b'"delta":{"stop_reason":"end_turn"},'
                    b'"usage":{"output_tokens":7}}\n')
        self.assertEqual(result4, {"completion_tokens": 7})
        # cache_read_input_tokens 原名保留
        result5 = f(b'data: {"type":"message_delta","usage":'
                    b'{"output_tokens":7,"cache_read_input_tokens":40}}\n')
        self.assertEqual(result5,
                         {"completion_tokens": 7, "cache_read_input_tokens": 40})
        # 空 usage → None（两帧型各一）
        self.assertIsNone(
            f(b'data: {"type":"message_start","message":{"usage":{}}}\n'))
        self.assertIsNone(f(b'data: {"type":"message_delta","usage":{}}\n'))
        # message 非 dict → None
        self.assertIsNone(f(b'data: {"type":"message_start","message":"oops"}\n'))
        # 非用法事件 type 带顶层 usage → 走既有顶层分支原样返回（语义不变）
        self.assertEqual(
            f(b'data: {"type":"content_block_delta","usage":{"input_tokens":3}}\n'),
            {"input_tokens": 3})
        # OpenAI 帧（choices + 顶层 usage）语义不变
        self.assertEqual(
            f(b'data: {"choices":[],"usage":{"prompt_tokens":1}}\n'),
            {"prompt_tokens": 1})
```

**2.T4 `UsageExtractTest`（:5598）类内新增方法**（追加在 `test_cache_read_alias_priority_first_hit_wins` 等既有方法之后）：
```python
    # ---- Anthropic 别名：input_tokens/output_tokens + total 兜底 ----

    def test_anthropic_input_output_tokens_alias(self) -> None:
        line = b'data: {"usage":{"input_tokens":10,"output_tokens":7}}\n\n'
        self.assertEqual(self.extract(line), (10, 7, 17, 0, 0))

    def test_anthropic_alias_with_cache_read(self) -> None:
        line = (b'data: {"usage":{"input_tokens":10,"output_tokens":7,'
                b'"cache_read_input_tokens":40}}\n\n')
        self.assertEqual(self.extract(line), (10, 7, 17, 40, 0))

    def test_explicit_total_wins_over_fallback(self) -> None:
        line = (b'data: {"usage":{"input_tokens":10,"output_tokens":7,'
                b'"total_tokens":99}}\n\n')
        self.assertEqual(self.extract(line), (10, 7, 99, 0, 0))

    def test_openai_keys_win_over_anthropic_alias(self) -> None:
        line = (b'data: {"usage":{"prompt_tokens":3,"input_tokens":10,'
                b'"completion_tokens":2,"output_tokens":7}}\n\n')
        self.assertEqual(self.extract(line), (3, 2, 5, 0, 0))

    def test_total_fallback_only_when_tokens_present(self) -> None:
        # 无任何 token 计数 → total 保持 0（不产出假总数）
        line = b'data: {"usage":{"reasoning_tokens":5}}\n\n'
        self.assertEqual(self.extract(line), (0, 0, 0, 0, 5))

    def test_anthropic_message_start_end_to_end(self) -> None:
        """端到端：Anthropic SSE 帧经 sse_line_usage 归一 + usage_dict_tokens 数值化。"""
        mod = load_proxy_module()
        line = (b'data: {"type":"message_start","message":'
                b'{"usage":{"input_tokens":10,"output_tokens":1}}}\n\n')
        usage = mod.sse_line_usage(line)
        self.assertEqual(usage, {"prompt_tokens": 10, "completion_tokens": 1})
        self.assertEqual(mod.usage_dict_tokens(usage), (10, 1, 11, 0, 0))
```

### 红态举证
- 2.T1：message_start 等帧现无 choices → `noise != content`（首个 Anthropic 断言即红）。
- 2.T3：message_start 现返回 `None`（无顶层 usage）→ `assertEqual(None, {...})` 红；message_delta 现返回原生键 `{"output_tokens": 7}` → 红。
- 2.T4：`test_anthropic_input_output_tokens_alias` 现得 `(0, 0, 0, 0, 0)` → 红。
- Card 2 落地后 2.T1-2.T4 全绿，且既有 OpenAI 断言零修改通过。

### 验证命令
```
$ python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest -v   # 焦点绿
$ python3 ctyun-stream-fix-proxy.test.py UsageExtractTest -v         # 焦点绿
$ python3 ctyun-stream-fix-proxy.test.py                             # 全量绿
```

---

## Card 3 — AnthropicSseTest：flush 实时性 + usage 分帧合并 + 集成字节透传

- **Goal**: 新增 `AnthropicSseTest`（插在 `RelaySseV3Test` 类尾与 `SettlePersistV3Test`（:6514）之间）：进程内复用 `_StubHandler`/`_FakeSseResp` 举证 message_start 首帧 priming flush（第二次 data 读取前 wfile 已非空）与 usage 分帧合并（10/7→17）；集成走 `make_scripted_upstream(body_override=...)` + `start_proxy` + `post_sse` 断言字节逐字节相同、`calls == 1`、daily per-key usage 入桶。
- **Files**: `ctyun-stream-fix-proxy.test.py`（新增 1 个测试类，4 个用例）。
- **Tier**: C — 留白理由：纯集成边界实测（loopback socket + 子进程代理 + flush 时序探针）。

### 测试代码（全部写死，整类新增）

```python
class AnthropicSseTest(unittest.TestCase):
    """Anthropic Messages SSE（spec 方案 A 边界归一化）。

    进程内：事件表归类 → message_start 首帧 priming flush 实时性举证（探针在
    第 4 次 readline 入口采样 wfile——此时首 record 含终结空行已处理完毕）+
    usage 分帧按键合并归一（message_start 10 + message_delta 7 → 17）。
    集成：body_override 全链路字节逐字节透传 + calls == 1（message_stop→done
    不触发 empty-retry）+ daily per-key usage 入桶（x-api-key → sha256[:12]）。
    复用 RelaySseV3Test._StubHandler/_FakeSseResp。"""

    ANTHROPIC_STREAM = (
        b'event: message_start\n'
        b'data: {"type":"message_start","message":{"id":"msg_1","role":"assistant",'
        b'"usage":{"input_tokens":10,"output_tokens":1}}}\n'
        b'\n'
        b'event: content_block_start\n'
        b'data: {"type":"content_block_start","index":0,'
        b'"content_block":{"type":"text","text":""}}\n'
        b'\n'
        b'event: content_block_delta\n'
        b'data: {"type":"content_block_delta","index":0,'
        b'"delta":{"type":"text_delta","text":"Hi"}}\n'
        b'\n'
        b'event: content_block_stop\n'
        b'data: {"type":"content_block_stop","index":0}\n'
        b'\n'
        b'event: message_delta\n'
        b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
        b'"usage":{"output_tokens":7}}\n'
        b'\n'
        b'event: message_stop\n'
        b'data: {"type":"message_stop"}\n'
        b'\n')

    def setUp(self) -> None:
        self.mod = load_proxy_module()

    def _drive(self, resp, handler):
        """STALL_THRESHOLD_S=0.0 下进程内直驱 _relay_sse，返回 (filtered, truncated)。"""
        mod = self.mod
        orig = mod.STALL_THRESHOLD_S
        try:
            mod.STALL_THRESHOLD_S = 0.0
            return mod.ProxyHandler._relay_sse(handler, resp, final=True,
                                               model="claude-x")
        finally:
            mod.STALL_THRESHOLD_S = orig

    def test_message_start_flushes_before_next_read(self) -> None:
        """flush 实时性：第 4 次 readline 入口（首 record 三行已喂入、终结空行已
        处理）采样 wfile——必已含 message_start 字节，证明未缓冲到 EOF。"""
        mod = self.mod
        probe = {"wfile": None}

        class _ProbeResp(RelaySseV3Test._FakeSseResp):
            def readline(self_inner):
                if self_inner._i == 3 and probe["wfile"] is None:
                    probe["wfile"] = handler.wfile.getvalue()
                return RelaySseV3Test._FakeSseResp.readline(self_inner)

        handler = RelaySseV3Test._StubHandler()
        resp = _ProbeResp(self.ANTHROPIC_STREAM.splitlines(keepends=True))
        filtered, truncated = self._drive(resp, handler)
        self.assertIsNotNone(probe["wfile"], "探针必须已采样（流未提前耗尽）")
        self.assertIn(b'"type":"message_start"', probe["wfile"],
                      "下一 record 读取前 message_start 必须已 flush（未缓冲到 EOF），"
                      "got %r" % probe["wfile"])
        self.assertEqual((filtered, truncated), (0, False))

    def test_anthropic_usage_merged_across_frames(self) -> None:
        """message_start(input=10/output=1) + message_delta(output=7) → 按键合并
        {prompt_tokens:10, completion_tokens:7}；五元组 (10,7,17,0,0)；
        settle 表达式 tokens[2] == 17。"""
        mod = self.mod
        handler = RelaySseV3Test._StubHandler()
        resp = RelaySseV3Test._FakeSseResp(
            self.ANTHROPIC_STREAM.splitlines(keepends=True))
        filtered, truncated = self._drive(resp, handler)
        self.assertEqual((filtered, truncated), (0, False))
        self.assertEqual(handler._tpm_usage,
                         {"prompt_tokens": 10, "completion_tokens": 7})
        self.assertEqual(handler._p3_usage_tokens, (10, 7, 17, 0, 0))
        tokens = (mod.usage_dict_tokens(handler._tpm_usage)
                  if handler._tpm_usage is not None else None)
        self.assertIsNotNone(tokens)
        self.assertEqual(tokens[2], 17)  # tpm_settle(tpm_key, tpm_est, tokens[2], model)

    def test_integration_byte_identical_single_call(self) -> None:
        """集成：Anthropic 流逐字节透传，calls == 1（message_stop→done 不触发
        empty-retry）。fake upstream 正常路径恒回 text/event-stream，必走流式分支。"""
        upstream_port, calls = make_scripted_upstream(
            body_override=self.ANTHROPIC_STREAM)
        self.addCleanup(stop_fake_upstreams)
        proc = start_proxy(upstream_port, free_port())
        self.addCleanup(stop_proxy, proc)
        data = post_sse(proc.proxy_port,
                        payload=b'{"model":"claude-x","stream":true,'
                                b'"max_tokens":64,"messages":[]}')
        self.assertEqual(data, self.ANTHROPIC_STREAM,
                         "Anthropic 流必须逐字节透传，got %r" % data)
        self.assertEqual(len(calls), 1,
                         "message_stop→done 不得触发 empty-retry, calls=%d"
                         % len(calls))

    def test_integration_usage_daily_by_key(self) -> None:
        """集成：带 x-api-key 的 Anthropic 流式请求 → /api/stats
        daily_by_key[sha256("itest-key")[:12]] tokens_prompt=10 / completion=7。"""
        upstream_port, calls = make_scripted_upstream(
            body_override=self.ANTHROPIC_STREAM)
        self.addCleanup(stop_fake_upstreams)
        proc = start_proxy(upstream_port, free_port())
        self.addCleanup(stop_proxy, proc)
        conn = http.client.HTTPConnection("127.0.0.1", proc.proxy_port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     body=b'{"model":"claude-x","stream":true,'
                          b'"max_tokens":64,"messages":[]}',
                     headers={"Content-Type": "application/json",
                              "x-api-key": "itest-key"})
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        data = resp.read()
        conn.close()
        self.assertEqual(data, self.ANTHROPIC_STREAM)
        _, body, _ = admin_get(proc.admin_port, "/api/stats")
        snap = json.loads(body.decode("utf-8"))
        days = snap["daily_by_key"]
        self.assertEqual(len(days), 1, "只有今天一个 day 桶，got %r" % days)
        kid12 = hashlib.sha256(b"itest-key").hexdigest()[:12]
        entry = next(iter(days.values())).get(kid12)
        self.assertIsNotNone(entry, "x-api-key 派生 key_id12 必须入 daily 桶")
        self.assertEqual(entry["tokens_prompt"], 10)
        self.assertEqual(entry["tokens_completion"], 7)
```

### 红态举证（Card 2 未落地时或单独先落本卡）
- `test_message_start_flushes_before_next_read`：现行归类全 noise → priming 不 flush → 探针采样 wfile 为空 → `assertIn` 红。
- `test_anthropic_usage_merged_across_frames`：`handler._tpm_usage` 为 `None` → 红。
- `test_integration_byte_identical_single_call`：首呼 final=False 缓冲到 EOF → `_EmptyStream` 重试 → `calls == 2` → 红（字节相等因重放仍绿，红点在 calls）。
- `test_integration_usage_daily_by_key`：x-api-key 不被旧 `tpm_key_id` 识别 → key_id12 None → daily 无桶 → `assertIsNotNone(entry)` 红。

### 验证命令
```
$ python3 ctyun-stream-fix-proxy.test.py AnthropicSseTest -v         # 焦点绿（4 用例）
$ python3 ctyun-stream-fix-proxy.test.py                             # 全量绿（最终验收）
```

---

## Acceptance ↔ 卡映射

| spec 验收 | 卡 | 证据 |
|---|---|---|
| 1 全量绿、OpenAI 零修改 | 全部 | `python3 ctyun-stream-fix-proxy.test.py` exit 0 |
| 2 事件表归类 | Card 2 (2.T1) | kind 矩阵 11 断言 |
| 3 flush 实时性 | Card 3 (探针) | 第 4 次 readline 前 wfile 含 message_start |
| 4 字节一致 + calls==1 | Card 3 (集成) | `data == ANTHROPIC_STREAM`、`len(calls)==1` |
| 5 usage 10/7/17 + settle 17 | Card 2 (2.T4) + Card 3 (合并/集成) | 五元组 + tokens[2]==17 + daily 桶 |
| 6 tpm_key_id 双头 | Card 1 (1.T1) | 优先级/确定性/回落矩阵 |
| 7 DEFAULT_UPSTREAM_BASE | Card 1 (1.T2) | 常量断言；优先级由 :742 既有用例守护 |
| 8 非流式 Anthropic body | Card 2 (2.6 别名 + 2.10 settle) | usage_dict_tokens 别名兜底（非流式透传路径零改动） |

## Self-Review（5 项）

1. **spec coverage**：spec Files 8 项 → Card 1 覆盖项 1/7（base+docstring+placeholder+tpm_key_id），Card 2 覆盖项 2/3/4/5/6，Card 3 覆盖项 8；验收 1-8 全部映射到卡与断言（见上表）。Exclusions 未越界：无协议转换、无 normalize_null_assistant_content 改动、无 schema/env/依赖变更。
2. **placeholder scan**：三卡生产/测试代码块全部为可逐字落盘的完整代码，无 `<TODO>`/伪码/指令式描述；docstring 同步文本亦写死。
3. **type consistency**：`tpm_key_id(auth_header, api_key_header=None)` 与调用点两实参一致；`anthropic_usage_dict(data) -> dict|None` 与两处消费（has_usage/usage）一致；`usage_merge(acc, new) -> dict|acc` 与 `self._tpm_usage` 赋值一致；`usage_dict_tokens` 仍返回 5 元组，`tokens[2]`/`p3_buf_tokens[2]` 消费位次不变；`_relay_sse` 返回 `(filtered, truncated)` 未动。
4. **可落盘性**：所有编辑给出锚点行号 + 前后对照或整函数替换；插入点均给"某函数与某函数之间"实测锚；测试落点给到类名与方法体末尾；2.2/2.4/2.5 给整函数替换避免部分匹配歧义。
5. **锚点实测**：Header 锚点表为 2026-10-20 对 worktree 文件 Read/grep 实测结果（非 spec 转抄）；关键行为锚（`:3291` priming flush 判定、`:3333-3336` EOF 分支、finish_hold 仅 priming 期生效、fake upstream body_override 路径恒回 `text/event-stream`、daily_by_key 以 `tpm_key[:12]` 入桶、`:735` 旧 URL 仅测 `valid_upstream_url` 不 pin 默认值）均经源码 Read 核实。

## Risks（执行期注意）

- 已持久化旧 base（file > default）优先于新默认，测试环境经 `CTYUN_UPSTREAM_BASE` seam 隔离不受影响；线上切换需 POST /api/config（spec Risks 已记，不属本 episode）。
- 2.9/2.10 行为口径变化：total 缺失时 settle 改用 prompt+completion 兜底（spec Risks 已声明接受）；usage 帧存在但完全无法解析数值时 settle 以 0 退款（原为不结算）——2.T4 `test_total_fallback_only_when_tokens_present` 守护假总数不产生。
- 集成用例依赖 fake upstream 正常路径的 `text/event-stream` 头（`:202-210` 实测恒设），若上游 harness 变更需同步复核。
