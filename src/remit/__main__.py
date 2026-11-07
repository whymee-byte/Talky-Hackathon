"""T6 · remittance advices → 1:N allocations, merged into work/<fase>/side_links.parquet (rule T6.remit.*).

    python -m src.remit --phase window|live|final|sample

1. parse every advice of the visible phases (src/remit/parse.py, cached in data_cache/<phase>/remit_raw.parquet);
2. advice → bank receipt: payment reference in the bank text (n-grams without separators) or same entity + amount =
   advice total, booking date within [-7, +12] days of the payment date; one receipt per advice and vice versa;
3. advice line → item: our number (exact / no prefix / digits / digit suffix), the customer's PO, then the gross
   amount within the advice's party; ties broken by amount equality and by the advice's majority party;
4. amount and exception (POLICIES §2-3): no deduction → open amount, ''; reason of a discount → SHORT_DISCOUNT
   (full amount); withholding → SHORT_WITHHOLDING (full); any other stated reason → SHORT_DISPUTE (amount paid);
   deduction without reason → discount / withholding by the item's terms or rate, else SHORT_DISPUTE; credit notes
   (negative lines) → the ARC/APC at full amount, no exception; line currency ≠ item currency → FX_DIFFERENCE on
   fully cleared invoices (SHORT_DISPUTE / PARTIAL keep their code).
Rows of lines already explained by T2 are never touched; a re-run replaces previous T6 rows.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO, phase_dir, safe_stdout
from src.common.profile import sql_wht_parties, withholding
from src.remit import parse

SOURCES = {"sample": ["sample"], "window": ["train"], "train": ["train"], "live": ["train", "live"],
           "final": ["train", "live", "final"]}
DATE_LO, DATE_HI = -7, 12          # booking_date - payment_date, days
DISC_WORDS = "DISCOUNT|SKONTO|DESCUENTO|DTO|PRONTO|ESCOMPTE|SCONTO|DESCONTO|RABAT"
WHT_WORDS = r"WITHHOLD|RETENC|RETENU|RITENUTA|RETEN|WHT|ISR|IRRF|QUELLEN|\bRET\b"
# SHORT_WITHHOLDING entities and rates: learned from TRAIN + POLICIES fallback (src.common.profile), not hard-coded


def build(phase: str) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase)
    raws = []
    for src in SOURCES[phase]:
        p = os.path.join(REPO, "data_cache", src, "remit_raw.parquet")
        if not os.path.exists(p) and os.path.isdir(os.path.join(phase_dir(src), "remittance_advices")):
            parse.build(src)
        if os.path.exists(p):
            raws.append(p)
    con = duckdb.connect()
    con.execute("SET threads=8; SET memory_limit='8GB'")
    side_p = f"{w}/side_links.parquet"
    con.execute(f"CREATE TABLE SL0 AS SELECT * FROM '{side_p}' WHERE rule NOT LIKE 'T6.%'" if os.path.exists(side_p) else
                "CREATE TABLE SL0 AS SELECT NULL::VARCHAR bank_txn_id, NULL::VARCHAR item_id, NULL::BIGINT amount_cents, "
                "NULL::VARCHAR exception, NULL::VARCHAR rule, NULL::VARCHAR link_key, NULL::DOUBLE confidence, "
                "NULL::VARCHAR evidence WHERE false")
    if not raws:
        print(f"[{phase}] no remittance advices"); return
    files = ", ".join(f"'{p}'" for p in raws)
    con.execute(f"CREATE TABLE R AS SELECT * FROM read_parquet([{files}], union_by_name=true) WHERE error IS NULL")
    con.execute(f"CREATE TABLE L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE TABLE I AS SELECT * FROM '{w}/items.parquet'")
    con.execute("""CREATE TABLE LT AS SELECT * FROM L WHERE direction = 'IN'
                   AND bank_txn_id NOT IN (SELECT bank_txn_id FROM SL0)""")

    # ---------------------------------------------------------------- 1. advices
    con.execute("""CREATE TABLE A AS
      SELECT file, any_value(entity) entity, any_value(payer_name) payer_name, any_value(payment_ref) payment_ref,
             regexp_replace(upper(coalesce(any_value(payment_ref), '')), '[^A-Z0-9]', '', 'g') ref_norm,
             coalesce(try_cast(any_value(payment_date) AS DATE), try_cast(any_value(received_date) AS DATE)) pay_date,
             try_cast(any_value(received_date) AS DATE) recv_date,
             round(coalesce(sum(paid), any_value(adv_amount)) * 100)::BIGINT total_cents,
             round(any_value(adv_amount) * 100)::BIGINT adv_cents, any_value(adv_currency) cur, count(*) n_lines
      FROM R GROUP BY file""")

    # ---------------------------------------------------------------- 2. advice → bank receipt
    con.execute("""CREATE TABLE NG AS   -- token n-grams of the receipts' text (no separators), as in the bank shows refs
      WITH t AS (SELECT bank_txn_id, string_split(text_norm, ' ') tok FROM LT)
      SELECT DISTINCT bank_txn_id, array_to_string(tok[i:i + k - 1], '') g
      FROM t, LATERAL (SELECT unnest(range(1, least(len(tok), 60) + 1)) i), LATERAL (SELECT unnest([1, 2, 3, 4]) k)
      WHERE i + k - 1 <= len(tok) AND length(array_to_string(tok[i:i + k - 1], '')) >= 6""")
    con.execute(f"""CREATE TABLE AL AS
      WITH byref AS (
        SELECT a.file, l.bank_txn_id, 3 AS how FROM A a JOIN NG n ON n.g = a.ref_norm
        JOIN LT l ON l.bank_txn_id = n.bank_txn_id AND l.entity = a.entity
        WHERE length(a.ref_norm) >= 6 AND date_diff('day', a.pay_date, l.booking_date) BETWEEN -20 AND 40),
      byamt AS (
        SELECT a.file, l.bank_txn_id, 2 AS how FROM A a JOIN LT l ON l.entity = a.entity
          AND (l.amount_cents = a.total_cents OR l.amount_cents = a.adv_cents
               OR (l.orig_amount_cents IS NOT NULL AND abs(l.orig_amount_cents) IN (a.total_cents, a.adv_cents)))
        WHERE date_diff('day', a.pay_date, l.booking_date) BETWEEN {DATE_LO} AND {DATE_HI}),
      c AS (SELECT file, bank_txn_id, sum(how) score FROM (SELECT * FROM byref UNION ALL SELECT * FROM byamt) GROUP BY ALL),
      c2 AS (SELECT c.*, abs(date_diff('day', a.pay_date, l.booking_date)) dd FROM c JOIN A a USING (file) JOIN LT l USING (bank_txn_id))
      SELECT * FROM c2
      QUALIFY row_number() OVER (PARTITION BY file ORDER BY score DESC, dd, bank_txn_id) = 1
          AND row_number() OVER (PARTITION BY bank_txn_id ORDER BY score DESC, dd, file) = 1""")

    # ---------------------------------------------------------------- 3. advice line → item
    con.execute("""CREATE TABLE RL AS
      SELECT r.file, r.line_no, al.bank_txn_id, al.score link_score, a.entity,
             regexp_replace(upper(coalesce(r.doc, '')), '[^A-Z0-9]', '', 'g') d_alnum,
             ltrim(regexp_replace(coalesce(r.doc, ''), '[^0-9]', '', 'g'), '0') d_dig,
             regexp_replace(upper(coalesce(r.po, '')), '[^A-Z0-9]', '', 'g') d_po,
             round(r.gross * 100)::BIGINT gross_c, round(coalesce(r.deduction, 0) * 100)::BIGINT ded_c,
             round(coalesce(r.paid, r.gross + coalesce(r.deduction, 0)) * 100)::BIGINT paid_c,
             upper(coalesce(r.reason, '')) reason, r.doc, r.po
      FROM R r JOIN AL al USING (file) JOIN A a USING (file)""")
    con.execute("""CREATE TABLE IE AS SELECT item_id, kind, entity, party_id, currency, total_cents, open_at_start_cents open_c,
                          issue_date, discount_pct, discount_days,
                          replace(doc_norm, ' ', '') k_alnum, doc_digits k_dig, replace(coalesce(po_norm, ''), ' ', '') k_po
                   FROM I WHERE side = 'AR'""")
    con.execute("""CREATE TABLE RC AS
      WITH k AS (
        SELECT rl.file, rl.line_no, i.*, 4 AS kr FROM RL rl JOIN IE i ON i.entity = rl.entity AND i.k_alnum = rl.d_alnum WHERE rl.d_alnum <> ''
        UNION ALL SELECT rl.file, rl.line_no, i.*, 3 FROM RL rl JOIN IE i ON i.entity = rl.entity AND i.k_dig = rl.d_dig WHERE length(rl.d_dig) >= 5
        UNION ALL SELECT rl.file, rl.line_no, i.*, 2 FROM RL rl JOIN IE i ON i.entity = rl.entity AND i.k_po = rl.d_po WHERE length(rl.d_po) >= 5
        UNION ALL SELECT rl.file, rl.line_no, i.*, 1 FROM RL rl JOIN IE i ON i.entity = rl.entity
                  AND length(rl.d_dig) BETWEEN 5 AND 9 AND length(i.k_dig) > length(rl.d_dig) AND right(i.k_dig, length(rl.d_dig)) = rl.d_dig)
      SELECT k.*, rl.gross_c, rl.bank_txn_id,
             (abs(k.total_cents - abs(rl.gross_c)) <= 1)::INT AS amt_ok,
             CASE WHEN rl.gross_c < 0 THEN k.kind = 'ARC' ELSE k.kind = 'ARI' END AS kind_ok
      FROM k JOIN RL rl USING (file, line_no)""")
    # majority party of the advice among the strong (number + amount) candidates
    con.execute("""CREATE TABLE AP AS SELECT file, party_id FROM (SELECT file, party_id, count(*) n FROM RC WHERE amt_ok = 1 AND kr >= 3 GROUP BY file, party_id)
                   QUALIFY row_number() OVER (PARTITION BY file ORDER BY n DESC, party_id) = 1""")
    con.execute("""CREATE TABLE RM AS
      SELECT rc.*, (ap.party_id = rc.party_id)::INT party_ok FROM RC rc LEFT JOIN AP ap USING (file)
      WHERE rc.kind_ok
      QUALIFY row_number() OVER (PARTITION BY file, line_no
                                 ORDER BY amt_ok + coalesce(party_ok, 0) DESC, kr DESC, (open_c > 0) DESC, issue_date DESC, item_id) = 1""")
    # lines without a number hit: the gross amount among the advice party's open items
    con.execute("""INSERT INTO RM
      SELECT rl.file, rl.line_no, i.*, 0 AS kr, rl.gross_c, rl.bank_txn_id, 1 AS amt_ok, true AS kind_ok, 1 AS party_ok
      FROM RL rl JOIN AP ap USING (file)
      JOIN IE i ON i.party_id = ap.party_id AND i.entity = rl.entity AND i.open_c > 0 AND abs(i.total_cents - abs(rl.gross_c)) <= 1
           AND i.kind = CASE WHEN rl.gross_c < 0 THEN 'ARC' ELSE 'ARI' END
      WHERE NOT EXISTS (SELECT 1 FROM RM m WHERE m.file = rl.file AND m.line_no = rl.line_no)
      QUALIFY row_number() OVER (PARTITION BY rl.file, rl.line_no ORDER BY i.issue_date, i.item_id) = 1""")
    # one advice line per item (a duplicated number in the advice keeps the first line)
    con.execute("""CREATE OR REPLACE TABLE RM AS SELECT * FROM RM
                   QUALIFY row_number() OVER (PARTITION BY bank_txn_id, item_id ORDER BY line_no) = 1""")

    # ---------------------------------------------------------------- 4. amount + exception
    prof = withholding(phase)
    WHTP = sql_wht_parties(con, prof)      # customers that may withhold (country of the customer, POLICIES §3)
    wht = "(" + " OR ".join(f"abs(short - round(total_cents * {r})) <= 1" for r in prof["rates"]) + ")"
    con.execute(f"""CREATE TABLE OUTR AS
      WITH x0 AS (
        SELECT m.*, rl.ded_c, rl.paid_c, rl.reason, l.booking_date, l.currency AS lcur,
               greatest(m.open_c, 0) - rl.paid_c AS short,
               m.discount_pct IS NOT NULL AND l.booking_date <= m.issue_date + m.discount_days + 3 AS in_terms
        FROM RM m JOIN RL rl USING (file, line_no) JOIN LT l ON l.bank_txn_id = m.bank_txn_id),
      x AS (SELECT *,
          CASE WHEN kind = 'ARC' THEN 'cn'
               WHEN open_c <= 0 THEN 'dup'
               WHEN short <= greatest(1, 0.005 * open_c) AND ded_c >= 0 THEN 'full'
               WHEN regexp_matches(reason, '{WHT_WORDS}') OR (reason = '' AND party_id IN {WHTP} AND {wht}) THEN 'wht'
               WHEN regexp_matches(reason, '{DISC_WORDS}')
                    OR (reason = '' AND discount_pct IS NOT NULL AND abs(short - round(total_cents * discount_pct / 100)) <= 1)
                    THEN CASE WHEN in_terms THEN 'disc' ELSE 'disc_late' END
               WHEN reason <> '' THEN 'dispute'
               WHEN short > 0 THEN 'partial'
               ELSE 'full' END AS how
        FROM x0)
      SELECT bank_txn_id, item_id,
             CASE WHEN how IN ('dispute', 'partial', 'disc_late', 'dup') THEN paid_c WHEN how = 'cn' THEN total_cents
                  ELSE greatest(open_c, 0) END AS amount_cents,
             CASE WHEN how = 'cn' THEN ''
                  WHEN how = 'dup' THEN 'DUPLICATE_PAYMENT'
                  WHEN how = 'dispute' THEN 'SHORT_DISPUTE'
                  WHEN how IN ('partial', 'disc_late') THEN 'PARTIAL'
                  WHEN currency <> lcur THEN 'FX_DIFFERENCE'
                  WHEN how = 'disc' THEN 'SHORT_DISCOUNT'
                  WHEN how = 'wht' THEN 'SHORT_WITHHOLDING'
                  ELSE '' END AS exception,
             'T6.remit.' || how AS rule, file AS link_key,
             CASE WHEN kr >= 3 AND amt_ok = 1 THEN 0.98 WHEN amt_ok = 1 THEN 0.9 ELSE 0.7 END AS confidence,
             'advice=' || coalesce(file, '?') || '; key_rank=' || coalesce(kr::varchar, '?') || '; amount_ok=' || coalesce(amt_ok::varchar, '?')
                 || '; short=' || coalesce(short::varchar, '?') AS evidence
      FROM x""")
    con.execute(f"COPY (SELECT * FROM OUTR WHERE amount_cents > 0) TO '{w}/remit_links.parquet' (FORMAT parquet)")
    if not os.environ.get("REMIT_NO_MERGE"):
        con.execute(f"""COPY (SELECT * FROM SL0 UNION ALL BY NAME SELECT * FROM OUTR WHERE amount_cents > 0)
                        TO '{side_p}' (FORMAT parquet)""")
    st = con.sql("""SELECT (SELECT count(*) FROM A) advices, (SELECT count(*) FROM AL) linked,
                           (SELECT count(*) FROM RL) adv_lines, (SELECT count(*) FROM RM) resolved,
                           (SELECT count(*) FROM OUTR) pairs""").fetchall()[0]
    print(f"[{phase}] advices {st[0]} · linked to a receipt {st[1]} · advice lines {st[2]} · resolved {st[3]} · pairs {st[4]}")
    print(con.sql("SELECT rule, exception, count(*) n FROM OUTR GROUP BY ALL ORDER BY n DESC").fetchall())
    print(f"side_links + T6 → {side_p} ({time.time() - t0:.0f}s)")


def main():
    safe_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
