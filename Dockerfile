FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt flask

# Copy the agent, strategy engine, data sources, and API
COPY trader.py .
COPY strategy_lab.py .
COPY data_sources.py .
COPY api.py .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Unbuffered logs so Railway shows output live
ENV PYTHONUNBUFFERED=1

# Default to PAPER mode (override TRADING_MODE=LIVE in Railway vars to go live)
ENV TRADING_MODE=PAPER

# Run both trader and API
ENTRYPOINT ["./entrypoint.sh"]

