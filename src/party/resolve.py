"""Resolve candidate parties for each bank line of a phase -> work/<phase>/line_party.parquet (contract §4).

Evidence families (each row keeps rule, confidence, evidence):
  iban      counterparty / text IBAN -> party (history, masters, bank-account-change notices)
  mandate   SEPA mandate id in text -> customer
  learned   text prefix seen before for exactly this party (history, per line entity)
  master    text prefix = start of a current/trade/printed party name (incl. "ORD. X", "EXPTE ... X")
  payrun    run_id of our payment file in the text + debtor account + amount/sequence -> beneficiary (payrun.py)
  fuzzy     rapidfuzz on unique extracted names, only for lines with nothing better (see fuzzy.py)
Confidences of different families are combined as 1 - prod(1 - c); top 3 per line are kept.
"""
from .common import KEY_LENGTHS, SHORT_MIN, connect, out_dir

MAX_TOKENS = 20
MASTER_BASE = {24: 0.90, 16: 0.85, 10: 0.70}
MASTER_SHORT = 0.60
IBAN_CONF = {"iban_master": 0.97, "iban_notice": 0.97}
MANDATE_CONF = 0.98
MIN_PAIR_SHARE = 0.01   # line entity -> party entity pairs seen in >= 1 % of history (payment factory)
MAX_MASTER_PARTIES = 40  # names repeat a lot (US01: 64 % of customers share a name); beyond this it is generic
AMOUNT_CONF = 0.6       # candidate party has an item issued before the line for exactly the line amount
AMOUNT_DAYS = 400
DOC_CONF = {"alnum": 0.85, "dig": 0.70}  # doc-number token of an item of this party (divided by n_parties)
DOC_MAX_PARTIES = 5
DOC_SINCE_DAYS = 550    # items issued up to ~18 months before the phase start


def lines_path(phase):
    import os
    t1 = f"work/{phase}/lines.parquet"
    return t1 if os.path.exists(t1) else f"work/{phase}/party_lines.parquet"


def candidates(con, phase, model):
    m = f"work/party_model/{model}"
    con.execute(f"CREATE OR REPLACE TEMP VIEW ln AS SELECT * FROM read_parquet('{lines_path(phase)}')")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE pairs_ok AS
        SELECT line_entity, party_entity FROM '{m}/entity_pairs.parquet' WHERE share >= {MIN_PAIR_SHARE}
        UNION SELECT entity, entity FROM '{m}/parties.parquet'""")
    con.execute(f"CREATE OR REPLACE TEMP VIEW parties AS SELECT * FROM '{m}/parties.parquet'")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE sfx AS
        SELECT bank_txn_id, entity, k, array_to_string(tok[k:], ' ') AS s
        FROM (SELECT bank_txn_id, entity, string_split(text_norm, ' ') AS tok FROM ln),
             LATERAL (SELECT unnest(range(1, least(len(tok), {MAX_TOKENS}) + 1)) AS k)""")

    q = []
    # learned prefixes (same line entity)
    for L in KEY_LENGTHS:
        f = 0.9 if L == min(KEY_LENGTHS) else 1.0
        q.append(f"""SELECT x.bank_txn_id, lk.party_id, 'learned' AS fam, 'learned_key_L{L}' AS rule,
                       lk.purity * lk.total / (lk.total + 1) * {f} AS conf, 'key=' || lk.key AS evidence
                FROM sfx x JOIN '{m}/learned_keys.parquet' lk
                  ON lk.entity = x.entity AND lk.L = {L} AND lk.key = left(x.s, {L})""")
    # master name prefixes
    for L in KEY_LENGTHS:
        q.append(f"""SELECT x.bank_txn_id, mk.party_id, 'master' AS fam, 'master_' || mk.source || '_L{L}' AS rule,
                       {MASTER_BASE[L]} / mk.n_parties AS conf, 'name=' || mk.key AS evidence
                FROM sfx x JOIN '{m}/master_keys.parquet' mk ON mk.L = {L} AND mk.key = left(x.s, {L}) AND mk.n_parties <= {MAX_MASTER_PARTIES}
                JOIN pairs_ok po ON po.line_entity = x.entity AND po.party_entity = mk.entity""")
    q.append(f"""SELECT x.bank_txn_id, mk.party_id, 'master' AS fam, 'master_' || mk.source || '_short' AS rule,
                   {MASTER_SHORT} / mk.n_parties AS conf, 'name=' || mk.key AS evidence
            FROM sfx x JOIN '{m}/master_keys.parquet' mk
              ON mk.L BETWEEN {SHORT_MIN} AND {min(KEY_LENGTHS) - 1} AND mk.key = left(x.s, mk.L)
             AND mk.n_parties <= {MAX_MASTER_PARTIES}
             AND (length(x.s) = mk.L OR substr(x.s, mk.L + 1, 1) = ' ')
            JOIN pairs_ok po ON po.line_entity = x.entity AND po.party_entity = mk.entity""")
    # IBAN
    q.append(f"""SELECT l.bank_txn_id, ib.party_id, 'iban' AS fam, ib.source AS rule,
                   CASE ib.source WHEN 'iban_history' THEN ib.purity * ib.n / (ib.n + 1)
                                  ELSE {IBAN_CONF['iban_master']} END AS conf, 'iban=' || ib.iban AS evidence
            FROM (SELECT bank_txn_id, unnest(ibans) AS iban FROM ln) l JOIN '{m}/ibans.parquet' ib USING (iban)""")
    # mandate
    q.append(f"""SELECT l.bank_txn_id, md.party_id, 'mandate' AS fam, md.source AS rule, {MANDATE_CONF} AS conf,
                   'mandate=' || md.mandate_id AS evidence
            FROM (SELECT bank_txn_id, unnest(mandate_ids) AS mandate_id FROM ln) l
            JOIN '{m}/mandates.parquet' md USING (mandate_id)""")
    # reduce each evidence query to its best row per (line, party, family) before the union (keeps it small)
    red = [f"""SELECT bank_txn_id, party_id, fam, arg_max(rule, conf) AS rule, max(conf) AS conf,
                      arg_max(evidence, conf) AS evidence FROM ({x}) GROUP BY ALL""" for x in q]
    con.execute("CREATE OR REPLACE TEMP TABLE cand (bank_txn_id VARCHAR, party_id VARCHAR, fam VARCHAR, "
                "rule VARCHAR, conf DOUBLE, evidence VARCHAR)")
    for x in red:
        con.execute(f"INSERT INTO cand {x}")
    # payment runs: run_id in the text + debtor account + amount/sequence -> beneficiary -> party (see payrun.py)
    from . import payrun
    payrun.add(con, phase, model)
    # doc_ref: text tokens (also 2-3 adjacent tokens glued: banks split "FV25-0109359") = an item's printed number
    base_ = "train" if phase in ("window", "learn") else phase
    from .common import src as _src
    since = "DATE '2024-07-01'" if phase == "window" else "(SELECT min(booking_date) FROM ln)"
    docs = f"""SELECT customer_id AS party_id, document_number AS d, issue_date FROM {_src(base_,'ar_invoices')}
               UNION ALL SELECT customer_id, customer_po, issue_date FROM {_src(base_,'ar_invoices')} WHERE customer_po IS NOT NULL
               UNION ALL SELECT supplier_id, supplier_invoice_number, issue_date FROM {_src(base_,'ap_invoices')}
               UNION ALL SELECT party_id, document_number, issue_date FROM {_src(base_,'credit_notes')}"""
    if base_ != "train":
        docs += f" UNION ALL SELECT party_id, document_number, issue_date FROM {_src(base_,'open_items_at_start')}"
    con.execute(f"""CREATE OR REPLACE TEMP TABLE docidx AS
        WITH d AS (SELECT DISTINCT party_id, regexp_replace(upper(d), '[^A-Z0-9]', '', 'g') AS k_alnum,
                          ltrim(regexp_replace(d, '[^0-9]', '', 'g'), '0') AS k_dig
                   FROM ({docs}) WHERE d IS NOT NULL
                     AND issue_date >= {since} - INTERVAL {DOC_SINCE_DAYS} DAY),
             k AS (SELECT 'alnum' AS kt, k_alnum AS key, party_id FROM d WHERE length(k_alnum) >= 6
                   UNION SELECT 'dig', k_dig, party_id FROM d WHERE length(k_dig) >= 6)
        SELECT kt, key, party_id, count(*) OVER (PARTITION BY kt, key) AS n_parties FROM k""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE linekeys AS
        WITH t AS (SELECT bank_txn_id, string_split(text_norm, ' ') AS tok FROM ln),
             g AS (SELECT bank_txn_id, array_to_string(tok[i:i + w - 1], '') AS x
                   FROM t, LATERAL (SELECT unnest(range(1, least(len(tok), 60) + 1)) AS i),
                        LATERAL (SELECT unnest([1, 2, 3]) AS w)
                   WHERE i + w - 1 <= len(tok)),
             k AS (SELECT DISTINCT bank_txn_id, 'alnum' AS kt, x AS key FROM g WHERE regexp_matches(x, '[0-9]')
                   UNION SELECT bank_txn_id, 'dig', ltrim(regexp_replace(x, '[^0-9]', '', 'g'), '0') FROM g
                   WHERE regexp_matches(x, '[0-9]'))
        SELECT * FROM k""")
    # (a) specific numbers (<= 5 parties) are evidence on their own
    con.execute(f"""INSERT INTO cand
        SELECT k.bank_txn_id, di.party_id, 'doc_ref', 'doc_ref_' || k.kt,
               max(CASE k.kt WHEN 'alnum' THEN {DOC_CONF['alnum']} ELSE {DOC_CONF['dig']} END / di.n_parties),
               'doc=' || min(k.key)
        FROM linekeys k JOIN docidx di ON di.kt = k.kt AND di.key = k.key
        WHERE di.n_parties <= {DOC_MAX_PARTIES}
        GROUP BY 1, 2, 4""")
    # (b) generic numbers (INV0002607 exists for many suppliers) only confirm a party already proposed
    con.execute(f"""INSERT INTO cand
        SELECT k.bank_txn_id, di.party_id, 'doc_confirm', 'doc_ref_confirms_' || k.kt,
               max(CASE k.kt WHEN 'alnum' THEN {DOC_CONF['alnum']} ELSE {DOC_CONF['dig']} END) * 0.8,
               'doc=' || min(k.key)
        FROM linekeys k JOIN docidx di ON di.kt = k.kt AND di.key = k.key
        JOIN (SELECT DISTINCT bank_txn_id, party_id FROM cand) c
          ON c.bank_txn_id = k.bank_txn_id AND c.party_id = di.party_id
        WHERE di.n_parties > {DOC_MAX_PARTIES}
        GROUP BY 1, 2, 4""")
    con.execute("DROP TABLE docidx; DROP TABLE linekeys")
    # amount hint: separates homonyms. Only for parties already proposed by other evidence (never on its own).
    base = "train" if phase in ("window", "learn") else phase
    from .common import src
    items = f"""SELECT customer_id AS party_id, item_id, issue_date, currency, total_amount FROM {src(base,'ar_invoices')}
                UNION ALL SELECT supplier_id, item_id, issue_date, currency, total_amount FROM {src(base,'ap_invoices')}"""
    if base != "train":
        items += f""" UNION ALL SELECT party_id, item_id, issue_date, currency, open_amount
                      FROM {src(base,'open_items_at_start')} WHERE item_type IN ('ARI','API')"""
    con.execute(f"""INSERT INTO cand
        SELECT c.bank_txn_id, c.party_id, 'amount', 'amount_exact_item', {AMOUNT_CONF}, 'item=' || min(i.item_id)
        FROM (SELECT DISTINCT bank_txn_id, party_id FROM cand) c
        JOIN ln l USING (bank_txn_id)
        JOIN ({items}) i ON i.party_id = c.party_id AND i.currency = l.currency
             AND CAST(round(i.total_amount * 100) AS BIGINT) = abs(l.amount_cents)
             AND i.issue_date BETWEEN l.booking_date - INTERVAL {AMOUNT_DAYS} DAY AND l.booking_date
        GROUP BY 1, 2""")
    con.execute("DROP TABLE sfx")


def rank(con, phase, extra=None):
    """Combine evidence per (line, party) and keep the top 3."""
    union = "SELECT * FROM cand" + (f" UNION ALL SELECT * FROM {extra}" if extra else "")
    out = f"{out_dir(phase)}/line_party.parquet"
    from .common import master
    con.execute(f"""COPY (
      WITH created AS (SELECT customer_id AS party_id, created FROM {master('customers')}
                       UNION ALL SELECT supplier_id, created FROM {master('suppliers')}),
      best AS (   -- strongest evidence per family; a party created after the line cannot be its party
        SELECT c.bank_txn_id, c.party_id, fam, arg_max(rule, conf) AS rule, max(conf) AS conf,
               arg_max(evidence, conf) AS evidence
        FROM ({union}) c JOIN ln l USING (bank_txn_id) LEFT JOIN created cr USING (party_id)
        WHERE cr.created IS NULL OR cr.created <= l.booking_date
        GROUP BY ALL),
      comb AS (
        SELECT bank_txn_id, party_id,
               1 - exp(sum(ln(greatest(1 - least(conf, 0.999), 1e-9)))) AS confidence,
               arg_max(rule, conf) AS rule,
               string_agg(fam || ':' || evidence, '; ' ORDER BY conf DESC) AS evidence
        FROM best GROUP BY ALL),
      -- share among the line's candidates: two homonyms at 0.8 are each ~0.4 likely, not 0.8
      cal AS (SELECT *, confidence * confidence / sum(confidence) OVER (PARTITION BY bank_txn_id) AS conf_cal
              FROM comb)
      SELECT bank_txn_id,
             row_number() OVER (PARTITION BY bank_txn_id ORDER BY confidence DESC, party_id) AS rank,
             party_id, CAST(NULL AS VARCHAR) AS payer_party_id, rule AS method, round(conf_cal, 4) AS confidence,
             round(confidence, 4) AS evidence_strength,
             round(confidence / sum(confidence) OVER (PARTITION BY bank_txn_id), 4) AS share, rule, evidence
      FROM cal
      QUALIFY rank <= 3
    ) TO '{out}' (FORMAT parquet)""")
    return out


def run(phase, model, fuzzy=True):
    con = connect()
    candidates(con, phase, model)
    extra = None
    if fuzzy:
        from . import fuzzy as fz
        extra = fz.add(con, phase, model)
    out = rank(con, phase, extra)
    con.close()
    import os
    from . import calibrate
    if os.path.exists(calibrate.CAL):
        calibrate.apply(phase)
    return out
