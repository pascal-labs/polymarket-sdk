#!/usr/bin/env python3
"""
Market Scanner

Discovers active Polymarket events, fetches orderbook data for each,
computes basic opportunity metrics, and ranks markets by tradability.

Metrics computed per market:
  - Volume (24h USDC traded)
  - Spread (best ask - best bid)
  - Bid depth (shares resting on top 3 bid levels)
  - Ask depth (shares resting on top 3 ask levels)
  - Score: composite ranking (lower spread + higher depth = better)

Usage:
    python market_scanner.py
    python market_scanner.py --limit 20
"""

import sys
import json
import requests
from polymarket_sdk.http_pool import pooled_get


GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


def fetch_active_events(limit: int = 50) -> list:
    """Fetch active, non-closed events from the Gamma API."""
    params = {"active": "true", "closed": "false", "limit": limit}
    resp = pooled_get(f"{GAMMA_API}/events", params=params, timeout=10)
    return resp.json()


def fetch_orderbook(token_id: str) -> dict:
    """Fetch L2 orderbook for a token from the CLOB API."""
    try:
        resp = pooled_get(f"{CLOB_API}/book", params={"token_id": token_id}, timeout=5)
        return resp.json()
    except Exception:
        return {"bids": [], "asks": []}


def score_market(spread: float, bid_depth: float, ask_depth: float, volume: float) -> float:
    """
    Composite opportunity score.  Higher is better.

    Rewards: tight spreads, deep books, high volume.
    A market with 1-cent spread and 1000 shares of depth scores
    much higher than one with 10-cent spread and 50 shares.
    """
    if spread <= 0 or (bid_depth + ask_depth) == 0:
        return 0.0

    depth = bid_depth + ask_depth
    spread_score = 1.0 / spread           # Tighter = better
    depth_score = min(depth / 500, 5.0)   # Diminishing returns above 500
    volume_score = min(volume / 10000, 3.0)

    return spread_score * depth_score * volume_score


def analyze_market(event: dict) -> list:
    """Analyze all markets within an event and return scored results."""
    results = []
    title = event.get("title", "Unknown")
    volume = float(event.get("volume", 0))

    for market in event.get("markets", []):
        if market.get("closed") or not market.get("active"):
            continue

        token_ids = json.loads(market.get("clobTokenIds", "[]"))
        if not token_ids:
            continue

        yes_token = token_ids[0]
        book = fetch_orderbook(yes_token)

        bids = book.get("bids", [])
        asks = book.get("asks", [])

        if not bids or not asks:
            continue

        best_bid = float(bids[0]["price"])
        best_ask = float(asks[0]["price"])
        spread = best_ask - best_bid

        bid_depth = sum(float(b["size"]) for b in bids[:3])
        ask_depth = sum(float(a["size"]) for a in asks[:3])

        opportunity = score_market(spread, bid_depth, ask_depth, volume)

        label = market.get("groupItemTitle", title)

        results.append({
            "event": title,
            "label": label,
            "yes_price": (best_bid + best_ask) / 2,
            "spread": spread,
            "bid_depth": bid_depth,
            "ask_depth": ask_depth,
            "volume": volume,
            "score": opportunity,
            "token_id": yes_token,
        })

    return results


def main():
    limit = 20
    if "--limit" in sys.argv:
        idx = sys.argv.index("--limit")
        limit = int(sys.argv[idx + 1]) if idx + 1 < len(sys.argv) else 20

    print(f"Scanning {limit} active Polymarket events...\n")

    events = fetch_active_events(limit)
    print(f"Found {len(events)} events. Fetching orderbooks...\n")

    all_markets = []
    for event in events:
        all_markets.extend(analyze_market(event))

    # Rank by composite score
    all_markets.sort(key=lambda m: m["score"], reverse=True)

    # Display results
    print(f"{'Rank':<5} {'Score':>6} {'Spread':>7} {'BidDpth':>8} {'AskDpth':>8}"
          f" {'Volume':>10} {'Price':>6}  {'Market'}")
    print("-" * 90)

    for i, m in enumerate(all_markets[:25], 1):
        label = m["label"][:40] if len(m["label"]) > 40 else m["label"]
        print(f"{i:<5} {m['score']:>6.1f} {m['spread']:>7.4f} {m['bid_depth']:>8.0f}"
              f" {m['ask_depth']:>8.0f} {m['volume']:>10,.0f} {m['yes_price']:>5.2f}"
              f"  {label}")

    if all_markets:
        top = all_markets[0]
        print(f"\nBest opportunity: {top['label']}")
        print(f"  Spread: {top['spread']:.4f}  Depth: {top['bid_depth']:.0f}/{top['ask_depth']:.0f}")
        print(f"  Token: {top['token_id'][:50]}...")


if __name__ == "__main__":
    main()
