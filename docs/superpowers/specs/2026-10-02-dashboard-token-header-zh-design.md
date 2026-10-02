# Dashboard Token 表头中文化设计（简化 spec）

R31-EXEMPT: feat-not-bugfix

[R31-S1] 纯字面量改动，无逻辑/接口变更；两表 `<th>` 字符串各唯一定义一次，JS 排序基于数值字段（`ctyun-stream-fix-proxy.py:4451-4453`、`ctyun-stream-fix-proxy.py:4833-4835`），不按 `<th>` 文本映射。

[R31-S2] 测试同步点：`ctyun-stream-fix-proxy.test.py:3350` 与 `ctyun-stream-fix-proxy.test.py:3352` 断言 `<th>cache tokens</th>` / `<th>reasoning tokens</th>`，改表头必须同步改断言否则红。

## Goal

将 dashboard "Token 用量" 两张表的英文 `<th>` 表头改为中文（纯文案，无逻辑变更），提升中文用户可读性。

## Files to Change

1. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:3754`（字符串常量 `_DASH_SECTIONS_STATIC` 内表 A 表头行）—
   改前：`<thead><tr><th>Key</th><th>请求</th><th>prompt</th><th>completion</th><th>cache</th><th>出流量</th><th>流式</th></tr></thead>`
   改后：`<thead><tr><th>密钥</th><th>请求</th><th>提示</th><th>补全</th><th>缓存</th><th>出流量</th><th>流式</th></tr></thead>`
   （`请求`/`出流量`/`流式` 已是中文，保持不动。）

2. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.py:3817`（字符串常量 `_DASH_SECTIONS_V2` 内表 B 表头行）—
   改前：`<thead><tr><th>日期</th><th>模型</th><th>prompt tokens</th><th>completion tokens</th><th>cache tokens</th><th>reasoning tokens</th></tr></thead>`
   改后：`<thead><tr><th>日期</th><th>模型</th><th>提示</th><th>补全</th><th>缓存</th><th>推理</th></tr></thead>`
   （卡片标题 `ctyun-stream-fix-proxy.py:3814` 已含"Token 用量"，列名去掉 `tokens` 后缀更干净，定稿不保留后缀。）

3. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:3350`（`test_v3_token_table_columns` 函数内第一条断言）—
   改前：`self.assertIn("<th>cache tokens</th>", v2, "token 按天×模型表缺 cache 列（T2）")`
   改后：`self.assertIn("<th>缓存</th>", v2, "token 按天×模型表缺缓存列（T2）")`

4. `/Users/peter/Documents/project/ctyun-stream-fix-proxy/ctyun-stream-fix-proxy.test.py:3352`（同函数第二条断言）—
   改前：`self.assertIn("<th>reasoning tokens</th>", v2, "token 按天×模型表缺 reasoning 列（T3）")`
   改后：`self.assertIn("<th>推理</th>", v2, "token 按天×模型表缺推理列（T3）")`

无新建/删除文件。

## Acceptance Criteria

- 表 A 最终 7 列表头（按序）：`密钥` `请求` `提示` `补全` `缓存` `出流量` `流式` — 源码中无 `<th>Key</th>`/`<th>prompt</th>`/`<th>completion</th>`/`<th>cache</th>` 残留。
- 表 B 最终 6 列表头（按序）：`日期` `模型` `提示` `补全` `缓存` `推理` — 源码中无 `<th>prompt tokens</th>`/`<th>completion tokens</th>`/`<th>cache tokens</th>`/`<th>reasoning tokens</th>` 残留。
- 测试断言与表头同步：`ctyun-stream-fix-proxy.test.py:3350` 断言 `<th>缓存</th>`、`ctyun-stream-fix-proxy.test.py:3352` 断言 `<th>推理</th>`。
- 验证命令：`python3 ctyun-stream-fix-proxy.test.py`（unittest `__main__` 入口见测试文件 8649-8655 行），全绿（exit code 0）。

## Risks

- 译名歧义：`提示`/`补全`/`缓存`/`推理` 为通用译法；若团队习惯 `输入`/`输出` 可后续 episode 再调，本次定稿以上述译名为准。
- 断言遗漏：仅 `test.py:3350` 与 `test.py:3352` 引用被改的 `<th>` 文本；已 Grep 全库确认无其他测试断言这些字符串，漏改风险低。
- 无排序/映射风险：JS 排序用 `tokens_prompt`/`tokens_completion` 数值字段（`ctyun-stream-fix-proxy.py:4451-4453` 表 B、`ctyun-stream-fix-proxy.py:4833-4835` 表 A），不读 `<th>` 文本；表头字符串全库各唯一定义一次，无跨文件接口。
- 无 HTML 结构变更：`<thead>`/`<tr>`/`<th>` 标签数量、顺序、嵌套均保持不变，tbody 渲染逻辑不受影响。

## Exclusions

- 不改任何 JS 渲染/排序逻辑（`_DASH_JS_V2` 等）。
- 不改卡片标题（`ctyun-stream-fix-proxy.py:3751`、`:3814`）与其他说明文案。
- 不动其他表（模型速度对比 `:3803`、最近请求 `:3781`、月末投影等）。
- 不做部署/webhook/systemd 变更（部署由 DELIVER 阶段主代理另行处理，本 episode 仅改源码与测试）。
- 不调整列顺序、不增删列、不改 `colspan`。
