"""Extracted API or orchestration implementation.

The factory receives the existing runtime module: state and test overrides are
shared, not copied into a second shard/adapter/transport registry.
"""

from types import ModuleType
from fastapi import APIRouter


def register(runtime: ModuleType) -> APIRouter:
    router = APIRouter()

    @router.get("/devices/{device_id}/events/{point_id}")
    async def get_events(device_id: str, point_id: int):
        event_log = runtime.get_or_create_event_log(device_id)
        events = event_log.events_for(point_id)
        events.sort(key=lambda e: e["seq"])
        return {"point_id": point_id, "events": events}

    runtime.get_events = get_events

    @router.get("/devices/{device_id}/trust/{point_id}")
    async def get_trust(device_id: str, point_id: int):
        """Return the consensus trust score for a fact (Steps 6 & 7)."""
        event_log = runtime.get_or_create_event_log(device_id)
        events = event_log.events_for(point_id)
        trust = runtime.fold_trust(events, decay=runtime.TRUST_DECAY)
        return {
            "point_id": point_id,
            "trust": trust,
            "lww_trust": runtime.lww_trust(events),
            "decay": runtime.TRUST_DECAY,
            "event_count": len(events),
        }

    runtime.get_trust = get_trust

    @router.get("/devices/{device_id}/conflicts")
    async def list_conflicts(device_id: str):
        """Return POSSIBLE_CONFLICT candidates the exact-key scheme would miss (Step 8)."""
        return {"device_id": device_id, "conflicts": runtime.get_conflicts(device_id)}

    runtime.list_conflicts = list_conflicts

    @router.get("/devices")
    async def list_devices():
        """Fleet state for the Fleet Overview screen (API.md §2)."""
        for d in runtime.DEFAULT_FLEET:
            runtime.get_or_create_shards(d["id"])
        devices = []
        for device_id, shards in runtime.device_shards.items():
            mutable_shard = shards["mutable"]
            immutable_shard = shards["immutable"]
            mutable_count = mutable_shard.count(runtime.CountRequest())
            immutable_count = immutable_shard.count(runtime.CountRequest())
            total_count = getattr(mutable_count, "count", mutable_count) + getattr(
                immutable_count, "count", immutable_count
            )
            from ..outbox import get_outbox

            outbox = get_outbox(mutable_shard, limit=10000)
            pending_by_priority = {"URGENT": 0, "ROUTINE": 0, "HELD": 0}
            for pid, rec in outbox:
                meta = (rec.payload or {}).get("_sync_meta", {})
                prio = meta.get("sync_priority", "ROUTINE")
                if prio in pending_by_priority:
                    pending_by_priority[prio] += 1
            last_sync = None
            for entry in runtime._activity_buffer:
                if entry["device_id"] == device_id and entry["kind"] in (
                    "push_result",
                    "pull_result",
                ):
                    last_sync = entry["timestamp"]
                    break
            dev_meta = runtime.FLEET_METADATA.get(device_id, {})
            name = dev_meta.get("name", device_id)
            devices.append(
                {
                    "id": device_id,
                    "name": name,
                    "kind": dev_meta.get("kind", "edge node"),
                    "zone": dev_meta.get("zone"),
                    "latitude": dev_meta.get("latitude"),
                    "longitude": dev_meta.get("longitude"),
                    "connectivity": runtime.network.get_mode(),
                    "memory": runtime._memory_dict(mutable_shard, total_count),
                    "last_sync_at": last_sync,
                    "trust": runtime._device_trust(device_id),
                    "activity_sparkline": runtime._activity_sparkline(device_id),
                }
            )
        return {"devices": devices}

    runtime.list_devices = list_devices

    @router.get("/devices/{device_id}")
    async def get_device(device_id: str):
        """Single device detail (header of the Device Console) (API.md §2)."""
        if (
            device_id not in runtime.device_shards
            and device_id in runtime.FLEET_METADATA
        ):
            runtime.get_or_create_shards(device_id)
        if device_id not in runtime.device_shards:
            raise runtime.HTTPException(status_code=404, detail="unknown device")
        shards = runtime.device_shards[device_id]
        mutable_shard = shards["mutable"]
        immutable_shard = shards["immutable"]
        mutable_count = mutable_shard.count(runtime.CountRequest())
        immutable_count = immutable_shard.count(runtime.CountRequest())
        total_count = getattr(mutable_count, "count", mutable_count) + getattr(
            immutable_count, "count", immutable_count
        )
        from ..outbox import get_outbox

        outbox = get_outbox(mutable_shard, limit=10000)
        pending_by_priority = {"URGENT": 0, "ROUTINE": 0, "HELD": 0}
        for pid, rec in outbox:
            meta = (rec.payload or {}).get("_sync_meta", {})
            prio = meta.get("sync_priority", "ROUTINE")
            if prio in pending_by_priority:
                pending_by_priority[prio] += 1
        last_sync = None
        for entry in runtime._activity_buffer:
            if entry["device_id"] == device_id and entry["kind"] in (
                "push_result",
                "pull_result",
            ):
                last_sync = entry["timestamp"]
                break
        dev_meta = runtime.FLEET_METADATA.get(device_id, {})
        name = dev_meta.get("name", device_id)
        return {
            "id": device_id,
            "name": name,
            "kind": dev_meta.get("kind", "edge node"),
            "zone": dev_meta.get("zone"),
            "latitude": dev_meta.get("latitude"),
            "longitude": dev_meta.get("longitude"),
            "connectivity": runtime.network.get_mode(),
            "memory": runtime._memory_dict(mutable_shard, total_count),
            "last_sync_at": last_sync,
            "trust": runtime._device_trust(device_id),
            "activity_sparkline": runtime._activity_sparkline(device_id),
        }

    runtime.get_device = get_device

    @router.get("/devices/{device_id}/memory")
    async def list_memory(
        device_id: str,
        q: runtime.Optional[str] = None,
        sync_state: runtime.Optional[str] = None,
        modality: runtime.Optional[str] = None,
        zone: runtime.Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ):
        """List/filter everything held locally (Memory Browser panel) (API.md §5)."""
        if device_id not in runtime.device_shards:
            raise runtime.HTTPException(status_code=404, detail="unknown device")
        shards = runtime.device_shards[device_id]
        mutable_shard = shards["mutable"]
        immutable_shard = shards["immutable"]
        must = []
        if q:
            must.append(
                runtime.FieldCondition(key="value", match=runtime.MatchValue(value=q))
            )
        if sync_state:
            if sync_state == "synced":
                must.append(
                    runtime.FieldCondition(
                        key=runtime.SYNCED_KEY, match=runtime.MatchValue(value=1)
                    )
                )
            elif sync_state == "pending":
                must.append(
                    runtime.FieldCondition(
                        key=runtime.SYNCED_KEY, match=runtime.MatchValue(value=0)
                    )
                )
            elif sync_state == "local_only":
                must.append(
                    runtime.FieldCondition(
                        key="_sync_meta.syncable", match=runtime.MatchValue(value=0)
                    )
                )
        if modality:
            must.append(
                runtime.FieldCondition(
                    key="modality", match=runtime.MatchValue(value=modality)
                )
            )
        if zone:
            must.append(
                runtime.FieldCondition(key="zone", match=runtime.MatchValue(value=zone))
            )
        f = runtime.Filter(must=must) if must else None
        from qdrant_edge import ScrollRequest

        points = []
        total = 0
        for shard in (mutable_shard, immutable_shard):
            req = ScrollRequest(
                limit=limit + offset, filter=f, with_payload=True, with_vector=False
            )
            res = shard.scroll(req)
            records = res[0] if isinstance(res, tuple) else res
            for r in records:
                payload = r.payload or {}
                meta = payload.get("_sync_meta", {})
                sync_state_val = (
                    "synced"
                    if meta.get("synced") == 1
                    else "local_only"
                    if meta.get("syncable") == 0
                    else "pending"
                )
                points.append(
                    {
                        "id": str(r.id),
                        "value": payload.get("value", ""),
                        "modality": payload.get("modality", "text"),
                        "thumbnail_url": payload.get("thumbnail_url"),
                        "zone": payload.get("zone"),
                        "corroboration_key": payload.get("corroboration_key"),
                        "sync_state": sync_state_val,
                        "model": payload.get("model", "text_dense"),
                        "model_version": payload.get(
                            "model_version", "bge-small-en-v1.5"
                        ),
                        "created_at": payload.get("client_timestamp_ns", 0),
                    }
                )
            total += len(records)
        seen = {}
        for p in points:
            if p["id"] not in seen:
                seen[p["id"]] = p
        deduped = list(seen.values())
        deduped.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return {"points": deduped[offset : offset + limit], "total": len(deduped)}

    runtime.list_memory = list_memory

    @router.get("/devices/{device_id}/memory/{point_id}")
    async def get_memory_point(device_id: str, point_id: str):
        if device_id not in runtime.device_shards:
            raise runtime.HTTPException(status_code=404, detail="unknown device")
        shards = runtime.device_shards[device_id]
        mutable_shard = shards["mutable"]
        immutable_shard = shards["immutable"]
        target_ids = []
        try:
            target_ids.append(int(point_id))
        except ValueError:
            pass
        for shard in (mutable_shard, immutable_shard):
            if target_ids:
                recs = shard.retrieve(target_ids, with_payload=True, with_vector=False)
                if recs:
                    rec = recs[0]
                    payload = rec.payload or {}
                    meta = payload.get("_sync_meta", {})
                    sync_state_val = (
                        "synced"
                        if meta.get("synced") == 1
                        else "local_only"
                        if meta.get("syncable") == 0
                        else "pending"
                    )
                    return {
                        "point": {
                            "id": str(rec.id),
                            "value": payload.get("value", ""),
                            "modality": payload.get("modality", "text"),
                            "thumbnail_url": payload.get("thumbnail_url"),
                            "zone": payload.get("zone"),
                            "corroboration_key": payload.get("corroboration_key"),
                            "sync_state": sync_state_val,
                            "model": payload.get("model", "text_dense"),
                            "model_version": payload.get(
                                "model_version", "bge-small-en-v1.5"
                            ),
                            "created_at": payload.get("client_timestamp_ns", 0),
                        },
                        "decision": None,
                        "activity": [],
                        "consensus": None,
                    }
            from qdrant_edge import ScrollRequest

            pts, _ = shard.scroll(
                ScrollRequest(limit=200, with_payload=True, with_vector=False)
            )
            for rec in pts or []:
                if str(rec.id)[:12] == point_id[:12]:
                    payload = rec.payload or {}
                    meta = payload.get("_sync_meta", {})
                    sync_state_val = (
                        "synced"
                        if meta.get("synced") == 1
                        else "local_only"
                        if meta.get("syncable") == 0
                        else "pending"
                    )
                    return {
                        "point": {
                            "id": str(rec.id),
                            "value": payload.get("value", ""),
                            "modality": payload.get("modality", "text"),
                            "thumbnail_url": payload.get("thumbnail_url"),
                            "zone": payload.get("zone"),
                            "corroboration_key": payload.get("corroboration_key"),
                            "sync_state": sync_state_val,
                            "model": payload.get("model", "text_dense"),
                            "model_version": payload.get(
                                "model_version", "bge-small-en-v1.5"
                            ),
                            "created_at": payload.get("client_timestamp_ns", 0),
                        },
                        "decision": None,
                        "activity": [],
                        "consensus": None,
                    }
        raise runtime.HTTPException(status_code=404, detail="point not found")

    runtime.get_memory_point = get_memory_point

    @router.get("/devices/{device_id}/sync")
    async def get_sync_status(device_id: str):
        """Sync-status strip data (API.md §7)."""
        if device_id not in runtime.device_shards:
            raise runtime.HTTPException(status_code=404, detail="unknown device")
        shards = runtime.device_shards[device_id]
        mutable_shard = shards["mutable"]
        from ..outbox import get_outbox

        outbox = get_outbox(mutable_shard, limit=10000)
        pending = {"URGENT": 0, "ROUTINE": 0, "HELD": 0}
        for pid, rec in outbox:
            meta = (rec.payload or {}).get("_sync_meta", {})
            prio = meta.get("sync_priority", "ROUTINE")
            if prio in pending:
                pending[prio] += 1
        last_attempt = None
        last_success = None
        consecutive_failures = 0
        last_pull = {}
        for entry in runtime._activity_buffer:
            if entry["device_id"] == device_id:
                if entry["kind"] == "push_attempt":
                    last_attempt = entry["timestamp"]
                elif entry["kind"] == "push_result":
                    if (
                        "failed" in entry["detail"].lower()
                        or "error" in entry["detail"].lower()
                    ):
                        consecutive_failures += 1
                    else:
                        consecutive_failures = 0
                        last_success = entry["timestamp"]
                elif entry["kind"] == "pull_result" and (not last_pull):
                    received = 0
                    m = runtime.re.search("pulled\\s+(\\d+)", entry["detail"])
                    if m:
                        received = int(m.group(1))
                    last_pull = {"at": entry["timestamp"], "points_received": received}
        last_push = runtime._last_push_summary(device_id)
        return {
            "pending": pending,
            "last_attempt_at": last_attempt,
            "last_success_at": last_success,
            "consecutive_failures": consecutive_failures,
            "next_backoff_ms": runtime._next_backoff_ms(consecutive_failures),
            "last_push": last_push,
            "last_pull": last_pull,
        }

    runtime.get_sync_status = get_sync_status

    @router.get("/devices/{device_id}/activity")
    async def get_device_activity(
        device_id: str, kind: runtime.Optional[str] = None, limit: int = 100
    ):
        """System-activity ring buffer, newest first (API.md §6)."""
        entries = runtime.get_activity(device_id, kind, limit)
        return {"entries": entries}

    runtime.get_device_activity = get_device_activity

    @router.get("/devices/{device_id}/telemetry")
    async def get_device_telemetry(device_id: str):
        """Live CPU/RAM/latency from the real cgroup/process (API.md §11)."""
        if device_id not in runtime.device_shards:
            raise runtime.HTTPException(status_code=404, detail="unknown device")
        return runtime.get_telemetry(model_load_ms=runtime.MODEL_LOAD_MS)

    runtime.get_device_telemetry = get_device_telemetry

    @router.get("/cloud/state")
    async def get_cloud_state(
        state: runtime.Optional[str] = None,
        zone: runtime.Optional[str] = None,
        modality: runtime.Optional[str] = None,
    ):
        """Merged trusted picture from the fold (API.md §8)."""
        if runtime.sync_transport is not None:
            try:
                cloud_res = runtime.sync_transport.get_cloud_state()
                if cloud_res and isinstance(cloud_res.get("facts"), list):
                    dt = cloud_res.get("device_trust")
                    if isinstance(dt, dict):
                        runtime._last_hub_device_trust.update(
                            {
                                k: float(v)
                                for k, v in dt.items()
                                if isinstance(v, (int, float))
                            }
                        )
                    if modality:
                        cloud_res["facts"] = [
                            f
                            for f in cloud_res["facts"]
                            if modality == "vision"
                            and (
                                f.get("modality") == "vision" or f.get("thumbnail_url")
                            )
                            or (modality != "vision" and f.get("modality") == modality)
                        ]
                    return cloud_res
            except Exception:
                pass
        facts_by_key: runtime.Dict[
            str, runtime.List[runtime.Tuple[str, str, int, runtime.Optional[str], str]]
        ] = {}
        for dev_id, shards in runtime.device_shards.items():
            for shard in (shards["mutable"], shards["immutable"]):
                from qdrant_edge import ScrollRequest

                try:
                    pts, _ = shard.scroll(
                        ScrollRequest(limit=1000, with_payload=True, with_vector=False)
                    )
                    for p in pts or []:
                        pl = p.payload or {}
                        k = pl.get("corroboration_key")
                        if not k:
                            continue
                        v = pl.get("value", "")
                        facts_by_key.setdefault(k, []).append(
                            (
                                dev_id,
                                str(v),
                                pl.get("client_timestamp_ns", 0),
                                pl.get("thumbnail_url"),
                                pl.get("modality", "text"),
                            )
                        )
                except Exception:
                    pass
        from datetime import datetime, timezone

        facts = []
        for k, entries in sorted(facts_by_key.items()):
            devices_by_val: runtime.Dict[str, set] = {}
            for dev_id, v, ts, t_url, m_val in entries:
                devices_by_val.setdefault(v, set()).add(dev_id)
            best_val = max(devices_by_val, key=lambda v: len(devices_by_val[v]))
            latest_ts = max((e[2] for e in entries), default=0)
            if latest_ts > 0:
                upd = datetime.fromtimestamp(
                    latest_ts / 1000000000.0, tz=timezone.utc
                ).isoformat()
            else:
                upd = datetime.now(timezone.utc).isoformat()
            thumb = next((e[3] for e in entries if e[3]), None)
            fact_mod = "vision" if any((e[4] == "vision" for e in entries)) else "text"
            if modality:
                if modality == "vision" and fact_mod != "vision" and (not thumb):
                    continue
                elif modality != "vision" and fact_mod != modality:
                    continue
            facts.append(
                {
                    "corroboration_key": k,
                    "state": "UNCORROBORATED",
                    "value": best_val,
                    "confidence": None,
                    "corroborating_devices": sorted(devices_by_val[best_val]),
                    "updated_at": upd,
                    "thumbnail_url": thumb,
                    "modality": fact_mod,
                }
            )
        return {"facts": facts, "device_trust": {}, "degraded": True}

    runtime.get_cloud_state = get_cloud_state

    @router.get("/devices/{device_id}/trend")
    async def get_device_trend(device_id: str, key: runtime.Optional[str] = None):
        """Change-over-time per camera: consecutive frames, cosine drift, EVOLVING/STABLE (API.md / Phase C3)."""
        if device_id not in runtime.device_shards:
            raise runtime.HTTPException(status_code=404, detail="unknown device")
        shards = runtime.device_shards[device_id]
        from qdrant_edge import ScrollRequest, Filter, FieldCondition, MatchValue

        f = (
            Filter(
                must=[
                    FieldCondition(key="corroboration_key", match=MatchValue(value=key))
                ]
            )
            if key
            else None
        )
        records = []
        for shard in (shards["mutable"], shards["immutable"]):
            try:
                res = shard.scroll(
                    ScrollRequest(
                        limit=50, filter=f, with_payload=True, with_vector=True
                    )
                )
                pts = res[0] if isinstance(res, tuple) else res
                for p in pts:
                    pl = p.payload or {}
                    if pl.get("modality") == "vision" or pl.get("thumbnail_url"):
                        records.append(p)
            except Exception:
                pass
        records.sort(key=lambda p: (p.payload or {}).get("client_timestamp_ns", 0))
        frames = []
        prev_vec = None
        latest_drift = 0.0
        for p in records:
            pl = p.payload or {}
            vec = None
            if p.vector:
                if isinstance(p.vector, dict):
                    vec = p.vector.get("image") or p.vector.get("dense")
                elif isinstance(p.vector, list):
                    vec = p.vector
            drift = 0.0
            if prev_vec is not None and vec is not None and (len(prev_vec) == len(vec)):
                sim = sum((a * b for a, b in zip(prev_vec, vec)))
                drift = round(max(0.0, 1.0 - sim), 4)
                latest_drift = drift
            if vec is not None:
                prev_vec = vec
            ts_ns = pl.get("client_timestamp_ns", 0)
            from datetime import datetime, timezone

            ts_str = (
                datetime.fromtimestamp(
                    ts_ns / 1000000000.0, tz=timezone.utc
                ).isoformat()
                if ts_ns
                else runtime._utcnow()
            )
            frames.append(
                {
                    "point_id": p.id,
                    "thumbnail_url": pl.get("thumbnail_url")
                    or f"/thumbnails/thumb_{p.id}.jpg",
                    "timestamp": ts_str,
                    "drift_from_prev": drift,
                    "caption": pl.get("value", ""),
                }
            )
        status = "EVOLVING" if latest_drift >= 0.15 else "STABLE"
        return {
            "device_id": device_id,
            "corroboration_key": key or "all",
            "status": status,
            "latest_drift": latest_drift,
            "frames": frames[-6:],
        }

    runtime.get_device_trend = get_device_trend

    return router
