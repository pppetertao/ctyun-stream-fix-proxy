# PLAN — SSE 热路径单次解析 + _calibrate_probe 死分支清理（E4 = OCR finding D + F）

- **源 spec**：`docs/superpowers/specs/2026-10-09-sse-single-parse-design.md`（分支 commit 0c87e1a）
- **worktree / 分支**：`.worktrees/sse-single-parse` / `fix/sse-single-parse`
- **基线**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 384 测试全绿（E3 后基线）；定向集 `RelaySseV3Test + 4 矩阵方法 + UsageExtractTest` 26 tests OK、`BodyErrorTest` 5 tests OK（本 PLAN 写作时实测）
- **执行模式**：3 卡全 tier B（卡内代码已写死 → executor 转写），**串行执行** 卡1→卡2→卡3；每卡先跑红（卡2/卡3）或验证红已登记（卡1），再实现跑绿
- **解释器**：本机 python 不可用，一律 `/usr/bin/python3`；验证命令一律在 worktree 根目录执行
- **行号说明**：文中行号为 PLAN 写作时现场实测值；Edit 一律用文本锚点，行号仅作定位参考（卡 1 插入约 45 行会使后续行号漂移）

## Global Constraints

1. **行为逐字节等价（最高优先）**：kind 分类、usage 提取、body-error 判定、saw_usage 判定、wfile 交付字节、`_tpm_usage`/`_p3_usage_tokens`/`_body_err_line` 赋值语义与改动前完全一致。只减 `json.loads` 次数，不改任何判定逻辑与分支顺序。
2. **`parsed` 默认参数零破坏**：四个 `sse_line_*` helper 新增 `parsed=None` 可选参数；不传 `parsed`（= None）时函数走原完整路径（前缀检查 + json.loads + ValueError catch），既有测试与 `sse_line_extract_usage`（:384）调用风格零改动。
3. **sentinel 语义**：模块级 `_SSE_PARSE_FAILED = object()` 区分"未传参（None → helper 自行解析）"与"调用方预解析失败（sentinel → 直接走原 ValueError 等价出口）"。`json.loads` 永不产出 `object()` 实例，`is` 判定无碰撞风险。
4. **净成本评估不许变亏**：预解析每 data 行 1 次 `json.loads`（旧：常态 3 次、priming 期 4 次）+ O(N) bytes 前缀检查（rstrip + startswith + DONE_RE，零 decode/parse）；新增 `pre_parsed` list 与 pending 同阶（典型 1-5 行/record）。省掉的每次 loads 含 UTF-8 decode 与完整 JSON tokenization，远超新增开销。
5. **TDD 铁律**：卡 1 只写测试并实跑红（预期 2 failure，红值 10/8 已在本 PLAN 写作时用一次性脚本实测）；卡 2 实现后同批转绿；卡 3 先源级断言红再删码转绿。禁止跳过红直接绿。
6. **验证范围**：每卡只用定向命令（RelaySseV3Test / 矩阵 4 方法 / UsageExtractTest / BodyErrorTest），不跑全量；`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全量留 whole-branch 验证。
7. **Exclusions 遵守**：不删 `sse_line_extract_usage`（:384）；不改 `_relay_sse` 外部 json.loads 调用点（`extract_model`/:509、`_calibrate_send`/:1265、`_relay_buffered`/:3034）；不引入 env seam、不加新依赖；不改 docs/ 对 `_calibrate_probe` 防御性语义的既有描述。
8. **Edit 全用文本锚点**：本 PLAN 每个 Edit 给出唯一 old_string/new_string 对；执行者不得用行号 sed/正则批量替换。

---

## Task 1（tier B）— 新增解析计数测试（只写测试，实跑红）

**文件**：`ctyun-stream-fix-proxy.test.py`

**改动点（2 处 Edit）**：

### Edit 1.1 — `_drive` 加 `final` 可覆盖参数（既有 5 个调用方零改动）

old_string：
```python
    def _drive(self, lines):
        """统一驱动：STALL_THRESHOLD_S=0.0 下走完 [SSE_A, b"\\n", SSE_DONE, b"\\n"] 流。"""
        mod = self.mod
        orig_thresh = mod.STALL_THRESHOLD_S
        try:
            mod.STALL_THRESHOLD_S = 0.0
            handler = self._StubHandler()
            resp = self._FakeSseResp(lines)
            return (mod.ProxyHandler._relay_sse(handler, resp, final=True,
                                                model="m1"), handler)
        finally:
            mod.STALL_THRESHOLD_S = orig_thresh
```

new_string：
```python
    def _drive(self, lines, final=True):
        """统一驱动：STALL_THRESHOLD_S=0.0 下走完 [SSE_A, b"\\n", SSE_DONE, b"\\n"] 流。

        final 可覆盖：空流计数测试用 final=False 触发 _EmptyStream 路径。"""
        mod = self.mod
        orig_thresh = mod.STALL_THRESHOLD_S
        try:
            mod.STALL_THRESHOLD_S = 0.0
            handler = self._StubHandler()
            resp = self._FakeSseResp(lines)
            return (mod.ProxyHandler._relay_sse(handler, resp, final=final,
                                                model="m1"), handler)
        finally:
            mod.STALL_THRESHOLD_S = orig_thresh
```

### Edit 1.2 — RelaySseV3Test 末尾（`SettlePersistV3Test` 之前）插入 3 个测试

old_string：
```python
        # qwait 落 m2 环
        self.assertIn(8, list(mod.STATS["qwait_ms_by_model"].get("m2", [])))


class SettlePersistV3Test(unittest.TestCase):
```

new_string：
```python
        # qwait 落 m2 环
        self.assertIn(8, list(mod.STATS["qwait_ms_by_model"].get("m2", [])))

    def test_relay_sse_json_loads_once_per_data_line_normal(self) -> None:
        """D：常态流每条 data 行全程恰 1 次 json.loads（mock 计数）。

        修复前 kind/usage/body-error 三处消费各 1 次 + priming 期 has_usage
        +1 = ≥3 次/行；修复后主循环预解析 1 次、四消费者复用 parsed。
        本流含 content（首 record 触发 priming flush）+ usage + finish + done。"""
        mod = self.mod
        lines = [SSE_A, b"\n", SSE_USAGE, b"\n", SSE_FINISH, b"\n", SSE_DONE, b"\n"]
        data_line_count = sum(
            1 for l in lines if l.rstrip(b"\r\n").startswith(b"data:"))
        self.assertEqual(data_line_count, 4)
        with mock.patch.object(mod.json, "loads",
                               wraps=mod.json.loads) as spy:
            (filtered, truncated), handler = self._drive(lines)
        self.assertEqual(filtered, 0)
        self.assertFalse(truncated)
        self.assertEqual(spy.call_count, data_line_count,
                         "每条 data 行必须恰 1 次 json.loads（修复前本流 10 次）")
        # 行为等价哨兵：usage 捕获与五元组抽取语义不变
        self.assertEqual(handler._tpm_usage,
                         {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2})
        self.assertEqual(handler._p3_usage_tokens[:3], (1, 1, 2))

    def test_relay_sse_json_loads_priming_finish_hold(self) -> None:
        """D：priming 期 finish-hold 流每行恰 1 次 json.loads。

        finish record 触发 finish_hold，尾段 usage 帧经 has_usage 判 saw_usage——
        修复前 priming 期每行 kind+usage+body-error+has_usage 共 8 次；
        修复后恰 2 次（2 条 data 行各 1 次）。"""
        mod = self.mod
        lines = [SSE_FINISH, b"\n", SSE_USAGE, b"\n"]
        data_line_count = sum(
            1 for l in lines if l.rstrip(b"\r\n").startswith(b"data:"))
        self.assertEqual(data_line_count, 2)
        with mock.patch.object(mod.json, "loads",
                               wraps=mod.json.loads) as spy:
            (filtered, truncated), handler = self._drive(lines)
        self.assertEqual(filtered, 0)
        self.assertFalse(truncated)
        self.assertEqual(spy.call_count, data_line_count,
                         "priming/finish-hold 期同样恰 1 次/行（修复前本流 8 次）")
        self.assertEqual(handler._tpm_usage,
                         {"prompt_tokens": 1, "completion_tokens": 1,
                          "total_tokens": 2})

    def test_relay_sse_json_loads_empty_stream_zero(self) -> None:
        """D：空流（零 record，EOF 直入 _EmptyStream）解析次数为 0。

        纯 EOF 迭代 pending 为空，预解析循环零迭代——守卫预解析不得引入
        空流额外 loads。"""
        mod = self.mod
        with mock.patch.object(mod.json, "loads",
                               wraps=mod.json.loads) as spy:
            with self.assertRaises(mod._EmptyStream):
                self._drive([], final=False)
        self.assertEqual(spy.call_count, 0,
                         "空流路径不得产生任何 json.loads 调用")


class SettlePersistV3Test(unittest.TestCase):
```

**依赖事实**（已实测）：`from unittest import mock` 已在测试文件 :32；`SSE_A`/:38、`SSE_DONE`/:41、`SSE_FINISH`/:44、`SSE_USAGE`/:45 为模块级常量；`mod._EmptyStream` 为源模块 :1424 顶层类。

**验证（红，预期输出）**：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py RelaySseV3Test
...
FAIL: test_relay_sse_json_loads_once_per_data_line_normal (__main__.RelaySseV3Test)
AssertionError: 每条 data 行必须恰 1 次 json.loads（修复前本流 10 次）
10 != 4
FAIL: test_relay_sse_json_loads_priming_finish_hold (__main__.RelaySseV3Test)
AssertionError: priming/finish-hold 期同样恰 1 次/行（修复前本流 8 次）
8 != 2
Ran 8 tests in ...s
FAILED (failures=2)
```
（`test_relay_sse_json_loads_empty_stream_zero` 修复前后均 0 次 = 绿，属回归守卫；其余 5 个既有测试保持绿。**若红值不是 10/8 或失败数不是 2 → STOP 报告主代理，不得继续卡 2。**）

Exit code: 1（unittest 有 failure 时的正常退出码，此为预期红）。

---

## Task 2（tier B）— 实现 D：sentinel + 四 helper `parsed` 参数 + `_relay_sse` 预解析

**文件**：`ctyun-stream-fix-proxy.py`

**改动点（9 处 Edit，全部文本锚点）**：

### Edit 2.1 — 模块级 sentinel（`sse_data_line_kind` 之后、`body_has_error` 之前，现 :286-289）

old_string：
```python
    return "noise"


def body_has_error(parsed) -> bool:
```

new_string：
```python
    return "noise"


_SSE_PARSE_FAILED = object()  # sentinel：_relay_sse 预解析失败的行（区别于未传 parsed=None）


def body_has_error(parsed) -> bool:
```

### Edit 2.2 — `sse_data_line_kind` 签名（现 :252）

old_string：
```python
def sse_data_line_kind(line: bytes) -> str:
```

new_string：
```python
def sse_data_line_kind(line: bytes, parsed=None) -> str:
```

### Edit 2.3 — `sse_data_line_kind` docstring 补 parsed 语义（现 :259 尾行）

old_string：
```python
    其余（reasoning-only、空 delta、choices 为空的 usage 帧、data:null、非 dict JSON）→ noise。"""
```

new_string：
```python
    其余（reasoning-only、空 delta、choices 为空的 usage 帧、data:null、非 dict JSON）→ noise。
    parsed 非 None 时复用调用方预解析结果跳过 json.loads（sentinel _SSE_PARSE_FAILED
    等价解析失败 → content）；None（默认）保持原自行解析路径。"""
```

### Edit 2.4 — `sse_data_line_kind` 解析块（现 :265-269）

old_string：
```python
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return "content"
    if not isinstance(data, dict):
```

new_string：
```python
    if parsed is _SSE_PARSE_FAILED:
        return "content"
    if parsed is None:
        try:
            data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
        except ValueError:
            return "content"
    else:
        data = parsed
    if not isinstance(data, dict):
```

### Edit 2.5 — `sse_line_has_usage` 签名 + docstring + 解析块（现 :300-313）

old_string：
```python
def sse_line_has_usage(line: bytes) -> bool:
    """判 SSE data 行是否携带非空 usage 帧（独立 usage 帧 choices=[] 与 ride-on finish
    帧都覆盖）；DONE/非 JSON/非 data 行 → False。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return False
    if not isinstance(data, dict):
        return False
    usage = data.get("usage")
    return isinstance(usage, dict) and bool(usage)
```

new_string：
```python
def sse_line_has_usage(line: bytes, parsed=None) -> bool:
    """判 SSE data 行是否携带非空 usage 帧（独立 usage 帧 choices=[] 与 ride-on finish
    帧都覆盖）；DONE/非 JSON/非 data 行 → False。
    parsed 非 None 时复用预解析结果跳过 json.loads；sentinel/None+解析失败 → False。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    if parsed is _SSE_PARSE_FAILED:
        return False
    if parsed is None:
        try:
            data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
        except ValueError:
            return False
    else:
        data = parsed
    if not isinstance(data, dict):
        return False
    usage = data.get("usage")
    return isinstance(usage, dict) and bool(usage)
```

### Edit 2.6 — `sse_line_usage` 签名 + docstring + 解析块（现 :316-333）

old_string：
```python
def sse_line_usage(line: bytes):
    """返回 SSE data 行中的 usage dict，无 usage 帧返回 None。

    解析逻辑与 sse_line_has_usage 同序：非 data: 前缀/[DONE]/非 JSON/非 dict →
    None；usage 非空 dict → 返回该 dict；其余 → None。
    独立 usage 帧（choices=[]）与 ride-on finish 帧均覆盖。
    """
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return None
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) and usage else None
```

new_string：
```python
def sse_line_usage(line: bytes, parsed=None):
    """返回 SSE data 行中的 usage dict，无 usage 帧返回 None。

    解析逻辑与 sse_line_has_usage 同序：非 data: 前缀/[DONE]/非 JSON/非 dict →
    None；usage 非空 dict → 返回该 dict；其余 → None。
    独立 usage 帧（choices=[]）与 ride-on finish 帧均覆盖。
    parsed 非 None 时复用预解析结果跳过 json.loads；sentinel → None（同解析失败）。
    """
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return None
    if parsed is _SSE_PARSE_FAILED:
        return None
    if parsed is None:
        try:
            data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
        except ValueError:
            return None
    else:
        data = parsed
    if not isinstance(data, dict):
        return None
    usage = data.get("usage")
    return usage if isinstance(usage, dict) and usage else None
```

### Edit 2.7 — `sse_line_body_error` 签名 + docstring + 解析块（现 :398-409）

old_string：
```python
def sse_line_body_error(line: bytes) -> bool:
    """判 SSE data 行是否为 body error 帧。rstrip → 非 data: 前缀或 [DONE] →
    False；json.loads ValueError → False（fail-open 同 sse_line_has_usage）；
    否则 return body_has_error(data)。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    try:
        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
    except ValueError:
        return False
    return body_has_error(data)
```

new_string：
```python
def sse_line_body_error(line: bytes, parsed=None) -> bool:
    """判 SSE data 行是否为 body error 帧。rstrip → 非 data: 前缀或 [DONE] →
    False；json.loads ValueError → False（fail-open 同 sse_line_has_usage）；
    否则 return body_has_error(data)。
    parsed 非 None 时复用预解析结果跳过 json.loads；sentinel 经 body_has_error
    非 dict 判定 → False（与原 ValueError→False 一致）。"""
    stripped = line.rstrip(b"\r\n")
    if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
        return False
    if parsed is None:
        try:
            data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
        except ValueError:
            return False
    else:
        data = parsed  # dict 或 sentinel；body_has_error 对非 dict 恒 False
    return body_has_error(data)
```

### Edit 2.8 — `_relay_sse` record 边界：预解析 + kinds 复用（现 :3160-3162）

old_string：
```python
                if line in (b"\n", b"\r\n", b""):
                    # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                    kinds = [sse_data_line_kind(buf_line) for buf_line in pending]
```

new_string：
```python
                if line in (b"\n", b"\r\n", b""):
                    # b"\n"/b"\r\n" = record 终结；b"" = EOF（残留 record 同规则收尾）
                    # 预解析（D 单次解析）：每 data 行全程恰 1 次 json.loads，四消费者
                    # 复用 pre_parsed；非 data/[DONE] 行存 None（helper 前缀分支自决，
                    # 零 loads）；parse 失败存 _SSE_PARSE_FAILED（等价原 ValueError 路径）
                    pre_parsed = []
                    for buf_line in pending:
                        stripped = buf_line.rstrip(b"\r\n")
                        if not stripped.startswith(b"data:") or DONE_RE.match(stripped):
                            pre_parsed.append(None)
                            continue
                        try:
                            pre_parsed.append(json.loads(
                                stripped[5:].strip().decode("utf-8", "replace")))
                        except ValueError:
                            pre_parsed.append(_SSE_PARSE_FAILED)
                    kinds = [sse_data_line_kind(buf_line, parsed=pre_parsed[i])
                             for i, buf_line in enumerate(pending)]
```

### Edit 2.9 — `_relay_sse` 三处消费者复用 `pre_parsed`（现 :3164-3172、:3184-3185）

old_string（usage 捕获，:3164-3167）：
```python
                    # TPM usage 捕获：本 record 内所有行取最后非空 usage 帧；
                    # v2 P3 同循环顺手消费已解析 dict——零额外 json.loads（R1）
                    for buf_line in pending:
                        usage_hit = sse_line_usage(buf_line)
```

new_string：
```python
                    # TPM usage 捕获：本 record 内所有行取最后非空 usage 帧；
                    # 复用预解析 dict（D）——全程每 data 行恰 1 次 json.loads（R1）
                    for i, buf_line in enumerate(pending):
                        usage_hit = sse_line_usage(buf_line, parsed=pre_parsed[i])
```

old_string（body-error 扫描，:3171-3172）：
```python
                    if self._body_err_line is None:
                        hit = next((l for l in pending if sse_line_body_error(l)), None)
```

new_string：
```python
                    if self._body_err_line is None:
                        hit = next((l for i, l in enumerate(pending)
                                    if sse_line_body_error(l, parsed=pre_parsed[i])),
                                   None)
```

old_string（priming saw_usage，:3184-3185）：
```python
                        if not saw_usage:
                            saw_usage = any(sse_line_has_usage(l) for l in pending)
```

new_string：
```python
                        if not saw_usage:
                            saw_usage = any(sse_line_has_usage(l, parsed=pre_parsed[i])
                                            for i, l in enumerate(pending))
```

**等价性论证（executor 无需重推导，供 review）**：预解析表达式与原 helper 内解析逐字符一致（`stripped[5:].strip().decode("utf-8", "replace")`）；非 data/[DONE] 行 helper 前缀分支先于 parsed 使用返回，pre_parsed 存 None 不影响；sentinel 出口与原 ValueError 出口逐一对齐（kind→"content"、has_usage→False、usage→None、body_error→False，后者经 `body_has_error` 非 dict 恒 False）；`pre_parsed` 与 `pending` 同长同序，`enumerate` 索引安全；`pending` 空（纯 EOF 迭代）→ `pre_parsed` 空表，四处遍历零迭代。

**验证（绿，预期输出）**：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py RelaySseV3Test \
  ProxyDashboardUnitTest.test_sse_data_line_kind_matrix \
  ProxyDashboardUnitTest.test_sse_line_has_usage_matrix \
  ProxyDashboardUnitTest.test_sse_line_body_error_matrix \
  ProxyDashboardUnitTest.test_sse_line_usage_matrix UsageExtractTest BodyErrorTest
...
Ran 34 tests in ...s
OK
```
Exit code: 0。**卡 1 的 2 个红必须全部转绿且 0 failure；任何既有用例翻红 → STOP 报告主代理。**

---

## Task 3（tier B）— 实现 F：`_calibrate_probe` 死守卫删除（红→绿）+ 定向回归

**文件**：`ctyun-stream-fix-proxy.test.py`、`ctyun-stream-fix-proxy.py`

**改动点（2 处 Edit）**：

### Edit 3.1 — RelaySseV3Test 末尾（卡 1 插入的空流测试之后、`SettlePersistV3Test` 之前）加源级断言测试

old_string：
```python
        self.assertEqual(spy.call_count, 0,
                         "空流路径不得产生任何 json.loads 调用")


class SettlePersistV3Test(unittest.TestCase):
```

new_string：
```python
        self.assertEqual(spy.call_count, 0,
                         "空流路径不得产生任何 json.loads 调用")

    def test_calibrate_probe_dead_guard_removed(self) -> None:
        """F：_proxy_relay 的 _calibrate_probe 恒假守卫已删除。

        该属性全仓零赋值（grep 仅源 :2799 一处 getattr 引用）→ getattr 恒 False
        → return 不可达；真校准走 _calibrate_send 直连上游。源级断言防回归：
        守卫不得再现于 _proxy_relay。"""
        import inspect
        src = inspect.getsource(self.mod.ProxyHandler._proxy_relay)
        self.assertNotIn("_calibrate_probe", src,
                         "_calibrate_probe 死守卫必须保持删除（不可达代码）")


class SettlePersistV3Test(unittest.TestCase):
```

### Edit 3.2 — 删除 `_proxy_relay` 首部死守卫（现 :2798-2801）

old_string：
```python
    def _proxy_relay(self, started: float) -> None:
        if getattr(self, "_calibrate_probe", False):
            return
        length = int(self.headers.get("Content-Length") or 0)
```

new_string：
```python
    def _proxy_relay(self, started: float) -> None:
        length = int(self.headers.get("Content-Length") or 0)
```

**验证步骤（先红后绿，两条命令）**：

步骤 1（仅 Edit 3.1 落盘后，红）：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py RelaySseV3Test.test_calibrate_probe_dead_guard_removed
...
FAIL: test_calibrate_probe_dead_guard_removed (__main__.RelaySseV3Test)
AssertionError: _calibrate_probe 死守卫必须保持删除（不可达代码）
'_calibrate_probe' unexpectedly found in ...
Ran 1 test in ...s
FAILED (failures=1)
```
Exit code: 1（预期红）。**若不红（guard 已不在）→ STOP 报告主代理。**

步骤 2（Edit 3.2 落盘后，绿）：
```
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py RelaySseV3Test \
  ProxyDashboardUnitTest.test_sse_data_line_kind_matrix \
  ProxyDashboardUnitTest.test_sse_line_has_usage_matrix \
  ProxyDashboardUnitTest.test_sse_line_body_error_matrix \
  ProxyDashboardUnitTest.test_sse_line_usage_matrix UsageExtractTest BodyErrorTest
...
Ran 35 tests in ...s
OK
```
Exit code: 0（35 = 卡 2 的 34 + 本卡新测试 1；BodyErrorTest 5 用例端到端覆盖 `_proxy_relay` 正常执行路径，证实删 guard 无 AttributeError/无逻辑跳过）。

---

## Self-Review（PLAN-B 自查 5 项，写作时已完成）

1. **spec coverage**：spec Files 1-4（四 helper `parsed` 参数）→ Edit 2.2/2.5/2.6/2.7；spec 5（sentinel）→ Edit 2.1；spec 6（_relay_sse 预解析 + 四处消费者）→ Edit 2.8/2.9；spec 7（死守卫）→ Edit 3.2；spec 8（解析计数测试三流）→ Edit 1.1/1.2；spec 9（既有矩阵/UsageExtract 零改）→ Global Constraint 2 + 卡 2/卡 3 定向回归命令。Acceptance 1→卡 3 绿 + whole-branch 留全量；AC2→卡 1 三测试；AC3→Edit 3.1 断言 + BodyErrorTest；AC4→卡 1 等价哨兵断言 + 卡 2 回归 34 绿。全覆盖，无遗漏。
2. **placeholder scan**：三卡全部 Edit 均为完整 old/new 代码对，无 "略"/"同上"/伪码；0 命中。
3. **type consistency**：`pre_parsed: list[dict | None | object]`，与四 helper `parsed=None` 默认参数、`_SSE_PARSE_FAILED = object()` 的 `is` 判定一致；`mod._EmptyStream`（源 :1424 顶层类）、`mod.json`、`from unittest import mock`（测试 :32）、`inspect.getsource` 对 importlib 载入模块可用——均已现场实测（一次性脚本实跑通过，含 mock.patch.object 拦截计数）。
4. **可落盘性**：所有 old_string 均为当前 worktree 文件逐字摘录（本 session Read/grep 实测）；唯一性已逐条核对（各 helper 签名全文唯一；各解析块以独特尾行锚定；`_drive`/测试插入点/`_proxy_relay` guard 锚点唯一）；验证命令三条均已在本机实跑过等价形式（26 tests OK / BodyErrorTest 5 OK / 红值 10 与 8 实测）。
5. **锚点实测 spot-check**（≥3）：① 四 helper 签名与解析块 ：252/:300/:316/:398 逐字核对；② `_relay_sse` 四消费者 :3162/:3166-3170/:3171-3174/:3184-3185 逐字核对（spec 写 :3185，实际 `saw_usage = any(...)` 在 :3185、`if not saw_usage:` 在 :3184，Edit 锚点取两行整体，无偏差）；③ `_proxy_relay` guard :2798-2800 逐字核对（两行 guard + def 行）；④ `RelaySseV3Test` :6026/`SettlePersistV3Test` :6173/`_EmptyStream` :1424/矩阵 :843/:884/:931/:958 全命中。

**spec 偏差记录（2 处，均为实现细节级，不动 spec 语义）**：
- 卡 1 对 `_drive` 增加 `final=True` 默认参数：spec 只说"新增测试方法"，未提及 driver 扩展；默认值保证既有 5 个调用方零改动，空流测试需 `final=False` 触发 `_EmptyStream` 路径（spec 要求该流覆盖）。
- `sse_line_body_error` 对 sentinel 不再尝试解析、直接经 `body_has_error(sentinel)`→False：spec item 4 明示 "`parsed=None` 或 `_SSE_PARSE_FAILED` 均返回 False（与原 ValueError→False 一致）"，此实现取其等价出口且避免 sentinel 行的冗余二次 loads（性能正确读法）；对外行为与 spec 完全一致。
