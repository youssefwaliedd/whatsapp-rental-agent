"""The last line before a price reaches a customer.

`check_unsupported_claims` already finds figures no tool produced — but it is an
evaluator: it runs afterwards, writes a finding, and feeds the learning loop. By
then the customer has the number.

Observed: asked for 4-6 September, a two-day rental, the agent said AED 2,831.85.
That is 899 x **three** days plus VAT. The engine cannot produce it for those
dates; it was the model's own arithmetic. It corrected itself to AED 1,887.90
only because it was asked to show the calculation, which most customers will
never do.

So the same rule runs on the way out. A message carrying a figure no tool
returned this turn does not get sent as it stands: the model is told which figure
is unsupported and what the real ones are, and given one chance to write the
message again. If it still cannot, the reply falls back to something that
promises no number at all — a customer told "let me confirm that" is
inconvenienced, and a customer told the wrong total is misled.

One retry rather than several. Each costs a model call against a 2-5 second
target, and a model that has invented a price twice with the figures in front of
it is not going to get there on a third pass.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Iterable

import re

from ..evaluation.checks import supported_numbers

#: What counts as a price *in a reply*. Stricter than the evaluator's rule, and
#: deliberately so: that one reports afterwards and a false positive is noise,
#: while this one stops a message and a false positive is a customer left
#: waiting. A bare four-digit number is a model year far more often than it is a
#: total — "BMW M4 Competition 2025" must not block a reply — so a figure counts
#: only when it is marked as money: a currency, a thousands separator, or two
#: decimal places.
_MONEY = re.compile(
    r"(?:aed|dhs?|dirhams?|درهم|دراهم|د\.إ|\$|usd)\s*(\d[\d,]*(?:\.\d+)?)"
    r"|(\d[\d,]*(?:\.\d+)?)\s*(?:aed|dhs?|dirhams?|درهم|دراهم|د\.إ)"
    r"|(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?)"
    r"|(\d+\.\d{2})\b",
    re.I,
)


def money_in(text: str) -> set[Decimal]:
    """Every figure in a reply that is stated as an amount."""
    found: set[Decimal] = set()
    for match in _MONEY.finditer((text or "").replace("٬", ",").replace("٫", ".")):
        raw = next((g for g in match.groups() if g), None)
        if raw is None:
            continue
        try:
            found.add(Decimal(raw.replace(",", "")))
        except Exception:  # noqa: BLE001 - a malformed match is not a claim
            continue
    return found


_BUDGET_AMOUNT = re.compile(
    r"\b(?:(?:your|my|current)\s+)?(?:daily\s+)?(?:budget|maximum|limit)"
    r"(?:\s+per day)?\s*(?:(?:is|of|remains|:)\s*)?"
    r"(?:(?:AED|dhs?|dirhams?)\s*\d[\d,]*(?:\.\d+)?|\d[\d,]*(?:\.\d+)?\s*(?:AED|dhs?|dirhams?))", re.I)


def budget_values(text: str) -> set[Decimal]:
    return set().union(*(money_in(m.group()) for m in _BUDGET_AMOUNT.finditer(text or "")))


def without_budget_echoes(text: str, budgets: set[Decimal]) -> str:
    """A customer's spending limit may be repeated, but cannot back a price."""
    return _BUDGET_AMOUNT.sub(lambda m: "customer budget" if money_in(m.group()) <= budgets else m.group(), text)


#: Sent when the model cannot produce a message without inventing a figure.
#: Deliberately says nothing about price or availability.
SAFE_REPLY = (
    "I could not verify that amount. Please ask for a fresh quote, or ask a "
    "colleague to confirm the missing fee."
)


@dataclass(frozen=True)
class Verdict:
    ok: bool
    unsupported: tuple[Decimal, ...] = ()
    supported: tuple[Decimal, ...] = ()

    @property
    def worst(self) -> Decimal | None:
        return max(self.unsupported) if self.unsupported else None


def inspect(reply: str, tool_calls: Iterable[Any], customer_budgets: set[Decimal] | None = None) -> Verdict:
    """Every figure in the reply, checked against what the tools returned.

    Replies without monetary claims pass even when no tools were called.
    """
    calls = list(tool_calls)
    reply = without_budget_echoes(reply, customer_budgets or set())

    # Generous on what counts as supported, strict on what counts as a claim:
    # both errors are one-sided, and only one of them blocks a message.
    supported = supported_numbers(calls, monetary_only=True)
    stated = money_in(reply)
    unsupported = sorted(stated - supported)
    # A deposit must be supported by deposit data, not a daily rate, a budget,
    # or a number in an unrelated tool result. Apply when a sentence has one
    # amount; multi-amount quote breakdowns are checked against result values.
    for sentence in re.split(r"[!?؟\n]|\.(?!\d)", reply):
        amounts = money_in(sentence)
        if len(amounts) != 1 or not re.search(r"\bdeposit\b|تأمين|وديعة", sentence, re.I):
            continue
        deposits = set()
        def visit(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == "deposit" and item is not None:
                        try:
                            deposits.add(Decimal(str(item)))
                        except Exception:
                            pass
                    elif isinstance(item, (dict, list)):
                        visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)
        for call in calls:
            if not (call.result or {}).get("error"):
                visit(call.result or {})
        if not amounts <= deposits:
            unsupported = sorted(set(unsupported) | (amounts - deposits))
    return Verdict(
        ok=not unsupported,
        unsupported=tuple(unsupported),
        supported=tuple(sorted(supported)),
    )


def correction(verdict: Verdict) -> str:
    """What to tell the model so its second attempt is different from its first.

    Names the figure, because "you invented a number" is not actionable, and
    lists what it may use, because the alternative is another guess.
    """
    invented = ", ".join(f"{value:,}" for value in verdict.unsupported)
    available = ", ".join(f"{value:,}" for value in verdict.supported[:20]) or "none"
    return (
        f"STOP. Your reply states {invented}, which no tool in this conversation "
        f"returned. Do not send it.\n"
        f"The only figures you may state are the ones your tools actually produced: "
        f"{available}.\n"
        f"Write the message again. If the figure you wanted is not in that list, do "
        f"not state one — say you will confirm it, or call the tool that would give "
        f"it to you. Never calculate a total yourself: a daily rate multiplied by "
        f"days is exactly the mistake this is catching."
    )
