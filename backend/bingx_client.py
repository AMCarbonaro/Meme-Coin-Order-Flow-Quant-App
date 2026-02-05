"""
BingX WebSocket client for real-time orderbook and trade data
"""
import asyncio
import json
import gzip
import time
from typing import Callable, Dict, List, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import websockets
from websockets.exceptions import ConnectionClosed

from config import BINGX_WS_URL, MEME_COINS


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
    bids: List[OrderBookLevel] = field(default_factory=list)  # Buy orders
    asks: List[OrderBookLevel] = field(default_factory=list)  # Sell orders
    timestamp: float = 0
    
    @property
    def bid_total_usd(self) -> float:
        return sum(b.value_usd for b in self.bids[:20])  # Top 20 levels
    
    @property
    def ask_total_usd(self) -> float:
        return sum(a.value_usd for a in self.asks[:20])
    
    @property
    def imbalance_ratio(self) -> float:
        """Ratio of bids to asks. >1 = more buying pressure"""
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
    side: str  # "buy" or "sell"
    timestamp: float
    
    @property
    def value_usd(self) -> float:
        return self.price * self.quantity


class BingXWebSocket:
    """
    BingX WebSocket client for subscribing to market data
    """
    
    def __init__(self, symbols: List[str] = None):
        self.symbols = symbols or MEME_COINS
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.orderbooks: Dict[str, OrderBook] = {}
        self.running = False
        
        # Callbacks
        self.on_orderbook: Optional[Callable[[OrderBook], None]] = None
        self.on_trade: Optional[Callable[[Trade], None]] = None
        self.on_large_order: Optional[Callable[[str, OrderBookLevel, str], None]] = None
        
    async def connect(self):
        """Establish WebSocket connection"""
        print(f"Connecting to BingX WebSocket...")
        self.ws = await websockets.connect(
            BINGX_WS_URL,
            ping_interval=20,
            ping_timeout=10,
        )
        print(f"Connected to BingX!")
        self.running = True
        
    async def subscribe(self):
        """Subscribe to orderbook and trade streams for all symbols"""
        if not self.ws:
            raise RuntimeError("Not connected")
            
        for symbol in self.symbols:
            # Subscribe to orderbook depth (top 20 levels, 500ms updates)
            depth_sub = {
                "id": f"depth_{symbol}",
                "reqType": "sub",
                "dataType": f"{symbol}@depth20@500ms"
            }
            await self.ws.send(json.dumps(depth_sub))
            
            # Subscribe to trade stream
            trade_sub = {
                "id": f"trade_{symbol}",
                "reqType": "sub",
                "dataType": f"{symbol}@trade"
            }
            await self.ws.send(json.dumps(trade_sub))
            
            print(f"  Subscribed: {symbol}")
            await asyncio.sleep(0.05)  # Rate limit
            
    def _decompress(self, data: bytes) -> Optional[dict]:
        """BingX sends gzip compressed data"""
        try:
            # Try gzip first
            decompressed = gzip.decompress(data)
            return json.loads(decompressed)
        except gzip.BadGzipFile:
            # Not gzipped, try raw JSON
            try:
                return json.loads(data)
            except:
                return None
        except Exception as e:
            # Try raw JSON as fallback
            try:
                return json.loads(data)
            except:
                return None
    
    def _parse_orderbook(self, data: dict, symbol: str) -> Optional[OrderBook]:
        """Parse orderbook update from BingX"""
        try:
            bids = [
                OrderBookLevel(price=float(b[0]), quantity=float(b[1]))
                for b in data.get("bids", [])
            ]
            asks = [
                OrderBookLevel(price=float(a[0]), quantity=float(a[1]))
                for a in data.get("asks", [])
            ]
            
            return OrderBook(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=time.time()
            )
        except Exception as e:
            print(f"Error parsing orderbook: {e}")
            return None
    
    def _parse_trade(self, trade_data: Any, symbol: str) -> Optional[Trade]:
        """Parse trade from BingX - handles both dict and list formats"""
        try:
            # BingX trade format can be a dict or part of a list
            if isinstance(trade_data, dict):
                return Trade(
                    symbol=symbol,
                    price=float(trade_data.get("p", 0)),
                    quantity=float(trade_data.get("q", 0)),
                    side="sell" if trade_data.get("m", False) else "buy",
                    timestamp=float(trade_data.get("T", time.time() * 1000)) / 1000
                )
            elif isinstance(trade_data, list) and len(trade_data) >= 4:
                # Format: [timestamp, price, quantity, side_bool]
                return Trade(
                    symbol=symbol,
                    price=float(trade_data[1]) if len(trade_data) > 1 else 0,
                    quantity=float(trade_data[2]) if len(trade_data) > 2 else 0,
                    side="sell" if trade_data[3] else "buy" if len(trade_data) > 3 else "buy",
                    timestamp=float(trade_data[0]) / 1000 if trade_data[0] > 1000000000000 else float(trade_data[0])
                )
            return None
        except Exception as e:
            # Silent fail for trade parsing - too noisy
            return None
            
    async def _handle_message(self, message: bytes):
        """Process incoming WebSocket message"""
        data = self._decompress(message)
        if not data:
            return
        
        # Handle ping/pong
        if "ping" in data:
            await self.ws.send(json.dumps({"pong": data["ping"]}))
            return
        
        # Handle Ping message type
        if data.get("code") == 0 and data.get("msg") == "Ping":
            await self.ws.send(json.dumps({"pong": data.get("pingTime", int(time.time() * 1000))}))
            return
            
        # Handle subscription confirmations
        if data.get("id") and not data.get("dataType"):
            return
            
        data_type = data.get("dataType", "")
        
        # Extract symbol from dataType (e.g., "WIF-USDT@depth20@500ms")
        symbol = data_type.split("@")[0] if "@" in data_type else ""
        
        # Orderbook update
        if "@depth" in data_type and "data" in data:
            orderbook = self._parse_orderbook(data["data"], symbol)
            if orderbook:
                self.orderbooks[orderbook.symbol] = orderbook
                if self.on_orderbook:
                    await self._call_handler(self.on_orderbook, orderbook)
                    
        # Trade update
        elif "@trade" in data_type and "data" in data:
            trade_data = data["data"]
            # Handle list of trades
            if isinstance(trade_data, list):
                for t in trade_data:
                    trade = self._parse_trade(t, symbol)
                    if trade and self.on_trade:
                        await self._call_handler(self.on_trade, trade)
            else:
                trade = self._parse_trade(trade_data, symbol)
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
                    # Log but continue
                    pass
        except ConnectionClosed as e:
            print(f"Connection closed: {e}")
            self.running = False
        except Exception as e:
            print(f"Error in listen loop: {e}")
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


# Simple test
async def main():
    client = BingXWebSocket(symbols=["WIF-USDT", "1000PEPE-USDT", "DOGE-USDT"])
    
    def on_orderbook(ob: OrderBook):
        print(f"📊 {ob.symbol}: Imbalance: {ob.imbalance_ratio:.2f}x | "
              f"Spread: {ob.spread_pct:.4f}% | "
              f"Bids: ${ob.bid_total_usd:,.0f} | Asks: ${ob.ask_total_usd:,.0f}")
    
    def on_trade(trade: Trade):
        emoji = "🟢" if trade.side == "buy" else "🔴"
        if trade.value_usd > 1000:  # Only show trades > $1k
            print(f"{emoji} {trade.symbol}: {trade.side.upper()} "
                  f"${trade.value_usd:,.0f} @ {trade.price}")
    
    client.on_orderbook = on_orderbook
    client.on_trade = on_trade
    
    print("Starting BingX WebSocket client...")
    print("Watching: WIF, PEPE, DOGE")
    print("-" * 60)
    
    try:
        await client.run()
    except KeyboardInterrupt:
        print("\nShutting down...")
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
