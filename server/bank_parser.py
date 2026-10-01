"""Reads HDFC credit SMS and HDFC account statements.

Both formats here are best guesses until real samples are collected. SMS that
are not understood are saved as "ignored" and shown on the dashboard; statement
rows without a UTR are listed in the statement report.

SMS examples it understands:
    "Money Received - INR 100.00 in your HDFC Bank A/c xx4821 on 30-09-26 by
     A/c linked to VPA aman.v@ybl (UPI Ref No 412345678901)."
    "Update! INR 100.00 deposited in HDFC Bank A/c XX4821 on 30-SEP-26 for
     IMPS-412345678901-AMAN VERMA-..."
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

AMOUNT_RE = re.compile(r"(?:₹|rs\.?|inr)\s*([\d,]+(?:\.\d{1,2})?)", re.IGNORECASE)
CREDIT_RE = re.compile(r"\b(credited|deposited|received)\b", re.IGNORECASE)
DEBIT_RE = re.compile(r"\b(debited|withdrawn|spent|sent|paid to|debit)\b", re.IGNORECASE)
REF_RE = re.compile(
    r"\b(?:upi\s*ref(?:erence)?\.?\s*(?:no\.?|number)?|ref(?:erence)?\.?\s*no\.?|rrn|utr(?:\s*no\.?)?)"
    r"\s*[:\-]?\s*([A-Z0-9]{12,16})\b", re.IGNORECASE)
IMPS_RE = re.compile(r"\bIMPS[-/ ](\d{12})\b", re.IGNORECASE)
UTR12_RE = re.compile(r"(?<![A-Za-z0-9])(\d{12})(?![0-9])")
UTR_NEFT_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z]{4}[A-Z0-9]{12})(?![A-Za-z0-9])")
VPA_RE = re.compile(r"\b([\w.\-]+@[a-z][\w.\-]*)\b", re.IGNORECASE)


@dataclass
class BankCredit:
    amount_paise: int
    utr: str | None
    sender: str | None


def to_paise(raw: str) -> int | None:
    try:
        value = Decimal(str(raw).replace(",", "").strip())
    except InvalidOperation:
        return None
    if value <= 0:
        return None
    return int((value * 100).to_integral_value())


def normalize_ref(ref: str) -> str | None:
    """A reference as a UTR: "0000412345678901" (zero-padded to 16) -> "412345678901"."""
    ref = (ref or "").strip().upper()
    if re.fullmatch(r"\d{13,}", ref) and set(ref[:-12]) == {"0"}:
        return ref[-12:]
    if re.fullmatch(r"\d{12}|[A-Z]{4}[A-Z0-9]{12}", ref):
        return ref
    return None


def find_utr(text: str) -> str | None:
    """The UTR in an SMS or a statement narration, or None if not exactly one."""
    m = REF_RE.search(text) or IMPS_RE.search(text)
    if m:
        return normalize_ref(m.group(1))
    if re.search(r"\bNEFT\b", text, re.IGNORECASE):
        neft = set(UTR_NEFT_RE.findall(text.upper()))
        if len(neft) == 1:
            return neft.pop()
    found = set(UTR12_RE.findall(text))
    return found.pop() if len(found) == 1 else None


def parse_bank_sms(body: str | None) -> BankCredit | None:
    body = re.sub(r"\s+", " ", body or "").strip()
    if not body or not CREDIT_RE.search(body) or DEBIT_RE.search(body):
        return None
    amount = AMOUNT_RE.search(body)
    paise = to_paise(amount.group(1)) if amount else None
    if not paise:
        return None
    vpa = VPA_RE.search(body)
    return BankCredit(paise, find_utr(body), vpa.group(1) if vpa else None)


# ---------- statements ----------

@dataclass
class StatementRow:
    date: datetime
    narration: str
    ref: str
    deposit_paise: int
    utr: str | None


class StatementError(Exception):
    pass


def _col(header: list[str], *names) -> int | None:
    for i, h in enumerate(header):
        h = h.strip().lower()
        if any(n in h for n in names):
            return i
    return None


def _date(s: str) -> datetime | None:
    s = s.strip()
    for fmt in ("%d/%m/%y", "%d/%m/%Y", "%d-%m-%Y", "%d-%m-%y", "%d-%b-%Y", "%d-%b-%y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def parse_statement(data: bytes) -> list[StatementRow]:
    """Credit rows of an HDFC netbanking statement saved as CSV / delimited text
    (columns Date, Narration, Chq./Ref.No., Value Dt, Withdrawal Amt.,
    Deposit Amt., Closing Balance)."""
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(text), dialect))
    head_i = next((i for i, r in enumerate(rows)
                   if any("narration" in c.lower() for c in r) and any("deposit" in c.lower() or "credit" in c.lower() for c in r)),
                  None)
    if head_i is None:
        raise StatementError("Couldn't find the Narration and Deposit columns. Download the statement from HDFC "
                             "netbanking as a delimited / CSV file.")
    header = rows[head_i]
    c_date = _col(header, "date")
    c_narr = _col(header, "narration")
    c_ref = _col(header, "ref", "chq")
    c_dep = _col(header, "deposit", "credit")
    out = []
    for r in rows[head_i + 1:]:
        if len(r) <= max(c_date, c_narr, c_dep):
            continue
        d = _date(r[c_date])
        paise = to_paise(r[c_dep]) if r[c_dep].strip() else None
        if not d or not paise:
            continue
        narration = r[c_narr].strip()
        ref = r[c_ref].strip() if c_ref is not None and c_ref < len(r) else ""
        utr = find_utr(narration)
        if not utr:
            utr = normalize_ref(ref)
        out.append(StatementRow(d, narration, ref, paise, utr))
    return out
