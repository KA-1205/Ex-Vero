# Decision memo (T11 / D12): hub `seq` + `client_sequence` ordering vs. a Hybrid Logical Clock

Status: **proposal / analysis only — no code change without team approval.**
Scope: ordering of `fact_events` for the consensus fold. Everything under
"Current behaviour" is read from the committed code; everything under "With an
HLC" is a design proposal, not a description of existing behaviour.

## 1. What the code does today (read from source)

Two counters order the append-only `fact_events` log; the fold reads nothing
else for ordering.

- **Hub `seq`** — a single monotonic integer assigned by the gateway at ingest
  (`cloud-gateway/app.py` `_next_seq`, resumed on restart from the
  `gateway_meta` high-water point). The gateway is the fleet's single
  sequencer. This is the **across-device** order.
- **`client_sequence`** — a persistent per-device monotonic counter carried in
  each envelope. This is the **within-device** order.
- **Ordering key** (`cloud-gateway/consensus_fold.py` `_event_order_key`):
  `(client_sequence, seq)`. `client_sequence` is primary; hub `seq` is the
  tie-break and the fallback for legacy events with no `client_sequence`.
- `_latest_by_device` takes the max of that key per device. A device whose
  latest event is `RETRACTED` has withdrawn its vote (terminal until a newer
  `OBSERVED`).
- **Wall clocks are never used for ordering.** `client_timestamp_ns` is
  metadata only; `received_at_ns` (the hub clock, stamped at ingest) feeds the
  time-based staleness decay (B4) but not the order. So a skewed device clock
  cannot change a verdict (fold invariant 5).

Event payload fields relevant here (from `app.py` ingest):
`seq`, `client_sequence`, `client_timestamp_ns`, `received_at_ns`,
`device_id`, `event_type`, `value`, `value_cluster`.

Key property this buys us: the fold is a pure function of the event set, so the
same events in any insertion order produce byte-identical output
(`test_fold_is_pure_shuffle_order_identical`). An HLC proposal must preserve
that property.

## 2. The three cases

Notation: each event is `{device, type, client_sequence (cs), seq, value}`.
`seq` is assigned in hub-arrival order.

### Case A — delayed OBSERVE arriving after a RETRACT (same device)

A device observes, then retracts; a stale **re-send of the old observe** (same
`client_sequence`) is delayed in the network and lands after the retract.

```
dev-01  OBSERVED   cs=5  seq=100  value="valve open"
dev-01  RETRACTED  cs=6  seq=101  value=None
dev-01  OBSERVED   cs=5  seq=140   value="valve open"   # delayed duplicate of cs=5
```

- **Current:** the duplicate `cs=5` is idempotent — its event id is
  `md5(device:point:client_sequence)`, so insert-if-absent keeps the original
  `seq=100` and does not re-order anything. The device's latest key is
  `(6,101)` (the retract), so the vote stays withdrawn. **Correct, no ghost.**
  (`test_delayed_older_observe_after_retract_stays_retracted`,
  `test_observe_retract_reobserve_stays_absent_until_newer_observe`.)
- **With an HLC:** each event carries `hlc=(physical, logical)`. The retract's
  HLC strictly succeeds the observe's (same device, monotonic). The delayed
  duplicate carries the *original* observe HLC, which precedes the retract, so
  it is still ordered before the retract. Same outcome. **No improvement here —
  `client_sequence` already encodes the needed per-device causal order.**

### Case B — long-offline reconnect (the real weakness)

`dev-03` is offline for a day, captures facts locally, then reconnects and
pushes its backlog. The hub assigns today's `seq` values and stamps today's
`received_at_ns`.

```
# captured yesterday on dev-03 while offline (cs are old-but-local)
dev-01  OBSERVED  cs=12  seq=200  received_at=T0      value="pressure 5 bar"
dev-02  OBSERVED  cs=9   seq=201  received_at=T0+1m   value="pressure 5 bar"
# dev-03 reconnects today and dumps a stale capture:
dev-03  OBSERVED  cs=3   seq=900  received_at=T0+1day value="pressure 50 bar"
```

- **Current:** across devices, recency uses hub `seq`, and staleness decay uses
  `received_at_ns`. Both say dev-03's event is the *newest/freshest* because it
  **arrived** last, even though it was **captured** a day earlier than the
  others. A day-stale reading therefore gets full (or boosted) recency weight
  and can wrongly tip a `CONFIRMED`/`DISPUTED` boundary. The B5 contradiction
  guard still keeps "5 bar" and "50 bar" in different clusters, so it surfaces
  as `DISPUTED` rather than silently flipping — but the stale reading is
  weighted as if fresh, which is the defect.
- **With an HLC:** the event carries the device's HLC at **capture**, so its
  physical component is yesterday's time (bounded by, not equal to, the device
  wall clock). Staleness could decay by HLC-physical age instead of
  `received_at_ns`, so a day-old capture is correctly treated as a day old no
  matter when it is uploaded. This is the one case where an HLC is materially
  better than the status quo.

### Case C — clock skew

`dev-04`'s wall clock is 3 hours fast.

```
dev-01  OBSERVED  cs=20  seq=300  client_ts=12:00  value="exit B clear"
dev-04  OBSERVED  cs=7   seq=301  client_ts=15:00  value="exit B blocked"
```

- **Current:** `client_timestamp_ns` is metadata only; ordering is `seq` /
  `client_sequence`. The skew changes nothing (`test_fold_ignores_device_clock_skew`).
  **Already correct and skew-proof.**
- **With an HLC:** the HLC's physical component is updated as
  `max(local_physical, last_seen_physical) + ε` and the logical component
  breaks ties, so a fast clock is tolerated but cannot run away. Equivalent
  correctness; an HLC does **not** beat the current scheme here, it only
  matches it — and only if the gateway (which already assigns a monotonic
  `seq`) remains the authority that bounds physical drift.

## 3. What adopting an HLC would cost

Schema (`fact_events` payload + envelope):
- add `hlc_physical_ns` and `hlc_logical` per event (two integers); keep `seq`
  for the stable across-device tie-break and the restart high-water mechanism.
- the device must persist its last HLC (like it persists `client_sequence`
  today) and advance it on every capture and on every hub exchange.

Fold (`consensus_fold.py`):
- `_event_order_key` would become `(hlc_physical_ns, hlc_logical, seq)` or keep
  `client_sequence` within-device and use HLC only for the staleness clock.
- staleness decay would switch from `received_at_ns` to the HLC physical age.
  **The purity invariant is preserved** (still a function of the event set; the
  caller still injects `now`), so the shuffle-order determinism test continues
  to hold.

Risk / effort:
- device-side persistence and backfill of HLC for already-stored points;
- a migration path for legacy events that have no HLC (fall back to
  `received_at_ns`, exactly as `_event_order_key` already falls back to `seq`
  when `client_sequence` is absent);
- more moving parts in the one module we most want to keep simple and pure.

## 4. Recommendation

**Do not adopt a full HLC now.** Two of the three cases (A delayed-after-retract,
C clock-skew) are already handled correctly by `client_sequence` + hub `seq`,
and an HLC adds schema and device-state complexity for no behavioural gain
there.

Case B (long-offline reconnect weighting a stale capture as fresh) is a genuine
defect, but it is **narrower than "we need an HLC."** The minimal fix is to
decay staleness by **capture age, not arrival age**: carry a trusted
`captured_at_ns` (bounded by the gateway the way `seq` already is) and feed that
to the B4 half-life instead of `received_at_ns`. That is a one-field, one-line
change to the staleness clock, keeps the fold pure, and leaves ordering
untouched.

Revisit a full HLC only if we drop the single-sequencer gateway (multi-region
hubs with no global `seq`), because that is the scenario where a logical clock
earns its complexity. Until then, `seq` + `client_sequence` + a capture-time
staleness clock is the cheaper, equally-correct choice.

*No code is changed by this memo. The capture-time-staleness fix above is a
proposal for team sign-off, not an implemented change.*
