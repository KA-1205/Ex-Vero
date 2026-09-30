"""Phase 2 — Load Disaster Response Messages into the fleet shards.

Reads config/seed/disaster_messages_train.parquet and distributes rows across
dev-01..dev-04 using the live edge-node capture endpoint. Ingests ~5,250 rows
per device (21,046 total) to stress-test memory eviction at 500-point cap.
"""

import sys
import os
import math
import time
import random
import json
import requests
from pathlib import Path

BASE_URL = os.getenv("EDGE_URL", "http://localhost:8000")
ROOT = Path(__file__).resolve().parent.parent
PARQUET = ROOT / "config" / "seed" / "disaster_messages_train.parquet"

DEVICES = ["dev-01", "dev-02", "dev-03", "dev-04"]
ZONES = ["zone_a", "zone_b", "zone_c", "zone_d"]

DISASTER_CATEGORIES = [
    "earthquake", "flood", "fire", "hurricane", "storm", "tornado",
    "shelter", "evacuation", "rescue", "missing_person", "supply_request",
]


def load_parquet():
    try:
        import pandas as pd
    except ImportError:
        print("pandas not installed — run: pip install pandas pyarrow")
        sys.exit(1)
    df = pd.read_parquet(PARQUET)
    print(f"Loaded {len(df)} rows from {PARQUET.name}")
    return df


def capture(device_id: str, corroboration_key: str, value: str) -> dict:
    resp = requests.post(
        f"{BASE_URL}/devices/{device_id}/capture",
        json={"device_id": device_id, "corroboration_key": corroboration_key, "value": value},
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def main(limit: int = 2000):
    df = load_parquet()

    # Identify text column
    text_col = None
    for c in ("message", "original_text", "translated", "text"):
        if c in df.columns:
            text_col = c
            break
    if text_col is None:
        text_col = df.columns[0]
    print(f"Using column: {text_col}")

    rows = df[text_col].dropna().tolist()
    random.shuffle(rows)
    rows = rows[:limit]

    per_device = math.ceil(len(rows) / len(DEVICES))
    total_captured = 0
    errors = 0

    for i, device_id in enumerate(DEVICES):
        chunk = rows[i * per_device:(i + 1) * per_device]
        zone = ZONES[i % len(ZONES)]
        print(f"\n[{device_id}] Ingesting {len(chunk)} messages into {zone}…")
        for j, text in enumerate(chunk):
            cat = DISASTER_CATEGORIES[j % len(DISASTER_CATEGORIES)]
            key = f"{zone}.{cat}"
            try:
                res = capture(device_id, key, str(text)[:500])
                total_captured += 1
                if (j + 1) % 100 == 0:
                    print(f"  {j+1}/{len(chunk)} — last verdict: {res.get('verdict', '?')}")
            except Exception as e:
                errors += 1
                if errors <= 5:
                    print(f"  [!] Error at {j}: {e}")

    print(f"\nDone. Captured {total_captured} points across {len(DEVICES)} devices. Errors: {errors}")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    main(limit)
