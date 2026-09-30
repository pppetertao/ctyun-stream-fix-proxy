# Follow-ups（跨 episode 独立问题）

ctyun-stream-fix-proxy.test.py:552（64 处同款 teardown 之一） | 全量套件约 1/6 概率 `proc.wait(timeout=5)` 抛 `subprocess.TimeoutExpired`（proxy SIGTERM handler flush stats 慢），复跑即绿，非 listen-host-lan 引入 | teardown 收敛为公共 helper + wait 超时后 kill 再 wait，或延长 timeout；单独 episode 处理

ctyun-stream-fix-proxy.test.py:3630（RequestIdTest.test_502_response_carries_x_request_id，observability-v2 P1 引入） | 测试内重赋 `self.proc` 重启 proxy 后，首实例 `persist_dir` tempdir 无人清理，每次跑泄漏一个 tempdir（纯测试卫生，无正确性影响） | 重启前先 rmtree 首实例 persist_dir，或 teardown helper 支持多实例清理；随 teardown 收敛 episode 一并处理
