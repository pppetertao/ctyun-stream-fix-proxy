# daily_by_model 持久化设计（2026-09-11）

## Goal
把 `STATS["daily_by_model"]`（日期→模型→5字段矩阵）落盘到现有持久化文件，重启后原样恢复，消除"重启清零"；不改存储形状、不改 retention、不动累计 counters 路径。

## Files to Change

### 1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`

- **`save_stats_counters()`:241-258 — dump 增加 `daily_by_model` 键**。方案 D1/D2 已定：在 `STATS_LOCK` 块内（:243-248）追加双层深拷贝（复用 `stats_snapshot()`:411-413 的既有拷贝模式）：
  ```python
  daily_by_model = {d: {m: dict(v) for m, v in models.items()}
                    for d, models in STATS["daily_by_model"].items()}
  ```
  `json.dump`（:256-257）改为 `dict(counters, daily=daily, daily_by_model=daily_by_model)`。`:247` 的 `_prune_daily(STATS["daily_by_model"])` 内存态 prune 已存在，落盘的是 prune 后拷贝，≤90 天天然保证，`_prune_daily`:220-228 本体零改动。

- **新增 `load_daily_by_model_buckets(path: str) -> dict`（置于 `load_daily_buckets`:285-300 之后）— 嵌套清洗**（方案 D3，不扩展 `load_daily_buckets`，保持其单层职责与既有测试不动）：
  ```python
  def load_daily_by_model_buckets(path: str) -> dict:
      """读日期→模型→_DAILY_FIELDS 矩阵；缺/损坏/legacy 无 daily_by_model 键 → {}。"""
      stats = _load_persist_file(path).get("stats")
      raw = stats.get("daily_by_model") if isinstance(stats, dict) else None
      if not isinstance(raw, dict):
          return {}
      out = {}
      for key, models in raw.items():
          # 外层 ISO 日期容错：fromisoformat + round-trip（拒 "20260101"/"2026-1-1"
          # 等非规范形；_prune_daily:226 依赖 ISO 字典序排序，坏 key 必须挡在内存外）
          try:
              d = datetime.date.fromisoformat(key)
          except (ValueError, TypeError):
              continue
          if str(d) != key or not isinstance(models, dict):
              continue
          bucket = {}
          for model, entry in models.items():
              # 内层逐模型清洗 + BY_MODEL_CAP=32(:45) 截断：按文件出现序保留前 32，
              # 与运行时 "len < BY_MODEL_CAP 才插新键"（:349/:394）语义对齐
              if not isinstance(entry, dict) or len(bucket) >= BY_MODEL_CAP:
                  continue
              clean = {}
              for field in _DAILY_FIELDS:  # :282，同 load_daily_buckets:296-298 逐字段规则
                  value = entry.get(field)
                  clean[field] = value if isinstance(value, int) and value >= 0 else 0
              bucket[model] = clean
          out[key] = bucket
      return out
  ```
  边界：entry 非 dict 跳过；字段缺/负/非 int → 0；超 32 模型截断。恢复出的 entry 必含全 5 键，满足 `:384-404` 空流重试计数对"形状同步否则 KeyError"的前置承诺（:387 注释）。

- **`main()`:1181-1189 — 恢复一行**。`:1182` 后加 `daily_by_model = load_daily_by_model_buckets(PERSIST_PATH)`；锁内 `:1188` 后加 `STATS["daily_by_model"] = daily_by_model`。

- **副表标题 `:824`** — `内存累计，重启清零` → `跨重启保留（每 60s 落盘）`。

- **footer `:851`** — `按天×模型计数自进程启动累计，不持久化（重启清零）；` → `按天×模型计数同 daily 口径跨重启保留（90 天 prune，副表数值 ≤ 主表，差值=当日无 model 请求，见下行）；`（与 :849-850、:852 现文案衔接，不引入新口径矛盾）。

### 2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`

- **`test_daily_by_model_prune_on_save`:757-784** — 删 `:774` "daily_by_model 不落盘" 注释与仅内存断言的限定；保留内存态断言（:775-777），`finally` 后新增文件断言：`json.load(open(path))["stats"]["daily_by_model"]` ≤90 且含最新桶（对齐 `test_daily_prune_on_save`:719-722 模式）。
- **`test_daily_by_model_prune_exact_boundary`:786-811** — 补文件断言：恰好 90 桶全落盘、91 桶 prune 回 90（对齐 :741-744）。
- **新增 `test_daily_by_model_persist_roundtrip`（置于 `test_daily_persist_roundtrip_legacy_and_corrupt`:676 旁同类）**，用例矩阵：
  1. 构造 `STATS["daily_by_model"]`（save 前备份/:762-784 模式恢复）→ `save_stats_counters(path)` → `load_daily_by_model_buckets(path)` 与原值全等；
  2. legacy 文件（`stats` 无该键，形状同 :681-683）→ `{}`；
  3. 损坏：`daily_by_model` 非 dict / 日期桶非 dict / entry 非 dict / 字段负值与非 int → 逐项容错（非法桶跳过、坏字段 0）；
  4. 非 ISO key（`"20260101"`、`"not-a-date"`）→ 跳过；
  5. 单日 40 个模型 → 读回恰 32 个（文件出现序前 32）。

### 3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/README.md`

- **`:77` 口径表副表行** — `主表是（90 天 prune）/ 副表否（重启清零）` → `主表/副表均是（90 天 prune）`；**`:69` 用例数** 45 → 新总数。

**老文件兼容论证**：新键嵌在 `stats` 下，老 reader（`load_stats_counters`:274 只取 4 counters 键、`load_daily_buckets`:288 只取 `stats["daily"]`）对多余键无感知；老文件无 `daily_by_model` 键时新 loader `stats.get(...)` → None → `{}`。双向兼容，零迁移。

## Acceptance Criteria
- `save_stats_counters` 后文件含 `stats.daily_by_model`，内容与内存矩阵一致且 ≤90 日期桶。
- save→load roundtrip 全等（roundtrip 用例 1）；模拟重启（main() 恢复路径）后 `STATS["daily_by_model"]` 与重启前相等。
- legacy 无键文件与各类损坏结构不崩，返回 `{}` 或逐桶清洗结果（用例 2-4）。
- 40 模型文件读回 32（用例 5）；3 个 prune 测试的文件断言全过。
- `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（README:69 用例数同步更新）。
- 三处文案（py:824、py:851、README:77）不再出现"重启清零/不持久化"。

## Risks
- **落盘体积/耗时**：90 天 × ≤32 模型 × 5 字段 ≈ ≤2880 entry，JSON <1MB，60s 周期落盘（flush_stats_if_dirty:303）可接受；实测首版确认文件体积。
- **STATS_LOCK 持锁拷贝开销**：双层 dict 拷贝与 stats_snapshot:411-413 同量级，微秒级，无新锁竞争面。
- **`fromisoformat` 版本差异**：`/usr/bin/python3`（README:69 指定）宽松解析行为随版本不同；round-trip `str(d) != key` 校验与版本无关，实现不得省略。
- **测试共享 `mod.STATS`**：新/改测试必须 `orig_dbm` 备份 + `finally` 恢复（:762-784 既有模式），否则污染 cap/matrix 等用例（:474-479、:620-672）。

## Exclusions
- 存储形状：不转置为模型→日期、不做预聚合/前缀和（联合分布不可由边缘还原，形状保持日期→模型→5字段）。
- retention 值：仍 90 天，不改 `DAILY_RETENTION_DAYS`:46。
- `load_daily_buckets`:285-300 的 daily 主表：不加 ISO 校验（既有行为不动，非 ISO key 隐患另立 followup）。
- 前端渲染逻辑（renderDailyByModel / JS:1044、:1108）：不动，仅改 :824 标题文案。
- 累计 counters 路径（`load_stats_counters`:272-279、4 个 *_total）：不动。
- `_prune_daily`:220-228、`stats_snapshot`:407-422、bump 路径（:347、:392）：不动。
