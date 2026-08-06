# Human-trader data matrix v5.1

| Analysis need | Source / computation | Failure behavior |
|---|---|---|
| Latest price, previous close, OHLC, volume, amount, quote time | Tencent batch quote | `tradeable=false`; no precise order |
| Bid/ask, VWAP proxy, spread | Tencent quote fields + local math | mark field unavailable |
| Intraday path and 5/15/30-minute behavior | Tencent minute data + local features | background only; no fallback |
| MA5/10/20/60, ATR14, returns, volume ratio | Tencent qfq daily K + local features | stale cache disclosed |
| Market regime | SSE, SZSE and CSI300 Tencent quotes | do not invent breadth |
| Intraday/5-day relative strength | stock minus CSI300 | unavailable if either side is stale |
| Sector performance | audited constituent snapshot + Tencent batch quotes | hard condition unavailable below coverage threshold |
| Account, sellable shares, T+1 | deterministic account state | block invalid sell/duplicate resource use |
| Candidate score and price levels | audited weekly candidate pool | freeze entries when pool/sector evidence invalid |
| Monitoring | Tencent snapshot + crossing/cooldown/rearm | signal triggers analysis only |
| Close analysis | new persisted `market_close` packet | reject summary-only close |
| Next-day outlook | base/bull/bear scenarios + conditional plans | not an order; reanalyze next day |

Every packet records source health, timestamp, coverage and policy. A missing field is an explicit fact, not permission to synthesize a replacement.
