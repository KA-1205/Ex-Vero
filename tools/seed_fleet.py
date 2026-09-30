"""Phase 1 — Device provisioning for the Aegis Edge fleet.

Ensures that the demo fleet (cam-01, cam-02, cam-03, dev-01, dev-02, dev-03, dev-04)
is initialized with persistent local storage, seed metadata, and initial observations
matching the frontend mock fleet structure.
"""

import sys
import os
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "src"))

FLEET_DEVICES = [
    {"id": "cam-01", "name": "Zone A Vision 01", "kind": "vision_camera", "zone": "Zone A"},
    {"id": "cam-02", "name": "Zone B Vision 02", "kind": "vision_camera", "zone": "Zone B"},
    {"id": "cam-03", "name": "Zone C Vision 03", "kind": "vision_camera", "zone": "Zone C"},
    {"id": "dev-01", "name": "Patrol Node Alpha", "kind": "edge_worker", "zone": "Zone A"},
    {"id": "dev-02", "name": "Patrol Node Beta", "kind": "edge_worker", "zone": "Zone B"},
    {"id": "dev-03", "name": "Patrol Node Gamma", "kind": "edge_worker", "zone": "Zone C"},
    {"id": "dev-04", "name": "Patrol Node Delta", "kind": "edge_worker", "zone": "Zone D"},
]


def seed_fleet_shards():
    print(f"Provisioning {len(FLEET_DEVICES)} fleet devices...")
    from edge_node.main import get_or_create_shards, log_activity

    for dev in FLEET_DEVICES:
        dev_id = dev["id"]
        shards = get_or_create_shards(dev_id)
        log_activity(dev_id, "provision", f"Device {dev_id} provisioned successfully", None)
        print(f"  [+] Device {dev_id} ({dev['name']}) ready.")

    print("All fleet devices provisioned.")


if __name__ == "__main__":
    seed_fleet_shards()
