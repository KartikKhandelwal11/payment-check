import io
import re
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from PIL import Image

import config
import service
from service import TZ


@pytest.fixture(autouse=True)
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(config, "DEMO_MODE", True)


def client():
    import main
    return TestClient(main.app)


def png(n=1) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (8, 8), (n, 3, 7)).save(buf, "PNG")
    return buf.getvalue()


def fake_ocr(monkeypatch, utr="412345678901", amount="100"):
    now = datetime.now(TZ).strftime("%d %b %Y, %I:%M %p")
    lines = ["Payment Successful", f"₹{amount}", "Paid to", "Rahul Kumar", "rahul.k@okhdfcbank", now, f"UTR: {utr}"]
    monkeypatch.setattr(service, "read_screenshot_lines", lambda path: lines)


def login_customer(c):
    r = c.post("/wallet/login", data={"name": "Aman", "phone": "98765 43210"}, follow_redirects=False)
    assert r.status_code == 303
    return c


def admin(c):
    c.post("/admin/login", data={"password": "test-admin"})
    return c


def test_full_add_cash_flow_over_http(monkeypatch):
    token = service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank")
    c = login_customer(client())
    assert "Add Cash" in c.get("/wallet").text

    r = c.post("/wallet/add", data={"amount": "100", "method": "upi"}, follow_redirects=False)
    order_id = r.headers["location"].rsplit("/", 1)[1]
    page = c.get(f"/pay/{order_id}").text
    assert "upi://pay?pa=rahul.k%40okhdfcbank" in page and "am=100.00" in page
    assert "Upload payment screenshot" in page and "/static/brands/phonepe.svg" in page

    fake_ocr(monkeypatch)
    r = c.post(f"/pay/{order_id}/screenshot", files={"file": ("s.png", png(), "image/png")}, follow_redirects=True)
    assert "Screenshot verified" in r.text and "412345678901" in r.text
    claim_id = re.search(r'name="claim_id" value="(\d+)"', r.text).group(1)

    r = c.post(f"/pay/{order_id}/proceed", data={"claim_id": claim_id}, follow_redirects=True)
    assert "Confirming with the bank" in r.text
    assert c.get(f"/api/orders/{order_id}/status").json()["status"] == "in_progress"

    # The receiving phone forwards a PhonePe alert (no credit) and then HDFC's SMS.
    auth = {"Authorization": f"Bearer {token}"}
    c.post("/api/notify", json={"package": "com.phonepe.app", "title": "Received ₹100", "text": "from Aman"}, headers=auth)
    assert "Payment received" in c.get(f"/pay/{order_id}").text and "final confirmation" in c.get(f"/pay/{order_id}").text
    r = c.post("/api/sms", json={"sender": "VM-HDFCBK", "body": "Money Received - INR 100.00 in your HDFC Bank A/c "
                                  "xx4821 by A/c linked to VPA aman@ybl (UPI Ref No 412345678901)."}, headers=auth)
    assert r.json()["order_id"] == order_id
    status = c.get(f"/api/orders/{order_id}/status").json()
    assert status["status"] == "paid" and status["utr"] == "412345678901"
    assert "₹100.00" in c.get("/wallet").text


def test_rejected_screenshot_shows_reason_and_retry(monkeypatch):
    service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank")
    c = login_customer(client())
    order_id = c.post("/wallet/add", data={"amount": "100"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    fake_ocr(monkeypatch, amount="50")
    r = c.post(f"/pay/{order_id}/screenshot", files={"file": ("s.png", png(), "image/png")}, follow_redirects=True)
    assert "couldn't accept this screenshot" in r.text
    assert "shows ₹50.00 but your order is ₹100.00" in r.text
    assert "Upload again" in r.text and "I paid ₹50.00" in r.text


def test_other_customer_cannot_upload():
    service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank")
    c = login_customer(client())
    order_id = c.post("/wallet/add", data={"amount": "100"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    other = client()
    other.post("/wallet/login", data={"name": "Bob", "phone": "9123456780"})
    r = other.post(f"/pay/{order_id}/screenshot", files={"file": ("s.png", png(), "image/png")})
    assert r.status_code == 403


def test_phone_needs_valid_code():
    c = client()
    assert c.post("/api/notify", json={"text": "₹5 received from X"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/api/sms", json={"sender": "VM-HDFCBK", "body": "x"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/api/heartbeat").status_code == 401


def test_orders_api_needs_admin_token():
    service.add_member("RAHUL01", "Rahul", "rahul@ybl")
    c = client()
    assert c.post("/api/orders", json={"amount": "10"}).status_code == 401
    r = c.post("/api/orders", json={"amount": "10"}, headers={"Authorization": "Bearer test-admin"})
    assert r.status_code == 200 and r.json()["status"] == "pending"


def test_every_admin_page_renders(monkeypatch):
    service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank", bank_account="50100123454821", ifsc="HDFC0001234")
    cust = service.login_customer("Aman", "9876543210")
    o = service.create_order("100", customer_id=cust["id"])
    fake_ocr(monkeypatch, amount="50")
    claim = service.process_screenshot(o["id"], png(9))
    service.request_review(o["id"], claim["id"])
    c = client()
    assert c.get("/admin", follow_redirects=False).status_code == 303
    admin(c)
    for url in ["/admin", "/admin/review", "/admin/orders", "/admin/orders?status=review", "/admin/payments",
                "/admin/payments?tab=all", "/admin/payments?tab=alerts", "/admin/statements", "/admin/customers",
                "/admin/team", "/admin/notifications", "/admin/new", f"/admin/orders/{o['id']}",
                f"/admin/claims/{claim['id']}/image"]:
        r = c.get(url)
        assert r.status_code == 200, url
    page = c.get("/admin/review").text
    assert "Customer says they paid a different amount" in page
    assert "UTR to search in the bank" in page and "Found in bank" in page and "Not in bank" in page


def test_admin_review_approve_over_http(monkeypatch):
    service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank")
    cust = service.login_customer("Aman", "9876543210")
    o = service.create_order("100", customer_id=cust["id"])
    fake_ocr(monkeypatch, amount="50")
    claim = service.process_screenshot(o["id"], png(11))
    service.request_review(o["id"], claim["id"])
    c = admin(client())
    r = c.post(f"/admin/review/{o['id']}/approve", data={"utr": "412345678901", "amount": "50"}, follow_redirects=True)
    assert "approved and credited" in r.text
    assert service.get_customer(cust["id"])["balance_paise"] == 5000


def test_statement_upload_over_http():
    service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank")
    c = admin(client())
    csv = ("Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
           "30/09/26,UPI-AMAN-aman@ybl-YESB0000123-412345678901-x,0000412345678901,30/09/26,,100.00,100.00\n")
    r = c.post("/admin/statements", data={"member_id": "RAHUL01"},
               files={"file": ("s.csv", csv.encode(), "text/csv")}, follow_redirects=True)
    assert "Statement checked: 0 confirmed, 1 not credited" in r.text


def test_demo_screenshot_and_simulated_sms(monkeypatch):
    service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank")
    c = login_customer(client())
    order_id = c.post("/wallet/add", data={"amount": "100"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    r = c.get(f"/pay/{order_id}/demo-screenshot.png?variant=ok")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"


def test_closing_account_over_http():
    service.add_member("RAHUL01", "Rahul Kumar", "rahul.k@okhdfcbank")
    cust = client()
    login_customer(cust)
    order_id = cust.post("/wallet/add", data={"amount": "100"}, follow_redirects=False).headers["location"].rsplit("/", 1)[1]
    c = admin(client())
    r = c.post("/admin/members/RAHUL01/status", data={"status": "closed"}, follow_redirects=True)
    assert "RAHUL01 closed" in r.text
    page = cust.get(f"/pay/{order_id}").text
    assert "Don't pay to this account" in page and "upi://pay" not in page
