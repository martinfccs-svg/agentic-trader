# Dockerfile — agentic trader
#
# THE LINE THAT MATTERS IS THE LAST ONE.
#
# On 2026-08-18 deployment 80af817f went out with no start command set. The
# container fell back to whatever the image's default CMD happened to be — a
# test harness. It ran, finished, exited 0, and Railway's ALWAYS restart
# policy treated that as a crash and restarted it. The loop ran for 18
# minutes before the service failed out.
#
# An explicit CMD removes the fallback entirely. If the service's start
# command is ever cleared again, the container runs the TRADER, not whatever
# was last in the image.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Dependencies first, so code changes don't invalidate the layer.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The trading loop. NEVER a backtest, a test harness, or any tool that exits
# when it finishes — Railway restarts anything that exits, and a research
# tool as entrypoint becomes an infinite loop that re-fetches market data on
# the same API budget as the live trader.
#
# Research tools additionally refuse to run as an entrypoint (see
# tool_guard.py), but that is the second line of defence. This is the first.
# --loop IS REQUIRED. Without it run(loop=False) executes 40 cycles and
# RETURNS — the process exits 0, Railway restarts it, and the trader runs 40
# more. That is the same restart loop as deployment 80af817f, moved into the
# trading service, where each restart also re-runs startup reconciliation
# against the broker.
#
# Nearly shipped exactly that in this file. The flag is not optional.
CMD ["python", "main.py", "--loop"]
