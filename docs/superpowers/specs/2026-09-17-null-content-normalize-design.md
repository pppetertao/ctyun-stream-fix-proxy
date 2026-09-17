# assistant content:null 归一为 ""（glm-5.3-oc 间歇 400 拒收修复）

## Goal

上游天翼网关对 `glm-5.3-oc` 间歇性拒收含 `role=assistant 且 content:null` 消息的请求（HTTP 200 SSE 帧内包 400），导致 ZCode 工具调用轮连环失败；代理在转发前把该形态归一为 `content:""`，使其余请求字节级不变。

## 现状与方案

根因为既定事实（本会话取证，不重开论证）：错误包在 200 的 SSE 帧内（`messages[N] 不符合支持的格式`，N 恒为 null 消息下标）；失败体直打 7920 重放失败率 ~60–75%；交错对照实验仅做 `null→""` 归一后 46/46 全过；影响面限 glm-5.3-oc（v4-pro 同形态 2334 次零命中），但归一对全模型语义等价，不按模型分流。

**方案：新增模块级纯函数 `normalize_null_assistant_content(body)`，在 `_proxy_relay` 读出 body 后一行插入。** 关键决策：

- **byte-identical 透传**：无改动时返回**原 bytes 对象本身**（不是重新序列化的等价体），保住上游 prompt cache 行为；单测以 `assertIs` 锁死。
- **fail-open**：解析/序列化任何 `ValueError`（含 `UnicodeDecodeError`/`JSONDecodeError`，均为其子类）/`TypeError` 一律原样返回，代理永不因归一失败而断流。`json.loads` 直吃 bytes（Python ≥3.6；运行时 `/usr/bin/python3`，plist `:9` 同款）。
- **Content-Length 无需处理**：`content-length` 已在 `STRIP_HEADERS`（`ctyun-stream-fix-proxy.py:115`）被剥掉，`_open_upstream` :669 `conn.request(...)` 由 http.client 按新 body 重算。
- **插入点单一**：赋值回同一 `body` 变量后，首次转发（:611）与空流重试（:634）自动共用归一后 body，零调用点改动。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:183`（`extract_model` :170-181 之后、`today_key` :184 之前）— 新增模块级纯函数，完整函数体：

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

2. `ctyun-stream-fix-proxy.py:598` `_proxy_relay()`（:594 起）— 在 :597 `model = extract_model(body)` 之后、:599 `fwd_headers = {}` 之前插一行：

```python
        body = normalize_null_assistant_content(body)
```

   :611 首次 `_open_upstream` 与 :634 空流重试自动共用归一后 body。

3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:57` `FakeUpstreamHandler` 类属性区（`calls = None` 行后）— 加 `bodies = None         # 共享 list：非 None 时按调用序 append 收到的原始请求体 bytes`。

4. `ctyun-stream-fix-proxy.test.py:59-62` `FakeUpstreamHandler.do_POST()` — 裸读改捕获并记录（既有 20+ 用例不受影响，`bodies` 默认 None 不记录）：

```python
        length = int(self.headers.get("Content-Length") or 0)
        if length > 0:
            raw = self.rfile.read(length)
            if self.bodies is not None:
                self.bodies.append(raw)
```

5. `ctyun-stream-fix-proxy.test.py:166` `make_fake_upstream()` — 签名末尾加 `record_bodies: bool = False`；:172-176 `attrs` 字典加一项 `"bodies": [] if record_bodies else None`。注意**不走 `scripted=True`**（其首呼空流分支 :99-100 会污染端到端断言）。

6. `ctyun-stream-fix-proxy.test.py:193`（`make_scripted_upstream` :186-191 之后、`stop_fake_upstreams` :194 之前）— 新增辅助：

```python
def make_body_recording_upstream(**kwargs) -> tuple:
    """记录上游收到的原始请求体：返回 (port, bodies)；正常路径响应 SSE_A+SSE_B+DONE。"""
    port = make_fake_upstream(False, record_bodies=True, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.bodies
```

7. `ctyun-stream-fix-proxy.test.py:289` `post_sse()` — 签名改 `def post_sse(port: int, payload: bytes = None) -> bytes:`，:291 字面量行改为 `if payload is None: payload = b'...'`（既有调用点零改动）。

8. `ctyun-stream-fix-proxy.test.py` `ProxyDashboardUnitTest`（:400，插在 :458 `test_extract_model` 之后）— 新增 8 个单测（完整方法体）：

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

9. `ctyun-stream-fix-proxy.test.py` `AdminIntegrationTest`（:1219，插在 :1919 `test_finish_tail_over_cap_fails_open` 之后、:1922 `if __name__` 之前）— 新增端到端（harness 能起真代理+假上游并断言收到的 payload，故按约加此用例）：

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

## Acceptance Criteria

- `cd /Users/peter/Documents/project/ctyun-stream-fix-proxy && /usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿 exit 0，输出 `Ran 76 tests` / `OK`（baseline 67 + 新增 9；**禁用 Homebrew python3**，有 http.server 挂死坑）。
- 真机验收（部署重启后）：从 `~/.zcode/cli/rollout/model-io-sess_subagent_agent_8529320c-1706-4729-813d-004c3002a983.jsonl` 抠出 `error.message` 含「不符合支持」记录的 `request.body`，带 `Authorization: Bearer <~/.zcode/v2/config.json 中 provider c8fbbb08 的 options.apiKey>` POST `http://127.0.0.1:7920/chat/completions`，**≥8 次连续无 `"error"` 帧且收到真实 content**；另跑 1–2 次已知正常 body 对照。判据：修复无效时 8/8 干净概率 ≤ 0.25^8 ≈ 1.5e-5。
- 无 null-content 请求字节级不变：单测 `assertIs`（Files 8/9）锁死 identity 返回。
- 三副本 cmp 一致（两文件都要）：worktree 文件 ↔ `~/.local/bin` 部署副本 ↔ 合并后 main 文件，`cmp` exit 0；注意 `~/.local/bin/ctyun-stream-fix-proxy.test.py` 当前是 09-13 旧版，部署时一并覆盖。
- 重启纪律：重启前 `lsof -nP -iTCP:7920 -sTCP:ESTABLISHED` 必须 0 条；`launchctl kickstart -k gui/$(id -u)/com.ctyun-stream-fix-proxy` 后 7920 LISTEN 在、`http://127.0.0.1:7921/api/stats` 的 `requests_total` 保留、`uptime_s` 归零。
- commit 只在本地，**禁止 push GitHub**。回滚：`git checkout <上一版> -- ctyun-stream-fix-proxy.py ctyun-stream-fix-proxy.test.py` → cp 两文件回 `~/.local/bin` → kickstart。

## Risks

- 仓库 plist 模板 `ProgramArguments` 指向 `/usr/local/bin`（`com.ctyun-stream-fix-proxy.plist:10`），与实际运行副本 `~/.local/bin` 口径不一致：kickstart 按 Label 重启不受影响；若部署后行为不符，先 `launchctl print gui/$(id -u)/com.ctyun-stream-fix-proxy` 核对实际 program 路径。
- 含 null 的请求经 `json.dumps` 重序列化后空白/转义布局变化，该轮上游 prompt cache 可能 miss——原请求本就 400 拒收，无净损失；无 null 请求 identity 透传不受影响。
- 间歇性根因单测无法复现：单测只锁代理侧行为（上游收到归一体），端到端有效性由卡 2 真机重放统计验证。
- 锚点核对结论：主代理给定行号与两份副本实测一致（`extract_model` :170、`_proxy_relay` :594、:597/:599/:611/:634；`~/.local/bin` 主文件同行号），未发现事实性冲突。

## Exclusions

- 不按模型分流：null→"" 对全模型语义等价无损（v4-pro 零命中也无损）。
- 不加 env 开关；不动 header（Content-Length 由 http.client 重算）；不动 v1.4/v1.5 空流/finish 重试逻辑与 SSE 滤毒。
- `Validation: Unsupported parameter(s): enable_thinking` 间歇校验错：独立问题，不进本 spec（另开 followup）。
- 不动 dashboard/admin；不 push GitHub。

## R31 Evidence

[R31-S1] 问题现场命中（2026-09-17 UTC 03:08–03:34，主代理会话取证）：10 个 reviewer 子代理会话连环挂，全部死于第一轮工具调用后的第二个请求；错误包在 HTTP 200 的 SSE 帧内，N 恒为 null 消息下标：
```
data:{"error":{"code":400,"message":"{\"error\":{\"message\":\"请求参数 'messages[N]' 不符合支持的格式\",...
```
失败体直打 7920 重放失败率 ~60–75%（同 body 时过时不过）；交错对照仅 null→"" 归一后两个失败体 46/46 全过、同轮不动则 ~60% 失败。失败记录定位命令（卡 2 真机验收输入）：
```
$ grep -c '不符合支持的格式' ~/.zcode/cli/rollout/model-io-sess_subagent_agent_8529320c-1706-4729-813d-004c3002a983.jsonl
$ jq -r 'select(.error.message | contains("不符合支持的格式")) | .request.body' <上jsonl> | head -1 > /tmp/failed-body.json
```
[R31-S2] 根因：ZCode 工具调用轮的 assistant 消息即 `content:null`（OpenAI 合法格式），网关当日新增严格化后间歇拒收；代理 `_proxy_relay` 读出 body 后原样透传，无归一层：
```
$ sed -n '594,599p' ctyun-stream-fix-proxy.py
    def _proxy_relay(self, started: float) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length > 0 else None
        model = extract_model(body)

        fwd_headers = {}
```
修复面在代理进程内一处纯函数 + 一行插入点，客户端与网关零改动。

## 卡划分（供 PLAN-B 参考）

- **卡 1（A/B → executor，TDD）**：先写 Files 3-9 测试跑红（AttributeError：模块无 `normalize_null_assistant_content`），commit `test(proxy): ... 先红`；再落 Files 1-2 实现跑绿，commit `feat(proxy): ...`；跑全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 贴 `Ran 76 tests` / `OK` / exit 0。
- **卡 2（C → implementer）**：部署 `cp` 两文件到 `~/.local/bin` + 双 `cmp` 验一致 → `lsof` ESTABLISHED 为 0 → `launchctl kickstart -k` → LISTEN 在 + `/api/stats` requests_total 保留、uptime 归零 → 真机重放 ≥8 次干净 + 1–2 次对照，全程贴原始 stdout/exit code。
