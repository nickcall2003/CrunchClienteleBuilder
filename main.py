import os, hashlib, secrets, json
from datetime import datetime, timezone
from typing import Optional, List
from fastapi import FastAPI, Depends, Header, HTTPException, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import Base, engine, SessionLocal
from pages import INDEX_HTML, INTAKE_HTML

APP_VERSION = "Aug4-7"
import models

Base.metadata.create_all(bind=engine)

def ensure_columns():
    # Add profile columns to an existing DB without Alembic (safe if they already exist).
    from sqlalchemy import inspect, text
    cols_needed = {
        "goal": "VARCHAR", "package": "VARCHAR", "weight": "VARCHAR",
        "body_fat": "VARCHAR", "measurements": "VARCHAR", "next_follow_up": "VARCHAR", "assigned_to": "VARCHAR", "appointment": "VARCHAR", "lead_type": "VARCHAR",
    }
    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns("leads")}
    with engine.begin() as conn:
        for name, coltype in cols_needed.items():
            if name not in existing:
                conn.execute(text(f'ALTER TABLE leads ADD COLUMN {name} {coltype} DEFAULT \'\''))
    try:
        with engine.begin() as conn:
            conn.execute(text("UPDATE leads SET lead_type='kickoff' WHERE lead_type IS NULL OR lead_type=''"))
    except Exception:
        pass
    try:
        ucols = {c["name"] for c in insp.get_columns("users")}
        if "availability" not in ucols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE users ADD COLUMN availability VARCHAR DEFAULT ''"))
    except Exception:
        pass
    try:
        ecols = {c["name"] for c in insp.get_columns("events")}
        if "user_id" not in ecols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE events ADD COLUMN user_id VARCHAR DEFAULT ''"))
    except Exception:
        pass
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

def bootstrap_admin():
    """Recovery hatch: if ADMIN_USERNAME + ADMIN_PASSWORD env vars are set, create or
    reset that user as an admin on startup. Lets a locked-out admin get back in from
    Railway env vars without touching the database directly."""
    u = os.environ.get("ADMIN_USERNAME", "").strip().lower()
    p = os.environ.get("ADMIN_PASSWORD", "").strip()
    if not u or not p:
        return
    s = SessionLocal()
    try:
        user = s.query(models.User).filter(models.User.username == u).first()
        if user:
            user.password_hash = hash_pw(p)
            user.is_admin = 1
        else:
            name = os.environ.get("ADMIN_NAME", "").strip() or u
            user = models.User(username=u, name=name, password_hash=hash_pw(p), is_admin=1)
            s.add(user)
        s.commit()
    except Exception:
        s.rollback()
    finally:
        s.close()

bootstrap_admin()

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

def require_admin(user=Depends(require_user)):
    if not user.is_admin:
        raise HTTPException(status_code=403, detail="Admin only")
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
    type: str = "kickoff"
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
    type: Optional[str] = None

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
        lead_type=d.get("type") or "kickoff",
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

@app.get("/api/gym-stats")
def gym_stats(s: Session = Depends(db), user=Depends(require_user)):
    leads = s.query(models.Lead).all()
    texted = sum(1 for l in leads if l.stage in ("reached", "replied", "scheduled", "showed", "sold"))
    replied = sum(1 for l in leads if l.stage in ("replied", "scheduled", "showed", "sold"))
    sold = sum(1 for l in leads if l.stage == "sold")
    return {"reply": round(replied / texted * 100) if texted else 0,
            "close": round(sold / replied * 100) if replied else 0,
            "texted": texted}

@app.get("/api/trainers")
def trainers(s: Session = Depends(db), user=Depends(require_user)):
    return [{"id": u.id, "name": u.name, "isAdmin": bool(u.is_admin)} for u in s.query(models.User).all()]

_DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DEFAULT_AVAIL = {d: {"on": d != "sun", "start": "06:00", "end": "20:00"} for d in _DAYS}

def get_avail(u):
    try:
        a = json.loads(u.availability) if u.availability else {}
    except Exception:
        a = {}
    return {d: a.get(d, DEFAULT_AVAIL[d]) for d in _DAYS}

class AvailIn(BaseModel):
    availability: dict

@app.get("/api/availability")
def get_availability(user=Depends(require_user)):
    return {"availability": get_avail(user)}

@app.put("/api/availability")
def put_availability(body: AvailIn, s: Session = Depends(db), user=Depends(require_user)):
    clean = {}
    for d in _DAYS:
        v = (body.availability or {}).get(d, DEFAULT_AVAIL[d])
        clean[d] = {"on": bool(v.get("on", True)), "start": str(v.get("start", "06:00")), "end": str(v.get("end", "20:00"))}
    user.availability = json.dumps(clean)
    s.commit()
    return {"availability": clean}

@app.get("/api/schedule")
def schedule(date: str, s: Session = Depends(db), user=Depends(require_user)):
    # date = YYYY-MM-DD
    try:
        import datetime as _dt
        wk = _DAYS[_dt.date.fromisoformat(date).weekday()]
    except Exception:
        raise HTTPException(400, "bad date")
    trainers = s.query(models.User).all()
    all_leads = s.query(models.Lead).all()
    out = []
    for t in trainers:
        av = get_avail(t)[wk]
        appts = []
        for l in all_leads:
            if (l.assigned_to or "") == t.name and (l.appointment or "")[:10] == date and l.stage in ("scheduled", "showed"):
                mine = (t.id == user.id or user.is_admin)
                nm = (l.first_name + " " + ((l.last_name or "")[:1] + "." if l.last_name else "")).strip() if mine else "Booked"
                appts.append({"time": l.appointment[11:16], "label": nm, "type": l.lead_type or "kickoff", "leadId": (l.id if mine else "")})
        appts.sort(key=lambda a: a["time"])
        out.append({"id": t.id, "name": t.name, "isAdmin": bool(t.is_admin), "avail": av, "appts": appts})
    return {"date": date, "weekday": wk, "trainers": out}

class PasswordIn(BaseModel):
    password: str

@app.post("/api/users/{uid}/password")
def admin_reset_password(uid: str, body: PasswordIn, s: Session = Depends(db), admin=Depends(require_admin)):
    u = s.get(models.User, uid)
    if not u: raise HTTPException(404)
    if not body.password or len(body.password) < 4:
        raise HTTPException(400, "Password must be at least 4 characters")
    u.password_hash = hash_pw(body.password)
    s.query(models.Session).filter(models.Session.user_id == uid).delete()  # force re-login
    s.commit()
    return {"ok": True}

@app.post("/api/users/{uid}/admin")
def admin_toggle(uid: str, s: Session = Depends(db), admin=Depends(require_admin)):
    u = s.get(models.User, uid)
    if not u: raise HTTPException(404)
    u.is_admin = 0 if u.is_admin else 1
    s.commit()
    return {"ok": True, "isAdmin": bool(u.is_admin)}

@app.delete("/api/users/{uid}")
def admin_delete_user(uid: str, s: Session = Depends(db), admin=Depends(require_admin)):
    if uid == admin.id:
        raise HTTPException(400, "You can't remove your own account")
    u = s.get(models.User, uid)
    if not u: raise HTTPException(404)
    # return their leads to the shared pool so nothing is orphaned
    for l in s.query(models.Lead).filter(models.Lead.assigned_to == u.name).all():
        l.assigned_to = ""
    s.query(models.Session).filter(models.Session.user_id == uid).delete()
    s.delete(u); s.commit()
    return {"ok": True}

# ---------- state ----------
@app.get("/api/state")
def get_state(s: Session = Depends(db), user=Depends(require_user)):
    lq = s.query(models.Lead)
    if not user.is_admin:
        lq = lq.filter((models.Lead.assigned_to == user.name) | (models.Lead.assigned_to == "") | (models.Lead.assigned_to == None))
    leads = [l.to_dict() for l in lq.all()]
    events = [e.to_dict() for e in s.query(models.Event).filter(models.Event.user_id == user.id).all()]
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
    _labels = {"reached":"Texted","replied":"They replied","scheduled":"Booked appointment","showed":"Showed up","sold":"Sold PT","dead":"Marked not interested","noresp":"No response","new":"Reset to New"}
    _txt = _labels.get(body.stage, "Stage updated")
    if body.stage == "sold" and body.saleValue:
        _txt = "Sold PT ($" + str(int(body.saleValue)) + ")"
    _h = list(lead.history or []); _h.insert(0, {"at": now().isoformat(), "text": _txt, "by": user.name})
    lead.history = _h
    if body.stage == "sold" and body.saleValue is not None:
        lead.sale_value = body.saleValue
    if body.stage != prev and body.stage in STAGE_EVENT:
        amt = body.saleValue if (body.stage == "sold" and body.saleValue is not None) else 0
        s.add(models.Event(type=STAGE_EVENT[body.stage], at=now(), amount=amt or 0, user_id=user.id))
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.post("/api/leads/{lead_id}/contact")
def log_contact(lead_id: str, s: Session = Depends(db), user=Depends(require_user)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    check_access(lead, user)
    lead.last_contact = now()
    lead.next_follow_up = ""
    hist = list(lead.history or []); hist.insert(0, {"at": now().isoformat(), "text": "Logged a contact", "by": user.name})
    lead.history = hist
    s.commit(); s.refresh(lead)
    return lead.to_dict()

@app.post("/api/leads/{lead_id}/note")
def add_note(lead_id: str, body: NoteIn, s: Session = Depends(db), user=Depends(require_user)):
    lead = s.get(models.Lead, lead_id)
    if not lead: raise HTTPException(404)
    check_access(lead, user)
    if body.text.strip():
        hist = list(lead.history or []); hist.insert(0, {"at": now().isoformat(), "text": body.text.strip(), "by": user.name})
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
    if body.type is not None: lead.lead_type = body.type
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
        low = line.lower()
        if "kickoff" not in low and "sga" not in low and not reEmail.search(line):
            continue
        ltype = "sga" if "sga" in low else "kickoff"
        em = reEmail.search(line); email = em.group(0) if em else ""
        ph = rePhone.search(line); phone = _norm_phone(ph.group(0)) if ph else ""
        if not email and not phone:
            continue
        nm = reName.search(line); first = last = ""
        if nm: last, first = nm.group(1), nm.group(2)
        if not first and email: first = email
        dates = reDate.findall(line); expire = dates[1] if len(dates) >= 2 else ""
        pm = rePend.search(line); pend = pm.group(1) if pm else ""
        if reActive.search(line):
            skipped_active += 1
            continue  # already has an active recurring service -> skip
        out.append({"firstName": first, "lastName": last, "phone": phone, "email": email,
                    "pending": pend, "expire": expire, "type": ltype,
                    "source": "SGA report" if ltype == "sga" else "Kickoff report"})
    return out, skipped_active

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
        if _re.search(r"event|appointment type|appt type|^type$|kind", s): return "etype"
        if _re.search(r"recurring|active service|current service|agreement|has pt|active pt", s): return "recurring"
        if "expir" in s: return "expire"
        return None
    header = [maph(x) for x in rows[0]]
    has = any(header)
    data = rows[1:] if has else rows
    mapping = header if has else ["first", "last", "phone", "email"]
    out = []
    skipped_active = 0
    for r in data:
        rec = {"first": "", "last": "", "phone": "", "email": "", "source": "Imported", "expire": "", "etype": "", "recurring": ""}
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
        rv = (rec.get("recurring") or "").strip().lower()
        if rv and rv not in ("no", "none", "0", "n", "false", "inactive", "na", "n/a", "-"):
            skipped_active += 1
            continue  # already has an active recurring service -> skip
        lt = "sga" if "sga" in (rec.get("etype") or "").lower() else "kickoff"
        out.append({"firstName": rec["first"], "lastName": rec["last"], "phone": rec["phone"],
                    "email": rec["email"], "source": rec["source"] or "Imported",
                    "expire": rec["expire"], "type": lt})
    return out, skipped_active

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
async def import_file(file: UploadFile = File(...), force_type: str = Form(""), s: Session = Depends(db), user=Depends(require_user)):
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
            leads, skipped_active = parse_report_text(text)
        elif fname.endswith(".csv"):
            import csv
            rows = list(csv.reader(_io.StringIO(content.decode("utf-8", "ignore"))))
            leads, skipped_active = parse_rows(rows)
        elif fname.endswith(".xlsx") or fname.endswith(".xls"):
            import openpyxl
            wb = openpyxl.load_workbook(_io.BytesIO(content), read_only=True, data_only=True)
            rows = [list(r) for r in wb.active.iter_rows(values_only=True)]
            leads, skipped_active = parse_rows(rows)
        else:
            raise HTTPException(400, "Unsupported file type. Use PDF, CSV, or Excel.")
        skipped_active = locals().get("skipped_active", 0)
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "Could not read that file. Try CSV, or paste the text instead.")
    ft = (force_type or "").strip().lower()
    if ft in ("kickoff", "sga"):
        for d in leads:
            d["type"] = ft
            d["source"] = "SGA report" if ft == "sga" else d.get("source") or "Kickoff report"
    kicks = sum(1 for d in leads if (d.get("type") or "kickoff") == "kickoff")
    sgas = sum(1 for d in leads if (d.get("type") or "kickoff") == "sga")
    added, skipped = insert_leads(s, leads)
    return {"added": added, "skipped": skipped, "parsed": len(leads), "kickoffs": kicks, "sgas": sgas, "skippedActive": skipped_active}

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
_NOCACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache"}

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse(INDEX_HTML, headers=_NOCACHE)

@app.get("/intake", response_class=HTMLResponse)
@app.get("/intake.html", response_class=HTMLResponse)
def intake_page():
    return HTMLResponse(INTAKE_HTML, headers=_NOCACHE)

@app.get("/health")
def health():
    return {"ok": True, "version": APP_VERSION}
