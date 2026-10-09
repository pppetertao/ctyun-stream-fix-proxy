# OCR Findings 修复拆分方案（2026-10-09）

来源：ocr scan 7 条 finding，逐条 tracer 核实（4真2半真1真无害），verdicts 见
`~/.zcode/cli/memories/projects/ctyun-stream-fix-proxy-d59fe894dee38d47/memory/ocr-scan-2026-10-09-verdicts.md`。
所有行号已逐条在 `ctyun-stream-fix-proxy.py` 复核，与 verdicts 一致。

**分组验证结论：原分组合理，仅一处调整——G 移出 E4 进「不修的项」（理由见文末）。**

---

## E1：持久化单写者 + 快照冻结（finding A + B）

- **Goal**：`save_stats_counters` 成为全量 schema 唯一写者（唯一 tmp + 独立持久化锁 + set_* 不再写窄 schema），`stats_snapshot` 的 5 个直方图键在锁内写回冻结副本。
- **Files**：`ctyun-stream-fix-proxy.py` — `save_stats_counters`(:1733)、`persist_upstream`(:1524，删除)、`set_upstream_base`(:2331)/`set_capture_errors`(:2343)/`set_tpm_model_budgets`(:2353)、`stats_snapshot`(:2629)、模块级新 `_PERSIST_LOCK`(:1442 旁)；`ctyun-stream-fix-proxy.test.py` — `test_persist_upstream_carries_tpm_model_budgets`(:1633，重写) + 新增并发/窄覆盖/冻结回归测试。
- **Acceptance**：`python ctyun-stream-fix-proxy.test.py` 全绿，且新增测试证明：三路并发（flush 替身/set_* /SIGTERM 替身）150 轮写后文件仍为合法全量 JSON 无 .tmp 残留、set_* 后 stats 计数不被窄 schema 覆盖、snap 的 5 键为冻结副本（改 snap 不动 STATS）。

详细 spec：`docs/superpowers/specs/2026-10-09-persist-race-snapshot-design.md`

---

## E2：TPM 按桶 FIFO 排队（finding C）

- **Goal**：TPM 排队按 (key_id, model) 分桶 FIFO，各桶队首各自准入，消除单一全局队首阻塞导致后方有预算桶被拖到 120s 超时 429。
- **Files**：`ctyun-stream-fix-proxy.py` — `TPM_WAITERS`(:477，全局 deque 改分桶结构或包一层)、`_tpm_admit`(:594-651，队首判定 :637-638 改"本桶队首"、入队/队满 :622-628、超大请求豁免 :610-611)、`tpm_settle`(:654-685，notify_all 语义)、`tpm_snapshot`(:688-729，queued/queue_total 计数按桶维护)；测试 — `TpmRateLimitTest`(:6558)/`TpmAdmitHookTest`(:6519) 扩展。
- **Acceptance**：`python ctyun-stream-fix-proxy.test.py` 全绿，且新增"两桶场景：队首桶持续超预算时，后方有预算桶的请求在窗口刷新后即准入（不被拖至 TPM_QUEUE_TIMEOUT_S）"的测试。

---

## E3：管理面 GET 门禁（finding E）

- **Goal**：非 loopback 来源的管理面 GET 套 token 门禁 + 启动日志显式警告暴露面，不断掉生产局域网 dashboard（plist 未设 CTYUN_ADMIN_HOST，默认 0.0.0.0）。
- **Files**：`ctyun-stream-fix-proxy.py` — `AdminHandler.do_GET`(:3335-3425，新增非 loopback 门禁判定，/api/errors 列表 :3386 同步纳入)、`ADMIN_HOST`(:37，默认值调整作为可选项论证，不作为必改)、启动日志 :5617（新增暴露面警告行）；测试 — `AdminIntegrationTest`(:3490) 扩展非 loopback 403 / loopback 直通 / token 直通矩阵。
- **Acceptance**：`python ctyun-stream-fix-proxy.test.py` 全绿；非 loopback 无 token GET /api/stats 等 → 403，127.0.0.1 → 200，非 loopback + 正确 token → 200；启动 stderr 含暴露面警告。

**约束**（已写入方向）：不得静默把默认绑定改 127.0.0.1——生产 plist 只设 CTYUN_LISTEN_HOST，用户从局域网看 dashboard，改默认即断远程看板。默认值调整只能作为"显式 opt-in + 日志"的可选附加项论证，不设为本 episode 验收。

---

## E4：SSE 热路径单次解析 + 死分支清理（finding D + F）

- **Goal**：`_relay_sse` 记录循环内每 data 行只 json.loads 一次（kind/usage/body-error 复用同一解析结果），删除 `_calibrate_probe` 恒假守卫。
- **Files**：`ctyun-stream-fix-proxy.py` — `_relay_sse`(:3122，循环 :3157-3233 内 :3161/:3166/:3170 三处 parse 与 :3184 priming 处合并为单次解析三元组)、`sse_data_line_kind`(:252)/`sse_line_usage`(:316)/`sse_line_body_error`(:398)（加"接受已解析 dict"的复用变体或改签名，保持原签名测试兼容）、`_proxy_relay`(:2797-2799，删 2 行 getattr 守卫；`_calibrate_send` 直连路径 :1208/:1374 不受影响)；测试 — `RelaySseV3Test`(:5919) 扩展 + 删除 F 相关守卫测试（若有）。
- **Acceptance**：`python ctyun-stream-fix-proxy.test.py` 全绿；新增"单 record 多行统计断言：mock json.loads 计数 == pending 行数（常态流每行 1 次，priming 期无额外）"测试；`_proxy_relay` 在未设 `_calibrate_probe` 属性时正常执行（删除守卫不改变任何可达路径，因全仓无赋值点）。

---

## Episode 间交互风险

- **E1 ↔ E3（推荐 E1 先做）**：E3 的鉴权测试会 POST /api/config → 走 `set_upstream_base`/`set_capture_errors`（:3486-3491）→ 触发持久化。E1 先把 set_* 改成"内存写 + 单次全量 save"，E3 测试落地时持久化不变量已稳定；若 E3 先做，其测试短暂依赖窄 schema 两阶段路径，E1 落地后需重跑。无编译级冲突。
- **E1-A 与 E1-B 同 episode 内**：A 改 :1524-1540/:1733-1774/:2331-2360，B 改 :2629-2715——行区间不相交，但都动 stats 持久化/快照语义，同一 episode 一次 task review 审整体一致性最省事。
- **E1 的新 `_PERSIST_LOCK` 与既有锁序**：必须在 STATS_LOCK、_CFG_LOCK 均释放后获取，且不得与 TPM_LOCK→STATS_LOCK（:674-684 注释）链产生新的交叉。E2 若先落地不改锁序，无冲突；任何一方引入"持 STATS_LOCK 再取其他锁"都必须走 PLAN 重新审。
- **E4-D 与 E1-B 同读 STATS 直方图键**：D 改的是热循环消费侧（:3157-3233），B 改的是快照产出侧（:2629-2715），行不相交；但 B 的冻结回归测试会覆盖 ttfb_hist 等键，D 落地后这些测试重跑一遍确认无侧漏。
- **E4 内部（D 与 F 邻近区域）**：F 删 :2797-2799（`_proxy_relay` 首部），D 改 :3122 起的 `_relay_sse`，同 class 不同函数；但两者都在 ProxyHandler 请求链上，E4 的 task review 应把请求链（_proxy_relay → _relay_sse）作为整体审，防 D 的重构影响 F 删除后的调用上下文（当前无影响：`_calibrate_probe` 属性全仓零赋值）。
- **执行顺序建议**：E1（P0）→ E2 → E3 → E4。E4 是热路径行为改动，放最后，且其解析次数断言（mock json.loads 计数）必须在 E1 的 STATS 键集合稳定后写，避免测试对 STATS 内部结构的双重依赖漂移。

---

## 不修的项

- **G（sse_line_extract_usage :384-395）——判断为保留，不开 episode**。理由：生产零调用属实（热路径 :3166 直接用 `sse_line_usage`+`usage_dict_tokens`，docstring :390 已声明 R1 契约"热路径不重复调用本函数"），但它是 `UsageExtractTest`（test.py :5270/:5326，约 14 用例）唯一消费的五元组抽取 test seam，不是粗心死代码。删除的收益为零生产改善，成本是重写整个测试矩阵为等价组合调用——净负。E4-D 的单次解析重构落地后该函数进一步退化为纯测试组合器，届时若主代理判定"测试 seam 也不留"，另开 episode 连测试一起删，不挤进本次 D/F。
