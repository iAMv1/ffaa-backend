from pydantic import BaseModel, ConfigDict
from typing import Optional, List
from datetime import date, datetime

class ClientBase(BaseModel):
    name: str
    email: Optional[str] = None
    gst_number: Optional[str] = None
    address: Optional[str] = None

class ClientCreate(ClientBase):
    pass

class ClientUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    gst_number: Optional[str] = None
    address: Optional[str] = None

class ClientOut(ClientBase):
    id: int
    auto_created: bool = False
    model_config = ConfigDict(from_attributes=True)

class InvoiceItemBase(BaseModel):
    description: str
    hsn_code: Optional[str] = None
    quantity: float
    rate: float
    taxable_value: float
    gst_rate: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    igst: float = 0.0
    line_total: float

class InvoiceItemCreate(InvoiceItemBase):
    pass

class InvoiceItemOut(InvoiceItemBase):
    id: int
    model_config = ConfigDict(from_attributes=True)

class InvoiceBase(BaseModel):
    client_id: int
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    company_name: Optional[str] = None
    gst_rate: float = 0.0
    taxable_value: float = 0.0
    total_amount: float = 0.0
    cgst: float = 0.0
    sgst: float = 0.0
    igst: float = 0.0
    hsn_code: Optional[str] = None
    quantity: Optional[float] = None
    item_description: Optional[str] = None
    invoice_type: str = "sales"

class InvoiceCreate(InvoiceBase):
    items: Optional[List[InvoiceItemCreate]] = []

class InvoiceOut(InvoiceBase):
    id: int
    status: str
    approved: bool
    ocr_confidence: Optional[float] = None
    file_path: Optional[str] = None
    is_duplicate: bool = False
    duplicate_of: Optional[int] = None
    items: List[InvoiceItemOut] = []
    audit: Optional[dict] = None
    model_config = ConfigDict(from_attributes=True)

class BankStatementBase(BaseModel):
    client_id: int
    date: date
    narration: str
    debit: float = 0.0
    credit: float = 0.0
    balance: float = 0.0

class BankStatementCreate(BankStatementBase):
    pass

class BankStatementOut(BankStatementBase):
    id: int
    reconciled: bool
    invoice_id: Optional[int] = None
    file_path: Optional[str] = None
    balance: Optional[float] = None
    model_config = ConfigDict(from_attributes=True)


class ReconciliationOut(BaseModel):
    id: int
    invoice_id: int
    invoice_number: Optional[str] = None
    bank_statement_id: int
    narration: Optional[str] = None
    amount: Optional[float] = None
    match_score: float
    matched_by: Optional[str] = None
    confirmed: bool
    created_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)

class OCRResponse(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    company_name: Optional[str] = None
    gst_rate: Optional[float] = None
    taxable_value: Optional[float] = None
    total_amount: Optional[float] = None
    cgst: Optional[float] = None
    sgst: Optional[float] = None
    igst: Optional[float] = None
    hsn_code: Optional[str] = None
    quantity: Optional[float] = None
    item_description: Optional[str] = None
    confidence: float = 0.0
    raw_text: Optional[str] = None


class DuplicateFlagBase(BaseModel):
    invoice_id: int
    potential_duplicate_id: int
    similarity_score: float
    matched_fields: Optional[str] = None
    status: str = "pending"


class DuplicateFlagOut(DuplicateFlagBase):
    id: int
    created_at: Optional[datetime] = None
    reviewed_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)


class DuplicateResolve(BaseModel):
    action: str  # accept or reject


class DuplicateScanResult(BaseModel):
    invoice_id: int
    flags_created: int
    matches: List[DuplicateFlagOut] = []


class UploadFileResult(BaseModel):
    filename: str
    status: str  # ok | failed
    invoice: Optional[InvoiceOut] = None
    error: Optional[str] = None


class ReminderTemplate(BaseModel):
    name: str
    subject: str
    body: str


class ReminderSend(BaseModel):
    template_name: Optional[str] = None
    custom_message: Optional[str] = None
    days: Optional[int] = 30
    send: bool = True


class EmailReminderOut(BaseModel):
    id: int
    client_id: int
    subject: str
    body: str
    status: str
    error_message: Optional[str] = None
    sent_at: Optional[datetime] = None
    created_at: Optional[datetime] = None
    model_config = ConfigDict(from_attributes=True)
