# Add Cash: how every case is checked and verified

For the admins who run Add Cash. It lists every case the system handles: what the
system checks by itself, what the customer sees, and what a person does when the
system can't decide.

## The one rule

**Money is credited only when the UTR read from the customer's screenshot and a
UTR reported by the bank are exactly the same, and so is the amount.** One
different digit is a mismatch. Nothing is ever credited from:

- a screenshot alone (screenshots can be edited),
- a PhonePe / Paytm / GPay alert (it has no UTR),
- a guess from time, name or amount.

The bank's side comes from:

| Source | Speed | Has the UTR | Used for |
|---|---|---|---|
| PhonePe / Paytm / GPay alert | seconds | no | Only tells the customer "Payment received, confirming". Never credits. |
| HDFC credit SMS | seconds to minutes | usually (to be confirmed with real SMS) | Credits at once when the UTR and amount match. |
| HDFC statement upload | a few times a day | always | Credits anything the SMS missed, and catches mistakes. |

## Daily routine

1. Clear the **Review** list. Target: within 4 hours.
2. Upload each account's statement at **10 AM, 2 PM, 6 PM and 10 PM** (Statements page),
   with the time it was downloaded (nothing after that time is reported missing).
   The owner should download it from netbanking, not the person who holds that account's phone.
3. Act on every **alert** on the Overview page, then press Done.
4. Check every account's phone is **online** (Accounts page).

## How to verify a UTR in the bank

1. Note the receiving account shown on the order (for example RAHUL01).
2. Open that account in the HDFC app or netbanking → Account statement.
3. Search the 12-digit UTR (16 characters for NEFT). In a UPI line it looks like
   `UPI-AMAN VERMA-aman@ybl-YESB0000123-412345678901-...`.
4. Found, with the amount → approve with **the UTR and amount exactly as the bank shows them**,
   and the account where you found it.
   Not found → check the other accounts too (customers sometimes pay an old saved QR),
   then reject.

Uploading the statement does steps 2–4 automatically.

---

## 1. At upload (checked in 1–3 seconds)

The screenshot is mandatory, and there is no way to type a UTR instead. The UTR is shown
to the customer read-only; if it looks wrong, the only option is a clearer screenshot.

| Case | How the system checks | Customer sees | Next step |
|---|---|---|---|
| Not an image / over 5 MB | File type and size | "Upload a PNG or JPG screenshot." | Upload again |
| Not a payment screen | No UTR, amount or status found | "This doesn't look like a payment screen…" | Upload again |
| Payment failed | "Failed / declined / reversed" on screen | "This payment didn't go through…" | Pay again. Can't go to review. |
| Payment pending | "Pending / processing" on screen | "This payment is still pending…" | Upload once it says Successful |
| No "Successful" | No success word found | "We couldn't see Successful…" | Upload again; review after 2 tries |
| UTR not found | No UTR next to a UTR / UPI Ref No / UPI transaction ID label | "We couldn't find the UTR / UPI Ref No…" | Upload again; review after 2 tries |
| Two possible UTRs | Two labelled numbers | "Which one is your UTR?" with both numbers | Customer picks one of the two (can't type one) |
| Amount different | Amount on screen ≠ order amount | "This screenshot shows ₹50.00 but your order is ₹100.00…" | Upload again, or "I paid ₹50.00" → review |
| Old screenshot | Date/time on screen is before the order (2 minutes allowed for clock difference) | "This screenshot is from an older payment…" | Upload again. Can't go to review. |
| Date not readable | No date found | "We couldn't read the payment date…" | Upload again; review after 2 tries |
| Another order's note | Note "Order XXXX" of a different order | "This screenshot belongs to order XXXX…" | Upload on the right order; review after 2 tries (in case the note was misread) |
| No note | Note missing (skipped, bank has no remarks, app dropped it) | Nothing: this is fine | Continues normally |
| Paid to an account that isn't ours | Screen doesn't show any of our UPI IDs, account endings or names | "…a payment to an account that isn't ours…" | Upload again, or send for review |
| Paid to another of our accounts | Screen shows a different account of ours (for example an old saved QR) | Nothing: allowed | The UTR decides; noted in the order history |
| Same image uploaded before | Image fingerprint | "This screenshot was already uploaded." | Rejected |
| UTR already used | UTR already credited to any order | "UTR … has already been used." | Rejected |

## 2. Waiting for the bank (after Proceed)

| Case | How the system checks | Customer sees | Next step |
|---|---|---|---|
| SMS arrives, same UTR + amount | Exact match | "Payment successful" | Credited, UTR locked forever |
| SMS arrived before the screenshot | Stored; matched on Proceed | "Payment successful" right after Proceed | Credited |
| PhonePe / Paytm alert arrives | Amount + account, and exactly one customer waiting with that amount | "Payment received. Confirming with the bank…" | No credit; wait for SMS |
| Alert arrives, two customers waiting with that amount | Can't tell whose | Normal "Checking with the bank…" | No change |
| No alert at all (app killed) | — | Normal "Checking with the bank…" | Nothing lost; the SMS or statement decides |
| SMS late (15 min UPI, 3 h NEFT) | Time since Proceed | "Bank confirmation is late… usually within 4 hours" (after 10 PM: "tomorrow morning") | Next statement upload decides |
| SMS has no UTR | SMS read, no reference number | Stays "Checking" / "late" | Next statement upload credits it |
| Phone offline | The app queues SMS and sends them later | Stays "Checking" | Dashboard shows the phone offline after 20 min |
| SMS with the UTR but a different amount | Amount compare | "We're checking this payment…" | **Review**: approve the amount the bank shows |
| One digit different (misread) | Exact compare fails | Stays "Checking", then review after the statement | **Review**: read the UTR from the image, search the bank |
| Not in the statement either | Statement covers the payment time, no such UTR | "We're checking this payment…" | **Review** → usually reject "No payment with this UTR reached our account" |

## 3. Money mix-ups

| Case | How the system checks | What happens |
|---|---|---|
| Paid after the order expired | UTR match doesn't depend on expiry; upload allowed for 48 hours | Credited normally |
| Paid, never uploaded | SMS/statement credit sits under Bank credits → Not claimed | Credited as soon as the customer uploads from My orders. An admin can also link it: Bank credits → enter order number → Credit |
| Paid twice | The second payment has a different UTR | The customer uploads the second screenshot on a new order; until then it waits under Not claimed |
| Two customers claim one UTR | Same UTR on two orders, whether waiting for the bank or already in review | Both go to **Review**. Never automatic, even when the SMS arrives. The statement shows the sender name and UPI ID; approve the matching customer, reject the other ("This UTR belongs to another customer's payment") |
| Upload window over (48 hours) | Time since order | "This order is closed…" Support handles it by hand |
| Wrong amount entered, wants to change it | A new Add Cash while one order waits for payment | Asked "You have an unpaid order of ₹321. Did you pay it?" → "No" closes it and starts the new amount; "Yes" opens the old order to upload. A closed order can still take a screenshot for 48 hours. |

## 4. Fraud attempts

| Attempt | Why it fails |
|---|---|
| Edited / fake screenshot | No bank SMS or statement row ever has that UTR. After review it is rejected. 3 rejections in 24 hours pause Add Cash for that customer for 24 hours. |
| Someone else's real screenshot | Same image → rejected. Same UTR → rejected if credited, review if both waiting. |
| Many orders to overlap other payments | One pending order per customer, at most 5 orders an hour. Exact UTR matching means overlapping gains nothing. |
| Fake SMS from a phone number | The Android app sends the network's sender address. Only HDFC sender IDs (like `VM-HDFCBK`) count. Anything else is ignored and shown as an alert. |
| Phone number saved as "VM-HDFCBK" | The contact name never reaches the server; the real address is `+91…` and is rejected. |
| The phone holder fakes an SMS (rooted phone, script) | The next statement shows no such credit, or a different amount for that UTR → red alert "credited but not in the bank". Freeze that customer's wallet and check that phone. This is why the owner, not the phone holder, uploads statements. |

## 5. Receiving account problems

| Case | What to do |
|---|---|
| Account frozen or given up suddenly | Accounts → **Close now**. Unpaid orders on it stop at once; those customers see "Don't pay to this account… if you already paid, upload your screenshot". No new orders go to it. |
| Closed with payments still waiting | An alert lists them. Upload that account's last statement if you still can; otherwise decide them in Review. |
| Short break (phone repair, limit reached) | Accounts → **Pause**. No new orders; open ones carry on. Resume later. |

## Reject reasons the customer sees

- No payment with this UTR reached our account.
- This payment went to an account that isn't ours.
- This UTR belongs to another customer's payment.
- This screenshot doesn't match any payment we received.

## Not decided yet

- HDFC SMS text, sender IDs and statement columns are best guesses until real samples
  are collected. The Phone messages page shows SMS that were not understood.
- PhonePe / Paytm / GPay / bank app screenshot layouts are guesses too. Test with real
  screenshots and fix the reader where needed.
- A customer support channel does not exist yet.
