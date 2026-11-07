"""CLI and streaming writer for normalized bank statements."""
from __future__ import annotations

import argparse
import logging
from collections import Counter
from pathlib import Path
from typing import Iterator

from .models import MalformedRecord, NormalizedTransaction
from .parsers import parse_file

LOG = logging.getLogger(__name__)


def _json_line(model: NormalizedTransaction | MalformedRecord) -> str:
    return model.model_dump_json()


def _iter_statement_files(input_dir: Path, excluded: set[Path]) -> Iterator[Path]:
    yield from (
        path
        for path in sorted(input_dir.rglob("*"))
        if path.is_file() and path.resolve() not in excluded
    )


def ingest(input_dir: Path, output: Path, errors: Path) -> tuple[int, int]:
    """Normalize all supported statements, deduplicating by deterministic id.

    Returns ``(normalized_count, malformed_count)``. Duplicate records are not
    written twice and are reported in the final log line.
    """

    if not input_dir.exists():
        raise FileNotFoundError(f"input directory does not exist: {input_dir}")
    if not input_dir.is_dir():
        raise NotADirectoryError(f"input path is not a directory: {input_dir}")
    output = output.resolve()
    errors = errors.resolve()
    if output == errors:
        raise ValueError("output and errors paths must be different")
    output.parent.mkdir(parents=True, exist_ok=True)
    errors.parent.mkdir(parents=True, exist_ok=True)

    seen: set[str] = set()
    counts: Counter[tuple[str, str, str]] = Counter()
    normalized_count = 0
    malformed_count = 0
    duplicate_count = 0

    with output.open("w", encoding="utf-8", newline="\n") as normalized_file, errors.open(
        "w", encoding="utf-8", newline="\n"
    ) as errors_file:
        excluded = {output, errors}
        for path in _iter_statement_files(input_dir, excluded):
            try:
                records = parse_file(path)
                for record in records:
                    if isinstance(record, MalformedRecord):
                        errors_file.write(_json_line(record) + "\n")
                        malformed_count += 1
                        continue
                    if record.bank_txn_id in seen:
                        duplicate_count += 1
                        continue
                    seen.add(record.bank_txn_id)
                    normalized_file.write(_json_line(record) + "\n")
                    normalized_count += 1
                    counts[(record.account, record.value_date.isoformat(), record.currency)] += 1
            except Exception as exc:
                malformed = MalformedRecord(
                    source_file=str(path),
                    raw_record=path.read_text(encoding="utf-8", errors="replace"),
                    error=f"unexpected parser failure: {exc}",
                    format=path.suffix.lower().lstrip(".") or None,
                )
                errors_file.write(_json_line(malformed) + "\n")
                malformed_count += 1

    for (account, statement_date, currency), count in sorted(counts.items()):
        LOG.info(
            "account=%s date=%s currency=%s count=%d",
            account,
            statement_date,
            currency,
            count,
        )
    LOG.info(
        "normalized=%d malformed=%d duplicates=%d",
        normalized_count,
        malformed_count,
        duplicate_count,
    )
    return normalized_count, malformed_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        "--input-dir",
        dest="input_dir",
        type=Path,
        default=Path("data/sample/bank_statements"),
        help="directory containing raw bank statements",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("normalized_transactions.jsonl"),
        help="JSONL output for normalized transactions",
    )
    parser.add_argument(
        "--errors",
        type=Path,
        default=Path("parsing_errors.jsonl"),
        help="JSONL output for malformed records",
    )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ingest(args.input_dir, args.output, args.errors)


if __name__ == "__main__":
    main()
