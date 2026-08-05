# Agent Harness Positioning

## Scope

This repository is a domain-specific Agent Harness for A-share analysis workflows. It provides the execution substrate around an analysis agent: scheduling, context loading, typed tools, deterministic state, policy gates, human feedback and audit records.

It is not a broker, an autonomous trading bot, a market-data vendor or a general-purpose agent framework.

## Harness map

| Harness concern | Project implementation |
|---|---|
| Run orchestration | Scheduled market nodes, atomic `dispatch_node_claim`, one analysis task per trading day |
| Context assembly | Account, candidate pool, active strategy and node-specific Prompt protocols |
| Tool interface | JSON-Schema-based MCP stdio tools |
| Deterministic state | Cash, positions, T+1 availability, pending-order locks, candidate-pool health and strategy version |
| Guardrails | Entry, exit, position sizing, stop, take-profit and T-trade checks |
| Failure handling | Independent nodes, replaceable adapters, stale-data rejection and observation-only degradation |
| Human feedback | Order intent followed by filled, partial or cancelled feedback |
| Observability | Analysis-run records, evidence metadata, daily journals and weekly/monthly reports |
| Verification | Unit tests for state transitions, node claims, order locks, candidate-pool rules and strategy logic |

## Run contract

1. The scheduler wakes a node-specific dispatcher.
2. The dispatcher atomically claims the most recent due node and skips duplicate, expired or future work.
3. The analysis agent loads deterministic context and collects node-specific evidence through replaceable adapters.
4. The agent runs strategy and state checks before creating any precise instruction.
5. The MCP core records an order intent but never sends it to a broker.
6. A human executes or rejects the instruction and records the actual outcome.
7. The state machine updates cash and positions, while the run and evidence remain auditable.

## Safety invariants

- No broker connection and no automatic order submission.
- No account or position inference from conversation memory.
- No precise new-buy instruction when critical evidence is stale, missing or conflicting.
- No reuse of cash or sellable shares locked by an order awaiting feedback.
- No silent strategy mutation during a trading session; proposed changes pass an explicit update gate.
- No skipped audit record merely because the conclusion is to hold or take no action.

## Public/private boundary

The public repository contains the Harness core, Prompt contracts, strategy template, tests and empty examples. Real accounts, positions, candidates, task identifiers, market-data credentials and run journals belong to the private runtime and must not be committed.
