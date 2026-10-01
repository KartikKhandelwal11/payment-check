import os
from pathlib import Path

DB_PATH = os.environ.get("DB_PATH", "payments.db")

# Admin dashboard password and API key for creating orders.
ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "")

# Public address of this server, used in payment links.
BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")

# How long the customer has to pay after creating an order.
ORDER_TTL_SECONDS = int(os.environ.get("ORDER_TTL_MINUTES", "15")) * 60

# How long after creating an order the customer can still upload a screenshot
# (for example after paying late or closing the page).
UPLOAD_WINDOW_SECONDS = 48 * 3600

# No bank SMS this long after the customer pressed Proceed: tell them it is
# late and will be confirmed from the bank statement.
LATE_UPI_SECONDS = 15 * 60
LATE_BANK_SECONDS = 3 * 3600          # NEFT settles in batches

# Statements are uploaded during the day; payments after this hour are
# confirmed the next morning.
NIGHT_HOUR = 22

# A phone counts as offline if it has not checked in for this long.
# (The phone checks in every 15 minutes, and Android may delay that a little.)
OFFLINE_AFTER_SECONDS = 40 * 60

# Limits against abuse.
MAX_ORDERS_PER_HOUR = 5
FAILED_CLAIMS_LIMIT = 3               # in 24 hours -> Add Cash paused for 24 hours

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", Path(__file__).parent / "uploads"))
SCREENSHOT_KEEP_DAYS = 90

# Allowed tolerance between the phone that took the screenshot and our clock.
CLOCK_SKEW_SECONDS = 2 * 60

# Real HDFC SMS come from registered sender IDs such as VM-HDFCBK or
# AD-HDFCBK-S. A normal phone number can never match this.
SMS_SENDER_PATTERN = os.environ.get("SMS_SENDER_PATTERN", r"^[A-Z]{2}-HDFCBK(-[A-Z])?$")

TIMEZONE = os.environ.get("TIMEZONE", "Asia/Kolkata")

# Name shown to customers on the checkout page.
MERCHANT_NAME = os.environ.get("MERCHANT_NAME", "Payment Check")

# Adds demo helpers: a sample screenshot on the pay page and "Simulate bank SMS"
# buttons for admins. Never turn this on for real customers.
DEMO_MODE = os.environ.get("DEMO_MODE", "") == "1"
