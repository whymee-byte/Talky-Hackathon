import pandas as pd, sys
D=sys.argv[1]; segs=sys.argv[2].split(','); n=int(sys.argv[3]) if len(sys.argv)>3 else 3
bt=pd.read_csv(f'{D}/bank_transactions.csv.gz',dtype=str).set_index('bank_txn_id')
g=pd.read_csv(f'{D}/golden_allocations.csv.gz',dtype=str)
ar=pd.read_csv(f'{D}/ar_invoices.csv.gz',dtype=str); ap=pd.read_csv(f'{D}/ap_invoices.csv.gz',dtype=str); cn=pd.read_csv(f'{D}/credit_notes.csv.gz',dtype=str)
it={}
for r in ar.itertuples(): it[r.item_id]=f"AR {r.document_number} {r.issue_date} {r.customer_id} {r.customer_name} {r.currency} {r.total_amount} po={r.customer_po} pm={r.payment_method} terms={r.payment_terms}"
for r in ap.itertuples(): it[r.item_id]=f"AP {r.supplier_invoice_number} {r.issue_date} {r.supplier_id} {r.supplier_name} {r.currency} {r.total_amount} po={r.our_po}"
for r in cn.itertuples(): it[r.item_id]=f"CN {r.side} {r.document_number} {r.issue_date} {r.party_id} {r.party_name} {r.currency} {r.total_amount} rel={r.related_document}"
for s in segs:
  print('#'*10,s)
  ids=g[g.segment==s].bank_txn_id.drop_duplicates().head(n)
  for b in ids:
    t=bt.loc[b]
    print(f"{b} {t.account_id} {t.booking_date} {t.amount} {t.currency} code={t.bank_code}/{t.bank_subcode} ref1={t.ref1} ref2={t.ref2} orig={t.orig_amount}{t.orig_currency}")
    print("   TEXT:",t.text,"| CP:",t.counterparty_name,t.counterparty_account,"| E2E:",t.end_to_end_id,"| RI:",t.remittance_info)
    for r in g[g.bank_txn_id==b].head(6).itertuples(): print("   ->",r.item_id,r.amount,r.category,r.exception,"::",it.get(r.item_id,''))
    k=(g.bank_txn_id==b).sum()
    if k>6: print("   ... total rows",k)
