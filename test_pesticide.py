#!/usr/bin/env python3
"""Pesticide buy page + license webhook tests — LOCAL ONLY.

In-process Flask test client + a stub callback HTTP server. No live
server, no real gateway calls. Run from the repo dir:
    python3 test_pesticide.py

Tests: GET /buy/pesticide (valid plans, validation, merchant config,
price override, checkout render), webhook firing (exactly once per
paid PSC payment, correct body + secret, retries on 500 / pending
body, never for non-PSC payments, never breaks checkout, skipped
when unconfigured), _parse_psc_invoice, and the shared
_create_payment helper via the merchant API.
"""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB = "/tmp/atikur_pay_pesticide_test.db"
for f in (DB, DB + "-wal", DB + "-shm"):
    if os.path.exists(f):
        os.remove(f)

os.environ["FLASK_SECRET_KEY"] = "test-secret"
os.environ["ADMIN_SECRET"] = "test-admin-secret"
os.environ["SMS_INGEST_SECRET"] = "test-sms-secret"
os.environ["PLATFORM_BKASH_NUMBER"] = "01700000000"
os.environ["ATIKUR_PAY_DB"] = DB
os.environ.pop("DATABASE_URL", None)
os.environ.pop("PLAN_PRICES_BDT", None)
# urllib must reach the stub callback server directly, never via a proxy.
for _v in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
    os.environ.pop(_v, None)
os.environ["NO_PROXY"] = "127.0.0.1,localhost"

sys.path.insert(0, HERE)
import app as gw  # noqa: E402
import db  # noqa: E402

passed = []
failed = []


def check(name, cond, extra=""):
    if cond:
        passed.append(name)
        print("  PASS  %s" % name)
    else:
        failed.append(name)
        print("  FAIL  %s  %s" % (name, extra))


# ---------------------------------------------------------------- stub callback --
class StubState:
    def __init__(self):
        self.posts = []          # (headers dict, parsed json body)
        self.script = []         # [(status, body dict)] served in order
        self.lock = threading.Lock()


STUB = StubState()


class StubHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8"))
        except Exception:
            body = {"_unparsed": raw.decode("utf-8", "replace")}
        with STUB.lock:
            STUB.posts.append((dict(self.headers), body))
            if STUB.script:
                status, resp = STUB.script.pop(0)
            else:
                status, resp = 200, {"status": "paid"}
        payload = json.dumps(resp).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


stub_server = ThreadingHTTPServer(("127.0.0.1", 0), StubHandler)
stub_server.daemon_threads = True
threading.Thread(target=stub_server.serve_forever, daemon=True).start()
STUB_URL = "http://127.0.0.1:%d/v1/payment-callback" % stub_server.server_address[1]

MERCHANT_ID = "MERCH-PESTICIDE-1"
API_KEY = "ap_testpesticidekey"


def setup_merchant():
    conn, pg = db.get_conn()
    try:
        db.execute(conn, pg,
                   "INSERT INTO merchants (id, name, email, phone, "
                   "password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   (MERCHANT_ID, "Pesticide Shop", "pest@local",
                    "01700000001", "x", db.now_iso()))
        db.execute(conn, pg,
                   "INSERT INTO api_keys (id, merchant_id, label, "
                   "key_value, created_at, revoked) VALUES (?, ?, ?, ?, ?, 0)",
                   (db.new_id(), MERCHANT_ID, "test", API_KEY, db.now_iso()))
        db.execute(conn, pg,
                   "INSERT INTO merchant_methods (id, merchant_id, method, "
                   "credentials_json, is_active, created_at, updated_at) "
                   "VALUES (?, ?, 'bkash', ?, 1, ?, ?)",
                   (db.new_id(), MERCHANT_ID,
                    json.dumps({"number": "01711111111",
                                "account_name": "Pesticide Shop"}),
                    db.now_iso(), db.now_iso()))
        db.commit(conn)
    finally:
        conn.close()


def get_payment_row(pid):
    conn, pg = db.get_conn()
    try:
        return db.fetchone(conn, pg,
                           "SELECT * FROM payments WHERE public_id = ?",
                           (pid,))
    finally:
        conn.close()


def ingest_sms(client, sender, amount, trx_id):
    return client.post("/api/sms",
                       headers={"X-SMS-Secret": "test-sms-secret",
                                "X-API-Key": API_KEY},
                       json={"sender": sender, "amount": amount,
                             "trx_id": trx_id, "sms_text": "test",
                             "method": "bkash"})


def pay_checkout(client, pid, phone, trx_id):
    return client.post("/pay/%s/verify" % pid,
                       data={"method": "bkash", "phone": phone,
                             "trx_id": trx_id},
                       follow_redirects=False)


def main():
    setup_merchant()
    gw.PESTICIDE_MERCHANT_ID = MERCHANT_ID
    gw.PAYMENT_WEBHOOK_SECRET = "test-hook-secret"
    gw.LICENSE_CALLBACK_URL = STUB_URL
    client = gw.app.test_client()

    print("== buy endpoint: valid plans ==")
    r = client.get("/buy/pesticide?plan=monthly&ref=PUR-TESTREF00000001&device=DEV-001")
    check("monthly buy 302", r.status_code == 302, r.status_code)
    pid = r.location.rsplit("/pay/", 1)[-1]
    check("redirects to checkout", r.location.startswith("/pay/AP"), r.location)
    row = get_payment_row(pid)
    check("row: merchant + 250 + BDT + pending",
          row and row["merchant_id"] == MERCHANT_ID and row["amount"] == 250
          and row["currency"] == "BDT" and row["status"] == "pending", row)
    check("row: invoice PSC-monthly-ref",
          row and row["merchant_invoice_number"] == "PSC-monthly-PUR-TESTREF00000001", row)
    check("row: customer_ref is device",
          row and row["customer_ref"] == "DEV-001", row)

    r = client.get("/buy/pesticide?plan=yearly&ref=PUR-Y1&device=DEV-002")
    pid_y = r.location.rsplit("/pay/", 1)[-1]
    check("yearly amount 2500", get_payment_row(pid_y)["amount"] == 2500)
    r = client.get("/buy/pesticide?plan=lifetime&ref=PUR-L1&device=DEV-003")
    pid_l = r.location.rsplit("/pay/", 1)[-1]
    check("lifetime amount 12500", get_payment_row(pid_l)["amount"] == 12500)

    print("== buy endpoint: checkout renders ==")
    r = client.get("/pay/%s" % pid)
    check("checkout 200 + merchant bkash number + amount",
          r.status_code == 200 and "01711111111" in r.text
          and "250" in r.text, r.status_code)

    print("== checkout: platform bkash fallback ==")
    conn, pg = db.get_conn()
    try:
        db.execute(conn, pg,
                   "DELETE FROM merchant_methods WHERE merchant_id = ?",
                   (MERCHANT_ID,))
        db.commit(conn)
    finally:
        conn.close()
    r = client.get("/pay/%s" % pid)
    check("checkout shows platform number without saved method",
          r.status_code == 200 and "01700000000" in r.text, r.status_code)
    conn, pg = db.get_conn()
    try:
        db.execute(conn, pg,
                   "INSERT INTO merchant_methods (id, merchant_id, method, "
                   "credentials_json, is_active, created_at, updated_at) "
                   "VALUES (?, ?, 'bkash', ?, 1, ?, ?)",
                   (db.new_id(), MERCHANT_ID,
                    json.dumps({"number": "01711111111",
                                "account_name": "Pesticide Shop"}),
                    db.now_iso(), db.now_iso()))
        db.commit(conn)
    finally:
        conn.close()

    print("== buy endpoint: validation ==")
    check("bad plan 400", client.get(
        "/buy/pesticide?plan=weekly&ref=R1&device=D1").status_code == 400)
    check("missing ref 400", client.get(
        "/buy/pesticide?plan=monthly&device=D1").status_code == 400)
    check("missing device 400", client.get(
        "/buy/pesticide?plan=monthly&ref=R1").status_code == 400)
    check("ref too long 400", client.get(
        "/buy/pesticide?plan=monthly&ref=" + "R" * 65 + "&device=D1").status_code == 400)
    check("plan case-insensitive", client.get(
        "/buy/pesticide?plan=Monthly&ref=PUR-CASE1&device=D1").status_code == 302)

    print("== buy endpoint: merchant config ==")
    old_mid = gw.PESTICIDE_MERCHANT_ID
    gw.PESTICIDE_MERCHANT_ID = ""
    r = client.get("/buy/pesticide?plan=monthly&ref=R1&device=D1")
    check("unset merchant + single merchant falls back 302",
          r.status_code == 302, r.status_code)
    check("fallback bills the single merchant",
          get_payment_row(r.location.rsplit("/pay/", 1)[-1])["merchant_id"]
          == MERCHANT_ID)
    conn, pg = db.get_conn()
    try:
        db.execute(conn, pg,
                   "INSERT INTO merchants (id, name, email, phone, "
                   "password_hash, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                   ("MERCH-SECOND", "Second Shop", "second@local",
                    "01700000002", "x", db.now_iso()))
        db.commit(conn)
    finally:
        conn.close()
    check("unset merchant + two merchants 503", client.get(
        "/buy/pesticide?plan=monthly&ref=R1&device=D1").status_code == 503)
    conn, pg = db.get_conn()
    try:
        db.execute(conn, pg, "DELETE FROM merchants WHERE id = ?",
                   ("MERCH-SECOND",))
        db.commit(conn)
    finally:
        conn.close()
    gw.PESTICIDE_MERCHANT_ID = "MERCH-DOES-NOT-EXIST"
    check("nonexistent merchant 503", client.get(
        "/buy/pesticide?plan=monthly&ref=R1&device=D1").status_code == 503)
    gw.PESTICIDE_MERCHANT_ID = old_mid

    print("== buy endpoint: price override ==")
    os.environ["PLAN_PRICES_BDT"] = '{"monthly": 300}'
    r = client.get("/buy/pesticide?plan=monthly&ref=PUR-OVERRIDE1&device=D1")
    check("override monthly 300",
          get_payment_row(r.location.rsplit("/pay/", 1)[-1])["amount"] == 300)
    os.environ["PLAN_PRICES_BDT"] = "not-json{"
    r = client.get("/buy/pesticide?plan=monthly&ref=PUR-OVERRIDE2&device=D1")
    check("bad override falls back to 250",
          get_payment_row(r.location.rsplit("/pay/", 1)[-1])["amount"] == 250)
    os.environ.pop("PLAN_PRICES_BDT", None)

    print("== webhook: fires exactly once, correct body ==")
    with STUB.lock:
        STUB.posts = []
        STUB.script = []
    r = ingest_sms(client, "01712345678", 250, "TRXPSC00001")
    check("sms stored", r.status_code == 201, r.text)
    r = pay_checkout(client, pid, "01712345678", "TRXPSC00001")
    check("verify redirects to result", r.status_code == 302
          and r.location.endswith("/result"), r.location)
    check("payment marked paid", get_payment_row(pid)["status"] == "paid")
    with STUB.lock:
        posts = list(STUB.posts)
    check("exactly one webhook POST", len(posts) == 1, len(posts))
    if posts:
        headers, body = posts[0]
        check("webhook secret header",
              headers.get("X-Webhook-Secret") == "test-hook-secret", headers)
        check("webhook body fields",
              body.get("payment_id") == pid
              and body.get("purchase_ref") == "PUR-TESTREF00000001"
              and body.get("install_id") == "DEV-001"
              and body.get("plan") == "monthly"
              and body.get("amount") == 250
              and body.get("trx_id") == "TRXPSC00001", body)

    print("== webhook: replay does not re-fire ==")
    r = pay_checkout(client, pid, "01712345678", "TRXPSC00001")
    check("replay redirects to result", r.status_code == 302)
    with STUB.lock:
        check("still one POST after replay", len(STUB.posts) == 1,
              len(STUB.posts))

    print("== webhook: never for non-PSC payments ==")
    r = client.post("/api/v1/create-payment",
                    headers={"X-API-Key": API_KEY},
                    json={"amount": 100, "currency": "BDT",
                          "merchantInvoiceNumber": "ORDER-9",
                          "customerRef": "someone@example.com"})
    pid2 = r.get_json()["payment_id"]
    check("api create-payment still 201", r.status_code == 201, r.status_code)
    ingest_sms(client, "01712345678", 100, "TRXNONPSC01")
    pay_checkout(client, pid2, "01712345678", "TRXNONPSC01")
    check("non-PSC payment paid", get_payment_row(pid2)["status"] == "paid")
    with STUB.lock:
        check("no webhook for non-PSC payment", len(STUB.posts) == 1,
              len(STUB.posts))

    print("== webhook: retries on 500, then succeeds ==")
    with STUB.lock:
        STUB.posts = []
        STUB.script = [(500, {"error": "boom"}),
                       (500, {"error": "boom"}),
                       (200, {"status": "paid"})]
    r = client.get("/buy/pesticide?plan=monthly&ref=PUR-RETRY500&device=DEV-R1")
    pid_r = r.location.rsplit("/pay/", 1)[-1]
    ingest_sms(client, "01712345678", 250, "TRXRETRY500")
    r = pay_checkout(client, pid_r, "01712345678", "TRXRETRY500")
    check("checkout still succeeds despite webhook 500s",
          r.status_code == 302 and r.location.endswith("/result")
          and get_payment_row(pid_r)["status"] == "paid", r.status_code)
    with STUB.lock:
        check("3 attempts on 500,500,paid", len(STUB.posts) == 3,
              len(STUB.posts))

    print("== webhook: retries on 200-pending body ==")
    with STUB.lock:
        STUB.posts = []
        STUB.script = [(200, {"status": "pending"}),
                       (200, {"status": "already_paid"})]
    r = client.get("/buy/pesticide?plan=yearly&ref=PUR-RETRYPEND&device=DEV-R2")
    pid_p = r.location.rsplit("/pay/", 1)[-1]
    ingest_sms(client, "01712345678", 2500, "TRXRETRYPEND")
    pay_checkout(client, pid_p, "01712345678", "TRXRETRYPEND")
    with STUB.lock:
        check("2 attempts on pending,already_paid", len(STUB.posts) == 2,
              len(STUB.posts))

    print("== webhook: unreachable callback never breaks checkout ==")
    old_url = gw.LICENSE_CALLBACK_URL
    gw.LICENSE_CALLBACK_URL = "http://127.0.0.1:9/nope"
    r = client.get("/buy/pesticide?plan=monthly&ref=PUR-UNREACH&device=DEV-R3")
    pid_u = r.location.rsplit("/pay/", 1)[-1]
    ingest_sms(client, "01712345678", 250, "TRXUNREACH01")
    r = pay_checkout(client, pid_u, "01712345678", "TRXUNREACH01")
    check("checkout succeeds with dead callback URL",
          r.status_code == 302 and get_payment_row(pid_u)["status"] == "paid",
          r.status_code)
    gw.LICENSE_CALLBACK_URL = old_url

    print("== webhook: skipped when unconfigured ==")
    with STUB.lock:
        STUB.posts = []
    old_secret = gw.PAYMENT_WEBHOOK_SECRET
    gw.PAYMENT_WEBHOOK_SECRET = ""
    r = client.get("/buy/pesticide?plan=monthly&ref=PUR-NOSECRET&device=DEV-R4")
    pid_n = r.location.rsplit("/pay/", 1)[-1]
    ingest_sms(client, "01712345678", 250, "TRXNOSECRET")
    r = pay_checkout(client, pid_n, "01712345678", "TRXNOSECRET")
    check("checkout succeeds with no webhook secret",
          r.status_code == 302 and get_payment_row(pid_n)["status"] == "paid")
    with STUB.lock:
        check("no POST without secret", len(STUB.posts) == 0, len(STUB.posts))
    gw.PAYMENT_WEBHOOK_SECRET = old_secret

    print("== _parse_psc_invoice ==")
    check("valid invoice", gw._parse_psc_invoice("PSC-monthly-PUR-ABC") == ("monthly", "PUR-ABC"))
    check("ref with dashes kept whole",
          gw._parse_psc_invoice("PSC-yearly-PUR-A-B-C") == ("yearly", "PUR-A-B-C"))
    check("non-PSC None", gw._parse_psc_invoice("ORDER-1") is None)
    check("empty None", gw._parse_psc_invoice("") is None)
    check("None None", gw._parse_psc_invoice(None) is None)
    check("bad plan None", gw._parse_psc_invoice("PSC-weekly-R1") is None)
    check("empty ref None", gw._parse_psc_invoice("PSC-monthly-") is None)
    check("no ref part None", gw._parse_psc_invoice("PSC-monthly") is None)

    print("== shared _create_payment via merchant API ==")
    r = client.post("/api/v1/create-payment",
                    headers={"X-API-Key": API_KEY},
                    json={"amount": 55, "currency": "BDT",
                          "customerRef": "c@example.com"})
    check("create-payment default invoice INV-+pid",
          r.status_code == 201
          and get_payment_row(r.get_json()["payment_id"])["merchant_invoice_number"]
          == "INV-" + r.get_json()["payment_id"], r.text)

    print("== buy endpoint: no merchants at all ==")
    conn, pg = db.get_conn()
    try:
        db.execute(conn, pg, "DELETE FROM merchants")
        db.execute(conn, pg, "DELETE FROM api_keys")
        db.execute(conn, pg, "DELETE FROM merchant_methods")
        db.commit(conn)
    finally:
        conn.close()
    gw.PESTICIDE_MERCHANT_ID = ""
    check("unset merchant + zero merchants 503", client.get(
        "/buy/pesticide?plan=monthly&ref=R1&device=D1").status_code == 503)

    print()
    print("PASSED %d  FAILED %d" % (len(passed), len(failed)))
    stub_server.shutdown()
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
