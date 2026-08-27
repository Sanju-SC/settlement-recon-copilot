"""
ai_explain.py
-------------
This is the ONLY file in the whole project that touches an AI model.
Everything else (matching, totals, fee math) is plain deterministic code -
on purpose. AI is used here for exactly one job: turning a confusing,
unresolved number difference into a plain-English explanation a human can
act on.

IMPORTANT - this is also our "one failure handled gracefully" requirement:
if there's no API key set, or the API call fails for any reason (network
down, rate limit, whatever), this does NOT crash the whole reconciliation
run. It falls back to a rule-based explanation instead, and says so
honestly. A finance tool should never go down just because one AI call
failed.

To turn on real AI explanations:
  1. Get an API key from https://console.anthropic.com
  2. Set it as an environment variable before running:
       Windows (PowerShell): $env:ANTHROPIC_API_KEY="your-key-here"
       Mac/Linux:             export ANTHROPIC_API_KEY="your-key-here"
Without a key set, everything still works - it just uses the fallback.
"""

import os

FEE_RATES_FOR_GUESSING = {
    "2%": 0.02, "2.15%": 0.0215, "3%": 0.03,
}
GST_ON_FEE = 0.18


def _rule_based_explanation(context):
    """
    No AI available - work out the most likely reason using plain logic
    instead. This never fails, so the tool always produces *something*
    useful even if the AI call couldn't be made.
    """
    diff = context["difference"]
    expected = context["expected_total"]
    actual = context["actual_total"]

    if abs(diff) <= 0.05:
        return ("[fallback reasoning] Difference is a few paise - almost certainly "
                "independent rounding on one side, not a real problem.")

    # Does the gap roughly match one of the known fee percentages, applied
    # to the ballpark transaction size? If so, say so plainly.
    approx_gross = expected / 0.98  # rough guess assuming ~2% fee already removed once
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


def _ai_explanation(context):
    """Try a real AI call. Raises an exception if it can't complete -
    the caller decides what to do with that (see explain_exception below)."""
    import anthropic  # imported here on purpose, so a missing package never
                       # breaks the rest of the program - only this function.

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    prompt = (
        f"A payment reconciliation tool found a mismatch.\n"
        f"Order: {context['order_id']}\n"
        f"Expected bank amount: {context['expected_total']}\n"
        f"Actual bank amount: {context['actual_total']}\n"
        f"Difference: {context['difference']}\n"
        f"Order status in shop system: {context.get('order_status')}\n\n"
        f"In one short sentence, explain the most likely reason for this "
        f"difference for a non-technical shop owner. Be specific and concrete."
    )
    response = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=150,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text.strip()


def explain_exception(context):
    """
    Public function reconcile.py calls for every unresolved amount mismatch.
    Tries real AI first; falls back to rule-based reasoning on ANY failure,
    and is honest in the output about which one actually ran.
    """
    if os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return _ai_explanation(context)
        except Exception as err:
            # Real AI call failed for some reason (no internet, bad key,
            # rate limit...) - don't crash the whole run, just fall back.
            fallback = _rule_based_explanation(context)
            return f"{fallback} (AI explanation unavailable: {err.__class__.__name__})"
    return _rule_based_explanation(context)
