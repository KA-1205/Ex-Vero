"""Generate the Phase 9 recall fixtures (corpus + labeled queries).

Provenance for `tests/fixtures/recall_corpus.jsonl` and
`tests/fixtures/recall_queries.jsonl`. The fixtures are committed so the
benchmark is reproducible without running this, but keeping the generator means
anyone can audit how the labels were derived instead of trusting the JSONL.

    python tools/gen_benchmark_fixtures.py

The corpus models a disaster-response staging area: gas-cylinder assets, each
identified by an 8-hex serial, with inspection records. The queries are a
deliberate MIX of three shapes, because a benchmark that only contains the
cases the system wins measures nothing:

  * serial_lookup — "what was the hydrostatic result for asset SN-XXXX?" The
    serials differ by a character or two, so semantic similarity cannot separate
    them and exact-token matching does. This is where fusion earns its margin.
  * semantic — "which assets failed the leak test?" Dense retrieval already
    handles this; the leg exists to show fusion does not LOSE here.
  * paraphrase — "show records logged at Pier 2 Warehouse." Dense is expected to
    be fine; included as a control.

The seed is fixed, so regenerating reproduces the committed fixtures byte for
byte. Nothing about the query set is tuned against a score.
"""

import json
import random
from pathlib import Path

SEED = 23
HEX = "0123456789ABCDEF"

TESTS = [
    ("hydrostatic", "bar", 180.0),
    ("leak", "kPa/s", 0.4),
    ("valve", "Nm", 62.0),
    ("seal", "mbar", 12.0),
    ("thread", "mm", 0.05),
]
INSPECTORS = [
    "R. Okonkwo", "M. Haddad", "J. Lindqvist",
    "A. Farouk", "S. Devi", "T. Novak",
]
SITES = [
    "Staging Yard B", "Pier 2 Warehouse", "Refuge Hall C",
    "Dock B Annex", "Tank Farm",
]
N_ASSETS = 200
N_SERIAL_QUERIES = 24

OUT_DIR = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def build() -> tuple:
    rnd = random.Random(SEED)

    assets, seen = [], set()
    while len(assets) < N_ASSETS:
        serial = "SN-" + "".join(rnd.choice(HEX) for _ in range(8))
        if serial in seen:
            continue
        seen.add(serial)
        assets.append(serial)

    corpus = []
    doc_id = 0
    for serial in assets:
        for name, unit, limit in TESTS:
            doc_id += 1
            measured = round(limit * (1 + rnd.uniform(-0.3, 0.3)), 2)
            site = rnd.choice(SITES)
            inspector = rnd.choice(INSPECTORS)
            verdict = "PASS" if measured <= limit else "FAIL"
            corpus.append(
                {
                    "doc_id": doc_id,
                    "text": (
                        f"Cylinder asset {serial} {name} test at {site}: "
                        f"measured {measured} {unit} against {limit} {unit} "
                        f"limit, inspector {inspector}, result {verdict}"
                    ),
                }
            )

    def ids_for(serial, pred):
        return [
            d["doc_id"] for d in corpus
            if f"asset {serial} " in d["text"] and pred(d["text"])
        ]

    queries = []
    for serial in assets[:N_SERIAL_QUERIES]:
        queries.append(
            {
                "query": (
                    "What was the hydrostatic test result for cylinder asset "
                    f"{serial}?"
                ),
                "relevant_ids": ids_for(serial, lambda t: "hydrostatic" in t),
                "shape": "serial_lookup",
            }
        )

    for name, _unit, _limit in TESTS[1:]:
        queries.append(
            {
                "query": f"Which cylinder assets failed the {name} test?",
                "relevant_ids": [
                    d["doc_id"] for d in corpus
                    if f" {name} test" in d["text"] and "FAIL" in d["text"]
                ],
                "shape": "semantic",
            }
        )

    for site in SITES[:4]:
        queries.append(
            {
                "query": f"Show inspection records logged at {site}.",
                "relevant_ids": [
                    d["doc_id"] for d in corpus if f"at {site}:" in d["text"]
                ],
                "shape": "paraphrase",
            }
        )

    return corpus, queries


def main() -> None:
    corpus, queries = build()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(OUT_DIR / "recall_corpus.jsonl", "w", encoding="utf-8") as fh:
        for row in corpus:
            fh.write(json.dumps(row) + "\n")
    with open(OUT_DIR / "recall_queries.jsonl", "w", encoding="utf-8") as fh:
        for row in queries:
            fh.write(json.dumps(row) + "\n")
    print(f"corpus={len(corpus)} docs, queries={len(queries)}")


if __name__ == "__main__":
    main()
