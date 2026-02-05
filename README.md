# Meme Quant - Order Flow Scanner

Real-time order flow scanner for meme coin perpetual futures.

## Features
- Multi-exchange support (BingX, BloFin, Hyperliquid)
- Real-time order book analysis
- Signal scoring (-100 to +100)
- Liquidity zone detection
- Trade suggestions (scalp + reversal)
- Near-price activity alerts

## Local Development

```bash
# Start the app
./run.sh

# Or manually:
cd backend
source venv/bin/activate  # if using venv
pip install -r requirements.txt
uvicorn server_v2:app --host 0.0.0.0 --port 8000

# Then open http://localhost:8000/app
```

## Deploy to Render

1. Push to GitHub
2. Connect repo to Render
3. Use the `render.yaml` blueprint or configure manually:
   - **Build Command:** `pip install -r backend/requirements.txt`
   - **Start Command:** `cd backend && uvicorn server_v2:app --host 0.0.0.0 --port $PORT`
   - **Python Version:** 3.11

## API Endpoints

- `GET /` - Health check
- `GET /app` - Main UI
- `GET /api/contracts` - List all contracts
- `GET /api/contracts/new` - Recently listed contracts
- `POST /api/watch/{exchange}/{symbol}` - Start watching a coin
- `DELETE /api/watch/{exchange}/{symbol}` - Stop watching
- `WS /ws` - WebSocket for real-time updates
