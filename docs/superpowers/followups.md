# Follow-ups（跨 episode 独立问题）

ctyun-stream-fix-proxy.test.py:552（64 处同款 teardown 之一） | 全量套件约 1/6 概率 `proc.wait(timeout=5)` 抛 `subprocess.TimeoutExpired`（proxy SIGTERM handler flush stats 慢），复跑即绿，非 listen-host-lan 引入 | teardown 收敛为公共 helper + wait 超时后 kill 再 wait，或延长 timeout；单独 episode 处理
