# 独立节点启动与当日任务投递协议 v3.0.0

本提示由八个采用简单单时点 RRULE 的 **standalone 定时自动化** 分别触发。每次触发都是短生命周期节点任务；不得用复杂 `BYSETPOS` 合并八个时刻，也不得创建或复用跨日“持久调度器”。每天只复用当天唯一的 `A股交易台 YYYY-MM-DD` 分析任务，次日必须新建并登记新的分析任务。

## 一、打开 Codex 后的回溯门禁

1. 第一项外部动作是取得北京时间。按照本地设置中的八个节点，计算今天截至当前最近的到期节点 `effective_node`。
2. 直接调用 `dispatch_node_claim(day=北京时间今天, node=effective_node)`，不要使用自动化原计划时刻作为 node。这样，Codex 恢复后即使同时补跑多个错过的定时触发，也只允许其中一个执行最近节点。
3. `action=skip` 时立即结束，不创建分析任务、不投递、不补发更早节点。能取得当前节点任务 ID 时将这个一次性节点任务归档。
4. `action=execute` 才继续。原子认领和180秒租约负责去重；门禁前禁止搜索行情、创建任务或写投递状态。
5. 首节点尚未到时、日期不是北京时间今天或工具异常均关闭式退出。交易日核验失败时只记录非交易日并结束。

## 二、创建或复用当天唯一分析任务

1. 调用 `task_session_get(day=今天)`。
2. 登记存在时，先 `set_thread_archived(archived=false)`，再用 `read_thread` 或 `wait_threads(timeoutMs=0)` 验证。`notLoaded` 或“本来未归档”属于可继续状态；只有明确 not found/deleted 才算失效。
3. 登记缺失时，本节点必须创建项目本地任务，标题 `A股交易台 YYYY-MM-DD`，初始化提示为：
   `你是今天唯一的A股分析任务。确认 analysis_protocol_get 与 node_packet_get 可用；不得用 Shell 读取协议。本回合仅回复 DAILY_TASK_READY。后续节点都在此任务分析，任务不得自行归档。`
4. 等待初始化完成，解除归档并验证可访问后，调用 `task_session_register` 登记 thread_id/host_id。旧登记明确失效时才允许 `replace=true`。
5. 分析任务按日期隔离：严禁把昨天或其他日期的 thread_id 登记给今天。

## 三、投递确认与分析完成是两个状态

1. 调用 `node_delivery_prepare`，取得 `delivery_id`；此时必须是 `delivery=pending, analysis=pending`。
2. 记录目标 cursor，向当日任务发送：`仅回复 DELIVERY_ACK <delivery_id>，不得调用工具。`
3. 用携带旧 cursor 的 `wait_threads` 等待目标新回合。只有目标最终文本含完全匹配的 ACK，才调用 `node_delivery_confirm`。两次各不超过60秒；明确失败或仍无 ACK 时调用 `node_delivery_fail`，不得留下永久 pending。
4. ACK 确认后生成唯一 `run_id`，向同一当日任务发送完整请求，必须包含 day、effective_node、delivery_id、run_id，并要求它：
   - 先调用 `analysis_protocol_get` 一次取得四份协议和活动策略，禁止用 Shell 读本地文件；
   - 调用 `context_get(day=今天)` 一次取得账户和完整候选池，不再重复调用 `candidate_pool_get`；
   - 调用 `node_packet_get(node=effective_node, include_intraday=true, persist=true)`；
   - 完成人类交易员式分析；
   - 调用 `analysis_run_record` 保存结构化证据和用户文本；
   - 最后调用 `node_analysis_complete(status=completed, 同一 delivery_id/run_id)`。
5. 节点任务可等待分析任务完成以转述结果；分析仍运行时不得因固定90秒上限误报失败。只有目标明确失败/取消，才由节点调用 `node_analysis_complete(status=failed)`。
6. 最终用 `node_execution_status_get` 报告真实状态。delivery 与 analysis 绝不互相代替。

## 四、分析边界

结构化报价、指数、分时、日K和本地技术特征只来自 `node_packet_get`；协议只来自 `analysis_protocol_get`。禁止任何 Shell、禁止模型重复拉行情。腾讯主源合规即停止备用报价；任一合规来源 `tradeable=true` 即可给精确建议。LLM只补充少量公告和权威消息，并且绝不连接券商自动下单。
