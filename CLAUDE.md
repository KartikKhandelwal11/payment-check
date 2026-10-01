# Payment Check

Add Cash for a website wallet, verified without a payment gateway. Customers pay by UPI
or bank transfer into a set of receiving HDFC accounts, upload the payment screenshot,
and the wallet is credited only when the bank confirms the same UTR and amount.

## Documents

| File | Contents |
|---|---|
| [PLAN.md](PLAN.md) | Goal, end-to-end user flow, current status, next steps, what is out of scope |
| [VERIFICATION.md](VERIFICATION.md) | Every case the system handles, what it checks, how an admin verifies it |
| [README.md](README.md) | Setup, demo mode, configuration, deployment, Android app |

All product text (customer pages, dashboard, errors, Android app, documentation) is in English.

## Ground rule for every change

This system moves money. Every rule is strict by design; loose alternatives were
considered and rejected (UTR tolerance of one digit, matching by amount or time,
typing a UTR instead of uploading a screenshot). Check any change against the
decisions below, and never add a path that skips a check.

## Design decisions

### Crediting

- **Credit rule:** the UTR read from the customer's screenshot and a bank record (HDFC
  credit SMS, HDFC statement row, or an admin who verified the bank) must match
  **exactly**, and so must the amount. A one-digit difference is a mismatch and is never
  credited automatically. Each UTR can be credited once.
- **Screenshot = claim, not proof.** It is mandatory, but screenshots can be edited, so
  they never credit on their own.
- **UPI app alerts (PhonePe / Paytm / GPay) never credit**, because they carry no UTR.
  They only switch the customer's message to "Payment received, confirming", and only
  when exactly one customer on that account is waiting with that amount.
- **No SMS, or SMS without a UTR → wait for the statement.** Amounts, times and names
  are never used to guess.

### Screenshot handling

- The UTR is read by OCR and shown **read-only**. The customer cannot type or edit it;
  they can only choose between two numbers read from the same screenshot. Unreadable →
  upload a clearer screenshot; after two failed uploads the customer can send it to Review.
- Checks (`service._check`): payment screen; status (failed / pending / successful);
  UTR present; amount equals the order; date not before the order (2-minute clock
  tolerance); note not from another order; paid to one of our accounts (current or
  closed); image not uploaded before; UTR not used before.
- The UPI note / bank remark `Order <id>` is a helper only. A missing note is accepted;
  a note belonging to another order is rejected.

### Bank proof

- The Android app reads HDFC SMS **directly** (SMS permission), not from the Messages
  app notification, because a notification shows the contact name and any number can
  be saved as "VM-HDFCBK". The server accepts only sender IDs matching
  `SMS_SENDER_PATTERN`; anything else is ignored and raises a `fake_sms` alert.
- Statements are uploaded at 10 AM, 2 PM, 6 PM and 10 PM, with their download time
  (only the period up to 15 minutes before download is checked). Each upload credits
  payments the SMS missed, sends claims not found in the bank to Review, and raises a
  `not_in_bank` alert for an SMS-based credit the bank never received or received with
  a different amount. The check uses when the bank reported the credit, not when the
  wallet was credited. Statements should be
  downloaded by the owner, not by the person holding that account's phone.

### Orders and accounts

- Customers pay the exact amount they chose; no unique-paisa amounts. Many orders of the
  same amount can be open at once because matching is by UTR.
- Orders go to the active account with the fewest open orders.
- **Pause** an account: no new orders. **Close now** (for example, a frozen account):
  unpaid orders stop and customers are told not to pay; customers who already paid can
  still upload; payments still waiting raise an alert.
- One order waiting for payment per customer. Starting a new amount asks "Did you pay
  it?"; "No" closes the old order (and "I haven't paid" in the leave dialog does the
  same). A customer-closed order loses its payment options but keeps the 48-hour
  upload, so a payment made after all is never stranded. Only admins set `cancelled`.
- Limits: five orders per hour; three rejected claims in
  24 hours pause Add Cash for 24 hours. Screenshots can be uploaded for 48 hours; a
  payment counts as late after 15 minutes (UPI) or 3 hours (NEFT). Screenshots are
  deleted after 90 days. No minimum or maximum amount apart from the ₹1,00,000 UPI limit.

### Review and alerts

- Two open claims on one UTR (waiting or in review) always go to Review; never automatic.
- Review is a person verifying the UTR in the bank: **Approve** with the UTR, amount and
  account the bank shows, **Reject** with a fixed reason shown to the customer, or **Ask** the
  customer to upload again. Alerts appear on the admin dashboard.

### Admin dashboard

- Plain words everywhere: menu Overview, Payments to check, All payments, Bank statement,
  Bank accounts, Customers; More: Money without a claim, Phone messages. Order states are
  shown as "Not paid yet", "Waiting for bank", "Needs your decision", "Added to wallet",
  "Rejected" (`STATUS` in `main.py`).
- Overview keeps the visual summary (today's total with 7-day bars, four stat tiles,
  recent orders, alerts, checklist, accounts) with a "to do" strip on top (decisions
  waiting, statements due every 4 hours, phones offline) from `service.todo`.
- Payments to check shows one decision per card: screenshot, the UTR to search (copy
  button), then "Found in bank · add ₹X" or "Not in bank · reject"; other amounts,
  accounts, reasons and "ask the customer" sit under More options.

### Technology and scope

- OCR: RapidOCR (offline, free, installed with pip; no system packages needed).
- The demo wallet and customer login live in this server and are expected to move to
  the real website later.
- The pay page is a mobile-first checkout in the style of a payment gateway sheet
  (`templates/pay.html`, `static/checkout.css`): one 480px column (a centered card on
  computers). App bar (merchant, "Secure checkout", timer); order summary with amount
  and a 3-step stepper (Pay → Upload proof → Confirmed); "Pay with UPI app" row of app
  icons (Google Pay, PhonePe, Paytm, Other); "Other ways to pay" grouped list (Scan QR,
  Bank transfer with copy buttons); a proof card showing what screenshot to upload.
  On phones a sticky bottom bar holds the one main action ("I've paid · Upload proof"
  → photo picker → "Verify screenshot", later "Proceed"); computers get in-card
  controls and the QR opened. Leaving an unfinished screen (page back arrow or the
  phone's back button, caught with a history entry) asks for confirmation; "I haven't
  paid, leave" cancels the order so a new one can be made. No `beforeunload` prompt,
  because opening a UPI app would trigger it. Neutral greys + one blue accent (#1a4fd6), green only for
  success. Official logos in `static/brands/` (Wikimedia Commons; `gpay-icon.svg` and
  `hdfc-icon.svg` are crops of the official logos). Bank details come from the receiving
  account's record (editable under Accounts), never hard-coded. Light theme only.
- Out of scope for now: payment gateway (upgrade path if volume grows), automated
  netbanking login (OTP, lockouts, stored passwords), PhonePe / Paytm Business apps,
  refunds, a customer support channel, third-party automation apps such as MacroDroid.

## Code map

| Path | Purpose |
|---|---|
| `server/service.py` | Business rules: orders, claims, matching (`_bank_credit`, `_match_claim`, `_credit`), statements, review, accounts, dashboard |
| `server/screenshot_parser.py` | Fields read from the screenshot text |
| `server/ocr.py` | RapidOCR → lines of text |
| `server/bank_parser.py` | HDFC credit SMS and statement CSV |
| `server/notification_parser.py` | UPI app alerts |
| `server/demo_screenshot.py` | Sample screenshots for demo mode |
| `server/main.py` | Routes: `/wallet`, `/pay/<id>`, phone API (`/api/sms`, `/api/notify`, `/api/heartbeat`), `/admin/...`, `POST /api/orders` |
| `server/db.py` | Schema and automatic column migration (table `members` = receiving accounts) |
| `android/.../SmsReceiver.kt` | Reads bank SMS with the network sender address |
| `android/.../PaymentListenerService.kt` | Reads UPI app alerts |
| `android/.../ForwardWorker.kt` | Sends to the server, retries when offline |

## Status (30 Sep 2026)

- The server works end to end in demo mode (`DEMO_MODE=1`: sample screenshots and
  "simulate SMS / alert" buttons). 106 automated tests pass. Real OCR has been checked
  on the demo screenshots (correct, wrong amount, pending, old date, missing UTR).
- Python 3.9 or newer. `main.py` uses `Optional[...]` rather than `X | None` because
  FastAPI evaluates route annotations at runtime.
- The Android app builds (debug APK, lint 0 errors) with JDK 17 and the Android SDK.
  It has not yet been run on a real phone.
- **Formats are provisional:** HDFC SMS text and sender IDs, HDFC statement columns and
  app screenshot layouts must be confirmed with real samples.
- `upi-link.html` is an early standalone QR generator, kept for reference.

## Next steps

See PLAN.md → "Next steps": collect real samples → update parsers and add them as
tests → test on one phone with ₹1 payments → connect the real website → deploy with https.
