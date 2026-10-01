import hashlib
import hmac
import io
import secrets
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import segno
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

import config
import demo_screenshot
import service
from db import init_db
from service import ServiceError, format_paise

if not config.ADMIN_TOKEN:
    raise RuntimeError("Set ADMIN_TOKEN (the dashboard password)")

init_db()
service.purge_old_screenshots()
app = FastAPI(title="Payment Check", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
TZ = ZoneInfo(config.TIMEZONE)


def rupees(paise: Optional[int]) -> str:
    if paise is None:
        return ""
    rupee, p = divmod(paise, 100)
    # Indian grouping: 1,00,000
    s = str(rupee)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        head = ",".join([head[max(i - 2, 0):i] for i in range(len(head), 0, -2)][::-1])
        s = f"{head},{tail}"
    return f"₹{s}.{p:02d}"


def local_time(ts: Optional[int]) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, TZ).strftime("%d %b, %I:%M %p")


def plain_amount(paise: int) -> str:
    """1000.00 -> "1000", 99.50 -> "99.50" (for form values)."""
    s = format_paise(paise)
    return s[:-3] if s.endswith(".00") else s


def rupees_short(paise: int) -> str:
    s = rupees(paise)
    return s[:-3] if s.endswith(".00") else s


def weekday(ts: int) -> str:
    return datetime.fromtimestamp(ts, TZ).strftime("%a")


templates.env.filters["weekday"] = weekday
templates.env.filters["rupees"] = rupees
templates.env.filters["plain_amount"] = plain_amount
templates.env.filters["rupees_short"] = rupees_short
templates.env.filters["local_time"] = local_time
# Plain words for order states, used across the dashboard.
templates.env.globals["STATUS"] = {
    "pending": "Not paid yet", "expired": "Not paid", "action": "Asked to upload again", "in_progress": "Waiting for bank",
    "review": "Needs your decision", "paid": "Added to wallet", "failed": "Rejected", "cancelled": "Cancelled",
}


def upi_query(order: dict) -> str:
    """The pay?… part shared by the generic upi:// link and the app-specific links."""
    params = {
        "pa": order["member_upi"],
        "pn": order["member_name"],
        "am": format_paise(order["amount_paise"]),
        "cu": "INR",
        "tn": f"Order {order['id']}",
    }
    return urlencode(params, quote_via=quote)


def upi_link(order: dict) -> str:
    return "upi://pay?" + upi_query(order)


def order_json(order: dict) -> dict:
    view = service.customer_view(order)
    return {
        "order_id": order["id"],
        "status": order["status"],
        "view": view["view"],
        "key": view["key"],
        "amount": format_paise(order["amount_paise"]),
        "credited": format_paise(order["credited_paise"]) if order["credited_paise"] else None,
        "utr": order["utr"],
        "expires_at": order["expires_at"],
        "paid_at": order["paid_at"],
        "pay_url": f"{config.BASE_URL}/pay/{order['id']}",
    }


# ---------- auth ----------

def device_member(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    member = service.member_for_token(token) if token else None
    if not member:
        raise HTTPException(401, "Invalid phone code")
    return member


def api_admin(authorization: str = Header(default="")):
    token = authorization.removeprefix("Bearer ").strip()
    if not secrets.compare_digest(token, config.ADMIN_TOKEN):
        raise HTTPException(401, "Invalid admin token")


def _admin_session() -> str:
    # The cookie holds a value derived from the password, never the password itself.
    return hmac.new(config.ADMIN_TOKEN.encode(), b"admin-session", hashlib.sha256).hexdigest()


def is_admin(request: Request) -> bool:
    return secrets.compare_digest(request.cookies.get("admin", ""), _admin_session())


@app.exception_handler(ServiceError)
async def service_error(request: Request, exc: ServiceError):
    return JSONResponse({"detail": str(exc)}, status_code=400)


# ---------- customer ----------

def _sign(value: str) -> str:
    return hmac.new(config.ADMIN_TOKEN.encode(), value.encode(), hashlib.sha256).hexdigest()[:32]


def current_customer(request: Request) -> Optional[dict]:
    raw = request.cookies.get("cust", "")
    cid, _, sig = raw.partition(".")
    if not cid.isdigit() or not hmac.compare_digest(sig, _sign(cid)):
        return None
    return service.get_customer(int(cid))


def back(url: str, error: Optional[str] = None) -> RedirectResponse:
    return RedirectResponse(url + (f"?{urlencode({'error': error})}" if error else ""), status_code=303)


@app.get("/wallet/login", response_class=HTMLResponse)
def wallet_login_page(request: Request):
    return templates.TemplateResponse(request, "wallet_login.html", {"error": None, "form": {}})


@app.post("/wallet/login")
def wallet_login(request: Request, name: str = Form(""), phone: str = Form("")):
    try:
        c = service.login_customer(name, phone)
    except ServiceError as e:
        return templates.TemplateResponse(request, "wallet_login.html",
                                          {"error": str(e), "form": {"name": name, "phone": phone}}, status_code=400)
    resp = RedirectResponse("/wallet", status_code=303)
    cid = str(c["id"])
    resp.set_cookie("cust", f"{cid}.{_sign(cid)}", httponly=True, samesite="lax",
                    secure=config.BASE_URL.startswith("https"), max_age=30 * 24 * 3600)
    return resp


@app.post("/wallet/logout")
def wallet_logout():
    resp = RedirectResponse("/wallet/login", status_code=303)
    resp.delete_cookie("cust")
    return resp


def render_wallet(request: Request, customer: dict, error: Optional[str] = None, amount: str = "",
                  status_code: int = 200, pending: Optional[dict] = None, method: str = "upi"):
    return templates.TemplateResponse(request, "wallet.html", {
        "customer": customer, "orders": service.customer_orders(customer["id"]), "error": error,
        "pending": pending, "form_method": method,
        "form_amount": amount, "bank_ok": service.bank_transfer_available(), "now_ts": int(time.time()),
    }, status_code=status_code)


@app.get("/wallet", response_class=HTMLResponse)
def wallet(request: Request):
    customer = current_customer(request)
    if not customer:
        return RedirectResponse("/wallet/login", status_code=303)
    return render_wallet(request, customer, request.query_params.get("error"))


@app.post("/wallet/add")
def wallet_add(request: Request, amount: str = Form(""), method: str = Form("upi"), replace: str = Form("")):
    customer = current_customer(request)
    if not customer:
        return RedirectResponse("/wallet/login", status_code=303)
    try:
        order = service.create_order(amount, customer_id=customer["id"], method=method, replace_pending=replace == "1")
    except service.PendingOrder as e:
        return render_wallet(request, customer, amount=amount, pending=e.order, method=method)
    except ServiceError as e:
        return render_wallet(request, customer, str(e), amount, status_code=400)
    return RedirectResponse(f"/pay/{order['id']}", status_code=303)


def _order_or_404(order_id: str) -> dict:
    order = service.get_order(order_id)
    if not order:
        raise HTTPException(404, "Order not found")
    return order


def _owner(request: Request, order: dict) -> Optional[int]:
    """The customer id that may act on this order (None for links made by an admin)."""
    if order["customer_id"] is None:
        return None
    customer = current_customer(request)
    if not customer or customer["id"] != order["customer_id"]:
        raise HTTPException(403, "Log in with the account that created this order.")
    return customer["id"]


@app.get("/pay/{order_id}", response_class=HTMLResponse)
def pay_page(request: Request, order_id: str):
    order = _order_or_404(order_id)
    view = service.customer_view(order)
    claim = order["claim"]
    reviewable = bool(claim and claim["state"] == "rejected" and (
        claim["problem"] in service.PROBLEMS_REVIEWABLE_NOW or
        (order["attempts"] >= 2 and claim["problem"] in service.PROBLEMS_REVIEWABLE_AFTER_2)))
    return templates.TemplateResponse(request, "pay.html", {
        "order": order,
        "view": view,
        "reviewable": reviewable,
        "merchant_name": config.MERCHANT_NAME,
        "customer": current_customer(request),
        "upi_query": upi_query(order),
        "server_now": int(time.time()),
        "error": request.query_params.get("error"),
        "demo_mode": config.DEMO_MODE,
        "demo_variants": demo_screenshot.VARIANTS if config.DEMO_MODE else {},
    })


@app.post("/pay/{order_id}/screenshot")
async def pay_screenshot(request: Request, order_id: str, file: UploadFile = File(...)):
    order = _order_or_404(order_id)
    owner = _owner(request, order)
    data = await file.read(config.MAX_UPLOAD_BYTES + 1)
    try:
        service.process_screenshot(order["id"], data, customer_id=owner)
    except ServiceError as e:
        return back(f"/pay/{order['id']}", str(e))
    return back(f"/pay/{order['id']}")


@app.post("/pay/{order_id}/pick")
def pay_pick(request: Request, order_id: str, claim_id: int = Form(...), utr: str = Form("")):
    order = _order_or_404(order_id)
    try:
        service.pick_utr(order["id"], claim_id, utr, customer_id=_owner(request, order))
    except ServiceError as e:
        return back(f"/pay/{order['id']}", str(e))
    return back(f"/pay/{order['id']}")


@app.post("/pay/{order_id}/proceed")
def pay_proceed(request: Request, order_id: str, claim_id: int = Form(...)):
    order = _order_or_404(order_id)
    try:
        service.submit_claim(order["id"], claim_id, customer_id=_owner(request, order))
    except ServiceError as e:
        return back(f"/pay/{order['id']}", str(e))
    return back(f"/pay/{order['id']}")


@app.post("/pay/{order_id}/review")
def pay_review(request: Request, order_id: str, claim_id: int = Form(...)):
    order = _order_or_404(order_id)
    try:
        service.request_review(order["id"], claim_id, customer_id=_owner(request, order))
    except ServiceError as e:
        return back(f"/pay/{order['id']}", str(e))
    return back(f"/pay/{order['id']}")


@app.post("/pay/{order_id}/cancel")
def pay_cancel(request: Request, order_id: str):
    order = _order_or_404(order_id)
    try:
        service.cancel_order(order["id"], customer_id=_owner(request, order))
    except ServiceError as e:
        return back(f"/pay/{order['id']}", str(e))
    return back(f"/pay/{order['id']}")


@app.get("/pay/{order_id}/demo-screenshot.png")
def pay_demo_screenshot(order_id: str, variant: str = "ok"):
    """Demo only: a sample success screen for this order."""
    if not config.DEMO_MODE:
        raise HTTPException(404)
    order = _order_or_404(order_id)
    png, _ = demo_screenshot.render(order, datetime.now(TZ).replace(tzinfo=None), variant)
    return Response(png, media_type="image/png",
                    headers={"Content-Disposition": f'attachment; filename="payment-{order["id"]}-{variant}.png"'})


@app.get("/qr/{order_id}.svg")
def order_qr(order_id: str):
    order = _order_or_404(order_id)
    # A standalone SVG (with xmlns) is needed for <img>; svg_inline() leaves it out.
    buf = io.BytesIO()
    segno.make(upi_link(order), error="m").save(buf, kind="svg", scale=6, border=2, xmldecl=False)
    return Response(buf.getvalue(), media_type="image/svg+xml", headers={"Cache-Control": "no-store"})


@app.get("/api/orders/{order_id}/status")
def order_status(order_id: str):
    return order_json(_order_or_404(order_id))


# ---------- phone app ----------

class NotificationIn(BaseModel):
    package: Optional[str] = None
    title: Optional[str] = None
    text: Optional[str] = None
    key: Optional[str] = None


class SmsIn(BaseModel):
    sender: str
    body: str
    key: Optional[str] = None


@app.post("/api/notify")
def notify(body: NotificationIn, member=Depends(device_member)):
    return service.ingest_notification(member["id"], body.package, body.title, body.text, body.key)


@app.post("/api/sms")
def sms(body: SmsIn, member=Depends(device_member)):
    return service.ingest_sms(member["id"], body.sender, body.body, body.key)


@app.post("/api/heartbeat")
def heartbeat(member=Depends(device_member)):
    service.touch_member(member["id"])
    return {"ok": True, "member": member["id"], "name": member["name"]}


# ---------- orders API (for a bot or another app) ----------

class OrderIn(BaseModel):
    amount: str
    member_id: Optional[str] = None
    note: Optional[str] = None


@app.post("/api/orders", dependencies=[Depends(api_admin)])
def create_order_api(body: OrderIn):
    order = service.create_order(body.amount, member_id=body.member_id, note=body.note)
    return order_json(service.get_order(order["id"]))


# ---------- admin dashboard ----------

@app.get("/")
def home():
    return RedirectResponse("/wallet")


@app.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/admin/login")
def login(request: Request, password: str = Form(...)):
    if not secrets.compare_digest(password, config.ADMIN_TOKEN):
        return templates.TemplateResponse(request, "login.html", {"error": "Wrong password"}, status_code=401)
    resp = RedirectResponse("/admin", status_code=303)
    resp.set_cookie("admin", _admin_session(), httponly=True, samesite="strict",
                    secure=config.BASE_URL.startswith("https"), max_age=30 * 24 * 3600)
    return resp


@app.post("/admin/logout")
def logout():
    resp = RedirectResponse("/admin/login", status_code=303)
    resp.delete_cookie("admin")
    return resp


def _day_start() -> int:
    return int(datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


def admin_page(request: Request, template: str, active: str, status_code: int = 200, **ctx):
    """Renders an admin page with what the sidebar needs (badge counts etc.)."""
    day_start = _day_start()
    dash = service.dashboard(day_start=day_start)
    return templates.TemplateResponse(request, template, {
        "active": active,
        "stats": dash["stats"],
        "nav": {
            "review": dash["stats"]["review_count"],
            "in_progress": dash["stats"]["in_progress_count"],
            "unmatched": dash["stats"]["unmatched_count"],
            "alerts": dash["stats"]["alert_count"],
            "ignored": len(dash["ignored"]),
            "online": dash["stats"]["online_count"],
            "members": dash["stats"]["member_count"],
        },
        "dash": dash,
        "day_start": day_start,
        "base_url": config.BASE_URL,
        "demo_mode": config.DEMO_MODE,
        "error": ctx.pop("error", None) or request.query_params.get("error"),
        "message": ctx.pop("message", None) or request.query_params.get("message"),
        # Only plain GET pages may reload themselves; a page rendered from a POST
        # (e.g. showing a new phone code once) must not.
        "auto_refresh": request.method == "GET",
        **ctx,
    }, status_code=status_code)


def need_login(request: Request):
    return None if is_admin(request) else RedirectResponse("/admin/login", status_code=303)


def done(url: str, message: Optional[str] = None, error: Optional[str] = None) -> RedirectResponse:
    if not url.startswith("/admin"):  # only our own pages, never another site
        url = "/admin"
    q = {k: v for k, v in (("message", message), ("error", error)) if v}
    return RedirectResponse(url + (f"?{urlencode(q)}" if q else ""), status_code=303)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if (r := need_login(request)):
        return r
    days = service.daily_totals(_day_start())
    return admin_page(request, "overview.html", "overview", days=days,
                      week_paise=sum(d["paise"] for d in days), week_count=sum(d["count"] for d in days),
                      day_max=max([d["paise"] for d in days] + [1]), **service.todo(_day_start()))


@app.get("/admin/review", response_class=HTMLResponse)
def admin_review(request: Request):
    if (r := need_login(request)):
        return r
    return admin_page(request, "review.html", "review", items=service.review_items(),
                      reject_reasons=service.REJECT_REASONS)


@app.post("/admin/review/{order_id}/approve")
def admin_review_approve(request: Request, order_id: str, utr: str = Form(""), amount: str = Form(""),
                         member_id: str = Form(""), next: str = Form("/admin/review")):
    if (r := need_login(request)):
        return r
    try:
        service.admin_approve(order_id, utr, amount, member_id=member_id or None)
    except ServiceError as e:
        return done(next, error=f"{order_id}: {e}")
    return done(next, message=f"Order {order_id.upper()} approved and credited")


@app.post("/admin/review/{order_id}/reject")
def admin_review_reject(request: Request, order_id: str, reason: str = Form(""),
                        next: str = Form("/admin/review")):
    if (r := need_login(request)):
        return r
    try:
        service.admin_reject(order_id, reason)
    except ServiceError as e:
        return done(next, error=f"{order_id}: {e}")
    return done(next, message=f"Order {order_id.upper()} rejected. The customer sees the reason.")


@app.post("/admin/review/{order_id}/ask")
def admin_review_ask(request: Request, order_id: str, message: str = Form(""),
                     next: str = Form("/admin/review")):
    if (r := need_login(request)):
        return r
    try:
        service.admin_ask(order_id, message)
    except ServiceError as e:
        return done(next, error=f"{order_id}: {e}")
    return done(next, message=f"Order {order_id.upper()} sent back to the customer")


@app.get("/admin/claims/{claim_id}/image")
def admin_claim_image(request: Request, claim_id: int):
    if not is_admin(request):
        raise HTTPException(401)
    path = service.claim_image_path(claim_id)
    if not path or not Path(path).exists():
        raise HTTPException(404, "Screenshot not kept any more")
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


@app.get("/admin/orders", response_class=HTMLResponse)
def admin_orders(request: Request, status: str = "all", q: str = ""):
    if (r := need_login(request)):
        return r
    listing = service.list_orders(status if status != "all" else None, q)
    return admin_page(request, "orders.html", "orders", status=status, q=q, **listing)


@app.get("/admin/payments", response_class=HTMLResponse)
def admin_payments(request: Request, tab: str = "unmatched"):
    if (r := need_login(request)):
        return r
    return admin_page(request, "payments.html", "payments", tab=tab, credits=service.list_bank_credits(),
                      signals=service.list_payments())


@app.post("/admin/credits/link")
def admin_link_credit(request: Request, utr: str = Form(...), amount: str = Form(...), order_id: str = Form("")):
    """An unmatched bank credit that an admin knows belongs to an order."""
    if (r := need_login(request)):
        return r
    try:
        service.admin_approve(order_id, utr, amount)
    except ServiceError as e:
        return done("/admin/payments", error=str(e))
    return done("/admin/payments", message=f"UTR {utr} credited to order {order_id.strip().upper()}")


@app.get("/admin/statements", response_class=HTMLResponse)
def admin_statements(request: Request):
    if (r := need_login(request)):
        return r
    return admin_page(request, "statements.html", "statements", statements=service.list_statements(),
                      accounts=service.statement_status(), now_local=datetime.now(TZ).strftime("%Y-%m-%dT%H:%M"))


@app.post("/admin/statements")
async def admin_upload_statement(request: Request, member_id: str = Form(...), downloaded_at: str = Form(""),
                                 file: UploadFile = File(...)):
    if (r := need_login(request)):
        return r
    data = await file.read(10 * 1024 * 1024)
    try:
        when = None
        if downloaded_at.strip():
            try:
                when = int(datetime.strptime(downloaded_at.strip(), "%Y-%m-%dT%H:%M").replace(tzinfo=TZ).timestamp())
            except ValueError:
                raise ServiceError("Enter when the statement was downloaded")
        rep = service.import_statement(member_id, file.filename or "statement.csv", data, downloaded_at=when)
    except ServiceError as e:
        return done("/admin/statements", error=str(e))
    return done("/admin/statements", message=(
        f"Statement checked: {len(rep['confirmed'])} confirmed, {len(rep['not_credited'])} not credited, "
        f"{len(rep['missing_in_bank']) + len(rep['wrong_amount'])} credited but not in bank, "
        f"{len(rep['claims_not_found'])} claims sent to review"))


@app.get("/admin/customers", response_class=HTMLResponse)
def admin_customers(request: Request):
    if (r := need_login(request)):
        return r
    return admin_page(request, "customers.html", "customers", customers=service.list_customers(), now_ts=int(time.time()))


@app.post("/admin/alerts/{alert_id}/resolve")
def admin_resolve_alert(request: Request, alert_id: int):
    if (r := need_login(request)):
        return r
    service.resolve_alert(alert_id)
    return done("/admin")


@app.get("/admin/team", response_class=HTMLResponse)
def admin_team(request: Request):
    if (r := need_login(request)):
        return r
    return admin_page(request, "team.html", "team")


@app.get("/admin/notifications", response_class=HTMLResponse)
def admin_notifications(request: Request, tab: str = "all"):
    if (r := need_login(request)):
        return r
    messages = service.list_phone_messages()
    counts = {g: sum(m["group"] == g for m in messages) for g in ("used", "unused", "blocked")}
    counts["all"] = len(messages)
    shown = messages if tab not in counts or tab == "all" else [m for m in messages if m["group"] == tab]
    return admin_page(request, "notifications.html", "notifications", messages=shown, counts=counts, tab=tab)


def render_new_order(request: Request, error: Optional[str] = None, form: Optional[dict] = None,
                     status_code: int = 200):
    members = [m for m in service.dashboard()["members"] if m["status"] != "closed"]
    return admin_page(request, "new_order.html", "new", status_code=status_code,
                      members=members, quick_amounts=service.recent_amounts(),
                      error=error, form=form or {})


@app.get("/admin/new", response_class=HTMLResponse)
def admin_new_order(request: Request):
    if (r := need_login(request)):
        return r
    return render_new_order(request)


@app.post("/admin/orders")
def admin_create_order(request: Request, member_id: str = Form(""), amount: str = Form(...),
                       note: str = Form("")):
    if (r := need_login(request)):
        return r
    try:
        order = service.create_order(amount, member_id=member_id or None, note=note)
    except ServiceError as e:
        form = {"member_id": member_id, "amount": amount, "note": note}
        return render_new_order(request, error=str(e), form=form, status_code=400)
    return RedirectResponse(f"/admin/orders/{order['id']}", status_code=303)


@app.get("/admin/orders/{order_id}", response_class=HTMLResponse)
def admin_order(request: Request, order_id: str):
    if (r := need_login(request)):
        return r
    order = _order_or_404(order_id)
    return admin_page(request, "order.html", "orders", order=order, view=service.customer_view(order),
                      events=service.order_events(order["id"]), reject_reasons=service.REJECT_REASONS,
                      reason=service.REVIEW_REASONS.get(order["review_reason"] or ""),
                      pay_url=f"{config.BASE_URL}/pay/{order['id']}")


@app.post("/admin/orders/{order_id}/simulate-sms")
def admin_simulate_sms(request: Request, order_id: str, amount: str = Form("")):
    """Demo only: pretends the account's phone got HDFC's credit SMS for this order."""
    if (r := need_login(request)):
        return r
    if not config.DEMO_MODE:
        raise HTTPException(404)
    try:
        paise = service.to_paise(amount) if amount.strip() else None
        service.demo_bank_sms(order_id, amount_paise=paise)
    except ServiceError as e:
        return done(f"/admin/orders/{order_id}", error=str(e))
    return done(f"/admin/orders/{order_id}", message="Demo HDFC SMS received")


@app.post("/admin/orders/{order_id}/simulate-alert")
def admin_simulate_alert(request: Request, order_id: str):
    """Demo only: pretends the account's phone got a PhonePe 'money received' alert."""
    if (r := need_login(request)):
        return r
    if not config.DEMO_MODE:
        raise HTTPException(404)
    service.demo_upi_alert(order_id)
    return done(f"/admin/orders/{order_id}", message="Demo UPI app alert received (it never credits money)")


@app.post("/admin/orders/{order_id}/cancel")
def admin_cancel_order(request: Request, order_id: str):
    if (r := need_login(request)):
        return r
    try:
        service.cancel_order(order_id)
    except ServiceError as e:
        return done(f"/admin/orders/{order_id}", error=str(e))
    return done(f"/admin/orders/{order_id}")


@app.post("/admin/members")
def admin_add_member(request: Request, member_id: str = Form(...), name: str = Form(...),
                     upi_id: str = Form(...), bank_account: str = Form(""), ifsc: str = Form("")):
    if (r := need_login(request)):
        return r
    try:
        token = service.add_member(member_id, name, upi_id, bank_account=bank_account, ifsc=ifsc)
    except ServiceError as e:
        return admin_page(request, "team.html", "team", error=str(e), status_code=400,
                          form={"member_id": member_id, "name": name, "upi_id": upi_id,
                                "bank_account": bank_account, "ifsc": ifsc})
    return admin_page(request, "team.html", "team", new_token={"member": member_id.strip().upper(), "token": token})


@app.post("/admin/members/{member_id}/reset")
def admin_reset_member(request: Request, member_id: str):
    if (r := need_login(request)):
        return r
    try:
        token = service.reset_member_token(member_id)
    except ServiceError as e:
        return admin_page(request, "team.html", "team", error=str(e), status_code=400)
    return admin_page(request, "team.html", "team", new_token={"member": member_id, "token": token})


@app.post("/admin/members/{member_id}/edit")
def admin_edit_member(request: Request, member_id: str, name: str = Form(""), bank_account: str = Form(""),
                      ifsc: str = Form("")):
    if (r := need_login(request)):
        return r
    try:
        service.update_member(member_id, name, bank_account, ifsc)
    except ServiceError as e:
        return done("/admin/team", error=f"{member_id}: {e}")
    return done("/admin/team", message=f"{member_id} updated")


@app.post("/admin/members/{member_id}/status")
def admin_member_status(request: Request, member_id: str, status: str = Form(...)):
    if (r := need_login(request)):
        return r
    try:
        msg = service.set_member_status(member_id, status)
    except ServiceError as e:
        return done("/admin/team", error=str(e))
    return done("/admin/team", message=msg)
