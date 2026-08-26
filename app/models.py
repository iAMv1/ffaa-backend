from datetime import datetime

from decimal import Decimal

from fastapi_users.db import SQLAlchemyBaseUserTable
from sqlalchemy import (
    Column, Integer, String, Float, Numeric, Date, DateTime, ForeignKey, Boolean,
    Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from .database import Base

# Money is exact: Numeric(14,2) with Decimal at the ORM boundary.
# Rates/quantities/scores stay Float — they are not currency.
Money = Numeric(14, 2, asdecimal=True)
_ZERO = Decimal("0.00")

class Client(Base):
    __tablename__ = "clients"
    id = Column(Integer, primary_key=True, index=True)
    # Global UNIQUE(name) dropped for multi-tenant: identical client names may
    # exist across owners; uniqueness is per-owner (see __table_args__).
    name = Column(String(255), index=True)
    # Tenant owner. Nullable only for pre-migration rows (backfilled to user 1
    # by scripts/migrate_multi_tenant.py).
    owner_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    email = Column(String(255), nullable=True)
    gst_number = Column(String(50), nullable=True)
    address = Column(Text, nullable=True)
    auto_created = Column(Boolean, default=False)  # minted from OCR company name (ticket 03)
    created_at = Column(DateTime)
    __table_args__ = (UniqueConstraint("owner_id", "name", name="uq_clients_owner_name"),)

class Invoice(Base):
    __tablename__ = "invoices"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    invoice_number = Column(String(100), index=True)
    invoice_date = Column(Date)
    company_name = Column(String(255))
    gst_rate = Column(Float, default=0.0)
    taxable_value = Column(Money, default=_ZERO)
    total_amount = Column(Money, default=_ZERO)
    cgst = Column(Money, default=_ZERO)
    sgst = Column(Money, default=_ZERO)
    igst = Column(Money, default=_ZERO)
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
    rate = Column(Money)
    taxable_value = Column(Money)
    gst_rate = Column(Float, default=0.0)
    cgst = Column(Money, default=_ZERO)
    sgst = Column(Money, default=_ZERO)
    igst = Column(Money, default=_ZERO)
    line_total = Column(Money)

    invoice = relationship("Invoice", back_populates="items")

class BankStatement(Base):
    __tablename__ = "bank_statements"
    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(Integer, ForeignKey("clients.id"))
    date = Column(Date)
    narration = Column(Text)
    debit = Column(Money, default=_ZERO)
    credit = Column(Money, default=_ZERO)
    balance = Column(Money, default=_ZERO)
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


class User(SQLAlchemyBaseUserTable[int], Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.now)
    # W1 revocation counter: JWTs embed it as the `tv` claim; any password or
    # email change bumps it and every outstanding cookie dies on next use.
    token_version = Column(Integer, nullable=False, default=0)


# --- Billing (P4 Razorpay) -----------------------------------------------------

class BillingPlan(Base):
    __tablename__ = "billing_plans"
    code = Column(String(50), primary_key=True)  # 'free' / 'pro'
    name = Column(String(100))
    price_rupees = Column(Integer, default=0)
    # NULL cap = unlimited (pro).
    invoice_cap = Column(Integer, nullable=True)
    client_cap = Column(Integer, nullable=True)
    features_json = Column(Text, default="[]")


class BillingSubscription(Base):
    __tablename__ = "billing_subscriptions"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), unique=True)
    plan_code = Column(String(50), default="free")
    status = Column(String(20), default="active")  # active / expired
    current_period_end = Column(DateTime)
    updated_at = Column(DateTime)


class BillingPayment(Base):
    __tablename__ = "billing_payments"
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"))
    razorpay_order_id = Column(String(100), index=True)
    # UNIQUE: the idempotency anchor — verify + webhook may race/replay (rev #6).
    razorpay_payment_id = Column(String(100), unique=True)
    amount_rupees = Column(Integer, default=0)
    status = Column(String(20), default="created")  # created / paid / failed
    created_at = Column(DateTime)
