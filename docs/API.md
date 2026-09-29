# Aegis Edge — API Contract (`API.md`)

**Role of this document:** the complete request/response contract between the backend and
the frontend, so the UI team can build in parallel against fixed shapes. This is the
authoritative source for routes; `backend.md` §10 is the summary and `frontend.md` maps
routes to screens.

> [!NOTE]
> **Conventions**
> - Base URL: `http://localhost:8000` (dev). Every route below is relative to it.
> - `{id}` is a device id, e.g. `medic-01`. Device-scoped routes are handled by that
>   device's edge-node process; `/cloud/*` and `/network/*` are handled by the gateway.
> - All bodies are JSON (`Content-Type: application/json`) unless stated (image capture
>   is `multipart/form-data`).
> - Timestamps are RFC-3339 UTC strings (e.g. `2026-09-29T19:53:17Z`). Scores are floats
>   in `[0,1]` unless noted. IDs are unsigned 64-bit integers serialized as JSON numbers.
> - Errors use standard HTTP status codes with body `{ "detail": "<message>" }`.
> - **The query and answer paths never touch the network layer** — they work identically
>   in every network mode.

---

## 1. Shared object shapes

These objects recur across routes. Defined once here; referenced by name below.

### `Device`
```json
{
  "id": "medic-01",
  "name": "Paramedic Tablet 01",
  "connectivity": "offline",              // "offline" | "degraded" | "full"
  "memory": { "used": 340, "cap": 500 },  // local points used / cap
  "last_sync_at": "2026-09-29T19:50:02Z", // null if never synced
  "trust": 0.82,                          // this device's current fleet trust score
  "activity_sparkline": [3, 5, 2, 8, 1]   // recent decision counts, oldest→newest
}
```

### `MemoryPoint`
```json
{
  "id": 172946122334,
  "value": "Structural collapse at Zone C east stairwell",
  "modality": "text",                     // "text" | "vision"
  "thumbnail_url": null,                   // set when modality == "vision"
  "zone": "C",
  "corroboration_key": "zone_c.hazard",
  "sync_state": "synced",                 // "local_only" | "pending" | "synced"
  "model": "text_dense",
  "model_version": "bge-small-en-v1.5",
  "created_at": "2026-09-29T19:49:00Z"
}
```

### `DecisionEvent` (decision feed)
```json
{
  "device_id": "medic-01",
  "point_id": 172946122334,
  "value_preview": "Structural collapse at Zone C east stairwell",
  "modality": "text",
  "thumbnail_url": null,
  "verdict": "QUEUE_HIGH",                // KEEP_LOCAL | QUEUE_LOW | QUEUE_HIGH | REDACT_AND_QUEUE | REJECT
  "reason": "urgency 0.91 — pushed immediately despite degraded link",
  "timestamp": "2026-09-29T19:49:00Z"
}
```

### `ActivityEntry`
```json
{
  "device_id": "medic-01",
  "kind": "push_result",                  // capture | decision | push_attempt | push_result | pull_result | consensus | retraction | error | mode_change
  "detail": "pushed 4 points, 2 failed, 18.2 KB, mode=degraded",
  "point_id": 172946122334,               // nullable
  "timestamp": "2026-09-29T19:50:02Z"
}
```

### `ConsensusEvent`
```json
{
  "corroboration_key": "zone_c.hazard",
  "state": "DISPUTED",                    // CONFIRMED | DISPUTED | RESOLVED_LWW | RETRACTED
  "confidence": 0.61,
  "resolved_value": null,                 // set when CONFIRMED/RESOLVED_LWW; null when DISPUTED
  "candidates": [
    { "value": "gas leak", "devices": ["medic-01","kiosk-02"], "weight": 0.74 },
    { "value": "no hazard", "devices": ["medic-03"], "weight": 0.39 }
  ],
  "explanation": "2 corroborating devices vs 1; confidence 0.61 < 0.7 → DISPUTED",
  "timestamp": "2026-09-29T19:50:05Z"
}
```

---

## 2. Fleet & devices

### `GET /devices`
Fleet state for the Fleet Overview screen.
- **200** → `{ "devices": [Device, ...] }`

### `GET /devices/{id}`
Single device detail (header of the Device Console).
- **200** → `Device`
- **404** → unknown device

---

## 3. Capture

### `POST /devices/{id}/capture` — text fact
Submit a text fact. Runs the Decision Engine synchronously and returns the verdict.
- **Body**
```json
{
  "value": "Gas smell near the east stairwell",
  "corroboration_key": "zone_c.hazard",
  "zone": "C",
  "entity": "gas",
  "status": "unverified",
  "reporter_device_id": "medic-01"
}
```
- **200**
```json
{
  "id": 172946122334,
  "verdict": "QUEUE_HIGH",
  "reason": "urgency 0.88 — queued high priority",
  "conflicts": [ /* POSSIBLE_CONFLICT records, see §9 */ ]
}
```

### `POST /devices/{id}/capture` — image fact
Same route, `multipart/form-data`.
- **Form fields:** `file` (image binary), `corroboration_key`, `zone`, `entity` (optional),
  `caption` (optional text).
- **200** → same shape as text capture, plus `"modality": "vision"` and `"thumbnail_url"`.

---

## 4. Query + Answer (on-device RAG)

### `POST /devices/{id}/query`
Hybrid search (dense + BM25, RRF) over both shards, then a grounded RAG answer. Works in
every network mode. `latency_ms` is measured.
- **Body**
```json
{ "text": "which zones have active gas hazards?", "answer": true, "limit": 10 }
```
`answer: false` returns retrieval only (no generation).
- **200**
```json
{
  "answer": "Zone C has a gas hazard reported at the east stairwell, confirmed by 3 devices. Zone E is disputed.",
  "answer_path": "offline",               // "offline" (Ollama) | "online" (cloud) | "extractive"
  "model": "qwen2.5:1.5b",
  "latency_ms": 42.7,
  "sources": [
    { "id": 172946122334, "score": 0.0163, "value": "Gas leak at Zone C east stairwell", "consensus_state": "CONFIRMED" }
  ],
  "results": [ MemoryPoint-with-score, ... ]   // full ranked list for the results panel
}
```

---

## 5. Local memory browser

### `GET /devices/{id}/memory`
List/filter everything held locally (Memory Browser panel).
- **Query params:** `q` (text filter), `sync_state`, `modality`, `zone`, `limit`
  (default 100), `offset`.
- **200** → `{ "points": [MemoryPoint, ...], "total": 340 }`

### `GET /devices/{id}/memory/{point_id}`
Full detail for one fact, including why it is in its state.
- **200**
```json
{
  "point": MemoryPoint,
  "decision": DecisionEvent,
  "activity": [ActivityEntry, ...],
  "consensus": ConsensusEvent            // null if not yet folded
}
```

---

## 6. Decision feed & activity (history + live)

### `GET /devices/{id}/feed`
Decision-feed history, newest first.
- **Query params:** `limit` (default 50).
- **200** → `{ "events": [DecisionEvent, ...] }`

### `GET /devices/{id}/activity`
System-activity ring buffer, newest first.
- **Query params:** `kind` (optional filter), `limit` (default 100).
- **200** → `{ "entries": [ActivityEntry, ...] }`

### `WS /devices/{id}/events`
Live stream for the Decision Feed and Activity Log. Server pushes frames:
```json
{ "type": "decision", "data": DecisionEvent }
{ "type": "activity", "data": ActivityEntry }
```

---

## 7. Sync status & manual sync

### `GET /devices/{id}/sync`
Sync-status strip data. Pending counts come from a `facet` on the mutable shard.
- **200**
```json
{
  "pending": { "URGENT": 2, "ROUTINE": 11, "HELD": 3 },
  "last_attempt_at": "2026-09-29T19:50:00Z",
  "last_success_at": "2026-09-29T19:50:02Z",
  "consecutive_failures": 1,
  "next_backoff_ms": 4000,
  "last_push": { "bytes": 18624, "duration_ms": 820, "points_attempted": 4, "points_accepted": 2, "points_failed": 2, "mode": "degraded" },
  "last_pull": { "at": "2026-09-29T19:48:00Z", "points_received": 37 }
}
```

### `POST /devices/{id}/push`
Manually trigger an outbox drain (also runs automatically). Delta-only; marks synced only
after the hub acks.
- **200** → `{ "pushed_count": 2, "failed_count": 2, "bytes": 18624, "errors": [] }`

### `POST /devices/{id}/pull`
Manually trigger a partial-snapshot pull from Qdrant Server into the immutable shard.
- **200** → `{ "pulled_count": 37, "errors": [] }`

---

## 8. Consensus, retraction & cloud state

### `GET /cloud/state`
The merged, trusted picture for the Command Dashboard (fold output). Only facts past the
trust gate; retracted facts are absent by construction.
- **Query params:** `state` (optional: `CONFIRMED`/`DISPUTED`), `zone`.
- **200**
```json
{
  "facts": [
    {
      "corroboration_key": "zone_c.hazard",
      "state": "CONFIRMED",
      "value": "gas leak",
      "confidence": 0.87,
      "corroborating_devices": ["medic-01","kiosk-02","medic-04"],
      "updated_at": "2026-09-29T19:50:05Z"
    }
  ],
  "device_trust": { "medic-01": 0.82, "medic-03": 0.42 }
}
```

### `POST /devices/{id}/retract/{point_id}`
Append a `RETRACTED` event. Retraction can never ghost back.
- **200** → `{ "retracted": true, "point_id": 172946122334 }`

### `GET /devices/{id}/conflicts`
`POSSIBLE_CONFLICT` candidates the exact-key scheme would miss (semantic detection).
- **200** → `{ "device_id": "medic-01", "conflicts": [ConflictRecord, ...] }` (see §9)

### `WS /consensus/events`
Live resolver decisions for the Conflict Theater. Frames are `ConsensusEvent` objects.

---

## 9. `ConflictRecord` shape (returned by capture + `/conflicts`)
```json
{
  "status": "POSSIBLE_CONFLICT",
  "new_point_id": 172946122334,
  "existing_point_id": 172900001111,
  "score": 0.0182,
  "zone": "C",
  "new_value": "gas smell near east stairs",
  "existing_value": "strong gas leak reported in the east stairwell",
  "new_key": "reportB",
  "existing_key": "reportA"
}
```

---

## 10. Network simulator

### `GET /network/mode`
- **200** → `{ "mode": "degraded" }`

### `POST /network/mode`
Set the global network mode. Affects sync transport only.
- **Body** → `{ "mode": "offline" }`  // "offline" | "degraded" | "full"
- **200** → `{ "mode": "offline" }`

---

## 11. Telemetry (real, from the emulated constrained target)

### `GET /devices/{id}/telemetry`
Live CPU/RAM/latency read from the real cgroup/process. Labeled as an emulated container.
- **200**
```json
{
  "target_label": "emulated constrained target (1 CPU / 512 MB container)",
  "cpu_pct": 63.2,
  "ram_mb": 218.4,
  "ram_limit_mb": 512,
  "model_load_ms": 1840,
  "query_latency_p50_ms": 38.1,
  "query_latency_p95_ms": 71.9
}
```

---

## 12. Demo controls (reproducible conflicts)

### `POST /demo/inject-conflict`
Force a specific disagreement between a device pair (Conflict Theater).
- **Body**
```json
{ "corroboration_key": "zone_c.hazard", "assignments": { "medic-01": "gas leak", "medic-03": "no hazard" } }
```
- **200** → `{ "injected": true }`

### `POST /devices/{id}/rogue`
Toggle a device into "rogue" mode (reports wrong values consistently) so trust decay is
observable.
- **Body** → `{ "rogue": true }`
- **200** → `{ "id": "medic-03", "rogue": true }`

---

## 13. Benchmarks

### `GET /benchmark/resolver-vs-lww`
Computed resolver-vs-last-write-wins accuracy over the labeled fixture set.
- **200**
```json
{
  "resolver_accuracy": 0.94,
  "lww_accuracy": 0.71,
  "scenarios": 300,
  "trajectory": [ { "seq": 1, "event": "OBSERVED", "resolver": 0.73, "lww": 1.0 } ]
}
```

### `GET /benchmark/recall`
Dense-only vs hybrid recall@5 on the labeled set.
- **200** → `{ "dense_recall_at_5": 0.62, "hybrid_recall_at_5": 0.84, "labeled_queries": 40 }`

---

## 14. Route → screen quick map

| Route | Screen (`frontend.md`) |
|---|---|
| `GET /devices`, `POST /network/mode` | 3.1 Fleet Overview |
| `POST /query`, `GET /telemetry` | 3.2 Device Console (Q&A + resource strip) |
| `WS /devices/{id}/events`, `GET /feed` | 3.2 Live Decision Feed |
| `GET /memory`, `GET /memory/{pid}` | 3.2 Local Memory Browser |
| `GET /sync` | 3.2 Sync Status strip |
| `GET /activity` | 3.2b Activity Log |
| `POST /capture` | 3.3 Capture Panel |
| `WS /consensus/events`, `POST /demo/inject-conflict`, `POST /rogue`, `POST /network/mode` | 3.4 Conflict Theater |
| `GET /cloud/state`, `POST /retract/{pid}` | 3.5 Command Dashboard |
| `GET /benchmark/*` | evidence overlays |
