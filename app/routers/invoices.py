import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from datetime import datetime
import os
import shutil
from .. import models, schemas
from ..database import SessionLocal
from ..ocr import process_invoice_document
from ..folders import archive_file
from ..audit import audit_invoice
from ..services import scan_and_flag_duplicates


def _out(inv) -> schemas.InvoiceOut:
    o = schemas.InvoiceOut.model_validate(inv)
    o.audit = audit_invoice(inv)
    return o

ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".pdf"}


def _safe_filename(name: str) -> str:
    """Keep only the basename; strip separators/dot-prefixes to block traversal."""
    base = os.path.basename(name.replace("\\", "/"))
    base = base.replace("..", "").replace("/", "").replace("\\", "").strip().lstrip(".")
    return base or "invoice"

logger = logging.getLogger("ffaa.invoices")

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# Day-first precedence is deliberate: Indian invoices dominate the corpus.
# Single source of truth for invoice date parsing — parsers return strings,
# this helper owns format semantics.
INVOICE_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y")


def _parse_invoice_date(value: str | None):
    if not value:
        return None
    for fmt in INVOICE_DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None

def _save_invoice(file: UploadFile, client_id: int | None, invoice_type: str, db: Session) -> models.Invoice:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"Unsupported file type: {ext}")

    upload_dir = "uploads/invoices"
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(
        upload_dir,
        f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{_safe_filename(file.filename)}",
    )
    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    ocr_result = process_invoice_document(file_path)

    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        company_name = ocr_result.get("company_name") or "Unknown"
        client = db.query(models.Client).filter(models.Client.name == company_name).first()
        if not client:
            client = models.Client(name=company_name, created_at=datetime.now())
            db.add(client)
            db.commit()
            db.refresh(client)

    invoice_date = _parse_invoice_date(ocr_result.get("invoice_date"))

    # ponytail: archive to Client/Year/Month/{Sales,Purchase}/
    doc_date = invoice_date or datetime.now().date()
    category = "Sales" if invoice_type == "sales" else "Purchase"
    archived = archive_file(
        file_path, client.name, doc_date, category, os.path.basename(file_path)
    )

    invoice = models.Invoice(
        client_id=client.id,
        invoice_number=ocr_result.get("invoice_number"),
        invoice_date=invoice_date,
        company_name=ocr_result.get("company_name"),
        gst_rate=ocr_result.get("gst_rate") or 0.0,
        taxable_value=ocr_result.get("taxable_value") or 0.0,
        total_amount=ocr_result.get("total_amount") or 0.0,
        cgst=ocr_result.get("cgst") or 0.0,
        sgst=ocr_result.get("sgst") or 0.0,
        igst=ocr_result.get("igst") or 0.0,
        hsn_code=ocr_result.get("hsn_code"),
        quantity=ocr_result.get("quantity"),
        item_description=ocr_result.get("item_description"),
        invoice_type=invoice_type,
        ocr_confidence=ocr_result.get("confidence", 0.0),
        file_path=archived,
        created_at=datetime.now(),
    )
    db.add(invoice)
    db.commit()
    db.refresh(invoice)

    # InvoiceItem from OCR header fields (ponytail: single line for now)
    if ocr_result.get("item_description") and (ocr_result.get("quantity") or ocr_result.get("taxable_value")):
        line = models.InvoiceItem(
            invoice_id=invoice.id,
            description=ocr_result.get("item_description"),
            hsn_code=ocr_result.get("hsn_code"),
            quantity=ocr_result.get("quantity") or 1.0,
            rate=ocr_result.get("taxable_value") / (ocr_result.get("quantity") or 1) if ocr_result.get("taxable_value") else 0.0,
            taxable_value=ocr_result.get("taxable_value") or 0.0,
            gst_rate=ocr_result.get("gst_rate") or 0.0,
            cgst=ocr_result.get("cgst") or 0.0,
            sgst=ocr_result.get("sgst") or 0.0,
            igst=ocr_result.get("igst") or 0.0,
            line_total=(ocr_result.get("taxable_value") or 0.0) + (ocr_result.get("cgst") or 0.0) + (ocr_result.get("sgst") or 0.0) + (ocr_result.get("igst") or 0.0),
        )
        db.add(line)

    # auto duplicate check — shared service (same rules as the re-scan endpoint)
    scan_and_flag_duplicates(db, invoice)

    db.commit()
    db.refresh(invoice)
    return invoice

@router.post("/upload-invoice", response_model=schemas.InvoiceOut)
async def upload_invoice(
    file: UploadFile = File(...),
    client_id: int | None = Form(None),
    invoice_type: str = Form("sales"),
    db: Session = Depends(get_db),
):
    try:
        return _out(_save_invoice(file, client_id, invoice_type, db))
    except HTTPException:
        raise
    except Exception:
        logger.exception("Invoice upload failed for %r", file.filename)
        raise HTTPException(
            status_code=500,
            detail=f"Processing failed for {file.filename!r}; see server logs.",
        )

@router.post("/upload-invoices", response_model=list[schemas.UploadFileResult])
async def upload_invoices_batch(
    files: list[UploadFile] = File(...),
    client_id: int = Form(...),
    invoice_type: str = Form("sales"),
    db: Session = Depends(get_db),
):
    results = []
    for file in files:
        try:
            invoice = _save_invoice(file, client_id, invoice_type, db)
            results.append(schemas.UploadFileResult(
                filename=file.filename or "?",
                status="ok",
                invoice=_out(invoice),
            ))
        except Exception as e:
            logger.exception("Batch invoice upload failed for %r", file.filename)
            db.rollback()
            results.append(schemas.UploadFileResult(
                filename=file.filename or "?",
                status="failed",
                error=f"{type(e).__name__} while processing {file.filename!r} (details logged server-side)",
            ))
    return results

@router.get("/invoices", response_model=list[schemas.InvoiceOut])
def list_invoices(
    client_id: int | None = None,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    q = db.query(models.Invoice)
    if client_id is not None:
        q = q.filter(models.Invoice.client_id == client_id)
    return [_out(i) for i in q.offset(skip).limit(limit).all()]

@router.get("/invoices/{invoice_id}", response_model=schemas.InvoiceOut)
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return _out(invoice)

@router.put("/invoices/{invoice_id}/review", response_model=schemas.InvoiceOut)
def review_invoice(
    invoice_id: int, invoice_update: schemas.InvoiceCreate, db: Session = Depends(get_db)
):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.approved:
        raise HTTPException(status_code=409, detail="Invoice already approved")

    invoice.invoice_number = invoice_update.invoice_number
    invoice.invoice_date = invoice_update.invoice_date
    invoice.company_name = invoice_update.company_name
    invoice.gst_rate = invoice_update.gst_rate
    invoice.taxable_value = invoice_update.taxable_value
    invoice.total_amount = invoice_update.total_amount
    invoice.cgst = invoice_update.cgst
    invoice.sgst = invoice_update.sgst
    invoice.igst = invoice_update.igst
    invoice.hsn_code = invoice_update.hsn_code
    invoice.quantity = invoice_update.quantity
    invoice.item_description = invoice_update.item_description
    invoice.status = "reviewed"
    invoice.reviewed_at = datetime.now()
    db.commit()
    db.refresh(invoice)
    return _out(invoice)

@router.put("/invoices/{invoice_id}/approve", response_model=schemas.InvoiceOut)
def approve_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.file_path:
        # ponytail: copy approved invoice to Final Books leaf
        doc_date = invoice.invoice_date or datetime.now().date()
        try:
            archive_file(
                invoice.file_path,
                invoice.client.name,
                doc_date,
                "Final Books",
                os.path.basename(invoice.file_path),
            )
        except Exception:
            pass
    invoice.approved = True
    invoice.status = "approved"
    db.commit()
    db.refresh(invoice)
    return _out(invoice)


@router.delete("/invoices/{invoice_id}")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    # cascade: items, dupe flags (both directions), reconciliations, bank links
    db.query(models.InvoiceItem).filter(models.InvoiceItem.invoice_id == invoice_id).delete()
    db.query(models.DuplicateFlag).filter(
        (models.DuplicateFlag.invoice_id == invoice_id)
        | (models.DuplicateFlag.potential_duplicate_id == invoice_id)
    ).delete()
    db.query(models.Reconciliation).filter(
        models.Reconciliation.invoice_id == invoice_id
    ).delete()
    db.query(models.Invoice).filter(
        models.Invoice.duplicate_of == invoice_id
    ).update({models.Invoice.duplicate_of: None}, synchronize_session=False)
    for bs in db.query(models.BankStatement).filter(models.BankStatement.invoice_id == invoice_id):
        bs.invoice_id = None
        bs.reconciled = False
    db.delete(invoice)
    db.commit()
    return {"deleted": invoice_id}
