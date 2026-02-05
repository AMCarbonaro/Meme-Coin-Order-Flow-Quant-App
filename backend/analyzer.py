"""
Order Flow Analyzer - Detects whale activity and large orders
"""
import asyncio
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Deque, Optional, Callable
import time

from bingx_client import BingXWebSocket, OrderBook, Trade, OrderBookLevel
from config import THRESHOLDS, MEME_COINS


@dataclass
class WhaleAlert:
    """Represents a detected whale/large order event"""
    symbol: str
    alert_type: str  # "large_order", "whale_trade", "imbalance", "wall_detected"
    side: str  # "buy" or "sell"
    value_usd: float
    price: float
    timestamp: float
    details: str = ""
    
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "type": self.alert_type,
            "side": self.side,
            "value_usd": self.value_usd,
            "price": self.price,
            "timestamp": self.timestamp,
            "time_str": datetime.fromtimestamp(self.timestamp).strftime("%H:%M:%S"),
            "details": self.details
        }


@dataclass
class SymbolStats:
    """Real-time stats for a symbol"""
    symbol: str
    last_price: float = 0
    bid_volume_usd: float = 0
    ask_volume_usd: float = 0
    imbalance_ratio: float = 1.0
    spread_pct: float = 0
    buy_volume_1m: float = 0  # Last 1 minute
    sell_volume_1m: float = 0
    largest_bid: Optional[OrderBookLevel] = None
    largest_ask: Optional[OrderBookLevel] = None
    last_update: float = 0
    
    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "last_price": self.last_price,
            "bid_volume_usd": self.bid_volume_usd,
            "ask_volume_usd": self.ask_volume_usd,
            "imbalance_ratio": self.imbalance_ratio,
            "spread_pct": self.spread_pct,
            "buy_volume_1m": self.buy_volume_1m,
            "sell_volume_1m": self.sell_volume_1m,
            "largest_bid_usd": self.largest_bid.value_usd if self.largest_bid else 0,
            "largest_ask_usd": self.largest_ask.value_usd if self.largest_ask else 0,
            "pressure": "BUY" if self.imbalance_ratio > 1.2 else ("SELL" if self.imbalance_ratio < 0.8 else "NEUTRAL"),
            "last_update": self.last_update
        }


class OrderFlowAnalyzer:
    """
    Analyzes order flow to detect whale activity and trading signals
    """
    
    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or MEME_COINS
        self.client = BingXWebSocket(symbols=self.symbols)
        
        # State tracking
        self.stats: Dict[str, SymbolStats] = {s: SymbolStats(symbol=s) for s in self.symbols}
        self.alerts: Deque[WhaleAlert] = deque(maxlen=500)  # Keep last 500 alerts
        self.recent_trades: Dict[str, Deque[Trade]] = {s: deque(maxlen=100) for s in self.symbols}
        
        # Callbacks for external handlers
        self.on_alert: Optional[Callable[[WhaleAlert], None]] = None
        self.on_stats_update: Optional[Callable[[SymbolStats], None]] = None
        
        # Set up client callbacks
        self.client.on_orderbook = self._handle_orderbook
        self.client.on_trade = self._handle_trade
        
    async def _handle_orderbook(self, orderbook: OrderBook):
        """Process orderbook update and detect signals"""
        symbol = orderbook.symbol
        stats = self.stats.get(symbol)
        if not stats:
            return
            
        # Update basic stats
        stats.bid_volume_usd = orderbook.bid_total_usd
        stats.ask_volume_usd = orderbook.ask_total_usd
        stats.imbalance_ratio = orderbook.imbalance_ratio
        stats.spread_pct = orderbook.spread_pct
        stats.last_update = time.time()
        
        if orderbook.bids:
            stats.last_price = orderbook.bids[0].price
            
        # Find largest orders (potential walls)
        if orderbook.bids:
            stats.largest_bid = max(orderbook.bids, key=lambda x: x.value_usd)
        if orderbook.asks:
            stats.largest_ask = max(orderbook.asks, key=lambda x: x.value_usd)
            
        # Detect buy/sell walls
        await self._detect_walls(symbol, orderbook, stats)
        
        # Detect significant imbalances
        await self._detect_imbalance(symbol, stats)
        
        # Notify listeners
        if self.on_stats_update:
            if asyncio.iscoroutinefunction(self.on_stats_update):
                await self.on_stats_update(stats)
            else:
                self.on_stats_update(stats)
                
    async def _handle_trade(self, trade: Trade):
        """Process trade and detect whale activity"""
        symbol = trade.symbol
        
        # Track recent trades
        if symbol in self.recent_trades:
            self.recent_trades[symbol].append(trade)
            
        # Update 1-minute volume
        self._update_volume_stats(symbol)
        
        # Detect whale trades
        if trade.value_usd >= THRESHOLDS.whale_order_usd:
            alert = WhaleAlert(
                symbol=symbol,
                alert_type="whale_trade",
                side=trade.side,
                value_usd=trade.value_usd,
                price=trade.price,
                timestamp=trade.timestamp,
                details=f"🐋 WHALE {trade.side.upper()}: ${trade.value_usd:,.0f}"
            )
            await self._emit_alert(alert)
            
        elif trade.value_usd >= THRESHOLDS.large_order_usd:
            alert = WhaleAlert(
                symbol=symbol,
                alert_type="large_trade",
                side=trade.side,
                value_usd=trade.value_usd,
                price=trade.price,
                timestamp=trade.timestamp,
                details=f"Large {trade.side}: ${trade.value_usd:,.0f}"
            )
            await self._emit_alert(alert)
            
    def _update_volume_stats(self, symbol: str):
        """Calculate 1-minute buy/sell volumes"""
        if symbol not in self.recent_trades:
            return
            
        cutoff = time.time() - 60  # Last 60 seconds
        trades = self.recent_trades[symbol]
        
        buy_vol = sum(t.value_usd for t in trades if t.timestamp > cutoff and t.side == "buy")
        sell_vol = sum(t.value_usd for t in trades if t.timestamp > cutoff and t.side == "sell")
        
        if symbol in self.stats:
            self.stats[symbol].buy_volume_1m = buy_vol
            self.stats[symbol].sell_volume_1m = sell_vol
            
    async def _detect_walls(self, symbol: str, orderbook: OrderBook, stats: SymbolStats):
        """Detect significant buy/sell walls"""
        # Check for large bid wall
        if stats.largest_bid and stats.largest_bid.value_usd >= THRESHOLDS.whale_order_usd:
            alert = WhaleAlert(
                symbol=symbol,
                alert_type="wall_detected",
                side="buy",
                value_usd=stats.largest_bid.value_usd,
                price=stats.largest_bid.price,
                timestamp=time.time(),
                details=f"🧱 BUY WALL: ${stats.largest_bid.value_usd:,.0f} @ {stats.largest_bid.price}"
            )
            await self._emit_alert(alert)
            
        # Check for large ask wall
        if stats.largest_ask and stats.largest_ask.value_usd >= THRESHOLDS.whale_order_usd:
            alert = WhaleAlert(
                symbol=symbol,
                alert_type="wall_detected",
                side="sell",
                value_usd=stats.largest_ask.value_usd,
                price=stats.largest_ask.price,
                timestamp=time.time(),
                details=f"🧱 SELL WALL: ${stats.largest_ask.value_usd:,.0f} @ {stats.largest_ask.price}"
            )
            await self._emit_alert(alert)
            
    async def _detect_imbalance(self, symbol: str, stats: SymbolStats):
        """Detect significant order book imbalances"""
        if stats.imbalance_ratio >= THRESHOLDS.imbalance_ratio:
            alert = WhaleAlert(
                symbol=symbol,
                alert_type="imbalance",
                side="buy",
                value_usd=stats.bid_volume_usd,
                price=stats.last_price,
                timestamp=time.time(),
                details=f"📈 BUY PRESSURE: {stats.imbalance_ratio:.1f}x more bids than asks"
            )
            await self._emit_alert(alert)
            
        elif stats.imbalance_ratio <= (1 / THRESHOLDS.imbalance_ratio):
            alert = WhaleAlert(
                symbol=symbol,
                alert_type="imbalance", 
                side="sell",
                value_usd=stats.ask_volume_usd,
                price=stats.last_price,
                timestamp=time.time(),
                details=f"📉 SELL PRESSURE: {1/stats.imbalance_ratio:.1f}x more asks than bids"
            )
            await self._emit_alert(alert)
            
    async def _emit_alert(self, alert: WhaleAlert):
        """Emit alert to listeners and store"""
        # Dedupe: don't spam same alert
        if self.alerts:
            last = self.alerts[-1]
            if (last.symbol == alert.symbol and 
                last.alert_type == alert.alert_type and 
                last.side == alert.side and
                time.time() - last.timestamp < 5):  # Within 5 seconds
                return
                
        self.alerts.append(alert)
        
        if self.on_alert:
            if asyncio.iscoroutinefunction(self.on_alert):
                await self.on_alert(alert)
            else:
                self.on_alert(alert)
                
    def get_all_stats(self) -> List[dict]:
        """Get current stats for all symbols"""
        return [s.to_dict() for s in self.stats.values()]
    
    def get_recent_alerts(self, limit: int = 50) -> List[dict]:
        """Get recent alerts"""
        alerts = list(self.alerts)[-limit:]
        return [a.to_dict() for a in reversed(alerts)]
    
    async def run(self):
        """Start the analyzer"""
        print("Starting Order Flow Analyzer...")
        print(f"Tracking {len(self.symbols)} symbols")
        print(f"Whale threshold: ${THRESHOLDS.whale_order_usd:,}")
        print("-" * 60)
        await self.client.run()
        
    async def close(self):
        """Shutdown"""
        await self.client.close()


# Test the analyzer
async def main():
    analyzer = OrderFlowAnalyzer(symbols=["WIF-USDT", "1000PEPE-USDT", "DOGE-USDT"])
    
    def on_alert(alert: WhaleAlert):
        emoji = "🟢" if alert.side == "buy" else "🔴"
        print(f"{emoji} [{alert.symbol}] {alert.details}")
        
    def on_stats(stats: SymbolStats):
        if stats.imbalance_ratio > 1.3 or stats.imbalance_ratio < 0.7:
            print(f"📊 {stats.symbol}: {stats.pressure} pressure ({stats.imbalance_ratio:.2f}x)")
    
    analyzer.on_alert = on_alert
    analyzer.on_stats_update = on_stats
    
    try:
        await analyzer.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await analyzer.close()


if __name__ == "__main__":
    asyncio.run(main())
