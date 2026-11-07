"""Empirical calibration of line_party.confidence (so that T3 can use a threshold ~ F1/2).

fit():   on the TRAIN window, P(candidate is a golden party | rank, strength bucket, share bucket)
         -> work/party_model/calibration.parquet  (buckets with < MIN_N lines fall back to a coarser bucket)
apply(): rewrites work/<phase>/line_party.parquet with confidence = calibrated probability.
strength = combined evidence 1 - prod(1 - c); share = strength / sum(strength of the line's candidates).
"""
import os

from .common import WINDOW_START, connect

MIN_N = 200
CAL = "work/party_model/calibration.parquet"
BUCKETS = "floor(least(evidence_strength, 0.9999) * 20) AS sb, floor(least(share, 0.9999) * 4) AS hb"


def _load(con, path):
    cols = con.sql(f"DESCRIBE SELECT * FROM '{path}'").df().column_name.tolist()
    share = "share" if "share" in cols else "confidence / nullif(evidence_strength, 0)"
    keep = "* EXCLUDE (share)" if "share" in cols else "*"
    con.execute(f"CREATE OR REPLACE TEMP TABLE lp AS SELECT {keep}, {share} AS share FROM '{path}'")


def fit(path="work/window/line_party.parquet"):
    con = connect()
    _load(con, path)
    con.execute(f"""COPY (
      WITH t AS (SELECT bank_txn_id, parties FROM 'work/train/party_truth.parquet'
                 WHERE booking_date >= DATE '{WINDOW_START}'),
           p AS (SELECT rank, {BUCKETS}, list_contains(t.parties, lp.party_id) AS hit
                 FROM lp JOIN t USING (bank_txn_id)),
           fine AS (SELECT rank, sb, hb, count(*) AS n, avg(hit::int) AS p FROM p GROUP BY ALL),
           coarse AS (SELECT rank, sb, count(*) AS n, avg(hit::int) AS p FROM p GROUP BY ALL)
      SELECT f.rank, f.sb, f.hb, CASE WHEN f.n >= {MIN_N} THEN f.p ELSE c.p END AS p, f.n
      FROM fine f JOIN coarse c USING (rank, sb)
    ) TO '{CAL}' (FORMAT parquet)""")
    return CAL


def apply(phase):
    if not os.path.exists(CAL):
        return None
    con = connect()
    path = f"work/{phase}/line_party.parquet"
    _load(con, path)
    con.execute(f"""COPY (
        SELECT x.* EXCLUDE (confidence, sb, hb), round(coalesce(c.p, x.share * x.evidence_strength), 4) AS confidence
        FROM (SELECT *, {BUCKETS} FROM lp) x
        LEFT JOIN '{CAL}' c ON c.rank = x.rank AND c.sb = x.sb AND c.hb = x.hb
    ) TO '{path}' (FORMAT parquet)""")
    return path
