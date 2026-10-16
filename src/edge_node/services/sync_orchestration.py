"""Extracted API or orchestration implementation.

The factory receives the existing runtime module: state and test overrides are
shared, not copied into a second shard/adapter/transport registry.
"""

from types import ModuleType


def register(runtime: ModuleType) -> None:
    def _envelope_from_record(rec, adapter_name: str) -> dict:
        """Serialize a shard record into a JSON-safe push envelope.

        Carries the dense + sparse vectors and the full payload (which already
        stamps the model + version — invariant 9), plus the client_sequence used by
        the delta handshake.
        """
        vec = rec.vector or {}
        payload = rec.payload or {}
        model_name = payload.get("model")
        dense = None
        if model_name and model_name in vec:
            dense = vec[model_name]
        elif adapter_name in vec:
            dense = vec[adapter_name]
        else:
            for k, v in vec.items():
                if k != "text_bm25" and v is not None:
                    dense = v
                    break
        sparse = vec.get("text_bm25")
        sparse_obj = None
        if sparse is not None:
            sparse_obj = {
                "indices": list(sparse.indices),
                "values": list(sparse.values),
            }
        payload = {k: v for k, v in payload.items() if k != "_redaction"}
        meta = payload.get("_sync_meta", {})
        return {
            "id": rec.id,
            "vector": list(dense) if dense is not None else None,
            "sparse": sparse_obj,
            "payload": payload,
            "client_sequence": meta.get("client_sequence", 0),
        }

    runtime._envelope_from_record = _envelope_from_record

    def _redact_envelope_in_place(
        env: dict,
    ) -> runtime.List[runtime.Dict[str, runtime.Any]]:
        """Redact PII from an outbound envelope so the hub never sees it (D5 / T7).

        Rebuilds the envelope from the REDACTED text: the `value`, the sparse BM25
        (recomputed from the redacted text — the stored sparse was built from the raw
        value and would otherwise leak PII tokens), and, for a text point, the dense
        vector (re-embedded from the redacted text). An image point's dense vector
        comes from the image, not the caption, so it is left untouched. Returns the
        removed spans (for the device's local record), or [] when there was no PII.
        """
        payload = env.get("payload") or {}
        raw_value = payload.get("value")
        patterns = [
            getattr(p, "pattern", p)
            for p in getattr(runtime.decision_engine, "pii_patterns", [])
        ]
        if not isinstance(raw_value, str) or not patterns:
            return []
        redacted, spans = runtime._redact_text(raw_value, patterns)
        if not spans:
            return []
        new_payload = dict(payload)
        new_payload["value"] = redacted
        new_payload.pop("_redaction", None)
        new_payload["redacted"] = 1
        env["payload"] = new_payload
        if env.get("sparse") is not None:
            sp = runtime.bm25.embed_document(redacted)
            env["sparse"] = {"indices": list(sp.indices), "values": list(sp.values)}
        if payload.get("modality", "text") == "text" and env.get("vector") is not None:
            model_name = payload.get("model")
            adapter = next((a for a in runtime.adapters if a.name == model_name), None)
            if adapter is None:
                adapter = next(
                    (a for a in runtime.adapters if a.modality == "text"),
                    runtime.adapters[0],
                )
            env["vector"] = list(adapter.embed(redacted))
        return spans

    runtime._redact_envelope_in_place = _redact_envelope_in_place

    def _do_push(device_id: str):
        """Drain the outbox to the hub. Returns (pushed_count, errors).

        Shared by the push endpoint and the pull flush-first step. Marks synced ONLY
        on ack (invariant 1): any failure leaves the point pending and on device.
        Queued retractions drain here too, in client_sequence order with observations.
        """
        shards = runtime.get_or_create_shards(device_id)
        mutable_shard = shards["mutable"]
        if runtime.sync_transport is None:
            return (0, ["no sync transport configured"])
        pushed_count = 0
        errors: runtime.List[str] = []
        outbox = runtime.get_outbox(mutable_shard, limit=1000)
        if outbox:
            adapter_name = runtime.adapters[0].name
            envelopes = []
            ids_in_order = []
            for pid, rec in outbox:
                env = runtime._envelope_from_record(rec, adapter_name)
                envelopes.append(env)
                ids_in_order.append(pid)
            redaction_records: runtime.Dict[
                int, runtime.List[runtime.Dict[str, runtime.Any]]
            ] = {}
            for pid, env in zip(ids_in_order, envelopes):
                spans = runtime._redact_envelope_in_place(env)
                if spans:
                    redaction_records[pid] = spans
            if runtime.SYNC_BUDGET_BYTES > 0 and envelopes:
                now_ns = runtime.time.time() * 1000000000
                candidates = []
                for pid, rec, env in zip(
                    [p for p, _ in outbox], [r for _, r in outbox], envelopes
                ):
                    meta = (rec.payload or {}).get("_sync_meta", {})
                    nov = float(meta.get("novelty", 0.0))
                    urg = float(meta.get("urgency", 0.0))
                    ts = (rec.payload or {}).get("client_timestamp_ns")
                    age_s = max(0.0, (now_ns - float(ts)) / 1000000000.0) if ts else 0.0
                    candidates.append(
                        {
                            "id": pid,
                            "env": env,
                            "bytes": len(
                                runtime.json.dumps(env, default=str).encode("utf-8")
                            ),
                            "value": nov + urg,
                            "age_s": age_s,
                            "urgent": meta.get("sync_priority") == "URGENT",
                            "client_sequence": meta.get("client_sequence", 0),
                        }
                    )
                selected, _deferred = runtime.sync_gate.select_batch(
                    candidates,
                    runtime.SYNC_BUDGET_BYTES,
                    aging_factor=runtime.SYNC_AGING_FACTOR,
                )
                envelopes = [c["env"] for c in selected]
                ids_in_order = [c["id"] for c in selected]
            if envelopes:
                try:
                    ack = runtime.sync_transport.push(device_id, envelopes)
                    acked = set(ack.get("acked_ids", []))
                    mark_ids = [pid for pid in ids_in_order if pid in acked]
                    runtime.mark_synced(mutable_shard, mark_ids)
                    for pid in mark_ids:
                        if pid in redaction_records:
                            mutable_shard.update(
                                runtime.UpdateOperation.set_payload_by_filter(
                                    filter=runtime.Filter(
                                        must=[runtime.HasIdCondition(point_ids={pid})]
                                    ),
                                    payload={"spans": redaction_records[pid]},
                                    key="_redaction",
                                )
                            )
                    event_log = runtime.get_or_create_event_log(device_id)
                    now = runtime._utcnow()
                    for pid in mark_ids:
                        event_log.append(pid, runtime.OBSERVED, device_ts=now)
                    pushed_count = len(mark_ids)
                except Exception as e:
                    return (0, [f"push failed: {e}"])
        _, retract_errors = runtime._drain_retractions(device_id)
        errors.extend(retract_errors)
        return (pushed_count, errors)

    runtime._do_push = _do_push

    def _do_pull(device_id: str) -> "PullResponse":
        """Learn from the fleet via a real partial snapshot (Phase 4).

        Mirrors Qdrant's sync guide (backend.md §6.3):
          1. flush the outbox to the hub first, so the snapshot we pull back isn't
             stale (our own latest reports are included);
          2. pull a partial snapshot keyed off the immutable shard's manifest and
             restore it with `update_from_snapshot` — the ONLY way we ever write the
             immutable shard (Phase 4 guardrail);
          3. dedupe the mutable shard: a point that is synced (on the hub) and was
             captured at/before this sync now lives in the immutable shard, so drop
             its mutable copy. Pending points (`synced == 0`) are never touched —
             deleting one would be silent data loss (invariant 1).

        Query still reads both shards and dedupes by id, so a point briefly present
        in both never shows up twice.
        """
        shards = runtime.get_or_create_shards(device_id)
        mutable_shard = shards["mutable"]
        immutable_shard = shards["immutable"]
        if runtime.sync_transport is None:
            return runtime.PullResponse(
                pulled_count=0, errors=["no sync transport configured"]
            )
        _, flush_errors = runtime._do_push(device_id)
        retracted_ids: runtime.List[int] = []
        retract_errors: runtime.List[str] = []
        try:
            retracted_ids = [
                int(x) for x in runtime.sync_transport.get_retracted(device_id)
            ]
        except Exception as e:
            retract_errors.append(f"retracted fetch failed: {e}")
        if retracted_ids:
            runtime.delete_points(mutable_shard, retracted_ids)
            runtime.delete_points(immutable_shard, retracted_ids)
        superseded_ids: runtime.List[int] = []
        if hasattr(runtime.sync_transport, "get_superseded"):
            try:
                superseded_ids = [
                    int(x) for x in runtime.sync_transport.get_superseded(device_id)
                ]
            except Exception as e:
                retract_errors.append(f"superseded fetch failed: {e}")
        if superseded_ids:
            runtime.delete_points(mutable_shard, superseded_ids)
            runtime.delete_points(immutable_shard, superseded_ids)
        flush_errors = flush_errors + retract_errors
        sync_timestamp = int(runtime.time.time() * 1000000000)
        manifest = immutable_shard.snapshot_manifest()
        try:
            snapshot_path = runtime.sync_transport.pull_snapshot(device_id, manifest)
        except Exception as e:
            return runtime.PullResponse(
                pulled_count=0,
                errors=flush_errors + [f"pull failed: {e}"],
                retracted_ids=retracted_ids,
                superseded_ids=superseded_ids,
            )
        if snapshot_path is None:
            return runtime.PullResponse(
                pulled_count=0,
                errors=flush_errors,
                retracted_ids=retracted_ids,
                superseded_ids=superseded_ids,
            )
        incoming_stamp = runtime.edge_snapshot.read_snapshot_stamp(snapshot_path)
        if incoming_stamp:
            problems = runtime.model_stamp.diff_fleet(
                runtime._current_fleet_stamp(),
                incoming_stamp,
                min_cosine=runtime.STAMP_COSINE,
            )
            if problems:
                try:
                    runtime.os.remove(snapshot_path)
                except OSError:
                    pass
                return runtime.PullResponse(
                    pulled_count=0,
                    errors=flush_errors
                    + [f"snapshot stamp mismatch, restore refused: {problems}"],
                    retracted_ids=retracted_ids,
                    superseded_ids=superseded_ids,
                )
        before = immutable_shard.count(runtime.CountRequest())
        try:
            immutable_shard.update_from_snapshot(snapshot_path)
            immutable_shard.optimize()
        except Exception as e:
            return runtime.PullResponse(
                pulled_count=0,
                errors=flush_errors + [f"snapshot restore failed: {e}"],
                retracted_ids=retracted_ids,
                superseded_ids=superseded_ids,
            )
        finally:
            try:
                runtime.os.remove(snapshot_path)
            except OSError:
                pass
        after = immutable_shard.count(runtime.CountRequest())
        pulled_count = runtime._count_value(after) - runtime._count_value(before)
        dedupe_filter = runtime.Filter(
            must=[
                runtime.FieldCondition(
                    key=runtime.SYNCED_KEY, match=runtime.MatchValue(value=1)
                ),
                runtime.FieldCondition(
                    key="client_timestamp_ns",
                    range=runtime.RangeFloat(lte=float(sync_timestamp)),
                ),
            ]
        )
        mutable_shard.update(
            runtime.UpdateOperation.delete_points_by_filter(filter=dedupe_filter)
        )
        mutable_shard.optimize()
        runtime.log_activity(
            device_id, "pull_result", f"pulled {pulled_count} points", point_id=None
        )
        if flush_errors:
            runtime.log_activity(
                device_id, "error", f"pull flush errors: {flush_errors}", point_id=None
            )
        return runtime.PullResponse(
            pulled_count=max(pulled_count, 0),
            errors=flush_errors,
            retracted_ids=retracted_ids,
            superseded_ids=superseded_ids,
        )

    runtime._do_pull = _do_pull

    def _count_value(count_result) -> int:
        """`count()` returns a CountResult with `.count`; be tolerant of a bare int."""
        return getattr(count_result, "count", count_result)

    runtime._count_value = _count_value

    def _drain_retractions(device_id: str):
        """Push pending retraction markers to the hub in client_sequence order.

        Marks a marker synced ONLY on ack (invariant 1): a transport failure leaves
        it pending so a later push (when back online) completes the retraction.
        Returns (drained_count, errors).
        """
        if runtime.sync_transport is None:
            return (0, ["no sync transport configured"])
        shards = runtime.device_shards.get(device_id)
        if not shards:
            return (0, [])
        mutable_shard = shards["mutable"]
        pending = runtime.get_pending_retractions(mutable_shard, limit=1000)
        drained = 0
        errors: runtime.List[str] = []
        for marker in pending:
            info = (marker.payload or {}).get("_retract", {})
            try:
                runtime.sync_transport.retract(
                    device_id=device_id,
                    point_id=info.get("target_point_id"),
                    corroboration_key=info.get("corroboration_key"),
                    client_sequence=info.get("client_sequence", 0),
                )
            except Exception as e:
                errors.append(f"retract push failed: {e}")
                break
            runtime.mark_retraction_synced(mutable_shard, [marker.id])
            runtime.delete_points(mutable_shard, [marker.id])
            drained += 1
        return (drained, errors)

    runtime._drain_retractions = _drain_retractions
