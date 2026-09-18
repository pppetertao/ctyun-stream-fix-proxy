R31-EXEMPT: feat-not-bugfix

# 运维可观测性：失败留痕 / capture_errors 开关 / 日志 API / 错误分类网关

## Goal
给单文件 stdlib 代理 ctyun-stream-fix-proxy.py 增加四件运维能力：失败请求环形留痕（/api/errors）、capture_errors 运行时开关（走 /api/config）、stderr 日志 API 化读取（/api/logs 游标分页）、把散落在 _relay_sse/_proxy 的错误口径收敛为可单测的纯函数分类网关——统计与重试行为零变化，现有 54 测试全绿。

## Files to Change

### 1. /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py

**常量与模块态（:48-51 常量区、:117-127 模块态区）**
- 新常量：`ERROR_RING_MAX=50`、`BODY_SNAPSHOT_CAP=4096`、`RESPONSE_SNIPPET_CAP=2048`、`LOG_RING_MAX=1000`、`LOG_TAIL_DEFAULT=100`、`LOG_PAGE_MAX=500`；分类常量 `CLASS_OK/CLASS_CLIENT_ABORT/CLASS_REQUEST_FAULT/CLASS_UPSTREAM_FAULT/CLASS_POISON_FIXED`；留痕 kind 常量 `ERR_KIND_POISON="poison_hit"`、`ERR_KIND_EMPTY_RETRY="empty_retry"`、`ERR_KIND_EOF_NO_DONE="eof_without_done"`、`ERR_KIND_SYNTH_502="synth_502"`、`ERR_KIND_UPSTREAM_5XX="upstream_5xx"`、`ERR_KIND_REQUEST_4XX="request_4xx"`。
- 新模块态：`CAPTURE_ERRORS=False`（_CFG_LOCK 守护）；`ERROR_EVENTS=collections.deque(maxlen=ERROR_RING_MAX)` + `ERROR_LOCK`（环满由 maxlen 淘汰最旧）；`LOG_RING=collections.deque(maxlen=LOG_RING_MAX)` + `LOG_LOCK`；两个独立单调 seq 计数器，各在对应锁内递增。

**错误分类网关（新纯函数，置于 sse_line_has_usage :101 之后）**

```python
_Outcome = collections.namedtuple("_Outcome", "category log_result counts_error capture")

def classify_outcome(status=None, synth_502=False, client_abort=False,
                     eof_without_done=False, poison_filtered=0) -> _Outcome:
```
判定优先级 client_abort > synth_502 > eof_without_done > status 阈值；status=None 且无 flags → ValueError。映射表（**行为保持现状**）：

| 输入 | category | log_result | counts_error(→errors_total/errors_proxy) | capture |
|---|---|---|---|---|
| client_abort=True | CLIENT_ABORT | "aborted" | False | False |
| synth_502=True | UPSTREAM_FAULT | "error" | True | True |
| eof_without_done=True | UPSTREAM_FAULT | "eof-without-done" | False | True |
| status<400, poison_filtered=0 | OK | "ok" | False | False |
| status<400, poison_filtered>0 | POISON_FIXED | "ok" | False | True |
| 400≤status<500 | REQUEST_FAULT | "upstream-err" | False | True |
| status≥500 | UPSTREAM_FAULT | "upstream-err" | False | True |

计数语义不变说明：EOF-without-DONE/空流重试耗尽仍只进 eof_without_done_total/empty_retries_total（不计 errors_total）；上游 5xx 仍由 _record_request:474,484 的 `status >= 500` 归入 errors_upstream——classifier 只供 log_result 字符串、_record_request 的 error= 标志、留痕资格三件事，**不改 _record_request 内部与 EVENT 流（:486-490）**。
另加 `def empty_stream_should_retry(budget: int) -> bool: return budget > 0`；调用点 :656 `final=(EMPTY_RETRY_MAX < 1)` 改为 `final=not empty_stream_should_retry(EMPTY_RETRY_MAX)`（语义等价，重试决策收敛到纯函数）。

**日志镜像（_safe_log_stderr :282-289）**：print 成功后 `with LOG_LOCK: 递增 seq; LOG_RING.append({"seq": seq, "line": msg})`——stderr 原文逐行入环（含启动 banner :1529），API 读的是 stderr 镜像。**选型 (a) 内存环**，弃 (b) 日志文件 env：launchd 已接管 stderr 文件（com.ctyun-stream-fix-proxy.plist），第二文件路径有旋转/归属冲突；环方案零新 I/O、单行结构化日志现成。

**留痕环（新函数，置于 _record_empty_retry :505 附近）**

```python
def _snapshot_text(raw: bytes, cap: int) -> str:
    # utf-8 replace 解码；超 cap 截断并追加 "…[truncated]" 标记

def record_error_event(kind, model=None, path=None, upstream_status=None, exc=None,
                       body=None, response=None, filtered=0, retried=0,
                       retry_reason="") -> None:
```
`CAPTURE_ERRORS` 为 False 直接返回（off=只走现有计数，不留痕不抓 body，杜绝 prompt 默认入内存）。条目 schema：`{id, ts, kind, category, model, path, upstream_status, exc(≤200ch，复用 _log:826-827 规则), body(≤4096ch), response(≤2048ch), filtered, retried, retry_reason}`；id 进程内单调递增（ERROR_LOCK 内）。

**留痕落点（每处 2-4 行，紧邻既有 _log/_record_request 调用）**
- :642-648 首呼 open 失败：kind=SYNTH_502，exc+body。
- :665-671 重试 open 失败：kind=SYNTH_502，exc+body+retried=1+retry_reason。
- :657-664 _EmptyStream 捕获处：kind=EMPTY_RETRY，response=`b"".join(exc.lines)`，retry_reason=exc.reason（重试成功也留此条——它记录的是首呼事件）。
- :674-678 eof-without-done：kind=EOF_NO_DONE，body+filtered（response=None，见 Exclusions）。
- :679-687 终态（SSE 与 buffered 合并一处调用）：classify_outcome(resp.status, poison_filtered=filtered) 替换 :651/:673 三元与 error= 硬编码；capture=True 时按 kind=UPSTREAM_5XX/REQUEST_4XX 留痕，buffered 5xx 附 response=`data[:2048]`。
- _proxy :610-618 499 路径：log_result 改由 classify_outcome(client_abort=True) 供给，不留痕不计错（现状）。

**config 通道（feature 2）**
- `persist_upstream(base, path, capture_errors=False)` :160-167 — payload 加 `"capture_errors"` 键；capture_errors 由参数传入（调用方 :445 已持 _CFG_LOCK，函数内禁再取锁）。
- `save_stats_counters` :292-315 — 现有 `with _CFG_LOCK:` (:304) 内随 base 一并读 CAPTURE_ERRORS 写入，持久化文件 v2：`{"upstream_base", "capture_errors", "stats"}`。
- 新 `load_capture_errors(path) -> bool`（load_stats_counters :329 旁，同款容错：缺失/非 bool → False）。
- 新 `set_capture_errors(enabled: bool)`（set_upstream_base :443 旁；锁内改全局+persist_upstream，save_stats_counters 锁外调——镜像 :449-451 死锁注释）。
- `main()` :1477-1494 — 启动时 load_capture_errors 回填 CAPTURE_ERRORS。
- AdminHandler.do_POST :854-883 — 接受可选 `"capture_errors"` 键：非 bool → 400；可与 upstream_base 同 POST；鉴权复用 write_allowed :865-870；200 响应体加 `capture_errors` 字段。do_GET /api/config :847-850 响应加 `capture_errors`。未知键仍忽略（向后兼容）。

**新端点（AdminHandler.do_GET :839-852 加路由）**
- `GET /api/errors` → `200 {"capture_errors": bool, "count": n, "events": [...]}`（newest-first，**不含 body/response**）；`?id=N` → 单条全量（含 body/response）；id 非整数 → 400；不存在 → 404。**鉴权口径：列表开放（敏感度 ≈ 已开放的 /api/stats 的 model/path/status），`?id=` 详情非本机需 X-Admin-Token——复用 write_allowed :435-440（localhost 恒放行、LAN hmac 比对）**。理由：body 快照含用户 prompt，属最高敏感级，按写边界管；列表开放使 dashboard 后续接入零成本。
- `GET /api/logs?cursor=&tail=N` → `200 {"lines": [{"seq": int, "line": str}], "next_cursor": int, "oldest_seq": int, "ring_max": 1000}`。契约：tail 缺省 100，合法 1..1000；cursor=C 返回 seq>C 最多 500 条 oldest→newest，next_cursor=末条 seq（空则原值返回）；cursor 早于 oldest_seq 时返回存量并可据 oldest_seq 察觉淘汰；cursor>最新 → lines=[]、next_cursor=C；参数非 int / tail 越界 / cursor 与 tail 同给 → 400。无鉴权（行内容=method/path/status/model/异常文本，与 /api/stats 同级；请求体永不入日志行）。分页逻辑收敛为纯函数 `logs_snapshot(cursor=None, tail=None) -> dict`（锁内取副本后计算，可单测）。

### 2. /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py
- 纯函数单测（:411 ProxyDashboardUnitTest 模式，load_proxy_module:382）：classify_outcome 全映射表（含 ValueError）；empty_stream_should_retry；_snapshot_text 截断；record_error_event 环淘汰/id 单调/off 不入环；logs_snapshot cursor 增量/越界/互斥语义。
- 集成测试（:1287 AdminIntegrationTest 模式，start_proxy:266 / admin_get:390 / admin_post:399；admin_get 需加可选 headers 参数以测 token）：POST capture_errors → GET 回读 → persist 文件含键 → seed_persist:279 预置重启后仍 on；fail_500 → /api/errors kind=upstream_5xx 且 ?id= 含 response；make_fake_upstream(poison=True) → poison_hit；fault_finish_stream → eof_without_done；empty_stream → empty_retry；/api/logs 行含 "REQ POST"、cursor 二次拉取 lines=[]、非法参数 400；?id= 不存在 → 404。

### 3. /Users/peter/Documents/project/ctyun-stream-fix-proxy/README.md
- :57 API 区补 /api/errors、/api/logs、capture_errors 与鉴权口径；:69 用例数更新（54 → 新总数）；环境变量表 :45-54 零新增。

## Acceptance Criteria
- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（现有 54 例 + 新增 ≈18 例；必须 /usr/bin/python3，README:69），exit 0。
- 单测锁死 classify_outcome 8 行映射表，逐行断言 category/log_result/counts_error/capture。
- 集成断言：默认 off 时 /api/errors 返回 `{"capture_errors": false, "count": 0, "events": []}`；on 后五种 kind 在对应假上游场景均可取到；capture_errors 经 POST→GET→重启（seed_persist）roundtrip 保持。
- /api/errors 列表无 body/response 键；?id= 有；404/400 分支可测。
- /api/logs：tail=20 返回 ≤20 行；cursor 增量拉取至 lines=[]；cursor+tail 同给 → 400。
- 统计口径回归：errors_total 仅 502 合成路径 +1；eof_without_done_total/empty_retries_total/errors_upstream 语义不变（现有相关用例全绿即证）。
- 手工 curl：`curl -s -X POST http://127.0.0.1:7921/api/config -d '{"capture_errors":true}'`；`curl -s http://127.0.0.1:7921/api/errors`；`curl -s 'http://127.0.0.1:7921/api/errors?id=1'`；`curl -s 'http://127.0.0.1:7921/api/logs?tail=20'`；`curl -s 'http://127.0.0.1:7921/api/logs?cursor=<next_cursor>'`。

## Risks
- _proxy_relay :651-687 是行为敏感区：log_result/error= 改由 classifier 供给，映射错位会改统计口径——单测锁映射表 + 现有 stderr 断言（如 :368 filtered=1）双兜底。
- 锁纪律：record_error_event 只取 ERROR_LOCK；set_capture_errors 必须锁外调 save_stats_counters（:449-451 同线程非重入死锁前例）；LOG_RING append 位于信号处理器路径（_on_signal :1496-1503 → _safe_log_stderr）——仅 deque.append+int 递增，无重入风险，但不得在锁内做 I/O。
- /api/errors 详情含用户 prompt 走 LAN 明文 HTTP + token 头，与现有 POST /api/config 同一嗅探面；README 标注不建议跨不可信网段使用。
- Python 3.9 兼容基线（README:69）：只用 namedtuple/deque/现有语法，不引 dataclasses 之外新依赖（本来也零依赖）。
- 持久化文件 v2 新键 capture_errors：旧文件缺键由 load_capture_errors 容错为 False；save 两条路径（persist_upstream/save_stats_counters）都必须带该键，漏一条会出现"热切上游把开关抹回 False"的回归——集成 roundtrip 测试覆盖。

## Exclusions
- 不引入 CLIProxyAPI 式凭证池、渠道冷却调度、插件系统、多协议/多上游转换、负载均衡等重型能力。
- 不做 dashboard UI 改版（/api/errors、/api/logs 仅 API；UI 接入另开 episode）。
- 错误留痕与日志环不持久化（内存环，重启即清，与 RECENT_REQUESTS :125 同级）；capture off 不 wipe 存量（停止新增、自然淘汰）。
- EOF-without-DONE 场景 response 片段为 null——不为它新增尾段缓冲。
- 不改空流重试策略本身与 EMPTY_RETRY_MAX 语义。
- IMPLEMENT 中发现的独立新问题记 docs/superpowers/followups.md，不扩本 scope。
