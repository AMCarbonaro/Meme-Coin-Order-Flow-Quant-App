"""
Hyperliquid WebSocket client for real-time orderbook and trade data
"""
import asyncio
import json
import time
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass, field
import websockets
from websockets.exceptions import ConnectionClosed
import aiohttp


@dataclass
class OrderBookLevel:
    price: float
    quantity: float
    
    @property
    def value_usd(self) -> float:
        return self.price * self.quantity


@dataclass 
class OrderBook:
    symbol: str
    bids: List[OrderBookLevel] = field(default_factory=list)
    asks: List[OrderBookLevel] = field(default_factory=list)
    timestamp: float = 0
    
    @property
    def bid_total_usd(self) -> float:
        return sum(b.value_usd for b in self.bids[:20])
    
    @property
    def ask_total_usd(self) -> float:
        return sum(a.value_usd for a in self.asks[:20])
    
    @property
    def imbalance_ratio(self) -> float:
        if self.ask_total_usd == 0:
            return 1.0
        return self.bid_total_usd / self.ask_total_usd
    
    @property
    def spread_pct(self) -> float:
        if not self.bids or not self.asks:
            return 0
        return ((self.asks[0].price - self.bids[0].price) / self.bids[0].price) * 100


@dataclass
class Trade:
    symbol: str
    price: float
    quantity: float
    side: str
    timestamp: float
    
    @property
    def value_usd(self) -> float:
        return self.price * self.quantity


class HyperliquidWebSocket:
    """
    Hyperliquid WebSocket client for subscribing to market data
    """
    
    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or []
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.orderbooks: Dict[str, OrderBook] = {}
        self.running = False
        
        # Callbacks
        self.on_orderbook: Optional[Callable[[OrderBook], None]] = None
        self.on_trade: Optional[Callable[[Trade], None]] = None
        
    async def connect(self):
        """Establish WebSocket connection"""
        print(f"Connecting to Hyperliquid WebSocket...")
        self.ws = await websockets.connect(
            "wss://api.hyperliquid.xyz/ws",
            ping_interval=20,
            ping_timeout=10,
        )
        print(f"Connected to Hyperliquid!")
        self.running = True
        
    async def subscribe(self):
        """Subscribe to orderbook and trade streams"""
        if not self.ws:
            raise RuntimeError("Not connected")
            
        for symbol in self.symbols:
            # Subscribe to L2 orderbook
            sub_msg = {
                "method": "subscribe",
                "subscription": {
                    "type": "l2Book",
                    "coin": symbol
                }
            }
            await self.ws.send(json.dumps(sub_msg))
            
            # Subscribe to trades
            trade_sub = {
                "method": "subscribe",
                "subscription": {
                    "type": "trades",
                    "coin": symbol
                }
            }
            await self.ws.send(json.dumps(trade_sub))
            
            print(f"  Subscribed: {symbol}")
            await asyncio.sleep(0.05)
    
    def _parse_orderbook(self, data: dict, symbol: str) -> Optional[OrderBook]:
        """Parse orderbook from Hyperliquid"""
        try:
            levels = data.get("levels", [[], []])
            
            bids = [
                OrderBookLevel(price=float(b["px"]), quantity=float(b["sz"]))
                for b in levels[0]
            ]
            asks = [
                OrderBookLevel(price=float(a["px"]), quantity=float(a["sz"]))
                for a in levels[1]
            ]
            
            return OrderBook(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=time.time()
            )
        except Exception as e:
            print(f"Error parsing HL orderbook: {e}")
            return None
    
    def _parse_trade(self, trade_data: dict, symbol: str) -> Optional[Trade]:
        """Parse trade from Hyperliquid"""
        try:
            return Trade(
                symbol=symbol,
                price=float(trade_data.get("px", 0)),
                quantity=float(trade_data.get("sz", 0)),
                side=trade_data.get("side", "B").lower().replace("b", "buy").replace("a", "sell"),
                timestamp=float(trade_data.get("time", time.time() * 1000)) / 1000
            )
        except Exception as e:
            return None
            
    async def _handle_message(self, message: str):
        """Process incoming WebSocket message"""
        try:
            data = json.loads(message)
        except:
            return
        
        channel = data.get("channel")
        msg_data = data.get("data", {})
        
        # L2 Book update
        if channel == "l2Book":
            coin = msg_data.get("coin", "")
            orderbook = self._parse_orderbook(msg_data, coin)
            if orderbook:
                self.orderbooks[coin] = orderbook
                if self.on_orderbook:
                    await self._call_handler(self.on_orderbook, orderbook)
                    
        # Trades
        elif channel == "trades":
            trades = msg_data if isinstance(msg_data, list) else [msg_data]
            for t in trades:
                coin = t.get("coin", "")
                trade = self._parse_trade(t, coin)
                if trade and self.on_trade:
                    await self._call_handler(self.on_trade, trade)
                    
    async def _call_handler(self, handler: Callable, *args):
        """Call handler, supporting both sync and async"""
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(*args)
            else:
                handler(*args)
        except Exception as e:
            print(f"Handler error: {e}")
            
    async def listen(self):
        """Main loop to receive and process messages"""
        if not self.ws:
            raise RuntimeError("Not connected")
            
        try:
            async for message in self.ws:
                try:
                    await self._handle_message(message)
                except Exception as e:
                    pass
        except ConnectionClosed as e:
            print(f"HL Connection closed: {e}")
            self.running = False
        except Exception as e:
            print(f"HL Error in listen loop: {e}")
            self.running = False
            
    async def run(self):
        """Connect, subscribe, and start listening"""
        await self.connect()
        await self.subscribe()
        await self.listen()
        
    async def close(self):
        """Close the WebSocket connection"""
        self.running = False
        if self.ws:
            await self.ws.close()


# Also provide a REST-based orderbook fetch for initial data
async def fetch_orderbook(symbol: str) -> Optional[OrderBook]:
    """Fetch orderbook via REST API"""
    url = "https://api.hyperliquid.xyz/info"
    
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"type": "l2Book", "coin": symbol}, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    levels = data.get("levels", [[], []])
                    
                    bids = [
                        OrderBookLevel(price=float(b["px"]), quantity=float(b["sz"]))
                        for b in levels[0]
                    ]
                    asks = [
                        OrderBookLevel(price=float(a["px"]), quantity=float(a["sz"]))
                        for a in levels[1]
                    ]
                    
                    return OrderBook(
                        symbol=symbol,
                        bids=bids,
                        asks=asks,
                        timestamp=time.time()
                    )
    except Exception as e:
        print(f"Error fetching HL orderbook: {e}")
    return None


# Test
async def main():
    client = HyperliquidWebSocket(symbols=["WIF", "FARTCOIN", "TRUMP"])
    
    def on_orderbook(ob: OrderBook):
        print(f"📊 {ob.symbol}: Imbalance: {ob.imbalance_ratio:.2f}x | "
              f"Bids: ${ob.bid_total_usd:,.0f} | Asks: ${ob.ask_total_usd:,.0f}")
    
    def on_trade(trade: Trade):
        emoji = "🟢" if trade.side == "buy" else "🔴"
        if trade.value_usd > 1000:
            print(f"{emoji} {trade.symbol}: {trade.side.upper()} "
                  f"${trade.value_usd:,.0f} @ {trade.price}")
    
    client.on_orderbook = on_orderbook
    client.on_trade = on_trade
    
    print("Starting Hyperliquid WebSocket client...")
    print("-" * 60)
    
    try:
        await client.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
