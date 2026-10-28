import hashlib
import hmac
import secrets

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .db import get_db
from .models import User

PLAYER_ROLES = ("company", "seafarer", "pirate", "marine")   # accounts that use the portal and the game, never the admin panel
ROLES = ["admin", "customs", "gate", "warehouse", "viewer"]  # staff roles; portal users have role "company"

# What each role may change. Everyone signed in can read everything.
PERMISSIONS = {
    "registry": {"admin"},                       # ships, industries, vehicles, ports
    "warehouse": {"admin", "warehouse"},
    "orders": {"admin", "warehouse"},
    "vessel": {"admin", "warehouse"},
    "gate": {"admin", "gate"},
    "customs": {"admin", "customs"},
    "users": {"admin"},
    "companies": {"admin"},
    "transit": {"admin", "warehouse"},
}


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"pbkdf2${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return hmac.compare_digest(check, digest)


def can(user: User | None, area: str) -> bool:
    return bool(user and user.active and user.role in PERMISSIONS.get(area, set()))


def current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    uid = request.session.get("uid")
    if not uid:
        return None
    user = db.get(User, uid)
    return user if user and user.active else None


class LoginRequired(Exception):
    pass


class PortalLoginRequired(Exception):
    pass


def require_user(user: User | None = Depends(current_user)) -> User:
    """Port-authority staff only. Company accounts are sent to the portal."""
    if not user:
        raise LoginRequired()
    if user.role in PLAYER_ROLES:
        raise PortalLoginRequired()
    return user


def require_seafarer(user: User | None = Depends(current_user)) -> User:
    if not user or user.role != "seafarer":
        raise PortalLoginRequired()
    return user


def require_company(user: User | None = Depends(current_user)) -> User:
    if not user or user.role != "company" or not user.company_id:
        raise PortalLoginRequired()
    return user


def api_user(request: Request, db: Session = Depends(get_db)) -> User:
    """JSON API: accepts a signed-in admin-panel session or the X-API-Key header."""
    key = request.headers.get("x-api-key")
    if settings.api_key and key and hmac.compare_digest(key, settings.api_key):
        user = db.scalar(select(User).where(User.username == settings.admin_user))
        if user:
            return user
    user = current_user(request, db)
    if not user or user.role in PLAYER_ROLES:
        raise HTTPException(status_code=401, detail="Sign in to the admin panel or send a valid X-API-Key header")
    return user


def require_area(user: User, area: str) -> None:
    if not can(user, area):
        raise HTTPException(status_code=403, detail=f"Your role ({user.role}) cannot change {area} records")
