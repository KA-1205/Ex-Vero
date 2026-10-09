<p align="center">
  <img src="docs/assets/ex-vero-logo.png" alt="Ex Vero: from the truth" width="320">
</p>

<h3 align="center">Offline-first edge memory on Qdrant Edge.</h3>
<p align="center">
Every device is a <b>smart notepad</b> that searches and answers with no network,<br>
and a <b>walkie-talkie</b> that disputes and converges with the fleet when it reconnects.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-3c873a?style=flat-square&logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/qdrant--edge--py-0.8.0%20pinned-8A2BE2?style=flat-square" alt="Qdrant Edge">
  <img src="https://img.shields.io/badge/hub-Qdrant%20Server-8A2BE2?style=flat-square" alt="Qdrant Server">
  <img src="https://img.shields.io/badge/datastore-Qdrant%20only-cc3836?style=flat-square" alt="Qdrant only">
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#the-demo">Demo</a> ·
  <a href="#why-ex-vero">Why Ex-Vero?</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#architecture">Architecture</a> ·
  <a href="#measured-not-typed-in">Measured</a> ·
  <a href="#docs">Docs</a>
</p>

<!-- TODO: add a short demo GIF at docs/assets/ex-vero-demo.gif -->
<p align="center">
  <img src="docs/assets/ex-vero-demo.gif" alt="Devices answer offline, disagree, then converge into one trusted answer on reconnect">
</p>
<p align="center">
Devices answer offline, disagree, then converge into one trusted answer with a visible confidence score.
Runs locally with Docker: <a href="#quick-start">quick start</a>.
</p>

*Built for Qdrant, Problem Statement 03: AI-Powered Edge Memory & Intelligence Platform.*

---

## Why Ex-Vero?

Kiosks, wearables, field tablets and robots need instant local answers with no network. They also keep learning things locally that the shared cloud picture needs to know about.

The naive fix is to sync everything with last-write-wins. It breaks in four real ways:

| What goes wrong with naive sync | What Ex-Vero does instead |
| --- | --- |
| **Floods bad connections** by sending everything | **Decision Engine** scores each fact (novelty, urgency, sensitivity, completeness) and only syncs what matters, delta-only |
| **Corrupts shared knowledge** when two devices disagree | **Trust-weighted N-way consensus**, with a visible confidence score and an explicit `DISPUTED` state |
| **Deleted facts ghost back** after a stale device syncs | **Append-only event log**, so a retraction can never come back |
| **A dozen devices = two devices**, since repeats count for nothing | Agreement from more devices raises confidence, and a device that is repeatedly wrong loses trust |

Ex-Vero is the intelligence layer *above* the vector store that fixes these. It is built entirely on Qdrant: **Qdrant Edge** on the device, **Qdrant Server** as the hub, and no SQL or second datastore anywhere.

|  | Sync-everything (LWW) | **Ex-Vero** |
| --- | --- | --- |
| Works with no network | depends | **yes, search and answers are fully local** |
| What gets sent | everything | **only what the Decision Engine decides is worth sending** |
| Two devices disagree | last write wins | **trust-weighted consensus or `DISPUTED`** |
| Deleted facts | can reappear | **cannot ghost back** |
| Why was X kept or dropped | opaque | **plain-language reason for every verdict** |
| Datastore | often several | **Qdrant only** |

> [!NOTE]
> The disaster-response fleet (paramedic tablets, triage kiosks) is **configuration, not code**. Swap `config/disaster-response.yaml` for another vertical and the same kernel runs unchanged. That is the claim this repo is built to defend.

> [!IMPORTANT]
> **Qdrant Edge is in beta**, per Qdrant's live documentation. `qdrant-edge-py` is pinned to `0.8.0` and never floated.

## Quick start

Everything runs in Docker: the Qdrant hub, the cloud gateway, Ollama, and a **real 1-CPU / 512 MB constrained edge node**.

**Prerequisites:** Docker with Compose, Python 3.11+, Node 18+.

```bash
git clone https://github.com/KA-1205/Ex-Vero.git && cd Ex-Vero

# 1. start the stack (Qdrant hub, gateway, Ollama, constrained edge node)
docker compose -f docker/docker-compose.yml up --build

# 2. start the dashboard (separate terminal)
cd frontend && npm install && npm run dev
# open http://localhost:5173

# 3. first run only: pull the on-device answer model
docker compose -f docker/docker-compose.yml exec ollama ollama pull qwen2.5:1.5b
```

> Without the model, answers fall back to extractive mode (correct, but less impressive).

| Service | URL | What it is |
| --- | --- | --- |
| Dashboard | <http://localhost:5173> | React UI |
| Edge node | <http://localhost:8000/docs> | FastAPI, interactive route docs |
| Cloud gateway | <http://localhost:8088> | Sync + consensus fold |
| Qdrant | <http://localhost:6333/dashboard> | Hub, inspect the `facts` collection |
| Ollama | <http://localhost:11434> | Local answer model |

### Captured facts reach Qdrant only after a push

This is the offline-first design, not a bug. A capture is stored **on the device**. Nothing leaves until you push it, and the hub never receives data it wasn't handed.

```bash
curl -sX POST localhost:8000/devices/dev-01/push
```

Refresh the Qdrant dashboard and the point appears in `facts` (and in `fact_events`, the append-only log). Device ids are `dev-01`…`dev-04` and `cam-01`…`cam-03`. Full route list: [`docs/API.md`](docs/API.md).

### Prove the constraint is real

```bash
docker inspect docker-edge-tiny-1 --format 'NanoCPus={{.HostConfig.NanoCpus}} Memory={{.HostConfig.Memory}}'
# NanoCPus=1000000000 Memory=536870912   <- 1 CPU, 512 MB

curl -s localhost:8000/devices/dev-01/telemetry
# {"target_label": "emulated constrained target (1.0 CPU / 512 MB container)", ...}
```

### Tests

```bash
.venv/bin/python -m pytest -q

# the container acceptance test needs a Docker daemon
docker info >/dev/null && .venv/bin/python -m pytest tests/test_telemetry_container.py -q
```

### Stop

```bash
docker compose -f docker/docker-compose.yml down       # keep data
docker compose -f docker/docker-compose.yml down -v    # wipe volumes
```

<details>
<summary><b>Troubleshooting</b></summary>

| Symptom | Cause | Fix |
| --- | --- | --- |
| Dashboard loads but shows no data | Browser blocked cross-origin calls to `:8000` | Hard-refresh (`Ctrl+Shift+R`) |
| Qdrant dashboard empty | Nothing pushed yet | `POST /devices/{id}/push` |
| Answers slow or timing out | Ollama model not pulled | `exec ollama pull qwen2.5:1.5b` |
| Gateway restarts in a loop | Qdrant not ready | It retries 5×; if it still fails it stops loudly rather than silently using memory |

</details>

## The demo

Five moments, all live in the dashboard:

1. **Offline, instantly.** Multiple devices, no network, answering in milliseconds.
2. **A decision feed that explains itself.** Kept / queued / synced / rejected, each with a plain-language reason.
3. **Disagreement, then convergence.** Two devices disagree offline and converge on reconnect into one answer with a visible confidence score.
4. **Safety first.** A safety-critical fact jumps the sync queue ahead of routine ones on a degraded link.
5. **Real numbers.** Latency and memory from the emulated constrained target, measured and on screen.

## What it does

| Module | What it owns |
| --- | --- |
| **Decision Engine** | Scores every captured fact (novelty, urgency, sensitivity, completeness) before it is kept, queued, or dropped, and emits a reason a non-expert can read. |
| **Model Adapter Registry** | Model- and modality-agnostic through a config file: text embedding, cross-modal CLIP, BM25, and the answer model all swap by one YAML edit. Zero training, pretrained checkpoints only. |
| **Sync Protocol** | Delta-only push and partial-snapshot pull against a real Qdrant Server, with a two-shard query path so devices both publish *and* learn. |
| **Trust / Consensus Resolver** | N-way, trust-weighted consensus over an append-only event log, with a visible confidence score and an explicit `DISPUTED` state. The novel piece. |
| **Answer layer (on-device RAG)** | Retrieve from Qdrant Edge, generate a grounded, sourced answer. A small local model offline, a cloud model online, one interface. |

## How it works

### 1. The notepad

Each device stores text and image facts in a local Qdrant Edge shard, answers hybrid queries (dense + BM25, fused with RRF) offline in milliseconds, and generates a grounded answer with on-device RAG. No network involved.

### 2. The walkie-talkie

Devices can't hear each other while offline. Each one just remembers what it saw. On reconnect they push to a real Qdrant Server and the gateway folds every report into one trusted picture:

- Three devices agreeing **raises confidence**.
- A device that keeps being wrong **loses trust**.
- Genuine disagreement is surfaced as **`DISPUTED`**, never silently resolved.
- A retraction **can never ghost back**.

### 3. Only the hardware and network are emulated

Everything measured is real. Each device runs in a real CPU/RAM-limited container, telemetry is read from the real cgroup, and the network layer injects real latency, a real byte cap, and real failures. The network layer sits on the sync transport only, never on the query or answer path (enforced by a test). That makes "instant offline search" an architectural guarantee rather than lucky timing.

## Architecture

```mermaid
flowchart LR
    subgraph EDGE["Edge Node: one process, CPU/RAM-limited container"]
        CAP[Capture: text / image] --> ADPT[Model Adapter Registry]
        ADPT --> MUT[mutable Edge Shard]
        MUT --> DE[Decision Engine]
        DE -->|verdict + plain-language reason| FEED[Live Decision Feed]
        MUT --> QRY[Hybrid query: dense + BM25, RRF]
        IMM[immutable Edge Shard] --> QRY
        QRY --> RAG[On-device RAG: retrieve, augment, generate]
    end
    MUT -->|delta push, mark-after-ack| GATE[Cloud Gateway]
    GATE --> FOLD[Consensus Fold]
    FOLD --> HUB[(Qdrant Server: fact_events, append-only)]
    HUB -.partial snapshot pull.-> IMM
    FOLD --> DASH[Command Dashboard]
    FOLD --> THEATER[Conflict Theater]
```

See [`docs/20-architecture.md`](docs/20-architecture.md) for the full design.

## Measured, not typed in

Every number below comes from real code run against labelled fixtures. No figure is stored.

| Measured | Result | How |
| --- | --- | --- |
| Constrained target | 1 CPU / 512 MB container, label read from real limits | `docker inspect` + `/telemetry` |
| CPU under load | **0.19%** idle, **100.2%** under load | real cgroup stats |
| Memory | RSS **~296 MB** | real cgroup stats |
| Hybrid search recall | <!-- TODO: paste from your benchmark output --> | labelled fixtures |
| Bytes sent vs sync-everything | <!-- TODO: paste from your benchmark output --> | benchmark run |
| Consensus / conflict resolver accuracy | <!-- TODO: paste from your benchmark output --> | labelled pairs |

Recall and resolver margins are deliberately narrower than the original hardcoded claims, and per-shape results are returned so a reader can see where each number comes from.

## Deploying to Vercel

`vercel.json` defines two services: `frontend` at `/` and `cloud-gateway` at `/gateway/*`. Everything else stays local.

**The edge node is not deployed, on purpose.** Its facts live in an on-disk Qdrant Edge shard and it serves the decision feed over WebSockets. A serverless function has no persistent filesystem and no WebSocket support, so a deployed node would come up empty on every cold start. Run it in Docker.

| Piece | Where | Talks to |
| --- | --- | --- |
| `frontend` | Vercel, `/` | the local node |
| `cloud-gateway` | Vercel, `/gateway/*` | Qdrant |
| edge node | local Docker, `:8000` | the deployed gateway |

<details>
<summary><b>Setup steps</b></summary>

**1. Let the deployed UI through the node's CORS.** The node reads `ALLOWED_ORIGINS` as a comma-separated list:

```bash
ALLOWED_ORIGINS="https://your-project.vercel.app" \
EDGE_HUB_URL="https://your-project.vercel.app/gateway" \
  docker compose -f docker/docker-compose.yml up -d edge-tiny
```

**2. Point the deployed gateway at a real Qdrant.** The gateway stores nothing of its own and refuses to start without one rather than silently dropping writes. Set these in the Vercel project's environment variables, not in the repo:

```
QDRANT_URL="https://xyz.cloud.qdrant.io:6333"
QDRANT_API_KEY="..."   # if your Qdrant requires auth
```

`OLLAMA_ENDPOINT` is a third optional override, for when Ollama is not on the same machine as the node.

Run `vercel dev` to exercise all services together before deploying.

</details>

Live UI: [ex-vero.vercel.app](https://ex-vero.vercel.app)

## Tech stack

- **Device:** Python 3.11+ and FastAPI, one process per node. [`qdrant-edge-py`](https://pypi.org/project/qdrant-edge-py/) with a mutable + immutable `EdgeShard`. Dense text embeddings from [`fastembed`](https://github.com/qdrant/fastembed) (`BAAI/bge-small-en-v1.5`), cross-modal images via CLIP (`Qdrant/clip-ViT-B-32`), BM25 built into Qdrant Edge.
- **Answer model:** [Ollama](https://ollama.com) running `qwen2.5:1.5b` offline, a cloud model online, both behind one `Generator` interface with an extractive fallback for tiny devices.
- **Hub:** a real Qdrant Server (`qdrant/qdrant` via Docker), collection `fact_events` (append-only), plus a FastAPI gateway running the schema middleware and consensus fold.
- **Frontend:** React + Vite + TypeScript + Tailwind, no component library. A thin renderer over the backend contract.
- **Constrained target:** a real 1-CPU / 512 MB container today, swappable for a Raspberry Pi later with zero code change.

## Project status

All ten backend build phases are complete and audited: capture, hybrid search, decision engine, outbox/push, snapshot pull, consensus fold, semantic conflict detection, network layer, on-device RAG, measured benchmarks, and telemetry from real cgroup stats. The React dashboard is built and wired to the live backend contract.

## Docs

- [`docs/00-problem-statement.md`](docs/00-problem-statement.md): the sponsor brief and acceptance bar per goal
- [`docs/10-prd.md`](docs/10-prd.md): product and software requirements, personas, traceability
- [`docs/20-architecture.md`](docs/20-architecture.md): context, single-node, sync loop and consensus-fold diagrams
- [`docs/backend.md`](docs/backend.md): engineering spec (model registry, decision engine, sync, consensus, answer layer)
- [`docs/frontend.md`](docs/frontend.md): UI spec, screens, data contract
- [`docs/API.md`](docs/API.md): REST/WebSocket route contract
- [`docs/AGENTS.md`](docs/AGENTS.md): verified Qdrant Edge facts, silent traps, correctness invariants

## Contributing

```bash
git clone https://github.com/KA-1205/Ex-Vero.git
cd Ex-Vero
uv sync            # or: pip install -e ".[dev]"
.venv/bin/python -m pytest -q
```

Issues and PRs welcome. Good first areas: new vertical configs in `config/`, new model adapters in the registry, and additional conflict-resolver test fixtures.

## Team

<!-- TODO: add names / GitHub handles -->

## License

<!-- TODO: add a LICENSE file to the repo, then name it here (e.g. MIT or Apache 2.0) -->

If Ex-Vero helps you, a ⭐ helps other edge and Qdrant builders find it.
