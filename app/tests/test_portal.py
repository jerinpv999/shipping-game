from fastapi.testclient import TestClient

from bluewater.main import app
from bluewater.marketplace import OPENING_BALANCE, imo_check_digit


def portal_client():
    return TestClient(app)


def test_imo_check_digit():
    assert imo_check_digit("907472") == 9  # IMO 9074729


def test_register_buy_and_carry(client):
    c = portal_client()
    r = c.post("/portal/register", data={"username": "seafox", "full_name": "Ravi Nair", "email": "ravi@seafox.example",
                                         "password": "seafox123", "company_name": "Seafox Logistics",
                                         "company_type": "both", "country": "India"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/portal"
    page = c.get("/portal").text
    assert "Seafox Logistics" in page and "reviewing" in page
    for path in ["/portal/market", "/portal/fleet", "/portal/requests", "/portal/ledger", "/portal/company"]:
        assert c.get(path).status_code == 200, path

    # buy a feeder and a road train
    c.post("/portal/market/ship", data={"model": "feeder", "name": "Seafox One", "flag": "India"})
    c.post("/portal/market/vehicle", data={"model": "roadtrain", "plate": "KL-07-SF-0001"})
    fleet = c.get("/portal/fleet").text
    assert "Seafox One" in fleet and "KL-07-SF-0001" in fleet
    assert "$41,720,000" in c.get("/portal").text  # 60M - 18M - 280k

    # can't buy beyond balance
    c.post("/portal/market/ship", data={"model": "suezmax", "name": "Too Big", "flag": ""})
    assert "Too Big" not in c.get("/portal/fleet").text

    # staff approve company, ship and vehicle
    staff = TestClient(app)
    staff.post("/admin/login", data={"username": "admin", "password": "admin123"})
    assert staff.get("/admin/companies").status_code == 200
    assert staff.get("/admin/transit").status_code == 200
    from bluewater.db import SessionLocal
    from bluewater.models import Company, Ship, TransitRequest, Vehicle
    with SessionLocal() as db:
        co = db.query(Company).filter_by(name="Seafox Logistics").one()
        ship = db.query(Ship).filter_by(name="Seafox One").one()
        veh = db.query(Vehicle).filter_by(plate="KL-07-SF-0001").one()
        ids = co.id, ship.id, veh.id
    staff.post(f"/admin/companies/{ids[0]}/status", data={"status": "approved"})
    staff.post(f"/admin/ships/{ids[1]}/status", data={"status": "approved"})
    staff.post(f"/admin/vehicles/{ids[2]}/status", data={"status": "active"})
    assert staff.get(f"/admin/companies/{ids[0]}").status_code == 200

    # pick an open container sea job that fits 120 TEU
    staff.post("/admin/transit", data={"mode": "sea", "cargo_type": "container", "origin": "Meridian Container Terminal",
                                       "destination": "Coral Bay Container Port", "quantity": 80, "rate": 400000, "days": 3})
    with SessionLocal() as db:
        req = db.query(TransitRequest).filter_by(rate=400000).one()
        rid = req.id
    board = c.get("/portal/requests?fit=1").text
    assert req.ref in board
    c.post(f"/portal/requests/{rid}/accept", data={"asset_id": ids[1]})
    c.post(f"/portal/requests/{rid}/start")
    c.post(f"/portal/requests/{rid}/deliver")
    with SessionLocal() as db:
        req = db.get(TransitRequest, rid)
        co = db.get(Company, ids[0])
        assert req.status == "delivered"
        assert round(co.balance) == round(OPENING_BALANCE - 18_000_000 - 280_000 + 400_000 * 0.97)


def test_company_cannot_use_admin_and_staff_cannot_use_portal(client):
    c = portal_client()
    c.post("/portal/login", data={"username": "oceanic", "password": "oceanic123"})
    assert c.get("/portal").status_code == 200
    r = c.get("/admin", follow_redirects=False)
    assert r.headers["location"] == "/portal/login"
    assert c.get("/api/orders").status_code == 401
    s = portal_client()
    r = s.post("/portal/login", data={"username": "admin", "password": "admin123"}, follow_redirects=False)
    assert r.headers["location"] == "/portal/login"


def test_order_workflow_posts_transit_jobs(api):
    from bluewater.db import SessionLocal
    from bluewater.models import TransitRequest
    with SessionLocal() as db:
        before = db.query(TransitRequest).filter(TransitRequest.order_id.isnot(None)).count()
    inds = {i["name"]: i["id"] for i in api.get("/api/industries").json()}
    o = api.post("/api/orders", json={"shipper_id": inds["Bayline Furniture"], "consignee_id": inds["Kestrel Auto Parts"],
                                      "containers_planned": 3, "value_per_container": 20000}).json()
    api.post(f"/api/orders/{o['id']}/confirm")
    api.post(f"/api/orders/{o['id']}/stuff")
    with SessionLocal() as db:
        new = db.query(TransitRequest).filter(TransitRequest.order_id == o["id"]).all()
        assert len(new) == 2 and all(r.mode == "road" for r in new)  # 3 boxes -> 2 + 1


def test_landing(client):
    assert "Register" in client.get("/").text


def test_port_consolidates_orders_per_route(api):
    from bluewater import marketplace as MK, services as S
    from bluewater.db import SessionLocal
    from bluewater.models import Industry, Port, SupplyOrder, TransitRequest, Vehicle
    with SessionLocal() as db:
        ports = {p.game_id: p for p in db.query(Port).all()}
        inds = {i.game_id: i for i in db.query(Industry).filter(Industry.game_id.isnot(None)).all()}
        truck = db.query(Vehicle).filter_by(plate="RT-S-1").one()
        made = []
        for shipper, n in (("ceramics", 2), ("agro", 3)):
            o = SupplyOrder(order_no=S.next_ref(db, SupplyOrder, "order_no", "ORD"), shipper_id=inds[shipper].id, consignee_id=inds["steel"].id,
                            origin_port_id=ports["saltmarsh"].id, destination_port_id=ports["tern"].id, goods=inds[shipper].goods,
                            hs_code=inds[shipper].hs_code, containers_planned=n, value_per_container=20000)
            db.add(o); db.flush(); db.refresh(o)
            S.confirm_order(db, o, "t"); boxes = S.stuff_containers(db, o, "t"); db.commit()
            for i in range(0, len(boxes), 2):
                S.gate_in(db, ports["saltmarsh"], truck, [x.number for x in boxes[i:i + 2]], "t"); db.commit()
                S.gate_out(db, ports["saltmarsh"], truck, [], "t"); db.commit()
            d = S.submit_export_declaration(db, o, "t"); db.commit()
            S.customs_action(db, d, "clear", "customs"); db.commit()
            made.append(o.id)
        o1, o2 = db.get(SupplyOrder, made[0]), db.get(SupplyOrder, made[1])
        # 2 boxes alone wait; with the second order (5 boxes) the route is booked as one sea request
        assert o1.sea_request_id is not None and o1.sea_request_id == o2.sea_request_id
        r = db.get(TransitRequest, o1.sea_request_id)
        assert r.mode == "sea" and r.quantity == 5 and o1.order_no in r.order_refs and o2.order_no in r.order_refs
        assert MK.pending_lanes(db) == [] or all(L["origin"].game_id != "saltmarsh" for L in MK.pending_lanes(db))
