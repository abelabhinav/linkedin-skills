# Production Dockerfile for Autonomous LinkedIn Automation Worker
FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SQLITE_DB_PATH=/data/approval_queue.sqlite3 \
    PORT=8080

WORKDIR /app

# Install curl for healthchecks
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code (excluding secrets via .dockerignore)
COPY lib/ ./lib/
COPY automation/ ./automation/
COPY scripts/ ./scripts/
COPY references/ ./references/
COPY skills/ ./skills/

# Create persistent data directory
RUN mkdir -p /data

EXPOSE 8080

# Healthcheck testing the /health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD curl -f http://localhost:8080/health || exit 1

# Start the supervised server and polling daemon
CMD ["python", "scripts/run_autonomous.py", "serve", "--port", "8080", "--enable-polling"]
