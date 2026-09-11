# daily_by_model prune 设计（followup: 内存线性增长）

## Goal

`STATS["daily_by_model"]`（外层日期串 key，每天 +1，内层 matrix 受 `BY_MODEL_CAP=32` 约束）只在 `_record_request`(:344) / `_record_empty_retry`(:389) 写入，从不 prune，长期运行内存线性膨胀。本 episode 给它加与 `daily` 同策略（`DAILY_RETENTION_DAYS=90`）的 prune 并补单测。

### 设计决策（已定死）

**D1 实现形态——方案 A：原样复用 `_prune_daily`，不参数化、不单写函数。**
- 方案 A（推荐）：`_prune_daily`(:220) 只做 `sorted(keys)[:-N]` + `del`，与 value 形状无关；`daily_by_model` 外层同为 ISO 日期串 key 的平 dict，字典序=时间序性质两处同享（:171 docstring 已声明该性质），**可直接复用，零改动调用**。仅在 `save_stats_counters` 的 `STATS_LOCK` 块内加一行调用。
- 方案 B（否决）：参数化 `_prune_daily(daily, retention=...)`——retention 两处同值（见 D3），参数无消费者，纯接口膨胀。
- 方案 C（否决）：单写 `_prune_daily_by_model()`——复制 5 行逻辑，DRY 违背，零收益。
- **关键约束：必须 prune 内存态 `STATS["daily_by_model"]`（原地），不能只 prune 副本。** 既有 `daily` 的 prune(:245) 作用于 save 内浅拷贝（:244），磁盘受控但内存 `STATS["daily"]` 实际同样无界——本 episode 不重蹈该模式（该姊妹问题记 followup，见 Risks）。

**D2 触发点——仅 save_stats_counters 路径，不设独立周期、不加启动恢复点。**
- 三个既有入口全覆盖：60s 脏刷 `flush_stats_if_dirty`(:300→:308)、SIGTERM/SIGINT `_on_signal`(:1190)、`set_upstream_base`(:333)；上游脏刷循环 :1213。
- 充分性：key 唯一来源是 `_record_request`/`_record_empty_retry`，两者都置 `_stats_dirty=True`(:369/:401) → 最迟 60s 内必经一次 `save_stats_counters`；无请求则无新 key，无 prune 需求。
- 启动无需 prune：`main()` 恢复路径 :1178-1186 只恢复 `daily`，`daily_by_model` 保持 `{}`（不持久化，重启即清），无恢复态可 prune。

**D3 retention——复用 `DAILY_RETENTION_DAYS=90`（:46），不设新常量/env。**
- 论证：`range_stats`(:205) 最远窗口 `last_month` ≤31 天，90 天留 ≥2 倍回溯余量，聚合永不断档；两处语义同为"日期桶滚动保留"，单一常量防策略漂移；不加新配置面。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`
   - `save_stats_counters(path: str) -> None`(:239) — 在 `with STATS_LOCK:` 块内（:244 `daily = {k: dict(v) ...}` 之后）新增一行 `_prune_daily(STATS["daily_by_model"])`。改后块体：
     ```python
     with STATS_LOCK:
         counters = {k: STATS[k] for k in ("requests_total", "filtered_total", "errors_total",
                                           "empty_retries_total")}
         daily = {k: dict(v) for k, v in STATS["daily"].items()}
         _prune_daily(STATS["daily_by_model"])  # 内存态原地 prune（副本 prune 修不了内存增长）
     _prune_daily(daily)
     ```
   - `_prune_daily(daily: dict) -> dict`(:220) — 仅 docstring 补一行共用说明（函数体零改动）：`value 形状无关（平 dict 按 key prune），daily 与 daily_by_model 共用；调用方须持 STATS_LOCK（对内存态调用时）`。边界分支不动：`len(daily) <= DAILY_RETENTION_DAYS` 早返回（恰好 90 全保留），`sorted(daily)[:-90]` 逐个 `del`。
2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`
   - `class ProxyDashboardUnitTest`(:362) 内新增 3 个用例，参照 `test_daily_prune_on_save`(:699) 模式：swap `mod.STATS["daily_by_model"]` + `try/finally` 还原（:704-713 同款）、`tempfile.mkdtemp` + `addCleanup(shutil.rmtree)`(:701-703 同款)、**断言对象是内存态 `mod.STATS["daily_by_model"]`（不能读回持久化文件——daily_by_model 不落盘）**。植入 entry 用 5 字段形状（:347-349 同款）保持自洽：
     - `test_daily_by_model_prune_on_save`：植入 95 个旧日期 key（`"2026-%02d-%02d"` 生成器 :708 同款）+ `mod.today_key()`；调 `mod.save_stats_counters(path)` 后断言 `len ≤ 90`、最旧 key `assertNotIn`、`today_key()` `assertIn`、存活桶 inner entry 内容未被改动。
     - `test_daily_by_model_prune_exact_boundary`：恰 90 key → save 后 len 不变（走 `<=` 早返回分支）；补 1 个更新日期成 91 → save 后 len==90 且仅最旧 1 个被删。
     - `test_daily_by_model_prune_empty`：`{}` → save 不抛异常且仍为 `{}`。

## Acceptance Criteria

- `python3 ctyun-stream-fix-proxy.test.py -v`（:1239 `unittest.main(verbosity=2)`）exit 0：上述 3 个新用例通过，既有用例零回归（含 :474 `test_daily_by_model_cap`、:699 `test_daily_prune_on_save`、:719 `test_stats_snapshot_daily_is_copy`）。
- 超期 key 从**内存** `STATS["daily_by_model"]` 消失（非仅落盘副本），验证点：单测直接断言 `mod.STATS["daily_by_model"]`。
- 持久化文件 schema 不变：save 输出 json 的 `stats` 键仍只含 4 counters + `daily`（无 `daily_by_model`），由既有 save 路径(:253-254)零改动保证。
- baseline 先行：IMPLEMENT 前跑一次全量测试登记红清单，VERIFY 只对"baseline 绿→现在红"负责。

## Risks

- **锁内原地删 key**：`_prune_daily(STATS["daily_by_model"])` 必须留在 `STATS_LOCK` 块内——`stats_snapshot`(:404-411 双层拷贝) 与写入点(:344/:389)同锁串行，无并发迭代风险；若为"减小锁区"移到锁外对副本操作，则退化为 :245 模式，修不了内存增长（本 spec 的核心禁止项）。
- **姊妹问题出 scope**：`STATS["daily"]` 内存态同样不受 :245 prune 保护（prune 只作用于副本）——同增长率泄漏。本 episode 不修（followup 路由：修它不属于本 acceptance 的必要条件），IMPLEMENT 时在 `docs/superpowers/followups.md` 追加一行。
- **UI 零可见影响**：`renderDailyByModel`(:990-996) 按 `range_bounds`（≤31 天）过滤后才渲染，90 天 retention 不会删掉任何 UI 可见数据；`range_stats`(:416) 只吃 `daily`，与本次改动无交集。
- **性能**：常态 `len <= 90` 早返回 O(1)；超限 `sorted` 为 O(n log n)、n≈91 天，每 60s 一次，可忽略。

## Exclusions

- 持久化 schema：`daily_by_model` 仍不落盘，`load_stats_counters`(:269)/`load_daily_buckets`(:282)/`main()` 恢复路径(:1178-1186)零改动。
- `BY_MODEL_CAP=32` 与"每日独立 cap"逻辑（:346-350、:391-395）不动。
- 前端 `renderDailyByModel`(:990) 及 dashboard 不动。
- `STATS["daily"]` 内存态 prune（独立 followup，另开 episode）。
- 不加新常量、env seam、依赖。
