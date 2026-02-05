#!/bin/bash
cd "$(dirname "$0")"

# Create venv if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate and install deps
source venv/bin/activate
pip install -q -r requirements.txt

# Run the server
echo "Starting Meme Flow server..."
echo "Open http://localhost:8000/ui in your browser"
echo ""
python server.py
