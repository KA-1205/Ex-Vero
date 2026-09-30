"""Prepare a small, deterministic Ex-Vero image seed from a Roboflow export.

The input is a YOLOv8 export with ``train``, ``valid`` and/or ``test`` folders.
Images with an empty annotation file are treated as the explicit ``none`` class.
The script does not require Roboflow credentials; download the export separately
and run this script offline.
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from PIL import Image


SOURCE_URL = (
    "https://universe.roboflow.com/server-room-fire-and-smoke-detection/"
    "serveroom-fire-and-smoke-dtc./dataset/9"
)
LABELS = {"fire": 0, "smoke": 1}
CAPTIONS = {
    "fire": "flames near a server rack",
    "smoke": "smoke pooling above a server rack",
    "none": "clear server room aisle with no visible hazard",
}
DEVICES = ("cam-01", "cam-02", "cam-03")
ZONES = ("server_room_A", "server_room_B", "server_room_C")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="Roboflow YOLO export directory")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("config/seed"),
        help="seed output directory (default: config/seed)",
    )
    parser.add_argument(
        "--per-label",
        type=int,
        default=4,
        help="images to keep for each label (default: 4)",
    )
    parser.add_argument(
        "--max-side",
        type=int,
        default=1024,
        help="maximum output image side in pixels (default: 1024)",
    )
    return parser.parse_args()


def image_label(label_path: Path) -> str | None:
    lines = [line.split() for line in label_path.read_text().splitlines() if line.strip()]
    if not lines:
        return "none"
    classes = {int(parts[0]) for parts in lines if len(parts) == 5}
    if len(classes) != 1 or next(iter(classes), -1) not in LABELS.values():
        return None
    return next(label for label, index in LABELS.items() if index in classes)


def source_images(source: Path) -> dict[str, list[Path]]:
    selected: dict[str, list[Path]] = {label: [] for label in (*LABELS, "none")}
    for image_path in sorted(source.glob("**/images/*")):
        if image_path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            continue
        label_path = image_path.parent.parent / "labels" / f"{image_path.stem}.txt"
        if not label_path.exists():
            continue
        label = image_label(label_path)
        if label is not None:
            selected[label].append(image_path)
    return selected


def write_image(source_path: Path, destination: Path, max_side: int) -> None:
    with Image.open(source_path) as image:
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        image.save(destination, format="JPEG", quality=88, optimize=True)


def manifest_row(image_id: str, label: str, index: int) -> dict[str, object]:
    zone = ZONES[index % len(ZONES)]
    device = DEVICES[index % len(DEVICES)]
    return {
        "image_id": image_id,
        "file": f"images/{image_id}.jpg",
        "label": label,
        "zone": zone,
        "device_id": device,
        "corroboration_key": f"{zone}.fire_status",
        "caption": CAPTIONS[label],
        "client_timestamp_ns": 1730000000000000000 + index * 1_000_000_000,
    }


def conflict(scenario_id: str, zone: str, reports: list[dict[str, str]], ground_truth: str) -> dict[str, object]:
    return {
        "scenario_id": scenario_id,
        "corroboration_key": f"{zone}.fire_status",
        "zone": zone,
        "reports": reports,
        "ground_truth": ground_truth,
    }


def write_conflicts(rows: list[dict[str, object]], output_path: Path) -> None:
    by_label = {label: [row for row in rows if row["label"] == label] for label in (*LABELS, "none")}
    def report(row: dict[str, object], device: str) -> dict[str, str]:
        return {"device_id": device, "image_id": str(row["image_id"]), "label": str(row["label"])}

    scenarios = [
        conflict("vsc_001", "server_room_A", [report(by_label["fire"][0], "cam-01"), report(by_label["none"][0], "cam-02"), report(by_label["fire"][1], "cam-03")], "fire"),
        conflict("vsc_002", "server_room_B", [report(by_label["smoke"][0], "cam-01"), report(by_label["smoke"][1], "cam-02"), report(by_label["none"][1], "cam-03")], "smoke"),
        conflict("vsc_003", "server_room_C", [report(by_label["none"][2], "cam-01"), report(by_label["none"][3], "cam-02"), report(by_label["fire"][2], "cam-03")], "none"),
        conflict("vsc_004", "server_room_A", [report(by_label["fire"][3], "cam-01"), report(by_label["none"][0], "cam-02"), report(by_label["none"][1], "cam-03")], "fire"),
    ]
    with output_path.open("w", encoding="utf-8") as handle:
        for scenario in scenarios:
            handle.write(json.dumps(scenario, separators=(",", ":")) + "\n")


def main() -> None:
    args = parse_args()
    if args.per_label < 4:
        raise ValueError("--per-label must be at least 4 for the built-in conflict scenarios")
    selected = source_images(args.source)
    missing = [label for label, paths in selected.items() if len(paths) < args.per_label]
    if missing:
        raise RuntimeError(f"not enough source images for: {', '.join(missing)}")

    image_dir = args.output / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    index = 0
    for label in ("fire", "smoke", "none"):
        for source_path in selected[label][: args.per_label]:
            image_id = f"img_{index + 1:03d}"
            write_image(source_path, image_dir / f"{image_id}.jpg", args.max_side)
            rows.append(manifest_row(image_id, label, index))
            index += 1

    with (args.output / "images_manifest.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, separators=(",", ":")) + "\n")
    write_conflicts(rows, args.output / "image_conflicts.jsonl")
    print(f"Wrote {len(rows)} images from {SOURCE_URL} to {args.output}")


if __name__ == "__main__":
    main()