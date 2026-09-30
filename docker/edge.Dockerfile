# A real constrained edge node (Phase 10).
#
# Built so the container can be run under an honest budget:
#
#   docker run -d --name edge --cpus 1 --memory 512m -p 8000:8000 <image>
#
# The model weights are baked into the image on purpose. Downloading them at
# startup would mean the node needed the network before it could answer, and it
# would load them *inside* the memory budget we are trying to measure.
#
#   docker build -f docker/edge.Dockerfile -t ex-vero-edge .
FROM python:3.11-slim

# Keep the image lean: no compiler toolchain, no build cache, no git history.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# onnxruntime is the heavy part of fastembed; these two flags stop it from
# spawning one thread per core inside a 1-CPU budget.
ENV OMP_NUM_THREADS=1 \
    ORT_NUM_THREADS=1 \
    FASTEMBED_CACHE_PATH=/app/.fastembed_cache

# 1) Install runtime dependencies first so a source edit does not re-resolve
#    the whole dependency tree.
COPY pyproject.toml ./
RUN pip install --no-cache-dir \
      "fastapi>=0.141.1" \
      "fastembed>=0.8.1" \
      "httpx>=0.28.1" \
      "pyyaml>=6.0.3" \
      "qdrant-edge-py==0.8.0" \
      "uvicorn>=0.54.0"

# 2) Bake the weights in, while the layer is still unconstrained. This is a
#    build-time cost, not a startup cost inside the budget.
#
#    Both CLIP legs and the small text model are warmed, because the default
#    config (config/disaster-response.yaml) loads all three and this image is
#    meant to run offline. A node given the smaller config/tiny-edge.yaml
#    simply never touches the CLIP weights.
#
#    The CLIP names are the per-modality legs the app resolves too
#    (`_clip_leg` splits "Qdrant/clip-ViT-B-32" into -text / -vision); the
#    family name itself is not a loadable fastembed model.
#    embed() (not just constructing) is what actually pulls the model down.
RUN python -c "\
from fastembed import TextEmbedding, ImageEmbedding; \
from PIL import Image; \
TextEmbedding('BAAI/bge-small-en-v1.5').embed(['warmup']); \
TextEmbedding('Qdrant/clip-ViT-B-32-text').embed(['warmup']); \
ImageEmbedding('Qdrant/clip-ViT-B-32-vision').embed([Image.new('RGB', (8, 8), (0, 0, 0))])" \
 && du -sh "$FASTEMBED_CACHE_PATH"

# Now that the weights are baked in, forbid network lookups at runtime. Set
# *after* the bake on purpose: HF_HUB_OFFLINE during the bake would make
# fastembed refuse the very download it needs to do.
ENV HF_HUB_OFFLINE=1

# 3) Application code and config.
COPY src ./src
COPY config ./config

ENV PYTHONPATH=/app/src \
    EDGE_CONFIG_PATH=/app/config/tiny-edge.yaml

EXPOSE 8000

# One worker: more would multiply memory and blur the per-node measurement.
CMD ["uvicorn", "edge_node.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
