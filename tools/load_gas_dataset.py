"""Phase 2 — Extract UCI Gas Sensor facts into config/seed/gas_sensors_seed.jsonl.

Reads C:\\Users\\HP\\Downloads\\ethylene_methane.txt\\ethylene_methane.txt
(638 MB, Tab-separated with header: Time, Methane_ppm, Ethylene_ppm, 16 sensor channels)

Samples every Nth row to keep a manageable subset (~500 rows) and classifies
each reading into a hazard label (safe / methane_low / methane_high /
ethylene_low / ethylene_high / combined_hazard).
"""

import sys
import json
import itertools
from pathlib import Path

GAS_FILE = Path(r"C:\Users\HP\Downloads\ethylene_methane.txt\ethylene_methane.txt")
OUT = Path(__file__).resolve().parent.parent / "config" / "seed" / "gas_sensors_seed.jsonl"

# Thresholds (ppm)
METHANE_LOW = 250
METHANE_HIGH = 1000
ETHYLENE_LOW = 5
ETHYLENE_HIGH = 50

SAMPLE_EVERY = 500  # sample every 500th row → ~12,800 rows / 500 = ~25 rows at 6M rows, too few; use dynamic

ZONE_CYCLE = ["zone_a", "zone_b", "zone_c", "zone_d"]


def classify(methane: float, ethylene: float) -> str:
    if methane >= METHANE_HIGH and ethylene >= ETHYLENE_HIGH:
        return "combined_hazard"
    if methane >= METHANE_HIGH:
        return "methane_high"
    if ethylene >= ETHYLENE_HIGH:
        return "ethylene_high"
    if methane >= METHANE_LOW:
        return "methane_low"
    if ethylene >= ETHYLENE_LOW:
        return "ethylene_low"
    return "safe"


def make_text(methane: float, ethylene: float, label: str) -> str:
    if label == "safe":
        return f"Gas sensors nominal: methane {methane:.0f}ppm, ethylene {ethylene:.2f}ppm — atmosphere clear"
    if label == "methane_high":
        return f"ALERT: high methane concentration {methane:.0f}ppm detected — evacuation recommended"
    if label == "methane_low":
        return f"WARNING: methane trace {methane:.0f}ppm — monitor ventilation"
    if label == "ethylene_high":
        return f"ALERT: high ethylene concentration {ethylene:.2f}ppm — flammable gas risk"
    if label == "ethylene_low":
        return f"WARNING: ethylene trace {ethylene:.2f}ppm — investigate source"
    return f"CRITICAL: combined hazard methane {methane:.0f}ppm + ethylene {ethylene:.2f}ppm — evacuate immediately"


def extract(target: int = 500):
    if not GAS_FILE.exists():
        print(f"Gas file not found: {GAS_FILE}")
        sys.exit(1)

    # Count total lines first (streaming)
    print("Counting rows …")
    total = 0
    with open(GAS_FILE, "r", encoding="utf-8") as f:
        next(f)  # skip header
        for _ in f:
            total += 1
    print(f"Total data rows: {total:,}")

    step = max(1, total // target)
    print(f"Sampling every {step}th row -> ~{total // step} records")

    records = []
    seen_labels: dict = {}

    with open(GAS_FILE, "r", encoding="utf-8") as f:
        next(f)  # skip header
        for i, line in enumerate(f):
            if i % step != 0:
                continue
            parts = line.strip().split()
            if len(parts) < 3:
                continue
            try:
                ts = float(parts[0])
                methane = float(parts[1])
                ethylene = float(parts[2])
            except ValueError:
                continue

            label = classify(methane, ethylene)
            zone = ZONE_CYCLE[len(records) % len(ZONE_CYCLE)]
            text = make_text(methane, ethylene, label)

            rec = {
                "scenario_id": f"gas_{len(records):04d}",
                "corroboration_key": f"{zone}.gas_{label}",
                "zone": zone,
                "value": text,
                "label": label,
                "methane_ppm": round(methane, 2),
                "ethylene_ppm": round(ethylene, 4),
                "time_s": round(ts, 2),
            }
            records.append(rec)
            seen_labels[label] = seen_labels.get(label, 0) + 1

    with open(OUT, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")

    print(f"\nWrote {len(records)} records -> {OUT}")
    print("Label distribution:", seen_labels)


if __name__ == "__main__":
    target = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    extract(target)
