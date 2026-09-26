# PLAN：上游头阶段 ConnectionResetError 纳入 header-stall 一次自动重试

日期：2026-09-26
spec：docs/superpowers/specs/2026-09-26-connreset-retry-design.md
分支：fix/connreset-retry
baseline：112 tests OK，46.253s（本 PLAN 写作时实测，见附录）

## Global Constraints

- **卡序 = TDD 序（红→绿）**：任务卡 1（测试）→ 任务卡 2（实现）。以下"卡 1/卡 2"按执行序编号（与 spec 原文分组倒序——spec 卡1=源码、卡2=测试，此处交换以满足先红后绿）。
- **单 except 元组扩列**：`except (socket.timeout, http.client.RemoteDisconnected, ConnectionResetError)`，不复复制重试体。
- **retry_reason isinstance 选择**：先判 `(socket.timeout, RemoteDisconnected)` → "header-timeout"，else（只有 ConnectionResetError）→ "conn-reset"。顺序关键——RemoteDisconnected ⊂ ConnectionResetError，必须排在前面。
- **零新增 kind/计数器/持久化键**：复用 `ERR_KIND_HEADER_TIMEOUT` / `_record_header_retry` / `header_retries_total`，不重命名任何函数。
- **RST 夹具**：`SO_LINGER struct.pack("ii",1,0)`（macOS 8 字节 linger struct）；`use_threading` 扩 `rst_all or rst_calls`（防 shutdown 挂）。
- **验证命令**：`/usr/bin/python3 ctyun-stream-fix-proxy.test.py`（worktree 根目录 + 语法检查 `python3 -c "import py_compile; py_compile.compile('ctyun-stream-fix-proxy.py', doraise=True)"`）。
- **退化预案（已写入 spec）**：若白盒用例③因 macOS 环回偶发 RemoteDisconnected（FIN 竞态），删除 `self.assertIs(type(cm.exception), ...)` 行，保留 assertRaises(ConnectionResetError)。此为预授权机械操作，不入 C 档。

---

## 任务卡 1（tier A，TDD 序第 1 —— 红）

**目标**：落测试夹具（rst_all/rst_calls、make_rst_upstream）与 3 组用例，用全量 run 证明 gap 存在。

### 文件

`ctyun-stream-fix-proxy.test.py`

### 编辑（9 笔，共一文件）

#### Edit 1 — import struct（字母序，插入 :22 socket 与 :23 subprocess 之间）

**old_string:**
```
import socket
import subprocess
```
**new_string:**
```
import socket
import struct
import subprocess
```

#### Edit 2 — FakeUpstreamHandler rst_all / rst_calls 类属性（:58-59 stall 声明处之后、:60 calls 之前）

**old_string:**
```
    stall_all = False       # True → 每呼读 body 后 close_connection=True 直接返回（代理 getresponse 抛 http.client.RemoteDisconnected）
    stall_calls = ()        # 1-based 呼叫序号元组：命中则 close_connection=True 不写响应（代理 getresponse 抛 http.client.RemoteDisconnected）
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流
```
**new_string:**
```
    stall_all = False       # True → 每呼读 body 后 close_connection=True 直接返回（代理 getresponse 抛 http.client.RemoteDisconnected）
    stall_calls = ()        # 1-based 呼叫序号元组：命中则 close_connection=True 不写响应（代理 getresponse 抛 http.client.RemoteDisconnected）
    rst_all = False         # True → 每呼读 body 后 SO_LINGER(1,0) close 强制发 RST（代理 getresponse 抛 ConnectionResetError）
    rst_calls = ()          # 1-based 呼叫序号元组：命中则 SO_LINGER(1,0) close 强制发 RST（同 rst_all 形态）
    calls = None          # 共享 list：非 None 时按调用序 append 计数；无 body_override 时首次回空流
```

#### Edit 3 — do_POST rst 分支（:81 # end stall 之后、:83 fail_500 之前）

**old_string:**
```
        # --- end stall ---
        if self.fail_500:
```
**new_string:**
```
        # --- end stall ---
        if self.rst_all or (self.rst_calls and self.calls is not None
                            and len(self.calls) in self.rst_calls):
            # SO_LINGER(1,0) close 强制发 RST（非 FIN），代理 getresponse 抛
            # ConnectionResetError；macOS 要求 8 字节 linger struct，int 直传 EINVAL
            self.request.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER,
                                    struct.pack("ii", 1, 0))
            self.close_connection = True
            return
        if self.fail_500:
```

#### Edit 4 — make_fake_upstream 签名加 rst_all / rst_calls（:194）

**old_string:**
```
                       stall_all: bool = False, stall_calls: tuple = ()) -> int:
```
**new_string:**
```
                       stall_all: bool = False, stall_calls: tuple = (),
                       rst_all: bool = False, rst_calls: tuple = ()) -> int:
```

#### Edit 5 — attrs dict 加 rst_all / rst_calls（:201-202）

**old_string:**
```
             "stall_all": stall_all,
             "stall_calls": stall_calls,
             "bodies": [] if record_bodies else None}
```
**new_string:**
```
             "stall_all": stall_all,
             "stall_calls": stall_calls,
             "rst_all": rst_all,
             "rst_calls": rst_calls,
             "bodies": [] if record_bodies else None}
```

#### Edit 6 — use_threading 扩 rst 变体（:209）

**old_string:**
```
    use_threading = stall_all or stall_calls
```
**new_string:**
```
    use_threading = stall_all or stall_calls or rst_all or rst_calls
```

#### Edit 7 — 新增 make_rst_upstream（:234 make_stall_upstream 之后、:237 make_body_recording_upstream 之前）

**old_string:**
```
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls


def make_body_recording_upstream(**kwargs) -> tuple:
```
**new_string:**
```
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls


def make_rst_upstream(rst_all: bool = False, rst_calls: tuple = (), **kwargs) -> tuple:
    """带调用计数的 RST 假上游：返回 (port, calls)。
    rst_all/rst_calls 控制哪些呼叫在读 body 后 SO_LINGER(1,0) close 强制发 RST。
    其余 kwargs 透传 empty_stream / body_override 等。"""
    port = make_fake_upstream(False, scripted=True,
                              rst_all=rst_all, rst_calls=rst_calls, **kwargs)
    return port, FAKE_SERVERS[-1].RequestHandlerClass.calls


def make_body_recording_upstream(**kwargs) -> tuple:
```

#### Edit 8 — 白盒用例③ test_open_upstream_rst_raises_connection_reset_error（:1753 之后、:1756 class AdminIntegrationTest 之前，属于 ProxyDashboardUnitTest）

**old_string:**
```
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()


class AdminIntegrationTest(unittest.TestCase):
```
**new_string:**
```
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()

    def test_open_upstream_rst_raises_connection_reset_error(self) -> None:
        """白盒：RST 上游 → _open_upstream 在 HEADER_TIMEOUT_S 内抛 ConnectionResetError。

        SO_LINGER(1,0) close 强制发 RST（非 FIN-close），代理侧抛 ConnectionResetError
        且类型恰为 ConnectionResetError（非其子类 RemoteDisconnected——后者是 FIN 竞态
        产物，此处锁死真 RST 以证夹具确定性）。"""
        mod = self.mod
        import types
        upstream_port = make_fake_upstream(False, rst_all=True)
        try:
            orig_base = mod.UPSTREAM_BASE
            mod.UPSTREAM_BASE = "http://127.0.0.1:%d" % upstream_port
            try:
                orig_ht = mod.HEADER_TIMEOUT_S
                mod.HEADER_TIMEOUT_S = 0.5
                try:
                    body = b'{"model":"test","stream":true,"messages":[]}'
                    with self.assertRaises(ConnectionResetError) as cm:
                        mod.ProxyHandler._open_upstream(
                            types.SimpleNamespace(), "POST", "/v1/chat/completions",
                            body, {"Content-Type": "application/json"})
                    self.assertIs(type(cm.exception), ConnectionResetError,
                                  "RST fixture must raise exactly ConnectionResetError,"
                                  " not a subclass like RemoteDisconnected")
                finally:
                    mod.HEADER_TIMEOUT_S = orig_ht
            finally:
                mod.UPSTREAM_BASE = orig_base
        finally:
            stop_fake_upstreams()


class AdminIntegrationTest(unittest.TestCase):
```

#### Edit 9 — spawn 用例①②（:2882 test_header_stall_retry_disabled 尾部之后、:2884 test_header_timeout_does_not_leak_into_body_phase 之前，属于 AdminIntegrationTest）

**old_string:**
```
        self.assertEqual(len(calls), 1,
                         "CTYUN_HEADER_RETRY=0 must disable retry, calls=%d" % len(calls))

    def test_header_timeout_does_not_leak_into_body_phase(self) -> None:
```
**new_string:**
```
        self.assertEqual(len(calls), 1,
                         "CTYUN_HEADER_RETRY=0 must disable retry, calls=%d" % len(calls))

    def test_header_rst_retry_success(self) -> None:
        """rst_calls=(1,) → 首呼 RST 触发 conn-reset 重试 → 次呼正常 → calls==2 + SSE 完整。"""
        upstream_port, calls = make_rst_upstream(rst_calls=(1,))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        data = post_sse(self.proc.proxy_port)
        self.assertEqual(len(calls), 2,
                         "header RST must trigger exactly one retry, calls=%d" % len(calls))
        self.assertEqual(data, SSE_A + SSE_B + SSE_DONE,
                         "attempt-2 must relay byte-exact stream, got %r" % data)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=conn-reset", stderr,
                      "REQ line must carry retry_reason=conn-reset, stderr:\n" + stderr)
        with open(os.path.join(self.proc.persist_dir, "settings.json"),
                  encoding="utf-8") as fh:
            saved = json.load(fh)["stats"]
        self.assertGreaterEqual(saved.get("header_retries_total", 0), 1,
                                "header_retries_total must be >=1 after RST retry")

    def test_header_rst_both_timeout_returns_502(self) -> None:
        """rst_all → 首呼+重试均 RST → 502 + calls==2 + synth_502 留痕含 retried=1 / conn-reset。"""
        upstream_port, calls = make_rst_upstream(rst_all=True)
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr_text(self.proc)
        self.proc = start_proxy(upstream_port, free_port(),
                                extra_env={"CTYUN_HEADER_TIMEOUT": "1"})
        conn = http.client.HTTPConnection("127.0.0.1", self.proc.proxy_port, timeout=30)
        body = b'{"model":"m","stream":true,"messages":[]}'
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        status = resp.status
        resp.read()
        conn.close()
        self.assertEqual(status, 502, "double RST must synthesize 502, got %d" % status)
        self.assertEqual(len(calls), 2,
                         "rst_all must trigger exactly one retry then fail, calls=%d" % len(calls))
        self.proc.terminate()
        self.proc.wait(timeout=5)
        stderr = stderr_text(self.proc)
        self.assertIn("retried=1", stderr,
                      "502 REQ line must carry retried=1, stderr:\n" + stderr)
        self.assertIn("retry_reason=conn-reset", stderr,
                      "502 REQ line must carry retry_reason=conn-reset, stderr:\n" + stderr)

    def test_header_timeout_does_not_leak_into_body_phase(self) -> None:
```

### 验证（期望红 —— 证明 gap）

**步骤 1：语法检查**
```bash
/usr/bin/python3 -c "import py_compile; py_compile.compile('ctyun-stream-fix-proxy.test.py', doraise=True)"
echo "Exit code: $?"
# 期望 exit 0
```

**步骤 2：全量跑（预期红）**
```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1
echo "Exit code: $?"
# 期望输出中包含：
#   - test_header_rst_retry_success ... FAIL（post_sse 因代理合成 502 而 AssertionError）
#   - test_header_rst_both_timeout_returns_502 ... FAIL（calls==1 != 2）
#   - test_open_upstream_rst_raises_connection_reset_error ... ok（夹具产出真 RST，已能通过 _open_upstream 直调）
#   - 其余 112 个已有测试 ... ok
# Exit code = 1（2 个新增失败 = 预期红）
```

**步骤 3：夹具确定性单独验证**
```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py ProxyDashboardUnitTest.test_open_upstream_rst_raises_connection_reset_error
echo "Exit code: $?"
# 期望 exit 0（证明 SO_LINGER 夹具在环回接口产出真 ConnectionResetError）
```

**验收**：① 和 ② FAIL（证明当前生产代码不重试 ConnectionResetError → gap 确认），③ OK（证明夹具确定性）。Exit code=1 为预期。若 ③ 失败（非 ConnectionResetError）→ 停止报告主代理（夹具环境不匹配 spec 预期）。

若 ③ 因 macOS 环回偶发 RemoteDisconnected（FIN 竞态，spec Risks 记录），执行预授权退化：删除 `self.assertIs(type(cm.exception), ConnectionResetError, ...)` 行（仅保留 `with self.assertRaises(ConnectionResetError)`），重试全量后仍红（①② 失败不变）。

---

## 任务卡 2（tier A，TDD 序第 2 —— 绿）

**目标**：更新生产代码 `except` 元组 + `isinstance` retry_reason + 注释/docstring，使卡 1 新增 3 用例全绿且既有 112 测试无回归。

### 文件

`ctyun-stream-fix-proxy.py`

### 编辑（1 笔——span 覆盖 except 元组 + 注释 + retry_reason 逻辑 + record_error_event 入参；加 1 笔 docstring）

#### Edit P1 — :865-876 整体替换（except 元组 → record_error_event）

**old_string:**
```
            except (socket.timeout, http.client.RemoteDisconnected) as first_exc:
                # socket.timeout = 响应头阶段阻塞到 HEADER_TIMEOUT_S；RemoteDisconnected
                # = 上游在响应头阶段直接关连接（未发任何响应字节）。两者同为"响应头阶段
                # 未收到任何响应"（RemoteDisconnected ⊂ ConnectionError→OSError 且 ⊂
                # BadStatusLine→HTTPException，外层兜底亦可捕获），priming 不可见论证
                # 对二者同样成立：重试一次不损伤客户端交付。
                if not header_timeout_should_retry(HEADER_RETRY_MAX):
                    raise
                header_retried = 1
                header_retry_reason = "header-timeout"
                record_error_event(ERR_KIND_HEADER_TIMEOUT, model=model, path=self.path,
                                   exc=first_exc, body=body, retry_reason="header-timeout")
```
**new_string:**
```
            except (socket.timeout, http.client.RemoteDisconnected, ConnectionResetError) as first_exc:
                # socket.timeout = 响应头阶段阻塞到 HEADER_TIMEOUT_S；RemoteDisconnected
                # = 上游在响应头阶段直接关连接（未发任何响应字节）；ConnectionResetError =
                # connect/TLS/request 发送/getresponse 头读阶段被 RST。三者同为"响应头阶段
                # 未收到任何响应字节"，priming 不可见论证对三者同样成立：重试一次不损伤
                # 客户端交付。retry_reason 有意偏离阶段命名：RST 指向上游 LB 健康、timeout
                # 指向上游慢，运维 grep 需区分；kind 与计数器仍按阶段复用 header_timeout，
                # 避免 9 触点统计形状改动。
                if not header_timeout_should_retry(HEADER_RETRY_MAX):
                    raise
                header_retried = 1
                header_retry_reason = ("header-timeout"
                                       if isinstance(first_exc, (socket.timeout,
                                                                 http.client.RemoteDisconnected))
                                       else "conn-reset")
                record_error_event(ERR_KIND_HEADER_TIMEOUT, model=model, path=self.path,
                                   exc=first_exc, body=body, retry_reason=header_retry_reason)
```

#### Edit P2 — :171-173 header_timeout_should_retry docstring 更新

**old_string:**
```
    """头超时重试决策：budget > 0 时允许重试。调用点 :864（_proxy_relay 首呼
    (socket.timeout, RemoteDisconnected) catch）。"""
```
**new_string:**
```
    """头超时重试决策：budget > 0 时允许重试。调用点 :864（_proxy_relay 首呼
    (socket.timeout, RemoteDisconnected, ConnectionResetError) catch），覆盖
    头阶段三类可重试故障（阻塞超时/上游 FIN-close/上游 RST）。"""
```

### 验证（期望绿）

**步骤 1：语法检查**
```bash
/usr/bin/python3 -c "import py_compile; py_compile.compile('ctyun-stream-fix-proxy.py', doraise=True)"
echo "Exit code: $?"
# 期望 exit 0
```

**步骤 2：全量跑**
```bash
/usr/bin/python3 ctyun-stream-fix-proxy.test.py
echo "Exit code: $?"
# 期望: Ran 115 tests in <time>s  OK（112 既存 + 3 新增）
# 新增 3 用例全部 ok：
#   - test_header_rst_retry_success ... ok（首呼 RST → 重试 → 200 + SSE 完整 + retry_reason=conn-reset）
#   - test_header_rst_both_timeout_returns_502 ... ok（双 RST → 502 + retried=1 + retry_reason=conn-reset）
#   - test_open_upstream_rst_raises_connection_reset_error ... ok（夹具产真 RST）
# 回归红线：
#   - test_header_stall_retry_success ... ok（retry_reason 仍为 header-timeout，验证 isinstance 顺序正确）
#   - test_header_stall_both_timeout_returns_502 ... ok
#   - test_header_stall_retry_disabled ... ok（CTYUN_HEADER_RETRY=0 still disables retry）
#   - 其余 109 既存测试 ... ok
# Exit = 0
```

---

## 附录 A：baseline 登记

```bash
$ /usr/bin/python3 ctyun-stream-fix-proxy.test.py 2>&1 | tail -4
test_valid_upstream_url (__main__.ProxyDashboardUnitTest) ... ok
test_write_allowed_matrix (__main__.ProxyDashboardUnitTest) ... ok

----------------------------------------------------------------------
Ran 112 tests in 46.253s

OK
Exit code: 0
```

---

## Plan Self-Review

### 1. Spec Coverage（5 section → 卡内全覆盖）

- **Goal**：ConnectionResetError 纳入 header-stall 重试 → 卡 2 Edit P1 扩充 except 元组 ✓
- **Files to Change**：
  - `ctyun-stream-fix-proxy.py` :865/:873-876/:866-870/:171-174 → 卡 2 Edit P1（含 except+注释+retry_reason+record_error_event）+ Edit P2（docstring）✓
  - `ctyun-stream-fix-proxy.test.py` :23/:58-59/:81/:194/:201-202/:209/:234/:2807 镜像/:2836 镜像/:1728-1744 镜像 → 卡 1 Edit 1-9（import struct、rst_all/rst_calls、do_POST rst 分支、签名+attrs+use_threading、make_rst_upstream、③ 白盒+①② spawn）✓
- **Acceptance**：全绿 115 tests ✓、用例① retry_reason=conn-reset ② retried=1 ③ 真 ConnectionResetError ✓、stall 回归 header-timeout 不变 ✓、CTYUN_HEADER_RETRY=0 禁用不变 ✓ —— 全部映射到验证命令
- **Risks**：isinstance 顺序（卡 2 Edit P1，RemoteDisconnected 在前）+ 夹具确定性（卡 1 ③ 带 assertIs 锁死，附退化预案）+ Dashboard 免改（复用 kind/计数器，零触点改动，卡 2 注释同步说明）✓
- **Exclusions**：零新增 kind/计数器/持久化键 ✓、不重命名 header_timeout_should_retry ✓（卡 2 Edit P2 仅扩 docstring，不更名）、其余 4 项 excluded 均不改代码 ✓

### 2. Placeholder 扫描

全文扫描关键词："todo"/"TODO"/"FIXME"/"XXX"/"待实现"/"..."（省略号做占位符）/盲填的 `raise NotImplementedError` / 无实际代码的 `# implement here`。

**结果：0 命中。** 所有卡内代码块均为完整 Python 源码（import、类属性、分支逻辑、函数体、完整测试方法），无省略或无实现占位符。

### 3. 类型一致性

- **except 元组**：`first_exc` 类型可为 `socket.timeout | RemoteDisconnected | ConnectionResetError`；`isinstance(first_exc, (socket.timeout, RemoteDisconnected))` 对 socket.timeout/RemoteDisconnected 返回 True，对 ConnectionResetError（非 RemoteDisconnected）返回 False → "conn-reset" ✓
- **retry_reason 类型**：字符串字面量，流出路径：`header_retry_reason`（:861 初始 ""）→ 赋值 → `_log(..., retry_reason=header_retry_reason)`（str） ✓
- **ConnectionResetError**：builtin，无需 import；`socket.timeout` 已 import ✓
- **struct.pack("ii", 1, 0)**：返回 `bytes`（8 字节），符合 `setsockopt(SOL_SOCKET, SO_LINGER, bytes)` 签名（macOS 要求 8 字节 linger struct） ✓
- **make_rst_upstream 返回类型**：`tuple[int, list]` = `(port, calls)`，与 `make_stall_upstream` 同型 ✓
- **FakeUpstreamHandler.calls**：`None | list`，`:70-71` 仅 `is not None` 时 append → `rst_calls` 分支保护需要 `self.calls is not None` 和 `len(self.calls) in self.rst_calls` → 与 stall_calls 分支同形 ✓

### 4. 可落盘性（逐 Edit 锚点实测）

逐一跑 `grep -n` / `Read` 核验旧字符串在文件中的唯一性和行号吻合度——所有锚点在 CURRENT 源文件状态下行号与内容一致。具体：

| 编辑 | 文件 | 锚点 | 实测（:行号） | 唯一性 |
|------|------|------|-------------|--------|
| Edit 1 | test.py | :22-23 | `import socket`(:22) → `import subprocess`(:23) ✓ | 唯一 |
| Edit 2 | test.py | :58-60 | stall_all :58, stall_calls :59, calls :60 ✓ | 唯一 |
| Edit 3 | test.py | :81-83 | `# --- end stall ---` (:81), `if self.fail_500:` (:83) ✓ | 唯一 |
| Edit 4 | test.py | :194 | `stall_all: bool = False, stall_calls: tuple = ()) -> int:` (:194) ✓ | 唯一 |
| Edit 5 | test.py | :201-203 | attrs stall_all :201, stall_calls :202, bodies :203 ✓ | 唯一 |
| Edit 6 | test.py | :209 | `use_threading = stall_all or stall_calls` (:209) ✓ | 唯一 |
| Edit 7 | test.py | :234-237 | `return port, ...` (:234) + 空行(:235-236) + `def make_body_recording_upstream` (:237) ✓ | 唯一 |
| Edit 8 | test.py | :1748-1756 | 跨类边界，含 `class AdminIntegrationTest` :1756 ✓ | 唯一 |
| Edit 9 | test.py | :2881-2884 | `test_header_stall_retry_disabled` 尾 + `test_header_timeout_does_not_leak_into_body_phase` (:2884) ✓ | 唯一 |
| Edit P1 | proxy.py | :865-876 | except 元组 :865 → record_error_event :876 ✓ | 唯一 |
| Edit P2 | proxy.py | :171-173 | docstring `(socket.timeout, RemoteDisconnected) catch` ✓ | 唯一 |

**锚点漂移纠正**：spec 锚 :23 原文"import subprocess 后新增 import struct（字母序）"——实测 struct 字母序在 socket(:22) 与 subprocess(:23) 之间（`struc` < `subpr`），故 Edit 1 插在 :22/:23 之间保持字母序（纠正 spec 原文"后"字的松散表达）。其余锚点均精准对应。

### 5. 跨卡一致性

- **TDD 顺序**：卡 1（测试夹具+红）→ 卡 2（实现→绿），内部验证命令的 `exit code` 预期明确（卡 1 exit=1 预期红，卡 2 exit=0 绿） ✓
- **退化预案**：若白盒③ `assertIs` 行因平台非确定性失败，执行机械删除（仅保留 `assertRaises`）——卡 1 验证步骤已标注、卡 2 绿验证不受影响（①② spawn 用例已通过 stderr 断言覆盖 RST→retry 路径） ✓
- **stall 回归**：卡 2 绿验证命令行已明确列出 `test_header_stall_*` 三个用例必须 OK，`retry_reason=header-timeout` ← isinstance 顺序正确 ✓
- **baseline**：112 tests OK, 0 unexpected failures ✓