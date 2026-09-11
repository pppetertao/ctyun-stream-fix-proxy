# ctyun-dashboard v1.1 优化 spec（simplified mode）

背景：v1 dashboard（4f2442a）上线后实机运行暴露三个可量化的问题，本 episode 收敛修复：客户端中断 traceback 噪音、统计无 model 维度、累计计数重启清零。附带 sparkline 剥行叠加展示。7920 剥行语义与 admin 权限模型不变。

## Goal

① 中断静默化：relay 阶段 `ConnectionError` 与 `socket.timeout` 不再进 `handle_error` 打 traceback，改记单行 `result=aborted`（status 499，nginx 惯例），不计 errors_total；**实现期发现**：客户端关闭是优雅 FIN 而非 RST（sample 实测代理线程卡 `__sendto` 不返回），send 会无限期阻塞把线程永久钉死——故 relay 期间对客户端 socket 设 send 超时 `SEND_TIMEOUT_S`（默认 60s，env `CTYUN_SEND_TIMEOUT` 为测试 seam），超时/断流统一按 aborted 记录；② model 维度：从 POST body JSON 提取 `model` 字段，per-model 计数 + recent 表新增模型列 + dashboard「按模型」chips；③ 计数持久化：`requests_total/filtered_total/errors_total` 并入现有持久化文件（SIGTERM/SIGINT 落盘 + 60s 脏刷守护线程），重启续算；④ sparkline 叠加红色剥行柱。

## Files to Change

### 1. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`（现状 553 行）

- **`_proxy`（:250 外壳）加 except 分支**：`except ConnectionError as exc:` → `self._log(started, 499, "aborted", 0)` + `_record_request(..., 499, ..., error=False)`，注释自证三件事（吞的是什么：relay 阶段任一端断流，客户端取消是常态；为何不重抛：重抛只进 handle_error 打 20+ 行 traceback 且无 status 记录；误标容忍：上游中途 RST 罕见且对"turn 中断非代理故障"语义无损）。catch 放在 `_proxy` 层使 `_relay_sse`/`_relay_buffered`/`_reply_502` 的 wfile 写失败全部覆盖；`finally active -= 1` 不动。
- **`extract_model(body: bytes) -> Optional[str]`（新增纯函数，:valid_upstream_url 旁）**：`json.loads(body.decode("utf-8","replace"))`，dict 且 `model` 为非空 str 才返回；ValueError → None（注释自证：GET/非 JSON body 是常态，统计字段 best-effort）。`_proxy_relay`（:255）读 body 后调用，`_record_request` 增参 `model=None`。
- **`_record_request`（:142）**：recent 条目加 `"model": model`；`STATS["by_model"][model]["requests"/"filtered"]` 累计（仅 model 非空时；键数上限 32 防膨胀，超出丢弃）。`STATS` 初始含 `"by_model": {}`（snapshot 顺带带出，JSON 可序列化）。
- **持久化合并（`persist_upstream`/`_write_persist` 重构，:81）**：统一 `_write_persist(path, base, stats_counters)` 原子写 `{"upstream_base": ..., "stats": {"requests_total","filtered_total","errors_total"}}`；`set_upstream_base` 改调它（顺带把当下计数一起落盘）。新增 `save_stats_counters(path)`（锁下取三计数 → `_write_persist`）与 `load_stats_counters(path)`（缺文件/损坏/无 stats 键 → 三零值，注释自证：文件是缓存，损坏回落零值等同首次运行）。`main()` 启动：`resolve_upstream_base` 后 `STATS` 三计数 = `load_stats_counters` 覆盖赋值；`signal.SIGTERM/SIGINT` handler → `save_stats_counters` + `SystemExit(0)`；daemon 线程每 60s 检查 `_stats_dirty`（`_record_request` 置位，落盘后清）脏则落盘。`_stats_dirty` 模块级 bool。
- **`DASHBOARD_SRC`（:358）**：sparkline 每桶叠加 `filtered` 红柱（`var(--err)`，同比例缩放，先红后琥珀覆盖）；卡片标题改「请求节奏 · 最近 10 分钟（琥珀=请求，红=剥行）」；新增「按模型」卡（chips：`model n 请求 · m 剥行`，来自 `by_model`，空态「暂无按模型统计」）；最近请求表加「模型」列（7 列，空态 colSpan=7，model 空 → "—"）；footer 文案改「累计计数跨重启保留（每 60s 落盘），最近请求/剥行流带为内存数据」。

### 2. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`（现状 ~450 行）

- **单元**：`extract_model`（合法/缺失/非 JSON/空 body/非 dict/非 str model）；`save_stats_counters`+`load_stats_counters` 往返（含保留 `upstream_base`、损坏/缺失/legacy 无 stats 键 → 零值）；`_record_request` by_model 累计与 32 上限。
- **集成**：预写持久化文件（计数 7/3/1 + upstream_base）→ 起代理 → `/api/stats` 计数为 7/3/1 且再 `post_sse` 后 requests_total=8（续算）；`post_sse` 后 `recent[-1]["model"] == "deepseek-v4-pro-0813-oc"` 且 `by_model` 有对应条目（`post_sse` :107 payload 本就带 model）；SIGTERM 落盘：起代理 → `post_sse` → `proc.terminate()` → wait → persist 文件 `stats.requests_total >= 1`；client-abort：假上游新增 `big` 模式（20000 条 record ≈ 1.1MB SSE）→ 客户端 POST 读首块即 close → 断言 stderr 含 `result=aborted`、不含 `Traceback`、`/api/stats` errors_total == 0；dashboard 断言扩展：`var(--err)` 出现于 JS、含「按模型」「模型」表头、「跨重启保留」。
- **既有 12 测试零改动全绿**（剥行回归 + admin 权限矩阵不变）。

## Acceptance Criteria

- ① `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（原 12 + 新增 ≥7），贴 stdout + exit 0。
- ② `/usr/bin/python3 -m py_compile` exit 0。
- ③ 部署后实机：`/api/stats` 含 `by_model`；`recent[*]` 含 `model` 键；日志 tail 无 Traceback 且有 `result=aborted` 样本（若验证窗口内无中断则以测试件为准）；`kill -TERM` 后 persist 文件计数非零、重启进程后 `/api/stats` 计数延续。
- ④ 7920 剥行回归不变（既有测试）；归档 `~/Documents/project/ctyun-stream-fix-proxy/` cmp byte-identical。

## Risks

- 上游中途 RST 会被记为 `aborted`（无法与客户端取消区分）——对账时以 dur 与 status 499 语义为准，误标无损"turn 中断非代理故障"结论。
- SIGTERM 时序：handler 在主线程 serve_forever 中断点执行，落盘后 SystemExit，finally 仍关 server；kill -9 丢最近 ≤60s 计数（脏刷兜底）。
- `by_model` 键上限 32：恶意多模型请求不膨胀内存。
- 持久化文件双写方（set_upstream_base / save_stats_counters）统一走 `_write_persist` 全量写，无半截文件（os.replace 原子）。
- 兼容旧格式文件（仅 upstream_base）→ 计数零值起步，不报错。

## Exclusions

- 不做 GET 鉴权/HTTPS/时序历史库/buffered 流式化/plist 加 token env/日志轮转（client-abort 修复后日志增速极低）。
- 不动 launchd plist、v2/config.json、7920 剥行正则与转发头逻辑。
- 测试进程 ResourceWarning（unclosed pipe，测试基建风格遗留）不修。

## R31 Evidence

[R31-S1] 问题现场命中（2026-09-05 采集）：① 日志已出现 2 次 `BrokenPipeError: [Errno 32] Broken pipe` traceback（ZCode 取消 turn 断开读端，proxy 写响应 EPIPE 进 handle_error，无 status 记录）；② 内存计数与日志 lifetime 脱节——部署重启后 `/api/stats` 从零起算；③ recent 表无 model 维度，glm-5.3 与 v4-pro 无法区分。
```
$ grep -c "Traceback" ~/.local/log/ctyun-fwd.err
2
$ curl -s http://127.0.0.1:7921/api/stats | /usr/bin/python3 -c "import json,sys; d=json.load(sys.stdin); print(d['requests_total'], d['filtered_total'])"
107 0
$ grep -o 'filtered=[0-9]*' ~/.local/log/ctyun-fwd.err | awk -F= '{s+=$2} END {print s}'
5
$ grep -c 'REQ' ~/.local/log/ctyun-fwd.err
216
```
[R31-S2] 根因：① `_proxy`（:250）只包住上游连接段，relay 阶段写客户端失败直接冒泡进 socketserver `handle_error`（traceback 且无单行日志）；② `_record_request`（:142）无 body 解析入口，model 信息在转发前已读入却丢弃；③ 持久化文件只写 `upstream_base`（`persist_upstream` :81 schema 无 stats），计数器纯内存。修复面全部在代理进程内，客户端与网关零改动。
```
$ /usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py && echo compile-ok
compile-ok
$ /usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -3
Ran 12 tests in 4.771s
OK
```
