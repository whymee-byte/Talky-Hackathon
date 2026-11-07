"""Show wrong lines of a segment: python src/diag.py out/sub_sample.csv.gz sample ar_1toN_text [n]"""
import sys, pandas as pd
sub, phase, seg = sys.argv[1], sys.argv[2], sys.argv[3]
n = int(sys.argv[4]) if len(sys.argv) > 4 else 5
R = "data/bankrec-docs-and-masters/" + phase
g = pd.read_csv(f"{R}/golden_allocations.csv.gz", dtype=str, keep_default_na=False)
s = pd.read_csv(sub, dtype=str, keep_default_na=False)
bt = pd.read_csv(f"{R}/bank_transactions.csv.gz", dtype=str, keep_default_na=False).set_index("bank_txn_id")
items = {}
for f, doc, party in (("ar_invoices", "document_number", "customer_name"), ("ap_invoices", "supplier_invoice_number", "supplier_name"), ("credit_notes", "document_number", "party_name")):
    df = pd.read_csv(f"{R}/{f}.csv.gz", dtype=str, keep_default_na=False)
    for r in df.itertuples():
        items[r.item_id] = f"{getattr(r, doc)} {r.issue_date} {getattr(r, party)[:30]} {r.currency} {r.total_amount} {getattr(r, 'payment_terms', '')}"
gs = g[g.segment == seg]
bad = 0
for b, grp in gs.groupby("bank_txn_id", sort=False):
    gold = set((r.item_id, r.amount, r.category, r.exception) for r in grp.itertuples())
    mine = set((r.item_id, r.amount, r.category, r.exception) for r in s[s.bank_txn_id == b].itertuples())
    if gold == mine:
        continue
    bad += 1
    if bad > n:
        continue
    t = bt.loc[b]
    print(f"=== {b} {t.account_id} {t.booking_date} {t.amount} {t.currency} orig={t.orig_amount}{t.orig_currency} code={t.bank_code}")
    print(f"    {t.text} || {t.counterparty_name} {t.counterparty_account} || {t.remittance_info} || {t.ref1} {t.ref2}")
    for x in sorted(gold):
        print("   G", x, "OK" if x in mine else "", items.get(x[0], ""))
    for x in sorted(mine - gold):
        print("   M", x, items.get(x[0], ""))
print("bad", bad, "of", gs.bank_txn_id.nunique())
