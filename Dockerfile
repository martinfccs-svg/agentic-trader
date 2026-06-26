FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV TRADING_MODE=PAPER BROKER=paper ALPACA_PAPER=true
# DIAGNOSTIC CMD (temporary): print the exact selftest exit code, then run main
# regardless (';' not '&&') so we can see which script is actually failing.
# Revert to the gated version once diagnosed.
CMD ["sh", "-c", "echo '--- starting selftest ---'; python selftest.py; echo \"--- selftest exit code: $? ---\"; echo '--- starting main.py ---'; python main.py --loop; echo \"--- main.py exited: $? ---\""]
