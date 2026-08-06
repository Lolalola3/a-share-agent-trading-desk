# Why this is an Agent Harness

The model does not own cash, positions, T+1 availability, timers, quote freshness or audit truth. Those are deterministic state-machine responsibilities.

The Agent performs tasks that benefit from judgment:

- interpret market, sector, holding and candidate evidence;
- explain facts before conclusions;
- apply a fixed, versioned trading discipline;
- select monitor templates and thresholds;
- produce conditional next-day scenarios;
- identify uncertainty and missing evidence.

The harness enforces:

- one daily task and one resettable timer;
- Tencent-only intraday data with explicit stale handling;
- audited sector membership and local proxy computation;
- T+1, cash/share reservation and feedback reconciliation;
- strict five-field human-execution instructions;
- analysis run records and close-review consistency;
- no automatic brokerage execution.

This separation makes the workflow reviewable: the model may explain and recommend, but it cannot silently rewrite the ledger, invent data freshness or bypass the closing gate.
