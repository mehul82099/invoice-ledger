#!/usr/bin/env python3
"""invoice-ledger: zero-dependency receivables CLI for Indian micro-SMEs.

Tracks customers, GST invoices (CGST/SGST/IGST), partial payments and
overdue aging - the follow-up half of invoice management: what is owed,
by whom, and how late it is. It does not design or print PDF invoices.

Storage: one JSON file (default ./invoice-ledger.json). Python 3.9+,
standard library only, no network access.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import tempfile
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

# The GST rate on each line is supplied by the user. This tool does no tax
# determination: it neither validates the rate against notified slabs nor
# decides intra- vs inter-state supply. Only a sanity range is enforced.
MAX_GST_RATE = Decimal("100")


class LedgerError(Exception):
    """User-facing error: bad input, invalid state transition, corrupt store."""


# ---------------------------------------------------------------------------
# money
# ---------------------------------------------------------------------------

def parse_amount(text):
    """Parse a rupee amount string into integer paise. Strict: max 2 decimals."""
    try:
        value = Decimal(str(text).strip())
    except InvalidOperation:
        raise LedgerError("invalid amount: %r" % text)
    if not value.is_finite():
        raise LedgerError("invalid amount: %r" % text)
    if value != value.quantize(Decimal("0.01")):
        raise LedgerError("amounts support at most 2 decimal places: %r" % text)
    if value <= 0:
        raise LedgerError("amount must be positive: %r" % text)
    return int(value * 100)


def format_paise(paise):
    """Format integer paise as rupees with Indian digit grouping (1,23,456.78)."""
    neg = paise < 0
    rupees, rem = divmod(abs(paise), 100)
    s = str(rupees)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        groups = []
        while head:
            groups.append(head[-2:])
            head = head[:-2]
        s = ",".join(reversed(groups)) + "," + tail
    return ("-" if neg else "") + s + ".%02d" % rem


def parse_gst_rate(text):
    """Parse a user-supplied GST percentage. Sanity range only: 0-100.

    Which rate applies depends on the goods/services and the date of
    supply; rates change by notification over time. Choosing the rate is
    the user's responsibility - see README.
    """
    try:
        rate = Decimal(str(text).strip())
    except InvalidOperation:
        raise LedgerError("invalid GST rate: %r" % text)
    if not rate.is_finite() or rate < 0 or rate > MAX_GST_RATE:
        raise LedgerError("GST rate must be between 0 and 100: %r" % text)
    return rate


# ---------------------------------------------------------------------------
# dates
# ---------------------------------------------------------------------------

def parse_date(text):
    try:
        return date.fromisoformat(str(text).strip())
    except ValueError:
        raise LedgerError("invalid date %r (expected YYYY-MM-DD)" % text)


def fy_label(d):
    """Indian financial year label for a date: Apr 2026 - Mar 2027 -> '2026-27'."""
    start = d.year if d.month >= 4 else d.year - 1
    return "%d-%s" % (start, str(start + 1)[2:])


# ---------------------------------------------------------------------------
# invoice math (all amounts integer paise)
# ---------------------------------------------------------------------------

def line_amounts(line, intra_state):
    """Return (taxable, cgst, sgst, igst, total) in paise for one line."""
    taxable = line["qty"] * line["rate_paise"]
    gst = int(
        (Decimal(taxable) * Decimal(line["gst_rate"]) / 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    if intra_state:
        cgst = (gst + 1) // 2
        sgst = gst - cgst
        igst = 0
    else:
        cgst = sgst = 0
        igst = gst
    return taxable, cgst, sgst, igst, taxable + gst


def invoice_totals(inv):
    taxable = cgst = sgst = igst = 0
    for line in inv["lines"]:
        t, c, s, g, _ = line_amounts(line, inv["intra_state"])
        taxable += t
        cgst += c
        sgst += s
        igst += g
    total = taxable + cgst + sgst + igst
    paid = sum(p["amount_paise"] for p in inv["payments"])
    return {
        "taxable": taxable,
        "cgst": cgst,
        "sgst": sgst,
        "igst": igst,
        "gst": cgst + sgst + igst,
        "total": total,
        "paid": paid,
        "balance": total - paid,
    }


def status_of(inv, as_of):
    """draft / sent / partial / overdue / paid, in that precedence (paid last check wins)."""
    if inv["number"] is None:
        return "draft"
    totals = invoice_totals(inv)
    if totals["balance"] <= 0:
        return "paid"
    if date.fromisoformat(inv["due_date"]) < as_of:
        return "overdue"
    if totals["paid"] > 0:
        return "partial"
    return "sent"


AGING_BUCKETS = ("not due", "1-30", "31-60", "61-90", "90+")


def aging_bucket(days_overdue):
    if days_overdue <= 0:
        return "not due"
    if days_overdue <= 30:
        return "1-30"
    if days_overdue <= 60:
        return "31-60"
    if days_overdue <= 90:
        return "61-90"
    return "90+"


# ---------------------------------------------------------------------------
# ledger
# ---------------------------------------------------------------------------

class Ledger:
    def __init__(self, state=None):
        self.state = state or {
            "customers": [],
            "invoices": [],
            "counters": {},
            "next": {"customer": 1, "invoice": 1},
        }

    @classmethod
    def load(cls, path):
        p = Path(path)
        if p.exists():
            try:
                return cls(json.loads(p.read_text(encoding="utf-8")))
            except json.JSONDecodeError as e:
                raise LedgerError("corrupt store %s: %s" % (p, e))
        return cls()

    def save(self, path):
        # Serialize fully, then write to a temp file and atomically replace
        # the store, so a crash or error mid-save cannot corrupt the ledger.
        # newline="" keeps byte output identical on Windows and Linux.
        payload = json.dumps(self.state, indent=2, sort_keys=True) + "\n"
        path = Path(path)
        fd, tmp = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
                f.write(payload)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # -- customers ---------------------------------------------------------

    def add_customer(self, name, email=None, phone=None):
        name = (name or "").strip()
        if not name:
            raise LedgerError("customer name is required")
        cid = "c%d" % self.state["next"]["customer"]
        self.state["next"]["customer"] += 1
        cust = {"id": cid, "name": name, "email": email or "", "phone": phone or ""}
        self.state["customers"].append(cust)
        return cust

    def _customer(self, cid):
        for c in self.state["customers"]:
            if c["id"] == cid:
                return c
        raise LedgerError("no such customer: %s" % cid)

    # -- invoices ----------------------------------------------------------

    def create_invoice(self, customer_id, issue_date, due_days=30, intra_state=True):
        self._customer(customer_id)
        if due_days < 0:
            raise LedgerError("due-days cannot be negative")
        iid = "i%d" % self.state["next"]["invoice"]
        self.state["next"]["invoice"] += 1
        inv = {
            "id": iid,
            "number": None,
            "customer_id": customer_id,
            "issue_date": issue_date.isoformat(),
            "due_date": (issue_date + timedelta(days=due_days)).isoformat(),
            "intra_state": bool(intra_state),
            "lines": [],
            "payments": [],
        }
        self.state["invoices"].append(inv)
        return inv

    def get_invoice(self, ref):
        for inv in self.state["invoices"]:
            if inv["id"] == ref or inv["number"] == ref:
                return inv
        raise LedgerError("no such invoice: %s" % ref)

    def add_line(self, ref, description, qty, rate_text, gst_rate_text):
        inv = self.get_invoice(ref)
        if inv["number"] is not None:
            raise LedgerError("invoice %s is finalized; lines are frozen" % inv["number"])
        description = (description or "").strip()
        if not description:
            raise LedgerError("line description is required")
        if not isinstance(qty, int) or qty <= 0:
            raise LedgerError("qty must be a positive integer")
        line = {
            "description": description,
            "qty": qty,
            "rate_paise": parse_amount(rate_text),
            "gst_rate": str(parse_gst_rate(gst_rate_text)),
        }
        inv["lines"].append(line)
        return line

    def finalize_invoice(self, ref):
        inv = self.get_invoice(ref)
        if inv["number"] is not None:
            raise LedgerError("invoice already finalized as %s" % inv["number"])
        if not inv["lines"]:
            raise LedgerError("cannot finalize an invoice with no lines")
        fy = fy_label(date.fromisoformat(inv["issue_date"]))
        seq = self.state["counters"].get(fy, 0) + 1
        self.state["counters"][fy] = seq
        inv["number"] = "INV-%s/%04d" % (fy, seq)
        return inv["number"]

    def record_payment(self, ref, amount_text, paid_date):
        inv = self.get_invoice(ref)
        if inv["number"] is None:
            raise LedgerError("finalize the invoice before recording payments")
        amount = parse_amount(amount_text)
        balance = invoice_totals(inv)["balance"]
        if amount > balance:
            raise LedgerError(
                "payment of %s exceeds the balance due of %s"
                % (format_paise(amount), format_paise(balance))
            )
        inv["payments"].append(
            {"date": paid_date.isoformat(), "amount_paise": amount}
        )
        return invoice_totals(inv)["balance"]

    # -- reports -----------------------------------------------------------

    def list_invoices(self, as_of, status=None):
        rows = []
        for inv in self.state["invoices"]:
            st = status_of(inv, as_of)
            if status and st != status:
                continue
            rows.append((inv, st, invoice_totals(inv)))
        return rows

    def aging_report(self, as_of):
        report = {
            b: {"count": 0, "balance": 0, "invoices": []} for b in AGING_BUCKETS
        }
        for inv in self.state["invoices"]:
            if inv["number"] is None:
                continue
            balance = invoice_totals(inv)["balance"]
            if balance <= 0:
                continue
            days = (as_of - date.fromisoformat(inv["due_date"])).days
            bucket = report[aging_bucket(days)]
            bucket["count"] += 1
            bucket["balance"] += balance
            bucket["invoices"].append(inv["number"])
        return report

    def summary(self, as_of):
        counts = {}
        outstanding = overdue = 0
        for inv, st, totals in self.list_invoices(as_of):
            counts[st] = counts.get(st, 0) + 1
            if inv["number"] is not None:
                outstanding += totals["balance"]
                if st == "overdue":
                    overdue += totals["balance"]
        return {
            "customers": len(self.state["customers"]),
            "invoices": len(self.state["invoices"]),
            "by_status": counts,
            "outstanding": outstanding,
            "overdue": overdue,
        }

    def export_csv(self, path, as_of):
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(
                [
                    "number",
                    "customer",
                    "issue_date",
                    "due_date",
                    "taxable",
                    "gst",
                    "total",
                    "paid",
                    "balance",
                    "status",
                ]
            )
            for inv, st, totals in self.list_invoices(as_of):
                if inv["number"] is None:
                    continue
                cust = self._customer(inv["customer_id"])
                w.writerow(
                    [
                        inv["number"],
                        cust["name"],
                        inv["issue_date"],
                        inv["due_date"],
                        "%.2f" % (totals["taxable"] / 100),
                        "%.2f" % (totals["gst"] / 100),
                        "%.2f" % (totals["total"] / 100),
                        "%.2f" % (totals["paid"] / 100),
                        "%.2f" % (totals["balance"] / 100),
                        st,
                    ]
                )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _fmt_row(cols, widths):
    return "  ".join(str(c).ljust(w) for c, w in zip(cols, widths)).rstrip()


def build_parser():
    p = argparse.ArgumentParser(
        prog="invoice-ledger",
        description="Zero-dependency receivables tracker for Indian micro-SMEs.",
    )
    p.add_argument("--db", default="invoice-ledger.json", help="store file path")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("add-customer", help="add a customer")
    sp.add_argument("--name", required=True)
    sp.add_argument("--email", default="")
    sp.add_argument("--phone", default="")

    sp = sub.add_parser("new-invoice", help="create a draft invoice")
    sp.add_argument("--customer", required=True, help="customer id, e.g. c1")
    sp.add_argument("--issue-date", default=None, help="YYYY-MM-DD (default: today)")
    sp.add_argument("--due-days", type=int, default=30)
    sp.add_argument(
        "--inter-state",
        action="store_true",
        help="charge IGST instead of CGST+SGST (place of supply in another state)",
    )

    sp = sub.add_parser("add-line", help="add a line to a draft invoice")
    sp.add_argument("--invoice", required=True)
    sp.add_argument("--description", required=True)
    sp.add_argument("--qty", type=int, required=True)
    sp.add_argument("--rate", required=True, help="unit rate in rupees, e.g. 1499.00")
    sp.add_argument("--gst", required=True,
                    help="GST rate %% for this line (you choose the rate that "
                         "applies to the item and invoice date, e.g. 5 or 18)")

    sp = sub.add_parser("finalize", help="assign the invoice number and freeze lines")
    sp.add_argument("--invoice", required=True)

    sp = sub.add_parser("record-payment", help="record a (partial) payment")
    sp.add_argument("--invoice", required=True)
    sp.add_argument("--amount", required=True)
    sp.add_argument("--date", default=None, help="YYYY-MM-DD (default: today)")

    sp = sub.add_parser("list", help="list invoices")
    sp.add_argument(
        "--status",
        choices=["draft", "sent", "partial", "overdue", "paid"],
        default=None,
    )
    sp.add_argument("--as-of", default=None, help="YYYY-MM-DD (default: today)")

    sp = sub.add_parser("show", help="show one invoice in full")
    sp.add_argument("--invoice", required=True)
    sp.add_argument("--as-of", default=None)

    sp = sub.add_parser("aging", help="receivables aging report")
    sp.add_argument("--as-of", default=None)

    sp = sub.add_parser("overdue", help="list overdue invoices")
    sp.add_argument("--as-of", default=None)

    sp = sub.add_parser("summary", help="totals at a glance")
    sp.add_argument("--as-of", default=None)

    sp = sub.add_parser("export-csv", help="export finalized invoices to CSV")
    sp.add_argument("path", help="output CSV path")
    sp.add_argument("--as-of", default=None)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        ledger = Ledger.load(args.db)
        as_of = parse_date(args.as_of) if getattr(args, "as_of", None) else date.today()
        mutate = True

        if args.cmd == "add-customer":
            c = ledger.add_customer(args.name, args.email, args.phone)
            out = "added customer %s: %s" % (c["id"], c["name"])
        elif args.cmd == "new-invoice":
            issue = parse_date(args.issue_date) if args.issue_date else date.today()
            inv = ledger.create_invoice(args.customer, issue, args.due_days, not args.inter_state)
            out = "created draft %s for %s (due %s)" % (
                inv["id"],
                inv["customer_id"],
                inv["due_date"],
            )
        elif args.cmd == "add-line":
            ledger.add_line(args.invoice, args.description, args.qty, args.rate, args.gst)
            out = "added line to %s" % args.invoice
        elif args.cmd == "finalize":
            out = "finalized as %s" % ledger.finalize_invoice(args.invoice)
        elif args.cmd == "record-payment":
            paid = parse_date(args.date) if args.date else date.today()
            balance = ledger.record_payment(args.invoice, args.amount, paid)
            out = "payment recorded; balance due: %s" % format_paise(balance)
        elif args.cmd == "list":
            mutate = False
            rows = ledger.list_invoices(as_of, args.status)
            lines = [_fmt_row(["invoice", "customer", "due", "total", "balance", "status"],
                              [18, 10, 12, 12, 12, 8])]
            for inv, st, totals in rows:
                lines.append(_fmt_row(
                    [inv["number"] or inv["id"] + " (draft)", inv["customer_id"],
                     inv["due_date"], format_paise(totals["total"]),
                     format_paise(totals["balance"]), st],
                    [18, 10, 12, 12, 12, 8]))
            out = "\n".join(lines) if rows else "no invoices match"
        elif args.cmd == "show":
            mutate = False
            inv = ledger.get_invoice(args.invoice)
            totals = invoice_totals(inv)
            cust = ledger._customer(inv["customer_id"])
            parts = [
                "%s  (%s)" % (inv["number"] or inv["id"], status_of(inv, as_of)),
                "customer : %s (%s)" % (cust["name"], cust["id"]),
                "issued   : %s   due: %s" % (inv["issue_date"], inv["due_date"]),
                "supply   : %s" % ("intra-state (CGST+SGST)" if inv["intra_state"]
                                   else "inter-state (IGST)"),
                "lines:",
            ]
            for ln in inv["lines"]:
                t, c, s, g, _tot = line_amounts(ln, inv["intra_state"])
                tax = ("CGST %s + SGST %s" % (format_paise(c), format_paise(s))
                       if inv["intra_state"] else "IGST %s" % format_paise(g))
                parts.append("  %dx %s @ %s = %s  (GST %s%%: %s)" % (
                    ln["qty"], ln["description"], format_paise(ln["rate_paise"]),
                    format_paise(t), ln["gst_rate"], tax))
            parts.append("taxable  : %s" % format_paise(totals["taxable"]))
            parts.append("GST      : %s" % format_paise(totals["gst"]))
            parts.append("total    : %s" % format_paise(totals["total"]))
            for pmt in inv["payments"]:
                parts.append("paid     : %s on %s" % (format_paise(pmt["amount_paise"]), pmt["date"]))
            parts.append("balance  : %s" % format_paise(totals["balance"]))
            out = "\n".join(parts)
        elif args.cmd == "aging":
            mutate = False
            report = ledger.aging_report(as_of)
            lines = ["aging as of %s" % as_of.isoformat()]
            for bucket in AGING_BUCKETS:
                b = report[bucket]
                lines.append("  %-8s %2d invoice(s)  %s" % (
                    bucket, b["count"], format_paise(b["balance"])))
            out = "\n".join(lines)
        elif args.cmd == "overdue":
            mutate = False
            rows = ledger.list_invoices(as_of, "overdue")
            lines = []
            for inv, _st, totals in rows:
                cust = ledger._customer(inv["customer_id"])
                days = (as_of - date.fromisoformat(inv["due_date"])).days
                lines.append("%s  %s  %s due  (%d days late)" % (
                    inv["number"], cust["name"], format_paise(totals["balance"]), days))
            out = "\n".join(lines) if lines else "nothing overdue"
        elif args.cmd == "summary":
            mutate = False
            s = ledger.summary(as_of)
            lines = [
                "customers     : %d" % s["customers"],
                "invoices      : %d (%s)" % (
                    s["invoices"],
                    ", ".join("%s %d" % kv for kv in sorted(s["by_status"].items())) or "none"),
                "outstanding   : %s" % format_paise(s["outstanding"]),
                "overdue       : %s" % format_paise(s["overdue"]),
            ]
            out = "\n".join(lines)
        elif args.cmd == "export-csv":
            mutate = False
            ledger.export_csv(args.path, as_of)
            out = "wrote %s" % args.path
        else:  # pragma: no cover - argparse enforces choices
            raise LedgerError("unknown command")

        if mutate:
            ledger.save(args.db)
        print(out)
        return 0
    except LedgerError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
