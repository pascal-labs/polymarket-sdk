# Orderbook Analysis on Polymarket

## How Polymarket Orderbooks Work

Every Polymarket market is a binary outcome contract with two tokens: YES and NO.
Each token has its own orderbook on the CLOB. The critical constraint is:

```
YES_price + NO_price = 1.00  (in a fair market)
```

This is the **pair invariant**. If YES trades at 0.65, NO should trade at 0.35.
Any deviation from this creates a riskless arbitrage opportunity -- you can buy
both sides for less than $1.00 and guarantee a $1.00 payout at resolution.

In practice, the sum drifts slightly from 1.00 due to spread on each side.
Monitoring `YES_mid + NO_mid - 1.00` gives you the **vig** or **overround**
of the market. On liquid Polymarket markets this is typically 0.5-2 cents.

---

## Reading the L2 Book

The CLOB API returns a full Level 2 orderbook:

```python
orderbook = client.get_orderbook(token_id)
# Returns:
# {
#     "bids": [{"price": "0.65", "size": "1500.0"}, {"price": "0.64", "size": "800.0"}, ...],
#     "asks": [{"price": "0.67", "size": "500.0"}, {"price": "0.68", "size": "1200.0"}, ...],
#     "min_order_size": "5.0"
# }
```

**Bids** are sorted by price descending (highest bid first). **Asks** are sorted
by price ascending (lowest ask first). The spread is `best_ask - best_bid`.

The `min_order_size` field is critical -- it tells you the minimum number of
shares required for an order on this market. Markets with higher minimums (often
50-100 shares) are typically newer or less liquid. This value is cached by the
SDK to avoid redundant fetches.

---

## Spread Patterns by Market Type

Spreads vary significantly by market structure and liquidity:

### High-Volume Binary Markets (e.g., "Will X happen by date Y?")

- **Typical spread:** 1-2 cents
- **Depth at BBO:** 500-5,000 shares
- **Profile:** Tight spreads, deep books, fast-moving. Market makers are active.
  These are the best markets for systematic trading.

### Multi-Outcome / NegRisk Markets (e.g., tweet count bins)

- **Typical spread:** 2-5 cents per bin
- **Depth at BBO:** 50-500 shares per bin
- **Profile:** Wider spreads because liquidity fragments across 10-30 bins. The
  YES side of popular bins (where most volume concentrates) tends to be tighter
  than tail bins. Arbitrage between bins is possible but the wider spreads make
  it harder to capture.

### Low-Volume / Long-Dated Markets

- **Typical spread:** 5-15 cents
- **Depth at BBO:** 10-100 shares
- **Profile:** Wide spreads, thin books. Price moves in chunks. These markets
  often have stale quotes that do not reflect current information. The Gamma API
  prices can lag real orderbook prices by minutes here.

---

## Liquidity Depth Analysis

Surface-level spread tells you the cost of a small trade. For meaningful size,
you need to walk the book:

```
Ask side (selling pressure):
  $0.67 x 500   -- $335 to lift
  $0.68 x 1200  -- $1,151 cumulative
  $0.70 x 300   -- $1,361 cumulative

Bid side (buying pressure):
  $0.65 x 1500  -- $975 to hit
  $0.64 x 800   -- $1,487 cumulative
  $0.62 x 200   -- $1,611 cumulative
```

**Key metrics:**

- **Depth ratio** = `sum(bid_sizes) / sum(ask_sizes)` -- Values above 1.0 mean
  more buying pressure; below 1.0 means more selling pressure. Not a signal on
  its own, but useful in context.

- **$100 slippage** = average fill price for a $100 market order vs the BBO. On
  liquid markets this is 0-1 cent. On thin markets it can be 5+ cents.

- **Microprice** = `(best_bid * ask_size + best_ask * bid_size) / (bid_size + ask_size)`.
  This weights the midpoint toward the side with less depth, giving a more
  accurate estimate of the "true" price than a simple midpoint.

The SDK's `PriceState.get_expected_fill_price()` method walks the book to compute
exact slippage for a given order size. This is essential for position sizing --
you need to know your actual entry cost, not the quoted spread.

---

## Why Connection Pooling Matters at Trading Speed

A single orderbook fetch over a cold connection:
```
DNS lookup:       ~5ms   (cached after first)
TCP handshake:    ~50ms  (SYN, SYN-ACK, ACK)
TLS negotiation:  ~150ms (ClientHello through Finished)
HTTP request:     ~30ms  (actual data transfer)
Total:            ~235ms
```

A pooled request reusing an existing connection:
```
HTTP request:     ~30ms
Total:            ~30ms
```

When you are fetching 26 bin midpoints in parallel, the difference is:
- Cold: 26 x 235ms = 6.1 seconds (even with 10 threads)
- Pooled: 26 x 30ms = 0.8 seconds (with 10 threads, ~3 batches)

For a market maker that needs to requote after every fill, or a scanner that
checks dozens of markets per minute, this 5+ second difference is the difference
between capturing edge and watching it evaporate.

The SDK's `HTTPPoolManager` pre-warms connections to all three API tiers at
initialization. The `patch_py_clob_client()` function extends this to the CLOB
client library itself, which otherwise creates fresh connections on every call.

---

## Using the WebSocket Feed for Real-Time Monitoring

The HTTP orderbook endpoint is fine for periodic snapshots, but for continuous
monitoring you want the WebSocket feed. It pushes full book snapshots on
subscription and incremental BBO updates on every change.

### Connection Setup

```python
from polymarket_sdk.websocket_feed import WebSocketPriceFeed

feed = WebSocketPriceFeed()
connected = feed.start(yes_token_id, no_token_id)

# Prices are now updating in a background thread
yes_price, no_price = feed.get_prices()  # Instant -- reads from memory
spread = feed.get_spread(yes_token_id)   # Best ask - best bid
age = feed.get_age()                     # Seconds since last update
```

### Staleness Detection

The feed tracks the timestamp of every update. If prices are older than a
threshold (default 5 seconds), the `is_stale()` method returns True. This is
your signal to fall back to HTTP polling or pause trading -- a stale feed usually
means the WebSocket disconnected and is in the reconnect backoff loop.

### Full Book Access

After the initial `book` event, the `PriceState` object holds the full L2 book:

```python
state = feed.prices[token_id]
# state.bids = [(0.65, 1500.0), (0.64, 800.0), ...]  -- descending by price
# state.asks = [(0.67, 500.0), (0.68, 1200.0), ...]   -- ascending by price
```

This lets you compute depth, microprice, and slippage in real time without any
additional API calls.

---

## Slippage Estimation Methodology

Before placing a market order, estimate your actual fill cost:

### Step 1: Walk the Book

For a BUY order of N shares, consume ask levels starting from the best ask:

```
remaining = N
total_cost = 0

for (price, size) in asks:
    fill = min(remaining, size)
    total_cost += fill * price
    remaining -= fill
    if remaining <= 0:
        break

avg_fill_price = total_cost / N
slippage = avg_fill_price - best_ask
```

### Step 2: Check Liquidity Sufficiency

If `remaining > 0` after exhausting the book, there is not enough liquidity for
your order size. Either reduce size or use a limit order.

### Step 3: Compare to Your Edge

If your model says fair value is 0.60 and your estimated fill price is 0.67
with 2 cents of slippage, your real edge is:

```
model_edge = fair_value - fill_price = 0.60 - 0.67 = -0.07
```

Negative edge after slippage means the trade is not worth taking at this size.
The SDK's `get_expected_slippage()` method returns all of this in a single call:

```python
result = feed.get_expected_slippage(token_id, 'BUY', 500)
# {
#     'avg_price': 0.672,
#     'best_price': 0.67,
#     'slippage': 0.002,
#     'slippage_pct': 0.30,
#     'total_cost': 336.0
# }
```

### Step 4: Factor in the Pair

When trading YES tokens, remember that selling NO tokens is economically
equivalent (minus spread). If the YES ask is thick but the NO bid is thin,
buying YES directly may give better execution than selling NO. Always check
both sides before routing.

---

## Practical Considerations

**Orderbook updates are not atomic.** The WebSocket can deliver a `book` event
for the YES token and a `price_change` for the NO token at slightly different
times. If you are computing the pair invariant or cross-side arbitrage, use
timestamps to ensure you are comparing contemporaneous data.

**Displayed size is not guaranteed.** Resting orders can be cancelled at any
time. By the time your market order reaches the matching engine, the liquidity
you saw may be gone. For large orders, use limit orders with a tight price cap
rather than market orders.

**Minimum tick size is $0.01.** All prices are in cents. The smallest meaningful
spread is 1 cent. When the spread is at the minimum, the market is maximally
tight and you are competing directly with dedicated market makers.

**Volume is denominated in USDC.** When the Gamma API reports $50,000 volume on
a market, that is $50,000 in USDC.e that has changed hands. For a binary market
priced at $0.10, that represents 500,000 shares traded.
