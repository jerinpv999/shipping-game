"""Business rules for the port: container lifecycle, gate, warehouse, customs and game sync."""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import (AuditLog, Container, CustomsDeclaration, GameEvent, GameState, GateTransaction, Industry, Port,
                     Ship, StockMovement, SupplyOrder, Vehicle, VesselCall, Warehouse, now)


class ServiceError(Exception):
    pass


ORDER_FLOW = ["draft", "confirmed", "stuffed", "at_origin_yard", "export_customs", "export_cleared", "shipped",
              "import_customs", "import_cleared", "out_for_delivery", "delivered"]
ORDER_LABELS = {
    "draft": "Draft", "confirmed": "Confirmed", "stuffed": "Stuffed at factory", "at_origin_yard": "In origin yard",
    "export_customs": "Export customs", "export_cleared": "Export cleared", "shipped": "On vessel",
    "import_customs": "Import customs", "import_cleared": "Import cleared", "out_for_delivery": "Out for delivery",
    "delivered": "Delivered", "customs_held": "Customs hold", "customs_rejected": "Customs rejected",
}
CONTAINER_LABELS = {
    "at_industry": "At factory", "in_yard_outbound": "Outbound yard", "on_ship": "On vessel",
    "in_yard_inbound": "Inbound yard", "on_road": "On road", "delivered": "Delivered",
}
YARD_STATUSES = ("in_yard_outbound", "in_yard_inbound")

# Simplified tariff by HS chapter (first two digits) for import duty.
DUTY_BY_CHAPTER = {
    "85": (0.05, "Electrical machinery & electronics"),
    "84": (0.04, "Machinery"),
    "61": (0.12, "Knitted apparel"),
    "62": (0.12, "Woven apparel"),
    "87": (0.075, "Vehicles & parts"),
    "16": (0.10, "Prepared meat & fish"),
    "03": (0.10, "Fish & seafood"),
    "20": (0.09, "Prepared vegetables & fruit"),
    "94": (0.08, "Furniture"),
    "39": (0.06, "Plastics"),
    "95": (0.04, "Toys & sports goods"),
}
DEFAULT_DUTY = 0.06


def duty_rate(hs_code: str) -> float:
    return DUTY_BY_CHAPTER.get((hs_code or "")[:2], (DEFAULT_DUTY, ""))[0]


def audit(db: Session, actor: str, action: str, entity: str, ref: str = "", detail: str = "") -> None:
    db.add(AuditLog(actor=actor, action=action, entity=entity, entity_ref=ref, detail=detail))


# ---------------------------------------------------------------- numbering
ISO_VALUES = {}
_v = 10
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    if _v % 11 == 0:
        _v += 1
    ISO_VALUES[_c] = _v
    _v += 1


def iso6346_check_digit(code10: str) -> int:
    """Check digit for the first 10 characters of an ISO 6346 container number."""
    total = 0
    for i, ch in enumerate(code10):
        val = int(ch) if ch.isdigit() else ISO_VALUES[ch]
        total += val * (2 ** i)
    return total % 11 % 10


def new_container_number(db: Session, owner: str = "BWF") -> str:
    for _ in range(50):
        serial = f"{random.randint(0, 999999):06d}"
        base = f"{owner}U{serial}"
        number = base + str(iso6346_check_digit(base))
        if not db.scalar(select(Container.id).where(Container.number == number)):
            return number
    raise ServiceError("Could not allocate a container number")


def next_ref(db: Session, model, field: str, prefix: str) -> str:
    year = datetime.now(timezone.utc).year
    count = db.scalar(select(func.count()).select_from(model)) or 0
    n = count + 1
    while True:
        ref = f"{prefix}-{year}-{n:05d}"
        if not db.scalar(select(model.id).where(getattr(model, field) == ref)):
            return ref
        n += 1


# ---------------------------------------------------------------- lookups
def yard_for_port(db: Session, port_id: int) -> Warehouse:
    wh = db.scalar(select(Warehouse).where(Warehouse.port_id == port_id, Warehouse.kind == "container_yard"))
    if not wh:
        raise ServiceError("This port has no container yard")
    return wh


def yard_occupancy(db: Session, wh: Warehouse) -> dict:
    rows = db.execute(select(Container.status, func.count()).where(Container.warehouse_id == wh.id,
                                                                    Container.status.in_(YARD_STATUSES))
                      .group_by(Container.status)).all()
    d = {s: c for s, c in rows}
    inbound, outbound = d.get("in_yard_inbound", 0), d.get("in_yard_outbound", 0)
    return {"inbound": inbound, "outbound": outbound, "total": inbound + outbound,
            "pct": round((inbound + outbound) / wh.capacity * 100, 1) if wh.capacity else 0}


def find_containers(db: Session, numbers: list[str]) -> list[Container]:
    clean = [n.strip().upper() for n in numbers if n.strip()]
    if not clean:
        return []
    found = db.scalars(select(Container).where(Container.number.in_(clean))).all()
    missing = set(clean) - {c.number for c in found}
    if missing:
        raise ServiceError("Unknown container number(s): " + ", ".join(sorted(missing)))
    return list(found)


def _move(db, wh, container, direction, source, reference, actor, qty=1.0):
    db.add(StockMovement(warehouse_id=wh.id, container_id=container.id if container else None, direction=direction,
                         quantity=qty, source=source, reference=reference, created_by=actor))


def _sync_order(db: Session, order: SupplyOrder) -> None:
    """Advance an order's status from the state of its containers."""
    statuses = {c.status for c in order.containers}
    if not statuses:
        return
    if statuses == {"delivered"}:
        order.status = "delivered"
    elif statuses <= {"on_road", "delivered"}:
        order.status = "out_for_delivery"
    elif statuses == {"in_yard_outbound"} and order.status in ("stuffed", "confirmed"):
        order.status = "at_origin_yard"


# ---------------------------------------------------------------- orders
def confirm_order(db: Session, order: SupplyOrder, actor: str) -> None:
    if order.status != "draft":
        raise ServiceError("Only draft orders can be confirmed")
    if order.shipper.status != "approved" or order.consignee.status != "approved":
        raise ServiceError("Shipper and consignee must both be approved industries")
    order.status = "confirmed"
    audit(db, actor, "confirm", "order", order.order_no)


def stuff_containers(db: Session, order: SupplyOrder, actor: str) -> list[Container]:
    if order.status != "confirmed":
        raise ServiceError("Confirm the order before stuffing containers")
    made = []
    for _ in range(order.containers_planned):
        c = Container(number=new_container_number(db), status="at_industry",
                      gross_weight_kg=random.randint(12000, 26000))
        order.containers.append(c)
        db.add(c)
        db.flush()
        made.append(c)
    order.status = "stuffed"
    db.flush()
    from . import marketplace
    marketplace.requests_for_order(db, order, "pickup")
    audit(db, actor, "stuff", "order", order.order_no, f"{len(made)} containers")
    return made


# ---------------------------------------------------------------- gate
def gate_in(db: Session, port: Port, vehicle: Vehicle, numbers: list[str], actor: str, purpose: str = "",
            source: str = "manual") -> GateTransaction:
    if vehicle.status != "active":
        raise ServiceError(f"Vehicle {vehicle.plate} is blocked at the gate")
    if vehicle.inside_port_id == port.id:
        raise ServiceError(f"{vehicle.plate} is already inside {port.name}. Record its gate-out first")
    containers = find_containers(db, numbers)
    if len(containers) > vehicle.max_containers:
        raise ServiceError(f"{vehicle.plate} can carry at most {vehicle.max_containers} containers")
    if containers:
        wh = yard_for_port(db, port.id)
        occ = yard_occupancy(db, wh)
        if occ["total"] + len(containers) > wh.capacity:
            raise ServiceError(f"{wh.name} is full ({occ['total']}/{int(wh.capacity)})")
        for c in containers:
            if c.status != "at_industry":
                raise ServiceError(f"{c.number} is not waiting at a factory (status: {CONTAINER_LABELS.get(c.status, c.status)})")
            if not c.order or c.order.origin_port_id != port.id:
                raise ServiceError(f"{c.number} is not booked to export through {port.name}")
            c.status, c.warehouse_id, c.vehicle_id = "in_yard_outbound", wh.id, None
            _move(db, wh, c, "in", "gate", f"Gate-in {vehicle.plate}", actor)
        db.flush()
        for o in {c.order for c in containers}:
            _sync_order(db, o)
    vehicle.inside_port_id = port.id
    tx = GateTransaction(port_id=port.id, vehicle_id=vehicle.id, direction="in",
                         containers=",".join(c.number for c in containers), container_count=len(containers),
                         purpose=purpose or ("Export delivery" if containers else "Empty, for pickup"),
                         clerk=actor, source=source)
    db.add(tx)
    audit(db, actor, "gate_in", "vehicle", vehicle.plate, tx.containers)
    return tx


def gate_out(db: Session, port: Port, vehicle: Vehicle, numbers: list[str], actor: str, purpose: str = "",
             source: str = "manual") -> GateTransaction:
    containers = find_containers(db, numbers)
    if len(containers) > vehicle.max_containers:
        raise ServiceError(f"{vehicle.plate} can carry at most {vehicle.max_containers} containers")
    if containers:
        wh = yard_for_port(db, port.id)
        for c in containers:
            if c.status != "in_yard_inbound" or c.warehouse_id != wh.id:
                raise ServiceError(f"{c.number} is not in the inbound yard at {port.name}")
            if c.customs_status != "import_cleared":
                raise ServiceError(f"{c.number} has not cleared import customs")
            c.status, c.warehouse_id, c.vehicle_id = "on_road", None, vehicle.id
            _move(db, wh, c, "out", "gate", f"Gate-out {vehicle.plate}", actor)
        db.flush()
        for o in {c.order for c in containers}:
            _sync_order(db, o)
    vehicle.inside_port_id = None
    tx = GateTransaction(port_id=port.id, vehicle_id=vehicle.id, direction="out",
                         containers=",".join(c.number for c in containers), container_count=len(containers),
                         purpose=purpose or ("Import delivery to consignee" if containers else "Leaving empty"),
                         clerk=actor, source=source)
    db.add(tx)
    audit(db, actor, "gate_out", "vehicle", vehicle.plate, tx.containers)
    return tx


def confirm_delivery(db: Session, order: SupplyOrder, actor: str) -> None:
    road = [c for c in order.containers if c.status == "on_road"]
    if not road:
        raise ServiceError("No containers of this order are on the road")
    for c in road:
        c.status, c.vehicle_id = "delivered", None
    db.flush()
    _sync_order(db, order)
    audit(db, actor, "deliver", "order", order.order_no, f"{len(road)} containers")


# ---------------------------------------------------------------- customs
def submit_export_declaration(db: Session, order: SupplyOrder, actor: str) -> CustomsDeclaration:
    if not order.containers or any(c.status != "in_yard_outbound" for c in order.containers):
        raise ServiceError("All containers must be in the origin yard before export declaration")
    open_decl = db.scalar(select(CustomsDeclaration).where(CustomsDeclaration.order_id == order.id,
                                                           CustomsDeclaration.decl_type == "export",
                                                           CustomsDeclaration.status.notin_(["rejected"])))
    if open_decl:
        raise ServiceError(f"Export declaration {open_decl.decl_no} already exists")
    value = order.value_per_container * len(order.containers)
    decl = CustomsDeclaration(decl_no=next_ref(db, CustomsDeclaration, "decl_no", "EXP"), decl_type="export",
                              order_id=order.id, port_id=order.origin_port_id, hs_code=order.hs_code,
                              declared_value=value, duty_rate=0, duty_amount=0, status="submitted")
    db.add(decl)
    for c in order.containers:
        c.customs_status = "export_pending"
    order.status = "export_customs"
    audit(db, actor, "submit", "customs", decl.decl_no, f"export value {value:,.0f}")
    return decl


def create_import_declaration(db: Session, order: SupplyOrder, actor: str) -> CustomsDeclaration:
    value = order.value_per_container * len(order.containers)
    rate = duty_rate(order.hs_code)
    decl = CustomsDeclaration(decl_no=next_ref(db, CustomsDeclaration, "decl_no", "IMP"), decl_type="import",
                              order_id=order.id, port_id=order.destination_port_id, hs_code=order.hs_code,
                              declared_value=value, duty_rate=rate, duty_amount=round(value * rate, 2),
                              status="submitted")
    db.add(decl)
    order.status = "import_customs"
    audit(db, actor, "submit", "customs", decl.decl_no, f"import value {value:,.0f}, duty {rate:.1%}")
    return decl


CUSTOMS_ACTIONS = {
    "review": ({"submitted"}, "under_review"),
    "inspect": ({"submitted", "under_review", "held"}, "inspection"),
    "hold": ({"submitted", "under_review", "inspection"}, "held"),
    "clear": ({"submitted", "under_review", "inspection", "held"}, "cleared"),
    "reject": ({"submitted", "under_review", "inspection", "held"}, "rejected"),
}


def customs_action(db: Session, decl: CustomsDeclaration, action: str, officer: str, notes: str = "") -> None:
    if action not in CUSTOMS_ACTIONS:
        raise ServiceError("Unknown customs action")
    allowed, new = CUSTOMS_ACTIONS[action]
    if decl.status not in allowed:
        raise ServiceError(f"Cannot {action} a declaration that is {decl.status.replace('_', ' ')}")
    decl.status, decl.officer = new, officer
    if notes:
        decl.notes = (decl.notes + "\n" if decl.notes else "") + f"[{officer}] {notes}"
    order = decl.order
    if new in ("cleared", "rejected"):
        decl.decided_at = now()
    if new == "cleared":
        for c in order.containers:
            c.customs_status = "export_cleared" if decl.decl_type == "export" else "import_cleared"
        order.status = "export_cleared" if decl.decl_type == "export" else "import_cleared"
        from . import marketplace
        marketplace.requests_for_order(db, order, "sea" if decl.decl_type == "export" else "delivery")
    elif new in ("held", "rejected"):
        for c in order.containers:
            c.customs_status = "held"
        order.status = "customs_held" if new == "held" else "customs_rejected"
    elif new in ("under_review", "inspection"):
        for c in order.containers:
            c.customs_status = "export_pending" if decl.decl_type == "export" else "import_pending"
        order.status = "export_customs" if decl.decl_type == "export" else "import_customs"
    audit(db, officer, action, "customs", decl.decl_no, notes)


# ---------------------------------------------------------------- vessels
def berth_ship(db: Session, ship: Ship, port: Port, actor: str, source: str = "manual") -> VesselCall:
    if ship.status != "approved":
        raise ServiceError(f"{ship.name} is not an approved vessel")
    if ship.ship_type != port.terminal_type:
        raise ServiceError(f"{port.name} does not handle {ship.ship_type} vessels")
    open_call = db.scalar(select(VesselCall).where(VesselCall.ship_id == ship.id, VesselCall.status == "berthed"))
    if open_call:
        if open_call.port_id == port.id:
            return open_call
        depart_ship(db, ship, actor, source)
    call = VesselCall(ship_id=ship.id, port_id=port.id, status="berthed", source=source)
    ship.current_port_id = port.id
    db.add(call)
    audit(db, actor, "berth", "ship", ship.imo, port.name)
    return call


def depart_ship(db: Session, ship: Ship, actor: str, source: str = "manual") -> None:
    call = db.scalar(select(VesselCall).where(VesselCall.ship_id == ship.id, VesselCall.status == "berthed"))
    if call:
        call.status, call.departed_at = "departed", now()
    ship.current_port_id = None
    audit(db, actor, "depart", "ship", ship.imo)


def load_order_on_ship(db: Session, order: SupplyOrder, ship: Ship, actor: str) -> None:
    if ship.current_port_id != order.origin_port_id:
        raise ServiceError(f"{ship.name} must be berthed at {order.origin_port.name}")
    if ship.ship_type != "container":
        raise ServiceError("Only container ships can load containers")
    boxes = [c for c in order.containers if c.status == "in_yard_outbound"]
    if not boxes:
        raise ServiceError("No containers of this order are in the outbound yard")
    not_cleared = [c.number for c in boxes if c.customs_status != "export_cleared"]
    if not_cleared:
        raise ServiceError("Export customs not cleared for: " + ", ".join(not_cleared))
    on_board = db.scalar(select(func.count()).select_from(Container).where(Container.ship_id == ship.id,
                                                                          Container.status == "on_ship")) or 0
    if on_board + len(boxes) > ship.capacity:
        raise ServiceError(f"{ship.name} has room for {int(ship.capacity - on_board)} more containers")
    for c in boxes:
        _move(db, c.warehouse, c, "out", "vessel", f"Loaded on {ship.name}", actor)
        c.status, c.ship_id, c.warehouse_id = "on_ship", ship.id, None
    order.ship_id, order.status = ship.id, "shipped"
    audit(db, actor, "load", "order", order.order_no, ship.name)


def discharge_order(db: Session, order: SupplyOrder, actor: str) -> CustomsDeclaration:
    ship = order.ship
    if not ship or ship.current_port_id != order.destination_port_id:
        raise ServiceError(f"The carrying vessel must be berthed at {order.destination_port.name}")
    boxes = [c for c in order.containers if c.status == "on_ship"]
    if not boxes:
        raise ServiceError("No containers of this order are on board")
    wh = yard_for_port(db, order.destination_port_id)
    occ = yard_occupancy(db, wh)
    if occ["total"] + len(boxes) > wh.capacity:
        raise ServiceError(f"{wh.name} has no room for {len(boxes)} more containers")
    for c in boxes:
        c.status, c.ship_id, c.warehouse_id, c.customs_status = "in_yard_inbound", None, wh.id, "import_pending"
        _move(db, wh, c, "in", "vessel", f"Discharged from {ship.name}", actor)
    db.flush()
    decl = create_import_declaration(db, order, actor)
    audit(db, actor, "discharge", "order", order.order_no, ship.name)
    return decl


# ---------------------------------------------------------------- bulk (tank farms)
def bulk_movement(db: Session, wh: Warehouse, direction: str, qty: float, reference: str, actor: str) -> None:
    if wh.kind != "tank_farm":
        raise ServiceError("Bulk movements are only for tank farms")
    if qty <= 0:
        raise ServiceError("Quantity must be positive")
    if direction == "in" and wh.bulk_level + qty > wh.capacity:
        raise ServiceError(f"Not enough tank space: {wh.capacity - wh.bulk_level:,.0f} {wh.unit} free")
    if direction == "out" and qty > wh.bulk_level:
        raise ServiceError(f"Only {wh.bulk_level:,.0f} {wh.unit} in stock")
    wh.bulk_level += qty if direction == "in" else -qty
    _move(db, wh, None, direction, "manual", reference, actor, qty)
    audit(db, actor, "bulk_" + direction, "warehouse", wh.code, f"{qty:,.0f} {wh.unit}")


# ---------------------------------------------------------------- 3D game sync
def _ship_by_name(db, name):
    return db.scalar(select(Ship).where(Ship.name == name))


def _port_by_short(db, short):
    return db.scalar(select(Port).where(Port.island == short))


def _game_vehicle(db, label: str) -> Vehicle:
    plate = label.replace("Road train ", "RT-")
    v = db.scalar(select(Vehicle).where(Vehicle.plate == plate))
    if not v:
        v = Vehicle(plate=plate, vehicle_type="road_train", haulier="Bluewater Haulage (game)", trailers=2,
                    max_containers=2)
        db.add(v)
        db.flush()
    return v


def ingest_game_event(db: Session, event_type: str, data: dict) -> GameEvent:
    ev = GameEvent(event_type=event_type, payload=data or {})
    db.add(ev)
    port = _port_by_short(db, data.get("port", "")) if data else None
    ship = _ship_by_name(db, data.get("ship", "")) if data else None
    if event_type == "ship_berthed" and ship and port:
        if ship.status == "approved" and ship.ship_type == port.terminal_type:
            berth_ship(db, ship, port, "game", source="game")
    elif event_type == "ship_departed" and ship:
        depart_ship(db, ship, "game", source="game")
    elif event_type in ("cargo_loaded", "cargo_discharged") and port:
        qty = float(data.get("boxes") or data.get("quantity") or 0)
        kind = "container_yard" if port.terminal_type == "container" else "tank_farm"
        wh = db.scalar(select(Warehouse).where(Warehouse.port_id == port.id, Warehouse.kind == kind))
        if wh and qty > 0:
            direction = "in" if event_type == "cargo_discharged" else "out"
            _move(db, wh, None, direction, "game", f"{data.get('ship', '')} {event_type.replace('cargo_', '')}", "game", qty)
        if ship:
            call = db.scalar(select(VesselCall).where(VesselCall.ship_id == ship.id, VesselCall.status == "berthed"))
            if call:
                op = "discharge" if event_type == "cargo_discharged" else "load"
                call.operation = op if not call.operation or call.operation == op else "both"
                call.quantity += qty
    elif event_type in ("gate_in", "gate_out") and port:
        v = _game_vehicle(db, data.get("vehicle", "Road train ?"))
        boxes = int(data.get("boxes") or 0)
        if event_type == "gate_in":
            purpose = f"From {data.get('origin', '')}: {data.get('goods', 'empty')}"
            v.inside_port_id = port.id
        else:
            purpose = f"To {data.get('destination', '')}"
            v.inside_port_id = None
        db.add(GateTransaction(port_id=port.id, vehicle_id=v.id, direction="in" if event_type == "gate_in" else "out",
                               containers="", container_count=boxes, purpose=purpose, clerk="game", source="game"))
    return ev


def ingest_game_snapshot(db: Session, data: dict) -> None:
    st = db.scalar(select(GameState).where(GameState.key == "latest"))
    if not st:
        st = GameState(key="latest", data={})
        db.add(st)
    st.data = data
    st.updated_at = now()
    ports = {p.game_id: p for p in db.scalars(select(Port)).all()}
    for h in data.get("hubs", []):
        p = ports.get(h.get("id"))
        if not p:
            continue
        wh = db.scalar(select(Warehouse).where(Warehouse.port_id == p.id, Warehouse.kind == "container_yard"))
        if wh:
            wh.live_inbound, wh.live_outbound = float(h.get("inbound", 0)), float(h.get("outbound", 0))
            wh.live_level, wh.live_updated_at = wh.live_inbound + wh.live_outbound, now()
        for i in h.get("industries", []):
            ind = db.scalar(select(Industry).where(Industry.game_id == i.get("id")))
            if ind:
                ind.live_ready, ind.live_stock, ind.live_status = float(i.get("ready", 0)), float(i.get("stock", 0)), i.get("status", "")
    for pdata in data.get("ports", []):
        p = ports.get(pdata.get("id"))
        if p and p.terminal_type != "container":
            wh = db.scalar(select(Warehouse).where(Warehouse.port_id == p.id, Warehouse.kind == "tank_farm"))
            if wh:
                wh.live_level, wh.live_updated_at = float(pdata.get("stock", 0)), now()
    for s in data.get("ships", []):
        ship = db.scalar(select(Ship).where(Ship.game_id == s.get("id")))
        if ship:
            ship.live_status, ship.live_cargo = str(s.get("status", ""))[:160], float(s.get("cargo", 0))


# ---------------------------------------------------------------- dashboard
def dashboard(db: Session) -> dict:
    today = datetime.now(timezone.utc) - timedelta(hours=24)
    count = lambda q: db.scalar(q) or 0
    containers_by_status = dict(db.execute(select(Container.status, func.count()).group_by(Container.status)).all())
    orders_by_status = dict(db.execute(select(SupplyOrder.status, func.count()).group_by(SupplyOrder.status)).all())
    customs_open = count(select(func.count()).select_from(CustomsDeclaration)
                         .where(CustomsDeclaration.status.in_(["submitted", "under_review", "inspection", "held"])))
    duty_collected = db.scalar(select(func.coalesce(func.sum(CustomsDeclaration.duty_amount), 0))
                               .where(CustomsDeclaration.status == "cleared")) or 0
    yards = []
    for wh in db.scalars(select(Warehouse).order_by(Warehouse.id)).all():
        if wh.kind == "container_yard":
            occ = yard_occupancy(db, wh)
            yards.append({"wh": wh, "booked": occ, "live": wh.live_level})
        else:
            yards.append({"wh": wh, "booked": {"total": wh.bulk_level, "pct": round(wh.bulk_level / wh.capacity * 100, 1) if wh.capacity else 0},
                          "live": wh.live_level})
    return {
        "ships": count(select(func.count()).select_from(Ship)),
        "ships_pending": count(select(func.count()).select_from(Ship).where(Ship.status == "pending")),
        "in_port": count(select(func.count()).select_from(VesselCall).where(VesselCall.status == "berthed")),
        "industries": count(select(func.count()).select_from(Industry)),
        "industries_pending": count(select(func.count()).select_from(Industry).where(Industry.status == "pending")),
        "vehicles": count(select(func.count()).select_from(Vehicle)),
        "vehicles_inside": count(select(func.count()).select_from(Vehicle).where(Vehicle.inside_port_id.isnot(None))),
        "gate_today": count(select(func.count()).select_from(GateTransaction).where(GateTransaction.at >= today)),
        "customs_open": customs_open,
        "duty_collected": duty_collected,
        "containers_by_status": containers_by_status,
        "orders_by_status": orders_by_status,
        "yards": yards,
        "recent_audit": db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(12)).all(),
        "recent_events": db.scalars(select(GameEvent).order_by(GameEvent.id.desc()).limit(8)).all(),
    }
