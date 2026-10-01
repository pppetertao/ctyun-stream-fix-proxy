# PLAN — TPM followups 消化 + rejected 空桶展示盲区（主代理直做）

- 侦察结论：followups 三条 TPM 条目（硬拒设计/rejected 计数/per-model 预算）已被 residuals episode（9c4b29e/b1de8b3）全部实现并有测试锁死——本 episode 消化三条目 + 修最后一个展示盲区。
- 盲区：`tpm_snapshot` 空桶过滤把 `rejected/timeouts >0` 但窗口已滚空的桶藏掉（观测数据丢失）。
- 修法：过滤条件追加"且无 rejected 且无 timeouts"；TDD 单测锚定。
- 验收：单测红→绿；全量 230+1 绿；followups TPM 三条删除；生产部署后 `/api/tpm_stats` 含 config.limit_by_model。
