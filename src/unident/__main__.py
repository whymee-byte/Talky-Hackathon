"""T7u · category + UNIDENTIFIED exception of the lines that ended with no item (POLICIES §3 UNIDENTIFIED, §4).

    python -m src.unident --phase window|live|final [--src allocations.parquet] [--dst allocations.parquet]

Runs after every other post-step; rewrites only the no-item rows of work/<phase>/allocations.parquet.
One learned table, nothing hard-coded:
  tokens  (direction, word of >= 4 letters of the bank text) -> majority no-item category in the TRAIN golden and its
          share among all lines with that word (items count as a class). Learned from TRAIN before the window for
          window/sample (no leakage), from all of TRAIN otherwise. A word "decides" if n >= TOK_MIN_N, share >= TOK_SHARE.
Receipts with category UNIDENTIFIED (T7 or the T3 fallback):
  token.<CAT>   a deciding word names another category (proforma -> CUSTOMER_ADVANCE, supplier refund…) -> that, no exc
  token.unid    a deciding word says UNIDENTIFIED (cash deposit, ATM…)                                   -> UNIDENTIFIED
  no_party      T4 found no party at all                                                                  -> UNIDENTIFIED
  weak_party    T4 only has a weak name key (10-char prefix, fuzzy, short name): unidentified unless the T3
                fallback line pays exactly the total of one of that party's invoices                     -> UNIDENTIFIED
  name_text     T4 has a 16/24-char name key: unidentified only if the T7 text model said UNIDENTIFIED
  strong_party  IBAN / mandate / document reference / exact amount: never UNIDENTIFIED (an item was missed)
  untagged rows keep category UNIDENTIFIED with no exception, or SUPPLIER_REFUND when the party is a supplier.
Debits left as T3 fallback BANK_FEES: a deciding word -> its category; else best party a customer (T4 >= 0.5)
  -> CUSTOMER_REFUND (POLICIES §4: refund of a duplicate payment).
Window 2024-07 -> 2025-06: see comms/board/javier.md.
"""
from __future__ import annotations

import argparse
import os
import time

import duckdb

from src.common import REPO
from src.party.common import WINDOW_START, src

TOK_MIN_N = 50       # a word must appear on >= 50 TRAIN lines to decide
TOK_SHARE = 0.9      # … and >= 90 % of them carry that category
PARTY_MIN = 0.5      # T4 confidence for "the party is known" (same as src.match UNIDENT_MAX_PARTY)
WEAK = ("%L10", "fuzzy%", "%short")                       # T4 method suffixes/prefixes of weak name keys
STRONG = ("iban%", "mandate%", "doc_ref%", "amount%")     # T4 methods that identify the party for sure
WORDS = r"""list_distinct(list_filter(string_split(regexp_replace(upper(strip_accents(coalesce({t}, ''))),
    '[^A-Z]+', ' ', 'g'), ' '), lambda x: length(x) >= 4))"""


def likes(col: str, pats) -> str:
    return "(" + " OR ".join(f"{col} LIKE '{p}'" for p in pats) + ")"


def learn(con, model: str) -> str:
    d = os.path.join(REPO, "work", "unident_model", model)
    path = f"{d}/tokens.parquet"
    if os.path.exists(path):
        return path
    os.makedirs(d, exist_ok=True)
    cut = f"WHERE b.booking_date < DATE '{WINDOW_START}'" if model == "pre_window" else ""
    con.execute(f"""COPY (
      WITH g AS (SELECT bank_txn_id, max(CASE WHEN coalesce(item_id, '') = '' THEN category END) cat
                 FROM {src('train', 'golden_allocations')} GROUP BY 1),
           t AS (SELECT CASE WHEN b.amount > 0 THEN 'IN' ELSE 'OUT' END dir, coalesce(g.cat, '<ITEM>') cat,
                        unnest({WORDS.format(t="concat_ws(' ', b.text, b.ref1, b.ref2, b.remittance_info)")}) tok
                 FROM {src('train', 'bank_transactions')} b JOIN g USING (bank_txn_id) {cut}),
           c AS (SELECT dir, tok, cat, count(*) nc FROM t GROUP BY ALL)
      SELECT dir, tok, sum(nc)::BIGINT n, arg_max(cat, nc) cat, max(nc) / sum(nc) share FROM c GROUP BY 1, 2
      HAVING sum(nc) >= {TOK_MIN_N}) TO '{path}' (FORMAT parquet)""")
    return path


def build(phase: str, src_name: str, dst_name: str) -> None:
    t0 = time.time()
    w = os.path.join(REPO, "work", phase)
    con = duckdb.connect()
    con.execute(f"SET temp_directory='{REPO}/work/tmp'; SET preserve_insertion_order=false")
    tok = learn(con, "pre_window" if phase in ("window", "sample") else "full")
    con.execute(f"CREATE TABLE A AS SELECT * FROM '{w}/{src_name}'")
    con.execute(f"""CREATE TABLE TK AS SELECT dir, tok, cat, share, n FROM '{tok}'
                    WHERE share >= {TOK_SHARE} AND cat <> '<ITEM>'""")
    lp = f"{w}/line_party.parquet"
    party = (f"""SELECT bank_txn_id, max(confidence) pc, arg_max(method, confidence) pm, arg_max(party_id, confidence) party
                 FROM '{lp}' GROUP BY 1""" if os.path.exists(lp) else
             "SELECT NULL::VARCHAR bank_txn_id, NULL::DOUBLE pc, NULL::VARCHAR pm, NULL::VARCHAR party WHERE false")
    # no-item rows in scope + their features
    con.execute(f"""CREATE TABLE N AS
      SELECT a.bank_txn_id, a.category, coalesce(a.exception, '') exc, a.rule old_rule, l.direction, l.currency,
             l.booking_date bd, abs(l.amount_cents) cash, l.text_norm, coalesce(p.pc, 0) pc, coalesce(p.pm, 'none') pm, p.party
      FROM A a JOIN '{w}/lines.parquet' l USING (bank_txn_id) LEFT JOIN ({party}) p USING (bank_txn_id)
      WHERE a.item_id IS NULL AND ((l.direction = 'IN' AND a.category = 'UNIDENTIFIED')
                                OR (l.direction = 'OUT' AND a.category = 'BANK_FEES' AND a.rule LIKE 'T3.none%'))""")
    # best deciding word of each line
    con.execute(f"""CREATE TABLE NT AS
      SELECT n.bank_txn_id, arg_max(tk.cat, tk.share * 1e6 + tk.n) tcat, arg_max(tk.tok, tk.share * 1e6 + tk.n) ttok,
             max(tk.share) tshare
      FROM (SELECT bank_txn_id, direction, unnest({WORDS.format(t='text_norm')}) tok FROM N) n
      JOIN TK tk ON tk.dir = n.direction AND tk.tok = n.tok GROUP BY 1""")
    # does the party have an invoice whose total is the cash (a likely missed match, not an unidentified receipt)?
    con.execute(f"""CREATE TABLE EQ AS
      SELECT DISTINCT n.bank_txn_id FROM N n JOIN '{w}/items.parquet' i
        ON i.party_id = n.party AND i.kind = 'ARI' AND i.currency = n.currency AND i.issue_date <= n.bd
       AND abs(i.total_cents - n.cash) <= greatest(1, 0.005 * i.total_cents)
      WHERE n.direction = 'IN'""")
    con.execute(f"""CREATE TABLE D AS
      WITH f AS (
        SELECT n.*, nt.tcat, nt.ttok, nt.tshare, eq.bank_txn_id IS NOT NULL has_eq,
               CASE WHEN n.pm = 'none' THEN 'none' WHEN {likes('n.pm', STRONG)} THEN 'strong'
                    WHEN {likes('n.pm', WEAK)} THEN 'weak' ELSE 'name' END mk
        FROM N n LEFT JOIN NT nt USING (bank_txn_id) LEFT JOIN EQ eq USING (bank_txn_id)),
      r AS (
        SELECT *, CASE
          WHEN direction = 'OUT' AND tcat IS NOT NULL THEN 'token.' || tcat
          WHEN direction = 'OUT' AND party LIKE 'C%' AND pc >= {PARTY_MIN} THEN 'customer_refund'
          WHEN direction = 'OUT' THEN NULL
          WHEN tcat = 'UNIDENTIFIED' THEN 'token.unid'
          WHEN tcat IS NOT NULL THEN 'token.' || tcat
          WHEN mk = 'none' THEN 'no_party'
          WHEN mk = 'weak' AND NOT (has_eq AND old_rule LIKE 'T3.none%') THEN 'weak_party'
          WHEN mk = 'name' AND old_rule NOT LIKE 'T3.none%' THEN 'name_text'
          WHEN party LIKE 'S%' THEN 'supplier_refund'
          ELSE 'known_party.' || mk END how
        FROM f)
      SELECT bank_txn_id, how,
             CASE WHEN how IN ('token.unid', 'no_party', 'weak_party', 'name_text') THEN 'UNIDENTIFIED'
                  WHEN how LIKE 'token.%' THEN tcat WHEN how = 'customer_refund' THEN 'CUSTOMER_REFUND'
                  WHEN how = 'supplier_refund' THEN 'SUPPLIER_REFUND' ELSE category END new_cat,
             CASE WHEN how IN ('token.unid', 'no_party', 'weak_party', 'name_text') THEN 'UNIDENTIFIED' ELSE '' END new_exc,
             CASE WHEN how LIKE 'token.%' THEN 0.5 + 0.4 * tshare WHEN how = 'no_party' THEN 0.9
                  WHEN how IN ('weak_party', 'name_text') THEN 0.6 ELSE 0.5 END conf,
             'T4 ' || mk || ' ' || pm || ' conf=' || round(pc, 2) || coalesce(' party=' || party, '')
               || CASE WHEN has_eq THEN '; party invoice total = cash' ELSE '' END
               || coalesce('; word ' || ttok || ' -> ' || tcat || ' (' || round(tshare, 2) || ')', '')
               || '; was ' || old_rule ev
      FROM r WHERE how IS NOT NULL""")
    con.execute("""UPDATE A SET category = d.new_cat, exception = d.new_exc, rule = 'T7u.' || d.how,
                                confidence = d.conf, evidence = d.ev
                   FROM D d WHERE A.bank_txn_id = d.bank_txn_id AND A.item_id IS NULL""")
    out = f"{w}/{dst_name}"
    con.execute(f"COPY A TO '{out}.tmp' (FORMAT parquet)")
    os.replace(f"{out}.tmp", out)
    print(con.sql("""SELECT how, new_cat, new_exc, count(*) n FROM D GROUP BY ALL ORDER BY n DESC""").df().to_string(index=False))
    print(f"T7u → {out} ({time.time() - t0:.0f}s)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True)
    ap.add_argument("--src", default="allocations.parquet")
    ap.add_argument("--dst", default="allocations.parquet")
    a = ap.parse_args()
    build(a.phase, a.src, a.dst)


if __name__ == "__main__":
    main()
