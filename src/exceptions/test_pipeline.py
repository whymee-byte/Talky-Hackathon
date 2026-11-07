"""Focused tests for T5 artifact merging and source priority."""
import unittest
from datetime import date

import polars as pl

from src.exceptions.__main__ import _merge_priority
from src.exceptions.engine import decide


class MergePriorityTests(unittest.TestCase):
    @staticmethod
    def _base_inputs(pairs, lines, items, customers=None):
        fx = pl.DataFrame({"date": [], "currency": [], "units_per_eur": []}, schema={
            "date": pl.Date, "currency": pl.String, "units_per_eur": pl.Float64,
        })
        return decide(
            pl.DataFrame(pairs), pl.DataFrame(lines), pl.DataFrame(items),
            pl.DataFrame(customers or {"customer_id": [], "country": []}), fx,
        )

    def test_t2_side_link_wins_stale_pair_overlap(self):
        side = pl.DataFrame({
            "bank_txn_id": ["BT-1", "BT-2"],
            "item_id": ["ARI-1", "ARI-2"],
            "rule": ["T2.side", "T2.side"],
        })
        t5 = pl.DataFrame({
            "bank_txn_id": ["BT-1", "BT-3"],
            "item_id": ["ARI-1", "ARI-3"],
            "rule": ["T5.exception", "T5.exception"],
        })

        merged = _merge_priority([side, t5]).sort("bank_txn_id")

        self.assertEqual(merged["bank_txn_id"].to_list(), ["BT-1", "BT-2", "BT-3"])
        self.assertEqual(merged["rule"].to_list(), ["T2.side", "T2.side", "T5.exception"])

    def test_credit_note_netted_with_discount_remains_unexcepted(self):
        pairs = pl.DataFrame({
            "bank_txn_id": ["BT-DISC", "BT-DISC"],
            "item_id": ["ARI-2025-0000001", "ARC-2025-0000001"],
            "booking_date": [date(2025, 1, 10)] * 2,
            "line_amount_cents": [8800, 8800],
            "line_currency": ["EUR", "EUR"],
            "item_currency": ["EUR", "EUR"],
            "open_before_cents": [10000, 1000],
            "cash_cents": [10000, 1000],
            "doc_hint": [None, None],
            "rule": ["T3.mock", "T3.mock"],
            "confidence": [1.0, 1.0],
            "evidence": ["mock", "mock"],
        })
        lines = pl.DataFrame({
            "bank_txn_id": ["BT-DISC"], "amount_cents": [8800],
            "booking_date": [date(2025, 1, 10)], "currency": ["EUR"],
            "direction": ["IN"],
        })
        items = pl.DataFrame({
            "item_id": ["ARI-2025-0000001", "ARC-2025-0000001"],
            "kind": ["ARI", "ARC"], "total_cents": [10000, 1000],
            "currency": ["EUR", "EUR"], "issue_date": [date(2025, 1, 1)] * 2,
            "discount_pct": [2.0, None], "discount_days": [10, None],
        })
        customers = pl.DataFrame({"customer_id": ["C1"], "country": ["ES"]})
        fx = pl.DataFrame({"date": [], "currency": [], "units_per_eur": []}, schema={
            "date": pl.Date, "currency": pl.String, "units_per_eur": pl.Float64,
        })

        out = decide(pairs, lines, items, customers, fx).sort("item_id")

        self.assertEqual(out["exception"].to_list(), [None, "SHORT_DISCOUNT"])
        self.assertEqual(out["amount_cents"].to_list(), [1000, 10000])

    def test_withholding_on_one_invoice_in_mixed_payment(self):
        ids = ["ARI-2025-0000002", "ARI-2025-0000003"]
        pairs = pl.DataFrame({
            "bank_txn_id": ["BT-WHT"] * 2, "item_id": ids,
            "booking_date": [date(2025, 1, 10)] * 2,
            "line_amount_cents": [14000, 14000], "line_currency": ["BRL", "BRL"],
            "item_currency": ["BRL", "BRL"], "open_before_cents": [10000, 5000],
            "cash_cents": [9000, 5000], "doc_hint": [None, None],
            "rule": ["T3.mock", "T3.mock"], "confidence": [1.0, 1.0],
            "evidence": ["mock", "mock"],
        })
        lines = pl.DataFrame({
            "bank_txn_id": ["BT-WHT"], "amount_cents": [14000],
            "booking_date": [date(2025, 1, 10)], "currency": ["BRL"],
            "direction": ["IN"],
        })
        items = pl.DataFrame({
            "item_id": ids, "kind": ["ARI", "ARI"], "total_cents": [10000, 5000],
            "currency": ["BRL", "BRL"], "issue_date": [date(2025, 1, 1)] * 2,
            "party_id": ["C1", "C1"],
        })
        customers = pl.DataFrame({"customer_id": ["C1"], "country": ["BR"]})
        fx = pl.DataFrame({"date": [], "currency": [], "units_per_eur": []}, schema={
            "date": pl.Date, "currency": pl.String, "units_per_eur": pl.Float64,
        })

        out = decide(pairs, lines, items, customers, fx).sort("item_id")

        self.assertEqual(out["exception"].to_list(), ["SHORT_WITHHOLDING", None])
        self.assertEqual(out["amount_cents"].to_list(), [10000, 5000])

    def test_empty_merge_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            _merge_priority([])

    def test_return_hint_precedes_duplicate_and_reopens_paid_item(self):
        out = self._base_inputs(
            {
                "bank_txn_id": ["BT-RETURN"], "item_id": ["ARI-RETURN"],
                "booking_date": [date(2025, 1, 10)], "line_amount_cents": [10000],
                "line_currency": ["EUR"], "item_currency": ["EUR"],
                "open_before_cents": [0], "cash_cents": [10000], "doc_hint": [None],
                "rule": ["T3.mock"], "confidence": [1.0], "evidence": ["mock"],
            },
            {"bank_txn_id": ["BT-RETURN"], "amount_cents": [-10000],
             "booking_date": [date(2025, 1, 10)], "currency": ["EUR"],
             "direction": ["OUT"], "is_return_hint": [True]},
            {"item_id": ["ARI-RETURN"], "kind": ["ARI"], "total_cents": [10000],
             "currency": ["EUR"]},
        )
        self.assertEqual(out["exception"].to_list(), ["RETURNED"])
        self.assertEqual(out["amount_cents"].to_list(), [10000])

    def test_multi_item_partial_uses_each_pair_applied_amount(self):
        out = self._base_inputs(
            {
                "bank_txn_id": ["BT-PARTIAL", "BT-PARTIAL"],
                "item_id": ["ARI-P1", "ARI-P2"],
                "booking_date": [date(2025, 1, 10)] * 2,
                "line_amount_cents": [15000, 15000], "line_currency": ["EUR", "EUR"],
                "item_currency": ["EUR", "EUR"], "open_before_cents": [10000, 10000],
                "cash_cents": [7000, 7000], "doc_hint": [None, None],
                "rule": ["T3.mock", "T3.mock"], "confidence": [1.0, 1.0],
                "evidence": ["mock", "mock"],
            },
            {"bank_txn_id": ["BT-PARTIAL"], "amount_cents": [17000],
             "booking_date": [date(2025, 1, 10)], "currency": ["EUR"],
             "direction": ["IN"]},
            {"item_id": ["ARI-P1", "ARI-P2"], "kind": ["ARI", "ARI"],
             "total_cents": [10000, 10000], "currency": ["EUR", "EUR"]},
        ).sort("item_id")
        self.assertEqual(out["exception"].to_list(), ["PARTIAL", "PARTIAL"])
        self.assertEqual(out["amount_cents"].to_list(), [7000, 7000])

    def test_returned_collection_represented_on_reopened_balance_is_normal(self):
        out = self._base_inputs(
            {
                "bank_txn_id": ["BT-REPRESENT"], "item_id": ["ARI-REPRESENT"],
                "booking_date": [date(2025, 1, 10)], "line_amount_cents": [6000],
                "line_currency": ["EUR"], "item_currency": ["EUR"],
                "open_before_cents": [10000], "cash_cents": [6000], "doc_hint": [None],
                "rule": ["T3.mock"], "confidence": [1.0], "evidence": ["mock"],
            },
            {"bank_txn_id": ["BT-REPRESENT"], "amount_cents": [6000],
             "booking_date": [date(2025, 1, 10)], "currency": ["EUR"],
             "direction": ["IN"], "is_return_hint": [True]},
            {"item_id": ["ARI-REPRESENT"], "kind": ["ARI"],
             "total_cents": [10000], "currency": ["EUR"]},
        )
        self.assertEqual(out["exception"].to_list(), ["PARTIAL"])
        self.assertEqual(out["amount_cents"].to_list(), [6000])


if __name__ == "__main__":
    unittest.main()
