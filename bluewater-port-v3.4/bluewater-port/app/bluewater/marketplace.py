"""Company portal rules: registration, wallet, buying ships and vehicles, and cargo transit requests."""
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Company, Industry, LedgerEntry, Port, Ship, SupplyOrder, TransitRequest, User, Vehicle, now
from .security import hash_password
from .services import ServiceError, audit, next_ref

OPENING_BALANCE = 60_000_000
PORT_DUES = 0.03  # share of each freight payout kept by the port authority

# key: (label, ship_type, capacity, unit, price, description)
SHIP_MODELS = {
    "feeder": ("Feeder container ship", "container", 120, "TEU", 18_000_000, "Short-sea workhorse for the Meridian–Coral Bay run."),
    "panamax": ("Panamax container ship", "container", 300, "TEU", 42_000_000, "More than twice the boxes per voyage."),
    "aframax": ("Aframax crude tanker", "crude", 600_000, "bbl", 35_000_000, "Petra to Vanta crude in one lift."),
    "suezmax": ("Suezmax crude tanker", "crude", 1_000_000, "bbl", 52_000_000, "Large crude parcels at lower cost per barrel."),
    "lng150": ("LNG carrier, 150k m³", "lng", 150_000, "m³", 48_000_000, "Moss-type spheres for Northgas exports."),
}
# key: (label, vehicle_type, trailers, max_containers, price, description)
VEHICLE_MODELS = {
    "roadtrain": ("Road train, 2 trailers", "road_train", 2, 2, 280_000, "Two 40-ft boxes per trip between port and factory."),
    "truck": ("Container truck", "truck", 1, 1, 140_000, "One 40-ft box. Cheaper, nimble in town."),
}
CARGO_UNIT = {"container": "boxes", "crude": "bbl", "lng": "m³"}
OPEN_STATES = ("accepted", "in_transit")      # a job lost at sea gets status "lost"
CONSOLIDATE_MIN = 4   # boxes on one route before the port books a ship automatically


# ---------------------------------------------------------------- helpers
def imo_check_digit(six: str) -> int:
    return sum(int(d) * w for d, w in zip(six, range(7, 1, -1))) % 10


def new_imo(db: Session) -> str:
    for _ in range(100):
        six = str(random.randint(900000, 999999))
        imo = six + str(imo_check_digit(six))
        if not db.scalar(select(Ship.id).where(Ship.imo == imo)):
            return imo
    raise ServiceError("Could not allocate an IMO number")


def post_ledger(db: Session, company: Company, amount: float, kind: str, description: str) -> LedgerEntry:
    company.balance = round(company.balance + amount, 2)
    entry = LedgerEntry(company_id=company.id, amount=amount, kind=kind, description=description,
                        balance_after=company.balance)
    db.add(entry)
    return entry


def is_expired(r: TransitRequest) -> bool:
    deadline = r.deadline if r.deadline.tzinfo else r.deadline.replace(tzinfo=timezone.utc)
    return r.status == "open" and deadline < datetime.now(timezone.utc)


def busy_asset_ids(db: Session, company_id: int) -> tuple[set, set]:
    rows = db.execute(select(TransitRequest.ship_id, TransitRequest.vehicle_id)
                      .where(TransitRequest.company_id == company_id, TransitRequest.status.in_(OPEN_STATES))).all()
    return {s for s, _ in rows if s}, {v for _, v in rows if v}


# ---------------------------------------------------------------- registration
def register_company(db: Session, *, username: str, full_name: str, email: str, password: str,
                     company_name: str, company_type: str, country: str, phone: str = "") -> User:
    username, company_name = username.strip().lower(), company_name.strip()
    if len(username) < 3 or not username.replace("_", "").replace(".", "").isalnum():
        raise ServiceError("Username must be at least 3 letters or digits")
    if len(password) < 8:
        raise ServiceError("Password must be at least 8 characters")
    if "@" not in email:
        raise ServiceError("Enter a valid email address")
    if company_type not in ("shipping_line", "haulier", "both"):
        raise ServiceError("Choose what your company does")
    if len(company_name) < 3:
        raise ServiceError("Enter your company name")
    if db.scalar(select(User.id).where(User.username == username)):
        raise ServiceError("That username is taken")
    if db.scalar(select(Company.id).where(func.lower(Company.name) == company_name.lower())):
        raise ServiceError("A company with that name is already registered")
    company = Company(reg_no=next_ref(db, Company, "reg_no", "CO"), name=company_name, company_type=company_type,
                      country=country, contact_email=email, phone=phone, status="pending")
    db.add(company)
    db.flush()
    post_ledger(db, company, OPENING_BALANCE, "opening_balance", "Founding capital")
    user = User(username=username, full_name=full_name, email=email, role="company", company_id=company.id,
                password_hash=hash_password(password))
    db.add(user)
    audit(db, username, "register", "company", company.reg_no, company_name)
    return user


# ---------------------------------------------------------------- marketplace
def buy_ship(db: Session, company: Company, model_key: str, name: str, flag: str, actor: str) -> Ship:
    if company.status == "suspended":
        raise ServiceError("Your company is suspended")
    if company.company_type == "haulier":
        raise ServiceError("Road hauliers can't own ships. Change your company type to shipping line or both")
    if model_key not in SHIP_MODELS:
        raise ServiceError("Unknown ship model")
    label, stype, cap, unit, price, _ = SHIP_MODELS[model_key]
    name = name.strip()
    if len(name) < 3:
        raise ServiceError("Give the ship a name")
    if db.scalar(select(Ship.id).where(func.lower(Ship.name) == name.lower())):
        raise ServiceError(f"A ship named {name} is already registered")
    if company.balance < price:
        raise ServiceError(f"Not enough funds: {label} costs ${price:,.0f}, balance ${company.balance:,.0f}")
    ship = Ship(imo=new_imo(db), name=name, ship_type=stype, flag=flag or "Bluewater", owner=company.name,
                capacity=cap, capacity_unit=unit, status="pending", company_id=company.id, model=label, price=price)
    db.add(ship)
    db.flush()
    post_ledger(db, company, -price, "purchase", f"{label} {name} (IMO {ship.imo})")
    audit(db, actor, "buy_ship", "company", company.reg_no, f"{name} · ${price:,.0f}")
    return ship


def buy_vehicle(db: Session, company: Company, model_key: str, plate: str, actor: str) -> Vehicle:
    if company.status == "suspended":
        raise ServiceError("Your company is suspended")
    if company.company_type == "shipping_line":
        raise ServiceError("Shipping lines can't own road vehicles. Change your company type to haulier or both")
    if model_key not in VEHICLE_MODELS:
        raise ServiceError("Unknown vehicle model")
    label, vtype, trailers, max_c, price, _ = VEHICLE_MODELS[model_key]
    plate = plate.strip().upper()
    if len(plate) < 4:
        raise ServiceError("Enter the registration plate")
    if db.scalar(select(Vehicle.id).where(Vehicle.plate == plate)):
        raise ServiceError(f"Plate {plate} is already registered")
    if company.balance < price:
        raise ServiceError(f"Not enough funds: {label} costs ${price:,.0f}, balance ${company.balance:,.0f}")
    v = Vehicle(plate=plate, vehicle_type=vtype, haulier=company.name, trailers=trailers, max_containers=max_c,
                status="pending", company_id=company.id)
    db.add(v)
    db.flush()
    post_ledger(db, company, -price, "purchase", f"{label} {plate}")
    audit(db, actor, "buy_vehicle", "company", company.reg_no, f"{plate} · ${price:,.0f}")
    return v


# ---------------------------------------------------------------- transit requests
def post_request(db: Session, *, mode: str, cargo_type: str, origin: str, destination: str, quantity: float,
                 rate: float, days: float = 3, description: str = "", origin_port: Port | None = None,
                 destination_port: Port | None = None, order: SupplyOrder | None = None,
                 posted_by: str = "system") -> TransitRequest:
    if mode not in ("sea", "road") or cargo_type not in CARGO_UNIT:
        raise ServiceError("Unknown mode or cargo type")
    if mode == "road" and cargo_type != "container":
        raise ServiceError("Road transit is for containers only")
    if quantity <= 0 or rate <= 0:
        raise ServiceError("Quantity and rate must be positive")
    r = TransitRequest(ref=next_ref(db, TransitRequest, "ref", "TR"), mode=mode, cargo_type=cargo_type,
                       description=description, origin=origin, destination=destination,
                       origin_port_id=origin_port.id if origin_port else None,
                       destination_port_id=destination_port.id if destination_port else None,
                       quantity=quantity, unit=CARGO_UNIT[cargo_type], rate=round(rate, 2),
                       deadline=now() + timedelta(days=days), order_id=order.id if order else None, posted_by=posted_by)
    db.add(r)
    db.flush()
    audit(db, posted_by, "post", "transit", r.ref, f"{origin} → {destination} · ${rate:,.0f}")
    return r


def requests_for_order(db: Session, order: SupplyOrder, leg: str) -> None:
    """Called by the order workflow: publish the road and sea legs as paid jobs."""
    n = len(order.containers) or order.containers_planned
    if leg == "pickup":
        for i in range(0, n, 2):
            k = min(2, n - i)
            post_request(db, mode="road", cargo_type="container", origin=order.shipper.name,
                         destination=order.origin_port.name, quantity=k, rate=k * 1400,
                         description=f"Truck {order.goods.lower()} from the factory to the export yard ({order.order_no})",
                         destination_port=order.origin_port, order=order, days=2)
    elif leg == "sea":
        db.flush()
        consolidate(db, order.origin_port, order.destination_port)
    elif leg == "delivery":
        for i in range(0, n, 2):
            k = min(2, n - i)
            post_request(db, mode="road", cargo_type="container", origin=order.destination_port.name,
                         destination=order.consignee.name, quantity=k, rate=k * 1400,
                         description=f"Deliver cleared {order.goods.lower()} to the consignee ({order.order_no})",
                         origin_port=order.destination_port, order=order, days=2)


def pending_for_lane(db: Session, origin: Port, destination: Port) -> list[SupplyOrder]:
    return db.scalars(select(SupplyOrder).where(SupplyOrder.origin_port_id == origin.id,
                                                SupplyOrder.destination_port_id == destination.id,
                                                SupplyOrder.status == "export_cleared",
                                                SupplyOrder.sea_request_id.is_(None))
                      .order_by(SupplyOrder.updated_at)).all()


def consolidate(db: Session, origin: Port, destination: Port, force: bool = False, posted_by: str = "system"):
    """Group every export-cleared order on a route into one sea booking once the route has enough boxes."""
    orders = pending_for_lane(db, origin, destination)
    boxes = sum(len(o.containers) or o.containers_planned for o in orders)
    if not orders or (boxes < CONSOLIDATE_MIN and not force):
        return None
    goods = sorted({o.goods for o in orders})
    r = post_request(db, mode="sea", cargo_type="container", origin=origin.name, destination=destination.name,
                     quantity=boxes, rate=boxes * 4800,
                     description=f"Consolidated booking: {len(orders)} order{'s' if len(orders) > 1 else ''} · {', '.join(goods)}",
                     origin_port=origin, destination_port=destination, order=orders[0] if len(orders) == 1 else None,
                     posted_by=posted_by, days=3)
    r.order_refs = ", ".join(o.order_no for o in orders)
    for o in orders:
        o.sea_request_id = r.id
    audit(db, posted_by, "consolidate", "transit", r.ref, r.order_refs)
    return r


def pending_lanes(db: Session) -> list[dict]:
    rows = db.scalars(select(SupplyOrder).where(SupplyOrder.status == "export_cleared", SupplyOrder.sea_request_id.is_(None))).all()
    lanes = {}
    for o in rows:
        k = (o.origin_port_id, o.destination_port_id)
        L = lanes.setdefault(k, {"origin": o.origin_port, "destination": o.destination_port, "orders": [], "boxes": 0})
        L["orders"].append(o); L["boxes"] += len(o.containers) or o.containers_planned
    return sorted(lanes.values(), key=lambda L: -L["boxes"])


def post_bulk_market(db: Session, posted_by: str = "system") -> list[TransitRequest]:
    ports = {p.game_id: p for p in db.scalars(select(Port)).all()}
    out = []
    crude = random.choice([400_000, 600_000, 900_000])
    out.append(post_request(db, mode="sea", cargo_type="crude", origin=ports["petra"].name, destination=ports["vanta"].name,
                            quantity=crude, rate=crude * 2.4, description="Refinery feedstock for Vanta",
                            origin_port=ports["petra"], destination_port=ports["vanta"], posted_by=posted_by, days=4))
    gas = random.choice([100_000, 150_000])
    out.append(post_request(db, mode="sea", cargo_type="lng", origin=ports["northgas"].name, destination=ports["harbor"].name,
                            quantity=gas, rate=gas * 6.5, description="LNG for Harbor Point send-out",
                            origin_port=ports["northgas"], destination_port=ports["harbor"], posted_by=posted_by, days=4))
    return out


def eligible_assets(db: Session, company: Company, r: TransitRequest) -> list:
    busy_s, busy_v = busy_asset_ids(db, company.id)
    if r.mode == "sea":
        return [s for s in company.ships if s.status == "approved" and s.ship_type == r.cargo_type
                and s.capacity >= r.quantity and s.id not in busy_s]
    return [v for v in company.vehicles if v.status == "active" and v.max_containers >= r.quantity and v.id not in busy_v]


def accept_request(db: Session, r: TransitRequest, company: Company, asset_id: int, actor: str) -> None:
    if company.status != "approved":
        raise ServiceError("The port authority hasn't approved your company yet")
    if r.status != "open" or is_expired(r):
        raise ServiceError("This request is no longer open")
    asset = next((a for a in eligible_assets(db, company, r) if a.id == asset_id), None)
    if not asset:
        raise ServiceError("Choose one of your approved, idle " + ("ships" if r.mode == "sea" else "vehicles") +
                           " that can carry this cargo")
    r.status, r.company_id, r.accepted_at = "accepted", company.id, now()
    if r.mode == "sea":
        r.ship_id = asset.id
    else:
        r.vehicle_id = asset.id
    audit(db, actor, "accept", "transit", r.ref, company.name)


def _own(r: TransitRequest, company: Company):
    if r.company_id != company.id:
        raise ServiceError("This job belongs to another company")


def start_request(db: Session, r: TransitRequest, company: Company, actor: str) -> None:
    _own(r, company)
    if r.status != "accepted":
        raise ServiceError("Only accepted jobs can start")
    r.status, r.started_at = "in_transit", now()
    audit(db, actor, "start", "transit", r.ref)


def deliver_request(db: Session, r: TransitRequest, company: Company, actor: str) -> float:
    _own(r, company)
    if r.status != "in_transit":
        raise ServiceError("Start the job before delivering it")
    r.status, r.completed_at = "delivered", now()
    dues = round(r.rate * PORT_DUES, 2)
    post_ledger(db, company, r.rate, "freight", f"{r.ref} {r.origin} → {r.destination}")
    post_ledger(db, company, -dues, "port_dues", f"Port dues {PORT_DUES:.0%} on {r.ref}")
    audit(db, actor, "deliver", "transit", r.ref, f"paid ${r.rate - dues:,.0f}")
    return r.rate - dues


def release_request(db: Session, r: TransitRequest, company: Company, actor: str) -> None:
    _own(r, company)
    if r.status != "accepted":
        raise ServiceError("Only jobs that haven't started can be handed back")
    r.status, r.company_id, r.ship_id, r.vehicle_id, r.accepted_at = "open", None, None, None, None
    audit(db, actor, "release", "transit", r.ref, company.name)


def cancel_request(db: Session, r: TransitRequest, actor: str) -> None:
    if r.status not in ("open", "accepted"):
        raise ServiceError("Only open or accepted requests can be cancelled")
    r.status = "cancelled"
    audit(db, actor, "cancel", "transit", r.ref)


def seed_marketplace(db: Session) -> None:
    """Demo company with a ship and a road train, plus a few open jobs."""
    if db.scalar(select(Company.id).limit(1)):
        return
    random.seed(11)
    user = register_company(db, username="oceanic", full_name="Asha Menon", email="asha@oceanic.example",
                            password="oceanic123", company_name="Oceanic Carriers", company_type="both",
                            country="India", phone="+91 471 000 0000")
    db.flush()
    co = user.company or db.get(Company, user.company_id)
    co.status = "approved"
    ship = buy_ship(db, co, "feeder", "Oceanic Dawn", "India", "seed")
    ship.status = "approved"
    truck = buy_vehicle(db, co, "roadtrain", "KL-01-OC-0001", "seed")
    truck.status = "active"
    post_bulk_market(db, "seed")
    ports = {p.game_id: p for p in db.scalars(select(Port)).all()}
    post_request(db, mode="sea", cargo_type="container", origin=ports["coral"].name, destination=ports["meridian"].name,
                 quantity=60, rate=60 * 4800, description="Spot cargo: mixed consumer goods",
                 origin_port=ports["coral"], destination_port=ports["meridian"], posted_by="seed")
    halden = db.scalar(select(Industry).where(Industry.game_id == "halden"))
    if halden:
        post_request(db, mode="road", cargo_type="container", origin=ports["meridian"].name, destination=halden.name,
                     quantity=2, rate=2800, description="Empty containers repositioned to Halden Electronics",
                     origin_port=ports["meridian"], posted_by="seed", days=2)
    db.commit()
