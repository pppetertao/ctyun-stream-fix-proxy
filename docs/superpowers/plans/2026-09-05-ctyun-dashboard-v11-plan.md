# PLAN — ctyun-dashboard v1.1（branch `fix/ctyun-dashboard-v11`）

Spec: `docs/superpowers/specs/2026-09-05-ctyun-dashboard-v11-design.md`（R31 validator exit 0）
执行模式：本 episode 延续用户指令——全程主代理执行，不派 subagent。

## Global Constraints

- 解释器锁定 `/usr/bin/python3`（3.9.6）；纯 stdlib；目标文件仅 `~/.local/bin/ctyun-stream-fix-proxy{,.test}.py`。
- 7920 剥行语义、admin 权限矩阵、既有 12 测试零改动全绿；集成测试用 `CTYUN_PERSIST_PATH` 等 seam 隔离真实文件。
- 每卡完成跑全量测试贴 stdout/exit code 才进下一卡。

## Task 1 — 三项加固 + dashboard 展示（TDD 先红后绿）

1. 测试先行：新增单元（`extract_model` 正反例 / `save_stats_counters`+`load_stats_counters` 往返与容错 / `by_model` 累计与 32 上限）+ 集成（预写计数文件→起代理续算 / `recent[*].model` 与 `by_model` 条目 / SIGTERM 落盘 / big-SSE 客户端中断→`result=aborted` 无 Traceback 且 errors_total=0 / dashboard 断言扩展）→ 运行预期红。
2. 实现：`_proxy` 加 `except ConnectionError` → 499/aborted；`extract_model` + `_record_request(model)` + `by_model`（上限 32）；`persist_upstream`→`_write_persist(path, base, counters)` 统一原子写 + `save_stats_counters`/`load_stats_counters`；`main()` 载入计数 + SIGTERM/SIGINT handler + 60s 脏刷 daemon 线程；dashboard sparkline 红柱叠加 + 按模型卡 + 模型列 + footer 文案。→ 全量绿。
3. 验证：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` exit 0 + `py_compile` exit 0。

## Task 2 — 部署 + 实机验证

1. 归档同步 + `cmp` byte-identical；`launchctl kickstart -k` 重启。
2. 实机：`/api/stats` 含 `by_model`/`model`；kill -TERM 后 persist 文件计数非零、重启后延续；7920 透传回归；日志无新增 Traceback。
3. ledger 收尾 → ff-merge 回 main → 清 worktree。
