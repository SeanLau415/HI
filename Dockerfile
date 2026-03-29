# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

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

# procps: required for `pgrep` in HEALTHCHECK (absent from slim by default)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libxml2 libxslt1.1 libssl3 procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY --from=builder /install /usr/local
COPY worker.py .

VOLUME ["/app/data"]

# BUG-3 FIX: DB_PATH was missing — users enabling PERSIST_STATE=true
# would silently fall back to MemoryState with no error.
ENV CONFIG_PATH=/app/data/config.yaml
ENV PERSIST_STATE=false
ENV DB_PATH=/app/data/history.db

RUN useradd -r -s /bin/false worker
USER worker

HEALTHCHECK --interval=60s --timeout=10s --start-period=15s --retries=3 \
    CMD pgrep -f worker.py || exit 1

CMD ["python", "-u", "worker.py"]
