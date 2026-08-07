# Why this is an Agent Harness

The model does not own cash, positions, T+1 availability, timers, quote freshness or audit truth. Those are deterministic state-machine responsibilities.

The agent performs work that benefits from judgment:

- interpret market, direct sector, holding and candidate evidence;
- explain facts before conclusions;
- apply a fixed, versioned trading discipline;
- select monitor templates, symbols, metrics and thresholds;
- produce conditional next-session scenarios;
- identify uncertainty and missing evidence.

The harness enforces:

- one daily task and a 09:15 earliest-analysis gate;
- a separate opening-auction workflow before 09:30;
- a resettable hidden one-hour timer and an independent hidden 30-second signal monitor;
- Tencent-only intraday data with explicit stale handling;
- Tencent direct sector aggregate data without constituent-wide local reconstruction;
- T+1, cash/share reservation and feedback reconciliation;
- strict five-field human-execution instructions;
- analysis run records and close-review consistency;
- no automatic brokerage execution.

This separation makes the workflow reviewable: the model may explain and recommend, but it cannot silently rewrite the ledger, invent freshness or bypass the closing gate.
