"""Server-rendered admin panel."""
from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from . import services as S
from .db import get_db
from .models import (AuditLog, Company, LedgerEntry, TransitRequest, Container, CustomsDeclaration, GameEvent, GameState, GateTransaction, Industry, Port,
                     Ship, StockMovement, SupplyOrder, User, Vehicle, VesselCall, Warehouse)
from .security import PERMISSIONS, ROLES, can, current_user, hash_password, require_user, verify_password

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
from .config import VERSION
templates.env.globals.update(VERSION=VERSION, ORDER_LABELS=S.ORDER_LABELS, CONTAINER_LABELS=S.CONTAINER_LABELS, ORDER_FLOW=S.ORDER_FLOW,
                             DUTY_BY_CHAPTER=S.DUTY_BY_CHAPTER, ROLES=ROLES, can=can)
templates.env.filters["num"] = lambda v, d=0: f"{(v or 0):,.{d}f}"
templates.env.filters["dt"] = lambda v: v.strftime("%d %b %H:%M") if v else "—"
templates.env.filters["money"] = lambda v: f"${(v or 0):,.0f}"


def _qty(r):
    q = r.quantity or 0
    unit = "box" if r.unit == "boxes" and q == 1 else r.unit
    return f"{q:,.0f} {unit}"


templates.env.filters["qty"] = _qty


def render(request: Request, name: str, user: User | None, **ctx):
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "flashes": flashes, "path": request.url.path, **ctx})


def flash(request: Request, msg: str, kind: str = "ok"):
    request.session.setdefault("flash", []).append({"msg": msg, "kind": kind})


def back(request: Request, fallback: str):
    ref = request.headers.get("referer", "")
    return RedirectResponse(ref if "/admin" in ref else fallback, status_code=303)


def guard(request: Request, user: User, area: str) -> bool:
    if can(user, area):
        return True
    flash(request, f"Your role ({user.role}) cannot change {area} records.", "error")
    return False


def run(request: Request, db: Session, fn, ok_msg: str):
    try:
        result = fn()
        db.commit()
        if ok_msg:
            flash(request, ok_msg)
        return result
    except S.ServiceError as e:
        db.rollback()
        flash(request, str(e), "error")
    return None


def ports_list(db):
    return db.scalars(select(Port).order_by(Port.id)).all()


# ------------------------------------------------------------- auth
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, user: User | None = Depends(current_user)):
    if user:
        return RedirectResponse("/admin", status_code=303)
    return render(request, "login.html", None)


@router.post("/login")
def login(request: Request, username: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == username.strip()))
    if not u or not u.active or not verify_password(password, u.password_hash):
        flash(request, "Wrong username or password.", "error")
        return RedirectResponse("/admin/login", status_code=303)
    if u.role == "company":
        flash(request, "Company accounts sign in at the company portal (/portal).", "error")
        return RedirectResponse("/admin/login", status_code=303)
    request.session["uid"] = u.id
    S.audit(db, u.username, "login", "user", u.username)
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/admin/login", status_code=303)


# ------------------------------------------------------------- dashboard
@router.get("", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    d = S.dashboard(db)
    live = db.scalar(select(GameState).where(GameState.key == "latest"))
    return render(request, "dashboard.html", user, d=d, live=live)


# ------------------------------------------------------------- ships
@router.get("/ships", response_class=HTMLResponse)
def ships(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.scalars(select(Ship).order_by(Ship.status, Ship.name)).all()
    on_board = dict(db.execute(select(Container.ship_id, func.count()).where(Container.status == "on_ship")
                               .group_by(Container.ship_id)).all())
    return render(request, "ships.html", user, ships=rows, ports=ports_list(db), on_board=on_board)


@router.post("/ships")
def ship_register(request: Request, imo: str = Form(...), name: str = Form(...), ship_type: str = Form(...),
                  flag: str = Form(""), owner: str = Form(""), capacity: float = Form(...),
                  db: Session = Depends(get_db), user: User = Depends(require_user)):
    if not guard(request, user, "registry"):
        return back(request, "/admin/ships")
    imo = imo.strip()

    def go():
        if not (imo.isdigit() and len(imo) == 7):
            raise S.ServiceError("IMO number must be 7 digits")
        from .marketplace import imo_check_digit
        if imo_check_digit(imo[:6]) != int(imo[6]):
            raise S.ServiceError(f"IMO {imo} fails the check digit; the last digit should be {imo_check_digit(imo[:6])}")
        if ship_type not in ("container", "crude", "lng"):
            raise S.ServiceError("Choose a ship type")
        if db.scalar(select(Ship).where(Ship.imo == imo)):
            raise S.ServiceError(f"IMO {imo} is already registered")
        db.add(Ship(imo=imo, name=name.strip(), ship_type=ship_type, flag=flag, owner=owner, capacity=capacity,
                    capacity_unit={"container": "TEU", "crude": "bbl", "lng": "m³"}[ship_type], status="pending"))
        S.audit(db, user.username, "register", "ship", imo, name)
    run(request, db, go, f"{name} registered. Approve it before it can berth.")
    return RedirectResponse("/admin/ships", status_code=303)


@router.post("/ships/{ship_id}/status")
def ship_status(request: Request, ship_id: int, status: str = Form(...), db: Session = Depends(get_db),
                user: User = Depends(require_user)):
    if guard(request, user, "registry"):
        s = db.get(Ship, ship_id)
        if s and status in ("pending", "approved", "suspended"):
            s.status = status
            S.audit(db, user.username, status, "ship", s.imo)
            db.commit()
            flash(request, f"{s.name} is now {status}.")
    return back(request, "/admin/ships")


@router.post("/ships/{ship_id}/berth")
def ship_berth(request: Request, ship_id: int, port_id: int = Form(...), db: Session = Depends(get_db),
               user: User = Depends(require_user)):
    if guard(request, user, "vessel"):
        s, p = db.get(Ship, ship_id), db.get(Port, port_id)
        run(request, db, lambda: S.berth_ship(db, s, p, user.username), f"{s.name} berthed at {p.name}.")
    return back(request, "/admin/ships")


@router.post("/ships/{ship_id}/depart")
def ship_depart(request: Request, ship_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "vessel"):
        s = db.get(Ship, ship_id)
        run(request, db, lambda: S.depart_ship(db, s, user.username), f"{s.name} departed.")
    return back(request, "/admin/ships")


@router.get("/vessels", response_class=HTMLResponse)
def vessels(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.scalars(select(VesselCall).order_by(VesselCall.id.desc()).limit(200)).all()
    return render(request, "vessels.html", user, calls=rows)


# ------------------------------------------------------------- industries
@router.get("/industries", response_class=HTMLResponse)
def industries(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.scalars(select(Industry).order_by(Industry.status, Industry.name)).all()
    ports = [p for p in ports_list(db) if p.terminal_type == "container"]
    return render(request, "industries.html", user, industries=rows, ports=ports)


@router.post("/industries")
def industry_register(request: Request, name: str = Form(...), sector: str = Form(...), goods: str = Form(...),
                      hs_code: str = Form(...), port_id: int = Form(...), contact: str = Form(""),
                      db: Session = Depends(get_db), user: User = Depends(require_user)):
    if not guard(request, user, "registry"):
        return back(request, "/admin/industries")

    def go():
        if not (hs_code.isdigit() and 4 <= len(hs_code) <= 8):
            raise S.ServiceError("HS code must be 4 to 8 digits")
        i = Industry(reg_no=S.next_ref(db, Industry, "reg_no", "IND"), name=name.strip(), sector=sector, goods=goods,
                     hs_code=hs_code, port_id=port_id, contact=contact, status="pending")
        db.add(i)
        S.audit(db, user.username, "register", "industry", i.reg_no, name)
    run(request, db, go, f"{name} registered and waiting for approval.")
    return RedirectResponse("/admin/industries", status_code=303)


@router.post("/industries/{ind_id}/status")
def industry_status(request: Request, ind_id: int, status: str = Form(...), db: Session = Depends(get_db),
                    user: User = Depends(require_user)):
    if guard(request, user, "registry"):
        i = db.get(Industry, ind_id)
        if i and status in ("pending", "approved", "suspended"):
            i.status = status
            S.audit(db, user.username, status, "industry", i.reg_no)
            db.commit()
            flash(request, f"{i.name} is now {status}.")
    return back(request, "/admin/industries")


# ------------------------------------------------------------- vehicles
@router.get("/vehicles", response_class=HTMLResponse)
def vehicles(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.scalars(select(Vehicle).order_by(Vehicle.plate)).all()
    last = {}
    for g in db.scalars(select(GateTransaction).order_by(GateTransaction.id.desc()).limit(500)).all():
        last.setdefault(g.vehicle_id, g)
    return render(request, "vehicles.html", user, vehicles=rows, last=last)


@router.post("/vehicles")
def vehicle_register(request: Request, plate: str = Form(...), vehicle_type: str = Form(...), haulier: str = Form(""),
                     trailers: int = Form(2), max_containers: int = Form(2), db: Session = Depends(get_db),
                     user: User = Depends(require_user)):
    if not guard(request, user, "registry"):
        return back(request, "/admin/vehicles")
    plate = plate.upper().strip()

    def go():
        if db.scalar(select(Vehicle).where(Vehicle.plate == plate)):
            raise S.ServiceError(f"{plate} is already registered")
        db.add(Vehicle(plate=plate, vehicle_type=vehicle_type, haulier=haulier, trailers=trailers,
                       max_containers=max_containers))
        S.audit(db, user.username, "register", "vehicle", plate)
    run(request, db, go, f"{plate} registered.")
    return RedirectResponse("/admin/vehicles", status_code=303)


@router.post("/vehicles/{vid}/status")
def vehicle_status(request: Request, vid: int, status: str = Form(...), db: Session = Depends(get_db),
                   user: User = Depends(require_user)):
    if guard(request, user, "registry"):
        v = db.get(Vehicle, vid)
        if v and status in ("pending", "active", "blocked"):
            v.status = status
            S.audit(db, user.username, status, "vehicle", v.plate)
            db.commit()
            flash(request, f"{v.plate} is now {status}.")
    return back(request, "/admin/vehicles")


# ------------------------------------------------------------- warehouses & containers
@router.get("/warehouses", response_class=HTMLResponse)
def warehouses(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    d = S.dashboard(db)
    return render(request, "warehouses.html", user, yards=d["yards"])


@router.get("/warehouses/{wh_id}", response_class=HTMLResponse)
def warehouse_detail(request: Request, wh_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    wh = db.get(Warehouse, wh_id)
    if not wh:
        return RedirectResponse("/admin/warehouses", status_code=303)
    boxes = db.scalars(select(Container).where(Container.warehouse_id == wh.id, Container.status.in_(S.YARD_STATUSES))
                       .order_by(Container.status, Container.number)).all()
    moves = db.scalars(select(StockMovement).where(StockMovement.warehouse_id == wh.id)
                       .order_by(StockMovement.id.desc()).limit(80)).all()
    occ = S.yard_occupancy(db, wh) if wh.kind == "container_yard" else None
    return render(request, "warehouse_detail.html", user, wh=wh, boxes=boxes, moves=moves, occ=occ)


@router.post("/warehouses/{wh_id}/bulk")
def warehouse_bulk(request: Request, wh_id: int, direction: str = Form(...), quantity: float = Form(...),
                   reference: str = Form(""), db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "warehouse"):
        wh = db.get(Warehouse, wh_id)
        run(request, db, lambda: S.bulk_movement(db, wh, direction, quantity, reference, user.username),
            f"Recorded {direction} of {quantity:,.0f} {wh.unit}.")
    return back(request, f"/admin/warehouses/{wh_id}")


@router.get("/containers", response_class=HTMLResponse)
def containers(request: Request, status: str = "", q: str = "", db: Session = Depends(get_db),
               user: User = Depends(require_user)):
    stmt = select(Container).order_by(Container.updated_at.desc())
    if status:
        stmt = stmt.where(Container.status == status)
    if q:
        stmt = stmt.where(Container.number.ilike(f"%{q.strip().upper()}%"))
    counts = dict(db.execute(select(Container.status, func.count()).group_by(Container.status)).all())
    return render(request, "containers.html", user, boxes=db.scalars(stmt.limit(300)).all(), status=status, q=q, counts=counts)


@router.get("/containers/{number}", response_class=HTMLResponse)
def container_detail(request: Request, number: str, db: Session = Depends(get_db), user: User = Depends(require_user)):
    c = db.scalar(select(Container).where(Container.number == number.upper()))
    if not c:
        flash(request, f"No container {number}.", "error")
        return RedirectResponse("/admin/containers", status_code=303)
    moves = db.scalars(select(StockMovement).where(StockMovement.container_id == c.id).order_by(StockMovement.id)).all()
    gates = db.scalars(select(GateTransaction).where(GateTransaction.containers.like(f"%{c.number}%"))
                       .order_by(GateTransaction.id)).all()
    return render(request, "container_detail.html", user, c=c, moves=moves, gates=gates)


# ------------------------------------------------------------- supply orders
@router.get("/orders", response_class=HTMLResponse)
def orders(request: Request, status: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    stmt = select(SupplyOrder).order_by(SupplyOrder.id.desc())
    if status:
        stmt = stmt.where(SupplyOrder.status == status)
    inds = db.scalars(select(Industry).where(Industry.status == "approved").order_by(Industry.name)).all()
    counts = dict(db.execute(select(SupplyOrder.status, func.count()).group_by(SupplyOrder.status)).all())
    return render(request, "orders.html", user, orders=db.scalars(stmt).all(), industries=inds, status=status, counts=counts)


@router.post("/orders")
def order_create(request: Request, shipper_id: int = Form(...), consignee_id: int = Form(...),
                 containers_planned: int = Form(...), value_per_container: float = Form(...),
                 db: Session = Depends(get_db), user: User = Depends(require_user)):
    if not guard(request, user, "orders"):
        return back(request, "/admin/orders")

    def go():
        sh, co = db.get(Industry, shipper_id), db.get(Industry, consignee_id)
        if not sh or not co:
            raise S.ServiceError("Choose a shipper and a consignee")
        if sh.port_id == co.port_id:
            raise S.ServiceError("Shipper and consignee are on the same island")
        if not 1 <= containers_planned <= 40:
            raise S.ServiceError("Containers must be between 1 and 40")
        o = SupplyOrder(order_no=S.next_ref(db, SupplyOrder, "order_no", "ORD"), shipper_id=sh.id, consignee_id=co.id,
                        origin_port_id=sh.port_id, destination_port_id=co.port_id, goods=sh.goods, hs_code=sh.hs_code,
                        containers_planned=containers_planned, value_per_container=value_per_container)
        db.add(o)
        S.audit(db, user.username, "create", "order", o.order_no)
        db.flush()
        return o
    o = run(request, db, go, "Order created.")
    return RedirectResponse(f"/admin/orders/{o.id}" if o else "/admin/orders", status_code=303)


@router.get("/orders/{order_id}", response_class=HTMLResponse)
def order_detail(request: Request, order_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    o = db.get(SupplyOrder, order_id)
    if not o:
        return RedirectResponse("/admin/orders", status_code=303)
    decls = db.scalars(select(CustomsDeclaration).where(CustomsDeclaration.order_id == o.id).order_by(CustomsDeclaration.id)).all()
    ships_here = db.scalars(select(Ship).where(Ship.current_port_id == o.origin_port_id, Ship.ship_type == "container",
                                               Ship.status == "approved")).all()
    log = db.scalars(select(AuditLog).where(AuditLog.entity_ref == o.order_no).order_by(AuditLog.id)).all()
    return render(request, "order_detail.html", user, o=o, decls=decls, ships_here=ships_here, log=log)


@router.post("/orders/{order_id}/action")
def order_action(request: Request, order_id: int, action: str = Form(...), ship_id: int | None = Form(None),
                 db: Session = Depends(get_db), user: User = Depends(require_user)):
    o = db.get(SupplyOrder, order_id)
    area = "vessel" if action in ("load", "discharge") else "orders"
    if o and guard(request, user, area):
        fns = {
            "confirm": (lambda: S.confirm_order(db, o, user.username), "Order confirmed."),
            "stuff": (lambda: S.stuff_containers(db, o, user.username), "Containers stuffed at the factory."),
            "export": (lambda: S.submit_export_declaration(db, o, user.username), "Export declaration submitted to customs."),
            "load": (lambda: S.load_order_on_ship(db, o, db.get(Ship, ship_id or 0) or _no_ship(), user.username), "Loaded on vessel."),
            "discharge": (lambda: S.discharge_order(db, o, user.username), "Discharged. Import declaration created."),
            "deliver": (lambda: S.confirm_delivery(db, o, user.username), "Delivery confirmed."),
        }
        if action in fns:
            fn, msg = fns[action]
            run(request, db, fn, msg)
    return RedirectResponse(f"/admin/orders/{order_id}", status_code=303)


def _no_ship():
    raise S.ServiceError("Choose a vessel")


# ------------------------------------------------------------- gate
@router.get("/gate", response_class=HTMLResponse)
def gate(request: Request, port_id: int | None = None, db: Session = Depends(get_db), user: User = Depends(require_user)):
    ports = [p for p in ports_list(db) if p.terminal_type == "container"]
    port = db.get(Port, port_id) if port_id else ports[0]
    stmt = select(GateTransaction).where(GateTransaction.port_id == port.id).order_by(GateTransaction.id.desc()).limit(60)
    waiting_in = db.scalars(select(Container).join(SupplyOrder).where(Container.status == "at_industry",
                                                                       SupplyOrder.origin_port_id == port.id)).all()
    wh = S.yard_for_port(db, port.id)
    ready_out = db.scalars(select(Container).where(Container.warehouse_id == wh.id, Container.status == "in_yard_inbound",
                                                   Container.customs_status == "import_cleared")).all()
    inside = db.scalars(select(Vehicle).where(Vehicle.inside_port_id == port.id)).all()
    vehicles = db.scalars(select(Vehicle).where(Vehicle.status == "active").order_by(Vehicle.plate)).all()
    return render(request, "gate.html", user, ports=ports, port=port, txs=db.scalars(stmt).all(), waiting_in=waiting_in,
                  ready_out=ready_out, inside=inside, vehicles=vehicles)


@router.post("/gate")
def gate_post(request: Request, port_id: int = Form(...), plate: str = Form(...), direction: str = Form(...),
              containers: list[str] = Form([]), purpose: str = Form(""), db: Session = Depends(get_db),
              user: User = Depends(require_user)):
    if guard(request, user, "gate"):
        port = db.get(Port, port_id)
        v = db.scalar(select(Vehicle).where(Vehicle.plate == plate.upper().strip()))
        if not v:
            flash(request, f"{plate} is not a registered vehicle.", "error")
        else:
            fn = S.gate_in if direction == "in" else S.gate_out
            run(request, db, lambda: fn(db, port, v, containers, user.username, purpose),
                f"{v.plate} gate-{direction} recorded with {len(containers)} container(s).")
    return RedirectResponse(f"/admin/gate?port_id={port_id}", status_code=303)


# ------------------------------------------------------------- customs
@router.get("/customs", response_class=HTMLResponse)
def customs(request: Request, status: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    stmt = select(CustomsDeclaration).order_by(CustomsDeclaration.id.desc())
    if status == "open":
        stmt = stmt.where(CustomsDeclaration.status.in_(["submitted", "under_review", "inspection", "held"]))
    elif status:
        stmt = stmt.where(CustomsDeclaration.status == status)
    counts = dict(db.execute(select(CustomsDeclaration.status, func.count()).group_by(CustomsDeclaration.status)).all())
    return render(request, "customs.html", user, decls=db.scalars(stmt).all(), status=status, counts=counts)


@router.get("/customs/{decl_id}", response_class=HTMLResponse)
def customs_detail(request: Request, decl_id: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    d = db.get(CustomsDeclaration, decl_id)
    if not d:
        return RedirectResponse("/admin/customs", status_code=303)
    log = db.scalars(select(AuditLog).where(AuditLog.entity_ref == d.decl_no).order_by(AuditLog.id)).all()
    return render(request, "customs_detail.html", user, d=d, log=log, actions=S.CUSTOMS_ACTIONS)


@router.post("/customs/{decl_id}/action")
def customs_post(request: Request, decl_id: int, action: str = Form(...), notes: str = Form(""),
                 db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "customs"):
        d = db.get(CustomsDeclaration, decl_id)
        run(request, db, lambda: S.customs_action(db, d, action, user.username, notes), f"Declaration {action} recorded.")
    return RedirectResponse(f"/admin/customs/{decl_id}", status_code=303)


# ------------------------------------------------------------- live game, audit, users
@router.get("/live", response_class=HTMLResponse)
def live(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    st = db.scalar(select(GameState).where(GameState.key == "latest"))
    events = db.scalars(select(GameEvent).order_by(GameEvent.id.desc()).limit(60)).all()
    whs = db.scalars(select(Warehouse).order_by(Warehouse.id)).all()
    inds = db.scalars(select(Industry).where(Industry.game_id.isnot(None)).order_by(Industry.port_id, Industry.name)).all()
    ships = db.scalars(select(Ship).where(Ship.game_id.isnot(None))).all()
    return render(request, "live.html", user, st=st, events=events, whs=whs, inds=inds, ships=ships)


@router.get("/audit", response_class=HTMLResponse)
def audit(request: Request, q: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    stmt = select(AuditLog).order_by(AuditLog.id.desc()).limit(300)
    if q:
        stmt = stmt.where(or_(AuditLog.entity_ref.ilike(f"%{q}%"), AuditLog.actor.ilike(f"%{q}%"), AuditLog.action.ilike(f"%{q}%")))
    return render(request, "audit.html", user, rows=db.scalars(stmt).all(), q=q)


@router.get("/users", response_class=HTMLResponse)
def users(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    return render(request, "users.html", user, users=db.scalars(select(User).order_by(User.username)).all(), perms=PERMISSIONS)


@router.post("/users")
def user_create(request: Request, username: str = Form(...), full_name: str = Form(""), role: str = Form(...),
                password: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "users"):
        def go():
            if role not in ROLES:
                raise S.ServiceError("Unknown role")
            if len(password) < 8:
                raise S.ServiceError("Password must be at least 8 characters")
            if db.scalar(select(User).where(User.username == username.strip())):
                raise S.ServiceError("That username is taken")
            db.add(User(username=username.strip(), full_name=full_name, role=role, password_hash=hash_password(password)))
            S.audit(db, user.username, "create", "user", username)
        run(request, db, go, f"User {username} created.")
    return RedirectResponse("/admin/users", status_code=303)


@router.post("/users/{uid}/toggle")
def user_toggle(request: Request, uid: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "users"):
        u = db.get(User, uid)
        if u and u.id != user.id:
            u.active = not u.active
            S.audit(db, user.username, "enable" if u.active else "disable", "user", u.username)
            db.commit()
            flash(request, f"{u.username} {'enabled' if u.active else 'disabled'}.")
    return RedirectResponse("/admin/users", status_code=303)


# ------------------------------------------------------------- companies (portal accounts)
@router.get("/companies", response_class=HTMLResponse)
def companies(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    rows = db.scalars(select(Company).order_by(Company.status, Company.name)).all()
    jobs = dict(db.execute(select(TransitRequest.company_id, func.count()).where(TransitRequest.status == "delivered")
                           .group_by(TransitRequest.company_id)).all())
    return render(request, "companies.html", user, companies=rows, jobs=jobs)


@router.get("/companies/{cid}", response_class=HTMLResponse)
def company_detail(request: Request, cid: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    co = db.get(Company, cid)
    if not co:
        return RedirectResponse("/admin/companies", status_code=303)
    entries = db.scalars(select(LedgerEntry).where(LedgerEntry.company_id == cid).order_by(LedgerEntry.id.desc()).limit(60)).all()
    reqs = db.scalars(select(TransitRequest).where(TransitRequest.company_id == cid).order_by(TransitRequest.id.desc()).limit(60)).all()
    return render(request, "company_detail.html", user, co=co, entries=entries, reqs=reqs)


@router.post("/companies/{cid}/status")
def company_status(request: Request, cid: int, status: str = Form(...), db: Session = Depends(get_db),
                   user: User = Depends(require_user)):
    if guard(request, user, "companies"):
        co = db.get(Company, cid)
        if co and status in ("pending", "approved", "suspended"):
            co.status = status
            for u in co.users:
                u.active = status != "suspended"
            S.audit(db, user.username, status, "company", co.reg_no)
            db.commit()
            flash(request, f"{co.name} is now {status}.")
    return back(request, "/admin/companies")


@router.post("/companies/{cid}/adjust")
def company_adjust(request: Request, cid: int, amount: float = Form(...), reason: str = Form(...),
                   db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "companies"):
        from . import marketplace as MK
        co = db.get(Company, cid)
        if co and amount:
            MK.post_ledger(db, co, amount, "adjustment", reason[:200])
            S.audit(db, user.username, "adjust_balance", "company", co.reg_no, f"{amount:+,.0f} {reason}")
            db.commit()
            flash(request, f"Balance adjusted by ${amount:+,.0f}.")
    return back(request, f"/admin/companies/{cid}")


# ------------------------------------------------------------- transit requests
@router.get("/transit", response_class=HTMLResponse)
def transit(request: Request, status: str = "", db: Session = Depends(get_db), user: User = Depends(require_user)):
    from . import marketplace as MK
    stmt = select(TransitRequest).order_by(TransitRequest.id.desc())
    if status:
        stmt = stmt.where(TransitRequest.status == status)
    counts = dict(db.execute(select(TransitRequest.status, func.count()).group_by(TransitRequest.status)).all())
    return render(request, "transit.html", user, rows=db.scalars(stmt.limit(300)).all(), status=status, counts=counts,
                  ports=ports_list(db), industries=db.scalars(select(Industry).where(Industry.status == "approved")).all(),
                  is_expired=MK.is_expired, lanes=MK.pending_lanes(db), min_boxes=MK.CONSOLIDATE_MIN)


@router.post("/transit/consolidate")
def transit_consolidate(request: Request, origin_id: int = Form(...), destination_id: int = Form(...),
                        db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "transit"):
        from . import marketplace as MK
        run(request, db, lambda: MK.consolidate(db, db.get(Port, origin_id), db.get(Port, destination_id), force=True, posted_by=user.username),
            "Ship booked for the route. The consolidated request is on the company board.")
    return RedirectResponse("/admin/transit", status_code=303)


@router.post("/transit")
def transit_post(request: Request, mode: str = Form(...), cargo_type: str = Form(...), origin: str = Form(...),
                 destination: str = Form(...), quantity: float = Form(...), rate: float = Form(...),
                 days: float = Form(3), description: str = Form(""), db: Session = Depends(get_db),
                 user: User = Depends(require_user)):
    if guard(request, user, "transit"):
        from . import marketplace as MK
        ports = {p.name: p for p in ports_list(db)}
        run(request, db, lambda: MK.post_request(db, mode=mode, cargo_type=cargo_type, origin=origin, destination=destination,
                                                quantity=quantity, rate=rate, days=days, description=description,
                                                origin_port=ports.get(origin), destination_port=ports.get(destination),
                                                posted_by=user.username), "Transit request posted to the company board.")
    return RedirectResponse("/admin/transit", status_code=303)


@router.post("/transit/bulk")
def transit_bulk(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "transit"):
        from . import marketplace as MK
        run(request, db, lambda: MK.post_bulk_market(db, user.username), "Posted crude and LNG charters.")
    return RedirectResponse("/admin/transit", status_code=303)


@router.post("/transit/{rid}/cancel")
def transit_cancel(request: Request, rid: int, db: Session = Depends(get_db), user: User = Depends(require_user)):
    if guard(request, user, "transit"):
        from . import marketplace as MK
        r = db.get(TransitRequest, rid)
        run(request, db, lambda: MK.cancel_request(db, r, user.username), f"{r.ref} cancelled.")
    return back(request, "/admin/transit")
