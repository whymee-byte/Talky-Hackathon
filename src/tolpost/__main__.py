"""T3g post-steps after src.excfix (one module so run_phase.sh, run_final.sh and quick_window.sh call it alike):
  1. overfix  OVERPAYMENT whose excess is not a whole amount nor 1 % / 10 % of the item -> no exception (src.tolmatch)
  2. T3u      learned UNIDENTIFIED flag for no-item receipts left without it (src.tolmatch.unid, versioned table)

    python -m src.tolpost --phase window|live|final
"""
import argparse

from src.tolmatch.__main__ import overfix
from src.tolmatch.unid import apply



def drop_ambiguous(phase):
    """T3g.ambig: a T5.full_clear row on a multi-item receipt whose invoice is NOT referenced in the bank text and whose
    amount is shared by >= 2 open invoices of the party is a coin toss between homonymous invoices (window: 29 % /
    0 % precision in those two cells, vs 95-100 % elsewhere) -> drop the pair (the line keeps its other rows)."""
    import duckdb
    w = f"work/{phase}"
    con = duckdb.connect()
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{w}/allocations.parquet'")
    con.execute(f"""CREATE TABLE D AS
      SELECT a.bank_txn_id, a.item_id
      FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id) JOIN '{w}/items.parquet' i USING (item_id)
      WHERE a.rule LIKE 'T5.full_clear%' AND l.direction = 'IN' AND i.kind = 'ARI'
        AND (SELECT count(*) FROM A a2 WHERE a2.bank_txn_id = a.bank_txn_id AND a2.item_id IS NOT NULL) > 1
        AND NOT (strpos(l.text_norm, replace(i.doc_number, '-', '')) > 0 OR strpos(l.text_norm, i.doc_digits) > 0)
        AND (SELECT count(*) FROM '{w}/items.parquet' i2 WHERE i2.party_id = i.party_id AND i2.kind = 'ARI'
             AND i2.open_at_start_cents = i.open_at_start_cents AND i2.issue_date <= l.booking_date) >= 2""")
    n = con.execute("SELECT count(*) FROM D").fetchone()[0]
    con.execute(f"""COPY (SELECT * FROM A WHERE (bank_txn_id, item_id) NOT IN (SELECT bank_txn_id, item_id FROM D))
                    TO '{w}/allocations_ambig.parquet' (FORMAT parquet)""")
    import os
    os.replace(f"{w}/allocations_ambig.parquet", f"{w}/allocations.parquet")
    print(f"[{phase}] T3g ambig: dropped {n} ambiguous full_clear pairs")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    a = ap.parse_args()
    overfix(a.phase)
    apply(a.phase)
    drop_ambiguous(a.phase)
