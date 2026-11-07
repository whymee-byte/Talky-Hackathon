"""T5 exception decision engine. Vectorized DuckDB SQL, no row-wise Python processing."""
from __future__ import annotations

from typing import Any

import duckdb
import polars as pl


OUTPUT = ["bank_txn_id", "item_id", "amount_cents", "category", "exception", "rule", "confidence", "evidence"]


def _frame(value: Any) -> pl.DataFrame:
    if isinstance(value, pl.DataFrame):
        return value
    if isinstance(value, pl.LazyFrame):
        return value.collect()
    if isinstance(value, duckdb.DuckDBPyRelation):
        return pl.from_arrow(value.arrow())
    if hasattr(value, "to_arrow"):
        return pl.from_arrow(value.to_arrow())
    return pl.DataFrame(value)


def decide(pairs_raw, lines, items, customers, fx) -> pl.DataFrame:
    """Return one final §8 decision per input pair.

    Inputs are contract DataFrames. Extra T3/T6 evidence (``doc_hint``) is preserved and
    interpreted only when it carries an explicit ``DISPUTE:`` reason. All bulk logic is SQL.
    The function has no filesystem or global-state effects.
    """
    frames = {"p": _frame(pairs_raw), "l": _frame(lines), "i": _frame(items),
              "cu": _frame(customers), "fx0": _frame(fx)}
    con = duckdb.connect(":memory:")
    for name, frame in frames.items():
        con.register(name, frame.to_arrow())
    # T3 builds may carry *_cents naming; these projections tolerate contract-compatible extras.
    pcols, lcols, icols, ccols, fcols = (set(x.columns) for x in frames.values())
    required = {"bank_txn_id", "item_id", "booking_date", "line_amount_cents", "line_currency",
                "item_currency", "open_before_cents", "cash_cents", "rule", "confidence", "evidence"}
    missing = required - pcols
    if missing:
        raise ValueError(f"pairs_raw missing contract columns: {sorted(missing)}")
    if "kind" not in icols or "total_cents" not in icols:
        raise ValueError("items must include kind and total_cents")
    # Nullable source columns are added with SQL defaults, keeping T3/T6 version drift harmless.
    for name, cols, additions in (
        ("p", pcols, {"doc_hint": "VARCHAR"}),
        ("l", lcols, {"bank_code": "VARCHAR", "bank_subcode": "VARCHAR", "statement_format": "VARCHAR",
                      "text_norm": "VARCHAR", "orig_amount_cents": "BIGINT", "orig_currency": "VARCHAR",
                      "direction": "VARCHAR", "entity": "VARCHAR"}),
        ("i", icols, {"issue_date": "DATE", "payment_terms": "VARCHAR", "discount_pct": "DOUBLE",
                      "discount_days": "INTEGER", "party_id": "VARCHAR", "currency": "VARCHAR"}),
        ("cu", ccols, {"country": "VARCHAR"}),
        ("fx0", fcols, {"date": "DATE", "currency": "VARCHAR", "units_per_eur": "DOUBLE"}),
    ):
        for col, typ in additions.items():
            if col not in cols:
                con.execute(f"ALTER TABLE {name} ADD COLUMN {col} {typ}") if False else None
        # Register as temp tables so optional columns can be safely appended.
        if any(col not in cols for col in additions):
            con.unregister(name)
            con.register(name, frames[name].to_arrow())
            selects = [f'"{c}"' for c in frames[name].columns]
            selects += [f"NULL::{typ} AS \"{col}\"" for col, typ in additions.items() if col not in cols]
            con.execute(f'CREATE TEMP TABLE {name}_x AS SELECT {", ".join(selects)} FROM {name}')
            con.unregister(name)
            con.execute(f"ALTER TABLE {name}_x RENAME TO {name}")
    # Currency reference table includes EUR and uses the last available rate on/before booking date.
    con.execute("""CREATE TEMP TABLE FX AS
        SELECT CAST(date AS DATE) d, upper(currency) currency, CAST(units_per_eur AS DOUBLE) u FROM fx0
        WHERE date IS NOT NULL AND currency IS NOT NULL AND units_per_eur IS NOT NULL
        UNION ALL SELECT DISTINCT CAST(date AS DATE), 'EUR', 1.0 FROM fx0 WHERE date IS NOT NULL""")
    con.execute("""CREATE TEMP TABLE B AS
      SELECT p.*, l.amount_cents AS actual_line_cents, l.booking_date AS line_booking_date,
        l.currency AS actual_line_currency, l.orig_amount_cents, l.orig_currency, l.bank_code,
        l.bank_subcode, l.statement_format, l.text_norm, l.direction, l.entity,
        i.kind, i.total_cents, i.issue_date, i.payment_terms, i.discount_pct, i.discount_days,
        i.party_id, coalesce(i.currency, p.item_currency) item_cur, fi.u AS item_units_per_eur,
        cu.country, abs(p.line_amount_cents)::BIGINT line_abs,
        CASE WHEN p.open_before_cents < 0 THEN 0 ELSE p.open_before_cents END open_c,
        CASE WHEN (i.kind='ARC' AND l.direction='IN') OR (i.kind='APC' AND l.direction='OUT') THEN -1 ELSE 1 END AS sgn,
        CASE WHEN upper(p.line_currency)=upper(coalesce(i.currency,p.item_currency)) THEN abs(p.line_amount_cents)::DOUBLE
             WHEN upper(l.orig_currency)=upper(coalesce(i.currency,p.item_currency)) AND l.orig_amount_cents IS NOT NULL
               THEN abs(l.orig_amount_cents)::DOUBLE
             ELSE abs(p.line_amount_cents)::DOUBLE / NULLIF(fr.u,0) * fi.u END cash_i,
        abs(p.cash_cents)::DOUBLE applied_i,
        coalesce(p.doc_hint,'') AS hint
      FROM p JOIN l USING (bank_txn_id) JOIN i USING (item_id)
      LEFT JOIN cu ON cu.customer_id=i.party_id
      ASOF LEFT JOIN FX fr ON fr.currency=upper(l.currency) AND fr.d<=p.booking_date
      ASOF LEFT JOIN FX fi ON fi.currency=upper(coalesce(i.currency,p.item_currency)) AND fi.d<=p.booking_date""")
    # Ensure an explicitly provided booking date (rather than duplicated joined line field) drives FX.
    con.execute("""CREATE TEMP TABLE G AS SELECT *,
       count(*) OVER (PARTITION BY bank_txn_id,item_cur) n_pair,
       count(DISTINCT item_cur) OVER (PARTITION BY bank_txn_id) n_currency,
       sum(sgn*open_c) OVER (PARTITION BY bank_txn_id,item_cur) signed_open_sum,
       sum(CASE WHEN sgn=1 THEN open_c ELSE 0 END) OVER (PARTITION BY bank_txn_id) invoice_open_sum,
       sum(CASE WHEN sgn=-1 THEN open_c ELSE 0 END) OVER (PARTITION BY bank_txn_id, item_cur) credit_open_sum,
       sum(CASE WHEN kind='ARI' AND discount_pct IS NOT NULL AND discount_days IS NOT NULL
                     AND booking_date <= issue_date + CAST(discount_days AS INTEGER) + 3
                THEN total_cents-round(total_cents*discount_pct/100) ELSE sgn*open_c END)
           OVER (PARTITION BY bank_txn_id,item_cur) discount_expected,
       sum(CASE WHEN kind='ARI' THEN total_cents ELSE 0 END) OVER (PARTITION BY bank_txn_id,item_cur) invoice_total,
       max(cash_i) OVER (PARTITION BY bank_txn_id, item_cur) line_cash_i,
       count(*) OVER (PARTITION BY bank_txn_id, item_cur) currency_pairs,
       bool_or(regexp_matches(upper(hint),'^DISPUTE\\s*:\\s*.+')) OVER (PARTITION BY bank_txn_id,item_id) has_dispute,
       regexp_extract(hint,'(?i)^DISPUTE\\s*:\\s*(.+)$',1) dispute_reason,
       regexp_matches(upper(coalesce(text_norm,'')), '\\bCHGS\\b|GASTOS (SHA|BEN)|\\b(SHA|BEN)\\b') OR
       coalesce(bank_code,'') LIKE '%XBCT' OR (lower(statement_format)='n43' AND bank_code='13') AS non_sepa
       FROM B""")
    con.execute("""CREATE TEMP TABLE D AS SELECT *,
      line_cash_i AS cash_total,
      abs(signed_open_sum) AS abs_open_sum,
      abs(line_cash_i-abs(signed_open_sum)) <= greatest(currency_pairs,1) AS exact_same,
      abs(abs(line_cash_i)-abs(signed_open_sum)) / NULLIF(abs(signed_open_sum),0) <= 0.03 AS fx_band,
      abs(open_c-abs(line_cash_i)) AS abs_diff,
      open_c-abs(line_cash_i) AS diff,
      (kind='ARI' AND discount_pct IS NOT NULL AND discount_days IS NOT NULL AND
       booking_date <= issue_date + CAST(discount_days AS INTEGER) + 3 AND
       abs(abs(line_cash_i)-discount_expected) <= greatest(currency_pairs,1)) AS discount_ok,
      (country IN ('MX','BR','MA') AND kind='ARI' AND
       least(abs(abs(line_cash_i)-round(invoice_total*(1-0.0125))),
             abs(abs(line_cash_i)-round(invoice_total*(1-0.015))),
             abs(abs(line_cash_i)-round(invoice_total*(1-0.10)))) <= greatest(currency_pairs,1)) AS wht_candidate,
      (kind='ARI' AND non_sepa AND open_c>0 AND diff>0 AND diff <= 0.02*open_c AND
       diff / NULLIF(item_units_per_eur,0) <= 6000) AS bank_charge_ok,
      (upper(line_currency)<>upper(item_cur)) AS is_fx,
      ((kind='ARI' AND direction='IN') OR (kind='API' AND direction='OUT') OR
       (kind IN ('ARC','APC') AND ((kind='ARC' AND direction='OUT') OR (kind='APC' AND direction='IN')))) AS normal_direction,
      ((kind='ARI' AND direction='OUT') OR (kind='API' AND direction='IN') OR
       (kind='APC' AND direction='OUT') OR (kind='ARC' AND direction='IN')) AS reverse_direction
      FROM G""")
    # Populate the policy decision from grouped pair features. Keep rules auditable at row granularity.
    con.execute("""CREATE TEMP TABLE R AS SELECT *,
      CASE
       WHEN open_before_cents<=0 AND normal_direction THEN 'DUPLICATE_PAYMENT'
       WHEN reverse_direction THEN 'RETURNED'
       WHEN is_fx AND kind IN ('ARI','API') AND fx_band THEN 'FX_DIFFERENCE'
       WHEN NOT is_fx AND kind='ARI' AND discount_ok THEN 'SHORT_DISCOUNT'
       WHEN NOT is_fx AND country IN ('MX','BR','MA') AND kind IN ('ARI','API') AND wht_candidate THEN 'SHORT_WITHHOLDING'
       WHEN NOT is_fx AND n_pair=1 AND bank_charge_ok THEN 'SHORT_BANK_CHARGES'
       WHEN NOT is_fx AND n_pair=1 AND kind='ARI' AND diff < -1 THEN 'OVERPAYMENT'
       WHEN has_dispute AND NOT exact_same THEN 'SHORT_DISPUTE'
       WHEN abs(line_cash_i) < abs(signed_open_sum)-greatest(currency_pairs,1) THEN 'PARTIAL'
       ELSE '' END exception_code
      FROM D""")
    # Shared multi-item partial: exact amount is allocated to all normal rows except one absorber.
    # Candidate scoring uses policy-measured total fractions, with most-recent invoice deterministic fallback.
    con.execute("""CREATE TEMP TABLE A AS SELECT *,
      abs(line_cash_i) AS total_cash,
      abs(signed_open_sum)-abs(line_cash_i) AS shortfall,
      CASE WHEN exception_code='PARTIAL' AND kind IN ('ARI','API') AND (
        abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.1))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.2))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.3))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.4))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.5))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.6))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.7))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.8))<=1 OR
         abs(open_c-(abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.9))<=1 OR
         abs((abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.03))<=1 OR
         abs((abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.05))<=1 OR
         abs((abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.10))<=1 OR
         abs((abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.20))<=1 OR
         abs((abs(signed_open_sum)-abs(line_cash_i))-round(total_cents*0.30))<=1) THEN 1 ELSE 0 END absorber_candidate,
      row_number() OVER (PARTITION BY bank_txn_id ORDER BY absorber_candidate DESC, issue_date DESC NULLS LAST, item_id DESC) absorber_rank
      FROM R""")
    con.execute("""CREATE TEMP TABLE FINAL AS SELECT *,
       CASE WHEN exception_code='PARTIAL' AND n_pair>1 AND kind IN ('ARI','API') AND
          absorber_rank=1
         THEN 'PARTIAL' WHEN exception_code='PARTIAL' AND n_pair>1 AND kind IN ('ARI','API') THEN ''
         ELSE exception_code END final_exception,
       CASE WHEN exception_code='DUPLICATE_PAYMENT' THEN abs(cash_i)
            WHEN exception_code='RETURNED' THEN abs(cash_i)
            WHEN exception_code IN ('SHORT_DISPUTE','PARTIAL') AND n_pair=1 THEN coalesce(applied_i,abs(cash_i))
            WHEN exception_code='PARTIAL' AND n_pair>1 AND kind IN ('ARI','API') AND
              absorber_rank=1
              THEN coalesce(applied_i,greatest(0,open_c-(abs(signed_open_sum)-abs(line_cash_i))))
            WHEN exception_code IN ('OVERPAYMENT','SHORT_DISCOUNT','SHORT_BANK_CHARGES','SHORT_WITHHOLDING','FX_DIFFERENCE') THEN open_c
            WHEN exception_code='PARTIAL' AND n_pair>1 THEN open_c
            ELSE open_c END final_amount
       FROM A""")
    out = con.sql("""SELECT bank_txn_id,item_id,CAST(round(final_amount) AS BIGINT) amount_cents,
       NULL::VARCHAR category, nullif(final_exception,'') exception,
       CASE WHEN exception_code='DUPLICATE_PAYMENT' THEN 'T5.duplicate.open_zero'
            WHEN exception_code='RETURNED' THEN 'T5.return.reverse_direction'
            WHEN exception_code='FX_DIFFERENCE' THEN 'T5.fx.reference_band_3pct'
            WHEN exception_code='SHORT_DISCOUNT' THEN 'T5.discount.terms_deadline_amount'
            WHEN exception_code='SHORT_WITHHOLDING' THEN 'T5.withholding.country_rate'
            WHEN exception_code='SHORT_BANK_CHARGES' THEN 'T5.bank_charges.non_sepa_tolerance'
            WHEN exception_code='OVERPAYMENT' THEN 'T5.overpayment.single_invoice'
            WHEN exception_code='SHORT_DISPUTE' THEN 'T5.dispute.explicit_remittance_reason'
            WHEN final_exception='PARTIAL' THEN 'T5.partial.open_balance'
            ELSE 'T5.full_clear.no_exception' END rule,
       CASE WHEN exception_code IN ('DUPLICATE_PAYMENT','RETURNED') THEN 1.0
            WHEN exception_code IN ('SHORT_DISCOUNT','SHORT_WITHHOLDING','SHORT_BANK_CHARGES') THEN 0.99
            WHEN exception_code='FX_DIFFERENCE' THEN 0.95 WHEN exception_code='SHORT_DISPUTE' THEN 0.95
            WHEN exception_code='PARTIAL' THEN 0.85 ELSE 0.90 END::DOUBLE confidence,
       concat('open=',open_before_cents,'; cash_line_item_ccy=',round(line_cash_i),'; currencies=',line_currency,'/',item_cur,
         '; direction=',coalesce(direction,''),'; kind=',coalesce(kind,''),'; hint=',left(hint,120),
         '; source=',coalesce(rule,''),'; ',coalesce(evidence,'')) evidence
       FROM FINAL""").pl()
    con.close()
    return out.select(OUTPUT)
