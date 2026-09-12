# ctyun-stream-fix-proxy

本地 SSE「剥行」反向代理：在 record 级剥除上游 SSE 流中非法终止的毒 record，解决客户端 SSE 解析器校验崩溃问题。纯 Python 标准库实现，零第三方依赖。

## 背景

某 OpenAI 兼容网关（天翼云 TY，`eaichat.ctyun.cn`）的 `deepseek-v4-pro-0813-oc` SSE 流会**间歇性**在 `[DONE]` 之前多发非法 `data:null` record（毒 record = `data:null\n` 行 + 一个终结空行）。OpenAI-compatible 客户端的流式解析器遇到该 record 会直接校验失败，导致整次请求失败。

剥行必须在 **record 级**进行：只删除 `data:null` 行会残留终结空行，输出多一个 `\n`——毒 record 必须连同其终结空行整弃。

## 功能

- **record 级剥行**：按空行切分 SSE record，毒 record 整弃，其余逐 record flush 转发；SSE 下游 close-delimited，非 SSE 请求原样透传
- **双端口**：转发代理 `127.0.0.1:7920` + 同进程内嵌监控台 `0.0.0.0:7921`
- **监控 dashboard**（浏览器运行台，深石板蓝+琥珀，零外部依赖，2s 轮询 `/api/stats`）：
  - 实时统计：按所选时间段（近3天/近7天/本月/上月）聚合的请求数 / 剥行 / 毒行率 / 错误数；活跃连接恒实时
  - 「最近请求」最近 100 条（含模型列、耗时、剥行数；跨天时自动按日期分组显示）
  - 「剥行流带」最近 20 条毒 record 预览 + 累计 sparkline
  - 「按天统计」「按天 × 模型」随所选时间段过滤日期（上月最多 31 行；daily 分桶，双口径错误列）
- **网页热切上游**：`POST /api/config` 免重启切换上游 base URL（本机免鉴权；局域网访问需 `X-Admin-Token`）
- **计数口径透明**：顶部「请求数」跨重启持久化；「按天统计」按本地日期分桶、重启续算
- **launchd 常驻**：KeepAlive 自愈

## 架构

- `ThreadingHTTPServer` + `http.client` 阻塞反代，纯 stdlib
- SSE 中继逐 record flush；relay 期间客户端 socket 设发送超时（默认 60s），避免客户端断开后写阻塞钉死线程
- 统计每 60s 脏刷 + SIGTERM/SIGINT 落盘（原子写），daily 桶 90 天 prune
- 错误双口径：`errors_proxy`（代理合成错误）与 `errors_upstream`（上游 ≥500 透传）互斥统计

## 部署

1. 将 `ctyun-stream-fix-proxy.py` 放到部署路径（plist 模板默认 `/usr/local/bin/`），`chmod +x`。
2. 按需调整 `com.ctyun-stream-fix-proxy.plist` 中的路径（脚本位置与日志位置），放入 `~/Library/LaunchAgents/`。
3. 加载：

```sh
launchctl load ~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist
# 或加载后重启生效：
launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy
```

4. 浏览器打开 `http://127.0.0.1:7921/` 查看监控台。

### 环境变量（seam）

| 变量 | 默认 | 说明 |
|------|------|------|
| `CTYUN_ADMIN_HOST` | `0.0.0.0` | 监控台监听地址 |
| `CTYUN_ADMIN_PORT` | `7921` | 监控台端口 |
| `CTYUN_ADMIN_TOKEN` | 空 | 局域网访问 `POST /api/config` 所需 token（未设则 LAN 只读） |
| `CTYUN_PERSIST_PATH` | `~/.local/etc/ctyun-stream-fix-proxy.json` | 统计持久化文件路径（原子写） |
| `CTYUN_SEND_TIMEOUT` | `60` | relay 期间客户端 socket 发送超时（秒） |

配置优先级：env > 持久化文件 > 内置默认。

## API

- `GET /api/stats` — 全量统计 JSON（计数 / daily 分桶 / daily_by_model / recent 100 / 毒行流带 / range_stats 四维度聚合 / range_bounds 四维度窗口闭区间；后两键为加性新增，旧消费者零破坏）
- `GET /api/config` — 当前上游 base URL
- `POST /api/config` `{"upstream_base": "..."}` — 热切上游（本机免鉴权，LAN 需 `X-Admin-Token`）

## 测试

```sh
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

54 个用例。**必须用 `/usr/bin/python3`**：Homebrew 的 Python 3.14 `http.server.HTTPServer` 构造会挂死（进程存活但不 LISTEN、零报错）。

## 计数口径

| 页面区块 | 口径 | 持久化 |
|----------|------|--------|
| 顶部「请求数 / 剥行 / 错误」 | 按所选时间段聚合（近3/近7天含今日滑动窗口；本月/上月自然月；错误=代理错误+上游5xx 合计） | 是（daily 桶跨重启） |
| 「活跃连接」 | 实时 gauge，不随时间段变化 | 否 |
| 「按天统计」「按天 × 模型」 | 随所选时间段过滤（最远回溯=上月+当月 ≤62 天 < 90 天 retention） | 主表/副表均是（90 天 prune） |
| 「最近请求」「剥行流带」 | 最近 100 / 20 条内存窗口 | 否 |
| 错误/重试数字 hover 明细 | 事件流最近 100 条（代理错误+上游5xx+空流重试） | 是（stats.events 随 60s 周期落盘） |

## License

[MIT](LICENSE)
