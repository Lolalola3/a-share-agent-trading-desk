# Architecture v5.1

## Components

1. **Session bootstrap**：SessionStart hook 注入幂等启动要求；工作日 cron 只作为兜底。登记可访问时复用唯一日任务，明确失效时才允许替换。
2. **Dynamic runtime**：`analysis_runtime_poll` 原子处理首次、60 分钟到期、用户请求、监控信号、收盘和收盘修订；15 分钟 lease 防止重复分析。
3. **Tencent packet**：持仓、候选、指数和已审计板块成分股批量取数；技术、相对强弱和板块代理在本地确定性计算。
4. **Monitoring**：Agent 每轮选择模板、股票、指标和阈值。crossing、cooldown 和 rearm 防止信号重复；信号只触发再分析。
5. **Deterministic state**：账户、T+1、现金/股份锁定、候选池、板块快照和审计文件不依赖对话记忆。
6. **Close gate**：必须依次完成新收盘分析、全天复核和次日展望，并以同一 `close_review` 写入 run 与 report；一致性失败时拒绝关闭。

## Contracts

- **Data contract**：盘中实时数据只认腾讯；失败即不可交易，不自动回退。
- **Sector contract**：成员来源同日、完整度≥95%、连续成功≥2；盘中覆盖不足则板块硬条件不可用。
- **Order contract**：建议不等于下单。真实建议必须先登记订单意图，用户反馈后才改变账户。
- **Timer contract**：每次完整分析完成后重置 60 分钟；收盘归档成功后 `next_analysis_at=null`。
- **Privacy contract**：公开代码与 `A_SHARE_DESK_HOME` 私有状态隔离。

## Failure behavior

- stale/invalid Tencent quote：观察，不生成精确交易建议。
- stale task registration：先验证任务；只有明确 not found 才替换。
- expired analysis lease：记录 expired 后允许下一轮重新认领。
- incomplete close review：拒绝 `reports_close_day` 或 `analysis_cycle_complete(close_session=true)`。
- user-requested close correction：`source=close_revision, force=true` 重新拉数据并重做三段式流程。
