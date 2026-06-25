FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent, strategy engine, and data sources
COPY trader.py .
COPY strategy_lab.py .
COPY data_sources.py .

# Unbuffered logs so Railway shows output live
ENV PYTHONUNBUFFERED=1

# Default to PAPER mode (override TRADING_MODE=LIVE in Railway vars to go live)
ENV TRADING_MODE=PAPER

CMD ["python", "trader.py"]

