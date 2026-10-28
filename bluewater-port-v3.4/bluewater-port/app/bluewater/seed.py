"""Initial data: the eight ports of the 3D game, plus demo ships, industries, vehicles and orders."""
import random

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services as S
from .config import settings
from .models import Industry, Port, Ship, SupplyOrder, User, Vehicle, Warehouse
from .security import hash_password

PORTS = [
    ("BWMER", "meridian", "Meridian Container Terminal", "Meridian", "container", "hub"),
    ("BWCOR", "coral", "Coral Bay Container Port", "Coral Bay", "container", "hub"),
    ("BWPET", "petra", "Petra Crude Oil Terminal", "Petra", "crude", "export"),
    ("BWVAN", "vanta", "Vanta Refinery Jetty", "Vanta", "crude", "import"),
    ("BWSAL", "saltmarsh", "Saltmarsh Container Quay", "Saltmarsh", "container", "hub"),
    ("BWTRN", "tern", "Tern Island Container Port", "Tern Island", "container", "hub"),
    ("BWNGS", "northgas", "Northgas LNG Plant", "Northgas", "lng", "export"),
    ("BWHPT", "harbor", "Harbor Point Gas Terminal", "Harbor Pt", "lng", "import"),
]
WAREHOUSES = [
    ("WH-MER-01", "Meridian container yard", "meridian", "container_yard", 400, "boxes"),
    ("WH-COR-01", "Coral Bay container yard", "coral", "container_yard", 400, "boxes"),
    ("WH-SAL-01", "Saltmarsh container yard", "saltmarsh", "container_yard", 400, "boxes"),
    ("WH-TRN-01", "Tern Island container yard", "tern", "container_yard", 400, "boxes"),
    ("TF-PET-01", "Petra crude tank farm", "petra", "tank_farm", 3_000_000, "bbl"),
    ("TF-VAN-01", "Vanta refinery tanks", "vanta", "tank_farm", 3_000_000, "bbl"),
    ("TF-NGS-01", "Northgas LNG tanks", "northgas", "tank_farm", 600_000, "m³"),
    ("TF-HPT-01", "Harbor Point LNG tanks", "harbor", "tank_farm", 600_000, "m³"),
]
SHIPS = [
    ("9734563", "Meridian Star", "container", "Marshall Islands", "Bluewater Freight", 120, "TEU", "approved", "star"),
    ("9612208", "Petra Spirit", "crude", "Liberia", "Bluewater Freight", 600_000, "bbl", "approved", "spirit"),
    ("9805116", "Northern Aurora", "lng", "Bahamas", "Bluewater Freight", 150_000, "m³", "approved", "aurora"),
    ("9701891", "Coral Trader", "container", "Panama", "Reefline Shipping", 120, "TEU", "approved", "trader"),
    ("9623441", "Tern Voyager", "container", "Singapore", "Bluewater Freight", 120, "TEU", "approved", "voyager"),
    ("9822451", "Island Pioneer", "container", "Singapore", "Archipel Lines", 90, "TEU", "pending", None),
]
INDUSTRIES = [
    ("IND-0001", "Halden Electronics", "Manufacturing", "Electronics", "8517", "meridian", "halden"),
    ("IND-0002", "Meridian Textiles", "Manufacturing", "Garments", "6109", "meridian", "loom"),
    ("IND-0003", "Kestrel Auto Parts", "Manufacturing", "Auto parts", "8708", "meridian", "kestrel"),
    ("IND-0004", "Coral Fresh Foods", "Food processing", "Frozen food", "1604", "coral", "fresh"),
    ("IND-0005", "Bayline Furniture", "Manufacturing", "Furniture", "9403", "coral", "bayline"),
    ("IND-0006", "Reef Retail DC", "Distribution", "Consumer goods", "9503", "coral", "reef"),
    ("IND-0007", "Saltmarsh Ceramics", "Manufacturing", "Tiles", "6907", "saltmarsh", "ceramics"),
    ("IND-0008", "Marsh Agro", "Agriculture", "Fertiliser", "3105", "saltmarsh", "agro"),
    ("IND-0009", "Delta Pharma", "Manufacturing", "Medicines", "3004", "saltmarsh", "pharma"),
    ("IND-0010", "Tern Steelworks", "Manufacturing", "Steel coils", "7208", "tern", "steel"),
    ("IND-0011", "Gannet Paper Mill", "Manufacturing", "Paper", "4802", "tern", "paper"),
    ("IND-0012", "Tern Toys", "Manufacturing", "Toys", "9503", "tern", "toys"),
]


def seed(db: Session) -> None:
    """Idempotent: fills in anything missing, so an older database picks up new ports, industries and ships."""
    if not db.scalar(select(User).where(User.username == settings.admin_user)):
        db.add(User(username=settings.admin_user, full_name="Port Administrator", role="admin",
                    password_hash=hash_password(settings.admin_password)))
    fresh = not db.scalar(select(Port.id).limit(1))
    ports = {p.game_id: p for p in db.scalars(select(Port)).all()}
    for code, gid, name, island, ttype, role in PORTS:
        if gid not in ports:
            p = Port(code=code, game_id=gid, name=name, island=island, terminal_type=ttype, role=role)
            db.add(p); ports[gid] = p
    db.flush()
    have_wh = {w.code for w in db.scalars(select(Warehouse)).all()}
    for code, name, gid, kind, cap, unit in WAREHOUSES:
        if code not in have_wh:
            level = {"petra": 1_800_000, "vanta": 700_000, "northgas": 380_000, "harbor": 150_000}.get(gid, 0)
            db.add(Warehouse(code=code, name=name, port_id=ports[gid].id, kind=kind, capacity=cap, unit=unit, bulk_level=level))
    for imo, name, stype, flag, owner, cap, unit, status, gid in SHIPS:
        ship = db.scalar(select(Ship).where(Ship.name == name))
        if ship:
            if gid and not ship.game_id and not db.scalar(select(Ship.id).where(Ship.game_id == gid)):
                ship.game_id = gid
        elif not db.scalar(select(Ship.id).where(Ship.imo == imo)):
            db.add(Ship(imo=imo, name=name, ship_type=stype, flag=flag, owner=owner, capacity=cap, capacity_unit=unit,
                        status=status, game_id=gid))
    db.flush()
    inds = {}
    for reg, name, sector, goods, hs, gid, game_id in INDUSTRIES:
        i = db.scalar(select(Industry).where(Industry.name == name))
        if i:
            i.port_id, i.game_id, i.goods, i.hs_code = ports[gid].id, game_id, goods, hs
            if i.status == "pending": i.status = "approved"
        else:
            if db.scalar(select(Industry.id).where(Industry.reg_no == reg)):
                reg = S.next_ref(db, Industry, "reg_no", "IND")
            i = Industry(reg_no=reg, name=name, sector=sector, goods=goods, hs_code=hs, port_id=ports[gid].id,
                         contact=f"logistics@{name.split()[0].lower()}.example", status="approved", game_id=game_id)
            db.add(i)
        db.flush()
        inds[game_id] = i
    if not db.scalar(select(Industry.id).where(Industry.name == "Island Breweries")):
        db.add(Industry(reg_no=S.next_ref(db, Industry, "reg_no", "IND"), name="Island Breweries", sector="Food processing",
                        goods="Beverages", hs_code="2203", port_id=ports["tern"].id, contact="ops@islandbrew.example", status="pending"))
    have_v = {v.plate for v in db.scalars(select(Vehicle)).all()}
    for n in range(1, 6):
        for pre in ("M", "C", "S", "T"):
            if f"RT-{pre}-{n}" not in have_v:
                db.add(Vehicle(plate=f"RT-{pre}-{n}", vehicle_type="road_train", haulier="Bluewater Haulage", trailers=2, max_containers=2))
    for plate, vt, hl, tr, mc in (("KL-01-TK-4410", "truck", "Island Express", 1, 1), ("KL-07-TT-0921", "tanker_truck", "Petra Fuels", 1, 0)):
        if plate not in have_v:
            db.add(Vehicle(plate=plate, vehicle_type=vt, haulier=hl, trailers=tr, max_containers=mc))
    for code, name in (("customs", "Customs Officer"), ("gate", "Gate Clerk"), ("warehouse", "Yard Planner")):
        if not db.scalar(select(User).where(User.username == code)):
            db.add(User(username=code, full_name=name, role=code, password_hash=hash_password(code + "123")))
    db.commit()
    if settings.seed_demo and fresh and not db.scalar(select(SupplyOrder.id).limit(1)):
        seed_demo_flow(db, ports, inds)


def seed_demo_flow(db: Session, ports, inds) -> None:
    """Push a few orders through different stages so every screen has something to show."""
    random.seed(7)
    ships = {s.game_id: s for s in db.scalars(select(Ship)).all() if s.game_id}
    star = ships["star"]
    v = {x.plate: x for x in db.scalars(select(Vehicle)).all()}

    def order(shipper, consignee, n, value):
        o = SupplyOrder(order_no=S.next_ref(db, SupplyOrder, "order_no", "ORD"), shipper_id=shipper.id,
                        consignee_id=consignee.id, origin_port_id=shipper.port_id, destination_port_id=consignee.port_id,
                        goods=shipper.goods, hs_code=shipper.hs_code, containers_planned=n, value_per_container=value)
        db.add(o)
        db.flush()
        db.refresh(o)
        return o

    # 1. Draft
    order(inds["kestrel"], inds["bayline"], 2, 31000)
    db.commit()
    # 2. Stuffed and waiting at the factory
    o2 = order(inds["loom"], inds["reef"], 2, 22000)
    S.confirm_order(db, o2, "seed"); S.stuff_containers(db, o2, "seed"); db.commit()
    # 3. In the origin yard, export declaration under review
    o3 = order(inds["halden"], inds["reef"], 2, 64000)
    S.confirm_order(db, o3, "seed"); boxes = S.stuff_containers(db, o3, "seed"); db.commit()
    S.gate_in(db, ports["meridian"], v["RT-M-1"], [b.number for b in boxes], "seed"); db.commit()
    S.gate_out(db, ports["meridian"], v["RT-M-1"], [], "seed"); db.commit()
    d3 = S.submit_export_declaration(db, o3, "seed"); db.commit()
    S.customs_action(db, d3, "review", "customs"); db.commit()
    # 4. Full journey to import customs, waiting for the officer
    o4 = order(inds["fresh"], inds["halden"], 2, 18000)
    S.confirm_order(db, o4, "seed"); boxes = S.stuff_containers(db, o4, "seed"); db.commit()
    S.gate_in(db, ports["coral"], v["RT-C-1"], [b.number for b in boxes], "seed"); db.commit()
    S.gate_out(db, ports["coral"], v["RT-C-1"], [], "seed"); db.commit()
    d4 = S.submit_export_declaration(db, o4, "seed"); db.commit()
    S.customs_action(db, d4, "clear", "customs", "Documents in order"); db.commit()
    coral_trader = db.scalar(select(Ship).where(Ship.name == "Coral Trader"))
    S.berth_ship(db, coral_trader, ports["coral"], "seed"); db.commit()
    S.load_order_on_ship(db, o4, coral_trader, "seed"); db.commit()
    S.berth_ship(db, coral_trader, ports["meridian"], "seed"); db.commit()
    S.discharge_order(db, o4, "seed"); db.commit()
    # 5. Delivered end to end
    o5 = order(inds["bayline"], inds["kestrel"], 1, 15000)
    S.confirm_order(db, o5, "seed"); boxes = S.stuff_containers(db, o5, "seed"); db.commit()
    S.gate_in(db, ports["coral"], v["RT-C-2"], [b.number for b in boxes], "seed"); db.commit()
    S.gate_out(db, ports["coral"], v["RT-C-2"], [], "seed"); db.commit()
    d5 = S.submit_export_declaration(db, o5, "seed"); db.commit()
    S.customs_action(db, d5, "clear", "customs"); db.commit()
    S.berth_ship(db, coral_trader, ports["coral"], "seed"); db.commit()
    S.load_order_on_ship(db, o5, coral_trader, "seed"); db.commit()
    S.berth_ship(db, coral_trader, ports["meridian"], "seed"); db.commit()
    imp = S.discharge_order(db, o5, "seed"); db.commit()
    S.customs_action(db, imp, "clear", "customs", "Duty paid"); db.commit()
    S.gate_in(db, ports["meridian"], v["RT-M-2"], [], "seed"); db.commit()
    S.gate_out(db, ports["meridian"], v["RT-M-2"], [b.number for b in boxes], "seed"); db.commit()
    S.confirm_delivery(db, o5, "seed"); db.commit()
    # leave Coral Trader alongside Meridian and Meridian Star at its berth (matches the game's opening)
    S.berth_ship(db, star, ports["meridian"], "seed"); db.commit()
