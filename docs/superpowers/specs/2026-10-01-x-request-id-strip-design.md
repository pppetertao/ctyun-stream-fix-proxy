# 2026-10-01 X-Request-Id 上游重复头剥离 — design spec

## Goal

上游响应头若自带 `X-Request-Id`，代理透传后再追加自己的 `r-N`，客户端会收到两个值，削弱 rid 关联保证；本 spec 在转发前剥除上游同名头，保证客户端只收到代理自己的一个 `X-Request-Id: r-N`。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:1922` `_send_sse_headers(resp)` — 头复制循环（实测 :1924-1927）skip 元组追加 `"x-request-id"`：

```python
for name, value in resp.getheaders():
    if name.lower() in ("content-length", "transfer-encoding", "connection",
                        "x-request-id"):
        continue
    self.send_header(name, value)
self.send_header("X-Request-Id", self._req_id)   # :1928 追加代理 rid，语义不变
```

2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:2055` `_relay_buffered(resp)` — 非流式路径头复制循环（实测 :2062-2065）同款追加 `"x-request-id"`；:2067 `send_header("X-Request-Id", self._req_id)` 保持不变。

3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py` 三处测试基建 + 新用例：

   - `FakeUpstreamHandler` 类属性区（实测 :50-75）加 `x_request_id = None`；`do_POST` 正常路径（实测 :204-206）与 `do_GET`（实测 :235-241）在 `self.send_header("Content-Type", ...)` 之后、`end_headers()` 之前加：

```python
if self.x_request_id:
    self.send_header("X-Request-Id", self.x_request_id)
```

   - `make_fake_upstream`（实测 :263-315）：签名追加 `x_request_id: str = None`，attrs dict（实测 :278-295）加 `"x_request_id": x_request_id`。
   - `RequestIdTest`（实测 :4458）加 2 个测试，各建自带上游头 fake upstream + 独立代理（复用 :4536-4540 的 restart 模式；spec B 落地后改用 `stop_proxy` helper）。断言用 `resp.msg.get_all("X-Request-Id")`（http.client 的 `getheader` 会把重复头合并成逗号串，无法区分单值/双值）：

```python
def test_upstream_x_request_id_stripped_for_sse(self) -> None:
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
```

   第二个测试同构走 `GET /plain`（`_relay_buffered` 路径），断言相同。`stop_fake_upstreams()` 由 :4469 tearDown 兜底，测试内无需重复。

注意 `_reply_502`（:2078）、`_reply_tpm_429`（:2089）、`_send`（:2285）均为合成响应（不调 `resp.getheaders()`），天然无上游头，不受本次改动影响；`test_502_response_carries_x_request_id`（:4534）继续有效。

## Acceptance Criteria

- 新加 2 测试在修复前红（`values == ["upstream-rid", "r-1"]` 或逗号合并串）、修复后绿；`/usr/bin/python3 ctyun-stream-fix-proxy.test.py RequestIdTest` 全绿。
- 全量 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 224 用例全绿（贴 stdout/exit code）。
- `_send_sse_headers` 与 `_relay_buffered` 的 skip 元组各含 `x-request-id`（grep -n 验证 :1925 与 :2063 两处），且上游头名大小写无关（`name.lower()` 比较不变）。
- 客户端 `msg.get_all("X-Request-Id")` 恒为 `["r-N"]` 单元素。
- followups.md:7 条目随本 episode 消化删除（一行文档改动，DELIVER 时处理）。

## Risks

- 假上游 `x_request_id` 头只加在 do_POST 正常路径与 do_GET；`stall/rst/empty_stream` 等变体路径（:87-175）不经 :204-206，若新测试误用这些变体会拿不到上游头——新测试只用默认正常路径。
- `getheaders()` 返回原始大小写（如 `X-Request-Id`），skip 依赖 `name.lower()` 比较——现有代码已统一该模式，改动沿用它即可。
- 若真实 ctyun 上游发的头名是 `x-request-id` 变体（如带空格/多值），HTTP 层 normalize 后仍是同名多值，`getheaders()` 会逐个返回、逐个被 skip，无遗漏。
- 与 Spec B 顺序耦合：本 spec 测试内联 terminate/wait 块，若 B 先落地改用 `stop_proxy(self.proc)` 替换 :4537-4539 三行（含 rmtree），否则保留当前样式。

## Exclusions

- 不改客户端读头逻辑（`post_sse_with_headers` :496 / `get_plain_with_headers` :524 保持 `getheader` 单一值语义）。
- 不做 `X-Upstream-Request-Id` rename 透传方案（保留上游溯源价值）——本 spec 采彻底剥除，rid 保证优先。
- 不动 502/429/`_send` 合成响应路径；不动 `_req_id` 生成与 REQ 行日志格式。

## R31 Evidence

[R31-S1] 现状证据（本会话实测）：两条上游响应头复制路径无条件透传上游全部响应头，排除清单只有 content-length/transfer-encoding/connection，X-Request-Id 不在其中；两条路径随后各自追加代理 rid，形成双值路径。

```
$ grep -n 'send_response\|getheaders()' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
1923:        self.send_response(resp.status)
1924:        for name, value in resp.getheaders():
2061:        self.send_response(resp.status)
2062:        for name, value in resp.getheaders():
2080:        self.send_response(502)
2096:        self.send_response(429)
2286:        self.send_response(status)
```

```
$ grep -n 'X-Request-Id' /Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py
1928:        self.send_header("X-Request-Id", self._req_id)
2067:        self.send_header("X-Request-Id", self._req_id)
2082:        self.send_header("X-Request-Id", self._req_id)
2098:        self.send_header("X-Request-Id", self._req_id)
```

[R31-S2] 根因：:1924 与 :2062 两处 getheaders 循环把上游 X-Request-Id 原样透传，随后 :1928/:2067 追加代理 self._req_id，客户端收到两个值，rid 关联保证被稀释。需求/裁决来源：docs/superpowers/followups.md:7（"若上游自带 X-Request-Id 头，代理透传后再追加自己的，客户端会收到两个值，削弱 rid 关联保证"，建议剥除或 rename）+ 用户 2026-10-01 "开 task 处理本会话的遗留问题"。裁决：采彻底剥除（hop-by-hop，rid 保证优先），rename 方案列入 Exclusions。_reply_502(:2078)/_reply_tpm_429(:2089)/_send(:2285) 为合成响应不调 getheaders，天然无上游头，不受影响。
