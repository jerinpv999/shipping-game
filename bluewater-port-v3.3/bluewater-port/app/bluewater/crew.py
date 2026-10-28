"""Seafarers, job applications and crew assignments, plus who may command which ship in the 3D game."""
import random
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import marketplace as MK
from .models import Company, CrewMember, JobApplication, Port, Ship, TransitRequest, User, now
from .security import hash_password
from .services import ServiceError, audit

POSITIONS = {"captain": "Captain", "chief_officer": "Chief officer", "officer": "Deck officer"}
LICENCES = ["Master Mariner (unlimited)", "Master (3,000 GT)", "Chief Mate", "Officer of the Watch", "Cadet"]


def _check_account(db: Session, username: str, password: str, email: str):
    username = username.strip().lower()
    if len(username) < 3 or not username.replace("_", "").replace(".", "").isalnum():
        raise ServiceError("Username must be at least 3 letters or digits")
    if len(password) < 8:
        raise ServiceError("Password must be at least 8 characters")
    if email and "@" not in email:
        raise ServiceError("Enter a valid email address")
    if db.scalar(select(User.id).where(User.username == username)):
        raise ServiceError("That username is taken")
    return username


def register_seafarer(db: Session, *, username: str, full_name: str, email: str, password: str,
                      licence: str = "", experience_years: int = 0) -> User:
    username = _check_account(db, username, password, email)
    if not full_name.strip():
        raise ServiceError("Enter your name")
    u = User(username=username, full_name=full_name.strip(), email=email, role="seafarer", licence=licence,
             experience_years=max(0, min(60, int(experience_years or 0))), password_hash=hash_password(password))
    db.add(u)
    db.flush()
    audit(db, username, "register", "seafarer", username, licence)
    return u


def membership(db: Session, user: User) -> CrewMember | None:
    return db.scalar(select(CrewMember).where(CrewMember.user_id == user.id, CrewMember.status == "active"))


def apply(db: Session, user: User, company: Company, position: str, message: str) -> JobApplication:
    if company.status != "approved":
        raise ServiceError(f"{company.name} isn't hiring yet")
    current = membership(db, user)
    if current and current.company_id == company.id:
        raise ServiceError(f"You already work for {company.name}")
    if db.scalar(select(JobApplication.id).where(JobApplication.user_id == user.id, JobApplication.company_id == company.id,
                                                 JobApplication.status == "pending")):
        raise ServiceError(f"You already applied to {company.name}")
    a = JobApplication(user_id=user.id, company_id=company.id, position=position if position in POSITIONS else "captain",
                       message=message[:1000])
    db.add(a)
    audit(db, user.username, "apply", "company", company.reg_no, POSITIONS.get(a.position, a.position))
    return a


def decide(db: Session, app: JobApplication, company: Company, accept: bool, actor: str) -> None:
    if app.company_id != company.id:
        raise ServiceError("That application isn't for your company")
    if app.status != "pending":
        raise ServiceError("This application has already been decided")
    app.status, app.decided_at = ("accepted" if accept else "rejected"), now()
    if accept:
        previous = membership(db, app.user)
        if previous:
            # transfer: joining the new company ends the old contract and hands back the old ship
            previous.status, previous.ship_id = "removed", None
            audit(db, app.user.username, "transfer", "seafarer", app.user.username, previous.company.name + " → " + company.name)
        db.add(CrewMember(company_id=company.id, user_id=app.user_id, position=app.position))
        for other in db.scalars(select(JobApplication).where(JobApplication.user_id == app.user_id, JobApplication.status == "pending",
                                                             JobApplication.id != app.id)).all():
            other.status = "withdrawn"
    audit(db, actor, "hire" if accept else "decline", "seafarer", app.user.username, company.name)


def add_staff(db: Session, company: Company, *, username: str, full_name: str, password: str, position: str,
              licence: str, actor: str) -> CrewMember:
    """A company creates a game login for one of its own people."""
    u = register_seafarer(db, username=username, full_name=full_name, email="", password=password, licence=licence)
    m = CrewMember(company_id=company.id, user_id=u.id, position=position if position in POSITIONS else "captain")
    db.add(m)
    audit(db, actor, "add_staff", "company", company.reg_no, u.username)
    return m


def assign_ship(db: Session, company: Company, member: CrewMember, ship_id: int | None, actor: str) -> None:
    if member.company_id != company.id or member.status != "active":
        raise ServiceError("That person isn't on your crew")
    if ship_id:
        ship = db.get(Ship, ship_id)
        if not ship or ship.company_id != company.id:
            raise ServiceError("Choose one of your company's ships")
        taken = db.scalar(select(CrewMember).where(CrewMember.ship_id == ship_id, CrewMember.status == "active",
                                                   CrewMember.id != member.id))
        if taken:
            taken.ship_id = None  # one commanding officer per ship: the previous one is relieved
        member.ship_id = ship_id
    else:
        member.ship_id = None
    audit(db, actor, "assign", "seafarer", member.user.username, db.get(Ship, ship_id).name if ship_id else "unassigned")


def remove_member(db: Session, company: Company, member: CrewMember, actor: str) -> None:
    if member.company_id != company.id:
        raise ServiceError("That person isn't on your crew")
    member.status, member.ship_id = "removed", None
    audit(db, actor, "remove", "seafarer", member.user.username, company.name)


# ---------------------------------------------------------------- game access
def game_identity(db: Session, user: User | None) -> dict:
    if not user or not user.active:
        return {"status": "none"}
    if user.role == "company":
        co = db.get(Company, user.company_id)
        return {"status": "user", "kind": "owner", "user": user, "company": co}
    if user.role == "seafarer":
        m = membership(db, user)
        return {"status": "user", "kind": "crew", "user": user, "company": m.company if m else None, "member": m}
    return {"status": "staff", "user": user}


def controllable_ships(db: Session, user: User | None) -> list[Ship]:
    ident = game_identity(db, user)
    if ident["status"] != "user" or not ident.get("company") or ident["company"].status != "approved":
        return []
    co = ident["company"]
    if ident["kind"] == "owner":
        return [s for s in co.ships if s.status == "approved"]
    m = ident["member"]
    if m and m.ship and m.ship.status == "approved":
        return [m.ship]
    return []


GAME_HOME = {"container": "meridian", "crude": "petra", "lng": "northgas"}


def job_payload(r: TransitRequest) -> dict:
    return {"id": r.id, "ref": r.ref, "cargo_type": r.cargo_type, "quantity": r.quantity, "unit": r.unit, "rate": r.rate,
            "origin": r.origin_port.game_id if r.origin_port else None, "destination": r.destination_port.game_id if r.destination_port else None,
            "origin_name": r.origin, "destination_name": r.destination, "status": r.status, "description": r.description,
            "orders": r.order_refs or (r.order.order_no if r.order else ""),
            "deadline_ms": int((r.deadline if r.deadline.tzinfo else r.deadline.replace(tzinfo=timezone.utc)).timestamp() * 1000)}


def ship_job(db: Session, ship: Ship) -> TransitRequest | None:
    return db.scalar(select(TransitRequest).where(TransitRequest.ship_id == ship.id, TransitRequest.status.in_(MK.OPEN_STATES)))


SPOT = [("container", "meridian", "coral", 40, 120), ("container", "coral", "tern", 30, 110), ("container", "saltmarsh", "meridian", 24, 90),
        ("container", "tern", "saltmarsh", 30, 100), ("container", "coral", "meridian", 36, 120), ("container", "meridian", "saltmarsh", 24, 80),
        ("crude", "petra", "vanta", 300000, 600000), ("lng", "northgas", "harbor", 80000, 150000)]


def ensure_spot_market(db: Session, minimum: int = 6) -> None:
    """Keep a few sea charters between game ports on the board so company ships always have work."""
    open_sea = [r for r in db.scalars(select(TransitRequest).where(TransitRequest.status == "open", TransitRequest.mode == "sea",
                                                                   TransitRequest.origin_port_id.isnot(None),
                                                                   TransitRequest.destination_port_id.isnot(None))).all()
                if not MK.is_expired(r)]
    ports = {p.game_id: p for p in db.scalars(select(Port)).all()}
    tries = 0
    while len(open_sea) < minimum and tries < 20:
        tries += 1
        kind, a, b, lo, hi = random.choice(SPOT)
        if a not in ports or b not in ports:
            continue
        qty = random.randint(lo, hi) if kind == "container" else round(random.randint(lo, hi), -4)
        rate = qty * (4800 if kind == "container" else 2.4 if kind == "crude" else 6.5)
        r = MK.post_request(db, mode="sea", cargo_type=kind, origin=ports[a].name, destination=ports[b].name, quantity=qty, rate=rate,
                            description="Spot charter", origin_port=ports[a], destination_port=ports[b], posted_by="market", days=2)
        open_sea.append(r)


def seed_crew(db: Session) -> None:
    """Demo captain hired by the demo company and assigned to its ship (idempotent)."""
    if db.scalar(select(User.id).where(User.username == "captain")):
        return
    co = db.scalar(select(Company).where(Company.name == "Oceanic Carriers"))
    if not co:
        return
    u = register_seafarer(db, username="captain", full_name="Ravi Kurup", email="ravi@seafarer.example", password="captain123",
                          licence="Master Mariner (unlimited)", experience_years=14)
    ship = next((s for s in co.ships if s.status == "approved"), None)
    db.add(CrewMember(company_id=co.id, user_id=u.id, position="captain", ship_id=ship.id if ship else None))
    db.commit()


def resign(db: Session, user: User) -> str:
    m = membership(db, user)
    if not m:
        raise ServiceError("You don't work for a company")
    name = m.company.name
    m.status, m.ship_id = "removed", None
    audit(db, user.username, "resign", "company", m.company.reg_no, name)
    return name
