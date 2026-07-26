from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from sqlalchemy.orm import Session
from datetime import datetime
import os
import shutil

from .. import models, schemas
from ..database import SessionLocal, engine
from ..ocr import process_invoice_document
from ..folders import archive_file

models.Base.metadata.create_all(bind=engine)

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def _save_invoice(file: UploadFile, client_id: int | None, invoice_type: str, db: Session) -> models.Invoice:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")

    upload_dir = "uploads/invoices"
    os.makedirs(upload_dir, exist_ok=True)
    file_path = os.path.join(
        upload_dir, f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
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

    invoice_date = None
    invoice_date_str = ocr_result.get("invoice_date")
    if invoice_date_str:
        for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
            try:
                invoice_date = datetime.strptime(invoice_date_str, fmt).date()
                break
            except ValueError:
                continue

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
    return invoice

@router.post("/upload-invoice", response_model=schemas.InvoiceOut)
async def upload_invoice(
    file: UploadFile = File(...),
    client_id: int | None = Form(None),
    invoice_type: str = Form("sales"),
    db: Session = Depends(get_db),
):
    return _save_invoice(file, client_id, invoice_type, db)

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
                invoice=schemas.InvoiceOut.model_validate(invoice),
            ))
        except Exception as e:
            db.rollback()
            results.append(schemas.UploadFileResult(
                filename=file.filename or "?",
                status="failed",
                error=str(e)[:300],
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
    return q.offset(skip).limit(limit).all()

@router.get("/invoices/{invoice_id}", response_model=schemas.InvoiceOut)
def get_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    return invoice

@router.put("/invoices/{invoice_id}/review", response_model=schemas.InvoiceOut)
def review_invoice(
    invoice_id: int, invoice_update: schemas.InvoiceCreate, db: Session = Depends(get_db)
):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")

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
    return invoice

@router.put("/invoices/{invoice_id}/approve", response_model=schemas.InvoiceOut)
def approve_invoice(invoice_id: int, db: Session = Depends(get_db)):
    invoice = db.query(models.Invoice).filter(models.Invoice.id == invoice_id).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    if invoice.file_path:
        # ponytail: copy approved invoice to Final Books leaf
        doc_date = invoice.invoice_date or datetime.now().date()
        archive_file(
            invoice.file_path,
            invoice.client.name,
            doc_date,
            "Final Books",
            os.path.basename(invoice.file_path),
        )
    invoice.approved = True
    invoice.status = "approved"
    db.commit()
    db.refresh(invoice)
    return invoice
