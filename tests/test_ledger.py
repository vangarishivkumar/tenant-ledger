import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app

engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def override_get_db():
    db = TestingSessionLocal()
    try:
        yield db
    finally:
        db.close()

app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)

@pytest.fixture(autouse=True)
def reset_db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)

def headers(tenant="northbridge", actor="u1", role="admin"):
    return {"X-Tenant-ID": tenant, "X-Actor-ID": actor, "X-Role": role}

def test_bulk_partial_success_and_idempotency():
    batch = [
        {"idempotency_key": "tx-1", "client_ref": "C100", "entry_type": "deposit", "amount": "1000.50", "currency": "USD", "effective_at": "2026-01-15T10:00:00Z"},
        {"idempotency_key": "tx-bad-neg", "client_ref": "C100", "entry_type": "deposit", "amount": "-50.00", "currency": "USD", "effective_at": "2026-01-15T10:00:00Z"},
        {"idempotency_key": "tx-bad-curr", "client_ref": "C100", "entry_type": "deposit", "amount": "50.00", "currency": "XYZ", "effective_at": "2026-01-15T10:00:00Z"},
        {"idempotency_key": "tx-bad-future", "client_ref": "C100", "entry_type": "deposit", "amount": "50.00", "currency": "USD", "effective_at": "2099-01-01T00:00:00Z"},
    ]
    res1 = client.post("/transactions/bulk", json=batch, headers=headers())
    assert res1.status_code == 200
    body1 = res1.json()
    assert body1["accepted_count"] == 1
    assert body1["rejected_count"] == 3

    res2 = client.post("/transactions/bulk", json=batch, headers=headers())
    assert res2.json()["accepted_count"] == 0
    assert res2.json()["duplicate_count"] == 1

    bal = client.get("/clients/C100/balance", headers=headers()).json()
    assert bal["balances"]["USD"] == "1000.5000"

def test_tenant_isolation():
    batch = [{"idempotency_key": "tx-1", "client_ref": "C100", "entry_type": "deposit", "amount": "500.00", "currency": "USD", "effective_at": "2026-01-15T10:00:00Z"}]
    client.post("/transactions/bulk", json=batch, headers=headers(tenant="northbridge"))

    other_bal = client.get("/clients/C100/balance", headers=headers(tenant="rival_firm")).json()
    assert other_bal["balances"] == {}

    res = client.post("/transactions/bulk", json=batch, headers=headers(tenant="rival_firm"))
    assert res.json()["accepted_count"] == 1

def test_point_in_time_balance_and_reversals():
    batch = [
        {"idempotency_key": "q1", "client_ref": "C200", "entry_type": "deposit", "amount": "1000.00", "currency": "USD", "effective_at": "2026-03-31T23:59:59Z"},
        {"idempotency_key": "q2", "client_ref": "C200", "entry_type": "fee", "amount": "25.00", "currency": "USD", "effective_at": "2026-04-02T12:00:00Z"},
    ]
    ingest = client.post("/transactions/bulk", json=batch, headers=headers()).json()
    fee_id = ingest["accepted"][1]["id"]

    q1_bal = client.get("/clients/C200/balance?as_of=2026-03-31T23:59:59Z", headers=headers()).json()
    assert q1_bal["balances"]["USD"] == "1000.0000"

    rev = client.post(f"/transactions/{fee_id}/reverse", json={"reason": "Fee billed in error"}, headers=headers())
    assert rev.status_code == 200

    rev_again = client.post(f"/transactions/{fee_id}/reverse", json={"reason": "Duplicate click"}, headers=headers())
    assert rev_again.status_code == 409

    curr_bal = client.get("/clients/C200/balance", headers=headers()).json()
    assert curr_bal["balances"]["USD"] == "1000.0000"

def test_support_role_redaction_and_rbac():
    batch = [{"idempotency_key": "secret-ref-99", "client_ref": "C300", "entry_type": "deposit", "amount": "250.00", "currency": "EUR", "effective_at": "2026-02-01T10:00:00Z", "note": "VIP settlement"}]
    client.post("/transactions/bulk", json=batch, headers=headers(role="writer"))

    assert client.post("/transactions/bulk", json=batch, headers=headers(role="support")).status_code == 403
    assert client.get("/audit", headers=headers(role="support")).status_code == 403

    hist = client.get("/clients/C300/transactions", headers=headers(role="support")).json()
    tx = hist["transactions"][0]
    assert "note" not in tx
    assert "idempotency_key" not in tx
