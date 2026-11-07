"""T20 · long receipts that list many truncated invoice numbers: add the candidates already due (before src.excfix).

    python -m src.duefill --phase window|live|final

Receipts (IN) whose current items still fall short of the cash and that have >= 5 T3f candidates
(work/<phase>/fuzz_cands.parquet). A truncated / one-typo candidate is added (open amount, no exception) when it is not
allocated anywhere and fell due 0-30 days before the booking date (customers pay what is due: window precision 68 %,
above the F1/2 break-even; not yet due 8 %, overdue > 45 days 3-10 %). An item claimed by two lines goes to the earliest.
"""
import argparse
import os

import duckdb

from src.common import REPO

DUE_MIN, DUE_MAX, NC_MIN = 0, 30, 5


def build(phase: str) -> None:
    w = os.path.join(REPO, "work", phase)
    p, cands = f"{w}/allocations.parquet", f"{w}/fuzz_cands.parquet"
    if not os.path.exists(cands):
        print(f"[{phase}] duefill: no fuzz_cands"); return
    con = duckdb.connect()
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{p}'")
    con.execute(f"""CREATE TABLE SH AS SELECT l.bank_txn_id FROM '{w}/lines.parquet' l LEFT JOIN A a USING (bank_txn_id)
                    LEFT JOIN '{w}/items.parquet' i USING (item_id) WHERE l.direction = 'IN'
                    GROUP BY l.bank_txn_id, l.amount_cents
                    HAVING coalesce(sum(CASE WHEN i.kind = 'ARC' THEN -a.amount_cents ELSE a.amount_cents END), 0)
                           < abs(l.amount_cents) - 100""")
    con.execute(f"""CREATE TABLE ADD AS
      SELECT c.bank_txn_id, c.item_id, c.open_c FROM (
        SELECT c.*, count(*) OVER (PARTITION BY c.bank_txn_id) nc,
               date_diff('day', i.due_date, l.booking_date) overdue
        FROM '{cands}' c JOIN '{w}/items.parquet' i USING (item_id) JOIN '{w}/lines.parquet' l USING (bank_txn_id)
        WHERE c.bank_txn_id IN (SELECT bank_txn_id FROM SH)
          AND c.item_id NOT IN (SELECT item_id FROM A WHERE item_id IS NOT NULL)
          AND c.how IN ('trunc', 'typo+trunc', 'trunc+typo') AND c.kind = 'ARI') c
      WHERE c.nc >= {NC_MIN} AND c.overdue BETWEEN {DUE_MIN} AND {DUE_MAX}
      QUALIFY row_number() OVER (PARTITION BY c.item_id ORDER BY c.bank_txn_id) = 1""")
    con.execute("DELETE FROM A WHERE item_id IS NULL AND bank_txn_id IN (SELECT bank_txn_id FROM ADD)")
    con.execute("""INSERT INTO A BY NAME SELECT bank_txn_id, item_id, open_c amount_cents, NULL::VARCHAR category, '' exception,
                     'T20.duefill' rule, 0.68 confidence, 'truncated number in text; invoice due 0-30 days before' evidence FROM ADD""")
    print(f"[{phase}] duefill:", con.execute("SELECT count(DISTINCT bank_txn_id), count(*) FROM ADD").fetchall())
    con.execute(f"COPY A TO '{p}' (FORMAT parquet)")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
