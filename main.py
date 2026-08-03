import os, hashlib, secrets
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import FastAPI, Depends, Header, HTTPException, Request, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal
from pages import INDEX_HTML, INTAKE_HTML
import models

Base.metadata.create_all(bind=engine)

def ensure_columns():
    # Add profile columns to an existing DB without Alembic (safe if they already exist).
    from sqlalchemy import inspect, text
    cols_needed = {
        "goal": "VARCHAR", "package": "VARCHAR", "weight": "VARCHAR",
        "body_fat": "VARCHAR", "measurements": "VARCHAR", "next_follow_up": "VARCHAR", "assigned_to": "VARCHAR", "appointment": "VARCHAR",
    }
    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns("leads")}
    with engine.begin() as conn:
        for name, coltype in cols_needed.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE leads ADD COLUMN {name} {coltype} DEFAULT \'\''))
ensure_columns()

app = FastAPI(title="TrainerCRM API")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"], allow_credentials=False,
)

REGISTER_CODE = os.environ.get("REGISTER_CODE", "").strip()

def now():
    return datetime.now(timezone.utc)

def db():
    s = SessionLocal()
    try:
        yield s
    finally:
        s.close()

def hash_pw(pw: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), 200000).hex()
    return f"pbkdf2$200000${salt}${h}"

def verify_pw(pw: str, stored: str) -> bool:
    try:
        _algo, iters, salt, h = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), int(iters)).hex()
        return secrets.compare_digest(calc, h)
    except Exception:
        return False

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

# --- auth: every /api route (except intake + auth) requires a valid session token ---
def require_user(authorization: Optional[str] = Header(None), s: Session = Depends(db)):
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="login required")
    sess = s.get(models.Session, token)
    if not sess:
        raise HTTPException(status_code=401, detail="session expired")
    user = s.get(models.User, sess.user_id)
    if not user:
        raise HTTPException(status_code=401, detail="no user")
    return user

def check_access(lead, user):
    """Non-admins may only touch their own leads or unassigned ones."""
    if user.is_admin:
        return
    a = (lead.assigned_to or "").strip()
    if a and a != user.name:
        raise HTTPException(status_code=403, detail="This lead belongs to another trainer")

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
    assignedTo: str = ""
    note: str = ""

class StageIn(BaseModel):
    stage: str
    saleValue: Optional[float] = None
    appointment: Optional[str] = None

class NoteIn(BaseModel):
    text: str

class SaleIn(BaseModel):
    saleValue: Optional[float] = None
    goal: Optional[str] = None
    package: Optional[str] = None
    weight: Optional[str] = None
    bodyFat: Optional[str] = None
    measurements: Optional[str] = None
    nextFollowUp: Optional[str] = None
    assignedTo: Optional[str] = None
    appointment: Optional[str] = None

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

class RegisterIn(BaseModel):
    name: str = ""
    username: str
    password: str
    code: str = ""

class LoginIn(BaseModel):
    username: str
    password: str

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
        goal=d.get("goal") or "",
        assigned_to=d.get("assignedTo") or "",
        stage="new", sale_value=0.0, created_at=now(), history=hist,
    )
    s.add(lead)
    return lead

# ---------- auth ----------
def issue_token(s: Session, user_id: str) -> str:
    tok = secrets.token_urlsafe(32)
    s.add(models.Session(token=tok, user_id=user_id, created_at=now()))
    return tok

@app.post("/api/register", status_code=201)
def register(body: RegisterIn, s: Session = Depends(db)):
    uname = body.username.strip().lower()
    if not uname or not body.password:
        raise HTTPException(400, "username and password required")
    if s.query(models.User).filter(models.User.username == uname).first():
        raise HTTPException(409, "That username is taken")
    total = s.query(models.User).count()
    is_admin = 0
    if total == 0:
        is_admin = 1  # first user becomes admin
    else:
        if not REGISTER_CODE or body.code.strip() != REGISTER_CODE:
            raise HTTPException(403, "A valid invite code is required to register")
    u = models.User(username=uname, name=(body.name.strip() or uname), password_hash=hash_pw(body.password), is_admin=is_admin)
    s.add(u); s.flush()
    tok = issue_token(s, u.id)
    s.commit()
    return {"token": tok, "user": u.to_dict()}

@app.post("/api/login")
def login(body: LoginIn, s: Session = Depends(db)):
    uname = body.username.strip().lower()
    u = s.query(models.User).filter(models.User.username == uname).first()
    if not u or not verify_pw(body.password, u.password_hash):
        raise HTTPException(401, "Wrong username or password")
    tok = issue_token(s, u.id)
    s.commit()
    return {"token": tok, "user": u.to_dict()}

@app.post("/api/logout")
def logout(authorization: Optional[str] = Header(None), s: Session = Depends(db), user=Depends(require_user)):
    if authorization and authorization.lower().startswith("bearer "):
        sess = s.get(models.Session, authorization[7:].strip())
        if sess: s.delete(sess); s.commit()
    return {"ok": True}

@app.get("/api/me")
def me(user=Depends(require_user)):
    return user.to_dict()

# ---------- state ----------
@app.get("/api/state")
def get_state(s: Session = Depends(db), user=Depends(require_user)):
    lq = s.query(models.Lead)
    if not user.is_admin:
        lq = lq.filter((models.Lead.assigned_to == user.name) | (models.Lead.assigned_to == "") | (models.Lead.assigned_to == None))
    leads = [l.to_dict() for l in lq.all()]
    events = [e.to_dict() for e in s.query(models.Event).all()]
    st = {row.key: row.value for row in s.query(models.Setting).all()}
    return {"leads": leads, "events": events,
            "settings": {"goal": int(st.get("goal", "30")), "level": st.get("level", "PT3")}}

@app.post("/api/leads", status_code=201)
def create_lead(body: LeadIn, s: Session = Depends(db), user=Depends(require_user)):
    data = body.model_dump()
    if not data.get("assignedTo"):
        data["assignedTo"] = user.name
    lead = make_lead(s, data)
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.post("/api/import")
def bulk_import(body: List[LeadIn], s: Session = Depends(db), user=Depends(require_user)):
    added, skipped = insert_leads(s, [d.model_dump() for d in body])
    return {"added": added, "skipped": skipped}

@app.post("/api/leads/{lead_id}/stage")
def set_stage(lead_id: str, body: StageIn, s: Session = Depends(db), user=Depends(require_user)):
    lead = s.get(models.Lead, lead_id)
    if not lead:
        raise HTTPException(404)
    check_access(lead, user)
    prev = lead.stage
    lead.stage = body.stage
    if body.stage in TOUCH:
        lead.last_contact = now()
    if body.stage in ("reached", "replied"):
        lead.next_follow_up = ""
    if body.appointment is not None:
        lead.appointment = body.appointment
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
def log_contact(lead_id: str, s: Session = Depends(db), user=Depends(require_user)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    check_access(lead, user)
    lead.last_contact = now()
    lead.next_follow_up = ""
    hist = list(lead.history or []); hist.insert(0, {"at": now().isoformat(), "text": "Logged a contact"})
    lead.history = hist
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.post("/api/leads/{lead_id}/note")
def add_note(lead_id: str, body: NoteIn, s: Session = Depends(db), user=Depends(require_user)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    check_access(lead, user)
    if body.text.strip():
        hist = list(lead.history or []); hist.insert(0, {"at": now().isoformat(), "text": body.text.strip()})
        lead.history = hist; s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.patch("/api/leads/{lead_id}")
def patch_lead(lead_id: str, body: SaleIn, s: Session = Depends(db), user=Depends(require_user)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    check_access(lead, user)
    if body.saleValue is not None: lead.sale_value = body.saleValue
    if body.goal is not None: lead.goal = body.goal
    if body.package is not None: lead.package = body.package
    if body.weight is not None: lead.weight = body.weight
    if body.bodyFat is not None: lead.body_fat = body.bodyFat
    if body.measurements is not None: lead.measurements = body.measurements
    if body.nextFollowUp is not None: lead.next_follow_up = body.nextFollowUp
    if body.assignedTo is not None: lead.assigned_to = body.assignedTo
    if body.appointment is not None: lead.appointment = body.appointment
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.delete("/api/leads/{lead_id}", status_code=204)
def delete_lead(lead_id: str, s: Session = Depends(db), user=Depends(require_user)):
    lead = s.get(models.Lead, lead_id)
    if lead:
        check_access(lead, user)
        s.delete(lead); s.commit()
    return JSONResponse(status_code=204, content=None)

@app.put("/api/settings")
def put_settings(body: SettingsIn, s: Session = Depends(db), user=Depends(require_user)):
    for k, v in [("goal", str(body.goal)), ("level", body.level)]:
        row = s.get(models.Setting, k)
        if row: row.value = v
        else: s.add(models.Setting(key=k, value=v))
    s.commit()
    return {"goal": body.goal, "level": body.level}

@app.post("/api/reset")
def reset(s: Session = Depends(db), user=Depends(require_user)):
    s.query(models.Lead).delete(); s.query(models.Event).delete(); s.commit()
    return {"ok": True}

# ---------- file import (PDF / CSV / Excel) ----------
import re as _re, io as _io

def _norm_phone(raw):
    d = _re.sub(r"[^0-9]", "", raw or "")
    return f"({d[0:3]}) {d[3:6]}-{d[6:10]}" if len(d) == 10 else (raw or "")

def parse_report_text(txt):
    reEmail = _re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
    rePhone = _re.compile(r"\(?\d{3}\)?[\s.\-]?\d{3}[\s.\-]?\d{4}")
    reDate = _re.compile(r"\d{2}/\d{2}/\d{4}")
    reName = _re.compile(r"([A-Za-z'\-]+),\s?([A-Za-z'\-]+)")
    rePend = _re.compile(r"KickOff\s*([01\-])", _re.I)
    out = []
    for line in (txt or "").split("\n"):
        if "kickoff" not in line.lower() and not reEmail.search(line):
            continue
        em = reEmail.search(line); email = em.group(0) if em else ""
        ph = rePhone.search(line); phone = _norm_phone(ph.group(0)) if ph else ""
        if not email and not phone:
            continue
        nm = reName.search(line); first = last = ""
        if nm: last, first = nm.group(1), nm.group(2)
        if not first and email: first = email
        dates = reDate.findall(line); expire = dates[1] if len(dates) >= 2 else ""
        pm = rePend.search(line); pend = pm.group(1) if pm else ""
        out.append({"firstName": first, "lastName": last, "phone": phone, "email": email,
                    "pending": pend, "expire": expire, "source": "Kickoff report"})
    return out

def parse_rows(rows):
    if not rows: return []
    def maph(h):
        s = (str(h) if h is not None else "").strip().lower()
        if _re.search(r"first", s): return "first"
        if _re.search(r"last|surname", s): return "last"
        if _re.search(r"member.*name|full.*name|^name$|client", s): return "full"
        if _re.search(r"phone|cell|mobile", s): return "phone"
        if "email" in s: return "email"
        if _re.search(r"source|origin", s): return "source"
        if "expir" in s: return "expire"
        return None
    header = [maph(x) for x in rows[0]]
    has = any(header)
    data = rows[1:] if has else rows
    mapping = header if has else ["first", "last", "phone", "email"]
    out = []
    for r in data:
        rec = {"first": "", "last": "", "phone": "", "email": "", "source": "Imported", "expire": ""}
        for i, val in enumerate(r):
            f = mapping[i] if i < len(mapping) else None
            if not f: continue
            v = str(val).strip() if val is not None else ""
            if f == "full":
                if "," in v:
                    a, b = v.split(",", 1); rec["last"], rec["first"] = a.strip(), b.strip()
                else:
                    parts = v.split()
                    if parts: rec["first"], rec["last"] = parts[0], " ".join(parts[1:])
            elif f in rec:
                rec[f] = v
        if not rec["first"] and not rec["email"] and not rec["phone"]:
            continue
        out.append({"firstName": rec["first"], "lastName": rec["last"], "phone": rec["phone"],
                    "email": rec["email"], "source": rec["source"] or "Imported", "expire": rec["expire"]})
    return out

def insert_leads(s, items):
    existing = s.query(models.Lead).all()
    def dupe(d):
        ph = "".join(ch for ch in (d.get("phone") or "") if ch.isdigit())
        em = (d.get("email") or "").lower()
        fn = (d.get("firstName") or "").lower()
        ln = (d.get("lastName") or "").lower()
        for l in existing:
            lp = "".join(ch for ch in (l.phone or "") if ch.isdigit())
            if ph and len(ph) >= 7 and lp == ph: return True
            if em and (l.email or "").lower() == em: return True
            if fn and l.first_name.lower() == fn and (l.last_name or "").lower() == ln: return True
        return False
    added = skipped = 0
    for d in items:
        if not (d.get("firstName") or d.get("email") or d.get("phone")): continue
        if dupe(d): skipped += 1; continue
        lead = make_lead(s, d); existing.append(lead); added += 1
    s.commit()
    return added, skipped

@app.post("/api/import-file")
async def import_file(file: UploadFile = File(...), s: Session = Depends(db), user=Depends(require_user)):
    fname = (file.filename or "").lower()
    content = await file.read()
    leads = []
    try:
        if fname.endswith(".pdf"):
            import pdfplumber
            text = ""
            with pdfplumber.open(_io.BytesIO(content)) as pdf:
                for pg in pdf.pages:
                    text += (pg.extract_text() or "") + "\n"
            leads = parse_report_text(text)
        elif fname.endswith(".csv"):
            import csv
            rows = list(csv.reader(_io.StringIO(content.decode("utf-8", "ignore"))))
            leads = parse_rows(rows)
        elif fname.endswith(".xlsx") or fname.endswith(".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
            rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
            leads = parse_rows(rows)
        else:
            raise HTTPException(400, "Unsupported file type. Use PDF, CSV, or Excel.")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Could not read that file. Try CSV, or paste the text instead.")
    added, skipped = insert_leads(s, leads)
    return {"added": added, "skipped": skipped, "parsed": len(leads)}

# ---------- public intake (no key required) ----------
@app.post("/api/intake", status_code=201)
def intake(body: IntakeIn, s: Session = Depends(db)):
    if not body.first.strip():
        raise HTTPException(400, "name required")
    lead = make_lead(s, {
        "firstName": body.first, "lastName": body.last, "phone": body.phone,
        "email": body.email, "source": "Web form", "referredBy": body.referred,
    })
    if body.goal:
        lead.goal = body.goal
    s.commit()
    return {"ok": True}

# ---------- serve frontend ----------
@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML)

@app.get("/intake", response_class=HTMLResponse)
@app.get("/intake.html", response_class=HTMLResponse)
def intake_page():
    return HTMLResponse(INTAKE_HTML)

@app.get("/health")
def health():
    return {"ok": True}
