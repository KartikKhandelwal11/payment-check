import pytest

from notification_parser import parse_notification


@pytest.mark.parametrize("title,text,paise,payer", [
    ("₹1,000.13 received", "from Rahul Sharma", 100013, "Rahul Sharma"),
    ("Payment received", "Received ₹ 1.91 from KARTIK KHANDELWAL", 191, "KARTIK KHANDELWAL"),
    ("", "₹500 received from Priya Verma on your UPI ID", 50000, "Priya Verma"),
    ("Rahul Sharma sent you ₹250", "Tap to view", 25000, "Rahul Sharma"),
    ("Google Pay", "Amit Singh sent you ₹99.50", 9950, "Amit Singh"),
    ("Money received", "Rs. 1200 credited to your account from Neha", 120000, "Neha"),
    ("Received ₹1000.07", "", 100007, None),
])
def test_reads_received_payments(title, text, paise, payer):
    p = parse_notification(title, text)
    assert p is not None
    assert p.amount_paise == paise
    assert p.payer_name == payer


@pytest.mark.parametrize("title,text", [
    ("You won ₹25 cashback", "Received on your payment"),
    ("Rahul requested ₹500", "Tap to pay"),
    ("Payment of ₹500 to Rahul Sharma", "Paid to Rahul Sharma successfully"),
    ("₹300 debited", "from your account"),
    ("Refund of ₹100 received", "from Swiggy"),
    ("Payment pending", "₹500 received from Rahul is pending"),
    ("Recharge done", "Your mobile recharge of ₹199 is successful"),
    ("New offer", "Get ₹50 off"),
    ("PhonePe", "Your account balance is updated"),
])
def test_ignores_other_notifications(title, text):
    assert parse_notification(title, text) is None
