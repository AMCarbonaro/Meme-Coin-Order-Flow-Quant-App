"""
Meme Flow v2 - Browse any coin, click to watch order flow
"""
import asyncio
from contextlib import asynccontextmanager
from typing import List, Dict, Any, Optional, Tuple
import json
import time

import os
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from exchanges import ExchangeDiscovery, PerpContract
from analyzer import OrderFlowAnalyzer, WhaleAlert, SymbolStats
from bingx_client import BingXWebSocket, OrderBook, Trade
from hyperliquid_client import HyperliquidWebSocket, fetch_orderbook as hl_fetch_orderbook
from signals import SignalEngine, OrderBookLevel as SignalOrderBookLevel


# Global state
discovery = ExchangeDiscovery()
active_watchers: Dict[str, "CoinWatcher"] = {}  # key: "exchange:symbol"
connected_clients: List[WebSocket] = []


class CoinWatcher:
    """Watches order flow for a single coin"""
    
    def __init__(self, exchange: str, symbol: str):
        self.exchange = exchange
        self.symbol = symbol
        self.key = f"{exchange}:{symbol}"
        self.client: Optional[BingXWebSocket] = None
        self.stats: Optional[SymbolStats] = None
        self.signal_engine: Optional[SignalEngine] = None
        self.last_signal: Optional[dict] = None
        self.recent_trades: List[Tuple[float, str]] = []  # [(value_usd, side), ...]
        self.running = False
        self.last_update = 0
        self.last_orderbook_bids = []
        self.last_orderbook_asks = []
        
    async def start(self):
        """Start watching this coin"""
        if self.running:
            return
            
        print(f"Starting watcher for {self.key}")
        self.running = True
        self.stats = SymbolStats(symbol=self.symbol)
        self.signal_engine = SignalEngine(self.symbol)
        self.recent_trades = []
        
        if self.exchange == "bingx":
            self.client = BingXWebSocket(symbols=[self.symbol])
            self.client.on_orderbook = self._on_orderbook
            self.client.on_trade = self._on_trade
            asyncio.create_task(self._run_client())
        elif self.exchange == "hyperliquid":
            self.client = HyperliquidWebSocket(symbols=[self.symbol])
            self.client.on_orderbook = self._on_orderbook
            self.client.on_trade = self._on_trade
            asyncio.create_task(self._run_client())
        elif self.exchange == "blofin":
            # TODO: Add BloFin websocket client
            pass
            
    async def _run_client(self):
        """Run the websocket client"""
        try:
            await self.client.run()
        except Exception as e:
            print(f"Watcher error for {self.key}: {e}")
        finally:
            self.running = False
            
    async def _on_orderbook(self, ob: OrderBook):
        """Handle orderbook update"""
        self.stats.bid_volume_usd = ob.bid_total_usd
        self.stats.ask_volume_usd = ob.ask_total_usd
        self.stats.imbalance_ratio = ob.imbalance_ratio
        self.stats.spread_pct = ob.spread_pct
        self.stats.last_update = time.time()
        
        if ob.bids:
            self.stats.last_price = ob.bids[0].price
            self.stats.largest_bid = max(ob.bids, key=lambda x: x.value_usd)
        if ob.asks:
            self.stats.largest_ask = max(ob.asks, key=lambda x: x.value_usd)
        
        # Store orderbook for signal analysis
        self.last_orderbook_bids = [
            SignalOrderBookLevel(price=b.price, quantity=b.quantity) for b in ob.bids
        ]
        self.last_orderbook_asks = [
            SignalOrderBookLevel(price=a.price, quantity=a.quantity) for a in ob.asks
        ]
        
        # Run signal analysis
        if self.signal_engine:
            # Trim old trades (keep last 60 seconds)
            cutoff = time.time() - 60
            self.recent_trades = [(v, s, t) for v, s, t in self.recent_trades if t > cutoff] if self.recent_trades and len(self.recent_trades[0]) == 3 else []
            
            trade_data = [(v, s) for v, s, t in self.recent_trades] if self.recent_trades else None
            result = self.signal_engine.analyze(
                self.last_orderbook_bids,
                self.last_orderbook_asks,
                trade_data
            )
            self.last_signal = result.to_dict()
            
        self.last_update = time.time()
        
        # Broadcast to clients watching this coin
        await broadcast_stats(self.key, self.stats)
        
    async def _on_trade(self, trade: Trade):
        """Handle trade"""
        # Track for signal analysis
        self.recent_trades.append((trade.value_usd, trade.side, time.time()))
        
        # Keep only last 100 trades
        if len(self.recent_trades) > 100:
            self.recent_trades = self.recent_trades[-100:]
        
        if trade.value_usd >= 10000:  # $10k+ trades
            await broadcast_alert(self.key, {
                "type": "trade",
                "symbol": self.symbol,
                "exchange": self.exchange,
                "side": trade.side,
                "value_usd": trade.value_usd,
                "price": trade.price,
                "time": time.strftime("%H:%M:%S")
            })
            
    async def stop(self):
        """Stop watching"""
        self.running = False
        if self.client:
            await self.client.close()
            
    def get_stats(self) -> dict:
        if not self.stats:
            return {}
        return {
            "symbol": self.symbol,
            "exchange": self.exchange,
            "last_price": self.stats.last_price,
            "bid_volume_usd": self.stats.bid_volume_usd,
            "ask_volume_usd": self.stats.ask_volume_usd,
            "imbalance_ratio": self.stats.imbalance_ratio,
            "spread_pct": self.stats.spread_pct,
            "largest_bid_usd": self.stats.largest_bid.value_usd if self.stats.largest_bid else 0,
            "largest_ask_usd": self.stats.largest_ask.value_usd if self.stats.largest_ask else 0,
            "pressure": "BUY" if self.stats.imbalance_ratio > 1.2 else ("SELL" if self.stats.imbalance_ratio < 0.8 else "NEUTRAL"),
            "last_update": self.last_update,
            "signal": self.last_signal
        }


async def broadcast_stats(key: str, stats: SymbolStats):
    """Broadcast stats to clients watching this coin"""
    if not connected_clients:
        return
    message = json.dumps({
        "type": "stats",
        "key": key,
        "data": active_watchers[key].get_stats() if key in active_watchers else {}
    })
    for client in connected_clients.copy():
        try:
            await client.send_text(message)
        except:
            if client in connected_clients:
                connected_clients.remove(client)


async def broadcast_alert(key: str, alert: dict):
    """Broadcast alert to clients"""
    if not connected_clients:
        return
    message = json.dumps({"type": "alert", "key": key, "data": alert})
    for client in connected_clients.copy():
        try:
            await client.send_text(message)
        except:
            if client in connected_clients:
                connected_clients.remove(client)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown"""
    print("Starting Meme Flow v2...")
    await discovery.refresh()
    
    # Auto-refresh contract list every 5 minutes
    async def refresh_loop():
        while True:
            await asyncio.sleep(300)
            await discovery.refresh()
            
    asyncio.create_task(refresh_loop())
    
    yield
    
    # Shutdown all watchers
    for watcher in active_watchers.values():
        await watcher.stop()


app = FastAPI(title="Meme Flow v2", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static frontend files
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/static", StaticFiles(directory=FRONTEND_DIR), name="static")


# ============ REST Endpoints ============

@app.get("/")
async def root():
    return {"status": "ok", "service": "meme-flow-v2", "contracts": len(discovery.contracts)}


@app.get("/api/contracts")
async def get_contracts(
    exchange: str = None,
    sort: str = "list_time",
    limit: int = 100
) -> List[dict]:
    """Get all available contracts"""
    contracts = discovery.get_all(sort_by=sort, exchange=exchange)
    return contracts[:limit]


@app.get("/api/contracts/new")
async def get_new_contracts(days: int = 7, limit: int = 50) -> List[dict]:
    """Get newly listed contracts"""
    return discovery.get_new_listings(days)[:limit]


@app.get("/api/contracts/search")
async def search_contracts(q: str) -> List[dict]:
    """Search contracts"""
    return discovery.search(q)


@app.post("/api/watch/{exchange}/{symbol}")
async def start_watching(exchange: str, symbol: str):
    """Start watching a coin's order flow"""
    key = f"{exchange}:{symbol}"
    
    if key in active_watchers:
        return {"status": "already_watching", "key": key}
        
    # Verify contract exists
    contract = discovery.get_contract(exchange, symbol)
    if not contract:
        return {"status": "error", "message": "Contract not found"}
        
    # Start watcher
    watcher = CoinWatcher(exchange, symbol)
    active_watchers[key] = watcher
    await watcher.start()
    
    return {"status": "watching", "key": key}


@app.delete("/api/watch/{exchange}/{symbol}")
async def stop_watching(exchange: str, symbol: str):
    """Stop watching a coin"""
    key = f"{exchange}:{symbol}"
    
    if key not in active_watchers:
        return {"status": "not_watching"}
        
    await active_watchers[key].stop()
    del active_watchers[key]
    
    return {"status": "stopped", "key": key}


@app.get("/api/watching")
async def get_watching() -> List[dict]:
    """Get all currently watched coins with their stats"""
    return [w.get_stats() for w in active_watchers.values() if w.stats]


@app.post("/api/refresh")
async def refresh_contracts():
    """Manually refresh contract list"""
    count = await discovery.refresh()
    return {"status": "refreshed", "count": count}


# ============ WebSocket ============

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    connected_clients.append(websocket)
    
    try:
        # Send initial state
        await websocket.send_text(json.dumps({
            "type": "init",
            "watching": [w.get_stats() for w in active_watchers.values() if w.stats],
            "contract_count": len(discovery.contracts)
        }))
        
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                msg = json.loads(data)
                
                # Handle commands via websocket
                if msg.get("action") == "watch":
                    exchange = msg.get("exchange")
                    symbol = msg.get("symbol")
                    if exchange and symbol:
                        key = f"{exchange}:{symbol}"
                        if key not in active_watchers:
                            watcher = CoinWatcher(exchange, symbol)
                            active_watchers[key] = watcher
                            await watcher.start()
                        await websocket.send_text(json.dumps({"type": "watching", "key": key}))
                        
                elif msg.get("action") == "unwatch":
                    exchange = msg.get("exchange")
                    symbol = msg.get("symbol")
                    key = f"{exchange}:{symbol}"
                    if key in active_watchers:
                        await active_watchers[key].stop()
                        del active_watchers[key]
                    await websocket.send_text(json.dumps({"type": "unwatched", "key": key}))
                    
            except asyncio.TimeoutError:
                await websocket.send_text(json.dumps({"type": "heartbeat"}))
                
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)


# ============ UI ============

@app.get("/ui", response_class=HTMLResponse)
async def ui():
    return """
<!DOCTYPE html>
<html>
<head>
    <title>Meme Flow v2 - Order Flow Scanner</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'SF Mono', 'Monaco', monospace; 
            background: #0a0a0f; 
            color: #e0e0e0; 
        }
        .header {
            background: #12121a;
            padding: 15px 20px;
            border-bottom: 1px solid #2a2a3a;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        h1 { color: #00ff88; font-size: 20px; }
        .status { color: #666; font-size: 12px; }
        .status.connected { color: #00ff88; }
        
        .container { display: flex; height: calc(100vh - 60px); }
        
        .sidebar {
            width: 350px;
            background: #12121a;
            border-right: 1px solid #2a2a3a;
            display: flex;
            flex-direction: column;
        }
        
        .search-box {
            padding: 10px;
            border-bottom: 1px solid #2a2a3a;
        }
        .search-box input {
            width: 100%;
            padding: 10px;
            background: #1a1a25;
            border: 1px solid #2a2a3a;
            border-radius: 6px;
            color: #fff;
            font-size: 14px;
        }
        .search-box input:focus { outline: none; border-color: #00ff88; }
        
        .filters {
            padding: 10px;
            display: flex;
            gap: 8px;
            border-bottom: 1px solid #2a2a3a;
        }
        .filter-btn {
            padding: 6px 12px;
            background: #1a1a25;
            border: 1px solid #2a2a3a;
            border-radius: 4px;
            color: #888;
            cursor: pointer;
            font-size: 12px;
        }
        .filter-btn.active { background: #00ff8822; border-color: #00ff88; color: #00ff88; }
        .filter-btn:hover { border-color: #444; }
        
        .coin-list {
            flex: 1;
            overflow-y: auto;
            padding: 10px;
        }
        .coin-item {
            padding: 12px;
            background: #1a1a25;
            border-radius: 6px;
            margin-bottom: 8px;
            cursor: pointer;
            border: 1px solid transparent;
        }
        .coin-item:hover { border-color: #333; }
        .coin-item.watching { border-color: #00ff88; background: #00ff8811; }
        .coin-item.new { border-left: 3px solid #ffaa00; }
        .coin-symbol { font-weight: bold; color: #fff; }
        .coin-meta { font-size: 11px; color: #666; margin-top: 4px; }
        .coin-exchange { 
            display: inline-block;
            padding: 2px 6px;
            background: #2a2a3a;
            border-radius: 3px;
            font-size: 10px;
            margin-right: 5px;
        }
        .coin-exchange.bingx { background: #f7931a22; color: #f7931a; }
        .coin-exchange.blofin { background: #627eea22; color: #627eea; }
        .coin-exchange.hyperliquid { background: #00ff8822; color: #00ff88; }
        
        .main {
            flex: 1;
            padding: 20px;
            overflow-y: auto;
        }
        
        .watching-section h2 {
            color: #888;
            font-size: 12px;
            text-transform: uppercase;
            margin-bottom: 15px;
        }
        
        .watching-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
            gap: 15px;
        }
        
        .watch-card {
            background: #12121a;
            border: 1px solid #2a2a3a;
            border-radius: 8px;
            padding: 15px;
        }
        .watch-card .symbol { 
            font-size: 16px; 
            font-weight: bold; 
            color: #fff;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .watch-card .price { color: #888; font-size: 13px; margin: 8px 0; }
        .watch-card .imbalance {
            font-size: 24px;
            font-weight: bold;
            margin: 10px 0;
        }
        .watch-card .imbalance.buy { color: #00ff88; }
        .watch-card .imbalance.sell { color: #ff4444; }
        .watch-card .imbalance.neutral { color: #888; }
        
        .signal-badge {
            display: inline-block;
            padding: 6px 12px;
            border-radius: 4px;
            font-weight: bold;
            font-size: 12px;
            margin-top: 10px;
        }
        .signal-badge.STRONG_BUY { background: #00ff8844; color: #00ff88; border: 1px solid #00ff88; }
        .signal-badge.BUY { background: #00ff8822; color: #00ff88; }
        .signal-badge.NEUTRAL { background: #88888822; color: #888; }
        .signal-badge.SELL { background: #ff444422; color: #ff4444; }
        .signal-badge.STRONG_SELL { background: #ff444444; color: #ff4444; border: 1px solid #ff4444; }
        
        .signal-score {
            font-size: 32px;
            font-weight: bold;
            margin: 5px 0;
        }
        .signal-score.positive { color: #00ff88; }
        .signal-score.negative { color: #ff4444; }
        .signal-score.neutral { color: #888; }
        
        .confidence { font-size: 11px; color: #666; }
        
        .reasons {
            margin-top: 10px;
            font-size: 11px;
            color: #888;
            border-top: 1px solid #2a2a3a;
            padding-top: 8px;
        }
        .reasons div { margin-bottom: 3px; }
        .watch-card .volumes {
            display: flex;
            justify-content: space-between;
            font-size: 12px;
            color: #666;
        }
        .watch-card .volumes .bid { color: #00ff88; }
        .watch-card .volumes .ask { color: #ff4444; }
        .unwatch-btn {
            background: none;
            border: none;
            color: #666;
            cursor: pointer;
            font-size: 16px;
        }
        .unwatch-btn:hover { color: #ff4444; }
        
        .empty-state {
            text-align: center;
            padding: 60px 20px;
            color: #666;
        }
        .empty-state h3 { color: #888; margin-bottom: 10px; }
        
        .alerts {
            margin-top: 30px;
        }
        .alerts h2 {
            color: #888;
            font-size: 12px;
            text-transform: uppercase;
            margin-bottom: 15px;
        }
        .alert-item {
            padding: 10px;
            background: #12121a;
            border-radius: 6px;
            margin-bottom: 8px;
            font-size: 13px;
            border-left: 3px solid #333;
        }
        .alert-item.buy { border-left-color: #00ff88; }
        .alert-item.sell { border-left-color: #ff4444; }
    </style>
</head>
<body>
    <div class="header">
        <h1>🚀 Meme Flow v2</h1>
        <div class="status" id="status">Connecting...</div>
    </div>
    
    <div class="container">
        <div class="sidebar">
            <div class="search-box">
                <input type="text" id="search" placeholder="Search coins..." />
            </div>
            <div class="filters">
                <button class="filter-btn active" data-filter="new">🆕 New</button>
                <button class="filter-btn" data-filter="all">All</button>
                <button class="filter-btn" data-filter="bingx">BingX</button>
                <button class="filter-btn" data-filter="blofin">BloFin</button>
                <button class="filter-btn" data-filter="hyperliquid">Hyperliquid</button>
            </div>
            <div class="coin-list" id="coinList">
                Loading...
            </div>
        </div>
        
        <div class="main">
            <div class="watching-section">
                <h2>📊 Watching Order Flow</h2>
                <div class="watching-grid" id="watchingGrid">
                    <div class="empty-state">
                        <h3>No coins selected</h3>
                        <p>Click a coin from the left to watch its order flow</p>
                    </div>
                </div>
            </div>
            
            <div class="alerts">
                <h2>🐋 Whale Alerts</h2>
                <div id="alertsList"></div>
            </div>
        </div>
    </div>
    
    <script>
        let ws;
        let contracts = [];
        let watching = {};
        let currentFilter = 'new';
        let alerts = [];
        
        function formatUSD(n) {
            if (n >= 1000000) return '$' + (n/1000000).toFixed(1) + 'M';
            if (n >= 1000) return '$' + (n/1000).toFixed(0) + 'K';
            return '$' + n.toFixed(0);
        }
        
        async function loadContracts() {
            const endpoint = currentFilter === 'new' 
                ? '/api/contracts/new?limit=100'
                : `/api/contracts?limit=200${currentFilter !== 'all' ? '&exchange=' + currentFilter : ''}`;
            const resp = await fetch(endpoint);
            contracts = await resp.json();
            renderCoinList();
        }
        
        function renderCoinList() {
            const search = document.getElementById('search').value.toLowerCase();
            const filtered = contracts.filter(c => 
                c.symbol.toLowerCase().includes(search) || 
                c.base_coin.toLowerCase().includes(search)
            );
            
            document.getElementById('coinList').innerHTML = filtered.map(c => {
                const key = c.exchange + ':' + c.symbol;
                const isWatching = watching[key];
                return `
                    <div class="coin-item ${isWatching ? 'watching' : ''} ${c.is_new ? 'new' : ''}" 
                         onclick="toggleWatch('${c.exchange}', '${c.symbol}')">
                        <div class="coin-symbol">
                            <span class="coin-exchange ${c.exchange}">${c.exchange}</span>
                            ${c.symbol}
                        </div>
                        <div class="coin-meta">
                            Listed: ${c.list_date} (${c.age_days}d ago) • ${c.max_leverage}x
                        </div>
                    </div>
                `;
            }).join('');
        }
        
        function renderWatching() {
            const grid = document.getElementById('watchingGrid');
            const keys = Object.keys(watching);
            
            if (keys.length === 0) {
                grid.innerHTML = `
                    <div class="empty-state">
                        <h3>No coins selected</h3>
                        <p>Click a coin from the left to watch its order flow</p>
                    </div>
                `;
                return;
            }
            
            grid.innerHTML = keys.map(key => {
                const w = watching[key];
                const s = w.signal || {};
                const scoreClass = (s.score || 0) > 10 ? 'positive' : ((s.score || 0) < -10 ? 'negative' : 'neutral');
                const reasons = s.reasons || [];
                
                return `
                    <div class="watch-card">
                        <div class="symbol">
                            ${w.symbol}
                            <button class="unwatch-btn" onclick="toggleWatch('${w.exchange}', '${w.symbol}')">×</button>
                        </div>
                        <div class="price">${w.last_price ? w.last_price.toFixed(6) : '...'}</div>
                        
                        <div class="signal-score ${scoreClass}">${(s.score || 0) > 0 ? '+' : ''}${(s.score || 0).toFixed(0)}</div>
                        <span class="signal-badge ${s.signal || 'NEUTRAL'}">${s.signal || 'NEUTRAL'}</span>
                        <div class="confidence">${(s.confidence || 0).toFixed(0)}% confidence</div>
                        
                        <div class="volumes">
                            <span class="bid">Bids: ${formatUSD(w.bid_volume_usd || 0)}</span>
                            <span class="ask">Asks: ${formatUSD(w.ask_volume_usd || 0)}</span>
                        </div>
                        
                        ${reasons.length > 0 ? `
                            <div class="reasons">
                                ${reasons.slice(0, 3).map(r => '<div>' + r + '</div>').join('')}
                            </div>
                        ` : ''}
                    </div>
                `;
            }).join('');
        }
        
        function renderAlerts() {
            document.getElementById('alertsList').innerHTML = alerts.slice(0, 20).map(a => `
                <div class="alert-item ${a.side}">
                    <strong>${a.symbol}</strong> ${a.side.toUpperCase()} ${formatUSD(a.value_usd)} @ ${a.price} 
                    <span style="color:#666">${a.time}</span>
                </div>
            `).join('');
        }
        
        async function toggleWatch(exchange, symbol) {
            const key = exchange + ':' + symbol;
            if (watching[key]) {
                // Unwatch
                await fetch(`/api/watch/${exchange}/${symbol}`, { method: 'DELETE' });
                delete watching[key];
            } else {
                // Watch
                await fetch(`/api/watch/${exchange}/${symbol}`, { method: 'POST' });
                watching[key] = { symbol, exchange, imbalance_ratio: 1 };
            }
            renderCoinList();
            renderWatching();
        }
        
        function connect() {
            ws = new WebSocket('ws://' + location.host + '/ws');
            
            ws.onopen = () => {
                document.getElementById('status').textContent = 'Connected';
                document.getElementById('status').className = 'status connected';
            };
            
            ws.onclose = () => {
                document.getElementById('status').textContent = 'Disconnected';
                document.getElementById('status').className = 'status';
                setTimeout(connect, 2000);
            };
            
            ws.onmessage = (e) => {
                const msg = JSON.parse(e.data);
                
                if (msg.type === 'init') {
                    msg.watching.forEach(w => {
                        watching[w.exchange + ':' + w.symbol] = w;
                    });
                    renderWatching();
                }
                else if (msg.type === 'stats') {
                    watching[msg.key] = msg.data;
                    renderWatching();
                }
                else if (msg.type === 'alert') {
                    alerts.unshift(msg.data);
                    if (alerts.length > 50) alerts.pop();
                    renderAlerts();
                }
            };
        }
        
        // Filter buttons
        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.onclick = () => {
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                currentFilter = btn.dataset.filter;
                loadContracts();
            };
        });
        
        // Search
        document.getElementById('search').oninput = renderCoinList;
        
        // Init
        loadContracts();
        connect();
    </script>
</body>
</html>
"""


# Serve frontend app
@app.get("/app")
async def serve_app():
    """Serve the main frontend app"""
    index_file = FRONTEND_DIR / "index.html"
    if index_file.exists():
        return FileResponse(index_file)
    return {"error": "Frontend not found"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
