from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy.orm import Session, joinedload

from .. import models
from ..database import SessionLocal
from ..tally import invoices_to_tally_xml

router = APIRouter()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@router.get("/export-tally")
def export_tally(
    client_id: int | None = Query(None),
    approved_only: bool = Query(True),
    db: Session = Depends(get_db),
):
    q = db.query(models.Invoice).options(joinedload(models.Invoice.items))
    if client_id is not None:
        q = q.filter(models.Invoice.client_id == client_id)
    if approved_only:
        q = q.filter(models.Invoice.approved == True)  # noqa: E712
    # ponytail: exclude accepted duplicates
    q = q.filter(models.Invoice.is_duplicate == False)  # noqa: E712
    invoices = q.all()
    if not invoices:
        raise HTTPException(status_code=404, detail="No invoices to export")
    xml = invoices_to_tally_xml(invoices)
    return Response(
        content=xml,
        media_type="application/xml",
        headers={"Content-Disposition": "attachment; filename=tally_import.xml"},
    )
