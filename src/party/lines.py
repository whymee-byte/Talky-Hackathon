"""Minimal stand-in for T1's lines.parquet (contract §1 column names), used until T1 publishes it.

Writes work/<phase>/party_lines.parquet. If work/<phase>/lines.parquet (T1) exists it is used instead.
Phases: sample, live, final, window (TRAIN >= 2024-07-01) and learn (TRAIN < 2024-07-01).
"""
import os

from .common import WINDOW_START, connect, out_dir, src

IBAN_RE = r"\b[A-Z]{2}[0-9]{2}[A-Z0-9]{11,30}\b"
MANDATE_RE = r"VTM-[A-Z]{2}-[0-9]{7}"


def build(phase, con=None):
    con = con or connect()
    t1 = f"work/{phase}/lines.parquet"
    out = f"{out_dir(phase)}/party_lines.parquet"
    if os.path.exists(t1):
        return t1
    base = "train" if phase in ("window", "learn") else phase
    where = {"window": f"WHERE booking_date >= DATE '{WINDOW_START}'",
             "learn": f"WHERE booking_date < DATE '{WINDOW_START}'"}.get(phase, "")
    con.execute(f"""
    COPY (
      SELECT bank_txn_id, account_id, entity, bank, statement_format, booking_date, value_date,
             CAST(round(amount * 100) AS BIGINT) AS amount_cents, currency,
             CASE WHEN amount >= 0 THEN 'IN' ELSE 'OUT' END AS direction,
             norm(concat_ws(' ', text, ref1, ref2, remittance_info, counterparty_name)) AS text_norm,
             nullif(norm(counterparty_name), '') AS cp_name_norm,
             list_distinct(list_concat(
                 CASE WHEN counterparty_account IS NULL THEN [] ELSE [upper(replace(counterparty_account, ' ', ''))] END,
                 regexp_extract_all(upper(concat_ws(' ', text, remittance_info)), '{IBAN_RE}'))) AS ibans,
             list_distinct(regexp_extract_all(upper(concat_ws(' ', text, ref1, ref2, remittance_info)), '{MANDATE_RE}'))
                 AS mandate_ids
      FROM {src(base, 'bank_transactions')} {where}
    ) TO '{out}' (FORMAT parquet)""")
    return out


if __name__ == "__main__":
    import sys
    print(build(sys.argv[1]))
