# OCR Gauntlet — Live Progress

## Overfitting check (2026-08-10) — PASSED
- **kirana holdout k_045–k_049 (never used in tuning): 5/5 totals EXACT, all suppliers correct**
- kenil train/val/test (22 docs, blind for totals): all produce sane positive values, zero crashes
- Verdict: parser rules are structural (label positions, currency tokens, layout conventions), not memorized. Generalizes to unseen invoices of same type.
- Known limitation: rules tuned on kirana-style purchase bills may not generalize to handwritten or WhatsApp-screenshot formats without more diverse data.

## VERIFIED FINAL (2026-08-10) — production engine on real Indian invoices
- **kirana 45/50 processed: totals 44/44 EXACT (100%), suppliers ~38/44, avg 7-12s/doc**
- kenil + sroie + synthetic regression: fields 0.900, all fixtures 5-9s
- pytest 19/19 · FE build green · E2E 21/22
- Supplier/buyer disambiguation: paired SOLD BY/BILL TO rule + From:/Supplier GSTIN labels
- Total rules v2: GRAND TOTAL → in-words marker → line-start TOTAL last-match → truncation fallback → SUBTOTAL

## PROMOTED (2026-08-10) — RapidOCR primary + parser v2 in production
- 3 independent reviewer agents audited ocr.py / bank_parse.py / routers+audit with fresh context.
- **20+ defects found, all fixed by 3 parallel fix teams:**
  - ocr.py: parser crash on junk numerics (500), self-defeating ValueError retry, temp-file race (concurrent same-name uploads corrupted results), GST-rate-captured-as-tax ("CGST 2.5%"), qty-from-rate-slot bug, integer totals rejected, invoice_number absorbing labels, legacy total first-vs-last, rec_boxes shape crash, dead code removed.
  - bank_parse.py: pending-amounts leak (shift-by-one corruption on every multi-record import), balance-delta exact-equality misclassification, Cr/DR suffix amounts → 0.0, keyword substring false positives (HYDRO→debit), statement-year fallback for Dec–Jan spans, OCR temp dir cleanup.
  - routers/security: **filename path traversal in uploads** (arbitrary write), **folder separator escape** (cross-client file read), BankStatementOut.balance None → permanent 500, Ladakh state code 38, delete-cascade orphaned duplicate_of FKs, client rename IntegrityError → 409, temp-file leaks on preview/400.
- Verified: pytest 19/19 after every team, pyflakes clean, E2E re-run stable at 21/22.

## PROMOTED (2026-08-10) — RapidOCR primary + parser v2 in production
- Engine: RapidOCR (INT8 ONNX) on RAW image → paddle v6-medium fallback (thin+no-total or suspicious totals). Parser v2 merged into `app/ocr.py` (Indian totals, supplier/buyer disambiguation).
- kirana (50 real Indian): totals 5→**50/50**, suppliers 7→**36/50**, dates 48/50, avg 6.9s. Paddle hybrid was 0/50 at 84s.
- Synthetic bench: fields 0.900 (was 0.908), items 0.500 (was 0.667 — tradeoff), **all fixtures 5-9s** (was 5-269s).
- Receipt 9.1s (was 227s — fallback trigger bug fixed: thin+no-total only).
- pytest 19/19. Requirements: +rapidocr-onnxruntime.

## Round 11 (2026-08-10) — parallel critical review + fixes
- **GET /reconciliations?client_id=** — history endpoint + FE section in Reconcile tab (with Undo).
- **DELETE endpoints**: invoices (cascade items/flags/reconciliations + unlink banks), bank-statements (cascade reconciliations), clients (full cascade), reminders.
- **Bank upload two-step preview**: `preview=true` parses without insert → FE preview panel → Import commits. No more blind inserts.
- **Audit catch**: `BankStatementOut` never returned `invoice_id` — bank-table link badge was dead. Fixed in schema.
- pytest 16/16 (4 new tests). FE build green.

## Round 10 (2026-08-10) — thermal-total ceiling (measured, closed)
- Total-keyword fallback trigger added (total missing + TOTAL present → medium rerun). Fired on 1 receipt; medium also failed → no gain.
- Upscale 2x + v5 mobile: no gain (79.5→75.0 is a RECOGNITION misread, not resolution).
- v5_server (torch now installed): same misreads, 108-242s. Removed (165MB freed).
- **Conclusion: failing SROIE totals are beyond every CPU model we can run — recognition-level ceiling. No trigger/resolution/engine lever helps.**
- Pilot kit ready: `bench/pilot/` folder + `bench/pilot_review.py` (OCR everything → CSV for eyeball review). Needs user-supplied real Indian invoices + one YONO/HDFC PDF.
- pytest 16/16.
- **Reconcile Undo split**: `DELETE /reconciliations/{id}` keeps the bank row, removes the match, reopens bank. FE Undo uses it (no more bank-row delete cascade).
- **Label-less company fallback** (receipts): first all-caps vendor-looking line when no label found. SROIE real scans: company 0/5 → **4/5**; total hits 3 → **7/30** (4/5 company, 2/5 date, 1/5 exact total — totals on dense thermal receipts remain hard; medium fallback doesn't trigger since word count is fine).
- Real-data hunt: no open Indian invoice images exist (Commons/GitHub/HF searched); no real YONO PDF (mechanism proven on synthetic word-broken PDF only). Both remain pilot-data items.
- **Env fix**: v5_server experiment auto-installed modelscope 1.38.1 + broke no-torch imports. Resolved: modelscope<1.30 + aistudio-sdk + torch CPU (200MB, needed by paddlex model-resolution chain; NOT the 6GB model — that stays uninstalled per user).
- pytest 16/16, FE build green.

**Goal:** Make FFAA invoice OCR significantly better + more accurate, on CPU, no GPU, no paid APIs.
**Bar:** baidu/Unlimited-OCR (3B VLM, whole-document structured parse, ParseBench Text Content 86.81).
We match its *behavior*: whole-doc understanding + structure + field accuracy — measured on our own invoice benchmark (10 fixtures, GT per field + line items).

**Bench:** `bench/gen_fixtures.py` (10 degraded invoices) + `bench/score.py` (full pipeline A/B) + `bench/cache_texts.py` + `bench/score_parser.py` (fast parser loop).
**Score:** fields 70% + line-items 30% (overall). Per-field: str exact-normalized, float rel ≤1%.

| Round | Variant | Change | fields | items | overall | verdict |
|-------|---------|--------|--------|-------|---------|---------|
| base | ocr_base | OTSU preprocess + regex parse | ~200s/page OCR | — | slow | baseline |
| R1 | app/ocr.py | fastNlMeans→adaptive (cap 1600px, CLAHE only when low contrast) | — | — | 90s/page | 2.2x faster |
| R2 | app/ocr.py | **text-layer fast path** (PDF text extraction, ms) | — | — | **0.03s/doc** | 50 docs = 1.12s |
| R3 | app/ocr.py | hardened parser: digit-fix, GST/HSN tolerance, newline labels, total priority, month dates | 9 real docs | — | — | azure 110 ✓ famoser 8390.4 ✓ pdfco date ✓ |
| R4 | app/ocr.py | **column-aware line items** (PyMuPDF word coords → header zones) | real rows | famoser 2 rows w/ qty/rate/amount ✓ | — | real tables extracted |
| R5 | app/ocr.py | `_num` guard (letters→None), label-line filter, tie-break last Total | — | — | — | probe 11/13 conf 1.0, pytest 12/12 |

## Current state (2026-08-09 round complete)
- **Speed**: born-digital PDF → text layer, 0.02-0.10s/doc. 50 docs ≈ 1.1s (target <10s ✓).
- **Scans**: OCR path ~80-90s/page on this CPU (no GPU — physics; PP-OCRv6 medium).
- **Real-doc quality**: Azure invoice.pdf → date/company/total/line_items all correct; famoser → total 8390.40 + 2 rows w/ qty/rate/amount; pdfco → invoice#/date/company.
- **Bar note**: baidu/Unlimited-OCR = VLM whole-doc parse (GPU). We match its behavior on digital docs via text layer + structure; scans stay pipeline-OCR.

## Round 6 (2026-08-09, Indian-domain pivot)
- **Indian bank statements**: 5 real fixtures (SBI yono, HDFC savings, ICICI savings, SBI credit, from indian-bank-statement-parser repo). Old code: 0/5 parse.
  - New block parser in `bank_parse.py`: HDFC multi-line records ✓ 18 rows (dates/debit/credit/balance), ICICI S.No rows ✓ 12 rows (balance-delta side detection), SBI credit card ✓ 8 rows (Cr flag). yono = word-broken column streams — needs word coords (PDF path), documented limitation.
  - Added `%d.%m.%Y` date format; narration continuation that starts with a date (HDFC "01/04/2024 Ref...") no longer splits records.
- **OCR-path line items**: refactored zone-table logic into shared `_items_from_word_lines`; OCR path now uses detection boxes (`structured_line_items_from_ocr`). Bench v2 running to measure (was 0/24).
- **Shadow fix**: std>90 → CLAHE(16x16) + adaptiveThreshold for uneven lighting.
- SROIE labeled scans downloaded (10 receipts + key-value GT) — Malaysian, used as scan-accuracy evidence only.

## Round 7 — measured final (2026-08-09)

**Labeled synthetic bench (10 degraded invoices, GT):**
| Metric | baseline | final | Δ |
|--------|----------|-------|---|
| fields | 0.725 | **0.975** (117/120) | +25 pts |
| line items | 0.000 | **0.958** (23/24) | +96 pts |
| overall | 0.507 | **0.970** | +46 pts |

Per-fixture: 9/10 at 12/12 fields + items 1.00 (incl. shadow_band 6→12, skew, noise, rotation). merged_cells 10/12 + items 0.50 (full-width merged row — known).
Fixes this round: always CLAHE+OTSU (grayscale branch lost grid digits — measured, reverted), line-gap clustering (box-height tol was scale-broken), single-predict text+items, regex fallback.

**SROIE real scans (5, Malaysian): total 2/5, company 0/5, date 3/5.**
Synthetic overstates real-scan generalization. Parser tuned on labeled-synthetic + born-digital; dense thermal receipts differ. NOT tuning to these 5 (overfit risk). Pilot data needed for real-scan accuracy.

**Indian bank statements (real, 4 files): HDFC 18 rows ✓, ICICI 12 rows ✓ (balance-delta side detection), SBI credit 8 rows ✓ (Cr flag). yono = word-broken column streams, unparseable from text dump (needs PDF word-coords — documented).**
Old code parsed 0/4.

**Speed: 50 unique born-digital docs 1.82s. Scans ~70-150s/page (CPU, no GPU).**

## Verification suite (2026-08-09, per rigor audit)

| Claim | Test | Result | Verdict |
|-------|------|--------|---------|
| 50 docs < 10s | 50 UNIQUE docs (8 real web PDFs + 42 distinct generated text-PDFs), cold process | **1.82s**, 49/50 with fields (1 miss = chart page) | HOLDS, honest composition |
| Scan OCR works | receipt.png vs modlens vision read | OCR total 2516.28 == vision GT $2,516.28 | HOLDS (after tie-break fix; was subtotal before) |
| New parser > old | blind-side A/B on 8 real texts | new 20 hits vs old 13; old CRASHED on pdfco | HOLDS, +54%, 0 crashes vs 1 |
| OCR path accuracy | 10 labeled synthetic fixtures (degradation: skew/noise/rotation/shadow/low-contrast) | **fields 87/120 (72.5%), items 0/24 (0%), avg_conf 0.97** | PARTIAL — items on OCR path = ZERO |
| Probe conf 1.0 | synthetic fixture | 11/13 fields | still self-referential; superseded by bench above |
| igst miss in bench | scorer artifact: GT 0.0 vs pred None → miss | scorer bug, not parser | FIX scorer |

## Known gaps (measured, ranked)
1. **OCR-path line items: 0/24** — regex needs clean single-line rows; OCR emits word-broken lines. Fix: box-based reading order + row grouping (v4) or PP-Structure. Biggest scan-path gap.
2. shadow_band fixture: 6/12 fields — heavy shadow breaks det. Fix: local contrast (CLAHE on tiles) before threshold.
3. Scan corpus thin: 1 receipt. Need labeled scans (SROIE/CORD) — HF gated, GitHub mirrors pending.
4. No comparison vs Unlimited-OCR or any VLM — GPU/API constraint; documented, not done.
5. 50-doc mix with scans: 10 scans + 40 PDFs ≈ 800s+ — OCR latency on scans is the binding constraint, not parser.
