# Human-trader data matrix v5.4

| Analysis need | Source / computation | Failure behavior |
|---|---|---|
| Latest price, previous close, OHLC, volume, amount and quote time | Tencent batch quote | `tradeable=false`; no precise suggestion |
| Bid/ask, VWAP proxy and spread | Tencent quote fields plus local math | mark the field unavailable |
| Intraday path and 5/15/30-minute behavior | Tencent minute data plus local features | record concrete error; no fallback |
| MA5/10/20/60, ATR14, returns and volume ratio | Tencent qfq daily K plus local features | disclose missing or stale history |
| Market regime | SSE, SZSE and CSI300 Tencent quotes | do not invent whole-market breadth |
| Intraday and five-day relative strength | stock return minus CSI300 return | unavailable when either side is stale |
| Sector change, turnover, main net flow, breadth, leader and multi-period returns | Tencent Shenwan level-2 aggregate quote | hard condition unavailable if incomplete or wrong-date |
| Account, sellable shares and T+1 | deterministic account state | block invalid sells and duplicate resource use |
| Candidate score and price levels | versioned weekly candidate pool | freeze entries when candidate evidence is invalid |
| Monitoring | Tencent quote snapshot plus crossing/cooldown/rearm | signal triggers analysis only |
| Close analysis | fresh persisted `market_close` packet | reject summary-only close |
| Next-session outlook | base/bull/bear scenarios plus conditional plans | not an order; reanalyze next session |

The desk never downloads all sector constituents to reconstruct sector performance during analysis. Each packet records source health, timestamps, field coverage and policy decisions. Missing data is an explicit fact, not permission to synthesize a replacement.
