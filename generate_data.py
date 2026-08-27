"""
generate_data.py  (v2 - Razorpay-authentic terminology + multi-event orders)
-----------------------------------------------------------------------------
Creates three raw source files:
  1. data/orders.csv            -> the shop's own sales system
  2. data/settlement_report.csv -> Razorpay's settlement report
  3. data/bank_statement.csv    -> the shop's actual bank statement

KEY UPGRADE FROM v1: a single order can now have MORE THAN ONE settlement
event, on different dates, in different bank payouts. This is real - a
chargeback or a delayed refund often lands in a completely separate later
payout from the original payment. v1 wrongly assumed "one order = one
payout", which broke on exactly this case.

Terminology matches Razorpay's own public settlement/report vocabulary:
entity_type, settlement_id, settlement_utr, fee, tax (GST on fee), arn,
dispute_id, settled_at.
"""

import csv
import random
from collections import defaultdict
from datetime import datetime, timedelta

random.seed(42)

FEE_RATES = {
    "upi": 0.02, "debit_card": 0.02, "credit_card": 0.02, "netbanking": 0.02,
    "wallet": 0.02, "rupay_credit_upi": 0.0215, "emi": 0.03,
    "international_card": 0.03, "amex": 0.03,
}
GST_ON_FEE = 0.18
BASE_DATE = datetime(2026, 8, 1)


def calc_fee(gross, method):
    fee = round(gross * FEE_RATES[method], 2)
    gst = round(fee * GST_ON_FEE, 2)
    net = round(gross - fee - gst, 2)
    return fee, gst, net


def add_business_days(start_date, days):
    d = start_date
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return d


_order_counter = 0
_settlement_counter = 0
_utr_counter = 700000
_payment_counter = 0


def new_order_id():
    global _order_counter
    _order_counter += 1
    return f"ORD{_order_counter:04d}"


def new_settlement_id():
    global _settlement_counter
    _settlement_counter += 1
    return f"STL{_settlement_counter:04d}"


def new_utr():
    global _utr_counter
    _utr_counter += 1
    return f"UTR{_utr_counter}"


def new_payment_id():
    global _payment_counter
    _payment_counter += 1
    return f"PAY{_payment_counter:05d}"


orders = []              # rows for orders.csv
settlement_events = []   # rows for settlement_report.csv (multiple per order allowed)

# order_ids that should NEVER get a bank credit (planted: pending / overdue)
NEVER_REACHES_BANK = set()


def add_order(gross, method, order_date, status="captured"):
    oid = new_order_id()
    orders.append({"order_id": oid, "order_date": order_date, "item_amount": gross,
                    "payment_method": method, "order_status": status})
    return oid


def add_settlement_event(order_id, gross, method, event_date, delay_days, entity_type,
                           settlement_id=None, override_net=None, txn_status="settled",
                           arn="", dispute_id="", notes=""):
    fee, gst, net = calc_fee(gross, method) if gross != "" else (0, 0, 0)
    if override_net is not None:
        net = override_net
    settled_at = add_business_days(event_date, delay_days)
    row = {
        "payment_id": new_payment_id(), "order_id": order_id,
        "settlement_id": settlement_id or new_settlement_id(),
        "entity_type": entity_type, "payment_method": method,
        "gross_amount": gross, "fee": fee if entity_type == "payment" else 0,
        "gst_on_fee": gst if entity_type == "payment" else 0, "net_amount": net,
        "settled_at": settled_at.strftime("%Y-%m-%d"), "txn_status": txn_status,
        "arn": arn, "dispute_id": dispute_id, "notes": notes,
    }
    settlement_events.append(row)
    return row


# ---------------------------------------------------------------------------
# 45 normal, cleanly-matching transactions across all payment methods
# ---------------------------------------------------------------------------
methods = list(FEE_RATES.keys())
for i in range(45):
    method = random.choice(methods)
    gross = round(random.uniform(200, 15000), 2)
    order_date = BASE_DATE + timedelta(days=random.randint(0, 20), hours=random.randint(8, 21))
    oid = add_order(gross, method, order_date)
    add_settlement_event(oid, gross, method, order_date, 2, "payment")

# ---------------------------------------------------------------------------
# Special cases (each tests one specific real-world reconciliation problem)
# ---------------------------------------------------------------------------

# 1. Weekend delay
fri = datetime(2026, 8, 7, 23, 10)
oid = add_order(2000, "upi", fri)
add_settlement_event(oid, 2000, "upi", fri, 2, "payment")

# 2. Full refund
oid = add_order(5000, "credit_card", BASE_DATE + timedelta(days=5), status="refunded")
fee, gst, _ = calc_fee(5000, "credit_card")
add_settlement_event(oid, 5000, "credit_card", BASE_DATE + timedelta(days=5), 2, "refund",
                       override_net=round(-5000 + fee + gst, 2), txn_status="refunded")

# 3. Partial refund
oid = add_order(3000, "debit_card", BASE_DATE + timedelta(days=6), status="partially_refunded")
fee, gst, _ = calc_fee(3000, "debit_card")
add_settlement_event(oid, 3000, "debit_card", BASE_DATE + timedelta(days=6), 2, "payment",
                       override_net=round(3000 - fee - gst - 1200, 2), txn_status="partially_refunded")

# 4. Still pending (no bank entry yet - correct and normal, not an error)
oid = add_order(1800, "upi", BASE_DATE + timedelta(days=19))
add_settlement_event(oid, 1800, "upi", BASE_DATE + timedelta(days=19), 2, "payment")
NEVER_REACHES_BANK.add(oid)

# 5. Duplicate settlement row (Razorpay's own reporting bug)
oid = add_order(999, "upi", BASE_DATE + timedelta(days=4))
row = add_settlement_event(oid, 999, "upi", BASE_DATE + timedelta(days=4), 2, "payment")
settlement_events.append(dict(row, payment_id=row["payment_id"]))  # exact duplicate row

# 6. Overdue - missing bank entry
oid = add_order(4200, "netbanking", BASE_DATE + timedelta(days=8))
add_settlement_event(oid, 4200, "netbanking", BASE_DATE + timedelta(days=8), 2, "payment")
NEVER_REACHES_BANK.add(oid)

# 7. Rounding difference
oid = add_order(333.33, "credit_card", BASE_DATE + timedelta(days=11))
fee, gst, net = calc_fee(333.33, "credit_card")
add_settlement_event(oid, 333.33, "credit_card", BASE_DATE + timedelta(days=11), 2, "payment",
                       override_net=round(net - 0.02, 2))

# 8. Failed transaction (correctly excluded - no settlement at all)
add_order(2200, "credit_card", BASE_DATE + timedelta(days=13), status="failed")

# 9 & 10. Batched settlement: two unrelated orders paid out in ONE bank credit
shared_id = new_settlement_id()
oid_a = add_order(1000, "upi", BASE_DATE + timedelta(days=14))
oid_b = add_order(1500, "upi", BASE_DATE + timedelta(days=14))
add_settlement_event(oid_a, 1000, "upi", BASE_DATE + timedelta(days=14), 2, "payment", settlement_id=shared_id)
add_settlement_event(oid_b, 1500, "upi", BASE_DATE + timedelta(days=14), 2, "payment", settlement_id=shared_id)

# 11. RuPay credit card via UPI (the 2.15% "hidden" rate)
oid = add_order(2500, "rupay_credit_upi", BASE_DATE + timedelta(days=15))
add_settlement_event(oid, 2500, "rupay_credit_upi", BASE_DATE + timedelta(days=15), 2, "payment")

# 12. EMI (3% premium rate)
oid = add_order(12000, "emi", BASE_DATE + timedelta(days=16))
add_settlement_event(oid, 12000, "emi", BASE_DATE + timedelta(days=16), 2, "payment")

# 13. Missing from gateway entirely (order exists, Razorpay has nothing)
add_order(750, "upi", BASE_DATE + timedelta(days=17))

# 14. Duplicate order entry (shop's own ledger logs the same sale twice)
oid = add_order(1250, "debit_card", BASE_DATE + timedelta(days=18))
add_settlement_event(oid, 1250, "debit_card", BASE_DATE + timedelta(days=18), 2, "payment")
orders.append(dict(orders[-1]))  # duplicate row, shop's ledger only

# 15 & 16. Clean matches at different fee tiers
oid = add_order(9000, "international_card", BASE_DATE + timedelta(days=9))
add_settlement_event(oid, 9000, "international_card", BASE_DATE + timedelta(days=9), 2, "payment")
oid = add_order(4500, "amex", BASE_DATE + timedelta(days=3))
add_settlement_event(oid, 4500, "amex", BASE_DATE + timedelta(days=3), 2, "payment")

# ---------------------------------------------------------------------------
# NEW (Perplexity-recommended) scenarios
# ---------------------------------------------------------------------------

# 17. Settlement hold, then released later (past normal T+2, but resolves correctly)
oid = add_order(7000, "credit_card", BASE_DATE + timedelta(days=2))
add_settlement_event(oid, 7000, "credit_card", BASE_DATE + timedelta(days=2), 7, "payment",
                       txn_status="released_after_hold")

# 18. Adjustment-only row, no order_id, correctly matched to its own bank debit
adj_id = new_settlement_id()
add_settlement_event("", "", "", BASE_DATE + timedelta(days=10), 0, "adjustment",
                       settlement_id=adj_id, override_net=-150.00,
                       notes="Manual correction - duplicate payout reversal")

# 19. Orphan adjustment: added directly to the bank statement further down,
#     with NO settlement row at all - tests honest "unresolved", not a forced match.

# 20. Refund deducted from a LATER, unrelated batch (not same-day as the sale)
orig_date = BASE_DATE + timedelta(days=1)
oid = add_order(6500, "upi", orig_date)
add_settlement_event(oid, 6500, "upi", orig_date, 2, "payment")  # original settles fine
fee, gst, _ = calc_fee(6500, "upi")
later_batch_id = new_settlement_id()
other_oid = add_order(2200, "upi", BASE_DATE + timedelta(days=18))
add_settlement_event(oid, 6500, "upi", BASE_DATE + timedelta(days=18), 2, "refund",
                       settlement_id=later_batch_id, override_net=round(-6500 + fee + gst, 2),
                       txn_status="refunded", arn="ARN90012345", notes="Refund processed in a later cycle")
add_settlement_event(other_oid, 2200, "upi", BASE_DATE + timedelta(days=18), 2, "payment",
                       settlement_id=later_batch_id)

# 21. Chargeback: principal reversal AND fee as TWO SEPARATE line items, in a later payout
orig_date = BASE_DATE + timedelta(days=12)
oid = add_order(6000, "international_card", orig_date)
add_settlement_event(oid, 6000, "international_card", orig_date, 2, "payment")
cb_id = new_settlement_id()
add_settlement_event(oid, 6000, "international_card", orig_date + timedelta(days=25), 0, "chargeback",
                       settlement_id=cb_id, override_net=-6000.00, txn_status="chargeback",
                       dispute_id="DSP4471", notes="Principal reversal for disputed transaction")
add_settlement_event(oid, "", "international_card", orig_date + timedelta(days=25), 0, "adjustment",
                       settlement_id=cb_id, override_net=-250.00,
                       dispute_id="DSP4471", notes="Chargeback processing fee")

print(f"Total orders: {len(orders)}, total settlement events: {len(settlement_events)}")

# ---------------------------------------------------------------------------
# Assign a settlement_utr to every settlement_id group, aggregating bank
# totals PER UTR - across order_id and entity_type, since a bank payout
# doesn't care what kind of entity it's paying for, only the total amount.
# ---------------------------------------------------------------------------
settlement_id_to_utr = {}
bank_totals = defaultdict(float)
bank_dates = {}
seen_for_bank = set()

for e in settlement_events:
    if e["order_id"] in NEVER_REACHES_BANK:
        continue
    sid = e["settlement_id"]
    dedupe_key = (sid, e["payment_id"])
    if dedupe_key in seen_for_bank:
        continue
    seen_for_bank.add(dedupe_key)
    bank_totals[sid] += e["net_amount"]
    bank_dates[sid] = e["settled_at"]
    if sid not in settlement_id_to_utr:
        settlement_id_to_utr[sid] = new_utr()

for e in settlement_events:
    e["settlement_utr"] = settlement_id_to_utr.get(e["settlement_id"], "")

with open("data/orders.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["order_id", "order_date", "item_amount", "payment_method", "order_status"])
    for o in orders:
        d = o["order_date"]
        writer.writerow([o["order_id"], d.strftime("%Y-%m-%d %H:%M") if hasattr(d, "strftime") else d,
                          o["item_amount"], o["payment_method"], o["order_status"]])

with open("data/settlement_report.csv", "w", newline="") as f:
    fieldnames = ["payment_id", "order_id", "settlement_id", "settlement_utr", "entity_type",
                  "payment_method", "gross_amount", "fee", "gst_on_fee", "net_amount",
                  "settled_at", "txn_status", "arn", "dispute_id", "notes"]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(settlement_events)

bank_rows = []
for sid, amount in bank_totals.items():
    utr = settlement_id_to_utr[sid]
    bank_rows.append({"txn_date": bank_dates[sid],
                        "description": f"NEFT CR RAZORPAY SETTLEMENT {utr}",
                        "utr": utr, "credit_amount": round(amount, 2)})

# Phantom credit: money in the bank, no matching settlement at all
bank_rows.append({"txn_date": "2026-08-12", "description": "NEFT CR RAZORPAY SETTLEMENT UTR799999",
                    "utr": "UTR799999", "credit_amount": 1500.00})

# Orphan adjustment debit: bank shows a deduction with no settlement row explaining it
bank_rows.append({"txn_date": "2026-08-19", "description": "NEFT DR BANK CHARGES UTR899999",
                    "utr": "UTR899999", "credit_amount": -75.00})

with open("data/bank_statement.csv", "w", newline="") as f:
    fieldnames = ["txn_date", "description", "utr", "credit_amount"]
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(bank_rows)

print(f"orders.csv -> {len(orders)} rows | settlement_report.csv -> {len(settlement_events)} rows "
      f"| bank_statement.csv -> {len(bank_rows)} rows")
