# Meme Quant App - Dockerfile for Render
FROM python:3.11-slim

WORKDIR /app

# Copy backend and frontend (server_v2 serves frontend from ../frontend)
COPY backend/ ./backend/
COPY frontend/ ./frontend/

WORKDIR /app/backend

RUN pip install --no-cache-dir -r requirements.txt

# Render sets PORT env (e.g. 10000)
ENV PORT=10000
EXPOSE $PORT

CMD uvicorn server_v2:app --host 0.0.0.0 --port $PORT
