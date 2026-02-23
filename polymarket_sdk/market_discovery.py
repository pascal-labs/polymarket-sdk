"""
Dynamic Market Discovery
Automatically finds all active Elon Musk tweet markets on Polymarket
"""

import requests
import re
from datetime import datetime, timedelta
from typing import List, Dict, Optional

class MarketDiscovery:
    def __init__(self):
        self.api_url = "https://gamma-api.polymarket.com"

    def search_elon_markets(self) -> List[Dict]:
        """
        Search for all Elon Musk tweet count markets

        Returns: List of market dictionaries with:
            - slug
            - title
            - start_date
            - end_date
            - active
            - closed
        """
        try:
            # Search API endpoint
            search_url = f"{self.api_url}/events"
            params = {
                'active': 'true',
                'closed': 'false',
                'limit': 100
            }

            response = requests.get(search_url, params=params, timeout=10)
            events = response.json()

            # Filter for Elon tweet markets
            elon_markets = []

            for event in events:
                title = event.get('title', '').lower()
                slug = event.get('slug', '')

                # Look for Elon Musk tweet count markets
                if 'elon' in title and 'tweet' in title and 'october' in title:
                    # Parse dates from title
                    start_date, end_date = self.parse_dates_from_title(event['title'])

                    elon_markets.append({
                        'slug': slug,
                        'title': event['title'],
                        'description': event.get('description', ''),
                        'start_date': start_date,
                        'end_date': end_date,
                        'active': event.get('active', False),
                        'closed': event.get('closed', False),
                        'volume': event.get('volume', 0),
                        'liquidity': event.get('liquidity', 0),
                        'tweet_count': event.get('tweetCount'),
                    })

            # Sort by start date
            elon_markets.sort(key=lambda x: x['start_date'] if x['start_date'] else datetime.max)

            return elon_markets

        except Exception as e:
            print(f"❌ Market discovery error: {e}")
            return []

    def parse_dates_from_title(self, title: str) -> tuple:
        """
        Parse start and end dates from market title

        Examples:
            "Elon Musk # tweets October 21 - October 28, 2025?"
            "Elon Musk # tweets October 2025?"

        Returns: (start_date, end_date) or (None, None)
        """
        try:
            # Pattern 1: "October 21 - October 28, 2025"
            pattern1 = r'(\w+ \d+)\s*-\s*(\w+ \d+),?\s*(\d{4})'
            match = re.search(pattern1, title)

            if match:
                start_str = f"{match.group(1)}, {match.group(3)}"
                end_str = f"{match.group(2)}, {match.group(3)}"

                start_date = datetime.strptime(start_str, "%B %d, %Y")
                end_date = datetime.strptime(end_str, "%B %d, %Y")

                # Markets typically close at 12:00 PM ET
                start_date = start_date.replace(hour=12, minute=0)
                end_date = end_date.replace(hour=12, minute=0)

                return start_date, end_date

            # Pattern 2: "October 2025" (monthly market)
            pattern2 = r'(\w+)\s+(\d{4})'
            match = re.search(pattern2, title)

            if match:
                month_str = f"{match.group(1)} 1, {match.group(2)}"
                start_date = datetime.strptime(month_str, "%B %d, %Y")

                # End date is last day of month
                next_month = start_date.replace(day=28) + timedelta(days=4)
                end_date = next_month - timedelta(days=next_month.day)

                start_date = start_date.replace(hour=12, minute=0)
                end_date = end_date.replace(hour=23, minute=59)

                return start_date, end_date

            return None, None

        except Exception as e:
            print(f"⚠️  Could not parse dates from: {title} ({e})")
            return None, None

    def get_active_windows(self) -> Dict[str, tuple]:
        """
        Get all active trading windows

        Returns: {
            'W1': (start_date, end_date, slug),
            'W2': (start_date, end_date, slug),
            ...
        }
        """
        markets = self.search_elon_markets()

        now = datetime.now()
        windows = {}

        # Filter for markets that:
        # 1. Have started or will start within 7 days
        # 2. Haven't closed yet
        # 3. Are weekly windows (7 days)

        window_idx = 1

        for market in markets:
            if not market['start_date'] or not market['end_date']:
                continue

            # Check if market is relevant (starts within 7 days and hasn't ended)
            days_until_start = (market['start_date'] - now).days
            has_ended = market['end_date'] < now

            if has_ended:
                continue

            # Only include markets starting within next 7 days or already started
            if days_until_start > 7:
                continue

            # Calculate duration
            duration = (market['end_date'] - market['start_date']).days

            # Categorize by duration
            if duration <= 7:  # Weekly windows
                window_key = f"W{window_idx}"
                windows[window_key] = (
                    market['start_date'],
                    market['end_date'],
                    market['slug']
                )
                window_idx += 1
            elif duration >= 28:  # Monthly window
                windows['MONTHLY'] = (
                    market['start_date'],
                    market['end_date'],
                    market['slug']
                )

        return windows

    def print_summary(self):
        """Print summary of all discovered markets"""
        markets = self.search_elon_markets()

        print("=" * 80)
        print("ELON MUSK TWEET MARKETS DISCOVERED")
        print("=" * 80)

        if not markets:
            print("\n❌ No active markets found")
            return

        for market in markets:
            status = "🟢 ACTIVE" if market['active'] and not market['closed'] else "🔴 CLOSED"

            print(f"\n{status} {market['title']}")
            print(f"  Slug: {market['slug']}")

            if market['start_date'] and market['end_date']:
                duration = (market['end_date'] - market['start_date']).days
                print(f"  Dates: {market['start_date'].date()} to {market['end_date'].date()} ({duration} days)")

            if market['tweet_count']:
                print(f"  Current count: {market['tweet_count']} tweets")

            print(f"  Volume: ${market['volume']:,.0f} | Liquidity: ${market['liquidity']:,.0f}")

        print("\n" + "=" * 80)
        print("ACTIVE WINDOWS FOR TRADING")
        print("=" * 80)

        windows = self.get_active_windows()

        for window_key, (start, end, slug) in windows.items():
            duration = (end - start).days
            hours_remaining = (end - datetime.now()).total_seconds() / 3600

            print(f"\n{window_key}:")
            print(f"  {start.date()} to {end.date()} ({duration} days)")
            print(f"  Hours remaining: {hours_remaining:.1f}h")
            print(f"  Slug: {slug}")

if __name__ == "__main__":
    discovery = MarketDiscovery()
    discovery.print_summary()
