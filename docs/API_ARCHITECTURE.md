# Polymarket API Architecture

## Overview

Polymarket operates a hybrid on-chain/off-chain trading system. Markets are
binary outcome contracts settled on Polygon, but order matching happens off-chain
on a central limit order book (CLOB). Understanding the three-tier API structure
is essential for building anything that trades, quotes, or monitors these markets.

```
                        +---------------------+
                        |   Your Application  |
                        +---------------------+
                           |       |       |
              REST (meta)  | REST  |  WSS  |  REST (positions)
                           |       |       |
            +--------------+--+ +--+----+  +--+--------------+
            | Gamma API       | | CLOB  |  |  Data API       |
            | (Market Index)  | | API   |  |  (Portfolio)    |
            +--------------+--+ +--+----+  +--+--------------+
            gamma-api.       clob.          data-api.
            polymarket.com   polymarket.com polymarket.com
                                  |
                        +---------+---------+
                        | Matching Engine   |
                        | (off-chain CLOB)  |
                        +---------+---------+
                                  |
                        +---------+---------+
                        |  Polygon Mainnet  |
                        |  (chain_id=137)   |
                        |  CTF + NegRisk    |
                        +-------------------+
```

---

## Tier 1: Gamma API (Market Metadata)

**Base URL:** `https://gamma-api.polymarket.com`

The Gamma API is the market index. It serves event metadata, market descriptions,
outcome definitions, and the mapping between human-readable slugs and on-chain
token IDs. It does not require authentication.

**Key endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `GET /events?slug={slug}` | Fetch event by slug (returns markets array) |
| `GET /events?active=true&closed=false&limit=100` | Discover active markets |
| `GET /markets/{condition_id}` | Single market metadata |

**What it returns:**

Each event contains a `markets` array. Each market contains:
- `outcomePrices` -- JSON-encoded array `["0.65", "0.35"]` (YES, NO)
- `clobTokenIds` -- JSON-encoded array `["token_yes", "token_no"]`
- `conditionId` -- On-chain condition identifier for settlement
- `groupItemTitle` -- Human-readable bin label (e.g., "260-279")
- `active`, `closed` -- Market lifecycle state
- `volume`, `liquidity` -- Aggregate trading stats

**When to use Gamma:**

- Market discovery and scanning
- Resolving slugs to token IDs before placing orders
- Getting the token ID pair (YES + NO) for a market
- Reading aggregate volume and liquidity stats

**Important caveat:** The `outcomePrices` field is stale. It updates on a delay,
sometimes lagging the real orderbook by minutes. For live trading, always fetch
the midpoint from the CLOB API instead. This SDK fetches midpoints in parallel
across all bins for exactly this reason -- see the `get_market_prices` method,
which spawns up to 10 concurrent threads to fetch real-time midpoints.

---

## Tier 2: CLOB API (Order Management)

**Base URL:** `https://clob.polymarket.com`

The CLOB API is where trading happens. It handles order submission, cancellation,
orderbook queries, midpoint lookups, and trade history. All write operations
require HMAC-SHA256 authentication.

**Key endpoints:**

| Endpoint | Auth | Purpose |
|----------|------|---------|
| `GET /book?token_id={id}` | No | Full L2 orderbook snapshot |
| `GET /midpoint?token_id={id}` | No | Current midpoint price |
| `POST /order` | HMAC | Submit a signed order |
| `POST /orders` | HMAC | Batch submit (up to 15 per call) |
| `DELETE /order/{id}` | HMAC | Cancel an order |
| `DELETE /orders` | HMAC | Batch cancel |
| `GET /orders` | HMAC | Open orders query |
| `GET /order/{id}` | HMAC | Order status + fill details |

**Order types supported:**

- **GTC** (Good-Til-Cancelled) -- Rests on the book until filled or cancelled
- **FOK** (Fill-Or-Kill) -- Executes immediately in full or not at all
- **FAK** (Fill-And-Kill) -- Fills what it can, cancels the rest

**Batch API:**

The batch endpoint (`POST /orders`) accepts up to 15 orders per call. This SDK
signs all orders locally, then sends them in chunks of 15. If a batch call fails,
it falls back to posting individually -- critical for reliability when you are
quoting both sides of multiple markets simultaneously.

**Minimum order sizes:**

Each market has a `min_order_size` (in shares) returned with the orderbook. The
SDK caches this per token ID to avoid redundant lookups. Orders below the minimum
are rejected client-side before hitting the API, returning `'MIN_SIZE_VIOLATION'`
instead of wasting a round trip.

---

## Tier 3: Data API (Portfolio & Positions)

**Base URL:** `https://data-api.polymarket.com`

The Data API provides portfolio-level reads: current positions with average entry
prices, P&L, and redeemable (resolved) positions. It does not require HMAC auth
-- just the wallet address.

**Key endpoints:**

| Endpoint | Purpose |
|----------|---------|
| `GET /positions?user={address}` | All positions with avg entry price |
| `GET /positions?user={address}&redeemable=true` | Resolved positions ready for redemption |

**Position fields:**

- `asset` -- Token ID (not `asset_id`; this is a common gotcha)
- `size` -- Current share count
- `avgPrice` -- Volume-weighted average entry price (not `avg_price`)
- `currentValue` -- Mark-to-market value at last traded price
- `cashPnl` -- Realized P&L
- `outcome` -- `"Yes"` or `"No"` (title case, not uppercase)
- `conditionId` -- Links back to the on-chain condition for redemption

---

## WebSocket Protocol

**URL:** `wss://ws-subscriptions-clob.polymarket.com/ws/market`

The WebSocket feed provides real-time L2 orderbook updates. It is unauthenticated.

**Subscription format:**

```json
{
    "assets_ids": ["token_id_1", "token_id_2"],
    "type": "market"
}
```

**Event types:**

1. **`book`** -- Full orderbook snapshot. Sent immediately after subscription and
   periodically thereafter.

   ```json
   {
       "event_type": "book",
       "asset_id": "0x...",
       "bids": [{"price": "0.65", "size": "1500.0"}, ...],
       "asks": [{"price": "0.67", "size": "800.0"}, ...]
   }
   ```

2. **`price_change`** -- Incremental BBO update. Sent whenever the best bid or
   ask changes.

   ```json
   {
       "event_type": "price_change",
       "price_changes": [
           {
               "asset_id": "0x...",
               "best_bid": "0.65",
               "best_ask": "0.67"
           }
       ]
   }
   ```

   Note: the field is `price_changes`, not `changes`. Some older documentation
   references `changes`; the SDK handles both for backward compatibility.

**Connection management:**

- Ping interval: 20 seconds
- Pong timeout: 10 seconds
- Re-subscribe every 180 seconds to keep the subscription alive
- Auto-reconnect with exponential backoff (1s initial, 5s cap)
- All state is held in a thread-safe `PriceState` dataclass

---

## Authentication: Wallet Signing Flow

Polymarket uses a two-layer auth model:

### Layer 1: Wallet Identity (EOA)

The SDK connects using a standard Ethereum private key (EOA wallet). The
`py-clob-client` library derives the wallet address and uses `signature_type=0`
(EOA) on Polygon mainnet (`chain_id=137`).

### Layer 2: CLOB API Credentials (HMAC-SHA256)

The CLOB API requires a separate set of credentials:
- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`

These can be provided via environment variables or derived on first use via
`client.create_or_derive_api_creds()`. The derived credentials are deterministic
from the private key, so they are stable across sessions.

Every authenticated request is signed with HMAC-SHA256 using the API secret.
This is why the HTTP connection pooling in this SDK uses **urllib3 directly**
instead of `requests.Session` -- the `requests` library mutates headers (adds
`Content-Length`, normalizes casing), which breaks the HMAC signature. The
`patch_py_clob_client()` function in `http_pool.py` monkey-patches the upstream
client to use a raw urllib3 `PoolManager` that preserves headers exactly as
signed.

### Layer 3: Polymarket Proxy Wallets

Polymarket's frontend uses proxy wallets (smart contract wallets) rather than
direct EOA signing. The SDK bypasses this entirely and signs directly with the
EOA key (`signature_type=0`). This is the correct approach for programmatic
trading -- proxy wallets add latency and complexity with no benefit for bots.

---

## NegRisk vs Standard CTF Contracts

Polymarket uses Gnosis Conditional Token Framework (CTF) contracts for all
markets, but there are two distinct flavors:

### Standard CTF Markets

Simple binary markets (YES/NO). Each market has one condition ID and two token
IDs. Settlement pays out to the winning side's token holders directly in USDC.e.

**Redemption path:**
```
CTF.redeemPositions(USDC.e, parentCollectionId=0x00, conditionId, [1, 2])
```

### NegRisk Markets

Multi-outcome markets (e.g., "How many tweets will Elon post?" with 26 bins).
These use a NegRisk adapter that wraps collateral to enable the multi-outcome
structure. Each bin is technically its own binary market, but they share a parent
event through the NegRisk framework.

**Key contracts:**
- `negRiskAdapter`: `0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296`
- `negRiskCtfExchange`: `0xC5d563A36AE78145C45a50134d48A1215220f80a`
- `negRiskWrappedCollateral`: `0x3A3BD7bb9528E159577F7C2e685CC81A765002E2`

**Redemption path (two steps):**
```
1. CTF.redeemPositions(wrappedCollateral, parentCollectionId=0x00, conditionId, [1, 2])
2. wrappedCollateral.unwrap(walletAddress, amount)
```

The SDK's `EnhancedWinningsRedeemer` handles this automatically -- it tries
wrapped collateral first for NegRisk markets, falls back to direct USDC.e, and
handles the unwrap step after all positions are redeemed.

**How to detect NegRisk:** The SDK uses heuristics on the market slug and outcome
format. Bucket-style outcomes (e.g., "260-279") and tweet-related slugs indicate
NegRisk markets. A more robust approach would be to check the `negRisk` field
from the Gamma API response, but this field is not always populated.

---

## Connection Pooling: Why It Matters

Every HTTP request to a new host requires:
- TCP handshake: ~50-100ms
- TLS negotiation: ~100-200ms

For a trading system that fetches 26 midpoints in parallel, polls orderbooks
every few seconds, and submits orders with time sensitivity, those 150-300ms add
up fast. The SDK solves this at two levels:

### Level 1: `requests.Session` Pool (Gamma + Data API)

The `HTTPPoolManager` singleton maintains a `requests.Session` with:
- 10 host connection pools
- 20 max connections per host
- Retry strategy with backoff for 429/5xx errors
- Pre-warmed connections to all three API endpoints

The `pooled_get()` function is a drop-in replacement for `requests.get()`.

### Level 2: urllib3 Pool (CLOB API via py-clob-client)

The CLOB client library creates a new `requests.Session` internally. The SDK
patches this with a raw `urllib3.PoolManager` that:
- Reuses TCP+TLS connections across CLOB API calls
- Does not mutate headers (preserving HMAC signatures)
- Pre-warms connections to `clob.polymarket.com`

This is called once at startup via `patch_py_clob_client()`. After the patch,
every order placement, cancellation, and orderbook fetch reuses the same
underlying TCP connection.

---

## Order Lifecycle

```
  Client                    CLOB API               Polygon (L1)
    |                          |                        |
    |-- sign order locally --> |                        |
    |-- POST /order ---------> |                        |
    |                          |-- match engine ------> |
    |                          |   (off-chain)          |
    |<-- {orderID, ...} -------|                        |
    |                          |                        |
    |   [if matched]           |                        |
    |                          |-- settle on-chain ---> |
    |                          |   CTF transfer         |
    |                          |                        |
    |   [at resolution]        |                        |
    |                          |   payoutNumerators set |
    |-- redeemPositions -------|----------------------> |
    |<-- USDC.e transferred ---|----------------------- |
```

1. **Sign** -- The SDK signs the order locally using the EOA private key. The
   signed order contains token ID, side, size, price, and an EIP-712 signature.

2. **Submit** -- The signed order is posted to the CLOB API with HMAC auth
   headers. For limit orders, it rests on the book; for FOK/FAK, it executes
   immediately or is killed.

3. **Match** -- The matching engine runs off-chain. When a taker order crosses a
   resting maker order, a trade occurs. The CLOB API records the fill.

4. **Settle** -- Matched trades settle on Polygon via CTF token transfers. The
   buyer receives conditional tokens; the seller receives USDC.e.

5. **Resolve** -- When the market outcome is determined, the oracle sets
   `payoutNumerators` on the CTF contract. Winners can then call
   `redeemPositions` to convert their conditional tokens back to USDC.e.

**Size conventions:**
- BUY orders: `size` = dollar amount to spend, `amount` for market orders
- SELL orders: `size` = number of shares to sell
- Limit BUY: SDK converts dollars to shares via `ceil(dollars / price)`
- Prices: max 2 decimal places (API constraint on `makerAmount`)

---

## Rate Limits and Error Handling

The CLOB API enforces rate limits per IP and per API key. The SDK handles this
through:

- Connection pooling (reduces TCP overhead, avoids hitting connection limits)
- Retry strategy with exponential backoff (3 retries, 0.3s backoff factor)
- Batch operations where possible (15 orders/call, batch cancel)
- Client-side validation (min order size check before API call)
- Fallback from batch to individual operations on failure

The Polygon RPC endpoints used for redemption have their own rate limits. The
SDK rotates through a pool of 5+ RPC endpoints and supports Alchemy as the
primary provider when configured.
