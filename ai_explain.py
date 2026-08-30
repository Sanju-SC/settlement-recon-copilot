"""
ai_explain.py
-------------
This is the ONLY file in the whole project that touches an AI model.
Everything else (matching, totals, fee math) is plain deterministic code -
on purpose. AI is used here for three small, specific jobs, and nothing else:

  1. explain_exception()       - turn an unresolved amount mismatch into a
                                  plain-English explanation
  2. suggest_next_step()       - suggest the one practical thing a shop
                                  owner should do about an exception
  3. draft_followup_message()  - write a ready-to-copy message the shop
                                  owner could send to Razorpay support
                                  (drafts only - never sends anything)
  4. generate_summary()        - one plain sentence at the top of the
                                  dashboard summarizing the whole run

IMPORTANT - this is also our "one failure handled gracefully" requirement:
if there's no API key set, or any API call fails for any reason (network
down, rate limit, whatever), none of this crashes the run. Every single one
of these falls back to a rule-based version instead, and says so honestly.

To turn on real AI:
  1. Get an API key from https://console.anthropic.com
  2. Set it as an environment variable before running:
       Windows (PowerShell): $env:ANTHROPIC_API_KEY="your-key-here"
       Mac/Linux:             export ANTHROPIC_API_KEY="your-key-here"
Without a key set, everything still works - it just uses the fallbacks.
"""

import os

FEE_RATES_FOR_GUESSING = {
    "2%": 0.02, "2.15%": 0.0215, "3%": 0.03,
}
GST_ON_FEE = 0.18


def _call_claude(prompt, max_tokens=150):
    """
    Shared helper - makes the actual API call. Used by all four AI features
    below, so there's only one place that talks to the API, not four
    separate copies of the same code (less places for a mistake to hide).
    Raises an exception if it can't complete - each caller decides what
    fallback to use when that happens.
    """
    import anthropic  # imported here on purpose, so a missing package never
                       # breaks the rest of the program - only this function.
    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# 1. Explain an unresolved amount mismatch (unchanged from before)
# ---------------------------------------------------------------------------
def _rule_based_explanation(context):
    diff = context["difference"]
    expected = context["expected_total"]

    if abs(diff) <= 0.05:
        return ("[fallback reasoning] Difference is a few paise - almost certainly "
                "independent rounding on one side, not a real problem.")

    approx_gross = expected / 0.98
    for label, rate in FEE_RATES_FOR_GUESSING.items():
        guess_fee = round(approx_gross * rate * (1 + GST_ON_FEE), 2)
        if abs(abs(diff) - guess_fee) <= 1.0:
            return (f"[fallback reasoning] The {abs(diff):.2f} gap is close to what a "
                    f"{label} fee + GST would produce on a transaction this size. "
                    f"Likely a fee-rate mismatch - worth confirming the payment method.")

    if diff < 0:
        return (f"[fallback reasoning] Bank received {abs(diff):.2f} LESS than expected. "
                f"Common causes: an unaccounted refund, an extra gateway fee, or a "
                f"chargeback. Needs a human to check the specific transaction.")
    return (f"[fallback reasoning] Bank received {abs(diff):.2f} MORE than expected. "
            f"Uncommon - could be a misapplied credit or a batching overlap. "
            f"Needs a human to check.")


def explain_exception(context):
    """Public function reconcile.py calls for every unresolved amount mismatch."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            prompt = (
                f"A payment reconciliation tool found a mismatch.\n"
                f"Order: {context['order_id']}\n"
                f"Expected bank amount: {context['expected_total']}\n"
                f"Actual bank amount: {context['actual_total']}\n"
                f"Difference: {context['difference']}\n"
                f"Order status in shop system: {context.get('order_status')}\n\n"
                f"In one short sentence, explain the most likely reason for this "
                f"difference for a non-technical shop owner. Be specific and concrete.\n\n"
                f"Rules: use neutral, factual language only - never accuse any party of "
                f"wrongdoing or use alarming words like 'stolen' or 'fraud'. If you cannot "
                f"determine a likely cause with reasonable confidence from the numbers given, "
                f"say plainly that it needs manual review instead of guessing."
            )
            return _call_claude(prompt, max_tokens=150)
        except Exception as err:
            fallback = _rule_based_explanation(context)
            return f"{fallback} (AI explanation unavailable: {err.__class__.__name__})"
    return _rule_based_explanation(context)


# ---------------------------------------------------------------------------
# 2. Suggest the one practical next step for an exception that needs action
# ---------------------------------------------------------------------------
def _rule_based_next_step(context):
    category = context.get("category", "")
    order_id = context.get("order_id") or "(no order)"
    utr = context.get("utr") or "(no UTR)"

    if category == "Missing from gateway":
        return f"[fallback] Check with Razorpay support whether order {order_id} was actually processed on their end."
    if category == "Overdue - missing bank entry":
        return f"[fallback] Contact Razorpay support referencing UTR {utr} - this payment is past its expected settlement date."
    if category == "Unexplained bank entry":
        return f"[fallback] Check your bank statement for UTR {utr} directly against Razorpay's dashboard, since no settlement record explains it."
    if category == "Amount mismatch":
        return f"[fallback] Review order {order_id} manually - the settled amount differs from what was expected."
    return "[fallback] Flag this for manual review."


def suggest_next_step(context):
    """
    Public function - for exceptions that need a human to actually do
    something (not the harmless 'still pending, wait' ones), suggest the
    one practical next action. Same AI-with-fallback safety pattern as
    explain_exception above.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            prompt = (
                f"A payment reconciliation tool flagged this exception:\n"
                f"Category: {context.get('category')}\n"
                f"Order: {context.get('order_id') or '(no order)'}\n"
                f"Details: {context.get('explanation')}\n\n"
                f"In one short, practical sentence, suggest the single next action a "
                f"shop owner should take. Be specific and concrete. Do not accuse any "
                f"party of wrongdoing - use neutral, factual language only."
            )
            return _call_claude(prompt, max_tokens=80)
        except Exception as err:
            fallback = _rule_based_next_step(context)
            return f"{fallback} (AI suggestion unavailable: {err.__class__.__name__})"
    return _rule_based_next_step(context)


# ---------------------------------------------------------------------------
# 3. Draft a ready-to-copy follow-up message (draft only - never sent)
# ---------------------------------------------------------------------------
def _rule_based_draft_message(context):
    category = context.get("category", "an issue")
    order_id = context.get("order_id") or "N/A"
    utr = context.get("utr") or "N/A"
    explanation = context.get("explanation", "")
    return (
        f"[fallback draft]\n"
        f"Hello Razorpay Support,\n\n"
        f"I'm writing to ask about {category.lower()} on order {order_id} "
        f"(UTR: {utr}). Our records show: {explanation}\n\n"
        f"Could you please confirm the current status and expected resolution?\n\n"
        f"Thank you."
    )


def draft_followup_message(context):
    """
    Public function - writes a short, polite, ready-to-copy message the
    shop owner could send to Razorpay support about this specific
    exception. IMPORTANT: this only drafts text. Nothing in this project
    ever sends an email or message automatically - a human always reads
    and sends it themselves.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            prompt = (
                f"A payment reconciliation tool flagged this exception for a shop "
                f"owner to escalate to Razorpay support:\n"
                f"Category: {context.get('category')}\n"
                f"Order: {context.get('order_id') or '(no order)'}\n"
                f"UTR: {context.get('utr') or '(no UTR)'}\n"
                f"Details: {context.get('explanation')}\n\n"
                f"Write a short, polite, professional message (3-4 sentences) the shop "
                f"owner could send to Razorpay support asking about this. Reference the "
                f"order/UTR. Use neutral, factual language - ask for clarification or "
                f"status, never accuse Razorpay of wrongdoing."
            )
            return _call_claude(prompt, max_tokens=200)
        except Exception as err:
            fallback = _rule_based_draft_message(context)
            return f"{fallback}\n(AI draft unavailable: {err.__class__.__name__})"
    return _rule_based_draft_message(context)


# ---------------------------------------------------------------------------
# 4. One-sentence summary for the top of the dashboard
# ---------------------------------------------------------------------------
def _rule_based_summary(metrics):
    return (
        f"[fallback] {metrics['matched']} of {metrics['total_settlement_groups_checked']} "
        f"transactions reconciled cleanly ({metrics['match_rate_percent']}%); "
        f"{metrics['exceptions_count']} need a closer look."
    )


def generate_summary(metrics):
    """
    Public function - one calm, factual headline sentence describing the
    whole reconciliation run, shown at the top of the dashboard.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            prompt = (
                f"A payment reconciliation run just finished:\n"
                f"Total checked: {metrics['total_settlement_groups_checked']}\n"
                f"Matched: {metrics['matched']}\n"
                f"Match rate: {metrics['match_rate_percent']}%\n"
                f"Exceptions: {metrics['exceptions_count']}\n\n"
                f"Write ONE short, calm, factual sentence summarizing this for a busy "
                f"shop owner glancing at a screen. Do not use alarming language."
            )
            return _call_claude(prompt, max_tokens=60)
        except Exception as err:
            fallback = _rule_based_summary(metrics)
            return f"{fallback} (AI summary unavailable: {err.__class__.__name__})"
    return _rule_based_summary(metrics)
