"""Extracted API or orchestration implementation.

The factory receives the existing runtime module: state and test overrides are
shared, not copied into a second shard/adapter/transport registry.
"""

from types import ModuleType
from fastapi import APIRouter


def register(runtime: ModuleType) -> APIRouter:
    router = APIRouter()

    class RogueRequest(runtime.BaseModel):
        rogue: bool = True

    runtime.RogueRequest = RogueRequest

    @router.post("/devices/{device_id}/rogue")
    async def set_device_rogue(
        device_id: str,
        request: runtime.Request,
        req: runtime.RogueRequest = runtime.RogueRequest(),
    ):
        """Flag or unflag a device as rogue/compromised (demo beat).

        Gated (A5 / P0-5): 404 unless ENABLE_DEMO_ENDPOINTS is set, and 401 without
        the configured admin token. A production node is not flaggable by default.
        """
        runtime._require_demo(request)
        demo_dev = runtime._demo_device_id(device_id)
        if req.rogue:
            runtime._rogue_devices.add(demo_dev)
        else:
            runtime._rogue_devices.discard(demo_dev)
        activity = runtime.log_activity(
            demo_dev, "mode_change", f"device rogue mode set to {req.rogue}", None
        )
        runtime._broadcast_device_event(demo_dev, "activity", activity)
        return {"id": demo_dev, "rogue": req.rogue}

    runtime.set_device_rogue = set_device_rogue

    class InjectConflictRequest(runtime.BaseModel):
        corroboration_key: str
        assignments: runtime.Dict[str, str]

    runtime.InjectConflictRequest = InjectConflictRequest

    @router.post("/demo/inject-conflict")
    async def inject_conflict(
        request: runtime.Request, req: runtime.InjectConflictRequest
    ):
        """Inject conflicting observations across devices for Conflict Theater.

        Gated (A5 / P0-5) and confined to the reserved `demo-` namespaces: every
        device id is prefixed and every key namespaced, ids are deterministic md5
        (never Python's randomized `hash()`), and each vector is a REAL embedding of
        the supplied value (never a constant fill). Demo writes therefore cannot
        alter a real device's shards or its `/delta`.
        """
        runtime._require_demo(request)
        now = runtime._utcnow()
        demo_key = runtime._demo_key(req.corroboration_key)
        candidates = []
        adapter = next(
            (a for a in runtime.adapters if a.modality == "text"), runtime.adapters[0]
        )
        if runtime.sync_transport:
            try:
                runtime.sync_transport.inject_conflict(
                    {"corroboration_key": demo_key, "assignments": req.assignments}
                )
            except Exception as exc:
                print(f"demo inject forward failed: {exc}")
        for dev_id, val in req.assignments.items():
            demo_dev = runtime._demo_device_id(dev_id)
            shards = runtime.get_or_create_shards(demo_dev)
            elog = runtime.get_or_create_event_log(demo_dev)
            pid = runtime.generate_point_id(demo_key, f"{demo_dev}:{val}")
            d_vec = adapter.embed(val)
            s_vec = runtime.bm25.embed_document(val)
            pl = {
                "value": val,
                "corroboration_key": demo_key,
                "modality": "text",
                "thumbnail_url": None,
                "client_timestamp_ns": int(runtime.time.time() * 1000000000),
                "_demo": 1,
                "_sync_meta": {
                    "synced": 1,
                    "syncable": 1,
                    "verdict": "QUEUE_HIGH",
                    "sync_priority": "URGENT",
                    "client_sequence": runtime._next_demo_client_seq(),
                },
            }
            pt = runtime.Point(
                id=pid, vector={adapter.name: d_vec, "text_bm25": s_vec}, payload=pl
            )
            shards["mutable"].update(runtime.UpdateOperation.upsert_points(points=[pt]))
            elog.append(pid, runtime.OBSERVED, device_ts=now)
            weight = 0.42 if demo_dev in runtime._rogue_devices else 0.88
            candidates.append({"value": val, "devices": [demo_dev], "weight": weight})
        unique_vals = set(req.assignments.values())
        outcome_state = "CONFIRMED" if len(unique_vals) <= 1 else "DISPUTED"
        conf = 0.95 if outcome_state == "CONFIRMED" else 0.52
        res_val = (
            list(req.assignments.values())[0] if outcome_state == "CONFIRMED" else None
        )
        consensus_frame = {
            "corroboration_key": demo_key,
            "state": outcome_state,
            "confidence": conf,
            "resolved_value": res_val,
            "candidates": candidates,
            "explanation": f"Injected conflict across {len(req.assignments)} devices for {demo_key}",
            "timestamp": now,
        }
        runtime._broadcast_consensus_event(consensus_frame)
        return {
            "injected": True,
            "corroboration_key": demo_key,
            "consensus": consensus_frame,
        }

    runtime.inject_conflict = inject_conflict

    def _delete_all_points(shard) -> int:
        """Delete every point in a shard (used by demo reset on confined `demo-*`
        shards only)."""
        from qdrant_edge import ScrollRequest

        res = shard.scroll(
            ScrollRequest(limit=100000, with_payload=False, with_vector=False)
        )
        records = res[0] if isinstance(res, tuple) else res
        ids = [r.id for r in records]
        if ids:
            runtime.delete_points(shard, ids)
        return len(ids)

    runtime._delete_all_points = _delete_all_points

    @router.post("/admin/reset-demo")
    async def reset_demo(request: runtime.Request):
        """Delete ONLY `demo-*` data (A5 / P0-5): drop every demo device's shard
        points and event log, leaving every real device untouched."""
        runtime._require_demo(request)
        demo_ids = [
            d
            for d in list(runtime.device_shards)
            if d.startswith(runtime.DEMO_DEVICE_PREFIX)
        ]
        deleted = 0
        for dev_id in demo_ids:
            shards = runtime.device_shards.pop(dev_id, None)
            if shards:
                deleted += runtime._delete_all_points(shards["mutable"])
                deleted += runtime._delete_all_points(shards["immutable"])
            elog = runtime.event_logs.pop(dev_id, None)
            if elog is not None:
                deleted += runtime._delete_all_points(elog.shard)
        runtime._rogue_devices.difference_update(demo_ids)
        return {"reset": True, "demo_devices": demo_ids, "deleted_points": deleted}

    runtime.reset_demo = reset_demo

    return router
