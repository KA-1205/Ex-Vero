# Problem Statement 03 — AI-Powered Edge Memory & Intelligence Platform

**Sponsor: Qdrant.** This project is built on and around Qdrant. Qdrant Edge is the
on-device vector engine, Qdrant Server is the cloud hub, and Qdrant is the *only*
datastore anywhere in the system — there is no SQL and no second store. Every design
decision in this repo is made to show Qdrant Edge doing real work on a real
edge-to-cloud workflow.

> This document is the source of truth for *what* we are building and *why*. It is
> our own restatement of the sponsor brief plus the acceptance bar we hold ourselves
> to. `backend.md` and `frontend.md` describe *how*. `AGENTS.md` is the build guide.

---

## 1. The brief, verbatim

> AI applications increasingly need to operate in environments where network
> connectivity is limited, latency is critical, and sensitive data cannot always
> leave the device. Robots, industrial systems, kiosks, vehicles, mobile devices,
> and other edge applications need to search and reason over locally generated
> information without continuously depending on a cloud service.
>
> Building such systems is challenging because applications need to maintain local
> vector memory, perform fast semantic retrieval, work offline, handle continuously
> changing data, and synchronize relevant information with the cloud when
> connectivity becomes available.
>
> The challenge is to build an AI-powered edge intelligence platform that uses
> **Qdrant Edge** to provide local semantic memory and retrieval, while intelligently
> managing the relationship between on-device data and centralized cloud knowledge.

### Goal — build an offline-first AI application powered by Qdrant Edge that can:

1. Maintain searchable semantic memory directly on an edge device.
2. Dynamically decide what information should remain local and what should be synchronized.
3. Synchronize data between edge devices and **Qdrant Server** when connectivity returns.
4. Provide a user-facing interface to inspect device memory, search results, synchronization status, and system activity.
5. Perform low-latency vector **and hybrid** search without network access.
6. Support intermittent connectivity and continue operating offline.
7. Handle evolving local memory, updates, and conflicting information.
8. Demonstrate a meaningful edge-to-cloud AI workflow, rather than simply running a local vector database.

### Expected outcome

> A complete edge-native AI product that can remember, retrieve, operate offline, and
> synchronize intelligently when connected.

---

## 2. Our reading of it (and the trap)

The single most important line is goal 8: *"a meaningful edge-to-cloud AI workflow,
rather than simply running a local vector database."* A local vector store with a
search box and a sync button satisfies goals 1, 3, and 5 and still fails the brief.
The product is the **intelligence layer on top of Qdrant**:

- a **decision layer** that reasons about every fact (keep / queue / redact / reject)
  and can explain itself in plain language, and
- a **consensus layer** that reconciles a *fleet* of devices that learned different,
  sometimes conflicting things while disconnected, into one trusted picture with a
  visible confidence score, and
- a **generation layer** that produces real answers on-device offline and in the cloud
  online, behind one interface.

Qdrant Edge is the substrate that makes all three possible offline; Qdrant Server is
where the fleet's knowledge converges.

## 3. What we are building — Ex-Vero

An **offline-first edge memory kernel** demonstrated as a **disaster-response fleet**
(paramedic tablets and triage kiosks — the hardest case: worst connectivity, highest
stakes). The disaster-response scenario is *configuration, not code*: swap one YAML
file and the same kernel runs a different vertical.

Each edge device runs a single process that:

- captures text and images, embeds them **on-device**, and stores them in a local
  **Qdrant Edge** shard;
- answers **hybrid** (dense + BM25) queries locally with measured latency, offline;
- runs every captured fact through a **Decision Engine** that decides what stays local
  and what syncs, with a human-readable reason;
- generates a grounded **answer** from retrieved context — a small local model when
  offline, a cloud model when connected;
- pushes local facts to a real **Qdrant Server** hub and pulls fleet knowledge back
  via partial snapshots when connectivity returns.

A cloud gateway folds every device's reports into a trusted, deduplicated picture and
resolves conflicts with a trust-weighted consensus, not last-write-wins.

## 4. Acceptance bar per goal

A goal is not "done" because it runs once. It is done when its acceptance line holds
and there is a test or an on-screen measurement proving it.

| # | Goal | Done when |
|---|------|-----------|
| 1 | On-device semantic memory | A query returns ranked results with a measured `latency_ms` while connectivity is `offline`, with no network call in the path — proven by a test that the query module never imports the transport. |
| 2 | Decide local vs sync | Every captured fact gets a verdict (`KEEP_LOCAL / QUEUE_LOW / QUEUE_HIGH / REDACT_AND_QUEUE / REJECT`) plus a reason a non-expert can read, traceable to a config value. |
| 3 | Sync edge ↔ Qdrant Server | Pull the network, capture facts, restore the network → the **Qdrant Server** collection updates with no manual step, delta-only, and the device learns something it never captured. |
| 4 | Inspection UI | A judge can pick any fact and see its content, sync state, decision reason, and full activity history in two clicks, plus fleet sync status and system activity. |
| 5 | Low-latency hybrid search offline | Reported recall@5 for dense-only vs hybrid (dense + BM25 fused with RRF) on a labeled set, hybrid strictly higher, all offline. |
| 6 | Intermittent connectivity | Switching to `offline` changes nothing about query latency or correctness; `degraded` is a genuinely worse link (injected latency, bandwidth cap, failure rate), not a label. |
| 7 | Evolving memory & conflicts | Local memory has a cap and evicts only synced points; two devices can disagree and converge on reconnect; a retracted fact never reappears anywhere. |
| 8 | Meaningful edge-to-cloud AI workflow | The same question produces a real generated answer offline (local model) and online (cloud model), and the UI shows which path served it. |

## 5. How we treat the data

- **Text facts**: short field reports (e.g. "structural collapse at Zone C stairwell").
  Embedded on-device with a dense text model **and** encoded for BM25 keyword search,
  stored as named vectors in one Qdrant Edge point.
- **Image facts**: photos of hazards, damage, equipment. Embedded with a **cross-modal
  CLIP** model so a text query can retrieve a relevant photo. Stored in the same shard
  as a separate named vector with a `modality: vision` payload field.
- **Payload**: every point carries `value`, `modality`, `model`, `model_version`
  (which pretrained checkpoint produced the vector — we never train our own),
  `corroboration_key` (what real-world fact it is about), `zone`, and `_sync_meta`
  (sync state, priority, sequence). Payload fields we filter or facet on are indexed.
- **Sensitive data** (PII) is detected at capture time and either redacted before it
  can be queued or flagged local-only so it never leaves the device — the privacy half
  of the brief, made observable by surfacing bytes-sent-to-cloud.

## 6. Non-goals

- No production auth / multi-tenant cloud — a single Qdrant Server collection is enough.
- No training of any model — we use pretrained, config-swappable checkpoints only.
- No custom ANN index — we use Qdrant's as-is.
- **No SQL and no second datastore anywhere.** Qdrant is the only store, on device and
  in the cloud. This is deliberate and defensible: there is no second copy of any fact
  and therefore no reconciliation bug between stores.
