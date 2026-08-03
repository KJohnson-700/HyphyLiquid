# External Liquidation Data Sources - 2026-08-03

## Question

Can HyphyLiquid accelerate BTC and HYPE validation by pulling external
Hyperliquid liquidation, position, and microstructure datasets instead of
waiting only on the live collector?

## Short Answer

Yes. The best acceleration path is not another strategy idea. It is external
data backfill plus cross-source validation. BTC and HYPE remain the front
runners, but the current live sample is too small and coverage-sensitive. We
should use third-party data to increase sample size, then rerun the same
candidate logic against cleaner, older, and broader windows.

## Priority Ranking

| Rank | Source | Best Use | Access | Why It Matters |
|---|---|---|---|---|
| 1 | Moon Dev Hyperliquid Data Layer | 30d liquidations, all-exchange liquidations, position snapshots near liquidation | `MOONDEV_API_KEY` | Highest near-term value. Covers event liquidations and pre-cascade pressure. Position snapshots explicitly track BTC, ETH, SOL, XRP, HYPE. |
| 2 | 0xArchive | Hyperliquid liquidation events, OI, funding, price history, freshness | API key likely required | Best candidate for normalized historical liquidation backfill and cross-checking our heuristic detector. |
| 3 | Hyperliquid official S3/node data | L2 book snapshots, asset contexts, node fills/trades | AWS requester-pays; no normal API key | Best source-of-truth for market microstructure and official backfill, but heavier to ingest. |
| 4 | Allium | Raw fills with `liquidation` field, analytics tables, no-rate-limit mirrored API | API key/contact | Strong schema reference and validation layer. Useful to confirm how market/backstop liquidations are represented in fills. |
| 5 | Tardis.dev | Historical trades, L2, quotes, book snapshots for Hyperliquid | Paid API; free first-day monthly CSV samples | Useful for book/trade backtests and slippage modeling. Does not appear to be the fastest direct liquidation-event path. |
| 6 | BlockLiquidity / Hyperliquid OHLC | Per-token liquidation event endpoint, timestamp filters, up to 10k records | Bearer token | Simple BTC/HYPE event pull if access is available. |
| 7 | PurrData | Normalized liquidation events, WebSocket, CSV/Parquet backfills | Waitlist/paid tiers | Looks purpose-built, but access may be slower. |
| 8 | CoinGlass/Gate liquidation pages | BTC/HYPE liquidation dashboards | Commercial/screen or API | Good sanity checks, not primary backtest data unless API access is clean. |

## MiniMax Scout Notes

MiniMax search confirmed:

- Moon Dev exposes `GET /api/liquidations/{timeframe}.json` for 10m, 1h,
  4h, 12h, 24h, 2d, 7d, 14d, and 30d windows.
- Moon Dev also exposes position snapshots for BTC, ETH, SOL, XRP, and HYPE
  within 15% of liquidation, captured at 1-minute frequency.
- Official Hyperliquid historical data gives L2 book snapshots and asset
  contexts in S3, plus node fills/trades in `hl-mainnet-node-data`.
- Allium identifies Hyperliquid liquidations through fills where the
  `liquidation` field is present, including market and backstop methods.
- Tardis has Hyperliquid historical market data since 2024-10-29 and gives
  sample CSVs for the first day of each month without an API key.

## How This Changes The Build

### BTC

BTC is still the v1 front runner, but we need more mature data before paper.
The immediate objective is to rerun:

- BTC side=B `failed_reclaim_continuation`
- event-VWAP stop with 15-25 bps buffer
- activation near 2R
- 10 bps trail
- 120m and 240m evaluation windows

External data must answer whether the current edge survives:

- higher n
- older windows
- broad `120m@120m` coverage
- different volatility regimes
- realistic slippage around liquidation bursts

### HYPE

HYPE is the alt front runner. The priority is not immediate execution. It is
to expand the HYPE B-side range/liquidation scalp sample and add pre-cascade
position-pressure features:

- positions within liquidation distance
- long/short liquidation-side imbalance
- liquidation cluster notional
- band-width regime
- market-wide liquidation correlation from Binance/Bybit/OKX

Moon Dev is especially useful here because it explicitly lists HYPE position
snapshots and HYPE buyer/position endpoints.

## Env Keys To Add When Available

Add only keys we actually obtain. Do not invent credentials.

```env
MOONDEV_API_KEY=
ZEROXARCHIVE_API_KEY=
BLOCKLIQUIDITY_API_KEY=
ALLIUM_API_KEY=
TARDIS_API_KEY=
PURRDATA_API_KEY=
COINGLASS_API_KEY=
```

## Marvis / MiniMax Delegation

Marvis should do low-level probing and logging, not strategy promotion.

### Task 1 - Source Access Matrix

For each source above, log:

- signup URL
- free tier or paid tier
- required env var
- symbols supported for BTC and HYPE
- historical retention
- rate limits
- whether liquidation side means liquidated long/short or taker side A/B
- sample response shape
- timestamp unit and timezone

### Task 2 - API Probe Once Keys Exist

Once an API key exists, make one read-only request per source and save raw
samples under `data/external_samples/{source}/`. Never overwrite old samples.
Never place orders.

### Task 3 - Normalization Draft

Draft a canonical liquidation schema:

```text
source
venue
symbol
ts_ms
side
liquidated_side
price
size
notional_usd
mark_price
method
trade_id
user_address_hash
raw
```

### Task 4 - Cross-Reference Plan

Compare external events against our `data/liquidations.jsonl` by:

- symbol
- side
- timestamp bucket
- price proximity
- notional bucket

The goal is to measure detector precision/recall, not to tune strategy
parameters blindly.

## Decision

Use Moon Dev first if Slim can get a key. If not, probe official Hyperliquid
S3/Tardis samples for book/trade history while our live collector keeps running.
Do not promote BTC or HYPE until external data confirms the edge outside the
current small live sample.

## Sources

- Hyperliquid historical data: https://hyperliquid.gitbook.io/hyperliquid-docs/historical-data
- Hyperliquid liquidations mechanics: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/liquidations
- Moon Dev examples/API reference: https://github.com/moondevonyt/Hyperliquid-Data-Layer-API/blob/main/examples/README.md
- Moon Dev repo: https://github.com/moondevonyt/Hyperliquid-Data-Layer-API
- 0xArchive data docs: https://docs.0xarchive.io/data
- Allium data guide: https://docs.allium.so/historical-data/supported-blockchains/hyperliquid/data-guide
- Tardis Hyperliquid data: https://docs.tardis.dev/historical-data-details/hyperliquid
- BlockLiquidity liquidation endpoint: https://hyperliquid-ohlc.gitbook.io/api-docs/rest-api/liquidation/liquidation-event-by-token
- PurrData: https://www.purrdata.io/
