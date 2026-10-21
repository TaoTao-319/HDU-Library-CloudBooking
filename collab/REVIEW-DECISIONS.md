# 跨模型评审结论：采纳了什么、还要改什么

- 日期：2026-10-21
- 参与方：
  - **DSH**（deepseek-flash，本仓库维护者）
  - **DeepSeek API**（deepseek-v4-pro，独立实例，通过 `deepseek-collab` helper 往返一次）
  - **Codex**：已通过 `collab/TO-CODEX.md` 发出交接，**截至本文件写入时尚未回复**，
    因此本文件不声称"已与 Codex 达成共识"。
- 对话存档：
  `%LOCALAPPDATA%\CodexDeepSeek\sessions\20261021T122552Z-ce56345179e9\discussion.md`

---

## 一、可取的观点（已采纳）

### 1. 我的结论说得过头了（最重要的纠正）

我原话："GitHub 原生 scheduler 不投递 → 不可靠 → 弃用"。
证据实际只支持：**该仓库没有收到 `schedule` 事件**。
"GitHub 调度器全局坏掉"这个推论**证据不足**。

- **采纳**：把结论降级为"本仓库在观测窗口内零投递，原因未定位"。
- **影响**：不再以"平台坏了"为前提做决策，而是保留原生 cron 作为冗余。

### 2. 更简单的解释：改 workflow 文件会重置调度器注册

我 16:17 建仓、16:43 改、20:09 改 cron、20:21 再改、20:22 再加步骤。
评审指出：**高频修改 workflow 文件本身会重置调度器注册状态**，
于是"零投递"可能是自我实现的预言，而不是平台故障。

- **采纳**：从 2026-10-21 20:30 起 **24 小时内冻结 `.github/workflows/main.yml`
  与 `scheduled-selftest.yml`**，明天 19:20 再判定。
- **我的补充证据**：`*/5` 从 16:43 到 20:09 稳定了 3.4 小时仍零投递，
  说明若"重置"为真，冷却期要 >3.4h，与旧仓库约 6h 才首次投递吻合。

### 3. 外部触发用 `workflow_dispatch`，不用 `repository_dispatch`

理由（采纳）：
- `repository_dispatch` 在 workflow 匹配失败时 **API 仍返回 204**，静默失败、极难排障；
- `workflow_dispatch` 的 run 在 Actions 页面明确显示 `event: workflow_dispatch`；
- 已有 `mode` input 可直接透传。

### 4. PAT 最小权限 + 短有效期

- fine-grained token，仅授权 `HDU-Library-CloudBooking`；
- `Actions: read and write`（`dispatches` 端点唯一必需）；
- 有效期 30 天，到期前轮换；
- **不**给 Secrets / Administration。

> 评审提到 `Contents: read` "因为 checkout 需要" —— 这点我**修正**了：
> checkout 用的是 workflow 内的 `GITHUB_TOKEN`，与这个 PAT 无关，
> 所以 `Contents: read` 并非必需。已写进 `docs/EXTERNAL-TRIGGER.md`。

### 5. 外部触发器只要一个，冗余放在程序内部

评审主张：单个 19:20 外部触发足够，真正的容错来自程序内 5 轮重试
（20:00 / 20:00:25 / 20:01:00 / 20:02:00 / 20:04:00），
双外部触发会因 `concurrency` 排队而互相拖延。

- **采纳**：外部只配 1 个 triggers；原生 3 个 cron 保留为"有则更好"的冗余。

### 6. PAT 可以不放第三方（可选加固）

用 **Cloudflare Worker** 中转：PAT 放 Worker 加密变量，cron-job.org 只拿
Worker URL + 一个 `x-trigger-key`。Worker 免费、邮箱注册即可，还可加时间窗校验。

- **采纳**为可选方案，代码片段已写入 `docs/EXTERNAL-TRIGGER.md`。

### 7. 触发地址用文件名 `main.yml` 的隐忧

用文件名触发时，**一旦重命名 workflow 文件就会静默失效**。

- **采纳**为注意事项：`main.yml` 文件名保持稳定，或改用数字 workflow id。

---

## 二、需要改进的地方（认可，但尚未完成）

| # | 问题 | 现状 | 改进方向 | 优先级 | 状态 |
|---|---|---|---|---|---|
| 1 | 原生 cron 零投递的**根因未定位** | 观测被"边测边改"污染，缺对照实验 | 冻结 24h 后再判；如仍无投递，另开一个无关仓库放 15 分钟 cron 做对照 | P1 | 进行中（冻结期） |
| 2 | 20:00 高峰期**服务器读超时** | 第 2~4 轮全部 `ReadTimeout`，拖到 20:04 才成交，只拿到二等座 | 预热提前并做两次；booking/search 读超时 15s → 6s 快速换座 | P1 | **已落地** |
| 3 | 第 1 轮"窗口未开 + 限流"撞在一起 | 20:00:00.2 被拒，0.4s 重试即被"请求太频繁" | 整点退避 0.4/0.8/1.6 → **1/2/3 秒**；限流仍优先短路整轮（不加重限流） | P2 | **已落地** |
| 4 | **可观测性不足** | 每轮结果只在日志里，失败要翻 artifact | 每轮结果写入 `$GITHUB_STEP_SUMMARY` | P2 | **已落地** |
| 5 | 外部触发链路**未端到端验证** | 还没有 PAT 与 cron-job.org 账号 | Test run 必须返回 **204** 且 Actions 出现 `workflow_dispatch` 运行 | P0（阻塞） | 等待用户 |
| 6 | 抢到后**没有签到提醒** | 只输出订单信息 | summary 里给出签到时间窗（开始前 30 分钟 ~ 开始后 30 分钟） | P3 | **已落地** |
| 7 | `scheduled-selftest.yml` 每 10 分钟探测 | 有自停用保护，但仍在探测期 | 首次定时投递出现即自动停用（已实现），之后删除该文件 | P3 | 已落地（探测中） |

---

## 三、我保留的分歧（不采纳，理由在此）

1. **不加 19:45 硬性时间窗守卫。**
   评审建议 job 在 19:45 之外直接 `exit 0`，避免延迟触发"半路启动"。
   但 `main.py` 的 `run_daily` 已经有 **20:15 cutoff** 和「晚启动立即补跑」分支：
   一个被延迟到 20:05 的触发仍然应该抢（那时二等座通常还有位）。
   硬性 19:45 退出只会降低成功率；浪费上界已被 20:15 兜住。

2. **不删除原生 cron。**
   即使准点性靠外部触发保证，原生 cron 在"外部服务挂了"时仍是唯一免费后路。

3. **不把"与 Codex 达成共识"写进结论。**
   Codex 尚未回复。本文件只代表 DSH ↔ DeepSeek API 这一轮。

---

## 四、代码落地清单（已实际修改，不是只写文档）

| 改动 | 文件 | 具体内容 |
|---|---|---|
| 高峰快速失败 | `main.py` `request()` | `bookSeats` / `searchSeats` 在读超时窗口内由 `min(15, …)` 改为 `min(6, …)` 秒 |
| 整点退避 | `main.py` `book_slot()` | 窗口未开的退避 `0.4/0.8/1.6s` → **`1.0/2.0/3.0s`**；限流（频繁/人数过多/稍后）仍**优先短路整轮**，避免加重限流 |
| 放号退避区 | `main.py` `reserve_day()` | `opening_deadline` 由 `target+4s` 扩到 `target+8s`，容纳 1+2+3 秒三段退避 |
| 双次预热 | `main.py` `reserve_day()` | 由"整点前 10 秒预热一次"改为 **整点前 30 秒 + 整点前 3 秒各一次**；30 秒那次失败还有时间重新登录 |
| 每轮可观测 | `main.py` `run_daily()` | 每轮开始/失败/跳过/无位都写入 `$GITHUB_STEP_SUMMARY`；收尾写"五轮结束" |
| 签到提醒 | `main.py` `reserve_day()` | 成功摘要增加**签到窗口**（开始前 30 分钟 ~ 开始后 30 分钟） |
| 回归测试 | `test_main.py` | 更新预热事件序列与退避断言；新增 `test_run_summary_reports_order_and_sign_in_window` 校验摘要含订单与签到窗口 |

验证：`python -m unittest test_main test_seat_plan` → **42 项通过**；
本机 `main.py --check` 真实登录+只读校验通过。

## 五、下一步行动项

1. **P0（等用户）**：创建 fine-grained PAT（Actions: write，30 天）+ cron-job.org 账号；
   按 `docs/EXTERNAL-TRIGGER.md` 配置，Test run 需返回 204。
2. **P0（DSH）**：用户配置完后，核对 Actions 是否出现 `workflow_dispatch` 运行，
   并确认那次运行是"预约成功"还是"已有覆盖订单"。
3. **P1（DSH）**：24h 内不动 workflow 文件；明天 19:20 观察是否出现 `event=schedule`。
4. **P2（DSH）**：下一轮修改 `main.py`，落地第 2、3、4、6 项改进。
5. **等待 Codex** 对 `collab/TO-CODEX.md` 的回复；若有修改建议，写入
   `collab/TO-DSH.md`，由 DSH 串行落盘，避免并发写冲突。
