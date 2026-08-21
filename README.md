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

## OCR (PaddleOCR CPU)

- Engine: `paddlepaddle==3.2.2` + `paddleocr` (no GPU, no EasyOCR/torch).
- **Before any paddle import** (also set in `app/ocr.py` and Docker):
  - `FLAGS_enable_pir_api=0`
  - `FLAGS_use_mkldnn=0`
- Lazy init: models load on **first** OCR call, not on `import app.main`.
- First run downloads models (needs network once). Cache under user home / Paddle cache.
- Invoice PDF: PyMuPDF render @ 200 DPI, max 20 pages, then OCR.
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
