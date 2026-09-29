# Aegis Edge — Architecture

**Role of this document:** the system view. It shows the processes, the data flow, the
Qdrant Edge shard model, the sync loop, and the consensus fold in one place, so anyone
can see how the parts fit before reading `backend.md` for detail.

**Principle:** Qdrant is the only datastore — Qdrant Edge on device, Qdrant Server in the
cloud. No SQL, no second store. Real code; only the hardware and network are emulated,
with measured effects.

---

## 1. System context

```mermaid
flowchart TB
    subgraph FLEET["Edge fleet — real processes, CPU/RAM-limited containers"]
        A["Edge Node A"]
        B["Edge Node B"]
        C["Edge Node C"]
    end
    NET{{"Network Layer\noffline / degraded / full\n(real latency, cap, loss)"}}
    GW["Cloud Gateway (FastAPI)\nschema + consensus fold"]
    QS[("Qdrant Server\ncollection: fact_events\nappend-only")]
    UI["Frontend (React/Vite/TS)\ndevice switcher + dashboards"]

    A -- sync only --> NET
    B -- sync only --> NET
    C -- sync only --> NET
    NET --> GW
    GW --> QS
    QS -- partial snapshot --> NET
    UI -. REST/WS .-> A
    UI -. REST/WS .-> B
    UI -. REST/WS .-> C
    UI -. GET /cloud/state .-> GW
```

The query and answer paths never touch the Network Layer — that isolation is the proof
that offline search is instant.

---

## 2. Inside one edge node

```mermaid
flowchart LR
    CAP["Capture\ntext / image"] --> REG["Model Adapter Registry\ntext_dense · CLIP · BM25"]
    REG --> MUT[("mutable shard\nlocal writes")]
    MUT --> DE["Decision Engine"]
    DE -->|verdict + reason| FEED["Decision Feed (WS)"]
    MUT --> QRY["Hybrid query\ndense + BM25 -> RRF"]
    IMM[("immutable shard\nhub mirror, HNSW")] --> QRY
    QRY --> RAG["Answer layer (RAG)\nOllama offline / cloud online"]
    RAG -->|answer + sources + path| OUT["Query response"]
    MUT -->|scroll synced == false| OBX["Outbox (view, not a store)"]
    OBX -->|delta push, mark after ack| NET{{Network Layer}}
```

Two shards; queries read both and dedupe by point ID. The outbox is a filtered scroll
over the mutable shard, not a second queue.

---

## 3. Sync loop (edge ↔ real Qdrant Server)

```mermaid
sequenceDiagram
    participant D as Edge device
    participant N as Network Layer
    participant G as Cloud Gateway
    participant S as Qdrant Server

    Note over D: offline — outbox accumulates, device fully queryable
    D->>D: capture + decide + store (mutable shard)
    Note over D,N: connectivity returns (mode = degraded/full)
    D->>N: push delta (only client_sequence > hub max), priority-ordered
    N->>G: forward (real latency / cap / possible failure)
    G->>S: upsert points + append OBSERVED to fact_events
    S-->>G: ack
    G-->>D: ack
    D->>D: mark synced (only now) 
    G->>S: fold events for corroboration_key
    D->>S: snapshot_manifest -> partial snapshot
    S-->>D: partial snapshot
    D->>D: update immutable shard, dedupe mutable by timestamp
    Note over D: device now knows facts it never captured
```

Mark-after-ack is what makes a crash mid-push harmless: the point stays pending and on
the device, and upsert is idempotent on re-push.

---

## 4. Consensus fold (the trust layer)

```mermaid
flowchart TB
    EV[("fact_events\nOBSERVED / RETRACTED / CONFLICT_OPENED\nappend-only")]
    EV --> SEL["select live events\nfor corroboration_key\nordered by hub sequence"]
    SEL --> N1{"how many\ndevices?"}
    N1 -->|one| LWW["LWW fast path"]
    N1 -->|agree| CONF["CONFIRMED\nconfidence = f(count × trust)"]
    N1 -->|disagree| DIS["weighted vote"]
    DIS -->|below threshold| DISP["DISPUTED\nshow both values"]
    DIS -->|above threshold| CONF
    SEL --> SEM["semantic detector\nhybrid-query hub,\nzone/entity guard"]
    SEM --> PC["POSSIBLE_CONFLICT\n(review, never auto-merge)"]
    CONF --> STATE["/cloud/state\ntrusted picture"]
    DISP --> STATE
```

Trust is derived from the log (agreement rate, recency-decayed), never stored beside it,
so it can't drift out of sync with history. Retraction is an append, so it can't ghost.

---

## 5. Physical emulation map

| Physical thing | Emulated by | Measured as |
|---|---|---|
| Device compute/RAM | Docker container with `--cpus` / `--memory` limits | real cgroup CPU %, RSS MB |
| Slow/lossy link | Network Layer token bucket + latency + failure injection | real bytes-over-wire, retries, duration |
| Going offline / reconnect | global `POST /network/mode` flip | outbox depth, drain events |
| Clock drift | per-device skew knob (metadata only) | fold result unchanged (test) |

Everything above is real code with real measurement; only the *device* and the *link
conditions* are stand-ins for physical hardware we don't have yet.
