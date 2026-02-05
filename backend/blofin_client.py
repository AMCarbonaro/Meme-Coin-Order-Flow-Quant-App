"""
BloFin WebSocket client for real-time orderbook and trade data.
Uses same OrderBook/Trade types as bingx_client for app compatibility.
Docs: https://docs.blofin.com (Public WebSocket: wss://openapi.blofin.com/ws/public)
"""
import asyncio
import json
import time
from typing import Callable, Dict, List, Optional
from dataclasses import dataclass, field

import websockets
from websockets.exceptions import ConnectionClosed

# Reuse types from bingx_client so server_v2 and CoinWatcher work unchanged
from bingx_client import OrderBook, OrderBookLevel, Trade


BLOFIN_WS_PUBLIC = "wss://openapi.blofin.com/ws/public"


class BloFinWebSocket:
    """
    BloFin public WebSocket client: trades + order book (books5 snapshots).
    No auth required for market data.
    """

    def __init__(self, symbols: List[str]):
        self.symbols = list(symbols)
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.orderbooks: Dict[str, OrderBook] = {}
        self.running = False
        self._ping_task: Optional[asyncio.Task] = None

        self.on_orderbook: Optional[Callable[[OrderBook], None]] = None
        self.on_trade: Optional[Callable[[Trade], None]] = None

    async def connect(self) -> None:
        print("Connecting to BloFin WebSocket...")
        self.ws = await websockets.connect(
            BLOFIN_WS_PUBLIC,
            ping_interval=None,  # we send our own "ping" per BloFin docs
            ping_timeout=None,
            close_timeout=5,
        )
        print("Connected to BloFin!")
        self.running = True

    async def subscribe(self) -> None:
        if not self.ws:
            raise RuntimeError("Not connected")

        for inst_id in self.symbols:
            # trades: real-time trades
            # books5: 5-level order book snapshot every 100ms (no incremental merge)
            sub = {
                "op": "subscribe",
                "args": [
                    {"channel": "trades", "instId": inst_id},
                    {"channel": "books5", "instId": inst_id},
                ],
            }
            await self.ws.send(json.dumps(sub))
            print(f"  Subscribed: {inst_id}")
            await asyncio.sleep(0.05)

    def _parse_orderbook(self, data: dict, symbol: str) -> Optional[OrderBook]:
        """Parse books5 snapshot: data.asks / data.bids = [[price, size], ...]"""
        try:
            raw_bids = data.get("bids", [])
            raw_asks = data.get("asks", [])
            bids = [
                OrderBookLevel(price=float(p), quantity=float(q))
                for p, q in raw_bids
            ]
            asks = [
                OrderBookLevel(price=float(p), quantity=float(q))
                for p, q in raw_asks
            ]
            return OrderBook(
                symbol=symbol,
                bids=bids,
                asks=asks,
                timestamp=time.time(),
            )
        except Exception as e:
            print(f"BloFin orderbook parse error: {e}")
            return None

    def _parse_trade(self, t: dict, symbol: str) -> Optional[Trade]:
        """Parse trade: price, size, side, ts (ms)."""
        try:
            return Trade(
                symbol=symbol,
                price=float(t.get("price", 0)),
                quantity=float(t.get("size", 0)),
                side=str(t.get("side", "buy")).lower(),
                timestamp=float(t.get("ts", 0)) / 1000.0,
            )
        except Exception:
            return None

    async def _handle_message(self, raw: str) -> None:
        # BloFin: server may send "pong" as text
        if raw.strip() == "pong":
            return

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        # Subscription confirmations / errors
        event = data.get("event")
        if event in ("subscribe", "unsubscribe", "error"):
            if event == "error":
                print(f"BloFin WS error: {data.get('msg', data)}")
            return

        arg = data.get("arg", {})
        channel = arg.get("channel")
        inst_id = arg.get("instId", "")

        # Trades
        if channel == "trades":
            for t in data.get("data", []):
                trade = self._parse_trade(t, inst_id)
                if trade and self.on_trade:
                    await self._call_handler(self.on_trade, trade)
            return

        # Order book (books5 snapshot)
        if channel == "books5":
            payload = data.get("data")
            if not payload:
                return
            # books5 push: single snapshot object with asks, bids, ts
            if isinstance(payload, list):
                payload = payload[0] if payload else {}
            ob = self._parse_orderbook(payload, inst_id)
            if ob:
                self.orderbooks[ob.symbol] = ob
                if self.on_orderbook:
                    await self._call_handler(self.on_orderbook, ob)

    async def _call_handler(self, handler: Callable, *args) -> None:
        try:
            if asyncio.iscoroutinefunction(handler):
                await handler(*args)
            else:
                handler(*args)
        except Exception as e:
            print(f"BloFin handler error: {e}")

    async def _ping_loop(self) -> None:
        """Send 'ping' every 25s to keep connection alive (BloFin docs)."""
        while self.running and self.ws:
            await asyncio.sleep(25)
            if not self.running or not self.ws:
                break
            try:
                await self.ws.send("ping")
            except Exception:
                break

    async def listen(self) -> None:
        if not self.ws:
            raise RuntimeError("Not connected")

        self._ping_task = asyncio.create_task(self._ping_loop())

        try:
            async for message in self.ws:
                try:
                    await self._handle_message(message)
                except Exception as e:
                    print(f"BloFin message error: {e}")
        except ConnectionClosed as e:
            print(f"BloFin connection closed: {e}")
            self.running = False
        except Exception as e:
            print(f"BloFin listen error: {e}")
            self.running = False
        finally:
            if self._ping_task:
                self._ping_task.cancel()
                try:
                    await self._ping_task
                except asyncio.CancelledError:
                    pass

    async def run(self) -> None:
        await self.connect()
        await self.subscribe()
        await self.listen()

    async def close(self) -> None:
        self.running = False
        if self._ping_task:
            self._ping_task.cancel()
        if self.ws:
            await self.ws.close()
