# TPM 限流模型选择 + 设置页设计（ctyun-stream-fix-proxy）

## Goal

把现有「全模型无差别 TPM 准入」改为「按模型 opt-in 限流」：仅用户勾选的模型走桶/排队/429（生产实证仅 kimi-k3-oc 需要），其余模型直通；dashboard 新增 TPM 限流设置区块，模型清单自动从运行数据生成、预算值由 body-err 日志实证推荐为选择档，服务所有经本代理访问天翼云上游的客户端。

## 设计决策（定死）

1. **opt-in 语义（admit 直通，settle 保留观测）**：`tpm_admit(key_id, est, model)` 在拿锁前短路：`model` 未在启用集合 → 直接 `return ("ok", 0)`（不建桶、不排队、不 429、不 touch rejected）。`tpm_settle` **保留全模型执行**（桶不存在时 no-op 现状不变；若桶存在——模型从启用→取消时遗留——继续校正并 notify，让在途 waiter 走完当次判定）。REQ 行 `tpm=` 字段维持现状口径（`tpm_used` 仅 `tpm_key is not None` 时输出，与限流启用与否无关）。
2. **运行时状态唯一权威 = 持久化文件**：新模块态 `TPM_MODEL_BUDGETS: dict`（`{model: int}`，在启用集合内的模型及其预算），由 `_CFG_LOCK` 护写（与 `UPSTREAM_BASE`/`CAPTURE_ERRORS` 同锁，读侧无锁靠引用赋值原子）。`TPM_LIMIT_BY_MODEL`（env 表）与 `_TPM_LIMIT_BY_MODEL_BUILTIN` 均**移除**，`CTYUN_TPM_LIMIT_BY_MODEL` env **废弃**。`tpm_limit_for(model)` 语义改为：在 `TPM_MODEL_BUDGETS` → 其值；否则 → 全局 `TPM_LIMIT`（仅启用模型的 budget 判定路径会消费它）。**迁移**：`main()` 启动读持久化 `tpm_model_budgets` 键；若缺失且 `CTYUN_TPM_LIMIT_BY_MODEL` env 非空 → 用 env 解析结果 seed 一次并立即落盘（`save_stats_counters` 顺带），stderr 打迁移一行；若两者皆无 → `{"kimi-k3-oc": 30000}` 默认 seed（生产实证线）。`_parse_tpm_limit_by_model` 保留供迁移用。
3. **模型清单数据源**：**RECENT_REQUESTS（100 条内存环）∪ STATS["daily_by_model"][今日] 键 ∪ TPM_MODEL_BUDGETS 键**（并集去重、字典序排序）。不扫 /api/logs REQ 行（正则提取脆、1000 行环噪音大）；不用 /api/tpm_stats buckets（限流后只含启用模型，鸡生蛋）。空态（三源全空）→ 返回空 list，UI 显示「暂无模型流量记录」。
4. **预算推荐算法（纯函数可单测）**：`tpm_budget_recommend(model, error_events, recent) -> dict`。数据源：`ERROR_EVENTS`（kind=body_error 且 model 匹配，取 `response` 无意义——**改用 LOG_RING 扫 `result=body-err` + `model=` + `tpm=` 的 REQ 行**，因 body-err 行的 `tpm=` 才是「上游拒绝时窗口用量」实证）。实现：`tpm_body_err_samples(lines, model) -> list[int]`，正则 `re.compile(r"result=body-err\b.*\bmodel=%s\b.*\btpm=(\d+)" % re.escape(model))` 扫 LOG_RING 副本。公式：`base = min(samples) if samples else None`；`recommended = (base * 9 // 10) // 1000 * 1000`（min×0.9 向下取整到千位，最小 1000）；无样本 → `{"recommended": None, "samples": 0}`。档位（choices）：有样本时 `[recommended, recommended*3//2//1000*1000, recommended*2]`（去重，标注重量名「推荐/宽松×1.5/宽松×2」）；无样本时 `[TPM_LIMIT//2, TPM_LIMIT, TPM_LIMIT*2]` + `hint="无上游拒绝证据，建议不限流"`。
5. **API 形态**：新端点 `GET /api/tpm_settings`（无鉴权，同 /api/stats 口径）返回 `{"models": [{"name", "enabled", "budget", "recommend": {"recommended", "samples", "choices": [{"value", "label"}]}, "hint"}...], "default_limit": TPM_LIMIT}`。`POST /api/config` 扩展可选键 `tpm_model_budgets`（dict[str, int]，int>0，键≤32 个，model 名 ≤200 字符）→ 全量替换 `TPM_MODEL_BUDGETS`（原子 dict 引用替换）+ 落盘；非法值 → 400 逐项说明。鉴权沿用 `write_allowed` 现状（localhost 直通 / X-Admin-Token）。GET /api/config 响应加 `tpm_model_budgets` 键。`tpm_snapshot()` 的 `config` 加 `"enabled_models": sorted(TPM_MODEL_BUDGETS.keys())`（`limit_by_model` 键改名 `model_budgets`，引用 `TPM_MODEL_BUDGETS`）。
6. **兼容与边界**：模型被取消启用时在途 waiter 继续走完当次准入判定（settle 仍 notify），下次请求起直通——不做 waiter 驱逐。tpm-queue-full/timeout 语义只对启用模型存在（429 响应体 `model_tpm_limit` code 不变）。未启用模型 REQ 行无 qwait= 字段（`tpm_admit` 直通返回 0 且不置 `tpm_qwait_ms`——注意：调用点仅在 `tpm_status in ("full","timeout")` 时用 qwait，直通路径 `qwait=0ms` 照常输出，与现行 ok 路径一致；`tpm=` 仍输出）。`TPM_KEY_CAP` 惰性驱逐逻辑不变（仅启用模型会建桶，桶数自然收缩）。
7. **设置页 UI（v1.5）**：`_DASHBOARD_SRC` 在「上游端点」section 后插入「TPM 限流设置」section：每模型一行 = 名称（自动生成）+ 启用 checkbox + 预算 `<select>`（选项由 /api/tpm_settings 的 choices 生成，含每档 value/label）+ 推荐 hint 文案；底部「保存限流设置」button → POST /api/config（`{"tpm_model_budgets": {勾选的 name: 选中 value}}`）；成功回显沿用 `.msg ok/err` 模式；区块数据独立 fetch /api/tpm_settings（不进 2s poll，仅加载时与保存后刷新）。沿用现有 `el()`/`textContent` 模式（禁 innerHTML 惯例不变）。

## Files to Change

- `ctyun-stream-fix-proxy.py:100-112`（常量区 TPM_LIMIT 块）— 删 `_TPM_LIMIT_BY_MODEL_BUILTIN`；`TPM_LIMIT_BY_MODEL` 保留解析但改名语义为「迁移 seed 专用」（`tpm_limit_for` 不再消费它）；新增 `TPM_MODEL_BUDGETS: dict = {}`（运行态，`_CFG_LOCK` 护写）。
- `ctyun-stream-fix-proxy.py:420 tpm_limit_for(model) -> int` — 改三级回退为两级：`TPM_MODEL_BUDGETS.get(model)` → `TPM_LIMIT`（删 `_TPM_LIMIT_BY_MODEL_BUILTIN` 分支；docstring 注明「仅启用模型走 budget 判定路径」）。
- `ctyun-stream-fix-proxy.py:480 tpm_admit(key_id: str, est: int, model)` — 函数体首行（`budget = tpm_limit_for(model)` 之前）加短路：`if model not in TPM_MODEL_BUDGETS: return ("ok", 0)`（model 为 None 时 `None not in {...}` 恒真 → 直通，天然符合 opt-in）。
- `ctyun-stream-fix-proxy.py:550 tpm_settle(key_id, est, actual, model)` — 不改签名；docstring 补一句「未启用模型桶不存在 → 现有 no-op 分支命中，直通模型天然免疫」。
- `ctyun-stream-fix-proxy.py:570 tpm_snapshot()` — `:593` `"remaining"` 与 `:604` `"limit_by_model"`：`limit_by_model` 键内容改引用 `TPM_MODEL_BUDGETS`；`config` 加 `"enabled_models": sorted(TPM_MODEL_BUDGETS.keys())`。
- `ctyun-stream-fix-proxy.py:669 logs_snapshot()` 之后 — 新增三个纯函数：`tpm_body_err_samples(lines: list, model: str) -> list[int]`（扫 LOG_RING 行副本，正则见决策 4）、`tpm_budget_recommend(model: str, lines: list) -> dict`（公式与档位生成见决策 4）、`tpm_settings_snapshot() -> dict`（模型清单并集 + 推荐组装，见决策 3/5；内部 `with LOG_LOCK: lines = [l["line"] for l in LOG_RING]`、`with STATS_LOCK` 读 RECENT_REQUESTS/daily_by_model、`with _CFG_LOCK` 读 TPM_MODEL_BUDGETS——三锁分别短持，不嵌套）。
- `ctyun-stream-fix-proxy.py:792 persist_upstream()` / `:929 save_stats_counters()` — 落盘 dict 加 `"tpm_model_budgets"` 键（`with _CFG_LOCK` 取快照，同 `capture_errors` 模式；`persist_upstream` 签名加可选形参 `tpm_model_budgets: dict = None` 或直接由 save_stats_counters 统一承载——implementer 按「save_stats_counters 为唯一全量出口」优先，persist_upstream 是增量写上游的窄出口，须同步带新键避免回写覆盖丢键）。
- `ctyun-stream-fix-proxy.py:980 load_capture_errors()` 之后 — 新增 `load_tpm_model_budgets(path: str) -> dict`（逐键校验：model 名非空 str ≤200 字符、limit 为 int 且 >0、≤32 键；非法项跳过，同 `load_daily_by_model_buckets` 容错风格）。
- `ctyun-stream-fix-proxy.py:1111 set_upstream_base()` / `:1122 set_capture_errors()` 同构 — 新增 `set_tpm_model_budgets(budgets: dict) -> None`：`with _CFG_LOCK` 原子替换 `TPM_MODEL_BUDGETS` 引用 + 调 `persist_upstream`；锁外 `save_stats_counters(PERSIST_PATH)`（同 :1117-1119 死锁注释纪律）。
- `ctyun-stream-fix-proxy.py:1912 do_GET` — `elif path == "/api/tpm_stats":` 分支后新增 `elif path == "/api/tpm_settings": self._send_json(200, tpm_settings_snapshot())`；`/api/config` GET payload（:1916-1918）加 `"tpm_model_budgets": dict(TPM_MODEL_BUDGETS)`。
- `ctyun-stream-fix-proxy.py:1987 do_POST` /api/config — `:2011` cap 解析后新增 `tpm_model_budgets` 可选键校验（dict 类型、≤32 键、每键非空 str、每值 int>0 → 否则 400 逐项错误文案）；`:2023` 后加 `if tpm is not None: set_tpm_model_budgets(tpm)`；响应 dict 加 `"tpm_model_budgets"`。
- `ctyun-stream-fix-proxy.py:2627 main()` 启动回填 — `CAPTURE_ERRORS = load_capture_errors(...)` 之后：`global TPM_MODEL_BUDGETS`；`budgets = load_tpm_model_budgets(PERSIST_PATH)`；空且 env `CTYUN_TPM_LIMIT_BY_MODEL` 非空 → `_parse_tpm_limit_by_model` seed + `_safe_log_stderr("... migrated CTYUN_TPM_LIMIT_BY_MODEL to tpm_model_budgets ...")` + `save_stats_counters`；仍空 → `{"kimi-k3-oc": 30000}`；赋给 `TPM_MODEL_BUDGETS`。
- `ctyun-stream-fix-proxy.py:2144`（上游端点 section `</section>` 后）— 插入「TPM 限流设置」section：`<section class="card" id="tpm-settings-card">`，含 `<div class="card-title">TPM 限流设置（按模型启用）</div>`、`<div id="tpm-models">`（行容器）、保存按钮 `<button id="tpm-save" type="button">`、`<p class="msg" id="tpm-msg">`；JS 侧新增 `renderTpmSettings(data)`（el()/textContent 构建行：checkbox `data-model` + select `data-model` + hint span）与 `loadTpmSettings()`（fetch /api/tpm_settings）+ 保存 handler（组装勾选行 → POST，沿用 :2570-2589 的 msg 模式）；`loadTpmSettings()` 在 :2600 `poll()` 首次调用旁挂一次。
- `ctyun-stream-fix-proxy.test.py` —
  - 纯函数单测（`ProxyDashboardUnitTest` 同文件，复用 `load_proxy_module` :595 + `_tpm_cleanup` :1035）：`tpm_body_err_samples` 对构造行集提取 tpm 值/过滤异模型/容忍无 tpm 字段行；`tpm_budget_recommend` 有样本（min×0.9 千位取整、choices 去重排序）与无样本（hint + 默认三档）两路；`tpm_admit` 未启用模型直通（不建桶、TPM_BUCKETS 空、返回 ("ok",0)）、启用模型照旧；`load_tpm_model_budgets` 容错矩阵；`tpm_limit_for` 两级回退。
  - 集成测试新 class `TpmModelSelectionTest`（复用 `make_scripted_upstream` :300 + `start_proxy(extra_env, seed_persist)` :414 + `post_sse_auth` :597 + `admin_get` :573 + `admin_post` :585 + `stderr_text` :369）：①seed_persist 不含新键 + extra_env 无 env → 仅 kimi 在 /api/tpm_settings enabled=true（默认 seed）；②POST /api/config `{"tpm_model_budgets": {}}` 后 kimi 大请求 → 200（直通，原 TpmPerModelTest 同 payload 在启用时 429）；③再 POST 启用 kimi:1000 → 同 payload 429；④重启（同 persist_dir 二次 start_proxy 或 seed_persist 回灌）后设置生效；⑤GET /api/tpm_settings 对发过流量的模型返回 choices 非空。
  - `test_dashboard_html_full_page` (:2600) 追加 v1.5 断言：`assertIn("TPM 限流设置", html)`、`assertIn('id="tpm-models"', html)`、`assertIn("/api/tpm_settings", html)`、`assertIn('id="tpm-save"', html)`。
  - `TpmPerModelTest` (:4983) 与 env 表相关用例（:4971-5011 `test_kimi_independent_budget` 等）：迁移路径改写——extra_env 的 `CTYUN_TPM_LIMIT_BY_MODEL` 改 seed_persist `{"tpm_model_budgets": {"kimi-k3-oc": 1000}}`（或保留 env 用例走迁移 seed 路径验证迁移语义，implementer 按「迁移语义有独立断言」优先）。

## Acceptance Criteria

- `python3 ctyun-stream-fix-proxy.test.py` 全绿（含既有 199 例，env 表相关用例迁移后语义等价）。
- 未启用模型直通：seed_persist `{"tpm_model_budgets": {}}`，`CTYUN_TPM_LIMIT=50`，发 est>50 的带 auth 请求 → 200，`GET /api/tpm_stats` 的 buckets 为空、stderr 无 `tpm-queue-full`。
- 启用模型照旧 429：seed `{"tpm_model_budgets": {"kimi-k3-oc": 1000}}`，同 key 先发小 kimi 占桶再发 est>1000 kimi → 429 `model_tpm_limit`；同 key 发未启用的 deepseek 大请求 → 200。
- 推荐算法：LOG_RING 构造 3 条 `result=body-err model=kimi-k3-oc tpm=31038/32375/33227` 行 → `tpm_budget_recommend` 返回 `recommended == 27000`（31038×0.9=27934.2 → 千位取整 27000）、choices 为 `[27000, 40000, 54000]`（×1.5=40500→40000，×2=54000）且 label 含「推荐」；无样本模型 → `recommended is None` 且 hint 含「无上游拒绝证据」。
- 模型清单自动生成：发过 deepseek 流量（无 auth）后 GET /api/tpm_settings 的 models 含 `deepseek-v4-pro-0813-oc`；清单与 TPM_MODEL_BUDGETS 键并集去重、字典序。
- 保存→重启→持久化：admin_post /api/config 写 `{"tpm_model_budgets": {"glm-5.3-oc": 50000}}` → 200 响应含同值；读 persist 文件含 `tpm_model_budgets` 键；同 persist 路径重启代理后 GET /api/config 返回该值。
- env 迁移：seed_persist 不含新键 + extra_env `CTYUN_TPM_LIMIT_BY_MODEL="kimi-k3-oc:1000"` → 启动后 GET /api/config 的 `tpm_model_budgets == {"kimi-k3-oc": 1000}` 且 persist 文件已落该键。
- UI 区块渲染：GET / 的 HTML 含「TPM 限流设置」、`id="tpm-models"`、`id="tpm-save"`、`/api/tpm_settings`；动态行构建不含 innerHTML（沿用 :2612 断言惯例）。
- 非法输入：POST `{"tpm_model_budgets": {"m": -1}}` → 400；POST `{"tpm_model_budgets": "not-a-dict"}` → 400；超 32 键 → 400。
- **部署与生产验证（主代理 DELIVER 阶段执行，非 implementer）**：`cp ctyun-stream-fix-proxy.py ~/.local/bin/` → `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy` → ①dashboard 设置区块可见 kimi 已勾选（默认 seed）；②deepseek/glm 流量 REQ 行不再有 `qwait=` 非零值；③grep `result=tpm-queue` 日志仅出现 kimi 模型名。

## Risks

- **`persist_upstream` 窄出口丢键**：:792 的 `persist_upstream` 重写整个 JSON（`{"upstream_base","capture_errors"}`），若不加 `tpm_model_budgets` 键会在「改上游端点」时抹掉限流设置。Files 条目已标注必须同步带新键；测试用「POST upstream_base 后 GET tpm_settings 不变」防回归。
- **模型清单空态**：重启后 RECENT_REQUESTS 清空、daily_by_model[今日] 可能尚无该模型 → 清单只剩 TPM_MODEL_BUDGETS 键（启用模型）。可接受：已启用模型恒可见，未启用模型待首请求后出现；UI 空态文案已定。
- **LOG_RING 正则误匹配**：REQ 行字段顺序固定（result 在 model 前、tpm 在 model 后，:1889-1898），正则按 `result=body-err.*model=X.*tpm=N` 顺序锚定；若未来 _log 字段重排，推荐退化为无样本 hint（fail-open 不崩）。
- **取消启用时的在途 waiter**：waiter 在 TPM_LOCK.wait 循环中每次唤醒重查 `bucket.used + est <= budget`（budget 已在入队时定死为局部变量）——取消启用不影响在途 waiter 的既有 budget 判定，语义自洽；新请求直通。已在决策 6 定死，不测驱逐。
- **`TPM_LIMIT_BY_MODEL` env 废弃的部署影响**：生产 launchd plist 若曾设该 env（实测未设，内置默认在扛），迁移 seed 逻辑覆盖；plist 本身不动（Exclusions）。
- **锁序**：`tpm_settings_snapshot` 短持 LOG_LOCK/STATS_LOCK/_CFG_LOCK 各一次、不嵌套、不做 IO，与 :723-731 锁纪律兼容；`tpm_admit` 的 `TPM_MODEL_BUDGETS` 读在 TPM_LOCK 外（引用读取原子），无新锁序。

## Exclusions

- 不改 `CTYUN_TPM_LIMIT` / `TPM_WINDOW_S` / `TPM_QUEUE_MAX` / `TPM_TOKEN_RATIO` 全局常量语义；不做 per-model ratio。
- 不做 429 排队参数（queue_max/timeout）的 UI 化；不做自定义数字输入（预算仅限推荐档位）。
- 不改 tpm-queue-full/timeout 的 429 响应体结构与 `model_tpm_limit` code；不改 ERROR_EVENTS schema。
- 不扫历史日志文件（~/.local/log/ctyun-fwd.err）生成推荐——推荐仅基于内存 LOG_RING（重启后样本从 0 累积，hint 兜底）。
- 不动 plist / launchd / deploy 配置；`CTYUN_TPM_LIMIT_BY_MODEL` env 保留解析仅为迁移 seed（下版本可删）。
- 不做多 key 维度的限流配置 UI（启用集合按 model 不按 key，与桶键 (key_id, model) 正交）。
- 不做设置页的鉴权提示 UI 强化（沿用现有 X-Admin-Token 错误文案模式）。

## R31 Evidence

[R31-S1] 问题存在：全模型无差别限流误伤无配额压力模型，且唯一需要的 kimi 预算靠硬编码

证据①（kimi 是唯一被上游限流的模型——body-err 仅出现在 kimi，18 次全部为 kimi-k3-oc）：

```
$ grep -c 'result=body-err' ~/.local/log/ctyun-fwd.err
18
$ grep -oE 'result=body-err filtered=0 model=[a-zA-Z0-9_-]+' ~/.local/log/ctyun-fwd.err | sort -u
result=body-err filtered=0 model=kimi-k3-oc
```

证据②（kimi 上游实际配额线 ~31k：body-err 发生时窗口用量 tpm=31038/32375/32375/33227）：

```
$ grep -nE 'result=body-err.*kimi-k3-oc' ~/.local/log/ctyun-fwd.err | head -4
23785:REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23312 host=eaichat.ctyun.cn ttfb=972.7ms stream=1 outcome=body_error qwait=7865ms tpm=31038 ts=2026-09-30T19:08:13+0800
23790:REQ POST /chat/completions -> 200 dur=18.4s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23317 host=eaichat.ctyun.cn ttfb=637.1ms stream=1 outcome=body_error qwait=17355ms tpm=32375 ts=2026-09-30T19:10:03+0800
23791:REQ POST /chat/completions -> 200 dur=11.1s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23318 host=eaichat.ctyun.cn ttfb=630.9ms stream=1 outcome=body_error qwait=9413ms tpm=32375 ts=2026-09-30T19:10:17+0800
23794:REQ POST /chat/completions -> 200 dur=1.7s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23321 host=eaichat.ctyun.cn ttfb=707.1ms stream=1 outcome=body_error qwait=0ms tpm=33227 ts=2026-09-30T19:11:10+0800
```

证据③（deepseek 无 body-err 且窗口用量冲 98k-113k 仍 200——其他模型无需限流）：

```
$ grep -nE 'result=ok .*model=deepseek.*tpm=(98459|103084|105010|112966)' ~/.local/log/ctyun-fwd.err
23820:REQ POST /chat/completions -> 200 dur=145.8s result=ok filtered=0 model=deepseek-v4-pro-0813-oc ... qwait=0ms tpm=98459 ts=2026-09-30T19:30:52+0800
23821:REQ POST /chat/completions -> 200 dur=140.0s result=ok filtered=0 model=deepseek-v4-pro-0813-oc ... qwait=60326ms tpm=103084 ts=2026-09-30T19:33:13+0800
```

[R31-S2] 根因：tpm_admit 对全模型无差别准入，per-model 预算只能靠 env/内置硬编码，无运行时选择机制

```
$ grep -nE 'def tpm_admit|budget = tpm_limit_for|_TPM_LIMIT_BY_MODEL_BUILTIN|def tpm_limit_for' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
112:_TPM_LIMIT_BY_MODEL_BUILTIN: dict = {"kimi-k3-oc": 30000}  # 实证 31k 触顶留余量；env 表优先覆盖
420:def tpm_limit_for(model) -> int:
480:def tpm_admit(key_id: str, est: int, model):
498:    budget = tpm_limit_for(model)
```

`tpm_admit`（:480）对任何带 auth 请求都走 `tpm_limit_for`（:420）预算判定：deepseek/glm 等上游无配额的模型也被迫排队（生产 qwait 60s+ 见证据③的 qwait=60326ms）甚至 429；kimi 的 30k 预算写死在 `_TPM_LIMIT_BY_MODEL_BUILTIN`（:112），调整需改代码或 env 重启——无「按模型勾选启用 + 预算推荐」的运行时机制。
