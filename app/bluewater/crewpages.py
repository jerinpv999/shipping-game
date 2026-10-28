"""Web pages for seafarers (/crew) and for companies managing their crew (/portal/crew)."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crew as CR
from . import services as S
from .admin import flash, templates
from .db import get_db
from .models import Company, CrewMember, JobApplication, Ship, User
from .security import require_company, require_seafarer

router = APIRouter()
templates.env.globals.update(POSITIONS=CR.POSITIONS, LICENCES=CR.LICENCES)


def page(request: Request, name: str, user, **ctx):
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "flashes": flashes, "path": request.url.path, **ctx})


def run(request, db, fn, ok):
    try:
        out = fn(); db.commit()
        if ok: flash(request, ok)
        return out
    except S.ServiceError as e:
        db.rollback(); flash(request, str(e), "error")


# ------------------------------------------------------------- seafarers
@router.get("/crew/register", response_class=HTMLResponse)
def register_page(request: Request):
    return page(request, "crew_register.html", None, form=request.session.pop("crew_form", {}))


@router.post("/crew/register")
def register(request: Request, username: str = Form(...), full_name: str = Form(...), email: str = Form(...),
             password: str = Form(...), licence: str = Form(""), experience_years: int = Form(0), db: Session = Depends(get_db)):
    try:
        u = CR.register_seafarer(db, username=username, full_name=full_name, email=email, password=password,
                                 licence=licence, experience_years=experience_years)
        db.commit()
    except S.ServiceError as e:
        db.rollback(); flash(request, str(e), "error")
        request.session["crew_form"] = {"username": username, "full_name": full_name, "email": email, "licence": licence, "experience_years": experience_years}
        return RedirectResponse("/crew/register", status_code=303)
    request.session["uid"] = u.id
    flash(request, "Welcome aboard, " + full_name.split(" ")[0] + ". Apply to a shipping company to get a ship to command.")
    return RedirectResponse("/crew", status_code=303)


@router.get("/crew", response_class=HTMLResponse)
def crew_home(request: Request, db: Session = Depends(get_db), user: User = Depends(require_seafarer)):
    m = CR.membership(db, user)
    apps = db.scalars(select(JobApplication).where(JobApplication.user_id == user.id).order_by(JobApplication.id.desc())).all()
    pending = {a.company_id for a in apps if a.status == "pending"}
    companies = db.scalars(select(Company).where(Company.status == "approved").order_by(Company.name)).all()
    return page(request, "crew_home.html", user, member=m, apps=apps, companies=companies, pending=pending,
                ships=CR.controllable_ships(db, user))


@router.post("/crew/apply")
def crew_apply(request: Request, company_id: int = Form(...), position: str = Form("captain"), message: str = Form(""),
               db: Session = Depends(get_db), user: User = Depends(require_seafarer)):
    co = db.get(Company, company_id)
    if co:
        run(request, db, lambda: CR.apply(db, user, co, position, message), f"Application sent to {co.name}.")
    return RedirectResponse("/crew", status_code=303)


@router.post("/crew/withdraw/{aid}")
def crew_withdraw(request: Request, aid: int, db: Session = Depends(get_db), user: User = Depends(require_seafarer)):
    a = db.get(JobApplication, aid)
    if a and a.user_id == user.id and a.status == "pending":
        a.status = "withdrawn"; db.commit(); flash(request, "Application withdrawn.")
    return RedirectResponse("/crew", status_code=303)


@router.post("/crew/resign")
def crew_resign(request: Request, confirm: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_seafarer)):
    if confirm != "yes":
        flash(request, "Tick the box to confirm your resignation.", "error")
        return RedirectResponse("/crew", status_code=303)
    run(request, db, lambda: CR.resign(db, user), "")
    flash(request, "You've resigned. Apply to another company to get a new command.")
    return RedirectResponse("/crew", status_code=303)


@router.get("/crew/logout")
def crew_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/portal/login", status_code=303)


# ------------------------------------------------------------- companies: crew management
@router.get("/portal/crew", response_class=HTMLResponse)
def company_crew(request: Request, db: Session = Depends(get_db), user: User = Depends(require_company)):
    co = db.get(Company, user.company_id)
    apps = db.scalars(select(JobApplication).where(JobApplication.company_id == co.id).order_by(JobApplication.status.desc(), JobApplication.id.desc())).all()
    members = db.scalars(select(CrewMember).where(CrewMember.company_id == co.id, CrewMember.status == "active").order_by(CrewMember.id)).all()
    employers = {}
    for x in apps:
        if x.status == "pending":
            cur = CR.membership(db, x.user)
            if cur and cur.company_id != co.id:
                employers[x.user_id] = cur.company.name
    former = db.scalars(select(CrewMember).where(CrewMember.company_id == co.id, CrewMember.status == "removed").order_by(CrewMember.id.desc()).limit(20)).all()
    for m in former:
        nxt = CR.membership(db, m.user)
        m.left_reason = ("Moved to " + nxt.company.name) if nxt else "Left the company"
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, "portal_crew.html", {"user": user, "company": co, "flashes": flashes, "path": request.url.path,
                                                                     "apps": apps, "members": members, "ships": [s for s in co.ships],
                                                                     "employers": employers, "former": former})


@router.post("/portal/crew/applications/{aid}")
def decide(request: Request, aid: int, decision: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_company)):
    co, a = db.get(Company, user.company_id), db.get(JobApplication, aid)
    if a:
        run(request, db, lambda: CR.decide(db, a, co, decision == "accept", user.username),
            (a.user.full_name + (" joined your crew." if decision == "accept" else " was declined.")))
    return RedirectResponse("/portal/crew", status_code=303)


@router.post("/portal/crew/add")
def add_staff(request: Request, username: str = Form(...), full_name: str = Form(...), password: str = Form(...),
              position: str = Form("captain"), licence: str = Form(""), ship_id: str = Form(""),
              db: Session = Depends(get_db), user: User = Depends(require_company)):
    co = db.get(Company, user.company_id)

    def go():
        m = CR.add_staff(db, co, username=username, full_name=full_name, password=password, position=position, licence=licence, actor=user.username)
        db.flush()
        if ship_id.strip():
            CR.assign_ship(db, co, m, int(ship_id), user.username)
    run(request, db, go, f"{full_name} added. They sign in to the game as '{username.strip().lower()}'.")
    return RedirectResponse("/portal/crew", status_code=303)


@router.post("/portal/crew/{mid}/assign")
def assign(request: Request, mid: int, ship_id: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_company)):
    co, m = db.get(Company, user.company_id), db.get(CrewMember, mid)
    if m:
        sid = int(ship_id) if ship_id else None
        run(request, db, lambda: CR.assign_ship(db, co, m, sid, user.username), "Assignment saved.")
    return RedirectResponse("/portal/crew", status_code=303)


@router.post("/portal/crew/{mid}/remove")
def remove(request: Request, mid: int, db: Session = Depends(get_db), user: User = Depends(require_company)):
    co, m = db.get(Company, user.company_id), db.get(CrewMember, mid)
    if m:
        run(request, db, lambda: CR.remove_member(db, co, m, user.username), m.user.full_name + " left the crew.")
    return RedirectResponse("/portal/crew", status_code=303)
