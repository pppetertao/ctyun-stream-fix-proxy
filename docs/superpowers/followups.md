# Follow-ups（跨 episode 独立问题）

- `ctyun-stream-fix-proxy.py` `save_stats_counters`（仅 prune `daily`，`daily_by_model` 不 prune，+1 天 key/天，重启清） | `daily_by_model` 内存随运行天数线性增长（键为日期串，值矩阵受 `BY_MODEL_CAP=32` 约束），长期运行进程内存缓慢膨胀 | 建议另开 episode：给 `daily_by_model` 加同 `DAILY_RETENTION_DAYS=90` 的 prune（与 `_prune_daily` 同路径）+ 单测
- `ctyun-stream-fix-proxy.py:38`（上游超时固定 600s） | 上游接受连接后若长期不返回响应头，请求会挂满 600s 才合成 502（借鉴 9router executor 的 FETCH_CONNECT_TIMEOUT_MS 模式：头部停滞应提前转为可重试错误） | 另开 episode：拆分 connect/headers 短超时（如 60s）与 body 长超时，头部超时按可重试口径走空流重试同类路径 + 单测
- `README.md:31-41`（部署段写 `/usr/local/bin/`） | 实际 plist（com.ctyun-stream-fix-proxy.plist）与部署副本在 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`，README 部署路径与事实不符（2026-09-18 部署验收时实测发现） | 另开小 episode：README 部署段改为 `~/.local/bin/` 实际路径 + 写明 deploy 步骤（cp + `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`）
