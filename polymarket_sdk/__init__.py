"""Polymarket SDK — Python client for Polymarket API interaction, order management, and market data."""

from .client import PolymarketClient
from .http_pool import HTTPPoolManager
from .positions import PositionManager

__version__ = "0.1.0"
__all__ = ["PolymarketClient", "HTTPPoolManager", "PositionManager"]
