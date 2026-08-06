# Architecture

## Design goal

构建一个面向高风险、时序敏感任务的领域型 Agent Harness：把不稳定、需要解释的数据研究交给 Agent，把资金、股份、T+1、风险规则、任务幂等性和审计交给确定性程序。核心程序不提供行情事实，也不自动连接券商。

Harness 的职责不是替代模型或行情服务，而是约束一次 Agent 运行如何获取上下文、调用工具、处理失败、形成建议、接收人工反馈并留下可复核记录。

## Layers

1. **Scheduler**：在预设节点唤起后台调度任务。
2. **Dispatcher**：8 个时间点作为同一个持久调度任务内的定时唤醒运行，不再各自产生 standalone 窗口；通过带 180 秒租约的 `dispatch_node_claim` 原子判断当前唯一可执行节点。首次有效节点创建并登记当日任务，随后校验其实际归档状态、等待带 ID 的短 ACK，再发起独立分析回合。
3. **Analysis Agent**：读取账户、候选池和策略，分类获取市场证据，形成操作或不操作结论。
4. **MCP core**：执行投递/分析双状态校验、T+1、风险计算、订单意图锁定、反馈更新和审计写入；只有分析留档后才能登记 `analysis=completed`。
5. **Private runtime**：保存账户、候选池、运行记录和日报；默认不进入版本控制。

## Harness contracts

- **Context contract**：每个节点显式读取账户、候选池和活动策略，不把聊天记忆作为状态事实来源。
- **Tool contract**：所有状态变更通过带 JSON Schema 的 MCP 工具完成；Agent 不能直接绕过 T+1、订单锁定和策略检查。
- **Scheduling contract**：`dispatch_node_claim` 只放行截至当前最近的到期节点；重复、过期或未来节点返回 skip。
- **Execution contract**：订单意图只是一条待人工执行的结构化建议，不会提交到券商；成交反馈是账户更新的唯一入口。
- **Audit contract**：每个节点无论是否产生交易建议，都必须写入数据健康、证据、策略检查和最终结论。

## Failure model

- 每个节点独立，前序节点失败不阻断下一节点。
- 本地任务登记不是 Codex 任务运行状态的事实来源；每次投递必须实际解归档并验证目标可访问。解除归档返回 `no archived rollout found` 表示目标已经处于活动状态；验证后的 `notLoaded` 是可接收新消息并冷启动的正常状态，二者都不等同于任务缺失。
- ACK 和分析状态都由调度器在观察到目标回合与留档后写入；目标任务不承担新增状态工具的兼容责任。
- 单个行情来源、脚本或网站不是系统前置条件。
- 实时报价必须在当前节点重新采集；历史失败不能替代当前备用源调用。
- 数据不完整时仍完成风险观察与归档，但不生成新买入或伪精确指令。
- 未反馈订单持续锁定资金或股份，防止后续节点基于错误余额继续建议。

## State ownership

`A_SHARE_DESK_HOME` 是运行状态根目录。核心模块只在该目录写入：

```text
state/      account, candidate pool, node claims, task sessions
records/    analysis evidence, intents, feedback, strategy reviews
journal/    daily, weekly and monthly reports
strategy/   optional runtime strategy override and review proposals
```

代码仓库只包含策略模板、Prompt和程序，不包含上述真实运行数据。
