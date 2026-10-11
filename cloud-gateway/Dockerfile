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
# evidence is imported at module load by app.py (D10 / T12, hash-sealed evidence
# packet); without it the gateway image exits at import with ModuleNotFoundError.
COPY cloud-gateway/evidence.py .
# The shared snapshot builder (one builder, used by the edge tests too). app.py
# falls back to `import snapshot` when the src/ tree isn't present (this image).
COPY src/edge_node/snapshot.py ./snapshot.py
# value_cluster is the second module app.py vendors next to itself in the image
# (it falls back to a bare `import value_cluster` off the src/ tree, like snapshot).
COPY src/edge_node/value_cluster.py ./value_cluster.py
# model_stamp is the pure embedding-model stamp guard app.py uses to refuse an
# ingest/snapshot from an incompatible model (D2 / T5); vendored like the above.
COPY src/edge_node/model_stamp.py ./model_stamp.py

# Fail fast at build time if the pinned Edge engine isn't importable, instead of
# discovering it at the first /snapshot call. app.py also imports it at module
# load, so this is a belt-and-braces check that keeps the image honest.
RUN python -c "import qdrant_edge"

# Run as an unprivileged user: the gateway never needs root, and a hygiene scan
# (or a hostile payload) should not find a root shell waiting. Files were COPY'd
# as root and stay world-readable; the app only writes to /tmp (snapshot tars).
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser
USER appuser

EXPOSE 8088
# Liveness probe hits the gateway's own /health over the loopback. Uses stdlib
# urllib so the slim image needs no extra curl/wget.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8088/health', timeout=4).status == 200 else 1)"

# QDRANT_URL is injected by docker-compose (the qdrant service).
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8088"]
