FROM python:3.11-slim

WORKDIR /app

# Build context is the repo root (see docker/docker-compose.yml) so the gateway
# image can vendor the ONE shared snapshot builder from src/edge_node, rather
# than keeping a second copy that could drift from the edge's.
COPY cloud-gateway/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cloud-gateway/app.py .
COPY cloud-gateway/consensus_fold.py .
COPY cloud-gateway/vector_space.py .
# The shared snapshot builder (one builder, used by the edge tests too). app.py
# falls back to `import snapshot` when the src/ tree isn't present (this image).
COPY src/edge_node/snapshot.py ./snapshot.py

EXPOSE 8088
# QDRANT_URL is injected by docker-compose (the qdrant service).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
