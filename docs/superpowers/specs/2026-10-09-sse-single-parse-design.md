# SSE 热路径单次解析 + _calibrate_probe 死分支清理

## Goal

`_relay_sse` 记录循环内每 data 行只 `json.loads` 一次（kind/usage/body-error/has-usage 四处消费者复用同一解析结果），常态流从每行 3 次降至 1 次；同时删除 `_proxy_relay` 中的 `_calibrate_probe` 恒假守卫（2 行不可达代码）。

## Files to Change

### 1. `ctyun-stream-fix-proxy.py:252` `sse_data_line_kind()` — 加 `parsed` 复用参数

```python
def sse_data_line_kind(line: bytes, parsed=None) -> str:
```

- 新增可选参数 `parsed`：当调用者传入已解析 dict 时，跳过 :265-268 的 `json.loads`+`ValueError` 分支，直接复用传入的 `parsed` 值进入 :269 起的 dict 判定逻辑
- 当 `parsed` 为模块级 sentinel `_SSE_PARSE_FAILED` 时，等价于原 ValueError 路径——返回 `"content"`
- 当 `parsed` 为 `None`（默认值）时，行为与原函数完全一致（走完整前缀检查 + json.loads + ValueError catch 路径）
- 边界条件：`parsed` 为 dict 但非预期结构（无 choices / choices 非 list / data:null）→ 后续逻辑照常处理，与原行为一致

### 2. `ctyun-stream-fix-proxy.py:300` `sse_line_has_usage()` — 加 `parsed` 复用参数

```python
def sse_line_has_usage(line: bytes, parsed=None) -> bool:
```

- 同上模式：`parsed` 非 None 时跳过 :306-309 的 json.loads+ValueError，直接进入 :310 起的 dict 判定
- `parsed is _SSE_PARSE_FAILED`或 `parsed=None` 时行为等同于原函数（前缀检查 + 解析），parse 失败 → False
- 边界条件：`usage` 键为 `{}`（空 dict）→ `bool({})` = False，返回 False（与原行为一致）

### 3. `ctyun-stream-fix-proxy.py:316` `sse_line_usage()` — 加 `parsed` 复用参数

```python
def sse_line_usage(line: bytes, parsed=None):
```

- 同上模式：`parsed` 非 None 时跳过 :326-329 的 json.loads+ValueError，直接进入 :330 起的 dict 判定
- 返回 None（无 usage）或 usage dict（与原行为一致）
- 边界条件：`parsed` 为 `_SSE_PARSE_FAILED` → None（同 ValueError 路径）

### 4. `ctyun-stream-fix-proxy.py:398` `sse_line_body_error()` — 加 `parsed` 复用参数

```python
def sse_line_body_error(line: bytes, parsed=None) -> bool:
```

- 同上模式：`parsed` 非 None 时跳过 :405-408 的 json.loads+ValueError，直接调用 `body_has_error(parsed)`
- `body_has_error`(:289) 对非 dict 返回 False，因此 `parsed=None` 或 `_SSE_PARSE_FAILED` 均返回 False（与原 ValueError→False 一致）
- 边界条件：`parsed` 为非 dict → False（fail-open，不因非预期输入破坏流处理）

### 5. `ctyun-stream-fix-proxy.py:284` — 新增模块级 sentinel

在 `sse_data_line_kind` 函数体后（:286 空行处或 `body_has_error` (:289) 之前）新增：

```python
_SSE_PARSE_FAILED = object()  # sentinel for pre-parse failure in _relay_sse
```

- 用途：区分"调用者传入表示 parse 失败的 None"和"未传 parsed 参数（默认 None）"
- 仅在 :252/:300/:316/:398 四个 sse_line_* 函数中引用；`sse_data_line_kind` 的 "content" 语义需要它，其余三个函数 semantic 无差异但统一使用 sentinel 对齐代码模式

### 6. `ctyun-stream-fix-proxy.py:3123` `ProxyHandler._relay_sse()` — 主循环预解析

- 在 :3160-3161 记录边界判断之后、:3162 `kinds = [...]` 之前，新增预解析循环：对 `pending` 中每行做一次 prefix 判定 + json.loads，结果存入 `pre_parsed` list（与 pending 等长、同序），非 data 行 / parse 失败行存 sentinel/None
- :3162 `kinds = [sse_data_line_kind(buf_line) ...]` → `kinds = [sse_data_line_kind(buf_line, parsed=pre_parsed[i]) ...]`
- :3166-3170 usage 遍历 `sse_line_usage(buf_line)` → `sse_line_usage(buf_line, parsed=pre_parsed[i])`
- :3171-3174 body-error 扫描 `sse_line_body_error(l)` → `sse_line_body_error(l, parsed=pre_parsed[i])`
- :3185 priming `sse_line_has_usage(l)` → `sse_line_has_usage(l, parsed=pre_parsed[i])`
- 行为等价性：kind 分类、usage 提取、body-error 判定、saw_usage 判定、TPM settle 捕获的 `_tpm_usage`/`_p3_usage_tokens` 输出与改动前逐字节一致。只减解析次数，不改判定逻辑
- 边界条件：`pending` 为空（纯 EOF 迭代）→ `pre_parsed` 为空 list，后续 zip 遍历无迭代

### 7. `ctyun-stream-fix-proxy.py:2798` `ProxyHandler._proxy_relay()` — 删死分支

删除第 2799-2800 行（当前 guard 占两行）：

```python
        if getattr(self, "_calibrate_probe", False):
            return
```

- `_calibrate_probe` 属性全仓零赋值（grep 确认仅此一处引用），`getattr` 始终返回 `False`，`return` 不可达
- 真校准路径走 `_calibrate_send`(:1214) 直连上游，不经过 `_proxy_relay`
- 如果原始 spec 文档引用过该 guard 表述，维持现状不移除（文档注记已标记防御性语义）；当前 split plan 与相关 spec 均不依赖它

### 8. `ctyun-stream-fix-proxy.test.py:6026` `RelaySseV3Test` — 扩展解析计数测试

新增测试方法，使用 `unittest.mock.patch.object(mod.json, 'loads', wraps=mod.json.loads)` 统计 `_relay_sse` 全程 json.loads 调用次数，断言等于 data 行数（每条 data 行恰 1 次）。至少测三条流：
- 常态流（content + finish + done，non-priming，含 usage 帧）
- 带 usage 帧的 finish-hold 流（priming 期 saw_usage 分支覆盖）
- 空流 `_EmptyStream` 路径（pending 全空，parse count = 0）

### 9. `ctyun-stream-fix-proxy.test.py:843,884,931,958` — 现有矩阵测试不变

`sse_data_line_kind`/:843、`sse_line_has_usage`/:884、`sse_line_body_error`/:931、`sse_line_usage`/:958 四个矩阵测试均不传 `parsed` 参数（默认 None），验证函数在原调用风格下行为不变。`UsageExtractTest`(:5369) 亦不传 `parsed`，验证 `sse_line_extract_usage`→`sse_line_usage` 链路不变。

## Acceptance Criteria

1. `python ctyun-stream-fix-proxy.test.py` 全绿（含现有全部测试 + 新增解析计数测试）
2. 新增 `RelaySseV3Test` 测试：mock `json.loads` 计数断言——常态流每 data 行恰 1 次 loads（修复前 ≥3 次），priming 期无额外 loads（修复前 +1 次），空流 0 次
3. 删除 `_calibrate_probe` 守卫后，`_proxy_relay` 在未设 `_calibrate_probe` 属性时执行不变（无 AttributeError、无逻辑跳过）
4. 行为等价性：所有现有测试的 wfile 输出逐字节一致，`_tpm_usage`/`_p3_usage_tokens`/`_body_err_line` 赋值语义不变

## Risks

| 风险 | 等级 | 缓解 |
|------|------|------|
| 预解析 list 内存分配：每条 pending line 新增一个 list 元素（pointer + sentinel/None），对于典型 1-5 行/record 的 SSE 流可忽略；极端场景（pending 汇聚数百行后再收尾）开销与 pending 本身同阶 | 低 | pending 是用户可控的恶意 blob？不——pending 只缓冲同一个 SSE record 的行，record 分界由上游 `\n\n` 控制；上游可控但恶意上游本身即可 OOM。~5 行/record 的典型开销可忽略。 |
| sentinel `_SSE_PARSE_FAILED` 全局唯一但模块级常量，非热路径对象分配；`is` 比较即 Python identity check（等同于 C 指针比较），零 CPU 开销 | 极低 | 不需缓解 |
| `parsed` 默认参数导致现有直接调用者（测试 / sse_line_extract_usage）无意中触发旧代码路径——这是期望行为，因为不传 `parsed` = 原路径，语义不变 | 无 | 不需要缓解 |
| F 的 `_calibrate_probe` guard 删除后，若未来有人通过外部 monkey-patch 或 pickle 恢复机制注入该属性（"防御性"场景），请求将不再被跳过 | 极低 | 该 guard 从未被触发；真校准路径走 `_calibrate_send`；即使未来有人误注入，也应在 review 时发现。若需恢复，重新加一行即可 |

**D 的成本对比**：每次省 1-3 次 `json.loads`（常态流从 3→1），代价是每次 record 收尾时额外分配一个与 `pending` 等长的 list + O(N) 的前缀检查（rstrip + startswith，全为 bytes 操作，零 decode/parse）。省掉的 json.loads 含 UTF-8 decode 与完整 JSON tokenization，远超新增开销。非 data 行（event:/id:/comment）原本就走快速前缀检查（0 次 loads），不受影响。

## Exclusions

- 不删除 `sse_line_extract_usage`(:384)：它是 `UsageExtractTest`(:5369) 唯一消费的测试 seam（约 14 用例），生产零调用，保留不增删
- 不改 `_relay_sse` 外部的 json.loads 调用点（`extract_model`/:509、`_calibrate_send`/:1265、`_relay_buffered`/:3034 等），这些非热路径或语义独立
- 不引入 env seam、不引入新依赖
- 不修改 `docs/` 下的 spec/plan 文档对 `_calibrate_probe` 防御性语义的描述（维持记录，不移除）

## R31 Evidence

[R31-S1] 真实 grep 回显——锚点行号实测

```
$ grep -n 'def sse_data_line_kind\|def sse_line_has_usage\|def sse_line_usage\|def sse_line_body_error\|def _proxy_relay\|def _relay_sse' ctyun-stream-fix-proxy.py
252:def sse_data_line_kind(line: bytes) -> str:
300:def sse_line_has_usage(line: bytes) -> bool:
316:def sse_line_usage(line: bytes):
398:def sse_line_body_error(line: bytes) -> bool:
2798:    def _proxy_relay(self, started: float) -> None:
3123:    def _relay_sse(self, resp: http.client.HTTPResponse, final: bool,

$ grep -n 'json\.loads\|_calibrate_probe\|_body_err_line = None' ctyun-stream-fix-proxy.py
266:        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
2799:        if getattr(self, "_calibrate_probe", False):
2905:                self._body_err_line = None
2946:                self._body_err_line = None
307:        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
3171:                    if self._body_err_line is None:
3174:                            self._body_err_line = hit
327:        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
406:        data = json.loads(stripped[5:].strip().decode("utf-8", "replace"))
```

[R31-S2] 根因定位

- **D（:3162 / :3167 / :3172 / :3185）**：`_relay_sse` 主循环 record 边界处对同一 `pending` 行的四处消费（`sse_data_line_kind` / `sse_line_usage` / `sse_line_body_error` / `sse_line_has_usage`）各自独立调用 `json.loads`，常态流每 data 行 3 次（kind + usage + body-error scan），priming 期 +1（has_usage）。代码注释 :3165 与 :390 宣称"零额外 json.loads (R1)"，与实际不符。
- **F（:2799）**：`_proxy_relay` 首部 `getattr(self, "_calibrate_probe", False)` 全仓零赋值，恒为 `False`，`return` 语句不可达——真校准路径走 `_calibrate_send`(:1214) 直连上游。

```
$ grep -rn '_calibrate_probe' ctyun-stream-fix-proxy.py
2799:        if getattr(self, "_calibrate_probe", False):
```

全仓仅此一处引用，无赋值点，证实为死分支。