"""
Polymarket WebSocket Price Feed

Real-time price updates via websocket instead of HTTP polling.
Reduces latency from ~2 seconds to ~50ms.

Usage:
    feed = WebSocketPriceFeed()
    feed.start(up_token_id, down_token_id)

    # In your trading loop:
    up_price, down_price = feed.get_prices()
"""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from typing import Optional, Dict, Callable
import threading

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("Warning: websockets not installed. Run: pip install websockets")

logger = logging.getLogger(__name__)

WSS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


@dataclass
class PriceState:
    """Thread-safe container for latest prices and orderbook."""
    best_bid: Optional[float] = None
    best_ask: Optional[float] = None
    mid_price: Optional[float] = None
    last_update: float = 0.0
    # Full orderbook for depth analysis
    bids: list = None  # [(price, size), ...] sorted by price descending
    asks: list = None  # [(price, size), ...] sorted by price ascending

    def __post_init__(self):
        if self.bids is None:
            self.bids = []
        if self.asks is None:
            self.asks = []

    def update(self, best_bid: float, best_ask: float):
        self.best_bid = best_bid
        self.best_ask = best_ask
        self.mid_price = (best_bid + best_ask) / 2 if best_bid and best_ask else None
        self.last_update = time.time()

    def update_book(self, bids: list, asks: list):
        """Update full orderbook. bids/asks are lists of (price, size) tuples."""
        self.bids = sorted(bids, key=lambda x: x[0], reverse=True)  # Highest first
        self.asks = sorted(asks, key=lambda x: x[0])  # Lowest first
        if self.bids and self.asks:
            self.best_bid = self.bids[0][0]
            self.best_ask = self.asks[0][0]
            self.mid_price = (self.best_bid + self.best_ask) / 2
        self.last_update = time.time()

    def get_expected_fill_price(self, side: str, shares: float) -> Optional[tuple]:
        """
        Calculate expected average fill price for a market order.

        Args:
            side: 'BUY' or 'SELL'
            shares: Number of shares to trade

        Returns:
            (avg_price, total_cost, slippage_vs_best) or None if not enough liquidity
        """
        if side == 'BUY':
            # Buying = taking from asks (lifting offers)
            book = self.asks
            best_price = self.best_ask
        else:
            # Selling = taking from bids (hitting bids)
            book = self.bids
            best_price = self.best_bid

        if not book or not best_price:
            return None

        remaining = shares
        total_cost = 0.0

        for price, size in book:
            if remaining <= 0:
                break
            fill_size = min(remaining, size)
            total_cost += fill_size * price
            remaining -= fill_size

        if remaining > 0:
            # Not enough liquidity
            return None

        avg_price = total_cost / shares
        slippage = abs(avg_price - best_price)

        return (avg_price, total_cost, slippage)

    def get_available_liquidity(self, side: str, max_price: float = None) -> float:
        """
        Get total shares available at or better than max_price.

        Args:
            side: 'BUY' or 'SELL'
            max_price: Maximum price willing to pay (for BUY) or minimum (for SELL)

        Returns:
            Total shares available
        """
        if side == 'BUY':
            book = self.asks
            if max_price is None:
                return sum(size for _, size in book)
            return sum(size for price, size in book if price <= max_price)
        else:
            book = self.bids
            if max_price is None:
                return sum(size for _, size in book)
            return sum(size for price, size in book if price >= max_price)


class WebSocketPriceFeed:
    """
    Real-time price feed using Polymarket websocket.

    Maintains latest prices in memory for instant access.
    Runs in background thread with auto-reconnect.
    """

    def __init__(self, on_price_update: Optional[Callable] = None):
        self.prices: Dict[str, PriceState] = {}  # token_id -> PriceState
        self.up_token: Optional[str] = None
        self.down_token: Optional[str] = None
        self.connected = False
        self.running = False
        self._ws = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._on_price_update = on_price_update
        self._reconnect_delay = 1.0
        self._max_reconnect_delay = 30.0

    def start(self, up_token: str, down_token: str):
        """Start websocket feed in background thread."""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets library not installed")
            return False

        self.up_token = up_token
        self.down_token = down_token
        self.prices[up_token] = PriceState()
        self.prices[down_token] = PriceState()
        self.running = True

        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()

        # Wait for connection (up to 5 seconds)
        start = time.time()
        while not self.connected and time.time() - start < 5:
            time.sleep(0.1)

        return self.connected

    def stop(self):
        """Stop the websocket feed gracefully."""
        self.running = False
        self.connected = False

        # Close websocket connection gracefully if it exists
        if self._ws and self._loop and self._loop.is_running():
            try:
                # Schedule the close on the event loop
                asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
                # Give it a moment to close cleanly
                time.sleep(0.1)
            except Exception:
                pass  # Ignore errors during cleanup

        # Now stop the event loop
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)

        # Wait for thread to finish
        if self._thread:
            self._thread.join(timeout=1)

    def _run_event_loop(self):
        """Run asyncio event loop in background thread."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        try:
            self._loop.run_until_complete(self._connect_loop())
        except Exception as e:
            if self.running:  # Only log if not intentionally stopped
                logger.error(f"Event loop error: {e}")
        finally:
            # Cancel any pending tasks before closing
            try:
                pending = asyncio.all_tasks(self._loop)
                for task in pending:
                    task.cancel()
                # Give tasks a chance to handle cancellation
                if pending:
                    self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass  # Ignore cleanup errors
            self._loop.close()

    async def _connect_loop(self):
        """Connection loop with auto-reconnect."""
        while self.running:
            try:
                await self._connect_and_subscribe()
            except Exception as e:
                print(f"  ⚠️  WebSocket disconnected: {e}")
                self.connected = False

            if self.running:
                print(f"  🔄 Reconnecting in {self._reconnect_delay:.1f}s...")
                await asyncio.sleep(self._reconnect_delay)
                # Faster reconnect - don't let it get too slow
                self._reconnect_delay = min(
                    self._reconnect_delay * 1.5,  # Slower backoff
                    5.0  # Max 5 seconds between retries
                )

    async def _connect_and_subscribe(self):
        """Connect to websocket and subscribe to market channel."""
        print(f"  📡 WebSocket connecting...")

        # More aggressive keep-alive settings
        async with websockets.connect(
            WSS_URL,
            ping_interval=20,  # Ping every 20s
            ping_timeout=10,   # Wait 10s for pong
            close_timeout=5,   # Faster close
        ) as ws:
            self._ws = ws
            self._reconnect_delay = 1.0  # Reset on successful connect

            # Subscribe to both tokens with type field (per Polymarket docs)
            subscribe_msg = {
                "assets_ids": [self.up_token, self.down_token],
                "type": "market"
            }

            await ws.send(json.dumps(subscribe_msg))
            self.connected = True
            self._reconnect_delay = 1.0  # Reset backoff on success
            print(f"  ⚡ WebSocket connected!")

            last_resubscribe = time.time()
            resubscribe_interval = 180  # Re-subscribe every 3 minutes to stay alive

            # Process messages
            async for message in ws:
                if not self.running:
                    break

                try:
                    self._handle_message(message)
                except Exception as e:
                    logger.error(f"Error handling message: {e}")

                # Periodic re-subscribe to keep connection alive
                if time.time() - last_resubscribe > resubscribe_interval:
                    await ws.send(json.dumps(subscribe_msg))
                    last_resubscribe = time.time()

    def _handle_message(self, raw_message: str):
        """Parse and handle incoming websocket message."""
        try:
            data = json.loads(raw_message)
        except json.JSONDecodeError:
            return

        # Handle empty ack
        if data == []:
            return

        # Handle list of events
        if isinstance(data, list):
            for item in data:
                self._process_event(item)
        elif isinstance(data, dict):
            self._process_event(data)

    def _process_event(self, event: dict):
        """Process a single event from the websocket."""
        if not isinstance(event, dict):
            return

        event_type = event.get("event_type")

        if event_type == "book":
            # Full orderbook snapshot
            asset_id = event.get("asset_id")
            if asset_id in self.prices:
                raw_bids = event.get("bids", [])
                raw_asks = event.get("asks", [])

                # Only update if we have real bids and asks
                if raw_bids and raw_asks:
                    # Convert to (price, size) tuples
                    bids = [(float(b["price"]), float(b["size"])) for b in raw_bids]
                    asks = [(float(a["price"]), float(a["size"])) for a in raw_asks]
                    # Store full orderbook
                    self.prices[asset_id].update_book(bids, asks)

        elif event_type == "price_change":
            # Incremental price updates - uses "price_changes" not "changes"
            changes = event.get("price_changes", []) or event.get("changes", [])
            for change in changes:
                asset_id = change.get("asset_id")
                if asset_id in self.prices:
                    best_bid_str = change.get("best_bid")
                    best_ask_str = change.get("best_ask")

                    if best_bid_str and best_ask_str:
                        best_bid = float(best_bid_str)
                        best_ask = float(best_ask_str)
                        self.prices[asset_id].update(best_bid, best_ask)

                        if self._on_price_update:
                            self._on_price_update(asset_id, best_bid, best_ask)

    def get_prices(self) -> tuple[Optional[float], Optional[float]]:
        """
        Get current UP and DOWN mid prices.
        Returns (up_price, down_price) or (None, None) if not available.

        This is instant - just reads from memory.
        """
        if not self.up_token or not self.down_token:
            return None, None

        up_state = self.prices.get(self.up_token)
        down_state = self.prices.get(self.down_token)

        up_price = up_state.mid_price if up_state else None
        down_price = down_state.mid_price if down_state else None

        return up_price, down_price

    def get_spread(self, token_id: str) -> Optional[float]:
        """Get bid-ask spread for a token."""
        state = self.prices.get(token_id)
        if state and state.best_bid and state.best_ask:
            return state.best_ask - state.best_bid
        return None

    def get_age(self) -> float:
        """Get age of oldest price update in seconds."""
        if not self.prices:
            return float('inf')

        updates = [s.last_update for s in self.prices.values() if s.last_update > 0]
        if not updates:
            return float('inf')

        oldest = min(updates)
        return time.time() - oldest

    def is_stale(self, max_age: float = 5.0) -> bool:
        """Check if prices are stale (older than max_age seconds)."""
        return self.get_age() > max_age

    def get_expected_slippage(self, token_id: str, side: str, shares: float) -> Optional[dict]:
        """
        Calculate expected slippage for a market order.

        Args:
            token_id: The token to trade
            side: 'BUY' or 'SELL'
            shares: Number of shares

        Returns:
            dict with 'avg_price', 'best_price', 'slippage', 'slippage_pct', 'total_cost'
            or None if not enough data/liquidity
        """
        state = self.prices.get(token_id)
        if not state:
            return None

        result = state.get_expected_fill_price(side, shares)
        if not result:
            return None

        avg_price, total_cost, slippage = result
        best_price = state.best_ask if side == 'BUY' else state.best_bid

        return {
            'avg_price': avg_price,
            'best_price': best_price,
            'slippage': slippage,
            'slippage_pct': (slippage / best_price * 100) if best_price else 0,
            'total_cost': total_cost
        }

    def get_orderbook_depth(self, token_id: str) -> dict:
        """Debug method to check orderbook state."""
        state = self.prices.get(token_id)
        if not state:
            return {'error': 'no state'}
        return {
            'bids_count': len(state.bids) if state.bids else 0,
            'asks_count': len(state.asks) if state.asks else 0,
            'best_bid': state.best_bid,
            'best_ask': state.best_ask,
            'top_3_asks': state.asks[:3] if state.asks else []
        }

    def get_liquidity(self, token_id: str, side: str, max_price: float = None) -> float:
        """
        Get available liquidity for a token.

        Args:
            token_id: The token
            side: 'BUY' or 'SELL'
            max_price: Max price willing to pay (BUY) or min price to receive (SELL)

        Returns:
            Total shares available
        """
        state = self.prices.get(token_id)
        if not state:
            return 0.0
        return state.get_available_liquidity(side, max_price)


# Convenience function for testing
if __name__ == "__main__":
    import requests

    logging.basicConfig(level=logging.INFO)

    GAMMA_API = 'https://gamma-api.polymarket.com'
    now = int(time.time())
    start_timestamp = (now // 900) * 900
    slug = f'btc-updown-15m-{start_timestamp}'

    print(f"Market: {slug}")

    response = requests.get(f'{GAMMA_API}/events?slug={slug}', timeout=10)
    data = response.json()

    if data and data[0].get('markets'):
        import json as json_module
        market = data[0]['markets'][0]
        token_ids = json_module.loads(market.get('clobTokenIds', '[]'))
        up_token = token_ids[0]
        down_token = token_ids[1]

        print(f"UP token:   {up_token[:30]}...")
        print(f"DOWN token: {down_token[:30]}...")
        print()

        feed = WebSocketPriceFeed()
        if feed.start(up_token, down_token):
            print("WebSocket connected!")
            print()

            for i in range(15):
                up, down = feed.get_prices()
                age = feed.get_age()

                if up and down:
                    print(f"[{i}s] UP={up:.4f} DOWN={down:.4f} age={age:.3f}s spread={up+down-1:.4f}")
                else:
                    print(f"[{i}s] Waiting for prices... age={age:.3f}s")
                time.sleep(1)

            feed.stop()
            print("\nFeed stopped.")
        else:
            print("Failed to connect")
    else:
        print(f"No data for slug: {slug}")
