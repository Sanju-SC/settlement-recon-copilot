# Settlement Reconciliation Copilot

Built for Razorpay's Buildathon **Track 4 - AI Finance Controller**.

## What it does

Automatically checks a business's sales ledger, Razorpay's settlement report, and its
bank statement against each other, and explains exactly why any of them don't line up -
instead of an accountant spending hours hunting for it by hand.

## Why reconciliation is hard

When a customer pays through Razorpay, the same payment shows up in three places that
almost never agree cleanly: the shop's own sale record, Razorpay's settlement report
(after fees), and the bank credit that actually lands. The gap between them isn't a bug -
it's caused by settlement delays, variable fees per payment method, refunds landing in a
*later* payout than the original sale, chargebacks with their own separate fee, batched
payouts covering several orders at once, and settlement holds for risk review. This tool
automates untangling all of that.

## Supported record types

`payment`, `refund`, `chargeback`, `adjustment` - matching Razorpay's own settlement
report vocabulary (`entity_type`).

## Matching strategy: evidence-first, never a forced guess

1. **order_id** - links the shop's ledger to Razorpay's settlement report
2. **settlement_utr** - links settlement events to the actual bank credit. This is done
   by grouping ALL settlement rows that share a UTR (which may span several orders in one
   batch, or several entity types for one order across time - see below) and comparing
   the group's combined total to the bank, once
3. Anything left over becomes a categorized, explained exception - never silently forced
   into a match. **A false match is more dangerous than an honest "unresolved", because it
   creates false confidence.**

### A key realism point this project specifically handles

One order can have **more than one settlement event, in completely separate bank
payouts, on different dates** - most obviously with chargebacks (the original payment
settles normally, then weeks later a *separate* payout reverses it and deducts a fee as
two distinct line items) and refunds processed in a later cycle. The matching engine
groups by `settlement_utr`, not by order, specifically so this works correctly instead of
wrongly assuming "one order = one payout."

## Exception taxonomy

| Category | Meaning |
|---|---|
| Missing from gateway | Order exists in the shop's ledger; Razorpay has no record of it |
| Pending settlement (normal) | Still inside the normal settlement window - not an error |
| Overdue - missing bank entry | Settlement date has passed with no bank credit - needs investigation |
| Unexplained bank entry | A bank credit or debit with no matching settlement record at all |
| Amount mismatch | UTR matched, but the total differs - sent to the AI explanation layer |

## Auditability and determinism

Every decision - every match, every exception, every dedupe - is logged to
`output/audit_trail.json` with the method used and a plain-language reason. Matching,
fee math, and totals are 100% deterministic code. AI is used for exactly one job:
explaining an unresolved amount mismatch in plain English. **AI never decides what
matches what - only rules do.**

## Metrics on the 69-event synthetic set

Run `python reconcile.py` to regenerate current numbers. Last verified run:
match rate 92.5%, 6 honest exceptions (including one genuine amount mismatch that the
AI explanation layer actually resolves - see below), Rs 42.50 unresolved value.

**Note on the AI layer actually running:** most mismatches in real reconciliation data
have a clean, rule-based reason (a fee, a refund, a timing delay) - so the AI step isn't
needed for those, by design. To make sure the AI explanation path is genuinely exercised
and demonstrable (not just present in the code, unused), the test data includes one
transaction with a small, deliberately unexplained shortfall that no rule can account
for - this is the one case that actually reaches `ai_explain.py`.

## Edge cases deliberately planted (22 scenarios)

Clean matches across every payment method and fee tier, a full refund, a partial
refund, a weekend settlement delay, a duplicate row in Razorpay's own report, a
duplicate entry in the shop's own ledger, an order missing from the gateway entirely, a
rounding difference, a still-pending settlement (correctly not flagged), an overdue
missing bank entry (correctly flagged), a bank credit with no settlement record
("phantom credit"), **a settlement placed on hold and released later**, **a
standalone adjustment row with no order_id**, **an unexplained orphan bank debit**, **a
refund processed in a later, unrelated settlement batch**, **a chargeback with its
principal reversal and processing fee as two separate line items in a separate later
payout**, and **a genuinely unexplained partial shortfall** that no rule can account
for - the one case that actually exercises the AI explanation layer.

## AI usage policy

`ai_explain.py` is the only file that touches an AI model, and only for unresolved
amount mismatches. If no `ANTHROPIC_API_KEY` is set, or the API call fails for any
reason, it falls back to rule-based reasoning automatically - this is the project's
deliberately-handled failure case, so a live demo never breaks.

## Architecture

```
data/*.csv --> reconcile.py --> output/metrics.json
                   |              output/exceptions_report.csv
                   |              output/audit_trail.json
                   v
             ai_explain.py  (only for unresolved amount mismatches)
                   +--> real AI call if ANTHROPIC_API_KEY is set
                   +--> rule-based fallback otherwise

dashboard.py --> reads the output/ files above and displays them visually
```

## How to run it

```
pip install -r requirements.txt
python generate_data.py       # (re)creates the three sample CSVs in data/
python reconcile.py           # runs the reconciliation, writes output/
streamlit run dashboard.py    # opens the visual dashboard in your browser
```

To turn on real AI explanations:
```
export ANTHROPIC_API_KEY="your-key-here"      # Mac/Linux
$env:ANTHROPIC_API_KEY="your-key-here"        # Windows PowerShell
```

## Known limitations (honest, on purpose)

- Single currency (INR) only - no multi-currency/FX handling
- Matching uses exact IDs and exact-total batch/group comparisons only; no fuzzy
  matching for corrupted or missing identifiers, since a wrong guess is worse than an
  honest "unresolved"
- Rounding tolerance is a fixed Rs 0.05
- Synthetic data only - no live Razorpay account is connected

## Tech stack

Python, Streamlit (dashboard), Anthropic API (optional AI explanation layer with a
rule-based fallback), CSV/JSON for data interchange.
