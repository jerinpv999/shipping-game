"""Marine insurance: hull and cargo cover for company ships, losses reported by the game, claims and payouts.

Underwriter: Archipelago Mutual (fictional). A policy runs for 30 days and is paid up front.
When a ship sinks in the game, the port records the loss, the ship's job is handled (cargo aboard is lost
and the company owes the shipper its value), and claims are raised under any active policy.
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import marketplace as MK
from .admin import flash, guard, render as admin_render, templates
from .db import get_db
from .models import Company, CrewMember, InsuranceClaim, InsurancePolicy, Ship, TransitRequest, User, now
from .security import require_company, require_user
from .services import ServiceError, audit, next_ref

UNDERWRITER = "Archipelago Mutual"
TERM_DAYS = 30
AUTO_SETTLE_MINUTES = 10      # claims nobody reviews are settled by the underwriter after this long

# key: label, hull cover, cargo cover, piracy covered, deductible, premium (share of hull value per term)
PLANS = {
    "basic":    {"label": "Basic hull",          "hull": 0.60, "cargo": 0.0, "piracy": False, "deductible": 500_000, "rate": 0.006,
                 "blurb": "60 % of the hull value. Collisions and accidents only: piracy is excluded. No cargo cover."},
    "standard": {"label": "Standard hull & cargo", "hull": 0.80, "cargo": 0.80, "piracy": True, "deductible": 250_000, "rate": 0.012,
                 "blurb": "80 % of the hull and 80 % of cargo liability. Piracy covered."},
    "premium":  {"label": "Premium full cover",  "hull": 1.00, "cargo": 1.00, "piracy": True, "deductible": 0, "rate": 0.020,
                 "blurb": "Full hull value and full cargo liability, no deductible. Piracy covered."},
}
# what the shipper's goods are worth, per unit, when a cargo is lost at sea
CARGO_VALUE = {"container": 25_000, "crude": 75, "lng": 220}


def ship_value(ship: Ship) -> float:
    if ship.price:
        return ship.price
    for label, stype, cap, unit, price, _ in MK.SHIP_MODELS.values():
        if label == ship.model:
            return price
    return {"container": 18_000_000, "crude": 35_000_000, "lng": 48_000_000}.get(ship.ship_type, 20_000_000)


def model_key(ship: Ship) -> str | None:
    for key, (label, *_rest) in MK.SHIP_MODELS.items():
        if label == ship.model:
            return key
    return next((k for k, m in MK.SHIP_MODELS.items() if m[1] == ship.ship_type), None)


def premium_for(ship: Ship, plan: str) -> float:
    return round(ship_value(ship) * PLANS[plan]["rate"], 2)


def cargo_value(r: TransitRequest) -> float:
    return round(r.quantity * CARGO_VALUE.get(r.cargo_type, 0), 2)


def active_policy(db: Session, ship: Ship) -> InsurancePolicy | None:
    p = db.scalar(select(InsurancePolicy).where(InsurancePolicy.ship_id == ship.id, InsurancePolicy.status == "active")
                  .order_by(InsurancePolicy.id.desc()))
    if p and _aware(p.valid_until) <= now():
        p.status = "expired"
        return None
    return p


def _aware(dt):
    from datetime import timezone
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def buy_policy(db: Session, company: Company, ship: Ship, plan: str, actor: str) -> InsurancePolicy:
    if plan not in PLANS:
        raise ServiceError("Choose an insurance plan")
    if ship.company_id != company.id:
        raise ServiceError("That ship isn't yours")
    if ship.status == "lost":
        raise ServiceError(f"{ship.name} has been lost. Insure its replacement instead")
    current = active_policy(db, ship)
    if current:
        if current.plan == plan:
            raise ServiceError(f"{ship.name} is already insured ({PLANS[plan]['label']}) until {_aware(current.valid_until):%d %b}. Renew it instead")
        current.status = "cancelled"   # switching plans: the new term starts today
    premium = premium_for(ship, plan)
    if company.balance < premium:
        raise ServiceError(f"Not enough funds: the premium is ${premium:,.0f}")
    p = InsurancePolicy(ref=next_ref(db, InsurancePolicy, "ref", "POL"), company_id=company.id, ship_id=ship.id, plan=plan,
                        insured_value=ship_value(ship), premium=premium, valid_until=now() + timedelta(days=TERM_DAYS))
    db.add(p)
    db.flush()
    MK.post_ledger(db, company, -premium, "insurance_premium", f"{p.ref} {PLANS[plan]['label']} · {ship.name} · {TERM_DAYS} days")
    audit(db, actor, "insure", "ship", ship.imo, f"{p.ref} {plan} ${premium:,.0f}")
    return p


def renew_policy(db: Session, company: Company, p: InsurancePolicy, actor: str) -> None:
    if p.company_id != company.id:
        raise ServiceError("That policy isn't yours")
    if p.status not in ("active", "expired") or p.ship.status == "lost":
        raise ServiceError("Only active or expired policies on a ship you still own can be renewed")
    if company.balance < p.premium:
        raise ServiceError(f"Not enough funds: the premium is ${p.premium:,.0f}")
    start = max(_aware(p.valid_until), now())
    p.valid_until, p.status = start + timedelta(days=TERM_DAYS), "active"
    MK.post_ledger(db, company, -p.premium, "insurance_premium", f"{p.ref} renewal · {p.ship.name} · {TERM_DAYS} days")
    audit(db, actor, "renew", "policy", p.ref, f"${p.premium:,.0f}")


def cancel_policy(db: Session, company: Company, p: InsurancePolicy, actor: str) -> None:
    if p.company_id != company.id or p.status != "active":
        raise ServiceError("Only your active policies can be cancelled")
    p.status = "cancelled"
    audit(db, actor, "cancel", "policy", p.ref, "no refund")


def report_loss(db: Session, ship: Ship, cause: str, actor: str) -> dict:
    """The ship sank. Record the loss, deal with its job, and raise claims under any active policy. Idempotent."""
    cause = cause if cause in ("pirates", "collision") else "other"
    if ship.status == "lost":
        claims = db.scalars(select(InsuranceClaim).where(InsuranceClaim.ship_id == ship.id)).all()
        return {"already": True, "claims": [claim_payload(c) for c in claims], "liability": 0}
    company = db.get(Company, ship.company_id)
    policy = active_policy(db, ship)
    ship.status, ship.lost_at, ship.game_seen_at = "lost", now(), None
    ship.live_status = f"Lost at sea ({cause})"
    for m in db.scalars(select(CrewMember).where(CrewMember.ship_id == ship.id)).all():
        m.ship_id = None
    audit(db, actor, "ship_lost", "ship", ship.imo, f"{ship.name} · {cause}")

    liability, job_ref = 0.0, ""
    job = db.scalar(select(TransitRequest).where(TransitRequest.ship_id == ship.id, TransitRequest.status.in_(MK.OPEN_STATES)))
    if job:
        job_ref = job.ref
        if job.status == "in_transit":
            # the cargo went down with her: the company owes the shipper, and the port re-posts the leg for replacement goods
            job.status, job.completed_at = "lost", now()
            liability = cargo_value(job)
            MK.post_ledger(db, company, -liability, "cargo_liability", f"Cargo lost with {ship.name} · {job.ref}")
            again = MK.post_request(db, mode=job.mode, cargo_type=job.cargo_type, origin=job.origin, destination=job.destination,
                                    quantity=job.quantity, rate=job.rate, days=3, description=f"Replacement for {job.ref}, lost at sea",
                                    origin_port=job.origin_port, destination_port=job.destination_port, order=job.order, posted_by="port")
            again.order_refs = job.order_refs
        else:
            job.status, job.company_id, job.ship_id, job.accepted_at = "open", None, None, None
            audit(db, "port", "release", "transit", job.ref, f"{ship.name} lost before loading")

    claims = []
    if policy:
        plan = PLANS[policy.plan]
        policy.status = "claimed"
        excluded = cause == "pirates" and not plan["piracy"]
        why = f"Piracy is excluded under {plan['label']}" if excluded else ""
        hull = max(0.0, policy.insured_value * plan["hull"] - plan["deductible"])
        claims.append(_claim(db, policy, ship, "hull", cause, job_ref, ship_value(ship), hull, why))
        if liability:
            why_c = why or ("" if plan["cargo"] else f"{plan['label']} has no cargo cover")
            claims.append(_claim(db, policy, ship, "cargo", cause, job_ref, liability, liability * plan["cargo"], why_c))
    return {"already": False, "claims": [claim_payload(c) for c in claims], "liability": liability,
            "insured": bool(policy), "plan": PLANS[policy.plan]["label"] if policy else None, "job": job_ref}


def _claim(db, policy, ship, kind, cause, job_ref, loss, amount, rejected_because):
    c = InsuranceClaim(ref=next_ref(db, InsuranceClaim, "ref", "CLM"), policy_id=policy.id, company_id=ship.company_id, ship_id=ship.id,
                       kind=kind, cause=cause, job_ref=job_ref, loss=round(loss, 2), amount=round(amount if not rejected_because else 0, 2))
    if rejected_because or c.amount <= 0:
        c.status, c.note, c.decided_at, c.decided_by = "rejected", rejected_because or "Loss is within the deductible", now(), UNDERWRITER
    db.add(c)
    db.flush()
    audit(db, UNDERWRITER, "claim", "ship", ship.imo, f"{c.ref} {kind} ${c.amount:,.0f} {c.status}")
    return c


def claim_payload(c: InsuranceClaim) -> dict:
    return {"ref": c.ref, "kind": c.kind, "loss": c.loss, "amount": c.amount, "status": c.status, "note": c.note}


def settle(db: Session, c: InsuranceClaim, approve: bool, actor: str, note: str = "") -> None:
    if c.status != "under_review":
        raise ServiceError("This claim has already been settled")
    c.decided_at, c.decided_by, c.note = now(), actor, (note or c.note)[:300]
    if approve:
        c.status = "paid"
        MK.post_ledger(db, c.company, c.amount, "insurance_payout", f"{c.ref} {c.kind} claim · {c.ship.name}")
    else:
        c.status = "rejected"
    audit(db, actor, "settle", "claim", c.ref, f"{c.status} ${c.amount:,.0f}")


def settle_due(db: Session) -> int:
    """The underwriter pays claims that have waited longer than AUTO_SETTLE_MINUTES without a decision."""
    cutoff = now() - timedelta(minutes=AUTO_SETTLE_MINUTES)
    due = [c for c in db.scalars(select(InsuranceClaim).where(InsuranceClaim.status == "under_review")).all() if _aware(c.created_at) <= cutoff]
    for c in due:
        settle(db, c, True, UNDERWRITER, "Settled by the underwriter")
    return len(due)


def replace_ship(db: Session, company: Company, lost: Ship, name: str, plan: str, actor: str) -> Ship:
    """Buy the same model again. A like-for-like replacement keeps the company's vetting, so it's approved straight away."""
    if lost.company_id != company.id or lost.status != "lost":
        raise ServiceError("Only your lost ships can be replaced")
    if lost.replaced_by:
        raise ServiceError(f"{lost.name} has already been replaced by {lost.replaced_by}")
    key = model_key(lost)
    if not key:
        raise ServiceError("That model is no longer sold")
    ship = MK.buy_ship(db, company, key, name, lost.flag, actor)
    ship.status, lost.replaced_by = "approved", ship.name
    audit(db, "port", "approve", "ship", ship.imo, f"replacement for {lost.name}")
    if plan in PLANS:
        buy_policy(db, company, ship, plan, actor)
    return ship


ROMAN = ["II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]


def replacement_name(db: Session, ship: Ship) -> str:
    """Oceanic Dawn → Oceanic Dawn II → Oceanic Dawn III …, skipping names already registered."""
    words = ship.name.split()
    base = " ".join(words[:-1]) if len(words) > 1 and words[-1] in ROMAN else ship.name
    for r in ROMAN:
        cand = f"{base} {r}"
        if not db.scalar(select(Ship.id).where(Ship.name == cand)):
            return cand
    return base + " Nova"


def seed_insurance(db: Session) -> None:
    """Demo: Oceanic Dawn carries a standard policy (idempotent)."""
    if db.scalar(select(InsurancePolicy.id).limit(1)):
        return
    ship = db.scalar(select(Ship).where(Ship.name == "Oceanic Dawn"))
    if ship and ship.company_id and ship.status != "lost":
        if not ship.price:
            ship.price = ship_value(ship)
        buy_policy(db, db.get(Company, ship.company_id), ship, "standard", "seed")
        db.commit()


# ------------------------------------------------------------- portal pages
router = APIRouter()
templates.env.globals.update(PLANS=PLANS, UNDERWRITER=UNDERWRITER, TERM_DAYS=TERM_DAYS)


def _company_page(request, db, user, name, **ctx):
    flashes = request.session.pop("flash", [])
    co = db.get(Company, user.company_id)
    return templates.TemplateResponse(request, name, {"user": user, "company": co, "flashes": flashes, "path": request.url.path, **ctx})


def _run(request, db, fn, ok):
    try:
        out = fn(); db.commit()
        if ok:
            flash(request, ok)
        return out
    except ServiceError as e:
        db.rollback(); flash(request, str(e), "error")


@router.get("/portal/insurance", response_class=HTMLResponse)
def portal_insurance(request: Request, db: Session = Depends(get_db), user: User = Depends(require_company)):
    if settle_due(db):
        db.commit()
    co = db.get(Company, user.company_id)
    policies = {s.id: active_policy(db, s) for s in co.ships}
    db.commit()
    claims = db.scalars(select(InsuranceClaim).where(InsuranceClaim.company_id == co.id).order_by(InsuranceClaim.id.desc())).all()
    history = db.scalars(select(InsurancePolicy).where(InsurancePolicy.company_id == co.id).order_by(InsurancePolicy.id.desc()).limit(30)).all()
    return _company_page(request, db, user, "portal_insurance.html", policies=policies, claims=claims, history=history,
                         premium_for=premium_for, ship_value=ship_value)


@router.post("/portal/insurance/buy")
def portal_buy(request: Request, ship_id: int = Form(...), plan: str = Form(...), db: Session = Depends(get_db), user: User = Depends(require_company)):
    co, ship = db.get(Company, user.company_id), db.get(Ship, ship_id)
    if ship:
        _run(request, db, lambda: buy_policy(db, co, ship, plan, user.username), f"{ship.name} insured: {PLANS.get(plan, {}).get('label', plan)} for {TERM_DAYS} days.")
    return RedirectResponse("/portal/insurance", status_code=303)


@router.post("/portal/insurance/{pid}/{action}")
def portal_policy(request: Request, pid: int, action: str, db: Session = Depends(get_db), user: User = Depends(require_company)):
    co, p = db.get(Company, user.company_id), db.get(InsurancePolicy, pid)
    if p and action == "renew":
        _run(request, db, lambda: renew_policy(db, co, p, user.username), f"{p.ref} renewed for {TERM_DAYS} days.")
    elif p and action == "cancel":
        _run(request, db, lambda: cancel_policy(db, co, p, user.username), f"{p.ref} cancelled.")
    return RedirectResponse("/portal/insurance", status_code=303)


@router.post("/portal/fleet/{sid}/replace")
def portal_replace(request: Request, sid: int, name: str = Form(...), plan: str = Form(""), db: Session = Depends(get_db),
                   user: User = Depends(require_company)):
    co, lost = db.get(Company, user.company_id), db.get(Ship, sid)
    if lost:
        _run(request, db, lambda: replace_ship(db, co, lost, name, plan, user.username),
             f"{name.strip()} bought to replace {lost.name}, approved and ready to sail.")
    return RedirectResponse("/portal/fleet", status_code=303)


# ------------------------------------------------------------- admin pages
@router.get("/admin/insurance", response_class=HTMLResponse)
def admin_insurance(request: Request, db: Session = Depends(get_db), user: User = Depends(require_user)):
    claims = db.scalars(select(InsuranceClaim).order_by(InsuranceClaim.status != "under_review", InsuranceClaim.id.desc()).limit(200)).all()
    policies = db.scalars(select(InsurancePolicy).order_by(InsurancePolicy.id.desc()).limit(200)).all()
    for p in policies:
        if p.status == "active" and _aware(p.valid_until) <= now():
            p.status = "expired"
    db.commit()
    lost = db.scalars(select(Ship).where(Ship.status == "lost").order_by(Ship.lost_at.desc())).all()
    return admin_render(request, "insurance.html", user, claims=claims, policies=policies, lost=lost)


@router.post("/admin/insurance/claims/{cid}")
def admin_settle(request: Request, cid: int, decision: str = Form(...), note: str = Form(""), db: Session = Depends(get_db),
                 user: User = Depends(require_user)):
    if guard(request, user, "companies"):
        c = db.get(InsuranceClaim, cid)
        if c:
            try:
                settle(db, c, decision == "pay", user.username, note)
                db.commit()
                flash(request, f"{c.ref} {'paid: $' + format(c.amount, ',.0f') if decision == 'pay' else 'rejected'}.")
            except ServiceError as e:
                db.rollback(); flash(request, str(e), "error")
    return RedirectResponse("/admin/insurance", status_code=303)
