# Payment Check

Add Cash for a website wallet, verified without a payment gateway. The customer pays by
UPI or bank transfer and uploads the payment screenshot. The money is added to the
wallet only when the UTR read from the screenshot matches a UTR reported by our bank
(HDFC credit SMS or the account statement), with the same amount.

```
server/          Python server: wallet, pay page, screenshot reading, matching, admin dashboard
android/         Android app on each receiving phone: forwards HDFC SMS and UPI app alerts
VERIFICATION.md  Every case the system handles, and how an admin verifies it
```

## How it works

1. The customer logs in at `/wallet`, taps **Add Cash**, picks an amount (₹100, ₹200,
   ₹500 or any amount) and UPI or bank transfer.
2. The server creates an order for that exact amount and shows one of the active
   receiving accounts (the one with the fewest open orders). The QR / Pay now link
   carries a note `Order <id>`; for bank transfers adding it in remarks is optional.
3. After paying, the customer **uploads the screenshot (required)**. The server reads it
   (offline OCR, 1–3 seconds) and checks: UTR present, amount, paid to one of our
   accounts, date after the order, status Successful, note, not uploaded before, UTR not
   used before. Any failure shows the reason and an **Upload again** button.
4. If everything passes, the details are shown read-only and the customer presses
   **Proceed**. The page shows "Checking with the bank…".
5. The receiving phone forwards:
   - the **PhonePe / Paytm / GPay alert** → the page says "Payment received, confirming".
     It has no UTR, so it never credits money;
   - the **HDFC credit SMS** → same UTR + same amount → the wallet is credited once and
     the UTR is locked forever.
6. SMS late or without a UTR → the next **statement upload** credits it. Anything the
   system can't decide goes to **Review**, where an admin checks the UTR in the bank.

See [VERIFICATION.md](VERIFICATION.md) for every case.

## 1. Running the server

Needs Python 3.9 or newer.

```bash
cd server
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

export ADMIN_TOKEN='some-long-password'
export BASE_URL='http://<Mac WiFi IP>:8000'
export DEMO_MODE=1          # only for trying it out, see below
.venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000
```

- Customer wallet: `http://localhost:8000/wallet` (demo login: any name + 10-digit number)
- Dashboard: `http://localhost:8000/admin` (password = `ADMIN_TOKEN`)
- Tests: `.venv/bin/python -m pytest -q`

### Trying the whole flow without money (`DEMO_MODE=1`)

1. Dashboard → **Accounts** → add an account (any UPI ID).
2. `/wallet` → log in → Add Cash ₹100 → Continue.
3. On the pay page, the yellow **Demo mode** box downloads sample screenshots: correct,
   wrong amount, pending, old date, UTR cut off. Upload one.
4. Correct screenshot → **Proceed**.
5. Dashboard → the order → **HDFC SMS with UTR …** → the wallet is credited.
   **PhonePe alert** shows "Payment received, confirming" but credits nothing.

Never turn on `DEMO_MODE` for real customers.

### For real customers

The server must be on the internet with **https** (a VPS with Caddy/Nginx, or
Render/Railway with a **persistent disk** for the database and the `uploads` folder).

| Variable | Meaning | Default |
|---|---|---|
| `ADMIN_TOKEN` | Dashboard password, API key, and the key that signs customer logins | required |
| `BASE_URL` | Public address of the server | `http://localhost:8000` |
| `DB_PATH` | Database file | `payments.db` |
| `UPLOAD_DIR` | Where screenshots are kept (deleted after 90 days) | `server/uploads` |
| `ORDER_TTL_MINUTES` | How long the customer has to pay | 15 |
| `SMS_SENDER_PATTERN` | Which SMS sender IDs count as HDFC | `^[A-Z]{2}-HDFCBK(-[A-Z])?$` |
| `MERCHANT_NAME` | Name shown on the checkout page | `Payment Check` |
| `DEMO_MODE` | `1` shows demo screenshots and simulate buttons | off |

Fixed rules (in `server/config.py`): screenshots can be uploaded for 48 hours after the
order; SMS counted as late after 15 minutes (UPI) or 3 hours (NEFT); one pending order
per customer, at most 5 orders an hour; 3 rejected claims in 24 hours pause Add Cash
for 24 hours. There is no minimum or maximum amount apart from the ₹1,00,000 UPI limit.

## 2. Receiving accounts

Dashboard → **Accounts**: account code, holder name, UPI ID, and optionally account
number + IFSC (needed for bank transfers). A phone code is shown once; enter it in the
app on the phone that receives that account's HDFC SMS.

- **Pause**: no new orders, open ones carry on.
- **Close now**: for a frozen or given-up account. Unpaid customers are told not to pay;
  those who already paid can still upload.

## 3. Statements

Upload each account's statement at 10 AM, 2 PM, 6 PM and 10 PM: HDFC netbanking →
Account statement → download as Delimited / CSV → Dashboard → **Statements**, with the
time you downloaded it. The report shows what was confirmed, money nobody claimed,
claims not in the bank (sent to Review), and credits the bank never received or received
with a different amount (red alert). Credits an admin approved after checking the bank
are not re-checked.

## 4. Android app

Build: `cd android && ./gradlew assembleDebug` (JDK 17 + Android SDK), APK at
`android/app/build/outputs/apk/debug/app-debug.apk`. Share it with each phone holder
("install unknown apps" must be allowed).

On each receiving phone:
1. Enter the server address and code → **Save and test** → "✅ Connected".
2. **Open notification access settings** → turn "Payment Forwarder" ON (UPI app alerts).
3. **Allow SMS** (HDFC credit SMS). The phone must hold the SIM registered with that
   HDFC account.
4. **Remove battery restriction**. On Xiaomi / Oppo / Vivo / Realme also turn Autostart
   ON and set battery to No restrictions.

### Privacy

- Notifications: only from PhonePe, Paytm, Paytm for Business and Google Pay, and only
  those with an amount.
- SMS: only messages that say credited / deposited / received with an amount, never
  OTPs. The app sends the sender address the network delivered, so a contact saved as
  "HDFCBK" can't pass as the bank.

## Do a real test first

The HDFC SMS text, HDFC statement columns and app screenshot layouts are best guesses.
Before real customers, send a few ₹1 payments from PhonePe, Paytm, GPay and a bank app,
upload their screenshots, and check the dashboard's **Phone messages** page for SMS that
were not understood.

## Things to keep in mind

- Receiving accounts should be used only for this. A personal payment of the same
  amount can't be credited to a customer (the UTR won't match), but it makes the
  statement harder to read.
- Business money arriving in personal accounts can get the account limited or frozen.
  A payment gateway or HDFC virtual accounts are the upgrade path.
- iPhones can't read other apps' notifications or SMS, so receiving phones must be Android.
