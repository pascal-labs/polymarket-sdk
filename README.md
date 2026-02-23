# polymarket-sdk

Python SDK for interacting with Polymarket's CLOB (Central Limit Order Book) API. Handles authentication, order management, position tracking, market data, and WebSocket price feeds.

## Features

- **REST API Client** — Market data, order placement (single + batch), position queries
- **WebSocket Feed** — Real-time L2 orderbook with thread-safe price access
- **HTTP Connection Pooling** — Pre-warmed connections, 150-300ms latency reduction
- **Position Manager** — Track open positions, P&L, exposure limits
- **Market Discovery** — Auto-discover active markets by category
- **Token Redemption** — Redeem winning positions (CTF + NegRisk contracts)

## Quick Start

```python
from polymarket_sdk import PolymarketClient

# Read-only (no wallet needed)
client = PolymarketClient(mode='paper')
prices = client.get_market_prices("market-slug")
orderbook = client.get_orderbook(token_id)
midpoint = client.get_midpoint(token_id)

# Trading (requires Polygon wallet)
client = PolymarketClient(
    private_key="0x...",
    mode='live'
)

# Place a limit order
client.place_order(
    token_id="0x...",
    side="BUY",
    size=10.0,       # USDC amount
    price=0.55,      # Price per share
    order_type="GTC"
)

# Batch orders (up to 15 per call)
client.place_orders_true_batch(orders, order_type='GTC')

# Position management
positions = client.get_positions_from_data_api()
balance = client.get_balances()
```

## WebSocket Feed

```python
from polymarket_sdk.websocket_feed import WebSocketPriceFeed

feed = WebSocketPriceFeed()
feed.start(up_token_id="0x...", down_token_id="0x...")

prices = feed.get_prices()  # (up_price, down_price)
spread = feed.get_spread()
depth = feed.get_orderbook_depth()
```

## API Endpoints
| Endpoint | Purpose |
|----------|---------|
| `gamma-api.polymarket.com` | Market metadata, events, search |
| `clob.polymarket.com` | Orders, orderbook, midpoints |
| `data-api.polymarket.com` | Positions, balances, trade history |
| `ws-subscriptions-clob.polymarket.com` | WebSocket L2 feed |

## Configuration

Copy `.env.example` to `.env`:
```
POLYGON_PRIVATE_KEY=0x...
POLYGON_RPC=https://polygon-rpc.com
```

## Tech Stack
- `py-clob-client` — Polymarket's official CLOB client
- `eth-account` — Wallet signing (Polygon)
- `websockets` — Real-time orderbook feed
- `requests` + `urllib3` — Connection pooling

## Installation
```bash
pip install -r requirements.txt
```
