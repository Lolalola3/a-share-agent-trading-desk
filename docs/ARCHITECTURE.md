# Architecture

## Design goal

构建一个面向高风险、时序敏感任务的领域型 Agent Harness：把结构化行情采集和指标计算交给确定性程序，把消息增量、解释和判断交给 Agent；资金、股份、T+1、风险规则、任务幂等性和审计同样由程序维护。系统不自动连接券商。

Harness 的职责不是替代模型或行情服务，而是约束一次 Agent 运行如何获取上下文、调用工具、处理失败、形成建议、接收人工反馈并留下可复核记录。

## Layers

1. **Scheduler**：八个简单单时点 standalone 自动化分别创建短生命周期节点任务，不复用跨日调度窗口。
2. **Node gate**：每个节点按实际北京时间回溯最近到期时刻，通过带180秒租约的 `dispatch_node_claim` 原子去重；当天首个有效节点创建并登记当日分析任务。
3. **Market packet**：程序批量拉报价，并发拉分时/日K，使用日缓存和本地确定性指标构造数据包。
4. **Analysis Agent**：只补充少量公告/权威消息，解释数据包，形成操作或不操作结论。
5. **MCP core**：执行投递/分析双状态校验、T+1、风险计算、订单意图锁定、反馈更新和审计写入；只有分析留档后才能登记 `analysis=completed`。
6. **Private runtime**：保存账户、候选池、行情缓存、运行记录和日报；默认不进入版本控制。

## Harness contracts

- **Context contract**：每个节点显式读取账户、候选池和活动策略，不把聊天记忆作为状态事实来源。
- **Tool contract**：所有状态变更通过带 JSON Schema 的 MCP 工具完成；Agent 不能直接绕过 T+1、订单锁定和策略检查。
- **Scheduling contract**：`dispatch_node_claim` 只放行截至当前最近的到期节点；重复、过期或未来节点返回 skip。
- **Execution contract**：订单意图只是一条待人工执行的结构化建议，不会提交到券商；成交反馈是账户更新的唯一入口。
- **Audit contract**：每个节点无论是否产生交易建议，都必须写入数据健康、证据、策略检查和最终结论。

## Failure model

- 每个节点独立，前序节点失败不阻断下一节点。
- 本地任务登记不是 Codex 任务运行状态的事实来源；每次投递必须实际解归档并验证目标可访问。解除归档返回 `no archived rollout found` 表示目标已经处于活动状态；验证后的 `notLoaded` 是可接收新消息并冷启动的正常状态，二者都不等同于任务缺失。
- ACK 后由节点任务确认投递；当日分析任务留档后自行登记分析完成，二者互不替代。
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
