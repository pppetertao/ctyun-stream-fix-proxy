# PLAN — ctyun-proxy-dashboard（branch `fix/ctyun-proxy-dashboard`）

Spec: `docs/superpowers/specs/2026-09-05-ctyun-proxy-dashboard-design.md`（R31 validator exit 0）
执行模式：**本 episode 用户显式指令——全程主代理执行，不派 subagent**（provider 故障 + 用户裁定）。TDD 与验收门槛不变。

## Global Constraints

- 解释器锁定 `/usr/bin/python3`（3.9.6）：禁 3.10+ 语法（match / `X | Y` 类型注解）；纯 stdlib，零新依赖。
- 目标文件仅两个：`/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`、`.../ctyun-stream-fix-proxy.test.py`（部署产物不进 git；归档 `~/Documents/project/ctyun-stream-fix-proxy/` 收尾时 cmp 同步）。
- 7920 代理行为逐字节不变：既有 `PoisonStreamTest`/`PassThroughTest` 零改动全绿。
- 集成测试须用 `CTYUN_PERSIST_PATH`/`CTYUN_ADMIN_PORT`/`CTYUN_ADMIN_HOST` seam 隔离真实持久化文件与真实端口。
- 每卡完成即跑该卡验证命令并贴 stdout/exit code，才进下一卡。

## Task 1 — 统计 + admin 服务 + 上游热切换（代码 + 测试，TDD 先红后绿）

1. 先改测试文件：新增 `load_proxy_module()`、单元测试类（`valid_upstream_url` 正反例 / `resolve_upstream_base` 四分支 / `persist_upstream` 往返 / `write_allowed` 六例矩阵 / `stats_snapshot` 形状），`start_proxy` 加 admin + persist seam env，新增集成测试类（dashboard 200/html、`/api/stats` 键齐与计数、`/api/config` GET source=env、POST 换上游 200+路由切换 tag 断言+持久化落盘、400×2、token 本机放行）。运行：**预期红**（AttributeError/连接拒绝）。
2. 实现：常量区扩展、双锁 + deque 状态、五个纯函数、`_proxy` 拆 instrumented 外壳 + `_relay_sse` 毒 record 预览埋点、`AdminHandler`、`main()` resolve + admin 启动。运行：**绿**。
3. 验证命令：`/usr/bin/python3 /Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`（全量，exit 0）。

## Task 2 — Dashboard HTML（代码 + 测试，TDD 先红后绿）

1. 先加测试：`GET /` → 200 + `text/html; charset=utf-8` + `<!doctype html` 开头 + 含 `/api/stats`、`poison`、`保存上游端点`、`textContent` 安全模式标记（无 innerHTML 注入动态数据）。
2. 实现 `_DASHBOARD_SRC`（普通 str 三引号，非 f-string）+ `DASHBOARD_HTML = .encode("utf-8")`；按 spec 设计段实现（深石板蓝/琥珀/mono、大数字卡、SVG 节奏图、剥行流带签名元素、最近请求表、2s 轮询、响应式/focus-visible/reduced-motion）。
3. 验证命令：同 Task 1 全量测试 + `curl -s http://127.0.0.1:7921/ | head`（部署卡前先由测试保障）。

## Task 3 — 部署 + 实机验证（C 档：真机/运行时）

1. `cp` 部署文件到 `~/Documents/project/ctyun-stream-fix-proxy/` 归档，`cmp` 双文件 byte-identical。
2. `launchctl kickstart -k gui/$UID/com.zcode.ctyun-stream-fix-proxy` 重启。
3. 验收 spec Acceptance ③–⑧：lsof 双端口、`/api/stats` JSON、`/` HTML、POST config 往返 + 持久化 cat + 换回、LAN IP curl、7920 回归 GET /plain。
4. 全部贴实际 stdout/exit code；任何一步失败先诊断根因再改，不 blind-retry。

## 收尾

- ledger 逐卡更新 `.superpowers/sdd/progress.md`；自审 diff 对照 spec（主代理兼 REVIEW，声明于最终汇报）；worktree ff-merge 回 main 后 remove。
