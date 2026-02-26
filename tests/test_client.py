"""
Unit tests for Polymarket SDK

Tests cover:
- PolymarketClient initialization (paper and live mode attributes)
- API URL construction
- Market price fetching with mocked HTTP responses
- Error handling (404, 500, empty/malformed responses)
- HTTPPoolManager singleton and connection pooling
- PositionManager data transformations and exit logic
- MarketDiscovery date parsing utility
- Edge cases (empty JSON, missing fields)

All HTTP calls are mocked — no real API connection required.
Run with: python -m unittest discover tests/
"""

import unittest
from unittest.mock import patch, MagicMock, PropertyMock
import json
from datetime import datetime


# ---------------------------------------------------------------------------
# 1. PolymarketClient initialization
# ---------------------------------------------------------------------------

class TestClientInitialization(unittest.TestCase):
    """Test that PolymarketClient sets correct attributes on init."""

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_mode_defaults(self, _mock_get):
        """Paper mode sets api_url, clob_url, mode, and empty creds."""
        from polymarket_sdk.client import PolymarketClient
        client = PolymarketClient(mode='paper')

        self.assertEqual(client.mode, 'paper')
        self.assertEqual(client.api_url, 'https://gamma-api.polymarket.com')
        self.assertEqual(client.clob_url, 'https://clob.polymarket.com')
        self.assertIsNone(client.creds)
        self.assertIsInstance(client._min_order_size_cache, dict)
        self.assertEqual(len(client._min_order_size_cache), 0)

    @patch('polymarket_sdk.client.pooled_get')
    def test_live_mode_requires_private_key(self, _mock_get):
        """Live mode without a private key raises ValueError."""
        from polymarket_sdk.client import PolymarketClient
        with self.assertRaises(ValueError) as ctx:
            PolymarketClient(private_key=None, mode='live')
        self.assertIn('Private key required', str(ctx.exception))

    @patch('polymarket_sdk.client.pooled_get')
    def test_default_mode_is_paper(self, _mock_get):
        """Default mode parameter is paper."""
        from polymarket_sdk.client import PolymarketClient
        client = PolymarketClient()
        self.assertEqual(client.mode, 'paper')


# ---------------------------------------------------------------------------
# 2. API URL construction
# ---------------------------------------------------------------------------

class TestURLConstruction(unittest.TestCase):
    """Verify the client builds correct API endpoint URLs."""

    @patch('polymarket_sdk.client.pooled_get')
    def test_events_url_with_slug(self, mock_get):
        """get_market_prices builds the correct events?slug= URL."""
        from polymarket_sdk.client import PolymarketClient

        mock_response = MagicMock()
        mock_response.json.return_value = []  # empty event list
        mock_get.return_value = mock_response

        client = PolymarketClient(mode='paper')
        client.get_market_prices('test-slug-123')

        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        self.assertEqual(call_url, 'https://gamma-api.polymarket.com/events?slug=test-slug-123')

    @patch('polymarket_sdk.client.pooled_get')
    def test_events_url_contains_base(self, mock_get):
        """URL always starts with the gamma-api base."""
        from polymarket_sdk.client import PolymarketClient

        mock_response = MagicMock()
        mock_response.json.return_value = []
        mock_get.return_value = mock_response

        client = PolymarketClient(mode='paper')
        client.get_market_prices('anything')

        call_url = mock_get.call_args[0][0]
        self.assertTrue(call_url.startswith('https://gamma-api.polymarket.com'))


# ---------------------------------------------------------------------------
# 3. Mocked HTTP responses — get_market_prices
# ---------------------------------------------------------------------------

class TestGetMarketPrices(unittest.TestCase):
    """Test get_market_prices with mocked HTTP responses."""

    def _make_event(self, markets):
        """Helper: wrap a list of market dicts inside an event envelope."""
        return [{'markets': markets}]

    @patch('polymarket_sdk.client.pooled_get')
    def test_single_market_prices(self, mock_get):
        """Correctly parses a single-market event response."""
        from polymarket_sdk.client import PolymarketClient

        market = {
            'groupItemTitle': '100-119',
            'outcomePrices': json.dumps([0.35, 0.65]),
            'clobTokenIds': json.dumps(['tok_yes_1', 'tok_no_1']),
            'conditionId': 'cond_1',
            'active': True,
            'closed': False,
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = self._make_event([market])
        mock_get.return_value = mock_resp

        client = PolymarketClient(mode='paper')
        prices = client.get_market_prices('test-slug')

        self.assertIn('100-119', prices)
        entry = prices['100-119']
        self.assertAlmostEqual(entry['yes_price'], 0.35)
        self.assertEqual(entry['yes_token_id'], 'tok_yes_1')
        self.assertEqual(entry['no_token_id'], 'tok_no_1')
        self.assertEqual(entry['condition_id'], 'cond_1')
        self.assertTrue(entry['active'])
        self.assertFalse(entry['closed'])
        # bid/ask are +-0.001 of yes_price
        self.assertAlmostEqual(entry['bid'], 0.349)
        self.assertAlmostEqual(entry['ask'], 0.351)

    @patch('polymarket_sdk.client.pooled_get')
    def test_multiple_bins(self, mock_get):
        """Handles multi-bin events (each market becomes a key)."""
        from polymarket_sdk.client import PolymarketClient

        markets = []
        for i, name in enumerate(['0-19', '20-39', '40-59']):
            markets.append({
                'groupItemTitle': name,
                'outcomePrices': json.dumps([0.10 + i * 0.05, 0.90 - i * 0.05]),
                'clobTokenIds': json.dumps([f'yes_{i}', f'no_{i}']),
                'conditionId': f'cond_{i}',
                'active': True,
                'closed': False,
            })

        mock_resp = MagicMock()
        mock_resp.json.return_value = self._make_event(markets)
        mock_get.return_value = mock_resp

        client = PolymarketClient(mode='paper')
        prices = client.get_market_prices('multi-slug')

        self.assertEqual(len(prices), 3)
        self.assertIn('0-19', prices)
        self.assertIn('20-39', prices)
        self.assertIn('40-59', prices)

    @patch('polymarket_sdk.client.pooled_get')
    def test_missing_group_title_defaults_to_other(self, mock_get):
        """Market without groupItemTitle falls back to 'Other'."""
        from polymarket_sdk.client import PolymarketClient

        market = {
            'outcomePrices': json.dumps([0.50, 0.50]),
            'clobTokenIds': json.dumps(['y', 'n']),
            'conditionId': 'c1',
            'active': True,
            'closed': False,
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = self._make_event([market])
        mock_get.return_value = mock_resp

        client = PolymarketClient(mode='paper')
        prices = client.get_market_prices('no-title-slug')

        self.assertIn('Other', prices)


# ---------------------------------------------------------------------------
# 4. Error handling (404, 500, empty, malformed)
# ---------------------------------------------------------------------------

class TestErrorHandling(unittest.TestCase):
    """Test client behaviour under error conditions."""

    @patch('polymarket_sdk.client.pooled_get')
    def test_empty_response_returns_empty_dict(self, mock_get):
        """An empty JSON array from the API yields an empty prices dict."""
        from polymarket_sdk.client import PolymarketClient

        mock_resp = MagicMock()
        mock_resp.json.return_value = []
        mock_get.return_value = mock_resp

        client = PolymarketClient(mode='paper')
        prices = client.get_market_prices('nonexistent-slug')
        self.assertEqual(prices, {})

    @patch('polymarket_sdk.client.pooled_get')
    def test_http_exception_propagates(self, mock_get):
        """Network errors from pooled_get propagate to the caller."""
        from polymarket_sdk.client import PolymarketClient
        import requests

        mock_get.side_effect = requests.exceptions.ConnectionError('Connection refused')

        client = PolymarketClient(mode='paper')
        with self.assertRaises(requests.exceptions.ConnectionError):
            client.get_market_prices('any-slug')

    @patch('polymarket_sdk.client.pooled_get')
    def test_malformed_json_raises(self, mock_get):
        """If .json() fails (malformed response), the exception surfaces."""
        from polymarket_sdk.client import PolymarketClient

        mock_resp = MagicMock()
        mock_resp.json.side_effect = json.JSONDecodeError('Expecting value', '', 0)
        mock_get.return_value = mock_resp

        client = PolymarketClient(mode='paper')
        with self.assertRaises(json.JSONDecodeError):
            client.get_market_prices('bad-json')

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_mode_orderbook_returns_mock(self, _mock_get):
        """In paper mode, get_orderbook returns a hardcoded mock orderbook."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        book = client.get_orderbook('any_token')

        self.assertIn('bids', book)
        self.assertIn('asks', book)
        self.assertEqual(book['min_order_size'], '0.0')
        self.assertEqual(len(book['bids']), 2)

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_mode_get_market_returns_stub(self, _mock_get):
        """In paper mode, get_market returns a description stub."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        result = client.get_market('any_id')
        self.assertIn('description', result)
        self.assertEqual(result['description'], 'Paper Mode Market')

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_mode_balances(self, _mock_get):
        """Paper mode returns mock USDC balance of 500."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        balances = client.get_balances()
        self.assertEqual(balances['USDC'], 500.0)

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_mode_get_min_order_size(self, _mock_get):
        """Paper mode returns 0.0 for min order size."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        self.assertEqual(client.get_min_order_size('tok123'), 0.0)

    @patch('polymarket_sdk.client.pooled_get')
    def test_min_order_size_cache(self, _mock_get):
        """get_min_order_size returns cached value when available."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        client._min_order_size_cache['tok_abc'] = 5.0

        result = client.get_min_order_size('tok_abc')
        self.assertEqual(result, 5.0)


# ---------------------------------------------------------------------------
# 5. HTTPPoolManager — singleton and session setup
# ---------------------------------------------------------------------------

class TestHTTPPoolManager(unittest.TestCase):
    """Test the connection pool singleton and its configuration."""

    @patch('polymarket_sdk.http_pool.HTTPPoolManager._prewarm')
    def test_singleton_pattern(self, _mock_prewarm):
        """HTTPPoolManager returns the same instance on repeated calls."""
        from polymarket_sdk.http_pool import HTTPPoolManager

        # Reset singleton for a clean test
        HTTPPoolManager._instance = None

        a = HTTPPoolManager()
        b = HTTPPoolManager()
        self.assertIs(a, b)

        # Cleanup: reset singleton so other tests are not affected
        HTTPPoolManager._instance = None

    @patch('polymarket_sdk.http_pool.HTTPPoolManager._prewarm')
    def test_session_has_keepalive_header(self, _mock_prewarm):
        """Session created by the pool has Connection: keep-alive."""
        from polymarket_sdk.http_pool import HTTPPoolManager

        HTTPPoolManager._instance = None
        pool = HTTPPoolManager()

        self.assertEqual(pool.session.headers.get('Connection'), 'keep-alive')
        HTTPPoolManager._instance = None

    @patch('polymarket_sdk.http_pool.HTTPPoolManager._prewarm')
    def test_get_pool_returns_manager(self, _mock_prewarm):
        """get_pool() module-level function returns an HTTPPoolManager."""
        import polymarket_sdk.http_pool as hp

        hp._pool = None
        hp.HTTPPoolManager._instance = None

        pool = hp.get_pool()
        self.assertIsInstance(pool, hp.HTTPPoolManager)

        # Cleanup
        hp._pool = None
        hp.HTTPPoolManager._instance = None

    @patch('polymarket_sdk.http_pool.HTTPPoolManager._prewarm')
    def test_pooled_get_delegates_to_session(self, _mock_prewarm):
        """pooled_get() calls through to the session's .get() method."""
        import polymarket_sdk.http_pool as hp

        hp._pool = None
        hp.HTTPPoolManager._instance = None

        pool = hp.get_pool()
        pool.session = MagicMock()

        fake_response = MagicMock()
        pool.session.get.return_value = fake_response

        result = pool.get('https://example.com', timeout=5)
        pool.session.get.assert_called_once_with('https://example.com', timeout=5)
        self.assertIs(result, fake_response)

        hp._pool = None
        hp.HTTPPoolManager._instance = None


# ---------------------------------------------------------------------------
# 6. PositionManager — data transformation and exit logic
# ---------------------------------------------------------------------------

class TestPositionManager(unittest.TestCase):
    """Test PositionManager calculations without disk I/O."""

    def _make_manager(self, bankroll=100.0):
        """Create a PositionManager with mocked file I/O."""
        with patch('polymarket_sdk.positions.Path.exists', return_value=False):
            from polymarket_sdk.positions import PositionManager
            pm = PositionManager(bankroll=bankroll, positions_file='/dev/null')
        return pm

    def test_initial_exposure_is_zero(self):
        pm = self._make_manager()
        self.assertEqual(pm.get_total_exposure(), 0)

    def test_can_open_within_bankroll(self):
        pm = self._make_manager(bankroll=100.0)
        can_open, msg = pm.can_open_position(50.0)
        self.assertTrue(can_open)
        self.assertEqual(msg, 'OK')

    def test_cannot_open_exceeding_bankroll(self):
        pm = self._make_manager(bankroll=100.0)
        can_open, msg = pm.can_open_position(150.0)
        self.assertFalse(can_open)
        self.assertIn('exceed', msg.lower())

    def test_should_exit_on_edge_flip(self):
        """Exit when model probability falls below market price (negative edge)."""
        pm = self._make_manager()
        position = {
            'entry_price': 0.10,
            'model_prob': 0.20,
            'size': 5.0,
        }
        # current_price=0.15, model_prob=0.10 -> edge = 0.10 - 0.15 = -0.05 (negative)
        should_exit, reason = pm.should_exit(position, current_price=0.15, model_prob=0.10, hours_to_close=100)
        self.assertTrue(should_exit)
        self.assertIn('EDGE_FLIP', reason)

    def test_hold_when_edge_positive(self):
        """Hold when model still shows positive edge."""
        pm = self._make_manager()
        position = {
            'entry_price': 0.10,
            'model_prob': 0.30,
            'size': 5.0,
        }
        # current_price=0.12, model_prob=0.25 -> edge = 0.25 - 0.12 = +0.13 (strong)
        should_exit, reason = pm.should_exit(position, current_price=0.12, model_prob=0.25, hours_to_close=100)
        self.assertFalse(should_exit)
        self.assertIsNone(reason)

    def test_rebalance_buy_signal(self):
        """Rebalance returns BUY when target size exceeds current by >20%."""
        pm = self._make_manager(bankroll=100.0)
        position = {'size': 10.0}
        # target = 0.25 * 100 = 25 -> delta = 15 -> change = 150% > 20%
        action, delta = pm.calculate_rebalance(position, new_kelly_fraction=0.25)
        self.assertEqual(action, 'BUY')
        self.assertAlmostEqual(delta, 15.0)

    def test_rebalance_hold_when_small_change(self):
        """Rebalance returns HOLD when change is under 20%."""
        pm = self._make_manager(bankroll=100.0)
        position = {'size': 10.0}
        # target = 0.11 * 100 = 11 -> delta = 1 -> change = 10% < 20%
        action, delta = pm.calculate_rebalance(position, new_kelly_fraction=0.11)
        self.assertEqual(action, 'HOLD')
        self.assertEqual(delta, 0)

    def test_summary_with_no_positions(self):
        pm = self._make_manager(bankroll=60.0)
        summary = pm.get_summary()
        self.assertEqual(summary['open_positions'], 0)
        self.assertEqual(summary['closed_positions'], 0)
        self.assertEqual(summary['bankroll'], 60.0)
        self.assertAlmostEqual(summary['total_pnl'], 0.0)


# ---------------------------------------------------------------------------
# 7. MarketDiscovery — date parsing utility
# ---------------------------------------------------------------------------

class TestMarketDiscoveryDateParsing(unittest.TestCase):
    """Test the parse_dates_from_title utility without any HTTP calls."""

    def setUp(self):
        from polymarket_sdk.market_discovery import MarketDiscovery
        self.discovery = MarketDiscovery()

    def test_weekly_range_parsing(self):
        """Parse 'October 21 - October 28, 2025' format."""
        title = "Elon Musk # tweets October 21 - October 28, 2025?"
        start, end = self.discovery.parse_dates_from_title(title)

        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertEqual(start.month, 10)
        self.assertEqual(start.day, 21)
        self.assertEqual(end.month, 10)
        self.assertEqual(end.day, 28)
        self.assertEqual(start.year, 2025)

    def test_monthly_market_parsing(self):
        """Parse 'October 2025' monthly format."""
        title = "Elon Musk # tweets October 2025?"
        start, end = self.discovery.parse_dates_from_title(title)

        self.assertIsNotNone(start)
        self.assertIsNotNone(end)
        self.assertEqual(start.month, 10)
        self.assertEqual(start.day, 1)
        self.assertEqual(end.month, 10)
        self.assertEqual(end.day, 31)

    def test_unparseable_title_returns_nones(self):
        """Unrecognized title format returns (None, None)."""
        title = "Some random title with no dates"
        start, end = self.discovery.parse_dates_from_title(title)
        self.assertIsNone(start)
        self.assertIsNone(end)


# ---------------------------------------------------------------------------
# 8. Paper-mode order placement and cancellation
# ---------------------------------------------------------------------------

class TestPaperTrading(unittest.TestCase):
    """Test paper-mode trading operations (no real orders placed)."""

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_place_order_returns_id(self, _mock_get):
        """Paper mode place_order returns a PAPER_ prefixed order ID."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        order_id = client.place_order('tok_1', 'BUY', 5.0, price=0.10)

        self.assertIsNotNone(order_id)
        self.assertTrue(order_id.startswith('PAPER_'))

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_cancel_order_succeeds(self, _mock_get):
        """Paper mode cancel_order always returns True."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        result = client.cancel_order('PAPER_123')
        self.assertTrue(result)

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_batch_orders(self, _mock_get):
        """Paper mode place_orders_batch returns PAPER_ IDs for each order."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        orders = [
            {'token_id': 't1', 'side': 'BUY', 'size': 5, 'price': 0.10},
            {'token_id': 't2', 'side': 'BUY', 'size': 3, 'price': 0.20},
        ]
        ids = client.place_orders_batch(orders)
        self.assertEqual(len(ids), 2)
        for oid in ids:
            self.assertTrue(oid.startswith('PAPER_'))

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_cancel_batch_succeeds(self, _mock_get):
        """Paper mode cancel_orders_batch returns True."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        result = client.cancel_orders_batch(['PAPER_1', 'PAPER_2'])
        self.assertTrue(result)

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_get_midpoint(self, _mock_get):
        """Paper mode get_midpoint returns '0.50'."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        mid = client.get_midpoint('tok_abc')
        self.assertEqual(mid, '0.50')

    @patch('polymarket_sdk.client.pooled_get')
    def test_paper_get_position_value(self, _mock_get):
        """Paper mode get_position_value returns (0, 0.0, 0.0)."""
        from polymarket_sdk.client import PolymarketClient

        client = PolymarketClient(mode='paper')
        shares, value, price = client.get_position_value('tok_x')
        self.assertEqual(shares, 0)
        self.assertEqual(value, 0.0)
        self.assertEqual(price, 0.0)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    unittest.main()
