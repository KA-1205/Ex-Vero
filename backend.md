# Aegis Edge — Backend Spec

**Role of this doc:** the engineering core. Everything here is designed to be judged, not just to work — each module maps to a specific novelty claim. Model/embedding choices and training/data prep are intentionally left out (owner: you, separately).

**Companion docs:** `AGENTS.md` (problem definition + verified Qdrant Edge facts + build order — read it first), `README.md` (the pitch), `frontend.md` (UI spec).

---

## 0. Assumptions and hard constraints

- **Python 3.11+ + FastAPI per edge node.** Qdrant Edge ships Python and Rust bindings only; Python is the fastest path for a coding agent to iterate on.
- **Qdrant Edge (`pip install qdrant-edge-py`) on every edge node — not `QdrantClient(path=...)`.** These are different products. Local mode is a pure-Python reimplementation that warns above 20k points and has no snapshots, no BM25, and no sync. This project requires all three. See `AGENTS.md` §5.
- **One Qdrant Server (or Qdrant Cloud) collection as the hub.**
- **No SQL on the edge node.** The outbox is a filtered view over the mutable Edge shard, not a second store. See §5.1. The gateway has no SQL either — consensus is a pure fold over an append-only Qdrant collection. See §4.
- **WebSocket** for live event streams (decision feed, consensus events, telemetry); **REST** for request/response.
- **Small-device target:** Raspberry Pi 4/5, or a 512MB / 1-CPU Docker container as an honest fallback. Note that the answer layer in §6.5 does not fit the 512MB fallback — see that section before choosing a target.
- **Qdrant Edge is in beta.** Pin the exact version in `pyproject.toml`/`uv.lock` and never float it. The API drifts between releases.

---

## 1. Architecture: the kernel is generic, the demo is the vertical

Everything below is a reusable **Edge Memory Kernel**. The disaster-response scenario only supplies configuration (which model adapters, which decision policy weights, what counts as "urgent") — none of it is hardcoded into the kernel itself. This is the actual claim to defend under judge questioning: swap the config, and the same kernel runs a retail kiosk or a field-service device with zero code changes.

```
┌───────────────────────────────────────────────────────────────┐
│                      EDGE NODE PROCESS                          │
│                                                               │
│  Capture ──► Model Adapter Registry ──► mutable Edge Shard     │
│  (text/img)    (config-driven)           (local writes)         │
│                                                │          │     │
│                                      Decision │          │ outbox│
│                                        Engine │          │ view  │
│                                                ▼          ▼     │
│                                          verdict + reason      │
│                                                               │
│  QUERY ──► immutable Edge Shard ──┐                             │
│           (server snapshot, HNSW) ├─► fuse (RRF) ──► answer    │
│           mutable Edge Shard    ──┘                             │
└──────────────────────────────┬─────────────────────────────────┘
                               │ delta push (dual-write, batched)
                               │ partial snapshot pull (indexing offload)
                               ▼
              ┌────────────────────────────────────────┐
              │        CLOUD GATEWAY (FastAPI)         │
              │  schema middleware                     │
              │  consensus fold   ◄── the novel piece  │
              └────────────────────┬───────────────────┘
                                   ▼
              ┌────────────────────────────────────────┐
              │  Qdrant Server — collection fact_events │
              │  append-only, immutable points         │
              └────────────────────────────────────────┘
```

Two Edge shards per device, per Qdrant's documented synchronization pattern:

| Shard | Holds | Indexed? | Sync role |
|---|---|---|---|
| `mutable` | local writes, everything this device observed or captured | no — `optimize()` builds the index | authoritative local store; the outbox is a filtered view over it |
| `immutable` | mirror of a server collection, restored from partial snapshots | yes, HNSW | cloud knowledge; never written to locally |

Querying reads **both** shards and dedupes by point ID. A point may exist in both (pushed then pulled back) — the union is the device's full view.

---

## 2. Model Adapter Registry (the "plugin" layer)

This is what makes the kernel genuinely reusable rather than a one-off demo, and it's where multimodal support lives — not as a headline feature, but as proof the plugin architecture actually works.

- **The embedder is a team boundary.** The ML model is built in parallel, so the seam between "their model" and "our kernel" has to be fixed now and written down. Two rules make the eventual integration mechanical:
  1. **Structural typing, not inheritance.** Declare the interface as a `typing.Protocol`, so any object with the right methods satisfies it. If the interface is an abstract base class, the model has to be edited to inherit from it, and that edit is where integration goes wrong. With a `Protocol`, their model drops in unmodified.
  2. **We write the adapter, they hand us the model.** The adapter is a thin ~20-line file that absorbs the model's quirks — preprocessing, tensor types, prompts, batching. That work is ours, so no changes are ever requested from them. The interface below is the entire contract.
- Embedding and generation are **two different interfaces**, not one. `Embedder` is needed from step 1; `Generator` is needed at step 9 for the answer layer. Do not conflate them — an adapter that can do both is a design smell, and a text embedder cannot generate.

```python
from typing import Protocol, runtime_checkable, Literal

Modality = Literal["text", "vision"]
Payload  = str | bytes | "Image.Image"      # text | raw image bytes | PIL image


@runtime_checkable
class Embedder(Protocol):
    """Everything the kernel needs from a dense-embedding model."""

    name: str        # shard vector-field name, e.g. "text" — stable, it's in stored data
    dim: int         # output dimensionality
    modality: Modality
    version: str     # model+weights version; stamped on every point (invariant 9)

    def embed_batch(self, payloads: list[Payload]) -> list[list[float]]:
        """Must return unit-norm float lists, len == dim, in input order."""


@runtime_checkable
class Generator(Protocol):
    """The answer layer. Optional until step 9."""

    name: str

    def generate(self, question: str, context: list[str]) -> str:
        """Answer `question` grounded only in `context`. No retrieval here."""


class AdapterRegistry:
    def __init__(self, embedders: dict[str, Embedder],
                 generator: Generator | None = None): ...

    @classmethod
    def from_config(cls, path: str | Path) -> "AdapterRegistry":
        """Build from config/disaster-response.yaml. The only place that
        knows which concrete model classes exist."""

    def get(self, name: str) -> Embedder: ...      # KeyError on unknown name
    def for_payload(self, p: Payload) -> Embedder: ...   # picks by modality
```

**Normalize at the boundary, once.** Every quirk the model has gets absorbed here, so nothing downstream ever branches on tensor type or dtype:

```python
def as_unit_vector(v) -> list[float]:
    a = np.asarray(v, dtype=np.float32).reshape(-1)
    n = float(np.linalg.norm(a))
    if not np.isfinite(a).all():
        raise ValueError("embedder returned NaN/Inf")
    if n == 0.0:
        raise ValueError("embedder returned a zero vector")
    return (a / n).tolist()
```

The zero-vector check is not defensive noise. A zero vector makes cosine similarity undefined and silently degrades search to returning arbitrary rows with no error anywhere.

**Ship a `FakeEmbedder` from step 1.** Deterministic vectors seeded from a hash of the payload — same input always gives the same vector, distinct inputs give distinct vectors. It is not semantic and will not make search work, but it makes the *plumbing* buildable and testable today, and it is what keeps the consensus-fold and sync tests free of a model download. Design note: a constant-vector fake would pass a smoke test and fail everything real, so the conformance suite below deliberately tests that distinct inputs diverge.

**Conformance suite — the actual integration contract.** Any `Embedder`, real or fake, must pass these. This is what makes "plug in" mean *adapter passes these tests* rather than *debug for an afternoon*:

| Test | Catches |
|---|---|
| `len(out) == len(in)`, order preserved | batching/dropped inputs |
| `len(out[0]) == adapter.dim` | dim declared ≠ dim produced |
| `‖v‖ == 1.0` within tolerance | unnormalized output silently breaking cosine |
| same input twice → identical output | nondeterminism (unseeded sampling, dropout at inference) |
| two different inputs → different vectors | a stub or constant fake |
| no NaN/Inf; zero vector raises | the two silent-degradation cases above |
| wrong-modality payload raises a clear error | a model that accepts anything and returns nonsense |
| `version` is non-empty and differs across weight versions | two different models passing as the same one |

Write this suite **before** the real model exists, and point it at the fake. When the model arrives, the suite is already green and the question is only whether their adapter passes — which is a yes/no, not an investigation.

- **Critical: text and image must be embedded by the *same* cross-modal model** if you want cross-modal retrieval (a text query returning a relevant photo). Two unrelated embedding models cannot retrieve across each other no matter how they are stored. Use one CLIP-class model for both modalities and put them in separate **named vector fields** of the same shard. In protocol terms, that means two `Embedder` instances sharing one `version` and one model family, not two independently chosen models.
- Both write into the same local Edge shard as **named vectors** (`"text"`, `"vision"`) with a `modality` payload field, so text and image memories are searchable side by side.
- Dense embeddings are **not** part of Qdrant Edge — they come from the separate `fastembed` package, or from the team model. BM25 sparse vectors, by contrast, **are** built into Edge. Do not add a second BM25 library.

**Dimensions come from the adapter, not from the spec.** A hardcoded `size=384` in this file would bind the kernel to whichever model happens to be 384-dimensional, which is the exact coupling the registry exists to remove. Build `EdgeConfig` from whatever the YAML resolved:

```python
adapters = registry.load(config)          # which models, from config
vectors  = {a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine)
            for a in adapters}
shard    = EdgeShard.create(path, EdgeConfig(vectors=vectors, ...))
```

**Swappable means chosen at deploy time on a fresh shard — never hot-swapped on a live one.** Dimensions are baked into a shard's stored vectors, so switching models means recreating the shard or re-embedding it. Loading a shard written by a different model yields shape mismatches that surface as *wrong scores rather than errors*, so assert the resolved adapter dims against the shard's stored vector config on every reopen and refuse to start on a mismatch (`AGENTS.md` §6.2, invariant 10).

---

## 3. Decision Engine (per-fact policy — also pluggable)

Runs synchronously the instant a new local vector is created. Output: `KEEP_LOCAL | QUEUE_LOW | QUEUE_HIGH | REDACT_AND_QUEUE | REJECT`, plus a human-readable reason string (the frontend's decision feed depends on this string being genuinely readable, not a code).

Scoring pipeline, in order (each policy's weights come from the same use-case config, so this whole stack is swappable per vertical):

1. **Novelty check** — k-NN against the node's own local collection. ≥0.98 similarity to something logged in the last N minutes → `REJECT` (redundant). <0.70 similarity → treat as high-priority novel information. This is a nearest-neighbour query against the mutable shard, not an application-side loop.
2. **Urgency score** — a lightweight rule/classifier tags payloads (`urgency_score` 0–1) at capture time from a small config-driven keyword/category list for this use case (e.g. "structural collapse," "gas," "no pulse" score high in the disaster config). `urgency_score > 0.8` → `QUEUE_HIGH` regardless of current bandwidth.
3. **Sensitivity/PII filter** — regex/NER pass; anything sensitive is either stripped to an anonymized embedding or flagged `local_only: true` and never queued at all. **Demo requirement:** surface the bytes-sent-to-cloud for a redacted record on screen so the privacy claim is observable, not asserted.
4. **Completeness gate** — a fact can't leave `QUEUE_*` state until required fields for this use case are populated (e.g. `status: verified`, `timestamp`, `reporter_device_id`) — stops half-formed captures from ever reaching the shared picture.
5. **Bandwidth tiering** — when actually transmitting, `QUEUE_HIGH` sends the full vector+payload even on a degraded link; `QUEUE_LOW` sends a Scalar-quantized vector + ID only, and only on an unmetered/strong connection. Qdrant Edge supports Scalar, Product, Binary, and TurboQuant quantization, configured on `EdgeConfig.quantization_config` or per-vector. Because the transport is your own code, bytes-over-wire is genuinely measurable — log before/after and surface the number.

---

## 3.5 Local Memory Management (evolving local memory)

The problem statement names this explicitly ("handle evolving local memory") so it's in scope for the demo, not a stretch goal.

- Local store has a config-driven cap (`max_local_points` and/or `max_age_hours`).
- Once a device's promoted/synced facts exceed the cap, evict oldest-and-least-queried first — but **only points already confirmed synced to the cloud**, never anything still pending in the outbox. Eviction is a cache-style pop, not a data-loss risk.
- **Eviction only actually reclaims space after `optimize()`.** Edge has no background optimizer — deleted points linger until you call it. See `AGENTS.md` §6.
- Point counts and per-zone/per-status breakdowns come from `count()` and `facet()` with payload indexes, not application-side aggregation. Surface a "local memory: 340/500 points" indicator in the Fleet Overview tile so eviction is visibly working.
- **Cap the hub collection too.** The `immutable` shard is a full local copy of the server collection, on every device. Unbounded hub growth × N devices is the most likely way this demo runs out of memory on stage, and nothing warns you. Pick a demo-scale number and hold it.
- On eviction, the point isn't gone — it's retrievable from the cloud on next reconnect if actually needed again; the edge node just stops holding a local copy.

---

## 4. Trust/Consensus Resolver — the crown novel feature

The piece that doesn't exist in any competitor or prior-art system checked. Lead with this in any pitch.

The problem: with a *dozen* devices, not two, simultaneously reporting on the same real-world fact while disconnected, simple last-write-wins is wrong — the newest report isn't necessarily the correct one, and you need to know *how sure* the system is before it goes on a dispatcher's dashboard.

### 4.1 Design: event-sourced, every fact immutable

The resolver is a **pure fold over an append-only event log**. Nothing is ever mutated or deleted. Three event types, all stored as ordinary points in the Qdrant Server `fact_events` collection:

| Event | Meaning |
|---|---|
| `OBSERVED` | a device reported something about a real-world fact |
| `RETRACTED` | someone withdrew or corrected a previous `OBSERVED` event |
| `CONFLICT_OPENED` | derived marker that a `corroboration_key` has unreconciled disagreement |

This is not a stylistic choice — it is what makes the guarantees hold:

- **Retraction cannot ghost.** A retraction is an append, not a delete, so it can never be half-applied or lost by a partial write. It propagates to every device through the same partial-snapshot mechanism as any other point. This is the structural answer to "deleted facts staying dead."
- **Trust cannot drift out of sync with history**, because trust is *derived from* the log rather than stored beside it.
- **No transactions are required**, because the resolver never performs a shared read-modify-write. It reads a group and emits derived state.

Every `OBSERVED` event carries `_sync_meta`: `device_id`, `client_sequence`, `client_timestamp_ns`, `payload_hash`, `device_trust_score_at_time_of_report`, `urgency_score`, plus `corroboration_key` — a normalized key identifying what real-world fact this is about (e.g. `zone_c.hazard_status`).

### 4.2 The fold

On reconnect, the gateway selects all live events for a given `corroboration_key` (payload-indexed `scroll`, retraction-aware) and resolves:

1. **One reporting device** → fall back to plain **LWW** (fast path; no need to reinvent this part).
2. **Multiple devices, agreeing values** → `CONFIRMED`. Confidence is a function of corroboration count and device trust weights. This populates the Command Dashboard.
3. **Multiple devices, disagreeing** → do **not** silently pick one. Compute a weighted vote (corroboration count × device trust × recency decay). If confidence stays below a threshold, mark the fact `DISPUTED` and surface **both** values to the dashboard rather than hiding the disagreement — a false single answer is worse than a visible dispute in this domain.
4. **Field-level patch merge** still applies underneath: if two devices touched *different* fields of the same record, merge non-destructively instead of treating it as a conflict at all.

### 4.3 Trust is derived, and should visibly move

Device trust = running agreement rate over that device's `OBSERVED` events in the log (agreements / (agreements + disagreements)), decayed by recency. It is recomputed on each fold, never stored as an independent mutable number.

Requirement: **the dashboard must show trust moving.** "Device C: 0.90 → 0.42 after 3 disagreements" is the difference between a weighted average and a system that is visibly learning who to believe. A static trust score is just a config value and is not a claim worth defending.

### 4.4 Prove the resolver beats last-write-wins

Do not assert this — measure it. Build a small labeled fixture set (≈30 disaster facts with known conflicting values), generate a few hundred simulated dispute scenarios, and report resolver accuracy against an LWW baseline on the same scenarios.

Target framing: *"resolver selects the correct value 94% of the time; LWW baseline selects correctly 71%."* This single number converts the crown feature from a claim into a measurement, and it is the most persuasive artifact in the whole project. It is a script over synthetic conflicts, not new infrastructure.

Expose all resolver output over `WS /consensus/events` so the Conflict Theater can narrate it live.

### 4.5 Catching the conflicts the key scheme misses

`corroboration_key` only detects a conflict when two devices happened to pick the *same* key. Real devices describe the same hazard with different keys and different wording, so the fold in §4.2 will silently miss genuine disagreements — and it will miss them invisibly, which is the worst way to be wrong.

Supplement the exact-key path with a **semantic** one, reusing the hybrid query from §6.1 as the detector:

1. On ingest, for each incoming `OBSERVED` event, hybrid-query the hub for reports above a similarity threshold (`zone` and entity extracted from the payload as a hard guard, so "Zone C has a gas leak" never merges with "Zone D has a gas leak").
2. Any report that clears the threshold is a **candidate** claim about the same real-world fact, whether or not the keys agree.
3. Surface candidates as a reviewable `POSSIBLE_CONFLICT` — **never auto-merge.** Report the similarity score and let the dashboard or a human confirm.

**Why this matters for the pitch:** it makes the vector index the *conflict-detection mechanism* rather than passive storage, and it reuses machinery you are building anyway. The two official Qdrant demos already do retrieval well; this is the layer above retrieval that neither of them has.

Risks to be honest about: the threshold is a precision/recall knob and will need tuning; a false positive looks like a bug on stage, which is precisely why it flags rather than merges; and it costs a nearest-neighbour query per incoming report at the gateway. If it misbehaves in the demo, disable it — the exact-key path still carries moment 3.

### 4.6 Make the disagreement reproducible

Demo moment 3 depends on two devices holding different values at the same moment, which is fragile if left to chance. Ship a **conflict injector** control that forces a specific disagreement between a chosen device pair on demand, plus a "rogue device" toggle that makes a device report wrong values consistently.

This is not a gimmick: it is the only reliable way to demonstrate the crown feature twice in a row, and the rogue toggle is what makes trust decay observable rather than theoretical. Build it early — it is cheap, and it removes the biggest live-demo risk in the project.

---

## 5. Sync protocol

**There is no built-in `.sync()` in Qdrant Edge.** Sync is a pattern you assemble from shard helpers plus your own transport. This section is that assembly.

### 5.1 The outbox is a view, not a store

The pending-sync queue is a **filtered view over the mutable shard**. Every point already carries `_sync_meta.client_sequence` and `_sync_meta.synced`, so the outbox is:

```
scroll(mutable_shard, filter = synced == false, ordered by (sync_priority, client_sequence))
```

This is deliberate. The earlier design kept the same fact in two places (Edge shard + separate queue) with no rule for which is authoritative, which is a latent data-loss bug: mark the queue row sent, have the push actually fail, and the field report is gone permanently. One store removes the reconciliation problem entirely.

Rules:
- **Drain in batches**, not one point at a time — a shard-wide scroll per point will not fit a RAM-capped device.
- **Mark `synced: true` only after a successful push.** Never before. This makes re-push after a crash safe, and upsert is idempotent, so a double-push costs nothing.
- **Envelope** per point: `{id, vector, payload, _sync_meta: {device_id, client_sequence, client_timestamp_ns, payload_hash, sync_priority, is_deleted, schema_version}}`.
- **Delta handshake:** the edge asks the cloud for the highest `client_sequence` the cloud holds for its `device_id` and pushes only rows above that. No full snapshots, ever.

### 5.2 Pull direction: indexing offload

Push alone leaves devices unable to learn anything from the rest of the fleet, and leaves them unable to receive retractions. So the second half of the pattern:

1. `immutable_shard.snapshot_manifest()` → POST to the server's partial-snapshot endpoint (`/collections/{c}/shards/0/snapshot/partial/create`) → `immutable_shard.update_from_snapshot(path)`.
2. Refresh incrementally from the manifest, not with a full snapshot every cycle.
3. Pause and buffer local writes while a snapshot is being restored; flush queued data to the server first so the snapshot is not stale on arrival.
4. Dedupe the mutable shard's now-duplicated points by timestamp range, exactly as the official pattern demonstrates.

**Schema version header** (`_schema_version`) with a small ingestion middleware that backfills/translates older payload shapes — needed if you demo an "old app version" device without crashing ingestion. This is a robustness flex, not a demo beat; treat it as a stretch goal.

### 5.3 The network simulator is a transport-layer interceptor

`POST /network/mode` sets one global mode. The modes are only meaningful if `degraded` degrades something *measurable*, so simulate the transport, not a boolean.

**Hard rule: the query path must not touch the simulator at all.** Not "works anyway" — not import it, not reference it, make it a test assertion. That is the proof for R1, R5, and R6: instant offline search is only convincing if the offline switch has no code path into retrieval.

| Mode | Transport behavior | Sync worker behavior |
|---|---|---|
| `offline` | connection refused immediately | does not attempt. Outbox accumulates; device remains fully queryable |
| `degraded` | injected per-request latency (config: default 400–1200 ms), a bandwidth cap (default ~64 kbps), and a failure rate (default ~8% of requests) | small batches, **priority-ordered so `QUEUE_HIGH` drains first**, per-point retry accounting. `QUEUE_LOW` items are held back rather than sent quantized — the degraded link is for the urgent thing only |
| `full` | no injection | normal batching, both directions |

Consequences worth keeping: because `degraded` fails some requests, the mark-after-push rule in §5.1 is exercised for real rather than theoretically, and because the bandwidth cap is real, the Scalar-quantized path in §3 step 5 produces an actual bytes-over-wire number. Surface both on screen.

Log, per push: bytes sent, duration, points attempted, points accepted, points failed, priority mix, and the mode it ran under. That log is what makes demo moment 4 arguable rather than asserted.

The Conflict Theater's "reconnect both devices simultaneously" is a single global mode flip, which is why the control is global rather than per-device.

**Device-to-device reach.** The problem statement says sync "between edge devices and Qdrant Server." There is no peer link: device A's fact reaches device B by A → hub → snapshot pull → B. Routing everything through the hub is what makes the append-only consensus log possible, and it is worth stating explicitly rather than leaving a judge to infer peer-to-peer from the wording.

---

## 6. API surface (contract for `frontend.md`)

| Endpoint | Purpose |
|---|---|
| `GET /devices` | fleet state |
| `WS /devices/{id}/events` | live decision-feed stream |
| `POST /devices/{id}/query` | instant local **hybrid** search (§6.1) + answer (§6.5); returns answer + `latency_ms` + source ids |
| `POST /devices/{id}/capture` | submit text or image fact |
| `POST /network/mode` | set global simulated network state: offline / degraded / full |
| `WS /consensus/events` | resolver decisions for Conflict Theater |
| `GET /cloud/state` | merged trusted picture, derived by the fold (Command Dashboard) |
| `GET /devices/{id}/telemetry` | live CPU/RAM/latency for resource strip |
| `GET /devices/{id}/sync` | sync status for that device (§6.6) |
| `GET /devices/{id}/activity` | system-activity feed, most recent first (§6.6) |

### 6.1 Hybrid search is dense + sparse, fused in application code

The problem statement explicitly requires "low-latency **vector and hybrid** search." Hybrid means dense + sparse, **not** "vector plus payload filters" — the latter is filtered search, and conflating the two is a common and expensive mistake.

Qdrant Edge ships BM25 built in (`Bm25`, `Bm25Config`, `EdgeSparseVectorParams(modifier=Modifier.Idf)`, `embed_document` / `embed_query`) and it is wire-compatible with server BM25, so a shard seeded from a server snapshot answers local keyword queries with no re-indexing. But **Edge does not fuse at query time** — it queries one named vector field per request and does not consume `Prefetch`. So:

1. Run the dense leg: `Query.Nearest(dense_vec, using="text")`.
2. Run the sparse leg: `Query.Nearest(bm25.embed_query(q), using="text_bm25")`.
3. Fuse the two ranked lists yourself with Reciprocal Rank Fusion.
4. Do the same over the `immutable` shard, then dedupe across all four result sets by point ID.

Always use `embed_query` for query text and `embed_document` for document text — BM25 weights them differently and using the wrong one silently degrades results.

**Prove it works:** report recall@5 for dense-only vs hybrid on a labeled set. "0.62 → 0.84 on exact hazard codes" is five minutes of work and converts a checklist item into evidence.

### 6.2 Index what you filter and facet on

Create payload indexes for every field you filter, facet, or range-query on (`zone`, `modality`, `status`, `urgency_score`, `corroboration_key`, `device_id`, `synced`). Do not aggregate in application code — `facet`, `count`, and `scroll` are built in.

### 6.3 Call `optimize()`

Edge has no background optimizer. Nothing is indexed and no deleted space is reclaimed until you call `shard.optimize()`. Call it on a schedule (low-traffic or post-batch), not per write.

### 6.4 Tune the thread pool

Each shard owns a search thread pool that defaults to **four threads per CPU core**. On a 1-CPU container that is four threads competing with your entire application. Set `max_search_threads` and `search_pool_core` explicitly in `EdgeConfig`.

Note the WAL is pre-allocated to 32 MB per shard and inflates disk and backup size; `wal_options` to shrink it is **Rust-only**, so a Python implementation eats this cost. Two shards × N devices is real, and it is pre-allocated *nothing*. Do not report raw directory size as memory usage.

### 6.5 The answer layer (required — do not skip)

The sponsor asks for an **AI application**, and the last rubric line is "a meaningful edge-to-cloud AI workflow, rather than simply running a local vector database." Retrieval alone returns chunks, not answers. You need a generation step, and the interesting part is that it has two paths behind one interface:

- **Offline:** a small local SLM generates a grounded answer from the retrieved hybrid-search context. Published work ([Pocket RAG, arXiv:2602.13229](https://arxiv.org/abs/2602.13229)) demonstrates first-aid guidance at 94.5% accuracy from a small model on a phone, so this is realistic on a Pi 4/5.
- **Online:** the cloud model answers from the same interface and the same retrieved context.
- The response reports which path served it. Same question, two paths, one contract — this is what makes "operate offline" mean something beyond "search still returns rows."

**Target tradeoff, decide it explicitly:** this does not fit the 512MB / 1-CPU fallback container. Either commit to a Pi 4/5 (or any ≥4GB host) as the constrained target, or run the answer layer only on a subset of nodes and say so. Discovering this on stage is the failure mode to avoid.

### 6.6 Inspection surfaces the problem statement asks for by name

The fourth bullet names four things a human must be able to inspect: **device memory, search results, sync status, and system activity.** Two of them have no natural screen otherwise, so they get explicit endpoints.

**Sync status** — `GET /devices/{id}/sync`:

- `last_attempt_at`, `last_success_at`, consecutive failure count, next backoff
- **pending counts by priority and by synced state, straight from `facet` on the mutable shard** (`synced == false` grouped by `sync_priority`) rather than a separately maintained counter that can drift from the data
- bytes and duration of the last push, and the mode it ran under (§5.3)
- per-direction state: last pull snapshot time, points received

**System activity** — `GET /devices/{id}/activity`, plus live frames on the existing `WS /devices/{id}/events`:

- a bounded in-memory ring buffer per device (this is operational telemetry, not facts, so it does not belong in the shard and does not need SQL)
- event kinds: `capture`, `decision`, `push_attempt`, `push_result`, `pull_result`, `consensus`, `retraction`, `error`, `mode_change`
- each entry carries timestamp, device, kind, a short human-readable detail string, and any decision verdict or confidence score that applies

This is what lets a judge answer "why is this fact in this state, and what happened to it?" without asking you anything.

---

## 7. Small-device proof point

Don't just claim optimization — instrument it:
- Run at least one edge node on the real constrained target and log p50/p95 query latency and steady-state RAM to the telemetry endpoint so it's visible live, not on a slide.
- Use the Scalar-quantized path (§3 step 5) on that node during the degraded-network beat to show the size reduction is real. Log bytes-over-wire before/after and surface that number.
- Show a **measured** cloud round-trip next to the **measured** on-device latency, not a slide claim. If either number is a labeled typical range rather than a measurement, label it that way on screen — the official Qdrant Edge demo does exactly this and it reads as rigor, not weakness.
- Keep the thread pool (§6.4) and WAL (§6.4) honest so the numbers reflect a tuned deployment rather than a misconfigured one.

---

## 8. Novel-feature recap (for the pitch/one-pager)

1. **N-way trust-weighted consensus**, not pairwise last-write-wins — over an append-only event log, so trust and confidence are *derived*, and retraction is structurally incapable of ghosting. The actual gap in every prior-art system checked.
2. **Event-sourced cloud memory** — every fact immutable, the trusted picture a pure fold. This is what makes the consensus guarantees hold rather than being asserted.
3. **Config-driven Model Adapter Registry** — same kernel, swap one YAML file, different vertical and modality, zero kernel code changes.
4. **Explainable decision feed** — every keep/queue/reject/redact decision ships a plain-language reason, live, not a black box.
5. **Visible dispute state and visible trust decay** — the system says "I'm not sure, here's why" and shows you who it's losing confidence in.
6. **Measured, not claimed** — resolver-vs-LWW accuracy, hybrid-vs-dense recall, and real on-device latency, all on screen during the demo.

---

## 9. Non-goals and explicit limits

- No production auth/multi-tenant cloud — a single hub collection is enough.
- No real NER/PII model — a config-driven regex allowlist is sufficient for demo data and keeps build time low.
- No custom ANN index — use Qdrant's as-is.
- **No SQL anywhere in the runtime path.** Deliberate, and defensible: it means there is no second copy of any fact and therefore no reconciliation bug.
- **Demo-scale limits, state them out loud:** the consensus fold is a `scroll` per `corroboration_key` per sync cycle (fine to low thousands of events, not beyond); the hub collection grows monotonically by design, because deletions are events; the `immutable` shard is a full local copy of the hub, so hub size × device count is your RAM budget.
- **Known gaps** (say these out loud if asked, don't hide them): embedding drift if edge and cloud ever run different-sized models; a short-lived working-memory tier distinct from the consolidated log.
