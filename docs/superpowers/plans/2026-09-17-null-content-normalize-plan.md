# PLAN: 请求体 null assistant content 归一为 ""（glm-5.3-oc 间歇 400 拒收修复）

## Header

- **目标**：在代理 `_proxy_relay` 读出 body 后插入 `normalize_null_assistant_content(body)`，把 `messages` 内 `role=assistant` 且显式 `content:null` 的消息归一为 `content:""`，其余请求字节级不变（identity 返回原 bytes 对象，`assertIs` 锁死），消除天翼网关对 glm-5.3-oc 间歇 400 拒收（错误包在 HTTP 200 SSE 帧内，失败率 ~60–75%）。
- **Spec**：`docs/superpowers/specs/2026-09-17-null-content-normalize-design.md`
- **规模**：小计划，2 卡。卡 1 = A/B 档 → executor（TDD：先测试跑红 → 实现跑绿）；卡 2 = C 档 → implementer（部署 cp+cmp 三副本 + launchctl kickstart + 真机 ≥8 次重放+对照）。
- **路径映射**：spec 中 Files/acceptance 引用的绝对路径指向 main 工作树，本 PLAN 实现与验证均在 worktree `null-content-normalize/` 内进行。

## Global Constraints

- **TDD 铁律**：卡 1 先改测试跑红 → commit `test(proxy): ... 先红` → 再写实现跑绿 → commit `feat(proxy): ...`。生产代码不得先于失败测试出现。
- **测试与验证一律用 `/usr/bin/python3`**（系统 3.9.6）；**禁用 Homebrew python3**（http.server 构造有挂死坑）。
- **commit 只在 worktree 分支本地，禁止 push GitHub**（用户明令；该仓库虽有 origin）。
- **零新依赖**：生产代码仅用已有 `json`（ctyun-stream-fix-proxy.py:18 已导入）。
- **不动**：v1.4/v1.5 空流/finish 重试逻辑、SSE 滤毒、dashboard/admin、Content-Length（已在 STRIP_HEADERS :115 剥掉）、header 处理、模型分流。
- **插入点单一**：`_proxy_relay` :598 一行 `body = normalize_null_assistant_content(body)`，:611 首次转发与 :634 空流重试自动共用。
- **基线**：worktree 当前 67 个 `def test_`，目标 76（新增 9）。
- **executor STOP 守则**：执行中遇到本计划外决策或代码与计划不符 → STOP 上报，不自行裁决。
- **卡 2 重启纪律**：`lsof -nP -iTCP:7920 -sTCP:ESTABLISHED` 必须 0 条才 kickstart；重启后确认 LISTEN + `/api/stats` 的 `requests_total` 保留、`uptime_s` 归零。

---

## 卡 1（tier A）：测试先行 + 实现 — 9 新测试跑红 → 2 处生产改动跑绿

### 阶段 1A：测试先行（不改生产代码，跑红）

**文件**：`ctyun-stream-fix-proxy.test.py`（worktree 内）

#### 改动 1 — FakeUpstreamHandler 加 bodies 记录（Files 3+4）

**①** :57 `calls = None` 行后加一行：

```python
    bodies = None        # 共享 list：非 None 时按调用序 append 收到的原始请求体 bytes
```

**②** :59–62 `do_POST()` 裸读改捕获并记录：

```python
    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            raw = self.rfile.read(length)
            if self.bodies is not None:
                self.bodies.append(raw)
```

#### 改动 2 — make_fake_upstream 加 record_bodies 参数（File 5）

**③** :166 签名末尾（`scripted: bool = False` 后）加 `, record_bodies: bool = False`：

```python
def make_fake_upstream(poison: bool, tag: str = "/plain", big: bool = False,
                       fail_500: bool = False, empty_stream: bool = False,
                       blank_stream: bool = False, body_override=None,
                       fault_finish_first: bool = False,
                       fault_finish_stream: bool = False,
                       scripted: bool = False, record_bodies: bool = False) -> int:
```

**④** :172–176 `attrs` 字典加一项 `"bodies": [] if record_bodies else None`：

```python
    attrs = {"poison": poison, "tag": tag, "big": big, "fail_500": fail_500,
             "empty_stream": empty_stream, "blank_stream": blank_stream,
             "body_override": body_override,
             "fault_finish_first": fault_finish_first,
             "fault_finish_stream": fault_finish_stream,
             "bodies": [] if record_bodies else None}
```

注意：此函数不走 `scripted=True`（其首呼空流分支 :99–100 会污染端到端断言）。

#### 改动 3 — 新增 make_body_recording_upstream 辅助（File 6）

**⑤** :193（`make_scripted_upstream` :186–191 之后、`stop_fake_upstreams` :194 之前）插入：

```python
def make_body_recording_upstream(**kwargs) -> tuple:
    """记录上游收到的原始请求体：返回 (port, bodies)；正常路径响应 SSE_A+SSE_B+DONE。"""
    port = make_fake_upstream(False, record_bodies=True, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.bodies
```

#### 改动 4 — post_sse 签名改可选 payload（File 7）

**⑥** :289 签名改 `def post_sse(port: int, payload: bytes = None) -> bytes:`，:291 字面量行改为：

```python
def post_sse(port: int, payload: bytes = None) -> bytes:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    if payload is None:
        payload = b'{"model":"deepseek-v4-pro-0813-oc","stream":true,"messages":[]}'
    conn.request("POST", "/v1/chat/completions", body=payload,
                 headers={"Content-Type": "application/json"})
```

#### 改动 5 — ProxyDashboardUnitTest 新增 8 个单测（File 8）

**⑦** :458 `test_extract_model` 方法体结束后（:459 空行处）插入以下 8 个测试方法：

```python
    def test_normalize_null_assistant_content_basic(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = json.dumps({"model": "glm-5.3-oc", "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1"}]},
        ]}).encode("utf-8")
        data = json.loads(f(body).decode("utf-8"))
        self.assertEqual(data["messages"][1]["content"], "")
        self.assertEqual(data["messages"][1]["tool_calls"], [{"id": "c1"}])
        self.assertEqual(data["messages"][0]["content"], "hi")

    def test_normalize_identity_without_null(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = b'{"model":"glm-5.3-oc","messages":[{"role":"assistant","content":"x"}]}'
        self.assertIs(f(body), body)  # 同一对象：字节级透传

    def test_normalize_mixed_only_assistant_null(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = json.dumps({"messages": [
            {"role": "user", "content": None},        # user null 不动
            {"role": "assistant", "content": None},   # 唯一改动点
            {"role": "assistant", "content": "done"},  # 非 null 不动
            "not-a-dict", 42,                          # 非 dict 元素跳过
        ]}).encode("utf-8")
        msgs = json.loads(f(body).decode("utf-8"))["messages"]
        self.assertIsNone(msgs[0]["content"])
        self.assertEqual(msgs[1]["content"], "")
        self.assertEqual(msgs[2]["content"], "done")
        self.assertEqual(msgs[3], "not-a-dict")
        self.assertEqual(msgs[4], 42)

    def test_normalize_invalid_json_passthrough(self) -> None:
        f = self.mod.normalize_null_assistant_content
        for body in (b"not json", b"[1,2,3]", b'{"messages":"oops"}'):
            self.assertIs(f(body), body)

    def test_normalize_none_body_passthrough(self) -> None:
        self.assertIsNone(self.mod.normalize_null_assistant_content(None))

    def test_normalize_missing_messages_passthrough(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = b'{"model":"glm-5.3-oc","stream":true}'
        self.assertIs(f(body), body)

    def test_normalize_missing_content_key_untouched(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = b'{"messages":[{"role":"assistant","tool_calls":[{"id":"c1"}]}]}'
        self.assertIs(f(body), body)  # 键不存在 != content:null

    def test_normalize_non_assistant_null_untouched(self) -> None:
        f = self.mod.normalize_null_assistant_content
        body = json.dumps({"messages": [
            {"role": "tool", "content": None},
            {"role": "user", "content": None},
        ]}).encode("utf-8")
        self.assertIs(f(body), body)  # 无改动 -> 原 bytes 对象
```

#### 改动 6 — AdminIntegrationTest 新增端到端测试（File 9）

**⑧** :1919（`test_finish_tail_over_cap_fails_open` :1903–1919 之后、:1922 `if __name__` 之前）插入：

```python
    def test_upstream_receives_normalized_null_content(self) -> None:
        """端到端：assistant content:null 经代理后上游收到 content:"" 归一体；
        无 null 的请求上游收到字节级一致 body。"""
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        upstream_port, bodies = make_body_recording_upstream()
        self.proc = start_proxy(upstream_port, free_port())
        payload = json.dumps({
            "model": "glm-5.3-oc", "stream": True,
            "messages": [
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": None,
                 "tool_calls": [{"id": "c1", "type": "function",
                                 "function": {"name": "f", "arguments": "{}"}}]},
            ],
        }).encode("utf-8")
        data = post_sse(self.proc.proxy_port, payload)
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE)
        self.assertEqual(len(bodies), 1)
        sent = json.loads(bodies[0].decode("utf-8"))
        self.assertEqual(sent["messages"][1]["content"], "")
        self.assertEqual(sent["messages"][1]["tool_calls"][0]["id"], "c1")
        clean = json.dumps({"model": "glm-5.3-oc", "stream": True,
                            "messages": [{"role": "user", "content": "hi"}]}).encode("utf-8")
        post_sse(self.proc.proxy_port, clean)
        self.assertEqual(len(bodies), 2)
        self.assertEqual(bodies[1], clean)  # 无 null 请求字节级透传
```

#### 阶段 1A 验收（跑红）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期红**：新增 9 个测试全部失败（生产代码尚无 `normalize_null_assistant_content` → **AttributeError** 针对 8 个单测 + 端到端测试因函数不存在而 **ERROR**），exit code 非 0。**其余 67 个既有测试必须全绿。**

若失败集合不匹配（多/少/名字不同）→ STOP，上报主代理。

#### commit（阶段 1A 结束）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize && git add ctyun-stream-fix-proxy.test.py && git commit -m "test(proxy): null assistant content 归一 9 新测试 + bodies 记录 harness（先红）"
```

---

### 阶段 1B：实现（使测试变绿）

**文件**：`ctyun-stream-fix-proxy.py`（worktree 内）

#### 改动 7 — 新增模块级纯函数 normalize_null_assistant_content（File 1）

**:183**（`extract_model` :170–182 之后、`today_key` :184 之前空行处）写入完整函数体：

```python
def normalize_null_assistant_content(body):
    """把 messages 内 role=assistant 且显式 content:null 的消息归一为 content:""。

    其余一切输入原样返回：空 body、非 JSON（ValueError 系）、顶层非 dict、
    messages 非 list、无改动时返回**原 bytes 对象**（byte-identical 透传，
    保上游 prompt cache）；序列化异常 fail-open 返回原 body，永不抛出。
    """
    if not body:
        return body
    try:
        data = json.loads(body)
    except ValueError:
        return body
    if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
        return body
    changed = False
    for msg in data["messages"]:
        if (isinstance(msg, dict) and msg.get("role") == "assistant"
                and "content" in msg and msg["content"] is None):
            msg["content"] = ""
            changed = True
    if not changed:
        return body
    try:
        return json.dumps(data, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return body
```

#### 改动 8 — _proxy_relay 插入一行归一调用（File 2）

**:598**（`:597` `model = extract_model(body)` 与 `:599` `fwd_headers = {}` 之间的空行）替换为：

```python
        body = normalize_null_assistant_content(body)
```

**:611 首次 `_open_upstream` 与 :634 空流重试自动共用归一后 body，零额外改动。**

#### 阶段 1B 验收（跑绿）

```bash
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize && /usr/bin/python3 ctyun-stream-fix-proxy.test.py
```

**预期绿**：全部 76 测试通过（67 既有 + 9 新增），`Ran 76 tests` / `OK`，exit code 0。贴 stdout 末尾 `OK` 行 / `Ran 76 tests` 行及 exit code。

#### commit（阶段 1B 结束）

```bash
git add ctyun-stream-fix-proxy.py && git commit -m "feat(proxy): normalize_null_assistant_content 归一 assistant content:null -> \"\""
```

---

## 卡 2（tier C → implementer）：部署 + 真机验收

> **留白理由**：真机部署（`cp` 到 `~/.local/bin` + `cmp` 三副本 + `launchctl kickstart`）与真机重放 ≥8 次+对照，均依赖运行时的 macOS launchd 环境 + 天翼网关真实响应，无法在 PLAN 阶段写死为可本地执行的确定性代码。

### 部署步骤（卡 2 执行时逐条实施，贴所有原始 stdout/exit code）

**1. 三副本 cmp 一致性（部署前）**

```bash
cmp /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize/ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py; echo "exit=$?"
cmp /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize/ctyun-stream-fix-proxy.test.py ~/.local/bin/ctyun-stream-fix-proxy.test.py; echo "exit=$?"
```

注意：`~/.local/bin/ctyun-stream-fix-proxy.test.py` 当前是 09-13 旧版，部署时一并覆盖（预期 `cmp` 返回非 0，记录差异）。

**2. 部署 cp 两文件到 ~/.local/bin**

```bash
cp /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize/ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py; echo "exit=$?"
cp /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize/ctyun-stream-fix-proxy.test.py ~/.local/bin/ctyun-stream-fix-proxy.test.py; echo "exit=$?"
```

**3. 三副本 cmp 一致性（部署后）**

```bash
cmp /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize/ctyun-stream-fix-proxy.py ~/.local/bin/ctyun-stream-fix-proxy.py; echo "exit=$?"
cmp /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize/ctyun-stream-fix-proxy.test.py ~/.local/bin/ctyun-stream-fix-proxy.test.py; echo "exit=$?"
```

若 worktree 合并回 main 后还需 `cmp` main ↔ `~/.local/bin`，卡 2 执行时再判。

**4. ESTABLISHED 连接清空**

```bash
lsof -nP -iTCP:7920 -sTCP:ESTABLISHED; echo "exit=$?"
```

**预期**：无输出（0 条连接）。非 0 条 → 等待/排查后再继续。

**5. kickstart 重启**

```bash
launchctl kickstart -k gui/$(id -u)/com.ctyun-stream-fix-proxy; echo "exit=$?"
sleep 2
```

**6. 重启后确认**

```bash
lsof -nP -iTCP:7920 -sTCP:LISTEN; echo "exit=$?"
```

**预期**：一行 LISTEN 输出，exit 0。

```bash
curl -s http://127.0.0.1:7921/api/stats | python3 -m json.tool | head -30
```

**预期**：`requests_total` 保留（非 0 或与重启前一致）、`uptime_s` 归零（< 10）。

**7. 真机重放（≥8 次）**

从失败记录抠出请求体（spec R31-S1 定位命令）：

```bash
jq -r 'select(.error.message | contains("不符合支持的格式")) | .request.body' ~/.zcode/cli/rollout/model-io-sess_subagent_agent_8529320c-1706-4729-813d-004c3002a983.jsonl | head -1 > /tmp/failed-body.json
```

从 `~/.zcode/v2/config.json` 提取 `provider c8fbbb08...options.apiKey`，≥8 次 POST 7920：

```bash
API_KEY="<从 config.json 提取>"
for i in $(seq 1 10); do
  echo "=== attempt $i ==="
  curl -s -X POST http://127.0.0.1:7920/chat/completions \
    -H "Authorization: Bearer $API_KEY" \
    -H "Content-Type: application/json" \
    -d @/tmp/failed-body.json | python3 -c "
import sys,json
lines=sys.stdin.read().strip().split('\n')
errors=[l for l in lines if l.startswith('data:') and '\"error\"' in l]
if errors:
    print(f'ERROR lines: {len(errors)}')
    for e in errors[:3]: print(e[:200])
else:
    contents=[l for l in lines if l.startswith('data:{\"choices\"')]
    print(f'OK: {len(contents)} content lines')
"; done
```

**预期**：≥8/10 次无 `"error"` 帧且收到真实 content（判据：修复无效时 8/8 干净概率 ≤ 0.25^8 ≈ 1.5e-5）。

**8. 对照（1–2 次已知正常 body）**

已知正常请求体（无 null content）POST 7920，验证仍正常返回。

### 卡 2 验收

- 三副本 `cmp` exit 0（worktree ↔ `~/.local/bin` 两文件一致）。
- `lsof` ESTABLISHED 0 条 → kickstart 后 LISTEN 在。
- `/api/stats` `requests_total` 保留、`uptime_s` 归零。
- 真机 ≥8 次连续无 `"error"` 帧 + 收到真实 content。
- 对照 1–2 次正常 body 仍正常返回。
- 全部原始 stdout/exit code 贴出。

---

## 验证命令速查

| 阶段 | 命令 |
|------|------|
| 1A 红 | `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/null-content-normalize && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` |
| 1B 绿 | 同上 |
| 全量最终 | 同上（目标 `Ran 76 tests` / `OK` / exit 0） |