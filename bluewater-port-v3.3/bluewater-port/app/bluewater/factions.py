"""Pirates and marines: players who sail their own skiff or navy frigate instead of working for a company."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import crew as CR
from .admin import flash, templates
from .db import get_db
from .models import PlayerVessel, User
from .security import current_user, hash_password
from .services import ServiceError, audit

FACTIONS = {
    "pirate": {"role": "pirate", "vessel": "pirate", "title": "Pirate captain", "group": "Brethren of the Reef",
               "craft": "Pirate skiff", "earn": "Plunder", "raids": "Raids", "kills": "Ships sunk"},
    "marine": {"role": "marine", "vessel": "navy", "title": "Marine officer", "group": "Bluewater Navy",
               "craft": "Frigate", "earn": "Pay and bounties", "raids": "Escorts", "kills": "Pirates destroyed"},
}
PLAY_ROLES = ("pirate", "marine")

# What the server pays out. The game reports events; the server sets (and caps) the amounts.
BOUNTY_PIRATE = 250_000       # marine destroys a pirate
BOUNTY_DRIVE_OFF = 40_000     # marine's presence breaks up an attack
ESCORT_RATE = 400             # per second of escort, capped per report
ESCORT_CAP = 240_000
RAID_CAP = 3_000_000          # most plunder one raid can report
SINK_BONUS = 500_000          # pirate sinks a cargo ship


def register_player(db: Session, *, faction: str, username: str, full_name: str, email: str, password: str, vessel_name: str) -> User:
    if faction not in FACTIONS:
        raise ServiceError("Choose pirate or marine")
    F = FACTIONS[faction]
    username = CR._check_account(db, username, password, email)
    if not full_name.strip():
        raise ServiceError("Enter your name")
    vessel_name = " ".join(vessel_name.split())[:40] or (full_name.split()[0] + "'s " + ("Revenge" if faction == "pirate" else "Shield"))
    if faction == "marine" and not vessel_name.upper().startswith("BNS "):
        vessel_name = "BNS " + vessel_name
    u = User(username=username, full_name=full_name.strip(), email=email, role=F["role"], password_hash=hash_password(password))
    db.add(u)
    db.flush()
    db.add(PlayerVessel(user_id=u.id, kind=F["vessel"], name=vessel_name))
    audit(db, username, "register", faction, username, vessel_name)
    return u


def vessel_of(db: Session, user: User | None) -> PlayerVessel | None:
    if not user or user.role not in PLAY_ROLES:
        return None
    v = db.scalar(select(PlayerVessel).where(PlayerVessel.user_id == user.id))
    if not v:  # account made before vessels existed: issue one
        v = PlayerVessel(user_id=user.id, kind="pirate" if user.role == "pirate" else "navy",
                         name=(user.full_name or user.username).split()[0] + ("'s Revenge" if user.role == "pirate" else "'s Shield"))
        db.add(v)
        db.flush()
    return v


def faction_of(user: User) -> str:
    return "pirate" if user.role == "pirate" else "marine"


def vessel_payload(v: PlayerVessel) -> dict:
    return {"id": "v%d" % v.id, "vessel_id": v.id, "name": v.name, "type": v.kind, "capacity": 1, "unit": "",
            "x": v.x, "z": v.z, "hd": v.hd, "cargo": 0, "home": None, "job": None}


def stats_payload(v: PlayerVessel) -> dict:
    return {"earnings": v.earnings, "raids": v.raids, "kills": v.kills, "saves": v.saves, "losses": v.losses}


def leaderboard(db: Session, kind: str, limit: int = 10) -> list[PlayerVessel]:
    return db.scalars(select(PlayerVessel).where(PlayerVessel.kind == kind).order_by(PlayerVessel.earnings.desc(), PlayerVessel.id).limit(limit)).all()


def report(db: Session, user: User, event: str, data: dict) -> dict:
    """Apply one game event for a pirate or marine and return what was paid."""
    v = vessel_of(db, user)
    if not v:
        raise ServiceError("Only pirates and marines report raids and bounties")
    paid, target = 0.0, str(data.get("target", ""))[:80]
    if v.kind == "pirate":
        if event == "raid":
            paid = max(0.0, min(float(data.get("loot", 0) or 0), RAID_CAP))
            v.raids += 1
            if data.get("sank"):
                v.kills += 1
                paid += SINK_BONUS
        elif event != "lost":
            raise ServiceError("Pirates report raids")
    else:
        if event == "pirate_destroyed":
            paid, v.kills = BOUNTY_PIRATE, v.kills + 1
        elif event == "drive_off":
            paid, v.saves = BOUNTY_DRIVE_OFF, v.saves + 1
        elif event == "escort":
            secs = max(0.0, float(data.get("seconds", 0) or 0))
            paid = min(secs * ESCORT_RATE, ESCORT_CAP)
            if secs >= 20:
                v.raids += 1
        elif event != "lost":
            raise ServiceError("Marines report bounties and escorts")
    if event == "lost":
        v.losses += 1
    v.earnings = round(v.earnings + paid, 2)
    if event != "escort" or paid:
        audit(db, user.username, event, v.kind, v.name, (target + " " + (f"${paid:,.0f}" if paid else "")).strip())
    return {"paid": paid, "stats": stats_payload(v)}


def seed_players(db: Session) -> None:
    """Demo pirate and marine (idempotent)."""
    for faction, username, name, pw, vessel in (("pirate", "blackgull", "Mara Teague", "pirate123", "Black Gull"),
                                                ("marine", "marine", "Lt. Dev Rao", "marine123", "BNS Resolute")):
        if not db.scalar(select(User.id).where(User.username == username)):
            register_player(db, faction=faction, username=username, full_name=name, email=f"{username}@sea.example", password=pw, vessel_name=vessel)
    db.commit()


# ------------------------------------------------------------- pages
router = APIRouter()


def require_player(request: Request, user: User | None = Depends(current_user)) -> User:
    from .security import PortalLoginRequired
    if not user or user.role not in PLAY_ROLES:
        raise PortalLoginRequired()
    return user


def page(request: Request, name: str, user, **ctx):
    flashes = request.session.pop("flash", [])
    return templates.TemplateResponse(request, name, {"user": user, "flashes": flashes, "path": request.url.path, "FACTIONS": FACTIONS, **ctx})


@router.get("/join/{faction}", response_class=HTMLResponse)
def join_page(request: Request, faction: str):
    if faction not in FACTIONS:
        return RedirectResponse("/", status_code=303)
    return page(request, "join.html", None, faction=faction, F=FACTIONS[faction], form=request.session.pop("join_form", {}))


@router.post("/join/{faction}")
def join(request: Request, faction: str, username: str = Form(...), full_name: str = Form(...), email: str = Form(...),
         password: str = Form(...), vessel_name: str = Form(""), db: Session = Depends(get_db)):
    try:
        u = register_player(db, faction=faction, username=username, full_name=full_name, email=email, password=password, vessel_name=vessel_name)
        db.commit()
    except ServiceError as e:
        db.rollback(); flash(request, str(e), "error")
        request.session["join_form"] = {"username": username, "full_name": full_name, "email": email, "vessel_name": vessel_name}
        return RedirectResponse(f"/join/{faction}", status_code=303)
    request.session["uid"] = u.id
    flash(request, ("Welcome to the Brethren, captain. Your skiff is waiting." if faction == "pirate"
                    else "Commission granted. Your frigate is ready to sail."))
    return RedirectResponse("/sea", status_code=303)


@router.get("/sea", response_class=HTMLResponse)
def sea_home(request: Request, db: Session = Depends(get_db), user: User = Depends(require_player)):
    v = vessel_of(db, user); db.commit()
    f = faction_of(user)
    return page(request, "sea_home.html", user, v=v, faction=f, F=FACTIONS[f],
                pirates=leaderboard(db, "pirate"), marines=leaderboard(db, "navy"))


@router.post("/sea/rename")
def rename(request: Request, name: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_player)):
    v = vessel_of(db, user)
    name = " ".join(name.split())[:40]
    if name:
        if v.kind == "navy" and not name.upper().startswith("BNS "):
            name = "BNS " + name
        v.name = name; db.commit(); flash(request, "Vessel renamed to " + name + ".")
    return RedirectResponse("/sea", status_code=303)


@router.get("/sea/logout")
def sea_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/portal/login", status_code=303)
