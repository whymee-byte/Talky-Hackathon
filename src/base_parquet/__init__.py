"""Interim T1 contract builder: work/<phase>/lines.parquet (§1) and items.parquet (§2) from the official CSVs.

Stopgap so downstream modules (T2…) can read Parquet while `src/base/` (T1, galileo) delivers its own; same columns
as contracts/schemas.md §1–§2 (subset where noted). Replace by T1 as soon as it writes these files.
"""
