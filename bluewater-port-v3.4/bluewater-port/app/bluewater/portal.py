"""Company portal: self-registration, fleet purchases and the cargo transit board."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import marketplace as MK
from . import services as S
from .admin import flash, templates
from .db import get_db
from .models import Company, LedgerEntry, TransitRequest, User
from .security import current_user, require_company, verify_password

router = APIRouter(prefix="/portal")
templates.env.globals.update(SHIP_MODELS=MK.SHIP_MODELS, VEHICLE_MODELS=MK.VEHICLE_MODELS, OPENING_BALANCE=MK.OPENING_BALANCE,
                             PORT_DUES=MK.PORT_DUES)


def ctx_company(db: Session, user: User) -> Company:
    return db.get(Company, user.company_id)


def render(request: Request, name: str, user: User | None, db: Session | None = None, **ctx):
    flashes = request.session.pop("flash", [])
    company = ctx_company(db, user) if (db and user) else None
    return templates.TemplateResponse(request, name, {"user": user, "company": company, "flashes": flashes,
                                                      "path": request.url.path, **ctx})


def run(request: Request, db: Session, fn, ok_msg: str):
    try:
        out = fn()
        db.commit()
        if ok_msg:
            flash(request, ok_msg)
        return out
    except S.ServiceError as e:
        db.rollback()
        flash(request, str(e), "error")
    return None


# ------------------------------------------------------------- auth
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(current_user)):
    if user and user.role == "company":
        return RedirectResponse("/portal", status_code=303)
    if user and user.role == "seafarer":
        return RedirectResponse("/crew", status_code=303)
    if user and user.role in ("pirate", "marine"):
        return RedirectResponse("/sea", status_code=303)
    return render(request, "portal_login.html", None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == username.strip().lower()))
    if not u or not u.active or not verify_password(password, u.password_hash):
        flash(request, "Wrong username or password.", "error")
        return RedirectResponse("/portal/login", status_code=303)
    if u.role not in ("company", "seafarer", "pirate", "marine"):
        flash(request, "Port authority staff sign in at the admin panel.", "error")
        return RedirectResponse("/portal/login", status_code=303)
    request.session["uid"] = u.id
    return RedirectResponse({"company": "/portal", "seafarer": "/crew"}.get(u.role, "/sea"), status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/portal/login", status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request):
    return render(request, "portal_register.html", None, form=request.session.pop("reg_form", {}))


@router.post("/register")
def register(request: Request, username: str = Form(...), full_name: str = Form(...), email: str = Form(...),
             password: str = Form(...), company_name: str = Form(...), company_type: str = Form(...),
             country: str = Form(""), phone: str = Form(""), db: Session = Depends(get_db)):
    try:
        user = MK.register_company(db, username=username, full_name=full_name, email=email, password=password,
                                   company_name=company_name, company_type=company_type, country=country, phone=phone)
        db.commit()
    except S.ServiceError as e:
        db.rollback()
        flash(request, str(e), "error")
        request.session["reg_form"] = {"username": username, "full_name": full_name, "email": email,
                                       "company_name": company_name, "company_type": company_type,
                                       "country": country, "phone": phone}
        return RedirectResponse("/portal/register", status_code=303)
    request.session["uid"] = user.id
    flash(request, f"Welcome aboard. {company_name} has ${MK.OPENING_BALANCE:,.0f} to build a fleet. "
                   "The port authority will review your registration.")
    return RedirectResponse("/portal", status_code=303)


# ------------------------------------------------------------- pages
@router.get("", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db), user: User = Depends(require_company)):
    co = ctx_company(db, user)
    jobs = db.scalars(select(TransitRequest).where(TransitRequest.company_id == co.id,
                                                   TransitRequest.status.in_(MK.OPEN_STATES))
                      .order_by(TransitRequest.accepted_at)).all()
    done = db.scalars(select(TransitRequest).where(TransitRequest.company_id == co.id, TransitRequest.status == "delivered")).all()
    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.company_id == co.id).order_by(LedgerEntry.id.desc()).limit(8)).all()
    open_count = sum(1 for r in db.scalars(select(TransitRequest).where(TransitRequest.status == "open")).all() if not MK.is_expired(r))
    earned = sum(r.rate for r in done)
    return render(request, "portal_home.html", user, db, jobs=jobs, done=done, entries=entries, open_count=open_count,
                  earned=earned)


@router.get("/market", response_class=HTMLResponse)
def market(request: Request, db: Session = Depends(get_db), user: User = Depends(require_company)):
    return render(request, "portal_market.html", user, db)


@router.post("/market/ship")
def buy_ship(request: Request, model: str = Form(...), name: str = Form(...), flag: str = Form(""), plan: str = Form(""),
             db: Session = Depends(get_db), user: User = Depends(require_company)):
    from . import insurance as INS
    co = ctx_company(db, user)

    def go():
        ship = MK.buy_ship(db, co, model, name, flag, user.username)
        if plan in INS.PLANS:
            INS.buy_policy(db, co, ship, plan, user.username)
    run(request, db, go, f"Purchased {name}" + (f" with {INS.PLANS[plan]['label'].lower()} insurance" if plan in INS.PLANS else "") +
        ". It will sail once the port authority approves its registration.")
    return RedirectResponse("/portal/fleet", status_code=303)


@router.post("/market/vehicle")
def buy_vehicle(request: Request, model: str = Form(...), plate: str = Form(...),
                db: Session = Depends(get_db), user: User = Depends(require_company)):
    co = ctx_company(db, user)
    run(request, db, lambda: MK.buy_vehicle(db, co, model, plate, user.username),
        f"Purchased {plate.upper()}. It can pass the port gates once the authority approves it.")
    return RedirectResponse("/portal/fleet", status_code=303)


@router.get("/fleet", response_class=HTMLResponse)
def fleet(request: Request, db: Session = Depends(get_db), user: User = Depends(require_company)):
    co = ctx_company(db, user)
    active = db.scalars(select(TransitRequest).where(TransitRequest.company_id == co.id,
                                                     TransitRequest.status.in_(MK.OPEN_STATES))).all()
    job_by_ship = {r.ship_id: r for r in active if r.ship_id}
    job_by_vehicle = {r.vehicle_id: r for r in active if r.vehicle_id}
    from . import insurance as INS
    from .models import InsuranceClaim
    if INS.settle_due(db):
        db.commit()
    policies = {s.id: INS.active_policy(db, s) for s in co.ships}
    claims_by_ship = {}
    for c in db.scalars(select(InsuranceClaim).where(InsuranceClaim.company_id == co.id)).all():
        claims_by_ship.setdefault(c.ship_id, []).append(c)
    db.commit()
    return render(request, "portal_fleet.html", user, db, job_by_ship=job_by_ship, job_by_vehicle=job_by_vehicle,
                  policies=policies, claims_by_ship=claims_by_ship, replacement_name=lambda s: INS.replacement_name(db, s))


@router.get("/requests", response_class=HTMLResponse)
def requests_board(request: Request, mode: str = "", cargo: str = "", fit: str = "", db: Session = Depends(get_db),
                   user: User = Depends(require_company)):
    co = ctx_company(db, user)
    stmt = select(TransitRequest).where(TransitRequest.status == "open").order_by(TransitRequest.deadline)
    if mode in ("sea", "road"):
        stmt = stmt.where(TransitRequest.mode == mode)
    if cargo in MK.CARGO_UNIT:
        stmt = stmt.where(TransitRequest.cargo_type == cargo)
    rows = [r for r in db.scalars(stmt).all() if not MK.is_expired(r)]
    options = {r.id: MK.eligible_assets(db, co, r) for r in rows}
    if fit:
        rows = [r for r in rows if options[r.id]]
    mine = db.scalars(select(TransitRequest).where(TransitRequest.company_id == co.id)
                      .order_by(TransitRequest.id.desc()).limit(50)).all()
    return render(request, "portal_requests.html", user, db, rows=rows, options=options, mine=mine, mode=mode,
                  cargo=cargo, fit=fit)


@router.post("/requests/{rid}/{action}")
def request_action(request: Request, rid: int, action: str, asset_id: int | None = Form(None),
                   db: Session = Depends(get_db), user: User = Depends(require_company)):
    co, r = ctx_company(db, user), db.get(TransitRequest, rid)
    if not r:
        flash(request, "Request not found.", "error")
        return RedirectResponse("/portal/requests", status_code=303)
    fns = {
        "accept": (lambda: MK.accept_request(db, r, co, asset_id or 0, user.username), f"Accepted {r.ref}. Start it when you're ready to load."),
        "start": (lambda: MK.start_request(db, r, co, user.username), f"{r.ref} is under way."),
        "deliver": (lambda: MK.deliver_request(db, r, co, user.username), f"{r.ref} delivered. Freight credited after {MK.PORT_DUES:.0%} port dues."),
        "release": (lambda: MK.release_request(db, r, co, user.username), f"{r.ref} handed back to the board."),
    }
    if action in fns:
        fn, msg = fns[action]
        run(request, db, fn, msg)
    return RedirectResponse(request.headers.get("referer") or "/portal/requests", status_code=303)


@router.get("/ledger", response_class=HTMLResponse)
def ledger(request: Request, db: Session = Depends(get_db), user: User = Depends(require_company)):
    co = ctx_company(db, user)
    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.company_id == co.id).order_by(LedgerEntry.id.desc())).all()
    totals = {}
    for e in entries:
        totals[e.kind] = totals.get(e.kind, 0) + e.amount
    return render(request, "portal_ledger.html", user, db, entries=entries, totals=totals)


@router.get("/company", response_class=HTMLResponse)
def company_page(request: Request, db: Session = Depends(get_db), user: User = Depends(require_company)):
    return render(request, "portal_company.html", user, db)


@router.post("/company")
def company_update(request: Request, company_type: str = Form(...), country: str = Form(""), phone: str = Form(""),
                   contact_email: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_company)):
    co = ctx_company(db, user)
    if company_type in ("shipping_line", "haulier", "both"):
        co.company_type, co.country, co.phone, co.contact_email = company_type, country, phone, contact_email
        S.audit(db, user.username, "update", "company", co.reg_no)
        db.commit()
        flash(request, "Company details saved.")
    return RedirectResponse("/portal/company", status_code=303)
