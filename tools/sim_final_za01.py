"""Build a simulated FINAL phase (data/final/) from LIVE: entity AU01 becomes the new subsidiary ZA01.

Not a scoring tool (LIVE has no golden): it measures robustness of party/item coverage on a new subsidiary.
  - account AU01-WATT-* -> ZA01-UMVC-*, bank UMVC, currency AUD -> ZAR, entity AU01 -> ZA01 (all files, paths too)
  - customer/supplier/group ids CAU01/SAU01/GAU01 -> CZA01/SZA01/GZA01 (no TRAIN history for them)
  - AU01 AR document numbers INV/WEB -> KWI... (everywhere: invoices, open items, bank texts, side files)
  - receipts move to an MT940-like layout without counterparty_name: '/ORDP/<NAME[:35]>/REMI/<remi>' or
    'EFT CREDIT <NAME[:16]> <remi>'
  - ~50 % of ZA01 customers are renamed on RENAME_DATE: masters_final has the new name only; invoices issued
    from that date print the new name; bank lines booked from that date carry the new name
  - masters_final/ = masters/ relabelled the same way (+ entity ZA01, ZAR fx = AUD fx)

    .venv/bin/python tools/sim_final_za01.py            # writes data/final/ (live data must be in data/live)
"""
import csv
import gzip
import hashlib
import io
import os
import re
import shutil
import sys

import duckdb

SRC = "data/live"
DST = "data/final"
RENAME_DATE = "2025-10-01"

GENERIC = [
    (re.compile(r"AU01-WATT-AUD"), "ZA01-UMVC-ZAR"),
    (re.compile(r"AU01-WATT-USD"), "ZA01-UMVC-USD"),
    (re.compile(r"\b([CSG])AU01(\d)"), r"\1ZA01\2"),
    (re.compile(r"(?<![A-Z0-9])AU01(?![0-9])"), "ZA01"),   # also AU01_202507.csv
    (re.compile(r"\bAUD\b"), "ZAR"),
    (re.compile(r"\blive/"), "final/"),
]
ITEM_RE = re.compile(r"\b(AR[IC]|AP[IC])-2(\d{3})-(\d{7})\b")
DOC_RE = re.compile(r"\b(INV|WEB)(\d{2})-?(\d{7})\b")
A = ["Ubuntu", "Kgotso", "Thaba", "Ithemba", "Lesedi", "Amandla", "Sizwe", "Nandi", "Mzansi", "Karoo", "Umhlanga",
     "Drakens", "Kalahari", "Limpopo", "Protea", "Fynbos", "Baobab", "Sable", "Impala", "Marula"]
B = ["Aqua", "Pool", "Leisure", "Outdoor", "Spa", "Water", "Garden", "Splash", "Blue", "Coastal"]
C = ["Holdings (Pty) Ltd", "Trading (Pty) Ltd", "Group (Pty) Ltd", "Solutions CC", "Distributors (Pty) Ltd"]


def h(s, n):
    return int(hashlib.md5(s.encode()).hexdigest()[:8], 16) % n


SYL = ["ba", "ka", "la", "ma", "na", "pa", "sa", "ta", "wa", "zi", "be", "ko", "lu", "mo", "nu", "ti", "vu", "ya"]


def new_name(cid):
    """Unique per customer: a pseudo-word from the id + two common words + a legal form."""
    n = int(cid[5:])
    w = "".join(SYL[(n // len(SYL) ** i) % len(SYL)] for i in range(3)).capitalize()
    return f"{A[h(cid + 'a', len(A))]} {w} {B[h(cid + 'b', len(B))]} {C[h(cid + 'c', len(C))]}"


def docmap(con):
    rows = con.sql(f"""SELECT DISTINCT document_number FROM read_csv('{SRC}/ar_invoices.csv.gz') WHERE entity='AU01'
        UNION SELECT document_number FROM read_csv('{SRC}/open_items_at_start.csv.gz')
              WHERE entity='AU01' AND item_type LIKE 'AR%'""").fetchall()
    m = {}
    for (d,) in rows:
        x = DOC_RE.fullmatch(d or "")
        if x:
            m[f"{x[1]}{x[2]}-{x[3]}"] = ("KWI" if x[1] == "INV" else "KWIW") + x[2] + x[3]
    return m


ITEMS = set()   # AU01 item ids: renumbered (ARI-2025-x -> ARI-Z025-x) so that TRAIN copies of them do not apply


def sub_text(s, dm):
    for r, t in GENERIC:
        s = r.sub(t, s)
    if ITEMS:
        s = ITEM_RE.sub(lambda x: f"{x[1]}-Z{x[2]}-{x[3]}" if x[0] in ITEMS else x[0], s)
    return DOC_RE.sub(lambda x: dm.get(f"{x[1]}{x[2]}-{x[3]}", x[0]), s)


def copy_tree(dm):
    for root, dirs, files in os.walk(SRC):
        for f in files:
            sp = os.path.join(root, f)
            rel = os.path.relpath(sp, SRC)
            dp = os.path.join(DST, sub_text(rel, {}))   # (paths carry no item ids)
            os.makedirs(os.path.dirname(dp), exist_ok=True)
            ext = f.lower().rsplit(".", 1)[-1]
            if ext in ("csv", "txt", "xml", "sta", "mt940", "n43", "json") or f.endswith(".csv.gz"):
                op = gzip.open if f.endswith(".gz") else open
                with op(sp, "rt", encoding="utf-8", errors="surrogateescape", newline="") as fi:
                    data = fi.read()
                with op(dp, "wt", encoding="utf-8", errors="surrogateescape", newline="") as fo:
                    fo.write(sub_text(data, dm))
            else:
                shutil.copyfile(sp, dp)   # pdf/xlsx: content unchanged (old ids/numbers inside)


def masters(con, renamed):
    md = f"{DST}/masters_final"
    os.makedirs(md, exist_ok=True)
    for f in os.listdir("data/masters"):
        with open(f"data/masters/{f}", encoding="utf-8") as fi:
            data = sub_text(fi.read(), {})
        if f == "customers.csv":
            rows = list(csv.reader(io.StringIO(data)))
            hd = rows[0]
            ic, iname = hd.index("customer_id"), hd.index("name")
            for r in rows[1:]:
                if r[ic] in renamed:
                    r[iname] = renamed[r[ic]]
            buf = io.StringIO()
            csv.writer(buf, lineterminator="\n").writerows(rows)
            data = buf.getvalue()
        if f == "entities.csv" and "\nZA01," not in data:
            data += 'ZA01,"Valtermia South Africa (Pty) Ltd",ZA,ZAR,commercial,2026-01-01\n'
        if f == "bank_accounts.csv" and "ZA01-UMVC-ZAR," not in data:
            data += "ZA01-UMVC-ZAR,ZA01,UMVC,Umvoti Bank,UMVCZAJJXXX,ZAR,,,Cobros,mt940,,2025-12-01\n"
        with open(f"{md}/{f}", "w", encoding="utf-8") as fo:
            fo.write(data)


def main():
    if os.path.exists(DST):
        shutil.rmtree(DST)
    con = duckdb.connect()
    dm = docmap(con)
    for t in ("ar_invoices", "ap_invoices", "credit_notes", "open_items_at_start"):
        ITEMS.update(r[0] for r in con.sql(f"SELECT item_id FROM read_csv('{SRC}/{t}.csv.gz', all_varchar=true) "
                                           "WHERE entity = 'AU01'").fetchall())
    print(f"AU01 items renumbered: {len(ITEMS)}")
    print(f"AU01 AR documents -> KWI: {len(dm)}")
    copy_tree(dm)
    # renames: ~50 % of ZA01 customers
    with open("data/masters/customers.csv", encoding="utf-8") as f:
        cust = [(r["customer_id"],) for r in csv.DictReader(f) if r["entity"] == "AU01"]
    renamed = {}
    for (cid,) in cust:
        nid = "CZA01" + cid[5:]
        if h(nid, 2) == 0:
            renamed[nid] = new_name(nid)
    print(f"ZA01 customers: {len(cust)}, renamed: {len(renamed)}")
    masters(con, renamed)
    con.execute("CREATE TABLE rn (customer_id VARCHAR, new_name VARCHAR)")
    con.executemany("INSERT INTO rn VALUES (?, ?)", list(renamed.items()))
    # invoices from RENAME_DATE print the new name
    p = f"{DST}/ar_invoices.csv.gz"
    con.execute(f"""COPY (SELECT a.* REPLACE (CASE WHEN rn.new_name IS NOT NULL AND CAST(a.issue_date AS DATE) >= DATE '{RENAME_DATE}'
                                     THEN rn.new_name ELSE a.customer_name END AS customer_name)
                         FROM read_csv('{p}', all_varchar=true) a LEFT JOIN rn USING (customer_id))
                    TO '{p}.tmp' (FORMAT csv, HEADER, COMPRESSION gzip)""")
    os.replace(f"{p}.tmp", p)
    # bank lines: old name -> new name from RENAME_DATE (via the customer's printed name), then MT940-like layout
    con.execute(f"""CREATE TABLE oldnm AS SELECT DISTINCT a.customer_id, upper(a.customer_name) AS old, upper(rn.new_name) AS new
        FROM read_csv('{SRC}/ar_invoices.csv.gz') a JOIN rn ON rn.customer_id = replace(a.customer_id, 'CAU01', 'CZA01')
        WHERE a.entity = 'AU01'""")
    b = f"{DST}/bank_transactions.csv.gz"
    con.execute(f"CREATE TABLE bt AS SELECT * FROM read_csv('{b}', all_varchar=true)")
    con.execute(f"""UPDATE bt SET counterparty_name = o.new
        FROM oldnm o WHERE bt.entity = 'ZA01' AND CAST(bt.booking_date AS DATE) >= DATE '{RENAME_DATE}'
          AND upper(bt.counterparty_name) = o.old""")
    con.execute("""UPDATE bt SET
        bank = 'UMVC', statement_format = 'mt940',
        text = CASE WHEN hash(bank_txn_id) % 3 = 0
                    THEN 'EFT CREDIT ' || left(upper(counterparty_name), 16) || ' ' || coalesce(remittance_info, '')
                    ELSE '/ORDP/' || left(upper(counterparty_name), 35) || '/REMI/' || coalesce(remittance_info, '') END,
        counterparty_name = NULL, remittance_info = NULL, end_to_end_id = NULL
        WHERE entity = 'ZA01' AND counterparty_name IS NOT NULL AND CAST(amount AS DOUBLE) > 0""")
    con.execute("UPDATE bt SET bank = 'UMVC' WHERE entity = 'ZA01'")
    con.execute(f"COPY bt TO '{b}.tmp' (FORMAT csv, HEADER, COMPRESSION gzip)")
    os.replace(f"{b}.tmp", b)
    print(con.sql("SELECT account_id, count(*) n, count(counterparty_name) n_cp FROM bt WHERE entity='ZA01' GROUP BY 1").df())


if __name__ == "__main__":
    sys.exit(main())
