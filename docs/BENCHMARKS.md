# Benchmarks and verification scope

## Reproduce the recorded local result

```bash
python -m pip install -r requirements.txt pytest==9.1.1
python tools/benchmark_report.py --offline --samples 30 --output results/offline-wiring-benchmark.json
```

The [raw result](../results/offline-wiring-benchmark.json) records all samples, warmups, Python/platform, dependency versions, CPU count, Git revision/dirty state, fixture hashes/counts, and workload settings. It uses a real Qdrant Edge shard and in-process ASGI requests with deterministic hash embeddings. It is an API/persistence wiring benchmark, **not semantic retrieval quality**, a physical edge device test, or a 1-CPU/512-MB measurement.

This recorded workload is sequential: three excluded warmups, 30 measured captures and matching queries, with answer generation off. Reported p50/p95 are descriptive order statistics of that small run; they are not capacity guarantees or confidence intervals. Gateway, Qdrant Server, network transport, and Ollama are excluded.

## Semantic and resolver comparisons

```bash
python tools/benchmark_report.py --config config/tiny-edge.yaml --samples 50 --output results/model-benchmark.json
```

This mode requires the configured real pretrained checkpoint. It also runs `/benchmark/recall` over the checked-in labeled corpus. The resolver comparison uses the actual gateway consensus fold and the same labeled disputes for the last-write-wins baseline. Fixture-specific accuracy is not an estimate of field-data performance. Do not relabel the offline hash run as a semantic result.

## Existing historical figures

The README's CPU/RSS, recall, resolver, and crash-safety figures originated from earlier runs. The [saved crash result](../results/crash_safety.json) and [crash harness](../tools/crash_test.py) support a narrowly defined capture/SIGKILL/restart scenario. They do not prove all possible crash or storage failure cases. Prior CPU/RSS claims concern the edge process/container, not the full stack including the model server.

Reproduce constrained-resource claims with Docker limits and record the exact host, image digest, models, data count, concurrency, warm/cold state, raw telemetry, and repetitions. No new constrained-device result is claimed here.

## CI and backend structure

[CI](https://github.com/KA-1205/Ex-Vero/actions) runs a named deterministic subset: offline API capture/query, failed-push/retry/ack/snapshot pull, consensus, sync selection, cgroup parsing, model stamps, and repository checks. It uploads JUnit and benchmark JSON artifacts. A green deterministic job is not a full model-suite pass. The full checkpoint-dependent suite is an explicit manual workflow option; external services and Docker must be available for their respective integration checks.

The backend entry point registers separate routers under `src/edge_node/api/`; blocking sync work lives in `src/edge_node/services/sync_orchestration.py`. Factories receive the existing runtime state to preserve shard ownership, mutable state, and test overrides. The extraction preserved the OpenAPI contract and existing endpoint names.
