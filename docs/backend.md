# Aegis Edge — Backend Specification

**Role of this document:** the engineering core. It defines every backend part, what
each part is *for*, and what we *expect* from it (the acceptance bar). Read
`00-problem-statement.md` first for *what* and *why*; read `AGENTS.md` for the build
guide and the verified Qdrant facts. `frontend.md` is the UI.

**One-line mental model:** every device is a **smart notepad + a walkie-talkie**. The
notepad is local Qdrant Edge memory that searches and answers offline. The
walkie-talkie is sync + trust-weighted consensus: many devices report the same
real-world fact, some are wrong, and the fleet converges on what to believe when they
can finally talk.

---

## 0. Principles (these override convenience)

1. **Qdrant everywhere, nothing else.** Qdrant Edge on device, Qdrant Server in the
   cloud. It is the only datastore in the system. **No SQL, no second store**, on
   device or in the gateway. This is what removes the "two copies of a fact drift
   apart" class of bug.
2. **Real code, emulated hardware.** Nothing is hardcoded, mocked, or faked in the
   runtime path. The only things emulated are the physical device (a real CPU/RAM-
   limited container) and the network conditions (real injected latency/loss). Every
   number shown is measured from the real system. See §2.
3. **Models are config, not code.** Every model (text embedder, image embedder,
   generator) is a pretrained checkpoint selected in YAML and reached through a
   `Protocol` interface. Swapping a model is a one-line config change. We never train
   weights and never claim to. See §4.
4. **Offline is the default, not a mode.** The query and answer paths must not import
   or reference the network layer at all — proven by a test. Connectivity only affects
   sync.
5. **Done means measured.** A part is done when its acceptance line holds and a test or
   an on-screen measurement proves it — not when it "works once."

---

## 1. Architecture

```
  Edge Node (one real process per device, in a CPU/RAM-limited container)
  ├─ FastAPI app (REST + WebSocket)
  ├─ Qdrant Edge: mutable shard  (local writes, indexed on optimize())
  ├─ Qdrant Edge: immutable shard (mirror of the hub via partial snapshots, HNSW)
  ├─ Model Adapter Registry (text embedder, image/CLIP embedder, BM25, generator)
  ├─ Decision Engine (per-fact verdict + human-readable reason)
  ├─ Outbox (a filtered view over the mutable shard — NOT a second store)
  ├─ Answer layer (on-device RAG: retrieve → augment → generate)
  └─ Telemetry (real cgroup/process CPU, RAM, latency)
        │
        │  sync transport ONLY passes through the Network Layer (offline/degraded/full)
        ▼
  Cloud Gateway (one real FastAPI process)
  ├─ schema middleware (payload versioning)
  └─ Consensus fold (trust-weighted resolution over an append-only event log)
        │
        ▼
  Qdrant Server (real, Docker `qdrant/qdrant`) — collection `fact_events`, append-only
        ▲
  Frontend (React + Vite + TS + Tailwind) — thin renderer + device switcher
```

**Two shards per device**, per Qdrant's documented sync pattern:

| Shard | Holds | Indexed | Role |
|---|---|---|---|
| `mutable` | everything this device captured locally | on `optimize()` | authoritative local store; the outbox is a filtered view over it |
| `immutable` | a mirror of the hub, restored from partial snapshots | HNSW | fleet knowledge; never written to locally |

Queries read **both** shards and dedupe by point ID. A point pushed up and later pulled
back exists in both; the union is the device's full view.

---

## 2. Physical model — how we emulate the edge (all effects real and measured)

We have no physical Pi. We emulate the *physical world* in four dimensions; each
emulation produces genuine, measurable behavior — never a label or a canned number.

### 2.1 Compute + memory (the device hardware)
- Each edge node runs in a **real Docker container with hard limits** — e.g.
  `--cpus=1 --memory=512m` for the tiny target, `--memory=4g` for a node that also
  hosts the Ollama model. The constraint is genuine: the process really has that core
  and that RAM.
- **What we expect:** telemetry reads **actual cgroup stats** and process RSS. CPU %,
  RAM MB, and query `latency_ms` on screen are measured. Labeled honestly as
  *"emulated constrained target (1 CPU / 512 MB container)"* — not "Raspberry Pi."
  Swap the container for real hardware later: same image, relabel, zero code change.

### 2.2 Network conditions (the link)
- A **transport-layer interceptor** wraps the sync HTTP client only. One global control
  `POST /network/mode` with three modes:

  | Mode | Real behavior on the wire | Sync worker |
  |---|---|---|
  | `offline` | connection refused immediately | does not attempt; outbox accumulates; device stays fully queryable |
  | `degraded` | real injected latency (default 400–1200 ms), a real byte-rate cap via a token bucket over actual payload bytes (default ~64 kbps), and a real failure rate (default ~8% of requests genuinely error) | small batches, priority-ordered so urgent facts drain first, real per-point retry accounting |
  | `full` | no injection | normal batching, both directions |

- **What we expect:** because the cap and failures are real, crash-safety
  (mark-after-push) and the byte savings of quantized payloads are exercised for real,
  and bytes-sent / points-failed / retries are measured, not asserted.

### 2.3 Connectivity transitions (going offline / reconnecting)
- Flipping the mode is a real event. `offline` → outbox grows; → `full`/`degraded`
  drains it to real Qdrant Server and pulls snapshots back.
- **What we expect:** "reconnect the fleet" is one global flip — genuinely
  simultaneous, so the consensus merge is a real reconciliation, not a scripted
  animation.

### 2.4 Time (clocks)
- A per-device **clock-skew knob** (metadata only). The consensus fold orders events by
  a hub-assigned sequence and ignores device wall-clocks.
- **What we expect:** skewing a device clock by an hour does not change the resolved
  value — proven by a test.

### 2.5 Query isolation (the honesty proof)
- **Hard rule:** the query/retrieval and answer modules never import the transport or
  the network simulator.
- **What we expect:** a static test asserts the import graph, plus a functional query
  under `offline`. This is the proof that "instant offline search" is architectural.

---

## 3. Model Adapter Registry — the config-driven model table

**Purpose:** make the kernel model-agnostic and modality-agnostic. Which models run is
declared in YAML and resolved into adapter objects at startup. Changing a model is a
one-line edit; no kernel code changes.

**What we are NOT doing:** we do not train models and we never say "training weights."
We reference **pretrained checkpoints** by name and stamp a `version` (the checkpoint
id) on every point, so a checkpoint change is detectable rather than silently comparing
incompatible vectors.

### 3.1 The interfaces (structural typing, so a model drops in unmodified)

```python
from typing import Protocol, runtime_checkable, Literal

Modality = Literal["text", "vision"]

@runtime_checkable
class Embedder(Protocol):
    name: str        # named vector field in the shard, e.g. "text_dense"
    dim: int         # output dimensionality (derived from the model, never hardcoded)
    modality: Modality
    version: str     # pretrained checkpoint id; stamped on every point
    def embed_batch(self, payloads: list) -> list[list[float]]: ...  # unit-norm vectors

@runtime_checkable
class Generator(Protocol):
    name: str
    def generate(self, question: str, context: list[str]) -> str: ...  # answer only from context
```

### 3.2 The registry table (config)

```yaml
models:
  embedders:
    - name: text_dense
      modality: text
      provider: fastembed
      model: BAAI/bge-small-en-v1.5
      version: bge-small-en-v1.5
      dim: 384
    - name: image
      modality: vision
      provider: fastembed
      model: Qdrant/clip-ViT-B-32          # cross-modal: a text query retrieves a photo
      version: clip-ViT-B-32
      dim: 512
  sparse:
    - name: text_bm25
      provider: qdrant-edge                # BM25 is built into Qdrant Edge — no extra lib
      modifier: idf
  generator:
    offline:                               # default on-device model
      provider: ollama
      model: qwen2.5:1.5b
      version: qwen2.5-1.5b-instruct
      endpoint: http://localhost:11434
      fallback: extractive                 # if the box is too small, stitch top snippets
    online:
      provider: openai-compatible
      model: <cloud-model-id>
      version: <cloud-model-id>
```

### 3.3 What we expect
- A **conformance suite** every embedder must pass (real or fallback): output length ==
  input length and order preserved; `len(vec) == dim`; unit norm; determinism (same
  input → same vector); distinct inputs → distinct vectors; no NaN/Inf; zero vector
  raises; wrong-modality payload raises; non-empty `version`.
- A `FakeEmbedder` (deterministic vectors hashed from the payload) so the whole system
  is buildable and testable without downloading a model.
- Cross-modal text and image use one CLIP-family model in two named vector fields of the
  same shard — two unrelated models cannot retrieve across each other.

---

## 4. Decision Engine — what stays local, what syncs, and why

**Purpose:** goal 2. Run synchronously the instant a fact is captured. Output one of
`KEEP_LOCAL | QUEUE_LOW | QUEUE_HIGH | REDACT_AND_QUEUE | REJECT` plus a **plain-language
reason** (the reason is the product, not a debug log). All thresholds come from config.

Pipeline, in order:

1. **Novelty** — k-NN against the device's own mutable shard. ≥0.98 similarity to a
   recent fact → `REJECT` (redundant). <0.70 → treat as high-priority novel info.
2. **Urgency** — a config-driven keyword/category score (0–1). `> 0.8` → `QUEUE_HIGH`
   regardless of bandwidth (e.g. "structural collapse," "gas," "no pulse").
3. **Sensitivity / PII** — regex/allowlist pass; sensitive data is redacted before
   queueing or flagged `local_only` and never queued. We surface bytes-sent-to-cloud
   for a redacted record so the privacy claim is observable.
4. **Completeness** — a fact can't leave a `QUEUE_*` state until required fields are
   present (e.g. `status`, `timestamp`, `reporter_device_id`).
5. **Bandwidth tiering** — at transmit time, `QUEUE_HIGH` sends full vector+payload even
   on a degraded link; `QUEUE_LOW` sends a quantized vector + id and only on a good link.

**What we expect:** every captured fact appears in the decision feed with a verdict and
a reason a non-expert can read, traceable to the config value that produced it.

---

## 5. Local memory management (evolving memory)

**Purpose:** goal 7. The brief names "evolving local memory," so it is in scope.

- Config-driven cap (`max_local_points` and/or `max_age_hours`).
- When exceeded, evict oldest-and-least-queried **but only points already confirmed
  synced to the hub** — never anything still pending in the outbox. Eviction is a
  cache pop, not data loss (the fact is retrievable from the hub on next reconnect).
- Eviction only reclaims space after `optimize()` (Qdrant Edge has no background
  optimizer).
- Counts come from `count()` / `facet()` with payload indexes, never app-side loops.

**What we expect:** overfill the cap with pending points → none evicted (test). The UI
shows `used/cap` (e.g. "340/500 points") so eviction is visibly working. Cap the hub
collection too, so `hub size × device count` RAM stays bounded.

---

## 6. Sync protocol — real Qdrant Server, delta-only, both directions

**Purpose:** goal 3. There is no `.sync()` in Qdrant Edge; sync is a pattern we
assemble from shard helpers + our own transport, per Qdrant's sync guide.

### 6.1 The outbox is a view, not a store
- The pending queue is a **filtered `scroll` over the mutable shard** where
  `_sync_meta.synced == false`, ordered by `(sync_priority, client_sequence)`.
- **Mark `synced: true` only after the hub acknowledges the push.** Never before — that
  is silent, permanent data loss. Re-push after a crash is safe because upsert is
  idempotent.

### 6.2 Push (dual-write to the real server)
- Drain in batches. For each point, write the envelope
  `{id, vector, payload, _sync_meta}` to the **real Qdrant Server** collection and, on
  success, mark the mutable point synced and append an `OBSERVED` event to `fact_events`.
- Delta handshake: ask the hub for the highest `client_sequence` it holds for this
  device and push only rows above it. No full snapshots.

### 6.3 Pull (indexing offload + learning from the fleet)
- `immutable_shard.snapshot_manifest()` → POST to the server's partial-snapshot endpoint
  → `update_from_snapshot(path)`. Incremental, per the manifest — never a full snapshot.
- Pause/buffer local writes during restore; flush queued data to the server first so the
  snapshot isn't stale; dedupe the mutable shard by `timestamp <= sync_timestamp` exactly
  as Qdrant's guide demonstrates.

**What we expect:** cut the network, capture facts, reconnect → the real Qdrant Server
collection updates with no manual step, and a device learns a fact it never captured
(sourced from another device). Device-to-device reach is A → hub → snapshot pull → B;
there is no peer link, stated explicitly.

---

## 7. Trust / Consensus resolver — the novel layer (the walkie-talkie)

**Purpose:** goal 7's "conflicting information," done properly. With a dozen devices
reporting the same real-world fact while disconnected, last-write-wins is wrong.

### 7.1 Event-sourced, everything immutable
- Cloud memory is an **append-only log** in the `fact_events` collection. Three event
  types: `OBSERVED`, `RETRACTED`, `CONFLICT_OPENED`. Nothing is ever mutated or deleted.
- **Retraction can't ghost** because it is an append, not a delete — it propagates via
  the same snapshot mechanism as any point.
- Trust is **derived from the log**, never stored beside it, so it can't drift out of
  sync with history.

### 7.2 The fold (pure function of the log)
- Select all live events for a `corroboration_key`, ordered by **hub-assigned sequence**
  (not device clocks), and resolve:
  - one device → last-write-wins fast path;
  - multiple agreeing → `CONFIRMED`, confidence rising with corroboration count × trust;
  - multiple disagreeing → do not silently pick; if confidence stays below threshold,
    mark `DISPUTED` and surface **both** values;
  - different fields of the same record → non-destructive field merge.

### 7.3 Trust that visibly moves
- Device trust = running agreement rate over its `OBSERVED` events, recency-decayed,
  recomputed each fold. The dashboard must show it move ("Device C: 0.90 → 0.42").

### 7.4 Semantic conflict detection (the vector index as detector)
- `corroboration_key` only catches conflicts when two devices pick the *same* key. Real
  reports use different words. So on ingest, hybrid-query the hub for reports above a
  similarity threshold, with a **hard zone/entity guard** ("Zone C gas leak" never merges
  with "Zone D gas leak"), and surface anything that clears it as a reviewable
  `POSSIBLE_CONFLICT` — **never auto-merge**.

### 7.5 Prove it beats LWW
- A script over a labeled fixture set reports resolver accuracy vs a last-write-wins
  baseline. The number is computed, not asserted.

**What we expect:** two devices disagree offline → converge on reconnect with a
confidence score; a retracted fact stays gone everywhere; shuffled event order →
identical fold output (test); trust score moves; a conflict the key scheme would miss is
surfaced.

---

## 8. Answer layer — on-device RAG (the meaningful AI workflow)

**Purpose:** goal 8. Retrieval returns chunks; RAG returns an answer. This is the line
between passing and failing the brief.

- **Retrieve** — the hybrid query (§9) over both shards.
- **Augment** — put the top-k retrieved facts into the prompt as the only allowed
  context; prefer consensus-`CONFIRMED` facts and flag `DISPUTED` ones in the answer.
- **Generate** — one `Generator` interface, two backends: **Qwen2.5-1.5B via Ollama**
  offline, the cloud model online; `extractive` fallback if the device is too small.
- The answer cites the **source point IDs** so it is verifiable, and the response
  reports **which path served it** (offline/online).

**No RAG framework** (LangChain/LlamaIndex): Qdrant Edge is the retriever; augment +
generate is a prompt-assembly + Ollama call. Keeping it lean matters on a constrained
device.

**What we expect:** the same question yields a real generated answer offline and online,
both grounded in retrieved context with sources, and the UI shows the path.

---

## 9. Hybrid search — dense + sparse, fused (offline)

**Purpose:** goal 5. Hybrid = dense + BM25 **fused**, not "vector + payload filter."

1. Dense leg: `Query.Nearest(dense_vec, using="text_dense")`.
2. Sparse leg: BM25 `embed_query` → `Query.Nearest(sparse, using="text_bm25")`.
3. Fuse with Reciprocal Rank Fusion; run over both shards; dedupe by point ID.
- Always `embed_query` for queries and `embed_document` for documents (BM25 weights
  them differently). Index every field we filter/facet on (`zone`, `modality`,
  `status`, `urgency_score`, `corroboration_key`, `device_id`, `synced`).
- Call `optimize()` after write batches (no background optimizer). Set
  `max_search_threads` / `search_pool_core` explicitly (default is 4 threads per core).

**What we expect:** measured recall@5, dense-only vs hybrid, hybrid strictly higher, all
offline.

---

## 10. API surface (contract for the frontend)

| Endpoint | Purpose |
|---|---|
| `GET /devices` | fleet state |
| `WS /devices/{id}/events` | live decision-feed + activity stream |
| `POST /devices/{id}/capture` | submit a text or image fact |
| `POST /devices/{id}/query` | hybrid search + RAG answer; returns answer, `latency_ms`, source ids, path (offline/online) |
| `POST /network/mode` | global network state: offline / degraded / full |
| `WS /consensus/events` | resolver decisions for the Conflict Theater |
| `GET /cloud/state` | merged trusted picture from the fold |
| `GET /devices/{id}/telemetry` | real CPU / RAM / latency |
| `GET /devices/{id}/sync` | sync status (pending-by-priority via facet, last attempt/success, bytes, mode) |
| `GET /devices/{id}/activity` | system-activity ring buffer, newest first |
| `POST /devices/{id}/retract/{point_id}` | append a RETRACTED event |
| `GET /devices/{id}/conflicts` | POSSIBLE_CONFLICT candidates |
| `GET /benchmark/resolver-vs-lww` | computed resolver-vs-LWW accuracy |

---

## 11. Build phases — what each phase delivers, and what proves it

Each phase states what we build, why it exists, and the test/measurement that closes it.
Phases 1–6 are load-bearing; 7–10 are enrichment. **Qdrant is exercised from phase 1.**

| # | Phase | What we need it to do | Done when (test/measurement) |
|---|-------|-----------------------|------------------------------|
| 1 | **Edge shard + capture + hybrid query** | prove local Qdrant Edge memory works offline | query returns ranked results with measured `latency_ms` under `offline`; query module imports no transport (static test) |
| 2 | **Model Adapter Registry + conformance** | make models config-swappable, incl. cross-modal | every embedder (incl. `FakeEmbedder` and CLIP) passes the conformance suite; a model swaps by YAML only |
| 3 | **Decision Engine** | decide local vs sync, explainably | every capture has a verdict + readable reason traceable to config |
| 4 | **Real Qdrant Server hub + outbox push** | delta-only push to a real server, crash-safe | kill transport mid-push → point still pending & on device; reconnect → real Qdrant Server collection updates |
| 5 | **Partial-snapshot pull** | a device learns from the fleet | a device answers with a fact it never captured, sourced from another device |
| 6 | **Consensus fold + retractions** | trust-weighted convergence; retraction can't ghost | two devices disagree offline → converge with confidence on reconnect; retracted fact stays gone; shuffled order → identical fold |
| 7 | **Network layer (offline/degraded/full)** | real intermittent connectivity | `offline` unchanged query latency; `degraded` shows measured added latency, byte cap, real failures; urgent drains first |
| 8 | **Answer layer (on-device RAG)** | the meaningful AI workflow | same question answered offline (Ollama) and online (cloud), grounded + sourced, path reported |
| 9 | **Trust decay + semantic conflict + benchmark** | novelty, measured | trust visibly moves; a missed-by-key conflict is surfaced; resolver-vs-LWW accuracy printed |
| 10 | **Telemetry on the emulated constrained target** | real device numbers on screen | CPU/RAM/latency read from real cgroup, labeled as emulated container |

---

## 12. Non-goals

- No production auth / multi-tenant cloud — one Qdrant Server collection is enough.
- No model training — pretrained, config-swappable checkpoints only.
- No custom ANN index — Qdrant's as-is.
- No SQL and no second datastore anywhere.
- Demo-scale limits, stated out loud: the fold is a `scroll` per `corroboration_key`
  per cycle (fine to low thousands); the hub grows monotonically by design (deletions
  are events); the immutable shard is a full local copy of the hub.
