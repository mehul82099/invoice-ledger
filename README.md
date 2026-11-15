# invoice-ledger

A zero-dependency Python CLI that tracks the money customers owe a small
business: GST invoices, partial payments, and overdue aging - the follow-up
half of invoice management that eats hours every week.

**Why this exists:** Razorpay's [Fix My Itch](https://razorpay.com/m/fix-my-itch/)
initiative lists *"Why do micro-SMEs waste 10+ hours weekly on invoice
management?"* (Itch Score 67.5, B2B Services; list checked on 2026-11-14) as
one of India's crowdsourced problems. That listing means the problem was
submitted and curated by Razorpay's initiative - it is **not** independent
evidence of market size or demand, and this repo does not claim any.

Most small open-source GST tools found in a GitHub search on 2026-11-14
(queries: `gst invoice cli`, `invoice tracker zero dependency`) focus on
**generating** invoice PDFs or extracting data from them. This tool instead
covers the tracking side: who owes what, which invoice is how late, and
whether that "payment sent" was the full amount. It leaves PDF design alone.

## What it does

- Customers and draft invoices, stored in one local JSON file
- Indian GST math per line: CGST+SGST split for intra-state supply, IGST for
  inter-state; paise-exact integer arithmetic (no float drift)
- Invoice numbers in Indian-financial-year format (`INV-2026-27/0001`),
  sequence restarting each April
- Partial payments with overpayment protection
- Status tracking: draft / sent / partial / overdue / paid
- Receivables aging report (not due / 1-30 / 31-60 / 61-90 / 90+ days)
- CSV export of finalized invoices for your accountant
- 52 tests; stdlib only; runs on Python 3.9+ on Linux and Windows

## Install

Needs only Python 3.9 or newer. No packages, no network, no accounts.

```bash
git clone https://github.com/mehul82099/invoice-ledger.git
cd invoice-ledger
python invoice_ledger.py --help
```

## Usage

```bash
# add a customer
python invoice_ledger.py add-customer --name "Sharma Traders" --phone "98xxxxxx01"
# -> added customer c1: Sharma Traders

# create a draft invoice (due in 30 days by default)
python invoice_ledger.py new-invoice --customer c1 --due-days 30
# -> created draft i1 for c1 (due 2026-12-14)

# add lines: qty, unit rate in rupees, GST slab
python invoice_ledger.py add-line --invoice i1 --description "Rice 25kg" \
    --qty 2 --rate 1500.00 --gst 5

# finalize: assigns INV-2026-27/0001 and freezes the lines
python invoice_ledger.py finalize --invoice i1

# record a partial payment
python invoice_ledger.py record-payment --invoice INV-2026-27/0001 --amount 1500.00

# what is owed, and how late is it?
python invoice_ledger.py overdue
python invoice_ledger.py aging
python invoice_ledger.py summary

# month-end handoff to the accountant
python invoice_ledger.py export-csv november.csv
```

Inter-state supply (customer in another state) charges IGST instead of
CGST+SGST:

```bash
python invoice_ledger.py new-invoice --customer c1 --inter-state
```

Every command takes `--as-of YYYY-MM-DD` for reports and `--db PATH` for the
store file (default `./invoice-ledger.json`).

## GST rates: you choose, the tool only computes

The GST rate on each line is **user-supplied**. The tool enforces only a
sanity range (0-100%) and then does paise-exact arithmetic. It does not
know or check which rate applies to your goods or services, and it does not
decide intra- vs inter-state supply for you (you pass `--inter-state`).

Why no built-in slab list: GST rates are set by notification and depend on
the item **and the date of supply**, so any hardcoded list goes stale and
can reject legitimate entries. This tool stays out of tax determination
entirely. For current rates and how they apply, use the official sources
(both checked 2026-11-14) and your CA:

- GST Council press releases and meeting decisions: https://gstcouncil.gov.in
- CBIC (Central Board of Indirect Taxes and Customs): https://www.cbic.gov.in

Nothing in this repo is tax advice; the tests prove the arithmetic, not
compliance.

## Limits (read before relying on it)

- **Not an e-invoice**: no IRN/QR generation via the GST portal, no GSTR
  filing, no e-way bills. If your turnover makes e-invoicing mandatory, this
  is not that.
- **Not a PDF maker**: no printable invoice layout; pair it with whatever
  you already use for the document itself.
- **Not tax advice**: GST math here is arithmetic only (rate x taxable).
  Whether a rate applies to your goods, and intra- vs inter-state place of
  supply, are legal determinations this tool does not make.
- Single user, single local file. Saves write to a temp file and then
  replace the store in one step, and a failed save leaves the previous
  store untouched (both covered by tests). There is no guarantee against
  power loss at the instant of replacement, and concurrent writers on the
  same file are not supported. No backup or encryption either - keep the
  JSON file somewhere safe and copy it elsewhere regularly.
- No payment links, reminders, email or WhatsApp sending - it never talks to
  your customers or the network.

## Tests

```bash
python -m unittest -v
```

52 tests covering money parsing/formatting, GST slab validation, CGST/SGST
rounding, financial-year numbering, payment and status transitions, aging
bucket boundaries, CSV export, persistence, and the CLI end-to-end. CI runs
them on ubuntu-24.04 and windows-latest x Python 3.9 and 3.13.

## License

MIT - see [LICENSE](LICENSE).
