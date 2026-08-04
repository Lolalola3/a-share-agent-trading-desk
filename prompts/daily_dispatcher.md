# 每日任务调度协议 v2.1.0

后台节点只负责把本节点请求投递到当天唯一的 `A股交易台 YYYY-MM-DD` 任务，不在调度任务内分析行情或创建订单。

## 强制时间门禁

1. 开始后先取得北京时间的当前日期，不搜行情、不读取线程。
2. 立即调用 `dispatch_node_claim(day=北京时间今天, node=本任务节点)`。
3. 返回 `action=skip` 时，输出一行中文跳过原因，调用 `set_thread_archived(archived=true)` 归档当前后台调度任务后结束；不得调用 `task_session_get`、`list_threads`、`read_thread`、`create_thread` 或 `send_message_to_thread`，不得分析行情或写交易归档。
4. 只有返回 `action=execute` 才能继续。工具不可用、超时或返回异常时关闭式退出，不得绕过门禁创建任务。

门禁按计划表计算截至当前最近的到期节点，并以本地文件原子认领。例如12:13只允许11:25；13:00后只允许13:00。Codex恢复时即使补调多个历史cron，也只有一个节点能继续；重复节点同样跳过。

## 线程投递

1. 门禁通过后核验A股交易日；非交易日结束。
2. 调用 `task_session_get`。登记存在时直接向登记的 `thread_id` 和 `host_id` 投递，不先调用 `read_thread` 或 `list_threads`。
3. 只有明确返回 not found/deleted 才创建替代任务；超时或服务暂时失败不得重复创建。
4. 当日无登记时创建项目本地任务，标题为 `A股交易台 YYYY-MM-DD`，初始提示就是本节点执行请求，并立即登记。
5. 任一前序节点缺失时只执行已通过门禁的当前节点，不补发历史指令。
6. 线程工具返回运行中单元时必须继续等待，单次操作总等待上限90秒。
7. 每日任务完成后可按委托元数据归档来源调度任务，严禁归档每日交易任务自身。

节点执行请求必须要求读取 `prompts/global_policy.md`、`prompts/data_acquisition.md` 和 `prompts/daily_nodes.md`，按需使用外部行情、技术分析和新闻适配器，并执行指定时间节点。
