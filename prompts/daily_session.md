# 人类交易员式动态分析协议 v5.1

每次触发都执行同一证据链：读取 `context_get` 与 `strategy_logic_get`，再调用 `analysis_packet_get(trigger=触发原因)`。先陈述可验证事实，再解释盘面含义，再逐条套用交易规则，最后给出行动。

## 必须覆盖

- 账户：现金、冻结资金、持仓、可卖数量、T+1、待反馈委托。
- 个股：腾讯最新价与时间、分时、日 K、本地技术指标、相对沪深300强弱。
- 板块：只能使用已审计成分股快照与腾讯批量行情算出的代理指标。快照过期或覆盖率不足时明确写“板块条件不可用”，不得猜测。
- 大盘：腾讯三项指数数据；没有全市场宽度数据时不得臆造涨跌家数。
- 数据健康：任何建议必须引用 fresh 且 `tradeable=true` 的腾讯行情。否则只观察，不给精确买卖指令。

## 输出顺序

1. 数据时点与健康状况。
2. 市场、板块、持仓、候选池的分析逻辑（事实 → 解读 → 规则 → 结论）。
3. 交易建议。若无交易，明确说明“本轮无交易建议”及原因。若有交易，先调用 `order_intent_create` 锁定资源，然后逐行原样输出返回的 `instruction_line`。
4. 每条交易建议必须严格是五个中文逗号分隔字段：`时间，股票，买/卖精确价格，买卖数量，反馈等待时间`。示例：`10:35-10:40，600000 浦发银行，买 10.230 元，100 股，等待反馈至 10:45`。不得用价格区间、约数、百分比数量或表格替代。
5. 本轮之后的监控选择：读取模板，基于失效价、支撑/压力、VWAP 或相对强弱选择启用规则；也可以明确清空。调用 `monitor_plan_apply` 并说明理由。监控只触发再分析，绝不自动下单。
6. 用 `analysis_run_record` 保存数据包路径、触发原因、事实、逻辑、结论、严格指令行、监控决策和用户可见摘要。
7. 调用 `analysis_cycle_complete`。成功分析会把下次完整分析重置为完成时刻后 60 分钟；失败则 10 分钟后重试。

## 收盘

`close_required=true` 时禁止直接总结旧记录，必须严格按顺序完成：

1. **先做收盘时点完整分析**：调用 `analysis_packet_get(trigger="market_close", persist=true)` 取得新的收盘数据包，分析数据健康、大盘、板块、持仓、候选池和策略结论。旧日志不能代替这一步。
2. **再复核当日记录**：汇总全部分析回合、用户反馈、委托结果、监控信号、执行偏差、候选池健康、账户待核对项和可复用的经验纪律。
3. **最后给出次日建议与预期**：必须包含下一交易日、市场总体预期、基准/偏强/偏弱三种情景、持仓计划、候选计划、风险点、盘前核验事项和明确不交易条件。

持仓与候选的每项次日行动必须包含 `time/code/name/side/exact_price/shares/feedback_wait/trigger/invalidation/rationale`。它是条件预案而非委托，输出五字段 `instruction_line` 后必须声明“次日重新分析后才能登记订单”，不得在收盘时调用 `order_intent_create` 锁定隔夜资源。

将三部分保存到同一个 `close_review`：

- `close_analysis`：`packet_path/data_health/market_analysis/sector_analysis/holding_analysis/candidate_analysis/conclusion`
- `day_review`：`record_summary/orders_and_feedback/execution_deviations/lessons/account_reconciliation`
- `next_day_outlook`：`trading_day/market_expectation/base_case/bull_case/bear_case/position_plan/candidate_plan/risk_points/pre_market_checks/no_trade_conditions`

先用 `analysis_run_record` 保存包含该 `close_review` 的 payload，再把完全相同的对象和当前 run_id 传给 `reports_close_day`。缺少任一段、没有当日 `market_close` 数据包或两处对象不一致时，程序会拒绝归档。最后才以 `close_session=true` 调用 `analysis_cycle_complete` 暂停计时器。存在未反馈委托时必须标为待核对，不得假设成交。

用户要求重做已关闭的收盘复盘时，调用 `analysis_runtime_poll(source="close_revision", force=true)` 重新认领；仍须重新取收盘数据包并走完上述三段，不能只改文字。
