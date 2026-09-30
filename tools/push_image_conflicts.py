"""Task B2 — Push image conflicts through the gateway / edge node.

Reads config/seed/image_conflicts.jsonl and injects each scenario's camera
disagreements (e.g. cam-01: fire vs cam-02: none for server_room_A.fire_status).
Scores the resulting consensus fold against the annotated ground_truth.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

CONFLICTS_PATH = ROOT / "config" / "seed" / "image_conflicts.jsonl"
BASE_URL = os.getenv("EDGE_URL", "http://localhost:8000")
GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost:8088")


def load_conflicts():
    if not CONFLICTS_PATH.exists():
        raise FileNotFoundError(f"Image conflicts file missing: {CONFLICTS_PATH}")
    entries = []
    with open(CONFLICTS_PATH, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def push_conflicts():
    scenarios = load_conflicts()
    print(f"Loaded {len(scenarios)} vision conflict scenarios from {CONFLICTS_PATH.name}")

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
        import httpx
        with httpx.Client(timeout=30.0) as client:
            for sc in scenarios:
                key = sc.get("corroboration_key", "server_room_A.fire_status")
                assignments = {rep["device_id"]: rep["label"] for rep in sc.get("reports", [])}
                gt = sc.get("ground_truth")

                resp = client.post(
                    f"{BASE_URL}/demo/inject-conflict",
                    json={"corroboration_key": key, "assignments": assignments},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    cons = data.get("consensus", {})
                    state = cons.get("state")
                    res_val = cons.get("resolved_value")
                    conf = cons.get("confidence", 0.0)
                    match_gt = (res_val == gt) if res_val is not None else False
                    print(f"  [+] Scenario {sc.get('scenario_id')}: {key} -> {state} (conf: {conf:.2f}), value: {res_val}, GT: {gt} (match: {match_gt})")
                else:
                    print(f"  [-] Failed injection for {sc.get('scenario_id')}: {resp.status_code} {resp.text}")
    else:
        print("[*] Server not reachable over HTTP; evaluating conflicts in-process via edge_node...")
        from fastapi.testclient import TestClient
        from edge_node.main import app

        with TestClient(app) as client:
            for sc in scenarios:
                key = sc.get("corroboration_key", "server_room_A.fire_status")
                assignments = {rep["device_id"]: rep["label"] for rep in sc.get("reports", [])}
                gt = sc.get("ground_truth")

                resp = client.post(
                    "/demo/inject-conflict",
                    json={"corroboration_key": key, "assignments": assignments},
                )
                if resp.status_code == 200:
                    data = resp.json()
                    cons = data.get("consensus", {})
                    state = cons.get("state")
                    res_val = cons.get("resolved_value")
                    conf = cons.get("confidence", 0.0)
                    match_gt = (res_val == gt) if res_val is not None else False
                    print(f"  [+] Scenario {sc.get('scenario_id')}: {key} -> {state} (conf: {conf:.2f}), value: {res_val}, GT: {gt} (match: {match_gt})")
                else:
                    print(f"  [-] Failed injection for {sc.get('scenario_id')}: {resp.status_code} {resp.text}")

    print("[*] Image conflict scenarios pushed successfully.")


if __name__ == "__main__":
    push_conflicts()
