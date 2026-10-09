# E1 Design: 持久化单写者 + 快照冻结（finding A + B）

## Goal

让 `save_stats_counters` 成为持久化文件全量 schema 的唯一写者（独立锁串行 + tmp 唯一化 + set_* 不再窄 schema 覆盖），并在 `stats_snapshot` 锁内把 5 个直方图键的冻结副本写回 snap——消除跨线程写坏 JSON/计数清零竞态与"响应内混合快照+活读"不一致。

## Files to Change

### 1. /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py

**模块级新增锁（放 `STATS_LOCK = threading.Lock()` :1442 旁）：**

```python
_PERSIST_LOCK = threading.RLock()
```

必须 RLock：`_on_signal` :5577 在 **main 线程**执行，若 SIGTERM 恰在 main 线程正跑 `save_stats_counters` 写段（如启动迁移 :5542）时到达，同线程重入 `save_stats_counters`，普通 Lock 自死锁挂死退出路径；RLock 同线程重入安全，跨线程互斥语义与 Lock 相同。

**`save_stats_counters(path: str) -> None` :1733-1774 重构（A）：**
- copy 阶段不动：:1735-1747 持 STATS_LOCK（counters 七键 + prune + daily 系副本 + events）；:1748-1758 持 _CFG_LOCK（base/capture/probe/budgets/样本副本）。两段锁短持、不嵌套，锁序 STATS_LOCK → _CFG_LOCK 保持现状。
- write 阶段 :1759-1774 改为整体包 `with _PERSIST_LOCK:`，且 tmp 唯一化：

```python
    with _PERSIST_LOCK:
        directory = os.path.dirname(path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = "%s.tmp.%d" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({...全量 11 键，逐字不动 :1764-1773...}, fh, ensure_ascii=False)
        os.replace(tmp, path)
```

接口契约：`_PERSIST_LOCK` 必须在 STATS_LOCK/_CFG_LOCK 均释放后获取，持锁块内**零**其他锁获取（保持 copy→write 分离，与 tpm_settle :674-684 的 TPM_LOCK→STATS_LOCK 链不产生新交叉）。磁盘 schema 键集合逐字不动。

**删除 `persist_upstream(base: str, path: str, capture_errors: bool = False, tpm_model_budgets: dict = None) -> None` :1524-1540（A）：** 重构后唯一窄 schema 写者（3 键 :1537-1539）零调用者即删除，消灭"窄写后全量写前崩溃丢 stats"的两阶段窗口。全仓调用点仅 :2336/:2347/:2357 三处（均随下条删除）。

**三个 set_* 去掉窄写（A）：**
- `set_upstream_base(base: str) -> None` :2331-2340：删 :2336-2337 `persist_upstream(...)` 调用；保留 :2333-2335 内存写与锁外 :2340 `save_stats_counters(PERSIST_PATH)`；:2338-2339 注释同步删"persist"相关表述。
- `set_capture_errors(enabled: bool) -> None` :2343-2350：删 :2347-2348；保留 :2346 + :2350。
- `set_tpm_model_budgets(budgets: dict) -> None` :2353-2360：删 :2357-2358；保留 :2356 + :2360。

**`stats_snapshot() -> dict` :2629 写回冻结副本（B）：** 在 :2658（`rates = dict(STATS["rates"])` 之后、STATS_LOCK 块内）追加 5 行：

```python
        snap["ttfb_hist"] = ttfb_hist
        snap["phase_ms"] = phase_hist
        snap["phase_ms_by_model"] = phase_by_model
        snap["stalls_by_model"] = stalls_by_model
        snap["rates"] = rates
```

qwait/settle 两键已在 :2645-2648 写回，不动；:2653-2656 的 `qwait_by_model`/`settle_by_model` 本地副本仍被 perf 分位 :2697-2703 消费，不是重复、勿删。写回后 `snap = dict(STATS)` :2631 的浅引用被全覆盖，序列化（无 indent、C encoder）与 perf 计算同一冻结视图。

### 2. /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py

- 重写 `ProxyDashboardUnitTest.test_persist_upstream_carries_tpm_model_budgets` :1633（persist_upstream 已删）：改断言 `set_tpm_model_budgets({...})` 后 `load_tpm_model_budgets(path)` 读回相等且文件含 `stats` 键（全量 schema）。
- 新增测试类 `PersistSingleWriterTest`（沿用 load_proxy_module + tempfile.mkdtemp 模式，见 :1720-1722）：
  1. **窄覆盖回归**：`_record_request` → `save_stats_counters` → `set_upstream_base(...)` → `load_stats_counters(path)` 的计数 == set 前计数（旧路径此处置 0）。
  2. **三路并发**：`threading.Barrier(3)` 同步 3 线程各 50 轮——a) 直调 `save_stats_counters(path)`（SIGTERM :5577 替身）；b) `set_capture_errors(bool(i%2))` 交替（admin POST :3486-3491 替身）；c) 直调 save（flush :5602 替身）→ 结束后 `json.load(path)` 成功、11 个顶层键齐全、`stats.filtered_total` 单调不减、目录无 `"*.tmp*"` 残留。用必过性断言而非"必现损坏"（竞态非确定性）。
  3. **损坏/丢失回归**：损坏 JSON → `load_stats_counters` 全零（对齐现有 :1727-1732）；文件缺失 → 全零。
- `SnapshotV3Test` :6277 新增 `test_snapshot_v3_hist_keys_are_frozen`：对 5 键断言 `snap[k] is not mod.STATS[k]`；`snap["ttfb_hist"]["m"]` 内容 == `list(mod.STATS["ttfb_hist"]["m"])`（先造一条含 m 的 ttfb 记录）；随后改 `snap["rates"]["bytes_out_total"] += 999` 与 `snap["ttfb_hist"][k][0] += 1`，断言 `mod.STATS` 不变。

## Acceptance Criteria

- `python ctyun-stream-fix-proxy.test.py` 全绿（含既有 8789 行全部用例 + 上述新增）。
- grep 生产文件断言：`persist_upstream` 定义+调用 = 0；`+ ".tmp"` 仅存 `"%s.tmp.%d" % (path, os.getpid())` 形式；`save_stats_counters` 的 `open(`/`os.replace` 均位于 `with _PERSIST_LOCK:` 块内（人工 review 确认）。
- 三路并发 150 轮后文件合法全量 JSON 且无 tmp 残留；set_* 三函数调用后 stats 计数不丢（窄 schema 覆盖回归通过）。
- 5 键冻结回归通过：改 snap 副本不动 STATS。
- **口径纠偏（B）**：本次不是崩溃修复——现状 json.dumps 无 indent 走 C encoder、无 dict changed-size 守卫，不抛 RuntimeError 也不 500（tracer 已证）；B 的验收标准是**快照一致性**（锁内写回冻结副本），验收不设"响应不再 500"类断言。

## Risks

- **磁盘 schema 不得变**：:1764-1773 的 11 个顶层键与 stats 子键集合逐字保留——load_* 白名单（:1792-1795 等）与升级历史兼容依赖它；键增删需独立 episode。
- **锁序纪律**：_PERSIST_LOCK 只包写段，持锁块内禁取 STATS_LOCK/_CFG_LOCK/TPM_LOCK；违反则与 tpm_settle :674-684 的 TPM_LOCK→STATS_LOCK 链形成潜在死锁。copy 两段锁 :1735-1758 保持原状不并入持久化锁。
- **信号重入**：SIGTERM 在 main 线程到达且 main 正持 _PERSIST_LOCK 时靠 RLock 防同线程自死锁；若误用 Lock，退出路径 :5577 挂死（launchd 需 kill -9）。
- **并发测试构造**：损坏窗口是竞态，测试用"结果必过性断言 + 轮数放大"，不得写"修复前必现损坏"的失败测试（写不出稳定的 red）。
- **测试 :1633 依赖删除**：persist_upstream 删除同步重写该用例，漏改则测试红。
- **tmp 残留边界**：kill -9 于写段中会留 `.tmp.<pid>` 残留（不损坏主文件），属可接受；不在本 episode 加启动清理逻辑。

## Exclusions

- 不修 C（TPM 跨桶 FIFO）、D（热路径重复 parse）、E（管理面鉴权）、F（_calibrate_probe 死分支）——见 split plan 各自 episode。
- 不删 `sse_line_extract_usage`（test-only seam，split plan 已论证保留）。
- 不改磁盘 schema、不加 env seam、不加依赖；不动 FLUSH_INTERVAL_S 与 flush 触发策略、不动 `_stats_dirty` 语义（flush_stats_if_dirty :2307-2320 原样）。
- 不修"信号在 STATS_LOCK/_CFG_LOCK 临界区内被中断"的既有理论窗口（非本次 finding；现路径同样存在且未被 tracer 定级）。

## R31 Evidence

[R31-S1] 问题存在证据（以下命令均在仓库根目录执行，回显为实际输出）：

证据 1：两条持久化写路径共用同一 tmp 中间文件名字面量（`persist_upstream` 窄写 与 `save_stats_counters` 全量写）：

```text
$ grep -n '\.tmp' ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:1535:    tmp = path + ".tmp"
ctyun-stream-fix-proxy.py:1762:    tmp = path + ".tmp"
```

证据 2：`save_stats_counters` 的写段在 STATS_LOCK（:1735-1747）与 _CFG_LOCK（:1748-1758）双双释放后锁外执行——`open(tmp, "w")` 在 :1763、`os.replace` 在 :1774：

```text
$ sed -n '1748,1774p' ctyun-stream-fix-proxy.py
    with _CFG_LOCK:
        base = UPSTREAM_BASE
        capture_enabled = CAPTURE_ERRORS
        probe_enabled = PROBE_ENABLED
        budgets = dict(TPM_MODEL_BUDGETS)
        _prune_body_err_samples(PERSISTED_BODY_ERR_SAMPLES)   # 内存态原地 prune（R7，同 _prune_daily）
        _prune_probe_results(PROBE_RESULTS)
        body_err_samples = {m: [dict(s) for s in items]
                            for m, items in PERSISTED_BODY_ERR_SAMPLES.items()}
        probe_results = {m: [dict(r) for r in items]
                         for m, items in PROBE_RESULTS.items()}
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"upstream_base": base,
                   "capture_errors": capture_enabled,
                   "probe_enabled": probe_enabled,
                   "model_pricing": MODEL_PRICING,
                   "tpm_model_budgets": budgets,
                   "tpm_body_err_samples": body_err_samples,
                   "tpm_probe_results": probe_results,
                   "stats": dict(counters, daily=daily, daily_by_model=daily_by_model,
                                 daily_by_key=daily_by_key, events=events)},
                  fh, ensure_ascii=False)
    os.replace(tmp, path)
```

证据 3（B）：`stats_snapshot` 在锁内把 5 键拷进本地变量（冻结副本），但全文件不存在任何 `snap["ttfb_hist"]` / `snap["rates"]` 写回语句——副本只喂 perf 计算，snap 以活引用进入序列化：

```text
$ grep -nE 'ttfb_hist = \{m:|phase_hist = \{k:|stalls_by_model = dict\(STATS|rates = dict\(STATS' ctyun-stream-fix-proxy.py
ctyun-stream-fix-proxy.py:2649:        ttfb_hist = {m: list(h) for m, h in STATS["ttfb_hist"].items()}
ctyun-stream-fix-proxy.py:2650:        phase_hist = {k: list(v) for k, v in STATS["phase_ms"].items()}
ctyun-stream-fix-proxy.py:2657:        stalls_by_model = dict(STATS["stalls_by_model"])
ctyun-stream-fix-proxy.py:2658:        rates = dict(STATS["rates"])
```

```text
$ grep -nE 'snap\["ttfb_hist"\]|snap\["rates"\]' ctyun-stream-fix-proxy.py
（无输出，exit 1：写回语句不存在）
```

[R31-S2] 根因（带 file:line 证据）：`save_stats_counters` 的写段（open :1763 / os.replace :1774）在 STATS_LOCK（:1735-1747）与 _CFG_LOCK（:1748-1758）均释放后锁外执行，且与 `persist_upstream`（:1535）共用同一 `path + ".tmp"` 中间文件名——flush 线程（:5602）、SIGTERM 处理器（:5579）、admin POST 触发的 set_*（:3487 → :2340）三路并发可交错写坏 JSON，`_load_persist_file`（:1777-1784）对 ValueError/OSError `return {}` 致计数清零；另 set_* 先 `persist_upstream` 窄写 3 键（:1537-1539）后 `save_stats_counters` 全量写（:2340），两阶段窗口内崩溃丢全部 stats。B 侧根因：`stats_snapshot` 锁内拷出 5 键（:2649-2658）但从不写回 snap，`snap = dict(STATS)`（:2631）浅拷贝携带活引用进入序列化，造成响应内混合快照+增量不一致。
