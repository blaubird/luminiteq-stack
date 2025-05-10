from sqlalchemy.orm import Session
from fastapi import Depends, HTTPException
from .db import SessionLocal
from .models import Tenant

def get_db():
    db: Session = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def tenant_by_phone_id(
    phone_id: str,
    db: Session = Depends(get_db)
) -> Tenant:
    tenant = db.query(Tenant).filter_by(phone_id=phone_id).first()
    if not tenant:
        raise HTTPException(404, "Unknown tenant")
    return tenant
