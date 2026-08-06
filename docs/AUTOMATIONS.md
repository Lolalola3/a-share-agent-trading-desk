# Automation design

## Suggested schedule

| Time | Purpose | May create an instruction |
|---|---|---|
| 09:08 | Pre-market state, overnight announcements, rollover | No |
| 09:22 | Auction and sector confirmation | Yes |
| 10:30 | Opening trend and false-breakout check | Yes |
| 11:25 | Morning summary and lunch risk | Only before 11:30 |
| 13:00 | Midday announcements and afternoon open | Yes |
| 14:25 | Late-session trend confirmation | Yes |
| 14:50 | Overnight-risk decision | Yes |
| 15:05 | Close review and reports | No |

## One task per day

所有时间点都作为同一个持久调度任务内的定时唤醒运行，不能配置成 8 个各自新建窗口的 standalone 定时任务。每次唤醒只负责认领节点和投递，不自行分析；调度延迟造成多个时间点同时唤醒时，原子门禁只允许最近的到期节点继续。当天第一个成功触发的有效节点必须创建、验证并登记分析任务，后续节点继续投递到同一任务。若早盘节点缺失，最近到期节点仍能独立创建或找回当天任务。

调度结果使用两段独立状态。节点认领带 180 秒租约；创建投递记录前失败时，租约过期后允许恢复，不能永久锁死节点。调度器每次投递前显式取消每日任务归档并验证可访问性；`no archived rollout found` 表示任务本来就处于活动状态，应继续读取验证，`notLoaded` 表示允许继续投递的正常冷启动状态。目标短回合返回带 `delivery_id` 的 ACK 后，由调度器写入 `delivery.status=confirmed`。随后目标完成 `analysis_run_record`，再由调度器写入 `analysis.status=completed`。完整分析不受旧的 90 秒投递上限约束；明确失败写为 `failed`，不得永久停留在 `pending`。状态保存在 `state/node_executions/YYYY-MM-DD/HHMM.json`。

## Timing

不要假设模型能在触发后立即开始。生产配置应把后台调度器适当前移，为模型启动、数据采集和最终报价刷新留出空间。任何已经错过的执行窗口都只归档，不补发历史指令。

## Data contract

定时任务应显式要求读取三个协议：

```text
prompts/global_policy.md
prompts/data_acquisition.md
prompts/daily_nodes.md
```

节点数据包必须通过 MCP 工具 `node_packet_get` 获取，不能在无人值守任务中通过 Shell 启动 CLI，以免进入审批等待。

行情适配器应返回来源、采集时间、市场时间、成功率和失败摘要。腾讯主源返回完整、新鲜、字段合规的所需报价后应立即结束报价采集；只有主源失败、缺失、字段异常或过期时才调用备用源。单一合规来源即可进入精确指令流程。
