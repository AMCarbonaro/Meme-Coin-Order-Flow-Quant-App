"""
Configuration for Meme Flow app
"""
from dataclasses import dataclass
from typing import List

# BingX WebSocket endpoints
BINGX_WS_URL = "wss://open-api-swap.bingx.com/swap-market"

# Meme coins to track (add more as needed)
MEME_COINS: List[str] = [
    "WIF-USDT",
    "1000PEPE-USDT", 
    "1000BONK-USDT",
    "DOGE-USDT",
    "SHIB-USDT",
    "FLOKI-USDT",
    "MEME-USDT",
    "BRETT-USDT",
    "POPCAT-USDT",
    "GOAT-USDT",
    "PNUT-USDT",
    "FARTCOIN-USDT",
    "SPX-USDT",
    "1000000MOG-USDT",
    "NEIROCTO-USDT",
]

@dataclass
class WhaleThresholds:
    """Thresholds for detecting whale activity"""
    # Minimum USD value to consider a "large" order
    large_order_usd: float = 10_000
    # Minimum USD value for "whale" alert
    whale_order_usd: float = 50_000
    # Order book imbalance ratio to alert (e.g., 2.0 = 2x more bids than asks)
    imbalance_ratio: float = 1.5

THRESHOLDS = WhaleThresholds()
