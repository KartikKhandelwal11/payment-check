"""Demo only: draws a payment success screen for an order, so the whole flow
can be tried without paying. Served only when DEMO_MODE=1.

Variants let you try the edge cases: a wrong amount, a pending payment, an
old date, or a screen without a UTR.
"""

from __future__ import annotations

import io
import random
from datetime import datetime, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

VARIANTS = {
    "ok": "Correct screenshot",
    "wrong_amount": "Wrong amount",
    "pending": "Payment pending",
    "old": "Old date",
    "no_utr": "UTR cut off",
}

# Fonts that have the ₹ sign come first.
FONT_PATHS = [
    "/System/Library/Fonts/Helvetica.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]


def _font(size: int):
    for p in FONT_PATHS:
        if Path(p).exists():
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def random_utr() -> str:
    return "6" + "".join(random.choice("0123456789") for _ in range(11))


def render(order: dict, now: datetime, variant: str = "ok", utr: str | None = None) -> tuple[bytes, str]:
    """PNG bytes and the UTR printed on it."""
    utr = utr or random_utr()
    paise = order["amount_paise"]
    if variant == "wrong_amount":
        paise = max(100, paise // 2)
    when = now - timedelta(days=3) if variant == "old" else now
    rupees = f"{paise // 100:,}" + (f".{paise % 100:02d}" if paise % 100 else "")

    im = Image.new("RGB", (720, 1280), "#ffffff")
    d = ImageDraw.Draw(im)
    d.rectangle([0, 0, 720, 420], fill="#5f259f" if variant != "pending" else "#b45309")
    d.text((60, 40), now.strftime("%H:%M"), fill="#ffffff", font=_font(26))
    title = "Payment Pending" if variant == "pending" else "Payment Successful"
    d.text((60, 150), title, fill="#ffffff", font=_font(46))
    d.text((60, 230), when.strftime("%d %b %Y, %I:%M %p"), fill="#e9d5ff", font=_font(30))
    y = 470
    rows = [
        ("Paid to", 26, "#6b7280"),
        (order["member_name"], 36, "#111827"),
        (order["member_upi"], 28, "#374151"),
        (f"₹{rupees}", 64, "#111827"),
        (f"Message: Order {order['id']}", 28, "#374151"),
        ("Transaction ID: T" + now.strftime("%y%m%d%H%M%S") + "4471", 26, "#374151"),
    ]
    if variant != "no_utr":
        rows.append((f"UTR: {utr}", 32, "#111827"))
    rows.append(("Debited from XXXX 1234", 26, "#6b7280"))
    for text, size, colour in rows:
        d.text((60, y), text, fill=colour, font=_font(size))
        y += size + 42
    buf = io.BytesIO()
    im.save(buf, "PNG")
    return buf.getvalue(), utr
