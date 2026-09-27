# followups 积压清偿（prune 验证 / README 部署段 / socket.timeout 覆盖）设计

## Goal

清偿 `docs/superpowers/followups.md` 剩余 3 条：条目 1 验证已落地并销账（零新代码），条目 2 修 README 部署段失配，条目 3 补 socket.timeout 重试分支的测试夹具与覆盖。

## 关键结论（Read 后定死）

**条目 1 是 stale followup。** `ctyun-stream-fix-proxy.py:473` 已有 `_prune_daily(STATS["daily_by_model"])`（与 :472 daily 同路径、同 `DAILY_RETENTION_DAYS=90`，py:47），单测 `test_daily_by_model_prune_on_save` / `test_daily_by_model_prune_exact_boundary` / `test_daily_by_model_prune_empty`（test.py:1158/1196/1237）已覆盖内存态+文件态+边界+空桶。系 2026-09-11-daily-by-model-prune episode 落地后 followup 未删。本批只做验证 + 销账，**不加用例、不改生产代码**。

**条目 3 属覆盖型用例（非回归修复）**：socket.timeout 本就在可重试元组（py:866），行为正确、直接绿；无法造红，不违反 TDD 红灯要求（测试对象已存在，补的是覆盖）。双超时 502 变体不做——py:885-896 的 502 合成对元组内三异常类型无分支差异，类型差异只影响 retry_reason 标签（py:877-880），已被用例①断言；`test_header_stall_both_timeout_returns_502` 已覆盖该机制。

## Files to Change

1. `docs/superpowers/followups.md:3` — 卡 1 删条目 1 行；`:4` 卡 2 删；`:5` 卡 3 删。删完只剩标题行。
2. `README.md:32-44`（部署段）— old→new 定死如下：
   - OLD：`1. 将 ctyun-stream-fix-proxy.py 放到部署路径（plist 模板默认 /usr/local/bin/），chmod +x。` + `2. 按需调整 plist…放入 ~/Library/LaunchAgents/` + `3. 加载：launchctl load … / kickstart -k` 块 + `4. 浏览器打开…`
   - NEW：
     ```
     本机实际部署（2026-09-18 验收口径）：脚本 `~/.local/bin/ctyun-stream-fix-proxy.py`，plist `~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist`（仓库内 plist 为模板，路径按目标机调整；日志路径见本机 plist 的 StandardOutPath/StandardErrorPath）。

     日常 deploy（改代码后生效）：

     ```sh
     cp ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py
     launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy
     ```

     首次安装：1. `mkdir -p ~/.local/bin` + cp + `chmod +x`；2. 按目标机调整仓库 plist 模板（脚本/日志路径）拷入 `~/Library/LaunchAgents/`；3. `launchctl load ~/Library/LaunchAgents/com.ctyun-stream-fix-proxy.plist`；4. 浏览器打开 `http://127.0.0.1:7921/`。
     ```
   - 日志实际路径无证据，不写死（铁律：不编造）。
3. `README.md:82` — `115 个用例` → `117 个用例`（+2，卡 3 同步）。
4. `ctyun-stream-fix-proxy.test.py:59-60`（`stall_calls` 类属性后）— 加三行类属性：
   ```python
   sleep_stall_all = False    # True → 每呼读 body 后 sleep 持连不写响应（代理 getresponse 抛 socket.timeout）
   sleep_stall_calls = ()     # 1-based 呼叫序号元组：命中则 sleep 持连不写响应（同 sleep_stall_all 形态）
   sleep_stall_seconds = 3.0  # 须 > 用例 HEADER_TIMEOUT（白盒 0.5s/黑盒 1s），余量 ≥2s 防 flaky
   ```
5. `ctyun-stream-fix-proxy.test.py:85`（`# --- end stall ---` 前）— 加 sleep-stall 分支：
   ```python
   # --- sleep-stall 路径：读 body 后持连 sleep 不写响应（连接保持，代理 getresponse 阻塞到
   # HEADER_TIMEOUT_S 抛 socket.timeout；区别于 stall 的关连接路径——后者产 RemoteDisconnected）---
   if self.sleep_stall_all or (self.sleep_stall_calls and self.calls is not None
                               and len(self.calls) in self.sleep_stall_calls):
       time.sleep(self.sleep_stall_seconds)
       self.close_connection = True
       return
   # --- end sleep-stall ---
   ```
6. `ctyun-stream-fix-proxy.test.py:198-233 make_fake_upstream()` — 签名加 `sleep_stall_all: bool = False, sleep_stall_calls: tuple = (), sleep_stall_seconds: float = 3.0`；attrs dict（:207-217）透传三键；**:223 `use_threading` 判定必须改为 `stall_all or stall_calls or rst_all or rst_calls or sleep_stall_all or sleep_stall_calls`**（漏改 = 单线程 HTTPServer 卡死 sleep 连接，测试挂死；daemon_threads=True 由 :231 兜底 teardown）。
7. `ctyun-stream-fix-proxy.test.py:254`（`make_stall_upstream` 后）— 新 helper `make_sleep_stall_upstream(sleep_stall_all=False, sleep_stall_calls=(), **kwargs) -> tuple`：体同 `make_stall_upstream`（:247-253），`make_fake_upstream(False, scripted=True, sleep_stall_all=…, sleep_stall_calls=…, **kwargs)`，返回 `(port, FAKE_SERVERS[-1].RequestHandlerClass.calls)`。
8. `ctyun-stream-fix-proxy.test.py:1789`（`test_open_upstream_stall_raises_remote_disconnected` 后，同 class）— 白盒用例 `test_open_upstream_sleep_stall_raises_socket_timeout`：逐行复刻 :1762-1788 结构，仅 `make_fake_upstream(False, sleep_stall_all=True)` + `with self.assertRaises(socket.timeout):`；docstring 写明"sleep-stall 持连静默 → socket.timeout，与 stall 夹具的 RemoteDisconnected 互补，锁可重试元组最后零覆盖子分支"。
9. `ctyun-stream-fix-proxy.test.py:2901`（`test_header_stall_retry_success` 后，同 class）— 黑盒用例 `test_header_sleep_stall_retry_success`：逐行复刻 :2873-2900 结构，仅上游换 `make_sleep_stall_upstream(sleep_stall_calls=(1,))`；断言 `len(calls)==2` + `data == SSE_A + SSE_B + SSE_DONE` + stderr 含 `retried=1` 与 `retry_reason=header-timeout` + `header_retries_total >= 1`。

生产代码 `ctyun-stream-fix-proxy.py`：**零改动**。

## Acceptance Criteria

- 卡 1（executor，验证型）：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py StatsUnitTestCase` 中 3 个 `daily_by_model_prune*` 用例绿（贴实际 stdout/exit code）；followups.md 条目 1 删除。
- 卡 2（executor，免 TDD）：README 部署段含 `~/.local/bin/ctyun-stream-fix-proxy.py` 与 `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy`；不再以 `/usr/local/bin/` 为部署路径默认表述。
- 卡 3（executor）：2 个新用例焦点跑绿（贴 stdout）；全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 117 用例 OK；README:82 写 117。
- followups.md 终态只剩标题行。

## Risks

- **use_threading 漏改**（锚点 6）：单线程 HTTPServer 在 sleep 连接上卡死，测试挂死无报错——PLAN-B 必须将此改动点写进卡内代码块。
- sleep 3s handler 线程 teardown 时仍存活：test.py:231 `daemon_threads=True` + `stop_fake_upstreams()` 的 shutdown 不阻塞，已兜底。
- 黑盒用例 `retry_reason=header-timeout` 与 RemoteDisconnected 路径标签相同（py:877-880），不能单独证明 socket.timeout 命中——分支类型证据由白盒用例 `assertRaises(socket.timeout)` 承担，两用例互锁。
- 时长：白盒 ~0.6s（0.5s 超时即抛）、黑盒 ~1.5s，全量增量 ~2.5s，无超时风险。
- 白盒 patch `mod.HEADER_TIMEOUT_S` 用共享 mod：同 class 已有 3 个同模式白盒用例（:1726/1762/1790），串行执行无并发。

## Exclusions

- `ctyun-stream-fix-proxy.py` 生产代码任何改动（条目 1 已落地、条目 3 行为已正确）。
- `daily_by_model` 持久化文件格式变更。
- 部署自动化脚本（deploy/、install.sh 等）；plist 模板 `com.ctyun-stream-fix-proxy.plist` 路径同步（仓库模板与本机实例分离属现状设计）。
- README:29「daily 桶 90 天 prune」措辞、其他 docstring/行号失配。
- 双超时 502 的 sleep-stall 变体用例（决策见「关键结论」）。

## R31 Evidence

[R31-S1] 问题现场确认（三条 followup 的现状证据）：

```
$ grep -n "_prune_daily\|DAILY_RETENTION_DAYS" ctyun-stream-fix-proxy.py
47:DAILY_RETENTION_DAYS = 90  # daily 分桶滚动保留天数（save 时 prune）
441:def _prune_daily(daily: dict) -> dict:
472:        _prune_daily(STATS["daily"])           # 内存态原地 prune
473:        _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune
```

```
$ grep -n 'usr/local/bin' README.md
34:1. 将 `ctyun-stream-fix-proxy.py` 放到部署路径（plist 模板默认 `/usr/local/bin/`），`chmod +x`。
```

[R31-S2] 根因陈述（带证据）：followups 积压的根因是「实现 episode 落地时未回销对应 backlog 条目」。条目 1 的 prune 由 2026-09-11 daily-by-model-prune episode 落地——该 episode 的 spec 仍在库（本 architect Glob 实测命中），且 S1 证据块显示 py:473 实现与 test.py:1158/1196/1237 三单测均已合入 main，但 followups.md:3 条目未删 → stale：

```
$ ls docs/superpowers/specs/ | grep prune
2026-09-11-daily-by-model-prune-design.md
2026-09-11-daily-memory-prune-design.md
```

条目 2 为文档失配（README 写仓库 plist 模板默认路径，2026-09-18 实际部署用 `~/.local/bin/`），条目 3 为覆盖缺口（现有 stall 夹具走 `close_connection=True` 关连接路径只产 RemoteDisconnected，test.py:75-85；socket.timeout 需持连静默夹具，此前不存在，test.py:1762-1767 注释自证）——两者非 stale，按本 spec 卡 2/卡 3 修复。
