from fastapi.testclient import TestClient

from bluewater.main import app


def signin(u, p):
    c = TestClient(app)
    r = c.post("/api/game/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return c, r.json()


def test_join_pirate_and_raid(client):
    c = TestClient(app)
    r = c.post("/join/pirate", data={"username": "redjack", "full_name": "Jack Rourke", "email": "jack@reef.example",
                                     "password": "redjack123", "vessel_name": "Sea Wolf"}, follow_redirects=False)
    assert r.headers["location"] == "/sea"
    page = c.get("/sea").text
    assert "Sea Wolf" in page and "Most wanted pirates" in page
    me = c.get("/api/game/me").json()
    assert me["kind"] == "pirate" and me["ships"][0]["type"] == "pirate" and me["ships"][0]["id"].startswith("v")
    vid = me["ships"][0]["id"]
    c.post("/api/game/ship-state", json={"ships": [{"id": vid, "x": -900, "z": 400, "hd": 1.2, "state": "sailing"}]})
    out = c.post("/api/game/report", json={"event": "raid", "target": "Coral Trader", "loot": 1_200_000, "sank": True}).json()
    assert out["paid"] == 1_700_000 and out["stats"]["kills"] == 1
    # capped
    assert c.post("/api/game/report", json={"event": "raid", "loot": 99_000_000}).json()["paid"] == 3_000_000
    # pirates can't claim bounties
    assert c.post("/api/game/report", json={"event": "pirate_destroyed"}).status_code == 400
    # others see the skiff
    m, _ = signin("marine", "marine123")
    ghosts = m.get("/api/game/world").json()["ships"]
    assert any(g["name"] == "Sea Wolf" and g["type"] == "pirate" for g in ghosts)


def test_marine_bounties_and_escorts(client):
    m, me = signin("marine", "marine123")
    assert me["kind"] == "marine" and me["ships"][0]["type"] == "navy" and me["ships"][0]["name"].startswith("BNS ")
    assert m.post("/api/game/report", json={"event": "pirate_destroyed", "target": "Pirate skiff 3"}).json()["paid"] == 250_000
    assert m.post("/api/game/report", json={"event": "escort", "target": "Coral Trader", "seconds": 60}).json()["paid"] == 24_000
    assert m.post("/api/game/report", json={"event": "escort", "seconds": 100000}).json()["paid"] == 240_000
    st = m.post("/api/game/report", json={"event": "drive_off"}).json()["stats"]
    assert st["kills"] == 1 and st["saves"] == 1 and st["raids"] == 2
    assert m.post("/api/game/report", json={"event": "raid", "loot": 5}).status_code == 400
    lb = m.get("/api/game/leaderboard").json()
    assert lb["marines"][0]["name"] == "BNS Resolute"


def test_player_roles_stay_out_of_admin_and_crew(client):
    c = TestClient(app)
    c.post("/portal/login", data={"username": "blackgull", "password": "pirate123"})
    assert c.get("/crew", follow_redirects=True).url.path == "/sea"
    assert c.get("/admin", follow_redirects=True).url.path != "/admin"
    assert "Join" in TestClient(app).get("/").text
    # seafarers and companies can't report raids
    s, _ = signin("captain", "captain123")
    assert s.post("/api/game/report", json={"event": "raid", "loot": 10}).status_code == 400
