"""Policy profile learned from TRAIN, with the written policy as fallback (replaces hard-coded entity / rate lists).

    from src.common.profile import withholding, sql_in, sql_wht_match

`withholding(phase)` → {"countries": [...], "entities": [...], "rates": [...]}: the customer countries where
SHORT_WITHHOLDING happens and the rates seen, learned from the TRAIN golden — lines booked before WINDOW_START for
`window`/`sample` (no leakage), all of TRAIN for `live`/`final` — united with POLICIES.md §3 ("some customers in
Mexico, Brazil and Morocco; rates 1.25 %, 1.5 % or 10 %"). The condition is keyed on the **customer's country**
(masters/customers.csv), as the policy says: a Moroccan customer withholds on an ES01 invoice too (TRAIN: 370 such
rows), while a 10 % shortfall on any other customer is PARTIAL (TRAIN: > 3 000 rows). `entities` = the entities of
those countries (masters/entities.csv), used only as a fallback for customers missing from the masters. Nothing is an
entity id in code: a new subsidiary in one of those countries (FINAL masters) is covered through the masters, a new
withholding country seen in TRAIN through learning. Cached in work/learn/policy_<model>.json; without the golden or
duckdb the policy alone is used, so an offline FINAL run never fails here.
"""
from __future__ import annotations

import csv
import json
import os

from src.common import REPO, WINDOW_START, data_root, phase_dir

POLICY_COUNTRIES = ("MX", "BR", "MA")          # POLICIES.md §3 SHORT_WITHHOLDING
POLICY_RATES = (0.0125, 0.015, 0.10)
MIN_ROWS = 20          # a learned entity / country / rate needs this many golden rows
MIN_RATE_SHARE = 0.01  # … and a rate ≥ 1 % of the single-invoice withholding rows


def _model(phase: str) -> str:
    return "pre_window" if phase in ("window", "sample") else "full"


def _entities_of(countries, root: str) -> list[str]:
    path = os.path.join(root, "masters", "entities.csv")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        return sorted(r["entity"] for r in csv.DictReader(fh) if (r.get("country") or "").upper() in countries)


def _learn(model: str, root: str) -> dict:
    """Entities, customer countries and rates of the golden SHORT_WITHHOLDING rows (empty dict when not learnable)."""
    train = phase_dir("train", root)
    golden, bank, inv = (os.path.join(train, f) for f in ("golden_allocations.csv.gz", "bank_transactions.csv.gz", "ar_invoices.csv.gz"))
    cust = os.path.join(root, "masters", "customers.csv")
    if not all(os.path.exists(p) for p in (golden, bank, inv, cust)):
        return {}
    try:
        import duckdb
    except ImportError:
        return {}
    q = lambda p: f"read_csv('{p.replace(os.sep, '/')}', all_varchar=true, header=true, quote='\"', escape='\"', delim=',')"  # noqa: E731
    cut = f"AND b.booking_date < '{WINDOW_START}'" if model == "pre_window" else ""
    con = duckdb.connect()
    try:
        con.execute(f"""CREATE TABLE W AS
          SELECT g.bank_txn_id, b.entity, upper(coalesce(c.country, '')) country, abs(b.amount::DOUBLE) cash,
                 i.total_amount::DOUBLE total, b.currency = i.currency same_cur,
                 count(*) OVER (PARTITION BY g.bank_txn_id) n_rows
          FROM {q(golden)} g JOIN {q(bank)} b USING (bank_txn_id)
          JOIN {q(inv)} i ON i.item_id = g.item_id LEFT JOIN {q(cust)} c ON c.customer_id = i.customer_id
          WHERE g.exception = 'SHORT_WITHHOLDING' {cut}""")
        ents = [e for (e,) in con.execute(f"SELECT entity FROM W GROUP BY 1 HAVING count(*) >= {MIN_ROWS} ORDER BY 1").fetchall()]
        ctys = [c for (c,) in con.execute(f"SELECT country FROM W WHERE country <> '' GROUP BY 1 HAVING count(*) >= {MIN_ROWS} ORDER BY 1").fetchall()]
        rates = [r for (r,) in con.execute(f"""
          SELECT round(1 - cash / total, 4) r FROM W WHERE same_cur AND n_rows = 1 AND total > 0
          GROUP BY 1 HAVING count(*) >= {MIN_ROWS} AND count(*) >= {MIN_RATE_SHARE} * (SELECT count(*) FROM W WHERE same_cur AND n_rows = 1)
          ORDER BY 1""").fetchall()]
    finally:
        con.close()
    return {"entities": ents, "countries": ctys, "rates": [float(r) for r in rates]}


def withholding(phase: str, data: str | None = None, relearn: bool = False) -> dict:
    root = data_root(data)
    model = _model(phase)
    d = os.path.join(REPO, "work", "learn")
    os.makedirs(d, exist_ok=True)
    cache = os.path.join(d, f"policy_{model}.json")
    learned = None
    if not relearn and os.path.exists(cache):
        try:
            with open(cache, encoding="utf-8") as fh:
                learned = json.load(fh).get("withholding")
        except (OSError, ValueError):
            learned = None
    if learned is None:
        try:
            learned = _learn(model, root)
        except Exception as e:  # never fail a phase on the learning step: the policy fallback below still applies
            print(f"[profile] withholding not learned ({type(e).__name__}: {e}); policy fallback only")
            learned = {}
        try:
            with open(cache, "w", encoding="utf-8") as fh:
                json.dump({"model": model, "withholding": learned}, fh, indent=1)
        except OSError:
            pass
    countries = sorted(set(learned.get("countries", [])) | set(POLICY_COUNTRIES))
    entities = _entities_of(countries, root)
    rates = sorted(set(round(float(r), 4) for r in learned.get("rates", [])) | set(POLICY_RATES))
    return {"entities": entities, "countries": countries, "rates": rates, "model": model,
            "learned_entities": learned.get("entities", [])}


def wht_parties(prof: dict, data: str | None = None) -> list[str]:
    """Customer ids that may withhold: country in the profile, or (country unknown) entity of a profile country."""
    path = os.path.join(data_root(data), "masters", "customers.csv")
    if not os.path.exists(path):
        return []
    countries, entities = set(prof["countries"]), set(prof["entities"])
    out = []
    with open(path, encoding="utf-8-sig", newline="") as fh:
        for r in csv.DictReader(fh):
            c = (r.get("country") or "").strip().upper()
            if c in countries or (not c and (r.get("entity") or "") in entities):
                out.append(r["customer_id"])
    return out


def sql_wht_parties(con, prof: dict, data: str | None = None, table: str = "WHTP") -> str:
    """Create table `table`(party_id) with wht_parties() in `con`; returns the subquery to use in `x IN (...)`."""
    con.execute(f"CREATE OR REPLACE TABLE {table} AS SELECT unnest(?::VARCHAR[]) AS party_id", [wht_parties(prof, data)])
    return f"(SELECT party_id FROM {table})"


def sql_in(values) -> str:
    """SQL tuple literal for `x IN (...)`; never empty (an impossible value keeps the SQL valid)."""
    vals = [str(v).replace("'", "''") for v in values] or ["__none__"]
    return "(" + ", ".join(f"'{v}'" for v in vals) + ")"


def sql_wht_match(rates, cash: str, total: str, tol: str = "1") -> str:
    """SQL boolean: `cash` equals `total` net of one of the withholding rates, within `tol` cents."""
    return "(" + " OR ".join(f"abs({cash} - round({total} * (1 - {r}))) <= {tol}" for r in rates) + ")"


def sql_wht_min_diff(rates, cash: str, total: str) -> str:
    """SQL number: smallest |cash − total·(1 − rate)| over the rates (for `<= tolerance` comparisons)."""
    return "least(" + ", ".join(f"abs({cash} - round({total} * (1 - {r})))" for r in rates) + ")"


if __name__ == "__main__":
    import sys
    print(json.dumps(withholding(sys.argv[1] if len(sys.argv) > 1 else "window", relearn="--relearn" in sys.argv), indent=1))
