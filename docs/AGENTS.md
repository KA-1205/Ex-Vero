# AGENTS.md — Aegis Edge

**Read this before writing code.** It is the build guide: the verified Qdrant Edge facts,
the traps that silently corrupt data, the invariants that must each have a test, and the
build order. For *what* and *why* see `00-problem-statement.md`; for requirements see
`10-prd.md`; for the system view see `20-architecture.md`; for the engineering detail see
`backend.md`; for the UI see `frontend.md`; for the route contract see `API.md`.

**Sponsor:** Qdrant. Qdrant Edge on device, Qdrant Server in the cloud, and Qdrant is the
only datastore anywhere — no SQL, no second store.

**One-line mental model:** each device is a **smart notepad** (local Qdrant Edge memory
that searches and answers offline) and a **walkie-talkie** (sync + trust-weighted
consensus across a fleet that disputes and converges).

---

## 1. What we are building

An offline-first **edge memory kernel** on Qdrant Edge, demonstrated as a
disaster-response fleet (configuration, not code). Each edge node keeps local semantic
memory, decides what to keep vs sync, answers with on-device RAG, and reconciles many
devices' conflicting reports into one trusted picture with a visible confidence score.
The product is the **decision + consensus + answer** layer on top of Qdrant — not the
vector store alone. See `00-problem-statement.md` §2 for the trap in goal 8.

---

## 2. Non-negotiable operating rules

1. **Real code, emulated hardware.** Nothing hardcoded, mocked, or faked in the runtime
   path. Only the physical device (a real CPU/RAM-limited container) and network
   conditions (real injected latency/loss) are emulated, and both produce measured
   effects. Numbers on screen are read from the real system. (`backend.md` §2)
2. **Qdrant only.** Qdrant Edge on device, Qdrant Server as the hub. No SQL, no second
   datastore.
3. **Models are config.** Pretrained checkpoints selected in YAML behind `Embedder` /
   `Generator` protocols. Swap = one-line edit. We never train weights and never say so;
   we stamp a checkpoint `version` on every point. (`backend.md` §3)
4. **Offline is default.** The query and answer modules never import the transport or the
   network simulator — enforced by a static test.
5. **Done means measured.** A step is done when its acceptance line holds and a test or
   on-screen number proves it.

---

## 3. Qdrant Edge — verified facts (checked against live docs)

- **What it is:** a lightweight, in-process, embedded vector engine — "SQLite for vector
  search." No background services. Runs inside the app process.
- **Status: beta.** The live docs state Qdrant Edge is in beta; the API may change. **Pin
  the exact version** (`qdrant-edge-py==0.8.0`) and never float it. Re-check the status
  line before presenting.
- **Packages:** `pip install qdrant-edge-py`, imported as `qdrant_edge`; Rust crate
  `qdrant-edge`. **Python and Rust only** — no browser binding. The frontend talks to the
  node over HTTP.
- **Not `QdrantClient(path=...)`.** That is `QdrantLocal` — no snapshots, no BM25, no
  sync. We need all three, so we use `qdrant-edge-py`.
- **No built-in `.sync()`.** Sync is a pattern assembled from shard helpers + our own
  transport, per Qdrant's sync guide (dual shard: mutable + immutable, partial snapshots).
- **No query-time fusion.** Query one named vector field per request (`using=`). Run the
  dense and BM25 legs separately and fuse rankings yourself (RRF).
- **No background optimizer.** Nothing is indexed and no deleted space is reclaimed until
  you call `optimize()`.
- **Dense embeddings come from `fastembed`** (or the team model); **BM25 is built into
  Edge** (`Bm25`, `EdgeSparseVectorParams(modifier=Modifier.Idf)`, `embed_document` /
  `embed_query`). Do not add a second BM25 library.
- **Snapshots (pull direction):** `GET /collections/{c}/shards/0/snapshot`, and partial
  via `snapshot_manifest()` → `POST .../snapshot/partial/create` → `update_from_snapshot`.
- **Config gotchas:** `EdgeConfig` rejects empty vectors+sparse; `create()` fails on a
  populated dir (use `load()` to reopen); the search pool defaults to 4 threads per core
  (set `max_search_threads` / `search_pool_core`).
- **Sources:** Qdrant Edge overview, Edge API, BM25, Data Synchronization Patterns, and
  the Synchronize-with-a-Server guide on qdrant.tech/documentation/edge.

---

## 4. Traps that fail silently — each gets a guard

| Trap | Symptom | Guard |
|---|---|---|
| Calling filtered search "hybrid" | dense and BM25 ranks never differ | two separate queries + your own RRF |
| Forgetting `optimize()` | inserts seem to vanish; sparse index stale | `optimize()` after each write batch |
| Marking synced before push | permanent silent data loss on a failed push | mark synced only after hub ack |
| Random point IDs per device | dedupe is a no-op; duplicate query results | deterministic ID from `corroboration_key` + value hash, or propagate the hub event id |
| Trusting device wall-clocks in the fold | wrong conflict resolution | order by hub-assigned sequence; timestamp is metadata |
| `embed_query` vs `embed_document` swapped | silently worse BM25 results | assert the BM25 leg on a keyword-only query |
| Two unrelated embedding models for cross-modal | text query never returns the image | one CLIP-family model, separate named vectors |
| Zero/NaN vectors from an embedder | cosine returns arbitrary rows | conformance suite; zero vector raises |
| Reporting container dir size as RAM | misleading memory numbers | read real cgroup RSS; WAL is pre-allocated |

---

## 5. Correctness invariants — write a test for each

1. **Mark after push, never before.** Kill transport mid-push → point still pending and
   still on device.
2. **`optimize()` before you believe a write.** Write then query with no restart → point
   retrievable.
3. **Eviction never touches unsynced points.** Overfill with pending points → none evicted.
4. **Deterministic point IDs across devices.**
5. **The fold ignores device clocks.** Skew a clock by an hour → resolved value unchanged.
6. **The fold is pure.** Shuffle event order → identical output.
7. **Retraction beats re-observation.** observe → retract → re-observe → stays absent
   until a newer observe, and a terminal retraction forces trust to 0.
8. **Query path never touches the network layer.** Static import check + a functional
   query under `offline`.
9. **Every point records its embedding model + version.**
10. **Shard vector dims match the resolved adapters on reopen.** Refuse to start on
    mismatch rather than returning quietly wrong scores.

---

## 6. Build order (mirrors `backend.md` §11)

Phases 1–6 are load-bearing; 7–10 are enrichment. Each phase closes on a test or a
measurement, not a look.

1. Edge shard + capture + hybrid query, offline (query imports no transport).
2. Model Adapter Registry + conformance suite (incl. `FakeEmbedder` and CLIP).
3. Decision Engine (verdict + readable reason per capture).
4. Real Qdrant Server hub + outbox push (crash-safe, delta-only).
5. Partial-snapshot pull (a device learns a fact it never captured).
6. Consensus fold + retractions (disagree → converge; retraction can't ghost; pure fold).
7. Network layer (offline/degraded/full with real, measured effects).
8. Answer layer — on-device RAG (offline Ollama / online cloud, sourced, path reported).
9. Trust decay + semantic conflict detection + resolver-vs-LWW benchmark.
10. Telemetry on the emulated constrained target (real cgroup numbers on screen).

Frontend (`frontend.md`) is built alongside from phase 3 onward; the Conflict Theater is
last and gets the most care.

---

## 7. Repo layout

```
edge-node/                    the repo root (pushed to GitHub)
  README.md                   the pitch (the only doc outside docs/)
  docs/
    00-problem-statement.md   the sponsor brief — what & why (source of truth)
    10-prd.md                 product & software requirements (FR/NFR + acceptance)
    20-architecture.md        system view (context, node, sync, consensus diagrams)
    backend.md                engineering spec
    frontend.md               UI spec
    API.md                    REST/WebSocket route contract
    AGENTS.md                 this file (build guide)
  src/edge_node/              FastAPI service, registry, decision engine, sync, consensus
  config/                     per-vertical YAML (disaster-response.yaml, ...)
  tests/
  pyproject.toml              deps; qdrant-edge-py pinned, never floated
  cloud-gateway/              schema middleware + consensus fold (created at phase 4)
  frontend/                   React app (created alongside from phase 3)
  docker/                     Qdrant Server + constrained-container definitions
```

Resolve paths from the package location, never the cwd (tests chdir into temp dirs).

---

## 8. Hard constraints

- **Backend:** Python 3.11+, FastAPI, `qdrant-edge-py` on device, real Qdrant Server hub.
  No SQL in the runtime path.
- **Generation:** Ollama (default `qwen2.5:1.5b`) offline; a cloud model online; both
  behind one `Generator` protocol; `extractive` fallback for tiny devices.
- **Frontend:** React + Vite + TS + Tailwind, no component library; thin renderer.
- **Models:** swappable via config, never hardcoded; one CLIP-family cross-modal embedder
  for text + image.
- **The kernel stays generic:** disaster-response is YAML, not code.

---

## 9. Definition of done

- Every invariant in §5 has a test that fails when the invariant breaks.
- All eight PS goals meet the acceptance lines in `00-problem-statement.md` §4.
- Measured on screen: hybrid vs dense recall@5; resolver vs LWW accuracy; real
  latency/RAM on the emulated constrained target; a `DISPUTED` state with both values; a
  retraction staying dead; trust moving; the same question answered offline and online.
- `qdrant-edge-py` pinned and committed; no placeholder values in shipped config.
- No claim in `README.md` we cannot link to a source for; Qdrant Edge's beta status
  re-checked against live docs before presenting.
