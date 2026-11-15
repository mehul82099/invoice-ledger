#!/usr/bin/env python3
"""Tests for invoice_ledger. Run: python -m unittest -v"""

import csv
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from datetime import date
from pathlib import Path

import invoice_ledger as il


class MoneyTests(unittest.TestCase):
    def test_parse_basic(self):
        self.assertEqual(il.parse_amount("1500.00"), 150000)

    def test_parse_one_paisa(self):
        self.assertEqual(il.parse_amount("0.01"), 1)

    def test_parse_integer(self):
        self.assertEqual(il.parse_amount("42"), 4200)

    def test_reject_three_decimals(self):
        with self.assertRaises(il.LedgerError):
            il.parse_amount("10.005")

    def test_reject_text(self):
        with self.assertRaises(il.LedgerError):
            il.parse_amount("abc")

    def test_reject_negative(self):
        with self.assertRaises(il.LedgerError):
            il.parse_amount("-5")

    def test_reject_zero(self):
        with self.assertRaises(il.LedgerError):
            il.parse_amount("0")

    def test_format_indian_grouping(self):
        self.assertEqual(il.format_paise(12345678), "1,23,456.78")

    def test_format_small(self):
        self.assertEqual(il.format_paise(100), "1.00")
        self.assertEqual(il.format_paise(50), "0.50")

    def test_format_thousands_boundary(self):
        self.assertEqual(il.format_paise(100000), "1,000.00")
        self.assertEqual(il.format_paise(10000000), "1,00,000.00")


class DateTests(unittest.TestCase):
    def test_fy_april_start(self):
        self.assertEqual(il.fy_label(date(2026, 4, 1)), "2026-27")

    def test_fy_march_end(self):
        self.assertEqual(il.fy_label(date(2027, 3, 31)), "2026-27")

    def test_fy_before_april(self):
        self.assertEqual(il.fy_label(date(2026, 3, 31)), "2025-26")

    def test_parse_date_bad(self):
        with self.assertRaises(il.LedgerError):
            il.parse_date("14/11/2026")


class GstRateTests(unittest.TestCase):
    def test_user_supplied_rates_accepted(self):
        # The tool does not judge slabs: current, special and historical
        # (pre-22-Sep-2025) rates all parse.
        for r in ("0", "0.25", "1.5", "3", "5", "12", "18", "28", "40"):
            il.parse_gst_rate(r)  # no raise

    def test_fractional_rate_accepted(self):
        self.assertEqual(str(il.parse_gst_rate("17.5")), "17.5")

    def test_negative_rejected(self):
        with self.assertRaises(il.LedgerError):
            il.parse_gst_rate("-1")

    def test_over_100_rejected(self):
        with self.assertRaises(il.LedgerError):
            il.parse_gst_rate("101")

    def test_nonsense_rejected(self):
        with self.assertRaises(il.LedgerError):
            il.parse_gst_rate("abc")


class LineMathTests(unittest.TestCase):
    def test_intra_state_split(self):
        line = {"qty": 2, "rate_paise": 50000, "gst_rate": "18"}
        taxable, cgst, sgst, igst, total = il.line_amounts(line, intra_state=True)
        self.assertEqual((taxable, cgst, sgst, igst, total),
                         (100000, 9000, 9000, 0, 118000))

    def test_inter_state_igst(self):
        line = {"qty": 2, "rate_paise": 50000, "gst_rate": "18"}
        taxable, cgst, sgst, igst, total = il.line_amounts(line, intra_state=False)
        self.assertEqual((taxable, cgst, sgst, igst, total),
                         (100000, 0, 0, 18000, 118000))

    def test_rounding_half_up_and_split_sums(self):
        # 33.33 at 5% -> GST 166.65 paise rounds to 167; CGST+SGST must sum to 167
        line = {"qty": 1, "rate_paise": 3333, "gst_rate": "5"}
        taxable, cgst, sgst, igst, total = il.line_amounts(line, intra_state=True)
        self.assertEqual(cgst + sgst, 167)
        self.assertEqual(cgst, 84)
        self.assertEqual(sgst, 83)
        self.assertEqual(total, taxable + 167)

    def test_zero_rated(self):
        line = {"qty": 3, "rate_paise": 1000, "gst_rate": "0"}
        taxable, cgst, sgst, igst, total = il.line_amounts(line, intra_state=True)
        self.assertEqual((cgst, sgst, igst), (0, 0, 0))
        self.assertEqual(total, taxable)

    def test_special_rate_quarter_percent(self):
        line = {"qty": 1, "rate_paise": 100000, "gst_rate": "0.25"}
        _t, cgst, sgst, _g, total = il.line_amounts(line, intra_state=True)
        self.assertEqual(cgst + sgst, 250)
        self.assertEqual(total, 100250)

    def test_demerit_40_percent(self):
        line = {"qty": 1, "rate_paise": 10000, "gst_rate": "40"}
        _t, cgst, sgst, _g, total = il.line_amounts(line, intra_state=True)
        self.assertEqual(cgst + sgst, 4000)
        self.assertEqual(total, 14000)


class LedgerFixture(unittest.TestCase):
    def setUp(self):
        self.ledger = il.Ledger()
        self.ledger.add_customer("Sharma Traders", email="s@example.com")

    def make_invoice(self, issue=date(2026, 11, 14), due_days=30, intra=True):
        inv = self.ledger.create_invoice("c1", issue, due_days, intra)
        self.ledger.add_line(inv["id"], "Rice 25kg", 2, "1500.00", "5")
        return inv


class InvoiceFlowTests(LedgerFixture):
    def test_finalize_number_format(self):
        inv = self.make_invoice()
        number = self.ledger.finalize_invoice(inv["id"])
        self.assertEqual(number, "INV-2026-27/0001")

    def test_sequence_increments(self):
        self.ledger.finalize_invoice(self.make_invoice()["id"])
        number = self.ledger.finalize_invoice(self.make_invoice()["id"])
        self.assertEqual(number, "INV-2026-27/0002")

    def test_fy_rollover_restarts_sequence(self):
        self.ledger.finalize_invoice(self.make_invoice()["id"])
        inv2 = self.make_invoice(issue=date(2027, 4, 5))
        self.assertEqual(self.ledger.finalize_invoice(inv2["id"]), "INV-2027-28/0001")

    def test_finalize_empty_rejected(self):
        inv = self.ledger.create_invoice("c1", date(2026, 11, 14))
        with self.assertRaises(il.LedgerError):
            self.ledger.finalize_invoice(inv["id"])

    def test_lines_frozen_after_finalize(self):
        inv = self.make_invoice()
        self.ledger.finalize_invoice(inv["id"])
        with self.assertRaises(il.LedgerError):
            self.ledger.add_line(inv["id"], "extra", 1, "10.00", "5")

    def test_double_finalize_rejected(self):
        inv = self.make_invoice()
        self.ledger.finalize_invoice(inv["id"])
        with self.assertRaises(il.LedgerError):
            self.ledger.finalize_invoice(inv["id"])

    def test_lookup_by_number(self):
        inv = self.make_invoice()
        number = self.ledger.finalize_invoice(inv["id"])
        self.assertIs(self.ledger.get_invoice(number), inv)

    def test_unknown_customer_rejected(self):
        with self.assertRaises(il.LedgerError):
            self.ledger.create_invoice("c99", date(2026, 11, 14))

    def test_invoice_totals_gst(self):
        inv = self.make_invoice()  # 2 x 1500.00 @5% intra
        totals = il.invoice_totals(inv)
        self.assertEqual(totals["taxable"], 300000)
        self.assertEqual(totals["gst"], 15000)
        self.assertEqual(totals["cgst"], 7500)
        self.assertEqual(totals["sgst"], 7500)
        self.assertEqual(totals["total"], 315000)
        self.assertEqual(totals["balance"], 315000)


class PaymentTests(LedgerFixture):
    def setUp(self):
        super().setUp()
        self.inv = self.make_invoice()
        self.ledger.finalize_invoice(self.inv["id"])  # total 3150.00

    def test_partial_payment(self):
        balance = self.ledger.record_payment(self.inv["id"], "1000.00", date(2026, 11, 20))
        self.assertEqual(balance, 215000)
        self.assertEqual(
            il.status_of(self.inv, date(2026, 11, 21)), "partial")

    def test_full_payment_marks_paid(self):
        self.ledger.record_payment(self.inv["id"], "3150.00", date(2026, 11, 20))
        self.assertEqual(il.status_of(self.inv, date(2026, 12, 31)), "paid")

    def test_overpayment_rejected(self):
        with self.assertRaises(il.LedgerError):
            self.ledger.record_payment(self.inv["id"], "3150.01", date(2026, 11, 20))

    def test_payment_on_draft_rejected(self):
        draft = self.make_invoice()
        with self.assertRaises(il.LedgerError):
            self.ledger.record_payment(draft["id"], "1.00", date(2026, 11, 20))

    def test_overdue_status(self):
        # due 2026-12-14; still open on 2026-12-15
        self.assertEqual(il.status_of(self.inv, date(2026, 12, 15)), "overdue")

    def test_sent_before_due(self):
        self.assertEqual(il.status_of(self.inv, date(2026, 11, 14)), "sent")

    def test_due_today_not_overdue(self):
        self.assertEqual(il.status_of(self.inv, date(2026, 12, 14)), "sent")

    def test_draft_status(self):
        draft = self.make_invoice()
        self.assertEqual(il.status_of(draft, date(2027, 1, 1)), "draft")


class AgingTests(LedgerFixture):
    def open_invoice(self, issue, due_days):
        inv = self.ledger.create_invoice("c1", issue, due_days, True)
        self.ledger.add_line(inv["id"], "item", 1, "100.00", "18")
        self.ledger.finalize_invoice(inv["id"])
        return inv

    def test_bucket_boundaries(self):
        as_of = date(2026, 12, 31)
        # (due_days_before_as_of -> expected bucket)
        cases = [(-1, "not due"), (0, "not due"), (1, "1-30"), (30, "1-30"),
                 (31, "31-60"), (60, "31-60"), (61, "61-90"), (90, "61-90"),
                 (91, "90+"), (400, "90+")]
        for days_late, expected in cases:
            self.assertEqual(il.aging_bucket(days_late), expected,
                             "days_late=%d" % days_late)

    def test_report_groups_balances(self):
        as_of = date(2026, 12, 31)
        recent = self.open_invoice(date(2026, 12, 1), 10)   # due 12-11, 20 days late
        old = self.open_invoice(date(2026, 8, 1), 10)       # due 08-11, 142 days late
        paid = self.open_invoice(date(2026, 8, 1), 10)
        self.ledger.record_payment(paid["id"], "118.00", date(2026, 9, 1))
        draft = self.ledger.create_invoice("c1", date(2026, 12, 1))  # excluded
        report = self.ledger.aging_report(as_of)
        self.assertEqual(report["1-30"]["count"], 1)
        self.assertEqual(report["1-30"]["balance"], 11800)
        self.assertEqual(report["90+"]["count"], 1)
        self.assertEqual(report["not due"]["count"], 0)
        self.assertIn(recent["number"], report["1-30"]["invoices"])
        self.assertIn(old["number"], report["90+"]["invoices"])


class CsvExportTests(LedgerFixture):
    def test_export_row(self):
        inv = self.make_invoice()
        self.ledger.finalize_invoice(inv["id"])
        self.ledger.record_payment(inv["id"], "150.00", date(2026, 11, 20))
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            self.ledger.export_csv(out, date(2026, 11, 21))
            with open(out, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows[0][0], "number")
        self.assertEqual(rows[1][0], "INV-2026-27/0001")
        self.assertEqual(rows[1][1], "Sharma Traders")
        self.assertEqual(rows[1][6], "3150.00")
        self.assertEqual(rows[1][8], "3000.00")
        self.assertEqual(rows[1][9], "partial")

    def test_drafts_excluded(self):
        self.make_invoice()  # never finalized
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out.csv"
            self.ledger.export_csv(out, date(2026, 11, 21))
            with open(out, newline="", encoding="utf-8") as f:
                rows = list(csv.reader(f))
        self.assertEqual(len(rows), 1)  # header only


class PersistenceTests(LedgerFixture):
    def test_round_trip(self):
        inv = self.make_invoice()
        self.ledger.finalize_invoice(inv["id"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ledger.json"
            self.ledger.save(db)
            raw = db.read_bytes()
            self.assertNotIn(b"\r\n", raw)  # LF-only, stable on Windows too
            loaded = il.Ledger.load(db)
        self.assertEqual(loaded.get_invoice("INV-2026-27/0001")["customer_id"], "c1")

    def test_load_missing_file_gives_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            loaded = il.Ledger.load(Path(tmp) / "nope.json")
        self.assertEqual(loaded.state["customers"], [])

    def test_corrupt_file_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "bad.json"
            db.write_text("{not json", encoding="utf-8")
            with self.assertRaises(il.LedgerError):
                il.Ledger.load(db)

    def test_failed_save_leaves_original_intact(self):
        inv = self.make_invoice()
        self.ledger.finalize_invoice(inv["id"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ledger.json"
            self.ledger.save(db)
            before = db.read_bytes()
            self.ledger.state["broken"] = object()  # not JSON-serializable
            try:
                with self.assertRaises(TypeError):
                    self.ledger.save(db)
                self.assertEqual(db.read_bytes(), before)
                leftovers = [p for p in Path(tmp).iterdir() if p.name != "ledger.json"]
                self.assertEqual(leftovers, [])
            finally:
                del self.ledger.state["broken"]

    def test_save_replaces_atomically_via_temp(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "ledger.json"
            self.ledger.save(db)
            self.ledger.add_customer("Second Customer")
            self.ledger.save(db)  # replace existing file
            loaded = il.Ledger.load(db)
        self.assertEqual(len(loaded.state["customers"]), 2)


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.tmp.name) / "cli.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = il.main(["--db", self.db] + list(argv))
        return code, out.getvalue(), err.getvalue()

    def test_full_flow(self):
        code, out, _ = self.run_cli("add-customer", "--name", "Verma Stores")
        self.assertEqual(code, 0)
        self.assertIn("c1", out)
        code, out, _ = self.run_cli("new-invoice", "--customer", "c1",
                                    "--issue-date", "2026-11-14", "--due-days", "15")
        self.assertEqual(code, 0)
        self.assertIn("i1", out)
        code, _, _ = self.run_cli("add-line", "--invoice", "i1",
                                  "--description", "Wheat flour 10kg",
                                  "--qty", "5", "--rate", "420.50", "--gst", "5")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli("finalize", "--invoice", "i1")
        self.assertEqual(code, 0)
        self.assertIn("INV-2026-27/0001", out)
        code, out, _ = self.run_cli("record-payment", "--invoice", "INV-2026-27/0001",
                                    "--amount", "500.00", "--date", "2026-11-20")
        self.assertEqual(code, 0)
        self.assertIn("balance due", out)
        code, out, _ = self.run_cli("list", "--as-of", "2026-11-21")
        self.assertEqual(code, 0)
        self.assertIn("partial", out)
        code, out, _ = self.run_cli("aging", "--as-of", "2026-12-31")
        self.assertEqual(code, 0)
        self.assertIn("1-30", out)
        code, out, _ = self.run_cli("summary", "--as-of", "2026-12-31")
        self.assertEqual(code, 0)
        self.assertIn("outstanding", out)
        code, out, _ = self.run_cli("overdue", "--as-of", "2026-12-31")
        self.assertEqual(code, 0)
        self.assertIn("days late", out)

    def test_cli_error_exit_code(self):
        self.run_cli("add-customer", "--name", "Verma Stores")
        self.run_cli("new-invoice", "--customer", "c1", "--issue-date", "2026-11-14")
        code, _, err = self.run_cli("finalize", "--invoice", "i1")
        self.assertEqual(code, 2)
        self.assertIn("no lines", err)

    def test_cli_historical_rate_accepted(self):
        # Rates vary by item and date of supply; older invoices can carry
        # rates outside today's main slabs (e.g. 12%). The tool must not
        # second-guess the user-supplied rate.
        self.run_cli("add-customer", "--name", "Verma Stores")
        self.run_cli("new-invoice", "--customer", "c1", "--issue-date", "2025-08-10")
        code, _, _ = self.run_cli("add-line", "--invoice", "i1",
                                  "--description", "x", "--qty", "1",
                                  "--rate", "10.00", "--gst", "12")
        self.assertEqual(code, 0)

    def test_cli_bad_rate_rejected(self):
        self.run_cli("add-customer", "--name", "Verma Stores")
        self.run_cli("new-invoice", "--customer", "c1", "--issue-date", "2026-11-14")
        code, _, err = self.run_cli("add-line", "--invoice", "i1",
                                    "--description", "x", "--qty", "1",
                                    "--rate", "10.00", "--gst", "150")
        self.assertEqual(code, 2)
        self.assertIn("between 0 and 100", err)

    def test_cli_export_csv(self):
        self.run_cli("add-customer", "--name", "Verma Stores")
        self.run_cli("new-invoice", "--customer", "c1", "--issue-date", "2026-11-14")
        self.run_cli("add-line", "--invoice", "i1", "--description", "x",
                     "--qty", "1", "--rate", "10.00", "--gst", "18")
        self.run_cli("finalize", "--invoice", "i1")
        out_path = str(Path(self.tmp.name) / "exp.csv")
        code, _, _ = self.run_cli("export-csv", out_path, "--as-of", "2026-11-14")
        self.assertEqual(code, 0)
        with open(out_path, newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1][6], "11.80")


if __name__ == "__main__":
    unittest.main()
