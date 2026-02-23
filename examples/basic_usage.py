"""Basic usage example for polymarket-sdk."""

import os
from polymarket_sdk import PolymarketClient

# Initialize client (paper mode for testing)
client = PolymarketClient(mode='paper')

# Fetch market prices
prices = client.get_market_prices("will-bitcoin-reach-100k")
print(f"Market prices: {prices}")

# Get orderbook
# orderbook = client.get_orderbook(token_id="your_token_id")

# Check balances (requires wallet)
# client = PolymarketClient(private_key=os.getenv("POLYGON_PRIVATE_KEY"), mode='live')
# balance = client.get_balances()
# print(f"USDC Balance: {balance}")
