import uuid
from datetime import datetime, timezone
from sqlalchemy import (
    Column, String, Numeric, DateTime, ForeignKey, UniqueConstraint, Index, Text
)
from app.database import Base

def utcnow():
    return datetime.now(timezone.utc)

class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, index=True)
    client_ref = Column(String(64), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False)
    
    entry_type = Column(String(32), nullable=False)
    signed_amount = Column(Numeric(18, 4), nullable=False)
    currency = Column(String(3), nullable=False)
    
    effective_at = Column(DateTime(timezone=True), nullable=False)
    recorded_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
    
    reverses_entry_id = Column(String(36), ForeignKey("ledger_entries.id"), nullable=True, unique=True)
    note = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_tenant_idempotency"),
        Index("ix_tenant_client_effective", "tenant_id", "client_ref", "effective_at"),
    )

class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    tenant_id = Column(String(64), nullable=False, index=True)
    actor_id = Column(String(64), nullable=False)
    actor_role = Column(String(32), nullable=False)
    action = Column(String(64), nullable=False)
    target_id = Column(String(36), nullable=False, index=True)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=utcnow)
