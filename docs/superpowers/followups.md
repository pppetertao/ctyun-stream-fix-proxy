# Follow-ups（跨 episode 独立问题）
ctyun-stream-fix-proxy.test.py:7210-7214/:7288-7292 deadline-poll 模式 | 全文件 10+ 处同模式复制（F1 新增 2 处 stderr 轮询）；提取 wait_stderr helper 收益低且失败语义分歧（:699/:4120 while/else+self.fail vs F1 兜底 assert），F1 spec Exclusions 已记录不提取决策 | 若未来再增同类轮询再评估统一 helper（注意统一失败语义）

# 已收敛（2026-10-10 followups 清零批）：
# - queue_timeout/usage_settle 两个 TpmRateLimitTest flaky → F1 有界轮询+墙钟放宽修复（c21b932）
# - calibrate_engine 401/403 误判 TPM 拒绝 → F2 credential_error 分类修复（89cf8bf）
# - tpm_probe_charges_budget 死声明、误导用例改名、do_GET 门禁 9 处复制 → F4 维护三件套修复
# - save_stats_counters 快照-写入乱序窗口 → F3 _PERSIST_LOCK 外扩修复
# - 墙钟放宽注释依据错误 → F5 注释订正（4d0c2d8）
