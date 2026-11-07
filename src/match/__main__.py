"""T3 · matcher → work/<fase>/pairs_raw.parquet (§7b) y work/<fase>/allocations.parquet (§8).

    python -m src.match --phase window      # y luego: python -m src.submit --phase window --from work/window/allocations.parquet --score
    python -m src.match --phase live

Etapas (prioridad descendente; cada partida se liquida una sola vez, salvo pagos parciales):
  side   side_links de T2 (remesas, lotes, órdenes de pago)                       → tal cual
  ref    nº de documento del texto (exacto, dígitos, sufijo 6-8, pedido) → partidas abiertas compatibles
  amt    tercero de T4 + importe exacto (o con descuento en plazo) → partida; FIFO / mes si hay varias
  none   sin partida: categoría de T7 si existe; si no, cobro → UNIDENTIFIED, pago → BANK_FEES
Importe y excepción según POLICIES §2-3 (reglas mínimas hasta que T5 publique su motor).
"""
import argparse
import os
import time

from src.match import combo
from src.common import data_root, safe_stdout
from src.common.profile import sql_wht_match, sql_wht_parties, withholding
from src.party.common import connect

MONTHS = {1: "ENERO JANUARY JANVIER JANUAR GENNAIO JANEIRO STYCZEN", 2: "FEBRERO FEBRUARY FEVRIER FEBRUAR FEBBRAIO FEVEREIRO LUTY",
          3: "MARZO MARCH MARS MAERZ MARZ MARCO MARZEC", 4: "ABRIL APRIL AVRIL APRILE KWIECIEN",
          5: "MAYO MAY MAI MAGGIO MAIO MAJ", 6: "JUNIO JUNE JUIN JUNI GIUGNO JUNHO CZERWIEC",
          7: "JULIO JULY JUILLET JULI LUGLIO JULHO LIPIEC", 8: "AGOSTO AUGUST AOUT SIERPIEN",
          9: "SEPTIEMBRE SETIEMBRE SEPTEMBER SEPTEMBRE SETTEMBRE SETEMBRO WRZESIEN",
          10: "OCTUBRE OCTOBER OCTOBRE OKTOBER OTTOBRE OUTUBRO PAZDZIERNIK", 11: "NOVIEMBRE NOVEMBER NOVEMBRE NOVEMBRO LISTOPAD",
          12: "DICIEMBRE DECEMBER DECEMBRE DEZEMBER DICEMBRE DEZEMBRO GRUDZIEN"}
UNIDENT_MAX_PARTY = 0.5  # a receipt is flagged UNIDENTIFIED only if T4 has no party at >= 0.5
OVERPAY_MAX = 0.10       # golden overpayments exceed the open amount by <= ~10 %; more = the line pays more items
COMB_PARTY_MIN = 0.5   # subset-sum only with a fairly sure party
COMB_MAX_ITEMS = 40    # most recent open items of the party considered for combinations
PARTY_MIN = 0.30          # T4 confidence to use a candidate party for amount matching (~F1/2 after calibration)
AMT_NAME_CONF = 0.6       # party under PARTY_MIN but named in the text with an item of exactly the amount (P ~0.94 on TRAIN window)
REF_CONF = {"alnum": 0.95, "po": 0.85, "dig": 0.90, "suf": 0.80}
FX_TOL = 0.03             # POLICIES: bank FX spread always well within 3 %
BANKCHG_EUR = 6000        # SHORT_BANK_CHARGES: shortfall <= 60 EUR equivalent ...
BANKCHG_PCT = 0.02        # ... and <= 2 % of the item
# SHORT_WITHHOLDING entities and rates: learned from TRAIN + POLICIES fallback (src.common.profile), not hard-coded


def tol(x):
    """scorer tolerance in cents: max(1 cent, 0.5 %)."""
    return f"greatest(1, 0.005 * abs({x}))"


def build(phase):
    t0 = time.time()
    w = f"work/{phase}"
    con = connect("5GB")
    model = "pre_window" if phase == "window" else "full"
    masters = os.path.join(data_root(), "masters").replace(os.sep, "/")
    wht = withholding(phase)
    wht_parties = sql_wht_parties(con, wht)
    print(f"withholding profile ({wht['model']}): countries {wht['countries']} rates {wht['rates']}")
    con.execute(f"CREATE TABLE L AS SELECT * FROM '{w}/lines.parquet'")
    con.execute(f"CREATE TABLE I AS SELECT * FROM '{w}/items.parquet'")
    con.execute(f"""CREATE TABLE ent_ok AS SELECT line_entity, party_entity FROM 'work/party_model/{model}/entity_pairs.parquet'
                    WHERE share >= 0.01 UNION SELECT DISTINCT entity, entity FROM I""")
    has_side = os.path.exists(f"{w}/side_links.parquet")
    con.execute(f"""CREATE TABLE SL AS SELECT bank_txn_id, item_id, amount_cents, coalesce(exception, '') AS exception,
                    rule, confidence, evidence FROM {f"'{w}/side_links.parquet'" if has_side else
                    "(SELECT NULL::VARCHAR bank_txn_id, NULL::VARCHAR item_id, NULL::BIGINT amount_cents, NULL::VARCHAR exception, NULL::VARCHAR rule, NULL::DOUBLE confidence, NULL::VARCHAR evidence WHERE false)"}""")
    # amt_name: T4 proposed the party from its master name in the text AND it has an item for exactly the line amount.
    # When a learned pattern key puts another party first, calibration leaves this one under PARTY_MIN although it is
    # the golden party ~94 % of the time on lines that otherwise end unmatched (TRAIN window, base run): the amount
    # stage admits it anyway (an unmatched receipt also shifts every later FIFO pick of that payer).
    con.execute(f"""CREATE TABLE P AS SELECT bank_txn_id, party_id, rank, confidence,
                       (method = 'amount_exact_item' AND evidence LIKE '%master:name=%') AS amt_name
                   FROM '{w}/line_party.parquet'""")
    con.execute(f"""CREATE TABLE FX AS SELECT date::DATE AS d, currency, units_per_eur::DOUBLE AS u
                   FROM read_csv('{masters}/fx_rates.csv', all_varchar=true)
                   UNION ALL SELECT DISTINCT date::DATE, 'EUR', 1.0
                   FROM read_csv('{masters}/fx_rates.csv', all_varchar=true)""")
    # lines still to explain + their cash per unit of currency
    con.execute("""CREATE TABLE LT AS SELECT l.bank_txn_id, l.entity, l.booking_date, l.amount_cents, abs(l.amount_cents) AS cash,
                       l.currency, l.direction, l.text_norm, l.orig_amount_cents, l.orig_currency
                   FROM L l WHERE NOT EXISTS (SELECT 1 FROM SL s WHERE s.bank_txn_id = l.bank_txn_id)""")
    # compatible item kinds per direction, with the sign each kind takes in the line total
    con.execute("""CREATE TABLE KS AS SELECT * FROM (VALUES ('IN','ARI',1), ('IN','ARC',-1), ('IN','APC',1),
                   ('OUT','API',1), ('OUT','APC',-1), ('OUT','ARC',1)) t(direction, kind, sgn)""")
    con.execute("CREATE TABLE IO AS SELECT * FROM I WHERE open_at_start_cents > 0")

    # ------------------------------------------------------------------ ref: document numbers in the text
    con.execute("""CREATE TABLE IK AS
        WITH k AS (
          SELECT item_id, entity, 'alnum' kt, replace(doc_norm, ' ', '') key FROM IO WHERE length(replace(doc_norm, ' ', '')) >= 5
          UNION ALL SELECT item_id, entity, 'po', replace(po_norm, ' ', '') FROM IO WHERE length(replace(po_norm, ' ', '')) >= 5
          UNION ALL SELECT item_id, entity, 'dig', doc_digits FROM IO WHERE length(doc_digits) >= 6
          UNION ALL SELECT item_id, entity, 'suf', right(doc_digits, n) FROM IO, (SELECT unnest([6, 7, 8]) n)
                    WHERE length(doc_digits) > n)
        SELECT *, count(*) OVER (PARTITION BY entity, kt, key) AS n_items FROM k""")
    con.execute("""CREATE TABLE LK AS
        WITH t AS (SELECT bank_txn_id, string_split(text_norm, ' ') tok FROM LT),
             g AS (SELECT bank_txn_id, array_to_string(tok[i:i + w - 1], '') x
                   FROM t, LATERAL (SELECT unnest(range(1, least(len(tok), 80) + 1)) i), LATERAL (SELECT unnest([1, 2, 3]) w)
                   WHERE i + w - 1 <= len(tok))
        SELECT DISTINCT bank_txn_id, 'alnum' kt, x key FROM g WHERE length(x) >= 5 AND regexp_matches(x, '[0-9]')
        UNION SELECT bank_txn_id, 'po', x FROM g WHERE length(x) >= 5 AND regexp_matches(x, '[0-9]')
        UNION SELECT bank_txn_id, 'dig', ltrim(regexp_replace(x, '[^0-9]', '', 'g'), '0') FROM g
              WHERE length(ltrim(regexp_replace(x, '[^0-9]', '', 'g'), '0')) >= 6
        UNION SELECT bank_txn_id, 'suf', regexp_replace(x, '[^0-9]', '', 'g') FROM g
              WHERE length(regexp_replace(x, '[^0-9]', '', 'g')) BETWEEN 6 AND 8""")
    con.execute(f"""CREATE TABLE RC AS   -- ref candidates
        SELECT lt.bank_txn_id, i.item_id, i.kind, ks.sgn, i.party_id, i.currency AS icur, i.open_at_start_cents AS open_c,
               i.total_cents, i.issue_date, i.discount_pct, i.discount_days,
               max(CASE k.kt WHEN 'alnum' THEN {REF_CONF['alnum']} WHEN 'po' THEN {REF_CONF['po']}
                             WHEN 'dig' THEN {REF_CONF['dig']} ELSE {REF_CONF['suf']} END
                   / CASE WHEN p.party_id IS NOT NULL THEN 1 ELSE ik.n_items END
                   * CASE WHEN p.party_id IS NOT NULL THEN 1 ELSE 0.8 END) AS conf,
               string_agg(DISTINCT k.kt || '=' || k.key, ',') AS ev
        FROM LK k JOIN LT lt USING (bank_txn_id)
        JOIN ent_ok e ON e.line_entity = lt.entity
        JOIN IK ik ON ik.entity = e.party_entity AND ik.kt = k.kt AND ik.key = k.key
        JOIN IO i ON i.item_id = ik.item_id
        JOIN KS ks ON ks.direction = lt.direction AND ks.kind = i.kind
        LEFT JOIN P p ON p.bank_txn_id = lt.bank_txn_id AND p.party_id = i.party_id
        WHERE i.issue_date <= lt.booking_date
          AND (p.party_id IS NOT NULL OR ik.n_items <= 3)          -- ambiguous numbers need the T4 party
        GROUP BY ALL""")
    con.execute("DROP TABLE LK; DROP TABLE IK")
    # cash of the line expressed in each item currency (same currency, instructed amount, or ECB rate of the day)
    con.execute("""CREATE TABLE CC AS
        SELECT DISTINCT lt.bank_txn_id, r.icur,
               CASE WHEN r.icur = lt.currency THEN lt.cash
                    WHEN r.icur = lt.orig_currency THEN abs(lt.orig_amount_cents)
                    ELSE round(lt.cash / fl.u * fi.u) END AS cash_i,
               r.icur <> lt.currency AS is_fx, coalesce(fl.u, 1.0) AS u_line
        FROM (SELECT DISTINCT bank_txn_id, icur FROM RC) r JOIN LT lt USING (bank_txn_id)
        ASOF LEFT JOIN FX fl ON fl.currency = lt.currency AND fl.d <= lt.booking_date
        ASOF LEFT JOIN FX fi ON fi.currency = r.icur AND fi.d <= lt.booking_date
        WHERE r.icur = lt.currency OR (fl.u IS NOT NULL AND fi.u IS NOT NULL)""")
    con.execute(f"""CREATE TABLE REFP AS
      WITH s AS (SELECT r.*, c.cash_i, c.is_fx, c.u_line, lt.booking_date, lt.entity,
                        sum(r.sgn * r.open_c) OVER (PARTITION BY r.bank_txn_id) AS sum_open,
                        count(*) OVER (PARTITION BY r.bank_txn_id) AS n_ref,
                        count(DISTINCT r.icur) OVER (PARTITION BY r.bank_txn_id) AS n_cur
                 FROM RC r JOIN CC c USING (bank_txn_id, icur) JOIN LT lt USING (bank_txn_id)),
      d AS (SELECT *,
          abs(cash_i - sum_open) <= CASE WHEN is_fx THEN {FX_TOL} * sum_open ELSE {tol('sum_open')} END AS exact,
          -- one invoice, less cash: discount within terms / withholding / partial
          n_ref = 1 AND discount_pct IS NOT NULL AND booking_date <= issue_date + discount_days + 3
            AND abs(cash_i - round(total_cents * (1 - discount_pct / 100))) <= 1 AS disc,
          n_ref = 1 AND party_id IN {wht_parties} AND {sql_wht_match(wht['rates'], 'cash_i', 'total_cents')} AS wht,
          n_ref = 1 AND kind IN ('ARI','API') AND cash_i > open_c + {tol('open_c')} AND cash_i <= open_c * (1 + {OVERPAY_MAX}) AS over,
          n_ref = 1 AND kind IN ('ARI','API') AND NOT is_fx AND cash_i < open_c - {tol('open_c')}
            AND open_c - cash_i <= least({BANKCHG_EUR} * u_line, {BANKCHG_PCT} * open_c) AS bankchg
        FROM s WHERE n_cur = 1)
      SELECT bank_txn_id, item_id, kind, party_id, issue_date, open_c, cash_i, is_fx,
        CASE WHEN exact OR disc OR wht OR (bankchg AND NOT disc AND NOT wht) THEN open_c
             WHEN n_ref = 1 AND cash_i < open_c THEN cash_i
             ELSE open_c END AS amount_cents,
        CASE WHEN is_fx AND kind IN ('ARI','API') AND (exact OR disc OR wht OR over) THEN 'FX_DIFFERENCE'
             WHEN exact THEN ''
             WHEN disc THEN 'SHORT_DISCOUNT'
             WHEN wht THEN 'SHORT_WITHHOLDING'
             WHEN bankchg THEN 'SHORT_BANK_CHARGES'
             WHEN n_ref = 1 AND cash_i < open_c THEN 'PARTIAL'
             WHEN over THEN 'OVERPAYMENT'
             ELSE '' END AS exception,
        'T3.ref.' || CASE WHEN exact THEN 'sum_exact' WHEN disc THEN 'discount' WHEN wht THEN 'withholding'
                          WHEN over THEN 'overpayment' WHEN bankchg THEN 'bank_charges' WHEN n_ref = 1 THEN 'single_diff' ELSE 'multi_diff' END AS rule,
        conf * CASE WHEN exact OR disc OR wht OR over OR bankchg THEN 1.0 WHEN n_ref = 1 THEN 0.8 ELSE 0.6 END AS confidence,
        ev || '; cash=' || cash_i || ' open_sum=' || sum_open AS evidence
      FROM d""")

    # ------------------------------------------------------------------ amt: T4 party + exact amount
    month_case = " ".join(f"WHEN regexp_matches(lt.text_norm, '\\b({'|'.join(v.split())})\\b') THEN {m}"
                          for m, v in MONTHS.items())
    con.execute(f"""CREATE TABLE AC AS
      WITH lt AS (SELECT *, CASE {month_case} ELSE NULL END AS month_hint FROM LT
                  WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM REFP)),
           c AS (
        SELECT lt.bank_txn_id, lt.month_hint, i.item_id, i.kind, i.party_id, i.issue_date, i.open_at_start_cents open_c,
               CASE WHEN p.confidence < {PARTY_MIN} THEN {AMT_NAME_CONF} ELSE p.confidence END AS pconf,
               p.confidence < {PARTY_MIN} AS by_amt_name,
               CASE WHEN abs(lt.cash - i.open_at_start_cents) <= 1 THEN 'exact'
                    ELSE 'discount' END AS how
        FROM lt JOIN P p USING (bank_txn_id)
        JOIN IO i ON i.party_id = p.party_id AND i.currency = lt.currency
        JOIN KS ks ON ks.direction = lt.direction AND ks.kind = i.kind AND ks.sgn = 1
        WHERE (p.confidence >= {PARTY_MIN} OR p.amt_name) AND i.issue_date <= lt.booking_date
          AND (abs(lt.cash - i.open_at_start_cents) <= 1
               OR (i.discount_pct IS NOT NULL AND lt.booking_date <= i.issue_date + i.discount_days + 3
                   AND abs(lt.cash - round(i.total_cents * (1 - i.discount_pct / 100))) <= 1
                   AND i.open_at_start_cents = i.total_cents)))
      SELECT *,
        -- service month: monthly fees are invoiced at the start of the following month
        (month_hint IS NOT NULL AND month(issue_date - INTERVAL 20 DAY) = month_hint) AS month_ok,
        count(*) OVER (PARTITION BY bank_txn_id) AS n_cand
      FROM c""")
    # one party per line (its best T4 candidate that has a matching item), then
    #  - service month named in the text -> that month's invoice
    #  - else k-th line of (party, amount) by date takes the k-th oldest open item (monthly fees are paid in order)
    con.execute("""CREATE TABLE AC1 AS SELECT * FROM AC
                   QUALIFY dense_rank() OVER (PARTITION BY bank_txn_id ORDER BY pconf DESC, party_id) = 1""")
    con.execute("""CREATE TABLE AM AS SELECT * FROM AC1 WHERE month_ok
                   QUALIFY row_number() OVER (PARTITION BY bank_txn_id ORDER BY issue_date DESC, item_id) = 1
                       AND row_number() OVER (PARTITION BY item_id ORDER BY bank_txn_id) = 1""")
    # FIFO (POLICIES §6): in booking order, each line takes the OLDEST still-unused open item of (party, amount) issued
    # on or before its date. Greedy per (party, cash) group — groups are small (a few lines each), the loop is over
    # ~10^4 candidate rows, not over the phase. Items already taken by side links, references or the month rule are out.
    fifo = con.sql("""SELECT a.bank_txn_id, a.item_id, a.party_id, l.cash, l.booking_date, a.issue_date
                      FROM AC1 a JOIN LT l USING (bank_txn_id)
                      WHERE a.bank_txn_id NOT IN (SELECT bank_txn_id FROM AM) AND a.item_id NOT IN (SELECT item_id FROM AM)
                        AND a.item_id NOT IN (SELECT item_id FROM SL) AND a.item_id NOT IN (SELECT item_id FROM REFP)
                      ORDER BY a.party_id, l.cash, l.booking_date, a.bank_txn_id, a.issue_date, a.item_id""").fetchall()
    picks, used, done = [], set(), set()
    for t, i, party, cash, bd, idate in fifo:      # rows come ordered: line by date, then its items oldest first
        if t in done or (party, cash, i) in used:
            continue
        picks.append((t, i)); used.add((party, cash, i)); done.add(t)
    import pandas as pd
    fp_df = pd.DataFrame(picks, columns=["bank_txn_id", "item_id"], dtype="object")  # noqa: F841 (read by duckdb)
    con.execute("CREATE TABLE FP AS SELECT bank_txn_id::VARCHAR bank_txn_id, item_id::VARCHAR item_id FROM fp_df")
    con.execute("""CREATE TABLE AF AS SELECT a.* FROM AC1 a JOIN FP USING (bank_txn_id, item_id)""")
    con.execute("""CREATE TABLE AMTP AS
      SELECT bank_txn_id, item_id, kind, party_id, issue_date, open_c, open_c AS cash_i, false AS is_fx, open_c AS amount_cents,
             CASE how WHEN 'discount' THEN 'SHORT_DISCOUNT' ELSE '' END AS exception,
             'T3.amt.' || how || CASE WHEN n_cand = 1 THEN '.unique' WHEN month_ok THEN '.month' ELSE '.fifo' END AS rule,
             pconf * CASE WHEN n_cand = 1 THEN 0.95 WHEN month_ok THEN 0.85 ELSE 0.75 END AS confidence,
             'party=' || party_id || ' conf=' || round(pconf, 2) || '; candidates=' || n_cand
               || CASE WHEN by_amt_name THEN '; party_by=amount+name' ELSE '' END AS evidence
      FROM (SELECT * FROM AM UNION ALL SELECT * FROM AF)""")

    # ------------------------------------------------------------------ comb: subset of the party's open items = cash
    # base = items already referenced in the text (may be empty); remainder R = cash - sum(base) is completed with
    # 1-3 more open items (credit notes count negative). Only unique solutions are accepted.
    con.execute(f"""CREATE TABLE CB AS     -- lines to complete: (line, party, base sum, remainder)
      WITH refmis AS (
        SELECT bank_txn_id, any_value(party_id) party_id, count(DISTINCT party_id) np,
               sum(CASE WHEN kind IN ('ARC','APC') AND kind <> CASE (SELECT direction FROM LT l WHERE l.bank_txn_id = r.bank_txn_id)
                        WHEN 'IN' THEN 'APC' ELSE 'ARC' END THEN -open_c ELSE open_c END) base_sum,
               any_value(cash_i) cash
        FROM REFP r WHERE rule IN ('T3.ref.multi_diff', 'T3.ref.single_diff') AND NOT is_fx
        GROUP BY 1 HAVING np = 1),
      pure AS (
        SELECT lt.bank_txn_id, p.party_id, 0 base_sum, lt.cash
        FROM LT lt JOIN P p ON p.bank_txn_id = lt.bank_txn_id AND p.rank = 1 AND p.confidence >= {COMB_PARTY_MIN}
        WHERE lt.bank_txn_id NOT IN (SELECT bank_txn_id FROM REFP) AND lt.bank_txn_id NOT IN (SELECT bank_txn_id FROM AMTP))
      SELECT bank_txn_id, party_id, base_sum, cash - base_sum AS rem, true AS has_base FROM refmis WHERE cash <> base_sum
      UNION ALL SELECT bank_txn_id, party_id, 0, cash, false FROM pure""")
    con.execute(f"""CREATE TABLE CI AS     -- candidate items per line (signed value v)
      SELECT cb.bank_txn_id, i.item_id, i.kind, i.issue_date, i.open_at_start_cents open_c,
             ks.sgn * i.open_at_start_cents AS v
      FROM CB cb JOIN LT lt USING (bank_txn_id)
      JOIN IO i ON i.party_id = cb.party_id AND i.currency = lt.currency AND i.issue_date <= lt.booking_date
           AND i.issue_date >= lt.booking_date - INTERVAL 400 DAY
      JOIN KS ks ON ks.direction = lt.direction AND ks.kind = i.kind
      WHERE i.item_id NOT IN (SELECT item_id FROM REFP r WHERE r.bank_txn_id = cb.bank_txn_id)
      QUALIFY row_number() OVER (PARTITION BY cb.bank_txn_id ORDER BY i.issue_date DESC, i.item_id) <= {COMB_MAX_ITEMS}""")
    con.execute("""CREATE TABLE CS AS     -- solutions of size 1 (completion only), 2 and 3
      WITH s1 AS (SELECT c.bank_txn_id, [c.item_id] items FROM CI c JOIN CB cb USING (bank_txn_id)
                  WHERE cb.has_base AND c.v = cb.rem),
           s2 AS (SELECT a.bank_txn_id, [a.item_id, b.item_id] items FROM CI a JOIN CB cb USING (bank_txn_id)
                  JOIN CI b ON b.bank_txn_id = a.bank_txn_id AND b.item_id > a.item_id AND b.v = cb.rem - a.v),
           ab AS (SELECT a.bank_txn_id, a.item_id ia, b.item_id ib, a.v + b.v vab FROM CI a
                  JOIN CI b ON b.bank_txn_id = a.bank_txn_id AND b.item_id > a.item_id),
           s3 AS (SELECT ab.bank_txn_id, [ab.ia, ab.ib, c.item_id] items FROM ab JOIN CB cb USING (bank_txn_id)
                  JOIN CI c ON c.bank_txn_id = ab.bank_txn_id AND c.item_id > ab.ib AND c.v = cb.rem - ab.vab),
           best AS (   -- smallest solution size wins; it must be unique at that size
             SELECT bank_txn_id, items, len(items) k FROM (SELECT * FROM s1 UNION ALL SELECT * FROM s2 UNION ALL SELECT * FROM s3)
             QUALIFY len(items) = min(len(items)) OVER (PARTITION BY bank_txn_id))
      SELECT bank_txn_id, any_value(items) items, any_value(k) k, false AS mitm FROM best GROUP BY 1 HAVING count(*) = 1""")
    con.execute("DROP TABLE CI")
    combo.add_solutions(con)     # larger subsets (meet in the middle), unique solutions only
    con.execute(f"""CREATE TABLE COMBP AS
      WITH found AS (SELECT cs.bank_txn_id, unnest(cs.items) item_id, cs.k, cs.mitm FROM CS cs),
           base AS (SELECT r.bank_txn_id, r.item_id, cs.k, cs.mitm FROM REFP r JOIN CS cs USING (bank_txn_id)),
           allp AS (SELECT bank_txn_id, item_id, k, mitm, 'found' src FROM found UNION ALL SELECT bank_txn_id, item_id, k, mitm, 'base' FROM base)
      SELECT a.bank_txn_id, a.item_id, i.kind, i.party_id, i.issue_date, i.open_at_start_cents open_c,
             i.open_at_start_cents cash_i, false is_fx, i.open_at_start_cents amount_cents, '' exception,
             'T3.comb.' || CASE WHEN cb.has_base THEN 'ref_plus_' ELSE 'party_' END || CASE WHEN a.mitm THEN 'mitm_' ELSE '' END || a.k AS rule,
             coalesce(p.confidence, 0.9) * CASE WHEN a.mitm THEN 0.65 WHEN a.k = 1 THEN 0.9 WHEN a.k = 2 THEN 0.8 ELSE 0.7 END AS confidence,
             'subset of ' || a.k || ' item(s) = remainder ' || cb.rem || ' (' || a.src || ')' AS evidence
      FROM allp a JOIN CB cb USING (bank_txn_id) JOIN I i USING (item_id)
      LEFT JOIN P p ON p.bank_txn_id = a.bank_txn_id AND p.rank = 1""")
    print(con.sql("SELECT rule, count(DISTINCT bank_txn_id) lines, count(*) pairs FROM COMBP GROUP BY 1 ORDER BY 1").df().to_string(index=False))

    # ------------------------------------------------------------------ arbitration: each item settled once
    con.execute("""CREATE TABLE T3P AS
      WITH u AS (SELECT *, 1 AS stage FROM REFP WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM COMBP)
                 UNION ALL BY NAME SELECT *, 1 AS stage FROM COMBP
                 UNION ALL BY NAME SELECT *, 2 AS stage FROM AMTP),
           -- an item fully cleared by a side link or by a stronger line is not available any more
           blocked AS (SELECT DISTINCT item_id FROM SL WHERE rule NOT LIKE 'T3f.%' AND item_id NOT IN (SELECT item_id FROM SL WHERE exception = 'RETURNED')),
           r AS (SELECT u.*, row_number() OVER (PARTITION BY item_id ORDER BY stage, confidence DESC, bank_txn_id) AS rk
                 FROM u WHERE item_id NOT IN (SELECT item_id FROM blocked))
      SELECT * EXCLUDE (rk) FROM r
      WHERE rk = 1 OR exception = 'PARTIAL'
      UNION ALL BY NAME   -- an exact single-document payment of an item another (earlier) line already cleared = duplicate
      SELECT * EXCLUDE (rk, exception, rule, amount_cents), cash_i AS amount_cents, 'DUPLICATE_PAYMENT' AS exception,
             'T3.ref.duplicate_in_phase' AS rule
      FROM r WHERE rk > 1 AND stage = 1 AND rule = 'T3.ref.sum_exact' AND exception = ''
        AND bank_txn_id IN (SELECT bank_txn_id FROM REFP GROUP BY 1 HAVING count(*) = 1)""")
    # partial payments: the one that pays the remaining balance is the last instalment (no exception)
    con.execute(f"""CREATE OR REPLACE TABLE T3P AS
      WITH x AS (SELECT p.*, lt.booking_date AS bd FROM T3P p JOIN LT lt USING (bank_txn_id)),
           c AS (SELECT *, coalesce(sum(amount_cents) FILTER (WHERE exception = 'PARTIAL') OVER (
                     PARTITION BY item_id ORDER BY bd, bank_txn_id ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) AS paid_before
                 FROM x)
      SELECT * EXCLUDE (bd, paid_before, exception, rule),
             CASE WHEN exception = 'PARTIAL' AND paid_before > 0 AND abs(amount_cents - (open_c - paid_before)) <= {tol('amount_cents')}
                  THEN '' ELSE exception END AS exception,
             CASE WHEN exception = 'PARTIAL' AND paid_before > 0 AND abs(amount_cents - (open_c - paid_before)) <= {tol('amount_cents')}
                  THEN 'T3.ref.last_instalment' ELSE rule END AS rule
      FROM c""")
    # a line keeps its pairs only if none of them was lost to another line (keep sets consistent)
    con.execute("""CREATE TABLE T3OK AS
      SELECT p.* FROM T3P p
      WHERE p.bank_txn_id NOT IN (
        SELECT u.bank_txn_id FROM (SELECT bank_txn_id, item_id FROM REFP WHERE bank_txn_id NOT IN (SELECT bank_txn_id FROM COMBP)
                                   UNION ALL SELECT bank_txn_id, item_id FROM COMBP
                                   UNION ALL SELECT bank_txn_id, item_id FROM AMTP) u
        LEFT JOIN T3P x USING (bank_txn_id, item_id) WHERE x.item_id IS NULL)""")
    con.execute(f"""COPY (SELECT t.bank_txn_id, t.item_id, lt.booking_date, lt.amount_cents AS line_amount_cents,
                         lt.currency AS line_currency, i.currency AS item_currency, t.open_c AS open_before_cents,
                         t.cash_i AS cash_cents, NULL::VARCHAR AS doc_hint, t.rule, t.confidence, t.evidence
                  FROM T3OK t JOIN LT lt USING (bank_txn_id) JOIN I i USING (item_id))
                  TO '{w}/pairs_raw.parquet' (FORMAT parquet)""")

    # ------------------------------------------------------------------ allocations (§8)
    cat = f"{w}/line_category.parquet"
    nonitem = (f"""SELECT l.bank_txn_id, NULL item_id, NULL::BIGINT amount_cents,
                         coalesce(c.category, CASE WHEN l.direction = 'IN' THEN 'UNIDENTIFIED' ELSE 'BANK_FEES' END) category,
                         CASE WHEN c.category = 'UNIDENTIFIED' THEN 'UNIDENTIFIED'
                              WHEN c.category IS NULL AND l.direction = 'IN' AND coalesce(p.c, 0) < {UNIDENT_MAX_PARTY} THEN 'UNIDENTIFIED'
                              ELSE '' END exception,
                         CASE WHEN c.category IS NULL THEN 'T3.none.fallback' ELSE coalesce(c.rule, 'T7.category') END rule,
                         coalesce(c.confidence, 0.3) confidence,
                         coalesce(c.evidence, 'no item found; best party conf=' || round(coalesce(p.c, 0), 2)) evidence
                  FROM L l LEFT JOIN '{cat}' c USING (bank_txn_id)
                  LEFT JOIN (SELECT bank_txn_id, max(confidence) c FROM P GROUP BY 1) p USING (bank_txn_id)""" if os.path.exists(cat) else
               f"""SELECT l.bank_txn_id, NULL item_id, NULL::BIGINT amount_cents,
                         CASE WHEN l.direction = 'IN' THEN 'UNIDENTIFIED' ELSE 'BANK_FEES' END category,
                         CASE WHEN l.direction = 'IN' AND coalesce(p.c, 0) < {UNIDENT_MAX_PARTY} THEN 'UNIDENTIFIED' ELSE '' END exception,
                         'T3.none.fallback' rule, 0.3 confidence,
                         'no item found; best party conf=' || round(coalesce(p.c, 0), 2) evidence
                  FROM L l LEFT JOIN (SELECT bank_txn_id, max(confidence) c FROM P GROUP BY 1) p USING (bank_txn_id)""")
    con.execute(f"""COPY (
      WITH items AS (
        SELECT bank_txn_id, item_id, amount_cents, NULL category, exception, rule, confidence, evidence FROM SL
        UNION ALL SELECT bank_txn_id, item_id, amount_cents, NULL, exception, rule, confidence, evidence FROM T3OK)
      SELECT * FROM items
      UNION ALL
      SELECT n.* FROM ({nonitem}) n WHERE n.bank_txn_id NOT IN (SELECT bank_txn_id FROM items)
    ) TO '{w}/allocations.parquet' (FORMAT parquet)""")
    st = con.sql(f"""SELECT count(*) rows, count(DISTINCT bank_txn_id) lines, count(item_id) pairs,
                            count(DISTINCT bank_txn_id) FILTER (WHERE item_id IS NOT NULL) lines_with_items
                     FROM '{w}/allocations.parquet'""").df()
    by_rule = con.sql(f"""SELECT split_part(rule, '.', 1) || '.' || split_part(rule, '.', 2) AS stage, count(*) rows,
                                 count(DISTINCT bank_txn_id) lines FROM '{w}/allocations.parquet' GROUP BY 1 ORDER BY 2 DESC""").df()
    print(st.to_string(index=False)); print(by_rule.to_string(index=False))
    print(f"allocations → {w}/allocations.parquet ({time.time() - t0:.0f}s)")


def main():
    safe_stdout()
    ap = argparse.ArgumentParser()
    ap.add_argument("--phase", required=True, choices=["sample", "window", "live", "final"])
    build(ap.parse_args().phase)


if __name__ == "__main__":
    main()
