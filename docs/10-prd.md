# Ex-Vero — Product & Software Requirements (PRD / SRS)

**Role of this document:** the requirements contract. It states, in testable terms, what
the product must do (functional requirements), how well it must do it (non-functional
requirements), who uses it (personas + user stories), and how we know each requirement
is satisfied (acceptance criteria). It maps every requirement back to the sponsor brief
in `00-problem-statement.md`.

**Status:** baseline v1. **Sponsor:** Qdrant. **Datastore:** Qdrant Edge (device) +
Qdrant Server (cloud) — the only store in the system.

---

## 1. Purpose and scope

### 1.1 Purpose
Deliver an offline-first, AI-powered edge memory and intelligence platform on Qdrant
Edge: each device remembers, retrieves, and answers locally with no network, decides
what to sync, and a fleet of devices reconciles conflicting knowledge into one trusted
picture when connectivity returns.

### 1.2 In scope
On-device semantic + hybrid memory, per-fact decision engine, real edge↔Qdrant Server
sync, trust-weighted multi-device consensus, on-device RAG answers (offline + online),
an inspection UI, and a real (emulated-hardware) constrained-target measurement path.

### 1.3 Out of scope
Production auth / multi-tenancy, model training, custom ANN indexes, any SQL or second
datastore. See `backend.md` §12.

### 1.4 Definitions
- **Fact** — a captured text or image observation stored as a Qdrant point.
- **Corroboration key** — a normalized identifier for the real-world fact a report is
  about (e.g. `zone_c.hazard_status`).
- **Hybrid search** — dense (vector) + sparse (BM25) results fused with RRF.
- **RAG** — retrieve from Qdrant Edge, augment the prompt, generate a grounded answer.
- **Emulated constrained target** — a real CPU/RAM-limited container standing in for
  physical edge hardware; all metrics measured, never faked.

---

## 2. Personas and user stories

### 2.1 Personas
- **Field responder** — captures facts on a device with poor/no connectivity; needs
  instant local answers.
- **Dispatcher / commander** — watches the merged, trusted cloud picture; needs to know
  confidence and disputes.
- **Judge / evaluator** — inspects any fact's full lifecycle and verifies claims.

### 2.2 User stories
- *As a field responder,* I capture a hazard note offline and immediately search and get
  an answer, so I don't wait for a network.
- *As a field responder,* I see why each note was kept, queued, redacted, or rejected, so
  the system's judgment is legible.
- *As a dispatcher,* I see one trusted value per real-world fact with a confidence score,
  and an explicit DISPUTED state when devices disagree, so I don't act on a false single
  answer.
- *As a dispatcher,* I see a safety-critical report jump the sync queue ahead of routine
  ones on a bad link.
- *As a judge,* I pick any fact and trace its content, decision reason, sync state, and
  full activity history in two clicks.

---

## 3. Functional requirements

Each maps to a brief goal (G1–G8) and has an acceptance criterion (AC).

| ID | Requirement | Goal | Acceptance criterion |
|----|-------------|------|----------------------|
| FR-1 | Capture text and image facts, embed on-device, store in a local Qdrant Edge shard | G1 | A captured fact is retrievable by search in the same process with no restart |
| FR-2 | Low-latency **hybrid** search (dense + BM25, RRF) over both shards, offline | G5 | Recall@5 dense-only vs hybrid measured on a labeled set; hybrid strictly higher; runs under `offline` |
| FR-3 | Per-fact Decision Engine → verdict + human-readable reason | G2 | Every capture shows a verdict from `{KEEP_LOCAL, QUEUE_LOW, QUEUE_HIGH, REDACT_AND_QUEUE, REJECT}` and a reason traceable to config |
| FR-4 | PII detection with redaction / local-only flagging | G2 | A sensitive record is redacted or never queued; bytes-to-cloud surfaced |
| FR-5 | Local memory cap + safe eviction | G7 | Only synced points evict; pending points never evict (test); UI shows `used/cap` |
| FR-6 | Delta-only push to a **real Qdrant Server**, mark-after-ack | G3 | Kill transport mid-push → point still pending & on device; reconnect → server collection updates |
| FR-7 | Partial-snapshot pull into the immutable shard | G3 | A device answers with a fact it never captured, sourced from another device |
| FR-8 | Trust-weighted consensus fold over an append-only log | G7 | Two devices disagree offline → converge with a confidence score on reconnect |
| FR-9 | Retraction that cannot ghost | G7 | Observe → retract → re-observe: fact stays absent everywhere until a newer observation; retraction propagates to all devices |
| FR-10 | Semantic conflict detection (vector index as detector) | G7 | A same-zone conflict with a different key is surfaced as POSSIBLE_CONFLICT; never auto-merged; cross-zone never flagged |
| FR-11 | On-device RAG answer, offline + online, one interface | G8 | Same question yields a real grounded answer offline (Ollama) and online (cloud), with cited sources and reported path |
| FR-12 | Network modes offline/degraded/full with real effects | G6 | `offline` leaves query latency unchanged; `degraded` shows measured added latency, byte cap, real failures; urgent drains first |
| FR-13 | Inspection surfaces: memory, search, sync status, activity | G4 | A judge traces any fact's content, reason, sync state, and activity in two clicks |
| FR-14 | Config-driven Model Adapter Registry | G2/G8 | A model swaps by YAML only; every adapter passes the conformance suite |
| FR-15 | Resolver-vs-LWW benchmark | G7 | Accuracy of resolver vs last-write-wins computed over a labeled fixture set and reported |

---

## 4. Non-functional requirements

| ID | Category | Requirement | Verification |
|----|----------|-------------|--------------|
| NFR-1 | Latency | Offline query returns with a measured `latency_ms`; no network in the path | Static import test + timed query under `offline` |
| NFR-2 | Resource honesty | CPU/RAM/latency read from the real process/cgroup; labeled "emulated constrained target" | Telemetry compared to `docker stats`; label present in UI |
| NFR-3 | Correctness | Fold is a pure function of the log (order-independent); orders by hub sequence, not clocks | Shuffle test; clock-skew test |
| NFR-4 | Durability | No fact lost on crash mid-push (mark-after-ack); upsert idempotent | Transport-kill test |
| NFR-5 | Portability | Same container image runs on a laptop or a Pi with no code change | Build once, run on both targets |
| NFR-6 | Privacy | Sensitive data redacted before leaving device; bytes-to-cloud observable | Redaction test + on-screen byte count |
| NFR-7 | Reproducibility | Benchmarks computed from real runs, not asserted constants | Benchmark endpoint recomputes on call |
| NFR-8 | Simplicity | No SQL, no second store, no RAG framework | Dependency + code review |
| NFR-9 | Pinning | `qdrant-edge-py` pinned exactly (beta API drifts); never floated | `pyproject.toml` / `uv.lock` check |

---

## 5. Constraints and assumptions

- **Beta dependency:** Qdrant Edge is in beta; the API drifts between releases — pin the
  exact version. (Verified against live Qdrant docs.)
- **No physical hardware yet:** the constrained target is a real CPU/RAM-limited
  container; all metrics measured; relabels to physical hardware later with no code
  change.
- **Models are pretrained checkpoints** selected in config; no training; `version`
  stamped on every point.
- **Device-to-device reach is via the hub** (A → hub → snapshot → B); no peer link.

---

## 6. Traceability (brief → requirements)

| Brief goal | Requirements |
|---|---|
| G1 on-device memory | FR-1, NFR-1 |
| G2 decide local vs sync | FR-3, FR-4, FR-14 |
| G3 sync edge ↔ server | FR-6, FR-7 |
| G4 inspection UI | FR-13 |
| G5 hybrid search offline | FR-2 |
| G6 intermittent connectivity | FR-12, NFR-1 |
| G7 evolving memory & conflicts | FR-5, FR-8, FR-9, FR-10, FR-15 |
| G8 meaningful AI workflow | FR-11 |

Every brief goal has at least one requirement, and every requirement has an acceptance
criterion. Requirements without a passing test or on-screen measurement are not "done."
