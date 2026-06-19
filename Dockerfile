FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy agent script
COPY trader.py .

# Railway needs this
ENV PYTHONUNBUFFERED=1

# Run the agent
CMD ["python", "trader.py"]
