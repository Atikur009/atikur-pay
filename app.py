#!/usr/bin/env python3
"""
Atikur Pay — payment gateway website (pay.atikur.cyou).

SMS-based TrxID verification (UdoktaPay manual-mode style).
NO bKash PGW API key is used — none is needed.

How it works:
1. A merchant signs up and adds their OWN bKash number (the number
   customers Send Money to). The platform default number comes from
   env var PLATFORM_BKASH_NUMBER.
2. The merchant's Android app (built separately) reads incoming bKash
   SMS on the merchant's phone and POSTs them to /api/sms. The server
   stores them in sms_transactions (deduped by TrxID).
3. A customer opens the checkout page, sends the exact amount via
   bKash Send Money to the shown number, then enters their phone
   number + TrxID. The server matches TrxID + amount + sender against
   sms_transactions and marks the payment paid — automatically.
4. Caveat: the merchant's phone (with the bKash SIM) must stay on
   with internet for verification to work.

Merchants also get API keys to create payments from their own site
or app (POST /api/v1/create-payment) and check status.

Pesticide Shop Calculator auto-buy (2026-11-14): the pesticide license
server runs on PythonAnywhere free, which CANNOT call this gateway
(allowlist). So the direction is reversed — the license server hands
the app a PUBLIC buy link, and this gateway webhooks the license
server when the payment is paid:
- GET /buy/pesticide?plan=<monthly|yearly|lifetime>&ref=<purchase_ref>
  &device=<install_id> — no API key; the merchant + prices are fixed
  server-side (PESTICIDE_MERCHANT_ID + the price table below). Creates
  the payment (invoice "PSC-<plan>-<ref>") and redirects to checkout.
- When a PSC payment is marked paid, the gateway POSTs
  /v1/payment-callback on the license server (LICENSE_CALLBACK_URL,
  header X-Webhook-Secret: PAYMENT_WEBHOOK_SECRET) with the purchase
  ref + amount. Up to 3 attempts; failures never break checkout.
  ONLY PSC-invoice payments are webhook'd — never other merchants'.

Env vars:
    FLASK_SECRET_KEY, ADMIN_SECRET, SMS_INGEST_SECRET,
    PLATFORM_BKASH_NUMBER, DATABASE_URL (Postgres) or ATIKUR_PAY_DB.
    PESTICIDE_MERCHANT_ID, PAYMENT_WEBHOOK_SECRET,
    LICENSE_CALLBACK_URL, PLAN_PRICES_BDT (pesticide auto-buy only).
"""
import json
import os
import secrets
import time
import urllib.error
import urllib.request
from functools import wraps

from flask import (Flask, abort, flash, jsonify, redirect, render_template,
                   request, session, url_for)
from werkzeug.security import check_password_hash, generate_password_hash

import db

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-only-change-me")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
SMS_INGEST_SECRET = os.environ.get("SMS_INGEST_SECRET", "")

# Pesticide Shop Calculator auto-buy: the merchant whose bKash number
# customers pay, and the license-server webhook. The price table MUST
# match the license server's copy — the callback rejects any payment
# whose amount differs. Override via PLAN_PRICES_BDT (JSON).
PESTICIDE_MERCHANT_ID = os.environ.get("PESTICIDE_MERCHANT_ID", "")
PAYMENT_WEBHOOK_SECRET = os.environ.get("PAYMENT_WEBHOOK_SECRET", "")
LICENSE_CALLBACK_URL = os.environ.get(
    "LICENSE_CALLBACK_URL",
    "https://atikur.pythonanywhere.com/v1/payment-callback")
DEFAULT_PLAN_PRICES_BDT = {"monthly": 250, "yearly": 2500, "lifetime": 12500}


def _plan_prices_bdt():
    """BDT price per pesticide plan: defaults, overridden per-plan by
    the PLAN_PRICES_BDT env JSON. A bad override never breaks the
    server — invalid entries fall back to the defaults."""
    prices = dict(DEFAULT_PLAN_PRICES_BDT)
    raw = os.environ.get("PLAN_PRICES_BDT")
    if raw:
        try:
            override = json.loads(raw)
        except ValueError:
            override = None
        if isinstance(override, dict):
            for plan, amount in override.items():
                if plan in prices and isinstance(amount, (int, float)) and amount >= 1:
                    prices[plan] = int(amount)
    return prices

# Payment methods shown at checkout. bKash is the only one that
# verifies payments in this build (via SMS); the rest are stored
# numbers whose processing is Phase 2.
MFS_METHODS = [
    ("bkash", "bKash Payment", "bKash"),
    ("nagad", "Nagad", "Nagad"),
    ("rocket", "Rocket", "Rocket"),
    ("upay", "Upay", "Upay"),
    ("mcash", "MCash", "MCash"),
    ("tap", "TAP", "TAP"),
    ("pathao", "Pathao Pay", "Pathao Pay"),
    ("cellfin", "Cellfin", "Cellfin"),
    ("islamic", "Islamic Wallet", "Islamic Wallet"),
    ("samadhan", "Samadhan", "Samadhan"),
]
PROCESSABLE = {"bkash"}


# ---------------- helpers ----------------
def current_merchant():
    mid = session.get("merchant_id")
    if not mid:
        return None
    conn, pg = db.get_conn()
    try:
        return db.fetchone(conn, pg,
                           "SELECT * FROM merchants WHERE id = ?", (mid,))
    finally:
        conn.close()


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("merchant_id"):
            flash("Please log in first.", "error")
            return redirect(url_for("login"))
        return fn(*args, **kwargs)
    return wrapper


def public_id():
    return "AP" + secrets.token_hex(8).upper()


def resolve_bkash_number(merchant_id):
    """The bKash number customers Send Money to for this merchant.

    Merchant's own saved number wins; otherwise the platform default
    from env var PLATFORM_BKASH_NUMBER. Returns "" when none set.
    """
    conn, pg = db.get_conn()
    try:
        row = db.fetchone(
            conn, pg,
            "SELECT credentials_json FROM merchant_methods "
            "WHERE merchant_id = ? AND method = 'bkash' AND is_active = 1",
            (merchant_id,))
    finally:
        conn.close()
    if row and row.get("credentials_json"):
        try:
            creds = json.loads(row["credentials_json"])
        except Exception:
            creds = {}
        num = db.normalize_phone(creds.get("number", ""))
        if num:
            return num
    return db.normalize_phone(os.environ.get("PLATFORM_BKASH_NUMBER", ""))


def get_payment(public_id_value):
    conn, pg = db.get_conn()
    try:
        return db.fetchone(conn, pg,
                           "SELECT * FROM payments WHERE public_id = ?",
                           (public_id_value,))
    finally:
        conn.close()


def _create_payment(merchant_id, amount, currency, invoice, customer_ref,
                    callback_url=None, pid=None):
    """Insert one pending payment row; return its public id. Shared by
    the merchant API, the dashboard test payment, and the pesticide
    buy page — all three create payments the same way."""
    pid = pid or public_id()
    conn, pg = db.get_conn()
    try:
        db.execute(
            conn, pg,
            "INSERT INTO payments (id, public_id, merchant_id, amount, "
            "currency, status, merchant_invoice_number, customer_ref, "
            "callback_url, created_at, updated_at) VALUES (?, ?, ?, ?, ?, "
            "'pending', ?, ?, ?, ?, ?)",
            (db.new_id(), pid, merchant_id, amount, currency,
             invoice, customer_ref, callback_url,
             db.now_iso(), db.now_iso()))
        db.commit(conn)
    finally:
        conn.close()
    return pid


def verify_trxid(payment, customer_phone, trx_id):
    """Match a customer-entered TrxID against stored bKash SMS.

    Idempotent per TrxID: a TrxID can only ever pay ONE payment.
    Returns one of: paid, already_paid, not_found, already_used,
    amount_mismatch, sender_mismatch.
    """
    trx = db.normalize_trx(trx_id)
    phone = db.normalize_phone(customer_phone)
    if not trx or not phone:
        return "not_found"
    conn, pg = db.get_conn()
    try:
        # Re-read the payment inside this connection (fresh status).
        p = db.fetchone(conn, pg, "SELECT * FROM payments WHERE id = ?",
                        (payment["id"],))
        if not p:
            return "not_found"
        if p["status"] == "paid":
            if (p.get("trx_id") or "") == trx:
                return "already_paid"
            return "already_paid"
        if p["status"] != "pending":
            return p["status"]
        sms = db.fetchone(
            conn, pg,
            "SELECT * FROM sms_transactions WHERE merchant_id = ? "
            "AND trx_id = ?", (p["merchant_id"], trx))
        if not sms:
            return "not_found"
        if sms.get("used"):
            if sms.get("used_by_payment_id") == p["id"]:
                return "already_paid"
            return "already_used"
        if abs(float(sms["amount"]) - float(p["amount"])) >= 0.01:
            return "amount_mismatch"
        if db.normalize_phone(sms.get("sender_phone", "")) != phone:
            return "sender_mismatch"
        # All checks pass — mark paid exactly once. The
        # status='pending' guard makes this atomic/idempotent.
        cur = conn.cursor()
        cur.execute(
            db.adapt("UPDATE payments SET status = 'paid', method = 'bkash', "
                     "trx_id = ?, customer_phone = ?, updated_at = ? "
                     "WHERE id = ? AND status = 'pending'", pg),
            (trx, phone, db.now_iso(), p["id"]))
        if cur.rowcount != 1:
            conn.rollback()
            cur.close()
            return "already_paid"
        cur.execute(
            db.adapt("UPDATE sms_transactions SET used = 1, "
                     "used_by_payment_id = ? WHERE trx_id = ? AND used = 0",
                     pg),
            (p["id"], trx))
        db.commit(conn)
        cur.close()
        return "paid"
    finally:
        conn.close()


# ---------------- pesticide auto-buy webhook ----------------
# Android intent URL that re-opens the Pesticide Shop Calculator app
# after a paid pesticide payment. Launched from the result page (auto
# + button). Falls back to atikur.cyou when the app is not installed.
PSC_APP_INTENT_URL = (
    "intent://#Intent;action=android.intent.action.MAIN;"
    "category=android.intent.category.LAUNCHER;"
    "package=com.atikur.pesticideshop;"
    "S.browser_fallback_url=https%3A%2F%2Fatikur.cyou;end")


def _parse_psc_invoice(invoice):
    """Parse a pesticide invoice "PSC-<plan>-<purchase_ref>" into
    (plan, purchase_ref). Returns None for any other invoice — the
    PSC- guard that keeps the webhook away from other merchants."""
    if not invoice or not str(invoice).startswith("PSC-"):
        return None
    parts = str(invoice).split("-", 2)
    if len(parts) != 3 or parts[1] not in DEFAULT_PLAN_PRICES_BDT:
        return None
    if not parts[2]:
        return None
    return parts[1], parts[2]


_WEBHOOK_WARNED = False


def _fire_license_webhook(payment, trx_id):
    """Notify the pesticide license server that a PSC payment is paid.

    Fires ONLY for PSC-invoice payments, up to 3 attempts (5s timeout,
    1s/2s backoff). A 2xx response with body status paid/already_paid/
    failed means "done" — anything else (network error, non-2xx, or a
    2xx "pending" body meaning "issuance in flight, try again") is
    retried. Failures are logged and NEVER raised: a failed webhook
    must not break the checkout response. If the webhook secret or the
    callback URL is unset, skip silently (warn once in the logs)."""
    global _WEBHOOK_WARNED
    parsed = _parse_psc_invoice(payment.get("merchant_invoice_number"))
    if not parsed:
        return
    plan, purchase_ref = parsed
    if not PAYMENT_WEBHOOK_SECRET or not LICENSE_CALLBACK_URL:
        if not _WEBHOOK_WARNED:
            _WEBHOOK_WARNED = True
            print("WARNING: pesticide webhook not configured "
                  "(PAYMENT_WEBHOOK_SECRET / LICENSE_CALLBACK_URL); "
                  "paid PSC payments will NOT auto-activate.", flush=True)
        return
    body = json.dumps({
        "payment_id": payment["public_id"],
        "purchase_ref": purchase_ref,
        "install_id": payment.get("customer_ref") or "",
        "plan": plan,
        "amount": payment["amount"],
        "trx_id": trx_id,
    }).encode("utf-8")
    last_error = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                LICENSE_CALLBACK_URL, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("X-Webhook-Secret", PAYMENT_WEBHOOK_SECRET)
            with urllib.request.urlopen(req, timeout=5) as resp:
                try:
                    data = json.loads(resp.read().decode("utf-8"))
                except Exception:
                    data = {}
                if data.get("status") in ("paid", "already_paid", "failed"):
                    return
                last_error = "unexpected callback body: %r" % (data,)
        except urllib.error.HTTPError as exc:
            last_error = "HTTP %s" % exc.code
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
        if attempt < 2:
            time.sleep(1 + attempt)
    print("WARNING: pesticide webhook failed for %s after 3 attempts "
          "(%s) — the purchase needs a manual key." % (
              payment["public_id"], last_error), flush=True)


# ---------------- public pages ----------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/docs")
def docs():
    return render_template("docs.html")


# ---------------- merchant auth ----------------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        if not name or not email or len(password) < 6:
            flash("Name, a valid email and a password of 6+ characters "
                  "are required.", "error")
            return render_template("signup.html")
        conn, pg = db.get_conn()
        try:
            existing = db.fetchone(
                conn, pg, "SELECT id FROM merchants WHERE email = ?",
                (email,))
            if existing:
                flash("An account with this email already exists. "
                      "Please log in.", "error")
                return render_template("signup.html")
            mid = db.new_id()
            db.execute(
                conn, pg,
                "INSERT INTO merchants (id, name, email, phone, "
                "password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (mid, name, email, phone,
                 generate_password_hash(password), db.now_iso()))
            db.commit(conn)
        finally:
            conn.close()
        session["merchant_id"] = mid
        flash("Welcome to Atikur Pay! Your merchant account is ready.",
              "success")
        return redirect(url_for("dashboard"))
    return render_template("signup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        conn, pg = db.get_conn()
        try:
            m = db.fetchone(conn, pg,
                            "SELECT * FROM merchants WHERE email = ?",
                            (email,))
        finally:
            conn.close()
        if m and check_password_hash(m["password_hash"], password):
            session["merchant_id"] = m["id"]
            return redirect(url_for("dashboard"))
        flash("Wrong email or password.", "error")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.pop("merchant_id", None)
    flash("Logged out.", "success")
    return redirect(url_for("index"))


# ---------------- merchant dashboard ----------------
@app.route("/dashboard")
@login_required
def dashboard():
    m = current_merchant()
    conn, pg = db.get_conn()
    try:
        keys = db.fetchall(
            conn, pg,
            "SELECT * FROM api_keys WHERE merchant_id = ? "
            "ORDER BY created_at DESC", (m["id"],))
        methods = db.fetchall(
            conn, pg,
            "SELECT * FROM merchant_methods WHERE merchant_id = ?",
            (m["id"],))
        payments = db.fetchall(
            conn, pg,
            "SELECT * FROM payments WHERE merchant_id = ? "
            "ORDER BY created_at DESC LIMIT 50", (m["id"],))
        stats = db.fetchone(
            conn, pg,
            "SELECT COUNT(*) AS total, "
            "COALESCE(SUM(CASE WHEN status='paid' THEN amount ELSE 0 END), 0) "
            "AS paid_volume, "
            "COALESCE(SUM(CASE WHEN status='paid' THEN 1 ELSE 0 END), 0) "
            "AS paid_count FROM payments WHERE merchant_id = ?",
            (m["id"],))
        sms_count = db.fetchone(
            conn, pg,
            "SELECT COUNT(*) AS c FROM sms_transactions "
            "WHERE merchant_id = ?", (m["id"],))
    finally:
        conn.close()
    method_map = {}
    for r in methods:
        try:
            creds = json.loads(r.get("credentials_json") or "{}")
        except Exception:
            creds = {}
        r["creds"] = creds
        method_map[r["method"]] = r
    new_secret = session.pop("new_api_secret", None)
    new_key = session.pop("new_api_key", None)
    return render_template(
        "dashboard.html", merchant=m, keys=keys, methods=method_map,
        payments=payments, stats=stats, mfs_methods=MFS_METHODS,
        new_api_key=new_key, new_api_secret=new_secret,
        sms_count=sms_count["c"],
        platform_bkash=db.normalize_phone(
            os.environ.get("PLATFORM_BKASH_NUMBER", "")))


@app.route("/dashboard/api-keys", methods=["POST"])
@login_required
def create_api_key():
    m = current_merchant()
    label = request.form.get("label", "Default").strip() or "Default"
    key_value = "ap_" + secrets.token_urlsafe(24)
    secret = secrets.token_urlsafe(32)
    conn, pg = db.get_conn()
    try:
        db.execute(
            conn, pg,
            "INSERT INTO api_keys (id, merchant_id, label, key_value, "
            "secret, created_at, revoked) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (db.new_id(), m["id"], label, key_value, secret, db.now_iso()))
        db.commit(conn)
    finally:
        conn.close()
    # Shown once only.
    session["new_api_key"] = key_value
    session["new_api_secret"] = secret
    flash("API key created. Copy it now — the secret is shown only once.",
          "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard/api-keys/<key_id>/revoke", methods=["POST"])
@login_required
def revoke_api_key(key_id):
    m = current_merchant()
    conn, pg = db.get_conn()
    try:
        db.execute(conn, pg,
                   "UPDATE api_keys SET revoked = 1 WHERE id = ? "
                   "AND merchant_id = ?", (key_id, m["id"]))
        db.commit(conn)
    finally:
        conn.close()
    flash("API key revoked.", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard/methods", methods=["POST"])
@login_required
def save_method():
    m = current_merchant()
    method = request.form.get("method", "").strip()
    if method not in [x[0] for x in MFS_METHODS]:
        flash("Unknown payment method.", "error")
        return redirect(url_for("dashboard"))
    number = db.normalize_phone(request.form.get("number", ""))
    name = request.form.get("account_name", "").strip()
    if not number:
        flash("Enter the number customers should send money to.", "error")
        return redirect(url_for("dashboard"))
    creds = {"number": number, "account_name": name}
    conn, pg = db.get_conn()
    try:
        existing = db.fetchone(
            conn, pg,
            "SELECT id FROM merchant_methods WHERE merchant_id = ? "
            "AND method = ?", (m["id"], method))
        now = db.now_iso()
        if existing:
            db.execute(
                conn, pg,
                "UPDATE merchant_methods SET credentials_json = ?, "
                "is_active = 1, updated_at = ? WHERE id = ?",
                (json.dumps(creds), now, existing["id"]))
        else:
            db.execute(
                conn, pg,
                "INSERT INTO merchant_methods (id, merchant_id, method, "
                "credentials_json, is_active, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)",
                (db.new_id(), m["id"], method, json.dumps(creds), now, now))
        db.commit(conn)
    finally:
        conn.close()
    if method == "bkash":
        flash("bKash number saved. Customers will now send money to this "
              "number and verify with their TrxID.", "success")
    else:
        flash(f"{method} number saved. Processing for {method} is "
              "coming soon — the number is stored and will be used "
              "when processing goes live.", "success")
    return redirect(url_for("dashboard"))


@app.route("/dashboard/test-payment", methods=["POST"])
@login_required
def test_payment():
    m = current_merchant()
    try:
        amount = float(request.form.get("amount", "0"))
    except ValueError:
        amount = 0
    if amount < 10:
        flash("Test payment amount must be at least 10 BDT.", "error")
        return redirect(url_for("dashboard"))
    pid = public_id()
    _create_payment(m["id"], amount, "BDT", "INV-" + pid,
                    "test-payment", pid=pid)
    return redirect(url_for("checkout", public_id=pid))


def _single_merchant_id():
    """The id of the gateway's only merchant, or "" unless exactly one
    merchant exists. Used by the pesticide buy page and by SMS ingest
    when no API key is supplied: with a single merchant there is no
    one else the request could belong to."""
    conn, pg = db.get_conn()
    try:
        rows = db.fetchall(conn, pg, "SELECT id FROM merchants LIMIT 2")
    finally:
        conn.close()
    if len(rows) == 1:
        return rows[0]["id"]
    return ""


def _resolve_pesticide_merchant_id():
    """The merchant id the pesticide buy page bills.

    PESTICIDE_MERCHANT_ID wins when set. Otherwise, when this gateway
    has exactly ONE merchant, that merchant is used: this gateway was
    built for Atikur's own apps, and a single-merchant install has no
    one else the buy page could bill. With zero or several merchants
    the fallback refuses (returns "") and the env var is required, so
    a multi-merchant gateway can never bill the wrong merchant.
    """
    if PESTICIDE_MERCHANT_ID:
        return PESTICIDE_MERCHANT_ID
    return _single_merchant_id()


# ---------------- pesticide buy page (public) ----------------
@app.route("/buy/pesticide")
def buy_pesticide():
    """Public checkout link for the Pesticide Shop Calculator app. No
    API key — the endpoint is safe because the merchant and the prices
    are fixed server-side: only the three plans at the fixed BDT
    prices, only for the pesticide merchant. Creates the payment and
    redirects to the normal checkout page, where the shopkeeper pays
    via bKash + TrxID as usual."""
    plan = str(request.args.get("plan", "")).strip().lower()
    ref = str(request.args.get("ref", "")).strip()
    device = str(request.args.get("device", "")).strip()
    if plan not in DEFAULT_PLAN_PRICES_BDT:
        return jsonify({"error": "plan must be monthly, yearly or "
                                 "lifetime."}), 400
    if not ref or len(ref) > 64 or not device or len(device) > 64:
        return jsonify({"error": "ref and device are required."}), 400
    merchant_id = _resolve_pesticide_merchant_id()
    if not merchant_id:
        return jsonify({"error": "Pesticide payments are not configured "
                                 "on this server."}), 503
    conn, pg = db.get_conn()
    try:
        merchant = db.fetchone(conn, pg,
                               "SELECT id FROM merchants WHERE id = ?",
                               (merchant_id,))
    finally:
        conn.close()
    if not merchant:
        # A mistyped merchant id must fail loud here, not strand the
        # shopkeeper at a checkout with no bKash number.
        return jsonify({"error": "Pesticide payments are not configured "
                                 "on this server."}), 503
    amount = _plan_prices_bdt()[plan]
    pid = _create_payment(merchant_id, amount, "BDT",
                          "PSC-%s-%s" % (plan, ref), device)
    return redirect(url_for("checkout", public_id=pid))


# ---------------- checkout (public) ----------------
@app.route("/pay/<public_id>")
def checkout(public_id):
    payment = get_payment(public_id)
    if not payment:
        abort(404)
    conn, pg = db.get_conn()
    try:
        merchant = db.fetchone(conn, pg,
                               "SELECT name FROM merchants WHERE id = ?",
                               (payment["merchant_id"],))
        method_rows = db.fetchall(
            conn, pg,
            "SELECT method FROM merchant_methods "
            "WHERE merchant_id = ? AND is_active = 1",
            (payment["merchant_id"],))
    finally:
        conn.close()
    configured = {r["method"] for r in method_rows}
    bkash_number = resolve_bkash_number(payment["merchant_id"])
    return render_template(
        "checkout.html", payment=payment,
        merchant_name=merchant["name"] if merchant else "Merchant",
        mfs_methods=MFS_METHODS, configured=configured,
        bkash_number=bkash_number, hide_chrome=True)


@app.route("/pay/<public_id>/verify", methods=["POST"])
def checkout_verify(public_id):
    payment = get_payment(public_id)
    if not payment:
        abort(404)
    if payment["status"] != "pending":
        return redirect(url_for("checkout_result", public_id=public_id))
    method = request.form.get("method", "")
    if method not in PROCESSABLE:
        flash("This payment method is not available yet.", "error")
        return redirect(url_for("checkout", public_id=public_id))
    phone = request.form.get("phone", "")
    trx_id = request.form.get("trx_id", "")
    result = verify_trxid(payment, phone, trx_id)
    if result == "paid":
        # Freshly marked paid (this call flipped it) — notify the
        # license server for PSC payments. Never breaks checkout: the
        # webhook swallows its own failures.
        _fire_license_webhook(get_payment(public_id) or payment,
                              db.normalize_trx(trx_id))
        return redirect(url_for("checkout_result", public_id=public_id))
    if result == "already_paid":
        return redirect(url_for("checkout_result", public_id=public_id))
    if result == "not_found":
        flash("Payment not received yet. Check the TrxID and your phone "
              "number, and try again — the bKash SMS can take a moment "
              "to arrive.", "error")
    elif result == "already_used":
        flash("This TrxID was already used for another payment.", "error")
    elif result == "amount_mismatch":
        flash("The TrxID amount does not match this payment amount. "
              "Please send the exact amount.", "error")
    elif result == "sender_mismatch":
        flash("The phone number does not match the sender of this TrxID.",
              "error")
    else:
        flash("Could not verify this payment. Please try again.", "error")
    return redirect(url_for("checkout", public_id=public_id))


@app.route("/pay/<public_id>/result")
def checkout_result(public_id):
    payment = get_payment(public_id)
    if not payment:
        abort(404)
    invoice = payment.get("merchant_invoice_number") or ""
    is_pesticide = invoice.startswith("PSC-")
    return render_template(
        "result.html", payment=payment, hide_chrome=True,
        is_pesticide=is_pesticide, app_intent_url=PSC_APP_INTENT_URL)


# ---------------- SMS ingest (Android app) ----------------
@app.route("/api/sms", methods=["POST"])
def sms_ingest():
    if not SMS_INGEST_SECRET:
        return jsonify({"error": "SMS ingest is not configured on this "
                                 "server."}), 503
    if request.headers.get("X-SMS-Secret", "") != SMS_INGEST_SECRET:
        return jsonify({"error": "Invalid SMS ingest secret."}), 401
    key = request.headers.get("X-API-Key", "")
    if key:
        conn, pg = db.get_conn()
        try:
            key_row = db.fetchone(
                conn, pg,
                "SELECT merchant_id FROM api_keys WHERE key_value = ? "
                "AND revoked = 0", (key,))
        finally:
            conn.close()
        if not key_row:
            return jsonify({"error": "Invalid or revoked API key."}), 401
        merchant_id = key_row["merchant_id"]
    else:
        # No API key: only acceptable on a single-merchant gateway,
        # where the SMS can only belong to that one merchant. The SMS
        # secret (checked above) is still required.
        merchant_id = _single_merchant_id()
        if not merchant_id:
            return jsonify({"error": "Missing merchant API key."}), 401
    data = request.get_json(silent=True) or {}
    sender = db.normalize_phone(data.get("sender", ""))
    trx_id = db.normalize_trx(data.get("trx_id", ""))
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    method = str(data.get("method", "bkash")).lower()
    if not sender or not trx_id or amount <= 0:
        return jsonify({"error": "sender, amount and trx_id are required."}), 400
    conn, pg = db.get_conn()
    try:
        existing = db.fetchone(
            conn, pg,
            "SELECT id FROM sms_transactions WHERE trx_id = ?", (trx_id,))
        if existing:
            return jsonify({"status": "duplicate", "trx_id": trx_id}), 200
        db.execute(
            conn, pg,
            "INSERT INTO sms_transactions (id, merchant_id, method, "
            "sender_phone, amount, trx_id, sms_text, received_at, used, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
            (db.new_id(), merchant_id, method, sender, amount,
             trx_id, str(data.get("sms_text", "")),
             str(data.get("received_at", "")), db.now_iso()))
        db.commit(conn)
    finally:
        conn.close()
    return jsonify({"status": "stored", "trx_id": trx_id}), 201


# ---------------- merchant API ----------------
def api_auth():
    key = request.headers.get("X-API-Key", "")
    if not key:
        return None
    conn, pg = db.get_conn()
    try:
        row = db.fetchone(
            conn, pg,
            "SELECT * FROM api_keys WHERE key_value = ? AND revoked = 0",
            (key,))
        if row:
            db.execute(conn, pg,
                       "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                       (db.now_iso(), row["id"]))
            db.commit(conn)
        return row
    finally:
        conn.close()


@app.route("/api/v1/create-payment", methods=["POST"])
def api_create_payment():
    key = api_auth()
    if not key:
        return jsonify({"error": "Invalid or revoked API key."}), 401
    data = request.get_json(silent=True) or {}
    try:
        amount = float(data.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount < 1:
        return jsonify({"error": "amount must be at least 1 BDT."}), 400
    currency = str(data.get("currency", "BDT")).upper()
    if currency != "BDT":
        return jsonify({"error": "Only BDT is supported."}), 400
    invoice = str(data.get("merchantInvoiceNumber") or "")
    pid = public_id()
    if not invoice:
        invoice = "INV-" + pid
    _create_payment(key["merchant_id"], amount, currency, invoice,
                    str(data.get("customerRef") or ""),
                    str(data.get("callbackURL") or ""), pid=pid)
    return jsonify({
        "payment_id": pid,
        "payment_url": url_for("checkout", public_id=pid, _external=True),
        "amount": amount,
        "currency": currency,
        "status": "pending",
    }), 201


@app.route("/api/v1/payment/status")
def api_payment_status():
    key = api_auth()
    if not key:
        return jsonify({"error": "Invalid or revoked API key."}), 401
    pid = request.args.get("payment_id", "")
    conn, pg = db.get_conn()
    try:
        row = db.fetchone(
            conn, pg,
            "SELECT * FROM payments WHERE public_id = ? AND merchant_id = ?",
            (pid, key["merchant_id"]))
    finally:
        conn.close()
    if not row:
        return jsonify({"error": "Payment not found."}), 404
    return jsonify({
        "payment_id": row["public_id"],
        "status": row["status"],
        "amount": row["amount"],
        "currency": row["currency"],
        "method": row.get("method"),
        "trx_id": row.get("trx_id"),
        "merchant_invoice_number": row.get("merchant_invoice_number"),
    })


# ---------------- admin (owner) ----------------
@app.route("/admin", methods=["GET", "POST"])
def admin():
    if not ADMIN_SECRET:
        return render_template("admin.html", disabled=True,
                               merchants=[], payments=[])
    if not session.get("is_admin"):
        if request.method == "POST":
            import hmac
            if hmac.compare_digest(request.form.get("admin_secret", ""),
                                   ADMIN_SECRET):
                session["is_admin"] = True
                return redirect(url_for("admin"))
            flash("Wrong admin secret.", "error")
        return render_template("admin.html", disabled=False,
                               merchants=None, payments=None)
    conn, pg = db.get_conn()
    try:
        merchants = db.fetchall(
            conn, pg,
            "SELECT id, name, email, phone, created_at FROM merchants "
            "ORDER BY created_at DESC")
        payments = db.fetchall(
            conn, pg,
            "SELECT * FROM payments ORDER BY created_at DESC LIMIT 100")
    finally:
        conn.close()
    return render_template("admin.html", disabled=False,
                           merchants=merchants, payments=payments)


# ---------------- startup ----------------
db.init_schema()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")),
            debug=True)
