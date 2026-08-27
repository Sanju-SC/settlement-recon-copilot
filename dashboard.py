"""
dashboard.py
------------
A simple visual screen on top of reconcile.py's output, so you can actually
show this to someone instead of pointing at terminal text.

Run:  streamlit run dashboard.py
(then open the local address it prints in your browser)

This file ONLY reads the files reconcile.py already wrote (output/metrics.json,
output/exceptions_report.csv, output/audit_trail.json). It doesn't recompute
anything itself - that keeps the "brain" (reconcile.py) and the "display"
(this file) cleanly separated, which is good practice and easy to explain
in an interview.
"""

import json
import csv
import subprocess
import sys

import streamlit as st

st.set_page_config(page_title="Settlement Reconciliation Copilot", layout="wide")

st.title("Settlement Reconciliation Copilot")
st.caption("Matches a shop's sales records, Razorpay's settlement report, and the bank "
           "statement - and explains exactly why anything doesn't line up.")

col1, col2 = st.columns([1, 4])
with col1:
    run_it = st.button("Run reconciliation now", type="primary")
if run_it:
    with st.spinner("Running reconcile.py ..."):
        result = subprocess.run([sys.executable, "reconcile.py"], capture_output=True, text=True)
    if result.returncode == 0:
        st.success("Reconciliation run complete.")
    else:
        st.error("reconcile.py failed - see details below.")
        st.code(result.stderr or result.stdout)

try:
    with open("output/metrics.json") as f:
        metrics = json.load(f)
except FileNotFoundError:
    st.warning("No results yet - click 'Run reconciliation now' above first.")
    st.stop()

st.subheader(f"As of {metrics['report_as_of']}")

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total records checked", metrics["total_records_checked"])
m2.metric("Match rate", f"{metrics['match_rate_percent']}%")
m3.metric("Reconciled value", f"Rs {metrics['reconciled_value']:,.2f}")
m4.metric("Unresolved value", f"Rs {metrics['unresolved_value']:,.2f}")

st.divider()
st.subheader(f"Exceptions ({metrics['exceptions_count']})")
st.caption("Everything that did NOT cleanly match, and why - nothing here was guessed at.")

try:
    with open("output/exceptions_report.csv", newline="") as f:
        exceptions = list(csv.DictReader(f))
except FileNotFoundError:
    exceptions = []

if not exceptions:
    st.info("No exceptions - every record matched cleanly.")
else:
    for e in exceptions:
        label = f"{e['order_id'] or '(no order)'} — {e['category']}"
        with st.expander(label):
            st.write(f"**Status:** {e['status']}")
            if e.get("entity_type"):
                st.write(f"**Entity type:** {e['entity_type']}")
            if e.get("utr"):
                st.write(f"**Bank reference (UTR):** {e['utr']}")
            if e.get("difference"):
                st.write(f"**Difference:** Rs {e['difference']}")
            st.write(f"**Explanation:** {e['explanation']}")

st.divider()
with st.expander("Full audit trail (every decision the engine made)"):
    try:
        with open("output/audit_trail.json") as f:
            audit = json.load(f)
        st.dataframe(audit, use_container_width=True)
    except FileNotFoundError:
        st.write("No audit trail yet.")

st.divider()
st.caption(
    "Known limitations: single currency (INR) only, tolerance for rounding is Rs 0.05, "
    "and AI explanations fall back to rule-based reasoning if no API key is set. "
    "See README.md for the full list."
)
