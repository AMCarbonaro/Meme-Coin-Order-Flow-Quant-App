#!/bin/bash
# Meme Quant - Order Flow Scanner

cd "$(dirname "$0")"

echo "🚀 Meme Quant - Order Flow Scanner"
echo "=================================="
echo ""

# Check if backend venv exists
if [ ! -d "backend/venv" ]; then
    echo "Setting up backend..."
    cd backend
    python3 -m venv venv
    source venv/bin/activate
    pip install -q -r requirements.txt
    cd ..
fi

# Start backend (serves both API and frontend)
echo "Starting server..."
cd backend
source venv/bin/activate
python server_v2.py &
BACKEND_PID=$!
cd ..

# Wait for backend to start
sleep 2

echo ""
echo "✅ Meme Quant is running!"
echo ""
echo "   App:      http://localhost:8000/app"
echo "   API:      http://localhost:8000"
echo "   API Docs: http://localhost:8000/docs"
echo ""
echo "Press Ctrl+C to stop..."
echo ""

# Handle shutdown
trap "kill $BACKEND_PID 2>/dev/null; exit" INT TERM

# Wait for process
wait
