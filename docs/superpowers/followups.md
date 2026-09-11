# Follow-ups（跨 episode 独立问题）

- `ctyun-stream-fix-proxy.py` `save_stats_counters`（仅 prune `daily`，`daily_by_model` 不 prune，+1 天 key/天，重启清） | `daily_by_model` 内存随运行天数线性增长（键为日期串，值矩阵受 `BY_MODEL_CAP=32` 约束），长期运行进程内存缓慢膨胀 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测
