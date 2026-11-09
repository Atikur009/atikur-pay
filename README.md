# Atikur Pay — payment gateway (pay.atikur.cyou)

A payment gateway website for Bangladeshi businesses, like Invoiy /
UdoktaPay. Merchants sign up, add their OWN payment numbers, get API
keys, and accept payments on their website or app.

## How payments work — SMS-based TrxID verification (NO bKash API key)

This gateway does **not** use the bKash PGW API — no API key and no
merchant account are needed.

1. A merchant signs up and adds their bKash number (the number
   customers Send Money to).
2. An Android app on the merchant's phone reads incoming bKash SMS
   (amount + sender + TrxID) and POSTs them to `POST /api/sms`.
3. A customer opens the checkout page, sends the exact amount via
   bKash Send Money to the shown number, then enters their phone
   number + TrxID.
4. The server matches TrxID + amount + sender against the stored SMS
   and marks the payment **paid** automatically. A TrxID can only
   ever pay one payment (idempotent per TrxID).
5. Caveat: the merchant's phone (with the bKash SIM) must stay on
   with internet for verification to work.

## Run locally

```bash
cd atikur-pay
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
export FLASK_SECRET_KEY="change-me"
export ADMIN_SECRET="change-me"
export SMS_INGEST_SECRET="change-me"
export PLATFORM_BKASH_NUMBER="01XXXXXXXXX"   # platform default number
python3 app.py   # http://127.0.0.1:5000
```

Postgres (production): set `DATABASE_URL`. SQLite is used otherwise.

## Merchant API

- `POST /api/v1/create-payment` — header `X-API-Key`, body
  `{amount, currency: "BDT", merchantInvoiceNumber, customerRef}`.
  Returns `{payment_id, payment_url, amount, currency, status}`.
- `GET /api/v1/payment/status?payment_id=...` — header `X-API-Key`.

## SMS ingest (Android app)

- `POST /api/sms` — headers `X-SMS-Secret: <SMS_INGEST_SECRET>` and
  `X-API-Key: <merchant API key>`. Body:
  `{sender, amount, trx_id, sms_text, method: "bkash"}`.
- Duplicate TrxIDs return `{"status": "duplicate"}` (idempotent).

## Admin

`GET /admin` — protected by `ADMIN_SECRET` env var. Lists all
merchants and payments.

## Test

```bash
export FLASK_SECRET_KEY=test-secret ADMIN_SECRET=test-admin \
  SMS_INGEST_SECRET=test-sms-secret \
  PLATFORM_BKASH_NUMBER=01700000000 \
  ATIKUR_PAY_DB=/tmp/atikur_pay_test.db
python3 app.py &
python3 test_local.py
```

The test drives signup → dashboard → bKash number → API key →
create-payment → checkout → SMS ingest (sample SMS) → TrxID verify
→ paid (idempotent) → status API → admin. See `test_local.py`.

## Still needed for live use

- The Android SMS-forwarder app (separate build) on the merchant's
  phone, pointed at `/api/sms` with the SMS ingest secret + the
  merchant's API key.
- Each merchant's own bKash number set in the dashboard (or the
  platform default `PLATFORM_BKASH_NUMBER`).
- A domain + deploy (Render blueprint in `render.yaml`; set the env
  vars there). No bKash API key is ever needed for this flow.

## Security notes (MVP)

- Passwords are hashed (werkzeug). API keys are random tokens.
- Per-merchant bKash numbers are stored as-is; encrypt sensitive
  fields at rest before production use with real merchants.
- `SMS_INGEST_SECRET` must be a strong random value in production.
