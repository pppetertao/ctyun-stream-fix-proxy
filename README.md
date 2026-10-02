# ctyun-stream-fix-proxy

本地 SSE「剥行」反向代理：在 record 级剥除上游 SSE 流中非法终止的毒 record，解决客户端 SSE 解析器校验崩溃问题。纯 Python 标准库实现，零第三方依赖。

## 背景

某 OpenAI 兼容网关（天翼云 TY，`eaichat.ctyun.cn`）的 `deepseek-v4-pro-0813-oc` SSE 流会**间歇性**在 `[DONE]` 之前多发非法 `data:null` record（毒 record = `data:null\n` 行 + 一个终结空行）。OpenAI-compatible 客户端的流式解析器遇到该 record 会直接校验失败，导致整次请求失败。

剥行必须在 **record 级**进行：只删除 `data:null` 行会残留终结空行，输出多一个 `\n`——毒 record 必须连同其终结空行整弃。

## 功能

- **record 级剥行**：按空行切分 SSE record，毒 record 整弃，其余逐 record flush 转发；SSE 下游 close-delimited，非 SSE 请求原样透传
- **双端口**：转发代理 `:7920`（默认绑 `127.0.0.1`；`CTYUN_LISTEN_HOST=0.0.0.0` 开放局域网）+ 同进程内嵌监控台 `0.0.0.0:7921`
- **监控 dashboard**（浏览器运行台，深石板蓝+琥珀，零外部依赖，2s 轮询 `/api/stats`）：
  - 实时统计：按所选时间段（近3天/近7天/本月/上月）聚合的请求数 / 剥行 / 毒行率 / 错误数；活跃连接恒实时
  - 「最近请求」最近 100 条（含模型列、耗时、剥行数；跨天时自动按日期分组显示）
  - 「剥行流带」最近 20 条毒 record 预览 + 累计 sparkline
  - 「按天统计」「按天 × 模型」随所选时间段过滤日期（上月最多 31 行；daily 分桶，双口径错误列）
  - 「模型速度对比」各模型 TTFB P50/P90（进程内直方图分位数）+ 生成 tok/s（最近 100 条流式请求按模型中位生成速度）+「延迟分布」连接/响应头/数据体三阶段 P50
  - 「Token 用量按天 × 模型」prompt/completion/cache/reasoning tokens（usage 帧抽取，cache/reasoning 为 parse-if-present，上游不回传即为 0；随所选时间段过滤）
  - 「三态可用率」ok/degraded/failed 占比条（随所选时间段过滤）
  - 「上游健康」主动探测 HEAD 结果（最近探测时间/延迟/连续失败/开关；探测告警 hover 明细）
  - v3 观测扩展（按模型区分）：按模型 TTFB 9 桶分布、三阶段 P50/P90 按模型、慢请求 Top 10、按模型错误/重试率（今日）、
    TPM 限流观测（per key × 模型 60s 窗口 used/remaining/queued/rejected/timeouts）、错误事件流（最近 50 条）、
    实时流量画像（60s 窗口出/入方向字节与 chunks/tokens 速率 + 当日按模型流式占比）、SSE 停顿按模型、TPM 排队等待 P50/P90、
    估算偏差 actual/est P50/P90、小时级 token 曲线与出流量吞吐（48h 内存环）、上游探测延迟趋势（7 天抽稀 ≤200 点折线）、
    慢请求 Top 10 含单请求生成速度列
  - 「Token 用量按 API Key」当日 per-key 用量（sha256 前 12 位脱敏，≤64 key/日，跨重启保留）
  - 「0-token 请求」所选时段零 token 请求计数与占比（被 stall/错误打断的流信号）
  - 「月末 Token 投影」按本月日均速率预估全月 prompt+completion 总量（纯前端估算）
- **网页热切上游**：`POST /api/config` 免重启切换上游 base URL（本机免鉴权；局域网访问需 `X-Admin-Token`）
- **计数口径透明**：顶部「请求数」跨重启持久化；「按天统计」按本地日期分桶、重启续算
- **launchd 常驻**：KeepAlive 自愈

## 架构

- `ThreadingHTTPServer` + `http.client` 阻塞反代，纯 stdlib
- SSE 中继逐 record flush；relay 期间客户端 socket 设发送超时（默认 60s），避免客户端断开后写阻塞钉死线程
- 头阶段（连接+请求发送+响应头读取）默认 45s 短超时、超时自动重试一次（重试对客户端不可见）；响应头到达后恢复长超时 600s 逐读 body/流式阶段
- 统计每 60s 脏刷 + SIGTERM/SIGINT 落盘（原子写），daily 桶 90 天 prune
- 错误双口径：`errors_proxy`（代理合成错误）与 `errors_upstream`（上游 ≥500 透传）互斥统计

## 部署

本机实际部署（2026-09-18 验收口径）：脚本 `~/.local/bin/ctyun-stream-fix-proxy.py`，plist `~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist`（仓库内 plist 为模板，路径按目标机调整；日志路径见本机 plist 的 StandardOutPath/StandardErrorPath）。

日常 deploy（改代码后生效）：

```sh
cp ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py
launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy
```

仓库 plist 模板已内置 `EnvironmentVariables.CTYUN_LISTEN_HOST=0.0.0.0`：live plist（~/Library/LaunchAgents/）同步该键并 kickstart -k 后转发端口 LAN 可达；不需要时删键或改回 `127.0.0.1`。

首次安装：1. `mkdir -p ~/.local/bin` + cp + `chmod +x`；2. 按目标机调整仓库 plist 模板（脚本/日志路径）拷入 `~/Library/LaunchAgents/`；3. `launchctl load ~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist`；4. 浏览器打开 `http://127.0.0.1:7921/`。

### 环境变量（seam）

| 变量 | 默认 | 说明 |
|------|------|------|
| `CTYUN_LISTEN_HOST` | `127.0.0.1` | 转发代理监听地址；`0.0.0.0` 开放局域网（转发端口无鉴权，仅可信网段） |
| `CTYUN_ADMIN_HOST` | `0.0.0.0` | 监控台监听地址 |
| `CTYUN_ADMIN_PORT` | `7921` | 监控台端口 |
| `CTYUN_ADMIN_TOKEN` | 空 | 局域网访问 `POST /api/config` 所需 token（未设则 LAN 只读） |
| `CTYUN_PERSIST_PATH` | `~/.local/etc/ctyun-stream-fix-proxy.json` | 统计持久化文件路径（原子写） |
| `CTYUN_SEND_TIMEOUT` | `60` | relay 期间客户端 socket 发送超时（秒） |
| `CTYUN_HEADER_TIMEOUT` | `45` | 头阶段（连接+响应头）超时（秒），最小值 1 |
| `CTYUN_HEADER_RETRY` | `1` | 头阶段故障（超时/断连/RST）自动重试开关（>0 开启单次重试，0 禁用） |
| `CTYUN_MODEL_PRICING` | 空 | 模型价目表 JSON 字符串（可选 cost 估算数据源）；仅落 persist 顶层 `model_pricing` schema，不实现任何计费 UI |
| `CTYUN_PROBE_INTERVAL_S` | `30` | 主动探测 HEAD 间隔（秒）；显式正数原样采用（测试加速 seam），≤0/非法值回落 `10` |

配置优先级：env > 持久化文件 > 内置默认。

## API

- `GET /api/stats` — 全量统计 JSON（计数 / daily 分桶 / daily_by_model / daily_by_key / hourly_tokens 48h 内存环 / recent 100 / 毒行流带 / range_stats 四维度聚合 / range_bounds 四维度窗口闭区间；perf 节含按模型 TTFB 9 桶直方图、三阶段 P50/P90、stalls、qwait 与 TPM 结算偏差分位；v3 新键均为加性新增，旧消费者零破坏）
- `GET /api/config` — 当前上游 base URL
- `POST /api/config` `{"upstream_base": "..."}` — 热切上游（本机免鉴权，LAN 需 `X-Admin-Token`）
- `GET /api/health` — 上游健康（最近 probe 时间/延迟/连续失败/开关 `probe_enabled`）
- `GET /api/probe_history` — 上游探测延迟历史（`{"points": [[ts, ms], ...], "count_total": n}`，均匀抽稀 ≤200 点，时间单调；无鉴权）
- `POST /api/probe` `{"enabled": true|false}` — 切主动探测开关（本机免鉴权，LAN 需 `X-Admin-Token`；env `CTYUN_PROBE_ENABLED`（`1/true` 开、`0/false` 关）优先，否则持久化顶层 `probe_enabled`，缺省开）
- `GET /api/errors` — 错误留痕列表（newest-first，不含 body/response）。无鉴权（敏感度与 `/api/stats` 同级）。默认 off（`count:0`、`events:[]`）；关闭开关只停止新增，已留痕事件保留至环自然淘汰，期间 `?id=` 详情仍可读取。
- `GET /api/errors?id=N` — 单条错误事件详情（含 body/response 快照，≤4096/≤2048 字符）。**需鉴权**：本机（127.0.0.1/::1）放行，非本机需 `X-Admin-Token` 头（与 `POST /api/config` 同一 HMAC 比对）。id 不存在返回 404。
- `POST /api/config` — 现支持可选 `capture_errors` 布尔键：`{"upstream_base":"...","capture_errors":true}`。仅 upstream_base 必填，capture_errors 可选；可单独 POST 开关。
- `GET /api/logs` — stderr 日志镜像（内存环，重启即清）。缺省返回最近 100 行；`?tail=N`（1..1000）；`?cursor=C` 分页（返回 seq>C 最多 500 条，oldest->newest）；cursor 与 tail 互斥。无鉴权（行内容=method/path/status/model/异常文本，与 /api/stats 同级）。

capture_errors 开关：
- 默认 `false`。通过 `POST /api/config` 开启（`{"capture_errors":true}`），`GET /api/config` 可回读。
- off 时错误计数照常但不留痕不抓 body（杜绝 prompt 默认入内存）。
- 持久化于 `~/.local/etc/ctyun-stream-fix-proxy.json` v2（新键 `capture_errors`），跨重启保留。
- /api/errors 详情含用户 prompt 走 LAN 明文 HTTP + token 头，不建议跨不可信网段使用。

## 测试

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

301 个用例（含 v3 观测扩展与 v3.1 吞吐：schema 迁移、daily_by_key、usage 五元组、per-model 分位、probe history、bytes_in 窗口、dashboard v3 卡）。**必须用 `/usr/bin/python3`**：Homebrew 的 Python 3.14 `http.server.HTTPServer` 构造会挂死（进程存活但不 LISTEN、零报错）。

## 计数口径

| 页面区块 | 口径 | 持久化 |
|----------|------|--------|
| 顶部「请求数 / 剥行 / 错误」 | 按所选时间段聚合（近3/近7天含今日滑动窗口；本月/上月自然月；错误=代理错误+上游5xx 合计） | 是（daily 桶跨重启） |
| 「活跃连接」 | 实时 gauge，不随时间段变化 | 否 |
| 「按天统计」「按天 × 模型」 | 随所选时间段过滤（最远回溯=上月+当月 ≤62 天 < 90 天 retention） | 主表/副表均是（90 天 prune） |
| 「最近请求」「剥行流带」 | 最近 100 / 20 条内存窗口 | 否 |
| 「模型速度对比」「延迟分布」 | 进程内 TTFB 直方图（9 桶）分位数：按模型 P50/P90 + 三阶段全局 P50 | 否（重启清零） |
| 「按模型 TTFB 分布」「三阶段按模型」「SSE 停顿按模型」 | 进程内直方图/计数（v3 per-model 双写） | 否（重启清零） |
| 「TPM 排队等待」「估算偏差」分位 | 进程内 per-model 环形（500 样本/模型）最近秩分位 | 否（重启清零） |
| 「小时级 Token 曲线」「小时级吞吐」 | 48h 内存环（hourly_tokens：tokens + bytes_out/chunks 同环滚动） | 否（重启清零） |
| 「生成速度」列 | RECENT_REQUESTS 派生（流式且 tokens>0：tokens ÷（耗时−TTFB））；模型对比列=按模型中位，慢请求列=单请求值 | 否（内存窗口） |
| 「TPM 限流观测」 | /api/tpm_stats 60s 窗口 per key × 模型桶 | 拒绝/超时计数随 stats 落盘 |
| 「错误事件流」 | /api/errors 内存环最近 50 条（capture_errors 开关控制留痕） | 否（环内存态） |
| 「上游探测延迟趋势」 | /api/probe_history 7 天 30s 采样抽稀 ≤200 点 | 否（内存 deque） |
| 「Token 用量按 API Key」 | 当日 per-key 8 字段子集（sha256 前 12 位） | 是（daily_by_key 90 天 prune，≤64 key/日） |
| 「0-token 请求」「月末 Token 投影」 | daily 桶 `requests_zero_token`（tokens=0 且非 5xx 非错误）；投影=前端本月日均×全月天数 | 是（daily 桶）/投影纯前端 |
| 「Token 用量按天 × 模型」 | usage 帧 prompt/completion tokens，随所选时间段过滤 | 是（daily_by_model 90 天 prune） |
| 「三态可用率」 | outcome ok/degraded/failed 按天×模型计数聚合，随所选时间段过滤 | 是（daily_by_model 90 天 prune） |
| 「上游健康」 | 最近一次 probe HEAD 的延迟/成败 + 连续失败计数 | 否（内存态 probe 状态） |
| 错误/重试数字 hover 明细 | 事件流最近 100 条（代理错误+上游5xx+空流重试+探测告警） | 是（stats.events 随 60s 周期落盘） |

## License

[MIT](LICENSE)
