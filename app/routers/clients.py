from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime
from fastapi.responses import FileResponse

from .. import models, schemas
from ..database import SessionLocal
from ..folders import list_client_folders, resolve_file_path, safe_unlink
from ..tenancy import get_owned_client, require_entitlement
from ..users import current_active_user

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/clients", response_model=schemas.ClientOut)
def create_client(
    client: schemas.ClientCreate,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    require_entitlement(db, user, "client_create")
    # Uniqueness is per-owner now — another tenant's identical name must not leak.
    db_client = (
        db.query(models.Client)
        .filter(models.Client.name == client.name, models.Client.owner_id == user.id)
        .first()
    )
    if db_client:
        raise HTTPException(status_code=400, detail="Client already exists")
    db_client = models.Client(**client.model_dump(), owner_id=user.id, created_at=datetime.now())
    db.add(db_client)
    db.commit()
    db.refresh(db_client)
    return db_client

@router.get("/clients", response_model=list[schemas.ClientOut])
def list_clients(
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    return (
        db.query(models.Client)
        .filter(models.Client.owner_id == user.id)
        .offset(skip)
        .limit(limit)
        .all()
    )


@router.put("/clients/{client_id}", response_model=schemas.ClientOut)
def update_client(
    client_id: int,
    client_update: schemas.ClientUpdate,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    client = get_owned_client(db, user, client_id)
    data = client_update.model_dump(exclude_unset=True)
    new_name = data.get("name")
    if new_name and new_name != client.name:
        clash = db.query(models.Client).filter(
            models.Client.owner_id == user.id,
            models.Client.name == new_name,
            models.Client.id != client_id,
        ).first()
        if clash:
            raise HTTPException(status_code=409, detail="Client name already exists")
    for field, value in data.items():
        setattr(client, field, value)
    db.commit()
    db.refresh(client)
    return client


def _chunks(ids: list, size: int = 900):
    """SQLite IN-lists blow up past 999 bound vars — chunk them."""
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


@router.delete("/clients/{client_id}")
def delete_client(
    client_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    client = get_owned_client(db, user, client_id)
    # S1 (audit): collect archived file paths before the DB cascade so the
    # on-disk copies can be removed after the commit.
    archived_paths = [
        p for (p,) in db.query(models.Invoice.file_path)
        .filter(models.Invoice.client_id == client_id) if p
    ] + [
        p for (p,) in db.query(models.BankStatement.file_path)
        .filter(models.BankStatement.client_id == client_id) if p
    ]
    inv_ids = [i.id for i in db.query(models.Invoice.id).filter(models.Invoice.client_id == client_id)]
    if inv_ids:
        # null duplicate_of on surviving invoices that point into the deleted set
        for chunk in _chunks(inv_ids):
            db.query(models.Invoice).filter(
                models.Invoice.duplicate_of.in_(chunk)
            ).update({models.Invoice.duplicate_of: None}, synchronize_session=False)
        for chunk in _chunks(inv_ids):
            db.query(models.InvoiceItem).filter(models.InvoiceItem.invoice_id.in_(chunk)).delete(
                synchronize_session=False)
            db.query(models.DuplicateFlag).filter(
                (models.DuplicateFlag.invoice_id.in_(chunk))
                | (models.DuplicateFlag.potential_duplicate_id.in_(chunk))
            ).delete(synchronize_session=False)
            db.query(models.Reconciliation).filter(
                models.Reconciliation.invoice_id.in_(chunk)
            ).delete(synchronize_session=False)
        db.query(models.Invoice).filter(models.Invoice.client_id == client_id).delete(
            synchronize_session=False)
    bank_ids = [b.id for b in db.query(models.BankStatement.id).filter(models.BankStatement.client_id == client_id)]
    if bank_ids:
        for chunk in _chunks(bank_ids):
            db.query(models.Reconciliation).filter(
                models.Reconciliation.bank_statement_id.in_(chunk)
            ).delete(synchronize_session=False)
        db.query(models.BankStatement).filter(models.BankStatement.client_id == client_id).delete(
            synchronize_session=False)
    db.query(models.EmailReminder).filter(models.EmailReminder.client_id == client_id).delete()
    db.delete(client)
    db.commit()
    for p in archived_paths:
        safe_unlink(p, user.id)
    return {"deleted": client_id}


@router.get("/clients/{client_id}/folders")
def get_client_folders(
    client_id: int,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    client = get_owned_client(db, user, client_id)
    return {
        "client": client.name,
        "folders": list_client_folders(client.name, owner_id=user.id),
    }


@router.get("/clients/{client_id}/files/{path:path}")
def download_client_file(
    client_id: int,
    path: str,
    db: Session = Depends(get_db),
    user: models.User = Depends(current_active_user),
):
    client = get_owned_client(db, user, client_id)
    resolved = resolve_file_path(client.name, path, owner_id=user.id)
    if not resolved:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(resolved, filename=resolved.name)
