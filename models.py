import uuid
from datetime import datetime, timezone
from sqlalchemy import Column, String, Float, DateTime, JSON, Integer
from database import Base

def _uuid():
    return uuid.uuid4().hex[:12]

def _now():
    return datetime.now(timezone.utc)

class Lead(Base):
    __tablename__ = "leads"
    id = Column(String, primary_key=True, default=_uuid)
    first_name = Column(String, default="")
    last_name = Column(String, default="")
    phone = Column(String, default="")
    email = Column(String, default="")
    contact = Column(String, default="")
    source = Column(String, default="Manual")
    referred_by = Column(String, default="")
    pending = Column(String, default="")
    expire = Column(String, default="")
    stage = Column(String, default="new")
    sale_value = Column(Float, default=0.0)
    created_at = Column(DateTime(timezone=True), default=_now)
    last_contact = Column(DateTime(timezone=True), nullable=True)
    history = Column(JSON, default=list)

    def to_dict(self):
        return {
            "id": self.id,
            "firstName": self.first_name or "",
            "lastName": self.last_name or "",
            "phone": self.phone or "",
            "email": self.email or "",
            "contact": self.contact or (self.phone or self.email or ""),
            "source": self.source or "Manual",
            "referredBy": self.referred_by or "",
            "pending": self.pending or "",
            "expire": self.expire or "",
            "stage": self.stage or "new",
            "saleValue": self.sale_value or 0,
            "createdAt": (self.created_at or _now()).isoformat(),
            "lastContact": self.last_contact.isoformat() if self.last_contact else None,
            "history": self.history or [],
        }

class Event(Base):
    __tablename__ = "events"
    id = Column(Integer, primary_key=True, autoincrement=True)
    type = Column(String)
    at = Column(DateTime(timezone=True), default=_now)
    amount = Column(Float, default=0.0)

    def to_dict(self):
        return {"type": self.type, "at": (self.at or _now()).isoformat(), "amount": self.amount or 0}

class Setting(Base):
    __tablename__ = "settings"
    key = Column(String, primary_key=True)
    value = Column(String, default="")
