"""B: time each SQL statement of the card / cheque steps (subset q1 or full window)."""
import sys, time, re, duckdb
from src.sidefiles import __main__ as m, deposits
subset = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1] != 'full' else None
class P:
    def __init__(s, c): s.c = c
    def sql(s, q, *a):
        t = time.time(); r = s.c.sql(q, *a); dt = time.time() - t
        name = re.search(r'table (\w+)', q); print(f"  {dt:6.1f}s {name.group(1) if name else q[:40]}", flush=True); return r
    def __getattr__(s, k): return getattr(s.c, k)
con = duckdb.connect(); m.setup(con, 'window', None, subset); d = m.detail_dirs('window', None); p = P(con)
t = time.time(); deposits.step_card(p, m.glob_csv(d, 'card_settlements')); print(f"card {time.time()-t:.0f}s")
t = time.time(); deposits.step_cheque(p, m.glob_csv(d, 'cheque_deposits'), m.resolve_receipts); print(f"chq {time.time()-t:.0f}s")
