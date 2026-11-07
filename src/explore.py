import pandas as pd, sys
pd.set_option('display.width',250); pd.set_option('display.max_colwidth',120); pd.set_option('display.max_columns',30)
D=sys.argv[1]
bt=pd.read_csv(f'{D}/bank_transactions.csv.gz',dtype=str)
g=pd.read_csv(f'{D}/golden_allocations.csv.gz',dtype=str)
print(bt.shape,g.shape); print(bt.columns.tolist()); print(g.columns.tolist())
print(g.exception.value_counts(dropna=False)); print(g.category.value_counts(dropna=False).head(25))
seg=g.groupby('bank_txn_id').segment.first().value_counts(); print(seg.to_string())
