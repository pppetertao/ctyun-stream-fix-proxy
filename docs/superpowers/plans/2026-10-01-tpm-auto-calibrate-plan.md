# PLAN — TPM 限流自动校准（探测实测 + 样本持久化 + 结构化启用建议 + 二选一预算设置）

> B0 骨架版：Header + Global Constraints + 卡清单（tier/文件/接口签名/验收/验证命令）已落定；
> 每卡卡体（完整生产代码 + 完整测试代码）由后续 B1..Bn 段逐卡填充，B-final 段跨全卡自查。

## Header

- **Spec（唯一需求来源）**：`docs/superpowers/specs/2026-10-01-tpm-auto-calibrate-design.md`
- **分支 / 工作树**：`fix/tpm-auto-calibrate`（工作树根 `.worktrees/tpm-auto-calibrate/`）
- **目标**：三个用户可见能力——① 主动校准探测（Dashboard 每模型"校准测试"按钮，线性加压实测阈值 → 推荐 = 阈值×0.9 千位向下）；② 被动分析增强（body-err 样本持久化跨重启 + `enabled_advice`/`source`/`probe` 结构化字段 + 无样本历史用量保守参考）；③ 二选一预算设置（「使用推荐值」/「自定义」，彻底删除多档 choices）。推荐优先级：探测实测 > body-err 样本 > 历史用量保守参考。
- **交付边界**：探测结果与 body-err 样本持久化进 `~/.local/etc/ctyun-stream-fix-proxy.json`；`GET /api/tpm_settings` 每模型 `recommend` 改为 `{recommended, samples, hint?, enabled_advice, source, probe}`（无 `choices`）；新增 `/api/tpm_calibrate`（POST 启动 / GET 轮询）与 `/api/tpm_calibrate_abort`（POST）；校准结果不自动写 `TPM_MODEL_BUDGETS`。
- **验收命令**（worktree 根执行）：
  ```
  /usr/bin/python3 ctyun-stream-fix-proxy.test.py
  ```
  必须全绿（0 failure / 0 error）。Baseline 实测 **248** 用例全绿（spec 文案"现有 118 用例"为旧口径，以当前实际 248 为准；本次新增 + 3 个既有用例更新后总数 ≥ 248）。
- **每卡验证命令**：全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（卡边界绿，跨卡不累积红；单卡自测可加 `-k` 式类名过滤：`/usr/bin/python3 -c "import ctyun_stream_fix_proxy_test as t; import unittest; unittest.main"` 不可用——测试文件非包结构，直接全量跑）。

## Global Constraints

1. **TDD 铁律**：每卡先写失败测试（红）→ 最小实现（绿）→ 全量验证。无失败测试不写生产代码。例外（本卡集不适用）：配置文件/构建脚本/文档/纯样式/spike。
2. **Baseline 豁免清单**：baseline 2026-10-01 实测 248 用例全绿（122s，`Ran 248 tests ... OK`），**豁免清单为空**——不存在"baseline 就红"的用例；任何新增红（含既有 3 个 choices 用例因去 choices 而红的中间态）归本 episode 负责，必须在同一卡内把既有测试定点更新翻绿，不允许跨卡留红。
3. **无新依赖**：纯 stdlib；生产代码全部进主文件 `ctyun-stream-fix-proxy.py`，测试全部进 `ctyun-stream-fix-proxy.test.py`；不引入数据库/独立持久化文件（复用 `PERSIST_PATH`）。
4. **空 catch 禁则**：`except` 必须显式分类或自证三要素（① 吞掉什么命名异常 ② 为何无其他路径可达 ③ `try` 限一条语句）。校准发送器 `except (OSError, http.client.HTTPException)` 必须显式分类为 `upstream_error` 状态 + `_safe_log_stderr` 留痕；`_calibrate_loop` 顶层 `except Exception as exc` 写入 `_CALIBRATE_STATE["error"]` + `_safe_log_stderr` 后复位 `running=False`，不静默吞。
5. **锁纪律**：`CALIBRATE_LOCK`（新）/`_AUTH_LOCK`（新或复用 `_CFG_LOCK` 短持）/`LOG_LOCK`/`STATS_LOCK`/`_CFG_LOCK` 全部短持一次取副本、不嵌套；`save_stats_counters` 已持 `_CFG_LOCK` 时新样本快照必须同 `set_*` 锁外调用纪律（:1196-1198 注释）。
6. **Commit 规约**：每卡一个 Conventional Commit（`feat:`/`test:`/`refactor:`/`docs:`），单逻辑单 commit；commit 动作由主代理执行，卡内代码保持可独立 commit 边界（不留半成品接口）。
7. **测试基建复用**：集成测试复用 `make_fake_upstream`（test:268）/ `make_scripted_upstream`（test:325）/ `start_proxy(upstream_port, proxy_port, extra_env=None, seed_persist=None)`（test:480，实测锚点）/ `admin_get`（test:639）/ `admin_post`（test:651）/ `AdminIntegrationTest`（test:2899）/ `stop_proxy`（test:436）。动态数据渲染禁 `innerHTML`，必须 `textContent`（沿用 test:3011-3012 门禁）。
8. **规模与验证**：每卡自带完整生产代码 + 完整测试代码 + 验证命令；No Placeholders（禁止"实现 X 逻辑"式指令替代代码）；每卡验证贴实际 stdout/exit code。

## 锚点核对记录（B0 实测，2026-10-01）

主文件 3493 行 / 测试文件 6100 行。spec 锚点逐条 grep/Read 核对结果：

| spec 锚点 | 实测 | 状态 |
|---|---|---|
| 主文件 TPM 常量区 :101-114 | :101-106 常量、:108-114 per-model 预算 seed | 命中（新区插 :114 之后） |
| `estimate_request_tokens` :437 | :437 `def estimate_request_tokens` | 精确命中 |
| `tpm_admit` :516 | :516 | 精确命中 |
| `tpm_body_err_samples` :756 | :756 | 精确命中 |
| `tpm_budget_recommend` :774；无样本三档 :784-793；有样本 choices :796-804 | 全部精确命中 | 精确命中 |
| `tpm_settings_snapshot` :808；`with LOG_LOCK` 取行 :816-817 | :808、:816-817 | 精确命中 |
| 新增插入点"约 :834 之后" | :834-835 空行，:836 `class _EmptyStream` | 命中（插 :834-836 之间） |
| `save_stats_counters` json.dump 载荷 :1098-1107 | def 在 :1077；json.dump 块 :1099-1106、`os.replace` :1107 | 漂移 +1（改载荷锚 :1099-1106） |
| 容错加载区 :1121-1188；`load_tpm_model_budgets` :1139-1158 | 精确命中 | 命中 |
| `record_error_event` :680 / `_record_request` :1484 | 精确命中 | 命中 |
| `_proxy` :1747 / `_proxy_relay` :1778 / tpm_admit hook :1784-1805 | 精确命中 | 命中 |
| `_open_upstream` :2038 | 精确命中 | 命中 |
| `_relay_sse` :2069 / `_relay_buffered` :2191 | 精确命中 | 命中 |
| `_log` :2242 | 精确命中 | 命中 |
| `_probe_once` :1327 / `_probe_loop` :1365 | 精确命中 | 命中 |
| `write_allowed` :1444；do_POST 鉴权同款 :2373-2378 | 精确命中 | 命中 |
| `do_GET` 分发 :2271-2357 / `do_POST` 分发 :2359-2443 | 精确命中 | 命中 |
| TPM 设置卡片 HTML :2567-2575 | :2567-2575 `<section class="card" id="tpm-settings-card">` | 精确命中 |
| `renderTpmSettings` :3288-3334；select 块 :3309-3325；label 拼接 :3317-3318 | def :3288、函数体 :3288-3336、select 块 :3309-3325、label 拼接 :3317-3318 | 漂移 +2（函数体止 :3336） |
| `#tpm-save` 监听 :3337-3347 | 精确命中 | 命中 |
| `DASHBOARD_HTML` :3373 | 精确命中 | 命中 |
| `main()` :3390；probe daemon :3475-3477 | 精确命中 | 命中 |
| `classify_outcome` :352；body_error 分类 :368-370、:1994 | :352、:368-370、:1994 `body_error = body_has_error(parsed)` | 精确命中 |
| `_prune_daily` :1052 | 精确命中 | 命中 |
| `_CFG_LOCK` 持锁段 :1089-1093 | 精确命中 | 命中 |
| CTYUN_TPM_LIMIT_BY_MODEL env 解析 :68 | :68 `def _parse_tpm_limit_by_model` | 精确命中 |
| 测试 `make_fake_upstream` :268 / `make_scripted_upstream` :325 | 精确命中 | 命中 |
| 测试 `start_proxy` :498 | **:480** | **漂移 -18（实测锚 :480，含 `seed_persist` 参数）** |
| 测试 `AdminIntegrationTest` :2899；XSS 门禁 :3011-3012 | 精确命中 | 命中 |
| 测试 `test_tpm_budget_recommend_with_samples` :1376-1396；choices 断言 :1393-1395 | 精确命中 | 命中 |
| 测试 `test_tpm_budget_recommend_no_samples` :1398-1405；choices 断言 :1404-1405 | 精确命中 | 命中 |
| 测试 `test_model_list_auto_generated` choices 断言 :5589-5590 | 精确命中 | 命中 |

**spec 内部缺口（B1..Bn 填码时依 AC 文本推导，不臆造）**：
- spec 引用的"设计决策 7/8/10"未出现在 spec 正文。`enabled_advice` 结构、`_CALIBRATE_STATE` 结构、校准请求标记策略依 AC 推导：AC5 定 state/progress 形态（`{"running", "model", "progress": {"batch", "consumed"}, "result"}`），AC1 定 result 形态（`{"threshold", "batches", "consumed"}`），AC2/AC3 定 `enabled_advice["suggest"]` 布尔。B1..Bn 不得引入 spec 未提及字段。
- spec :3373 处称新 UI 落在 `_DASH_SECTIONS_STATIC` 与 `_DASH_JS_CORE` 段内；实测 `renderTpmSettings`/`#tpm-save` 在 `_DASH_JS_V2`（:3013 起），HTML 卡片在 `_DASH_SECTIONS_STATIC`（:2567-2575）。UI 卡的 JS 改动落 `_DASH_JS_V2`。
- 持久化样本的**实时追加点** spec 未给锚：B1 卡 2 落定——真实流量 body-err 判定处（:1994 附近 `body_error = body_has_error(parsed)` 判定为真且有可用 usage 数值时）追加 `(model, tpm_value, ts)`，口径与 `tpm_body_err_samples` 日志解析的 `tpm=` 一致；校准请求绝不走该路径（R2 直连）。

## 任务卡清单（8 卡，全 A；卡体由 B1..Bn 填充）

### 卡 1 — TPM 校准常量 + 样本/探测持久化层（save/load + prune）

- **涉及文件**：`ctyun-stream-fix-proxy.py`（:20 加 `import math`、:107 后插入校准常量、:114 后插入内存态 dict、:1052-1075 间插入 prune 函数、:1089-1093 扩展 `save_stats_counters` `_CFG_LOCK` 块、:1099-1106 扩展 `json.dump` 载荷、:1158 后插入 load/record 函数）；`ctyun-stream-fix-proxy.test.py`（`ProxyDashboardUnitTest` 新增 8 个测试方法）
- **tier**：A — 全部常量取值 / env seam / load/save/prune/re 函数逻辑与测试均写死，无运行时数据依赖。
- **覆盖 AC**：AC10（容错 load + prune 7 形态全覆盖）；R7（持久化膨胀上限 prune 逻辑）。
- **笔记**：spec AC10 文本"返回 []"与 Files 段"风格仿 load_tpm_model_budgets（返回 dict）"矛盾；取 Files 段 dict 形态——调用方 main() 用 `dict.update()` 依赖 dict、PERSISTED_BODY_ERR_SAMPLES 自身即 dict。本卡测试断言 `load_tpm_body_err_samples(path)` 返回 `{}`（空 dict），与 Files 锚一致。

#### 生产代码（ctyun-stream-fix-proxy.py）

**插入点 A**：在 `import json`（:20）之后插入 `import math`：
```
import math
```

**插入点 B**：在 `TPM_KEY_CAP`（:106）之后、per-model 预算注释（:108）之前插入校准常量块：
```
# TPM 校准常量（主动校准探测；全部 env seam 以便测试加速）
TPM_CALIBRATE_INPUT_BYTES = int(os.environ.get("CTYUN_CALIBRATE_INPUT_BYTES", "240000"))
TPM_CALIBRATE_STEP_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_STEP_TOKENS", "5000"))
TPM_CALIBRATE_MAX_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_MAX_TOKENS", "1"))
TPM_CALIBRATE_BATCH_GAP_S = float(os.environ.get("CTYUN_CALIBRATE_BATCH_GAP_S", "2.0"))
TPM_CALIBRATE_MAX_DURATION_S = float(os.environ.get("CTYUN_CALIBRATE_MAX_DURATION_S", "240"))
TPM_CALIBRATE_MIN_INTERVAL_S = float(os.environ.get("CTYUN_CALIBRATE_MIN_INTERVAL_S", "1800"))
TPM_CALIBRATE_HARD_CAP_TOKENS = int(os.environ.get("CTYUN_CALIBRATE_HARD_CAP_TOKENS", "400000"))
TPM_CALIBRATE_SETTLE_WAIT_S = float(os.environ.get("CTYUN_CALIBRATE_SETTLE_WAIT_S", "60"))
# 样本/探测结果持久化上限（R7：防持久化膨胀）
TPM_SAMPLES_MAX = int(os.environ.get("CTYUN_TPM_SAMPLES_MAX", "200"))
TPM_SAMPLES_RETENTION_DAYS = int(os.environ.get("CTYUN_TPM_SAMPLES_RETENTION_DAYS", "30"))
TPM_PROBE_RESULTS_MAX = 5  # 每模型探测结果最近保留条数（R7；无需 env seam）
```

**插入点 C**：在 `TPM_MODEL_BUDGETS: dict = {}`（:114）之后插入内存态 dict：
```
# 持久化 body-err 样本与探测结果内存态（决策 2 同款锁纪律：_CFG_LOCK 护写，
# 读侧无锁靠引用赋值原子；main() 启动由持久化加载回填，save_stats_counters 落盘）
PERSISTED_BODY_ERR_SAMPLES: dict = {}  # {model: [{"tpm": int, "ts": float}, ...]}
PROBE_RESULTS: dict = {}               # {model: [{"threshold": int, "ts": float, ...}, ...]}
```

**插入点 D**：在 `_safe_log_stderr` 函数体结束（:1074）、`def save_stats_counters`（:1077）之前插入 prune 函数：
```
def _prune_body_err_samples(samples: dict) -> None:
    """原地 prune 持久化 body-err 样本（R7；save 时执行，同 _prune_daily 模式）。

    逐模型：丢弃保留窗口（TPM_SAMPLES_RETENTION_DAYS 天）之外的条目；
    条数超 TPM_SAMPLES_MAX → 按 ts 升序保留最新 TPM_SAMPLES_MAX 条（最旧先弃）。
    非法键（非 str 模型名 / 空模型名）直接丢弃。
    """
    cutoff = time.time() - TPM_SAMPLES_RETENTION_DAYS * 86400
    for model in list(samples.keys()):
        if not isinstance(model, str) or not model:
            del samples[model]
            continue
        items = samples[model]
        if not isinstance(items, list):
            del samples[model]
            continue
        kept = [item for item in items
                if isinstance(item, dict)
                and isinstance(item.get("tpm"), int)
                and not isinstance(item.get("tpm"), bool)
                and item["tpm"] > 0
                and isinstance(item.get("ts"), (int, float))
                and not isinstance(item.get("ts"), bool)
                and math.isfinite(item["ts"])
                and item["ts"] >= max(cutoff, 0)]
        if len(kept) > TPM_SAMPLES_MAX:
            kept.sort(key=lambda item: item["ts"])
            kept = kept[-TPM_SAMPLES_MAX:]
        if kept:
            samples[model] = kept
        else:
            del samples[model]


def _prune_probe_results(results: dict) -> None:
    """原地 prune 探测结果（R7）：每模型只保留最近 TPM_PROBE_RESULTS_MAX 条（按 ts 升序截尾）。
    条目校验与 _prune_body_err_samples 同款；threshold int>0、ts 有限数值；模型名非空 str。
    """
    for model in list(results.keys()):
        if not isinstance(model, str) or not model:
            del results[model]
            continue
        items = results[model]
        if not isinstance(items, list):
            del results[model]
            continue
        kept = [item for item in items
                if isinstance(item, dict)
                and isinstance(item.get("threshold"), int)
                and not isinstance(item.get("threshold"), bool)
                and item["threshold"] > 0
                and isinstance(item.get("ts"), (int, float))
                and not isinstance(item.get("ts"), bool)
                and math.isfinite(item["ts"])
                and item["ts"] >= 0]
        if len(kept) > TPM_PROBE_RESULTS_MAX:
            kept.sort(key=lambda item: item["ts"])
            kept = kept[-TPM_PROBE_RESULTS_MAX:]
        if kept:
            results[model] = kept
        else:
            del results[model]
```

**插入点 E**：在 `save_stats_counters` 的 `with _CFG_LOCK:` 块末尾（:1093，`budgets = dict(TPM_MODEL_BUDGETS)` 之后）插入 prune+快照：
```
        _prune_body_err_samples(PERSISTED_BODY_ERR_SAMPLES)   # 内存态原地 prune（R7，同 _prune_daily）
        _prune_probe_results(PROBE_RESULTS)
        body_err_samples = {m: [dict(s) for s in items]
                            for m, items in PERSISTED_BODY_ERR_SAMPLES.items()}
        probe_results = {m: [dict(r) for r in items]
                         for m, items in PROBE_RESULTS.items()}
```

**插入点 F**：在 `json.dump` 调用（:1099-1106）中向载荷 dict 追加两个键。将 `"tpm_model_budgets": budgets,` 所在行改为三行：
```
                   "tpm_model_budgets": budgets,
                   "tpm_body_err_samples": body_err_samples,
                   "tpm_probe_results": probe_results,
```

**插入点 G**：在 `load_tpm_model_budgets` 函数结束（:1158 `return out`）、`def load_model_pricing`（:1161）之前插入两个 load 函数和两个 record 函数：
```
def load_tpm_body_err_samples(path: str) -> dict:
    """从持久化文件读 tpm_body_err_samples；缺/损坏/非 dict → {}。

    逐条校验（容错同 load_tpm_model_budgets 风格）：model 非空 str 且 ≤200 字符、
    键数 ≤32；样本条目需 tpm int>0、ts 有限数值 ≥0 且在保留窗口内；
    非法条目跳过不报错。校验后 prune（条数上限 + 保留窗口，R7）。
    """
    data = _load_persist_file(path)
    val = data.get("tpm_body_err_samples") if isinstance(data, dict) else None
    if not isinstance(val, dict):
        return {}
    out = {}
    cutoff = time.time() - TPM_SAMPLES_RETENTION_DAYS * 86400
    for model, items in val.items():
        if len(out) >= 32:
            break
        if not isinstance(model, str) or not model or len(model) > 200:
            continue
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            tpm = item.get("tpm")
            ts = item.get("ts")
            if not isinstance(tpm, int) or isinstance(tpm, bool) or tpm <= 0:
                continue
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            if not math.isfinite(ts) or ts < 0 or ts < cutoff:
                continue
            cleaned.append({"tpm": tpm, "ts": float(ts)})
        if len(cleaned) > TPM_SAMPLES_MAX:
            cleaned.sort(key=lambda item: item["ts"])
            cleaned = cleaned[-TPM_SAMPLES_MAX:]
        if cleaned:
            out[model] = cleaned
    return out


def load_tpm_probe_results(path: str) -> dict:
    """从持久化文件读 tpm_probe_results；缺/损坏/非 dict → {}。

    逐条校验同 load_tpm_body_err_samples 容错口径：threshold int>0、ts 有限数值 ≥0；
    保留额外标量字段（outcome/batches/consumed 等，未来兼容）。每模型保留最近
    TPM_PROBE_RESULTS_MAX 条（R7）。
    """
    data = _load_persist_file(path)
    val = data.get("tpm_probe_results") if isinstance(data, dict) else None
    if not isinstance(val, dict):
        return {}
    out = {}
    for model, items in val.items():
        if len(out) >= 32:
            break
        if not isinstance(model, str) or not model or len(model) > 200:
            continue
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            threshold = item.get("threshold")
            ts = item.get("ts")
            if not isinstance(threshold, int) or isinstance(threshold, bool) \
                    or threshold <= 0:
                continue
            if not isinstance(ts, (int, float)) or isinstance(ts, bool):
                continue
            if not math.isfinite(ts) or ts < 0:
                continue
            kept = {"threshold": threshold, "ts": float(ts)}
            for extra_key in ("outcome", "batches", "consumed"):
                value = item.get(extra_key)
                if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                    kept[extra_key] = value
            cleaned.append(kept)
        if len(cleaned) > TPM_PROBE_RESULTS_MAX:
            cleaned.sort(key=lambda item: item["ts"])
            cleaned = cleaned[-TPM_PROBE_RESULTS_MAX:]
        if cleaned:
            out[model] = cleaned
    return out


def record_tpm_body_err_sample(model, tpm_value, ts) -> None:
    """真实流量 body-err 判定为真时追加一条持久化样本（_CFG_LOCK 内 append）。

    仅接受 str 模型名（≤200 字符、非空）、int>0 tpm、有限数值 ts；非法入参直接忽略
    （调用点在观测路径，追加失败不得影响转发主流程——fail-open）。
    prune 在 save 时执行（R7），此处不 prune。
    """
    if not isinstance(model, str) or not model or len(model) > 200:
        return
    if not isinstance(tpm_value, int) or isinstance(tpm_value, bool) or tpm_value <= 0:
        return
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) \
            or not math.isfinite(ts):
        return
    with _CFG_LOCK:
        PERSISTED_BODY_ERR_SAMPLES.setdefault(model, []).append(
            {"tpm": tpm_value, "ts": float(ts)})


def record_tpm_probe_result(model, result) -> None:
    """校准完成时追加一条探测结果（_CFG_LOCK 内 append；卡 5 校准结束时调用）。

    result 为 calibrate_engine 返回 dict：threshold int>0、ts 有限数值、batches int、
    consumed int、outcome str；字段缺失/非法 → 忽略（fail-open，不打断校准收尾）。
    """
    if not isinstance(model, str) or not model or len(model) > 200:
        return
    if not isinstance(result, dict):
        return
    threshold = result.get("threshold")
    ts = result.get("ts")
    if not isinstance(threshold, int) or isinstance(threshold, bool) or threshold <= 0:
        return
    if not isinstance(ts, (int, float)) or isinstance(ts, bool) \
            or not math.isfinite(ts):
        return
    entry = {"threshold": threshold, "ts": float(ts)}
    for key in ("outcome", "batches", "consumed"):
        value = result.get(key)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            entry[key] = value
    with _CFG_LOCK:
        PROBE_RESULTS.setdefault(model, []).append(entry)
```

#### 测试代码（ctyun-stream-fix-proxy.test.py）

在 `ProxyDashboardUnitTest` 类（:715，`setUpClass` 加载模块）中新增以下 8 个测试方法：

（1）`test_tpm_calibrate_constants_defaults` — 默认值
```
    def test_tpm_calibrate_constants_defaults(self) -> None:
        mod = self.mod
        self.assertEqual(mod.TPM_CALIBRATE_INPUT_BYTES, 240000)
        self.assertEqual(mod.TPM_CALIBRATE_STEP_TOKENS, 5000)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_TOKENS, 1)
        self.assertEqual(mod.TPM_CALIBRATE_BATCH_GAP_S, 2.0)
        self.assertEqual(mod.TPM_CALIBRATE_MAX_DURATION_S, 240)
        self.assertEqual(mod.TPM_CALIBRATE_MIN_INTERVAL_S, 1800)
        self.assertEqual(mod.TPM_CALIBRATE_HARD_CAP_TOKENS, 400000)
        self.assertEqual(mod.TPM_CALIBRATE_SETTLE_WAIT_S, 60)
        self.assertEqual(mod.TPM_SAMPLES_MAX, 200)
        self.assertEqual(mod.TPM_SAMPLES_RETENTION_DAYS, 30)
        self.assertEqual(mod.TPM_PROBE_RESULTS_MAX, 5)
```

（2）`test_tpm_calibrate_constants_env_seam` — env seam（subprocess 验证）
```
    def test_tpm_calibrate_constants_env_seam(self) -> None:
        import subprocess
        code = (
            "import os; "
            "os.environ['CTYUN_CALIBRATE_INPUT_BYTES']='111'; "
            "os.environ['CTYUN_CALIBRATE_STEP_TOKENS']='222'; "
            "os.environ['CTYUN_CALIBRATE_MAX_TOKENS']='3'; "
            "os.environ['CTYUN_CALIBRATE_BATCH_GAP_S']='0.5'; "
            "os.environ['CTYUN_CALIBRATE_MAX_DURATION_S']='10'; "
            "os.environ['CTYUN_CALIBRATE_MIN_INTERVAL_S']='9'; "
            "os.environ['CTYUN_CALIBRATE_HARD_CAP_TOKENS']='999'; "
            "os.environ['CTYUN_CALIBRATE_SETTLE_WAIT_S']='7'; "
            "os.environ['CTYUN_TPM_SAMPLES_MAX']='4'; "
            "os.environ['CTYUN_TPM_SAMPLES_RETENTION_DAYS']='5'; "
            "import importlib.util, sys; "
            "spec = importlib.util.spec_from_file_location('m', %r); "
            "m = importlib.util.module_from_spec(spec); "
            "spec.loader.exec_module(m); "
            "print(m.TPM_CALIBRATE_INPUT_BYTES, m.TPM_CALIBRATE_STEP_TOKENS, "
            "m.TPM_CALIBRATE_MAX_TOKENS, m.TPM_CALIBRATE_BATCH_GAP_S, "
            "m.TPM_CALIBRATE_MAX_DURATION_S, m.TPM_CALIBRATE_MIN_INTERVAL_S, "
            "m.TPM_CALIBRATE_HARD_CAP_TOKENS, m.TPM_CALIBRATE_SETTLE_WAIT_S, "
            "m.TPM_SAMPLES_MAX, m.TPM_SAMPLES_RETENTION_DAYS, m.TPM_PROBE_RESULTS_MAX)"
            % PROXY_SCRIPT)
        out = subprocess.check_output([sys.executable, "-c", code], text=True,
                                      timeout=30).strip()
        self.assertEqual(out.split(),
                         ["111", "222", "3", "0.5", "10.0", "9.0", "999", "7.0",
                          "4", "5", "5"])
```

（3）`test_load_tpm_body_err_samples_fault_tolerant` — 缺键/损坏/非 dict/缺字段/非法 ts/tpm → `{}`/跳过
```
    def test_load_tpm_body_err_samples_fault_tolerant(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 缺文件
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 损坏 JSON
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(["not", "dict"], fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 顶层非 dict
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"other": 1}, fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 键缺失
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": [1, 2]}, fh)
        self.assertEqual(mod.load_tpm_body_err_samples(path), {})   # 值非 dict
        now = time.time()
        fresh = now - 60
        stale = now - 40 * 86400
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": {
                "kimi": [{"tpm": 31000, "ts": fresh},
                         {"tpm": 32000, "ts": fresh + 1},
                         {"tpm": 30000, "ts": stale},        # 超保留期 → 丢
                         {"tpm": "999", "ts": fresh},         # tpm 非 int → 丢
                         {"tpm": 28000},                       # 缺 ts → 丢
                         {"tpm": -5, "ts": fresh},             # tpm ≤0 → 丢
                         "garbage"],                           # 非 dict → 丢
                "": [{"tpm": 1, "ts": fresh}],                # 空 model → 丢
                "x" * 201: [{"tpm": 1, "ts": fresh}],         # model 超长 → 丢
            }}, fh)
        out = mod.load_tpm_body_err_samples(path)
        self.assertEqual(sorted(out.keys()), ["kimi"])
        self.assertEqual([s["tpm"] for s in out["kimi"]], [31000, 32000])
```

（4）`test_load_tpm_body_err_samples_count_cap` — 条数超 `TPM_SAMPLES_MAX` → prune 至上限
```
    def test_load_tpm_body_err_samples_count_cap(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        now = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_body_err_samples": {
                "m": [{"tpm": 1000 + i, "ts": now - 100 + i} for i in range(10)]}}, fh)
        orig = mod.TPM_SAMPLES_MAX
        mod.TPM_SAMPLES_MAX = 3
        self.addCleanup(setattr, mod, "TPM_SAMPLES_MAX", orig)
        out = mod.load_tpm_body_err_samples(path)
        self.assertEqual(len(out["m"]), 3)
        self.assertEqual([s["tpm"] for s in out["m"]], [1007, 1008, 1009],
                         "keep newest 3 by ts")
```

（5）`test_load_tpm_probe_results_fault_tolerant_and_prune` — 容错 + 5 条上限
```
    def test_load_tpm_probe_results_fault_tolerant_and_prune(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        now = time.time()
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"tpm_probe_results": {
                "kimi": [
                    {"threshold": 31000, "ts": now - 10,
                     "outcome": "rejected", "batches": 7, "consumed": 35123},
                    {"threshold": 32000, "ts": now - 5,
                     "outcome": "capped", "batches": 80, "consumed": 400123},
                    {"threshold": 0, "ts": now},              # threshold ≤0 → 丢
                    {"threshold": "x", "ts": now},             # 非 int → 丢
                ],
                "m2": [{"threshold": 1000 + i, "ts": now - 60 + i} for i in range(7)],
            }}, fh)
        out = mod.load_tpm_probe_results(path)
        self.assertEqual(sorted(out.keys()), ["kimi", "m2"])
        kim = out["kimi"]
        self.assertEqual([r["threshold"] for r in kim], [31000, 32000])
        self.assertEqual(kim[0]["outcome"], "rejected")
        self.assertEqual(kim[0]["batches"], 7)
        self.assertEqual(kim[0]["consumed"], 35123)
        self.assertEqual([r["threshold"] for r in out["m2"]], [1002, 1003, 1004, 1005, 1006],
                         "keep newest 5 of 7")
```

（6）`test_record_tpm_body_err_sample_appends_and_saves` — record → save → 重载读回 roundtrip
```
    def test_record_tpm_body_err_sample_appends_and_saves(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        mod.PERSISTED_BODY_ERR_SAMPLES.clear()
        self.addCleanup(mod.PERSISTED_BODY_ERR_SAMPLES.clear)
        mod.record_tpm_body_err_sample("kimi", 31038, time.time() - 1)
        mod.record_tpm_body_err_sample("kimi", 33227, time.time())
        mod.record_tpm_body_err_sample(None, 1, time.time())    # 非法 model → 忽略
        mod.record_tpm_body_err_sample("kimi", -1, time.time()) # 非法 tpm → 忽略
        self.assertEqual(len(mod.PERSISTED_BODY_ERR_SAMPLES["kimi"]), 2)
        mod.save_stats_counters(path)
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("tpm_body_err_samples", payload)
        saved = payload["tpm_body_err_samples"]["kimi"]
        self.assertEqual([s["tpm"] for s in saved], [31038, 33227])
        reloaded = mod.load_tpm_body_err_samples(path)
        self.assertEqual([s["tpm"] for s in reloaded["kimi"]], [31038, 33227])
```

（7）`test_record_tpm_probe_result_and_save_roundtrip` — record → save → 重载
```
    def test_record_tpm_probe_result_and_save_roundtrip(self) -> None:
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        mod.PROBE_RESULTS.clear()
        self.addCleanup(mod.PROBE_RESULTS.clear)
        mod.record_tpm_probe_result("kimi", {
            "threshold": 31000, "ts": time.time() - 1,
            "outcome": "rejected", "batches": 7, "consumed": 35123})
        mod.record_tpm_probe_result("kimi", {})  # 缺 threshold → 忽略
        self.assertEqual(len(mod.PROBE_RESULTS["kimi"]), 1)
        mod.save_stats_counters(path)
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("tpm_probe_results", payload)
        saved = payload["tpm_probe_results"]["kimi"]
        self.assertEqual(saved[0]["threshold"], 31000)
        self.assertEqual(saved[0]["outcome"], "rejected")
        reloaded = mod.load_tpm_probe_results(path)
        self.assertEqual(reloaded["kimi"][0]["threshold"], 31000)
```

（8）`test_save_prunes_stale_and_overcap_samples` — save 原地 prune + save prunes probe to 5
```
    def test_save_prunes_stale_and_overcap_samples(self) -> None:
        """save 原地 prune：超期条目 + 超上限条目被裁，内存态同步收缩。"""
        mod = self.mod
        tmp = tempfile.mkdtemp(prefix="ctyun-proxy-unit-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = os.path.join(tmp, "settings.json")
        mod.PERSISTED_BODY_ERR_SAMPLES.clear()
        mod.PROBE_RESULTS.clear()
        self.addCleanup(mod.PERSISTED_BODY_ERR_SAMPLES.clear)
        self.addCleanup(mod.PROBE_RESULTS.clear)
        now = time.time()
        # body-err：5 条近期 + 1 条过期
        for i in range(5):
            mod.record_tpm_body_err_sample("m", 1000 + i, now - 100 + i)
        mod.record_tpm_body_err_sample("m", 9999, now - 40 * 86400)
        # probe：7 条 → prune 至 5
        for i in range(7):
            mod.record_tpm_probe_result("m", {"threshold": 100 + i, "ts": now - 60 + i})
        orig = mod.TPM_SAMPLES_MAX
        mod.TPM_SAMPLES_MAX = 3
        self.addCleanup(setattr, mod, "TPM_SAMPLES_MAX", orig)
        mod.save_stats_counters(path)
        self.assertEqual([s["tpm"] for s in mod.PERSISTED_BODY_ERR_SAMPLES["m"]],
                         [1002, 1003, 1004])
        self.assertEqual([r["threshold"] for r in mod.PROBE_RESULTS["m"]],
                         [102, 103, 104, 105, 106])
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertEqual([s["tpm"] for s in payload["tpm_body_err_samples"]["m"]],
                         [1002, 1003, 1004])
```

#### 验证命令

`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（全量）
预期：248 + 8 = 256 test methods, OK（新增 8 个全绿，既有 248 不退化）。

### 卡 2 — `tpm_body_err_samples` 持久化优先 + 真实流量 body-err 追加点 + `main()` 加载

- **涉及文件**：`ctyun-stream-fix-proxy.py`（:756-771 `tpm_body_err_samples` 扩展 + 新增 `tpm_body_err_samples_persisted` 纯函数、:1959/:2005 两处真实流量追加点、:3407-3418 后 `main()` 加载）；`ctyun-stream-fix-proxy.test.py`（`ProxyDashboardUnitTest` 新增 2 个单元测试 + `AdminIntegrationTest` 新增 1 个 AC4 集成测试）
- **tier**：A — 纯函数扩展 + 追加点 + 加载逻辑均可写死；集成测试复用 `start_proxy(..., seed_persist=...)` 与 `make_fake_upstream(fail_200_error=True)`，无真机依赖。
- **覆盖 AC**：AC4（持久化样本跨重启集成级全链路）；AC11 部分（既有 `tpm_body_err_samples` 两用例因持久化初始为空走 LOG_RING 兜底，行为不变）。
- **卡边界说明**：`tpm_settings_snapshot`（:808）与 `tpm_budget_recommend` 的调用点**本次不改**——卡 2 结束时 `recommend` 仍是旧两参签名（卡 3 才扩展），持久化样本经 `tpm_body_err_samples` 内部优先读取 `PERSISTED_BODY_ERR_SAMPLES` 生效，`GET /api/tpm_settings` 即反映持久化值。snapshot 显式传 samples/probe/usage 快照（spec :33 的改造）由**卡 3** 完成。

#### 生产代码（ctyun-stream-fix-proxy.py）

**插入点 A**：在 `def tpm_body_err_samples`（:756）之前插入纯函数：
```
def tpm_body_err_samples_persisted(samples: dict, model: str) -> list:
    """从持久化样本 dict 提取指定模型的 tpm 值列表（决策 4 实证样本源的持久化优先来源）。

    样本条目为 {"tpm": int, "ts": float}（load_tpm_body_err_samples 产出形态）；
    tpm 非法（非 int / bool / ≤0）/条目非 dict 跳过。纯函数：无锁无 IO。
    """
    items = samples.get(model)
    if not isinstance(items, list):
        return []
    out = []
    for item in items:
        if not isinstance(item, dict):
            continue
        tpm = item.get("tpm")
        if isinstance(tpm, int) and not isinstance(tpm, bool) and tpm > 0:
            out.append(tpm)
    return out
```

**插入点 B**：整体替换 `tpm_body_err_samples`（:756-771）为：
```
def tpm_body_err_samples(lines: list, model: str) -> list:
    """提取指定模型 body-err 行的 tpm= 值：持久化样本优先，LOG_RING 解析兜底。

    持久化优先：PERSISTED_BODY_ERR_SAMPLES（_CFG_LOCK 护写）有该模型样本时直接
    返回其 tpm 列表（跨重启保留的实证证据 > 本次进程日志）；无持久化样本才解析
    LOG_RING 行（进程内新样本，重启丢失前的兜底）。
    正则锚定字段序（REQ 行 result 在 model 前、tpm 在 model 后，见 _log）：
    `result=body-err\\b.*\\bmodel=X\\b.*\\btpm=(\\d+)`；不匹配/无 tpm 字段的行跳过。
    纯函数：无锁（读侧靠引用赋值原子）无 IO。
    """
    persisted = tpm_body_err_samples_persisted(PERSISTED_BODY_ERR_SAMPLES, model)
    if persisted:
        return persisted
    pattern = re.compile(r"result=body-err\b.*\bmodel=%s\b.*\btpm=(\d+)" % re.escape(model))
    out = []
    for line in lines:
        if not isinstance(line, str):
            continue
        match = pattern.search(line)
        if match:
            out.append(int(match.group(1)))
    return out
```

**插入点 C（真实流量追加点，非流式分支）**：在 `_proxy_relay` 非流式分支 :2005（`tpm_final_used = total`，settle 块末尾）与 :2006（`outcome = classify_outcome(...)`）之间插入：
```
            # 持久化 body-err 实证样本（决策 6）：真实流量判定 body-err 且 tpm 结算值
            # 有效时追加；校准请求走 _calibrate_send 直连不经 _proxy_relay（R2），
            # 绝不污染样本源。record 内部对 None/非法值 fail-open 忽略。
            if body_error and model:
                record_tpm_body_err_sample(model, tpm_final_used, time.time())
```

**插入点 D（真实流量追加点，流式分支）**：在 `_proxy_relay` 流式分支 :1959（settle 块末尾）与 :1960（`self._log(...)`）之间插入：
```
            # 持久化 body-err 实证样本（同非流式分支；body_error 由 _body_err_line 判定）
            if body_error and model:
                record_tpm_body_err_sample(model, tpm_final_used, time.time())
```

**插入点 E（main() 加载）**：在 `main()` 的预算装载块（:3407-3418，`else: TPM_MODEL_BUDGETS = {"kimi-k3-oc": 30000}`）之后、`# v2 P3：model_pricing 装载`（:3419）之前插入：
```
    # 持久化 body-err 样本与探测结果装载（决策 6：实证证据跨重启保留）
    PERSISTED_BODY_ERR_SAMPLES.clear()
    PERSISTED_BODY_ERR_SAMPLES.update(load_tpm_body_err_samples(PERSIST_PATH))
    PROBE_RESULTS.clear()
    PROBE_RESULTS.update(load_tpm_probe_results(PERSIST_PATH))
```

#### 测试代码（ctyun-stream-fix-proxy.test.py）

**（1）`ProxyDashboardUnitTest` 新增**：

`test_tpm_body_err_samples_persisted` — 纯函数提取 + 非法条目跳过：
```
    def test_tpm_body_err_samples_persisted(self) -> None:
        mod = self.mod
        samples = {
            "kimi-k3-oc": [{"tpm": 31038, "ts": 1.0}, {"tpm": 33227, "ts": 2.0}],
            "glm-5.3-oc": [{"tpm": 999, "ts": 1.0}, {"tpm": "bad", "ts": 2.0},
                            {"tpm": -1, "ts": 3.0}, "garbage", None],
        }
        self.assertEqual(mod.tpm_body_err_samples_persisted(samples, "kimi-k3-oc"),
                         [31038, 33227])
        self.assertEqual(mod.tpm_body_err_samples_persisted(samples, "glm-5.3-oc"),
                         [999])
        self.assertEqual(mod.tpm_body_err_samples_persisted(samples, "absent"), [])
        self.assertEqual(mod.tpm_body_err_samples_persisted({}, "kimi-k3-oc"), [])
```

`test_tpm_body_err_samples_persisted_priority_over_log_ring` — 持久化优先 / LOG_RING 兜底：
```
    def test_tpm_body_err_samples_persisted_priority_over_log_ring(self) -> None:
        mod = self.mod
        self.addCleanup(mod.PERSISTED_BODY_ERR_SAMPLES.clear)
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=777 ts=T",
        ]
        mod.PERSISTED_BODY_ERR_SAMPLES["kimi-k3-oc"] = [{"tpm": 31038, "ts": 1.0}]
        self.assertEqual(mod.tpm_body_err_samples(lines, "kimi-k3-oc"), [31038],
                         "persisted sample must take priority over LOG_RING")
        mod.PERSISTED_BODY_ERR_SAMPLES.clear()
        self.assertEqual(mod.tpm_body_err_samples(lines, "kimi-k3-oc"), [777],
                         "without persisted samples LOG_RING parse is the fallback")
```

**（2）`AdminIntegrationTest`（:2899）新增 AC4 集成测试**：

`test_body_err_samples_persist_across_restart`：
```
    def test_body_err_samples_persist_across_restart(self) -> None:
        """AC4：seed_persist 预写样本 → /api/tpm_settings 反映（recommended 按
        min×0.9 千位向下）；真实 body-err 流量 → 样本增加；SIGTERM → 持久化文件
        含新样本（save_stats_counters 原子写，跨重启保留）。"""
        stop_proxy(self.proc)
        stop_fake_upstreams()
        seed_ts = time.time() - 60
        self.proc = start_proxy(
            self.upstream_port, self.proxy_port,
            seed_persist={"tpm_body_err_samples": {
                "kimi-k3-oc": [{"tpm": 31000, "ts": seed_ts}]}})
        try:
            _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
            snap = json.loads(body_bytes.decode("utf-8"))
            by_name = {m["name"]: m for m in snap["models"]}
            self.assertIn("kimi-k3-oc", by_name,
                          "seeded model must appear in tpm_settings")
            rec = by_name["kimi-k3-oc"]["recommend"]
            self.assertEqual(rec["samples"], 1,
                             "persisted sample must be reflected, got %r" % rec)
            self.assertEqual(rec["recommended"], 27000,
                             "31000*0.9=27900 -> 千位向下 27000, got %r" % rec)
            # 切到 body-err 假上游并触发真实 body-err 流量（带 Authorization）
            err_upstream = make_fake_upstream(False, fail_200_error=True)
            status, _ = admin_post(
                self.proc.admin_port, "/api/config",
                json.dumps({"upstream_base": "http://127.0.0.1:%d" % err_upstream}
                           ).encode("utf-8"))
            self.assertEqual(status, 200)
            conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port,
                                              timeout=10)
            req_body = b'{"model":"kimi-k3-oc","messages":[{"role":"user","content":"hi"}]}'
            conn.request("POST", "/v1/chat/completions", body=req_body,
                         headers={"Content-Type": "application/json",
                                  "Authorization": "Bearer test-token"})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            resp.read()
            conn.close()
            _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
            snap = json.loads(body_bytes.decode("utf-8"))
            by_name = {m["name"]: m for m in snap["models"]}
            rec = by_name["kimi-k3-oc"]["recommend"]
            self.assertEqual(rec["samples"], 2,
                             "body-err traffic must append a sample, got %r" % rec)
        finally:
            # SIGTERM → save_stats_counters 落盘（含新样本）
            self.proc.send_signal(signal.SIGTERM)
            self.proc.wait(timeout=10)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            payload = json.load(fh)
        self.assertIn("tpm_body_err_samples", payload,
                      "persisted file must carry tpm_body_err_samples")
        saved = payload["tpm_body_err_samples"]["kimi-k3-oc"]
        self.assertEqual(len(saved), 2, "SIGTERM flush must persist the new sample")
        self.assertEqual(saved[0]["tpm"], 31000)
        self.assertGreater(saved[1]["tpm"], 0)
        self.assertGreater(saved[1]["ts"], saved[0]["ts"])
```

#### 验证命令

`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（全量）
预期：256 + 3 = 259 test methods, OK（新增 3 个全绿：2 单元 + 1 AC4 集成；既有 256 不退化——`tpm_body_err_samples` 两处既有用例因 `PERSISTED_BODY_ERR_SAMPLES` 初始为空走 LOG_RING 兜底，行为不变）。

### 卡 3 — `tpm_budget_recommend` 去 choices + 三源优先级 + `enabled_advice`/`source`/`probe` + snapshot 调用点改造

- **涉及文件**：`ctyun-stream-fix-proxy.py`（:756 前新增 `tpm_usage_peak` 纯函数、:774-805 函数体重写、:808-833 `tpm_settings_snapshot` 调用点改造——:816-817 内同时取持久化样本与探测结果快照、:831 recommend 调用点传 samples/probe/usage 显式快照）；`ctyun-stream-fix-proxy.test.py`（定点更新 3 个既有测试 + `ProxyDashboardUnitTest` 新增 6 个单元测试）
- **tier**：A — 纯函数重构 + snapshot 锁纪律改造，所有优先级判定与返回结构写死。
- **覆盖 AC**：AC2（probe 推荐 + 无 choices）；AC3（三源优先级三条全路径）；AC12①（有样本路径无 choices）、AC12②（snapshot 每模型 recommend 不含 choices）；AC11（两参调用行为不变除 choices 键删除——回归通过 `test_tpm_budget_recommend_with_samples` 更新后的 recommended/samples/hint 断言验实）。

#### 生产代码（ctyun-stream-fix-proxy.py）

**插入点 A**：在 `tpm_body_err_samples`（:771）之后、`tpm_budget_recommend`（:774）之前插入纯函数 `tpm_usage_peak`：
```
def tpm_usage_peak(lines: list, model: str) -> int | None:
    """从 REQ 行列表提取指定模型的历史 tpm= 峰值（usage 保守参考源，决策 4 三源之末）。

    正则同 tpm_body_err_samples 字段序：`model=X\\b.*\\btpm=(\\d+)`（不限 result）；
    无匹配 → None。纯函数：无锁无 IO。
    """
    pattern = re.compile(r"model=%s\b.*\btpm=(\d+)" % re.escape(model))
    peak = None
    for line in lines:
        if not isinstance(line, str):
            continue
        match = pattern.search(line)
        if match:
            value = int(match.group(1))
            if peak is None or value > peak:
                peak = value
    return peak
```

**插入点 B**：整段替换 `tpm_budget_recommend`（:774-805）：
```
def tpm_budget_recommend(model: str, lines: list, samples=None, probe=None,
                         usage=None) -> dict:
    """预算推荐（决策 4 v2）：三源优先级 探测实测 > body-err 样本 > 历史用量。

    - probe 有效（dict 且 threshold int>0）→ source="probe"，recommended =
      阈值×0.9 千位向下（最小 1000），enabled_advice.suggest=True。
    - samples 非空 → source="body_err"，recommended = min×0.9 千位向下（最小 1000），
      suggest=True。samples 参数为 None 时回退 tpm_body_err_samples(lines, model)
      ——该函数内部已持久化样本优先、LOG_RING 兜底。
    - usage 有效（int>0）→ source="usage_estimate"，recommended = 峰值千位向下
      （保守参考，最小 1000），suggest=False（无拒绝证据不主动建议启用）。
      usage 参数为 None 时回退 tpm_usage_peak(lines, model)。
    - 全无 → recommended=None + hint + source="none"，suggest=False。
    返回 dict 无 choices 键（二选一预算设置的硬需求，spec AC12）。
    纯函数：无锁无 IO（TPM_LIMIT 只读）。
    """
    threshold = None
    if isinstance(probe, dict):
        value = probe.get("threshold")
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            threshold = value
    if threshold is not None:
        return {
            "recommended": max(1000, (threshold * 9 // 10) // 1000 * 1000),
            "samples": 0,
            "enabled_advice": {
                "suggest": True,
                "reason": "探测实测：上游拒绝阈值 %d tokens（×0.9 推荐）" % threshold,
            },
            "source": "probe",
            "probe": probe,
        }
    if samples is None:
        samples = tpm_body_err_samples(lines, model)
    if samples:
        base = min(samples)
        recommended = max(1000, (base * 9 // 10) // 1000 * 1000)
        return {
            "recommended": recommended,
            "samples": len(samples),
            "enabled_advice": {
                "suggest": True,
                "reason": "上游拒绝实证：%d 条样本，min×0.9 推荐" % len(samples),
            },
            "source": "body_err",
            "probe": probe,
        }
    peak = usage if isinstance(usage, int) and not isinstance(usage, bool) \
        and usage > 0 else tpm_usage_peak(lines, model)
    if peak:
        return {
            "recommended": max(1000, peak // 1000 * 1000),
            "samples": 0,
            "enabled_advice": {
                "suggest": False,
                "reason": "仅历史用量峰值 %d 参考，无拒绝证据" % peak,
            },
            "source": "usage_estimate",
            "probe": probe,
        }
    return {
        "recommended": None,
        "samples": 0,
        "hint": "无上游拒绝证据，建议不限流",
        "enabled_advice": {"suggest": False, "reason": "无可用证据"},
        "source": "none",
        "probe": probe,
    }
```

**插入点 C**：改造 `tpm_settings_snapshot`（:808-833）。将 `with _CFG_LOCK:` 块（:822-823）内仅取 `budgets` 扩展为三份快照；将 :831 的 `tpm_budget_recommend(name, lines)` 调用改为显式四参。

原 :822-823：
```
    with _CFG_LOCK:
        budgets = dict(TPM_MODEL_BUDGETS)
```
替换为：
```
    with _CFG_LOCK:
        budgets = dict(TPM_MODEL_BUDGETS)
        persist_samples = dict(PERSISTED_BODY_ERR_SAMPLES)
        probe_results = dict(PROBE_RESULTS)
```

原 :826-832 的 models 构建循环体**整体替换**（:826-832，含 `for name in sorted(...):` 到 `return` 之间的全部 7 行）：
原代码：
```
    models = []
    for name in sorted(n for n in names if n):
        models.append({
            "name": name,
            "enabled": name in budgets,
            "budget": budgets.get(name),
            "recommend": tpm_budget_recommend(name, lines),
        })
    return {"models": models, "default_limit": TPM_LIMIT}
```
替换为：
```
    models = []
    for name in sorted(n for n in names if n):
        persisted = tpm_body_err_samples_persisted(persist_samples, name)
        if not persisted:
            persisted = tpm_body_err_samples(lines, name)
        probe_items = probe_results.get(name)
        probe = probe_items[-1] if probe_items else None
        usage_peak = tpm_usage_peak(lines, name)
        models.append({
            "name": name,
            "enabled": name in budgets,
            "budget": budgets.get(name),
            "recommend": tpm_budget_recommend(name, lines, samples=persisted,
                                              probe=probe, usage=usage_peak),
        })
    return {"models": models, "default_limit": TPM_LIMIT}
```

#### 测试代码（ctyun-stream-fix-proxy.test.py）

**（A）定点更新 3 个既有测试**：

*更新 1 — `test_tpm_budget_recommend_with_samples`（:1389-1396）*：
将 :1389 `rec = mod.tpm_budget_recommend(...)` 之后到 :1396 的 choices 断言替换为：
```
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines)
        # min=31038 → 31038*9//10=27934 → //1000*1000=27000
        self.assertEqual(rec["recommended"], 27000)
        self.assertEqual(rec["samples"], 3)
        self.assertNotIn("choices", rec)
        self.assertEqual(rec["source"], "body_err")
        self.assertIs(rec["enabled_advice"]["suggest"], True)
        self.assertNotIn("hint", rec)
```

*更新 2 — `test_tpm_budget_recommend_no_samples`（:1400-1405）*：
将 :1400 `rec = mod.tpm_budget_recommend(...)` 之后到 :1405 替换为：
```
        rec = mod.tpm_budget_recommend("glm-5.3-oc", [])
        self.assertIsNone(rec["recommended"])
        self.assertEqual(rec["samples"], 0)
        self.assertIn("无上游拒绝证据", rec["hint"])
        self.assertNotIn("choices", rec)
        self.assertIs(rec["enabled_advice"]["suggest"], False)
        self.assertEqual(rec["source"], "none")
```

*更新 3 — `test_model_list_auto_generated`（:5589-5590）*：
将 :5589-5590 的两行 choices 断言替换为：
```
        recommend = by_name["deepseek-v4-pro-0813-oc"]["recommend"]
        self.assertNotIn("choices", recommend)
        self.assertIn("enabled_advice", recommend)
        self.assertIn("source", recommend)
```

**（B）`ProxyDashboardUnitTest`（setUpClass 加载模块，:715）新增 6 个测试方法**：

（1）`test_tpm_usage_peak` — 纯函数峰值提取
```
    def test_tpm_usage_peak(self) -> None:
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=12000 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=47850 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=deepseek-v4-pro-0813-oc retried=0 rid=r-3 host=h ttfb=1.0ms "
            "stream=1 outcome=ok qwait=0ms tpm=99999 ts=T",
            "garbage", None,
        ]
        self.assertEqual(mod.tpm_usage_peak(lines, "kimi-k3-oc"), 47850)
        self.assertEqual(mod.tpm_usage_peak(lines, "deepseek-v4-pro-0813-oc"),
                         99999)
        self.assertIsNone(mod.tpm_usage_peak(lines, "absent"))
        self.assertIsNone(mod.tpm_usage_peak([], "kimi-k3-oc"))
```

（2）`test_tpm_budget_recommend_probe_source`（AC2）
```
    def test_tpm_budget_recommend_probe_source(self) -> None:
        """AC2：probe 有效 → recommended = 阈值×0.9 千位向下、source="probe"、
        无 choices。"""
        mod = self.mod
        probe = {"threshold": 31000, "ts": time.time() - 10,
                 "outcome": "rejected", "batches": 7, "consumed": 35123}
        rec = mod.tpm_budget_recommend("m", [], samples=None, probe=probe,
                                       usage=None)
        self.assertEqual(rec["recommended"], 27000)
        self.assertEqual(rec["source"], "probe")
        self.assertNotIn("choices", rec)
        self.assertIs(rec["enabled_advice"]["suggest"], True)
        self.assertEqual(rec["probe"]["threshold"], 31000)
        self.assertEqual(rec["samples"], 0)
        self.assertNotIn("hint", rec)
```

（3）`test_tpm_budget_recommend_source_priority`（AC3 三路径全遍历）
```
    def test_tpm_budget_recommend_source_priority(self) -> None:
        """AC3：探测实测 > body-err 样本 > 历史用量。"""
        mod = self.mod
        probe = {"threshold": 31000, "ts": time.time() - 10}
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=body-err filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=body_error qwait=0ms tpm=31038 ts=T",
        ]
        # 三源同给 → probe 优先
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines, samples=[31038],
                                       probe=probe, usage=99999)
        self.assertEqual(rec["source"], "probe")
        self.assertEqual(rec["recommended"], 27000)
        self.assertNotIn("choices", rec)
        # 仅 body-err 样本 → body_err
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines, samples=[31038, 32375],
                                       probe=None, usage=99999)
        self.assertEqual(rec["source"], "body_err")
        self.assertEqual(rec["recommended"], 27000)
        self.assertEqual(rec["samples"], 2)
        # 仅历史用量 → usage_estimate（suggest=False）
        rec = mod.tpm_budget_recommend("kimi-k3-oc", [], samples=[], probe=None,
                                       usage=47850)
        self.assertEqual(rec["source"], "usage_estimate")
        self.assertEqual(rec["recommended"], 47000)
        self.assertIs(rec["enabled_advice"]["suggest"], False)
```

（4）`test_tpm_budget_recommend_usage_estimate_from_log_ring` — usage=None 时回退 LOG_RING 解析
```
    def test_tpm_budget_recommend_usage_estimate_from_log_ring(self) -> None:
        """usage=None → 从 lines 解析 tpm 峰值做保守参考。"""
        mod = self.mod
        lines = [
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-1 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=12000 ts=T",
            "REQ POST /chat/completions -> 200 dur=9.5s result=ok filtered=0 "
            "model=kimi-k3-oc retried=0 rid=r-2 host=h ttfb=1.0ms stream=1 "
            "outcome=ok qwait=0ms tpm=47850 ts=T",
        ]
        rec = mod.tpm_budget_recommend("kimi-k3-oc", lines)
        self.assertEqual(rec["source"], "usage_estimate")
        self.assertEqual(rec["recommended"], 47000)
        self.assertIs(rec["enabled_advice"]["suggest"], False)
        self.assertNotIn("choices", rec)
```

（5）`test_tpm_budget_recommend_probe_invalid_ignored` — 非法 probe 回落
```
    def test_tpm_budget_recommend_probe_invalid_ignored(self) -> None:
        """probe 非 dict / threshold 非法 → 不采信，回落下一优先级。"""
        mod = self.mod
        for bad_probe in (None, "x", {}, {"threshold": 0}, {"threshold": "x"},
                          {"threshold": -5}):
            rec = mod.tpm_budget_recommend("m", [], samples=None,
                                           probe=bad_probe, usage=20000)
            self.assertEqual(rec["source"], "usage_estimate",
                             "bad probe %r must fall through" % (bad_probe,))
            self.assertEqual(rec["recommended"], 20000)
```

（6）`test_tpm_settings_snapshot_recommend_no_choices`（AC12②）
```
    def test_tpm_settings_snapshot_recommend_no_choices(self) -> None:
        """AC12②：snapshot 每模型 recommend 不含 choices，含 enabled_advice/source；
        持久化样本走 body_err 源、探测结果走 probe 源。"""
        mod = self.mod
        self._tpm_cleanup(mod)
        orig_recent = list(mod.RECENT_REQUESTS)
        orig_daily = dict(mod.STATS["daily_by_model"])
        orig_ring = list(mod.LOG_RING)
        orig_persist = dict(mod.PERSISTED_BODY_ERR_SAMPLES)
        orig_probe = dict(mod.PROBE_RESULTS)
        try:
            mod.RECENT_REQUESTS.clear()
            mod.STATS["daily_by_model"] = {mod.today_key(): {}}
            mod.RECENT_REQUESTS.append({"model": "glm-5.3-oc"})
            mod.RECENT_REQUESTS.append({"model": "deepseek-v4-pro-0813-oc"})
            mod.TPM_MODEL_BUDGETS["kimi-k3-oc"] = 30000
            mod.PERSISTED_BODY_ERR_SAMPLES["kimi-k3-oc"] = [
                {"tpm": 31000, "ts": time.time() - 5}]
            mod.PROBE_RESULTS["glm-5.3-oc"] = [
                {"threshold": 31000, "ts": time.time() - 10}]
            snap = mod.tpm_settings_snapshot()
            by_name = {m["name"]: m for m in snap["models"]}
            for m in snap["models"]:
                rec = m["recommend"]
                self.assertNotIn("choices", rec)
                self.assertIn("enabled_advice", rec)
                self.assertIn("source", rec)
            self.assertEqual(by_name["kimi-k3-oc"]["recommend"]["source"],
                             "body_err")
            self.assertEqual(by_name["kimi-k3-oc"]["recommend"]["recommended"],
                             27000)
            self.assertEqual(by_name["glm-5.3-oc"]["recommend"]["source"],
                             "probe")
            self.assertEqual(by_name["glm-5.3-oc"]["recommend"]["recommended"],
                             27000)
            # 无证据模型 → none 源
            self.assertEqual(
                by_name["deepseek-v4-pro-0813-oc"]["recommend"]["source"],
                "none", "model with no evidence must be source='none'")
        finally:
            mod.RECENT_REQUESTS.clear()
            mod.RECENT_REQUESTS.extend(orig_recent)
            mod.STATS["daily_by_model"] = orig_daily
            mod.LOG_RING.clear()
            mod.LOG_RING.extend(orig_ring)
            mod.PERSISTED_BODY_ERR_SAMPLES.clear()
            mod.PERSISTED_BODY_ERR_SAMPLES.update(orig_persist)
            mod.PROBE_RESULTS.clear()
            mod.PROBE_RESULTS.update(orig_probe)
            self._tpm_cleanup(mod)
```

#### 验证命令

`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（全量）
预期：259 + 6 = 265 test methods, OK（新增 6 个全绿；3 个既有测试定点更新不退化；旧两参调用回归——`test_tpm_budget_recommend_with_samples` 分号更新后 recommended/samples/hint 断言同旧版）。

### 卡 4 — `calibrate_engine` 纯逻辑 + 探测请求构造（注入发送器）

- **涉及文件**：`ctyun-stream-fix-proxy.py`（:833 之后、:836 `class _EmptyStream` 之前插入 `build_calibrate_body` + `calibrate_engine`）；`ctyun-stream-fix-proxy.test.py`（新增单元测试类 `TpmCalibrateEngineTest`，11 个测试方法）
- **tier**：A — 发送器注入（`send_one(batch, est) -> (status, body_error)`），脚本化假发送器穷举拒绝/安全阀/abort 全时序；`sleep`/`should_abort` 注入，无真机依赖。
- **覆盖 AC**：AC1（加压与判定全形态 + 首拒即停 + threshold/batches/consumed 公式）；AC6①（硬顶 capped 不超发）、AC6②（墙钟 timeout 两形态）；AC9（探测请求形态 + `estimate_request_tokens` 量级证明）。
- **接口契约（卡 5 真发送器依此实现）**：`send_one(batch: int, est: int) -> (status: int | None, body_error: bool)`——`status=None` 表示连接级异常（无 HTTP 响应）；真发送器按 batch 放大 input 填充并复用 est 计入 consumed。engine 返回 `{"threshold": int|None, "batches": int, "consumed": int, "outcome": str, "status": int|None, "ts": float}`——`outcome ∈ {"rejected","capped","timeout","aborted","upstream_error"}`；`threshold` = 拒绝批 est（rejected）/ 最后成功批 est（timeout/aborted）/ None（capped/upstream_error）。

#### 生产代码（ctyun-stream-fix-proxy.py）

**插入点**：在 `tpm_settings_snapshot` 结束（:833 `return {"models": models, "default_limit": TPM_LIMIT}`）与 `class _EmptyStream(Exception):`（:836）之间插入：
```
def build_calibrate_body(model: str, input_bytes: int) -> dict:
    """构造校准探测请求体（AC9 形态）：单条 user 大 input、max_tokens 压最小、非流式。

    content 以 "x" 填充至 input_bytes 字节（逐级加压：input_bytes 随批次递增）。
    纯函数：仅依赖入参与模块常量。
    """
    return {
        "model": model,
        "stream": False,
        "max_tokens": TPM_CALIBRATE_MAX_TOKENS,
        "messages": [{"role": "user", "content": "x" * max(0, input_bytes)}],
    }


def calibrate_engine(model, send_one, should_abort=None, sleep=None,
                     estimate=estimate_request_tokens):
    """校准引擎（决策 4/5 纯逻辑；发送器/abort/sleep/estimate 全部注入）。

    线性逐批加压：第 batch 批名义 est = base + (batch-1) * TPM_CALIBRATE_STEP_TOKENS，
    base = estimate(build_calibrate_body(model, TPM_CALIBRATE_INPUT_BYTES))——首批 est
    由准入口径 estimate 实测，保证逐级加压真实作用于限流判定（AC9）。
    每批经 send_one(batch, est) 发送，返回 (status, body_error)；status=None 表示
    连接级异常（无 HTTP 响应）。判定复用 classify_outcome：CLASS_BODY_ERROR /
    CLASS_REQUEST_FAULT（status>=400）→ 拒绝；CLASS_OK / CLASS_POISON_FIXED →
    未拒绝继续加压；CLASS_UPSTREAM_FAULT（5xx/连接异常）→ 同一批重试一次后仍败
    → 中止标 upstream_error（不把上游故障误判为阈值，R5）。
    首拒即停（不追加确认批次）。安全阀：下一批 est 将超过
    TPM_CALIBRATE_HARD_CAP_TOKENS → capped 且不超发；墙钟超
    TPM_CALIBRATE_MAX_DURATION_S → timeout；should_abort() 为真（批间隙检查）
    → aborted。返回 {"threshold", "batches", "consumed", "outcome", "status", "ts"}：
    threshold = 拒绝批 est（rejected）或最后成功批 est（timeout/aborted），其余 None。
    """
    started = time.monotonic()
    base = estimate(build_calibrate_body(model, TPM_CALIBRATE_INPUT_BYTES))
    batch = 0
    consumed = 0
    last_ok_est = None
    sleep_fn = sleep if sleep is not None else time.sleep
    abort_fn = should_abort if should_abort is not None else (lambda: False)

    def _result(outcome, threshold, status):
        return {"threshold": threshold, "batches": batch, "consumed": consumed,
                "outcome": outcome, "status": status, "ts": time.time()}

    def _attempt(batch_num, est):
        """发送一次，返回 (category, status)；连接异常 → (CLASS_UPSTREAM_FAULT, None)。"""
        status, body_error = send_one(batch_num, est)
        if status is None:
            return CLASS_UPSTREAM_FAULT, None
        return classify_outcome(status=status, body_error=body_error).category, status

    while True:
        if abort_fn():
            return _result("aborted", last_ok_est, None)
        if time.monotonic() - started >= TPM_CALIBRATE_MAX_DURATION_S:
            return _result("timeout", last_ok_est, None)
        batch += 1
        est = base + (batch - 1) * TPM_CALIBRATE_STEP_TOKENS
        if consumed + est > TPM_CALIBRATE_HARD_CAP_TOKENS:
            return _result("capped", None, None)
        category, status = _attempt(batch, est)
        consumed += est
        if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
            return _result("rejected", est, status)
        if category == CLASS_UPSTREAM_FAULT:
            # 上游故障不是限流信号：同一批重试一次（不递增 batch，est 不变）
            if abort_fn():
                return _result("aborted", last_ok_est, None)
            sleep_fn(TPM_CALIBRATE_BATCH_GAP_S)
            category, status = _attempt(batch, est)
            consumed += est
            if category in (CLASS_BODY_ERROR, CLASS_REQUEST_FAULT):
                return _result("rejected", est, status)
            if category == CLASS_UPSTREAM_FAULT:
                return _result("upstream_error", None, status)
        last_ok_est = est
        if abort_fn():
            return _result("aborted", last_ok_est, None)
        sleep_fn(TPM_CALIBRATE_BATCH_GAP_S)
```

#### 测试代码（ctyun-stream-fix-proxy.test.py）

新增测试类（放在 `ProxyDashboardUnitTest` 之后，:1500 附近）：
```
class TpmCalibrateEngineTest(unittest.TestCase):
    """校准引擎纯逻辑单测：注入脚本化发送器穷举拒绝/安全阀/abort 时序。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_proxy_module()

    def _scripted(self, responses):
        """按调用序号依次返回 responses，越界重复最后一项。"""
        calls = {"n": 0}

        def sender(batch, est):
            calls["n"] += 1
            idx = min(calls["n"], len(responses)) - 1
            return responses[idx]

        return sender, calls
```

11 个测试方法：

（1）`test_build_calibrate_body_shape`（AC9）
```
    def test_build_calibrate_body_shape(self) -> None:
        """AC9：max_tokens == TPM_CALIBRATE_MAX_TOKENS、stream == False、
        messages 内容长度 ≥ TPM_CALIBRATE_INPUT_BYTES；estimate 量级 ≥ 大 input 比例。"""
        mod = self.mod
        body = mod.build_calibrate_body("kimi-k3-oc", mod.TPM_CALIBRATE_INPUT_BYTES)
        self.assertEqual(body["model"], "kimi-k3-oc")
        self.assertEqual(body["max_tokens"], mod.TPM_CALIBRATE_MAX_TOKENS)
        self.assertIs(body["stream"], False)
        content = body["messages"][0]["content"]
        self.assertGreaterEqual(len(content), mod.TPM_CALIBRATE_INPUT_BYTES)
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        est = mod.estimate_request_tokens(raw)
        self.assertGreaterEqual(est, int(mod.TPM_CALIBRATE_INPUT_BYTES
                                         * mod.TPM_TOKEN_RATIO),
                                "estimate must prove large input magnitude")
        # 输入字节递增 → est 递增（逐级加压真实作用于准入口径）
        bigger = mod.build_calibrate_body("kimi-k3-oc",
                                          mod.TPM_CALIBRATE_INPUT_BYTES + 4000)
        est_bigger = mod.estimate_request_tokens(
            json.dumps(bigger, ensure_ascii=False).encode("utf-8"))
        self.assertGreater(est_bigger, est)
```

（2）`test_calibrate_engine_linear_press_first_reject_stops`（AC1，429 拒绝）
```
    def test_calibrate_engine_linear_press_first_reject_stops(self) -> None:
        """AC1：前 N-1 批 200 正常，第 N 批 429 → 首拒即停（不追加确认批次）；
        threshold = (N-1)*step + base、consumed = 累计 est。"""
        mod = self.mod
        step = mod.TPM_CALIBRATE_STEP_TOKENS
        base = mod.estimate_request_tokens(
            mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES))
        sender, calls = self._scripted([(200, False)] * 4 + [(429, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(calls["n"], 5, "first reject must stop, no confirm batch")
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 5)
        self.assertEqual(result["threshold"], base + 4 * step)
        self.assertEqual(result["consumed"], base * 5 + step * (0 + 1 + 2 + 3 + 4))
        self.assertEqual(result["status"], 429)
```

（3）`test_calibrate_engine_rejects_on_body_error_200`（200+错误体拒绝）
```
    def test_calibrate_engine_rejects_on_body_error_200(self) -> None:
        """200 + body_error=True → CLASS_BODY_ERROR → 拒绝（R5 判定协同）。"""
        mod = self.mod
        step = mod.TPM_CALIBRATE_STEP_TOKENS
        base = mod.estimate_request_tokens(
            mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES))
        sender, calls = self._scripted([(200, False), (200, False), (200, True)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 3)
        self.assertEqual(result["threshold"], base + 2 * step)
        self.assertEqual(result["status"], 200)
        self.assertEqual(calls["n"], 3)
```

（4）`test_calibrate_engine_hard_cap_stops_before_oversend`（AC6①）
```
    def test_calibrate_engine_hard_cap_stops_before_oversend(self) -> None:
        """AC6①：下一批将超硬顶 → capped 且不发超限批。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES))
        orig = mod.TPM_CALIBRATE_HARD_CAP_TOKENS
        mod.TPM_CALIBRATE_HARD_CAP_TOKENS = base + mod.TPM_CALIBRATE_STEP_TOKENS
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_HARD_CAP_TOKENS", orig)
        sender, calls = self._scripted([(200, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "capped")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["consumed"], base)
        self.assertEqual(calls["n"], 1, "must not oversend beyond hard cap")
```

（5）`test_calibrate_engine_timeout_zero_duration`（AC6② 即刻超时）
```
    def test_calibrate_engine_timeout_zero_duration(self) -> None:
        """AC6②：墙钟上限为 0 → 首轮检查即 timeout，发送器零调用。"""
        mod = self.mod
        orig = mod.TPM_CALIBRATE_MAX_DURATION_S
        mod.TPM_CALIBRATE_MAX_DURATION_S = 0
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_MAX_DURATION_S", orig)
        sender, calls = self._scripted([(200, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["batches"], 0)
        self.assertEqual(calls["n"], 0)
```

（6）`test_calibrate_engine_timeout_between_batches`（AC6② 批间超时）
```
    def test_calibrate_engine_timeout_between_batches(self) -> None:
        """AC6②：第一批成功，批间真实流逝超过墙钟上限 → timeout，
        threshold = 最后成功批 est。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES))
        orig = mod.TPM_CALIBRATE_MAX_DURATION_S
        mod.TPM_CALIBRATE_MAX_DURATION_S = 0.2
        self.addCleanup(setattr, mod, "TPM_CALIBRATE_MAX_DURATION_S", orig)
        sender, calls = self._scripted([(200, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: time.sleep(0.25))
        self.assertEqual(result["outcome"], "timeout")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["threshold"], base)
        self.assertEqual(calls["n"], 1)
```

（7）`test_calibrate_engine_abort_between_batches`
```
    def test_calibrate_engine_abort_between_batches(self) -> None:
        """abort 在批间隙生效：第二次 abort 检查后返回，发送器不再调用。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES))
        checks = {"n": 0}

        def abort_fn():
            checks["n"] += 1
            return checks["n"] >= 2

        sender, calls = self._scripted([(200, False)])
        result = mod.calibrate_engine("m", sender, should_abort=abort_fn,
                                      sleep=lambda _: None)
        self.assertEqual(result["outcome"], "aborted")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["threshold"], base)
        self.assertEqual(calls["n"], 1)
```

（8）`test_calibrate_engine_upstream_fault_retry_then_abort`（5xx 重试一次 → upstream_error）
```
    def test_calibrate_engine_upstream_fault_retry_then_abort(self) -> None:
        """CLASS_UPSTREAM_FAULT（5xx）重试同批一次后仍败 → upstream_error。"""
        mod = self.mod
        base = mod.estimate_request_tokens(
            mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES))
        sender, calls = self._scripted([(503, False), (503, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "upstream_error")
        self.assertEqual(result["batches"], 1)
        self.assertEqual(result["status"], 503)
        self.assertEqual(result["consumed"], base * 2, "retry re-charges same est")
        self.assertEqual(calls["n"], 2)
```

（9）`test_calibrate_engine_connection_error_is_upstream_fault`（连接异常同路径）
```
    def test_calibrate_engine_connection_error_is_upstream_fault(self) -> None:
        """status=None（连接异常）→ CLASS_UPSTREAM_FAULT 同路径：重试后 upstream_error。"""
        mod = self.mod
        sender, calls = self._scripted([(None, False), (None, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "upstream_error")
        self.assertEqual(calls["n"], 2)
```

（10）`test_calibrate_engine_retry_succeeds_then_reject`（重试成功后继续加压至拒绝）
```
    def test_calibrate_engine_retry_succeeds_then_reject(self) -> None:
        """503 重试一次成功（200）→ 未拒绝继续加压，下一批 429 → rejected。"""
        mod = self.mod
        step = mod.TPM_CALIBRATE_STEP_TOKENS
        base = mod.estimate_request_tokens(
            mod.build_calibrate_body("m", mod.TPM_CALIBRATE_INPUT_BYTES))
        sender, calls = self._scripted([(503, False), (200, False), (429, False)])
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        self.assertEqual(result["outcome"], "rejected")
        self.assertEqual(result["batches"], 2)
        self.assertEqual(result["threshold"], base + step)
        self.assertEqual(result["consumed"],
                         base * 3 + step, "503 retry + batch1 + batch2")
        self.assertEqual(calls["n"], 3)
```

（11）`test_calibrate_engine_result_ts_and_fields`
```
    def test_calibrate_engine_result_ts_and_fields(self) -> None:
        """返回 dict 六键齐全；ts 为完成时刻时间戳。"""
        mod = self.mod
        sender, _ = self._scripted([(429, False)])
        before = time.time()
        result = mod.calibrate_engine("m", sender, sleep=lambda _: None)
        for key in ("threshold", "batches", "consumed", "outcome", "status", "ts"):
            self.assertIn(key, result)
        self.assertEqual(result["outcome"], "rejected")
        self.assertIsInstance(result["threshold"], int)
        self.assertIsInstance(result["consumed"], int)
        self.assertGreaterEqual(result["ts"], before)
```

#### 验证命令

`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（全量）
预期：265 + 11 = 276 test methods, OK（新增 11 个全绿；既有 265 不退化）。

### 卡 5 — 校准状态/daemon/发送器 + `_LAST_AUTH` + relay bypass + `_log` probe + `main()` 启动

- **涉及文件**：`ctyun-stream-fix-proxy.py`（6 处：① :28 后加 `import uuid`；② 卡 4 新增 `calibrate_engine` 之后、`class _EmptyStream(:836)` 之前插入 state+daemon+sender 共 8 个函数/变量；③ :1778 后 bypass + _LAST_AUTH 更新；④ :2242 `_log` 加 `probe` kwarg；⑤ :3390 `main()` :3477 后加 daemon 启动；⑥ :834 附近加 `_CALIBRATE_BYTES_PER_STEP` 常量）；`ctyun-stream-fix-proxy.test.py`（新增 2 个测试类：`TpmCalibrateStateTest` 5 个方法 + `CalibrateSendTest` 4 个方法）
- **tier**：A — daemon 模式仿既有 `_probe_loop`；真发送器走既有 `http.client` 直连口径；state 纯状态机单测；发送器用假上游 in-process 单测；全部写死。
- **覆盖 AC**：AC6③（最小间隔 429）、AC6④（abort 批间隙生效——卡 4 引擎层 + 卡 5 daemon 状态机衔接）；R2（直连不经 relay）、R3（_LAST_AUTH 凭证复用）、R6（显式异常分类不静默吞）、R8（_LAST_AUTH 仅内存不落盘）。

#### 生产代码（ctyun-stream-fix-proxy.py）

**插入点 1**：在 `import urllib.parse`（:28）之后加：
```
import uuid
```

**插入点 2**：在卡 4 新增的 `calibrate_engine` 函数之后、`class _EmptyStream`（:836）之前插入 state/daemon/sender 完整代码块（包含 _CALIBRATE_BYTES_PER_STEP 常量）：
```
# v2 P5：校准 daemon 与发送器（决策 9/10）
_CALIBRATE_BYTES_PER_STEP = max(1, int(TPM_CALIBRATE_STEP_TOKENS
                                       / max(TPM_TOKEN_RATIO, 0.01)))
CALIBRATE_LOCK = threading.Lock()
_LAST_AUTH = None   # 最近一次真实流量的 Authorization 头（R3 凭证来源 a；仅内存）
_CALIBRATE_TOKEN = None  # calibrate_start 注入的 Bearer token（None=回落 _LAST_AUTH）

_CALIBRATE_STATE = {
    "running": False,       # bool：校准进行中
    "task_id": None,        # str：本次校准 uuid 短 hex（12 位）
    "model": None,          # str：校准目标模型名
    "started_at": None,     # float：启动时间戳
    "progress": {"batch": 0, "consumed": 0},
    "result": None,         # dict：calibrate_engine 返回结果
    "error": None,          # str：异常消息（R6）
    "abort": False,         # bool：calibrate_abort 置位，engine 批间隙检查
    "last_probe_ts": {},    # {model: float}：同模型上次校准完成时间戳（最小间隔判定）
}


def calibrate_state_snapshot() -> dict:
    """GET /api/tpm_calibrate 轮询快照（短持 CALIBRATE_LOCK）。"""
    with CALIBRATE_LOCK:
        return {
            "running": _CALIBRATE_STATE["running"],
            "task_id": _CALIBRATE_STATE["task_id"],
            "model": _CALIBRATE_STATE["model"],
            "progress": dict(_CALIBRATE_STATE["progress"]),
            "result": _CALIBRATE_STATE["result"],
        }


def calibrate_start(model, token=None) -> tuple:
    """POST /api/tpm_calibrate 处理：校验 → 置位 state，由 daemon 消费。

    返回 (http_status, payload_dict)。
    """
    if not isinstance(model, str) or not model:
        return 400, {"error": "model 必须为非空字符串"}
    with CALIBRATE_LOCK:
        if _CALIBRATE_STATE["running"]:
            return 409, {"error": "已有校准任务在进行中（模型 %s），请等待或中止后重试"
                         % _CALIBRATE_STATE["model"]}
        last_ts = _CALIBRATE_STATE["last_probe_ts"].get(model)
        if last_ts is not None and last_ts > 0:
            elapsed = time.time() - last_ts
            if elapsed < TPM_CALIBRATE_MIN_INTERVAL_S:
                return 429, {"error": "模型 %s 距上次校准不足 %ds（已过 %.0fs），"
                             "请 %ds 后重试"
                             % (model, TPM_CALIBRATE_MIN_INTERVAL_S, elapsed,
                                int(TPM_CALIBRATE_MIN_INTERVAL_S - elapsed))}
        if not token and not _LAST_AUTH:
            return 400, {"error": "无可用凭证：请先产生一次真实流量，或在请求体提供 token"}
        task_id = uuid.uuid4().hex[:12]
        _CALIBRATE_STATE["running"] = True
        _CALIBRATE_STATE["task_id"] = task_id
        _CALIBRATE_STATE["model"] = model
        _CALIBRATE_STATE["started_at"] = time.time()
        _CALIBRATE_STATE["progress"] = {"batch": 0, "consumed": 0}
        _CALIBRATE_STATE["result"] = None
        _CALIBRATE_STATE["error"] = None
        _CALIBRATE_STATE["abort"] = False
        global _CALIBRATE_TOKEN
        _CALIBRATE_TOKEN = token  # None=回落 _LAST_AUTH（daemon 消费后置 None）
    return 200, {"ok": True, "task_id": task_id}


def calibrate_abort() -> bool:
    """POST /api/tpm_calibrate_abort 处理：置 abort 标志（幂等，不强制杀线程）。"""
    with CALIBRATE_LOCK:
        was_running = _CALIBRATE_STATE["running"]
        _CALIBRATE_STATE["abort"] = True
    return was_running


def _calibrate_send(model, batch, est, auth_token):
    """单次校准探测发送（R2：http.client 直连 UPSTREAM_BASE，不经 _proxy_relay）。

    body 由 build_calibrate_body 按批次放大 input 填充：batch 1=INPUT_BYTES，
    每批 +_CALIBRATE_BYTES_PER_STEP 字节（使 estimate 增量 ≈ TPM_CALIBRATE_STEP_TOKENS）。
    Authorization = auth_token 或 _LAST_AUTH。
    返回 (status, body_error)；连接异常（OSError / HTTPException，R6 显式分类）
    → (None, False) 并 _safe_log_stderr 留痕；engine 将连接异常判为
    CLASS_UPSTREAM_FAULT 走重试路径。
    """
    input_bytes = TPM_CALIBRATE_INPUT_BYTES \
        + (batch - 1) * _CALIBRATE_BYTES_PER_STEP
    body = build_calibrate_body(model, input_bytes)
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    parsed = urllib.parse.urlparse(UPSTREAM_BASE)
    upstream_path = parsed.path.rstrip("/") + "/v1/chat/completions"
    headers = {"Content-Type": "application/json"}
    token = auth_token or _LAST_AUTH
    if token:
        headers["Authorization"] = token
    try:
        if parsed.scheme == "https":
            conn = http.client.HTTPSConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname, parsed.port, timeout=HEADER_TIMEOUT_S)
        conn.request("POST", upstream_path, body=body_bytes, headers=headers)
        resp = conn.getresponse()
        status = resp.status
        data = resp.read()
        conn.close()
    except (OSError, http.client.HTTPException) as exc:
        # 显式分类（R6）：连接级异常不是限流信号；stderr 留痕，由 engine 判
        # CLASS_UPSTREAM_FAULT 走重试。
        _safe_log_stderr("ctyun-stream-fix-proxy: calibrate send failed "
                         "model=%s batch=%d: %s" % (model, batch, exc))
        return None, False
    body_error = False
    if data:
        try:
            parsed_data = json.loads(data.decode("utf-8", "replace"))
        except ValueError:
            parsed_data = None  # 非 JSON 响应体 → 无 body error（fail-open）
        if isinstance(parsed_data, dict):
            body_error = body_has_error(parsed_data)
    return status, body_error


def _calibrate_log(model, task_id, result, dur_s) -> None:
    """校准摘要日志（R2：校准不经 relay 的 _log，用同款 REQ 行格式 + probe=1 标记）。

    同时写 stderr 与 LOG_RING（_LOG_SEQ 同 _log 口径递增）；AC7 的
    /api/logs 含 probe=1 断言依赖本函数。
    """
    status = result.get("status")
    line = ("REQ POST /v1/chat/completions -> %s dur=%.1fs result=calibrate "
            "filtered=0 model=%s retried=0 retry_reason=- exc=- "
            "rid=calibrate-%s host=%s ttfb=- stream=0 outcome=%s "
            "consumed=%d probe=1 ts=%s"
            % (status if status is not None else "-", dur_s, model, task_id,
               urllib.parse.urlparse(UPSTREAM_BASE).netloc,
               result.get("outcome"), result.get("consumed", 0),
               time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())))
    _safe_log_stderr(line)
    global _LOG_SEQ
    with LOG_LOCK:
        _LOG_SEQ += 1
        LOG_RING.append({"seq": _LOG_SEQ, "line": line})


def _calibrate_loop() -> None:
    """校准 daemon 线程（main() 恒启动一次；无待跑任务时轮询空转）。

    检测 running=True → 取模型与凭证 → 构造 send_one 注入发送器
    （_calibrate_send 直连 UPSTREAM_BASE，R2）→ 调 calibrate_engine 跑完整校准；
    结束后写 state.result、复位 running，rejected 时落探测结果（record_tpm_probe_result），
    写 probe=1 摘要日志（_calibrate_log）。
    顶层 except Exception：写 _CALIBRATE_STATE["error"] + _safe_log_stderr 后复位
    running=False——不静默吞掉（R6），供 Dashboard 展示。
    """
    while True:
        time.sleep(1)
        with CALIBRATE_LOCK:
            if not _CALIBRATE_STATE["running"]:
                continue
            task_id = _CALIBRATE_STATE["task_id"]
            model = _CALIBRATE_STATE["model"]
            token = _CALIBRATE_TOKEN
            _CALIBRATE_TOKEN = None
        auth_token = token or _LAST_AUTH
        started = time.time()

        def send_one(batch, est):
            return _calibrate_send(model, batch, est, auth_token)

        def should_abort():
            with CALIBRATE_LOCK:
                return _CALIBRATE_STATE["abort"]

        try:
            result = calibrate_engine(model, send_one, should_abort=should_abort)
        except Exception as exc:
            # 顶层兜底（R6）：写入 state.error + stderr 留痕后复位 running，
            # 让该线程继续服务（下次 POST 仍可启动新任务）。
            _safe_log_stderr("ctyun-stream-fix-proxy: calibrate loop failed "
                             "model=%s: %s" % (model, exc))
            with CALIBRATE_LOCK:
                if _CALIBRATE_STATE.get("task_id") == task_id:
                    _CALIBRATE_STATE["running"] = False
                    _CALIBRATE_STATE["error"] = "%s: %s" % (
                        type(exc).__name__, exc)
            continue
        with CALIBRATE_LOCK:
            if _CALIBRATE_STATE.get("task_id") != task_id:
                continue  # 已被新任务取代：结果丢弃
            _CALIBRATE_STATE["result"] = result
            _CALIBRATE_STATE["progress"] = {"batch": result["batches"],
                                             "consumed": result["consumed"]}
            _CALIBRATE_STATE["running"] = False
            _CALIBRATE_STATE["last_probe_ts"][model] = result["ts"]
        if result["outcome"] == "rejected" and result["threshold"] is not None:
            record_tpm_probe_result(model, {
                "threshold": result["threshold"],
                "ts": result["ts"],
                "outcome": result["outcome"],
                "batches": result["batches"],
                "consumed": result["consumed"],
            })
        _calibrate_log(model, task_id, result, time.time() - started)
```

**插入点 3**：在 `_proxy_relay`（:1778）开头加 bypass 分支；在 `model = normalize_null_assistant_content(body)`（:1782）之后加 `_LAST_AUTH` 更新——在 `# --- TPM 准入 hook ---` 注释（:1784）之前插入：
```
        # 校准探测 bypass（R2 防御性）：校准请求经 _calibrate_send 直连上游，
        # 不经 relay——不经过 TPM hook、不污染 _record_request/daily_by_model。
        # 正常路径不触发。
        if getattr(self, "_calibrate_probe", False):
            return
        ...
```
将 :1778 开头改为：
```
    def _proxy_relay(self, started: float) -> None:
        if getattr(self, "_calibrate_probe", False):
            return
        length = int(self.headers.get("Content-Length") or 0)
```
在 `model = normalize_null_assistant_content(body)`（:1782）之后、`# --- TPM 准入 hook ---`（:1784）之前插入：
```
        # 记录最近一次真实流量的 Authorization（R3 凭证来源 a：校准默认复用；
        # 仅内存持有，save_stats_counters 白名单不含它，绝不落盘）
        auth_header = self.headers.get("Authorization")
        if auth_header and isinstance(auth_header, str) \
                and auth_header.startswith("Bearer "):
            with _CFG_LOCK:
                _LAST_AUTH = auth_header
```

**插入点 4**：在 `_log` 方法（:2242）签名末尾加 `probe=None`；在 :2255 末尾（`extra += " tpm=%d" % tpm_used` 之后）加 `probe` kwarg 处理：

签名行 `:2243-2245` 改为：
```
             outcome=None, qwait_ms=None, tpm_used=None, probe=None) -> None:
```
在 `if tpm_used is not None:` 代码块之后（:2254-2255 附近，`extra += " tpm=%d" % tpm_used` 行后）加：
```
        if probe:
            extra += " probe=1"
```
精确锚：在 `"REQ %s %s -> %d dur=%.1fs result=%s filtered=%d "` 格式化字符串准备之前，完成 `extra` 拼接。实际位置：现有代码 `if tpm_used is not None: extra += " tpm=%d" % tpm_used`（:2254-2255），在其后添加上述两行。

**插入点 5**：在 `main()` 函数 :3477（probe daemon 启动行）之后加校准 daemon：
```
    # v2 P5：校准 daemon（恒启动一次；无待跑任务时轮询空转，见 _calibrate_loop docstring）
    threading.Thread(target=_calibrate_loop, daemon=True,
                     name="tpm-calibrate").start()
```

#### 测试代码（ctyun-stream-fix-proxy.test.py）

**（A）新增 `TpmCalibrateStateTest` 类**（in-process state 单元测试，放在 `TpmCalibrateEngineTest` 之后），5 个测试方法：

setUp 骨架：
```
class TpmCalibrateStateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_proxy_module()

    def setUp(self) -> None:
        mod = self.mod
        with mod.CALIBRATE_LOCK:
            mod._CALIBRATE_STATE.clear()
            mod._CALIBRATE_STATE.update({
                "running": False, "task_id": None, "model": None,
                "started_at": None, "progress": {"batch": 0, "consumed": 0},
                "result": None, "error": None, "abort": False,
                "last_probe_ts": {},
            })
            mod._CALIBRATE_TOKEN = None
        mod._LAST_AUTH = None
```

（1）`test_calibrate_state_snapshot_initial` — 初始形态六键 + running False：
```
    def test_calibrate_state_snapshot_initial(self) -> None:
        snap = self.mod.calibrate_state_snapshot()
        for key in ("running", "task_id", "model", "progress", "result"):
            self.assertIn(key, snap)
        self.assertIs(snap["running"], False)
        self.assertIsNone(snap["model"])
```

（2）`test_calibrate_start_validation_and_auth` — 非法 model 400 + 无凭证 400 + 带 token 200：
```
    def test_calibrate_start_validation_and_auth(self) -> None:
        mod = self.mod
        for bad in (None, "", 123, True):
            status, _ = mod.calibrate_start(bad)
            self.assertEqual(status, 400, "bad model=%r" % (bad,))
        mod._LAST_AUTH = None
        status, payload = mod.calibrate_start("kimi", token=None)
        self.assertEqual(status, 400)
        self.assertIn("无可用凭证", payload["error"])
        status, payload = mod.calibrate_start("kimi", token="Bearer t")
        self.assertEqual(status, 200)
        self.assertIn("task_id", payload)
```

（3）`test_calibrate_start_sets_state` — 正常启动 state 全字段验证：
```
    def test_calibrate_start_sets_state(self) -> None:
        mod = self.mod
        status, payload = mod.calibrate_start("kimi", token="t")
        self.assertEqual(status, 200)
        with mod.CALIBRATE_LOCK:
            st = mod._CALIBRATE_STATE
            self.assertTrue(st["running"])
            self.assertEqual(st["model"], "kimi")
            self.assertEqual(st["progress"], {"batch": 0, "consumed": 0})
            self.assertEqual(st["task_id"], payload["task_id"])
            self.assertIsNone(st["result"])
            self.assertFalse(st["abort"])
```

（4）`test_calibrate_start_conflict_and_interval` — 409 互斥 + 429 最小间隔：
```
    def test_calibrate_start_conflict_and_interval(self) -> None:
        mod = self.mod
        status, _ = mod.calibrate_start("m1", token="t")
        self.assertEqual(status, 200)
        status, payload = mod.calibrate_start("m2", token="t")
        self.assertEqual(status, 409)
        self.assertIn("m1", payload["error"])
        with mod.CALIBRATE_LOCK:
            mod._CALIBRATE_STATE["last_probe_ts"]["m1"] = time.time()
        # 同模型 < MIN_INTERVAL → 429
        status, payload = mod.calibrate_start("m1", token="t")
        self.assertEqual(status, 429)
        self.assertIn("距上次校准不足", payload["error"])
        with mod.CALIBRATE_LOCK:
            self.assertFalse(mod._CALIBRATE_STATE["running"],
                             "429 must not set running=True")
        # 清除 last_probe_ts → 200 正常启动
        with mod.CALIBRATE_LOCK:
            del mod._CALIBRATE_STATE["last_probe_ts"]["m1"]
        status, _ = mod.calibrate_start("m1", token="t")
        self.assertEqual(status, 200)
```

（5）`test_calibrate_abort_idempotent`：
```
    def test_calibrate_abort_idempotent(self) -> None:
        mod = self.mod
        self.assertFalse(mod.calibrate_abort())  # 无运行任务 → False
        status, _ = mod.calibrate_start("m", token="t")
        self.assertEqual(status, 200)
        self.assertTrue(mod.calibrate_abort())   # 有任务 → True
        with mod.CALIBRATE_LOCK:
            self.assertTrue(mod._CALIBRATE_STATE["abort"])
        self.assertTrue(mod.calibrate_abort())   # 幂等
```

**（B）新增 `CalibrateSendTest` 类**（in-process 假上游直连验证），4 个测试方法：

setUp/tearDown 骨架：
```
class CalibrateSendTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = load_proxy_module()

    def setUp(self) -> None:
        self._orig_base = self.mod.UPSTREAM_BASE
        self._orig_auth = self.mod._LAST_AUTH
        self.upstream_port = make_fake_upstream(False)
        self.mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % self.upstream_port
        self.mod._LAST_AUTH = None

    def tearDown(self) -> None:
        self.mod.UPSTREAM_BASE = self._orig_base
        self.mod._LAST_AUTH = self._orig_auth
        stop_fake_upstreams()
```

（1）`test_calibrate_send_200_to_ok` — 正常上游 → (200, False)：
```
    def test_calibrate_send_200_to_ok(self) -> None:
        status, body_error = self.mod._calibrate_send(
            "m", 1, 60013, "Bearer t")
        self.assertEqual(status, 200)
        self.assertIs(body_error, False)
```

（2）`test_calibrate_send_200_body_error` — fail_200_error 上游 → (200, True)：
```
    def test_calibrate_send_200_body_error(self) -> None:
        stop_fake_upstreams()
        err_port = make_fake_upstream(False, fail_200_error=True)
        self.mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % err_port
        status, body_error = self.mod._calibrate_send(
            "m", 1, 60013, "Bearer t")
        self.assertEqual(status, 200)
        self.assertIs(body_error, True)
```

（3）`test_calibrate_send_500` — fail_500 → (500, False)：
```
    def test_calibrate_send_500(self) -> None:
        stop_fake_upstreams()
        err_port = make_fake_upstream(False, fail_500=True)
        self.mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % err_port
        status, body_error = self.mod._calibrate_send(
            "m", 3, 70013, "Bearer t")
        self.assertEqual(status, 500)
        self.assertIs(body_error, False)
```

（4）`test_calibrate_send_connection_refused` — 不可达端口 → (None, False)：
```
    def test_calibrate_send_connection_refused(self) -> None:
        free = free_port()
        self.mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % free
        status, body_error = self.mod._calibrate_send(
            "m", 1, 60013, None)
        self.assertIsNone(status)
        self.assertIs(body_error, False)
```

#### 验证命令

`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（全量）
预期：276 + 5 + 4 = 285 test methods, OK（新增 9 个全绿 + 既有不变）。
注意：`_calibrate_send` 的 200 假上游测试需要 `make_fake_upstream(False)` 在测试前监听；stop_fake_upstreams 在 tearDown 执行。

### 卡 6 — Admin API：`/api/tpm_calibrate`（GET 轮询 / POST 启动）+ `/api/tpm_calibrate_abort`（POST）

- **涉及文件**：`ctyun-stream-fix-proxy.py`（do_GET :2271-2357 加 GET 分支；do_POST :2359-2443 加两个 POST 分支 + `_handle_calibrate_post`/`_handle_calibrate_abort_post` 两个 handler）；`ctyun-stream-fix-proxy.test.py`（`AdminIntegrationTest` 新增 3 个用例）
- **tier**：A — 三个端点语义、鉴权复用 `write_allowed`、互斥/400/403 形态全部写死；集成测试复用 `admin_post`/`admin_get`/`start_proxy`。
- **覆盖 AC**：AC5 全部（200+task_id、409 互斥、GET 形态、abort 使 running 转 False 且结果标 aborted、缺 model/非字符串 → 400、非本机无 token → 403）；AC7 全部（一次完整校准后 `/api/stats` 的 `requests_total`/`daily_by_model`/`recent` 增量 == 0、`/api/logs` 校准行含 `probe=1`、`/api/tpm_stats` 桶 `used` 不因校准增长）。

#### 生产代码（ctyun-stream-fix-proxy.py）

**插入点 A（do_GET 分支）**：在 `/api/tpm_settings` 分支（:2281-2282）之后加：
```
        elif path == "/api/tpm_calibrate":
            self._send_json(200, calibrate_state_snapshot())
```

**插入点 B（do_POST 分支）**：在 :2367-2369（`if path == "/api/probe": ...`）之后加两个分支：
```
        if path == "/api/tpm_calibrate":
            self._handle_calibrate_post(raw)
            return
        if path == "/api/tpm_calibrate_abort":
            self._handle_calibrate_abort_post(raw)
            return
```

**插入点 C（两个 handler）**：在 `_handle_probe_post`（:2424-2443）之后插入：
```
    def _handle_calibrate_post(self, raw: bytes) -> None:
        """POST /api/tpm_calibrate：启动校准探测（鉴权同 /api/config 的 write_allowed）。"""
        if not write_allowed(self.client_address[0],
                             self.headers.get("X-Admin-Token") or "",
                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            self._send_json(403, {"error": "非本机启动校准需要 X-Admin-Token 头"
                                           "（值 = 服务器环境变量 CTYUN_ADMIN_TOKEN）"})
            return
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(400, {"error": "请求体不是合法 JSON——"
                                           "请发 {\"model\": \"...\"}"})
            return
        model = data.get("model") if isinstance(data, dict) else None
        token = data.get("token") if isinstance(data, dict) else None
        status, payload = calibrate_start(model, token=token)
        self._send_json(status, payload)

    def _handle_calibrate_abort_post(self, raw: bytes) -> None:
        """POST /api/tpm_calibrate_abort：中止校准（鉴权同款；幂等——无任务也 200）。"""
        if not write_allowed(self.client_address[0],
                             self.headers.get("X-Admin-Token") or "",
                             os.environ.get("CTYUN_ADMIN_TOKEN", "")):
            self._send_json(403, {"error": "非本机中止校准需要 X-Admin-Token 头"
                                           "（值 = 服务器环境变量 CTYUN_ADMIN_TOKEN）"})
            return
        aborted = calibrate_abort()
        self._send_json(200, {"ok": True, "aborted": aborted})
```

#### 测试代码（ctyun-stream-fix-proxy.test.py）

`AdminIntegrationTest`（:2899）新增 3 个测试方法：

（1）`test_calibrate_api_start_poll_abort`（AC5 核心流程）
```
    def test_calibrate_api_start_poll_abort(self) -> None:
        """AC5：先带 Authorization 的真实流量喂 _LAST_AUTH（R3a）→ POST 无 token 200+task_id；
        二启 → 409；GET 轮询形态；POST abort → 轮询至 running=False 且 result.outcome=="aborted"。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port, extra_env={
            "CTYUN_CALIBRATE_BATCH_GAP_S": "0.2",
        })
        # 真实流量喂 _LAST_AUTH（校准默认复用最近流量凭证）
        post_sse_auth(self.proxy_port,
                      b'{"model":"kimi-k3-oc","stream":true,'
                      b'"messages":[{"role":"user","content":"hi"}]}',
                      "Bearer test-cal-token")
        status, body = admin_post(
            self.proc.admin_port, "/api/tpm_calibrate",
            json.dumps({"model": "kimi-k3-oc"}).encode("utf-8"))
        self.assertEqual(status, 200, body)
        payload = json.loads(body.decode("utf-8"))
        self.assertIn("task_id", payload)
        self.assertTrue(payload["task_id"])
        # 同一时刻只允许一个校准任务
        status, body = admin_post(
            self.proc.admin_port, "/api/tpm_calibrate",
            json.dumps({"model": "kimi-k3-oc"}).encode("utf-8"))
        self.assertEqual(status, 409)
        # 轮询形态
        _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_calibrate")
        snap = json.loads(body.decode("utf-8"))
        self.assertTrue(snap["running"])
        self.assertEqual(snap["model"], "kimi-k3-oc")
        self.assertIn("progress", snap)
        self.assertIn("result", snap)
        # abort → 轮询至 running=False 且结果标 aborted
        status, _ = admin_post(self.proc.admin_port, "/api/tpm_calibrate_abort",
                               b"{}")
        self.assertEqual(status, 200)
        deadline = time.time() + 20
        while time.time() < deadline:
            _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_calibrate")
            snap = json.loads(body.decode("utf-8"))
            if not snap["running"]:
                break
            time.sleep(0.2)
        self.assertFalse(snap["running"], "abort must stop the calibration")
        self.assertEqual(snap["result"]["outcome"], "aborted")
```

（2）`test_calibrate_api_validation_and_auth`（AC5：400 全形态 + 凭证 400/200）
```
    def test_calibrate_api_validation_and_auth(self) -> None:
        """AC5：缺 model → 400；非字符串 model → 400；无 _LAST_AUTH 且无 token → 400
        （error 含'无可用凭证'）；请求体带 token → 200；随后 abort 收尾。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                extra_env={"CTYUN_CALIBRATE_BATCH_GAP_S": "0.1"})
        status, _ = admin_post(self.proc.admin_port, "/api/tpm_calibrate",
                               json.dumps({}).encode("utf-8"))
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/tpm_calibrate",
                               json.dumps({"model": 123}).encode("utf-8"))
        self.assertEqual(status, 400)
        status, _ = admin_post(self.proc.admin_port, "/api/tpm_calibrate",
                               b"not json")
        self.assertEqual(status, 400)
        # 无凭证（代理刚启动、无真实流量）
        status, body = admin_post(self.proc.admin_port, "/api/tpm_calibrate",
                                  json.dumps({"model": "kimi-k3-oc"}).encode("utf-8"))
        self.assertEqual(status, 400)
        self.assertIn("无可用凭证", json.loads(body.decode("utf-8"))["error"])
        # 请求体带 token → 启动成功
        status, body = admin_post(self.proc.admin_port, "/api/tpm_calibrate",
                                  json.dumps({"model": "kimi-k3-oc",
                                              "token": "Bearer t"}).encode("utf-8"))
        self.assertEqual(status, 200)
        # 收尾：abort 停止后台校准
        admin_post(self.proc.admin_port, "/api/tpm_calibrate_abort", b"{}")
```

（3）`test_calibrate_stats_not_polluted`（AC7）
```
    def test_calibrate_stats_not_polluted(self) -> None:
        """AC7：一次完整校准（硬顶小值加速 capped）后 /api/stats 增量 == 0、
        /api/logs 含 probe=1 行、/api/tpm_stats 桶不因校准增长。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port, extra_env={
            "CTYUN_CALIBRATE_BATCH_GAP_S": "0.1",
            "CTYUN_CALIBRATE_HARD_CAP_TOKENS": "120000",  # 1-2 批即 capped，快速收尾
        })
        status, body = admin_post(
            self.proc.admin_port, "/api/tpm_calibrate",
            json.dumps({"model": "kimi-k3-oc", "token": "Bearer t"}).encode("utf-8"))
        self.assertEqual(status, 200, body)
        # 轮询至完成
        deadline = time.time() + 30
        snap = None
        while time.time() < deadline:
            _, body, _ = admin_get(self.proc.admin_port, "/api/tpm_calibrate")
            snap = json.loads(body.decode("utf-8"))
            if not snap["running"]:
                break
            time.sleep(0.2)
        self.assertIsNotNone(snap)
        self.assertFalse(snap["running"], "calibration must finish (capped)")
        # 统计零污染
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/stats")
        stats = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(stats["requests_total"], 0,
                         "calibration must not pollute requests_total")
        self.assertEqual(stats["daily_by_model"], {},
                         "calibration must not pollute daily_by_model")
        self.assertEqual(stats["recent"], [],
                         "calibration must not pollute recent")
        # 日志含 probe=1 标记
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/logs")
        logs = json.loads(body_bytes.decode("utf-8"))
        lines = [entry["line"] for entry in logs["lines"]]
        self.assertTrue(any("probe=1" in line for line in lines),
                        "calibration log line must carry probe=1")
        # tpm_stats 桶不增（校准不调 tpm_admit）
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/tpm_stats")
        tpm_snap = json.loads(body_bytes.decode("utf-8"))
        self.assertEqual(tpm_snap["buckets"], [],
                         "calibration must not touch tpm buckets")
```

#### 验证命令

`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（全量）
预期：285 + 3 = 288 test methods, OK（新增 3 个全绿 + 既有不变）。
注：AC5 的 403 分支在 API 层与 `_handle_probe_post` 结构同款，非本机场景由既有 `test_write_allowed_matrix`（:754，含 `("192.168.1.5","","")→False`）覆盖——集成测试只能从 127.0.0.1 连接，无法造非本机来源，不再重复断言。

### 卡 7 — Dashboard UI：校准按钮/进度/中止/一键应用 + 二选一预算控件 + `#tpm-save` 读取逻辑

- **涉及文件**：`ctyun-stream-fix-proxy.py`（4 处：① :2567-2575 TPM 卡片 HTML 整体替换（`_DASH_SECTIONS_STATIC` 段内）；② :3288-3336 `renderTpmSettings` 整体替换；③ :3337-3369 `#tpm-save` 监听整体替换；④ :3370 `loadTpmSettings();` 之后、:3371 `"""` 之前插入校准交互 JS——以上 ②③④ 均在 `_DASH_JS_V2` 段内）；`ctyun-stream-fix-proxy.test.py`（`AdminIntegrationTest` 新增 3 个用例）
- **tier**：A — HTML/JS 文案与控件结构、radio 二选一渲染、保存链路取值逻辑全部写死；集成断言沿用既有 HTML 源文本门禁风格（:3011-3012 口径：`assertIn("textContent")` + `assertNotIn("innerHTML")`）。
- **覆盖 AC**：AC8（校准 UI 存在性 + `textContent` 门禁 + `renderTpmSettings` 对 `probe`/`enabled_advice` payload 兼容）；AC12②③④⑤（API 每模型无 choices、HTML 新旧文案、`recommended is None` disabled、二选一保存链路取值 + 非法输入不提交）；AC13（一键应用切模式回填 + 校准结果不自动写 `TPM_MODEL_BUDGETS`）。
- **前端 JS 无法在集成测试中执行**：AC12④⑤/AC13 的运行时行为以 JS 源码断言锁定（radio disabled 逻辑、budget===recommended 默认选中、save 取值路径、probe 源一键应用分支），与既有测试的静态源断言口径一致。

#### 生产代码（ctyun-stream-fix-proxy.py）

**插入点 1 — TPM 卡片 HTML 整体替换（:2567-2575，`_DASH_SECTIONS_STATIC` 段内）**：
原代码：
```
  <section class="card" id="tpm-settings-card">
    <div class="card-title">TPM 限流设置（勾选启用的模型）</div>
    <p class="dimmed">仅勾选的模型走限流（桶/排队/429），其余模型直通；预算档位由上游拒绝实证推荐。加载中……</p>
    <div id="tpm-models"></div>
    <div class="actions">
      <button id="tpm-save" type="button">保存限流设置</button>
      <p class="msg" id="tpm-msg" role="status"></p>
    </div>
  </section>
```
替换为：
```
  <section class="card" id="tpm-settings-card">
    <div class="card-title">TPM 限流设置（勾选启用的模型）</div>
    <p class="dimmed">仅勾选的模型走限流（桶/排队/429），其余模型直通；预算推荐值由上游拒绝实证或校准探测得出。加载中……</p>
    <div id="tpm-models"></div>
    <div class="calibrate-bar dimmed" id="tpm-cali-status" style="display:none">
      <span id="tpm-cali-status-text"></span>
      <button id="tpm-cali-abort" type="button" style="display:none">中止校准</button>
    </div>
    <div class="actions">
      <button id="tpm-save" type="button">保存限流设置</button>
      <p class="msg" id="tpm-msg" role="status"></p>
    </div>
  </section>
```
（本插入使 _DASH_SECTIONS_STATIC 后续行号 +4；后续 ②③④ 的插入用整段函数替换锚定，不受行号漂移影响。）

**插入点 2 — `renderTpmSettings` 整体替换（:3288-3336）**：
原代码（:3288-3336，含 select/choices 遍历与 label 档位拼接）整段替换为：
```
function renderTpmSettings(data) {
  var box = $("tpm-models");
  box.textContent = "";
  var models = data.models || [];
  if (!models.length) {
    box.appendChild(el("p", "empty", "暂无模型流量记录"));
    return;
  }
  for (var i = 0; i < models.length; i++) {
    var m = models[i];
    var rec = m.recommend || {};
    var hasRec = rec.recommended !== null && rec.recommended !== undefined;
    var row = el("div", "tpm-row");
    var check = document.createElement("input");
    check.type = "checkbox";
    check.id = "tpm-check-" + i;
    check.setAttribute("data-model", m.name);
    check.checked = !!m.enabled;
    var label = el("label", "", "");
    label.setAttribute("for", "tpm-check-" + i);
    label.textContent = m.name;
    row.appendChild(check);
    row.appendChild(label);
    // 二选一预算设置：radio「使用推荐值 N」/「自定义」+ 数字输入框
    var modeGroup = "tpm-mode-" + i;
    var radioRec = document.createElement("input");
    radioRec.type = "radio";
    radioRec.name = modeGroup;
    radioRec.id = modeGroup + "-rec";
    radioRec.setAttribute("data-model", m.name);
    radioRec.value = "recommended";
    if (!hasRec) {
      radioRec.disabled = true;  // 无推荐值只能自定义（AC12④）
    } else {
      radioRec.setAttribute("data-rec-value", String(rec.recommended));
      if (m.budget === rec.recommended) radioRec.checked = true;
    }
    var labelRec = el("label", "", "");
    labelRec.setAttribute("for", modeGroup + "-rec");
    labelRec.textContent = hasRec
      ? "使用推荐值 " + rec.recommended : "使用推荐值（暂无）";
    row.appendChild(radioRec);
    row.appendChild(labelRec);
    var radioCust = document.createElement("input");
    radioCust.type = "radio";
    radioCust.name = modeGroup;
    radioCust.id = modeGroup + "-cust";
    radioCust.setAttribute("data-model", m.name);
    radioCust.value = "custom";
    if (!hasRec || m.budget !== rec.recommended) radioCust.checked = true;
    var labelCust = el("label", "", "");
    labelCust.setAttribute("for", modeGroup + "-cust");
    labelCust.textContent = "自定义";
    row.appendChild(radioCust);
    row.appendChild(labelCust);
    var inputCust = document.createElement("input");
    inputCust.type = "number";
    inputCust.id = modeGroup + "-val";
    inputCust.setAttribute("data-model", m.name);
    inputCust.setAttribute("aria-label", "自定义预算值");
    inputCust.setAttribute("min", "1");
    inputCust.setAttribute("step", "1000");
    if (m.budget !== null && m.budget !== undefined) {
      inputCust.value = String(m.budget);
    }
    row.appendChild(inputCust);
    // 校准测试按钮（事件由 #tpm-models 委托处理）
    var caliBtn = el("button", "tpm-cali-btn", "校准测试");
    caliBtn.setAttribute("type", "button");
    caliBtn.setAttribute("data-cali-model", m.name);
    caliBtn.title = "对 " + m.name + " 发起校准探测（约消耗一次上游限额的 token 配额）";
    row.appendChild(caliBtn);
    // 一键应用（仅 probe 源：校准刚完成，推荐值来自实测；AC13）
    if (rec.source === "probe" && hasRec) {
      (function (idx, recVal) {
        var applyBtn = el("button", "tpm-apply-btn", "一键应用");
        applyBtn.setAttribute("type", "button");
        applyBtn.title = "将该模型预算设置为推荐值 " + recVal;
        applyBtn.addEventListener("click", function () {
          var radioRec = box.querySelector(
            "input[name='tpm-mode-" + idx + "'][value='recommended']");
          if (radioRec) {
            radioRec.checked = true;
            radioRec.setAttribute("data-rec-value", String(recVal));
          }
        });
        row.appendChild(applyBtn);
      })(i, rec.recommended);
    }
    // 提示文案：enabled_advice.reason 优先，回退 hint / 推荐值展示
    if (rec.enabled_advice) {
      row.appendChild(el("span", rec.enabled_advice.suggest ? "hint ok" : "hint",
                         rec.enabled_advice.reason));
    } else if (rec.hint) {
      row.appendChild(el("span", "hint", rec.hint));
    } else if (hasRec) {
      row.appendChild(el("span", "hint", "推荐 " + rec.recommended +
                         "（样本 " + (rec.samples || 0) + "）"));
    }
    box.appendChild(row);
  }
}
```

**插入点 3 — `#tpm-save` 监听整体替换（:3337-3369）**：
原代码（:3337-3369，`select[data-model]` 读取逻辑）整段替换为：
```
$("tpm-save").addEventListener("click", function () {
  var msg = $("tpm-msg");
  var box = $("tpm-models");
  var checks = box.querySelectorAll("input[type=checkbox][data-model]");
  var budgets = {};
  for (var i = 0; i < checks.length; i++) {
    if (!checks[i].checked) continue;
    var model = checks[i].getAttribute("data-model");
    var modeRec = box.querySelector(
      "input[name='tpm-mode-" + i + "'][value='recommended']");
    if (modeRec && modeRec.checked) {
      var recVal = parseInt(modeRec.getAttribute("data-rec-value"), 10);
      if (!recVal || recVal <= 0) {
        msg.className = "msg err";
        msg.textContent = "模型 " + model + " 暂无推荐值，请选择自定义预算";
        return;
      }
      budgets[model] = recVal;
    } else {
      var input = box.querySelector("#tpm-mode-" + i + "-val");
      var v = input ? parseInt(input.value, 10) : 0;
      if (!v || v <= 0) {
        msg.className = "msg err";
        msg.textContent = "模型 " + model + " 自定义预算值需为正整数";
        return;
      }
      budgets[model] = v;
    }
  }
  msg.className = "msg wait";
  msg.textContent = "保存中……";
  fetch("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tpm_model_budgets: budgets })
  }).then(function (resp) {
    return resp.json().then(function (data) { return { status: resp.status, data: data }; });
  }).then(function (r) {
    if (r.status === 200) {
      msg.className = "msg ok";
      msg.textContent = "已保存，立即生效";
      loadTpmSettings();
    } else {
      msg.className = "msg err";
      msg.textContent = (r.data && r.data.error) ? r.data.error : ("保存失败：HTTP " + r.status);
    }
  }).catch(function (err) {
    msg.className = "msg err";
    msg.textContent = "保存失败：" + String(err) + " —— 确认能访问管理接口 /api/config。";
  });
});
```

**插入点 4 — 校准交互 JS（:3370 `loadTpmSettings();` 之后、:3371 `"""` 之前，`_DASH_JS_V2` 段内）**：
```
// --- TPM 校准交互（校准测试 / 轮询 / 中止；一键应用回填在 renderTpmSettings 内） ---
var _calibModel = null;
function calibStart(model) {
  if (!confirm("对 " + model + " 发起校准探测？\n\n"
             + "将发送多批大输入请求逐级加压实测上游 TPM 阈值，"
             + "约消耗一次上游限额的 token 配额。\n\n"
             + "建议在低流量时段操作。")) {
    return;
  }
  fetch("/api/tpm_calibrate", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: model })
  }).then(function (resp) {
    return resp.json().then(function (data) { return { status: resp.status, data: data }; });
  }).then(function (r) {
    if (r.status === 200) {
      _calibModel = model;
      $("tpm-cali-status").style.display = "";
      $("tpm-cali-status-text").textContent = "校准 " + model + " 中…";
      $("tpm-cali-abort").style.display = "";
      calibPoll();
    } else {
      $("tpm-cali-status").style.display = "";
      $("tpm-cali-status-text").textContent = "校准启动失败：" +
        ((r.data && r.data.error) ? r.data.error : ("HTTP " + r.status));
    }
  }).catch(function (err) {
    $("tpm-cali-status").style.display = "";
    $("tpm-cali-status-text").textContent = "校准启动失败：" + String(err);
  });
}
function calibPoll() {
  fetch("/api/tpm_calibrate")
    .then(function (resp) { return resp.json(); })
    .then(function (snap) {
      if (!snap.running) { calibDone(snap); return; }
      var p = snap.progress || {};
      $("tpm-cali-status-text").textContent =
        "校准 " + (snap.model || _calibModel || "") + " 中… 批 " + (p.batch || 0) +
        "，已消耗 " + (p.consumed || 0) + " tokens";
      setTimeout(calibPoll, 1500);
    })
    .catch(function () { setTimeout(calibPoll, 3000); });
}
function calibDone(snap) {
  var res = snap.result || {};
  var txt = "校准完成：" + (res.outcome || "unknown");
  if (res.threshold) {
    txt += "，实测阈值 " + res.threshold + " tokens（推荐 " +
           Math.floor(res.threshold * 0.9 / 1000) * 1000 + "）";
  }
  txt += "。";
  $("tpm-cali-status-text").textContent = txt;
  $("tpm-cali-abort").style.display = "none";
  loadTpmSettings();  // 刷新 recommend.probe / enabled_advice / 一键应用按钮
  _calibModel = null;
}
$("tpm-cali-abort").addEventListener("click", function () {
  fetch("/api/tpm_calibrate_abort", { method: "POST", body: "{}" })
    .then(function () {
      $("tpm-cali-status-text").textContent = "已请求中止，等待当前批次结束…";
    });
});
$("tpm-models").addEventListener("click", function (e) {
  if (e.target && e.target.getAttribute("data-cali-model")) {
    calibStart(e.target.getAttribute("data-cali-model"));
  }
});
```

#### 测试代码（ctyun-stream-fix-proxy.test.py）

`AdminIntegrationTest`（:2899）新增 3 个测试方法（沿用既有 HTML 源文本断言口径）：

（1）`test_dashboard_tpm_two_choice_and_calibrate_ui`（AC8 + AC12③ + XSS 门禁 + 旧档位零残留）
```
    def test_dashboard_tpm_two_choice_and_calibrate_ui(self) -> None:
        """AC8/AC12③：HTML 含校准测试/中止校准/一键应用/使用推荐值/自定义文案；
        不含旧档位文案（宽松×1.5/宽松×2/默认×2/默认/2）与 select choices 标记；
        不含 innerHTML（XSS 门禁）。"""
        _, html_bytes, _ = admin_get(self.proc.admin_port, "/")
        html = html_bytes.decode("utf-8")
        for text in ("校准测试", "中止校准", "一键应用", "使用推荐值", "自定义",
                     "tpm-cali-status", "tpm-cali-abort", "/api/tpm_calibrate",
                     "/api/tpm_calibrate_abort", "data-cali-model",
                     "data-rec-value", "tpm-mode-"):
            self.assertIn(text, html)
        # 旧档位文案与 select choices 标记零残留
        for stale in ("宽松×1.5", "宽松×2", "默认×2", "默认/2", "预算档位",
                      "tpm-select", "m.recommend.choices",
                      "recommend.choices"):
            self.assertNotIn(stale, html, "stale UI text must be gone: %s" % stale)
        # XSS 门禁：动态数据禁走 innerHTML，必须 textContent（沿用 :3011-3012 口径）
        self.assertNotIn("innerHTML", html)
        self.assertIn("textContent", html)
```

（2）`test_dashboard_tpm_settings_render_contract`（AC8 渲染分支 + AC12④ 源码锁定）
```
    def test_dashboard_tpm_settings_render_contract(self) -> None:
        """AC8/AC12④：renderTpmSettings 对 probe/enabled_advice/source 的渲染分支存在；
        recommended=None → radio disabled + 默认自定义（JS 源锁定，
        集成测试无法执行 JS——与既有静态源断言口径一致）。"""
        _, html_bytes, _ = admin_get(self.proc.admin_port, "/")
        html = html_bytes.decode("utf-8")
        self.assertIn("enabled_advice", html)
        self.assertIn("rec.source", html)
        self.assertIn("rec.enabled_advice.suggest", html)
        self.assertIn("radioRec.disabled = true", html)  # AC12④
        self.assertIn("m.budget === rec.recommended", html)  # 默认选中逻辑
        self.assertIn('rec.source === "probe"', html)  # 一键应用仅 probe 源
        self.assertIn("tpm-apply-btn", html)
```

（3）`test_tpm_settings_api_no_choices_anywhere`（AC12② 集成级）
```
    def test_tpm_settings_api_no_choices_anywhere(self) -> None:
        """AC12②：GET /api/tpm_settings 每模型 recommend 均不含 choices 键，
        且含 enabled_advice/source。"""
        self.proc = start_proxy(self.upstream_port, self.proxy_port,
                                seed_persist={"tpm_model_budgets":
                                              {"kimi-k3-oc": 30000}})
        post_sse(self.proxy_port)  # 无 auth，model=deepseek-v4-pro-0813-oc
        _, body_bytes, _ = admin_get(self.proc.admin_port, "/api/tpm_settings")
        snap = json.loads(body_bytes.decode("utf-8"))
        self.assertGreaterEqual(len(snap["models"]), 1)
        for m in snap["models"]:
            self.assertNotIn("choices", m["recommend"])
            self.assertIn("enabled_advice", m["recommend"])
            self.assertIn("source", m["recommend"])
```

#### 验证命令

`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（全量）
预期：288 + 3 = 291 test methods, OK（新增 3 个全绿 + 既有 288 不退化——既有 `test_dashboard_and_stats_served` 的 TPM 区块断言 :3053-3061 全部仍命中：`TPM 限流设置`/`id="tpm-models"`/`id="tpm-save"`/`/api/tpm_settings`/`id="tpm-msg"`/`data-model`/`loadTpmSettings`/`renderTpmSettings` 均保留在替换后的源码中）。

### 卡 8 — 收尾：全量回归 + 死代码清理 + docstring 同步

- **涉及文件**：`ctyun-stream-fix-proxy.py`（清理 + docstring 核对）；`ctyun-stream-fix-proxy.test.py`（如卡 1-7 有遗漏断言在此补齐）
- **tier**：A — 纯收尾：无新增功能，跑全量回归贴证。
- **覆盖 AC**：AC11（既有全量用例全绿 + 两参调用行为回归）、AC12⑥（3 个更新用例全绿）、全局 0 failure / 0 error。

#### 死代码清理清单（逐条执行，全部应有确定性结果）

1. 旧档位文案零残留：
```
grep -n "宽松×\|默认×\|默认/2\|预算档位\|tpm-select" ctyun-stream-fix-proxy.py
```
预期：无输出（卡 3 删 choices 生成、卡 7 删 select 遍历与卡片文案后应零命中）。

2. `choices` 残留限定在上游响应 schema 解析路径（`sse_line_classify`/`body_has_error` 读取 OpenAI 响应的 `choices` 字段属正常，非本次删除范围）：
```
grep -n "choices" ctyun-stream-fix-proxy.py
```
预期命中行仅限：`sse_data_line_kind`（:219-255 区间，`data.get("choices")` 读取上游 OpenAI 响应 schema）、SSE 常量注释等上游响应解析；`tpm_budget_recommend`/`tpm_settings_snapshot`/`renderTpmSettings`/`#tpm-save` 路径**零命中**；`DASHBOARD_HTML` 的 JS 源内 `m.recommend.choices` 零命中。

3. `recommend["choices"]`/`rec["choices"]` 全仓零残留（含测试）：
```
grep -n 'choices' ctyun-stream-fix-proxy.test.py | grep -v SSE_ | grep -v '"choices"'
```
预期：无输出（3 个既有用例已在卡 3 更新，其余 `choices` 命中均为 SSE 测试载荷）。

4. 未使用 import/变量核对（逐条确认后如发现未用即删）：
- `import uuid`（卡 5 加）→ `calibrate_start` 使用 ✓ 保留
- `import math`（卡 1 加）→ `_prune_body_err_samples`/`load_tpm_body_err_samples` 等使用 ✓ 保留
- `TPM_PROBE_RESULTS_MAX` → `_prune_probe_results`/`load_tpm_probe_results` 使用 ✓
- `_CALIBRATE_BYTES_PER_STEP` → `_calibrate_send` 使用 ✓
- `est` 参数：`_calibrate_send(model, batch, est, auth_token)` 的 `est` 参数当前未读（engine 传名不传实）——保留签名参数并在 docstring 注明"est 由 engine 传入供未来观测/断言用"，**不删**（与卡 4 契约 `send_one(batch, est)` 一致）。
- 逐条过一遍卡 1-7 新增代码的 docstring 与代码一致（无陈旧描述：如 `tpm_budget_recommend` docstring 不含 choices、`tpm_settings_snapshot` docstring 含三源快照说明、`_log` 签名含 probe kwarg）。

#### 验证命令

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
```
预期：`Ran 291 tests ... OK`（0 failure / 0 error；含卡 3 更新的 3 个既有用例 `test_tpm_budget_recommend_with_samples`/`test_tpm_budget_recommend_no_samples`/`test_model_list_auto_generated` 全绿 = AC11 + AC12⑥）。

```
grep -n "宽松×\|默认×\|默认/2" ctyun-stream-fix-proxy.py
```
预期：无输出（exit 1）。

本卡无新增测试方法——收尾卡只做清理 + 全量贴证；测试计数与卡 7 相同（291）。

## tier 判定总述

**8 卡全部 tier A，无 B/C 卡。** 判据：PLAN 阶段能把卡内生产代码 + 测试代码完整写死——
- 校准引擎 `calibrate_engine` 注入发送器，单元测试用脚本化假发送器穷举"第 N 次拒绝/硬顶/墙钟/abort"全部时序，无需真机；
- 集成测试全部复用现有基建：`start_proxy`（实测 :480，支持 `seed_persist` 预写持久化文件）、`make_scripted_upstream`（:325 脚本化上游响应）、`admin_post`/`admin_get`（:651/:639）、`AdminIntegrationTest`（:2899）——AC 无一条依赖真实上游、真机环境或运行时不可复现数据；
- UI 卡断言沿用既有 HTML 文本门禁（:3011-3012 风格），无浏览器/视觉验收依赖。
- C 档三留白（真机验证/运行时数据依赖/DI 集成边界实测）均不命中，故无 C 卡。

## 分段写作状态

- [x] B0 — 骨架（Header/Global Constraints/卡清单/锚点核对/卡序建议）：本文件。
- [x] B1 — 填卡 1-2（常量与持久化层 save/load/prune；样本持久化优先 + 真实流量追加点 + main() 加载）：已完成，卡体含完整生产/测试代码。
- [x] B2 — 填卡 3-4（recommend 去 choices + 三源优先级 + snapshot 改造 + 3 个既有测试定点更新；calibrate_engine 纯逻辑 + 探测请求构造）：已完成，卡体含完整生产/测试代码。
- [x] B3 — 填卡 5-6（daemon + 发送器 + _LAST_AUTH、Admin API 三端点）：已完成，卡体含完整生产/测试代码。
- [x] B4 — 填卡 7-8（Dashboard UI 二选一 + 校准交互、收尾回归）：已完成，卡体含完整生产/测试代码与清理清单。
- [x] B-final — 跨全卡 Self-Review 5 项（spec coverage / placeholder scan / type consistency / 可落盘性 / 锚点实测）+ 就地修正：已完成（修正清单见各卡内笔记与本次报告）。
