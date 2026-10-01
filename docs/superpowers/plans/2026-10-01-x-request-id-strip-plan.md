# PLAN — X-Request-Id 上游重复头剥离（简化 PLAN）

- **Goal**：上游响应头自带 `X-Request-Id` 时，代理在转发前剥除它（两条头复制路径的 skip 元组各追加 `"x-request-id"`），保证客户端只收到代理自己的 `X-Request-Id: r-N` 一个值。
- **Spec**：`docs/superpowers/specs/2026-10-01-x-request-id-strip-design.md`（本 worktree 内）。
- **分支/worktree**：`fix/x-request-id-strip` @ `/Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/x-request-id-strip`。
- **Baseline**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 224 用例全绿，exit 0（本会话实测：`Ran 224 tests in 114.924s / OK`）。
- **followups.md:7**：随本 episode 消化的文档行删除在 DELIVER 时由主代理处理，不属于实现卡。

## Global Constraints

1. **TDD 顺序**：卡 1（测试基建 + 2 新测试）先落地并验证红（`get_all` 返回 `["upstream-rid", "r-1"]` 双值）；卡 2 再改生产代码转绿。禁止颠倒。
2. **解释器**：所有验证命令用 `/usr/bin/python3`；exit code 用 `; echo "EXIT: $?"` 直接取真值（不接管道）。
3. **改动面**：只改 spec Files 列出的 2 个文件；不碰 `_reply_502`/`_reply_tpm_429`/`_send` 合成响应路径、`_req_id` 生成、客户端读头 helper。
4. **不加依赖、不改配置、不建新文件**（除本 PLAN 与测试内联代码）。
5. **耦合条款**：新测试的 proxy 重启块按当前惯例写死 inline terminate/wait/rmtree 五行（与 `test_502_response_carries_x_request_id` 同款），**不加 TODO**；Episode B 的 `stop_proxy` helper 落地后统一收敛（spec Risks 已记该顺序耦合）。
6. **断言口径**：必须用 `resp.msg.get_all("X-Request-Id")`（`getheader` 会把重复头合并成逗号串，无法区分单/双值）。
7. **Edit 纪律**：每处 Edit 的 old_string 均以本会话 Read 实测为锚（行号见各卡），逐字匹配（含缩进）；old/new 必须不同。
8. **提交**：实现卡执行者只改代码 + 跑测试，不做 git 操作（commit 归主代理）。

## 任务卡

### 卡 1（tier A）— 测试基建 + 2 个失败测试（红）

**文件**：`ctyun-stream-fix-proxy.test.py`（仅此一个文件，6 处 Edit）

**目标**：给假上游加可选 `X-Request-Id` 响应头开关，并新增 2 个黑盒测试断言"上游带 rid 头 → 客户端只收 `["r-1"]` 单值"。此卡完成后两新测试必红。

**Edit 1-1** — `FakeUpstreamHandler` 类属性区（锚 :75，唯一匹配 `head_fail` 行）：

old_string：
```
    head_fail = False    # P4 probe 测试：True → do_HEAD 回 500（连续失败告警场景）
```
new_string：
```
    head_fail = False    # P4 probe 测试：True → do_HEAD 回 500（连续失败告警场景）
    x_request_id = None  # 非 None → 正常路径响应头携带上游 X-Request-Id（剥除测试用）
```

**Edit 1-2** — `do_POST` 正常路径（锚 :203-207；`body += SSE_DONE` 唯一出现于 :203，后随 `try:` 与 `_respond_big_sse` 区分）：

old_string：
```
            body += SSE_DONE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        try:
```
new_string：
```
            body += SSE_DONE
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        if self.x_request_id:
            self.send_header("X-Request-Id", self.x_request_id)
        self.end_headers()
        try:
```

**Edit 1-3** — `do_GET`（锚 :237-241；`Content-Length, str(len(body))` + 后随 `self.wfile.write(body)` 唯一）：

old_string：
```
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
```
new_string：
```
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        if self.x_request_id:
            self.send_header("X-Request-Id", self.x_request_id)
        self.end_headers()
        self.wfile.write(body)
```

**Edit 1-4** — `make_fake_upstream` 签名（锚 :277，唯一匹配 `tail_delay_after_done_s: float = 0.0) -> int:`）：

old_string：
```
                       tail_delay_after_done_s: float = 0.0) -> int:
```
new_string：
```
                       tail_delay_after_done_s: float = 0.0,
                       x_request_id: str = None) -> int:
```

**Edit 1-5** — `make_fake_upstream` attrs dict（锚 :294-295）：

old_string：
```
             "tail_delay_after_done_s": tail_delay_after_done_s,
             "bodies": [] if record_bodies else None}
```
new_string：
```
             "tail_delay_after_done_s": tail_delay_after_done_s,
             "x_request_id": x_request_id,
             "bodies": [] if record_bodies else None}
```

**Edit 1-6** — `RequestIdTest` 追加 2 测试（锚 :4554-4557，插在 `test_502_response_carries_x_request_id` 末尾断言与 `class TpmAdmitHookTest` 之间）：

old_string：
```
        self.assertIsNotNone(m,
            "502 响应必须带 X-Request-Id，got %r" % x_request_id)

class TpmAdmitHookTest(unittest.TestCase):
```
new_string：
```
        self.assertIsNotNone(m,
            "502 响应必须带 X-Request-Id，got %r" % x_request_id)

    def test_upstream_x_request_id_stripped_for_sse(self) -> None:
        """上游自带 X-Request-Id 时 SSE 路径剥除，客户端只收代理 rid 单值。"""
        upstream_port = make_fake_upstream(False, x_request_id="upstream-rid")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        self.proc = start_proxy(upstream_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        conn.request("POST", "/v1/chat/completions",
                     body=b'{"model":"m","stream":true,"messages":[]}',
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        values = resp.msg.get_all("X-Request-Id")
        resp.read()
        conn.close()
        self.assertEqual(values, ["r-1"],
                         "客户端必须只收到代理 rid 一个值，got %r" % (values,))

    def test_upstream_x_request_id_stripped_for_plain(self) -> None:
        """上游自带 X-Request-Id 时非流式（/plain → _relay_buffered）路径剥除。"""
        upstream_port = make_fake_upstream(False, x_request_id="upstream-rid")
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        shutil.rmtree(self.proc.persist_dir, ignore_errors=True)
        self.proc = start_proxy(upstream_port, free_port())
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        conn.request("GET", "/plain")
        resp = conn.getresponse()
        values = resp.msg.get_all("X-Request-Id")
        resp.read()
        conn.close()
        self.assertEqual(values, ["r-1"],
                         "客户端必须只收到代理 rid 一个值，got %r" % (values,))

class TpmAdmitHookTest(unittest.TestCase):
```

**依赖已核实**：`http.client`（:15）、`shutil`（:21）、`stderr_text`（:403）、`free_port`（:429）、`start_proxy(upstream_port, proxy_port, extra_env=None, seed_persist=None)`（:448）、`stop_fake_upstreams`（:386）均存在；代理无 model 白名单（:862 仅读取 model 字段），`{"model":"m"}` 安全；重启块内 `start_proxy(upstream_port, free_port())` 与 :4540 同款（proxy_port 换新 free port）；tearDown（:4469）会清理重启后的 `self.proc` 并 `stop_fake_upstreams()`，测试内无需重复。

**红验证（此卡完成时执行，预期 exit 非 0，双值失败）**：

```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/x-request-id-strip
/usr/bin/python3 ctyun-stream-fix-proxy.test.py RequestIdTest.test_upstream_x_request_id_stripped_for_sse RequestIdTest.test_upstream_x_request_id_stripped_for_plain; echo "EXIT: $?"
```

预期：2 个失败，FAIL 消息含 `["upstream-rid", "r-1"]`；exit 1。贴 stdout/stderr 原文与 exit code。

---

### 卡 2（tier A）— 生产修复：两处 skip 元组追加 `"x-request-id"`（绿）

**文件**：`ctyun-stream-fix-proxy.py`（仅此一个文件，2 处 Edit）

**Edit 2-1** — `_send_sse_headers`（锚 :1924-1929；`X-Request-Id` 后随 `Connection close` 唯一标识此函数）：

old_string：
```
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("X-Request-Id", self._req_id)
        self.send_header("Connection", "close")
```
new_string：
```
            if name.lower() in ("content-length", "transfer-encoding", "connection",
                                "x-request-id"):
                continue
            self.send_header(name, value)
        self.send_header("X-Request-Id", self._req_id)
        self.send_header("Connection", "close")
```

**Edit 2-2** — `_relay_buffered`（锚 :2062-2066；后随 `Content-Length, str(len(data))` 唯一标识此函数）：

old_string：
```
            if name.lower() in ("content-length", "transfer-encoding", "connection"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
```
new_string：
```
            if name.lower() in ("content-length", "transfer-encoding", "connection",
                                "x-request-id"):
                continue
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(data)))
```

**语义说明**：`getheaders()` 返回原始大小写头名（如 `X-Request-Id`），`name.lower()` 比较保证大小写无关剥除（现有模式不变）；:1928/:2067 追加代理 rid 的行不动；`_reply_502`/`_reply_tpm_429`/`_send` 为合成响应不调 `getheaders()`，天然无上游头，不受影响。

**绿验证（此卡完成时按序执行）**：

```
cd /Users/peter/Documents/project/ctyun-stream-fix-proxy/.worktrees/x-request-id-strip
/usr/bin/python3 ctyun-stream-fix-proxy.test.py RequestIdTest.test_upstream_x_request_id_stripped_for_sse RequestIdTest.test_upstream_x_request_id_stripped_for_plain; echo "EXIT: $?"
```
预期：2 个通过（`get_all` == `["r-1"]`），exit 0。

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py RequestIdTest; echo "EXIT: $?"
```
预期：7 个通过（原 5 + 新 2），exit 0。

**Whole-branch 收尾（最后一卡执行者同次 dispatch 跑，全量回归）**：

```
/usr/bin/python3 ctyun-stream-fix-proxy.test.py; echo "EXIT: $?"
```
预期：**226** 用例（baseline 224 + 新 2）全绿，exit 0，无 FAIL/ERROR。

```
grep -n 'x-request-id' ctyun-stream-fix-proxy.py
```
预期：两处 skip 元组各含 `"x-request-id"`（约 :1925-1926 与 :2063-2064），:1928/:2067 追加 rid 行不变。

---

## PLAN Self-Review（5 项，B 完成后必做）

1. **Spec coverage**：spec Files 三条全覆盖——生产两处 skip 元组（卡 2）+ 测试基建三处（类属性/do_POST/do_GET/make_fake_upstream 签名与 attrs = 卡 1 Edit 1-1..1-5）+ 2 新测试（卡 1 Edit 1-6）。Acceptance 四项：新测试红→绿（卡 1/卡 2 验证命令闭环）、全量 224→226 绿（whole-branch 收尾）、grep 验证两处 `x-request-id`（收尾命令）、`get_all` 恒 `["r-N"]`（断言本身）。
2. **Placeholder scan**：全 PLAN 无 TODO/"待补"/"此处实现"类占位；inline 重启块按 spec 写死五行，无预留。唯一"后续"表述为耦合条款（spec 自身 Risks 记载，非占位）。
3. **Type consistency**：`x_request_id: str = None` 与 spec 一致，与同签名 `sleep_stall_seconds: float = 3.0` 风格一致；`if self.x_request_id:` 真值判断与现有 `body_override` 开关同型；测试断言 `["r-1"]` 与新起 proxy 的 rid 起始值一致（fresh process → r-1，与 :4488-4489 既有断言同口径）。
4. **可落盘性**：8 处 Edit 全部 old/new 双写，old_string 均经本会话 Read 逐字核对（含缩进），每处含唯一性论证或唯一上下文锚；验证命令带完整 cd 路径与 exit 取真值写法，执行者无需设计决策。
5. **锚点实测**：本会话 Read 实测生产 :1922-1930（_send_sse_headers）与 :2055-2074（_relay_buffered）逐字匹配；测试 :50-75（类属性区）、:195-216（do_POST）、:235-241（do_GET）、:263-315（make_fake_upstream）、:4458-4557（RequestIdTest 与 502 测试）逐字匹配；helper 存在性经 grep 确认（:15/:21/:386/:403/:429/:448）；baseline 224 全绿 exit 0 实跑确认。
