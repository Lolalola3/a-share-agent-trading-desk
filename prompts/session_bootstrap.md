# 当日交易任务启动协议 v5

目标：北京时间当日第一次进入 Codex 项目时，只创建或复用一个“A股交易台 YYYY-MM-DD”任务，并立即完成首次分析。

1. 先调用 `daily_session_get`。若已存在活动任务，先用 Codex 任务读取工具验证登记的 thread_id：可访问时禁止创建第二个任务；明确返回不存在时，才允许把当前任务以 `replace=true` 替换失效登记。
2. 若不存在且当前任务就是首次入口，将当前 thread_id/host_id 用 `daily_session_register` 登记。新交易日且账户仍是前一日时，在没有未反馈委托的前提下先执行 `trading_day_rollover`。
3. 创建当日唯一的 5 分钟聊天心跳自动化，并用 `daily_session_heartbeat_register` 保存 automation_id。心跳只调用 `analysis_runtime_poll`；返回 `skip` 时不分析、不发冗余消息，返回 `analyze` 时在本任务执行完整分析。
4. 首次调用 `analysis_runtime_poll(day, source="startup", force=true)`，获得 run_id 后按 `daily_session.md` 分析。
5. 不得创建或恢复 09:08、09:22 等固定节点。每日兜底启动任务也必须先做唯一性检查。

限制：Codex 应用未运行时，定时任务不会自行执行；SessionStart hook 只能注入启动要求，实际创建任务和分析由 Agent 完成。
