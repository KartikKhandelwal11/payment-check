from datetime import datetime

import pytest

from bank_parser import find_utr, parse_bank_sms, parse_statement
from screenshot_parser import parse_screenshot


# ---------- screenshots (layouts are guesses until real samples come in) ----------

def test_phonepe_like_screen_prefers_labelled_utr():
    f = parse_screenshot(["10:45", "Transaction Successful", "10:42 am on 30 Sep 2026", "Paid to", "RAHUL KUMAR",
                          "rahul.k@okhdfcbank", "₹100", "Transaction ID", "T2609301042178822", "UTR: 412345678901"])
    assert f.utr == "412345678901"
    assert f.amounts == [10000]
    assert f.status == "success"
    assert f.when == datetime(2026, 9, 30, 10, 42) and f.has_time


def test_gpay_like_screen():
    f = parse_screenshot(["₹100", "Completed", "30 Sep 2026, 10:42 am", "To: Rahul Kumar", "UPI transaction ID",
                          "4123 4567 8901", "Google transaction ID", "CICAgKCQx4bYbw"])
    assert f.utr == "412345678901"


@pytest.mark.parametrize("line", ["■200", "?200", "%200", "Z200", "₹ 200", "200"])
def test_rupee_sign_read_as_another_symbol(line):
    assert parse_screenshot(["Payment Successful", line]).amounts == [20000]


def test_amount_without_rupee_sign_on_its_own_line():
    assert parse_screenshot(["Paid successfully", "1,250.50"]).amounts == [125050]


def test_two_labelled_numbers_are_both_options():
    f = parse_screenshot(["Successful", "UTR 412345678901", "Bank Ref No 998877665544"])
    assert f.utr is None and f.utr_options == ["412345678901", "998877665544"]


def test_failed_beats_everything():
    assert parse_screenshot(["Payment Failed", "Paid to Rahul"]).status == "failed"


def test_neft_utr_is_16_characters():
    assert parse_screenshot(["Transfer successful", "UTR No: SBIN226273012345"]).utr == "SBIN226273012345"


def test_status_bar_clock_is_not_the_payment_time():
    f = parse_screenshot(["09:15", "Payment Successful", "Paid to x", "30 Sep 2026", "at 10:42 PM"])
    assert f.when == datetime(2026, 9, 30, 22, 42)


def test_amount_like_time_is_ignored():
    f = parse_screenshot(["Successful", "₹10.50", "30 Sep 2026"])
    assert not f.has_time and f.amounts == [1050]


def test_note_is_read():
    assert parse_screenshot(["Message: Order K7QM2P9X"]).note_order == "K7QM2P9X"


# ---------- HDFC SMS ----------

@pytest.mark.parametrize("body,paise,utr", [
    ("Money Received - INR 100.00 in your HDFC Bank A/c xx4821 on 30-09-26 by A/c linked to VPA aman.v@ybl "
     "(UPI Ref No 412345678901).", 10000, "412345678901"),
    ("Update! INR 1,250.00 deposited in HDFC Bank A/c XX4821 on 30-SEP-26 for IMPS-412345678901-AMAN VERMA-SBIN.",
     125000, "412345678901"),
    ("Rs.500 credited to a/c XX4821 on 30-09-26 by NEFT from SBIN226273012345 AMAN VERMA", 50000, "SBIN226273012345"),
    ("INR 100.00 credited to HDFC Bank A/c xx4821 on 30-09-26.", 10000, None),
])
def test_reads_credit_sms(body, paise, utr):
    p = parse_bank_sms(body)
    assert p and p.amount_paise == paise and p.utr == utr


@pytest.mark.parametrize("body", [
    "INR 100.00 debited from HDFC Bank A/c xx4821 to VPA shop@ybl (UPI Ref No 412345678901)",
    "Your OTP is 123456",
    "Sent Rs.100 from HDFC Bank A/c xx4821 to aman@ybl",
])
def test_ignores_other_sms(body):
    assert parse_bank_sms(body) is None


def test_find_utr_needs_exactly_one_number():
    assert find_utr("ref 412345678901 and 998877665544") is None


# ---------- statements ----------

def test_parses_hdfc_statement_rows():
    csv = ("HDFC BANK Ltd.\nStatement of account\n\n"
           "Date,Narration,Chq./Ref.No.,Value Dt,Withdrawal Amt.,Deposit Amt.,Closing Balance\n"
           "30/09/26,UPI-AMAN VERMA-aman@ybl-YESB0000123-412345678901-Order K7QM2P9X,0000412345678901,30/09/26,,100.00,5100.00\n"
           "30/09/26,IMPS-512345678901-PRIYA-SBIN,512345678901,30/09/26,,1250.00,6350.00\n"
           "30/09/26,NEFT CR-SBIN0001234-AMAN VERMA-SBIN226273012345,SBIN226273012345,30/09/26,,500.00,6850.00\n"
           "30/09/26,ATM WDL,000000,30/09/26,200.00,,6650.00\n"
           "30/09/26,CASH DEPOSIT,000000,30/09/26,,300.00,6950.00\n")
    rows = parse_statement(csv.encode())
    assert [(r.deposit_paise, r.utr) for r in rows] == [
        (10000, "412345678901"), (125000, "512345678901"), (50000, "SBIN226273012345"), (30000, None)]
