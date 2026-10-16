"""Extracted API or orchestration implementation.

The factory receives the existing runtime module: state and test overrides are
shared, not copied into a second shard/adapter/transport registry.
"""

from types import ModuleType
from fastapi import APIRouter


def register(runtime: ModuleType) -> APIRouter:
    router = APIRouter()

    class NetworkModeRequest(runtime.BaseModel):
        mode: str

    runtime.NetworkModeRequest = NetworkModeRequest

    @router.get("/network/mode")
    async def get_network_mode():
        """Current link state + the config-driven degraded parameters, plus the
        measured per-push log (bytes/duration/attempted/accepted/failed/priority)."""
        return {
            "mode": runtime.network.get_mode(),
            "config": runtime.network.config_dict(),
            "push_log": runtime.network.get_push_log(),
        }

    runtime.get_network_mode = get_network_mode

    @router.post("/network/mode")
    async def set_network_mode(request: runtime.NetworkModeRequest):
        """Flip the global link mode. This is the single 'reconnect the fleet' knob;
        it affects sync only — the query/answer path never reads it."""
        try:
            runtime.network.set_mode(request.mode)
        except ValueError as e:
            raise runtime.HTTPException(status_code=400, detail=str(e))
        runtime.log_activity(
            "system", "mode_change", f"network mode changed to {request.mode}"
        )
        return {"mode": runtime.network.get_mode()}

    runtime.set_network_mode = set_network_mode

    return router
