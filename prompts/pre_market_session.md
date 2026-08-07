# 集合竞价盘前分析协议 v5.4

本协议只用于北京时间09:15-09:30。09:15以前不分析；09:30以后改用 `daily_session.md`。

## 数据边界

1. 读取 `context_get`、`strategy_logic_get`，确认运行时已自动完成当日交易日滚动；若 `rollover.status=blocked`，停止分析并要求先处理未反馈委托。
2. 调用 `analysis_packet_get(trigger="opening_auction", include_intraday=false, persist=true)`。腾讯分时此时必须显示 `not_expected`，不是 `offline`。
3. 只使用腾讯集合竞价指示报价及时间、昨收、前一交易日日K、持仓成本/可卖量和候选既定支撑压力。直接板块总体行情若能返回，只作前收盘背景，`hard_filter_available` 必须为 false。成交额、换手率、VWAP、量比、日内高低点和分钟相对强弱一律视为尚未形成。
4. 集合竞价价格是指示性事实，不把瞬时高开低开外推为全天趋势；没有全市场宽度时不得推断普涨普跌。

## 独立盘前逻辑

- 大盘：比较三项指数的竞价涨跌、前一日位置和日K趋势，只形成“高开/平开/低开及风险提示”。
- 板块：不得用盘前返回的总体行情推断当日板块强弱；不请求成分股，不计算竞价覆盖。所有候选买入仍等待09:30后的直接板块总体数据确认。
- 持仓：计算固定止损、止盈和T+1可卖数量。竞价触及阈值只列为09:30优先复核信号，不在盘前创建 `order_intent`。
- 候选：检查是否价格失效、竞价涨幅是否超过追涨上限及参考价偏离；不得使用量比、VWAP或反弹确认，不得给出买入指令。
- 监控：可启用持仓硬止损和候选失效价监控，只用于09:30后触发重新分析。

## 输出与留档

必须形成 `data_health/facts/interpretation/rules_applied/conclusion/monitor_decision`，按“数据健康 → 事实 → 解读 → 规则 → 结论 → 监控”完整展示。盘前结论必须写明“本轮无交易建议；09:30盘中重新分析后才能登记订单”。

调用 `analysis_run_record` 后，逐字完整输出其返回的 `user_visible_output`，不得改成一段摘要。再调用 `analysis_cycle_complete`；运行时会把下一次完整分析安排在09:30。
