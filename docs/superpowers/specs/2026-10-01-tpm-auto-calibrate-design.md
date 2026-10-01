# TPM 限流自动校准（探测实测 + 样本持久化 + 结构化启用建议 + 二选一预算设置）— 设计文档

状态：待实现（本 spec 落盘不 commit）
R31-EXEMPT: feat-not-bugfix
范围：单文件纯 stdlib Python 本地反代（主文件 `ctyun-stream-fix-proxy.py`，测试 `ctyun-stream-fix-proxy.test.py`，测试解释器固定 `/usr/bin/python3`）
决策已与用户对齐：用户选定"主动校准探测 + 被动分析增强"两者都要；已明确接受校准代价（一次校准约消耗相当于上游限额的 token 配额；校准当分钟真实请求可能被上游连带拒绝）。用户补充硬需求：**TPM 预算设置只保留「推荐值」与「自定义值」两个选项，彻底删除多档 choices（推荐×1.5 / 推荐×2 / 默认÷2 / 默认×2）。**

## Goal

为 TPM 限流补齐"自动测试并给出推荐值"闭环，交付三个用户可见能力：

1. **主动校准探测**：Dashboard TPM 设置卡片上每模型一个"校准测试"按钮。对选定模型发受控请求（大 input + `max_tokens` 压到最小）逐级加压，直到上游出现拒绝信号，实测阈值 → 推荐预算 = 实测阈值 × 0.9（与现有九折口径一致）。
2. **被动分析增强**：把现有 body-err 实证推荐做厚——样本持久化（现在只存内存 `LOG_RING`，重启丢失）、结构化"是否建议对该模型启用限流"字段、无样本时按历史用量给保守参考。
3. **二选一预算设置（本次新增硬需求）**：每个模型行的预算设置简化为**两个选项**——「使用推荐值 N」或「自定义值」。彻底删除 `tpm_budget_recommend` 生成的多档 `choices`（有样本三档 `:796-804`、无样本默认三档 `:784-793` 全部移除）；无推荐值时只能自定义。

推荐逻辑综合三源，优先级明确：**探测实测 > body-err 样本 > 历史用量保守参考**。

本次交付边界：
- 探测结果与 body-err 样本都持久化进 `~/.local/etc/ctyun-stream-fix-proxy.json`，跨重启保留。
- `GET /api/tpm_settings` 的每模型 `recommend` 由 `{recommended, samples, hint, choices}` 改为 `{recommended, samples, hint?, enabled_advice, source, probe}`——**去掉 `choices`**，新增 `enabled_advice`（结构化"是否建议启用"）+ `source`（推荐来源）+ `probe`（最近一次探测结果）。
- 新增 `POST /api/tpm_calibrate`（启动校准）/ `GET /api/tpm_calibrate`（轮询进度与结果）/ `POST /api/tpm_calibrate_abort`（中止）三个 admin 端点。
- 校准结果不自动写 `TPM_MODEL_BUDGETS`；Dashboard 展示"一键应用"按二选一模式回填（推荐值或自定义值），仍走现有 `#tpm-save` → `POST /api/config`。

## Files to Change

所有生产代码进主文件 `ctyun-stream-fix-proxy.py`；测试进 `ctyun-stream-fix-proxy.test.py`。锚点为当前工作树行号。

- `ctyun-stream-fix-proxy.py:101-114`（TPM 常量区）——新增校准常量：`TPM_CALIBRATE_INPUT_BYTES`（大 input 填充字节数，env seam `CTYUN_CALIBRATE_INPUT_BYTES`，默认 240000）、`TPM_CALIBRATE_STEP_TOKENS`（每批 token 增量，默认 5000）、`TPM_CALIBRATE_MAX_TOKENS`（探测请求 `max_tokens`，默认 1）、`TPM_CALIBRATE_BATCH_GAP_S`（批间隔，默认 2.0）、`TPM_CALIBRATE_MAX_DURATION_S`（单次校准墙钟上限，默认 240）、`TPM_CALIBRATE_MIN_INTERVAL_S`（同模型最小校准间隔，默认 1800）、`TPM_CALIBRATE_HARD_CAP_TOKENS`（单次校准 token 消耗硬顶，默认 400000）、`TPM_CALIBRATE_SETTLE_WAIT_S`（窗口清零等待上限，默认 60）。全部走 env seam 以便测试加速。
- `ctyun-stream-fix-proxy.py:437` `estimate_request_tokens(body)` —— 不改签名；校准引擎复用它估算单批 est（大 input 长度 × `TPM_TOKEN_RATIO` + `max_tokens`）。
- `ctyun-stream-fix-proxy.py:516` `tpm_admit(key_id, est, model)` —— 不改签名。校准请求**不**调用 `tpm_admit`（见 Risk 2 的校准豁免论证）；新增纯函数 `tpm_probe_charges_budget(model)` 仅用于文档/断言目的，返回 `False`。
- `ctyun-stream-fix-proxy.py:756` `tpm_body_err_samples(lines, model)` —— 保留，扩展为优先消费持久化样本、`LOG_RING` 作为兜底：新增 `tpm_body_err_samples_persisted(samples, model)` 纯函数（输入持久化样本列表）。
- `ctyun-stream-fix-proxy.py:774` `tpm_budget_recommend(model, lines)` —— 签名扩展为 `tpm_budget_recommend(model, lines, samples=None, probe=None, usage=None)`；新增优先级判定与 `enabled_advice`/`source` 输出。**删除多档 `choices` 生成逻辑**：有样本分支 `:796-804`（`values`/`labels`/`choices` 去重保序）与无样本分支 `:784-793`（`[TPM_LIMIT//2, TPM_LIMIT, TPM_LIMIT*2]` 三档）**整体移除**；返回字典只保留 `recommended`（int 或 None）、`samples`、`hint`（仅 `recommended is None` 时给）、`enabled_advice`、`source`、`probe`，**不再含 `choices` 键**。旧两参调用行为（除 choices 移除）不变。
- `ctyun-stream-fix-proxy.py:808` `tpm_settings_snapshot()` —— 每模型 `recommend` 结构随之去掉 `choices`（其余 `enabled_advice`/`source`/`probe` 字段见"设计决策 7"）；`with LOG_LOCK` 取行处（:816-817）改为同时取持久化样本快照与 `PROBE_STATE` 快照（各短持一次锁，不嵌套）。`default_limit` 保留。
- 新增（放在 `tpm_settings_snapshot` 之后，约 :834 之后）：`CALIBRATE_LOCK = threading.Lock()`、`_CALIBRATE_STATE`（结构见"设计决策 8"）、`calibrate_state_snapshot()`、`calibrate_start(model)`、`calibrate_abort()`、`calibrate_engine(model, send_one)`（纯逻辑，注入发送器）、`_calibrate_loop(model)`（daemon 线程入口）、`_calibrate_send(model, body)`（真发送器，走 `_open_upstream` 同款 `http.client` 直连）。
- `ctyun-stream-fix-proxy.py:1098-1107` `save_stats_counters(path)` —— `json.dump` 载荷新增两个顶层键：`tpm_body_err_samples`、`tpm_probe_results`（从 `_CFG_LOCK` 护的内存取快照）。
- `ctyun-stream-fix-proxy.py:1121-1188` 容错加载区 —— 新增 `load_tpm_body_err_samples(path)`、`load_tpm_probe_results(path)`，逐条校验 + prune（风格仿 `load_tpm_model_budgets` :1139-1158）。
- `ctyun-stream-fix-proxy.py:680` `record_error_event` / `ctyun-stream-fix-proxy.py:1484` `_record_request` —— 不改签名；校准请求**不**调用二者（见"设计决策 10"标记策略）。
- `ctyun-stream-fix-proxy.py:1747` `_proxy` / `:1778` `_proxy_relay` —— 不改。校准不经 relay（见"设计决策 2"）；仅在 `_proxy_relay` 的 TPM hook 前新增一行 `if getattr(self, "_calibrate_probe", False): ...` 的 bypass 分支（防御性，正常路径不触发）。
- `ctyun-stream-fix-proxy.py:2242` `_log` —— 新增可选 kwarg `probe=None`；`probe=True` 时在 `extra` 追加 ` probe=1`（校准请求的日志标记）。
- `ctyun-stream-fix-proxy.py:1365` `_probe_loop` —— 不改；新增的 `_calibrate_loop` 参照其 daemon 线程模式（恒启动、开关检查、显式异常处理）。
- `ctyun-stream-fix-proxy.py:2271-2357` `do_GET` 分发 —— 新增 `/api/tpm_calibrate` GET 分支（轮询）。
- `ctyun-stream-fix-proxy.py:2359-2443` `do_POST` 分发 —— 新增 `/api/tpm_calibrate` POST 与 `/api/tpm_calibrate_abort` POST 分支，鉴权同 `write_allowed`（:2373-2378 同款）。
- `ctyun-stream-fix-proxy.py:2567-2575` TPM 设置卡片 HTML —— 每行加"校准测试"按钮 + 进度/结果容器 + 全局"中止校准"按钮。
- `ctyun-stream-fix-proxy.py:3288-3334` `renderTpmSettings(data)` —— **下拉框（`:3309-3325` 的 `select` + `choices` 遍历）整体替换为二选一控件**：每个模型行渲染一个 radio 组（`name="tpm-mode-<i>"`）——「使用推荐值 N」（`recommended` 非 None 时可用；勾选即用推荐值）与「自定义」（选中后显示 `<input type="number" data-model=...>` 数字输入框）。`recommended is None` 时「使用推荐值」选项 `disabled`、只能自定义；若模型当前 `budget` 等于 `recommended` 则默认选中"使用推荐值"，否则默认选中"自定义"并回填 `budget`。删除 `:3317-3318` 的 `label（value）` 档位文案拼接。
- `ctyun-stream-fix-proxy.py:3337-3347` `#tpm-save` 监听 —— 读取逻辑由 `select[data-model]` 改为按模式取值：模式=推荐 → `budgets[model] = recommend.recommended`；模式=自定义 → `budgets[model] = parseInt(customInput.value, 10)`。启用（checkbox 勾选）但取值为空/非正数 → 前端 `#tpm-msg` 报错且**不提交**（不静默写 0）。仍 `POST /api/config {tpm_model_budgets}`。
- `ctyun-stream-fix-proxy.py:3373` `DASHBOARD_HTML` —— 六段 join 不变（新 UI 落在既有 `_DASH_SECTIONS_STATIC` 与 `_DASH_JS_CORE` 段内）。
- `ctyun-stream-fix-proxy.py:3390` `main()` —— 启动时加载两个新持久化键；`threading.Thread(target=_calibrate_loop, daemon=True, name="tpm-calibrate").start()`（恒启动一次，仿 :3475-3477 probe daemon）。
- `ctyun-stream-fix-proxy.test.py:1376-1396` `test_tpm_budget_recommend_with_samples` —— **删除 `:1393-1395` 的 choices 断言**，改为 `assertNotIn("choices", rec)` + `recommended == 27000` + `source == "body_err"`。
- `ctyun-stream-fix-proxy.test.py:1398-1405` `test_tpm_budget_recommend_no_samples` —— **删除 `:1404-1405` 的默认三档 choices 断言**，改为 `assertNotIn("choices", rec)` + `recommended is None` + `hint` 存在 + `enabled_advice["suggest"] is False`。
- `ctyun-stream-fix-proxy.test.py:5589-5590` `test_model_list_auto_generated` —— **删除 `recommend["choices"]` 是 list 的断言**，改为断言 `recommend` 不含 `choices` 键且含 `enabled_advice`/`source`。
- `ctyun-stream-fix-proxy.test.py` —— 新增测试类（见 Acceptance Criteria 测试锚点）。

## Acceptance Criteria

全部测试用 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 运行；测试基建复用现有 `make_fake_upstream`（:268）/ `make_scripted_upstream`（:325）/ `start_proxy`（:498）/ `AdminIntegrationTest`（:2899）。验收命令：

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

必须全绿（现有 118 用例 + 新增用例 + 本次更新的 3 个既有用例，0 failure / 0 error）。

AC1 — 加压与判定（mock 上游脚本化返回，单元级）：`calibrate_engine(model, send_one)` 注入假发送器，发送器按调用序号返回"200 正常"直到第 N 次返回拒绝。断言：探测从 `TPM_CALIBRATE_STEP_TOKENS` 起逐级线性加压；首次拒绝即停（不追加确认批次）；返回 `{"threshold": (N-1)*step + base, "batches": N, "consumed": <累计 est>}`。判定复用 `classify_outcome`：429 状态 → `CLASS_REQUEST_FAULT`（`status>=400`）、200+错误体 → `CLASS_BODY_ERROR`（`body_error=True`），两者任一命中即判"拒绝"；`CLASS_OK` 与 `CLASS_POISON_FIXED` 判"未拒绝"。

AC2 — 推荐值 = 实测阈值 × 0.9 向下取整千位且**无 choices**（单元级）：`tpm_budget_recommend(model, [], samples=None, probe={"threshold": 31000, "ts": ...}, usage=None)` → `recommended == 27000`（31000×0.9=27900 → 千位向下 27000），`source == "probe"`，且 `assertNotIn("choices", rec)`。

AC3 — 优先级：探测实测 > body-err 样本 > 历史用量（单元级）：构造三源同时存在，断言 `source == "probe"`；仅 body-err 样本存在时 `source == "body_err"` 且推荐 = `min(samples)×0.9` 千位向下（沿用 :795 口径）；仅历史用量存在时 `source == "usage_estimate"` 且为保守参考值（如 `max(1000, peak_tpm // 1000 * 1000)`），`enabled_advice["suggest"] == False`（无拒绝证据不主动建议启用）。三路径均 `assertNotIn("choices", rec)`。

AC4 — 持久化样本跨重启（集成级）：用 `start_proxy(..., seed_persist={...})` 预写 `tpm_body_err_samples`，`GET /api/tpm_settings` 的 `recommend.samples` 与 `source=="body_err"` 反映预写值；再用 `admin_post` 触发一次真实 body-err 流量后 `GET`，断言样本条数增加；`SIGTERM` 后重读持久化文件，断言 `tpm_body_err_samples` 键存在且含新样本（`save_stats_counters` :1098 原子写）。

AC5 — 校准 API 形态与全局互斥（集成级）：`POST /api/tpm_calibrate {"model": X}` 返回 200 + 任务 id；立即再 `POST` 同/异模型 → 409（同一时刻只允许一个校准任务）；`GET /api/tpm_calibrate` 返回 `{"running": bool, "model": ..., "progress": {"batch": n, "consumed": t}, "result": {...}}`；`POST /api/tpm_calibrate_abort` 使 `running` 转 `False` 且结果标 `aborted`；`POST` 缺 `model` / 非字符串 → 400；非本机且无 `X-Admin-Token` → 403（复用 `write_allowed` :1444）。

AC6 — 安全阀（单元级 + 集成级）：① `consumed > TPM_CALIBRATE_HARD_CAP_TOKENS` → 引擎停止并标 `capped`；② 墙钟超 `TPM_CALIBRATE_MAX_DURATION_S` → 停止并标 `timeout`；③ 距上次同模型校准 < `TPM_CALIBRATE_MIN_INTERVAL_S` → `calibrate_start` 返回 429 + `{"error": "..."}`，引擎不启动（防连点烧配额）；④ abort 在批间隙生效（发送器下一次调用前返回）。

AC7 — 校准不污染真实流量统计（集成级）：一次完整校准后，`GET /api/stats` 的 `requests_total` / `daily_by_model` / `recent` 增量 == 0（校准不走 `_record_request`）；`GET /api/logs` 中校准行含 ` probe=1` 标记；`GET /api/tpm_stats` 的桶 `used` 不因校准增长（校准不调 `tpm_admit`）。

AC8 — Dashboard 校准 UI（集成级，`AdminIntegrationTest` 风格）：`GET /` HTML 含"校准测试"按钮文案、"中止校准"、"一键应用"、`enabled_advice` 渲染分支；断言 HTML 含 `textContent` 且不含 `innerHTML`（沿用 :3011-3012 防 XSS 门禁）；`renderTpmSettings` 对含 `probe`/`enabled_advice` 的 payload 不抛错。

AC9 — 探测请求形态（单元级）：构造校准请求体，断言 `max_tokens == TPM_CALIBRATE_MAX_TOKENS`（默认 1）、`stream == False`、`messages` 内容长度 ≥ `TPM_CALIBRATE_INPUT_BYTES`；`estimate_request_tokens(body)` 对校准体的估算 ≥ 预期大 input 量级（证明逐级加压真实作用于准入口径）。

AC10 — 容错加载与 prune（单元级）：`load_tpm_body_err_samples` 对缺键/损坏 JSON/非 list/元素缺字段/时间戳非法 → 返回 `[]` 不抛；超 `TPM_SAMPLES_MAX`（条数上限）或超 `TPM_SAMPLES_RETENTION_DAYS`（保留天数）→ prune 后长度符合上限；`load_tpm_probe_results` 同款逐模型容错。

AC11 — 回归：现有 118 用例全绿（其中 3 个 choices 相关用例已按上文更新）；`tpm_budget_recommend(model, lines)` 两参调用的 `recommended`/`samples`/`hint` 行为与旧版一致（仅 `choices` 键按需求删除）。

AC12 — 二选一预算设置（本次硬需求，单元级 + 集成级）：① `tpm_budget_recommend` 有样本、无样本两路径返回 dict 均 `assertNotIn("choices", rec)`；② `tpm_settings_snapshot` 每模型 `recommend` 均不含 `choices`；③ `GET /` HTML 含"使用推荐值"与"自定义"文案，且**不含**旧档位文案"宽松×1.5"/"宽松×2"/"默认×2"/"默认/2"；④ `renderTpmSettings` 对 `recommended is None` 的模型渲染"自定义"输入框且"使用推荐值"选项 `disabled`；⑤ 保存链路：模式=推荐 → `POST /api/config` body 中该模型 budget == `recommend.recommended`；模式=自定义 → == 输入框值；启用但取值为空/非法 → 前端报错且不发请求；⑥ 更新后的 `test_tpm_budget_recommend_with_samples`、`test_tpm_budget_recommend_no_samples`、`test_model_list_auto_generated` 全绿。

AC13 — 校准"一键应用"遵循二选一（集成级）：对某模型完成校准后，Dashboard"一键应用"把该行模式切到"使用推荐值"并回填 `recommend.recommended`（若该行此前为自定义且用户未改则保留自定义），点击 `#tpm-save` 后 `POST /api/config` 携带的 budget 与所选模式一致；校准结果**不**自动写 `TPM_MODEL_BUDGETS`（未点保存前 `GET /api/config` 不变）。

## Risks

R1 — **校准消耗真实配额且连带拒绝真实请求**（用户已接受）。缓解：默认 `TPM_CALIBRATE_HARD_CAP_TOKENS=400000`（≈ 全局 TPM_LIMIT 110000 的 3.6×，即 min(3×预估阈值, 硬顶) 的上界）、`TPM_CALIBRATE_MAX_DURATION_S=240`、同模型最小间隔 1800s、Dashboard 显式二次确认文案明示"将消耗约一次上游限额的 token 配额"。校准不自动执行（无定时任务，见 Exclusions）。

R2 — **探测请求被自家 TPM 桶拦截或排队**。决策：探测**不**走 `_proxy_relay`（因而不经过 :1784-1805 的 `tpm_admit` hook），而是由 `_calibrate_send` 用 `http.client` 直连 `UPSTREAM_BASE`（复用 `_open_upstream` :2038 的 URL 拼接与超时口径）。理由：走 relay 会让"被校准模型已启用预算"这一最常见场景下探测被自家桶 429 或 FIFO 排队，导致实测的是自家桶阈值而非上游阈值，校准结论失真；绕开 relay 同时避免污染 `_record_request`/`daily_by_model`（见 AC7）。代价：放弃 relay 的日志/分类观测——用 `_calibrate_loop` 自己的 `_log(..., probe=True)` 与独立状态补偿。`_proxy_relay` 的 bypass 分支仅作防御（若未来有人把校准接到 relay 上）。

R3 — **凭证来源**。决策：**(a) 复用最近流量中见过的 key**——proxy 是透传的、不持有上游 Bearer token，唯一无需用户操作的来源是"最近一次经代理的真实请求的 Authorization 头"。实现：新增 `_LAST_AUTH`（模块级，独立 `_AUTH_LOCK` 或复用 `_CFG_LOCK` 短持）在 `_proxy_relay` 读到 `Authorization` 时更新为原始 token 字符串；**仅内存持有、绝不落盘**（`save_stats_counters` 白名单不含它）。UI 明示"将使用最近一次请求的凭证（<token 前 8 位>…）"，token 本身用 `textContent` 展示脱敏前缀。**fallback 保留 (b)**：`POST /api/tpm_calibrate` 接受可选 `token` 字段；若 `_LAST_AUTH` 为空（如代理刚重启、尚无真实流量）则要求请求体带 `token`，否则返回 400 + `{"error": "无可用凭证：请先产生一次真实流量，或在请求体提供 token"}`。理由：(a) 是默认无摩擦路径，(b) 覆盖冷启动，二者互补而非替代。

R4 — **加压策略**。决策：**线性逐批**（非二分）。理由：上游限流是"窗口内累计 token 超阈值"的阶跃行为，二分假设单调连续可探；线性从低起加压每次只增加一个 `TPM_CALIBRATE_STEP_TOKENS`，首次拒绝点即最接近真实阈值的下界，且每批增量小 → 超调（overshoot）小，对上游的实际冲击可控。窗口滚动关系：每批之间 `sleep(TPM_CALIBRATE_BATCH_GAP_S)`（默认 2s），累计消耗按批累加；因上游窗口是 60s 滚动，线性加压的累计消耗在 <60s 内会叠加在同一窗口 → 这正是"测出窗口阈值"的机制。终止条件：**首个拒绝即停**（不追加确认批次），理由：追加确认会二次消耗配额，而拒绝信号（429 或 200+错误体）语义已明确。窗口起点清零：**不做**强制等待——校准前调用方（`calibrate_start`）记录 `started_at`，引擎在首批发送前可选等待至"距该模型上一次校准/流量窗口起点 ≥ TPM_WINDOW_S"；但为控制时长，默认仅在检测到"启动前 60s 内有该模型成功流量"时等待（上限 `TPM_CALIBRATE_SETTLE_WAIT_S`），否则直接开跑（Dashboard 明示"建议在低流量时段校准"）。

R5 — **拒绝信号判定与 `classify_outcome` 协同**。决策：探测复用 `classify_outcome`（:352）。`_calibrate_send` 返回 `(status, body_error)`，引擎调 `classify_outcome(status=status, body_error=body_error)`：`CLASS_BODY_ERROR` 或 `CLASS_REQUEST_FAULT`（`status>=400`）→ 拒绝；`CLASS_OK`/`CLASS_POISON_FIXED` → 未拒绝；`CLASS_UPSTREAM_FAULT`（5xx/连接异常）→ **不是限流信号**，重试一次后仍失败则中止校准标 `upstream_error`（避免把上游故障误判为阈值）。这与现有 `body_error` 分类路径（:368-370、:1994）完全一致。

R6 — **线程与异常**。`_calibrate_loop` 仿 `_probe_loop`（:1365）daemon 模式；发送器内 `except (OSError, http.client.HTTPException)` 必须显式分类（网络异常 → `upstream_error` 状态并 `_safe_log_stderr` 留痕），**禁空 catch**；引擎主循环的每次批间 `time.sleep` 需检查 abort 标志（不能睡死）。任何未预期异常经 `_calibrate_loop` 顶层 `except Exception as exc` 记录到 `_CALIBRATE_STATE["error"]` + `_safe_log_stderr` 后复位 `running=False`（不是静默吞掉——写入状态供 Dashboard 展示）。

R7 — **持久化膨胀**。`tpm_body_err_samples` 与 `tpm_probe_results` 均需上限：样本 `TPM_SAMPLES_MAX`（默认 200 条/模型）、`TPM_SAMPLES_RETENTION_DAYS`（默认 30 天）；探测结果每模型只保留最近 5 条。prune 在 save 时执行（仿 `_prune_daily` :1052）。新增键缺失时加载回落 `{}`（仿 :1121-1188），旧版二进制读入新键自动忽略（无版本号双向 degrade）。

R8 — **`_LAST_AUTH` 内存持有 token 的安全面**。仅进程内存、不落盘、不进日志（脱敏后展示）；进程退出即失。风险接受：本代理本就是本机透传，token 已在进程内流转。

R9 — **`_CFG_LOCK` 死锁**。`save_stats_counters`（:1089-1093）已持 `_CFG_LOCK`；新增样本快照若也需 `_CFG_LOCK`，必须与 `set_*` 同款"锁外调用"纪律（:1196-1198 注释），禁嵌套非重入锁。

R10 — **删除 `choices` 的兼容面**。`recommend.choices` 是既有 `GET /api/tpm_settings` 响应字段，删除后任何外部消费者（含既有 3 个测试 `:1393-1395`/`:1404-1405`/`:5589-5590`）会失配——本次一并更新测试；`TPM_MODEL_BUDGETS` 的数据形态（`{model: int}`）不受影响，**无需数据迁移**（已存预算值本身不依赖档位）。`renderTpmSettings` 对旧响应（仍带 choices）不应抛错：改为按 `recommended` 字段渲染，不读 `choices`。

## Exclusions

本次**不做**：

- **不保留任何多档预算 choices**：推荐×1.5 / 推荐×2 / 默认÷2 / 默认×2 及 `tpm_budget_recommend` 的 `choices` 生成逻辑（:784-793、:796-804）全部删除；预算设置只有「推荐值」与「自定义值」两选项。
- 不动 FIFO 排队机制本身（`tpm_admit` :516 的排队/超时/退款语义、`TPM_WAITERS` 结构、`_tpm_remove_waiter` 同一性摘除）。
- 不改全局 `TPM_LIMIT` 默认值（110000）与 `CTYUN_TPM_LIMIT_BY_MODEL` env 解析（:68）。
- 不做定时/自动校准（无后台周期探测；校准只由 Dashboard 手动触发）。
- 不自动写 `TPM_MODEL_BUDGETS`（校准结果不自动启用限流，仅回填 UI 由用户点 `#tpm-save` 确认）。
- 不改 `classify_outcome`（:352）的判定优先级与既有类别常量。
- 不新增依赖（纯 stdlib）；不引入数据库/独立持久化文件（复用现有 `PERSIST_PATH`）。
- 不改 `_relay_sse`（:2069）/`_relay_buffered`（:2191）的剥毒与观测语义。
- 不做多并发校准（全局单任务互斥，见 AC5）。
- 不做跨模型的联合校准或全局预算自动推导。
- 不改 probe daemon（`_probe_once` :1327 / `_probe_loop` :1365）的既有行为。
- 不做旧预算值到"推荐/自定义"模式的自动重映射（budget 值原样保留，UI 仅按 `budget == recommended` 决定默认选中项）。
