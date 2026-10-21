# 给 Codex 的交接：云端自动预约 · 当前状态与分工

> 本文件是 DSH（deepseek-flash）写给 Codex 的共享交接。协作记录见
> `collab/briefing.md`、DeepSeek 会话
> `%LOCALAPPDATA%/CodexDeepSeek/sessions/20261021T122552Z-ce56345179e9/discussion.md`。

## 一、已经完成且已验证，不要再重做

1. **纯 HTTP 统一身份认证**（替代 Selenium）：
   `GET /User/Index/hduCASLogin` → `sso.hdu.edu.cn/login` → 取 `login-croypto` /
   `login-page-flowkey` → `base64(AES-128-ECB(PKCS7(pwd), key))` →
   `POST https://sso.hdu.edu.cn/login`。云端 3–8 秒，整个 job 22 秒。
2. **真实抢座成功**：2026-10-21 20:00 云端 run 拿到 `2026-10-23 13:00–22:00`
   四楼 147 号（二等座），订单 `22949345`，查单核验通过。
3. 离线回归 41 项通过；`.github/workflows/main.yml` 定时 19:20/19:35/19:50。

## 二、唯一未解决：GitHub 原生 `schedule` 零投递

事实：新仓库 16:17 创建后，`*/5` 与 `13,15,17,19 12 * * *` 全部零投递；
同一 commit 的 `on: push` 正常工作；GitHub 状态页 Actions operational。

DeepSeek 评审指出我**证据链的缺口**：这只能证明"该仓库没收到 schedule 事件"，
不能证明"GitHub scheduler 不可靠"。它给出一个更简单的解释——
**频繁修改 workflow 文件会重置调度器注册**，观察期内可能一直在重置。
我认可这个质疑，"原生 cron 坏了"这个结论我说得太满。

## 三、DeepSeek 与我一致同意的方案

- 外部触发用 **`workflow_dispatch`**（不用 `repository_dispatch`：后者在匹配失败时
  API 仍返回 204，静默失败，难排障）。
- 定时器用 **cron-job.org**，每天 19:20 北京（11:20 UTC）POST
  `/repos/TaoTao-319/HDU-Library-CloudBooking/actions/workflows/main.yml/dispatches`
  body `{"ref":"main"}`，Header `Authorization: Bearer <PAT>`。
- PAT 用 fine-grained：`Actions: read and write` + `Contents: read` +
  `Metadata: read`（自动），有效期 30–60 天，只授权本仓库。
- **原生 cron 保留作为冗余，不要删。**

## 四、我不采纳的两点（保留分歧，理由在此）

1. **不加 19:45 硬性时间窗守卫。** 评审建议 job 在 19:45 之外直接 `exit 0`。
   但 `main.py` 的 `run_daily` 已有 **20:15 cutoff** 和「晚启动立即补跑」分支：
   一次被延迟到 20:05 的触发仍然应该抢（那时二等座还有位），硬性 19:45 退出
   只会减少成功率。浪费上界已被 20:15 兜住。
2. **不全盘否定原生 cron。** 但它只能当"有则更好"的冗余；准点保证必须来自外部触发。

## 五、分工（避免同时改同一文件）

- **DSH 负责**：`main.py`、`.github/workflows/*`、`tests`、`docs/`。
- **Codex 负责**：审阅本文件与 DeepSeek 记录；对方案提出反驳或改进；
  若要做修改，**先写进 `collab/TO-DSH.md` 说明要改哪个文件、改成什么**，
  由 DSH 串行落盘，避免两边同时写导致冲突。
- **共同约束**：从 2026-10-21 20:30 起 24 小时内**不要再改
  `.github/workflows/main.yml`**，让调度器注册稳定下来；明天 19:20 观察是否出现
  `event=schedule`。

## 六、需要用户提供（阻塞外部触发）

1. 一个 fine-grained PAT（仅本仓库，Actions: write），**不要**发到聊天或仓库里，
   只填到 cron-job.org。
2. 一个 cron-job.org 免费账号（邮箱即可，无需手机号）。
3. 可选：若要 PAT 不落第三方，可用 Cloudflare Worker 中转（见
   `docs/EXTERNAL-TRIGGER.md`）。
