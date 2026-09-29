<div align="center">

# Aegis Edge

**An offline-first edge memory kernel on Qdrant Edge — each device is a smart notepad that searches and answers offline, and a walkie-talkie that disputes and converges with the fleet when it reconnects.**

Built for Qdrant · Problem Statement 03 — AI-Powered Edge Memory & Intelligence Platform

![Python](https://img.shields.io/badge/python-3.11%2B-3c873a?style=flat-square&logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white)
![Qdrant Edge](https://img.shields.io/badge/qdrant--edge--py-0.8.0%20pinned-8A2BE2?style=flat-square)
![Qdrant Server](https://img.shields.io/badge/hub-Qdrant%20Server-8A2BE2?style=flat-square)
![No SQL](https://img.shields.io/badge/datastore-Qdrant%20only-cc3836?style=flat-square)

[Overview](#overview) • [How it works](#how-it-works) • [The five demo moments](#the-five-demo-moments) • [Architecture](#architecture) • [Tech stack](#tech-stack) • [Docs](#docs)

</div>

---

## Overview

Edge devices — kiosks, wearables, field tablets, robots — need instant local answers with
no network, but they also keep learning things locally that a shared cloud picture needs
to know about. The naive version, *sync everything with last-write-wins*, breaks in real
ways: it floods bad connections, it corrupts shared knowledge when two devices disagree,
it lets deleted facts ghost back into existence, and it treats a dozen devices reporting
the same event no differently than two.

**Aegis Edge is the intelligence layer above the vector store that fixes those four
things** — built entirely on Qdrant. Qdrant Edge is the on-device engine, Qdrant Server
is the cloud hub, and Qdrant is the only datastore anywhere. No SQL, no second store.

> [!NOTE]
> The disaster-response fleet (paramedic tablets, triage kiosks) is **configuration, not
> code**. Swap `config/disaster-response.yaml` for another vertical and the same kernel
> runs unchanged — that is the claim this repo is built to defend.

> [!IMPORTANT]
> **Qdrant Edge is in beta**, per Qdrant's live documentation. `qdrant-edge-py` is pinned
> to `0.8.0` and never floated. The beta status is re-checked against the live docs before
> any presentation.

## What it does

| Module | What it owns |
| --- | --- |
| **Decision Engine** | Scores every captured fact — novelty, urgency, sensitivity, completeness — before it is kept, queued, or dropped, and emits a reason a non-expert can read. |
| **Model Adapter Registry** | Model- and modality-agnostic through a config file: text embedding, cross-modal CLIP, BM25, and the answer model all swap by one YAML edit. Zero training, pretrained checkpoints only. |
| **Sync Protocol** | Delta-only push and partial-snapshot pull against a real Qdrant Server, with a two-shard query path so devices both publish *and* learn. |
| **Trust / Consensus Resolver** | N-way, trust-weighted consensus over an append-only event log, with a visible confidence score and an explicit `DISPUTED` state. The novel piece. |
| **Answer layer (on-device RAG)** | Retrieve from Qdrant Edge, generate a grounded, sourced answer — a small local model offline, a cloud model online, one interface. |

## How it works

Two ideas carry most of the weight:

- **The notepad.** Each device stores text and image facts in a local Qdrant Edge shard,
  answers hybrid (dense + BM25, fused with RRF) queries offline in milliseconds, and
  generates a grounded answer with on-device RAG — all with no network.
- **The walkie-talkie.** Devices can't hear each other while offline; each just remembers
  what it saw. On reconnect they push to a real Qdrant Server and the gateway folds every
  report into one trusted picture. Three devices agreeing raises confidence; a device
  that keeps being wrong loses trust; genuine disagreement is surfaced as `DISPUTED`, not
  silently resolved; and a retraction can never ghost back.

> [!NOTE]
> **Everything measured is real; only the hardware and the network are emulated.** Each
> device runs in a real CPU/RAM-limited container, telemetry is read from the real cgroup,
> and the network layer injects real latency, a real byte cap, and real failures. Numbers
> on screen are measured, never typed in.

## The five demo moments

1. Multiple devices, fully offline, answering instantly.
2. A live decision feed narrating *why* — kept / queued / synced / rejected — in plain language.
3. Two devices disagreeing offline, converging on reconnect into one trusted answer with a visible confidence score.
4. A safety-critical fact jumping the sync queue ahead of routine ones on a degraded link.
5. Real latency and memory numbers from the emulated constrained target, on screen, measured.

## Architecture

```mermaid
flowchart LR
    subgraph EDGE["Edge Node — one process, CPU/RAM-limited container"]
        CAP[Capture: text / image] --> ADPT[Model Adapter Registry]
        ADPT --> MUT[mutable Edge Shard]
        MUT --> DE[Decision Engine]
        DE -->|verdict + plain-language reason| FEED[Live Decision Feed]
        MUT --> QRY[Hybrid query: dense + BM25, RRF]
        IMM[immutable Edge Shard] --> QRY
        QRY --> RAG[On-device RAG: retrieve → augment → generate]
    end
    MUT -->|delta push, mark-after-ack| GATE[Cloud Gateway]
    GATE --> FOLD[Consensus Fold]
    FOLD --> HUB[(Qdrant Server — fact_events, append-only)]
    HUB -.partial snapshot pull.-> IMM
    FOLD --> DASH[Command Dashboard]
    FOLD --> THEATER[Conflict Theater]
```

The network layer sits on the sync transport only — never on the query or answer path
(enforced by a test), which is what makes "instant offline search" an architectural
guarantee rather than lucky timing.

## Tech stack

- **Device:** Python 3.11+ + FastAPI, one process per node, [`qdrant-edge-py`](https://pypi.org/project/qdrant-edge-py/) with a mutable + immutable `EdgeShard`. Dense text embeddings from [`fastembed`](https://github.com/qdrant/fastembed) (`BAAI/bge-small-en-v1.5`); cross-modal images via CLIP (`Qdrant/clip-ViT-B-32`); BM25 built into Qdrant Edge.
- **Answer model:** [Ollama](https://ollama.com) running `qwen2.5:1.5b` offline; a cloud model online; both behind one `Generator` interface with an extractive fallback for tiny devices.
- **Hub:** a real Qdrant Server (`qdrant/qdrant` via Docker) collection `fact_events`, append-only, plus a FastAPI gateway running the schema middleware and consensus fold.
- **Frontend:** React + Vite + TypeScript + Tailwind, no component library — a thin renderer over the backend contract.
- **Constrained target:** a real 1-CPU / 512 MB container today (swappable for a Raspberry Pi later with zero code change).

## Docs

| Document | Purpose |
| --- | --- |
| [`docs/00-problem-statement.md`](docs/00-problem-statement.md) | The sponsor brief — what we are building and why, with the acceptance bar per goal. |
| [`docs/10-prd.md`](docs/10-prd.md) | Product & software requirements: FR/NFR, personas, acceptance criteria, traceability. |
| [`docs/20-architecture.md`](docs/20-architecture.md) | System view: context, single-node, sync loop, and consensus-fold diagrams. |
| [`docs/backend.md`](docs/backend.md) | Engineering spec: physical model, model registry, decision engine, real-server sync, consensus, answer layer, build phases. |
| [`docs/frontend.md`](docs/frontend.md) | UI spec: screens, design direction, data contract. |
| [`docs/API.md`](docs/API.md) | REST/WebSocket route contract for the frontend team. |
| [`docs/AGENTS.md`](docs/AGENTS.md) | Build guide: verified Qdrant Edge facts, silent traps, correctness invariants, build order. |

## Project status

Backend build phases 1–6 exist in `edge-node/` (capture, hybrid search, decision engine,
outbox/push, snapshot pull, consensus fold, semantic conflict detection) with a passing
test suite against an in-memory hub. In progress: promoting the hub to a real Qdrant
Server, the network layer with measured effects, the on-device RAG answer layer, local
memory eviction, telemetry from real cgroup stats, and the frontend.
