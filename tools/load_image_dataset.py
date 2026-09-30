"""Task B1 — Replay images manifest onto camera fleet.

Reads config/seed/images_manifest.jsonl and captures each image through
multipart POST /devices/{cam}/capture onto cam-01..cam-03.
Zone and corroboration_key come directly from the manifest.
Supports both live HTTP server and offline direct TestClient replay.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

MANIFEST_PATH = ROOT / "config" / "seed" / "images_manifest.jsonl"
SEED_DIR = ROOT / "config" / "seed"
BASE_URL = os.getenv("EDGE_URL", "http://localhost:8000")


def load_manifest():
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")
    entries = []
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def replay_manifest():
    entries = load_manifest()
    print(f"Loaded {len(entries)} items from {MANIFEST_PATH.name}")

    # Determine whether server is live or we use direct TestClient
    is_live = False
    try:
        import httpx
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{BASE_URL}/network/mode")
            if resp.status_code == 200:
                is_live = True
    except Exception:
        is_live = False

    if is_live:
        print(f"[*] Replaying images via live server at {BASE_URL}...")
        import httpx
        with httpx.Client(timeout=30.0) as client:
            for item in entries:
                dev = item.get("device_id", "cam-01")
                rel_file = item.get("file", "")
                img_path = SEED_DIR / rel_file
                if not img_path.exists():
                    print(f"[-] Image file missing: {img_path}")
                    continue

                with open(img_path, "rb") as img_f:
                    files = {"file": (img_path.name, img_f.read(), "image/jpeg")}
                    data = {
                        "corroboration_key": item.get("corroboration_key", "server_room_A.fire_status"),
                        "zone": item.get("zone", "server_room_A"),
                        "caption": item.get("caption", ""),
                        "label": item.get("label", "none"),
                    }
                    resp = client.post(f"{BASE_URL}/devices/{dev}/capture", data=data, files=files)
                    if resp.status_code == 200:
                        res = resp.json()
                        print(f"  [+] {dev} captured {item.get('image_id')} -> Point #{res.get('id')} ({res.get('verdict')})")
                    else:
                        print(f"  [-] Failed capture on {dev}: {resp.status_code} {resp.text}")
    else:
        print("[*] Server not reachable over HTTP; replaying via direct in-process TestClient...")
        from fastapi.testclient import TestClient
        from edge_node.main import app

        with TestClient(app) as client:
            for item in entries:
                dev = item.get("device_id", "cam-01")
                rel_file = item.get("file", "")
                img_path = SEED_DIR / rel_file
                if not img_path.exists():
                    print(f"[-] Image file missing: {img_path}")
                    continue

                with open(img_path, "rb") as img_f:
                    files = {"file": (img_path.name, img_f.read(), "image/jpeg")}
                    data = {
                        "corroboration_key": item.get("corroboration_key", "server_room_A.fire_status"),
                        "zone": item.get("zone", "server_room_A"),
                        "caption": item.get("caption", ""),
                        "label": item.get("label", "none"),
                    }
                    resp = client.post(f"/devices/{dev}/capture", data=data, files=files)
                    if resp.status_code == 200:
                        res = resp.json()
                        print(f"  [+] {dev} captured {item.get('image_id')} -> Point #{res.get('id')} ({res.get('verdict')})")
                    else:
                        print(f"  [-] Failed capture on {dev}: {resp.status_code} {resp.text}")

    print("[*] Replay of images_manifest complete.")


if __name__ == "__main__":
    replay_manifest()
