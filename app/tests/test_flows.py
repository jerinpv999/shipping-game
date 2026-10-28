from bluewater.services import iso6346_check_digit

ADMIN_PAGES = ["/admin", "/admin/ships", "/admin/industries", "/admin/vehicles", "/admin/warehouses",
               "/admin/containers", "/admin/orders", "/admin/gate", "/admin/customs", "/admin/vessels",
               "/admin/live", "/admin/audit", "/admin/users"]


def test_iso6346_check_digit():
    # Published example: CSQU3054383
    assert iso6346_check_digit("CSQU305438") == 3


def test_health(client):
    assert client.get("/api/health").json()["service"] == "bluewater"


def test_admin_requires_login(client):
    r = client.get("/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin/login"


def test_every_admin_page_renders(client):
    r = client.post("/admin/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
    assert r.status_code == 303
    for page in ADMIN_PAGES:
        res = client.get(page)
        assert res.status_code == 200, page
        assert "Bluewater" in res.text
    # detail pages
    assert client.get("/admin/warehouses/1").status_code == 200
    assert client.get("/admin/warehouses/3").status_code == 200
    assert client.get("/admin/orders/4").status_code == 200
    assert client.get("/admin/customs/1").status_code == 200
    box = client.get("/api/containers").json()[0]["number"]
    assert client.get(f"/admin/containers/{box}").status_code == 200


def test_full_supply_chain_via_api(api):
    ports = {p["island"]: p["id"] for p in api.get("/api/ports").json()}
    inds = {i["name"]: i["id"] for i in api.get("/api/industries").json()}
    ships = {s["name"]: s["id"] for s in api.get("/api/ships").json()}

    o = api.post("/api/orders", json={"shipper_id": inds["Halden Electronics"], "consignee_id": inds["Bayline Furniture"],
                                      "containers_planned": 2, "value_per_container": 50000}).json()
    oid = o["id"]
    assert api.post(f"/api/orders/{oid}/confirm").json()["status"] == "confirmed"
    boxes = api.post(f"/api/orders/{oid}/stuff").json()["containers"]
    assert len(boxes) == 2

    # vehicle and gate-in at Meridian
    api.post("/api/vehicles", json={"plate": "TEST-RT-1", "haulier": "Test Haulage"})
    r = api.post("/api/gate", json={"port_id": ports["Meridian"], "plate": "TEST-RT-1", "direction": "in", "containers": boxes})
    assert r.status_code == 201, r.text
    assert api.post("/api/gate", json={"port_id": ports["Meridian"], "plate": "TEST-RT-1", "direction": "out"}).status_code == 201

    # cannot load before export customs
    api.post(f"/api/ships/{ships['Meridian Star']}/berth", json={"port_id": ports["Meridian"]})
    r = api.post(f"/api/orders/{oid}/load", json={"ship_id": ships["Meridian Star"]})
    assert r.status_code == 400

    api.post(f"/api/orders/{oid}/export-declaration")
    decl = [d for d in api.get("/api/customs").json() if d["order"] == o["order_no"]][0]
    assert api.post(f"/api/customs/{decl['id']}/clear", json={"notes": "ok"}).json()["status"] == "cleared"

    assert api.post(f"/api/orders/{oid}/load", json={"ship_id": ships["Meridian Star"]}).json()["status"] == "shipped"
    # discharge requires the vessel at Coral Bay
    assert api.post(f"/api/orders/{oid}/discharge").status_code == 400
    api.post(f"/api/ships/{ships['Meridian Star']}/berth", json={"port_id": ports["Coral Bay"]})
    assert api.post(f"/api/orders/{oid}/discharge").json()["status"] == "import_customs"

    imp = [d for d in api.get("/api/customs").json() if d["order"] == o["order_no"] and d["type"] == "import"][0]
    assert imp["duty_rate"] == 0.05 and imp["duty_amount"] == 5000

    # gate-out refused until import cleared
    api.post("/api/gate", json={"port_id": ports["Coral Bay"], "plate": "TEST-RT-1", "direction": "in"})
    r = api.post("/api/gate", json={"port_id": ports["Coral Bay"], "plate": "TEST-RT-1", "direction": "out", "containers": boxes})
    assert r.status_code == 400 and "customs" in r.json()["detail"]
    api.post(f"/api/customs/{imp['id']}/clear")
    r = api.post("/api/gate", json={"port_id": ports["Coral Bay"], "plate": "TEST-RT-1", "direction": "out", "containers": boxes})
    assert r.status_code == 201, r.text
    assert api.post(f"/api/orders/{oid}/deliver").json()["status"] == "delivered"
    hist = api.get(f"/api/containers/{boxes[0]}").json()["history"]
    assert [h["direction"] for h in hist] == ["in", "out", "in", "out"]


def test_yard_capacity_and_blocked_vehicle(api):
    vid = [v for v in api.get("/api/vehicles").json() if v["plate"] == "KL-01-TK-4410"][0]
    assert vid["max_containers"] == 1


def test_game_ingest(client):
    assert client.post("/api/game/events", json={"type": "ship_berthed", "data": {"ship": "Petra Spirit", "port": "Vanta"}}).json()["ok"]
    client.post("/api/game/events", json={"type": "cargo_discharged", "data": {"ship": "Petra Spirit", "port": "Vanta", "quantity": 600000, "unit": "bbl"}})
    client.post("/api/game/events", json={"type": "gate_in", "data": {"port": "Meridian", "vehicle": "Road train M-3", "origin": "Halden Electronics", "boxes": 2, "goods": "Electronics"}})
    client.post("/api/game/snapshot", json={"ports": [{"id": "vanta", "stock": 1300000, "cap": 3000000}],
                                            "hubs": [{"id": "meridian", "inbound": 30, "outbound": 90,
                                                      "industries": [{"id": "halden", "ready": 5, "stock": 3, "status": "Running"}]}],
                                            "ships": [{"id": "spirit", "cargo": 0, "status": "Alongside Vanta"}]})
    calls = client.get("/api/vessel-calls", headers={"X-API-Key": "test-key"}).json()
    assert any(c["ship"] == "Petra Spirit" and c["quantity"] == 600000 for c in calls)
    whs = {w["code"]: w for w in client.get("/api/warehouses", headers={"X-API-Key": "test-key"}).json()}
    assert whs["WH-MER-01"]["live_level"] == 120 and whs["TF-VAN-01"]["live_level"] == 1300000


def test_roles_enforced(client):
    c2 = client.__class__(client.app)
    c2.post("/admin/login", data={"username": "gate", "password": "gate123"})
    r = c2.post("/api/customs/1/clear")
    assert r.status_code == 403
