#!/usr/bin/env python3
"""
Orderbook Snapshot & Analysis

Connects to the Polymarket WebSocket feed, captures L2 book snapshots,
and computes spread, microprice, depth, and slippage estimates.

Usage:
    python orderbook_snapshot.py <event_slug>
    python orderbook_snapshot.py will-bitcoin-reach-100k
"""

import sys
import time
import json
import requests

from polymarket_sdk.websocket_feed import WebSocketPriceFeed


def resolve_tokens(slug: str) -> tuple:
    """Resolve an event slug to its YES and NO token IDs via Gamma API."""
    url = f"https://gamma-api.polymarket.com/events?slug={slug}"
    resp = requests.get(url, timeout=10)
    data = resp.json()

    if not data or not data[0].get("markets"):
        raise ValueError(f"No markets found for slug: {slug}")

    market = data[0]["markets"][0]
    token_ids = json.loads(market.get("clobTokenIds", "[]"))
    title = market.get("groupItemTitle", data[0].get("title", slug))

    if len(token_ids) < 2:
        raise ValueError(f"Market {slug} does not have two tokens")

    return token_ids[0], token_ids[1], title


def compute_microprice(bids: list, asks: list) -> float:
    """
    Compute size-weighted microprice.
    Weights the midpoint toward the thinner side of the book.
    """
    if not bids or not asks:
        return 0.0

    best_bid_price, best_bid_size = bids[0]
    best_ask_price, best_ask_size = asks[0]
    total = best_bid_size + best_ask_size

    if total == 0:
        return (best_bid_price + best_ask_price) / 2

    return (best_bid_price * best_ask_size + best_ask_price * best_bid_size) / total


def compute_depth(book_side: list, levels: int = 5) -> float:
    """Sum share volume across the top N levels."""
    return sum(size for _, size in book_side[:levels])


def print_snapshot(feed: WebSocketPriceFeed, yes_token: str, no_token: str):
    """Print a single orderbook snapshot with analytics."""
    yes_state = feed.prices.get(yes_token)
    no_state = feed.prices.get(no_token)

    if not yes_state or not yes_state.best_bid:
        print("  Waiting for YES book...")
        return

    spread = (yes_state.best_ask - yes_state.best_bid) if yes_state.best_ask else 0
    microprice = compute_microprice(yes_state.bids, yes_state.asks)
    bid_depth = compute_depth(yes_state.bids)
    ask_depth = compute_depth(yes_state.asks)

    # Slippage for a hypothetical 100-share market buy
    slip = yes_state.get_expected_fill_price("BUY", 100)

    print(f"  YES  bid={yes_state.best_bid:.4f}  ask={yes_state.best_ask:.4f}"
          f"  spread={spread:.4f}  microprice={microprice:.4f}")
    print(f"       bid_depth(5)={bid_depth:.0f}  ask_depth(5)={ask_depth:.0f}"
          f"  ratio={bid_depth / ask_depth:.2f}" if ask_depth > 0 else "")

    if slip:
        avg_price, total_cost, slippage = slip
        print(f"       100-share buy: avg_fill={avg_price:.4f}"
              f"  slippage={slippage:.4f} ({slippage / yes_state.best_ask * 100:.2f}%)")

    if no_state and no_state.best_bid:
        pair_sum = yes_state.mid_price + no_state.mid_price
        overround = pair_sum - 1.0
        print(f"  NO   bid={no_state.best_bid:.4f}  ask={no_state.best_ask:.4f}"
              f"  pair_sum={pair_sum:.4f}  overround={overround:+.4f}")

    print(f"  age={feed.get_age():.1f}s")


def main():
    if len(sys.argv) < 2:
        print("Usage: python orderbook_snapshot.py <event_slug>")
        print("Example: python orderbook_snapshot.py will-bitcoin-reach-100k")
        sys.exit(1)

    slug = sys.argv[1]
    print(f"Resolving tokens for: {slug}")

    yes_token, no_token, title = resolve_tokens(slug)
    print(f"Market: {title}")
    print(f"YES token: {yes_token[:40]}...")
    print(f"NO token:  {no_token[:40]}...")
    print()

    feed = WebSocketPriceFeed()
    if not feed.start(yes_token, no_token):
        print("Failed to connect to WebSocket feed.")
        sys.exit(1)

    print("Connected. Capturing snapshots every 2 seconds (Ctrl+C to stop):\n")

    try:
        for i in range(30):
            print(f"[snapshot {i + 1}]")
            print_snapshot(feed, yes_token, no_token)
            print()
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        feed.stop()


if __name__ == "__main__":
    main()
