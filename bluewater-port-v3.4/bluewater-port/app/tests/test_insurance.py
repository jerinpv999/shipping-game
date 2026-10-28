from fastapi.testclient import TestClient

from bluewater.main import app


def signin(u, p):
    c = TestClient(app)
    assert c.post("/api/game/login", json={"username": u, "password": p}).status_code == 200
    return c


def fresh_company(name, user):
    from bluewater import marketplace as MK
    from bluewater.db import SessionLocal
    from bluewater.models import Company
    with SessionLocal() as db:
        u = MK.register_company(db, username=user, full_name="Owner " + name, email=f"{user}@x.example", password="secret123",
                                company_name=name, company_type="both", country="India")
        db.flush(); db.get(Company, u.company_id).status = "approved"; db.commit()
        return u.company_id


def test_insured_ship_lost_to_pirates_pays_out_and_is_replaced(client):
    from bluewater.db import SessionLocal
    from bluewater.models import Company, Ship, TransitRequest
    cid = fresh_company("Reef Runner Lines", "reefrunner")
    web = TestClient(app); web.post("/portal/login", data={"username": "reefrunner", "password": "secret123"})
    # buy a feeder with standard cover in one go
    web.post("/portal/market/ship", data={"model": "feeder", "name": "Reef Runner", "flag": "India", "plan": "standard"})
    with SessionLocal() as db:
        co = db.get(Company, cid); ship = db.query(Ship).filter_by(name="Reef Runner").one()
        ship.status = "approved"; db.commit(); sid = ship.id
        assert round(co.balance) == 60_000_000 - 18_000_000 - 216_000
    assert "Standard hull" in web.get("/portal/insurance").text
    g = signin("reefrunner", "secret123")
    jobs = g.get(f"/api/game/jobs?ship_id={sid}").json()["board"]
    job = next(j for j in jobs if j["fits"] and j["cargo_type"] == "container")
    assert g.post(f"/api/game/jobs/{job['id']}/accept", json={"ship_id": sid}).status_code == 200
    g.post(f"/api/game/jobs/{job['id']}/loaded", json={"ship_id": sid})
    out = g.post("/api/game/ship-lost", json={"ship_id": sid, "cause": "pirates"}).json()
    assert out["insured"] and out["liability"] == job["quantity"] * 25_000
    kinds = {c["kind"]: c for c in out["claims"]}
    assert kinds["hull"]["amount"] == 18_000_000 * 0.8 - 250_000 and kinds["hull"]["status"] == "under_review"
    assert kinds["cargo"]["amount"] == out["liability"] * 0.8
    # repeat report is harmless
    assert g.post("/api/game/ship-lost", json={"ship_id": sid, "cause": "pirates"}).json()["already"]
    with SessionLocal() as db:
        assert db.get(Ship, sid).status == "lost"
        r = db.get(TransitRequest, job["id"]); assert r.status == "lost"
        assert db.query(TransitRequest).filter(TransitRequest.description.like(f"Replacement for {job['ref']}%")).count() == 1
    me = g.get("/api/game/me").json()
    assert me["ships"] == [] and "lost at sea" in me["note"]
    # the port authority pays the claims
    admin = TestClient(app); admin.post("/admin/login", data={"username": "admin", "password": "admin123"})
    page = admin.get("/admin/insurance").text
    assert "Reef Runner" in page and "under review" in page.lower()
    from bluewater.models import InsuranceClaim
    with SessionLocal() as db:
        ids = [c.id for c in db.query(InsuranceClaim).filter_by(company_id=cid).all()]
        before = db.get(Company, cid).balance
    for i in ids:
        admin.post(f"/admin/insurance/claims/{i}", data={"decision": "pay"})
    with SessionLocal() as db:
        co = db.get(Company, cid)
        assert round(co.balance - before) == round(kinds["hull"]["amount"] + kinds["cargo"]["amount"])
    # replace her from the fleet page: approved straight away, insured again
    web.post(f"/portal/fleet/{sid}/replace", data={"name": "Reef Runner II", "plan": "premium"})
    with SessionLocal() as db:
        new = db.query(Ship).filter_by(name="Reef Runner II").one()
        assert new.status == "approved" and new.company_id == cid
    assert any(s["name"] == "Reef Runner II" for s in g.get("/api/game/me").json()["ships"])


def test_basic_cover_excludes_piracy_and_uninsured_gets_nothing(client):
    from bluewater.db import SessionLocal
    from bluewater.models import Ship
    fresh_company("Tidewater Freight", "tidewater")
    web = TestClient(app); web.post("/portal/login", data={"username": "tidewater", "password": "secret123"})
    web.post("/portal/market/ship", data={"model": "feeder", "name": "Tide One", "flag": "India", "plan": "basic"})
    web.post("/portal/market/ship", data={"model": "feeder", "name": "Tide Two", "flag": "India", "plan": ""})
    with SessionLocal() as db:
        for s in db.query(Ship).filter(Ship.name.in_(["Tide One", "Tide Two"])): s.status = "approved"
        db.commit()
        one, two = (db.query(Ship).filter_by(name=n).one().id for n in ("Tide One", "Tide Two"))
    g = signin("tidewater", "secret123")
    out = g.post("/api/game/ship-lost", json={"ship_id": one, "cause": "pirates"}).json()
    assert out["claims"][0]["status"] == "rejected" and "Piracy" in out["claims"][0]["note"]
    out2 = g.post("/api/game/ship-lost", json={"ship_id": two, "cause": "collision"}).json()
    assert out2["insured"] is False and out2["claims"] == []
    # someone else can't sink your ship
    assert signin("captain", "captain123").post("/api/game/ship-lost", json={"ship_id": one}).status_code == 403


def test_claims_auto_settle(client):
    from datetime import timedelta
    from bluewater import insurance as INS
    from bluewater.db import SessionLocal
    from bluewater.models import Company, InsuranceClaim, Ship
    cid = fresh_company("Slow Review Co", "slowco")
    with SessionLocal() as db:
        from bluewater import marketplace as MK
        co = db.get(Company, cid)
        ship = MK.buy_ship(db, co, "feeder", "Slow Boat", "India", "t"); ship.status = "approved"
        INS.buy_policy(db, co, ship, "premium", "t")
        INS.report_loss(db, ship, "collision", "t"); db.commit()
        c = db.query(InsuranceClaim).filter_by(company_id=cid).one()
        assert c.amount == 18_000_000 and c.status == "under_review"
        c.created_at = c.created_at - timedelta(minutes=11); db.commit()
        bal = db.get(Company, cid).balance
        assert INS.settle_due(db) == 1; db.commit()
        assert db.get(InsuranceClaim, c.id).status == "paid" and db.get(Company, cid).balance == bal + 18_000_000
