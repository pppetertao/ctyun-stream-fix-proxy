# 设计：上游连接/响应头停滞短超时（header stall timeout）

## Goal
上游接受连接后迟迟不返回响应头时，代理当前挂满 `UPSTREAM_TIMEOUT=600s` 才合成 502（客户端干等 10 分钟）。本设计把 connect+request+getresponse（头阶段）改为短超时（默认 45s），超时转为可重试错误重试一次（重试对客户端不可见），仍失败才快速 502；body/流式阶段逐读 600s 语义不变。

## Files to Change

### 1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`

**机制（核心决策）**：`_open_upstream` 内分段——构造连接用 `HEADER_TIMEOUT_S`（覆盖 TCP connect、TLS 握手、request 发送、`getresponse()` 头读取），`getresponse()` 输出响应头后立即在 socket 上 `settimeout(UPSTREAM_TIMEOUT)` 恢复长超时。实测 `getresponse()` 返回后 `conn.sock` 已被置 None（socket 移交 `HTTPResponse.fp.raw`），因此必须在 `getresponse()` 前保留 `sock = conn.sock` 引用，然后在 `sock.settimeout(UPSTREAM_TIMEOUT)`。该引用与 `HTTPResponse` 包装的是同一 socket 对象，`resp.readline()`/`resp.read()`（经 makefile→SocketIO→recv）读的是当前 socket 的 timeout，头/体切换点在这一行 settimeout，relay 循环零改动。不选"手动 connect 后分段 settimeout"（connect 与头停滞同属"客户端未收到字节"阶段，无需区分）；不选"relay 循环内逐读 settimeout"（把传输层关注点漏进 relay 逻辑）。

- `:38` 后新增 `HEADER_TIMEOUT_S = max(1.0, float(os.environ.get("CTYUN_HEADER_TIMEOUT", "45")))` — `max(1.0,...)` 防御 0/负值（socket timeout 0 = 非阻塞，必炸）
- `:50` 后新增 `HEADER_RETRY_MAX = max(0, int(os.environ.get("CTYUN_HEADER_RETRY", "1")))` — env-only 启动时读，同 `SEND_TIMEOUT_S`(:42)/`EMPTY_RETRY_MAX`(:50) 惯例；不进 /api/config 热切（超时是稳健性参数非运营开关，错误值启动时一次性暴露好过运行期热改）
- `:66-71` 新增 `ERR_KIND_HEADER_TIMEOUT = "header_timeout"`；`:73-80` `_KIND_CATEGORY` 新增一行 `ERR_KIND_HEADER_TIMEOUT: CLASS_UPSTREAM_FAULT`（纯新增键，既有 6 行不动，classify_outcome 零改动——最终失败仍走既有 `synth_502=True` 路径）
- `:161-164` 旁新增 `def header_timeout_should_retry(budget: int) -> bool:` docstring 注明调用点，镜像 `empty_stream_should_retry`
- `:960-973 _open_upstream()`：两个构造点 `timeout=HEADER_TIMEOUT_S`；`conn.request()` 后保留 `sock = conn.sock`，`getresponse()` 成功后 `sock.settimeout(UPSTREAM_TIMEOUT)` 再 `return conn, resp`（`getresponse()` 返回后 `conn.sock` 已被置 None，必须提前持有引用）。空流重试二呼 `:903` 自动获得短头超时（快速失败），但不做头重试（见 Exclusions）
- `:842-884 _proxy_relay()` 首呼改嵌套 try：
  ```python
  header_retried = 0; header_retry_reason = ""
  try:
      try:
          conn, resp = self._open_upstream(...)
      except (socket.timeout, http.client.RemoteDisconnected) as first_exc:
          # socket.timeout = 头阶段阻塞到 HEADER_TIMEOUT_S；RemoteDisconnected =
          # 上游在头阶段直接关连接（未发任何响应字节）。两者同为"响应头阶段未收到
          # 任何响应"，priming 不可见论证同样成立；kind/retry_reason 保持
          # header_timeout/"header-timeout"（阶段命名，不按异常命名）。
          if not header_timeout_should_retry(HEADER_RETRY_MAX): raise
          header_retried = 1; header_retry_reason = "header-timeout"
          record_error_event(ERR_KIND_HEADER_TIMEOUT, model=model, path=self.path,
                             exc=first_exc, body=body, retry_reason="header-timeout")
          _record_header_retry(model)
          conn, resp = self._open_upstream(...)  # 二呼再抛 → 外层 502
  except (OSError, http.client.HTTPException) as exc:
      # 既有块 :873-884 原样，_log 与 record_error_event 增传
      # retried=header_retried, retry_reason=header_retry_reason（0/"" 时行为与今天逐字节一致）
  ```
  RemoteDisconnected 同时 ⊂ ConnectionError→OSError 与 BadStatusLine→HTTPException 两族：内层在前即命中重试；二呼再抛时由外层 `except (OSError, http.client.HTTPException)` 兜底走 502，不逃逸到 `_proxy` 的 client_abort 吞口。
- `:836/:843`：`retried = 0` → `retried = header_retried`；`retried = 1` → `retried = header_retried + 1`（retry_reason 仍取 `exc.reason`——日志行显示最近一次重试原因，头重试细节由留痕环+计数器承载）
- 新增 `def _record_header_retry(model=None) -> None:`（`:715-737 _record_eof_without_done` 逐行镜像：STATS 总量 + daily 桶 + daily_by_model，**不追加 EVENTS**——沿用 eof 的"事件流不扩展"决策）；`STATS["header_retries_total"] += 1`、桶/entry `"header_retries"` 字段
- 计数器形状同步（形状不同步时 `+=` 直接 KeyError，见 :688-690 docstring）——`"header_retries": 0` 六处 init：`:644-648`（_record_request dm entry）、`:656-659`（daily 桶）、`:700-703`、`:706-709`（_record_empty_retry）、`:725-729`、`:732-736`（_record_eof_without_done）；`STATS` init `:268-270` 加 `"header_retries_total": 0`
- 持久化：`save_stats_counters` counters 元组 `:458-460` 加 `"header_retries_total"`；`load_stats_counters` 循环 `:498-499` 同加（旧文件缺键自动补 0）；启动回填 `:1780-1785` 加一行；`_DAILY_FIELDS` `:512-513` 加 `"header_retries"`（load_daily_buckets/by_model 按 `_DAILY_FIELDS` 逐字段 `bucket.get(field,0)` 补键，旧持久化文件自动兼容，range 聚合 :403/:409 免改）

### 2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`

- `FakeUpstreamHandler`(:47) 新增类属性 `stall_all = False`（每呼：读 body、记 calls、**不写任何响应不关连接**直接返回——关连接会变 RemoteDisconnected 而非超时）、`stall_calls = ()`（按 1-based 呼叫序 stall）、`empty_stream_calls = ()`（按 1-based 呼叫序回空流，与 `empty_stream=True` 全局旗互斥用）；`make_fake_upstream`(:183) 对 stall 变体改用 `http.server.ThreadingHTTPServer`（daemon_threads=True，否则单线程 HTTPServer 的 serve_forever 卡死在 stall 连接上，shutdown 挂测试）
- 新测试（全部秒级）：① `header_timeout_should_retry` 真值表（镜像 ：1655）；② `_KIND_CATEGORY["header_timeout"]=="upstream_fault"` + 既有 classify_outcome 矩阵回归；③ 白盒分段测试（进程内）：patch `mod.UPSTREAM_BASE`→假上游 + `mod.HEADER_TIMEOUT_S=0.5`，`mod.ProxyHandler._open_upstream(types.SimpleNamespace(), ...)` 直调——正常上游断言返回后 `conn.sock` 为 None 且 `resp.fp.raw._sock.gettimeout()==mod.UPSTREAM_TIMEOUT`（体阶段已恢复长超时），stall 上游断言 ~0.5s 抛 `http.client.RemoteDisconnected`（上游直接关连接，`getresponse()` 未收到任何响应字节）；④ 进程 spawn `extra_env={"CTYUN_HEADER_TIMEOUT":"1"}`（同 ：1889 惯例）：`stall_calls=(1,)` → 呼2 正常 → 客户端收完整 SSE 含 [DONE]、`calls==2`、持久化 `header_retries_total>=1`；⑤ `stall_all` → 502、`calls==2`、synth_502 事件带 retried=1；⑥ `CTYUN_HEADER_RETRY=0`+`stall_all` → 502、`calls==1`（禁重试仍快速失败）；⑦ 体阶段回归：滴流上游（chunk 间隔 1.5s×2，`HEADER_TIMEOUT_S` patch 为 1）→ 完整收流含 [DONE]，证明短超时未漏进体阶段；⑧ 叠加：`stall_calls=(1,)`+`empty_stream_calls=(2,)` → `calls==3`、日志 retried=2、客户端 200

### 3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/README.md`

- `:45` env 表加 `CTYUN_HEADER_TIMEOUT`（45，秒）与 `CTYUN_HEADER_RETRY`（1）两行；`:27` 行为清单加一条：头阶段（连接+响应头）45s 短超时、超时自动重试一次、重试不可见，响应头到达后恢复逐读 600s

## Acceptance Criteria
- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（baseline 103 + 新增 8 组）
- stall 上游首呼：客户端 ~2×CTYUN_HEADER_TIMEOUT 内收到完整流（重试成功路径），或 ~2×后收 502（双超时路径）——均不再等 600s
- 体阶段回归（滴流间隔 > 头超时）完整收流，证明 `settimeout(UPSTREAM_TIMEOUT)` 切换生效
- 重试成功/失败在留痕环（header_timeout×1 / synth_502+retried=1）与计数器（header_retries_total、daily.header_retries）可观测；`CTYUN_HEADER_RETRY=0` 时 calls==1
- 旧持久化文件（无 header_retries/header_retries_total 键）加载不崩、自动补 0

## Risks
- **异常捕获顺序**：Python 3.9 中 `socket.timeout` ⊂ `OSError`，`http.client.RemoteDisconnected` ⊂ `OSError` 且 ⊂ `http.client.HTTPException`；内层 `except (socket.timeout, RemoteDisconnected)` 必须排在 `except (OSError, HTTPException)` 之前，RemoteDisconnected 同时命中外层两族，顺序正确即重试正确；漏捕（如缺 RemoteDisconnected）会直接走外层 502 路径，统计正确但不重试
- **持久化形状**：6 处桶/entry init 与 `_DAILY_FIELDS`、counters 双清单（:458/:498）共 9 个触点缺一即 `+=` KeyError 或计数静默丢失；load 侧已按 `_DAILY_FIELDS` 容错补键，风险集中在 init 漏改
- **假上游 stall 语义**：handler 直接关连接（`close_connection = True; return`）会抛 RemoteDisconnected（不是 socket.timeout），内层重试 catch 同时覆盖两异常；ThreadingHTTPServer 必须 daemon，否则 teardown 挂死
- **HTTPS**：TLS 握手在短超时内，`conn.sock` 为 SSL socket；提前持有的 `sock` 引用同样指向该 SSL socket（`SSLSocket` 继承 `socket.socket`），settimeout 同样生效

## Exclusions
- 不做 body 阶段空闲超时（逐读 600s 不动）、不做整请求 deadline
- 不做多上游/failover；重试只打同一上游
- 不动 dashboard UI（新 kind/计数经现有 /api/errors、/api/stats 透出即可）
- 空流重试的二呼（:851）不加头重试（它本身已是重试，socket.timeout 由既有 ：852 except 快速 502）；两预算独立（HEADER_RETRY_MAX 与 EMPTY_RETRY_MAX 不共用）
- 新 env 不进 /api/config 热切；EVENTS 事件流不扩展（沿用 eof 决策）

## R31 Evidence
[R31-S1] 现场证据——600s 挂死路径与头/体共 Socket 事实：
```
$ grep -n "UPSTREAM_TIMEOUT\|getresponse\|conn.sock" ctyun-stream-fix-proxy.py
38:UPSTREAM_TIMEOUT = 600
913:                parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
916:                parsed.hostname, parsed.port, timeout=UPSTREAM_TIMEOUT)
918:        return conn, conn.getresponse()
952:                line = resp.readline()
```
`_open_upstream`(:908-918) 将 600s 同时用于 connect 与 getresponse；:952 体阶段 `readline()` 走同一 socket——在 :918 后 settimeout 即精确完成头/体切换。
[R31-S2] 根因：http.client 的 `timeout` 参数同时约束 connect 与 socket 读——单一 600s 参数喂给全部调用点，无任何分段：
```
$ grep -n "timeout=UPSTREAM_TIMEOUT" ctyun-stream-fix-proxy.py | wc -l
       2
$ grep -c "settimeout" ctyun-stream-fix-proxy.py
4
```
上游 accept 后不回响应头时 `getresponse()`(:918) 阻塞至 600s，首呼异常路径（:823-832）才合成 502；头阶段（客户端零字节）与体阶段（已开始交付）的超时需求本质不同，须分段设值。
