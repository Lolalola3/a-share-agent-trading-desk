# Dynamic task and local delivery model

## One daily task

Each Beijing-date trading day has one user-visible task. The SessionStart hook only injects startup context; the agent verifies or registers the task and performs the analysis workflow.

Before 09:15 the runtime registers delivery state but does not fetch market data. From 09:15 to 09:30 it uses the opening-auction workflow. After that phase completes, the next analysis is scheduled for 09:30.

## Split timer and monitor workers

The desk does not create a five-minute chat heartbeat.

- The **timer worker** is a hidden one-shot process. It reads only local time and a cancellation token, performs no network polling, and sends one prompt when due.
- The **monitor worker** is a different hidden process with its own token and state. It polls active Tencent quote rules in Python every 30 seconds and wakes the task only after a real threshold crossing.
- Manual or signal-driven analysis invalidates both old tokens. `analysis_cycle_complete` starts fresh workers after the new record is safely written.
- No-signal checks are silent and never consume conversation context.

During continuous trading the normal interval is 60 minutes. Near the close, runtime scheduling clamps the next cycle to the close boundary so the close workflow is not missed.

## Close

When `close_required=true`, the task must:

1. persist a fresh `market_close` packet;
2. analyze the closing market, sectors, holdings and candidates;
3. review the day's runs, feedback, monitor signals, deviations and reconciliation items;
4. produce base, bullish and bearish next-session scenarios and conditional plans;
5. write the same structured close review to the run record and close reports;
6. call `analysis_cycle_complete(close_session=true)` to stop local delivery.

Next-session plans are conditional scenarios, not overnight orders.

## Runtime limitation

Local delivery requires the computer and Codex runtime to remain available. The project does not claim to execute analyses while the host is fully shut down.
