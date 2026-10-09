# Follow-ups（跨 episode 独立问题）
ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_queue_timeout_returns_429 | 偶发 flaky（全量 6 跑 1 红：queue-timeout seam ~2s 在满载下竞态；隔离 8/8 绿；TPM 限流既有测试，与本 episode 无关） | 建议单独 episode：放宽 seam 或改轮询断言
ctyun-stream-fix-proxy.py calibrate_engine 判定 | 401/403 凭证失败会被误判为 TPM 阈值拒绝并持久化（status>=400 统一判拒绝系 spec R5 口径） | 后续 episode 区分 429 与其他 4xx
ctyun-stream-fix-proxy.py | spec Files 的 tpm_probe_charges_budget 纯文档函数未实现（零影响，声明性条目） | 确认弃用或补一个断言用函数
ctyun-stream-fix-proxy.test.py TpmRateLimitTest.test_usage_settle_releases_budget | 偶发 flaky（满载全量下时序竞态：assertLess(elapsed,1.0)+stderr REQ 行计数≥2；隔离复跑绿、baseline 两次全量绿；TPM 限流既有测试，与本 episode 无关） | 建议单独 episode：放宽时序断言或改轮询
ctyun-stream-fix-proxy.py:1747-1748 快照-写入乱序覆盖（E1 OCR #2） | save_stats_counters 快照在 STATS_LOCK/_CFG_LOCK 取、_PERSIST_LOCK 只包写段：线程 A 旧快照可覆盖线程 B 新值（内存新、磁盘旧，重启回退）。既有问题被 E1 收窄而非新引入；spec 锁序纪律（持锁内禁取其它锁）限定本 episode 不改 | 后续 episode 评估锁范围重构（_PERSIST_LOCK 置最外层需连带信号重入防御分析，OCR 核实无死锁风险）
ctyun-stream-fix-proxy.test.py:1634 用例名引用已删函数（E1 OCR #6） | test_persist_upstream_carries_tpm_model_budgets 名中的 persist_upstream 已随 E1 删除，误导维护者 | 顺手改名为 test_set_tpm_model_budgets_persists_full_schema（低价值维护项，不阻塞）
ctyun-stream-fix-proxy.py:3357-3458 do_GET 读门禁 5 行块逐字复制 9 处（E3 OCR #3） | 纯 maintainability：新增 GET 端点时漏挂风险真实 | 独立 episode 提 AdminHandler._read_gate() helper 收敛 9 处（行为等价零风险，但交付前不宜动安全 gate 位）
