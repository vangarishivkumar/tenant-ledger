from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional
from fastapi import FastAPI, Depends, Header, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import func, extract
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import Base, engine, get_db
from app.models import LedgerEntry, AuditLog
from app.schemas import TransactionIn, ReversalRequest

Base.metadata.create_all(bind=engine)
app = FastAPI(title="Tenant Ledger API")

OUTFLOW_TYPES = {"withdrawal", "fee", "transfer_out"}
VALID_ROLES = {"admin", "writer", "analyst", "support"}

class AuthContext:
    def __init__(self, tenant_id: str, actor_id: str, role: str):
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.role = role

def get_auth(
    x_tenant_id: str = Header(..., alias="X-Tenant-ID"),
    x_actor_id: str = Header(..., alias="X-Actor-ID"),
    x_role: str = Header(..., alias="X-Role"),
) -> AuthContext:
    role = x_role.lower()
    if role not in VALID_ROLES:
        raise HTTPException(status_code=403, detail="Invalid role")
    return AuthContext(tenant_id=x_tenant_id, actor_id=x_actor_id, role=role)

def require_write(auth: AuthContext = Depends(get_auth)) -> AuthContext:
    if auth.role not in {"admin", "writer"}:
        raise HTTPException(status_code=403, detail="Write access required")
    return auth

@app.post("/transactions/bulk")
def ingest_bulk(
    payload: list[dict[str, Any]],
    auth: AuthContext = Depends(require_write),
    db: Session = Depends(get_db),
):
    accepted, duplicates, rejected = [], [], []

    for idx, raw_row in enumerate(payload):
        try:
            tx = TransactionIn.model_validate(raw_row)
        except ValidationError as exc:
            rejected.append({"row": idx, "data": raw_row, "errors": exc.errors(include_url=False)})
            continue

        signed = -tx.amount if tx.entry_type in OUTFLOW_TYPES else tx.amount
        entry = LedgerEntry(
            tenant_id=auth.tenant_id,
            client_ref=tx.client_ref,
            idempotency_key=tx.idempotency_key,
            entry_type=tx.entry_type,
            signed_amount=signed,
            currency=tx.currency,
            effective_at=tx.effective_at,
            note=tx.note,
        )

        try:
            with db.begin_nested():
                db.add(entry)
                db.flush()
                db.add(AuditLog(
                    tenant_id=auth.tenant_id,
                    actor_id=auth.actor_id,
                    actor_role=auth.role,
                    action="ENTRY_CREATED",
                    target_id=entry.id,
                    details=f"idempotency_key={tx.idempotency_key}",
                ))
            accepted.append({"row": idx, "id": entry.id, "idempotency_key": tx.idempotency_key})
        except IntegrityError:
            duplicates.append({"row": idx, "idempotency_key": tx.idempotency_key})

    db.commit()
    return {
        "accepted_count": len(accepted),
        "duplicate_count": len(duplicates),
        "rejected_count": len(rejected),
        "accepted": accepted,
        "duplicates": duplicates,
        "rejected": rejected,
    }

@app.post("/transactions/{entry_id}/reverse")
def reverse_entry(
    entry_id: str,
    body: ReversalRequest,
    auth: AuthContext = Depends(require_write),
    db: Session = Depends(get_db),
):
    original = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.id == entry_id, LedgerEntry.tenant_id == auth.tenant_id)
        .first()
    )
    if not original:
        raise HTTPException(status_code=404, detail="Transaction not found")
    if original.entry_type == "reversal":
        raise HTTPException(status_code=400, detail="Cannot reverse a reversal entry")

    reversal = LedgerEntry(
        tenant_id=auth.tenant_id,
        client_ref=original.client_ref,
        idempotency_key=f"rev-{original.id}",
        entry_type="reversal",
        signed_amount=-original.signed_amount,
        currency=original.currency,
        effective_at=datetime.now(timezone.utc),
        reverses_entry_id=original.id,
        note=f"Reversal of {original.id}: {body.reason}",
    )

    try:
        db.add(reversal)
        db.flush()
        db.add(AuditLog(
            tenant_id=auth.tenant_id,
            actor_id=auth.actor_id,
            actor_role=auth.role,
            action="ENTRY_REVERSED",
            target_id=original.id,
            details=f"reversal_id={reversal.id}; reason={body.reason}",
        ))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Entry has already been reversed")

    return {"reversal_id": reversal.id, "reversed_entry_id": original.id}

@app.get("/clients/{client_ref}/balance")
def get_balance(
    client_ref: str,
    as_of: Optional[datetime] = Query(default=None),
    auth: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    cutoff = as_of or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)

    rows = (
        db.query(LedgerEntry.currency, func.sum(LedgerEntry.signed_amount))
        .filter(
            LedgerEntry.tenant_id == auth.tenant_id,
            LedgerEntry.client_ref == client_ref,
            LedgerEntry.effective_at <= cutoff,
        )
        .group_by(LedgerEntry.currency)
        .all()
    )
    balances = {curr: str(Decimal(total or 0).quantize(Decimal("0.0001"))) for curr, total in rows}
    return {"client_ref": client_ref, "as_of": cutoff.isoformat(), "balances": balances}

@app.get("/clients/{client_ref}/summary")
def get_monthly_summary(
    client_ref: str,
    year: int = Query(..., ge=2000, le=2100),
    month: int = Query(..., ge=1, le=12),
    auth: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    if auth.role == "support":
        raise HTTPException(status_code=403, detail="Support role cannot view analytical summaries")

    entries = (
        db.query(LedgerEntry)
        .filter(
            LedgerEntry.tenant_id == auth.tenant_id,
            LedgerEntry.client_ref == client_ref,
            extract("year", LedgerEntry.effective_at) == year,
            extract("month", LedgerEntry.effective_at) == month,
        )
        .all()
    )

    summary: dict[str, dict[str, Decimal]] = {}
    for e in entries:
        bucket = summary.setdefault(
            e.currency,
            {"inflows": Decimal("0"), "outflows": Decimal("0"), "fees": Decimal("0"), "net_change": Decimal("0")},
        )
        amt = Decimal(e.signed_amount)
        if e.entry_type == "fee":
            bucket["fees"] += abs(amt)
        elif amt > 0:
            bucket["inflows"] += amt
        else:
            bucket["outflows"] += abs(amt)
        bucket["net_change"] += amt

    formatted = {
        curr: {k: str(v.quantize(Decimal("0.0001"))) for k, v in vals.items()}
        for curr, vals in summary.items()
    }
    return {"client_ref": client_ref, "period": f"{year}-{month:02d}", "currencies": formatted}

@app.get("/clients/{client_ref}/transactions")
def get_client_history(
    client_ref: str,
    auth: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    entries = (
        db.query(LedgerEntry)
        .filter(LedgerEntry.tenant_id == auth.tenant_id, LedgerEntry.client_ref == client_ref)
        .order_by(LedgerEntry.effective_at.desc())
        .all()
    )
    result = []
    for e in entries:
        item = {
            "id": e.id,
            "entry_type": e.entry_type,
            "amount": str(e.signed_amount),
            "currency": e.currency,
            "effective_at": e.effective_at.isoformat(),
        }
        if auth.role != "support":
            item["idempotency_key"] = e.idempotency_key
            item["note"] = e.note
            item["reverses_entry_id"] = e.reverses_entry_id
        result.append(item)
    return {"client_ref": client_ref, "transactions": result}

@app.get("/audit")
def get_audit_log(
    auth: AuthContext = Depends(get_auth),
    db: Session = Depends(get_db),
):
    if auth.role != "admin":
        raise HTTPException(status_code=403, detail="Admin role required")
    logs = (
        db.query(AuditLog)
        .filter(AuditLog.tenant_id == auth.tenant_id)
        .order_by(AuditLog.created_at.desc())
        .limit(100)
        .all()
    )
    return [
        {
            "id": l.id,
            "actor_id": l.actor_id,
            "actor_role": l.actor_role,
            "action": l.action,
            "target_id": l.target_id,
            "details": l.details,
            "created_at": l.created_at.isoformat(),
        }
        for l in logs
    ]
