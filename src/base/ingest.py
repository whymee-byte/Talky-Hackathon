"""CLI ingestion for supported bank statement formats."""
from __future__ import annotations

import argparse
import json
import logging
from collections import Counter
from pathlib import Path

from .models import MalformedRecord, NormalizedTransaction
from .parsers import parse_file

LOG = logging.getLogger(__name__)


def ingest(input_dir: Path, output: Path, errors: Path) -> tuple[int, int]:
    output.parent.mkdir(parents=True, exist_ok=True)
    errors.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set(); good = bad = 0; counts = Counter()
    with output.open("w", encoding="utf-8", newline="\n") as out, errors.open("w", encoding="utf-8", newline="\n") as err:
        for path in sorted(p for p in input_dir.rglob("*") if p.is_file()):
            for item in parse_file(path):
                if isinstance(item, MalformedRecord):
                    err.write(item.model_dump_json() if hasattr(item, "model_dump_json") else item.json()); err.write("\n"); bad += 1; continue
                if item.bank_txn_id in seen: continue
                seen.add(item.bank_txn_id); out.write(item.model_dump_json() if hasattr(item, "model_dump_json") else item.json()); out.write("\n"); good += 1
                counts[(item.account, item.value_date, item.currency)] += 1
    for (account, date, currency), n in sorted(counts.items()): LOG.info("account=%s date=%s currency=%s count=%d", account, date, currency, n)
    LOG.info("normalized=%d malformed=%d duplicates=%d", good, bad, sum(1 for _ in []))
    return good, bad


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/sample/bank_statements"))
    parser.add_argument("--output", type=Path, default=Path("normalized_transactions.jsonl"))
    parser.add_argument("--errors", type=Path, default=Path("parsing_errors.jsonl"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ingest(args.input, args.output, args.errors)


if __name__ == "__main__": main()
