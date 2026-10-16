"""Extracted API or orchestration implementation.

The factory receives the existing runtime module: state and test overrides are
shared, not copied into a second shard/adapter/transport registry.
"""

from types import ModuleType
from fastapi import APIRouter


def register(runtime: ModuleType) -> APIRouter:
    router = APIRouter()

    @router.websocket("/devices/{device_id}/events")
    async def ws_device_events(websocket: runtime.WebSocket, device_id: str):
        """Live stream for Decision Feed and Activity Log (API.md §6)."""
        await websocket.accept()
        with runtime._ws_lock:
            if device_id not in runtime._ws_device_events:
                runtime._ws_device_events[device_id] = set()
            runtime._ws_device_events[device_id].add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except runtime.WebSocketDisconnect:
            pass
        finally:
            with runtime._ws_lock:
                runtime._ws_device_events[device_id].discard(websocket)
                if not runtime._ws_device_events[device_id]:
                    del runtime._ws_device_events[device_id]

    runtime.ws_device_events = ws_device_events

    @router.websocket("/consensus/events")
    async def ws_consensus_events(websocket: runtime.WebSocket):
        """Live resolver decisions for the Conflict Theater (API.md §8)."""
        await websocket.accept()
        with runtime._ws_lock:
            runtime._ws_consensus_events.add(websocket)
        try:
            while True:
                await websocket.receive_text()
        except runtime.WebSocketDisconnect:
            pass
        finally:
            with runtime._ws_lock:
                runtime._ws_consensus_events.discard(websocket)

    runtime.ws_consensus_events = ws_consensus_events

    return router
