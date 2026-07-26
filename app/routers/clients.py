from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime

from .. import models, schemas
from ..database import SessionLocal
from ..folders import list_client_folders

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.post("/clients", response_model=schemas.ClientOut)
def create_client(client: schemas.ClientCreate, db: Session = Depends(get_db)):
    db_client = db.query(models.Client).filter(models.Client.name == client.name).first()
    if db_client:
        raise HTTPException(status_code=400, detail="Client already exists")
    db_client = models.Client(**client.model_dump(), created_at=datetime.now())
    db.add(db_client)
    db.commit()
    db.refresh(db_client)
    return db_client

@router.get("/clients", response_model=list[schemas.ClientOut])
def list_clients(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    return db.query(models.Client).offset(skip).limit(limit).all()


@router.put("/clients/{client_id}", response_model=schemas.ClientOut)
def update_client(
    client_id: int, client_update: schemas.ClientCreate, db: Session = Depends(get_db)
):
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    for field, value in client_update.model_dump().items():
        setattr(client, field, value)
    db.commit()
    db.refresh(client)
    return client


@router.get("/clients/{client_id}/folders")
def get_client_folders(client_id: int, db: Session = Depends(get_db)):
    client = db.query(models.Client).filter(models.Client.id == client_id).first()
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"client": client.name, "folders": list_client_folders(client.name)}
