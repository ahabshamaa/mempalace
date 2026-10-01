# MemPalace remote MCP connector for Fly.io.
# Build context = repository root (the fork), e.g.:
#   fly deploy . -c deploy/fly/fly.toml --dockerfile deploy/fly/Dockerfile
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/root

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl sqlite3 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Install the fork (pins chromadb>=1.5.4,<2) plus the HTTP front door deps.
COPY pyproject.toml README.md /app/
COPY mempalace /app/mempalace
RUN pip install --no-cache-dir /app "chromadb==1.5.9" "starlette>=0.46,<1" "uvicorn[standard]>=0.30" "python-multipart>=0.0.9" "httpx>=0.27"

# Bake the MiniLM ONNX embedding model into the image so the first write after a
# deploy never waits on a download (chroma caches it under ~/.cache/chroma).
RUN python - <<'PY'
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2
ef = ONNXMiniLM_L6_V2()
ef._download_model_if_not_exists()
print("embedding model cached:", ef.DOWNLOAD_PATH)
PY

COPY deploy/fly/server /app/server
COPY deploy/fly/scripts /app/scripts
COPY deploy/fly/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh /app/scripts/*.sh /app/scripts/*.py 2>/dev/null || true

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD curl -fsS http://127.0.0.1:8080/health || exit 1
CMD ["/app/entrypoint.sh"]
