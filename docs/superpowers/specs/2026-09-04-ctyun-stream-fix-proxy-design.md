# ctyun-stream-fix-proxy 设计 spec（simplified mode）

背景：TY Pro（天翼云网关 `eaichat.ctyun.cn`）模型 `deepseek-v4-pro-0813-oc` 的 SSE 流在 `data:[DONE]` 前多发一行非法 `data:null`，AI SDK openai-compatible 解析器 chunk schema 校验抛 `AI_TypeValidationError`（retryable=false），每 turn 必失败。方案已定：本地阻塞式反代剥掉该行，TY Pro baseURL 改指本地代理。数据流：ZCode → `http://127.0.0.1:7920`（剥行）→ `https://eaichat.ctyun.cn/ai/platform/v2/cp/<原 path>`。

## Goal
让 TY Pro `deepseek-v4-pro-0813-oc` 在 ZCode 中可用：本地 `127.0.0.1:7920` 反向代理仅剥除上游 SSE 中的 `data:null` 行，其余流量（glm-5.3、非 SSE 响应）逐字节透传。

## Files to Change

### 1. 新建 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`
纯 stdlib（`http.server` + `http.client` + `re`，零第三方依赖），全阻塞——规避旧 opencode-forward-proxy 的 asyncio/CLT-Python3.9 `start_tls` 坑（Python ≥3.9 即可）。锚点：

```python
UPSTREAM_BASE = os.environ.get("CTYUN_UPSTREAM_BASE", "https://eaichat.ctyun.cn/ai/platform/v2/cp")
LISTEN_HOST, LISTEN_PORT = "127.0.0.1", int(os.environ.get("CTYUN_LISTEN_PORT", "7920"))
UPSTREAM_TIMEOUT = 600                          # 长生成不中途断
POISON_RE = re.compile(rb"^data:\s*null\s*$")   # 整行精确匹配；data:[DONE] 不命中
HOP_HEADERS = {"connection", "keep-alive", "proxy-connection",
               "te", "trailer", "transfer-encoding", "upgrade"}

class ProxyHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def _proxy(self) -> None: ...                              # do_GET/do_POST/do_PUT/do_DELETE/do_PATCH 均指向它
    def _relay_sse(self, resp: http.client.HTTPResponse) -> int: ...   # SSE record 级剥除；返回 filtered record 数
    def _relay_buffered(self, resp: http.client.HTTPResponse) -> None: ...

def main() -> None: ...   # ThreadingHTTPServer(LISTEN_HOST:PORT, ProxyHandler), daemon_threads=True
```

- `_proxy`：按 `Content-Length` 读请求体；转发头 = 原头剥 `HOP_HEADERS` 与 `Host`（http.client 自带）+ 强制 `Accept-Encoding: identity`（防 gzip 破坏按行过滤）；`HTTPSConnection("eaichat.ctyun.cn", timeout=600)`，任意 method/path 原样 prepend `UPSTREAM_BASE` 转发。
- SSE 判定：响应 `Content-Type` 含 `text/event-stream` → `_relay_sse` 按 SSE record 处理：逐行 `readline()` 缓冲，空行（`b"\n"`/`b"\r\n"`）为 record 终结；record 内任一行 rstrip 后命中 `POISON_RE` → 整 record 连同终结空行丢弃并计数，否则按缓冲原字节写出 + 终结空行，逐 record `flush()`（保持流式）；EOF 残留 record 同规则收尾。
- 下游响应头（SSE）：原状态码 + `Content-Type` 等透传，剥 `Content-Length`/`Transfer-Encoding`，加 `Connection: close` 并 `self.close_connection = True`——剥行后长度必变，close-delimited（HTTP/1.1 合法）替代 Content-Length。
- 非 SSE：`_relay_buffered` 读全量后按实际字节数设 `Content-Length` 原样转发。
- 异常：上游连接失败/超时 → 502 纯文本，`result=error`；上游状态 ≥400 照常转发（多为 JSON，走 buffered），`result=upstream-err`；成功 `result=ok`。
- 日志：每请求一行 stderr（`print(..., file=sys.stderr, flush=True)`），格式固定：`REQ {method} {path} -> {status} dur={}s result={ok|upstream-err|error} filtered={n}`。
- 环境变量两个仅为测试 seam，生产走默认值。

### 2. 新建 `/Users/peter/Library/LaunchAgents/com.zcode.ctyun-stream-fix-proxy.plist`
照现有 `com.zcode.aliyun-env.plist` 风格（同 XML header/doctype、2 空格缩进）。键锚点：`Label=com.zcode.ctyun-stream-fix-proxy`；`ProgramArguments=["/usr/bin/python3", "/Users/peter/.local/bin/ctyun-stream-fix-proxy.py"]`（安装时 `/usr/bin/python3 --version` 实测 ≥3.9）；`RunAtLoad=true`；`KeepAlive=true`；`ThrottleInterval=10`；`ProcessType=Background`；`StandardOutPath=/Users/peter/.local/log/ctyun-fwd.log`；`StandardErrorPath=/Users/peter/.local/log/ctyun-fwd.err`。安装步骤：`mkdir -p /Users/peter/.local/bin /Users/peter/.local/log`（幂等创建）→ `launchctl load -w <plist>`。

### 3. 编辑 `/Users/peter/.zcode/v2/config.json`
- 先备份：`cp /Users/peter/.zcode/v2/config.json /Users/peter/.zcode/v2/config.json.bak.20260904-ctyun-proxy`。
- `:613`（provider 块 `"57b84b95-a300-4b97-9e05-bc71c8fa6096"` 起于 `:608`）：`"baseURL": "https://eaichat.ctyun.cn/ai/platform/v2/cp"` → `"baseURL": "http://127.0.0.1:7920"`；`:612` `apiKey` 不动；旧 TY（`144922ca`）及其他 provider 块不碰。
- 端口冲突联动：7920 被占 → 代理 `LISTEN_PORT` 默认值与本 baseURL 同步改 7921（全系统仅此两处联动点）。

### 4. 新建（测试）`/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`
- `def make_fake_upstream(poison: bool) -> int:` — stdlib 假上游（`127.0.0.1:0` 随机端口），`POST /v1/chat/completions` 回 SSE 毒流（close-delimited）：`data: {"choices":[{"delta":{"content":"A"}}]}`、`data: {"choices":[{"delta":{"content":"B"}}]}`、`data:null`、`data: [DONE]`；`GET /plain` 回带 `Content-Length` 的 JSON。代理经 `CTYUN_UPSTREAM_BASE`/`CTYUN_LISTEN_PORT` 指向假上游。
- `def wait_port(port: int, timeout: float = 5.0) -> None:` 轮询连接就绪。
- 断言：①输出不含 `data:null`；②以 `data: [DONE]` 结尾；③内容 chunk `A`/`B` 逐字节保留；④`/plain` 经代理字节一致且 Content-Length 不变；⑤对照组 `poison=False` 时 `filtered=0` 且输出与直连一致。
- TDD 顺序：先写测试并运行（失败：代理文件缺失/连接拒绝，贴 stderr）→ 实现代理 → 重跑全绿。

## Acceptance Criteria
- ① `python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` 全绿，贴实际 stdout + exit code 0。
- ② `launchctl load -w` 后 `lsof -iTCP:7920 -sTCP:LISTEN` 有 python3 进程。
- ③ 真实 curl 经代理 POST `/v1/chat/completions`（`deepseek-v4-pro-0813-oc`, stream）：流中无 `data:null`、以 `data: [DONE]` 结束、内容与直连一致。
- ④ 经代理打 `glm-5.3`：行为不变（日志 `filtered=0`）。
- ⑤ config 切换后 ZCode TY Pro 正常出流；旧 TY（144922ca）不受影响。

## Risks
- 上游将来去掉 `data:null` → 过滤零命中，无害透传，代理可保留。
- 代理进程挂 → TY Pro 全断；`KeepAlive` + `ThrottleInterval=10` 自愈，ZCode 侧报错可见。
- 端口 7920 冲突 → 换 7921（Files #3 联动点，两处同步）。
- `/usr/bin/python3` 随 CLT 变动 → 仅 plist `ProgramArguments` 一处换解释器路径。

## Exclusions
- 不改 `~/.zcode/agents/*.md` frontmatter。
- 不修/不上报上游网关。
- 不做鉴权/加密（仅绑 127.0.0.1）。
- 不动 opencode-forward-proxy（7898 链路独立；原脚本已不存在，不属本 episode）。
- 不动旧 TY 及其他 provider 块、不新增依赖包。

## R31 Evidence
[R31-S1] 问题现场命中：2026-09-04 原始 curl 复现——`deepseek-v4-pro-0813-oc` SSE 流在 `data:[DONE]` 前出现非法 `data:null` 行；ZCode 每 turn 报 `AI_TypeValidationError: expected object, received null (path [])`，retryable=false，turn 必失败。
```
$ curl -sN -X POST https://eaichat.ctyun.cn/ai/platform/v2/cp/v1/chat/completions -H "Authorization: Bearer $TY_KEY" -d '{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[...]}' | tail -5
data: {"id":"...","choices":[{"delta":{"content":"...done"}}]}
data:null

data: [DONE]
```
[R31-S2] 根因：天翼云网关仅对该模型流末尾多发一行 `data:null`（同网关 `glm-5.3` 与旧 TY `deepseek-v4-flash-0731-oc` 无此行）；AI SDK openai-compatible 解析器对每个 `data:` 负载按 chunk schema 校验，`null` 非法即抛不可重试错误，整 turn 失败。修复面在本地：反代剥行，客户端与网关零改动。
```
$ curl -sN -X POST https://eaichat.ctyun.cn/ai/platform/v2/cp/v1/chat/completions -H "Authorization: Bearer $TY_KEY" -d '{"model":"glm-5.3","stream":true,"messages":[...]}' | tail -3
data: {"id":"...","choices":[{"delta":{}}]}

data: [DONE]
```
