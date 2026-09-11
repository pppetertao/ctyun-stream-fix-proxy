# ctyun-proxy-dashboard 设计 spec（simplified mode）

背景：`~/.local/bin/ctyun-stream-fix-proxy.py`（launchd 常驻，127.0.0.1:7920）只做剥行反代，运行情况只能靠 grep 日志，上游端点是 import 期常量。用户需求：监测网页实时看请求数/剥行累计等、可在网页上设置上游端点、局域网可访问。裁定：launchd 常驻 + 代理进程内嵌 web dashboard 即满足"无界面 mac 应用"语义，不打包 .app。数据流不变：ZCode → `http://127.0.0.1:7920`（剥行）→ 上游；新增旁路 `0.0.0.0:7921`（admin/dashboard，只读统计 + 受限写）。

## Goal

给剥行代理进程内嵌一个 admin/dashboard HTTP 服务（7921）：内存实时统计（请求数、剥行累计、毒行率、活跃连接、错误数、最近请求、最近剥除的 record 预览）+ 单文件零依赖中文 dashboard + 上游端点运行时热切换（持久化）。7920 代理行为逐字节不变。

## Files to Change

### 1. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`（170 行 → 约 550 行）

新增 imports：`json`、`collections`、`threading`、`hmac`。锚点（现状 file:line）：

- **常量区（:18–25 扩展）**：`DEFAULT_UPSTREAM_BASE = "https://eaichat.ctyun.cn/ai/platform/v2/cp"`；`UPSTREAM_BASE` 改为**运行时可变**模块全局（初始 = DEFAULT，`main()` 启动时经 resolve 赋值——env seam 语义保持）；新增 `ADMIN_HOST = os.environ.get("CTYUN_ADMIN_HOST", "0.0.0.0")`、`ADMIN_PORT = int(os.environ.get("CTYUN_ADMIN_PORT", "7921"))`、`PERSIST_PATH = os.environ.get("CTYUN_PERSIST_PATH", os.path.expanduser("~/.local/etc/ctyun-stream-fix-proxy.json"))`（env 作测试 seam，隔离真实持久化文件）。
- **模块级状态（新增，`_CFG_LOCK = threading.Lock()` 与 `STATS_LOCK = threading.Lock()` 两把锁分离）**：
  ```python
  STATS = {"requests_total": 0, "filtered_total": 0, "errors_total": 0, "active": 0}
  STARTED_AT = time.time()
  RECENT_REQUESTS = collections.deque(maxlen=100)   # {"ts","method","path","status","dur_ms","filtered"}
  POISON_PREVIEWS  = collections.deque(maxlen=20)   # {"ts","preview"}，preview 截断 200 bytes
  ```
- **纯函数（新增，全部可独立单测）**：
  - `def valid_upstream_url(url: str) -> bool:` — `urllib.parse.urlsplit`，要求 scheme ∈ (http, https) 且 netloc 非空；`ValueError` → False。
  - `def resolve_upstream_base(env_base: str, persist_path: str) -> tuple:` — 优先级 env > file > DEFAULT，返回 `(base, source)`，source ∈ ("env","file","default")；文件缺失/损坏 JSON/非法 URL → 静默跳到下一级（catch 自证：缺文件是首次运行常态，非错误）。
  - `def persist_upstream(base: str, path: str) -> None:` — `os.makedirs(dirname, exist_ok=True)` + tmp 文件 + `os.replace` 原子写 `{"upstream_base": base}`。
  - `def write_allowed(peer_ip: str, token_header: str, token_env: str) -> bool:` — peer ∈ ("127.0.0.1","::1") 放行；否则需 token_env 非空且 `hmac.compare_digest(token_header, token_env)`。
  - `def set_upstream_base(base: str) -> None:` — `_CFG_LOCK` 下改 `UPSTREAM_BASE` 全局 + `_upstream_source = "api"` + persist。
- **统计记录（新增）**：`_record_request(method, path, status, dur_ms, filtered, error=False)` — `STATS_LOCK` 下累计 `filtered_total`（error 时 `errors_total += 1`）并 append `RECENT_REQUESTS`；`_record_poison_preview(raw: bytes)` — utf-8/replace 解码截断 200 append `POISON_PREVIEWS`；`stats_snapshot() -> dict` — 锁下聚合 counters + `uptime_s` + `upstream_base`/`upstream_source` + `list(RECENT_REQUESTS)` + `list(POISON_PREVIEWS)`。
- **`_proxy`（:52）拆为 instrumented 外壳**：`_proxy()` = 进入口 `requests_total += 1`、`active += 1`（try/finally 保证 `active -= 1`），实际转发逻辑原样移入 `_proxy_relay(started)`；三处出口各记一笔——上游异常 502 路径（:79–82）加 `error=True`；SSE 路径（:87–88）带 `filtered` 返回值；buffered 路径（:90–91）`filtered=0`。`_relay_sse`（:94）毒 record 丢弃分支（:110–111）加 `_record_poison_preview(b"".join(pending))`，返回值语义不变。
- **`AdminHandler(BaseHTTPRequestHandler)`（新增 class）**：`protocol_version = "HTTP/1.1"`；`GET /` → `DASHBOARD_HTML`（text/html; charset=utf-8）；`GET /api/stats` → `stats_snapshot()` JSON；`GET /api/config` → `{"upstream_base", "source"}`；`POST /api/config`（body `{"upstream_base": "..."}`，Content-Length 上限 8192 超出 413）→ `write_allowed(self.client_address[0], X-Admin-Token 头, env CTYUN_ADMIN_TOKEN)` 不过 403 → JSON 解析失败/URL 非法 400（错误信息给改法）→ `set_upstream_base` 200。`log_message` 覆写为 pass（2s 轮询访问日志是纯噪音；异常 traceback 走 handle_error 不经此，注释自证同 :46–50 风格）。
- **`DASHBOARD_HTML`（新增模块常量）**：`_DASHBOARD_SRC` 为普通 `str` 三引号常量（**禁 f-string**，CSS/JS 大括号零冲突），`DASHBOARD_HTML = _DASHBOARD_SRC.encode("utf-8")`（bytes 字面量不能含中文，故走 str+encode）。设计（"网络硬件运维台"，零外部依赖，禁 CDN）：深石板蓝 `#0F141D` 底 / `#161E2B` 卡面 / 琥珀 `#F5A623` 主强调（毒行告警语义）/ ok `#4CC38A` / err `#E5646C`；全等宽系统字体栈 `ui-monospace, "SF Mono", Menlo, monospace`。布局：顶栏（标题「CTYUN 剥行代理 · 运行台」+ uptime + 轮询状态点）→ 上游端点卡（当前值 mono 琥珀 + source 徽标 env/file/api + 输入框 + 按钮「保存上游端点」+ 内联错误/成功提示）→ 大数字卡行（请求数/剥行累计/毒行率%/活跃连接/错误数；毒行率 = filtered/requests，requests=0 显 "—"）→ 最近 10 分钟请求节奏（内联 SVG 柱状，前端按 recent ts 分桶）→ **签名元素「剥行流带」**：POISON_PREVIEWS 横向滚动 chips，琥珀左边框 + 删除线 record 文本，空态「暂无剥行记录 —— 上游干净，或尚未有 SSE 流经过」→ 最近请求表（时间/方法/路径/状态/耗时/剥行，filtered>0 行琥珀高亮）。JS：2s `setInterval` 轮询 `/api/stats`，失败态给原因与改法（「无法连接管理接口——检查代理进程是否存活」）；**所有动态数据经 `textContent` 插入（poison preview 来自上游字节，禁 innerHTML）**；按钮动词化「保存上游端点」；403 提示「非本机修改需要 X-Admin-Token」。质量底线：`<720px` 单列响应式、`:focus-visible` 琥珀 outline、`prefers-reduced-motion: reduce` 关过渡。
- **`main()`（:156）**：开头 `global UPSTREAM_BASE, _upstream_source` 并 `resolve_upstream_base(os.environ.get("CTYUN_UPSTREAM_BASE"), PERSIST_PATH)` 赋值；起 admin `ThreadingHTTPServer((ADMIN_HOST, ADMIN_PORT), AdminHandler)`（daemon_threads=True，`serve_forever` 丢进 daemon Thread），bind 失败 `OSError` → stderr 明示原因后 `SystemExit(1)` 交 launchd KeepAlive 重启；7920 代理服务器启动逻辑不变，启动日志行追加 admin 地址。

### 2. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`（189 行 → 约 380 行）

- **新增 in-process 载入**：`load_proxy_module()` — `importlib.util.spec_from_file_location("ctyun_stream_fix_proxy", PROXY_SCRIPT)`（模块顶层只读 env/定义，无副作用，可安全 exec）。
- **单元测试类（新）**：`valid_upstream_url` 正反例（http/https 过、ftp/空/无 netloc 不过）；`resolve_upstream_base` 四分支（env 优先 / file / 损坏 JSON→default / 缺文件→default，tmpdir 隔离）；`persist_upstream`+`resolve` 往返；`write_allowed` 矩阵六例（本机放行；LAN 无 token 403 语义=False；token 未设+LAN=False；token 设+对/错=True/False）；`stats_snapshot` 形状断言（键齐、类型 int/list）。
- **集成测试类（新，subprocess 复用 `start_proxy` :96 扩展）**：`start_proxy` 增注 `CTYUN_ADMIN_PORT`（`free_port()`）与 `CTYUN_ADMIN_HOST=127.0.0.1`、`CTYUN_PERSIST_PATH`（tmpdir）env，返回 proc 后等待 admin 端口；断言：`GET /admin端口/` → 200 text/html、body 以 `<!doctype html` 开头、含 `/api/stats`；`GET /api/stats` → JSON 键齐且 `post_sse` 后 `requests_total >= 1`；`GET /api/config` → `source == "env"`；`POST /api/config` 合法 URL（指向第二个假上游端口）→ 200 `{"ok": true}` → 再 `post_sse` 打到新上游（`FakeUpstreamHandler` 加 `tag` 类属性，`/plain` body 带 tag 以区分路由命中）+ `GET /api/config` 反映新值 `source == "api"` + tmp 持久化文件落盘；`POST` 非法 URL → 400；非法 JSON → 400；token env 设定后本机 POST 无头 → 200（本机白名单语义）。
- **既有 `PoisonStreamTest`/`PassThroughTest`（:162/:178）零改动**——剥行回归保证。

## Acceptance Criteria

- ① `/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py` 全绿（旧 2 + 新增 ≥7），贴 stdout + exit 0。
- ② `/usr/bin/python3 -m py_compile /Users/peter/.local/bin/ctyun-stream-fix-proxy.py` exit 0（3.9 语法兼容，禁 3.10+ 语法）。
- ③ launchd 重启后 `lsof -iTCP:7920 -sTCP:LISTEN` 与 `lsof -iTCP:7921 -sTCP:LISTEN` 各有 python3 进程。
- ④ `curl -s http://127.0.0.1:7921/api/stats` 返回 JSON 含 requests_total/filtered_total/errors_total/active/uptime_s/recent/poison_previews/upstream_base/source。
- ⑤ `curl -s http://127.0.0.1:7921/` 返回 text/html，含「剥行」与 `/api/stats` 引用。
- ⑥ `POST /api/config` 换上游 → 200 → `GET /api/config` 反映新值 source=api → 持久化文件 `cat` 验证 → 换回原值。
- ⑦ 局域网可达：`ipconfig getifaddr en0` 取 IP，`curl -s http://<LAN-IP>:7921/api/stats` 返回 200。
- ⑧ 7920 回归：经代理 GET `/plain`（打到真实上游任一路径）行为同前；归档副本 `~/Documents/project/ctyun-stream-fix-proxy/` 与部署文件 `cmp` byte-identical。

## Risks

- 7921 被占 → admin bind 失败进程退出，KeepAlive 每 10s 重试（ThrottleInterval），stderr 有明确原因；当前 `lsof` 实测 7921 空闲。
- `0.0.0.0` 暴露面：GET 无鉴权但仅统计与请求路径（无 apiKey/JWT）；写操作本机白名单 + 可选 token。家庭局域网威胁模型下可接受。
- 统计为进程内存态，重启清零（日志仍是持久事实源）；持久化仅上游端点，损坏时静默回落 default。
- 内嵌 HTML 转义：str 常量 + 一次 encode，规避大括号/中文 bytes 限制；动态数据一律 textContent。
- Homebrew 3.14.7 http.server 挂死坑 → plist 仍锁 `/usr/bin/python3`（3.9.6），本次不改 plist。
- 客户端中断 `BrokenPipeError` 噪音为既有问题，不在本 episode 修（进 followups）。

## Exclusions

- 不打包 .app / 菜单栏应用（launchd 常驻即"无界面 mac 应用"；需要 Dock 图标另开 episode）。
- 不做 GET 鉴权、HTTPS、多用户；不做统计持久化/历史库。
- 不改 launchd plist、`/Users/peter/.zcode/v2/config.json`（ZCode provider baseURL 仍指 7920）、7920 剥行行为、`agents/*.md`。
- 不修 BrokenPipeError 日志噪音、不动 7898 链路。

## R31 Evidence

[R31-S1] 问题现场命中：2026-09-05 运行台数据只能手拆日志行，无内存聚合器；上游端点为 import 期常量（`ctyun-stream-fix-proxy.py:18`），换端点须改 env + launchd 重启，运行中不可设。
```
$ grep -o 'filtered=[0-9]*' ~/.local/log/ctyun-fwd.err | awk -F= '{s+=$2} END {print s}'
5
$ grep -c 'REQ POST' ~/.local/log/ctyun-fwd.err
99
```
[R31-S2] 根因：计数仅存在于 `_log` 单行输出（`ctyun-stream-fix-proxy.py:150-153`，print 到 stderr 即丢），进程内无状态；`UPSTREAM_BASE`（:18）在 `_proxy`（:68）按模块全局读取但无任何运行时变更入口。修复面在代理进程内：新增内存统计 + 内嵌 admin 服务（7921）+ 上游热切换持久化，客户端（ZCode）与网关零改动。
```
$ /usr/bin/python3 --version
Python 3.9.6
$ /usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py 2>&1 | tail -3
Ran 2 tests in 1.157s
OK
```
