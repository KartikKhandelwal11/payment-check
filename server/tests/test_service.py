import io
import itertools
from datetime import datetime

import pytest
from PIL import Image

import config
import service
from service import TZ, ServiceError

# 30 Sep 2026, 10:40 AM in India
T0 = int(datetime(2026, 9, 30, 10, 40, tzinfo=TZ).timestamp())
UTR = "412345678901"
_colour = itertools.count(1)


def png() -> bytes:
    """A real, different image each time (the image fingerprint must differ)."""
    n = next(_colour)
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (n % 256, n // 256 % 256, 7)).save(buf, "PNG")
    return buf.getvalue()


def shot(amount="100", utr=UTR, to="rahul.k@okhdfcbank", when="30 Sep 2026, 10:42 AM",
         status="Payment Successful", note=None, extra=()):
    lines = [status, f"₹{amount}", "Paid to", "Rahul Kumar", to, when]
    if utr:
        lines.append(f"UPI Ref No: {utr}")
    if note:
        lines.append(f"Message: Order {note}")
    return lines + list(extra)


@pytest.fixture
def ocr(monkeypatch):
    """Set .lines to what the OCR should read from the next upload."""
    class Fake:
        lines = shot()
    fake = Fake()
    monkeypatch.setattr(service, "read_screenshot_lines", lambda path: fake.lines)
    return fake


@pytest.fixture(autouse=True)
def uploads(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")


@pytest.fixture
def rahul():
    service.add_member("rahul01", "Rahul Kumar", "rahul.k@okhdfcbank", now=T0)
    return "RAHUL01"


@pytest.fixture
def aman(rahul):
    return service.login_customer("Aman", "9876543210", now=T0)["id"]


def sms(member, body, now, sender="VM-HDFCBK"):
    return service.ingest_sms(member, sender, body, now=now)


def credit_sms(utr=UTR, amount="100.00"):
    return (f"Money Received - INR {amount} in your HDFC Bank A/c xx4821 on 30-09-26 by A/c linked to "
            f"VPA aman.v@ybl (UPI Ref No {utr}).")


def order_for(customer, amount="100", now=T0):
    return service.create_order(amount, customer_id=customer, now=now)["id"]


def upload(order_id, ocr, lines=None, now=T0 + 120, customer=None):
    if lines is not None:
        ocr.lines = lines
    return service.process_screenshot(order_id, png(), customer_id=customer, now=now)


def proceed(order_id, ocr, lines=None, now=T0 + 120):
    c = upload(order_id, ocr, lines, now)
    assert c["state"] == "ready", c["problem_text"]
    service.submit_claim(order_id, c["id"], now=now + 5)
    return c


def status(order_id, now=T0 + 600):
    return service.get_order(order_id, now=now)["status"]


# ---------- orders ----------

def test_same_amount_many_orders_and_accounts_rotate(rahul):
    service.add_member("PRIYA", "Priya", "priya@ybl", now=T0)
    members = [service.create_order("100", now=T0 + i)["member_id"] for i in range(4)]
    assert sorted(members) == ["PRIYA", "PRIYA", "RAHUL01", "RAHUL01"]


def test_paused_and_closed_accounts_get_no_new_orders(rahul):
    service.add_member("PRIYA", "Priya", "priya@ybl", now=T0)
    service.set_member_status("PRIYA", "paused", now=T0)
    assert {service.create_order("100", now=T0 + i)["member_id"] for i in range(3)} == {"RAHUL01"}
    service.set_member_status("RAHUL01", "closed", now=T0)
    service.set_member_status("PRIYA", "closed", now=T0)
    with pytest.raises(ServiceError, match="not available"):
        service.create_order("100", now=T0)


def test_one_pending_order_per_customer_and_hourly_limit(aman):
    first = order_for(aman)
    with pytest.raises(service.PendingOrder, match="unpaid order"):
        order_for(aman, now=T0 + 10)
    service.cancel_order(first, customer_id=aman, now=T0 + 20)
    for i in range(4):
        o = order_for(aman, now=T0 + 30 + i)
        service.cancel_order(o, customer_id=aman, now=T0 + 30 + i)
    with pytest.raises(ServiceError, match="at most 5 orders"):
        order_for(aman, now=T0 + 60)


def test_no_minimum_or_maximum_besides_upi_limit(aman):
    assert service.create_order("1", customer_id=aman, now=T0)["amount_paise"] == 100


# ---------- happy paths ----------

def test_screenshot_then_sms_credits_wallet(aman, ocr):
    o = order_for(aman)
    c = upload(o, ocr)
    assert c["state"] == "ready" and c["utr"] == UTR and c["paid_to_member"] == "RAHUL01"
    service.submit_claim(o, c["id"], now=T0 + 130)
    assert status(o) == "in_progress"
    r = sms("RAHUL01", credit_sms(), T0 + 140)
    assert r["order_id"] == o
    assert status(o) == "paid"
    assert service.get_customer(aman)["balance_paise"] == 10000


def test_sms_first_then_screenshot(aman, ocr):
    o = order_for(aman)
    sms("RAHUL01", credit_sms(), T0 + 60)
    proceed(o, ocr)
    assert status(o) == "paid"


def test_money_is_credited_once_only(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    sms("RAHUL01", credit_sms(), T0 + 140)
    sms("RAHUL01", credit_sms(), T0 + 900)  # same SMS again later
    assert service.get_customer(aman)["balance_paise"] == 10000


# ---------- screenshot checks ----------

@pytest.mark.parametrize("lines,problem", [
    (shot(utr=None), "utr_missing"),
    (shot(amount="50"), "amount_mismatch"),
    (shot(status="Payment Pending"), "status_pending"),
    (shot(status="Payment Failed"), "status_failed"),
    (shot(when="27 Sep 2026, 06:10 PM"), "date_old"),
    (shot(when="30 Sep 2026, 09:10 AM"), "date_old"),
    (shot(when="no date here"), "date_missing"),
    (shot(note="ZZZZ2222"), "note_other"),
    (shot(to="someone@ybl").__class__(["Payment Successful", "₹100", "Paid to", "Someone Else", "someone@ybl",
                                        "30 Sep 2026, 10:42 AM", f"UPI Ref No: {UTR}"]), "paid_to_other"),
    (["hello", "a photo of a cat"], "not_payment"),
])
def test_screenshot_rejections(aman, ocr, lines, problem):
    o = order_for(aman)
    c = upload(o, ocr, lines)
    assert c["state"] == "rejected"
    assert c["problem"] == problem
    assert c["problem_text"]  # always a reason for the customer


def test_missing_note_is_fine(aman, ocr):
    o = order_for(aman)
    assert upload(o, ocr, shot(note=None))["state"] == "ready"


def test_own_note_is_fine(aman, ocr):
    o = order_for(aman)
    assert upload(o, ocr, shot(note=o))["state"] == "ready"


def test_not_an_image(aman):
    o = order_for(aman)
    with pytest.raises(ServiceError, match="PNG or JPG"):
        service.process_screenshot(o, b"%PDF-1.4 not an image", now=T0 + 60)


def test_same_image_on_another_order_is_rejected(aman, ocr):
    bob = service.login_customer("Bob", "9123456780", now=T0)["id"]
    o1, o2 = order_for(aman), order_for(bob)
    img = png()
    service.process_screenshot(o1, img, now=T0 + 60)
    c = service.process_screenshot(o2, img, now=T0 + 70)
    assert c["problem"] == "duplicate_image"


def test_used_utr_is_rejected(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    sms("RAHUL01", credit_sms(), T0 + 140)
    bob = service.login_customer("Bob", "9123456780", now=T0)["id"]
    o2 = order_for(bob, now=T0 + 200)
    c = upload(o2, ocr, now=T0 + 300)
    assert c["problem"] == "utr_used"


def test_two_utrs_customer_picks_one_from_the_screenshot(aman, ocr):
    o = order_for(aman)
    c = upload(o, ocr, shot(utr=None, extra=["UTR: 412345678901", "Bank Ref No: 998877665544"]))
    assert c["state"] == "pick" and set(c["utr_options"]) == {"412345678901", "998877665544"}
    with pytest.raises(ServiceError, match="Choose one"):
        service.pick_utr(o, c["id"], "111111111111", now=T0 + 130)
    c = service.pick_utr(o, c["id"], "412345678901", now=T0 + 130)
    assert c["state"] == "ready"


def test_customer_cannot_submit_a_rejected_screenshot(aman, ocr):
    o = order_for(aman)
    c = upload(o, ocr, shot(amount="50"))
    with pytest.raises(ServiceError):
        service.submit_claim(o, c["id"], now=T0 + 130)


# ---------- matching is exact ----------

def test_one_digit_different_never_credits(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr, shot(utr="412345678907"))
    sms("RAHUL01", credit_sms(utr="412345678901"), T0 + 140)
    assert status(o) == "in_progress"
    assert service.get_customer(aman)["balance_paise"] == 0


def test_bank_amount_different_goes_to_review(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    sms("RAHUL01", credit_sms(amount="50.00"), T0 + 140)
    assert status(o) == "review"
    assert service.get_order(o)["review_reason"] == "bank_amount_differs"


def test_two_customers_one_utr_goes_to_review(aman, ocr):
    bob = service.login_customer("Bob", "9123456780", now=T0)["id"]
    o1, o2 = order_for(aman), order_for(bob)
    proceed(o1, ocr)
    proceed(o2, ocr)
    assert status(o1) == status(o2) == "review"
    sms("RAHUL01", credit_sms(), T0 + 300)
    assert status(o1) == status(o2) == "review"  # never automatic


# ---------- phone alerts ----------

def test_upi_app_alert_never_credits(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    r = service.ingest_notification("RAHUL01", "com.phonepe.app", "Received ₹100", "from Aman", now=T0 + 130)
    assert r["order_id"] == o
    order = service.get_order(o, now=T0 + 140)
    assert order["status"] == "in_progress" and order["push_seen_at"]
    assert service.get_customer(aman)["balance_paise"] == 0


def test_upi_app_alert_is_not_shown_when_two_customers_wait(aman, ocr):
    bob = service.login_customer("Bob", "9123456780", now=T0)["id"]
    o1, o2 = order_for(aman), order_for(bob)
    proceed(o1, ocr)
    proceed(o2, ocr, shot(utr="555555555555"))
    r = service.ingest_notification("RAHUL01", "com.phonepe.app", "Received ₹100", "from X", now=T0 + 200)
    assert r["order_id"] is None


def test_sms_from_a_phone_number_is_ignored(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    r = sms("RAHUL01", credit_sms(), T0 + 140, sender="+919876543210")
    assert r["result"] == "sms_rejected"
    assert status(o) == "in_progress"
    assert any(a["kind"] == "fake_sms" for a in service.dashboard(now=T0 + 150)["alerts"])


def test_contact_name_like_bank_is_still_a_phone_number(aman, ocr):
    # The phone app sends the network's sender address; a saved contact name
    # never reaches the server. A made-up header without the dash still fails.
    o = order_for(aman)
    proceed(o, ocr)
    assert sms("RAHUL01", credit_sms(), T0 + 140, sender="HDFCBK Bank")["result"] == "sms_rejected"


def test_sms_without_utr_waits_for_statement(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    r = sms("RAHUL01", "INR 100.00 credited to HDFC Bank A/c xx4821 on 30-09-26.", T0 + 140)
    assert r["result"] == "sms" and r["utr"] is None
    assert status(o) == "in_progress"
    csv = ("Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
           f"30/09/26,UPI-AMAN VERMA-aman.v@ybl-YESB0000123-{UTR}-Order {o},0000{UTR},30/09/26,,100.00,5100.00\n")
    rep = service.import_statement("RAHUL01", "s.csv", csv.encode(), now=T0 + 4 * 3600)
    assert status(o, now=T0 + 4 * 3600) == "paid"
    assert rep["confirmed"][0]["order_id"] == o


# ---------- statement ----------

STMT_HEAD = "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"


def test_statement_flags_claims_not_in_bank(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    csv = STMT_HEAD + "30/09/26,UPI-SOMEONE-x@ybl-YESB0000123-999999999999-,0,30/09/26,,20.00,20.00\n"
    rep = service.import_statement("RAHUL01", "s.csv", csv.encode(), now=T0 + 5 * 3600)
    assert rep["claims_not_found"][0]["order_id"] == o
    assert rep["not_credited"][0]["utr"] == "999999999999"
    assert status(o, now=T0 + 5 * 3600) == "review"


def test_statement_alerts_credit_that_bank_never_got(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    sms("RAHUL01", credit_sms(), T0 + 140)  # e.g. a fake SMS sent from a hacked phone
    csv = STMT_HEAD + "30/09/26,UPI-SOMEONE-x@ybl-YESB0000123-999999999999-,0,30/09/26,,20.00,20.00\n"
    rep = service.import_statement("RAHUL01", "s.csv", csv.encode(), now=T0 + 5 * 3600)
    assert rep["missing_in_bank"][0]["order_id"] == o
    assert any(a["kind"] == "not_in_bank" for a in service.dashboard(now=T0 + 5 * 3600)["alerts"])


def test_statement_matches_claim_in_review_after_late_money(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    service.import_statement("RAHUL01", "a.csv", (STMT_HEAD + "30/09/26,x,0,30/09/26,,5.00,5.00\n").encode(),
                             now=T0 + 5 * 3600)
    assert status(o, now=T0 + 5 * 3600) == "review"
    sms("RAHUL01", credit_sms(), T0 + 6 * 3600)
    assert status(o, now=T0 + 6 * 3600) == "paid"


# ---------- review ----------

def test_amount_claimed_review_and_approve_bank_amount(aman, ocr):
    o = order_for(aman)
    c = upload(o, ocr, shot(amount="50"))
    service.request_review(o, c["id"], now=T0 + 130)
    assert service.get_order(o)["review_reason"] == "amount_claimed"
    service.admin_approve(o, UTR, "50", now=T0 + 400)
    assert status(o) == "paid"
    assert service.get_customer(aman)["balance_paise"] == 5000


def test_review_needs_two_failed_uploads_for_unreadable(aman, ocr):
    o = order_for(aman)
    c = upload(o, ocr, shot(utr=None))
    with pytest.raises(ServiceError, match="can't be sent for review"):
        service.request_review(o, c["id"], now=T0 + 130)
    c = upload(o, ocr, shot(utr=None), now=T0 + 140)
    service.request_review(o, c["id"], now=T0 + 150)
    assert service.get_order(o)["review_reason"] == "unreadable"


def test_failed_status_can_never_go_to_review(aman, ocr):
    o = order_for(aman)
    upload(o, ocr, shot(status="Payment Failed"))
    c = upload(o, ocr, shot(status="Payment Failed"), now=T0 + 140)
    with pytest.raises(ServiceError):
        service.request_review(o, c["id"], now=T0 + 150)


def test_admin_cannot_approve_a_used_utr(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    sms("RAHUL01", credit_sms(), T0 + 140)
    bob = service.login_customer("Bob", "9123456780", now=T0)["id"]
    o2 = order_for(bob, now=T0 + 200)
    c = upload(o2, ocr, shot(utr="555555555555"), now=T0 + 300)
    service.submit_claim(o2, c["id"], now=T0 + 310)
    with pytest.raises(ServiceError, match="already credited"):
        service.admin_approve(o2, UTR, "100", now=T0 + 400)


def test_three_rejections_pause_add_cash(aman, ocr):
    for i in range(3):
        o = order_for(aman, now=T0 + i * 1000)
        when = datetime.fromtimestamp(T0 + i * 1000 + 60, TZ).strftime("%d %b %Y, %I:%M %p")
        c = upload(o, ocr, shot(utr=f"55555555555{i}", when=when), now=T0 + i * 1000 + 60)
        service.submit_claim(o, c["id"], now=T0 + i * 1000 + 70)
        service.admin_reject(o, "not_received", now=T0 + i * 1000 + 80)
        assert service.get_order(o)["customer_message"] == service.REJECT_REASONS["not_received"]
    with pytest.raises(ServiceError, match="paused"):
        order_for(aman, now=T0 + 4000)


def test_admin_asks_for_new_screenshot(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr, shot(utr="555555555555"))
    service.admin_ask(o, "Upload the full transaction details page", now=T0 + 400)
    order = service.get_order(o, now=T0 + 410)
    assert order["status"] == "action"
    assert service.customer_view(order, now=T0 + 410)["message"].startswith("Upload the full")
    proceed(o, ocr, now=T0 + 500)
    assert status(o, now=T0 + 600) == "in_progress"


# ---------- timing ----------

def test_late_payment_after_expiry_still_credits(aman, ocr):
    o = order_for(aman)
    late = T0 + 40 * 60
    proceed(o, ocr, shot(when="30 Sep 2026, 11:20 AM"), now=late)
    sms("RAHUL01", credit_sms(), late + 30)
    assert status(o, now=late + 60) == "paid"


def test_upload_window_closes_after_48_hours(aman, ocr):
    o = order_for(aman)
    with pytest.raises(ServiceError, match="48 hours"):
        upload(o, ocr, now=T0 + 49 * 3600)


def test_customer_sees_late_message(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    order = service.get_order(o, now=T0 + 20 * 60)
    assert service.customer_view(order, now=T0 + 20 * 60)["late"] is True


# ---------- account closed suddenly ----------

def test_closing_an_account_stops_unpaid_orders(aman, ocr):
    o = order_for(aman)
    service.set_member_status("RAHUL01", "closed", now=T0 + 60)
    order = service.get_order(o, now=T0 + 70)
    view = service.customer_view(order, now=T0 + 70)
    assert order["status"] == "expired" and not view["can_pay"] and view["upload_open"]
    assert "Don't pay" in view["message"]
    # A customer who had already paid can still upload and get credited.
    proceed(o, ocr, now=T0 + 80)
    sms("RAHUL01", credit_sms(), T0 + 90)
    assert status(o) == "paid"


def test_closing_with_payments_waiting_raises_alert(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    service.set_member_status("RAHUL01", "closed", now=T0 + 200)
    assert any(a["kind"] == "account_closed" for a in service.dashboard(now=T0 + 300)["alerts"])


# ---------- fixes from the code review ----------

def test_review_claim_blocks_auto_credit_of_same_utr(aman, ocr):
    bob = service.login_customer("Bob", "9123456780", now=T0)["id"]
    o1, o2 = order_for(aman), order_for(bob)
    proceed(o1, ocr)                                   # A waits for the bank with UTR
    c = upload(o2, ocr, shot(amount="50"))             # B: same UTR, says "I paid ₹50"
    service.request_review(o2, c["id"], now=T0 + 130)
    assert status(o1) == status(o2) == "review"
    sms("RAHUL01", credit_sms(), T0 + 140)
    assert status(o1) == status(o2) == "review"
    assert service.get_customer(aman)["balance_paise"] == 0


def test_late_upload_does_not_raise_false_not_in_bank_alert(aman, ocr):
    o = order_for(aman)
    sms("RAHUL01", credit_sms(), T0 + 60)              # money arrives on day 1
    day2 = T0 + 86400
    proceed(o, ocr, now=day2)                          # customer uploads on day 2
    csv = STMT_HEAD + "01/10/26,UPI-SOMEONE-x@ybl-YESB0000123-999999999999-,0,01/10/26,,20.00,20.00\n"
    rep = service.import_statement("RAHUL01", "s.csv", csv.encode(), now=day2 + 5 * 3600)
    assert rep["missing_in_bank"] == []


def test_statement_ignores_time_after_download(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)                                    # paid 10:42
    csv = STMT_HEAD + "30/09/26,UPI-SOMEONE-x@ybl-YESB0000123-999999999999-,0,30/09/26,,20.00,20.00\n"
    downloaded = T0 - 600                              # downloaded before the payment
    rep = service.import_statement("RAHUL01", "s.csv", csv.encode(), downloaded_at=downloaded, now=T0 + 4 * 3600)
    assert rep["claims_not_found"] == [] and status(o, now=T0 + 4 * 3600) == "in_progress"


def test_statement_amount_different_from_credit_is_an_alert(aman, ocr):
    o = order_for(aman, amount="1000")
    proceed(o, ocr, shot(amount="1000"))
    sms("RAHUL01", credit_sms(amount="1000.00"), T0 + 140)   # e.g. a faked SMS amount
    csv = STMT_HEAD + f"30/09/26,UPI-AMAN-aman@ybl-YESB0000123-{UTR}-,0,30/09/26,,100.00,100.00\n"
    rep = service.import_statement("RAHUL01", "s.csv", csv.encode(), now=T0 + 5 * 3600)
    assert rep["confirmed"] == [] and rep["wrong_amount"][0]["bank_amount"] == 10000
    assert any(a["kind"] == "not_in_bank" for a in service.dashboard(now=T0 + 5 * 3600)["alerts"])


def test_admin_approve_records_the_account_where_utr_was_found(aman, ocr):
    service.add_member("OLD01", "Old Account", "old@okhdfcbank", now=T0)
    o = order_for(aman)
    c = upload(o, ocr, shot(amount="50"))
    service.request_review(o, c["id"], now=T0 + 130)
    service.admin_approve(o, UTR, "50", member_id="OLD01", now=T0 + 400)
    assert [b["member_id"] for b in service.list_bank_credits() if b["utr"] == UTR] == ["OLD01"]


def test_customer_cannot_cancel_after_proceed(aman, ocr):
    o = order_for(aman)
    proceed(o, ocr)
    with pytest.raises(ServiceError):
        service.cancel_order(o, customer_id=aman, now=T0 + 200)


def test_zero_padded_reference_is_the_same_utr():
    from bank_parser import find_utr, normalize_ref
    assert normalize_ref("0000412345678901") == "412345678901"
    assert find_utr("INR 100 credited, Ref No 0000412345678901") == "412345678901"
    assert normalize_ref("012345678901") == "012345678901"   # a UTR starting with 0 stays whole


def test_update_bank_details(rahul):
    service.update_member("RAHUL01", "Rahul Kumar", "5010 0482 736195", "hdfc0001203")
    m = [x for x in service.dashboard()["members"] if x["id"] == "RAHUL01"][0]
    assert (m["bank_account"], m["ifsc"]) == ("50100482736195", "HDFC0001203")
    with pytest.raises(ServiceError, match="IFSC"):
        service.update_member("RAHUL01", "Rahul Kumar", "50100482736195", "HDFC123")


def test_new_amount_asks_about_the_unpaid_order_then_replaces_it(aman, ocr):
    first = order_for(aman, amount="321")
    with pytest.raises(service.PendingOrder) as e:
        order_for(aman, amount="200", now=T0 + 30)
    assert e.value.order["id"] == first
    second = service.create_order("200", customer_id=aman, now=T0 + 40, replace_pending=True)["id"]
    old = service.get_order(first, now=T0 + 50)
    view = service.customer_view(old, now=T0 + 50)
    assert old["status"] == "expired" and not view["can_pay"] and view["upload_open"]
    assert status(second, now=T0 + 50) == "pending"
    # They had paid the old one after all: it can still be uploaded and credited.
    proceed(first, ocr, shot(amount="321"), now=T0 + 60)
    sms("RAHUL01", credit_sms(amount="321.00"), T0 + 70)
    assert status(first) == "paid"


def test_customer_cancel_keeps_upload_open(aman, ocr):
    o = order_for(aman)
    service.cancel_order(o, customer_id=aman, now=T0 + 30)
    order = service.get_order(o, now=T0 + 40)
    assert order["status"] == "expired" and order["customer_message"] == service.CLOSED_BY_CUSTOMER


def test_paid_to_survives_ocr_spacing(aman, ocr):
    service.add_member("DEMO01", "Aarav Mehta", "demo@ybl", now=T0)
    service.set_member_status("RAHUL01", "paused", now=T0)
    o = order_for(aman)
    c = upload(o, ocr, ["Payment Successful", "Paid to", "AaravMehta", "demo @ybl", "₹100",
                        "30 Sep 2026, 10:42 AM", f"UTR:{UTR}"])
    assert c["state"] == "ready" and c["paid_to_member"] == "DEMO01"
