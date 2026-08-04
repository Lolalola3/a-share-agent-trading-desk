# Architecture

## Design goal

把不稳定、需要解释的数据研究交给 Agent，把资金、股份、T+1、风险规则和审计交给确定性程序。核心程序不提供行情事实，也不自动连接券商。

## Layers

1. **Scheduler**：在预设节点唤起后台调度任务。
2. **Dispatcher**：通过 `dispatch_node_claim` 原子判断当前唯一可执行节点，并投递到当天唯一分析任务。
3. **Analysis Agent**：读取账户、候选池和策略，分类获取市场证据，形成操作或不操作结论。
4. **MCP core**：执行状态校验、T+1、风险计算、订单意图锁定、反馈更新和审计写入。
5. **Private runtime**：保存账户、候选池、运行记录和日报；默认不进入版本控制。

## Failure model

- 每个节点独立，前序节点失败不阻断下一节点。
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
