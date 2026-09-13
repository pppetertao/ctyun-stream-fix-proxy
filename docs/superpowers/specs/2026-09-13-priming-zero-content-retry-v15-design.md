# priming 零内容流判别与 finish-form 重试设计（ctyun-stream-fix-proxy v1.5）

## Goal
上游新故障形态（09-13 生产取证 44 例）：reasoning 起步后 finish_reason 帧正常到达并带 [DONE] 收尾，但全程零 delta.content / 零 tool_calls / 零 usage 帧。v1.4 把 finish_reason 判为 content 即刻 flush，不触发重试，客户端收到"合法但全空"回合即报错。v1.5 在 priming 期对 finish record 做 hold：缓冲 finish 后尾段至 EOF，按"有无非空 usage 帧"判别——有 usage = 合法零内容流原样 flush 不重试（保持 v1.4 语义）；无 usage = 故障形态，弃缓冲走既有透明重试（共享每请求上限 1）。

## Files to Change
改动限单文件项目，共 2 文件。运行副本 `~/.local/bin/ctyun-stream-fix-proxy.py` 与仓库归档已核对：总行数 1453 全等、全部 def/class 行号逐条全等、:653-712 `_relay_sse` 区逐字全等（架构侧无 Bash，字面 `cmp` 由主代理实施前终检）。以下行号以仓库归档为准。

**`/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`**

1. `:50` 后加常量 `PRIMED_TAIL_CAP = 262144`（finish hold 尾段字节上限，超限 fail-open）。
2. `sse_data_line_kind` (:53-84)：finish_reason 从 content 拆出——`delta.content` 非空 str / `delta.tool_calls` 真值 → `"content"`；`choice.get("finish_reason") is not None`（且无前两者）→ **`"finish"`**（新值域）；JSON 解析失败仍 `"content"`（fail-open）；docstring 同步。
3. 新函数（置于 :85 后）：
   ```python
   def sse_line_has_usage(line: bytes) -> bool:
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
   ```
   顶层 `usage` 键判非空 dict：独立 usage 帧（choices=[]）与 ride-on finish 帧都覆盖。
4. `_EmptyStream.__init__` (:89)：签名加 `reason: str = "eof-priming"`，存 `self.reason`。
5. 计数器接线（4 处机械改动）：`STATS` (:101-103) 加 `"finish_retries_total": 0`；`save_stats_counters` counters 元组 (:247-248)、`load_stats_counters` key 清单 (:284-285)、`main()` 恢复块 (:1396-1400) 各加该键（旧持久化缺键 load 0，零迁移）。
6. `_record_empty_retry` (:456) 签名 `(model=None, reason: str = "eof-priming")`：`STATS["empty_retries_total"] += 1` 后加 `if reason == "finish-no-usage": STATS["finish_retries_total"] += 1`；daily/daily_by_model/EVENTS 逻辑不变（两形态共用 `retries` 维度与 `"retry"` kind）。
7. `_relay_sse` (:655-710) 核心改造：循环前加 `finish_hold = False`、`saw_usage = False`、`hold_bytes = 0`，并提局部闭包 `_flush_primed()`（补头 + write + flush，替换 :681-683 与 :696-698 既有两处重复）。priming record 分支（:675-684 替换）：
   ```python
   if not saw_usage:
       saw_usage = any(sse_line_has_usage(l) for l in pending)
   if finish_hold:
       hold_bytes += sum(len(l) for l in pending) + len(line)
       if "content" in kinds:              # fail-open：finish 后反常 content
           _flush_primed(); priming = False; finish_hold = False
       elif hold_bytes > PRIMED_TAIL_CAP:  # 恶意/异常长尾 fail-open
           _flush_primed(); priming = False; finish_hold = False
       # done/finish/noise（含 usage）：继续缓冲尾段
   elif "content" in kinds or "done" in kinds:
       _flush_primed(); priming = False    # v1.4 原语义不变
   elif "finish" in kinds:
       finish_hold = True                  # hold 至 EOF 判 usage
   ```
   EOF 且 priming 判定表（替换 :694-700）：
   | finish_hold | saw_usage | final | 动作 |
   |---|---|---|---|
   | T | T | 任意 | `_flush_primed()`（合法零内容流，不重试） |
   | T | F | F | `raise _EmptyStream(filtered, primed, reason="finish-no-usage")` → 既有重试路径 |
   | T | F | T | `_flush_primed()`（预算已尽，fail-open 原样下发） |
   | F | — | T | `_flush_primed()`（v1.4 原语义：switch off / 重试流） |
   | F | — | F | `raise _EmptyStream(filtered, primed, reason="eof-priming")`（v1.4 原语义） |
   streaming 阶段、poison 分支、`saw_done`/`truncated` 全部不动。
8. `_proxy_relay`：`retried = 0`（:600）旁初始化 `retry_reason = ""`；catch 块 (:603-606) 加 `retry_reason = exc.reason`、`_record_empty_retry(model, exc.reason)`；两处 `_log`（:612 retry 后 502 传 `exc.reason`、:623 常规传 `retry_reason`）透传。
9. `_log` (:739-745)：签名加 `retry_reason: str = ""`，格式串 `retried=%d retry_reason=%s ts=%s`，值 `retry_reason or "-"`（对齐 `model or "-"` 惯例）。

**`/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`**

- 新常量（:36 后）：`SSE_FINISH = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'`、`SSE_USAGE = b'data: {"id":"u","choices":[],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'`、`SSE_FAULT_TAIL = SSE_REASONING + SSE_FINISH + SSE_DONE`。
- `FakeUpstreamHandler` (:42) 镜像 empty_stream 机制（:67-68 分支结构）加类属性 `fault_finish_first`（scripted 首呼回 SSE_FAULT_TAIL 后断连，次呼走正常 body）/ `fault_finish_stream`（每呼回故障尾段），`make_fake_upstream`/`make_scripted_upstream`（:134/:150）透传。
- `test_sse_data_line_kind_matrix` (:423)：:432 finish 断言 `"content"` → `"finish"`；新增 content+finish 同帧 → `"content"`、tool_calls+finish 同帧 → `"content"`。
- 新单测 `test_sse_line_has_usage_matrix`（:446 后）：真（独立 usage 帧 / ride-on finish 帧）、假（空 usage `{}`、`usage:null`、[DONE]、非 JSON、非 data 行）。
- `test_req_log_line_has_ts_and_model` (:1321)：正则 `retried=\d+ ` 后插 `retry_reason=\S+ `。
- 精确断言补键 `finish_retries_total`：:453-455 与 :458-460（load 零值 dict 两处）、:488-491（snapshot 键清单）、:1154-1157（admin 键清单）。
- 新集成用例（仿 :1464-1569 scripted 结构）：`test_finish_with_usage_zero_content_legal_no_retry`（body_override=SSE_REASONING+SSE_FINISH+SSE_USAGE+SSE_DONE → calls==1、逐字节全量、result=ok）；`test_finish_without_usage_retried_second_stream_relayed`（fault_finish_first → calls==2、data==SSE_A+SSE_B+SSE_DONE 零字节重复、stderr 含 `retried=1 retry_reason=finish-no-usage`）；`test_double_finish_fault_falls_back_after_two_calls`（fault_finish_stream → calls==2、data==SSE_FAULT_TAIL）；`test_finish_fault_counters_and_req_line`（/api/stats 的 empty_retries_total≥1 + finish_retries_total≥1 + daily[today].retries≥1 + daily_by_model…retries≥1；SIGTERM 落盘含 finish_retries_total；seed resume 仿 :1541-1569）；`test_finish_then_content_fails_open_no_retry`（SSE_REASONING+SSE_FINISH+SSE_A+SSE_DONE → calls==1、逐字节全量）；`test_finish_fault_retry_zero_disables`（CTYUN_EMPTY_RETRY=0 → calls==1、data==SSE_FAULT_TAIL）；`test_finish_tail_over_cap_fails_open`（SSE_FINISH 后接 >256KB noise record + SSE_DONE → calls==1、全量字节交付）。

## Acceptance Criteria
- 验证命令（唯一解释器）：`cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（PATH python3 是 Homebrew 3.14.7，禁用；unittest 直跑脚本，v1.4 基线 Ran 54 tests OK，改动前 implementer 先跑 baseline 登记）。
- TDD：上述新集成用例改主文件前必红（finish 流被即刻 flush → calls==1 / retry_reason 字段缺失），改后转绿。
- 不回归（既有用例不改一字全绿）：:1464（EOF 无 [DONE] 重试 + retried=1）、:1494（[DONE] 无 finish 合法）、:1481（priming 前缀次序）、:1506/:1519（双空/零字节 fallback）、:1529（CTYUN_EMPTY_RETRY=0）、:1541（重试计数持久化续算）、:1571/:1614/:1643（eof-without-done 三组）、client-abort 499/RST 不重试（:1335）。
- REQ 行格式：新增 `retry_reason=` 字段恒出现，无重试时为 `-`；重试时为 `eof-priming` / `finish-no-usage`。

## Risks
- 缓冲上限：hold 期尾段持续流动不触发 UPSTREAM_TIMEOUT（600s 只管 stall），无界缓冲 = 内存膨胀 + 客户端挂死——PRIMED_TAIL_CAP 256KB 超限 fail-open 兜底；reasoning 前缀缓冲维持 v1.4 现状不另设限（不回归既有行为）。
- 计费：每次重试 = 上游重复计费一次。仅故障形态触发（usage 判别器挡住合法零内容流），每请求上限 1 构造性保证，发生量由 finish_retries_total 可观测，超预期可 CTYUN_EMPTY_RETRY=0 一键全关。
- dashboard 兼容：`retries` 列现含两形态（口径变宽是预期）；无新列/新 EVENTS kind，`load_stats_events` 白名单（:357）与 EVT_KIND_LABELS 不动；新顶层计数器对 /api/stats 纯加性。
- `"finish"` 拆分改变 `sse_data_line_kind` 值域：唯一生产调用点 :670 + 测试矩阵 :423-446 已 enumerable；`saw_done`/truncated 判定只依赖 `"done"`，不受影响。
- 交付时点变化：合法 finish 流的 usage/[DONE] 从"finish record 即刻"延后到 EOF 下发（毫秒级，字节序不变）；[DONE]-无-finish 流仍即刻 flush（:1494 锁定）。

## Exclusions
- DSML 文本通道泄漏乱码问题（另一失败模式，本期不做）。
- 模型换绑；dashboard 新增页面/卡片/列；EVENTS kind 扩展；`_DAILY_FIELDS` 第 7 字段（finish-form 不进按天维度，仅顶层计数器区分）。
- 重试上限 >1、跨请求重试预算、非 SSE 分支（`_relay_buffered` :712）任何改动。
- 部署沿 v1.4 惯例，DELIVER 阶段执行（只写不跑）：`cp` 仓库归档到 `~/.local/bin/` 后 `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`；实施前主代理对运行副本补 `cmp` 终检。

## R31 Evidence

[R31-S1] 问题存在的现场证据：2026-09-13 生产取证，model-io 日志 44 例实锤——上游"reasoning 起步即断流"新形态：流内仅少量 reasoning delta，随后 finish_reason 帧正常到达并带 [DONE] 收尾，但全程零 delta.content、零 tool_calls、零 usage 帧。代理侧 v1.4 对其记 `result=ok retried=0`（finish 被 priming 当首个信号 record 即刻 flush，重试路径不触发），客户端收到"合法但全空"回合即报 stream 错误。判别依据：正常完成的零内容流（如 max_tokens 被推理吃光）必有 usage 帧，故障形态无 usage 帧。（原始 REQ 行与 model-io 明细由主代理取证材料承载，本 spec 不复制。）

基线验证（改动前测试全绿，为 TDD 红相提供基线，改动后不得回归；implementer 实跑登记）：
```
$ cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
Ran 54 tests in ...
OK
```

spec 结构现场校验——5 个必含 section + Evidence 段自身，共 6 个顶格二级标题（本 architect dispatch 无 Bash：由会话内 ripgrep 真实执行捕获，格式对齐单文件 `grep -n` 输出）：
```
$ grep -nE "^## " /Users/peter/Documents/project/ctyun-stream-fix-proxy/docs/superpowers/specs/2026-09-13-priming-zero-content-retry-v15-design.md
3:## Goal
6:## Files to Change
70:## Acceptance Criteria
76:## Risks
83:## Exclusions
89:## R31 Evidence
```

锚点抽查 4 处（`sed -n 'Np'` 原样行内容，与 Files to Change 及 [R31-S2] 断言逐字一致；同上由逐行 Read 真实捕获）：
```
$ sed -n '82p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
    if choice.get("finish_reason") is not None:
$ sed -n '83p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
        return "content"
$ sed -n '679p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
                        if "content" in kinds or "done" in kinds:
$ sed -n '432p' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py
            f(b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n'), "content")
```

[R31-S2] 根因陈述（带锚点）：`sse_data_line_kind`（ctyun-stream-fix-proxy.py:82-83）把 `choice.finish_reason is not None` 归入 `"content"`；`_relay_sse`（:679）priming 期见 `"content"`/`"done"` 即刻补头 flush → finish record 成为首个信号 record，priming 正常结束、流原样转发（:617-625 记 result=ok），`_EmptyStream` 重试路径（:603）永远不触发；usage 帧被归 `"noise"`（:72-73 choices 为空）无判别作用。修法即 spec 正文：finish 拆独立 kind → hold 至 EOF → `sse_line_has_usage` 判别 → 合法 flush / 故障弃缓冲重试（共享上限 1）。
