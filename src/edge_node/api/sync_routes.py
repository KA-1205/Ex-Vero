"""Extracted API or orchestration implementation.

The factory receives the existing runtime module: state and test overrides are
shared, not copied into a second shard/adapter/transport registry.
"""

from types import ModuleType
from fastapi import APIRouter


def register(runtime: ModuleType) -> APIRouter:
    router = APIRouter()

    class PushResponse(runtime.BaseModel):
        pushed_count: int
        errors: runtime.List[str] = []

    runtime.PushResponse = PushResponse

    @router.post("/devices/{device_id}/push")
    async def push(device_id: str):
        pushed_count, errors = await runtime.asyncio.to_thread(
            runtime._do_push, device_id
        )
        if pushed_count > 0:
            runtime.log_activity(
                device_id, "push_result", f"pushed {pushed_count} points", point_id=None
            )
        if errors:
            runtime.log_activity(
                device_id, "error", f"push errors: {errors}", point_id=None
            )
        return runtime.PushResponse(pushed_count=pushed_count, errors=errors)

    runtime.push = push

    class PullResponse(runtime.BaseModel):
        pulled_count: int
        errors: runtime.List[str] = []
        retracted_ids: runtime.List[int] = []
        superseded_ids: runtime.List[int] = []

    runtime.PullResponse = PullResponse

    @router.post("/devices/{device_id}/pull")
    async def pull(device_id: str):
        return await runtime.asyncio.to_thread(runtime._do_pull, device_id)

    runtime.pull = pull

    class RetractResponse(runtime.BaseModel):
        retracted: bool
        point_id: int

    runtime.RetractResponse = RetractResponse

    @router.post("/devices/{device_id}/retract/{point_id}")
    async def retract(device_id: str, point_id: int):
        """Withdraw a fact end-to-end (P0-2).

        Resolves the point's corroboration_key from the stored observation (404 if
        the device never held it), deletes it from BOTH local shards, queues a
        persistent retraction in the outbox (its own client_sequence, pushed in order
        with observations), and attempts to notify the hub immediately. An offline /
        failed notify leaves the retraction queued — never a silent `except: pass`.
        """
        if device_id not in runtime.device_shards:
            raise runtime.HTTPException(status_code=404, detail="unknown device")
        shards = runtime.device_shards[device_id]
        mutable_shard = shards["mutable"]
        immutable_shard = shards["immutable"]
        corroboration_key = None
        found = False
        for shard in (mutable_shard, immutable_shard):
            recs = shard.retrieve([point_id], with_payload=True, with_vector=False)
            if recs and recs[0].payload is not None:
                found = True
                corroboration_key = recs[0].payload.get("corroboration_key")
                if corroboration_key:
                    break
        if not found or not corroboration_key:
            raise runtime.HTTPException(
                status_code=404, detail="no local observation for this point"
            )
        now = runtime._utcnow()
        event_log = runtime.get_or_create_event_log(device_id)
        event_log.append(point_id, runtime.RETRACTED, device_ts=now)
        runtime.log_activity(
            device_id, "retraction", f"retracted point {point_id}", point_id
        )
        runtime.delete_points(mutable_shard, [point_id])
        runtime.delete_points(immutable_shard, [point_id])
        shards["client_seq"] += 1
        rseq = shards["client_seq"]
        runtime.queue_retraction(mutable_shard, point_id, corroboration_key, rseq)
        _, retract_errors = runtime._drain_retractions(device_id)
        if retract_errors:
            runtime.log_activity(
                device_id, "error", f"retract push deferred: {retract_errors}", point_id
            )
        runtime._broadcast_consensus_event(
            {
                "corroboration_key": corroboration_key,
                "state": "RETRACTED",
                "confidence": 0.0,
                "resolved_value": None,
                "candidates": [],
                "explanation": f"Fact retracted by {device_id}",
                "timestamp": now,
            }
        )
        return runtime.RetractResponse(retracted=True, point_id=point_id)

    runtime.retract = retract

    return router
