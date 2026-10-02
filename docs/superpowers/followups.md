# Follow-ups（跨 episode 独立问题）
ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_queue_timeout_returns_429 | 偶发 flaky（全量 6 跑 1 红：queue-timeout seam ~2s 在满载下竞态；隔离 8/8 绿；TPM 限流既有测试，与本 episode 无关） | 建议单独 episode：放宽 seam 或改轮询断言
ctyun-stream-fix-proxy.py calibrate_engine 判定 | 401/403 凭证失败会被误判为 TPM 阈值拒绝并持久化（status>=400 统一判拒绝系 spec R5 口径） | 后续 episode 区分 429 与其他 4xx
ctyun-stream-fix-proxy.py | spec Files 的 tpm_probe_charges_budget 纯文档函数未实现（零影响，声明性条目） | 确认弃用或补一个断言用函数
ctyun-stream-fix-proxy.test.py:43 | SSE_USAGE 的 prompt_tokens 与 completion_tokens 同为 1，GenSpeedRecentEntryTest 的 ==1 断言无法区分 p3_tokens 索引互换（[0]/[1]） | 把 fake upstream 的 completion_tokens 改为异于 prompt 的值（如 3），增强 recent 条目取值判别力
