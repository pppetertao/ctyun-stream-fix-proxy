# TPM 限流残留三项设计（ctyun-stream-fix-proxy）

## Goal

为已上线的 per-key TPM 限流补齐三项实测暴露的残留：per-model 预算（kimi-k3-oc 31k 即触顶，110k 形同虚设）、超大单请求空闲放行（est>limit 硬拒改为窗口空闲时放行）、拒绝计数盲区（est>limit 硬拒不计入 /api/tpm_stats），服务所有经本代理访问天翼云上游的客户端。

## 设计决策（定死）

1. **桶键改为 (key_id, model)**：`TPM_BUCKETS` 键从 `key_id` 改为 `(key_id, model)` 二元组（model 为 `extract_model(body)` 返回值，可为 `None`）。`_TpmWaiter` namedtuple 加 `model` 字段（`key_id est enqueued_at model`）。`tpm_admit`/`tpm_settle`/`tpm_snapshot` 签名加 `model` 形参。`_tpm_get_bucket(key_id, model)` 惰性驱逐逻辑不变（`w.key_id == k` 改 `(w.key_id, w.model) == k`）。`TPM_KEY_CAP=64` 天然适配（桶数随 key×model 组合增长，惰性驱逐兜底）。
2. **per-model 预算表**：新增 `TPM_LIMIT_BY_MODEL: dict[str, int]`，env seam `CTYUN_TPM_LIMIT_BY_MODEL="kimi-k3-oc:30000,glm-5.3-oc:110000"`（逗号分隔 `model:limit` 对）。解析函数 `_parse_tpm_limit_by_model(env_str) -> dict`：逐项 `split(":")`，非两项/int 失败/负值 → 跳过该项并 `_safe_log_stderr` 一行（fail-open 回默认）。查表函数 `tpm_limit_for(model) -> int`：`TPM_LIMIT_BY_MODEL.get(model, TPM_LIMIT)`（model=None 或查不到 → 默认 `TPM_LIMIT`）。默认表内置 `{"kimi-k3-oc": 30000}`（实证 31k 触顶，留余量；可用 env 覆盖）。
3. **超大放行语义**：`tpm_admit` 中 `est > budget` 分支改为：先 `_tpm_get_bucket` + `_tpm_prune`，若 `bucket.used <= 0` 且 `not any((w.key_id, w.model) == (key_id, model) for w in TPM_WAITERS)` → 放行（照常 `bucket.append((now, est))` + `bucket.used += est`，返回 `("ok", 0)`）；否则维持 `("full", 0)` 硬拒。防饥饿：放行的超大请求会占满窗口，后续正常请求排队属预期（窗口 60s 滚过即恢复）。
4. **拒绝计数**：`est > budget` 硬拒路径先 `_tpm_get_bucket`（touch 建桶）再 `_tpm_rejected[(key_id, model)] += 1`；`_tpm_rejected`/`_tpm_timeouts` 键同步改为 `(key_id, model)` 二元组。`tpm_snapshot` 遍历 `TPM_BUCKETS` 时按 `(key_id, model)` 取计数。日志不变（REQ 行已有 `model=` 字段）。
5. **ratio 不 per-model**：`TPM_TOKEN_RATIO` 保持全局 0.25。glm 低估（settle 后 used 冲 119470>110k）由 settle 自校正兜底，超装窗口有限（60s），不做 per-model ratio 复杂度。
6. **tpm_stats 演进**：bucket 条目加 `"model": model`（原样字符串或 `null`，不脱敏）；`config` 加 `"limit_by_model": TPM_LIMIT_BY_MODEL`（原样 dict）。key 脱敏规则不变（`sha256:` + 前 12 位 hex）。dashboard HTML 不动。
7. **向后兼容**：无 Authorization 仍直通（`tpm_key_id` 返回 None 不进准入）；`CTYUN_TPM_LIMIT` 语义不变（作为默认预算）；`CTYUN_TPM_LIMIT_BY_MODEL` 缺省为空 dict（仅内置 kimi 默认）。

## Files to Change

- `ctyun-stream-fix-proxy.py:62`（常量区 `TPM_KEY_CAP` 旁）— 新增 `TPM_LIMIT_BY_MODEL = _parse_tpm_limit_by_model(os.environ.get("CTYUN_TPM_LIMIT_BY_MODEL", ""))`；内置默认 `{"kimi-k3-oc": 30000}` 在 `_parse_tpm_limit_by_model` 返回空 dict 时由 `tpm_limit_for` 兜底（或解析函数直接返回含默认的 dict，implementer 定）。
- `ctyun-stream-fix-proxy.py:273 _TpmWaiter` — namedtuple 字段从 `"key_id est enqueued_at"` 改为 `"key_id est enqueued_at model"`。
- `ctyun-stream-fix-proxy.py:281 TPM_BUCKETS` — 注释更新：键从 `key_id` 改为 `(key_id, model)` 二元组。
- `ctyun-stream-fix-proxy.py:283 _tpm_rejected` / `:284 _tpm_timeouts` — 键从 `key_id` 改为 `(key_id, model)` 二元组。
- `ctyun-stream-fix-proxy.py:305 estimate_request_tokens()` 之后 — 新增 `_parse_tpm_limit_by_model(env_str: str) -> dict`（fail-open 解析 + `_safe_log_stderr`）与 `tpm_limit_for(model) -> int`（查表回默认）。
- `ctyun-stream-fix-proxy.py:340 _tpm_get_bucket(key_id)` — 签名改 `_tpm_get_bucket(key_id, model)`；惰性驱逐 `w.key_id == k` 改 `(w.key_id, w.model) == k`。
- `ctyun-stream-fix-proxy.py:370 tpm_admit(key_id, est)` — 签名改 `tpm_admit(key_id, est, model)`；`:385 est > TPM_LIMIT` 分支改为决策 3 的超大放行逻辑（budget = `tpm_limit_for(model)`）；`:391`、`:412` 的 `TPM_LIMIT` 改 `budget`；`:396`、`:405` 的 `_tpm_rejected[key_id]`/`_tpm_timeouts[key_id]` 改 `(key_id, model)` 键。
- `ctyun-stream-fix-proxy.py:424 tpm_settle(key_id, est, actual)` — 签名改 `tpm_settle(key_id, est, actual, model)`；`:433 TPM_BUCKETS.get(key_id)` 改 `(key_id, model)`。
- `ctyun-stream-fix-proxy.py:444 tpm_snapshot()` — bucket 条目加 `"model": model`；`config` 加 `"limit_by_model": TPM_LIMIT_BY_MODEL`；`:459`、`:465`、`:466`、`:467` 的 `key_id` 键改 `(key_id, model)`。
- `ctyun-stream-fix-proxy.py:1258 _proxy_relay()` — `:1262` 后 `model = extract_model(body)` 已在 `:1258` 提取，直接复用；`:1268` `tpm_admit(tpm_key, tpm_est)` 改 `tpm_admit(tpm_key, tpm_est, model)`；`:1327`、`:1373`、`:1433`、`:1471` 的 `tpm_settle(tpm_key, tpm_est, ...)` 均加 `model` 实参。
- `ctyun-stream-fix-proxy.test.py` — 新增 `TpmPerModelTest(unittest.TestCase)`（集成，复用 `make_scripted_upstream` :300 + `start_proxy` :414 extra_env + `post_sse_auth` :597 + `admin_get` :573 + `stderr_text` + `stop_fake_upstreams`）+ 纯函数单测（`load_proxy_module` :561 测 `_parse_tpm_limit_by_model`/`tpm_limit_for`/per-model admit/settle/snapshot）。`_tpm_cleanup` :953 无需改（clear 逻辑对二元组键透明）。

## Acceptance Criteria

- `python3 ctyun-stream-fix-proxy.test.py` 全绿（含既有 161 例无回归）。
- per-model 表解析：`_parse_tpm_limit_by_model("kimi:30000,glm:110000")` 返回 `{"kimi":30000,"glm":110000}`；畸形项（`"kimi"`、`"kimi:abc"`、`"kimi:-1"`）跳过并 log；空串返回 `{}`。
- kimi 桶独立预算：env `CTYUN_TPM_LIMIT=110000 CTYUN_TPM_LIMIT_BY_MODEL="kimi-k3-oc:1000"`，同 key 发 kimi 请求 est>1000 → 429；同 key 发 deepseek 请求 est>1000 但 ≤110000 → 200（证明桶隔离）。
- 超大空闲放行：env `CTYUN_TPM_LIMIT=50`，首发 est>50 请求 → 200（空闲放行）；窗口内再发同 key 请求 → 429（used>0 不再放行）。
- 超大忙时 429：env `CTYUN_TPM_LIMIT=50`，先发正常请求占预算，再发 est>50 请求 → 429（used>0）。
- 拒绝计数可见：est>limit 硬拒后 GET `/api/tpm_stats`，bucket `rejected` ≥1 且 `model` 字段正确。
- tpm_stats 形态：bucket 含 `model` 字段；config 含 `limit_by_model` dict。
- 无 Authorization 直通：无 auth 请求仍 200，stderr 无 TPM 字段。
- **部署与生产验证（主代理 DELIVER 阶段执行，非 implementer）**：`cp ctyun-stream-fix-proxy.py ~/.local/bin/` → `launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy` → ①kimi 流量 `result=body-err` 消失或减少（grep `~/.local/log/ctyun-fwd.err`）；②glm 超大请求 200 放行（grep `model=glm-5.3-oc` 无 `tpm-queue-full`）；③`curl http://192.168.5.234:7921/api/tpm_stats` 的 `rejected` 计数增长且 `buckets[].model` 字段存在。

## Risks

- **桶键膨胀**：(key_id, model) 组合数 > 原 key_id 数，`TPM_KEY_CAP=64` 可能更早触顶。惰性驱逐（窗口空且无排队）兜底，但高频多模型场景可能频繁驱逐/重建。可接受（重建代价仅一次 dict 插入）。
- **glm 低估超装**：ratio 0.25 对 glm 内容偏低（实测 settle 后 used 冲 119470>110k），存在超装窗口（60s 内上游可能真实超限）。由 settle 自校正兜底，方向与上游限流一致（宁可本地 429）。如实测恶化用 `CTYUN_TPM_LIMIT_BY_MODEL` 调低 glm 预算（不改代码）。
- **超大放行饥饿**：放行的超大请求（est>limit）会占满窗口，后续正常请求排队至窗口滚过（60s）。属预期（窗口空闲才放行，忙时仍 429），但客户端超大上下文重试循环可能反复占窗。观测 `/api/tpm_stats` 的 `rejected`/`timeouts` 增长可发现。
- **model=None 桶**：非 JSON body 或无 model 字段的请求归入 `(key_id, None)` 桶，预算用默认 `TPM_LIMIT`。与有 model 的同 key 请求隔离（不同桶），符合上游 per-model 配额语义。
- **锁序**：`tpm_admit` 内 `_safe_log_stderr`（`_parse_tpm_limit_by_model` 不在锁内调用，仅模块加载时一次）无锁序风险；`_tpm_rejected`/`_tpm_timeouts` 计数在 `TPM_LOCK` 内更新，与既有路径一致。

## Exclusions

- 不做 per-model ratio（`TPM_TOKEN_RATIO` 保持全局 0.25）。
- 不改 dashboard HTML/JS（`_DASHBOARD_SRC` 不动）；tpm_stats 仅 API 可见。
- 不做跨进程/持久化限流状态（重启清零可接受）。
- 不改 `classify_outcome` 既有分支与 `_KIND_CATEGORY` 映射；429 计数复用现有口径。
- 不动 plist / launchd 配置（部署仅 cp + kickstart）。
- 不处理无 `max_tokens` 时的输出侧精确预估；不引入 tokenizer 依赖。
- 客户端断连排队检测、按 key 公平出队——见前 episode Risks，明确不做。

## R31 Evidence

[R31-S1] 问题存在：三项残留均有生产现场记录

残留①（per-model 预算形同虚设）：kimi-k3-oc 本地 used 仅 ~31k 即遭上游 200 包 TPM 错误（body-err 观测抓到 4 次）：

```
$ grep -nE 'body-err.*kimi-k3-oc' ~/.local/log/ctyun-fwd.err
23785:REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23312 host=eaichat.ctyun.cn ttfb=972.7ms stream=1 outcome=body_error qwait=7865ms tpm=31038 ts=2026-09-30T19:08:13+0800
23790:REQ POST /chat/completions -> 200 dur=18.4s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23317 host=eaichat.ctyun.cn ttfb=637.1ms stream=1 outcome=body_error qwait=17355ms tpm=32375 ts=2026-09-30T19:10:03+0800
23791:REQ POST /chat/completions -> 200 dur=11.1s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23318 host=eaichat.ctyun.cn ttfb=630.9ms stream=1 outcome=body_error qwait=9413ms tpm=32375 ts=2026-09-30T19:10:17+0800
23794:REQ POST /chat/completions -> 200 dur=1.7s result=body-err filtered=0 model=kimi-k3-oc retried=0 retry_reason=- exc=- rid=r-23321 host=eaichat.ctyun.cn ttfb=707.1ms stream=1 outcome=body_error qwait=0ms tpm=33227 ts=2026-09-30T19:11:10+0800
```

残留②（超大请求硬拒）：2026-09-30 20:39 起 glm-5.3-oc 每 10-20s 一次真实硬拒（7+ 次，客户端超大上下文重试循环）：

```
$ grep -nE 'tpm-queue-full.*glm' ~/.local/log/ctyun-fwd.err
23884:REQ POST /chat/completions -> 429 dur=0.0s result=tpm-queue-full filtered=0 model=glm-5.3-oc retried=0 retry_reason=- exc=- rid=- host=- ttfb=- stream=- outcome=- qwait=0ms ts=2026-09-30T20:39:27+0800
23885:REQ POST /chat/completions -> 429 dur=0.0s result=tpm-queue-full filtered=0 model=glm-5.3-oc retried=0 retry_reason=- exc=- rid=- host=- ttfb=- stream=- outcome=- qwait=0ms ts=2026-09-30T20:39:29+0800
23886:REQ POST /chat/completions -> 429 dur=0.0s result=tpm-queue-full filtered=0 model=glm-5.3-oc retried=0 retry_reason=- exc=- rid=- host=- ttfb=- stream=- outcome=- qwait=0ms ts=2026-09-30T20:39:33+0800
```

残留③（拒绝计数盲区）：est>limit 硬拒发生在建桶之前（`tpm_admit:385` 在 `_tpm_get_bucket:389` 之前 return），`/api/tpm_stats` 的 `rejected` 不计数，只能 grep 日志 `result=tpm-queue-full`。

[R31-S2] 根因：per-key 单一预算 + est>limit 硬拒在建桶前

```
$ grep -nE 'def tpm_admit|est > TPM_LIMIT|_tpm_get_bucket|_tpm_rejected' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
370:def tpm_admit(key_id: str, est: int):
385:    if est > TPM_LIMIT:
386:        return ("full", 0)
389:        bucket = _tpm_get_bucket(key_id)
396:            _tpm_rejected[key_id] = _tpm_rejected.get(key_id, 0) + 1
```

`tpm_admit` 用全局 `TPM_LIMIT`（110k）做唯一预算（:385、:391、:412），kimi-k3-oc 上游实际配额 ~31k 时本地限流形同虚设；`est > TPM_LIMIT` 分支（:385）在 `_tpm_get_bucket`（:389）之前 return，硬拒请求不留桶、不计 `rejected`。
