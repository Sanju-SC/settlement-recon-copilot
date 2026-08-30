"""
reconcile.py  (v2 - UTR-first matching, supports multi-event orders)
----------------------------------------------------------------------
KEY DESIGN CHANGE FROM v1: matching is now done by grouping settlement
rows by settlement_utr FIRST (the bank payout is the real unit of truth),
not by order_id first. This is what makes it possible for one order to
have a normal payment AND a much-later chargeback in a completely
separate bank payout - v1 couldn't handle that correctly.

Stages, strongest evidence first:
  A. Order-level checks: duplicate ledger rows, failed transactions
     (excluded), orders with no settlement record at all (missing from
     gateway)
  B. Settlement rows deduped by payment_id, then grouped by settlement_id
     -> settlement_utr
  C. Each UTR group's total net_amount is compared to the bank credit for
     that UTR (or, if no UTR was ever assigned, judged pending vs overdue
     by its settled_at date)
  D. Bank credits whose UTR matches no settlement group at all -> flagged,
     never silently ignored

Run: python reconcile.py
Writes: output/exceptions_report.csv, output/audit_trail.json, output/metrics.json
"""

import csv
import json
from collections import defaultdict
from datetime import datetime

from ai_explain import explain_exception, suggest_next_step, draft_followup_message, generate_summary

REPORT_AS_OF = datetime(2026, 8, 22)
AMOUNT_TOLERANCE = 0.05


def load_csv(path):
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def is_pending_or_overdue(settled_at_str):
    settled_at = datetime.strptime(settled_at_str, "%Y-%m-%d")
    return "pending_normal" if settled_at > REPORT_AS_OF else "overdue_missing_bank_entry"


# ---------------------------------------------------------------------------
# Stage A: order-level checks
# ---------------------------------------------------------------------------
def check_orders(orders, settlements):
    settlement_order_ids = {s["order_id"] for s in settlements if s["order_id"]}
    audit = []
    excluded_failed = set()
    seen = set()

    for o in orders:
        oid = o["order_id"]
        if oid in seen:
            audit.append({"stage": "order_level", "order_id": oid, "method": "duplicate_ledger_row",
                           "note": "This order_id already appeared earlier in orders.csv - "
                                   "treated as a duplicate log entry, not a second real sale.",
                           "confidence": "high"})
            continue
        seen.add(oid)

        if o["order_status"] == "failed":
            excluded_failed.add(oid)
            audit.append({"stage": "order_level", "order_id": oid, "method": "excluded_failed_transaction",
                           "note": "Order status is 'failed' - correctly expected to have no "
                                   "settlement or bank record.", "confidence": "high"})
            continue

        if oid not in settlement_order_ids:
            audit.append({"stage": "order_level", "order_id": oid, "method": "missing_from_gateway",
                           "note": "Order exists in the shop's ledger but Razorpay's settlement "
                                   "report has no record of it at all.", "confidence": "high"})

    missing_from_gateway = [
        o["order_id"] for o in orders
        if o["order_id"] not in settlement_order_ids and o["order_status"] != "failed"
    ]
    return audit, excluded_failed, set(missing_from_gateway)


# ---------------------------------------------------------------------------
# Stage B + C: dedupe, group by settlement_id -> settlement_utr, compare to bank
# ---------------------------------------------------------------------------
def match_settlements_to_bank(settlements, bank_rows, excluded_failed):
    audit = []
    bank_by_utr = {b["utr"]: b for b in bank_rows}

    seen_payment_ids = set()
    groups = defaultdict(list)   # settlement_id -> rows
    for s in settlements:
        if s["order_id"] in excluded_failed:
            continue
        pid = s["payment_id"]
        if pid in seen_payment_ids:
            audit.append({"stage": "settlement_dedupe", "order_id": s["order_id"],
                           "method": "duplicate_settlement_row",
                           "note": f"payment_id {pid} appears more than once in Razorpay's own "
                                   f"settlement report. Treated as one row.", "confidence": "high"})
            continue
        seen_payment_ids.add(pid)
        groups[s["settlement_id"]].append(s)

    results = []
    claimed_utrs = set()

    for sid, rows in groups.items():
        utr = rows[0]["settlement_utr"]
        order_ids = sorted({r["order_id"] for r in rows if r["order_id"]})
        entity_types = sorted({r["entity_type"] for r in rows})
        expected_total = round(sum(float(r["net_amount"]) for r in rows), 2)
        settled_at = rows[0]["settled_at"]
        held_then_released = any(r["txn_status"] == "released_after_hold" for r in rows)

        if not utr:
            status = is_pending_or_overdue(settled_at)
            note = (f"Settlement {sid} ({', '.join(entity_types)}, orders: "
                    f"{', '.join(order_ids) or 'none'}) has no bank credit yet. "
                    + ("Still within a normal window - not an error."
                       if status == "pending_normal" else
                       "Settlement date has passed - needs investigation."))
            audit.append({"stage": "settlement_to_bank", "order_id": ", ".join(order_ids) or None,
                           "method": status, "note": note,
                           "confidence": "high" if status == "pending_normal" else "medium"})
            results.append({"settlement_id": sid, "utr": utr, "order_ids": order_ids,
                             "entity_types": entity_types, "status": status,
                             "expected_total": expected_total, "bank_row": None, "diff": None})
            continue

        claimed_utrs.add(utr)
        bank_row = bank_by_utr.get(utr)
        if bank_row is None:
            status = is_pending_or_overdue(settled_at)
            results.append({"settlement_id": sid, "utr": utr, "order_ids": order_ids,
                             "entity_types": entity_types, "status": status,
                             "expected_total": expected_total, "bank_row": None, "diff": None})
            continue

        actual = float(bank_row["credit_amount"])
        diff = round(actual - expected_total, 2)
        status = "matched" if abs(diff) <= AMOUNT_TOLERANCE else "amount_mismatch"
        batch_note = f" (batch: {', '.join(order_ids)})" if len(order_ids) > 1 else ""
        note = (f"UTR {utr}{batch_note} [{', '.join(entity_types)}]: expected {expected_total}, "
                f"bank shows {actual}" + (". Matches." if status == "matched" else f" (diff {diff}).")
                + (" Settlement was held then released - took longer than normal but resolved correctly."
                   if held_then_released and status == "matched" else ""))
        audit.append({"stage": "settlement_to_bank", "order_id": ", ".join(order_ids) or None,
                       "method": "utr_match" if status == "matched" else "utr_amount_differs",
                       "note": note, "confidence": "high" if status == "matched" else "low"})
        results.append({"settlement_id": sid, "utr": utr, "order_ids": order_ids,
                         "entity_types": entity_types, "status": status,
                         "expected_total": expected_total, "bank_row": bank_row, "diff": diff,
                         "held_then_released": held_then_released})

    # Bank credits whose UTR matches no settlement group at all
    for b in bank_rows:
        if b["utr"] not in claimed_utrs:
            audit.append({"stage": "settlement_to_bank", "order_id": None, "method": "unexplained_bank_row",
                           "note": f"Bank entry of {b['credit_amount']} (UTR {b['utr']}) has no matching "
                                   f"settlement record at all. Needs investigation.", "confidence": "medium"})
            results.append({"settlement_id": None, "utr": b["utr"], "order_ids": [], "entity_types": [],
                             "status": "unexplained_bank_row", "expected_total": None,
                             "bank_row": b, "diff": None})

    return results, audit


# ---------------------------------------------------------------------------
# Build the human-facing exception list
# ---------------------------------------------------------------------------
def build_exceptions(results):
    exceptions = []
    for r in results:
        status = r["status"]
        if status == "matched":
            continue

        base = {"order_id": ", ".join(r["order_ids"]) or "", "utr": r.get("utr") or "",
                 "entity_type": ", ".join(r["entity_types"]), "status": status}

        if status == "pending_normal":
            base["category"] = "Pending settlement (normal)"
            base["explanation"] = "Still within the normal settlement window - not an error."
        elif status == "overdue_missing_bank_entry":
            base["category"] = "Overdue - missing bank entry"
            base["explanation"] = ("Razorpay's report says this settled, but the money never "
                                    "showed up in the bank statement, and it's now overdue.")
        elif status == "unexplained_bank_row":
            b = r["bank_row"]
            base["category"] = "Unexplained bank entry"
            direction = "credit" if float(b["credit_amount"]) >= 0 else "debit"
            base["explanation"] = (f"A bank {direction} of {b['credit_amount']} has no matching "
                                    f"settlement record. Could be an unrecorded adjustment or a "
                                    f"misapplied transfer.")
        elif status == "amount_mismatch":
            diff = r["diff"]
            explanation = explain_exception({
                "order_id": base["order_id"] or "(adjustment, no order)",
                "expected_total": r["expected_total"],
                "actual_total": float(r["bank_row"]["credit_amount"]),
                "difference": diff, "order_status": None,
            })
            base["category"] = "Amount mismatch"
            base["explanation"] = explanation
            base["difference"] = diff
        else:
            base["category"] = "Unclassified"
            base["explanation"] = "Did not fit a known category - flagged for manual review."

        # For anything that actually needs a human to DO something (not the
        # harmless "still pending, wait" case), add a suggested action and a
        # ready-to-copy message. Nothing here ever sends anything - it only
        # drafts text for a human to review and send themselves.
        if base["category"] != "Pending settlement (normal)":
            base["suggested_action"] = suggest_next_step(base)
            base["draft_message"] = draft_followup_message(base)
        else:
            base["suggested_action"] = ""
            base["draft_message"] = ""

        exceptions.append(base)
    return exceptions


def compute_metrics(results, order_audit, missing_from_gateway, exceptions):
    total = len(results)
    matched = len([r for r in results if r["status"] == "matched"])
    match_rate = round(100 * matched / total, 1) if total else 0.0
    reconciled_value = sum(float(r["bank_row"]["credit_amount"]) for r in results if r["status"] == "matched")
    unresolved_value = sum(
        abs(float(r["diff"])) for r in results if r.get("diff") not in (None,) and r["status"] != "matched"
    )
    return {
        "report_as_of": REPORT_AS_OF.date().isoformat(),
        "total_settlement_groups_checked": total,
        "matched": matched,
        "match_rate_percent": match_rate,
        "orders_missing_from_gateway": len(missing_from_gateway),
        "exceptions_count": len(exceptions),
        "reconciled_value": round(reconciled_value, 2),
        "unresolved_value": round(unresolved_value, 2),
    }


def main():
    orders = load_csv("data/orders.csv")
    settlements = load_csv("data/settlement_report.csv")
    bank = load_csv("data/bank_statement.csv")

    order_audit, excluded_failed, missing_from_gateway = check_orders(orders, settlements)
    results, bank_audit = match_settlements_to_bank(settlements, bank, excluded_failed)
    exceptions = build_exceptions(results)

    # Missing-from-gateway orders have no settlement group at all, so add them
    # as exceptions explicitly here (they never appear in `results`).
    for oid in sorted(missing_from_gateway):
        mfg = {"order_id": oid, "utr": "", "entity_type": "", "status": "missing_from_gateway",
               "category": "Missing from gateway",
               "explanation": "This order exists in the shop's own sales system, but "
                              "Razorpay's settlement report has no record of it at all."}
        mfg["suggested_action"] = suggest_next_step(mfg)
        mfg["draft_message"] = draft_followup_message(mfg)
        exceptions.append(mfg)

    metrics = compute_metrics(results, order_audit, missing_from_gateway, exceptions)
    metrics["summary"] = generate_summary(metrics)
    audit_trail = order_audit + bank_audit

    with open("output/metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)
    with open("output/audit_trail.json", "w") as f:
        json.dump(audit_trail, f, indent=2)
    with open("output/exceptions_report.csv", "w", newline="") as f:
        fieldnames = ["order_id", "utr", "entity_type", "status", "category", "explanation",
                      "difference", "suggested_action", "draft_message"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for e in exceptions:
            writer.writerow({k: e.get(k, "") for k in fieldnames})

    print("=== Reconciliation complete ===")
    print(json.dumps(metrics, indent=2))
    print(f"\n{len(exceptions)} exceptions written to output/exceptions_report.csv")
    print(f"{len(audit_trail)} audit entries written to output/audit_trail.json")


if __name__ == "__main__":
    main()
