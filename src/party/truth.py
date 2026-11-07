"""Golden party per TRAIN bank line (only for learning and evaluation, never for LIVE/FINAL).

party = cliente/proveedor dueño de las partidas que liquida la línea (contrato §4).
Writes work/train/party_truth.parquet: bank_txn_id, booking_date, parties (list), n_party, segment, line_type.
"""
from .common import connect, out_dir, src

# Segment -> line type used in the evaluation breakdown. Generic grouping of golden segment names.
LINE_TYPE_SQL = """
CASE
  WHEN segment IN ('ar_dd_batch','ar_card_settlement','ar_cheque_deposit') THEN 'lote_cobro'
  WHEN segment IN ('ar_dd_returned','ap_returned_transfer') THEN 'devolucion'
  WHEN segment IN ('ar_third_party_confirming','ap_confirming','ap_factored') THEN 'confirming_factoring'
  WHEN segment = 'ar_third_party_parent' THEN 'matriz'
  WHEN segment = 'ar_third_party_agency' THEN 'agencia'
  WHEN segment = 'ar_new_iban' THEN 'iban_nuevo'
  WHEN segment = 'ap_paid_by_holding' THEN 'payment_factory'
  WHEN segment = 'ap_direct_debit' THEN 'adeudo_proveedor'
  WHEN segment LIKE 'ap_%' THEN 'pago_proveedor'
  WHEN segment LIKE 'ar_%' THEN 'transferencia_cobro'
  ELSE 'otro'
END"""


def build(con=None):
    con = con or connect()
    out = f"{out_dir('train')}/party_truth.parquet"
    con.execute(f"""
    COPY (
      WITH items AS (
        SELECT item_id, customer_id AS party_id, entity FROM {src('train', 'ar_invoices')}
        UNION ALL SELECT item_id, supplier_id, entity FROM {src('train', 'ap_invoices')}
        UNION ALL SELECT item_id, party_id, entity FROM {src('train', 'credit_notes')}),
      g AS (
        SELECT g.bank_txn_id, any_value(g.segment) AS segment,
               list_distinct(list(i.party_id)) AS parties,
               list_distinct(list(i.entity)) AS party_entities
        FROM {src('train', 'golden_allocations')} g JOIN items i USING (item_id)
        GROUP BY 1)
      SELECT g.*, len(parties) AS n_party, b.booking_date, b.entity AS line_entity, {LINE_TYPE_SQL} AS line_type
      FROM g JOIN {src('train', 'bank_transactions')} b USING (bank_txn_id)
    ) TO '{out}' (FORMAT parquet)""")
    return out


if __name__ == "__main__":
    print(build())
