"""Local benchmark with raw timings. --offline measures hash-embedding API wiring only."""

import argparse
import contextlib
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def summary(values):
    ordered = sorted(values)
    return {
        "p50": statistics.median(ordered),
        "p95": ordered[int((len(ordered) - 1) * 0.95)],
        "min": min(ordered),
        "max": max(ordered),
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--offline", action="store_true")
    p.add_argument("--config", default=str(ROOT / "config" / "tiny-edge.yaml"))
    p.add_argument("--samples", type=int, default=50)
    p.add_argument("--warmup", type=int, default=3)
    p.add_argument("--output", type=Path, default=ROOT / "results" / "benchmark.json")
    a = p.parse_args()
    if not 1 <= a.samples <= 1000 or not 0 <= a.warmup <= 100:
        p.error("samples 1..1000; warmup 0..100")
    import yaml
    from fastapi.testclient import TestClient
    import edge_node.main as runtime

    config_path, output = Path(a.config).resolve(), a.output.resolve()
    config = yaml.safe_load(config_path.read_text())
    if a.offline:
        config["models"] = {
            "embedders": [
                {
                    "name": "text",
                    "modality": "text",
                    "provider": "fake",
                    "dim": 64,
                    "version": "hash-bench-v1",
                }
            ]
        }
    config["hub"] = {}
    config["network"] = {"mode": "full"}
    raw = {"capture": [], "query": []}
    cwd = Path.cwd()
    try:
        with tempfile.TemporaryDirectory(prefix="ex-vero-bench-") as directory:
            d = Path(directory)
            path = d / "config.yaml"
            path.write_text(yaml.safe_dump(config))
            runtime.DEFAULT_CONFIG_PATH = path
            runtime.SHARD_BASE_PATH = str(d / "shards")
            runtime.device_shards = {}
            runtime.event_logs = {}
            runtime._bench_seeded = set()
            os.chdir(d)
            with contextlib.redirect_stdout(io.StringIO()):
                with TestClient(runtime.app) as client:
                    for i in range(a.samples + a.warmup):
                        text = f"structural collapse observation {i} at shelter {i}"
                        started = time.perf_counter()
                        r = client.post(
                            "/devices/bench/capture",
                            json={"corroboration_key": f"bench.{i}", "value": text},
                        )
                        r.raise_for_status()
                        cap = (time.perf_counter() - started) * 1000
                        started = time.perf_counter()
                        r = client.post(
                            "/devices/bench/query", json={"text": text, "answer": False}
                        )
                        r.raise_for_status()
                        qry = (time.perf_counter() - started) * 1000
                        if i >= a.warmup:
                            raw["capture"].append(cap)
                            raw["query"].append(qry)
                    r = client.get("/benchmark/resolver-vs-lww")
                    r.raise_for_status()
                    resolver = r.json()
                    recall = None
                    if not a.offline:
                        r = client.get("/benchmark/recall")
                        r.raise_for_status()
                        recall = r.json()
                    stored = client.get("/devices/bench/memory").json()["total"]
            runtime.device_shards.clear()
            runtime.event_logs.clear()
    finally:
        os.chdir(cwd)
    fixtures = {}
    for name in ["recall_corpus.jsonl", "recall_queries.jsonl", "conflict_facts.jsonl"]:
        data = (ROOT / "tests" / "fixtures" / name).read_bytes()
        fixtures[name] = {
            "sha256": hashlib.sha256(data).hexdigest(),
            "rows": len(data.splitlines()),
        }
    result = {
        "schema": "ex-vero.local-benchmark.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "working_tree_dirty": bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"], cwd=ROOT, text=True
            ).strip()
        ),
        "mode": "offline_hash_wiring_only"
        if a.offline
        else "configured_embedding_models",
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "dependencies": {
                n: importlib.metadata.version(n)
                for n in ["fastapi", "qdrant-edge-py", "qdrant-client", "httpx"]
            },
        },
        "workload": {
            "samples": a.samples,
            "warmup_excluded": a.warmup,
            "stored_facts": stored,
            "concurrency": 1,
            "answer_generation": False,
            "transport": "in-process ASGI TestClient",
            "models": config["models"],
        },
        "fixtures": fixtures,
        "raw_ms": raw,
        "summary_ms": {k: summary(v) for k, v in raw.items()},
        "resolver": resolver,
        "semantic_recall": recall,
        "limitations": [
            "No CPU/RAM constraint imposed; not a physical-device result.",
            "Excludes gateway, Qdrant Server, network transport, Ollama and answer generation.",
            "Sequential synthetic workload; offline hash embeddings do not measure semantic quality.",
        ],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2) + "\n")
    print(
        json.dumps(
            {
                "output": str(output),
                "mode": result["mode"],
                "summary_ms": result["summary_ms"],
            }
        )
    )


if __name__ == "__main__":
    main()
