"""Reads the fields we check from the text of a payment screenshot.

The text comes from OCR as a list of lines, top to bottom. Screens differ
between apps (PhonePe, Paytm, Google Pay, bank apps), so every field is looked
for by its label first and by its shape second. Anything not found is None and
the check that needs it fails, so the customer uploads a clearer screenshot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MON = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"

# Labels that sit next to the UTR in the apps we know.
UTR_LABEL_RE = re.compile(
    r"\b(utr|upi\s*ref(?:erence)?|ref(?:erence)?\.?\s*(?:no|number|id)|upi\s*transaction\s*id|"
    r"transaction\s*reference|rrn|bank\s*ref(?:erence)?)\b", re.IGNORECASE)
# 12 digits (UPI / IMPS) not part of a longer code such as PhonePe's T2609...
UTR12_RE = re.compile(r"(?<![A-Za-z0-9])(\d{12})(?![0-9])")
# 16 characters for NEFT, e.g. SBIN226273012345 (never an 11-character IFSC).
UTR_NEFT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{4}[A-Z0-9]{12})(?![A-Za-z0-9])")

CURRENCY_AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
# A line that is only an amount: "100", "1,000.00", "₹ 100". OCR often drops the
# ₹ or reads it as another symbol ("%", "■", "?") or as "Z" / "F".
BARE_AMOUNT_RE = re.compile(r"^(?:[^\w\s]|[ZF])?\s*([\d,]+(?:\.\d{1,2})?)$")

FAILED_RE = re.compile(r"\b(failed|failure|declined|unsuccessful|reversed|cancelled)\b", re.IGNORECASE)
SUCCESS_RE = re.compile(
    r"\b(successful|success|successfully|completed|paid|money sent|transferred|sent)\b", re.IGNORECASE)
PENDING_RE = re.compile(r"\b(pending|processing|in progress|awaiting)\b", re.IGNORECASE)

NOTE_RE = re.compile(r"\border\s*[:#-]?\s*([A-Z2-9]{8})\b", re.IGNORECASE)

DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})\s*" + MON + r",?\s*(\d{4})\b", re.IGNORECASE),        # 30 Sep 2026
    re.compile(r"\b" + MON + r"\s*(\d{1,2}),?\s*(\d{4})\b", re.IGNORECASE),        # Sep 30, 2026
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b"),                     # 30/09/2026
]
# Colon only: "10.50" is far more often an amount than a time.
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?", re.IGNORECASE)


@dataclass
class ScreenshotFields:
    text: str
    utr_options: list[str] = field(default_factory=list)
    amounts: list[int] = field(default_factory=list)
    status: str | None = None            # success | failed | pending
    when: datetime | None = None         # naive local time
    has_time: bool = False
    note_order: str | None = None

    @property
    def utr(self) -> str | None:
        return self.utr_options[0] if len(self.utr_options) == 1 else None


def _paise(raw: str) -> int | None:
    try:
        value = Decimal(raw.replace(",", ""))
    except InvalidOperation:
        return None
    if value <= 0 or value != value.quantize(Decimal("0.01")):
        return None
    return int(value * 100)


def _utrs_in(s: str) -> list[str]:
    s = re.sub(r"(?<=\d)[  ](?=\d)", "", s)   # "4123 4567 8901" -> one number
    return UTR12_RE.findall(s) + UTR_NEFT_RE.findall(s)


def _unique(items):
    out = []
    for i in items:
        if i not in out:
            out.append(i)
    return out


def _find_utrs(lines: list[str]) -> list[str]:
    labelled = []
    for i, line in enumerate(lines):
        m = UTR_LABEL_RE.search(line)
        if not m:
            continue
        found = _utrs_in(line[m.end():])
        if not found and i + 1 < len(lines):
            found = _utrs_in(lines[i + 1])
        labelled += found
    if labelled:
        return _unique(labelled)
    return _unique(u for line in lines for u in _utrs_in(line))


def _find_amounts(lines: list[str], utrs: list[str]) -> list[int]:
    out = []
    for line in lines:
        s = line.strip()
        found = CURRENCY_AMOUNT_RE.findall(s)
        m = BARE_AMOUNT_RE.match(s)
        if m:
            found.append(m.group(1))
        for raw in found:
            if raw.replace(",", "") in utrs:
                continue
            p = _paise(raw)
            if p and p not in out:
                out.append(p)
    return out


def _find_status(text: str) -> str | None:
    if FAILED_RE.search(text):
        return "failed"
    if PENDING_RE.search(text):
        return "pending"
    if SUCCESS_RE.search(text):
        return "success"
    return None


def _find_when(text: str) -> tuple[datetime | None, bool]:
    date, date_pos = None, 0
    for i, rx in enumerate(DATE_PATTERNS):
        m = rx.search(text)
        if not m:
            continue
        try:
            if i == 0:
                d, mon, y = int(m.group(1)), MONTHS[m.group(2).lower()[:3]], int(m.group(3))
            elif i == 1:
                mon, d, y = MONTHS[m.group(1).lower()[:3]], int(m.group(2)), int(m.group(3))
            else:
                d, mon, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                y += 2000 if y < 100 else 0
            date, date_pos = datetime(y, mon, d), m.start()
            break
        except (ValueError, KeyError):
            continue
    if not date:
        return None, False
    # The payment time sits on the date's line or just before/after it; the
    # phone's clock at the very top of the screen is not the payment time.
    line_start = text.rfind("\n", 0, date_pos) + 1
    prev_start = text.rfind("\n", 0, max(line_start - 1, 0)) + 1
    window = text[prev_start:]
    window = "\n".join(window.split("\n")[:4])
    for m in TIME_RE.finditer(window):
        h, mi, ap = int(m.group(1)), int(m.group(2)), (m.group(3) or "").lower().replace(".", "")
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
        if h < 24 and mi < 60:
            return date.replace(hour=h, minute=mi), True
    return date, False


def parse_screenshot(lines: list[str]) -> ScreenshotFields:
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in lines if ln and ln.strip()]
    text = "\n".join(lines)
    utrs = _find_utrs(lines)
    when, has_time = _find_when(text)
    note = NOTE_RE.search(text)
    return ScreenshotFields(
        text=text,
        utr_options=utrs,
        amounts=_find_amounts(lines, utrs),
        status=_find_status(text),
        when=when,
        has_time=has_time,
        note_order=note.group(1).upper() if note else None,
    )
