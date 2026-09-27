# Follow-ups（跨 episode 独立问题）

- `README.md:31-41`（部署段写 `/usr/local/bin/`） | 实际 plist（com.ctyun-stream-fix-proxy.plist）与部署副本在 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`，README 部署路径与事实不符（2026-09-18 部署验收时实测发现） | 另开小 episode：README 部署段改为 `~/.local/bin/` 实际路径 + 写明 deploy 步骤（cp + `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`）
- `ctyun-stream-fix-proxy.py:865`（header-stall 可重试 except 元组） | `(socket.timeout, RemoteDisconnected)` 中 socket.timeout 子分支零测试覆盖——现有 stall 夹具走关连接路径只产出 RemoteDisconnected（review r1 建议可选项） | 另开小 episode：加真·持连 stall 夹具（handler 读 body 后 sleep 到超时）覆盖 socket.timeout 分支 + 单测
