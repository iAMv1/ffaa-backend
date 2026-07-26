from sqlalchemy import Column, Integer, String, Float, Date, DateTime, ForeignKey, Boolean, Text
from sqlalchemy.orm import relationship
from .database import Base

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, index=True)
    email = Column(String(255), nullable=True)
    gst_number = Column(String(50), nullable=True)
    address = Column(Text, nullable=True)
    created_at = Column(DateTime)

class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    invoice_number = Column(String(100), index=True)
    invoice_date = Column(Date)
    company_name = Column(String(255))
    gst_rate = Column(Float, default=0.0)
    taxable_value = Column(Float, default=0.0)
    total_amount = Column(Float, default=0.0)
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    hsn_code = Column(String(50), nullable=True)
    quantity = Column(Float, nullable=True)
    item_description = Column(Text, nullable=True)
    invoice_type = Column(String(20))  # sales or purchase
    status = Column(String(20), default="pending_review")
    ocr_confidence = Column(Float, nullable=True)
    file_path = Column(String(500), nullable=True)  # ponytail: archived location
    is_duplicate = Column(Boolean, default=False)
    duplicate_of = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    created_at = Column(DateTime)
    reviewed_at = Column(DateTime, nullable=True)
    approved = Column(Boolean, default=False)

    client = relationship("Client")
    items = relationship("InvoiceItem", back_populates="invoice")
    duplicate_flags = relationship("DuplicateFlag", foreign_keys="DuplicateFlag.invoice_id", back_populates="invoice")

class InvoiceItem(Base):
    __tablename__ = "invoice_items"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    description = Column(Text)
    hsn_code = Column(String(50), nullable=True)
    quantity = Column(Float)
    rate = Column(Float)
    taxable_value = Column(Float)
    gst_rate = Column(Float, default=0.0)
    cgst = Column(Float, default=0.0)
    sgst = Column(Float, default=0.0)
    igst = Column(Float, default=0.0)
    line_total = Column(Float)

    invoice = relationship("Invoice", back_populates="items")

class BankStatement(Base):
    __tablename__ = "bank_statements"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    date = Column(Date)
    narration = Column(Text)
    debit = Column(Float, default=0.0)
    credit = Column(Float, default=0.0)
    balance = Column(Float, default=0.0)
    reconciled = Column(Boolean, default=False)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=True)
    file_path = Column(String(500), nullable=True)  # ponytail: archived location
    created_at = Column(DateTime)

class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"))
    bank_statement_id = Column(Integer, ForeignKey("bank_statements.id"))
    match_score = Column(Float)
    matched_by = Column(String(50))  # manual or auto
    confirmed = Column(Boolean, default=False)
    created_at = Column(DateTime)


class DuplicateFlag(Base):
    __tablename__ = "duplicate_flags"
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    potential_duplicate_id = Column(Integer, ForeignKey("invoices.id"), nullable=False)
    similarity_score = Column(Float)
    matched_fields = Column(String(255))
    status = Column(String(20), default="pending")  # pending, accepted, rejected
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime)

    invoice = relationship("Invoice", foreign_keys=[invoice_id], back_populates="duplicate_flags")
    potential_duplicate = relationship("Invoice", foreign_keys=[potential_duplicate_id])


class EmailReminder(Base):
    __tablename__ = "email_reminders"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"), nullable=False)
    subject = Column(String(255))
    body = Column(Text)
    status = Column(String(20), default="pending")  # pending, sent, failed
    error_message = Column(Text, nullable=True)
    sent_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime)

    client = relationship("Client")
