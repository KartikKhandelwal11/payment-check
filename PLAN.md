# Payment Check: plan and user flow

What we are building, how it flows end to end, what is done, and what is next. Written
so that anyone who gets this folder (or Claude on another computer) can carry on.
Setup steps are in [README.md](README.md); every case and how to verify it is in
[VERIFICATION.md](VERIFICATION.md); decisions and their reasons are in [CLAUDE.md](CLAUDE.md).

## The problem

A website has a wallet. Customers **Add Cash** (₹100, ₹200, ₹500, any amount) by UPI
(PhonePe, Paytm, GPay, any app) or bank transfer (IMPS / NEFT). There is **no payment
gateway**, so nothing tells us "paid". Money goes to a small set of **receiving HDFC
accounts** that change over time. We must credit the wallet only when the money really
arrived, never for a fake screenshot.

## The idea in one line

**Screenshot = the customer's claim. Bank SMS / statement = the proof. The UTR and the
amount must be exactly the same in both. Then, and only then, the wallet is credited.**

## End-to-end user flow

### Customer

1. Logs in at `/wallet` (demo login: name + mobile, no OTP).
2. **Add Cash** → amount → UPI or bank transfer → Continue.
3. Pay page shows **Pay now** (opens UPI apps) + **QR**, or account number + IFSC for a
   bank transfer. The QR carries the note `Order <id>` (bank remarks optional). 15 minutes
   to pay.
4. Pays in their app, takes the success screenshot.
5. **Uploads the screenshot (mandatory)** on the same page → "Reading your screenshot…".
6. One of:
   - **Screenshot checked**: UTR, amount, paid to, date, status shown read-only →
     **Proceed**. (No typing or editing the UTR.)
   - **Which one is your UTR?**: two numbers found → picks one of them.
   - **We couldn't accept this screenshot**: the exact reason + **Upload again**. For a
     wrong amount / other account (or after 2 failed tries) also "Send for review".
7. After Proceed: "Checking with the bank…" → "Payment received. Confirming…" (if a UPI
   app alert came) → **Payment successful** (wallet credited). If the SMS is late:
   "Bank confirmation is late… usually within 4 hours" (after 10 PM: tomorrow morning).
8. If a person has to check: "We're checking this payment". If rejected: the reason.
9. **My orders** lists every order and its status; screenshots can still be uploaded
   for 48 hours (e.g. paid late or closed the page).

### Receiving phone (one per HDFC account)

- Our Android app reads the **HDFC credit SMS** directly (real network sender address,
  only HDFC sender IDs count) and **PhonePe / Paytm / GPay alerts**, and forwards them.
  Offline → queued and sent later.

### Server

1. Creates the order, picks the active account with the fewest open orders.
2. Reads the screenshot with offline OCR and runs every check (see VERIFICATION.md §1).
3. On Proceed and on every SMS / statement row: **exact UTR + exact amount → credit once**.
   UTR locked forever. UPI app alerts only change the customer's message.
4. Things it can't decide → **Review**. Suspicious things → **alerts**.

### Admin (dashboard `/admin`)

- **Review**: screenshot + what was read + bank record + "How to verify" steps →
  Approve (UTR + amount as the bank shows) / Reject (reason the customer sees) / Ask
  customer to upload again.
- **Statements**: upload each account's HDFC statement (CSV) at 10 AM, 2 PM, 6 PM,
  10 PM. It credits what the SMS missed, sends claims not in the bank to Review, and
  raises a red alert for any credit the bank never received.
- **Accounts**: add / pause / **close now** (frozen account: unpaid customers are told
  not to pay; those who paid can still upload).
- **Bank credits**: money nobody claimed yet (can be linked to an order), all credits,
  UPI app alerts. **Customers**: wallets and paused customers. **Phone messages**:
  SMS / alerts not understood, and fake-sender SMS.

## What exists now (30 Sep 2026)

Working demo, end to end, in this folder:

- `server/` FastAPI + SQLite. Wallet and customer login, Add Cash, pay page with
  screenshot upload, OCR (RapidOCR, offline, free), all screenshot checks, exact UTR
  matching, HDFC SMS + statement parsing, review / reject / ask, account pause / close,
  alerts, fraud limits, screenshot deletion after 90 days, demo mode, payment-gateway style checkout with official app and bank logos. 106 tests pass.
- `android/` Kotlin app: SMS receiver + notification listener + retrying uploads.
  Builds (debug APK, lint 0 errors). **Not yet run on a real phone.**
- `VERIFICATION.md`: every case and how an admin verifies it.

## Next steps (in order)

1. **Collect real samples** (blocks going live):
   - 2–3 HDFC credit SMS (UPI, IMPS, NEFT) with the sender ID shown in the SMS app;
   - one HDFC statement CSV (a few lines are enough, account number hidden);
   - success screenshots from PhonePe, Paytm, GPay and 1–2 bank apps;
   - PhonePe / Paytm "money received" alert text.
   Then fix `server/bank_parser.py`, `server/screenshot_parser.py`,
   `server/notification_parser.py` and `SMS_SENDER_PATTERN`, and add them as tests.
2. Install the APK on one receiving phone, connect it, send real ₹1 payments, and check
   every step on the dashboard.
3. Connect to the real website: replace the demo wallet login with the site's own login,
   and let the site create orders via `POST /api/orders` and read the result
   (a signed webhook to the site is not built yet).
4. Deploy with https and a persistent disk (database + `server/uploads`).
5. Later: HDFC email alerts as a second proof channel (verify DKIM), a customer
   support channel, a payment gateway or HDFC virtual accounts if volume grows.

## Decided NOT to do

- Crediting from a screenshot alone, from a UPI app alert, or from time / name / amount
  guesses. One wrong UTR digit = no match.
- Typing a UTR instead of a screenshot, or editing the UTR read from it.
- Unique paisa amounts (₹100.07): the customer pays exactly what they chose.
- Logging in to netbanking automatically (OTP, lockouts, password on the server).
- PhonePe Business / Paytm Business accounts.
- Refunds, min/max amounts (not needed for now).
