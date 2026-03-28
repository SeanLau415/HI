# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

# System deps for curl_cffi and lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev libssl-dev libxml2-dev libxslt1-dev \
    curl git \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --upgrade pip && \
    pip install --prefix=/install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

LABEL maintainer="you"
LABEL description="VPS monitoring worker — stock checker + RSS alerter"

# Minimal runtime system deps
# procps: required for `pgrep` used in HEALTHCHECK — absent from slim by default
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 libssl3 procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy worker source
COPY worker.py .

# Data volume — config.yaml and history.db live here
# Mount this as a volume or SFTP config.yaml into it
VOLUME ["/app/data"]

# Config path env — can override at runtime
ENV CONFIG_PATH=/app/data/config.yaml
ENV PERSIST_STATE=false

# Run as non-root for security
RUN useradd -r -s /bin/false worker
USER worker

# Health check — verifies process is alive
HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f worker.py || exit 1

CMD ["python", "-u", "worker.py"]
