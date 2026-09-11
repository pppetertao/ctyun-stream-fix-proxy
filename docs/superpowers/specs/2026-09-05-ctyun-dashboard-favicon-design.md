# ctyun-dashboard favicon + meta spec（simplified mode）

R31-EXEMPT: feat-not-bugfix

背景：dashboard 已有 `<title>CTYUN 剥行代理 · 运行台</title>`，但浏览器标签图标是默认空白（/favicon.ico 404）。用户要求补 ico 与标题配套元素。

## Goal

`GET /favicon.ico` 返回内嵌 220 字节 ICO（32×32 深石板底 + 三根琥珀 SSE 行、中根被删除线剥除——与页面"剥行流带"同一视觉语言）；HTML head 补 `<link rel="icon">`、`theme-color`（#0F141D）、`apple-mobile-web-app-title`（剥行代理，主屏幕添加用）；既有 `<title>` 不动。

## Files to Change

### 1. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.py`
- 新增 `import base64`；`_FAVICON_B64` 常量（4 行）+ 模块级 `FAVICON_ICO = base64.b64decode(...)`。
- `AdminHandler.do_GET` 的路径分支（`GET /` 判断处）加 `elif path == "/favicon.ico": self._send(200, "image/x-icon", FAVICON_ICO)`。
- `_DASHBOARD_SRC` head 区（`<meta name="viewport">` 后）加上述三行 meta/link。

### 2. 编辑 `/Users/peter/.local/bin/ctyun-stream-fix-proxy.test.py`
- `test_dashboard_html_full_page` 追加断言：html 含 `rel="icon"` 与 `theme-color`。
- 新增 `test_favicon_served`：GET /favicon.ico → 200、Content-Type image/x-icon、body 以 ICO magic `b"\x00\x00\x01\x00"` 开头、长度 ≈220。

## Acceptance Criteria
- ① 全量测试 `/usr/bin/python3 ctyun-stream-fix-proxy.test.py` 全绿（20 tests）exit 0。
- ② 部署后 `curl -s -o /dev/null -w "%{content_type}" http://127.0.0.1:7921/favicon.ico` = image/x-icon；浏览器标签出现琥珀剥行图标。
- ③ 归档 cmp 一致；7920 行为不变。

## Risks
- ICO 内嵌 PNG 格式（Vista+ 标准），老 IE 不支持——家庭局域网无此需求。
- base64 常量损坏 → base64.b64decode 抛 binascii.Error 在 import 期即炸，launchd 重启循环可见，测试先行覆盖。

## Exclusions
- 不做多尺寸 ICO / PNG 系 favicon / manifest；不改 admin 其他端点与 7920 代理行为。
