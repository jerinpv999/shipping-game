from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from . import admin, api, crewpages, factions, gameapi, insurance, portal
from .config import VERSION, settings
from .db import Base, SessionLocal, engine, sync_schema, wait_for_db
from .security import LoginRequired, PortalLoginRequired
from .marketplace import seed_marketplace
from .seed import seed

STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    wait_for_db()
    Base.metadata.create_all(engine)
    added = sync_schema()
    if added:
        print("Upgraded database, added columns:", ", ".join(added), flush=True)
    with SessionLocal() as db:
        seed(db)
        if settings.seed_demo:
            seed_marketplace(db)
            from .crew import seed_crew
            seed_crew(db)
            factions.seed_players(db)
            insurance.seed_insurance(db)
    yield


app = FastAPI(title="Bluewater Port Management", version=VERSION, lifespan=lifespan,
              description="Ship, industry and vehicle registry, warehouses, supply orders, gate and customs for the Bluewater archipelago.")
app.add_middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="bluewater_session",
                   max_age=60 * 60 * 12, same_site="lax")
app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
app.include_router(api.router)
app.include_router(admin.router)
app.include_router(portal.router)
app.include_router(gameapi.router)
app.include_router(crewpages.router)
app.include_router(factions.router)
app.include_router(insurance.router)


@app.exception_handler(LoginRequired)
async def login_required(request: Request, exc: LoginRequired):
    return RedirectResponse("/admin/login", status_code=303)


@app.exception_handler(PortalLoginRequired)
async def portal_login_required(request: Request, exc: PortalLoginRequired):
    return RedirectResponse("/portal/login", status_code=303)


@app.get("/", include_in_schema=False)
def root(request: Request):
    return admin.templates.TemplateResponse(request, "landing.html", {})


@app.get("/game", include_in_schema=False)
def game():
    return FileResponse(STATIC / "game" / "index.html", media_type="text/html")
