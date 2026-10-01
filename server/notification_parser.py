"""Reads "money received" notifications from PhonePe, Paytm and Google Pay.

Examples it understands:
    "₹1,000.13 received from Rahul Sharma"
    "Received ₹ 500 from PRIYA VERMA"
    "Rahul Sharma sent you ₹250"
    "Rs. 99.50 credited to your account from Amit"

The exact wording differs between apps and changes over time. Any notification
that is not understood is saved as "ignored" and shown on the dashboard, so the
patterns here can be adjusted from real examples.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

AMOUNT = r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)"
AMOUNT_RE = re.compile(AMOUNT, re.IGNORECASE)

INCOMING_RE = re.compile(r"\b(received|credited|got|added to)\b", re.IGNORECASE)
SENT_YOU_RE = re.compile(r"^(.+?)\s+(?:has\s+)?sent\s+you\s+" + AMOUNT, re.IGNORECASE)
FROM_RE = re.compile(
    r"\bfrom\s+(.+?)(?=\s+(?:on|via|in|to|at|using|for|through)\b|[.,!|:\n]|$)",
    re.IGNORECASE,
)

# Notifications that mention money but are not a payment you received.
NOT_A_PAYMENT_RE = re.compile(
    r"\b(?:cashback|rewards?|offers?|coupons?|scratch|refund(?:ed)?|"
    r"requests?|requested|collect|debited|paid to|you sent|money sent|"
    r"sent successfully|failed|declined|pending|reminder|recharge)\b",
    re.IGNORECASE,
)


@dataclass
class ParsedPayment:
    amount_paise: int
    payer_name: str | None


def _to_paise(raw: str) -> int | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return int((value * 100).to_integral_value())


def _clean_name(name: str) -> str | None:
    name = re.sub(r"\s+", " ", name).strip(" -:")
    if not name or AMOUNT_RE.search(name):
        return None
    return name[:80]


def parse_notification(title: str | None, text: str | None) -> ParsedPayment | None:
    title = (title or "").strip()
    text = (text or "").strip()
    full = re.sub(r"\s+", " ", f"{title} . {text}" if title and text else title or text)

    if not full or NOT_A_PAYMENT_RE.search(full):
        return None

    # "Rahul Sharma sent you ₹250" (title and text checked separately so the
    # sender name does not swallow the other line)
    for part in (text, title):
        m = SENT_YOU_RE.search(re.sub(r"\s+", " ", part))
        if m:
            paise = _to_paise(m.group(2))
            return ParsedPayment(paise, _clean_name(m.group(1))) if paise else None

    if not INCOMING_RE.search(full):
        return None

    amount = AMOUNT_RE.search(full)
    if not amount:
        return None
    paise = _to_paise(amount.group(1))
    if not paise:
        return None

    payer = None
    m = FROM_RE.search(full)
    if m:
        payer = _clean_name(m.group(1))
    return ParsedPayment(paise, payer)
