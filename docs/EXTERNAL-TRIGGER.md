# 外部定时触发（保底方案）

原生 `schedule` 若长期不投递，用外部定时器每天调用 GitHub API 的
`workflow_dispatch`。这条路不依赖 GitHub 的调度器，准点由外部服务保证。

## 0. 两条路，已在跑的是第 1 条

| 路径 | 是否需要新账号 | 是否已落地 | 对电脑的要求 |
|---|---|---|---|
| **1. 本机 Windows 计划任务**（用已登录的 `gh` 触发云端） | 不需要 | ✅ **已完成并验证** | 每天 19:20 电脑需开机联网 |
| 2. cron-job.org + fine-grained PAT（真正脱离电脑） | 需要（网页登录创建） | ⏳ 待用户创建 | 无 |

### 路径 1 的实现（已生效）

- 脚本：[`scripts/Trigger-Cloud.ps1`](../scripts/Trigger-Cloud.ps1)
- 计划任务名：`HDU-Cloud-Trigger-1920`
- 触发时间：每天 **19:20**（本机时区 China Standard Time）
- 行为：调用 `gh workflow run main.yml --repo TaoTao-319/HDU-Library-CloudBooking -f mode=book`，
  失败自动重试（最多 8 分钟），日志写 `.local/trigger-cloud.log`
- 守卫：`book` 模式若在 **20:14** 之后才被启动（例如电脑开机很晚），直接跳过并退出 0，
  不产生无意义的红色运行
- 任务设置：`StartWhenAvailable`、`WakeToRun`、单实例 `IgnoreNew`、超时 10 分钟

验证记录：

| 验证项 | 结果 |
|---|---|
| 脚本 `-Mode check` 直接调用 | exit 0，日志 `attempt 1 dispatched` |
| `Start-ScheduledTask` 真实拉起任务 | `LastTaskResult = 0`，派发 `mode=book` |
| 云端收到的 `workflow_dispatch` 运行 | run `39601467954`（check）登录通过 + 只读检查完成 |
| 20:41 的 `book` 派发 | 被守卫跳过，exit 0（不再产生红叉） |

日常维护：`Get-ScheduledTaskInfo -TaskName HDU-Cloud-Trigger-1920` 看上次结果；
`Unregister-ScheduledTask -TaskName HDU-Cloud-Trigger-1920 -Confirm:$false` 可移除。

---

## 1. 创建 fine-grained PAT（只有你能做）

1. GitHub → 右上头像 → Settings → Developer settings →
   Personal access tokens → **Fine-grained tokens** → Generate new token。
2. Resource owner: `TaoTao-319`。
3. Repository access: **Only select repositories** → 只勾
   `HDU-Library-CloudBooking`。
4. Permissions（只给必需的）：
   - `Actions`: **Read and write** —— `workflow_dispatch` 端点唯一必需的写权限；
   - `Metadata`: Read（自动授予，不可取消）；
   - `Contents: Read` **不是必需的**（workflow 里的 checkout 用的是
     `GITHUB_TOKEN`，不是这个 PAT），加上也只在"读取代码"层面，风险低，可留可去；
   - **不要**给 Secrets、Administration 或任何仓库写权限。
5. Expiration：**30 天**（到期前换新，避免长期泄露）。
6. 生成后立刻复制，**只粘贴到 cron-job.org**，不要发到聊天、不要写进仓库。

## 2. 在 cron-job.org 建任务

1. 注册免费账号（邮箱即可，无需手机号）。
2. Create cronjob：
   - Title: `HDU seat booking 19:20`
   - URL:
     ```
     https://api.github.com/repos/TaoTao-319/HDU-Library-CloudBooking/actions/workflows/main.yml/dispatches
     ```
   - Schedule: **每天 19:20**，时区选 `Asia/Shanghai`（或 UTC+8）。
     **不要**选 UTC 再自己换算。
   - Request method: **POST**
   - Headers:
     ```
     Authorization: Bearer <你的 fine-grained PAT>
     Accept: application/vnd.github+json
     X-GitHub-Api-Version: 2022-11-28
     Content-Type: application/json
     ```
   - Request body: `{"ref":"main"}`
   - 开启 `Save responses` 与失败重试（若免费计划支持）。
3. 保存后点 **Test run**：
   - 预期 GitHub 返回 **HTTP 204 No Content**（成功）；
   - 到仓库 Actions 页面应立刻出现一条 `workflow_dispatch` 运行。
   - 若返回 401 → PAT 无效/过期；403 → 权限不足；404 → 仓库名或文件名写错。

## 3. 为什么用 `workflow_dispatch` 而不是 `repository_dispatch`

| | `workflow_dispatch` | `repository_dispatch` |
|---|---|---|
| 目标 | 直接指向某个 workflow 文件 | 向仓库推一个 `event_type` |
| 失败可见性 | 参数错/文件错会报错 | **匹配失败也返回 204，静默失败** |
| 额外改动 | 无（workflow 已支持） | 要在 `on:` 里加 `repository_dispatch` |
| 输入透传 | 可直接传 `inputs.mode` | 要走 `client_payload` |

## 4. 触发方式与并发

`main.yml` 的 `concurrency.group = hdu-seat-booking`、
`cancel-in-progress: false`。多个触发不会互相取消，只会**排队串行**：

- 19:20 的外部触发进入 job，登录后等到 20:00:00 提交；
- 若同一时刻还有一次触发，它会排队，等前一个结束后再跑；
- 后到的那次会先查单：已有覆盖订单 → 直接确认并成功退出，不会重复下单。

## 5. 安全：让 PAT 不落第三方（可选，更稳）

若不想把 PAT 交给 cron-job.org，用 Cloudflare Worker（免费、邮箱注册即可）中转：

```js
// Cloudflare Worker
export default {
  async fetch(request, env) {
    if (request.headers.get('x-trigger-key') !== env.TRIGGER_KEY) {
      return new Response('forbidden', { status: 403 });
    }
    return fetch(
      'https://api.github.com/repos/TaoTao-319/HDU-Library-CloudBooking/actions/workflows/main.yml/dispatches',
      {
        method: 'POST',
        headers: {
          Authorization: `Bearer ${env.GH_PAT}`,
          Accept: 'application/vnd.github+json',
          'X-GitHub-Api-Version': '2022-11-28',
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ ref: 'main' }),
      }
    );
  },
};
```

- `GH_PAT` 与 `TRIGGER_KEY` 放 Worker 的加密环境变量；
- cron-job.org 只拿到 Worker URL + `x-trigger-key`，**看不到 PAT**；
- Worker 里可再加时间窗校验（只在 19:15–19:25 放行），进一步降低被滥用风险。

## 6. 校验清单

- [ ] cron-job.org 的 Test run 返回 204，且 Actions 出现 `workflow_dispatch` run
- [ ] 该 run 的结果是"预约成功"或"已有覆盖订单"，不是失败
- [ ] 原生长 cron **保持不动**，作为冗余（24 小时内不要改 `main.yml`）
- [ ] PAT 到期日写进日历，提前 3 天更换
