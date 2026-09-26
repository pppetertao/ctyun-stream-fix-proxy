# 设计：上游头阶段 ConnectionResetError 纳入 header-stall 一次自动重试

## Goal
上游在连接/响应头阶段发 TCP RST（ConnectionResetError, Errno 54）时，代理当前直接合成 502 且 retried=0；本设计把该异常纳入既有 header-stall 一次重试机制（预算 HEADER_RETRY_MAX 不变），重试对客户端不可见，并以独立 retry_reason="conn-reset" 区分观测，使瞬时上游 LB RST 故障不再直接 502。

## Files to Change

### 1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`

- `:865 _proxy_relay()` except 元组扩为 `except (socket.timeout, http.client.RemoteDisconnected, ConnectionResetError) as first_exc:`（单 except 不复制重试体）。`:873-876` retry_reason 改为 isinstance 选择——**顺序敏感**：`http.client.RemoteDisconnected` ⊂ `ConnectionResetError`（多继承 ConnectionResetError+BadStatusLine），必须先排 RemoteDisconnected：
  ```python
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
  ```
- `:866-870` 注释改写：补 ConnectionResetError = 头阶段（connect/TLS/request 发送/getresponse 头读）被 RST，与 RemoteDisconnected 同类（"上游未交付任何响应字节"），priming 不可见论证对三者同样成立；并注明 retry_reason 偏离"阶段命名"惯例的理由（RST 指向上游 LB 健康、timeout 指向上游慢，运维 grep 需区分；kind/计数器仍按阶段复用，避免 9 触点统计形状改动）。
- `:171-174 header_timeout_should_retry()` docstring 更新：调用点覆盖头阶段三类可重试故障（socket.timeout / RemoteDisconnected / ConnectionResetError），函数名不改（改名徒增触点）。

### 2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`

- `:23 import subprocess` 后新增 `import struct`（字母序，SO_LINGER 打包用；现有 import 块 :12-29 无 struct）。
- `FakeUpstreamHandler` `:58-59`（stall_all/stall_calls 声明处）旁新增类属性 `rst_all = False`、`rst_calls = ()`（语义镜像 stall 变体，注释注明产出 ConnectionResetError）；`do_POST` `:81`（end stall 标记）后新增 rst 分支（放在 calls append :70-71 之后、fail_500 :83 之前）：
  ```python
  if self.rst_all or (self.rst_calls and self.calls is not None
                      and len(self.calls) in self.rst_calls):
      # SO_LINGER(1,0) close 强制发 RST（非 FIN），代理 getresponse 抛
      # ConnectionResetError；macOS 要求 8 字节 linger struct，int 直传 EINVAL
      self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                              struct.pack("ii", 1, 0))
      self.close_connection = True
      return
  ```
- `make_fake_upstream()` `:194` 签名加 `rst_all: bool = False, rst_calls: tuple = ()`；`:201-202` attrs dict 加两键；`:209` 改 `use_threading = stall_all or stall_calls or rst_all or rst_calls`（与 stall 同理防 shutdown 挂）。
- `:234 make_stall_upstream()` 后新增 `def make_rst_upstream(rst_all: bool = False, rst_calls: tuple = (), **kwargs) -> tuple:` 逐行镜像 :228-234（`make_fake_upstream(False, scripted=True, rst_all=rst_all, rst_calls=rst_calls, **kwargs)`，返回 `(port, FAKE_SERVERS[-1].RequestHandlerClass.calls)`）。
- 新测试 3 组（全部秒级，命令沿用 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`）：
  ① spawn（镜像 :2807 test_header_stall_retry_success）：`rst_calls=(1,)` + `extra_env={"CTYUN_HEADER_TIMEOUT":"1"}` → 200、`calls==2`、SSE 逐字节等于 SSE_A+SSE_B+SSE_DONE、stderr 含 `retried=1` 与 `retry_reason=conn-reset`、持久化 `header_retries_total>=1`。
  ② spawn（镜像 :2836 test_header_stall_both_timeout_returns_502）：`rst_all=True` → 502、`calls==2`、stderr 含 `retried=1` 与 `retry_reason=conn-reset`。
  ③ 白盒（镜像 :1728-1744）：`rst_all=True` 上游 + patch `mod.UPSTREAM_BASE`，直调 `mod.ProxyHandler._open_upstream(types.SimpleNamespace(), ...)` → `assertRaises(ConnectionResetError)` 且 `self.assertIs(type(cm.exception), ConnectionResetError)`（排除 RemoteDisconnected 子类，证明真 RST 非 FIN-close）。

## Acceptance Criteria
- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（现有 baseline + 新增 3 组）。
- 用例①：首呼 RST → 重试 → 客户端收 200 + 完整 SSE（含 [DONE]），`calls==2`，REQ 日志 `retried=1 retry_reason=conn-reset`，格式仍为 `REQ ... retried=N retry_reason=X exc=Y ts=` 单行。
- 用例②：连续两次 RST → 502 + `calls==2` + synth_502 留痕带 retried=1 / retry_reason=conn-reset。
- 用例③：白盒断言 `_open_upstream` 对 RST 上游抛出的异常 type 恰为 ConnectionResetError（非 RemoteDisconnected）。
- stall 回归：既有 `test_header_stall_*`（:2807-2882）不改动即通过，retry_reason 仍为 `header-timeout`（验证 isinstance 顺序正确）。
- `CTYUN_HEADER_RETRY=0` 语义不变：RST 首呼直接 502（复用既有禁用路径，`header_timeout_should_retry` 真值表 :1660 免改）。

## Risks
- **isinstance 顺序**：RemoteDisconnected ⊂ ConnectionResetError，若把 ConnectionResetError 判在前面，旧 stall 路径会被误标 conn-reset；回归由既有 :2860 `retry_reason=header-timeout` 断言兜底。
- **RST 夹具确定性**：SO_LINGER(1,0) close 即发 RST；夹具先读 body 再 set linger（收缓冲无残留），环回接口确定性高。若实测偶发 RemoteDisconnected（FIN 竞态），用例③降级为 assertRaises(ConnectionResetError)（重试路径仍覆盖，区分性由①② stderr 断言承担）。
- **Dashboard 核查结论（已查）**：全仓 grep `header-timeout`/`header_timeout`/`header_retries` 仅命中 :74/:874/:876 与统计/持久化通用键迭代（:280/:470/:509/:523/:659-775/:1850）；dashboard 经 `/api/errors`（:1169 `retry_reason` 透传）与 `/api/stats` 通用渲染，**无硬编码字符串**，新增 "conn-reset" 值免改 UI。
- **空流重试二呼遇 RST**：`:909` 二呼的 `except (OSError, http.client.HTTPException)`（:910）已覆盖 ConnectionResetError ⊂ OSError → 直接 502，不进头重试（沿用两预算独立决策，行为与 socket.timeout 一致）。
- **catch 顺序**：内层 except 必须仍在 `:879` 外层 `except (OSError, HTTPException)` 之前；ConnectionResetError ⊂ OSError，顺序正确则重试/502 分流正确。

## Exclusions
- socket.timeout 分支测试覆盖缺口（followups.md :6 已登记：真·持连 stall 夹具，另开 episode）。
- socketserver 客户端断连 traceback 噪音抑制（历史发现，另开 episode）。
- 不新增 ERR_KIND / `_KIND_CATEGORY` 行 / 计数器 / `_DAILY_FIELDS` / 持久化键（复用 header_timeout 形状，9 触点零改动）；daily_by_model 与 admin dashboard 统计不动。
- 不重命名 `header_timeout_should_retry` / `header_retries_total` / `ERR_KIND_HEADER_TIMEOUT`。
- 空流重试二呼（:909）不加头重试；HEADER_RETRY_MAX 与 EMPTY_RETRY_MAX 预算不共用（沿用 header-stall spec）。
- README env 表不动（无新 env）；CTYUN_HEADER_RETRY 语义不变。

## R31 Evidence
[R31-S1] 现场证据——2026-09-26 09:37:07–09:37:11 连续 3 个请求（deepseek-v4-pro-0813-oc）瞬时 502，同秒其他请求成功（上游 LB 瞬时 RST）：
```
$ grep "ConnectionResetError" ~/Library/Logs/ctyun-stream-fix-proxy.log | head -3
REQ ... status=502 dur=0.0s exc=ConnectionResetError:_[Errno_54]_Connection_reset_by_peer retried=0 retry_reason=- ...
```
[R31-S2] 根因：`:865` 可重试 except 元组只覆盖 `(socket.timeout, RemoteDisconnected)`，ConnectionResetError 直接落入 `:879` 外层 OSError 兜底 → 合成 502、重试未触发：
```
$ grep -n "except (socket.timeout\|except (OSError\|RemoteDisconnected, ConnectionResetError" ctyun-stream-fix-proxy.py
865:            except (socket.timeout, http.client.RemoteDisconnected) as first_exc:
879:        except (OSError, http.client.HTTPException) as exc:
$ /usr/bin/python3 -c "import http.client; print(http.client.RemoteDisconnected.__mro__)"
(<class 'http.client.RemoteDisconnected'>, <class 'ConnectionResetError'>, ..., <class 'http.client.BadStatusLine'>, ...)
```
头阶段 RST 与 RemoteDisconnected 同为"未交付任何响应字节"（RemoteDisconnected 本身即 ConnectionResetError 子类），重试一次不损伤客户端交付；纳入 :865 元组即获得既有重试路径。
