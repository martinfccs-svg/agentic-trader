FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Safe defaults. Real money requires overriding ALL of: TRADING_MODE=LIVE,
# BROKER=alpaca, ALPACA_PAPER=false, LIVE_CONFIRM=<exact phrase>.
ENV TRADING_MODE=PAPER BROKER=paper ALPACA_PAPER=true
# Startup gates on the self-test so broken math never deploys.
CMD ["sh", "-c", "python selftest.py && python main.py --loop"]
