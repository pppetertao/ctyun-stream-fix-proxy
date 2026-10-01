# PLAN — Episode B 测试 teardown 收敛（主代理直做模式）

- **spec**：`docs/superpowers/specs/2026-10-01-test-teardown-convergence-design.md`（唯一权威源，helper 代码/分型策略/锚点全部以 spec 为准）
- **分支**：fix/test-teardown-convergence @ main@483253a；唯一改动文件 `ctyun-stream-fix-proxy.test.py`
- **模式**：用户 2026-10-01 指令"本会话全部任务主代理执行"——无 subagent，主代理施工 + self-review
- **baseline**：main@483253a = 226 全绿（Episode A 后）

## 执行步骤（串行，每步验证后推进）

1. **批 1（TDD）**：`StopProxyKillPathTest` 单测先行（红：stop_proxy 不存在）→ 落 `stop_proxy` helper（spec 1.1 原文，:408 kill_registered 之后）→ 单测绿 → commit。
2. **批 2（型 B 先行防误替换）**：grep `settings.json` 找全部"wait 后读旧 persist_dir"块 → 按型 B 改造（terminate+wait(timeout=10) 手工保留 + 读后 rmtree）→ 逐类跑绿 → commit。
3. **批 3（标准款批量）**：四行标准模式（8 空格缩进）replace_all → `stop_proxy(self.proc)`（覆盖 8 处 tearDown + 型 A 内联同款）；12 空格缩进变体单独 replace_all；卫款 4 处逐处替换 → 逐类跑绿 → commit。
4. **批 4（泄漏 + 收敛）**：3 处重赋泄漏补 rmtree（spec 1.4）；`_teardown_proc` 委托 stop_proxy（spec 1.5）→ 全量绿 → commit。
5. **批 5（验收）**：连续 3 次全量绿贴证据；grep 验收（tearDown 无裸 wait(timeout=5)）；followups.md:3/:5 消化。

## Global Constraints

- helper 代码逐字按 spec 1.1（含注释与空 catch 自证）。
- 型 B 块禁止被 helper 替换（rmtree 会破坏读旧 dir）——批 2 必须先于批 3。
- 每批 commit 独立、可回退；任何一类跑绿失败 → 停下诊断，不盲改。
