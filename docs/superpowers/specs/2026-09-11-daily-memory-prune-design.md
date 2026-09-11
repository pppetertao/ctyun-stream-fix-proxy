# daily 内存态 prune 设计（STATS["daily"] 无界增长修复）

来源：`docs/superpowers/followups.md:4`（daily-by-model-prune episode 的姊妹问题）。canonical = 项目目录，`/usr/bin/python3` 3.9.6，纯 stdlib。

## Goal

修复 `save_stats_counters` 中 `_prune_daily` 只 prune 锁内浅拷贝副本、内存 `STATS["daily"]` 随运行天数线性增长（无界）的泄漏——对齐已落地的 `STATS["daily_by_model"]` 内存态 prune 模式（:247）。

## Files to Change

### 1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`

`save_stats_counters(path: str) -> None`（:241）锁内块（:243-248）重排。**方案决策（写死）：**

- **方案 A（推荐，采纳）**：锁内先对 `STATS["daily"]` 原地 prune，再浅拷贝；删 ：248 的副本 prune 行。
  优点：① prune 与拷贝同锁原子，磁盘副本 = prune 后内存态，两态收敛；② 与 ：247 `daily_by_model` 行完全对称（可读性/可审性）；③ 无死代码——prune 后副本再 `_prune_daily(daily)` 是 len≤90 早退的空转（违反无死代码铁律）。
- 方案 B（否决）：保留 ：248，仅锁内加一行 prune（两行并存）。缺点：:248 变成永久空转的死代码；prune 路径二义。
- 方案 C（否决）：拷贝后再原地 prune 内存态（先 ：246 拷贝后 prune）。缺点：磁盘含已被 prune 的旧天，磁盘与内存不一致；拷贝先于 prune 无任何收益。

目标代码（替换 ：246-248 三行；:244-245 counters 行原样）：

```python
        _prune_daily(STATS["daily"])           # 内存态原地 prune（副本 prune 修不了内存增长）
        _prune_daily(STATS["daily_by_model"])  # 既有 :247 原样保留
        daily = {k: dict(v) for k, v in STATS["daily"].items()}  # prune 后拷贝：磁盘与内存一致
```

原 ：248 `_prune_daily(daily)` 整行删除。`_prune_daily`（:220-228）本身零改动（原地 `del` + len≤90 早退，语义已满足）；:223 docstring「调用方须持 STATS_LOCK（对内存态调用时）」改后仍准确。

### 2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`

- `test_daily_prune_on_save`（:699-717）断言迁内存态。**方案决策（写死）：内存断言为主 + 保留既有文件断言作双重保障**（`daily` 与 `daily_by_model` 不同——它落盘 ：257，磁盘契约是真实约束；文件断言 len(saved)≤90 同时守护「先拷贝后 prune」的顺序 bug：若顺序错，内存过但文件 95 天即 fail）。`save_stats_counters(path)`（:711）之后、`finally`（:712）之前加（结构同 ：735-744 dbm 测试）：

```python
            d = mod.STATS["daily"]  # 断言内存态：prune 必须落到 STATS["daily"] 原对象
            self.assertLessEqual(len(d), 90, "in-memory daily must be pruned on save")
            self.assertNotIn("2026-01-01", d, "oldest buckets must be pruned from memory")
            self.assertIn("2026-04-11", d, "most recent bucket must survive prune")
            self.assertEqual(d["2026-04-11"]["requests"], 94, "surviving buckets untouched")
```

既有文件断言（:714-717）原样保留；try/finally 整体替换 `STATS["daily"]` 的模式（:704-713）兼容原地 prune，零改动。

- 新增 `test_daily_prune_exact_boundary`：逐字镜像 ：748 `test_daily_by_model_prune_exact_boundary` 结构，桶改 daily 平桶形状（`{"requests": i, "filtered": 0, "errors_proxy": 0, "errors_upstream": 0, "retries": 0}`）。断言：恰好 90 桶 → save 后内存 `len == 90`、`"2026-01-01"` 仍在内存、文件 `len == 90`（off-by-one 守护，先例 ：748）。

### 3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/docs/superpowers/followups.md`

删除第 4 行（本 followup 已被本 episode 消化，留行即陈旧引用）。

## Acceptance Criteria

- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（runner ：1303-1309）全绿，含迁移后的 `test_daily_prune_on_save` 与新增 `test_daily_prune_exact_boundary`。
- 迁移测试证伪旧实现：还原 ：246-248 旧代码跑迁移测试必须 fail（内存断言 95>90）——验证断言确实迁到内存态。
- `grep -n "_prune_daily" ctyun-stream-fix-proxy.py` 仅剩 ：247 区域锁内两处调用（:220 定义 + 锁内 2 次），：248 独立调用消失。
- `stats_snapshot`（:410）/ `range_stats`（:205）消费路径零改动且测试全绿（90 天 retention 下窗口数据完整）。

## Risks

- **顺序敏感**：prune 必须在 ：246 拷贝之前。若实现成先拷贝后 prune，磁盘含 95 天——文件断言 len(saved)≤90 兜底捕获。
- **手工改坏的持久化文件**：`load_daily_buckets`（:285-300）不 prune，>90 天脏文件在首次 save（60s 脏刷 ：311 / SIGTERM ：1193）才收敛——与 daily_by_model 既有行为一致，瞬态非回归。
- **锁持有时间**：prune 移入锁内，O(n log n) 排序 n≈运行天数（百级），可忽略；daily_by_model（:247）已在锁内同量级先例。
- **Python 3.9**：无新语法，纯语句重排，无兼容风险。

## Exclusions

- 持久化 schema：`json.dump`（:256-257）键结构零改动。
- `STATS["daily_by_model"]` 已落地 prune（:247）零改动（仅对称参照）。
- 读取/聚合路径：`load_daily_buckets`（:285）、`main()` 恢复（:1183-1189）、`stats_snapshot`（:407-422）、`aggregate_daily_range`（:188）、`range_stats`（:205）零改动。
- 触发点与 retention：`DAILY_RETENTION_DAYS=90`（:46）不变；触发仍为 save_stats_counters 单一收口（60s 脏刷 ：303-317 / SIGTERM ：1193 / set_upstream_base），不新增。**range_stats 无影响依据**：最远窗口 `last_month` 回溯 ≤62 天（12-31 → 11-01 = 61 天），90 > 62，prune 永不触及窗口桶；且 `aggregate_daily_range`（:195-197）对缺失 key 静默跳过，边界竞态也只退化为 0 不崩。
- 前端零改动。
