"""Create persistent local shards for the configured demo fleet."""

import asyncio
import sys
from pathlib import Path

# Add project root to sys.path
root_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root_dir / "src"))

async def _seed_fleet_shards():
    from edge_node.main import DEFAULT_FLEET, app, get_or_create_shards, log_activity

    async with app.router.lifespan_context(app):
        print(f"Provisioning {len(DEFAULT_FLEET)} fleet devices...")
        for device in DEFAULT_FLEET:
            device_id = device["id"]
            get_or_create_shards(device_id)
            log_activity(device_id, "mode_change", f"Device {device_id} provisioned", None)
            print(f"  [+] {device_id} ({device['name']}) ready.")
        print("All fleet device shards provisioned.")


def seed_fleet_shards():
    asyncio.run(_seed_fleet_shards())


if __name__ == "__main__":
    seed_fleet_shards()
