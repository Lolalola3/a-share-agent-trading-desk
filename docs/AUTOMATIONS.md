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

每个定时器只负责认领节点和投递，不自行分析。当天第一个成功触发的节点创建分析任务并登记；后续节点直接投递到同一任务。若早盘节点缺失，最近到期节点仍能独立创建或找回当天任务。

## Timing

不要假设模型能在触发后立即开始。生产配置应把后台调度器适当前移，为模型启动、数据采集和最终报价刷新留出空间。任何已经错过的执行窗口都只归档，不补发历史指令。

## Data contract

定时任务应显式要求读取三个协议：

```text
prompts/global_policy.md
prompts/data_acquisition.md
prompts/daily_nodes.md
```

行情适配器应返回来源、采集时间、市场时间、成功率和失败摘要。双源核验允许配置最小价格容差，避免把相邻秒级的一档波动误判为来源冲突。
