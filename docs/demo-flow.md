# Ex-Vero — Demo Walkthrough & Evaluation Guide

This guide details the end-to-end evaluation flow for Ex-Vero's edge-native intelligence platform and dashboard.

---

## 1. Fleet Overview Dashboard (`/numeric/overview`)

The initial landing view provides high-level situational awareness across all field-deployed nodes.

### What to observe:
- **Fleet KPIs**: Total connected devices, offline/degraded node counters, and verified sync indicators. Note that sync success rate only surfaces verified measurements rather than unverified static claims.
- **Live Fleet Map**: Visual coordinates of active edge devices across geographical zones.
- **Hardware Telemetry**: Real-time cgroup statistics (CPU load %, memory usage MB, query latency p50/p95 ms) reflecting physical container limits rather than host system metrics.
- **Field Conditions**: Integrated live weather observations (temperature, humidity, wind speed, visibility) for operational context.
- **Recent System Activity**: Real-time stream of capture, push, sync, and deduplication events. Duplicate frames are filtered locally and discarded before transmission.

---

## 2. Edge Device Console (`/numeric/overview/devices/:deviceId`)

Clicking any node card (e.g., `dev-01`, `dev-03`) opens its dedicated on-device console.

### Key capabilities to test:
- **Q&A Panel**: Ask domain-specific emergency queries (e.g. *"What is the status of Zone C?"*). The node queries its local on-disk Qdrant Edge shard using hybrid dense + sparse retrieval, synthesizing extractive answers strictly from locally stored evidence.
- **Decision Feed**: Inspect the explainable triage gate. Every observation is categorized into verdicts (`QUEUE_HIGH`, `QUEUE_LOW`, `KEEP_LOCAL`, `REJECT`) with clear justification (e.g., novelty score, PII sanitization, urgency).
- **Memory Browser**: Explore stored points on the device shard, filtering by modality (text/vision), sync state (`synced`, `pending`, `local_only`), and zone.
- **Capture Panel**: Simulate capturing new field observations with corroboration keys and priority levels.

---

## 3. Conflict Theater (`/numeric/overview/conflict-theater`)

The Conflict Theater showcases Ex-Vero's core distributed consensus algorithm under network partition.

### Step-by-step walkthrough:
1. **Network Partition (Offline)**:
   - Click `RESET (GO OFFLINE)` to simulate edge disconnection.
   - Click `INJECT CONFLICT` to post contradictory sensor observations from two offline nodes (e.g., `dev-01` reports active hazard; `dev-03` reports contained).
2. **Observe Disagreement**:
   - Both nodes maintain conflicting local truths with distinct corroboration keys.
3. **Reconnect & Consensus Fold**:
   - Click `RECONNECT` to restore connectivity to the cloud sequencer.
   - The cloud gateway computes a trust-weighted Bayesian consensus fold over the conflicting claims.
   - Observe the **Consensus Fold Result**:
     - Visual confidence meter and percentage display.
     - Outcome badge (`CONFIRMED`, `DISPUTED`, or `LWW`).
     - Breakdown of each device's claim, trust weight score, and report timestamp.
     - Human-readable resolution summary.
4. **Rogue Node Mitigation**:
   - Toggle `ROGUE DEVICE` on `dev-03` to observe how trust degradation prevents compromised or faulty nodes from poisoning the consensus outcome.

---

## 4. Cloud Command Dashboard (`/numeric/overview/command`)

The command dashboard presents the global merged truth synthesized from all edge pushes.

### Key elements:
- **Trusted Facts vs Disputed Facts**: Aggregated state across zones, showing which facts achieved multi-node corroboration.
- **Benchmark Comparisons**:
  - **Resolver Accuracy vs LWW Baseline**: Quantifies accuracy gains of trust-weighted consensus over naive Last-Write-Wins timestamps.
  - **Hybrid R@5 vs Dense-Only Recall**: Demonstrates retrieval quality improvements on emergency nomenclature.
- **Fact Retraction**: Allows authorized operators to initiate verifiable retractions across the fleet, propagating tombstone events back to edge shards.
