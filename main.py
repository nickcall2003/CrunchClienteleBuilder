import os
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import FastAPI, Depends, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal
import models

Base.metadata.create_all(bind=engine)

app = FastAPI(title="TrainerCRM API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)

FRONTEND = os.path.join(os.path.dirname(__file__), "frontend")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()

def now():
    return datetime.now(timezone.utc)

def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

def seed_settings():
    s = SessionLocal()
    try:
        for k, v in [("goal", "30"), ("level", "PT3")]:
            if not s.get(models.Setting, k):
                s.add(models.Setting(key=k, value=v))
        s.commit()
    finally:
        s.close()
seed_settings()

# --- auth: only enforced if APP_PASSWORD is set. Intake stays public. ---
def require_key(x_app_key: Optional[str] = Header(None)):
    if APP_PASSWORD and x_app_key != APP_PASSWORD:
        raise HTTPException(status_code=401, detail="bad key")
    return True

# ---------- request bodies ----------
class LeadIn(BaseModel):
    firstName: str = ""
    lastName: str = ""
    phone: str = ""
    email: str = ""
    contact: str = ""
    source: str = "Manual"
    referredBy: str = ""
    pending: str = ""
    expire: str = ""
    note: str = ""

class StageIn(BaseModel):
    stage: str
    saleValue: Optional[float] = None

class NoteIn(BaseModel):
    text: str

class SaleIn(BaseModel):
    saleValue: float

class SettingsIn(BaseModel):
    goal: int
    level: str

class IntakeIn(BaseModel):
    first: str = ""
    last: str = ""
    phone: str = ""
    email: str = ""
    goal: str = ""
    referred: str = ""

STAGE_EVENT = {"reached": "text", "replied": "reply", "scheduled": "schedule", "sold": "sold"}
TOUCH = {"reached", "replied", "scheduled", "showed", "sold"}

def make_lead(s: Session, d: dict) -> models.Lead:
    hist = [{"at": now().isoformat(), "text": "Added"}]
    if d.get("note"):
        hist.insert(0, {"at": now().isoformat(), "text": d["note"]})
    lead = models.Lead(
        first_name=(d.get("firstName") or "").strip(),
        last_name=(d.get("lastName") or "").strip(),
        phone=(d.get("phone") or "").strip(),
        email=(d.get("email") or "").strip(),
        contact=(d.get("contact") or d.get("phone") or d.get("email") or "").strip(),
        source=d.get("source") or "Manual",
        referred_by=(d.get("referredBy") or "").strip(),
        pending=d.get("pending") or "",
        expire=d.get("expire") or "",
        stage="new", sale_value=0.0, created_at=now(), history=hist,
    )
    s.add(lead)
    return lead

# ---------- state ----------
@app.get("/api/state")
def get_state(s: Session = Depends(db), _=Depends(require_key)):
    leads = [l.to_dict() for l in s.query(models.Lead).all()]
    events = [e.to_dict() for e in s.query(models.Event).all()]
    st = {row.key: row.value for row in s.query(models.Setting).all()}
    return {"leads": leads, "events": events,
            "settings": {"goal": int(st.get("goal", "30")), "level": st.get("level", "PT3")}}

@app.post("/api/leads", status_code=201)
def create_lead(body: LeadIn, s: Session = Depends(db), _=Depends(require_key)):
    lead = make_lead(s, body.model_dump())
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.post("/api/import")
def bulk_import(body: List[LeadIn], s: Session = Depends(db), _=Depends(require_key)):
    existing = s.query(models.Lead).all()
    def dupe(d):
        ph = "".join(ch for ch in (d.phone or "") if ch.isdigit())
        em = (d.email or "").lower()
        for l in existing:
            lp = "".join(ch for ch in (l.phone or "") if ch.isdigit())
            if ph and len(ph) >= 7 and lp == ph:
                return True
            if em and (l.email or "").lower() == em:
                return True
            if d.firstName and l.first_name.lower() == d.firstName.lower() and (l.last_name or "").lower() == (d.lastName or "").lower():
                return True
        return False
    added = skipped = 0
    for d in body:
        if not (d.firstName or d.email or d.phone):
            continue
        if dupe(d):
            skipped += 1; continue
        lead = make_lead(s, d.model_dump())
        existing.append(lead); added += 1
    s.commit()
    return {"added": added, "skipped": skipped}

@app.post("/api/leads/{lead_id}/stage")
def set_stage(lead_id: str, body: StageIn, s: Session = Depends(db), _=Depends(require_key)):
    lead = s.get(models.Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    prev = lead.stage
    lead.stage = body.stage
    if body.stage in TOUCH:
        lead.last_contact = now()
    if body.stage == "sold" and body.saleValue is not None:
        lead.sale_value = body.saleValue
    if body.stage != prev and body.stage in STAGE_EVENT:
        amt = body.saleValue if (body.stage == "sold" and body.saleValue is not None) else 0
        s.add(models.Event(type=STAGE_EVENT[body.stage], at=now(), amount=amt or 0))
    hist = list(lead.history or [])
    label = {"new":"Not Contacted","reached":"Texted","replied":"Replied","scheduled":"Scheduled","showed":"Showed / Kickoff","sold":"Sold PT","noresp":"No Response","dead":"Not Interested"}.get(body.stage, body.stage)
    hist.insert(0, {"at": now().isoformat(), "text": "\u2192 " + label})
    lead.history = hist
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.post("/api/leads/{lead_id}/contact")
def log_contact(lead_id: str, s: Session = Depends(db), _=Depends(require_key)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    lead.last_contact = now()
    hist = list(lead.history or []); hist.insert(0, {"at": now().isoformat(), "text": "Logged a contact"})
    lead.history = hist
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.post("/api/leads/{lead_id}/note")
def add_note(lead_id: str, body: NoteIn, s: Session = Depends(db), _=Depends(require_key)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    if body.text.strip():
        hist = list(lead.history or []); hist.insert(0, {"at": now().isoformat(), "text": body.text.strip()})
        lead.history = hist; s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.patch("/api/leads/{lead_id}")
def patch_lead(lead_id: str, body: SaleIn, s: Session = Depends(db), _=Depends(require_key)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    lead.sale_value = body.saleValue
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.delete("/api/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: str, s: Session = Depends(db), _=Depends(require_key)):
    lead = s.get(models.Lead, lead_id)
    if lead: s.delete(lead); s.commit()
    return JSONResponse(status_code=204, content=None)

@app.put("/api/settings")
def put_settings(body: SettingsIn, s: Session = Depends(db), _=Depends(require_key)):
    for k, v in [("goal", str(body.goal)), ("level", body.level)]:
        row = s.get(models.Setting, k)
        if row: row.value = v
        else: s.add(models.Setting(key=k, value=v))
    s.commit()
    return {"goal": body.goal, "level": body.level}

@app.post("/api/reset")
def reset(s: Session = Depends(db), _=Depends(require_key)):
    s.query(models.Lead).delete(); s.query(models.Event).delete(); s.commit()
    return {"ok": True}

# ---------- public intake (no key required) ----------
@app.post("/api/intake", status_code=201)
def intake(body: IntakeIn, s: Session = Depends(db)):
    if not body.first.strip():
        raise HTTPException(400, "name required")
    note = ("Goal: " + body.goal) if body.goal else ""
    make_lead(s, {
        "firstName": body.first, "lastName": body.last, "phone": body.phone,
        "email": body.email, "source": "Web form", "referredBy": body.referred, "note": note,
    })
    s.commit()
    return {"ok": True}

# ---------- serve frontend ----------
@app.get("/")
def index():
    return FileResponse(os.path.join(FRONTEND, "index.html"))

@app.get("/intake")
@app.get("/intake.html")
def intake_page():
    return FileResponse(os.path.join(FRONTEND, "intake.html"))

@app.get("/health")
def health():
    return {"ok": True}
