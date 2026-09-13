# EOF-without-done 检测设计（ctyun-stream-fix-proxy）

## Goal
上游 SSE 流在 `[DONE]` 之前优雅断流（TCP FIN）时，代理目前按正常结束记 `result=ok`，客户端却因流缺 `[DONE]` 报 stream 错误。本次在 `_relay_sse` 的 EOF 退出点检测"未见过 [DONE]"，把该请求的 result 标记为 `eof-without-done` 并计数落盘（全局 + 按天 + 按模型），使上游截断故障在日志/统计可见。不改字节透传行为。

## Files to Change

全部改动限于单文件项目（无跨文件接口），共 2 文件：

**`/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`**

1. `:101-103` STATS 初始化字面量 — 加 `"eof_without_done_total": 0`（命名跟 `empty_retries_total` 惯例）。
2. `:290` `_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream", "retries")` — 追加第 6 字段 `"eof_without_done"`。`load_daily_buckets`(:304)/`load_daily_by_model_buckets`(:334) 按 `_DAILY_FIELDS` 迭代清洗，旧持久化文件缺该键自动补 0，零迁移。
3. 桶/entry 创建字面量 4 处加 `"eof_without_done": 0`（否则新计数 `+=` 直接 KeyError，见 :456 注释）：`:414-416`（_record_request dm entry）、`:424-426`（_record_request daily 桶）、`:464-466` 与 `:469-471`（_record_empty_retry 两处）。
4. 新函数 `_record_eof_without_done(model=None) -> None`（置于 `_record_empty_retry` :474 之后，逐行镜像它，去掉 EVENTS.append）：持 `STATS_LOCK`，`STATS["eof_without_done_total"] += 1`；有 model 时按 BY_MODEL_CAP 惯例写 `daily_by_model[today][model]["eof_without_done"] += 1`；`daily[today]["eof_without_done"] += 1`；置 `_stats_dirty = True`。
5. `:247-248` `save_stats_counters` counter 元组、`:284` `load_stats_counters` key 元组 — 各加 `"eof_without_done_total"`；`:1355-1358` `main()` 恢复块加 `STATS["eof_without_done_total"] = counters["eof_without_done_total"]`。
6. `:619` `def _relay_sse(self, resp, final: bool) -> int` — 签名改 `-> tuple`，返回 `(filtered, truncated)`：
   - 循环前初始化 `saw_done = False`、`truncated = False`（:623 附近）；
   - `:632` `kinds = [sse_data_line_kind(...)]` 之后加一行 `saw_done = saw_done or ("done" in kinds)`（覆盖 priming 与 streaming 两阶段所有 record）；
   - `:654-662` EOF 分支：`if priming:` 内部逻辑不动（final flush / raise _EmptyStream 均保持 `truncated=False`）；加 `else: truncated = not saw_done`（仅 streaming 阶段 EOF 且全程无 done 才置 True）；
   - `:667` `return filtered` → `return filtered, truncated`。
7. `:571` 调用点 → `filtered, truncated = self._relay_sse(resp, final=(EMPTY_RETRY_MAX < 1))`；`:585` 调用点 → `filtered, truncated = self._relay_sse(resp, final=True)`（_EmptyStream 异常路径下 ：571 不赋值，:585 必然重赋值，:586 无 unbound 路径）。
8. `:586` result 重算之后加：
   ```python
   if result == "ok" and truncated:
       result = "eof-without-done"
       _record_eof_without_done(model)
   ```
   仅覆盖 `"ok"`：`upstream-err`（status≥400，更有信息量）与 `aborted`（:527 异常路径，不经过此处）不被误标；`:567` 初值只服务非 SSE 分支（:592），不动。

**`/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`**（TDD：先写下列用例看红，再改主文件转绿）

- 新增集成用例（仿 `test_empty_stream_retried_and_second_attempt_relayed` :1456 结构，`make_scripted_upstream(body_override=SSE_A + SSE_B)` 伪造"有 content 无 [DONE] 即 EOF"）：
  ① 客户端仍逐字节收到 `SSE_A + SSE_B`（透传不动）；stderr 含 `result=eof-without-done`；`/api/stats` 的 `eof_without_done_total >= 1`、`daily[today]["eof_without_done"] >= 1`、`daily_by_model[today]["deepseek-v4-pro-0813-oc"]["eof_without_done"] >= 1`；SIGTERM 落盘后 JSON 含同键（仿 :1533 persist/resume 用例）。
  ② 对照组：正常流（默认 poison=False）与 reasoning+[DONE] 流（:1486 场景）断言 stderr 含 `result=ok` 且 `eof_without_done_total == 0`。
  ③ 双空流 fallback（:1498 场景）断言 stderr 不含 `eof-without-done`（priming 路径不加标记）。
- 更新既有精确断言（_DAILY_FIELDS 加第 6 键波及）：`:453-460` load_stats_counters 零值 dict 加 `"eof_without_done_total": 0`；`:538-539` 与 `:541-543` aggregate 精确 dict 加 `"eof_without_done": 0`；`:560-562` range_stats 键集合加 `"eof_without_done"`；`:488-491`、`:1149-1152` assertIn 键清单可顺带补新键（加性）。

## Acceptance Criteria

- `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全部通过（必须用 `/usr/bin/python3`，README:69；预期 54+新增 用例 0 failure 0 error）。
- 新用例 ① 改主文件前运行必红（`result=ok` 且计数缺位），改后转绿（TDD 证据）。
- 正常 `[DONE]` 流、reasoning+[DONE] 流、双空流重试流、`/plain` 非 SSE 流的 stderr result 与计数不回归（用例 ②③ + 既有 :1456/:1473/:1486/:1498 全绿）。
- 重启续算：seed_persist 含 `eof_without_done_total` 的代理经 `/api/stats` 透出该值，旧文件（无该键/无该日字段）加载为 0 不崩。
- 日志行格式不变（`_log` :698-704 串不动），仅 result 值域多一个 `eof-without-done`。

## Risks

- `_DAILY_FIELDS` 第 6 字段使 `aggregate_daily_range`/`range_stats` 输出多一键：dashboard JS 按具名键读取（`rs.errors_proxy` 等）不受影响；但任何"键集合精确相等"类断言会红——已 enumerable（见上 4 处）。
- `saw_done` 依赖 `sse_data_line_kind` 的 done 判定（:63-64 DONE_RE）；若上游以非标形式发 `[DONE]`（如 `data:[DONE] ` 带尾空格已被 rstrip 覆盖）误判为未 done → 多计一次 eof。现有 DONE_RE 与客户端解析器口径一致，风险低。
- 统计口径：eof-without-done 不计入 `errors_total`/`errors_proxy`/`errors_upstream`（独立计数器），dashboard 顶部"错误数"不含它——这是有意为之，避免改动既有错误口径。
- 持久化文件为 `~/.local/etc/ctyun-stream-fix-proxy.json`（`CTYUN_PERSIST_PATH` seam），原子写（tmp+os.replace）；新字段随 60s 脏刷/SIGTERM 落盘，无新依赖。

## Exclusions

- **EVENTS 事件流不扩展**：新增 kind 需改 `load_stats_events` 白名单（:354）+ dashboard `EVT_KIND_LABELS`（:973）+ tooltip 过滤语义，超出"一行级成本"；eof 信号由计数器 + result 标记承载。
- **dashboard 不加任何展示**（卡片/列/tooltip 均不动）；`_DASHBOARD_SRC` 零改动。
- **字节透传逻辑不动**：`_relay_sse` 的滤毒/priming/flush 分支、`_EmptyStream` 重试机制（EMPTY_RETRY_MAX）、UPSTREAM_TIMEOUT/SEND_TIMEOUT_S/499 aborted 路径全部维持现状；priming 阶段 EOF（空流）不标 eof-without-done（已由 retries 计数 + `retried=N` 日志承载，避免同一失败双计数）。
- **不改转发行为、不发合成 [DONE]**（纯观测性改动）。
- **部署机制**：仓库无 repo→运行副本自动同步脚本（plist 模板指向 `/usr/local/bin/`，与实际 `~/.local/bin/` 不一致，按 README 手工部署）。DELIVER 阶段执行（只写不跑）：
  ```sh
  cp /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py
  launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy
  grep 'result=eof-without-done' ~/.local/log/ctyun-fwd.err
  ```
- 提交遵循 Conventional Commits（`feat:` 单逻辑单 commit），LF 换行，无新依赖（纯 stdlib）。

## R31 Evidence

[R31-S1] 问题存在的现场证据：2026-09-13 代理日志（~/.local/log/ctyun-fwd.err）中三条请求与客户端三次 stream 失败时间一一对应（10:50:01 / 10:55:15 / 10:59:47Z），代理侧全部记 `-> 200 result=ok`、`retried=0`，且 dur 远短于复审任务合理完成时长（7.1s 不可能完成一次 diff 复审输出）；当天统计 errors_proxy=0 / errors_upstream=0——问题真实存在且在代理日志/统计中完全不可见。

$ grep 'model=deepseek-v4-pro-0813-oc' ~/.local/log/ctyun-fwd.err | grep -E '10:(50|55|59):'
```
REQ POST /chat/completions -> 200 dur=7.1s result=ok filtered=0 model=deepseek-v4-pro-0813-oc retried=0 ts=2026-09-13T10:50:00+0800
REQ POST /chat/completions -> 200 dur=67.2s result=ok filtered=0 model=deepseek-v4-pro-0813-oc retried=0 ts=2026-09-13T10:55:15+0800
REQ POST /chat/completions -> 200 dur=59.9s result=ok filtered=0 model=deepseek-v4-pro-0813-oc retried=0 ts=2026-09-13T10:59:47+0800
```

客户端侧错误（~/.zcode/cli/db/db.sqlite message 表，三次逐字一致；`errorPhase:"stream"` 与 [R31-S1] 三次失败时间对应）：
```json
{"name":"AiSdkModelAdapterError","data":{"message":"Model request failed.","code":"model_request_failed","attribution":{"source":"provider","reason":"unknown","errorPhase":"stream","exceptionKind":"generic","providerId":"c8fbbb08-aa67-445e-8b50-377d76b4cb0b","modelId":"deepseek-v4-pro-0813-oc","providerKind":"openai-compatible","transport":"sse","retryable":false}}}
```

基线验证（改动前测试全绿，为 TDD 红相提供基线，改动后不得回归）：
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```
Ran 54 tests in 20.143s

OK
```

[R31-S2] 根因陈述（带证据）：`_relay_sse()`（ctyun-stream-fix-proxy.py:654-662）streaming 阶段 EOF 直接 break，不检测本次流是否收到过 `[DONE]`；`result` 判定（:567/:586）只看上游状态码 <400。上游网关中途 TCP FIN 优雅断流 → 代理按正常结束记 `result=ok`（证据：[R31-S1] 三条 REQ 行 200/ok/filtered=0）→ 客户端流缺 `[DONE]` 报 `errorPhase:"stream"`（证据：上方客户端错误 JSON，三次逐字一致）。修法即 spec 正文所述：`_relay_sse` 返回 `(filtered, truncated)`，streaming 阶段 EOF 且全程未见过 done record 时 `truncated=True`；`result=="ok" and truncated` → `eof-without-done` 并经 `_record_eof_without_done()` 计数落盘。
