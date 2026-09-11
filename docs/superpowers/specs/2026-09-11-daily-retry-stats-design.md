# ctyun-proxy 按天统计加「重试」列 + 按模型区分（简化路径 v2，supersede 上版）

背景：SSE 空流重试计数链已通（daily 桶 `retries` 键 + `/api/stats` 已透出，`_record_empty_retry` ctyun-stream-fix-proxy.py:319-337），但 dashboard「按天统计」表只渲染 5 列（:742），且按天维度无模型区分——`STATS["by_model"]` 是累计维度（:101），`STATS["daily"]` 是平桶（无 model key）。本 episode 合并两需求：①按天表补「重试」列；②新增「按天 × 模型」明细（重试为主维度）。canonical = 项目目录，`/usr/bin/python3` 3.9.6，纯 stdlib，禁 3.10+ 语法，前端 ES5 风格（`var`/`function`，无箭头函数）。

## Goal

dashboard「按天统计」可按模型查看每日请求/剥行/重试：主表补「重试」总量列，其下新增「按天 × 模型」副表，口径与既有计数链严格一致，持久化 schema 零变更。

## 口径定义（硬约束）

- **矩阵结构**：新增内存结构 `STATS["daily_by_model"][date_key][model] = {"requests":0,"filtered":0,"retries":0}`；**不持久化**（`save_stats_counters` :184-199 只写 4 counter + daily，新键天然不落盘），与 `by_model`「重启清零」语义一致（footer :763 文案同步）。
- **重试归因**：归 `_record_empty_retry(model)` 实参 model = attempt-1 请求体提取值（`extract_model` :146-157，`_proxy_relay` :400 提取、:432 传入）——与 `by_model["retries"]` 同一实参。跨天归属 `today_key()`（:160-162 本地时区），即 attempt-1 判空日。
- **缺 model fallback**：model=None（非 JSON body/body 无 model/499 中断路径 :391-392 恒 None）**不进** `daily_by_model`，只进 daily 总桶——`if model:` 与 :288/:326 同构。恒等式：对 k∈{requests,filtered,retries}，`daily[d][k] ≥ Σ_m daily_by_model[d][m][k]`，差值 = 该日无 model 请求。前端**不造「未知」键**（与 chips 口径一致），footer 注明差值含义。
- **cap**：每日子桶模型键数 ≤ `BY_MODEL_CAP=32`（:44），`len(day_models) < BY_MODEL_CAP` 才新建（与 :291/:329 同构），防膨胀。内存上界：每天 ≤32 小 dict × 不 prune（内存 daily 现状同样不 prune，:189-190 只 prune 落盘拷贝）——同量级可接受。
- **总量列 vs 分模型列**：主表 6 列 = 全量（含无 model）；副表 = 分模型明细，两表同卡片，各自独立成立，不互相凑数。
- **接口兼容**：`/api/stats` 新增顶层键 `daily_by_model`，既有键零变更。旧消费者无感；`test_stats_snapshot_shape` :488-492 是 assertIn 非精确集合，不破坏。

**方案对比**：A 新顶层内存结构（推荐——持久化 schema 零变更，与 by_model 语义对齐）；B daily 桶内嵌 by_model 并持久化（需动 `_DAILY_FIELDS` :224 清洗/roundtrip/`load_daily_buckets` :227-245 兼容分支 + 持久化膨胀 32×90 天 + 打破不持久化语义，弃）；C 前端从 recent 聚合（窗口仅 100 条，弃）。

## Files to Change

### 1. 编辑 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py`

- **`:99-101` STATS init**：字典字面量加 `"daily_by_model": {}`。
- **`_record_request` :296 前（:288 `if model:` 块后）**：追加分模型日桶自增——
  ```python
  day_models = STATS["daily_by_model"].setdefault(today_key(), {})
  entry_dm = day_models.get(model)
  if entry_dm is None and len(day_models) < BY_MODEL_CAP:
      entry_dm = day_models[model] = {"requests": 0, "filtered": 0, "retries": 0}
  if entry_dm is not None:
      entry_dm["requests"] += 1
      entry_dm["filtered"] += filtered
  ```
  （置于既有 `if model:` 块内，缩进对齐 :289。）
- **`_record_empty_retry` :319-337**：docstring :320-322 补「daily_by_model」一句；:326 `if model:` 块内同构追加 `entry_dm["retries"] += 1`。
- **`stats_snapshot` :344 后**：`snap["daily_by_model"] = {d: {m: dict(v) for m, v in models.items()} for d, models in STATS["daily_by_model"].items()}`（双层深拷贝，模式同 :343-344）。
- **thead `:742`**：`<th>上游5xx</th>` 后加 `<th>重试</th>`；**`:743`** colspan="5"→"6"；其后追加副表：`<div class="card-title">按天 × 模型（内存累计，重启清零）</div>` + `<table><thead>日期|模型|请求|剥行|重试</thead><tbody id="daily-model-body"><tr><td class="empty" colspan="5">读取中……</td></tr></tbody></table>`（同 `.table-wrap` 包裹）。
- **`renderDaily` :889-911**：`:896` colSpan 5→6；`:908` 后追加 `tr.appendChild(el("td", "num", String(b.retries || 0)));`。
- **新函数 `renderDailyByModel(dbm)`**（置于 :888 前后）：`Object.keys(dbm).sort().reverse().slice(0,14)` 每天遍历模型行（5 td：日期/模型名/requests/filtered/retries，全部 `|| 0` 防御），空态 colspan=5；行 class 沿用 :903 hit 逻辑可选。
- **`poll` :972 后**：追加 `renderDailyByModel(snap.daily_by_model || {});`。
- **footer `:762-765`**：补两句——按天×模型计数不持久化重启清零；主表含无 model 请求故 ≥ 副表合计，差值即无 model 请求。

### 2. 编辑 `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py`

- **新用例 `test_daily_by_model_matrix_fallback`**（置于 `test_daily_bucket_spans_days` :522 后）：`_record_request(model="m-a")` + `_record_request(...)` 无 model + `_record_empty_retry("m-a")` → 断言 `STATS["daily_by_model"][today]["m-a"]["retries"] >= 1`、无 model 请求不入 `daily_by_model`、恒等式 `daily[today][k] >= Σ_m dm[m][k]` 对 requests/retries 成立。
- **cap 并入 `test_by_model_accumulation_and_cap` :474-481**：40 次循环后加 `self.assertLessEqual(len(mod.STATS["daily_by_model"][mod.today_key()]), 32)`。
- **snapshot 并入 `test_stats_snapshot_daily_is_copy` :578-585**：改 `snap["daily_by_model"]` 嵌套值，断言不影响 `STATS["daily_by_model"]`。
- **dashboard 断言并入 `test_dashboard_and_stats_served` :752-755**：`<th>重试</th>`、`colspan="6"`、`id="daily-model-body"`。
- **集成并入 :777 附近 SSE 用例**：`snap["daily_by_model"][today]["deepseek-v4-pro-0813-oc"]["requests"] >= 1`。

### 3. 同步部署副本

- 项目两文件全绿后字节级复制到 `/Users/peter/.local/bin/`（同名），`cmp` 双向 exit 0；`launchctl kickstart -k gui/$UID/com.ctyun-stream-fix-proxy` 重启生效（主代理执行）。

## Acceptance Criteria

- ① `/usr/bin/python3 -m py_compile ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py` exit 0。
- ② `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（41 + 1 新用例），exit 0，贴 stdout 末尾。
- ③ 冒烟：scripted 场景（带 model 请求 + 空 stream 触发 retry + 无 model 请求各≥1）后 `curl /api/stats`：`daily_by_model[today][<model>]["retries"] >= 1`；恒等式 `daily[today]["requests"] == Σ_m daily_by_model[today][m]["requests"] + <无model请求数>` 实测成立；dashboard 主表 6 td/行、副表每活跃模型一行且数值与 JSON 一致。
- ④ `cmp` 项目 vs `~/.local/bin/` 两文件 exit 0。
- ⑤ `node ~/.zcode/scripts/validate-r31-evidence.mjs <本 spec>` exit 0。

## Risks

- **colspan/列序三处同步**：`:743`、`:896`、新 th 与副表 colspan 必须 same-commit 同改，错位由 ② 的 `colspan="6"`/`id="daily-model-body"` 断言兜底。
- **`stats_snapshot` 浅拷贝陷阱**：`snap = dict(STATS)` :342 只拷顶层，漏加深拷贝行 → 前端持引用跨 2s 轮询读到中途态/并发写异常；由 snapshot 隔离用例兜底。
- **cap 边界语义**：每日独立 cap（非全局），同模型跨天都记录；与 by_model 全局 cap 口径略异，测试注释写明。
- **内存不 prune**：daily_by_model 长期运行累积（≤32 键/天小 dict），与内存 daily 现状一致，不另做清理。
- **回归护栏**：不碰 `_record_request` 既有行/`_record_empty_retry` 既有行/`load_daily_buckets`/`save_stats_counters`/`_DAILY_FIELDS`；② 全量绿兜底。

## Exclusions

- daily_by_model 持久化/历史回填（方案 B）；「按模型」chips 卡片加 retries 显示（:869-870 现只显示 requests/filtered，不在本 scope）；顶部累计卡加「重试」。
- 499/非 SSE 重试归因细分；按天 retry 率/成功率列；per-model 错误列（副表只列 请求/剥行/重试）。
- `/api/stats` 既有字段结构变更；`BY_MODEL_CAP`/`DAILY_RETENTION_DAYS` 值调整。

## R31 Evidence

[R31-S1] 展示缺失现场（Read/grep 实测 2026-09-11）：按天表 5 列无重试——
```
$ grep -n '<th>上游5xx</th>\|colspan="5"\|td0.colSpan\|b.errors_upstream' ctyun-stream-fix-proxy.py
742:      <thead><tr><th>日期</th><th>请求</th><th>剥行</th><th>代理错误</th><th>上游5xx</th></tr></thead>
743:      <tbody id="daily-body"><tr><td class="empty" colspan="5">读取中……</td></tr></tbody>
896:    td0.colSpan = 5;
908:    tr.appendChild(el("td", "num", String(b.errors_upstream)));
```
[R31-S2] retries 数据链已全通（grep 实测）——
```
224:_DAILY_FIELDS = ("requests", "filtered", "errors_proxy", "errors_upstream", "retries")
336:        bucket["retries"] += 1
344:        snap["daily"] = {k: dict(v) for k, v in STATS["daily"].items()}
```
[R31-S3] model 维度锚点（grep 实测）：cap=32、提取函数、两处 `if model:` 分支、snapshot 深拷贝、前端 chips 渲染——
```
$ grep -n 'BY_MODEL_CAP = \|def extract_model\|if model:\|snap\["by_model"\] = \|renderModelChips(snap' ctyun-stream-fix-proxy.py
44:BY_MODEL_CAP = 32
146:def extract_model(body):
288:        if model:
326:        if model:
343:        snap["by_model"] = {k: dict(v) for k, v in STATS["by_model"].items()}
970:      renderModelChips(snap.by_model || {});
```
model 实参调用点：`:400 model = extract_model(body)`、`:432 _record_empty_retry(model)`；499 路径 `:391-392 model=None`（fallback 口径依据）。snapshot shape 断言为 assertIn（test :488-492），新增顶层键不破坏。
baseline = 全量 41 用例（`grep -c 'def test_' ctyun-stream-fix-proxy.test.py` → 41）。
