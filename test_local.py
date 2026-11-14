#!/usr/bin/env python3
"""End-to-end test for Atikur Pay (SMS-based TrxID flow).

Run the server first (fresh test DB):
    export FLASK_SECRET_KEY=test-secret ADMIN_SECRET=test-admin-secret \
      SMS_INGEST_SECRET=test-sms-secret \
      PLATFORM_BKASH_NUMBER=01700000000 \
      ATIKUR_PAY_DB=/tmp/atikur_pay_test.db
    rm -f /tmp/atikur_pay_test.db
    python3 app.py &
    python3 test_local.py

Tests: landing, signup, dashboard, bKash number save, API key,
create-payment API, checkout page, SMS ingest (+dedupe), TrxID
verify (wrong / correct / replay / already-used / amount mismatch /
sender mismatch), status API, admin.
"""
import re
import sys

import requests

BASE = "http://127.0.0.1:5000"
SMS_SECRET = "test-sms-secret"
ADMIN_SECRET = "test-admin-secret"

passed = []
failed = []


def check(name, cond, extra=""):
    if cond:
        passed.append(name)
        print(f"  PASS  {name}")
    else:
        failed.append(name)
        print(f"  FAIL  {name}  {extra}")


def main():
    s = requests.Session()

    print("== landing ==")
    r = s.get(BASE + "/")
    check("landing 200", r.status_code == 200, r.status_code)
    check("landing mentions Atikur Pay", "Atikur Pay" in r.text)
    check("landing mentions no API key", "No API key" in r.text)

    print("== signup ==")
    r = s.post(BASE + "/signup", data={
        "name": "Test Merchant", "email": "test@atikurpay.local",
        "phone": "01700000001", "password": "testpass123"},
        allow_redirects=True)
    check("signup -> dashboard", r.status_code == 200
          and "Dashboard" in r.text, r.status_code)

    print("== save bKash number ==")
    r = s.post(BASE + "/dashboard/methods", data={
        "method": "bkash", "number": "01711111111",
        "account_name": "Test Shop"}, allow_redirects=True)
    check("bkash number saved", r.status_code == 200
          and "01711111111" in r.text, r.status_code)

    print("== API key ==")
    r = s.post(BASE + "/dashboard/api-keys",
               data={"label": "Test website"}, allow_redirects=True)
    m = re.search(r"API key: <code>(ap_[A-Za-z0-9_\-]+)</code>", r.text)
    check("api key created + shown once", bool(m))
    api_key = m.group(1) if m else ""
    check("api key format", api_key.startswith("ap_"))

    print("== create payment via API ==")
    r = s.post(BASE + "/api/v1/create-payment",
               headers={"X-API-Key": api_key},
               json={"amount": 128.55, "currency": "BDT",
                     "merchantInvoiceNumber": "ORDER-1",
                     "customerRef": "customer@example.com"})
    check("create-payment 201", r.status_code == 201, r.status_code)
    data = r.json()
    pid = data.get("payment_id", "")
    check("payment_url returned", "payment_url" in data, data)
    check("payment status pending", data.get("status") == "pending", data)

    print("== checkout page ==")
    r = s.get(BASE + f"/pay/{pid}")
    check("checkout 200", r.status_code == 200, r.status_code)
    check("checkout shows merchant bkash number 01711111111",
          "01711111111" in r.text)
    check("checkout shows amount 128.55", "128.55" in r.text)
    check("checkout has TrxID field", "trx_id" in r.text)
    check("checkout has phone field", 'name="phone"' in r.text)

    print("== SMS ingest ==")
    sms_body = {
        "sender": "01712345678", "amount": 128.55,
        "trx_id": "9HX7K2M4P1",
        "sms_text": "You have received Tk 128.55 from 01712345678. "
                    "Ref: TrxID 9HX7K2M4P1. Fee Tk 0.00. Balance Tk 1234.56.",
        "method": "bkash",
    }
    r = s.post(BASE + "/api/sms",
               headers={"X-SMS-Secret": SMS_SECRET, "X-API-Key": api_key},
               json=sms_body)
    check("sms ingest 201 stored", r.status_code == 201
          and r.json().get("status") == "stored", f"{r.status_code} {r.text}")
    r = s.post(BASE + "/api/sms",
               headers={"X-SMS-Secret": SMS_SECRET, "X-API-Key": api_key},
               json=sms_body)
    check("sms ingest duplicate deduped", r.status_code == 200
          and r.json().get("status") == "duplicate", f"{r.status_code} {r.text}")
    r = s.post(BASE + "/api/sms",
               headers={"X-SMS-Secret": "wrong-secret", "X-API-Key": api_key},
               json=sms_body)
    check("sms ingest rejects bad secret", r.status_code == 401, r.status_code)
    r = s.post(BASE + "/api/sms",
               headers={"X-SMS-Secret": SMS_SECRET},
               json=dict(sms_body, trx_id="9HX7K2M4P2"))
    check("sms ingest without api key falls back on single merchant",
          r.status_code == 201
          and r.json().get("status") == "stored", f"{r.status_code} {r.text}")
    r = s.post(BASE + "/api/sms",
               headers={"X-SMS-Secret": SMS_SECRET, "X-API-Key": "bad-key"},
               json=dict(sms_body, trx_id="9HX7K2M4P3"))
    check("sms ingest rejects wrong api key", r.status_code == 401,
          r.status_code)

    def status_of(payment_id):
        r = s.get(BASE + "/api/v1/payment/status",
                  headers={"X-API-Key": api_key},
                  params={"payment_id": payment_id})
        return r.json().get("status")

    print("== TrxID verify: wrong TrxID ==")
    r = s.post(BASE + f"/pay/{pid}/verify",
               data={"method": "bkash", "phone": "01712345678",
                     "trx_id": "WRONG123"}, allow_redirects=True)
    check("wrong trxid -> not received yet", "not received yet" in r.text
          and status_of(pid) == "pending", status_of(pid))

    print("== TrxID verify: correct ==")
    r = s.post(BASE + f"/pay/{pid}/verify",
               data={"method": "bkash", "phone": "01712345678",
                     "trx_id": "9HX7K2M4P1"}, allow_redirects=True)
    check("correct trxid -> paid", "Payment successful" in r.text
          and status_of(pid) == "paid", status_of(pid))

    print("== TrxID verify: replay (idempotent) ==")
    r = s.post(BASE + f"/pay/{pid}/verify",
               data={"method": "bkash", "phone": "01712345678",
                     "trx_id": "9HX7K2M4P1"}, allow_redirects=True)
    check("replay same trxid -> still paid, no double",
          status_of(pid) == "paid", status_of(pid))

    print("== TrxID verify: already used by another payment ==")
    r = s.post(BASE + "/api/v1/create-payment",
               headers={"X-API-Key": api_key},
               json={"amount": 128.55, "currency": "BDT",
                     "merchantInvoiceNumber": "ORDER-2"})
    pid2 = r.json()["payment_id"]
    r = s.post(BASE + f"/pay/{pid2}/verify",
               data={"method": "bkash", "phone": "01712345678",
                     "trx_id": "9HX7K2M4P1"}, allow_redirects=True)
    check("same trxid on 2nd payment -> already used",
          "already used" in r.text and status_of(pid2) == "pending",
          status_of(pid2))

    print("== TrxID verify: amount mismatch ==")
    r = s.post(BASE + "/api/sms",
               headers={"X-SMS-Secret": SMS_SECRET, "X-API-Key": api_key},
               json={"sender": "01712345678", "amount": 500.00,
                     "trx_id": "AMT9999999",
                     "sms_text": "You have received Tk 500.00 from "
                                 "01712345678. Ref: TrxID AMT9999999.",
                     "method": "bkash"})
    check("sms for amount test stored", r.status_code == 201, r.text)
    r = s.post(BASE + "/api/v1/create-payment",
               headers={"X-API-Key": api_key},
               json={"amount": 100.00, "currency": "BDT",
                     "merchantInvoiceNumber": "ORDER-3"})
    pid3 = r.json()["payment_id"]
    r = s.post(BASE + f"/pay/{pid3}/verify",
               data={"method": "bkash", "phone": "01712345678",
                     "trx_id": "AMT9999999"}, allow_redirects=True)
    check("amount mismatch -> not paid",
          "does not match" in r.text and status_of(pid3) == "pending",
          status_of(pid3))

    print("== TrxID verify: sender mismatch ==")
    r = s.post(BASE + f"/pay/{pid3}/verify",
               data={"method": "bkash", "phone": "01999999999",
                     "trx_id": "AMT9999999"}, allow_redirects=True)
    check("sender mismatch -> not paid",
          ("does not match" in r.text or "phone number" in r.text)
          and status_of(pid3) == "pending", status_of(pid3))

    print("== status API ==")
    r = s.get(BASE + "/api/v1/payment/status",
              headers={"X-API-Key": api_key}, params={"payment_id": pid})
    check("status api paid + trxid", r.status_code == 200
          and r.json().get("status") == "paid"
          and r.json().get("trx_id") == "9HX7K2M4P1", r.text)
    r = s.get(BASE + "/api/v1/payment/status",
              headers={"X-API-Key": "bad-key"}, params={"payment_id": pid})
    check("status api rejects bad key", r.status_code == 401, r.status_code)

    print("== admin ==")
    s2 = requests.Session()
    r = s2.post(BASE + "/admin", data={"admin_secret": ADMIN_SECRET},
                allow_redirects=True)
    check("admin login + lists merchants/payments",
          r.status_code == 200 and "test@atikurpay.local" in r.text
          and pid in r.text, r.status_code)

    print()
    print(f"PASSED {len(passed)}  FAILED {len(failed)}")
    if failed:
        print("FAILED:", ", ".join(failed))
        sys.exit(1)
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    main()
