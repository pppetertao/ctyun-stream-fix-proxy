# ctyun-proxy REQ 日志行加时间戳与 model（simplified mode）

背景：2026-09-06 TY Pro 空返回溯源发现取证盲区——代理 REQ 日志行无时间戳、无 model，dashboard recent 仅存最近 100 条（deque maxlen=100，:57）会滚出，事件无法在代理侧定位。本 episode 纯观测性改造：REQ 行加 `ts=` 与 `model=` 两个字段，请求处理行为零变更。

## Goal

REQ 日志行（stderr）统一升级为 `REQ %s %s -> %d dur=%.1fs result=%s filtered=%d model=%s ts=%s`：`ts` 为本地时区 ISO 8601 秒级时间戳，`model` 复用 `extract_model` 结果（取不到为 `-`），使空返回类事件可按时间+模型在 stderr 精确 grep；转发与统计行为零变更。

## Files to Change

### 1. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`（无新 import）

- **`_log`（:444-447）**：签名加可选参 `model=None`，格式串与实参改为——

```python
def _log(self, started: float, status: int, result: str, filtered: int, model=None) -> None:
    _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d model=%s ts=%s"
                     % (self.command, self.path, status, time.time() - started,
                        result, filtered, model or "-",
                        time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
```

时间戳格式理由：`%Y-%m-%dT%H:%M:%S%z`（如 `2026-09-06T14:32:05+0800`）= 本地 ISO 8601 + 数字时区偏移，人眼可扫读；经 `time.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")` 机械换算即得 dashboard recent 的 epoch float（`_record_request` :257）或 ZCode 会话日志的 UTC ISO——两个关联目标都只是时区偏移加减。复用已 import 的 `time`（:24）零新增依赖；秒级精度够用（次序由既有 `dur=` 与 stderr 行序补足）。`model or "-"`：`extract_model`（:99-110）非 JSON/缺字段合法返回 None，占位符不抛异常不改处理路径；strftime 调用点现取现格式化，无共享状态不加锁（线程安全）。字段尾插：`REQ` 前缀与既有键值序逐字节不变，存量 grep 与子串断言零破坏。
- **`_proxy_relay`（:357、:367、:372 三处 `_log` 调用）**：各加第 5 实参 `model=model`（变量来自 :331 `extract_model(body)`）——502 error / SSE / buffered 非 SSE 三路径全覆盖。
- **`_proxy` 499 aborted（:321）**：调用保持原样（不传 model → 行内 `model=-`）——abort 时 body 可能未读，见 Risks。

### 2. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`

- **import 区（:12-25）**：加 `import re`（新测试用；现有 import 无 re）。
- **AdminIntegrationTest 新增 `test_req_log_line_has_ts_and_model`（插 :733 `test_client_abort_is_quiet_and_not_error` 旁）**：复用 setUp 的 `self.proc`/`self.proxy_port` → `post_sse(self.proxy_port)`（:210，body 带 `"model":"deepseek-v4-pro-0813-oc"`）→ terminate/wait 后 `stderr_text(self.proc)`（:132，17d26fd 基建）→ 断言 stderr 有行匹配
  `^REQ POST /v1/chat/completions -> \d+ dur=\d+\.\ds result=\S+ filtered=\d+ model=deepseek-v4-pro-0813-oc ts=\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{4}$`（re.M），并取 ts 做 `time.strptime(ts, "%Y-%m-%dT%H:%M:%S%z")` 回析成功（防 %z 平台差异静默退化）。
- **`test_client_abort_is_quiet_and_not_error`（:733-765）**：:763 `assertIn("result=aborted", stderr)` 后加 `self.assertIn("model=-", stderr)`（499 占位路径锁定）。
- **受影响用例核对（实测 grep 结论：现有 0 个破坏）**：断言 REQ 行仅三处且全是 assertIn 子串——:278 `filtered=1`、:286 `filtered=0`（尾插字段存活）、:763 `result=aborted`（存活）；:518/:547-550 为上游 stderr drain 单测与 REQ 行无关。改后 32 用例（31+1）。

### 3. 同步归档

- `/Users/peter/.local/bin/` 两文件过测后字节级复制到 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/`（同名两文件），`cmp` 验证（代码不在 git 仓库，无 commit；worktree 只承载 docs）。

## Acceptance Criteria

- ① `/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` 全绿（32 用例），贴 stdout 末尾 + exit 0。
- ② `/usr/bin/python3 -m py_compile` 两文件 exit 0。
- ③ 实机 REQ 行验收：`launchctl kickstart -k` 重启（stderr 落点按 launchd plist StandardErrorPath，实测时确认）后发一条经代理请求，grep 最新 REQ 行含 `model=<model> ts=<时间>`，且 ts 与 `date +%Y-%m-%dT%H:%M:%S%z` 秒级吻合。
- ④ `cmp` 生产文件与 test 文件（bin vs 归档目录）均 exit 0。
- ⑤ `node ~/.zcode/scripts/validate-r31-evidence.mjs` 对本 spec exit 0。

## Risks

- `%z` 平台差异：macOS 系统 Python 的 `time.localtime()` struct 带 tm_gmtoff，`%z` 稳定输出 `+0800`；新测试的 strptime 回析断言即为此设防。
- 499 aborted 行恒 `model=-`：REQ 行无法归因 abort 请求的模型（与 :322 `_record_request(model=None)` 现状一致），by_model 统计不受影响。
- 尾插字段对"行尾锚定"的消费方是格式变更：已知消费方仅人眼 grep 与测试子串断言，均兼容。
- 回归护栏：不碰 09-05 修复的 stderr drain / kill_registered 基建（`stderr_text` :132、`kill_registered` :137、:547/:552 两用例）——本改动不触这些函数，①的全量 32 绿兜底。

## Exclusions

- 非 REQ 日志行不加 ts/model：启动行、毒 record 预览行、stats flush/save 失败行（:210、:915 等）。
- dashboard 与 RECENT_REQUESTS 结构不变（ts 仍 epoch float）；不改 DASHBOARD_HTML、不改 `/api/stats`。
- 不做 499 路径 model 回传改造（需穿异常路径，行为变更风险）。
- 不做日志落盘/轮转（REQ 行仍仅到 stderr）；不动 launchd plist、剥行正则、鉴权、upstream 解析、持久化格式。
- 不加第三方依赖（生产文件零新 import；test 文件仅 stdlib `re`）。

## R31 Evidence

[R31-S1] 问题现场命中（2026-09-06 空返回溯源实测）：REQ 行格式串只有 command/path/status/dur/result/filtered 六要素，无时间戳无 model；recent 为 maxlen=100 滚动 deque，事件滚出后代理侧失联——
```
$ grep -n 'REQ %s\|def _log' /Users/peter/.local/bin/ctyun-stream-fix-proxy.py
444:    def _log(self, started: float, status: int, result: str, filtered: int) -> None:
445:        _safe_log_stderr("REQ %s %s -> %d dur=%.1fs result=%s filtered=%d"
$ grep -n 'RECENT_REQUESTS = ' /Users/peter/.local/bin/ctyun-stream-fix-proxy.py
57:RECENT_REQUESTS = collections.deque(maxlen=100)
```
[R31-S2] 根因：`_log`（:444-447）格式串从未含时间与模型字段；`extract_model`（:99）结果只进内存统计（`_record_request` :257 → RECENT_REQUESTS，:57 maxlen=100 滚出即失）。修复面收敛为 `_log` 格式串 + `_proxy_relay` 三处调用点传参；baseline = 全量 31 用例（grep 实测）——
```
$ grep -c 'def test_' /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py
31
$ grep -n 'assertIn("result=aborted"' /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py
763:        self.assertIn("result=aborted", stderr)
```
