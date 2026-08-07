# 当日交易任务启动协议 v5.4

目标：北京时间当日第一次进入 Codex 项目时，只创建或复用一个“A股交易台 YYYY-MM-DD”任务；任何分析最早从09:15开始。

1. 先调用 `daily_session_get`。若已存在活动任务，先用 Codex 任务读取工具验证登记的 thread_id：可访问时禁止创建第二个任务；明确返回不存在时，才允许把当前任务以 `replace=true` 替换失效登记。
2. 若不存在且当前任务就是首次入口，将当前 thread_id/host_id 用 `daily_session_register` 登记。登记和每次轮询都会自动检查交易日滚动：无未反馈委托时自动滚动；存在未反馈委托时返回 `blocked`，不得使用昨日账户状态继续分析。
3. 禁止创建5分钟聊天心跳。首次调用 `analysis_runtime_poll(day, source="startup", force=true)`；运行时把纯一小时计时器与30秒行情监控拆成两个独立隐藏worker。计时worker只读取本地时间/令牌，绝不拉行情；监控worker只用Python进程内HTTP请求腾讯，只有阈值真实跨越才唤醒任务。两者普通检查都不产生对话。
4. 09:15以前即使 `force=true` 也必须返回 `skip`，禁止调用 `analysis_packet_get`；本地唤醒器自动等待到09:15。
5. 09:15-09:30返回 `analysis_mode=pre_market` 时，必须执行 `pre_market_session.md` 并使用 `include_intraday=false`；盘前周期完成后，运行时把下一次完整分析安排在09:30，而不是延后60分钟。
6. 09:30以后返回 `analysis_mode=intraday` 时才执行 `daily_session.md` 的盘中逻辑。
7. 不得创建或恢复 09:08、09:22 等固定节点。每日兜底启动任务也必须先做唯一性检查。

限制：本地唤醒器依赖本机保持运行、Codex CLI已登录且目标任务可恢复。SessionStart hook 只注入启动要求，实际创建任务和分析由 Agent 完成。
