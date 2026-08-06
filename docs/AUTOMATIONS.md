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

## Standalone node runs, one analysis task per day

正式配置是八个采用简单单时点 RRULE 的 standalone 自动化。每次触发创建一个短生命周期节点任务；不存在跨日复用的“持久调度器”。不使用 `BYSETPOS` 把八个时刻压入一个复杂规则，因为当前 Codex 运行时可能保存配置却不生成预期 occurrence。节点按实际北京时间计算最近到期时刻，再调用 `dispatch_node_claim`，所以 Codex 恢复后即使多个错过的触发同时补跑，也只有一个任务能认领最近节点，其余立即跳过。

当天首次成功认领的节点读取 `task_session_get`。登记缺失时，它创建并登记 `A股交易台 YYYY-MM-DD`；后续节点解除归档、验证并继续向同一任务发送。登记按日期存储，次日一定创建新的分析任务，不能复用昨天的窗口。

投递和分析是两个独立状态：带 ID 的短 ACK 后写入 `delivery=confirmed`；当日分析任务取得数据包、留档并调用完成工具后才写入 `analysis=completed`。分析不受投递 ACK 的60秒单次等待限制，明确失败才写 `failed`，不得永久停在 `pending`。

## Catch-up limitation

应用恢复后是否补跑错过的 standalone 触发由 Codex 自动化运行时决定；项目没有伪造“应用启动事件”。本项目保证的是：只要运行时启动任意一个当天已到期触发，原子门禁就会选择截至当时最近的节点并去重。任何已错过的交易执行窗口只做分析归档，不补发历史订单。

## Data contract

节点通过 MCP `node_packet_get` 获取程序化数据包，禁止无人值守任务用 Shell 启动 CLI。腾讯主源合规即停止备用报价；日K按日缓存，分时按节点刷新，本地生成技术与相对强弱特征。完整契约见 [TRADER_DATA_MATRIX.md](TRADER_DATA_MATRIX.md)。
