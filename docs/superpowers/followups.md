# Follow-ups（跨 episode 独立问题）

ctyun-stream-fix-proxy.py tpm_admit est>limit 硬拒路径（TPM 限流 episode 引入） | est>TPM_LIMIT 单请求直接 429（ratio 0.25 下 body>~440KB）；2026-09-30 20:39 起 glm-5.3-oc 7 次真实硬拒，客户端疑似超大上下文重试循环 | 设计权衡：窗口空闲时放行超大单请求（spec 改动小 episode）或调 CTYUN_TPM_LIMIT/ratio，需用户拍板

ctyun-stream-fix-proxy.py _tpm_rejected/tpm_snapshot（TPM 限流 episode 引入） | est>limit 硬拒发生在建桶之前，/api/tpm_stats 的 rejected 计数不增长，观测只能 grep 日志 result=tpm-queue-full | 拒绝路径先 touch 桶再计数，纯观测小修，可随下个 TPM episode 顺带

ctyun-stream-fix-proxy.py TPM_LIMIT 常量（TPM 限流 episode 引入） | 110k 预算对 kimi-k3-oc 形同虚设：2026-09-30 19:08-19:11 kimi 本地 used 仅 ~31k 即遭上游 200 包 TPM 错误（body-err 观测抓到 4 次）；且 0.25 ratio 对 glm 内容低估（settle 后 used 冲至 119470>110k，存在超装窗口） | per-model（或 per-key×model）预算/比率：kimi ~30k、deepseek/glm 110k；新 episode 处理

