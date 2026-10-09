## Goal

为 7920 转发生成所有路径请求的诊断日志，消除当前 HEAD/OPTIONS 无声 501 与异常路径无 REQ 行的观察盲区，确保每个到达代理的请求（regardless of method/path/outcome）在 stderr 有一行 DIAG-IN + 一行 REQ 结果。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:2718 ProxyHandler` — 类体内新增 `do_HEAD` / `do_OPTIONS` 方法与 `_diag_log` 辅助方法，拓宽 `_proxy` 异常捕获。

   - `ProxyHandler.do_HEAD(self) -> None:2718+`（类内新建）— 委托 `self._proxy()`（同 do_GET）。BaseHTTPRequestHandler 默认对无 do_HEAD 的类会以 501 转 log_message（已抑制），客户端根本见不到 501 外的任何痕迹。委托 `_proxy` 后 HEAD 请求走完整 relay + `_log` 路径，上游响应的 Content-Length 等头正确返回而无 body（HTTP 规范已保证）。
   
   - `ProxyHandler.do_OPTIONS(self) -> None:2718+`（类内新建）— 委托 `self._proxy()`。上游如需 CORS 预检由上游自己回答；代理无条件转发与其余方法同口径。
   
   - `ProxyHandler._diag_log(self) -> None:2718+`（类内新建）— 请求入口诊断行：`_safe_log_stderr("DIAG IN %s %s from=%s rid=%s" % (self.command, self.path, self.client_address[0], getattr(self, "_req_id", "-")))`。耗时忽略不计（`print` + `append` 同 `_safe_log_stderr` 路径）。

   - `ProxyHandler._proxy(self) -> None:2742` — 方法体首行（STATS 锁前）插入 `self._diag_log()`。`except` 子句（line 2755）从 `(ConnectionError, socket.timeout)` 拓宽为 `Exception`；新宽分支内调用 `self._log(started, 499, "proxy-error", 0, exc=exc)` 后 `raise`（保留原始 traceback）。499 沿用 nginx 语义"客户端中断/代理内部故障"，不新造 status。

   - `ProxyHandler._log(self, ...):3280` — 不需要改动（接口不变、字段不变）。入口诊断由 `_diag_log` 独立成行，不与 REQ 行耦合。

2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:5491 main()` — 无须改动。日志落点由 launchd plist 的 `StandardErrorPath` 决定（仓库 plist 模板 `com.ctyun-stream-fix-proxy.plist:28` 指向 `/usr/local/var/log/ctyun-fwd.err`），不在此次 scope。

3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py` — 测试追加。至少两个新用例：

   - `TestProxyHandler.test_diag_log_on_entry(self)` — 向代理发任意请求后断言 LOG_RING 含 `DIAG IN` 行，含 method、path、client IP、rid 字段；`TestProxyHandler.test_head_method_responds_200` — 构造上游返回 HEAD 的 mock，验证客户端收到正确响应 + LOG_RING 含 REQ 行。

## Acceptance Criteria

- AC1: 任意 HTTP 方法（HEAD/OPTIONS/CONNECT/TRACE 除外，Python stdlib `BaseHTTPRequestHandler` 对无 handler 的方法自动 501）到达 7920 后 LOG_RING 必有 DIAG-IN 行紧随 REQ 行（2 行/请求，DIAG-IN 在先）。
- AC2: HEAD 请求委托 `_proxy_relay` 成功且上游响应正确（响应头不含 body），LOG_RING 含 REQ HEAD 行（status=200/4xx/5xx 取决于上游）。
- AC3: OPTIONS 请求委托 `_proxy_relay` 成功，LOG_RING 含 REQ OPTIONS 行。
- AC4: `_proxy_relay` 内部抛出非连接异常（如 `ValueError` / `AttributeError` / `http.client.BadStatusLine`）时，stderr 出现 `REQ ... -> 499 dur=...s result=proxy-error` 行且错误不导致线程崩溃（`_proxy` 宽 except log 后 re-raise 至 `handle_error` 打 traceback，但 REQ 行已落地）。
- AC5: 入口 DIAG-IN 行格式固定 `DIAG IN <METHOD> <path> from=<ip> rid=<rid>`，不含 token/body 敏感信息。
- AC6: 全量测试套件 `python3 ctyun-stream-fix-proxy.test.py` 通过（含新增用例），放行现有 300+ 用例。

## Risks

- **HTTP 501 冲突**：Python `http.server.BaseHTTPRequestHandler` 在 `handle_one_request` 中对未定义 do_* 的方法直接返回 501；加了 do_HEAD/do_OPTIONS 后这两个方法走 relay 不再 501。但生产客户端不使用 HEAD/OPTIONS 时代理行为零变化；使用时代理转发（同 GET）而非硬答 501。
- **do_HEAD 上游兼容**：_relay_buffered（line 3229）对 HEAD 的响应体 read 可能返回空 bytes（依赖上游实现），但 body 写已在 `_reply_502`/`_reply_tpm_429` 中以 `if self.command != "HEAD"` 守卫。_relay_buffered 写 data 时 `if data:` 已兼容空 body。风险低。
- **性能**：DIAG-IN 行每次请求增加一次 `print` + `LOG_RING.append`（同 _safe_log_stderr），约微秒级，与现有 _log 调用同量级，不引入新开销类型。
- **499 语义漂移**：当前 499 仅用于客户端中断（ConnectionError/socket.timeout）；拓宽后 proxy-internal 异常也记 499。dashboard 中 499 不再纯粹等于 "client abort"，需要区分。但 499 在本代理的 NATURE 就是"未正常 relay 完成"，语义匹配。
- **日志音量**：每请求 2 行（DIAG-IN + REQ）替代当前 1 行，日志量翻倍。环大小 LOG_RING_MAX 不变；stderr 输出由 launchd 逐行写文件（无 buffer 爆炸）。

## Exclusions

- 不在 7921 admin 端口加诊断日志（AdminHandler 已有独立的 `_safe_log_stderr` 路径，且 admin 侧请求频率极低）。
- 不改变 `_log` 的 REQ 行格式（字段序、字段名、分隔符全部保持向后兼容）。
- 不新增环境变量 / 开关 / 采样率（全量记录，不采样）。
- 不处理 CONNECT / TRACE 方法（保持 BaseHTTPRequestHandler 默认 501 行为，DIAG-IN 行由 `_proxy` 入口覆盖不到这两个方法，因为无 do_* handler → `handle_one_request` 在调用 do_* 之前就 501 返回；需额外 override `handle_one_request` 才能覆盖——若后续发现需要，另开 episode）。
- 不实现日志轮转或磁盘管理（由 launchd/系统日志基础设施负责）。
- 不引入新依赖。