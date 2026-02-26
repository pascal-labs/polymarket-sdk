"""
Polymarket API client for automated trading
Uses py-clob-client for order placement
"""

import os
import json
from datetime import datetime
import requests

# Connection pooling for faster HTTP requests
from .http_pool import pooled_get

VERBOSE = False

class PolymarketClient:
    def __init__(self, private_key=None, mode='paper'):
        """
        Initialize Polymarket client

        Args:
            private_key: Polygon wallet private key (only needed for live trading)
            mode: 'paper' or 'live'
        """
        self.mode = mode
        self.api_url = "https://gamma-api.polymarket.com"
        self.clob_url = "https://clob.polymarket.com"

        # Cache for min_order_size per token_id
        self._min_order_size_cache = {}

        if mode == 'live':
            if not private_key:
                raise ValueError("Private key required for live trading")

            try:
                from py_clob_client.client import ClobClient
                from py_clob_client.clob_types import ApiCreds
                from eth_account import Account
                import os

                # Derive wallet address from private key
                account = Account.from_key(private_key)
                self.wallet_address = account.address

                self.client = ClobClient(
                    host=self.clob_url,
                    key=private_key,
                    chain_id=137,  # Polygon mainnet
                    signature_type=0,  # 0 for EOA wallets
                    funder=self.wallet_address
                )

                # Set API credentials - use env vars if available, otherwise derive
                api_key = os.getenv('POLYMARKET_API_KEY')
                api_secret = os.getenv('POLYMARKET_API_SECRET')
                api_passphrase = os.getenv('POLYMARKET_API_PASSPHRASE')

                if api_key and api_secret and api_passphrase:
                    self.creds = ApiCreds(
                        api_key=api_key,
                        api_secret=api_secret,
                        api_passphrase=api_passphrase
                    )
                    print("✅ Using provided API credentials")
                else:
                    self.creds = self.client.create_or_derive_api_creds()
                    print("✅ Derived API credentials from wallet")

                self.client.set_api_creds(self.creds)

                print("✅ Connected to Polymarket CLOB")
                print(f"   Wallet: {self.wallet_address[:6]}...{self.wallet_address[-4:]}")
            except ImportError:
                print("⚠️  py-clob-client not installed. Run: pip install py-clob-client")
                self.mode = 'paper'
                self.creds = None
        else:
            print("📝 Running in PAPER TRADING mode")
            self.creds = None

    def get_market_prices(self, slug):
        """
        Get current market prices for all bins in an event
        Uses REAL-TIME midpoint prices from CLOB orderbook instead of stale API data
        Fetches midpoints in PARALLEL for speed (26 bins = 26 API calls!)

        Returns: dict of {bin: {'bid': X, 'ask': Y, 'last': Z}}
        """
        url = f"{self.api_url}/events?slug={slug}"
        response = pooled_get(url, timeout=10)
        data = response.json()

        if not data:
            return {}

        event = data[0]
        prices = {}

        # Phase 1: Build market data with token IDs
        markets_data = []
        for market in event['markets']:
            bin_name = market.get('groupItemTitle', 'Other')
            outcome_prices = json.loads(market['outcomePrices'])
            api_yes_price = float(outcome_prices[0])

            clob_token_ids = json.loads(market.get('clobTokenIds', '[]')) if market.get('clobTokenIds') else []
            yes_token_id = clob_token_ids[0] if len(clob_token_ids) > 0 else None
            no_token_id = clob_token_ids[1] if len(clob_token_ids) > 1 else None

            markets_data.append({
                'bin_name': bin_name,
                'api_yes_price': api_yes_price,
                'yes_token_id': yes_token_id,
                'no_token_id': no_token_id,
                'condition_id': market.get('conditionId'),
                'active': market['active'],
                'closed': market['closed']
            })

        # Phase 2: Fetch ALL midpoints in PARALLEL (massive speedup!)
        if self.mode == 'live':
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import threading

            midpoint_prices = {}
            midpoint_lock = threading.Lock()

            def fetch_midpoint(token_id, fallback_price):
                """Fetch midpoint for a single token (runs in thread)"""
                try:
                    mid_data = self.client.get_midpoint(token_id)
                    return token_id, float(mid_data.get('mid', fallback_price)), None
                except Exception as e:
                    return token_id, fallback_price, e

            # Fetch all midpoints in parallel (10 concurrent workers)
            with ThreadPoolExecutor(max_workers=10) as executor:
                futures = {
                    executor.submit(fetch_midpoint, m['yes_token_id'], m['api_yes_price']): m['yes_token_id']
                    for m in markets_data if m['yes_token_id']
                }

                for future in as_completed(futures):
                    token_id, price, error = future.result()
                    with midpoint_lock:
                        midpoint_prices[token_id] = price

        # Phase 3: Build final prices dict
        for market_data in markets_data:
            token_id = market_data['yes_token_id']

            # Use parallel-fetched midpoint or fallback to API price
            if self.mode == 'live' and token_id and token_id in midpoint_prices:
                yes_price = midpoint_prices[token_id]
            else:
                yes_price = market_data['api_yes_price']

            prices[market_data['bin_name']] = {
                'yes_price': yes_price,
                'bid': yes_price - 0.001,
                'ask': yes_price + 0.001,
                'last': yes_price,
                'token_id': token_id,
                'yes_token_id': token_id,
                'no_token_id': market_data['no_token_id'],
                'condition_id': market_data['condition_id'],
                'active': market_data['active'],
                'closed': market_data['closed']
            }

        return prices

    def get_orderbook(self, token_id):
        """
        Get orderbook depth for a specific token
        Also caches min_order_size for later validation

        Returns: {'bids': [...], 'asks': [...], 'min_order_size': '5.0'}
        """
        if self.mode == 'paper':
            # In paper mode, return mock orderbook
            return {
                'bids': [{'price': 0.10, 'size': 100}, {'price': 0.09, 'size': 200}],
                'asks': [{'price': 0.11, 'size': 100}, {'price': 0.12, 'size': 150}],
                'min_order_size': '0.0'  # No minimum in paper mode
            }

        try:
            orderbook = self.client.get_order_book(token_id)

            # py-clob-client returns OrderBookSummary object, not dict
            # Check if it has min_order_size attribute
            if hasattr(orderbook, 'min_order_size') and orderbook.min_order_size:
                self._min_order_size_cache[token_id] = float(orderbook.min_order_size)
            elif isinstance(orderbook, dict) and 'min_order_size' in orderbook:
                # Fallback for dict response
                self._min_order_size_cache[token_id] = float(orderbook['min_order_size'])

            return orderbook
        except Exception as e:
            error_msg = str(e)
            # Check if orderbook doesn't exist (market resolved/closed)
            if 'orderbook' in error_msg.lower() and ('does not exist' in error_msg.lower() or '404' in error_msg):
                # Re-raise so caller can catch and handle appropriately
                print(f"❌ Error fetching orderbook: {e}")
                raise
            else:
                # Other errors - return empty orderbook as fallback
                print(f"❌ Error fetching orderbook: {e}")
                return {'bids': [], 'asks': [], 'min_order_size': '0.0'}

    def get_min_order_size(self, token_id):
        """
        Get minimum order size for a token (cached or fetch from orderbook)

        Args:
            token_id: Token ID

        Returns: min_order_size as float (in shares)
        """
        # Check cache first
        if token_id in self._min_order_size_cache:
            return self._min_order_size_cache[token_id]

        # Not in cache - fetch from orderbook
        if self.mode == 'paper':
            return 0.0

        try:
            orderbook = self.get_orderbook(token_id)

            # Extract min_order_size from orderbook (object or dict)
            if hasattr(orderbook, 'min_order_size'):
                min_size = float(orderbook.min_order_size) if orderbook.min_order_size else 0.0
            elif isinstance(orderbook, dict):
                min_size = float(orderbook.get('min_order_size', 0.0))
            else:
                min_size = 0.0

            self._min_order_size_cache[token_id] = min_size
            return min_size
        except Exception as e:
            error_msg = str(e)
            # Re-raise orderbook errors (dead markets) so caller can handle
            if 'orderbook' in error_msg.lower() and ('does not exist' in error_msg.lower() or '404' in error_msg):
                raise
            # Other errors - return default
            print(f"⚠️  Warning: Could not fetch min_order_size for {token_id[:20]}...: {e}")
            return 0.0  # Default to no minimum if we can't fetch

    def place_order(self, token_id, side, size, price=None, order_type='MARKET'):
        """
        Place an order

        Args:
            token_id: Token ID from market data
            side: 'BUY' or 'SELL'
            size: Dollar amount for BUY, shares for SELL (e.g., 1.0 = $1 or 1 share)
            price: Limit price (optional, for limit orders)
            order_type: 'MARKET' or 'LIMIT'

        Returns: order_id or None (or 'MIN_SIZE_VIOLATION' if order too small)
        """
        if self.mode == 'paper':
            order_id = f"PAPER_{datetime.now().timestamp()}"
            print(f"📝 PAPER TRADE: {side} ${size:.2f} @ {price if price else 'MARKET'}")
            print(f"   Order ID: {order_id}")
            return order_id

        # Validate minimum order size BEFORE placing
        min_size = self.get_min_order_size(token_id)
        if min_size > 0:
            # Convert size to shares for comparison
            if side == 'BUY':
                if order_type == 'LIMIT' and price and price > 0:
                    shares_to_order = size / price
                else:
                    # For MARKET BUY, we can't validate until we know execution price
                    # So we skip validation for MARKET BUY orders
                    shares_to_order = None
            else:
                # SELL already uses shares
                shares_to_order = size

            # Check minimum (only for LIMIT orders and SELL orders)
            if shares_to_order is not None and shares_to_order < min_size:
                print(f"⚠️  Order too small: {shares_to_order:.2f} shares < minimum {min_size:.0f} shares")
                print(f"   Token: {token_id[:20]}...")
                print(f"   Skipping order placement")
                return 'MIN_SIZE_VIOLATION'

        try:
            from py_clob_client.clob_types import OrderArgs, MarketOrderArgs, OrderType

            if order_type == 'MARKET':
                # Market orders use MarketOrderArgs with 'amount'
                # For BUY: amount = dollars to spend (max 2 decimals)
                # For SELL: amount = number of shares to sell (max 4 decimals)
                if side == 'BUY':
                    size = round(size, 2)
                else:
                    size = round(size, 4)
                # Builder address for fee rebates
                builder_addr = os.getenv('POLYMARKET_BUILDER_ADDRESS')
                signed_order = self.client.create_market_order(
                    MarketOrderArgs(
                        token_id=token_id,
                        amount=size,
                        side=side,
                        order_type=OrderType.FOK,
                        taker=builder_addr if builder_addr else None
                    )
                )
                # Post the signed order
                resp = self.client.post_order(signed_order, OrderType.FOK)
            else:
                # Limit orders use OrderArgs with 'size' = number of shares
                # For BUY: convert dollar amount to shares
                # For SELL: size is already in shares
                if side == 'BUY':
                    if not price or price <= 0:
                        raise ValueError("Price required for BUY limit orders")
                    shares = size / price  # Convert dollars to shares
                    dollar_amount = size
                else:
                    shares = size  # SELL already uses shares
                    dollar_amount = size * price if price else size

                # API limits: makerAmount must have max 2 decimals
                # Integer shares × price always gives max 2 decimals
                # Round UP to not leave money on the table
                import math
                price = round(price, 2)
                shares = math.ceil(shares)  # Round UP to next whole share

                # Builder address for fee rebates
                builder_addr = os.getenv('POLYMARKET_BUILDER_ADDRESS')
                if builder_addr:
                    signed_order = self.client.create_order(
                        OrderArgs(
                            token_id=token_id,
                            size=shares,
                            price=price,
                            side=side,
                            taker=builder_addr
                        )
                    )
                else:
                    signed_order = self.client.create_order(
                        OrderArgs(
                            token_id=token_id,
                            size=shares,
                            price=price,
                            side=side
                        )
                    )
                # Post the signed order - use FOK/FAK if specified, otherwise GTC
                if order_type == 'FOK':
                    resp = self.client.post_order(signed_order, OrderType.FOK)
                elif order_type == 'FAK':
                    resp = self.client.post_order(signed_order, OrderType.FAK)
                else:
                    resp = self.client.post_order(signed_order, OrderType.GTC)

            # Display what was actually ordered
            if order_type == 'LIMIT' and side == 'BUY':
                print(f"✅ ORDER PLACED: {side} {shares:.2f} shares (${dollar_amount:.2f}) @ ${price:.4f}")
            else:
                unit = "shares" if side == 'SELL' else "$"
                print(f"✅ ORDER PLACED: {side} {size:.2f} {unit}")
            print(f"   Order ID: {resp['orderID']}")
            return resp['orderID']

        except Exception as e:
            unit = "shares" if side == 'SELL' else "$"
            print(f"❌ Order failed: {side} {size:.2f} {unit}")
            print(f"   Error: {e}")
            print(f"   Token: {token_id[:20]}...")
            return None

    def cancel_order(self, order_id, silent=False):
        """Cancel an open order"""
        if self.mode == 'paper':
            return True

        try:
            self.client.cancel(order_id)
            if not silent:
                print(f"✅ Cancelled 1 order")
            return True
        except Exception as e:
            if not silent:
                print(f"❌ Cancel failed: {e}")
            return False

    def cancel_orders_batch(self, order_ids: list) -> bool:
        """Cancel multiple orders in a single API call"""
        if self.mode == 'paper':
            return True
        if not order_ids:
            return True

        try:
            self.client.cancel_orders(order_ids)
            print(f"✅ Cancelled {len(order_ids)} orders")
            return True
        except Exception as e:
            print(f"❌ Batch cancel failed ({len(order_ids)} orders): {e}")
            # Fallback: cancel individually
            ok = 0
            for oid in order_ids:
                try:
                    self.client.cancel(oid)
                    ok += 1
                except Exception:
                    pass
            if ok:
                print(f"✅ Cancelled {ok}/{len(order_ids)} orders (fallback)")
            return False

    def place_orders_batch(self, orders: list) -> list:
        """
        Sign and place multiple orders in a batch.

        Args:
            orders: list of {token_id, side, size, price} dicts
                    size = dollar cost for BUY, shares for SELL

        Returns: list of order_id strings (None for failures)
        """
        if self.mode == 'paper':
            return [f"PAPER_{i}" for i in range(len(orders))]
        if not orders:
            return []

        from py_clob_client.clob_types import OrderArgs, OrderType
        import math

        builder_addr = os.getenv('POLYMARKET_BUILDER_ADDRESS')
        results = []

        for order in orders:
            try:
                price = round(order['price'], 2)
                if order.get('size_in_shares'):
                    shares = order['size']  # Already in shares, no conversion
                elif order['side'] == 'BUY':
                    shares = math.ceil(order['size'] / price)
                else:
                    shares = order['size']

                if builder_addr:
                    args = OrderArgs(
                        token_id=order['token_id'],
                        size=shares,
                        price=price,
                        side=order['side'],
                        taker=builder_addr,
                    )
                else:
                    args = OrderArgs(
                        token_id=order['token_id'],
                        size=shares,
                        price=price,
                        side=order['side'],
                    )

                signed = self.client.create_order(args)
                r = self.client.post_order(signed, OrderType.GTC)
                oid = r.get('orderID') if isinstance(r, dict) else getattr(r, 'orderID', None)
                results.append(oid)
            except Exception as e:
                err_str = str(e).lower()
                if 'balance' in err_str or 'allowance' in err_str:
                    # Suppress balance errors — caller tracks these
                    results.append(None)
                else:
                    print(f"❌ Order failed: {e}")
                    results.append(None)

        return results

    def place_orders_true_batch(self, orders: list, order_type: str = 'GTC') -> list:
        """
        Sign and place multiple orders using the true batch API (client.post_orders).
        Sends in chunks of 15 (API limit per call).

        Args:
            orders: list of {token_id, side, size, price} dicts
                    size is always in shares (not dollars)
            order_type: 'GTC' (default), 'FOK', or 'FAK'

        Returns: list of order_id strings (None for failures), same length as input
        """
        if self.mode == 'paper':
            return [f"PAPER_{i}_{datetime.now().timestamp()}" for i in range(len(orders))]
        if not orders:
            return []

        from py_clob_client.clob_types import OrderArgs, OrderType, PostOrdersArgs
        import math

        otype = getattr(OrderType, order_type, OrderType.GTC)
        builder_addr = os.getenv('POLYMARKET_BUILDER_ADDRESS')

        # Step 1: Sign all orders locally
        signed_orders = []
        for order in orders:
            try:
                price = round(order['price'], 2)
                shares = int(order['size'])

                kwargs = dict(
                    token_id=order['token_id'],
                    size=shares,
                    price=price,
                    side=order['side'],
                )
                if builder_addr:
                    kwargs['taker'] = builder_addr

                signed = self.client.create_order(OrderArgs(**kwargs))
                signed_orders.append(PostOrdersArgs(order=signed, orderType=otype))
            except Exception as e:
                print(f"❌ Sign failed: {e}")
                signed_orders.append(None)

        # Step 2: Send in chunks of 15
        CHUNK_SIZE = 15
        results = [None] * len(orders)

        for chunk_start in range(0, len(signed_orders), CHUNK_SIZE):
            chunk_end = min(chunk_start + CHUNK_SIZE, len(signed_orders))
            chunk = []
            chunk_indices = []

            for i in range(chunk_start, chunk_end):
                if signed_orders[i] is not None:
                    chunk.append(signed_orders[i])
                    chunk_indices.append(i)

            if not chunk:
                continue

            try:
                resp = self.client.post_orders(chunk)

                # Response is a list of dicts: [{"orderID": "...", "success": true, ...}, ...]
                if isinstance(resp, list):
                    for j, idx in enumerate(chunk_indices):
                        if j < len(resp):
                            item = resp[j]
                            if isinstance(item, dict):
                                if item.get('success', True):
                                    results[idx] = item.get('orderID')
                                else:
                                    err = item.get('errorMsg', 'unknown')
                                    print(f"    Order {j} rejected: {err}")
                            elif isinstance(item, str):
                                results[idx] = item
                elif isinstance(resp, dict):
                    # Single-order fallback format
                    oid = resp.get('orderID')
                    if oid and chunk_indices:
                        results[chunk_indices[0]] = oid
            except Exception as e:
                print(f"❌ Batch post failed (chunk {chunk_start}-{chunk_end}): {e}")
                # Fallback: post individually for this chunk
                for j, idx in enumerate(chunk_indices):
                    try:
                        r = self.client.post_order(signed_orders[idx].order, otype)
                        oid = r.get('orderID') if isinstance(r, dict) else getattr(r, 'orderID', None)
                        results[idx] = oid
                    except Exception as e2:
                        err_str = str(e2).lower()
                        if 'balance' not in err_str and 'allowance' not in err_str:
                            print(f"❌ Individual fallback failed [{idx}]: {e2}")
                        results[idx] = None

        return results

    def cancel_market_orders(self, token_id: str) -> bool:
        """Cancel all orders for a specific token (asset_id) in one API call."""
        if self.mode == 'paper':
            return True

        try:
            self.client.cancel_market_orders(asset_id=token_id)
            return True
        except Exception as e:
            print(f"❌ Cancel market orders failed: {e}")
            return False

    def refresh_balance(self):
        """Refresh the cached balance on Polymarket's API"""
        if self.mode == 'paper':
            return

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            # This forces Polymarket to update their cache
            self.client.update_balance_allowance(params)
            print("✅ Balance cache refreshed")
        except Exception as e:
            print(f"⚠️  Warning: Could not refresh balance cache: {e}")

    def get_balances(self):
        """Get USDC balance and allowance"""
        if self.mode == 'paper':
            return {'USDC': 500.0}  # Mock balance

        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType

            # Get USDC balance (collateral)
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            result = self.client.get_balance_allowance(params)

            # Convert from wei (6 decimals for USDC)
            balance_usdc = float(result.get('balance', 0)) / 1e6

            return {
                'USDC': balance_usdc,
                'allowance': float(result.get('allowance', 0)) / 1e6
            }
        except Exception as e:
            print(f"❌ Error fetching balances: {e}")
            return {}

    def get_open_positions(self):
        """
        Get all open orders from Polymarket

        Returns: list of open orders with token_id, size, price
        """
        if self.mode == 'paper':
            print("📝 Paper mode: No real positions to fetch")
            return []

        try:
            from py_clob_client.clob_types import OpenOrderParams

            # Get all open orders (no filtering)
            open_orders = self.client.get_orders(OpenOrderParams())

            positions = []
            for order in open_orders:
                positions.append({
                    'order_id': order.get('id'),
                    'token_id': order.get('asset_id'),
                    'side': order.get('side'),
                    'size': float(order.get('size', 0)),
                    'price': float(order.get('price', 0)),
                    'status': order.get('status'),
                    'timestamp': order.get('created_at')
                })

            return positions

        except Exception as e:
            print(f"❌ Error fetching positions: {e}")
            return []

    def get_positions_from_data_api(self):
        """
        Get actual positions with average entry prices from Polymarket Data API
        https://docs.polymarket.com/developers/misc-endpoints/data-api-get-positions

        Returns: list of positions with token_id, shares, avg_entry_price, pnl, current_value
        """
        if self.mode == 'paper':
            return []

        try:
            url = f"https://data-api.polymarket.com/positions?user={self.wallet_address}"
            response = pooled_get(url, timeout=10)
            response.raise_for_status()

            raw_data = response.json()

            positions = []
            for pos in raw_data:
                # Only include positions with non-zero size
                size = float(pos.get('size', 0))
                if size > 0:
                    positions.append({
                        'token_id': pos.get('asset'),  # Field is 'asset', not 'asset_id'
                        'market_id': pos.get('conditionId'),
                        'shares': size,
                        'value': float(pos.get('currentValue', 0)),  # Current value at market price
                        'avg_entry_price': float(pos.get('avgPrice', 0)),  # Field is 'avgPrice'
                        'pnl': float(pos.get('cashPnl', 0)),  # Real P&L from Polymarket
                        'side': pos.get('outcome')  # YES or NO
                    })

            return positions

        except Exception as e:
            print(f"❌ Error fetching positions from Data API: {e}")
            return []

    def get_position_value(self, token_id):
        """
        Get current value of a position (shares owned * current price)
        Uses the Data API to get actual position size.

        Args:
            token_id: The token to check

        Returns: (shares_owned, current_value, current_price)
        """
        if self.mode == 'paper':
            return 0, 0.0, 0.0

        try:
            # Get positions from Data API
            positions = self.get_positions_from_data_api()

            # Find the position for this token
            shares_owned = 0
            for pos in positions:
                if pos.get('token_id') == token_id:
                    shares_owned = pos.get('shares', 0)
                    break

            if shares_owned > 0:
                # Get current market price
                orderbook = self.get_orderbook(token_id)
                # Handle both OrderBookSummary object and dict
                if hasattr(orderbook, 'bids'):
                    bids = orderbook.bids
                else:
                    bids = orderbook.get('bids', [])

                current_price = 0
                if bids and len(bids) > 0:
                    bid = bids[0]
                    if hasattr(bid, 'price'):
                        current_price = float(bid.price)
                    elif isinstance(bid, dict):
                        current_price = float(bid.get('price', 0))

                current_value = shares_owned * current_price
                return shares_owned, current_value, current_price

            return 0, 0.0, 0.0

        except Exception as e:
            print(f"❌ Error fetching position value: {e}")
            return 0, 0.0, 0.0

    def get_trades(self, params=None):
        """
        Get trade history

        Args:
            params: TradeParams object (optional)

        Returns: list of trades
        """
        if self.mode == 'paper':
            return []

        try:
            return self.client.get_trades(params if params else {})
        except Exception as e:
            print(f"❌ Error fetching trades: {e}")
            return []

    def get_balance_allowance(self, params):
        """
        Get balance and allowance for an asset

        Args:
            params: BalanceAllowanceParams object

        Returns: dict with 'balance' and 'allowance'
        """
        if self.mode == 'paper':
            return {'balance': '0', 'allowance': '0'}

        try:
            return self.client.get_balance_allowance(params)
        except Exception as e:
            print(f"❌ Error fetching balance/allowance: {e}")
            return {'balance': '0', 'allowance': '0'}

    def get_orders(self, params):
        """
        Get open orders

        Args:
            params: OpenOrderParams object

        Returns: list of orders
        """
        if self.mode == 'paper':
            return []

        try:
            return self.client.get_orders(params)
        except Exception as e:
            print(f"❌ Error fetching orders: {e}")
            return []

    def get_order_fill(self, order_id: str) -> float:
        """
        Get filled shares for an order from CLOB API.

        Args:
            order_id: The order hash/ID

        Returns: size_matched (filled shares) or 0 on error
        """
        if self.mode == 'paper':
            return 0

        try:
            order = self.client.get_order(order_id)
            if order and 'size_matched' in order:
                return float(order['size_matched'])
            return 0
        except Exception as e:
            print(f"   ⚠️ Could not get order fill: {e}")
            return 0

    def get_order_fill_details(self, order_id: str) -> tuple:
        """
        Get filled shares AND average fill price from CLOB API.

        Args:
            order_id: The order hash/ID

        Returns: (size_matched, avg_price) or (0, None) on error
        """
        if self.mode == 'paper':
            return (0, None)

        try:
            order = self.client.get_order(order_id)
            if order:
                size_matched = float(getattr(order, 'size_matched', 0) if hasattr(order, 'size_matched') else order.get('size_matched', 0))

                # Get trade IDs from associate_trades
                trade_ids = getattr(order, 'associate_trades', None) if hasattr(order, 'associate_trades') else order.get('associate_trades')

                avg_price = None
                if trade_ids and isinstance(trade_ids, list) and len(trade_ids) > 0:
                    # Fetch each trade and get maker_orders for actual fill prices
                    total_size = 0
                    total_value = 0
                    for trade_id in trade_ids:
                        try:
                            # Fetch trade by ID
                            trades = self.client.get_trades({'id': trade_id})
                            if trades and len(trades) > 0:
                                trade = trades[0]
                                # Debug: show trade structure
                                trade_price = trade.get('price') if isinstance(trade, dict) else getattr(trade, 'price', None)
                                trade_size = trade.get('size') if isinstance(trade, dict) else getattr(trade, 'size', None)
                                print(f"    [TRADE] id={trade_id[:20]}... price={trade_price} size={trade_size}")

                                # Get maker_orders which contain individual fills
                                maker_orders = getattr(trade, 'maker_orders', None) if hasattr(trade, 'maker_orders') else trade.get('maker_orders', [])
                                if maker_orders:
                                    for mo in maker_orders:
                                        mo_size = float(mo.get('matched_amount', 0) if isinstance(mo, dict) else getattr(mo, 'matched_amount', 0))
                                        mo_price = float(mo.get('price', 0) if isinstance(mo, dict) else getattr(mo, 'price', 0))
                                        print(f"    [MAKER] price={mo_price} size={mo_size}")
                                        if mo_size > 0 and mo_price > 0:
                                            total_size += mo_size
                                            total_value += mo_size * mo_price
                                else:
                                    # No maker_orders - use trade's price and size directly
                                    if trade_price and trade_size:
                                        t_price = float(trade_price)
                                        t_size = float(trade_size)
                                        if t_size > 0 and t_price > 0:
                                            total_size += t_size
                                            total_value += t_size * t_price
                                            print(f"    [FALLBACK] Using trade price={t_price} size={t_size}")
                        except Exception as te:
                            print(f"    [TRADE ERROR] {te}")

                    if total_size > 0:
                        avg_price = total_value / total_size

                return (size_matched, avg_price)
            return (0, None)
        except Exception as e:
            print(f"   ⚠️ Could not get order fill details: {e}")
            return (0, None)

    def get_market(self, market_id):
        """
        Get market information

        Args:
            market_id: Market ID

        Returns: market data dict
        """
        if self.mode == 'paper':
            return {'description': 'Paper Mode Market'}

        try:
            return self.client.get_market(market_id)
        except Exception as e:
            print(f"❌ Error fetching market: {e}")
            return {'description': 'Unknown'}

    def get_midpoint(self, token_id):
        """
        Get midpoint price for a token

        Args:
            token_id: Token ID

        Returns: midpoint price as string
        """
        if self.mode == 'paper':
            return "0.50"

        try:
            return self.client.get_midpoint(token_id)
        except Exception as e:
            print(f"❌ Error fetching midpoint: {e}")
            return "0.00"
