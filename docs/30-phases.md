# Aegis Edge — Execution Roadmap (the live phase plan)

**Role of this document:** the forward, numbered plan that `/p <n>` executes. Each phase is
written to be executed with **no guessing**: it names the exact files, the concrete steps, the
interfaces, the precise acceptance test, and what is explicitly out of scope. It supersedes the
old committed "Step 1–8" labels (several are partial/broken — see the audit).

> [!IMPORTANT]
> **Backend-only lane.** Phases touch `src/edge_node/`, `config/`, `tests/`, `cloud-gateway/`,
> `docker/`, `tools/`, `docs/`. They **must not modify `frontend/`** — the team's parallel lane.
> The contract between lanes is `docs/API.md`; if a phase adds/renames a route, update
> `docs/API.md` and note it for the team, but never edit `frontend/`.

> [!NOTE]
> **Anti-hallucination rules for every phase.** (1) If an API isn't confirmed in this doc or
> `docs/AGENTS.md` §3, **probe it in `tools/` first** — do not assume a signature. (2) Read the
> named existing file before editing it. (3) Build against `FakeEmbedder`, never a network call,
> in tests. (4) If a step can't be completed correctly, **stop and report** — do not stub, fake,
> or half-implement to make a test pass. (5) Qdrant is the only datastore — no SQL, no second
> store, ever.

## Status index
`TODO` · `WIP` · `DONE` (green + audited) · `PARTIAL`/`BROKEN`/`WEAK`/`HALF` (from audit)

| # | Phase | Status |
|---|-------|--------|
| 0 | Edge API probe + doc reconcile | DONE |
| 1 | Decision Engine fix | DONE |
| 2 | Model Adapter Registry | DONE |
| 3 | Real Qdrant Server hub + outbox | DONE |
| 4 | Partial-snapshot pull | DONE |
| 5 | Multi-device consensus | DONE |
| 6 | Network layer | DONE |
| 7 | Answer layer (on-device RAG) | DONE |
| 8 | Eviction + inspection endpoints | DONE |
| 9 | Benchmarks (measured) | PARTIAL |
| 10 | Docker + telemetry integration | TODO |

**Critical path:** 0 → 1 → 2 → 3 → 4 → 5 → 7. Phases 6, 8, 9, 10 run alongside.

---

## Phase 0 — Edge API probe + doc reconcile

**Why:** the running code uses server-side `Prefetch` + `Fusion.Rrf` in one `EdgeQueryRequest`,
which contradicts the "Edge has no query-time fusion" claim in the docs. Sync phases also depend
on snapshot APIs we have not verified. Guessing here corrupts phases 3–5. Verify first.

**Touch:** `tools/probe_edge_api.py` (exists — extend it), then `docs/AGENTS.md` §3 and
`docs/backend.md` §9 to record findings.

**Build (a script that prints facts, imports nothing from `src/edge_node`):**
1. Create a temp `EdgeShard` with one dense vector field + one `text_bm25` sparse field; insert
   3–4 documents with known text so ranking is decidable (not one doc).
2. Confirm hybrid: run dense-only, BM25-only, and the `Prefetch`+`Fusion.Rrf` request; print the
   three ranked id lists. **Determine whether fusion actually blends the two legs** or silently
   returns one. Record the answer.
3. Confirm sync APIs by signature/callability: `snapshot_manifest()` return shape;
   `update_from_snapshot(...)` argument (path vs bytes vs handle); `unpack_snapshot(...)`;
   `EdgeShard.create`/`load`; `UpdateOperation.upsert_points` /
   `delete_points_by_filter` / `set_payload_by_filter`.
4. Confirm `facet` works on a boolean payload field (`synced`) and that `scroll` accepts a
   `Filter`; note whether `scroll` supports sort/order.

**Acceptance test:** `tools/probe_edge_api.py` runs and prints a PASS/behaviour line for each of
the four checks; `docs/AGENTS.md` §3 updated so no later phase relies on an unverified call.

**Guardrails:** probe only — do not change `src/`. If `Fusion.Rrf` does NOT fuse, phase 3/hybrid
must fuse in Python (two queries + manual RRF); record that decision here.

**Result (verified on `qdrant-edge-py==0.8.0`):** `Prefetch`+`Fusion.Rrf(k=...)` **does** blend
the dense and BM25 legs (probe: fused order `[1,3,4,2]` matches neither dense `[4,1,2,3]` nor
BM25 `[1,3]`). **Decision: Phase 3/hybrid uses server-side RRF per shard**, not Python RRF.
Second finding — a **boolean payload filter matches nothing** in `scroll`/`count` (silent zero)
while `facet` still counts it; **store `_sync_meta.synced` as Integer `0/1`, Integer-indexed**,
so the outbox actually drains. Full sync/facet/scroll signatures recorded in `docs/AGENTS.md`
§3.1; test: `tests/test_probe.py`.

**Out of scope:** any behavior change to the app.

---

## Phase 1 — Decision Engine fix (the real bug)

**Why:** today every capture short-circuits to `KEEP_LOCAL` and the verdict never affects sync.
Two concrete bugs: (a) `decision_engine.py` runs the completeness gate before novelty/urgency and
`config/disaster-response.yaml` requires `status/timestamp/reporter_device_id` which
`main.py capture()` never sets → always incomplete; (b) `main.py push()` marks everything
`synced==false` regardless of verdict.

**Touch:** `src/edge_node/decision_engine.py`, `src/edge_node/main.py`,
`config/disaster-response.yaml`, `tests/` (new test file, e.g. `tests/test_decision.py`).

**Build:**
1. Decide capture-time fields: `capture()` should stamp `status` (default `"unverified"`),
   `client_timestamp_ns`, and `reporter_device_id` (= the path `device_id`) into the payload, so
   completeness is a real gate, not an always-fail. Keep `required_fields` in config but make it
   satisfiable.
2. Reorder/adjust `evaluate()` so the pipeline is: PII → novelty → urgency → completeness →
   verdict, per `backend.md` §4. Completeness should downgrade a `QUEUE_*` to held, **not**
   pre-empt urgency/novelty. `REDACT_AND_QUEUE` must be reachable when PII is present but the
   fact is still worth queuing.
3. Store the verdict + a `sync_priority` (`URGENT` for `QUEUE_HIGH`, `ROUTINE` for `QUEUE_LOW`,
   `HELD` for incomplete) in `_sync_meta` so sync can honor it.
4. Make `push()` sync **only** facts whose verdict allows it: never push `KEEP_LOCAL` /
   `REJECT` / `REDACT`-local; order `URGENT` before `ROUTINE`.

**Acceptance test (`tests/test_decision.py`):**
- Capture a high-urgency fact ("structural collapse ...") → verdict `QUEUE_HIGH`.
- Capture a near-duplicate of an existing fact → `REJECT` (novelty ≥ high threshold).
- Capture a fact containing a PII pattern → redacted/local, not queued raw.
- Capture a `KEEP_LOCAL` fact, run push → assert its id is **absent** from the pushed set.
- Rewrite `test_capture_and_query_same_process` so it asserts a *specific* verdict, not "any".

**Guardrails:** verdict thresholds come from config only; reasons must stay human-readable.

**Out of scope:** real server (phase 3), quantized bandwidth tiering (note as a TODO).

---

## Phase 2 — Model Adapter Registry (Protocol + config, cross-modal)

**Why:** current `adapter.py` is an ABC, text-only, no `FakeEmbedder`, no conformance, no CLIP.
`backend.md` §3 requires config-swappable models behind Protocols.

**Touch:** `src/edge_node/adapter.py`, `src/edge_node/registry.py`,
`config/disaster-response.yaml`, `tests/test_registry.py`, `main.py` (use the registry).

**Build:**
1. Define `Embedder` and `Generator` as `typing.Protocol` (`@runtime_checkable`) with the exact
   fields in `backend.md` §3.1 (`name/dim/modality/version`, `embed_batch`; `generate`).
2. Add `as_unit_vector()` normalization with NaN/Inf and zero-vector guards (`backend.md` §3).
3. Implement adapters: `FakeEmbedder` (deterministic vector hashed from payload — distinct
   inputs diverge), `TextDenseAdapter` (fastembed `BAAI/bge-small-en-v1.5`), and the cross-modal
   CLIP adapters (`Qdrant/clip-ViT-B-32` text + vision → shared 512-dim space, named vectors
   `text` and `image`).
4. Migrate config to the `models:` schema in `backend.md` §3.2; `registry.from_config()` resolves
   it. `dim` is read from the adapter, never hardcoded.
5. Write the conformance suite (the 8 checks in `backend.md` §3.3) and run it over every adapter.

**Acceptance test (`tests/test_registry.py`):** conformance suite passes for `FakeEmbedder`,
the dense text adapter, and CLIP; swapping the model name in a temp config changes the resolved
adapter with no code change; a wrong-modality payload raises; a zero vector raises.

**Guardrails:** the registry is the ONLY place that names concrete model classes. No model is
imported anywhere else. Tests use `FakeEmbedder` (no downloads) except one guarded real-model
test.

**Result:** `adapter.py` now defines `Embedder`/`Generator` as `@runtime_checkable` Protocols
plus `as_unit_vector()` (rejects zero/NaN/Inf), `FakeEmbedder`, `TextDenseAdapter`, and the
cross-modal `ClipTextAdapter`/`ClipVisionAdapter` (one CLIP family → shared 512-dim space).
`registry.from_config()` resolves the new `models:` schema (provider `fake`/`fastembed`, CLIP by
modality); `load_adapters()` still reads the legacy `adapters:` schema for older tests. `dim` is
read from the model, never hardcoded (fakes excepted). `main.py` now stamps the checkpoint
`version` on every point (invariant 9) and no longer imports a model library. Config migrated to
`models:`. Test: `tests/test_registry.py` (conformance §3.3 over fake + real dense + CLIP, config
swap, wrong-modality/zero-vector raise, invariant 9). Full suite green (37).

**Out of scope:** the generator implementation (phase 7 — only the `Generator` Protocol here).

---

## Phase 3 — Real Qdrant Server hub + shard-view outbox

**Why:** `hub.py` is an in-memory dict; the outbox is an in-memory set; mark-after-ack is never
tested against failure. `backend.md` §6 requires a real server and a scroll-view outbox.

**Touch:** new `cloud-gateway/` (FastAPI + `qdrant-client`), `docker/` (compose with
`qdrant/qdrant`), `src/edge_node/outbox.py` (already has the scroll helper — wire it in),
`src/edge_node/main.py` (replace `hub` usage), delete reliance on `hub.py`,
`tests/test_sync.py`.

**Build:**
1. `docker/docker-compose.yml`: a `qdrant/qdrant` service on 6333; the gateway service; an env
   var `QDRANT_URL`.
2. Gateway: create the `fact_events` collection if absent; expose an ingest endpoint that
   upserts points and appends events; a delta endpoint returning the max `client_sequence` held
   for a `device_id`.
3. Edge `push()`: build the outbox from `outbox.get_outbox(mutable_shard, ...)` (the
   `scroll(_sync_meta.synced == false)` view, priority-ordered), push the delta to the gateway,
   and **only on HTTP ack** call `mark_synced` (the `set_payload_by_filter` path) + append the
   `OBSERVED` event. On failure, leave `synced=false`.
4. Idempotent upsert so a re-push after a crash is safe.

**Acceptance test (`tests/test_sync.py`):** with the transport forced to fail mid-push, assert
the point is still `synced==false` and still retrievable on the device; then let it succeed and
assert the server collection contains it. (Use a gateway stub that can be toggled to fail; the
real server runs via compose in an integration test marked accordingly.)

**Guardrails:** never mark synced before ack. No SQL in the gateway — `fact_events` is a Qdrant
collection. Delta only, never a full snapshot upload.

**Result:** the outbox is now a real `scroll` view over the mutable shard
(`_sync_meta.synced == 0 AND syncable == 1`, URGENT before ROUTINE); `synced`/`syncable`/
`client_sequence` are stored as **Integer 0/1** and Integer-indexed so the view actually drains
(the Phase 0 bool-filter trap). `push()` runs the delta handshake (`GET /delta/{device}`), pushes
only rows above the hub high-water mark through a `SyncTransport`, and marks synced **only on ack**
(`set_payload_by_filter` merging `_sync_meta`); on failure the point stays `synced == 0` and on the
device. New `cloud-gateway/` (FastAPI + `qdrant-client`) owns the `facts` + append-only `fact_events`
Qdrant collections — no SQL — with idempotent upserts and a hub-assigned event `seq`; `docker/
docker-compose.yml` brings up `qdrant/qdrant` + the gateway with `QDRANT_URL`. `hub.py` (in-memory)
deleted. Tests: `tests/test_sync.py` — forced mid-push failure leaves the point pending + queryable
(invariant 1), recovery marks it synced and the hub holds it (invariant 9), delta re-push is a
no-op, KEEP_LOCAL never leaves the device, URGENT drains before ROUTINE, plus an integration test
against the real compose stack (skipped without `GATEWAY_URL`, verified passing locally). Full suite
green (42, +1 skipped integration).

**Out of scope:** the consensus fold logic (phase 5) — here the gateway just stores events.

---

## Phase 4 — Partial-snapshot pull (learn from the fleet)

**Why:** `pull()` re-upserts a Python list, not a real partial snapshot; no test proves a device
learns a fact it never captured.

**Touch:** `src/edge_node/main.py` (`pull()`), sync helper module, `tests/test_pull.py`. Uses the
snapshot signatures confirmed in Phase 0 and the pattern in `docs/backend.md` §6.3 (mirrors the
Qdrant sync guide: `snapshot_manifest()` → POST `/collections/{c}/shards/0/snapshot/partial/create`
→ `update_from_snapshot(path)`).

**Build:**
1. Pause/buffer local writes; flush the outbox to the server first so the snapshot isn't stale.
2. Pull the partial snapshot into the `immutable` shard using the verified API.
3. Dedupe the mutable shard by `client_timestamp_ns <= sync_timestamp` via
   `delete_points_by_filter` (as the sync guide shows), so a pushed-then-pulled point isn't
   duplicated.

**Acceptance test (`tests/test_pull.py`):** device A captures + pushes fact X; device B (a
separate shard set, having never captured X) pulls; assert X is retrievable on B **and** came
from B's immutable shard (query B's immutable shard directly, not the union), proving cross-device
learning.

**Guardrails:** never write the immutable shard locally except via snapshot restore. Query still
reads both shards and dedupes by id.

**Result:** `pull()` now learns from the fleet via a **real partial snapshot**, not a re-upserted
list. It (1) flushes the outbox to the hub first so the snapshot isn't stale, (2) asks the hub for a
snapshot keyed off the immutable shard's `snapshot_manifest()` and restores it with the verified
`EdgeShard.update_from_snapshot(path)` — the only way the immutable shard is ever written — and (3)
dedupes the mutable shard by `client_timestamp_ns <= sync_timestamp` via `delete_points_by_filter`,
**guarded by `synced == 1`** so a pending point is never deleted (invariant 1). New
`src/edge_node/snapshot.py` packs hub facts into a genuine Edge shard snapshot (write + `optimize()`
so the segment version rises above the empty destination's, or the restore is silently skipped — a
trap found here); the Cloud Gateway gained `POST /snapshot/{device}` that materializes that snapshot
from its `facts` collection (Qdrant only, no second store), and `GatewayTransport.pull_snapshot`
streams it. `client_timestamp_ns` is now Integer-indexed for the range dedupe. Tests
(`tests/test_pull.py`): device B learns a fact it never captured and answers with it **from its
immutable shard** (queried directly), pull flushes before snapshotting, the mutable dedupe drops the
synced copy but never a pending one, and the union query dedupes by id when a fact is genuinely in
both shards; plus an integration test against the real compose stack (skipped without `GATEWAY_URL`).
Full suite green (47, +2 skipped integration). `docs/API.md` unchanged — `/pull` shape is identical.

**Out of scope:** trust/consensus (phase 5).

---

## Phase 5 — Multi-device consensus (the crown feature)

**Why:** events carry no `device_id` or `value`; the fold is a per-point trust scalar; there is
no `CONFIRMED`/`DISPUTED` or value resolution. This is the differentiator and it is mostly unbuilt.

**Touch:** `src/edge_node/consensus.py`, the gateway (fold lives here), `tests/test_consensus.py`.

**Build:**
1. Extend the event payload to `{corroboration_key, device_id, value, event_type, seq,
   client_timestamp_ns, device_trust_at_report}` where `seq` is **hub-assigned** (the gateway is
   the single sequencer) — device wall-clock is metadata only.
2. Implement the fold in the gateway over all live events for a `corroboration_key`, per
   `backend.md` §7.2: one device → LWW; agreeing devices → `CONFIRMED` with
   confidence = f(corroboration count × trust); disagreeing → weighted vote, and if below
   threshold → `DISPUTED` surfacing **both** values; field-level merges stay non-destructive.
3. Per-device trust = recency-decayed agreement rate, recomputed each fold (never stored
   separately). Keep the existing pure-fold + retraction-terminal guarantees.

**Acceptance test (`tests/test_consensus.py`):**
- Two devices report different values for one key offline → after reconnect the fold yields
  `DISPUTED` with both values and a confidence < threshold.
- Three agreeing devices → `CONFIRMED` with confidence rising vs one device.
- Shuffle event insertion order → byte-identical fold output.
- Skew one device's `client_timestamp_ns` by an hour → resolved value unchanged.
- observe → retract → re-observe identical fact → stays absent until the newer observe.

**Guardrails:** order strictly by hub `seq`. Trust is derived, never stored. Retraction is an
append, never a delete.

**Result:** the trust-weighted fold now lives in the Cloud Gateway as a pure, dependency-free module
(`cloud-gateway/consensus_fold.py`) — the single source of truth. `fold_consensus(events)` groups a
`corroboration_key`'s events, takes each device's latest event by **hub `seq`** (wall-clock is
metadata, never ordering), drops devices whose latest event is a retraction, weights the rest by
`device_trust_at_report × recency`, and resolves: one device → `LWW`; agreeing devices → `CONFIRMED`
with confidence rising via noisy-OR of the backers; disagreement below the config supermajority
(`consensus.confidence_threshold`, default 0.66) → `DISPUTED` surfacing **both** values, never a
silent pick; all-retracted → `ABSENT`. Output is byte-identical under event reordering (floats
rounded, sums taken in sorted order). `derive_device_trust` recomputes each device's agreement rate
from the log (never stored). The gateway ingest now stamps the rich event payload
(`corroboration_key/value/client_timestamp_ns/device_trust_at_report`, `seq` hub-assigned) and
exposes `GET /consensus/{key}`; `fact_events` gains a `corroboration_key` index (Qdrant only, no SQL).
Because the fold module is pure and importable, `tests/test_consensus.py` exercises the exact gateway
code with no network: DISPUTED-with-both-values, CONFIRMED-confidence-rises-vs-one, order-shuffle
identical (inv 6), hub-seq beats a one-hour clock skew (inv 5), observe→retract→re-observe stays
absent until the newer observe (inv 7), and a disagreeing device's derived trust drops. Each was
verified as a real guard by mutation (clock-ordering, retraction-ignore, silent-pick, unsorted output
all flip a test red). Full suite green (53, +3 skipped integration).

**Out of scope / deferred:** field-level non-destructive merge (each `corroboration_key` resolves as
one value today); propagating device-side `RETRACTED` events to the gateway so the hub fold sees them
(the local `/retract` still only appends to the on-device log — a follow-up). Semantic conflict
detection stays in `conflicts.py` as before.

---

## Phase 6 — Network layer (real intermittent connectivity)

**Why:** no `offline/degraded/full` simulator exists; goal 6 needs real, measured effects.

**Touch:** new `src/edge_node/network.py` (transport interceptor), `main.py` (`GET`/`POST
/network/mode`, route the sync client through it), `tests/test_network.py`.

**Build:**
1. A global mode with three states. The interceptor wraps ONLY the sync HTTP client used by
   push/pull — never the query/answer path.
2. `offline`: connection refused immediately; sync worker doesn't attempt.
3. `degraded`: real injected latency (config 400–1200 ms), a token-bucket byte cap (~64 kbps) over
   actual payload bytes, and a real failure rate (~8%). `URGENT` drains before `ROUTINE`.
4. `full`: no injection.
5. Log per push: bytes, duration, attempted/accepted/failed, priority mix, mode.

**Acceptance test (`tests/test_network.py`):** a static check that `query`/answer modules do not
import `network` (the isolation proof); query latency under `offline` is unchanged; under
`degraded`, measured added latency and at least one genuinely failed request are observed, and the
mark-after-ack rule from phase 3 holds under real failures.

**Guardrails:** the query path never references the simulator. Effects are real, never a label.

**Result:** new `src/edge_node/network.py` is a transport interceptor with a global mode
(`offline`/`degraded`/`full`) that wraps **only** the sync client (`get_delta`/`push`/`pull`/
`pull_snapshot`) via `NetworkTransport`. Effects are real: `offline` raises `NetworkError`
(connection refused) before any I/O; `degraded` does a genuine `time.sleep` latency (config
400–1200 ms), a real `TokenBucket` byte cap over the actual serialized payload bytes (~64 kbps),
and a real failure rate (~8%) that raises so `push` leaves the point pending (mark-after-ack,
invariant 1). `full` passes through. Each push logs bytes/duration/attempted/accepted/failed/
priority-mix/mode. To make invariant 8 architectural, the hybrid query moved into a new
`src/edge_node/retrieval.py` that imports **only** `qdrant_edge` — never the transport or the
simulator; `main.py`'s `/query` calls it, so `latency_ms` measures local search alone. `main.py`
now wires `NetworkTransport(GatewayTransport(...))` and exposes `GET`/`POST /network/mode`; config
gained a `network:` block. Tests (`tests/test_network.py`): a clean-subprocess import-graph check
that `retrieval` pulls in neither `network` nor `sync_transport` (invariant 8, verified red by
mutation), query stays fully answerable under `offline` with latency uninflated by an 800 ms wire,
`degraded` injects real measured latency and (over a seeded coin-flip wire) genuinely fails some
pushes while landing others, mark-after-ack holds under a forced drop (invariant 1), `offline`
refuses and the outbox accumulates, URGENT drains before ROUTINE, and the token bucket is a real
proportional wait. Full suite green (62, +3 skipped integration). `docs/API.md` already lists
`POST /network/mode`; no route contract change for the frontend.

**Out of scope:** UI wiring (frontend lane).

---

## Phase 7 — Answer layer (on-device RAG)

**Why:** goal 8. Retrieval returns chunks; we need a grounded generated answer, offline + online.

**Touch:** new `src/edge_node/answer.py`, a `Generator` adapter (Ollama + cloud) in the registry,
`main.py` (`POST /query` gains `answer`), `tests/test_answer.py`.

**Build:**
1. Retrieve = the hybrid query over both shards (phase 0 decides fuse-in-Python vs server fusion).
2. Augment: build a prompt with the top-k retrieved facts as the ONLY allowed context; prefer
   `CONFIRMED` facts, flag `DISPUTED`.
3. Generate: `OllamaGenerator` (POST `http://localhost:11434`, model `qwen2.5:1.5b`) offline;
   a cloud generator online; both satisfy the `Generator` Protocol. `ExtractiveGenerator`
   fallback (stitch top snippets) when no model is reachable.
4. Response reports `answer`, `answer_path` (`offline`/`online`/`extractive`), `model`,
   `latency_ms`, and `sources` (cited point ids) per `docs/API.md` §4.

**Acceptance test (`tests/test_answer.py`):** with a stub/extractive generator (no network in the
test), the same question returns a grounded answer citing real retrieved ids and a correct
`answer_path`; a live Ollama test is marked integration and skipped if the daemon is absent.

**Guardrails:** answer only from retrieved context (no free generation); never call the network in
unit tests; the generator is config-selected.

**Result:** new `src/edge_node/answer.py` is the RAG orchestrator (retrieve → augment → generate) and
imports **only** the standard library — never `qdrant_edge`, the sync transport, or the network
simulator (invariant 8, proven by a clean-subprocess import-graph test, red under mutation). Retrieval
stays the Phase-0 server-side-RRF hybrid over both shards (`retrieval.py`); `answer_question` augments
by putting **only** the top-k retrieved values into the prompt context — CONFIRMED facts promoted ahead
of uncorroborated ones, DISPUTED surfaced with a visible `[DISPUTED]` flag rather than silently dropped
(read from each hit's `consensus_state`, so the answer path never reaches the gateway). Three generators
behind the `Generator` Protocol live in the registry (`adapter.py`): `ExtractiveGenerator` (no model,
stitches snippets — the always-available fallback), `OllamaGenerator` (offline, `qwen2.5:1.5b` via
`POST /api/generate`), and `CloudGenerator` (online, openai-compatible); `registry.build_generators`
resolves the config `models.generator` block into a chain that always ends in extractive, so the device
answers even with no reachable model. The orchestrator tries the chain head-first and reports the
serving generator's `answer_path` (`offline`/`online`/`extractive`) + `name`; `sources` cites the exact
retrieved point ids with score + consensus state. `main.py`'s `POST /query` gained `answer`/`limit` and
returns `answer`/`answer_path`/`model`/`latency_ms`/`sources` (docs/API.md §4 shape — no route change);
`latency_ms` is measured over local search + local generation only. Tests (`tests/test_answer.py`):
answer-module isolation (inv 8), a grounded answer over three facts citing real ids with the correct
`answer_path` under `offline` (the acceptance test), the generator seeing **only** retrieved context
(grounding guard), CONFIRMED-preferred/DISPUTED-flagged augment, chain fallback past an unreachable model,
config-selected chain, and a live-Ollama integration test (warm-up-or-skip, verified passing locally).
Each guard was confirmed real by mutation (ungrounded stitch, dropped consensus preference, injected
outside context, broken isolation all flip a test red). Full suite green (69, +3 skipped integration).

**Out of scope:** fine-tuning (never), streaming tokens (nice-to-have).

**Deferred:** stamping the gateway fold's `consensus_state` onto pulled facts so the prefer-CONFIRMED /
flag-DISPUTED augment is driven end-to-end in production (today it activates on any fact that carries
`consensus_state`, exercised by the tests; the on-device query path stays offline and never calls the
gateway fold). A cloud `online:` generator block is resolvable but omitted from shipped config rather
than shipped as a placeholder.

---

## Phase 8 — Eviction + inspection endpoints

**Why:** goal 4/7. Memory cap + safe eviction and the four inspection surfaces are missing.

**Touch:** `main.py`, new `src/edge_node/telemetry.py`, `tests/test_memory.py`,
`tests/test_endpoints.py`. Shapes are fixed by `docs/API.md`.

**Build:**
1. Config cap (`max_local_points`); evict oldest-and-least-queried **only among
   `_sync_meta.synced==true`**, then `optimize()`. Never evict pending points.
2. Endpoints returning the exact `API.md` shapes: `GET /devices`, `/devices/{id}`,
   `/devices/{id}/memory` + `/{point_id}`, `/sync` (pending via `facet`), `/activity` (ring
   buffer), `/telemetry`, `/cloud/state`; WS `/devices/{id}/events`, `/consensus/events`.
3. `telemetry.py` reads real cgroup CPU/RSS and measured query p50/p95; labels the target
   "emulated constrained target".

**Acceptance test:** overfill the cap with pending points → none evicted; overfill with synced
points → oldest evicted and count drops after `optimize()`; each endpoint returns the documented
shape; telemetry values are read from the real process, not constants.

**Guardrails:** counts come from `count()`/`facet()`, not app-side loops. Activity ring buffer is
in-memory (operational telemetry, not facts) — still no SQL.

**Result:** new `src/edge_node/telemetry.py` reads real cgroup CPU/RSS and process metrics, with
query latency p50/p95 recorded on every `/query` call; label is "emulated constrained target" per
API.md §11. New `src/edge_node/eviction.py` enforces `max_local_points` from config — evicts
oldest synced points (`_sync_meta.synced==1`) by `client_timestamp_ns` and calls `optimize()`
to reclaim space; pending points (`synced==0`) are NEVER touched (invariant 3, proven by test).
All inspection endpoints implemented in `main.py` with exact API.md shapes:
`GET /devices`, `/devices/{id}`, `/devices/{id}/memory`, `/devices/{id}/memory/{point_id}`,
`/devices/{id}/sync`, `/devices/{id}/activity`, `/devices/{id}/telemetry`, `/cloud/state`;
WebSocket `/devices/{id}/events` and `/consensus/events` for live feeds. Activity ring buffer
(in-memory, bounded, newest-first) logs capture/decision/push/pull/retraction/mode_change events.
Config `memory.max_local_points` added to `config/disaster-response.yaml`. Tests
(`tests/test_memory.py`): pending-points-not-evicted, synced-points-evicted-oldest-first,
telemetry-reads-real-metrics, all endpoint shapes verified, retrieval/answer isolation
(invariant 8). Full suite green (81 passed, 3 skipped).

**Out of scope:** frontend rendering.

---

## Phase 9 — Benchmarks (measured, not asserted)

**Why:** goal 5/7 evidence. Current benchmark is a synthetic single-point trajectory; no labeled
recall or resolver-accuracy.

**Touch:** `tests/fixtures/` (labeled data), `main.py` (`GET /benchmark/recall`,
`/benchmark/resolver-vs-lww`), `tests/test_benchmark.py`.

**Build:**
1. A small labeled set (~30–40 queries with known-relevant ids) → compute recall@5 dense-only vs
   hybrid; return both.
2. A labeled conflict fixture (~30 facts, known-correct values) + generated dispute scenarios →
   compute resolver accuracy vs an LWW baseline over the same scenarios; return both numbers.

**Acceptance test (`tests/test_benchmark.py`):** hybrid recall@5 strictly > dense on the labeled
set; resolver accuracy computed from real runs and > LWW; numbers are recomputed on each call, not
hardcoded.

**Guardrails:** fixtures live in the repo; endpoints recompute, never return constants.

**Out of scope:** tuning to hit a specific headline number — report whatever is measured.

---

## Phase 10 — Docker + telemetry integration

**Why:** the "emulated constrained target" must be a real CPU/RAM-limited container so telemetry
is honest.

**Touch:** `docker/` (compose + edge Dockerfile), `docs/` run instructions.

**Build:** compose brings up Qdrant Server, the gateway, N edge containers with
`--cpus`/`--memory` limits, and Ollama; telemetry reads the real cgroup limit; the UI label
reflects the actual container size.

**Acceptance test:** an edge node runs in a `--cpus=1 --memory=512m` container; `/telemetry`
reports RSS/CPU read from that container's cgroup; the label matches the real limit.

**Guardrails:** never report a Pi number we didn't measure; label emulated targets as such.

**Out of scope:** cloud deployment, k8s.

---

## Definition of done for any phase (the gate `/p` enforces before committing)
1. The acceptance test exists, was red before the change, green after.
2. Full suite green; **no trivial/false test** (single-doc ranking, tolerates zero/empty,
   un-isolated immutable shard, a push that never fails).
3. Every invariant the phase touches (`docs/AGENTS.md` §5) has a test.
4. No bug of the audit classes (dead branch, verdict-with-no-effect, second datastore,
   hardcoded/mocked runtime path).
5. `frontend/` untouched; `docs/API.md` updated if routes changed; this file's status set to DONE.
