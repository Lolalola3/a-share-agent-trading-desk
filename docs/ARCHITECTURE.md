# Architecture v5.4

## Components

1. **Session bootstrap**: a SessionStart hook injects an idempotent startup request. One Beijing-date task is registered and reused. Analysis never starts before 09:15.
2. **Phase routing**: 09:15-09:30 uses a dedicated opening-auction workflow without minute data or intraday entry logic. From 09:30 onward, `analysis_runtime_poll` routes intraday, manual, monitor-signal and close analyses.
3. **Split local delivery**: a hidden one-shot timer worker only waits for the next due time. A separate hidden monitor worker polls active rules every 30 seconds. Ordinary checks never write chat messages.
4. **Tencent market packet**: holdings, candidates, indices, minute bars and qfq daily bars use Tencent. Sector evidence comes from Tencent Shenwan level-2 aggregate quotes directly; the desk does not download all constituents or rebuild sector performance locally.
5. **Deterministic state**: account balances, T+1 availability, reservations, candidate pools, timers, monitor plans and audit records are persisted outside model memory.
6. **Close gate**: close handling first builds a fresh market-close packet, then reviews the full day and produces next-session base, bullish and bearish scenarios before archiving and stopping the timer.

## Contracts

- **Data contract**: stale or invalid Tencent quotes are non-tradeable and never silently fall back to another intraday provider.
- **Sector contract**: only complete, current-date Tencent sector aggregate fields may satisfy the sector hard condition. Failure freezes dependent entries.
- **Order contract**: advice is not execution. A valid suggestion must first reserve deterministic resources and use the strict five-field instruction format.
- **Timer contract**: manual and signal-driven analyses cancel old delivery tokens. Completion starts a fresh timer and, when rules exist, a separate monitor worker.
- **Privacy contract**: `state/`, `records/`, `journal/`, runtime logs and strategy reviews remain local and are excluded from the public repository.

## Failure behavior

- Tencent quote, minute-bar or sector failure: record the concrete error and withhold any affected precise suggestion.
- Stale task registration: verify the registered task; replace it only after an explicit not-found result.
- Expired analysis lease: record expiration and allow a later run to claim a new cycle.
- Incomplete close review: reject close archival and keep the session recoverable.
- User-requested close correction: claim a new `close_revision` cycle and fetch fresh close data instead of editing summary text only.
