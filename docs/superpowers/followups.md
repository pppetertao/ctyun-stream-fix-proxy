# Follow-ups（跨 episode 独立问题）

- `ctyun-stream-fix-proxy.py:865`（header-stall 可重试 except 元组） | `(socket.timeout, RemoteDisconnected)` 中 socket.timeout 子分支零测试覆盖——现有 stall 夹具走关连接路径只产出 RemoteDisconnected（review r1 建议可选项） | 另开小 episode：加真·持连 stall 夹具（handler 读 body 后 sleep 到超时）覆盖 socket.timeout 分支 + 单测
