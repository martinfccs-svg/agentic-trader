FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# Defaults to PAPER. Gate startup on the selftest so bad math never deploys.
ENV TRADING_MODE=PAPER
CMD ["sh", "-c", "python selftest.py && python main.py --loop"]
