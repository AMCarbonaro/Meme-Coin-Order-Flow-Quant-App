"""
FastAPI server for Meme Flow - serves real-time order flow data
"""
import asyncio
from contextlib import asynccontextmanager
from typing import List, Dict, Any
import json

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from analyzer import OrderFlowAnalyzer, WhaleAlert, SymbolStats
from config import MEME_COINS, THRESHOLDS


# Global analyzer instance
analyzer: OrderFlowAnalyzer = None
connected_clients: List[WebSocket] = []


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown events"""
    global analyzer
    
    # Startup: initialize and start analyzer
    print("Starting Meme Flow server...")
    analyzer = OrderFlowAnalyzer(symbols=MEME_COINS[:10])  # Start with top 10
    
    # Set up broadcast handlers
    analyzer.on_alert = broadcast_alert
    analyzer.on_stats_update = broadcast_stats
    
    # Start analyzer in background
    asyncio.create_task(analyzer.run())
    
    yield
    
    # Shutdown
    print("Shutting down...")
    if analyzer:
        await analyzer.close()


app = FastAPI(
    title="Meme Flow API",
    description="Real-time meme coin perpetual order flow data",
    lifespan=lifespan
)

# CORS for frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def broadcast_alert(alert: WhaleAlert):
    """Broadcast alert to all connected WebSocket clients"""
    if not connected_clients:
        return
    message = json.dumps({"type": "alert", "data": alert.to_dict()})
    for client in connected_clients.copy():
        try:
            await client.send_text(message)
        except:
            connected_clients.remove(client)


async def broadcast_stats(stats: SymbolStats):
    """Broadcast stats update to all connected clients"""
    if not connected_clients:
        return
    message = json.dumps({"type": "stats", "data": stats.to_dict()})
    for client in connected_clients.copy():
        try:
            await client.send_text(message)
        except:
            connected_clients.remove(client)


# ============ REST Endpoints ============

@app.get("/")
async def root():
    """Health check"""
    return {"status": "ok", "service": "meme-flow"}


@app.get("/api/symbols")
async def get_symbols() -> List[str]:
    """Get list of tracked symbols"""
    return analyzer.symbols if analyzer else MEME_COINS


@app.get("/api/stats")
async def get_stats() -> List[Dict[str, Any]]:
    """Get current stats for all symbols"""
    if not analyzer:
        return []
    return analyzer.get_all_stats()


@app.get("/api/stats/{symbol}")
async def get_symbol_stats(symbol: str) -> Dict[str, Any]:
    """Get stats for a specific symbol"""
    if not analyzer or symbol not in analyzer.stats:
        return {"error": "Symbol not found"}
    return analyzer.stats[symbol].to_dict()


@app.get("/api/alerts")
async def get_alerts(limit: int = 50) -> List[Dict[str, Any]]:
    """Get recent alerts"""
    if not analyzer:
        return []
    return analyzer.get_recent_alerts(limit)


@app.get("/api/config")
async def get_config() -> Dict[str, Any]:
    """Get current thresholds config"""
    return {
        "large_order_usd": THRESHOLDS.large_order_usd,
        "whale_order_usd": THRESHOLDS.whale_order_usd,
        "imbalance_ratio": THRESHOLDS.imbalance_ratio,
    }


# ============ WebSocket Endpoint ============

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket for real-time updates"""
    await websocket.accept()
    connected_clients.append(websocket)
    print(f"Client connected. Total clients: {len(connected_clients)}")
    
    try:
        # Send initial state
        if analyzer:
            await websocket.send_text(json.dumps({
                "type": "init",
                "data": {
                    "stats": analyzer.get_all_stats(),
                    "alerts": analyzer.get_recent_alerts(20),
                    "symbols": analyzer.symbols
                }
            }))
        
        # Keep connection alive and handle incoming messages
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=30)
                # Handle ping/pong
                if data == "ping":
                    await websocket.send_text("pong")
            except asyncio.TimeoutError:
                # Send heartbeat
                await websocket.send_text(json.dumps({"type": "heartbeat"}))
                
    except WebSocketDisconnect:
        pass
    finally:
        if websocket in connected_clients:
            connected_clients.remove(websocket)
        print(f"Client disconnected. Total clients: {len(connected_clients)}")


# ============ Simple Test UI ============

@app.get("/ui", response_class=HTMLResponse)
async def test_ui():
    """Simple test UI to view the data"""
    return """
<!DOCTYPE html>
<html>
<head>
    <title>Meme Flow - Order Flow Monitor</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Monaco', 'Menlo', monospace; 
            background: #0a0a0f; 
            color: #e0e0e0; 
            padding: 20px;
        }
        h1 { color: #00ff88; margin-bottom: 20px; }
        h2 { color: #888; font-size: 14px; margin: 20px 0 10px; text-transform: uppercase; }
        .container { display: flex; gap: 20px; }
        .panel { 
            background: #12121a; 
            border: 1px solid #2a2a3a; 
            border-radius: 8px; 
            padding: 15px;
            flex: 1;
        }
        .stats-grid { display: grid; gap: 10px; }
        .stat-card {
            background: #1a1a25;
            padding: 12px;
            border-radius: 6px;
            border-left: 3px solid #333;
        }
        .stat-card.buy { border-left-color: #00ff88; }
        .stat-card.sell { border-left-color: #ff4444; }
        .stat-card.neutral { border-left-color: #888; }
        .symbol { font-weight: bold; color: #fff; }
        .price { color: #888; font-size: 12px; }
        .imbalance { font-size: 18px; font-weight: bold; }
        .imbalance.buy { color: #00ff88; }
        .imbalance.sell { color: #ff4444; }
        .alerts { max-height: 500px; overflow-y: auto; }
        .alert {
            padding: 10px;
            margin-bottom: 8px;
            background: #1a1a25;
            border-radius: 4px;
            font-size: 13px;
        }
        .alert.buy { border-left: 3px solid #00ff88; }
        .alert.sell { border-left: 3px solid #ff4444; }
        .alert-time { color: #666; font-size: 11px; }
        .status { 
            position: fixed; 
            top: 10px; 
            right: 10px; 
            padding: 8px 12px;
            border-radius: 4px;
            font-size: 12px;
        }
        .status.connected { background: #00ff8833; color: #00ff88; }
        .status.disconnected { background: #ff444433; color: #ff4444; }
    </style>
</head>
<body>
    <div class="status disconnected" id="status">Connecting...</div>
    <h1>🚀 Meme Flow</h1>
    <p style="color:#666;margin-bottom:20px;">Real-time perpetual order flow for meme coins</p>
    
    <div class="container">
        <div class="panel">
            <h2>📊 Order Book Stats</h2>
            <div class="stats-grid" id="stats"></div>
        </div>
        <div class="panel">
            <h2>🐋 Whale Alerts</h2>
            <div class="alerts" id="alerts"></div>
        </div>
    </div>
    
    <script>
        const statsEl = document.getElementById('stats');
        const alertsEl = document.getElementById('alerts');
        const statusEl = document.getElementById('status');
        
        const stats = {};
        const alerts = [];
        
        function formatUSD(n) {
            if (n >= 1000000) return '$' + (n/1000000).toFixed(1) + 'M';
            if (n >= 1000) return '$' + (n/1000).toFixed(0) + 'K';
            return '$' + n.toFixed(0);
        }
        
        function renderStats() {
            statsEl.innerHTML = Object.values(stats)
                .sort((a,b) => Math.abs(b.imbalance_ratio - 1) - Math.abs(a.imbalance_ratio - 1))
                .map(s => {
                    const pressure = s.imbalance_ratio > 1.2 ? 'buy' : (s.imbalance_ratio < 0.8 ? 'sell' : 'neutral');
                    return `
                        <div class="stat-card ${pressure}">
                            <div class="symbol">${s.symbol}</div>
                            <div class="price">${s.last_price?.toFixed(6) || '...'}</div>
                            <div class="imbalance ${pressure}">${s.imbalance_ratio?.toFixed(2) || '1.00'}x</div>
                            <div style="font-size:11px;color:#666;margin-top:5px;">
                                Bids: ${formatUSD(s.bid_volume_usd || 0)} | Asks: ${formatUSD(s.ask_volume_usd || 0)}
                            </div>
                        </div>
                    `;
                }).join('');
        }
        
        function renderAlerts() {
            alertsEl.innerHTML = alerts.slice(0, 50).map(a => `
                <div class="alert ${a.side}">
                    <div><strong>${a.symbol}</strong> ${a.details}</div>
                    <div class="alert-time">${a.time_str}</div>
                </div>
            `).join('');
        }
        
        function connect() {
            const ws = new WebSocket('ws://' + location.host + '/ws');
            
            ws.onopen = () => {
                statusEl.textContent = 'Connected';
                statusEl.className = 'status connected';
            };
            
            ws.onclose = () => {
                statusEl.textContent = 'Disconnected - Reconnecting...';
                statusEl.className = 'status disconnected';
                setTimeout(connect, 2000);
            };
            
            ws.onmessage = (e) => {
                const msg = JSON.parse(e.data);
                
                if (msg.type === 'init') {
                    msg.data.stats.forEach(s => stats[s.symbol] = s);
                    alerts.push(...msg.data.alerts);
                    renderStats();
                    renderAlerts();
                }
                else if (msg.type === 'stats') {
                    stats[msg.data.symbol] = msg.data;
                    renderStats();
                }
                else if (msg.type === 'alert') {
                    alerts.unshift(msg.data);
                    if (alerts.length > 100) alerts.pop();
                    renderAlerts();
                }
            };
        }
        
        connect();
    </script>
</body>
</html>
"""


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
