from fastapi.testclient import TestClient

from bluewater.main import app


def test_seafarer_applies_gets_hired_and_sails_a_job(client):
    # 1. a player registers as a seafarer and applies to the demo company
    sea = TestClient(app)
    r = sea.post("/crew/register", data={"username": "mira", "full_name": "Mira Thomas", "email": "mira@sea.example",
                                         "password": "mira12345", "licence": "Chief Mate", "experience_years": 6}, follow_redirects=False)
    assert r.headers["location"] == "/crew"
    assert "Companies hiring" in sea.get("/crew").text
    assert sea.get("/api/game/me").json()["ships"] == []
    from bluewater.db import SessionLocal
    from bluewater.models import Company, CrewMember, JobApplication, Ship, TransitRequest
    with SessionLocal() as db:
        co = db.query(Company).filter_by(name="Oceanic Carriers").one()
        ship = db.query(Ship).filter_by(name="Oceanic Dawn").one()
        co_id, ship_id = co.id, ship.id
    sea.post("/crew/apply", data={"company_id": co_id, "position": "captain", "message": "Ten years on feeders"})
    # 2. the company hires her and gives her the ship
    owner = TestClient(app)
    owner.post("/portal/login", data={"username": "oceanic", "password": "oceanic123"})
    assert "Mira Thomas" in owner.get("/portal/crew").text
    with SessionLocal() as db:
        app_id = db.query(JobApplication).filter_by(status="pending").order_by(JobApplication.id.desc()).first().id
    owner.post(f"/portal/crew/applications/{app_id}", data={"decision": "accept"})
    with SessionLocal() as db:
        mid = db.query(CrewMember).join(CrewMember.user).filter_by(username="mira").one().id
    owner.post(f"/portal/crew/{mid}/assign", data={"ship_id": str(ship_id)})
    # 3. in the game she sees only that ship, takes a job, loads and delivers it; the company is paid
    me = sea.get("/api/game/me").json()
    assert me["status"] == "user" and me["kind"] == "crew" and [s["name"] for s in me["ships"]] == ["Oceanic Dawn"]
    jobs = sea.get(f"/api/game/jobs?ship_id={ship_id}").json()
    job = next(j for j in jobs["board"] if j["fits"] and j["cargo_type"] == "container")
    before = jobs["balance"]
    assert sea.post(f"/api/game/jobs/{job['id']}/accept", json={"ship_id": ship_id}).status_code == 200
    assert sea.post(f"/api/game/jobs/{job['id']}/loaded", json={"ship_id": ship_id}).json()["job"]["status"] == "in_transit"
    out = sea.post(f"/api/game/jobs/{job['id']}/delivered", json={"ship_id": ship_id}).json()
    assert out["job"]["status"] == "delivered" and round(out["balance"] - before) == round(job["rate"] * 0.97)
    # 4. her position is shared with other players
    sea.post("/api/game/ship-state", json={"ships": [{"id": ship_id, "x": -600, "z": 300, "hd": 1.2, "cargo": 0, "state": "sailing"}]})
    other = TestClient(app)
    world = other.get("/api/game/world").json()["ships"]
    assert any(s["name"] == "Oceanic Dawn" and s["driver"] == "Mira Thomas" for s in world)
    # she cannot command a ship that isn't hers
    assert sea.post("/api/game/jobs/1/accept", json={"ship_id": 1}).status_code == 403


def test_owner_commands_all_company_ships_and_adds_staff(client):
    owner = TestClient(app)
    me = owner.post("/api/game/login", json={"username": "oceanic", "password": "oceanic123"}).json()
    assert me["kind"] == "owner" and "Oceanic Dawn" in [s["name"] for s in me["ships"]]
    r = owner.post("/portal/crew/add", data={"full_name": "Joe Ferris", "username": "joeferris", "password": "joe123456",
                                             "position": "officer", "licence": "Officer of the Watch", "ship_id": ""})
    assert r.status_code == 200 and "Joe Ferris" in owner.get("/portal/crew").text
    joe = TestClient(app)
    assert joe.post("/api/game/login", json={"username": "joeferris", "password": "joe123456"}).json()["note"].startswith("Your company hasn't assigned")
    staff = TestClient(app)
    assert staff.post("/api/game/login", json={"username": "admin", "password": "admin123"}).status_code == 403


def test_game_page_served(client):
    assert "Bluewater Canal" in client.get("/game").text


def test_resign_and_transfer(client):
    from bluewater import marketplace as MK
    from bluewater.db import SessionLocal
    from bluewater.models import Company, CrewMember
    # a second shipping company
    with SessionLocal() as db:
        u = MK.register_company(db, username="tidal", full_name="Lena Ortiz", email="lena@tidal.example", password="tidal12345",
                                company_name="Tidal Lines", company_type="both", country="Spain")
        db.flush(); db.get(Company, u.company_id).status = "approved"; db.commit()
        tidal_id = u.company_id
    sea = TestClient(app)
    sea.post("/portal/login", data={"username": "captain", "password": "captain123"})
    assert "Oceanic Carriers" in sea.get("/crew").text
    # apply to transfer while still employed
    sea.post("/crew/apply", data={"company_id": tidal_id, "position": "captain", "message": "Looking for a bigger ship"})
    tidal = TestClient(app); tidal.post("/portal/login", data={"username": "tidal", "password": "tidal12345"})
    with SessionLocal() as db:
        from bluewater.models import JobApplication
        aid = db.query(JobApplication).filter_by(company_id=tidal_id, status="pending").one().id
    tidal.post(f"/portal/crew/applications/{aid}", data={"decision": "accept"})
    with SessionLocal() as db:
        rows = db.query(CrewMember).join(CrewMember.user).filter_by(username="captain").all()
        active = [m for m in rows if m.status == "active"]
        assert len(active) == 1 and active[0].company_id == tidal_id and active[0].ship_id is None
    # resign needs the confirmation box
    sea.post("/crew/resign", data={})
    with SessionLocal() as db:
        assert db.query(CrewMember).join(CrewMember.user).filter(CrewMember.status == "active").filter_by(username="captain").count() == 1
    sea.post("/crew/resign", data={"confirm": "yes"})
    with SessionLocal() as db:
        assert db.query(CrewMember).join(CrewMember.user).filter(CrewMember.status == "active").filter_by(username="captain").count() == 0
    assert sea.get("/api/game/me").json()["ships"] == []


def test_transfer_shows_on_company_pages(client):
    from bluewater.db import SessionLocal
    from bluewater import marketplace as MK
    from bluewater.models import Company
    with SessionLocal() as db:
        u = MK.register_company(db, username="harbourco", full_name="Ana Silva", email="ana@h.example", password="harbour123",
                                company_name="Harbour Line", company_type="both", country="Portugal")
        db.flush(); db.get(Company, u.company_id).status = "approved"; db.commit(); hid = u.company_id
    sea = TestClient(app); sea.post("/portal/login", data={"username": "captain", "password": "captain123"})
    oc = TestClient(app); oc.post("/portal/login", data={"username": "oceanic", "password": "oceanic123"})
    from bluewater.models import JobApplication
    with SessionLocal() as db:
        ocid = db.query(Company).filter_by(name="Oceanic Carriers").one().id
    sea.post("/crew/apply", data={"company_id": ocid, "position": "captain", "message": "Back again"})
    with SessionLocal() as db:
        oaid = db.query(JobApplication).filter_by(company_id=ocid, status="pending").one().id
    oc.post(f"/portal/crew/applications/{oaid}", data={"decision": "accept"})
    page = sea.get("/crew").text
    assert "Resign" in page and "Apply to transfer" in page
    sea.post("/crew/apply", data={"company_id": hid, "position": "captain", "message": ""})
    h = TestClient(app); h.post("/portal/login", data={"username": "harbourco", "password": "harbour123"})
    assert "currently with" in h.get("/portal/crew").text
    with SessionLocal() as db:
        aid = db.query(JobApplication).filter_by(company_id=hid, status="pending").one().id
    h.post(f"/portal/crew/applications/{aid}", data={"decision": "accept"})
    assert "Moved to Harbour Line" in oc.get("/portal/crew").text
