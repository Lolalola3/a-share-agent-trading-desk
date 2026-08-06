# Dynamic task and automation model

## One daily task

每个北京时间交易日只有一个用户可见任务。SessionStart hook 只注入启动上下文，不能自行调用 MCP 或创建任务；Agent 回合负责验证登记、创建/复用任务并执行首次分析。

工作日每日启动 cron 是兜底，不是固定分析节点。它先检查当日任务：可访问时静默退出；明确不存在时才替换失效登记。

## Heartbeat and timer

日任务创建唯一 5 分钟 heartbeat：

1. 调用 `analysis_runtime_poll(source="heartbeat")`。
2. `skip` 时静默，不拉取完整数据包。
3. `analyze` 时执行完整协议。
4. 完成后将下一次分析重置为 60 分钟后。

用户主动分析和监控信号也会重置同一个计时器。heartbeat automation id 保存于日任务状态；收盘完成后暂停。

## Close

`close_required=true` 后按顺序：

1. `analysis_packet_get(trigger="market_close", persist=true)`。
2. 完成收盘时点市场、板块、持仓和候选分析。
3. 复核全天 run、订单反馈、监控信号、偏差与账户核对项。
4. 生成下一交易日 base/bull/bear 三情景、持仓/候选条件计划、风险、盘前核验和不交易条件。
5. 同一 `close_review` 先写 `analysis_run_record`，再传给 `reports_close_day`。
6. `analysis_cycle_complete(close_session=true)` 暂停计时。

收盘计划不是订单，不得在收盘时锁定隔夜资金或股份。次日必须重新分析。

## Runtime limitation

Codex scheduled tasks 需要应用和电脑处于可运行状态。项目依靠 hook + daily fallback 提高恢复能力，但不会声称在应用完全关闭时执行分析。
