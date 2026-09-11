# PLAN — ctyun-dashboard favicon（branch `fix/ctyun-dashboard-favicon`）

Spec: docs/superpowers/specs/2026-09-05-ctyun-dashboard-favicon-design.md（R31-EXEMPT: feat-not-bugfix）
执行模式：延续用户指令，全程主代理执行。

## Global Constraints
- 只动 `~/.local/bin/ctyun-stream-fix-proxy{,.test}.py`；7920 与既有 admin 端点行为不变；既有 19 测试零改动全绿。

## Task 1 — favicon 路由 + head meta（TDD 先红后绿）
1. 测试先行：`test_favicon_served`（200/image-x-icon/ICO magic/长度）+ dashboard 断言补 `rel="icon"`、`theme-color` → 红。
2. 实现：`import base64` + `_FAVICON_B64`/`FAVICON_ICO`、do_GET 分支、head 三行 meta/link → 全量绿（20 tests）。
3. 部署：归档 cmp、kickstart、curl content_type 验证、浏览器标签确认；ledger 收尾 → ff-merge 回 main → 清 worktree。
