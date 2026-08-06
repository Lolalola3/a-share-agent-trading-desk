# 每日任务调度协议 v2.4.0

后台节点以同一个持久调度任务内的定时唤醒方式运行，只负责确定性地把当前有效节点投递到当天唯一的 `A股交易台 YYYY-MM-DD` 任务，不在调度任务内分析行情或创建订单。不得为每个节点创建独立 standalone 任务窗口。状态分为 `delivery`（目标任务已实际接收）和 `analysis`（分析已完成留档）；二者不得混用。

## 一、强制时间门禁

1. 第一项外部动作是取得北京时间日期并调用 `dispatch_node_claim(day=今天, node=本任务节点)`；门禁前不得搜行情或读取任务。
2. `action=skip` 时只报告跳过原因并结束，不创建每日任务，不补发历史节点。
3. 只有 `action=execute` 才继续。工具异常时关闭式退出。
4. 门禁只允许截至当前最近的到期节点。例如调度延迟到 09:28 时，09:08 唤醒只返回 `skip`，只有 09:22 继续；12:13 只允许 11:25，后续 13:00 仍可独立执行。
5. 认领使用 180 秒租约。同一节点租约内禁止并发；若租约过期且尚未产生投递记录，允许同一节点恢复执行，避免创建任务前的基础设施失败永久锁死当天节点。

## 二、取得一个可投递的每日任务

1. 核验 A 股交易日后调用 `task_session_get`。
2. 登记存在时，先对登记的 `thread_id` 调用 `set_thread_archived(archived=false)`，再用 `read_thread` 或 `wait_threads(timeoutMs=0)` 验证任务可访问，并保存返回的 cursor。解除归档后返回 `status=notLoaded` 表示可冷启动的正常状态，允许继续发送；只有明确的 not found/deleted 才表示目标不存在。禁止只相信本地登记中的 `status=active`。
3. 取消归档明确返回 not found/deleted 时才创建替代任务；其他异常不得重复创建。
4. 登记缺失或旧任务明确不存在时，当前有效节点必须立即执行以下首次启动流程，不得只报告 `status=missing`：
   - 创建项目本地任务，标题为 `A股交易台 YYYY-MM-DD`；初始提示只回复 `DAILY_TASK_READY`，不分析行情。
   - 等待初始化回合完成。
   - 初始化完成后显式调用 `set_thread_archived(archived=false)`，再验证任务可访问。
   - 最后调用 `task_session_register(..., replace=旧登记是否存在)`。未完成解归档和可访问性验证不得登记；登记成功后再进入 `node_delivery_prepare`。
   - 若调度任务缺少 `create_thread`、`read_thread`、`wait_threads` 或 `send_message_to_thread`，这是调度面配置错误；结束本次执行且不得创建投递记录。认领租约过期后允许恢复，不能把 `status=missing` 当作正常完成。
5. 每次发送确认或分析前都再次幂等调用 `set_thread_archived(archived=false)`。每日任务自身永不由调度器归档。

## 三、真实投递确认

1. 调用 `node_delivery_prepare` 并保存 `delivery_id`；初始状态必须是 `delivery=pending`、`analysis=pending`。
2. 记录目标任务当前 cursor，然后调用 `send_message_to_thread` 发送一个无工具短消息：
   `仅回复 DELIVERY_ACK <delivery_id>；不得调用工具或分析行情。`
3. `send_message_to_thread` 返回只表示消息已进入队列，不表示目标已回复。必须使用 `wait_threads` 携带发送前 cursor 等待目标新回合完成；单次等待不超过 60 秒，可重复一次。
4. 只有目标最终文本严格包含 `DELIVERY_ACK <delivery_id>`，调度器才调用 `node_delivery_confirm(..., transport_id="thread-message:<目标turn_id>")`。确认工具由调度器调用，不依赖每日任务拥有新增状态工具。
5. 目标不可访问、等待两次仍超时、目标回合失败或 ACK 不匹配时，调用 `node_delivery_fail` 写入明确原因并结束。不得永久保留 pending，不得进入分析。

## 四、独立分析完成

1. 投递确认后生成唯一 `run_id`，再次解归档每日任务，并发送完整节点分析请求。请求必须包含 day、node、delivery_id 和 run_id。
2. 目标任务读取 `prompts/global_policy.md`、`prompts/data_acquisition.md`、`prompts/daily_nodes.md`，读取确定性上下文；运行当前节点 `node-packet`，按节点要求刷新并形成建议。
3. 目标任务必须调用 `analysis_run_record(day, run_id, payload)`，但不负责调用 `node_analysis_complete`。
4. 调度器用 `wait_threads` 等待分析回合完成。单次等待不超过 60 秒；等待期间可报告进度，不以原 90 秒投递上限截断分析。
5. 目标回合完成后，调度器调用 `node_analysis_complete(status=completed, run_id=同一值)`；该工具会校验 `analysis_run_record` 已存在。目标失败、超时或未留档时登记 `status=failed`。
6. 最后调用 `node_execution_status_get`，分别报告 delivery 和 analysis。只有 `confirmed/completed` 才算节点成功。

## 五、节点分析边界

完整分析请求必须先运行：

```powershell
python -m trading_desk.cli node-packet --node "当前节点"
```

结构化行情、分时、持仓和候选以数据包为准；LLM 只补充公告与权威消息。腾讯主源合规时不调用备用报价；单一合规来源且 `tradeable=true` 可生成精确建议。严格执行 T+1、委托锁定、策略和节点时间窗，只生成待人工执行建议，绝不连接券商下单。
