# Follow-ups（跨 episode 独立问题）

- `ctyun-stream-fix-proxy.py` `save_stats_counters`（仅 prune `daily`，`daily_by_model` 不 prune，+1 天 key/天，重启清） | `daily_by_model` 内存随运行天数线性增长（键为日期串，值矩阵受 `BY_MODEL_CAP=32` 约束），长期运行进程内存缓慢膨胀 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测
- `ctyun-stream-fix-proxy.py` `save_stats_counters`（`_prune_daily(daily)` 仅 prune 锁内浅拷贝副本，内存 `STATS["daily"]` 不受保护） | `STATS["daily"]` 内存态随运行天数线性增长——磁盘副本受 90 天滚动约束但内存无界（与已修复的 daily_by_model 同增长率泄漏） | 建议另开 episode：`daily` 改为锁内对 `STATS["daily"]` 原地 prune（同 daily_by_model 模式），`test_daily_prune_on_save` 断言对象迁到内存态并复核
