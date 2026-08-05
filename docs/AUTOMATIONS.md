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

每个定时器只负责认领节点和投递，不自行分析。当天第一个成功触发的节点创建并登记分析任务；后续节点继续投递到同一任务。若早盘节点缺失，最近到期节点仍能独立创建或找回当天任务。

调度结果使用两段独立状态。调度器每次投递前显式取消每日任务归档并验证可访问性；解归档后的 `notLoaded` 是允许继续投递的正常冷启动状态。目标短回合返回带 `delivery_id` 的 ACK 后，由调度器写入 `delivery.status=confirmed`。随后目标完成 `analysis_run_record`，再由调度器写入 `analysis.status=completed`。完整分析不受旧的 90 秒投递上限约束；明确失败写为 `failed`，不得永久停留在 `pending`。状态保存在 `state/node_executions/YYYY-MM-DD/HHMM.json`。

## Timing

不要假设模型能在触发后立即开始。生产配置应把后台调度器适当前移，为模型启动、数据采集和最终报价刷新留出空间。任何已经错过的执行窗口都只归档，不补发历史指令。

## Data contract

定时任务应显式要求读取三个协议：

```text
prompts/global_policy.md
prompts/data_acquisition.md
prompts/daily_nodes.md
```

行情适配器应返回来源、采集时间、市场时间、成功率和失败摘要。腾讯主源返回完整、新鲜、字段合规的所需报价后应立即结束报价采集；只有主源失败、缺失、字段异常或过期时才调用备用源。单一合规来源即可进入精确指令流程。
