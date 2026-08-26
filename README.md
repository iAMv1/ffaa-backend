# FFAA Backend

Free Accounting Automation — FastAPI backend.

## Requirements

- Windows 10/11 or Linux/macOS
- Python 3.12

## Setup

```powershell
# create venv
python -m venv .venv
.venv\Scripts\activate

# install
pip install -r requirements.txt

# env
cp .env.example .env
# edit .env with your SMTP credentials
```

## Run

```powershell
python -m uvicorn app.main:app --reload --port 8000
```

API docs: http://localhost:8000/docs

## Security posture (updated for P1 multi-tenant)

**Auth now exists.** fastapi-users backs `/api/v1/auth` (register / login /
logout / forgot-password / reset-password) with JWT-in-httpOnly-cookie
(`ffaaauth`, `samesite=lax`; no token storage in JS). Every tenant endpoint
requires a logged-in user and is scoped to that user's data (`Client.owner_id`;
404 — not 403 — on other tenants' resources). Sessions are revocable: each JWT
embeds `tv=users.token_version`, and any password rotate / password reset /
email change bumps the column, so every outstanding cookie (all devices) gets
401 on its next use. Rate limits via slowapi decorators on wrapper routes we
own (no monkeypatching of fastapi-users internals): register 5/h/IP,
login 10/h/IP, forgot-password 3/h/IP, reset-password 10/h/IP,
PATCH /auth/account 10/h per user.

Set `FFAA_SECRET` in production (JWT/reset-token signing); `FFAA_COOKIE_SECURE=true`
behind https. The operator account comes from `scripts/migrate_multi_tenant.py`
(email from `FFAA_OPERATOR_EMAIL`); the one-time random password is printed to
stdout once at creation and never written to disk — rotate it via
PATCH /api/v1/auth/account after first login.

The localhost posture is retained for dev: CORS pinned to the vite dev origins,
compose ports bound to `127.0.0.1`. Do not expose FFAA beyond localhost without
a reverse proxy; billing entitlement caps (free-plan limits) land in P4.

## Billing (P4 Razorpay)

- Plans: **Free** ₹0 (10 invoices/mo, 1 client) and **Pro** ₹499/mo (unlimited),
  seeded idempotently on startup into `billing_plans`.
- Entitlement: every write/compute endpoint (invoice upload, bank upload,
  client create + OCR auto-mint, reconcile, duplicate scan, reminder send)
  goes through `require_entitlement` — over-cap free usage gets `402` with an
  `{upgrade: true}` payload; reads stay free.
- Checkout: `POST /api/v1/billing/order` → Razorpay order → `POST .../verify`
  (hmac signature) extends the subscription 30 days. `POST .../webhook`
  verifies `payment.captured` over the RAW body (`x-razorpay-signature`)
  before parsing. Credits are idempotent on `razorpay_payment_id` UNIQUE —
  verify/webhook replays never double-extend.
- Keys come from env (`RAZORPAY_KEY_ID`, `RAZORPAY_KEY_SECRET`,
  `RAZORPAY_WEBHOOK_SECRET`, see `.env.example`). Without keys the order
  endpoint answers `503 payments not configured`.

## OCR (RapidOCR primary, PaddleOCR fallback)

Promoted architecture (see `app/ocr.py` docstring + `bench/PROGRESS.md` for measured numbers):

- **Born-digital PDFs**: PyMuPDF text layer directly — no OCR at all (fast path).
- **Images / scanned PDFs**: **RapidOCR** (ONNX INT8, CPU) on the RAW image — no binarization
  (it degrades INT8). Measured: kirana corpus 50/50 totals @ ~6.9 s avg vs paddle-only 0/50 @ 84 s.
- **Conditional fallback**: PaddleOCR v6-medium runs only when the rapid read is thin — empty
  result; <25 words and no total; a TOTAL label present but no total parsed; or GST rate > 0 with
  total ≈ taxable. Fallback runs on a CLAHE+Otsu binarized copy.
- Parser v2: Indian total rules (GRAND TOTAL → in-words marker → line-start TOTAL → SUBTOTAL),
  supplier/buyer disambiguation, zone-based line items from word coordinates.
- Invoice PDF render: PyMuPDF @ 200 DPI, max 20 pages.
- Paddle flags (`FLAGS_enable_pir_api=0`, `FLAGS_use_mkldnn=0`) set in `app/ocr.py`, Dockerfile,
  compose, `.env.example`; models lazy-init on first OCR call (first run downloads once).
- Probe: `python scripts/probe_paddle_ocr.py` (uses `tests/fixtures/sample_invoice.jpg`).

## Notes

- SQLite is created as `ffaa.db` on first run.
- Schema changes need `ffaa.db` delete or manual `ALTER TABLE` (no migrations yet).
- Client folders live under `FFAA_CLIENT_ROOT` (default `./data/clients`).

## Demo data

```powershell
python seed.py --force   # wipe + reseed: 3 clients, 14 invoices, 12 bank rows, dup flag, reminders
```

## Reminder scheduling

`app/reminder_job.py` emails clients who uploaded nothing in the last N days.
Body lists each client's missing document types (invoices / bank statements).
Every attempt is logged to reminder history.

```powershell
python -m app.reminder_job --days 30            # send
python -m app.reminder_job --days 30 --dry-run  # print only
```

Requires SMTP_* env vars. Windows Task Scheduler daily at 09:00:

```powershell
schtasks /create /tn "FFAA reminders" /sc daily /st 09:00 `
  /tr "C:\Users\ItzP\AppData\Local\Programs\Python\Python312\python.exe -m app.reminder_job --days 7" `
  /f
# set task "Start in" directory to ffaa-backend, or run via a .cmd that cd's there first
```
