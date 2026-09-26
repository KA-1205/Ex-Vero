# AGENTS.md — Aegis Edge

**Read this file completely before writing any code.** It exists because the single largest source of wasted effort in this project is rediscovering Qdrant Edge's actual API surface by trial and error, and because the failure modes here are silent — an outbox that marks before it pushes loses data with no error, and a fold that trusts device clocks resolves conflicts incorrectly while looking fine.

**Docs:** `README.md` (the pitch) · `backend.md` (engineering spec) · `frontend.md` (UI spec) · this file (problem + verified facts + build order)

---

## 1. What we are building

An **offline-first edge memory kernel**. Edge devices keep searchable semantic memory locally, decide intelligently what to keep local versus what to send to the cloud, and reconcile many devices that disagree while disconnected. We demo it as a disaster-response fleet (paramedic tablets / triage kiosks) because that is the hardest case — worst connectivity, highest stakes — but nothing in the kernel is disaster-specific.

The vendor is Qdrant. The rubric explicitly says: **"demonstrate a meaningful edge-to-cloud AI workflow, rather than simply running a local vector database."** So a local vector store with a UI is a failing submission. The product is the **decision layer** on top of it.

---

## 2. The problem, as testable requirements

Each requirement states what "done" means. Do not mark something done because it works; mark it done when the acceptance line is satisfied.

### R1 — Searchable semantic memory on-device
Vectors and payloads live in a Qdrant Edge shard on local disk, in the device's own process.
**Done when:** `POST /devices/{id}/query` returns ranked results plus a measured `latency_ms` while the network simulator is set to `offline`, with no network call anywhere in the path.

### R2 — Decide what stays local and what syncs
Every captured fact passes through a policy engine **synchronously at capture time** and receives one of: `KEEP_LOCAL | QUEUE_LOW | QUEUE_HIGH | REDACT_AND_QUEUE | REJECT`, plus a **human-readable reason string**.
**Done when:** every row in the decision feed has a verdict and a reason a non-expert can read, and you can point at the config value that produced it. The reason is the product, not a debug log.

### R3 — Sync when connectivity returns
Bidirectional. Local mutations push up; cloud knowledge comes back down. Delta only, never a full snapshot.
**Done when:** pull the network, capture facts, restore the network, and the cloud picture updates with no manual intervention — and the device has also *learned something new* from the fleet.

### R4 — User-facing inspection
A human can inspect device memory, search results, sync status, and system activity — all four named by the sponsor, so all four need a surface.
**Done when:** a judge can locate any individual fact and see why it is in whatever state it is in, without asking you anything — including its sync state and the activity log entry that produced it. `GET /devices/{id}/sync` and `GET /devices/{id}/activity` are the two that don't fall out of a screen for free.

### R5 — Low-latency vector *and hybrid* search, offline
**Hybrid means dense + sparse (BM25), fused.** It does **not** mean "vector plus payload filters" — that is filtered search. Conflating the two is the most common and most expensive mistake in this project.
**Done when:** you can report recall@5 for dense-only versus hybrid on a labeled set, and hybrid wins by a number you measured.

### R6 — Intermittent connectivity
Network modes: `offline` / `degraded` / `full`. Everything in §2 R1–R5 must hold in all three.
**Done when:** switching to airplane mode changes nothing about query latency or correctness, and `degraded` is a genuinely worse link — injected latency, a bandwidth cap, and a real failure rate (`backend.md` §5.3) — not a label on a status tile. The simulator intercepts the sync transport only; assert in a test that the query path never references it.

### R7 — Evolving memory, updates, and conflicts
- Local memory has a cap and evicts safely (synced points only, never pending ones).
- Two devices can hold different values for the same real-world fact.
- A retracted fact must never reappear anywhere.
**Done when:** a retracted fact is absent from every device and from the cloud, and you can show *why* it cannot come back.
**Stretch:** surface a conflict that `corroboration_key` matching alone would have missed (see `backend.md` §4.5). Novelty, not rubric coverage — build it only after every R1–R8 acceptance line passes.

### R8 — A meaningful edge-to-cloud AI workflow
There must be a **generated answer**, not retrieved chunks. One interface, two backends: a small local model when offline, the cloud model when connected.
**Done when:** the same question produces a real answer in both modes, and the UI shows which path served it.

---

## 3. Non-negotiable: the five demo moments

Everything else is optional. A feature that does not serve one of these does not earn its place.

1. **Multiple devices, fully offline, answering instantly.**
2. **A live decision feed narrating why** — kept / queued / synced / rejected, in plain language.
3. **Two devices disagreeing offline, converging on reconnect** into one trusted answer with a visible confidence score.
4. **A safety-critical fact jumping the sync queue** ahead of routine ones on a degraded link.
5. **Real latency and memory numbers** from a real constrained target, on screen, measured — not claimed.

Rehearse these five in order, explicitly, until each is boring.

---

## 4. Architecture in one screen

- **Device:** two Edge shards. `mutable` holds local writes (unindexed until `optimize()`). `immutable` mirrors the server via partial snapshots (HNSW-indexed). Query reads both, dedupes by point ID.
- **Outbox:** not a store. A filtered `scroll` over the `mutable` shard where `_sync_meta.synced == false`. Drained in batches; marked synced **only after** a successful push.
- **Hub:** one Qdrant Server collection, `fact_events`, **append-only and immutable**. `OBSERVED` / `RETRACTED` events.
- **Consensus:** a **pure fold** over that log. No SQL anywhere in the runtime path. No transactions needed, because nothing is ever mutated.
- **App:** FastAPI per node + a cloud gateway. REST for request/response, WebSocket for live feeds.

Full detail in `backend.md`.

---

## 5. Qdrant Edge — verified facts

Get these right. Most were verified against Qdrant's live documentation in September 2026; sources at the bottom.

**What it is.** A lightweight, in-process, embedded vector search engine. No background services, no server process, ~11 MB install footprint. Think "SQLite, but for vector search." Not a thin wrapper around a server.

**Packages.** `pip install qdrant-edge-py`, imported as `qdrant_edge`. Rust crate: `qdrant-edge`. **Python and Rust only** — there is no JavaScript/TypeScript binding. If you need Edge in the browser, you cannot have it; the frontend talks to the edge node over HTTP.

**Status: beta.** Announced as a private beta on 29 July 2025 and still in beta as of this writing. The API drifts between releases. **Pin the exact version** in `uv.lock`/`pyproject.toml` and never float it.

**Not the same thing as `QdrantClient(path=...)`.** That is `QdrantLocal`, a pure-Python reimplementation for small-scale dev and tests. It warns above 20,000 points, and it has **no snapshots, no BM25, and no sync**. This project needs all three, so it must use `qdrant-edge-py`.

**Full `EdgeShard` method surface:** `create`, `load`, `update`, `query`, `facet`, `scroll`, `count`, `retrieve`, `flush`, `close`, `optimize`, `info`, `unpack_snapshot`, `snapshot_manifest`, `update_from_snapshot`.

**What Edge does *not* have:**
- **No built-in `.sync()`.** Synchronization is a pattern you assemble from shard helpers plus your own transport.
- **No query-time fusion.** It queries one named vector field per request (`using=`) and does not consume `Prefetch`. You run the dense leg and the BM25 leg separately and fuse the rankings yourself.
- **No background optimizer.** Nothing is indexed and no deleted space is reclaimed until you call `optimize()`.
- **No dense embeddings.** Those come from the separate `fastembed` package.

**BM25 is built in.** `Bm25`, `Bm25Config`, `EdgeSparseVectorParams(modifier=Modifier.Idf)`, `embed_document`, `embed_query`. It is wire-compatible with server BM25 — a shard seeded from a server snapshot answers local keyword queries with no re-indexing. **Do not add a second BM25 library.** Note `Bm25Config` takes `language`, `k`, `b`, `avg_len`, `tokenizer`, `stopwords`, and more. Always `embed_query` for queries, `embed_document` for documents — the weighting differs and using the wrong one silently degrades results.

**Quantization:** Scalar, Product, Binary, TurboQuant. Set globally on `EdgeConfig.quantization_config` or per-vector on `EdgeVectorParams`.

**Configuration sketch:**

```python
from qdrant_edge import (
    Bm25, Bm25Config, Distance, EdgeConfig, EdgeOptimizersConfig,
    EdgeShard, EdgeSparseVectorParams, EdgeVectorParams, Modifier,
)

# Which models run comes from config, never from an import in this file.
adapters = registry.load(yaml_config)

# Vector dimensions are a property of the resolved adapter, not a constant here.
# Deriving them is what makes "swappable" true rather than aspirational — a hardcoded
# size=384 silently binds you to whichever model happens to be 384-dimensional.
config = EdgeConfig(
    vectors={a.name: EdgeVectorParams(size=a.dim, distance=Distance.Cosine)
             for a in adapters},
    sparse_vectors={"text_bm25": EdgeSparseVectorParams(modifier=Modifier.Idf)},
    quantization_config=<ScalarQuantization>,   # placeholder — resolve the real
                                               # class name from qdrant_edge.__all__
                                               # before using this line (§6.3)
    max_search_threads=2,          # default is 4 PER CORE
    search_pool_core=0,
    optimizers=EdgeOptimizersConfig(deleted_threshold=0.2, default_segment_number=2),
)
shard = EdgeShard.create("./shard", config)   # create() FAILS if the dir has data
shard = EdgeShard.load("./shard")             # use load() to reopen
```

**Swappable means chosen at deploy time on a fresh shard — never hot-swapped on a live one.** Dimensions are baked into a shard's stored vectors, so changing models means recreating the shard or re-embedding it. Loading a shard written by a different model produces shape mismatches that surface as wrong scores, not as errors. Guard it on reopen (invariant 10, §6.2).

**Snapshot endpoints** (for the pull direction): `GET /collections/{c}/shards/0/snapshot` and `POST /collections/{c}/shards/0/snapshot/partial/create` with a `snapshot_manifest()` body.

**Sources:** [Edge overview](https://qdrant.tech/documentation/edge/) · [Quickstart](https://qdrant.tech/documentation/edge/edge-quickstart/) · [BM25](https://qdrant.tech/documentation/edge/edge-bm25/) · [Sync guide](https://qdrant.tech/documentation/edge/edge-synchronization-guide/) · [Official agent skill](https://skills.qdrant.tech/) (`qdrant-edge/SKILL.md` is effectively a "what not to do" list — read it, it is free credibility with judges)

---

## 6. Traps, invariants, and how to catch each one

### 6.1 API gotchas

Each of these costs a debugging cycle. The guard column is what turns it from a mystery into a five-minute fix.

| Trap | Symptom | Guard |
|---|---|---|
| Calling filtered search "hybrid" | dense and BM25 ranks never differ; recall numbers are identical | Two separate queries + your own RRF (§6.1 of `backend.md`) |
| Forgetting `optimize()` | inserts look like they vanished; sparse index never built; deleted space never reclaimed | `optimize()` after every write batch, not on a timer |
| Default thread pool | 4 threads **per core**; on 1 CPU they fight the whole app | set `max_search_threads` and `search_pool_core` in config |
| The 32 MB WAL | pre-allocated per shard; 2 shards × N devices is mostly nothing on disk | Rust-only `wal_options`; in Python just don't report WAL as memory |
| `immutable` shard is a full hub copy, per device | RAM scales hub × devices; kills the demo with no warning | cap the hub collection size |
| `create()` on a populated dir | hard failure, not a no-op | `load()` to reopen, `create()` only for a fresh dir |
| `EdgeConfig` with empty `vectors` **and** `sparse_vectors` | rejected, even when you only wanted to tune threads | always declare at least one vector field |
| Beta API drift | code works on your machine, not the pinned one | pin the exact version; never float it |
| `embed_query` vs `embed_document` swapped | results silently worse, no error | assert the BM25 leg on a keyword-only query in a test |
| Two unrelated embedding models | text query never returns the image | one cross-modal model, separate named vectors |
| Zero, NaN, or constant vectors from an embedder | cosine returns arbitrary rows, no error anywhere | the conformance suite in `backend.md` §2 — run it for every adapter, including the fake |

### 6.2 Correctness invariants — write a test for each

These are the ones that corrupt data or produce a wrong answer while the UI looks perfectly healthy. Every one is a test, not a code-review promise.

1. **Mark after push, never before.** A point flips `_sync_meta.synced = true` only after the hub acknowledges it. Marking first is permanent, silent data loss. *Test:* kill the transport mid-push; assert the point is still pending **and** still on the device.
2. **`optimize()` before you believe a write.** Until it runs, the sparse index is stale and deleted space is unreclaimed. *Test:* write, then query **without any restart** and assert the new point is retrievable. If a test only passes after a process restart, you have this bug.
3. **Eviction must never touch unsynced points.** *Test:* overfill the cap with pending points, assert none were evicted.
4. **Point IDs must be deterministic across devices** — derive from `corroboration_key` + value hash, or propagate the hub's event ID. If each device mints random UUIDs, the mutable/immutable dedupe in §4 is a silent no-op and every query returns duplicates. Nothing else in this file protects you here.
5. **The fold must not trust device wall-clocks.** Offline devices drift; two devices can disagree on ordering while both report sane timestamps. Order by a **hub-assigned sequence** and treat the device timestamp as metadata only. *Test:* skew one device's clock by an hour and assert the resolved value does not change.
6. **The fold is a pure function of the log.** Same events, different arrival order, identical result. *Test:* shuffle insertion order and assert byte-identical fold output.
7. **Retraction beats re-observation.** *Test:* observe → retract → re-observe the identical fact; assert it stays absent everywhere.
8. **The query path must not touch the network layer** — not "works anyway," not even an import. *Test:* a static check that the query module does not import the transport or the simulator, plus a functional query under `offline`.
9. **Record the embedding model and version on every point.** A re-embed otherwise invalidates every prior score with no error.
10. **Shard vector dimensions must match the resolved adapters, checked on reopen.** A shard's stored vectors have fixed dimensions, so loading one written by a different model fails as *wrong scores*, not as an error. *Test:* create a shard with a 384-dim adapter, restart with a 768-dim adapter, and assert the node refuses to start (or refuses to write) with a clear message — rather than returning quietly bad results.

### 6.3 Unverified API surface — confirm before you rely on it

The facts in §5 were checked against live docs. These were not, and each will fail in a confusing way if you assume it works. Write a two-line probe for each before building on it:

- **`scroll` ordering.** Does it support sort-by on `sync_priority`? If not, the outbox drains in arbitrary order and demo moment 4 breaks. Pull a bounded batch and sort it yourself, or filter per priority and issue one scroll per priority.
- **The Scalar-quantization class name.** `backend.md` and §5 both use a placeholder. Resolve it from the installed package's `__all__` before writing that config path.
- **`snapshot_manifest()` return shape** and the body `POST /collections/{c}/shards/0/snapshot/partial/create` actually expects.
- **`update_from_snapshot` signature** — whether it takes a path, bytes, or a handle.
- **Faceting on a boolean field** (`synced`) — confirm bools are indexable, since the sync-status endpoint depends on it.
- **`Bm25Config` defaults** for `tokenizer` and `stopwords`, and whether the sparse leg needs `using=` naming like the dense one.
- **`shard.info()` return shape.** The dimension guard in invariant 10 needs to read the stored vector names and sizes back out; confirm the field path before relying on it.

---

## 7. Build order, with the verification for each step

Ship in this order. Steps 1–5 are the load-bearing path; everything after is enrichment. **The third column is not a manual check** — each one is an assertion in a test file that fails if the behavior regresses. A step is not done when it works once; it is done when its test is red before the fix and green after.

| # | Step | Verify by (must be a test, not a look) |
|---|---|---|
| 1 | Edge shard + capture + instant query. No sync yet. Build against `FakeEmbedder`. | Test: query returns results with network mode = `offline` |
| 2 | Decision Engine (§3 of `backend.md`), logging every verdict + reason | Test: every captured fact appears in the feed with a verdict and a readable reason |
| 3 | Hybrid search: BM25 leg + RRF fusion | Test: dense-only vs hybrid recall@5 on the labeled set, hybrid strictly higher |
| 4 | Outbox + push handshake | Test: kill transport mid-push → point still pending; reconnect → cloud updates itself |
| 5 | Pull direction: `immutable` shard from partial snapshot | Test: a device learns a fact it never captured, sourced from another device |
| 6 | Consensus fold + retractions. Build the **conflict injector** UI early. | Test: two devices disagree offline → converge on reconnect with a confidence score; retracted fact stays gone; shuffled event order → identical fold |
| 7 | Trust decay visible + resolver-vs-LWW benchmark | Test: trust score moves after disagreements; benchmark number is printed, not asserted by hand |
| 8 | Semantic conflict detection (`backend.md` §4.5) | Test: a conflict is surfaced that `corroboration_key` alone would have missed |
| 9 | Answer layer, two paths | Test: same question answered offline and online, both real answers, response reports which path served it |
| 10 | Frontend per `frontend.md` | Each screen demonstrably serves one of the five moments |
| 11 | Rehearse the five moments, in order | Full run, no failures, no narration |

**On priority:** the answer layer (step 9) is a stated rubric requirement, so it outranks novelty work. Semantic conflict detection is the most original idea here and the least load-bearing — originality is worth less than covering the checklist, and that ordering is deliberate.

---

## 8. Repo layout

Everything lives under `edge-node/`. The git repo root holds only `.gitignore`
— that one file has to sit at the root to cover the whole tree, and `.venv` is
nested two levels down.

```
/.gitignore
/edge-node/
  AGENTS.md         this file
  backend.md        engineering spec
  frontend.md       UI spec
  README.md         the pitch
  config/           per-vertical YAML (disaster-response.yaml, ...)
  tools/            throwaway API probes, not imported by anything
  pyproject.toml    deps
  uv.lock           pinned here; qdrant-edge-py must never float
  .venv/            local, gitignored
  src/edge_node/    FastAPI service, Decision Engine, Model Adapter Registry, shards
  tests/
```

`cloud-gateway/` and `frontend/` get created here at steps 4 and 10, each as its
own package with its own entry point — separate processes, just colocated.

Each edge node is a **real independent process**; the frontend's device switcher only changes which one you are watching.

**Resolve paths from the package location, never the cwd.** `edge_node.main` exposes `REPO_ROOT` and `DEFAULT_CONFIG_PATH` for this reason: tests chdir into a temp directory, so a relative `./config/...` silently resolves against the wrong root and fails far from the cause.

---

## 9. Hard constraints

- **Backend:** Python 3.11+, FastAPI, `qdrant-edge-py` on device, Qdrant Server for the hub. **No SQL in the runtime path.**
- **Frontend:** React + Vite + TypeScript + Tailwind, no component library. It is a thin renderer over the backend contract — all model and policy logic lives in the backend.
- **Frontend design:** tactical ops console, not "AI product." Near-black base, off-white text, one alert-red accent, one green/amber accent. Monospace for all data, IDs, timestamps, and scores. Sharp corners, 1px hairline borders, no gradients, no glass cards, no chat-bubble UI, no chatbot icon. That aesthetic reads as "wrapped a model in a template," and it is the single most common failure in this genre. Full rationale in `frontend.md` §2.
- **Models:** swappable via config, never hardcoded. One cross-modal embedder if you want cross-modal retrieval.
- **The embedder is a team boundary and is being built in parallel.** The seam is fixed in `backend.md` §2: an `Embedder` `Protocol` (structural typing, so the model needs no edits to satisfy it) plus a `Generator` `Protocol` for the answer layer. **Build everything against `FakeEmbedder` — do not block on the real model, and do not edit their code when it arrives.** You write the adapter; the adapter is the only thing that knows tensor types and preprocessing. Every adapter, real or fake, must pass the conformance suite in `backend.md` §2, which is written before the model exists.
- **The kernel stays generic.** Disaster-response is configuration, not code. The claim to defend is: swap the YAML, same kernel, different vertical, zero code changes.

---

## 10. Definition of done

**Correctness — no exceptions, each of these is a passing test:**
- [ ] Every one of the nine invariants in §6.2 has a test that fails when the invariant is broken
- [ ] Every entry in §6.3 is probed and the answer written down in this file, so the next person does not re-guess
- [ ] The pinned `qdrant-edge-py` version is committed and matches what the docs claim
- [ ] No placeholder values survive into shipped config

**Evidence — measured, on screen, reproducible:**
- [ ] All five demo moments work live, in order, without narration from you
- [ ] Every one of R1–R8 in §2 satisfies its acceptance line
- [ ] Resolver accuracy vs LWW baseline measured and stated
- [ ] Hybrid vs dense recall@5 measured and stated
- [ ] On-device latency and RAM measured on the constrained target, on screen
- [ ] At least one `DISPUTED` state demonstrated with both values visible
- [ ] A retraction demonstrated staying dead across all devices
- [ ] Trust score visibly moves
- [ ] Same question answered offline and online, both real answers

**Integrity — the claims hold up under a judge's check:**
- [ ] Known gaps written down and said out loud if asked
- [ ] No claim in `README.md` that we cannot link to a source for
- [ ] Qdrant Edge's release status re-checked against the live docs, and §5/§11 updated if it changed since September 2026 — beta status is a moving target and an outdated claim is still a wrong claim

---

## 11. The two claims most likely to cost us the room

Both are already corrected in `README.md`. Keep them corrected, because a judge can check either in under a minute:

1. **Never claim Qdrant Edge is GA.** It is **in beta** — private beta announced 29 July 2025, and still beta at the September 2026 check this file is based on. Re-verify against the live docs before you present (§10) and update this line if it changed. Overclaiming about the vendor's own product is the fastest way to lose a technical judge.
2. **Never describe our sync layer as novel.** [`qdrant/qdrant-edge-demo`](https://github.com/qdrant/qdrant-edge-demo) and [`qdrant-labs/edge-mission-control`](https://github.com/qdrant-labs/edge-mission-control) are first-party Qdrant demos that already do dual-shard sync, partial snapshots, persistent queues, and RRF hybrid search. Cite them as the closest prior art and differentiate above the transport layer, where the Decision Engine and the multi-device consensus fold live.

The honest, stronger framing: **nobody we found handles a fleet of devices learning and disputing each other's updates.** That is true, and it is the pitch.
