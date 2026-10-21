# 协作任务：让 GitHub Actions 云端在每天 20:00 放座时可靠自动预约

## 背景

- 学校：杭州电子科技大学图书馆（`hdu.huitu.zhishulib.com`），每天 20:00 放开"后天"的座位。
- 目标：**云端（GitHub Actions）每天 19:20 左右自动触发 → 程序内部等到 20:00:00 整点 → 按 特等/一等/二等 优先级抢四楼座位。**
- 新仓库：`TaoTao-319/HDU-Library-CloudBooking`（本次新建，本地工作空间 `E:\26年10月份\26 10 21 图书馆脚本`）。
- 旧仓库 `TaoTao-319/HDU-Library-AutoBook` 和本机计划任务属于 codex 之前的成果，**不允许改动**。

## 已经验证可行的部分（不要重复怀疑）

1. **云端下单链路完全可用。** 2026-10-21 20:00，云端 run 抢到 `2026-10-23 13:00–22:00` 四楼 147 号（二等座），订单 22949345，查单核验通过。
2. **纯 HTTP 登录已复现**：`GET /User/Index/hduCASLogin` → `sso.hdu.edu.cn/login`，读隐藏字段 `login-croypto`（base64 的 16 字节 AES key）与 `login-page-flowkey`（execution），密码按 `base64(AES-128-ECB(PKCS7(pwd), key))` 加密，POST `https://sso.hdu.edu.cn/login`，表单字段 `username/type/_eventId/geolocation/execution/captcha_code/croypto/password/captcha_payload`。云端登录 3–8 秒，整个 job 22 秒。不再需要 Selenium/Chrome。
3. **整点触发精度** 0.06 毫秒；程序内置 5 轮重试 `20:00:00 / 20:00:25 / 20:01:00 / 20:02:00 / 20:04:00`；20:00–20:02 服务器高峰会读超时/限流，第 5 轮仍然抢到。

## 唯一未解决的问题：GitHub 原生 `schedule` 不投递

证据（全部来自 `gh api` / `gh run list`）：

- 新仓库 2026-10-21 16:17 创建，`main.yml` 配 `cron: '20 11 * * *'`（19:20 北京）、`'35 11'`、`'50 11'`。
- 观测到 **零次** `event=schedule` 运行（4 小时以上）。
- 另建 `scheduled-selftest.yml`，先 `cron: '*/5 * * * *'`（16:17 起，约 4 小时 ≈ 48 次预期），**一次都没有投递**。
- 20:09 把 cron 改成 `13,15,17,19 12 * * *`（20:13/15/17/19 北京），**4 个点全部没投递**。
- 排除了推送问题：同一 commit `7edfd09` 触发的 `on: push` 的 `Offline tests` 正常跑完，说明 GitHub 已接收 commit，只是 scheduler 不产生 `schedule` 事件。
- 旧仓库的历史线索：`cloud-schedule-test.yml` 在 02:05 UTC 创建，**08:00:40 UTC 才出现第一次也是唯一一次 `schedule` 运行**（约 6 小时后）。旧仓库 `main.yml` 的 `50 11` 今晚（19:50 北京）也没有触发。
- GitHub 状态页 `Actions: operational`。

## 候选方案与取舍

| 方案 | 优点 | 缺点 |
|---|---|---|
| A. 继续等原生 cron 生效（怀疑 ~6h 注册延迟） | 零成本、零外部依赖 | 4 小时+ 未生效；即便生效，投递时刻不受控，可能错过 20:00；旧仓库 6h 才第一次印证了不可靠 |
| B. 外部定时器（cron-job.org 等免费服务）每天 19:20 调 `POST /repos/{owner}/{repo}/actions/workflows/main.yml/dispatches` | 准点、可观测、失败可重试 | 需要 fine-grained PAT（仅该仓库 Actions:write）与外部账号；PAT 泄露风险 |
| C. 本机 Windows 计划任务每天 `gh workflow run` 触发云端 | 已有可用环境 | 依赖本机开机（违背"云端自动化"初衷） |
| D. 换一个调度平台（Cloudflare Workers cron / Vercel Cron 等） | 同样准点 | 同样需要外部账号；且需要 PAT |

## 需要 reviewer 回答的问题

1. 我们是否遗漏了某个 GitHub 原生机制，能让 `schedule` 恢复投递？（例如仓库可见性、默认分支、workflow 文件位置、`permissions`、账号风控、`gh` token 类型等）
2. 若确认原生 cron 不可靠，方案 B 的具体最小实现是什么？包括 PAT 类型/权限最小集、cron-job.org 的请求配置、失败告警方式。
3. 有没有比 B 更稳妥且不需要额外账号的办法？
4. 在等 cron 生效期间，如何用最少改动让"每晚都有人兜底"？
