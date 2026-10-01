from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import config
from bank_parser import StatementError, parse_bank_sms, parse_statement
from db import reader, transaction
from notification_parser import parse_notification
from screenshot_parser import parse_screenshot

# The one rule for crediting money: the UTR read from the customer's screenshot
# and the UTR the bank reports (SMS or statement) are exactly the same, and so
# is the amount. Nothing is ever credited from a screenshot alone, from a UPI
# app alert (no UTR), or from a guess based on time, name or amount.

MAX_ORDER_PAISE = 1_00_000_00  # ₹1,00,000 UPI limit
ORDER_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
MEMBER_ID_RE = re.compile(r"^[A-Z0-9_-]{2,20}$")
TZ = ZoneInfo(config.TIMEZONE)

# A notification with the same text from the same phone within this window
# is the app re-posting it, not a new payment.
DUPLICATE_WINDOW_SECONDS = 10 * 60

OPEN_ORDER = ("pending", "expired", "action")      # still waiting for a screenshot
LIVE_CLAIM = ("rejected", "pick", "ready")          # latest upload, not yet submitted

# Why an order is in review: title for the admin, text for the customer, and how
# an admin verifies it. The same steps are in VERIFICATION.md.
REVIEW_REASONS = {
    "two_claims": (
        "Two customers claimed the same UTR",
        "Another claim was made with the same UTR. Our team will confirm who paid.",
        ["Open the bank statement of the receiving account and find this UTR.",
         "Compare the sender name / UPI ID in the statement with each customer's screenshot (Debited from, name).",
         "Ask both customers for the full transaction details page if it is still unclear.",
         "Approve the customer whose sender matches; reject the other with “This UTR belongs to another customer's payment”."]),
    "amount_claimed": (
        "Customer says they paid a different amount",
        "You told us you paid a different amount. Our team will check it with the bank.",
        ["Find the UTR in the receiving account's statement (or HDFC app).",
         "Approve with the amount the bank shows, not the order amount.",
         "Not found → reject with “No payment with this UTR reached our account”."]),
    "paid_to_other": (
        "Screenshot shows an account that isn't one of ours",
        "Your screenshot shows a payment to a different account. Our team will check it.",
        ["Check every receiving account's statement (current and closed) for this UTR.",
         "Found with the right amount → approve.",
         "Not found → reject with “This payment went to an account that isn't ours”."]),
    "unreadable": (
        "Screenshot couldn't be read twice",
        "We couldn't read your screenshot automatically. Our team will check it.",
        ["Open the screenshot and read the UTR, amount, date and receiver yourself.",
         "Search that UTR in the receiving account's statement or HDFC app.",
         "Found with the same amount → approve with that UTR. Not found → reject."]),
    "not_in_statement": (
        "UTR not found in the bank statement",
        "We couldn't find this payment in our bank statement yet. Our team will check it.",
        ["Open the screenshot: check the UTR was read correctly (one wrong digit = no match).",
         "Search the correct UTR in the statement of the receiving account and of any other account.",
         "Found with the same amount → approve with the bank's UTR.",
         "Still not found after the next statement → reject with “No payment with this UTR reached our account”."]),
    "bank_amount_differs": (
        "Bank amount is different from the order",
        "The amount the bank received is different from your order. Our team will check it.",
        ["Compare the bank amount with the order amount.",
         "Approve with the amount the bank actually received."]),
    "utr_credited_elsewhere": (
        "UTR was already credited to another order",
        "This UTR was already used. Our team will check it.",
        ["Open the other order with this UTR and compare both screenshots.",
         "Usually a re-used screenshot: reject with “This UTR belongs to another customer's payment”."]),
}

REJECT_REASONS = {
    "not_received": "No payment with this UTR reached our account.",
    "wrong_account": "This payment went to an account that isn't ours.",
    "other_customer": "This UTR belongs to another customer's payment.",
    "not_genuine": "This screenshot doesn't match any payment we received.",
}

# Screenshot checks: problems the customer can fix by uploading again.
PROBLEMS_REVIEWABLE_NOW = ("amount_mismatch", "paid_to_other")
PROBLEMS_REVIEWABLE_AFTER_2 = ("utr_missing", "amount_missing", "date_missing", "status_missing", "note_other",
                               "paid_to_other", "amount_mismatch")


PROOF_NAMES = {"sms": "HDFC SMS", "statement": "bank statement", "admin": "an admin", "demo": "demo SMS"}


class ServiceError(Exception):
    """An error whose message can be shown to the user as is."""


def _now(now: float | None) -> int:
    return int(time.time() if now is None else now)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def to_paise(amount) -> int:
    try:
        value = Decimal(str(amount).replace(",", "").replace("₹", "").strip())
    except InvalidOperation:
        raise ServiceError("Invalid amount")
    if value != value.quantize(Decimal("0.01")):
        raise ServiceError("Amount can have at most 2 decimal places")
    paise = int(value * 100)
    if paise <= 0:
        raise ServiceError("Amount must be more than 0")
    if paise > MAX_ORDER_PAISE:
        raise ServiceError("Amount is above the UPI limit")
    return paise


def format_paise(paise: int) -> str:
    return f"{paise // 100}.{paise % 100:02d}"


def rupees(paise: int) -> str:
    return "₹" + f"{paise // 100:,}.{paise % 100:02d}"


def local_str(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%d %b, %I:%M %p")


def _event(conn, order_id: str, text: str, now: int) -> None:
    conn.execute("INSERT INTO order_events (order_id, at, text) VALUES (?, ?, ?)", (order_id, now, text))


def _alert(conn, kind: str, text: str, now: int, order_id: str | None = None, member_id: str | None = None) -> None:
    conn.execute("INSERT INTO alerts (at, kind, text, order_id, member_id) VALUES (?, ?, ?, ?, ?)",
                 (now, kind, text, order_id, member_id))


# ---------- receiving accounts ----------

def add_member(member_id: str, name: str, upi_id: str, now: float | None = None,
               bank_account: str | None = None, ifsc: str | None = None) -> str:
    """Adds a receiving account and returns the secret code for its phone app."""
    member_id = member_id.strip().upper()
    name = name.strip()
    upi_id = upi_id.strip()
    bank_account = re.sub(r"\s+", "", bank_account or "") or None
    ifsc = (ifsc or "").strip().upper() or None
    if not MEMBER_ID_RE.match(member_id):
        raise ServiceError("Account code can only use A-Z, 0-9, - or _ (2 to 20 characters)")
    if not name:
        raise ServiceError("Enter the account holder's name")
    if not re.match(r"^[\w.\-]+@[\w.\-]+$", upi_id):
        raise ServiceError("Invalid UPI ID (for example name@okhdfcbank)")
    if bank_account and not re.fullmatch(r"\d{9,18}", bank_account):
        raise ServiceError("Bank account number should be 9 to 18 digits")
    if bool(bank_account) != bool(ifsc):
        raise ServiceError("Enter both the account number and the IFSC, or neither")
    if ifsc and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        raise ServiceError("Invalid IFSC (for example HDFC0001234)")
    token = secrets.token_urlsafe(24)
    try:
        with transaction() as conn:
            conn.execute(
                """INSERT INTO members (id, name, upi_id, token_hash, created_at, bank_account, ifsc)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (member_id, name, upi_id, hash_token(token), _now(now), bank_account, ifsc),
            )
    except sqlite3.IntegrityError:
        raise ServiceError(f"Account code {member_id} already exists")
    return token


def update_member(member_id: str, name: str, bank_account: str | None, ifsc: str | None) -> None:
    """Changes the holder name and bank details shown to customers."""
    name = (name or "").strip()
    bank_account = re.sub(r"\s+", "", bank_account or "") or None
    ifsc = (ifsc or "").strip().upper() or None
    if not name:
        raise ServiceError("Enter the account holder's name")
    if bank_account and not re.fullmatch(r"\d{9,18}", bank_account):
        raise ServiceError("Bank account number should be 9 to 18 digits")
    if bool(bank_account) != bool(ifsc):
        raise ServiceError("Enter both the account number and the IFSC, or neither")
    if ifsc and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", ifsc):
        raise ServiceError("Invalid IFSC (for example HDFC0001234)")
    with transaction() as conn:
        cur = conn.execute("UPDATE members SET name = ?, bank_account = ?, ifsc = ? WHERE id = ?",
                           (name, bank_account, ifsc, member_id))
        if cur.rowcount == 0:
            raise ServiceError("Account not found")


def reset_member_token(member_id: str) -> str:
    """Gives an account's phone a new code, e.g. when the phone is lost."""
    token = secrets.token_urlsafe(24)
    with transaction() as conn:
        cur = conn.execute("UPDATE members SET token_hash = ? WHERE id = ?", (hash_token(token), member_id))
        if cur.rowcount == 0:
            raise ServiceError("Account not found")
    return token


def member_for_token(token: str):
    with reader() as conn:
        return conn.execute("SELECT * FROM members WHERE token_hash = ?", (hash_token(token),)).fetchone()


def touch_member(member_id: str, now: float | None = None) -> None:
    with transaction() as conn:
        conn.execute("UPDATE members SET last_seen = ? WHERE id = ?", (_now(now), member_id))


def set_member_status(member_id: str, status: str, now: float | None = None) -> str:
    """active: gets new orders. paused: no new orders, open ones carry on.
    closed: stopped at once (account frozen or given up). Customers who have not
    paid yet are told not to pay; those who paid can still upload."""
    now = _now(now)
    if status not in ("active", "paused", "closed"):
        raise ServiceError("Unknown status")
    with transaction() as conn:
        m = conn.execute("SELECT * FROM members WHERE id = ?", (member_id,)).fetchone()
        if not m:
            raise ServiceError("Account not found")
        if m["status"] == "closed" and status != "closed":
            raise ServiceError("A closed account can't be opened again. Add it as a new account.")
        conn.execute("UPDATE members SET status = ?, closed_at = ? WHERE id = ?",
                     (status, now if status == "closed" else None, member_id))
        if status != "closed":
            return f"{member_id} is now {status}"
        stopped = conn.execute(
            "SELECT id FROM orders WHERE member_id = ? AND status = 'pending'", (member_id,)).fetchall()
        for o in stopped:
            conn.execute(
                """UPDATE orders SET status = 'expired', expires_at = ?, customer_message = ? WHERE id = ?""",
                (now, "The account for this order is no longer available. Don't pay to it. "
                      "If you already paid, upload your screenshot below.", o["id"]))
            _event(conn, o["id"], "Receiving account closed: customer told not to pay", now)
        waiting = conn.execute(
            "SELECT COUNT(*) FROM orders WHERE member_id = ? AND status IN ('in_progress', 'review')",
            (member_id,)).fetchone()[0]
        if waiting:
            _alert(conn, "account_closed",
                   f"Account {member_id} was closed with {waiting} payment(s) waiting for the bank. Upload its "
                   "latest statement if you still can; otherwise check them in Review.", now, member_id=member_id)
        return f"{member_id} closed. {len(stopped)} unpaid order(s) stopped, {waiting} waiting for the bank."


# ---------- customers ----------

def login_customer(name: str, phone: str, now: float | None = None) -> dict:
    """Demo login: name + mobile number, no OTP."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    if not re.fullmatch(r"[6-9]\d{9}", digits):
        raise ServiceError("Enter a 10-digit mobile number")
    name = (name or "").strip()[:60]
    if not name:
        raise ServiceError("Enter your name")
    with transaction() as conn:
        row = conn.execute("SELECT * FROM customers WHERE phone = ?", (digits,)).fetchone()
        if row:
            conn.execute("UPDATE customers SET name = ? WHERE id = ?", (name, row["id"]))
            cid = row["id"]
        else:
            cid = conn.execute("INSERT INTO customers (phone, name, created_at) VALUES (?, ?, ?)",
                               (digits, name, _now(now))).lastrowid
        return dict(conn.execute("SELECT * FROM customers WHERE id = ?", (cid,)).fetchone())


def get_customer(customer_id: int) -> dict | None:
    with reader() as conn:
        row = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
        return dict(row) if row else None


def customer_orders(customer_id: int, now: float | None = None, limit: int = 50) -> list[dict]:
    with transaction() as conn:
        _tick(conn, _now(now))
        return [dict(r) for r in conn.execute(
            "SELECT * FROM orders WHERE customer_id = ? ORDER BY created_at DESC LIMIT ?", (customer_id, limit))]


# ---------- orders ----------

def _tick(conn, now: int) -> None:
    conn.execute("UPDATE orders SET status = 'expired' WHERE status = 'pending' AND expires_at <= ?", (now,))


def _new_order_id(conn) -> str:
    while True:
        order_id = "".join(secrets.choice(ORDER_ID_ALPHABET) for _ in range(8))
        if not conn.execute("SELECT 1 FROM orders WHERE id = ?", (order_id,)).fetchone():
            return order_id


def _pick_member(conn, method: str):
    """The active account with the fewest orders still open; ties go to the one
    used longest ago, so money spreads over all accounts."""
    need_bank = "AND m.bank_account IS NOT NULL" if method == "bank" else ""
    return conn.execute(
        f"""SELECT m.*, (SELECT COUNT(*) FROM orders o WHERE o.member_id = m.id
                         AND o.status IN ('pending', 'in_progress')) AS load
            FROM members m WHERE m.status = 'active' {need_bank}
            ORDER BY load, IFNULL(m.last_assigned_at, 0), m.id LIMIT 1""").fetchone()


def bank_transfer_available() -> bool:
    with reader() as conn:
        return bool(conn.execute(
            "SELECT 1 FROM members WHERE status = 'active' AND bank_account IS NOT NULL").fetchone())


class PendingOrder(ServiceError):
    """The customer already has an order waiting for payment."""
    def __init__(self, order):
        super().__init__(f"You have an unpaid order of {rupees(order['amount_paise'])} ({order['id']}).")
        self.order = dict(order)


def create_order(amount, customer_id: int | None = None, member_id: str | None = None, note: str | None = None,
                 method: str = "upi", now: float | None = None, replace_pending: bool = False) -> dict:
    now = _now(now)
    amount_paise = to_paise(amount)
    note = (note or "").strip()[:100] or None
    if method not in ("upi", "bank"):
        raise ServiceError("Choose UPI or bank transfer")
    with transaction() as conn:
        _tick(conn, now)
        if customer_id is not None:
            c = conn.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
            if not c:
                raise ServiceError("Customer not found")
            if c["paused_until"] and c["paused_until"] > now:
                raise ServiceError("Add Cash is paused on your account until " + local_str(c["paused_until"]) +
                                   " because of payment claims we couldn't verify.")
            open_one = conn.execute(
                "SELECT * FROM orders WHERE customer_id = ? AND status = 'pending'", (customer_id,)).fetchone()
            if open_one and not replace_pending:
                raise PendingOrder(open_one)
            if open_one:
                _close_by_customer(conn, open_one["id"], now, "Replaced by a new order (customer said it was not paid)")
            recent = conn.execute("SELECT COUNT(*) FROM orders WHERE customer_id = ? AND created_at > ?",
                                  (customer_id, now - 3600)).fetchone()[0]
            if recent >= config.MAX_ORDERS_PER_HOUR:
                raise ServiceError(f"You can create at most {config.MAX_ORDERS_PER_HOUR} orders an hour. "
                                   "Please try again later.")
        if member_id:
            member = conn.execute("SELECT * FROM members WHERE id = ?", (member_id.strip().upper(),)).fetchone()
            if not member:
                raise ServiceError("Account not found")
            if member["status"] == "closed":
                raise ServiceError("This account is closed")
            if method == "bank" and not member["bank_account"]:
                raise ServiceError("This account has no bank details for transfers")
        else:
            member = _pick_member(conn, method)
            if not member:
                raise ServiceError("Add Cash is not available right now. Please try again later.")
        conn.execute("UPDATE members SET last_assigned_at = ? WHERE id = ?", (now, member["id"]))

        order_id = _new_order_id(conn)
        conn.execute(
            """INSERT INTO orders (id, member_id, base_paise, amount_paise, note, status, created_at, expires_at,
                                   customer_id, method, upload_until)
               VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
            (order_id, member["id"], amount_paise, amount_paise, note, now, now + config.ORDER_TTL_SECONDS,
             customer_id, method, now + config.UPLOAD_WINDOW_SECONDS),
        )
        _event(conn, order_id, f"Order created for {rupees(amount_paise)} by {method.upper()}, "
                               f"account {member['id']}", now)
        return dict(conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone())


def _order_row(conn, order_id: str):
    return conn.execute(
        """SELECT o.*, m.name AS member_name, m.upi_id AS member_upi, m.status AS member_status,
                  m.bank_account AS member_bank, m.ifsc AS member_ifsc,
                  c.name AS customer_name, c.phone AS customer_phone
           FROM orders o JOIN members m ON m.id = o.member_id
           LEFT JOIN customers c ON c.id = o.customer_id
           WHERE o.id = ?""", (order_id.strip().upper(),)).fetchone()


def get_order(order_id: str, now: float | None = None) -> dict | None:
    """Order with its account, customer and latest screenshot, or None."""
    with transaction() as conn:
        _tick(conn, _now(now))
        row = _order_row(conn, order_id)
        if not row:
            return None
        order = dict(row)
        claim = conn.execute("SELECT * FROM claims WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                             (order["id"],)).fetchone()
        order["claim"] = _claim_dict(claim) if claim else None
        order["attempts"] = conn.execute(
            "SELECT COUNT(*) FROM claims WHERE order_id = ? AND state = 'rejected'", (order["id"],)).fetchone()[0]
        order["payer_name"] = None
        if order["push_seen_at"]:
            p = conn.execute("SELECT payer_name FROM payments WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                             (order["id"],)).fetchone()
            order["payer_name"] = p["payer_name"] if p else None
        return order


def _claim_dict(row) -> dict:
    c = dict(row)
    c["utr_options"] = json.loads(c["utr_options"] or "[]")
    c["amounts_seen"] = json.loads(c["amounts_seen"] or "[]")
    return c


def order_events(order_id: str) -> list[dict]:
    with reader() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM order_events WHERE order_id = ? ORDER BY id", (order_id,))]


def cancel_order(order_id: str, customer_id: int | None = None, now: float | None = None) -> None:
    """Closes an order that has no screenshot submitted yet."""
    now = _now(now)
    order_id = order_id.strip().upper()
    with transaction() as conn:
        _tick(conn, now)
        order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
        if not order or (customer_id is not None and order["customer_id"] != customer_id):
            raise ServiceError(f"Order {order_id} not found")
        if order["status"] not in OPEN_ORDER:
            raise ServiceError(f"Order {order_id} can't be cancelled now")
        if conn.execute("SELECT 1 FROM claims WHERE order_id = ? AND state IN ('submitted', 'review', 'matched')",
                        (order_id,)).fetchone():
            raise ServiceError(f"Order {order_id} already has a screenshot being checked")
        if customer_id is None:
            conn.execute("UPDATE orders SET status = 'cancelled' WHERE id = ?", (order_id,))
            _event(conn, order_id, "Cancelled by an admin", now)
        else:
            _close_by_customer(conn, order_id, now, "Cancelled by the customer")


CLOSED_BY_CUSTOMER = ("You closed this order. Don't pay on it. If you already paid, upload the screenshot "
                      "below within 48 hours.")


def _close_by_customer(conn, order_id: str, now: int, event: str) -> None:
    """The customer says they did not pay: payment options go away, but the
    upload stays open for 48 hours in case they did pay after all."""
    conn.execute("UPDATE orders SET status = 'expired', expires_at = MIN(expires_at, ?), customer_message = ? WHERE id = ?",
                 (now, CLOSED_BY_CUSTOMER, order_id))
    _event(conn, order_id, event, now)


def customer_view(order: dict, now: float | None = None) -> dict:
    """What the pay page shows: one `view` name plus the details it needs."""
    now = _now(now)
    st = order["status"]
    claim = order.get("claim")
    upload_open = st in OPEN_ORDER and now <= (order["upload_until"] or 0)
    v = {"view": st, "upload_open": upload_open, "can_pay": False, "late": False, "night": False,
         "push_seen": bool(order.get("push_seen_at")), "message": order.get("customer_message")}
    if st in OPEN_ORDER:
        if claim and claim["state"] in LIVE_CLAIM and upload_open:
            v["view"] = "claim_" + claim["state"]
        elif st == "pending" and order["member_status"] != "closed":
            v["view"] = "pay"
            v["can_pay"] = True
        elif st == "pending":
            v["view"] = "expired"
            v["message"] = ("The account for this order is no longer available. Don't pay to it. "
                            "If you already paid, upload your screenshot below.")
        elif not upload_open:
            v["view"] = "upload_closed"
    if st == "in_progress" and claim and claim.get("submitted_at"):
        wait = config.LATE_BANK_SECONDS if order["method"] == "bank" else config.LATE_UPI_SECONDS
        v["late"] = now - claim["submitted_at"] > wait
        v["night"] = datetime.fromtimestamp(now, TZ).hour >= config.NIGHT_HOUR
    if st == "review":
        info = REVIEW_REASONS.get(order.get("review_reason") or "")
        v["message"] = info[1] if info else "Our team is checking this payment."
    v["key"] = "|".join(str(x) for x in (st, claim and claim["id"], claim and claim["state"], v["late"],
                                          v["push_seen"]))
    return v


# ---------- screenshots (the customer's claim) ----------

def read_screenshot_lines(path: str) -> list[str]:
    """OCR; replaced in tests."""
    import ocr
    return ocr.read_lines(path)


def _image_ext(data: bytes) -> str:
    from PIL import Image, UnidentifiedImageError
    try:
        with Image.open(BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            im.verify()
    except (UnidentifiedImageError, OSError, ValueError):
        fmt = ""
    ext = {"PNG": "png", "JPEG": "jpg", "WEBP": "webp"}.get(fmt)
    if not ext:
        raise ServiceError("Upload a PNG or JPG screenshot.")
    return ext


def _ours(text: str, members) -> str | None:
    """The receiving account the screenshot says was paid, if it is one of ours.
    OCR often drops or adds spaces ("AaravMehta", "demo @ybl"), so names and UPI
    IDs are also compared with all spaces removed."""
    low = re.sub(r"\s+", " ", text.lower())
    squashed = low.replace(" ", "")
    for m in members:
        if m["upi_id"].lower() in squashed:
            return m["id"]
        if m["bank_account"] and re.search(r"(x{2,}|\*{2,}|\.{2,}|ending\s*)\s*" + m["bank_account"][-4:], low):
            return m["id"]
    for m in members:
        words = [w for w in re.findall(r"[a-z]+", m["name"].lower()) if len(w) >= 3]
        if words and all(re.search(r"\b" + w + r"\b", low) for w in words):
            return m["id"]
        full = "".join(re.findall(r"[a-z]+", m["name"].lower()))
        if len(full) >= 6 and full in squashed:
            return m["id"]
    return None


def _check(conn, order, f, image_hash: str, now: int, members) -> tuple[str | None, str | None, str | None]:
    """Runs the screenshot checks in order. Returns (problem, text, paid_to_member)."""
    amt = order["amount_paise"]
    if not f.utr_options and not f.amounts and not f.status:
        return "not_payment", ("This doesn't look like a payment screen. Upload the success screen from PhonePe, "
                               "Paytm, Google Pay or your bank app."), None
    if f.status == "failed":
        return "status_failed", ("This payment didn't go through (the screen says Failed). If money left your "
                                 "account, your bank returns it, usually within 48 hours."), None
    if f.status == "pending":
        return "status_pending", ("This payment is still pending. Wait until your UPI app says Successful, then "
                                  "upload that screen."), None
    if f.status != "success":
        return "status_missing", "We couldn't see “Successful” on this screenshot. Upload the full success screen.", None
    if not f.utr_options:
        return "utr_missing", ("We couldn't find the UTR / UPI Ref No. Upload the full, uncropped success screen "
                               "with the UTR visible."), None
    if not f.amounts:
        return "amount_missing", "We couldn't read the amount. Upload the full success screen.", None
    if amt not in f.amounts:
        return "amount_mismatch", (f"This screenshot shows {rupees(f.amounts[0])} but your order is {rupees(amt)}. "
                                   "Upload the screenshot of this payment."), None
    if not f.when:
        return "date_missing", "We couldn't read the payment date. Upload the full success screen.", None
    shot = f.when.replace(tzinfo=TZ)
    created = datetime.fromtimestamp(order["created_at"], TZ)
    too_old = (shot.timestamp() < order["created_at"] - config.CLOCK_SKEW_SECONDS if f.has_time
               else shot.date() < created.date())
    if too_old:
        when = shot.strftime("%d %b, %I:%M %p") if f.has_time else shot.strftime("%d %b %Y")
        return "date_old", (f"This screenshot is from an older payment ({when}). Your order was created "
                            f"{created.strftime('%d %b, %I:%M %p')}. Upload the screenshot of this payment."), None
    if f.note_order and f.note_order != order["id"]:
        return "note_other", (f"This screenshot belongs to order {f.note_order}. Open that order to upload it, "
                              "or upload this order's screenshot."), None
    paid_to = _ours(f.text, members)
    if not paid_to:
        return "paid_to_other", ("This screenshot shows a payment to an account that isn't ours. Upload the "
                                 "screenshot of your payment to the account shown on this page."), None
    return None, None, paid_to


def _utr_problem(conn, utr: str, order_id: str) -> tuple[str | None, str | None]:
    used = conn.execute(
        """SELECT order_id FROM claims WHERE utr = ? AND state = 'matched'
           UNION SELECT order_id FROM bank_credits WHERE utr = ? AND status = 'credited'""", (utr, utr)).fetchone()
    if used:
        return "utr_used", f"UTR {utr} has already been used. Each payment can be added only once."
    return None, None


def process_screenshot(order_id: str, data: bytes, customer_id: int | None = None,
                       now: float | None = None) -> dict:
    """Saves a screenshot, reads it and runs every check. The result is shown to
    the customer at once: details + Proceed, a choice between two UTRs, or the
    reason it was rejected."""
    now = _now(now)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise ServiceError("The screenshot is larger than 5 MB.")
    if not data:
        raise ServiceError("Choose a screenshot to upload.")
    ext = _image_ext(data)
    image_hash = hashlib.sha256(data).hexdigest()

    with transaction() as conn:
        _tick(conn, now)
        order = _order_row(conn, order_id)
        _check_upload_allowed(order, customer_id, now)
        dup = conn.execute("SELECT order_id FROM claims WHERE image_hash = ? AND order_id != ? LIMIT 1",
                           (image_hash, order["id"])).fetchone()

    folder = config.UPLOAD_DIR / order["id"]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{image_hash[:16]}.{ext}"
    path.write_bytes(data)

    problem = text = paid_to = None
    fields = None
    if dup:
        problem, text = "duplicate_image", "This screenshot was already uploaded. Upload the screenshot of this payment."
    else:
        fields = parse_screenshot(read_screenshot_lines(str(path)))

    with transaction() as conn:
        _tick(conn, now)
        order = _order_row(conn, order_id)
        _check_upload_allowed(order, customer_id, now)
        members = conn.execute("SELECT * FROM members").fetchall()
        state = "rejected"
        if not problem and conn.execute("SELECT 1 FROM claims WHERE image_hash = ? AND order_id != ? LIMIT 1",
                                        (image_hash, order["id"])).fetchone():
            problem, text = "duplicate_image", "This screenshot was already uploaded. Upload the screenshot of this payment."
            fields = None
        if fields and not problem:
            problem, text, paid_to = _check(conn, order, fields, image_hash, now, members)
            # Recorded even when an earlier check failed, so a reviewer sees it.
            paid_to = paid_to or _ours(fields.text, members)
            if not problem and len(fields.utr_options) > 1:
                state = "pick"
            elif not problem:
                problem, text = _utr_problem(conn, fields.utr, order["id"])
                state = "rejected" if problem else "ready"
        conn.execute("UPDATE claims SET state = 'replaced' WHERE order_id = ? AND state IN ('pick', 'ready')",
                     (order["id"],))
        shot_at = int(fields.when.replace(tzinfo=TZ).timestamp()) if fields and fields.when else None
        claim_id = conn.execute(
            """INSERT INTO claims (order_id, created_at, image_path, image_hash, ocr_text, utr, utr_options,
                                   amount_paise, amounts_seen, paid_to_member, shot_at, shot_has_time,
                                   status_word, note_order, problem, problem_text, state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (order["id"], now, str(path), image_hash, fields.text if fields else None,
             fields.utr if fields else None, json.dumps(fields.utr_options if fields else []),
             (order["amount_paise"] if order["amount_paise"] in fields.amounts else fields.amounts[0])
             if fields and fields.amounts else None,
             json.dumps(fields.amounts if fields else []), paid_to, shot_at,
             int(bool(fields and fields.has_time)), fields.status if fields else None,
             fields.note_order if fields else None, problem, text, state)).lastrowid
        if order["status"] == "action":
            conn.execute("UPDATE orders SET status = 'expired' WHERE id = ? AND (expires_at <= ? OR ? = 'closed')",
                         (order["id"], now, order["member_status"]))
            conn.execute("UPDATE orders SET status = 'pending' WHERE id = ? AND status = 'action'", (order["id"],))
        _event(conn, order["id"], f"Screenshot uploaded: " + (f"rejected ({problem})" if problem else
               "two UTRs found, customer to pick" if state == "pick" else
               f"all checks passed, UTR {fields.utr}"), now)
        if paid_to and paid_to != order["member_id"]:
            _event(conn, order["id"], f"Screenshot shows account {paid_to}, not {order['member_id']} "
                                      "(both are ours; the UTR decides)", now)
        return _claim_dict(conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone())


def _check_upload_allowed(order, customer_id, now: int) -> None:
    if not order or (customer_id is not None and order["customer_id"] not in (None, customer_id)):
        raise ServiceError("Order not found")
    if order["status"] not in OPEN_ORDER:
        raise ServiceError("This order already has a screenshot submitted.")
    if now > (order["upload_until"] or 0):
        raise ServiceError("Screenshots for this order could be uploaded for 48 hours only.")


def _live_claim(conn, order_id: str, claim_id: int, states):
    c = conn.execute("SELECT * FROM claims WHERE id = ? AND order_id = ?", (claim_id, order_id)).fetchone()
    latest = conn.execute("SELECT MAX(id) FROM claims WHERE order_id = ?", (order_id,)).fetchone()[0]
    if not c or c["id"] != latest or c["state"] not in states:
        raise ServiceError("This screenshot is no longer current. Upload it again.")
    return c


def pick_utr(order_id: str, claim_id: int, utr: str, customer_id: int | None = None,
             now: float | None = None) -> dict:
    """The screenshot had two possible UTRs; the customer chose one of them. They
    can't type one: only numbers read from their own screenshot are accepted."""
    now = _now(now)
    with transaction() as conn:
        order = _order_row(conn, order_id)
        _check_upload_allowed(order, customer_id, now)
        c = _live_claim(conn, order["id"], claim_id, ("pick",))
        if utr not in json.loads(c["utr_options"] or "[]"):
            raise ServiceError("Choose one of the numbers from your screenshot.")
        problem, text = _utr_problem(conn, utr, order["id"])
        conn.execute("UPDATE claims SET utr = ?, state = ?, problem = ?, problem_text = ? WHERE id = ?",
                     (utr, "rejected" if problem else "ready", problem, text, c["id"]))
        _event(conn, order["id"], f"Customer picked UTR {utr}", now)
        return _claim_dict(conn.execute("SELECT * FROM claims WHERE id = ?", (c["id"],)).fetchone())


def submit_claim(order_id: str, claim_id: int, customer_id: int | None = None, now: float | None = None) -> None:
    """Proceed: the customer confirms the details read from the screenshot."""
    now = _now(now)
    with transaction() as conn:
        order = _order_row(conn, order_id)
        _check_upload_allowed(order, customer_id, now)
        c = _live_claim(conn, order["id"], claim_id, ("ready",))
        problem, text = _utr_problem(conn, c["utr"], order["id"])
        if problem:
            conn.execute("UPDATE claims SET state = 'rejected', problem = ?, problem_text = ? WHERE id = ?",
                         (problem, text, c["id"]))
            return
        conn.execute("UPDATE claims SET state = 'submitted', submitted_at = ? WHERE id = ?", (now, c["id"]))
        conn.execute("UPDATE orders SET status = 'in_progress', customer_message = NULL WHERE id = ?", (order["id"],))
        _event(conn, order["id"], f"Customer pressed Proceed (UTR {c['utr']}); waiting for the bank", now)
        others = _open_claims(conn, c["utr"], exclude_id=c["id"])
        if others:
            for x in [c, *others]:
                _to_review(conn, x["order_id"], x["id"], "two_claims", now)
            return
        _match_claim(conn, order["id"], c["id"], now)


def request_review(order_id: str, claim_id: int, customer_id: int | None = None, now: float | None = None) -> None:
    """For a rejected screenshot the customer believes is right: a person checks it."""
    now = _now(now)
    with transaction() as conn:
        order = _order_row(conn, order_id)
        _check_upload_allowed(order, customer_id, now)
        c = _live_claim(conn, order["id"], claim_id, ("rejected",))
        attempts = conn.execute("SELECT COUNT(*) FROM claims WHERE order_id = ? AND state = 'rejected'",
                                (order["id"],)).fetchone()[0]
        allowed = (c["problem"] in PROBLEMS_REVIEWABLE_NOW or
                   (attempts >= 2 and c["problem"] in PROBLEMS_REVIEWABLE_AFTER_2))
        if not allowed:
            raise ServiceError("This screenshot can't be sent for review. Upload the right screenshot.")
        reason = {"amount_mismatch": "amount_claimed", "paid_to_other": "paid_to_other"}.get(c["problem"], "unreadable")
        conn.execute("UPDATE claims SET state = 'submitted', submitted_at = ? WHERE id = ?", (now, c["id"]))
        _to_review(conn, order["id"], c["id"], reason, now)
        # Another open claim on the same UTR: nobody gets it automatically.
        if c["utr"]:
            others = _open_claims(conn, c["utr"], exclude_id=c["id"])
            if others:
                for x in [c, *others]:
                    _to_review(conn, x["order_id"], x["id"], "two_claims", now)


def _open_claims(conn, utr: str, exclude_id: int | None = None) -> list:
    """Claims on this UTR that are waiting for the bank or for a person."""
    return conn.execute(
        "SELECT * FROM claims WHERE utr = ? AND id != ? AND state IN ('submitted', 'review')",
        (utr, exclude_id or 0)).fetchall()


def _to_review(conn, order_id: str, claim_id: int, reason: str, now: int) -> None:
    conn.execute("UPDATE claims SET state = 'review' WHERE id = ?", (claim_id,))
    conn.execute("UPDATE orders SET status = 'review', review_reason = ? WHERE id = ?", (reason, order_id))
    _event(conn, order_id, "Sent to review: " + REVIEW_REASONS[reason][0], now)


# ---------- the bank's proof ----------

def _credit(conn, order_id: str, claim_id: int | None, credit_id: int, amount: int, source: str, now: int) -> None:
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    credit = conn.execute("SELECT * FROM bank_credits WHERE id = ?", (credit_id,)).fetchone()
    if claim_id:
        conn.execute("UPDATE claims SET state = 'matched', utr = ?, decided_at = ? WHERE id = ?",
                     (credit["utr"], now, claim_id))
    conn.execute("UPDATE bank_credits SET status = 'credited', order_id = ? WHERE id = ?", (order_id, credit_id))
    conn.execute("""UPDATE orders SET status = 'paid', paid_at = ?, credited_paise = ?, utr = ?, review_reason = NULL,
                    customer_message = NULL WHERE id = ?""", (now, amount, credit["utr"], order_id))
    if order["customer_id"]:
        conn.execute("""INSERT INTO wallet_entries (customer_id, order_id, amount_paise, utr, source, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)""", (order["customer_id"], order_id, amount, credit["utr"],
                                                       source, now))
        conn.execute("UPDATE customers SET balance_paise = balance_paise + ? WHERE id = ?",
                     (amount, order["customer_id"]))
    proof = PROOF_NAMES.get(credit["source"], credit["source"])
    _event(conn, order_id, f"Credited {rupees(amount)}: UTR {credit['utr']} confirmed by {proof}", now)


def _match_claim(conn, order_id: str, claim_id: int, now: int) -> bool:
    c = conn.execute("SELECT * FROM claims WHERE id = ?", (claim_id,)).fetchone()
    credit = conn.execute("SELECT * FROM bank_credits WHERE utr = ?", (c["utr"],)).fetchone()
    if not credit:
        return False
    order = conn.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
    if credit["status"] == "credited":
        _to_review(conn, order_id, claim_id, "utr_credited_elsewhere", now)
    elif credit["amount_paise"] == order["amount_paise"] == c["amount_paise"]:
        _credit(conn, order_id, claim_id, credit["id"], credit["amount_paise"], "auto", now)
        return True
    else:
        _event(conn, order_id, f"Bank shows {rupees(credit['amount_paise'])} for UTR {c['utr']}", now)
        _to_review(conn, order_id, claim_id, "bank_amount_differs", now)
    return False


def _bank_credit(conn, member_id: str, utr: str | None, amount: int, sender: str | None, source: str, raw: str,
                 now: int, notification_id: int | None = None, statement_id: int | None = None):
    """Records money the bank says arrived and matches it to a waiting claim."""
    if not utr:
        conn.execute(
            """INSERT INTO bank_credits (member_id, utr, amount_paise, sender, source, received_at, raw,
                                         notification_id, statement_id, in_statement, status)
               VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'no_utr')""",
            (member_id, amount, sender, source, now, raw, notification_id, statement_id, int(bool(statement_id))))
        return None
    existing = conn.execute("SELECT * FROM bank_credits WHERE utr = ?", (utr,)).fetchone()
    if existing:
        if statement_id:
            conn.execute("UPDATE bank_credits SET in_statement = 1, statement_id = ? WHERE id = ?",
                         (statement_id, existing["id"]))
        if existing["amount_paise"] != amount:
            _alert(conn, "amount_conflict", f"UTR {utr}: {existing['source']} said {rupees(existing['amount_paise'])}, "
                   f"{source} says {rupees(amount)}.", now, existing["order_id"], member_id)
        credit_id = existing["id"]
    else:
        credit_id = conn.execute(
            """INSERT INTO bank_credits (member_id, utr, amount_paise, sender, source, received_at, raw,
                                         notification_id, statement_id, in_statement, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unmatched')""",
            (member_id, utr, amount, sender, source, now, raw, notification_id, statement_id,
             int(bool(statement_id)))).lastrowid
    credit = conn.execute("SELECT * FROM bank_credits WHERE id = ?", (credit_id,)).fetchone()
    if credit["status"] != "unmatched":
        return credit
    claims = conn.execute(
        """SELECT c.*, o.review_reason FROM claims c JOIN orders o ON o.id = c.order_id
           WHERE c.utr = ? AND c.state IN ('submitted', 'review')""", (utr,)).fetchall()
    if len(claims) > 1:
        # More than one customer claims this UTR (in any state): a person decides.
        for c in claims:
            _to_review(conn, c["order_id"], c["id"], "two_claims", now)
    elif len(claims) == 1:
        c = claims[0]
        # Only a claim that was just waiting for the money is matched automatically.
        if c["state"] == "submitted" or c["review_reason"] == "not_in_statement":
            conn.execute("UPDATE claims SET state = 'submitted' WHERE id = ?", (c["id"],))
            _match_claim(conn, c["order_id"], c["id"], now)
    return conn.execute("SELECT * FROM bank_credits WHERE id = ?", (credit_id,)).fetchone()


def ingest_sms(member_id: str, sender: str | None, body: str | None, key: str | None = None,
               now: float | None = None, demo: bool = False) -> dict:
    """An SMS forwarded by a receiving phone. Only SMS from a real HDFC sender ID
    count; the sender address comes from the network, not from contact names."""
    now = _now(now)
    sender = (sender or "").strip()[:40]
    body = (body or "")[:2000]
    with transaction() as conn:
        conn.execute("UPDATE members SET last_seen = ? WHERE id = ?", (now, member_id))
        _tick(conn, now)
        duplicate = conn.execute(
            """SELECT 1 FROM notifications WHERE member_id = ? AND package = 'sms' AND title = ? AND text = ?
               AND received_at >= ? AND result != 'duplicate'""",
            (member_id, sender, body, now - DUPLICATE_WINDOW_SECONDS)).fetchone()
        parsed = parse_bank_sms(body)
        genuine = bool(re.fullmatch(config.SMS_SENDER_PATTERN, sender.upper()))
        result = ("duplicate" if duplicate else "sms_rejected" if parsed and not genuine
                  else "sms" if parsed else "ignored")
        nid = conn.execute(
            """INSERT INTO notifications (member_id, package, title, text, notif_key, received_at, result)
               VALUES (?, 'sms', ?, ?, ?, ?, ?)""", (member_id, sender, body, key, now, result)).lastrowid
        if result == "sms_rejected":
            _alert(conn, "fake_sms", f"An SMS that looks like a bank credit ({rupees(parsed.amount_paise)}) came "
                   f"from “{sender}”, not an HDFC sender ID, on {member_id}'s phone. Ignored.", now,
                   member_id=member_id)
        if result != "sms":
            return {"result": result}
        credit = _bank_credit(conn, member_id, parsed.utr, parsed.amount_paise, parsed.sender,
                              "demo" if demo else "sms", body, now, notification_id=nid)
        return {"result": "sms", "utr": parsed.utr, "amount": format_paise(parsed.amount_paise),
                "order_id": credit["order_id"] if credit else None}


def ingest_notification(member_id: str, package: str | None, title: str | None, text: str | None,
                        notif_key: str | None = None, now: float | None = None) -> dict:
    """A UPI app alert (PhonePe / Paytm / GPay) from a receiving phone. It has no
    UTR, so it never credits money. If exactly one customer on this account is
    waiting with this amount, their page says "Payment received, confirming"."""
    now = _now(now)
    title = (title or "")[:500]
    text = (text or "")[:2000]
    with transaction() as conn:
        conn.execute("UPDATE members SET last_seen = ? WHERE id = ?", (now, member_id))
        _tick(conn, now)
        duplicate = conn.execute(
            """SELECT 1 FROM notifications
               WHERE member_id = ? AND IFNULL(notif_key, '') = ? AND title = ? AND text = ?
                 AND received_at >= ? AND result != 'duplicate'""",
            (member_id, notif_key or "", title, text, now - DUPLICATE_WINDOW_SECONDS)).fetchone()
        parsed = None if duplicate else parse_notification(title, text)
        result = "duplicate" if duplicate else ("payment" if parsed else "ignored")
        nid = conn.execute(
            """INSERT INTO notifications (member_id, package, title, text, notif_key, received_at, result)
               VALUES (?, ?, ?, ?, ?, ?, ?)""", (member_id, package, title, text, notif_key, now, result)).lastrowid
        if not parsed:
            return {"result": result}
        waiting = conn.execute(
            """SELECT id FROM orders WHERE member_id = ? AND amount_paise = ? AND status = 'in_progress'
               AND push_seen_at IS NULL""", (member_id, parsed.amount_paise)).fetchall()
        order_id = waiting[0]["id"] if len(waiting) == 1 else None
        conn.execute(
            """INSERT INTO payments (member_id, notification_id, amount_paise, payer_name, received_at, status, order_id)
               VALUES (?, ?, ?, ?, ?, 'signal', ?)""",
            (member_id, nid, parsed.amount_paise, parsed.payer_name, now, order_id))
        if order_id:
            conn.execute("UPDATE orders SET push_seen_at = ? WHERE id = ?", (now, order_id))
            _event(conn, order_id, f"UPI app alert: {rupees(parsed.amount_paise)} received"
                                   f"{' from ' + parsed.payer_name if parsed.payer_name else ''} (no UTR, not a proof)", now)
        return {"result": "payment", "amount": format_paise(parsed.amount_paise),
                "payer_name": parsed.payer_name, "order_id": order_id}


# ---------- statements ----------

def import_statement(member_id: str, filename: str, data: bytes, downloaded_at: int | None = None,
                     now: float | None = None) -> dict:
    """Checks every credit in a receiving account's statement against what we
    credited. The statement is the final word: it has every UTR and amount.
    `downloaded_at` is when the statement was downloaded from netbanking; rows
    after that moment can't be in it, so nothing after it is called missing."""
    now = _now(now)
    try:
        rows = parse_statement(data)
    except StatementError as e:
        raise ServiceError(str(e))
    if not rows:
        raise ServiceError("No credit rows found in this statement.")
    downloaded_at = int(downloaded_at or now)
    if downloaded_at > now + 300:
        raise ServiceError("The download time can't be in the future.")
    start = int(min(r.date for r in rows).replace(tzinfo=TZ).timestamp())
    last_day = max(r.date for r in rows).replace(tzinfo=TZ)
    end = int((last_day + timedelta(days=1)).timestamp())
    # Leave 15 minutes before the download out: those credits may still be posting.
    cut = min(end, downloaded_at - 15 * 60)
    with transaction() as conn:
        _tick(conn, now)
        if not conn.execute("SELECT 1 FROM members WHERE id = ?", (member_id,)).fetchone():
            raise ServiceError("Account not found")
        sid = conn.execute(
            """INSERT INTO statements (member_id, uploaded_at, filename, period_start, period_end, rows, credits, report)
               VALUES (?, ?, ?, ?, ?, ?, ?, '{}')""",
            (member_id, now, filename, start, cut, len(rows), sum(r.deposit_paise for r in rows))).lastrowid
        no_utr = []
        in_bank = {}
        for r in rows:
            posted = int(r.date.replace(tzinfo=TZ).timestamp())
            if r.utr:
                in_bank[r.utr] = in_bank.get(r.utr, 0) + r.deposit_paise
                _bank_credit(conn, member_id, r.utr, r.deposit_paise, None, "statement", r.narration, posted,
                             statement_id=sid)
            else:
                no_utr.append({"date": posted, "amount": r.deposit_paise, "narration": r.narration})

        def freeze_alert(order_id, text):
            if not conn.execute("SELECT 1 FROM alerts WHERE kind = 'not_in_bank' AND order_id = ? AND resolved = 0",
                                (order_id,)).fetchone():
                _alert(conn, "not_in_bank", text + " Freeze that wallet and check the phone.", now, order_id, member_id)

        confirmed, not_credited, wrong_amount = [], [], []
        for x in conn.execute("SELECT b.*, o.credited_paise FROM bank_credits b LEFT JOIN orders o ON o.id = b.order_id "
                              "WHERE b.statement_id = ? AND b.utr IS NOT NULL", (sid,)):
            x = dict(x)
            bank_amount = in_bank.get(x["utr"])
            if x["status"] == "unmatched":
                not_credited.append(x)
            elif x["status"] == "credited" and x["credited_paise"] != bank_amount:
                wrong_amount.append({**x, "bank_amount": bank_amount})
                freeze_alert(x["order_id"], f"Order {x['order_id']} was credited {rupees(x['credited_paise'])} for "
                             f"UTR {x['utr']}, but the {member_id} statement shows {rupees(bank_amount)}.")
            elif x["status"] == "credited":
                confirmed.append(x)

        # Credits we gave on an SMS from this account's phone, in the period the
        # statement covers, that the bank doesn't show. (Credits an admin approved
        # after checking the bank are not re-checked here.)
        missing = []
        for x in conn.execute(
                """SELECT b.utr, b.order_id, o.credited_paise FROM bank_credits b JOIN orders o ON o.id = b.order_id
                   WHERE b.member_id = ? AND b.status = 'credited' AND b.source IN ('sms', 'demo')
                   AND b.received_at >= ? AND b.received_at < ?""", (member_id, start, cut)):
            if x["utr"] not in in_bank:
                missing.append(dict(x))
                freeze_alert(x["order_id"], f"Order {x['order_id']} was credited {rupees(x['credited_paise'])} "
                             f"(UTR {x['utr']}) on an SMS, but the {member_id} statement has no such credit.")

        not_found = []
        for c in conn.execute(
                """SELECT c.id, c.order_id, c.utr, IFNULL(c.shot_at, c.submitted_at) AS paid_time
                   FROM claims c JOIN orders o ON o.id = c.order_id
                   WHERE c.state = 'submitted' AND o.status = 'in_progress' AND o.member_id = ?""", (member_id,)):
            if start <= c["paid_time"] < cut and c["utr"] not in in_bank:
                not_found.append(dict(c))
                _to_review(conn, c["order_id"], c["id"], "not_in_statement", now)

        report = {
            "downloaded_at": downloaded_at,
            "confirmed": [{"utr": x["utr"], "amount": x["amount_paise"], "order_id": x["order_id"]} for x in confirmed],
            "not_credited": [{"utr": x["utr"], "amount": x["amount_paise"], "narration": x["raw"]} for x in not_credited],
            "missing_in_bank": [{"order_id": x["order_id"], "utr": x["utr"], "amount": x["credited_paise"]} for x in missing],
            "wrong_amount": [{"order_id": x["order_id"], "utr": x["utr"], "amount": x["credited_paise"],
                              "bank_amount": x["bank_amount"]} for x in wrong_amount],
            "claims_not_found": [{"order_id": x["order_id"], "utr": x["utr"]} for x in not_found],
            "no_utr": no_utr,
        }
        conn.execute("UPDATE statements SET report = ? WHERE id = ?", (json.dumps(report), sid))
        return {"id": sid, **report}


def list_statements(limit: int = 30) -> list[dict]:
    with reader() as conn:
        out = []
        for r in conn.execute("SELECT * FROM statements ORDER BY id DESC LIMIT ?", (limit,)):
            s = dict(r)
            s["report"] = json.loads(s["report"] or "{}")
            out.append(s)
        return out


# ---------- review (admin decisions) ----------

def review_items() -> list[dict]:
    with reader() as conn:
        items = []
        for r in conn.execute(
                """SELECT o.*, m.name AS member_name, m.status AS member_status, c.name AS customer_name,
                          c.phone AS customer_phone
                   FROM orders o JOIN members m ON m.id = o.member_id LEFT JOIN customers c ON c.id = o.customer_id
                   WHERE o.status = 'review' ORDER BY o.created_at"""):
            o = dict(r)
            claim = conn.execute("SELECT * FROM claims WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                                 (o["id"],)).fetchone()
            o["claim"] = _claim_dict(claim) if claim else None
            o["reason"] = REVIEW_REASONS.get(o["review_reason"] or "", ("Needs a check", "", []))
            o["bank"] = None
            if claim and claim["utr"]:
                b = conn.execute("SELECT * FROM bank_credits WHERE utr = ?", (claim["utr"],)).fetchone()
                o["bank"] = dict(b) if b else None
            o["others"] = [dict(x) for x in conn.execute(
                "SELECT order_id, state FROM claims WHERE utr = ? AND order_id != ?",
                (claim["utr"], o["id"]))] if claim and claim["utr"] else []
            items.append(o)
        return items


def admin_approve(order_id: str, utr: str, amount, now: float | None = None, member_id: str | None = None) -> None:
    """An admin found the UTR in the bank and credits the amount the bank shows.
    `member_id` is the account whose statement had the UTR (default: the order's)."""
    now = _now(now)
    utr = re.sub(r"\s+", "", utr or "").upper()
    if not re.fullmatch(r"\d{12}|[A-Z]{4}[A-Z0-9]{12}", utr):
        raise ServiceError("Enter the UTR exactly as the bank shows it (12 digits, or 16 characters for NEFT)")
    paise = to_paise(amount)
    with transaction() as conn:
        order = _order_row(conn, order_id)
        if not order:
            raise ServiceError("Order not found")
        if order["status"] not in ("review", "in_progress", "failed", "expired", "pending", "action"):
            raise ServiceError(f"Order {order['id']} is {order['status']}")
        credit = conn.execute("SELECT * FROM bank_credits WHERE utr = ?", (utr,)).fetchone()
        if credit and credit["status"] == "credited":
            raise ServiceError(f"UTR {utr} was already credited to order {credit['order_id']}")
        if credit and credit["amount_paise"] != paise:
            raise ServiceError(f"The bank record for UTR {utr} is {rupees(credit['amount_paise'])}. Approve that amount.")
        if not credit:
            found_on = (member_id or order["member_id"]).strip().upper()
            if not conn.execute("SELECT 1 FROM members WHERE id = ?", (found_on,)).fetchone():
                raise ServiceError(f"Account {found_on} not found")
            cid = conn.execute(
                """INSERT INTO bank_credits (member_id, utr, amount_paise, source, received_at, raw, status)
                   VALUES (?, ?, ?, 'admin', ?, 'Checked in the bank by an admin', 'unmatched')""",
                (found_on, utr, paise, now)).lastrowid
        else:
            cid = credit["id"]
        claim = conn.execute("SELECT id FROM claims WHERE order_id = ? ORDER BY id DESC LIMIT 1",
                             (order["id"],)).fetchone()
        try:
            _credit(conn, order["id"], claim["id"] if claim else None, cid, paise, "admin", now)
        except sqlite3.IntegrityError:
            raise ServiceError(f"UTR {utr} was already credited")
        # Anyone else claiming this UTR can't get it now.
        for other in conn.execute(
                "SELECT order_id FROM claims WHERE utr = ? AND order_id != ? AND state IN ('submitted', 'review')",
                (utr, order["id"])):
            _event(conn, other["order_id"], f"UTR {utr} was approved for order {order['id']}", now)
        _event(conn, order["id"], "Approved by an admin after checking the bank", now)


def admin_reject(order_id: str, reason: str, now: float | None = None) -> None:
    now = _now(now)
    if reason not in REJECT_REASONS:
        raise ServiceError("Choose a reason")
    with transaction() as conn:
        order = _order_row(conn, order_id)
        if not order or order["status"] not in ("review", "in_progress"):
            raise ServiceError("Only orders waiting for the bank or in review can be rejected")
        conn.execute("UPDATE claims SET state = 'failed', decided_at = ? WHERE order_id = ? AND state IN "
                     "('submitted', 'review')", (now, order["id"]))
        conn.execute("UPDATE orders SET status = 'failed', customer_message = ? WHERE id = ?",
                     (REJECT_REASONS[reason], order["id"]))
        _event(conn, order["id"], "Rejected by an admin: " + REJECT_REASONS[reason], now)
        if order["customer_id"]:
            fails = conn.execute(
                """SELECT COUNT(*) FROM orders WHERE customer_id = ? AND status = 'failed'
                   AND id IN (SELECT order_id FROM order_events WHERE at > ? AND text LIKE 'Rejected by an admin%')""",
                (order["customer_id"], now - 86400)).fetchone()[0]
            if fails >= config.FAILED_CLAIMS_LIMIT:
                conn.execute("UPDATE customers SET paused_until = ? WHERE id = ?", (now + 86400, order["customer_id"]))
                _alert(conn, "customer_paused", f"Customer {order['customer_name']} ({order['customer_phone']}) had "
                       f"{fails} rejected claims in 24 hours. Add Cash paused for 24 hours.", now, order["id"])


def admin_ask(order_id: str, message: str, now: float | None = None) -> None:
    """Asks the customer for a better screenshot (e.g. the full transaction details page)."""
    now = _now(now)
    message = (message or "").strip()[:300]
    if not message:
        raise ServiceError("Write what the customer should upload")
    with transaction() as conn:
        order = _order_row(conn, order_id)
        if not order or order["status"] not in ("review", "in_progress"):
            raise ServiceError("Only orders waiting for the bank or in review can be sent back")
        conn.execute("UPDATE claims SET state = 'replaced' WHERE order_id = ? AND state IN ('submitted', 'review')",
                     (order["id"],))
        conn.execute("""UPDATE orders SET status = 'action', customer_message = ?, review_reason = NULL,
                        upload_until = MAX(IFNULL(upload_until, 0), ?) WHERE id = ?""",
                     (message, now + config.UPLOAD_WINDOW_SECONDS, order["id"]))
        _event(conn, order["id"], "Admin asked the customer: " + message, now)


def resolve_alert(alert_id: int) -> None:
    with transaction() as conn:
        conn.execute("UPDATE alerts SET resolved = 1 WHERE id = ?", (alert_id,))


def demo_bank_sms(order_id: str, utr: str | None = None, amount_paise: int | None = None,
                  now: float | None = None) -> dict:
    """Demo only: the SMS HDFC would send when this order's money arrives."""
    order = get_order(order_id, now)
    if not order:
        raise ServiceError("Order not found")
    claim = order["claim"]
    utr = utr or (claim and claim["utr"]) or "6" + "".join(secrets.choice("0123456789") for _ in range(11))
    paise = amount_paise or order["amount_paise"]
    body = (f"Money Received - INR {format_paise(paise)} in your HDFC Bank A/c xx4821 on "
            f"{datetime.now(TZ).strftime('%d-%m-%y')} by A/c linked to VPA demo.customer@ybl (UPI Ref No {utr}).")
    return ingest_sms(order["member_id"], "VM-HDFCBK", body, key=f"demo-{order_id}-{utr}", now=now, demo=True)


def demo_upi_alert(order_id: str, now: float | None = None) -> dict:
    order = get_order(order_id, now)
    if not order:
        raise ServiceError("Order not found")
    return ingest_notification(order["member_id"], "demo", "Money received",
                               f"₹{format_paise(order['amount_paise'])} received from Demo Customer",
                               notif_key=f"demo-{order_id}-{time.time()}", now=now)


# ---------- housekeeping ----------

def purge_old_screenshots(now: float | None = None) -> int:
    """Screenshots are kept for 90 days; the details read from them stay."""
    cutoff = _now(now) - config.SCREENSHOT_KEEP_DAYS * 86400
    removed = 0
    with transaction() as conn:
        for c in conn.execute("SELECT id, image_path FROM claims WHERE created_at < ? AND image_path IS NOT NULL",
                              (cutoff,)).fetchall():
            try:
                Path(c["image_path"]).unlink(missing_ok=True)
            except OSError:
                pass
            conn.execute("UPDATE claims SET image_path = NULL WHERE id = ?", (c["id"],))
            removed += 1
    return removed


def claim_image_path(claim_id: int) -> str | None:
    with reader() as conn:
        row = conn.execute("SELECT image_path FROM claims WHERE id = ?", (claim_id,)).fetchone()
        return row["image_path"] if row else None


def recent_amounts(limit: int = 6) -> list[int]:
    """Most used order amounts, for the quick-pick buttons."""
    with reader() as conn:
        return [r[0] for r in conn.execute(
            """SELECT amount_paise FROM orders GROUP BY amount_paise
               ORDER BY COUNT(*) DESC, MAX(created_at) DESC LIMIT ?""", (limit,))]


# ---------- dashboard ----------

ORDER_STATUSES = ("pending", "in_progress", "review", "paid", "failed", "expired", "action", "cancelled")


def daily_totals(day_start: int, days: int = 7) -> list[dict]:
    """Money credited per day for the last `days` days, oldest first.
    `day_start` is local midnight today."""
    first = day_start - (days - 1) * 86400
    out = [{"start": first + i * 86400, "paise": 0, "count": 0} for i in range(days)]
    with reader() as conn:
        for paid_at, paise in conn.execute(
            "SELECT paid_at, IFNULL(credited_paise, amount_paise) FROM orders WHERE status = 'paid' AND paid_at >= ?",
            (first,)
        ):
            i = (paid_at - first) // 86400
            if 0 <= i < days:
                out[i]["paise"] += paise
                out[i]["count"] += 1
    return out


def list_orders(status: str | None = None, q: str | None = None, limit: int = 300,
                now: float | None = None) -> dict:
    """Orders for the Orders page, filtered by status and a search text, plus
    how many orders each status has."""
    q = (q or "").strip()
    where, args = [], []
    if status in ORDER_STATUSES:
        where.append("o.status = ?")
        args.append(status)
    if q:
        cond = ("o.id LIKE ? OR o.note LIKE ? OR o.member_id LIKE ? OR m.name LIKE ? OR o.utr LIKE ? "
                "OR c.name LIKE ? OR c.phone LIKE ?")
        args += [f"%{q}%"] * 7
        try:  # "1250" or "₹1,250" also finds orders of that amount
            args.append(to_paise(q))
            cond += " OR o.amount_paise = ?"
        except ServiceError:
            pass
        where.append(f"({cond})")
    sql = """SELECT o.*, m.name AS member_name, c.name AS customer_name FROM orders o
             JOIN members m ON m.id = o.member_id
             LEFT JOIN customers c ON c.id = o.customer_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY o.created_at DESC LIMIT ?"
    with transaction() as conn:
        _tick(conn, _now(now))
        orders = [dict(r) for r in conn.execute(sql, (*args, limit))]
        counts = {s: 0 for s in ORDER_STATUSES}
        for st, n in conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status"):
            counts[st] = n
    counts["all"] = sum(counts.values())
    return {"orders": orders, "counts": counts}


def list_bank_credits(limit: int = 200) -> list[dict]:
    with reader() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT b.*, m.name AS member_name FROM bank_credits b JOIN members m ON m.id = b.member_id
               ORDER BY b.received_at DESC, b.id DESC LIMIT ?""", (limit,))]


def list_payments(limit: int = 200) -> list[dict]:
    """UPI app alerts the phones reported, newest first."""
    with reader() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT p.*, m.name AS member_name, n.package FROM payments p
               JOIN members m ON m.id = p.member_id
               LEFT JOIN notifications n ON n.id = p.notification_id
               ORDER BY p.received_at DESC LIMIT ?""", (limit,))]


def list_phone_messages(limit: int = 300) -> list[dict]:
    """Everything the receiving phones forwarded, newest first, with what happened to it."""
    with reader() as conn:
        rows = [dict(r) for r in conn.execute(
            """SELECT n.*, b.status AS credit_status, b.order_id AS credit_order, b.utr AS credit_utr,
                      p.order_id AS alert_order
               FROM notifications n
               LEFT JOIN bank_credits b ON b.notification_id = n.id
               LEFT JOIN payments p ON p.notification_id = n.id
               ORDER BY n.received_at DESC, n.id DESC LIMIT ?""", (limit,))]
    for r in rows:
        # kind of message, one-word status, and the order it relates to
        r["kind"] = "Bank SMS" if r["package"] == "sms" else "UPI alert"
        r["order"] = r["credit_order"] or r["alert_order"]
        if r["result"] == "sms_rejected":
            r.update(kind="Fake SMS", status="Blocked", tone="failed", group="blocked")
        elif r["result"] == "sms" and r["credit_status"] == "credited":
            r.update(status="Used", tone="paid", group="used")
        elif r["result"] == "sms":
            r.update(status="Waiting", tone="review", group="used")
        elif r["result"] == "payment":
            r.update(status="Signal", tone="in_progress", group="used")
        elif r["result"] == "duplicate":
            r.update(status="Duplicate", tone="expired", group="unused")
        else:
            r.update(status="Ignored", tone="expired", group="unused")
    return rows


def list_customers(limit: int = 300) -> list[dict]:
    with reader() as conn:
        return [dict(r) for r in conn.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM orders o WHERE o.customer_id = c.id AND o.status = 'paid') AS paid_count
               FROM customers c ORDER BY c.created_at DESC LIMIT ?""", (limit,))]


STATEMENT_EVERY_SECONDS = 4 * 3600


def statement_status(now: float | None = None) -> list[dict]:
    """Each open account with when its statement was last checked and whether one is due."""
    now = _now(now)
    with reader() as conn:
        last = {r[0]: (r[1], r[2]) for r in conn.execute(
            """SELECT member_id, MAX(uploaded_at), (SELECT s2.report FROM statements s2 WHERE s2.member_id = s.member_id
               ORDER BY s2.id DESC LIMIT 1) FROM statements s GROUP BY member_id""")}
        out = []
        for m in conn.execute("SELECT * FROM members WHERE status != 'closed' ORDER BY id"):
            at, report = last.get(m["id"], (None, None))
            rep = json.loads(report) if report else {}
            problems = len(rep.get("missing_in_bank", [])) + len(rep.get("wrong_amount", [])) if rep else 0
            out.append({"id": m["id"], "name": m["name"], "upi_id": m["upi_id"], "bank_account": m["bank_account"],
                        "last": at, "due": not at or now - at > STATEMENT_EVERY_SECONDS,
                        "hours": (now - at) // 3600 if at else None, "problems": problems})
        return out


def todo(day_start: int, now: float | None = None) -> dict:
    """What the admin should do next, for the Home page."""
    now = _now(now)
    with reader() as conn:
        last = dict(conn.execute("SELECT member_id, MAX(uploaded_at) FROM statements GROUP BY member_id").fetchall())
        members = conn.execute("SELECT * FROM members WHERE status != 'closed' ORDER BY id").fetchall()
        statements_due = [{"id": m["id"], "name": m["name"], "last": last.get(m["id"])} for m in members
                          if not last.get(m["id"]) or now - last[m["id"]] > STATEMENT_EVERY_SECONDS]
        offline = [dict(m) for m in members
                   if not m["last_seen"] or now - m["last_seen"] > config.OFFLINE_AFTER_SECONDS]
        unclaimed = conn.execute(
            "SELECT COUNT(*), IFNULL(SUM(amount_paise), 0) FROM bank_credits WHERE status = 'unmatched'").fetchone()
        recent = [dict(r) for r in conn.execute(
            """SELECT o.*, c.name AS customer_name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
               WHERE o.status NOT IN ('pending', 'expired', 'cancelled') ORDER BY o.created_at DESC LIMIT 8""")]
    return {"statements_due": statements_due, "offline": offline, "unclaimed_count": unclaimed[0],
            "unclaimed_paise": unclaimed[1], "recent": recent}


def dashboard(now: float | None = None, day_start: int | None = None) -> dict:
    """Everything the admin pages show. `day_start` is local midnight, for the
    "credited today" numbers."""
    now = _now(now)
    if day_start is None:
        day_start = now - 24 * 3600
    with transaction() as conn:
        _tick(conn, now)
        members = [dict(r) for r in conn.execute("SELECT * FROM members ORDER BY status = 'closed', id")]
        for m in members:
            m["online"] = bool(m["last_seen"]) and now - m["last_seen"] < config.OFFLINE_AFTER_SECONDS
            m["open"] = conn.execute(
                "SELECT COUNT(*) FROM orders WHERE member_id = ? AND status IN ('pending', 'in_progress', 'review')",
                (m["id"],)).fetchone()[0]
        orders = [dict(r) for r in conn.execute(
            """SELECT o.*, c.name AS customer_name FROM orders o LEFT JOIN customers c ON c.id = o.customer_id
               ORDER BY o.created_at DESC LIMIT 100""")]
        unmatched = [dict(r) for r in conn.execute(
            """SELECT b.*, m.name AS member_name FROM bank_credits b JOIN members m ON m.id = b.member_id
               WHERE b.status = 'unmatched' ORDER BY b.received_at DESC LIMIT 100""")]
        ignored = [dict(r) for r in conn.execute(
            "SELECT * FROM notifications WHERE result IN ('ignored', 'sms_rejected') ORDER BY received_at DESC LIMIT 20")]
        alerts = [dict(r) for r in conn.execute("SELECT * FROM alerts WHERE resolved = 0 ORDER BY id DESC")]
        paid_count, paid_paise = conn.execute(
            """SELECT COUNT(*), IFNULL(SUM(IFNULL(credited_paise, amount_paise)), 0) FROM orders
               WHERE status = 'paid' AND paid_at >= ?""", (day_start,)).fetchone()
        counts = dict(conn.execute("SELECT status, COUNT(*) FROM orders GROUP BY status").fetchall())
        late = conn.execute(
            """SELECT COUNT(*) FROM claims c JOIN orders o ON o.id = c.order_id
               WHERE o.status = 'in_progress' AND c.state = 'submitted'
               AND c.submitted_at < CASE o.method WHEN 'bank' THEN ? ELSE ? END""",
            (now - config.LATE_BANK_SECONDS, now - config.LATE_UPI_SECONDS)).fetchone()[0]
    active = [m for m in members if m["status"] != "closed"]
    stats = {
        "paid_today_count": paid_count,
        "paid_today_paise": paid_paise,
        "pending_count": counts.get("pending", 0),
        "in_progress_count": counts.get("in_progress", 0),
        "late_count": late,
        "review_count": counts.get("review", 0),
        "unmatched_count": len(unmatched),
        "alert_count": len(alerts),
        "online_count": sum(m["online"] for m in active),
        "member_count": len(active),
    }
    return {"members": members, "orders": orders, "unmatched": unmatched, "ignored": ignored,
            "alerts": alerts, "stats": stats}
