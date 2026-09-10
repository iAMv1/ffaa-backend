# Model Licenses — OCR / Doc-AI Weights in FFAA

> Required by the OpenRAIL-M licenses themselves (attribution §4a + downstream
> notice §7). Read this before touching the OCR stack. Sources verified 2026-09;
> full analysis in `.scratch/oss-survey/SURYA-WEIGHTS.md` + `CHANDRA-PLAN.md`.

## Deployed hot path — Apache-2.0 (commercial-SaaS-safe)

| Model | License | Role |
|---|---|---|
| RapidOCR ONNX (PP-OCRv4 INT8, © Baidu/PaddleOCR) | Apache-2.0 | Primary OCR (`app/ocr.py`) |
| PaddleOCR 3.7 (PP-OCRv4 family) | Apache-2.0 | Accuracy fallback (`app/ocr.py`) |
| RapidFuzz | MIT | reconcile/duplicate scoring |

## Escalation ladder (`app/ocr_escalate.py`)

| Backend | License posture | Status |
|---|---|---|
| **Datalab cloud** (Extraction / OCR) | Plain commercial API — usage-based, no RAIL entanglement | ✅ supported (`FFAA_OCR_ESCALATION=datalab` + `DATALAB_API_KEY`) |
| **Surya open weights** (`surya-ocr-2`, `surya_layout2`) | Code Apache-2.0; **weights modified OpenRAIL-M** | ⬜ future prospect (`FFAA_OCR_ESCALATION=surya` fails cleanly until provisioned) |
| **Chandra OCR 2 open weights** (`chandra-ocr-2`) | Code Apache-2.0; **weights modified OpenRAIL-M — $2M cap (TIGHTER than Surya's $5M)** | ⬜ future prospect (same adapter slot) |

## OpenRAIL-M obligations IF/WHEN local weights ship

FFAA runs on the sub-threshold allowance (free for research/personal/startups under
the cap **and** not competing with Datalab's platform). Shipping local weights then
requires ALL of:

1. **Pass-through restrictions** — customers must receive the use restrictions.
2. **License copy** — ship `MODEL_LICENSE` text with the product.
3. **Attribution** — outputs must credit Datalab (we tag `warnings` provenance
   `escalated via <backend>`; surface attribution in the UI when local weights land).
4. **Share-a-Like (§8)** — any model fine-tuned/distilled FROM Surya/Chandra weights
   or trained on their outputs must itself be OpenRAIL-M, as must the outputs.
   → train owned Indian-invoice models ONLY on Apache-2.0/MIT bases
   (PP-DocLayout / PP-OCRv5 / Table-Transformer), never bootstrapped from Surya output.
5. **Competition clause (2c)** — FFAA is a GST bookkeeping SaaS that *consumes* OCR;
   it must never be positioned as a document-AI API competing with Datalab.
6. **Revenue/funding threshold** — crossing $2M (Chandra) / $5M (Surya) requires a
   written commercial license from https://www.datalab.to/ BEFORE shipping local weights.

## Blocked dependencies (never add to the commercial build)

- `microsoft/layoutlmv3-base` — **cc-by-nc-sa-4.0** (non-commercial)
- Ultralytics YOLOv8/v11 — **AGPL-3.0** (SaaS copyleft)
- GOT-OCR2.0 — unlicensed
