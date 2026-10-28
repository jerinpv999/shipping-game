"""API used by the 3D game: who is playing, which ships they command, their jobs, and live positions."""
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crew as CR
from . import factions as FX
from . import insurance as INS
from . import marketplace as MK
from .db import get_db
from .models import Company, PlayerVessel, Ship, TransitRequest, User
from .security import current_user, verify_password
from .services import ServiceError

router = APIRouter(prefix="/api/game", tags=["game"])


def ship_payload(db: Session, s: Ship) -> dict:
    job = CR.ship_job(db, s)
    return {"id": s.id, "name": s.name, "type": s.ship_type, "capacity": s.capacity, "unit": s.capacity_unit,
            "imo": s.imo, "x": s.game_x, "z": s.game_z, "hd": s.game_hd, "cargo": s.game_cargo or 0,
            "home": CR.GAME_HOME.get(s.ship_type, "meridian"), "job": CR.job_payload(job) if job else None}


def me_payload(db: Session, user: User | None) -> dict:
    v = FX.vessel_of(db, user)
    if v and user.active:
        db.commit()
        F = FX.FACTIONS[FX.faction_of(user)]
        return {"status": "user", "kind": FX.faction_of(user), "name": user.full_name or user.username, "username": user.username,
                "company": None, "role": F["title"], "group": F["group"], "ships": [FX.vessel_payload(v)],
                "stats": FX.stats_payload(v), "note": ""}
    ident = CR.game_identity(db, user)
    if ident["status"] != "user":
        return {"status": ident["status"]}
    co = ident.get("company")
    ships = CR.controllable_ships(db, user)
    if INS.settle_due(db):
        db.commit()
    note = ""
    if not co:
        note = "You're not on a company crew yet. Apply to a company on your seafarer page."
    elif co.status != "approved":
        note = co.name + " is waiting for port-authority approval."
    elif not ships:
        lost = [s for s in co.ships if s.status == "lost"]
        if lost and ident["kind"] == "owner":
            note = f"{lost[-1].name} was lost at sea. Use the insurance payout to replace her under Portal → Fleet."
        elif lost and ident.get("member") and not ident["member"].ship_id:
            note = f"Your ship was lost at sea. {co.name} needs to assign you a new one."
        else:
            note = ("Buy a ship in the marketplace and wait for its registration to be approved." if ident["kind"] == "owner"
                    else "Your company hasn't assigned you a ship yet.")
    return {"status": "user", "kind": ident["kind"], "name": user.full_name or user.username, "username": user.username,
            "company": {"id": co.id, "name": co.name, "balance": co.balance, "status": co.status} if co else None,
            "role": "Owner" if ident["kind"] == "owner" else CR.POSITIONS.get(ident["member"].position, "Crew") if ident.get("member") else "Seafarer",
            "ships": [ship_payload(db, s) for s in ships], "note": note}


def owned(db: Session, user: User | None, ship_id: int) -> Ship:
    ship = next((s for s in CR.controllable_ships(db, user) if s.id == ship_id), None)
    if not ship:
        raise HTTPException(403, "You don't command that ship")
    return ship


@router.get("/me")
def me(db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    return me_payload(db, user)


class LoginIn(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(body: LoginIn, request: Request, db: Session = Depends(get_db)):
    u = db.scalar(select(User).where(User.username == body.username.strip().lower()))
    if not u or not u.active or not verify_password(body.password, u.password_hash):
        raise HTTPException(401, "Wrong username or password")
    if u.role not in ("company", "seafarer", "pirate", "marine"):
        raise HTTPException(403, "Port-authority staff use the admin panel. Sign in with a company, seafarer, pirate or marine account")
    request.session["uid"] = u.id
    return me_payload(db, u)


@router.post("/logout")
def logout(request: Request):
    request.session.clear()
    return {"ok": True}


@router.get("/jobs")
def jobs(ship_id: int, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    ship = owned(db, user, ship_id)
    CR.ensure_spot_market(db)
    db.commit()
    current = CR.ship_job(db, ship)
    rows = db.scalars(select(TransitRequest).where(TransitRequest.status == "open", TransitRequest.mode == "sea",
                                                   TransitRequest.cargo_type == ship.ship_type,
                                                   TransitRequest.origin_port_id.isnot(None), TransitRequest.destination_port_id.isnot(None))
                      .order_by(TransitRequest.deadline)).all()
    board = [CR.job_payload(r) | {"fits": r.quantity <= ship.capacity} for r in rows if not MK.is_expired(r)]
    co = db.get(Company, ship.company_id)
    return {"current": CR.job_payload(current) if current else None, "board": board[:12], "balance": co.balance if co else 0}


class ShipIn(BaseModel):
    ship_id: int


def _act(db, fn, user, ship_id, rid):
    ship = owned(db, user, ship_id)
    r = db.get(TransitRequest, rid)
    if not r:
        raise HTTPException(404, "Job not found")
    co = db.get(Company, ship.company_id)
    try:
        out = fn(r, co, ship)
        db.commit()
    except ServiceError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return {"job": CR.job_payload(r), "balance": co.balance, "paid": out if isinstance(out, (int, float)) else None}


@router.post("/jobs/{rid}/accept")
def accept(rid: int, body: ShipIn, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    return _act(db, lambda r, co, ship: MK.accept_request(db, r, co, ship.id, user.username), user, body.ship_id, rid)


@router.post("/jobs/{rid}/loaded")
def loaded(rid: int, body: ShipIn, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    def go(r, co, ship):
        if r.ship_id != ship.id:
            raise ServiceError("That job belongs to another ship")
        if r.status == "accepted":
            MK.start_request(db, r, co, user.username)
    return _act(db, go, user, body.ship_id, rid)


@router.post("/jobs/{rid}/delivered")
def delivered(rid: int, body: ShipIn, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    def go(r, co, ship):
        if r.ship_id != ship.id:
            raise ServiceError("That job belongs to another ship")
        if r.status == "accepted":
            MK.start_request(db, r, co, user.username)
        return MK.deliver_request(db, r, co, user.username)
    return _act(db, go, user, body.ship_id, rid)


class StateIn(BaseModel):
    ships: list[dict]


@router.post("/ship-state")
def ship_state(body: StateIn, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    mine = {s.id: s for s in CR.controllable_ships(db, user)}
    v = FX.vessel_of(db, user)
    for row in body.ships[:20]:
        rid = str(row.get("id", ""))
        if v and rid == "v%d" % v.id:
            v.x, v.z, v.hd = float(row.get("x", 0)), float(row.get("z", 0)), float(row.get("hd", 0))
            v.state, v.seen_at = str(row.get("state", ""))[:20], datetime.now(timezone.utc)
            continue
        s = mine.get(int(rid)) if rid.isdigit() else None
        if not s:
            continue
        s.game_x, s.game_z, s.game_hd = float(row.get("x", 0)), float(row.get("z", 0)), float(row.get("hd", 0))
        s.game_cargo, s.game_state = float(row.get("cargo", 0)), str(row.get("state", ""))[:20]
        s.game_driver, s.game_seen_at = (user.full_name or user.username)[:80], datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}


@router.get("/world")
def world(db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    """Other players' company ships that reported a position in the last two minutes."""
    mine = {s.id for s in CR.controllable_ships(db, user)}
    since = datetime.now(timezone.utc) - timedelta(minutes=2)
    own = FX.vessel_of(db, user)
    vessels = db.scalars(select(PlayerVessel).where(PlayerVessel.seen_at.isnot(None), PlayerVessel.seen_at >= since)).all()
    others = [{"id": "v%d" % p.id, "name": p.name, "type": p.kind, "x": p.x, "z": p.z, "hd": p.hd, "cargo": 0, "capacity": 1,
               "company": FX.FACTIONS["pirate" if p.kind == "pirate" else "marine"]["group"], "driver": p.user.full_name, "state": p.state}
              for p in vessels if not own or p.id != own.id]
    rows = db.scalars(select(Ship).where(Ship.company_id.isnot(None), Ship.game_seen_at.isnot(None), Ship.game_seen_at >= since)).all()
    return {"ships": [{"id": s.id, "name": s.name, "type": s.ship_type, "x": s.game_x, "z": s.game_z, "hd": s.game_hd,
                       "cargo": s.game_cargo, "capacity": s.capacity, "company": s.company.name if s.company else "",
                       "driver": s.game_driver, "state": s.game_state} for s in rows if s.id not in mine] + others}


class LostIn(BaseModel):
    ship_id: int
    cause: str = "other"


@router.post("/ship-lost")
def ship_lost(body: LostIn, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    """A company ship sank in the game: record the loss, settle its job and raise insurance claims."""
    ship = db.get(Ship, body.ship_id)
    ident = CR.game_identity(db, user)
    same_company = bool(ship and ident.get("company") and ident["company"].id == ship.company_id)
    # a repeat report for a ship already lost is answered for anyone in her company (the loss itself is recorded once)
    if not ship or not (ship in CR.controllable_ships(db, user) or (ship.status == "lost" and same_company)):
        raise HTTPException(403, "You don't command that ship")
    out = INS.report_loss(db, ship, body.cause, user.username)
    db.commit()
    co = db.get(Company, ship.company_id)
    return out | {"ship": ship.name, "balance": co.balance if co else 0}


class ReportIn(BaseModel):
    event: str                  # pirates: raid, lost · marines: pirate_destroyed, drive_off, escort, lost
    target: str = ""
    loot: float = 0
    sank: bool = False
    seconds: float = 0


@router.post("/report")
def report(body: ReportIn, db: Session = Depends(get_db), user: User | None = Depends(current_user)):
    if not user:
        raise HTTPException(401, "Sign in first")
    try:
        out = FX.report(db, user, body.event, body.model_dump())
        db.commit()
    except ServiceError as e:
        db.rollback()
        raise HTTPException(400, str(e))
    return out


@router.get("/leaderboard")
def board(db: Session = Depends(get_db)):
    row = lambda p: {"name": p.name, "captain": p.user.full_name, "earnings": p.earnings, "kills": p.kills, "raids": p.raids}
    return {"pirates": [row(p) for p in FX.leaderboard(db, "pirate")], "marines": [row(p) for p in FX.leaderboard(db, "navy")]}
