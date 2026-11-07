"""T3f · noisy references → side_links rows (rule T3f.*), run after T4 (party) and before T3 (matcher).

    python -m src.fuzzref --phase window|live|final

For receipts not explained by T2/T6: digit tokens of the bank text (single tokens and adjacent pairs joined, ≥ 6
digits, leading zeros dropped) are compared with the digits of the open AR items of the line's T4 candidate parties:
  typo        same length, Damerau-Levenshtein ≤ 1 (one wrong or transposed digit)
  trunc       token is a prefix of the item digits (≥ 6 digits; bank cut at 16/35/38 chars) or the item digits a
              suffix of the token (our number without prefix glued to something)
  po          the token equals the customer PO digits of the item
A line is accepted only if the matched items explain the cash: their signed open amounts sum to the cash (±1 cent),
or a single invoice paid with its early-payment discount within terms. Same currency only. One line per item.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO, safe_stdout

PARTY_MIN = 0.2


def build(phase: str) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase)
    side_p = f"{w}/side_links.parquet"
    con = duckdb.connect()
    con.execute("SET threads=8; SET memory_limit='10GB'")
    con.execute(f"CREATE TABLE SL0 AS SELECT * FROM '{side_p}' WHERE rule NOT LIKE 'T3f.%'")
    con.execute(f"""CREATE TABLE LT AS SELECT bank_txn_id, entity, booking_date, abs(amount_cents) cash, currency, text_norm
                    FROM '{w}/lines.parquet' WHERE direction = 'IN' AND bank_txn_id NOT IN (SELECT bank_txn_id FROM SL0)""")
    # T4 is contractually capped at three candidates.  Keep the explicit rank
    # guard here too: low-confidence candidates beyond the top three can make a
    # coincidental digit string look valid and silently trade precision for
    # recall.
    con.execute(f"""CREATE TABLE P AS SELECT bank_txn_id, party_id, confidence FROM '{w}/line_party.parquet'
                    WHERE confidence >= {PARTY_MIN} AND rank <= 3
                      AND bank_txn_id IN (SELECT bank_txn_id FROM LT)""")
    con.execute(f"""CREATE TABLE IO AS SELECT item_id, kind, party_id, entity, currency, issue_date, total_cents,
                           open_at_start_cents open_c, doc_digits dig, discount_pct, discount_days,
                           ltrim(regexp_replace(doc_digits, '[^0-9]', '', 'g'), '0') dig_norm,
                           ltrim(regexp_replace(coalesce(po_norm, ''), '[^0-9]', '', 'g'), '0') po_dig
                    FROM '{w}/items.parquet'
                    WHERE side = 'AR' AND kind IN ('ARI', 'ARC') AND open_at_start_cents > 0
                      AND item_id NOT IN (SELECT item_id FROM SL0) AND length(doc_digits) >= 6""")
    con.execute("""CREATE TABLE TK AS   -- digits of 1-3 adjacent tokens ("INV23 0211203" -> 230211203)
      WITH t AS (SELECT bank_txn_id, string_split(text_norm, ' ') tok FROM LT),
           g AS (SELECT bank_txn_id, array_to_string(tok[i:i + k - 1], '') x
                 FROM t, LATERAL (SELECT unnest(range(1, least(len(tok), 60) + 1)) i), LATERAL (SELECT unnest([1, 2, 3]) k)
                 WHERE i + k - 1 <= len(tok) AND regexp_matches(tok[i], '[0-9]'))
      SELECT DISTINCT bank_txn_id, ltrim(regexp_replace(x, '[^0-9]', '', 'g'), '0') d FROM g
      WHERE length(ltrim(regexp_replace(x, '[^0-9]', '', 'g'), '0')) BETWEEN 6 AND 14
        AND length(regexp_replace(x, '[0-9]', '', 'g')) <= 6""")
    con.execute("""CREATE TABLE C AS
      WITH pi AS (SELECT p.bank_txn_id, p.confidence pconf, i.* FROM P p JOIN IO i USING (party_id)
                  JOIN LT l USING (bank_txn_id) WHERE i.currency = l.currency AND i.issue_date <= l.booking_date),
      m AS (
        SELECT pi.*, 'typo' how FROM pi JOIN TK t USING (bank_txn_id)
        WHERE length(t.d) = length(pi.dig_norm) AND t.d <> pi.dig_norm AND damerau_levenshtein(t.d, pi.dig_norm) <= 1
        UNION ALL SELECT pi.*, 'trunc' FROM pi JOIN TK t USING (bank_txn_id)
        WHERE (length(t.d) < length(pi.dig_norm) AND starts_with(pi.dig_norm, t.d))
           OR (length(t.d) > length(pi.dig_norm) AND ends_with(t.d, pi.dig_norm))
           OR (length(t.d) >= 6 AND length(t.d) < length(pi.dig_norm) AND ends_with(pi.dig_norm, t.d))
        UNION ALL SELECT pi.*, 'exact' FROM pi JOIN TK t USING (bank_txn_id) WHERE t.d = pi.dig_norm
        UNION ALL SELECT pi.*, 'po' FROM pi JOIN TK t USING (bank_txn_id) WHERE length(pi.po_dig) >= 6 AND t.d = pi.po_dig)
      SELECT bank_txn_id, item_id, any_value(kind) kind, any_value(open_c) open_c, any_value(total_cents) total_cents,
             any_value(issue_date) issue_date, any_value(discount_pct) discount_pct, any_value(discount_days) discount_days,
             max(pconf) pconf, string_agg(DISTINCT how, '+') how
      FROM m GROUP BY bank_txn_id, item_id""")
    con.execute(f"COPY C TO '{w}/fuzz_cands.parquet' (FORMAT parquet)")
    # keep lines where at least one item came from a noisy key (exact-only lines are the matcher's job)
    con.execute("""CREATE TABLE S AS
      SELECT c.bank_txn_id, sum(CASE WHEN kind = 'ARC' THEN -open_c ELSE open_c END) s, count(*) n,
             bool_or(how <> 'exact') noisy, any_value(l.cash) cash, any_value(l.booking_date) bd
      FROM C c JOIN LT l USING (bank_txn_id) GROUP BY 1""")
    con.execute("""CREATE TABLE OK AS
      SELECT c.*, CASE WHEN abs(s.s - s.cash) <= 1 THEN 'sum'
                       ELSE 'disc' END fit
      FROM C c JOIN S s USING (bank_txn_id)
      WHERE s.noisy AND (abs(s.s - s.cash) <= 1
             OR (s.n = 1 AND c.kind = 'ARI' AND c.discount_pct IS NOT NULL AND s.bd <= c.issue_date + c.discount_days + 3
                 AND abs(s.cash - round(c.total_cents * (1 - c.discount_pct / 100))) <= 1 AND c.open_c = c.total_cents))
      QUALIFY row_number() OVER (PARTITION BY item_id ORDER BY pconf DESC, bank_txn_id) = 1""")
    # a line keeps its set only if no item went to another line
    con.execute("""CREATE TABLE OUTR AS
      SELECT bank_txn_id, item_id, open_c amount_cents, CASE WHEN fit = 'disc' THEN 'SHORT_DISCOUNT' ELSE '' END exception,
             'T3f.' || replace(how, '+', '_') || '.' || fit rule, how link_key, round(0.9 * pconf + 0.09, 3) confidence,
             'noisy ref ' || how || '; items sum = cash' evidence
      FROM OK WHERE bank_txn_id NOT IN (SELECT c.bank_txn_id FROM C c JOIN S s USING (bank_txn_id)
                                        WHERE s.noisy AND c.item_id NOT IN (SELECT item_id FROM OK WHERE OK.bank_txn_id = c.bank_txn_id)
                                          AND c.bank_txn_id IN (SELECT bank_txn_id FROM OK))""")
    # stage B: one open item of the entity whose amount = the cash and whose number is close to a text token
    # (no party needed; beats an exact hit on a different item, i.e. a typo that lands on another number)
    con.execute(f"""CREATE TABLE B AS
      WITH c AS (SELECT l.bank_txn_id, i.item_id, i.dig_norm dig, i.po_dig, i.party_id FROM LT l
                 JOIN IO i ON i.entity = l.entity AND i.currency = l.currency AND i.open_c = l.cash AND i.kind = 'ARI'
                      AND i.issue_date <= l.booking_date
                 WHERE l.bank_txn_id NOT IN (SELECT bank_txn_id FROM OUTR)),
      m AS (SELECT c.*, CASE WHEN t.d = c.dig THEN 'exact'
                             WHEN length(t.d) = length(c.dig) AND damerau_levenshtein(t.d, c.dig) <= 1 THEN 'typo'
                             WHEN length(t.d) >= 7 AND length(t.d) < length(c.dig) AND starts_with(c.dig, t.d) THEN 'trunc'
                             WHEN length(t.d) > length(c.dig) AND ends_with(t.d, c.dig) THEN 'trunc'
                             WHEN length(c.po_dig) >= 6 AND t.d = c.po_dig THEN 'po' END how
            FROM c JOIN TK t USING (bank_txn_id))
      SELECT bank_txn_id, item_id, min(how) how, bool_or(party_id IN (SELECT party_id FROM P p WHERE p.bank_txn_id = m.bank_txn_id)) pok
      FROM m WHERE how IS NOT NULL GROUP BY 1, 2""")
    con.execute("""CREATE TABLE B1 AS SELECT * FROM B WHERE how <> 'exact'
                   QUALIFY count(*) OVER (PARTITION BY bank_txn_id) = 1 AND count(*) OVER (PARTITION BY item_id) = 1""")
    con.execute("""INSERT INTO OUTR SELECT b.bank_txn_id, b.item_id, i.open_c, '', 'T3f.amt_' || b.how || '.unique', b.how,
                          CASE WHEN b.pok THEN 0.95 ELSE 0.8 END, 'single open item = cash, number ' || b.how
                   FROM B1 b JOIN IO i USING (item_id) WHERE (b.how <> 'typo' OR b.pok) AND b.item_id NOT IN (SELECT item_id FROM OUTR)""")
    con.execute(f"COPY (SELECT * FROM OUTR) TO '{w}/fuzzref_links.parquet' (FORMAT parquet)")
    if not os.environ.get("FUZZ_NO_MERGE"):
        con.execute(f"COPY (SELECT * FROM SL0 UNION ALL BY NAME SELECT * FROM OUTR) TO '{side_p}' (FORMAT parquet)")
    print(con.sql("SELECT rule, count(DISTINCT bank_txn_id) lines, count(*) pairs FROM OUTR GROUP BY 1 ORDER BY 3 DESC").fetchall())
    print(f"[{phase}] fuzzy refs → {w}/fuzzref_links.parquet ({time.time() - t0:.0f}s)")


def main():
    safe_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
