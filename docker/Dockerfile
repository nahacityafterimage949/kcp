# KCP Peer Node — Docker Image
# Usage:
#   docker build -t kcp-peer .
#   docker run -e KCP_NODE_ID=mypeer -e KCP_PORT=8800 -p 8800:8800 -v /dados/kcp-data:/data kcp-peer

FROM python:3.12-slim

# Metadata
LABEL org.opencontainers.image.title="KCP Peer Node" \
      org.opencontainers.image.description="Knowledge Context Protocol — Community peer node" \
      org.opencontainers.image.source="https://github.com/kcp-protocol/kcp" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="0.2.0"

# System deps — SQLite is already included in Python slim image
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN useradd -m -u 1000 kcp

WORKDIR /app

# Copy SDK source
COPY sdk/python/pyproject.toml ./
COPY sdk/python/kcp/ ./kcp/

# Install SDK with server extras
RUN pip install --no-cache-dir -e ".[server]"

# Data volume — all peer state lives here
VOLUME ["/data"]

# Healthcheck
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://localhost:${KCP_PORT:-8800}/kcp/v1/health || exit 1

# Drop to non-root
USER kcp

# Environment defaults (override via docker-compose or -e flags)
ENV KCP_NODE_ID=""
ENV KCP_PORT=8800
ENV KCP_HOST=0.0.0.0
ENV KCP_USER_ID=""
ENV KCP_TENANT_ID="community"
ENV KCP_DATA_DIR="/data"
ENV KCP_PEERS_URL="https://kcp-protocol.org/peers.json"
ENV PYTHONUNBUFFERED=1

# Entrypoint script
COPY docker/entrypoint.sh /entrypoint.sh
USER root
RUN chmod +x /entrypoint.sh
USER kcp

ENTRYPOINT ["/entrypoint.sh"]
