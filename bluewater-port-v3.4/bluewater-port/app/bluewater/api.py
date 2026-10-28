"""JSON REST API (documented at /docs)."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services as S
from .db import get_db
from .models import (Container, CustomsDeclaration, GameEvent, GateTransaction, Industry, Port, Ship, StockMovement,
                     SupplyOrder, User, Vehicle, VesselCall, Warehouse)
from .security import api_user, require_area

router = APIRouter(prefix="/api")


def fail(e: Exception):
    raise HTTPException(status_code=400, detail=str(e))


def get_or_404(db, model, id_):
    obj = db.get(model, id_)
    if not obj:
        raise HTTPException(404, f"{model.__name__} {id_} not found")
    return obj


# ------------------------------------------------------------- health & game
@router.get("/health")
def health():
    from .config import VERSION
    return {"status": "ok", "service": "bluewater", "version": VERSION}


class GameEventIn(BaseModel):
    type: str = Field(max_length=40)
    data: dict = {}


@router.post("/game/events", tags=["game"])
def game_event(ev: GameEventIn, db: Session = Depends(get_db)):
    try:
        S.ingest_game_event(db, ev.type, ev.data)
        db.commit()
    except S.ServiceError as e:
        db.rollback()
        return {"ok": False, "detail": str(e)}
    return {"ok": True}


@router.post("/game/snapshot", tags=["game"])
def game_snapshot(data: dict, db: Session = Depends(get_db)):
    S.ingest_game_snapshot(db, data)
    db.commit()
    return {"ok": True}


@router.get("/game/events", tags=["game"])
def list_game_events(limit: int = 50, db: Session = Depends(get_db), user: User = Depends(api_user)):
    rows = db.scalars(select(GameEvent).order_by(GameEvent.id.desc()).limit(min(limit, 500))).all()
    return [{"id": r.id, "type": r.event_type, "payload": r.payload, "at": r.at} for r in rows]


# ------------------------------------------------------------- serializers
def ship_out(s: Ship):
    return {"id": s.id, "imo": s.imo, "name": s.name, "type": s.ship_type, "flag": s.flag, "owner": s.owner,
            "capacity": s.capacity, "unit": s.capacity_unit, "status": s.status,
            "current_port": s.current_port.name if s.current_port else None, "live_status": s.live_status}


def industry_out(i: Industry):
    return {"id": i.id, "reg_no": i.reg_no, "name": i.name, "sector": i.sector, "goods": i.goods, "hs_code": i.hs_code,
            "port": i.port.name, "status": i.status, "live": {"ready": i.live_ready, "stock": i.live_stock, "status": i.live_status}}


def vehicle_out(v: Vehicle):
    return {"id": v.id, "plate": v.plate, "type": v.vehicle_type, "haulier": v.haulier, "trailers": v.trailers,
            "max_containers": v.max_containers, "status": v.status, "inside_port": v.inside_port.name if v.inside_port else None}


def container_out(c: Container):
    return {"number": c.number, "size": c.size, "status": c.status, "customs_status": c.customs_status,
            "order": c.order.order_no if c.order else None, "warehouse": c.warehouse.code if c.warehouse else None,
            "ship": c.ship.name if c.ship else None, "vehicle": c.vehicle.plate if c.vehicle else None,
            "gross_weight_kg": c.gross_weight_kg}


def order_out(o: SupplyOrder):
    return {"id": o.id, "order_no": o.order_no, "status": o.status, "shipper": o.shipper.name, "consignee": o.consignee.name,
            "origin": o.origin_port.name, "destination": o.destination_port.name, "goods": o.goods, "hs_code": o.hs_code,
            "containers_planned": o.containers_planned, "value_per_container": o.value_per_container,
            "ship": o.ship.name if o.ship else None, "containers": [c.number for c in o.containers]}


def decl_out(d: CustomsDeclaration):
    return {"id": d.id, "decl_no": d.decl_no, "type": d.decl_type, "order": d.order.order_no, "port": d.port.name,
            "hs_code": d.hs_code, "declared_value": d.declared_value, "duty_rate": d.duty_rate,
            "duty_amount": d.duty_amount, "status": d.status, "officer": d.officer, "notes": d.notes,
            "submitted_at": d.submitted_at, "decided_at": d.decided_at}


# ------------------------------------------------------------- registry
class ShipIn(BaseModel):
    imo: str = Field(pattern=r"^\d{7}$")
    name: str
    ship_type: Literal["container", "crude", "lng"]
    flag: str = ""
    owner: str = ""
    capacity: float = Field(gt=0)


UNIT = {"container": "TEU", "crude": "bbl", "lng": "m³"}


@router.get("/ships", tags=["registry"])
def ships(db: Session = Depends(get_db), user: User = Depends(api_user)):
    return [ship_out(s) for s in db.scalars(select(Ship).order_by(Ship.name)).all()]


@router.post("/ships", tags=["registry"], status_code=201)
def register_ship(body: ShipIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "registry")
    from .marketplace import imo_check_digit
    if imo_check_digit(body.imo[:6]) != int(body.imo[6]):
        fail(S.ServiceError(f"IMO {body.imo} fails the check digit"))
    if db.scalar(select(Ship).where(Ship.imo == body.imo)):
        fail(S.ServiceError(f"IMO {body.imo} is already registered"))
    s = Ship(**body.model_dump(), capacity_unit=UNIT[body.ship_type], status="pending")
    db.add(s)
    S.audit(db, user.username, "register", "ship", body.imo, body.name)
    db.commit()
    return ship_out(s)


class StatusIn(BaseModel):
    status: Literal["pending", "approved", "suspended"]


@router.patch("/ships/{ship_id}/status", tags=["registry"])
def ship_status(ship_id: int, body: StatusIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "registry")
    s = get_or_404(db, Ship, ship_id)
    s.status = body.status
    S.audit(db, user.username, body.status, "ship", s.imo)
    db.commit()
    return ship_out(s)


class IndustryIn(BaseModel):
    name: str
    sector: str
    goods: str
    hs_code: str = Field(pattern=r"^\d{4,8}$")
    port_id: int
    contact: str = ""


@router.get("/industries", tags=["registry"])
def industries(db: Session = Depends(get_db), user: User = Depends(api_user)):
    return [industry_out(i) for i in db.scalars(select(Industry).order_by(Industry.name)).all()]


@router.post("/industries", tags=["registry"], status_code=201)
def register_industry(body: IndustryIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "registry")
    get_or_404(db, Port, body.port_id)
    i = Industry(reg_no=S.next_ref(db, Industry, "reg_no", "IND"), status="pending", **body.model_dump())
    db.add(i)
    S.audit(db, user.username, "register", "industry", i.reg_no, body.name)
    db.commit()
    return industry_out(i)


@router.patch("/industries/{ind_id}/status", tags=["registry"])
def industry_status(ind_id: int, body: StatusIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "registry")
    i = get_or_404(db, Industry, ind_id)
    i.status = body.status
    S.audit(db, user.username, body.status, "industry", i.reg_no)
    db.commit()
    return industry_out(i)


class VehicleIn(BaseModel):
    plate: str = Field(min_length=3, max_length=20)
    vehicle_type: Literal["road_train", "truck", "tanker_truck"] = "road_train"
    haulier: str = ""
    trailers: int = Field(default=2, ge=0, le=4)
    max_containers: int = Field(default=2, ge=0, le=4)


@router.get("/vehicles", tags=["registry"])
def vehicles(db: Session = Depends(get_db), user: User = Depends(api_user)):
    return [vehicle_out(v) for v in db.scalars(select(Vehicle).order_by(Vehicle.plate)).all()]


@router.post("/vehicles", tags=["registry"], status_code=201)
def register_vehicle(body: VehicleIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "registry")
    plate = body.plate.upper().strip()
    if db.scalar(select(Vehicle).where(Vehicle.plate == plate)):
        fail(S.ServiceError(f"{plate} is already registered"))
    v = Vehicle(**{**body.model_dump(), "plate": plate})
    db.add(v)
    S.audit(db, user.username, "register", "vehicle", plate)
    db.commit()
    return vehicle_out(v)


@router.get("/ports", tags=["registry"])
def ports(db: Session = Depends(get_db), user: User = Depends(api_user)):
    return [{"id": p.id, "code": p.code, "name": p.name, "island": p.island, "terminal": p.terminal_type, "role": p.role}
            for p in db.scalars(select(Port).order_by(Port.id)).all()]


# ------------------------------------------------------------- warehouse
@router.get("/warehouses", tags=["warehouse"])
def warehouses(db: Session = Depends(get_db), user: User = Depends(api_user)):
    out = []
    for w in db.scalars(select(Warehouse).order_by(Warehouse.id)).all():
        row = {"id": w.id, "code": w.code, "name": w.name, "port": w.port.name, "kind": w.kind, "capacity": w.capacity,
               "unit": w.unit, "live_level": w.live_level}
        row["booked"] = S.yard_occupancy(db, w) if w.kind == "container_yard" else {"level": w.bulk_level}
        out.append(row)
    return out


@router.get("/warehouses/{wh_id}/movements", tags=["warehouse"])
def movements(wh_id: int, limit: int = 100, db: Session = Depends(get_db), user: User = Depends(api_user)):
    get_or_404(db, Warehouse, wh_id)
    rows = db.scalars(select(StockMovement).where(StockMovement.warehouse_id == wh_id)
                      .order_by(StockMovement.id.desc()).limit(min(limit, 1000))).all()
    return [{"at": m.at, "direction": m.direction, "quantity": m.quantity, "container": m.container.number if m.container else None,
             "source": m.source, "reference": m.reference, "by": m.created_by} for m in rows]


class BulkIn(BaseModel):
    direction: Literal["in", "out"]
    quantity: float = Field(gt=0)
    reference: str = ""


@router.post("/warehouses/{wh_id}/bulk", tags=["warehouse"])
def bulk(wh_id: int, body: BulkIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "warehouse")
    w = get_or_404(db, Warehouse, wh_id)
    try:
        S.bulk_movement(db, w, body.direction, body.quantity, body.reference, user.username)
    except S.ServiceError as e:
        fail(e)
    db.commit()
    return {"level": w.bulk_level, "capacity": w.capacity, "unit": w.unit}


@router.get("/containers", tags=["warehouse"])
def containers(status: str | None = None, db: Session = Depends(get_db), user: User = Depends(api_user)):
    q = select(Container).order_by(Container.id.desc())
    if status:
        q = q.where(Container.status == status)
    return [container_out(c) for c in db.scalars(q.limit(500)).all()]


@router.get("/containers/{number}", tags=["warehouse"])
def container(number: str, db: Session = Depends(get_db), user: User = Depends(api_user)):
    c = db.scalar(select(Container).where(Container.number == number.upper()))
    if not c:
        raise HTTPException(404, "Container not found")
    moves = db.scalars(select(StockMovement).where(StockMovement.container_id == c.id).order_by(StockMovement.id)).all()
    return {**container_out(c), "history": [{"at": m.at, "warehouse": m.warehouse.code, "direction": m.direction,
                                             "reference": m.reference} for m in moves]}


# ------------------------------------------------------------- supply orders
class OrderIn(BaseModel):
    shipper_id: int
    consignee_id: int
    containers_planned: int = Field(ge=1, le=40)
    value_per_container: float = Field(gt=0)


@router.get("/orders", tags=["supply chain"])
def orders(db: Session = Depends(get_db), user: User = Depends(api_user)):
    return [order_out(o) for o in db.scalars(select(SupplyOrder).order_by(SupplyOrder.id.desc())).all()]


@router.post("/orders", tags=["supply chain"], status_code=201)
def create_order(body: OrderIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "orders")
    sh, co = get_or_404(db, Industry, body.shipper_id), get_or_404(db, Industry, body.consignee_id)
    if sh.port_id == co.port_id:
        fail(S.ServiceError("Shipper and consignee are on the same island; no sea leg needed"))
    o = SupplyOrder(order_no=S.next_ref(db, SupplyOrder, "order_no", "ORD"), shipper_id=sh.id, consignee_id=co.id,
                    origin_port_id=sh.port_id, destination_port_id=co.port_id, goods=sh.goods, hs_code=sh.hs_code,
                    containers_planned=body.containers_planned, value_per_container=body.value_per_container)
    db.add(o)
    S.audit(db, user.username, "create", "order", o.order_no)
    db.commit()
    db.refresh(o)
    return order_out(o)


class OrderAction(BaseModel):
    ship_id: int | None = None


@router.post("/orders/{order_id}/{action}", tags=["supply chain"])
def order_action(order_id: int, action: Literal["confirm", "stuff", "export-declaration", "load", "discharge", "deliver"],
                 body: OrderAction | None = None, db: Session = Depends(get_db), user: User = Depends(api_user)):
    o = get_or_404(db, SupplyOrder, order_id)
    area = {"export-declaration": "orders", "load": "vessel", "discharge": "vessel"}.get(action, "orders")
    require_area(user, area)
    try:
        if action == "confirm":
            S.confirm_order(db, o, user.username)
        elif action == "stuff":
            S.stuff_containers(db, o, user.username)
        elif action == "export-declaration":
            S.submit_export_declaration(db, o, user.username)
        elif action == "load":
            if not body or not body.ship_id:
                raise S.ServiceError("ship_id is required")
            S.load_order_on_ship(db, o, get_or_404(db, Ship, body.ship_id), user.username)
        elif action == "discharge":
            S.discharge_order(db, o, user.username)
        elif action == "deliver":
            S.confirm_delivery(db, o, user.username)
        db.commit()
    except S.ServiceError as e:
        db.rollback()
        fail(e)
    db.refresh(o)
    return order_out(o)


# ------------------------------------------------------------- vessels
class BerthIn(BaseModel):
    port_id: int


@router.post("/ships/{ship_id}/berth", tags=["vessels"])
def berth(ship_id: int, body: BerthIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "vessel")
    try:
        S.berth_ship(db, get_or_404(db, Ship, ship_id), get_or_404(db, Port, body.port_id), user.username)
        db.commit()
    except S.ServiceError as e:
        db.rollback()
        fail(e)
    return {"ok": True}


@router.post("/ships/{ship_id}/depart", tags=["vessels"])
def depart(ship_id: int, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "vessel")
    S.depart_ship(db, get_or_404(db, Ship, ship_id), user.username)
    db.commit()
    return {"ok": True}


@router.get("/vessel-calls", tags=["vessels"])
def vessel_calls(db: Session = Depends(get_db), user: User = Depends(api_user)):
    rows = db.scalars(select(VesselCall).order_by(VesselCall.id.desc()).limit(200)).all()
    return [{"ship": r.ship.name, "port": r.port.name, "status": r.status, "operation": r.operation, "quantity": r.quantity,
             "arrived_at": r.arrived_at, "departed_at": r.departed_at, "source": r.source} for r in rows]


# ------------------------------------------------------------- gate
class GateIn(BaseModel):
    port_id: int
    plate: str
    direction: Literal["in", "out"]
    containers: list[str] = []
    purpose: str = ""


@router.post("/gate", tags=["gate"], status_code=201)
def gate(body: GateIn, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "gate")
    port = get_or_404(db, Port, body.port_id)
    v = db.scalar(select(Vehicle).where(Vehicle.plate == body.plate.upper().strip()))
    if not v:
        fail(S.ServiceError(f"Vehicle {body.plate} is not registered"))
    try:
        fn = S.gate_in if body.direction == "in" else S.gate_out
        tx = fn(db, port, v, body.containers, user.username, body.purpose)
        db.commit()
    except S.ServiceError as e:
        db.rollback()
        fail(e)
    return {"id": tx.id, "direction": tx.direction, "containers": tx.containers.split(",") if tx.containers else []}


@router.get("/gate", tags=["gate"])
def gate_log(limit: int = 100, db: Session = Depends(get_db), user: User = Depends(api_user)):
    rows = db.scalars(select(GateTransaction).order_by(GateTransaction.id.desc()).limit(min(limit, 1000))).all()
    return [{"at": g.at, "port": g.port.name, "vehicle": g.vehicle.plate, "direction": g.direction,
             "containers": g.containers, "count": g.container_count, "purpose": g.purpose, "clerk": g.clerk, "source": g.source}
            for g in rows]


# ------------------------------------------------------------- customs
@router.get("/customs", tags=["customs"])
def customs(status: str | None = None, db: Session = Depends(get_db), user: User = Depends(api_user)):
    q = select(CustomsDeclaration).order_by(CustomsDeclaration.id.desc())
    if status:
        q = q.where(CustomsDeclaration.status == status)
    return [decl_out(d) for d in db.scalars(q).all()]


class CustomsActionIn(BaseModel):
    notes: str = ""


@router.post("/customs/{decl_id}/{action}", tags=["customs"])
def customs_action(decl_id: int, action: Literal["review", "inspect", "hold", "clear", "reject"],
                   body: CustomsActionIn | None = None, db: Session = Depends(get_db), user: User = Depends(api_user)):
    require_area(user, "customs")
    d = get_or_404(db, CustomsDeclaration, decl_id)
    try:
        S.customs_action(db, d, action, user.username, body.notes if body else "")
        db.commit()
    except S.ServiceError as e:
        db.rollback()
        fail(e)
    return decl_out(d)


@router.get("/dashboard", tags=["reports"])
def dashboard(db: Session = Depends(get_db), user: User = Depends(api_user)):
    d = S.dashboard(db)
    return {k: v for k, v in d.items() if k not in ("yards", "recent_audit", "recent_events")} | {
        "yards": [{"code": y["wh"].code, "name": y["wh"].name, "kind": y["wh"].kind, "capacity": y["wh"].capacity,
                   "unit": y["wh"].unit, "booked": y["booked"], "live": y["live"]} for y in d["yards"]]}
