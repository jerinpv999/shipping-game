from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def now():
    return datetime.now(timezone.utc)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    full_name: Mapped[str] = mapped_column(String(120), default="")
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # admin, customs, gate, warehouse, viewer, company
    email: Mapped[str] = mapped_column(String(160), default="")
    licence: Mapped[str] = mapped_column(String(80), default="")          # seafarers: certificate of competency
    experience_years: Mapped[int] = mapped_column(Integer, default=0)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    company = relationship("Company", back_populates="users")


class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    reg_no: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    company_type: Mapped[str] = mapped_column(String(20), default="both")  # shipping_line, haulier, both
    country: Mapped[str] = mapped_column(String(60), default="")
    contact_email: Mapped[str] = mapped_column(String(160), default="")
    phone: Mapped[str] = mapped_column(String(40), default="")
    balance: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, approved, suspended
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    users = relationship("User", back_populates="company")
    ships = relationship("Ship", back_populates="company")
    vehicles = relationship("Vehicle", back_populates="company")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    amount: Mapped[float] = mapped_column(Float)  # positive = money in
    kind: Mapped[str] = mapped_column(String(30))  # opening_balance, purchase, freight, port_dues, adjustment
    description: Mapped[str] = mapped_column(String(200), default="")
    balance_after: Mapped[float] = mapped_column(Float, default=0)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    company = relationship("Company")


class Port(Base):
    __tablename__ = "ports"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(10), unique=True)       # e.g. BWMER
    game_id: Mapped[str] = mapped_column(String(30), unique=True)    # id used by the 3D game
    name: Mapped[str] = mapped_column(String(120))
    island: Mapped[str] = mapped_column(String(80))
    terminal_type: Mapped[str] = mapped_column(String(20))           # container, crude, lng
    role: Mapped[str] = mapped_column(String(20))                    # hub, export, import


class Ship(Base):
    __tablename__ = "ships"
    id: Mapped[int] = mapped_column(primary_key=True)
    imo: Mapped[str] = mapped_column(String(12), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    ship_type: Mapped[str] = mapped_column(String(20))               # container, crude, lng
    flag: Mapped[str] = mapped_column(String(60), default="")
    owner: Mapped[str] = mapped_column(String(120), default="")
    capacity: Mapped[float] = mapped_column(Float, default=0)
    capacity_unit: Mapped[str] = mapped_column(String(10), default="TEU")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, approved, suspended, lost
    game_id: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)
    current_port_id: Mapped[int | None] = mapped_column(ForeignKey("ports.id"), nullable=True)
    live_status: Mapped[str] = mapped_column(String(160), default="")
    live_cargo: Mapped[float] = mapped_column(Float, default=0)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    model: Mapped[str] = mapped_column(String(60), default="")
    price: Mapped[float] = mapped_column(Float, default=0)            # purchase price, the basis for hull insurance
    lost_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    replaced_by: Mapped[str] = mapped_column(String(120), default="")   # name of the ship bought to replace a lost one
    # last position reported by the 3D game for company-owned ships
    game_x: Mapped[float | None] = mapped_column(Float, nullable=True)
    game_z: Mapped[float | None] = mapped_column(Float, nullable=True)
    game_hd: Mapped[float | None] = mapped_column(Float, nullable=True)
    game_cargo: Mapped[float] = mapped_column(Float, default=0)
    game_state: Mapped[str] = mapped_column(String(20), default="")
    game_driver: Mapped[str] = mapped_column(String(80), default="")
    game_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    current_port = relationship("Port")
    company = relationship("Company", back_populates="ships")


class Industry(Base):
    __tablename__ = "industries"
    id: Mapped[int] = mapped_column(primary_key=True)
    reg_no: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    sector: Mapped[str] = mapped_column(String(60))
    goods: Mapped[str] = mapped_column(String(120))
    hs_code: Mapped[str] = mapped_column(String(10), default="")
    port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"))
    contact: Mapped[str] = mapped_column(String(120), default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, approved, suspended, lost
    game_id: Mapped[str | None] = mapped_column(String(30), nullable=True, unique=True)
    live_ready: Mapped[float] = mapped_column(Float, default=0)
    live_stock: Mapped[float] = mapped_column(Float, default=0)
    live_status: Mapped[str] = mapped_column(String(20), default="")
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    port = relationship("Port")


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[int] = mapped_column(primary_key=True)
    plate: Mapped[str] = mapped_column(String(20), unique=True)
    vehicle_type: Mapped[str] = mapped_column(String(30), default="road_train")  # road_train, truck, tanker_truck
    haulier: Mapped[str] = mapped_column(String(120), default="")
    trailers: Mapped[int] = mapped_column(Integer, default=2)
    max_containers: Mapped[int] = mapped_column(Integer, default=2)
    status: Mapped[str] = mapped_column(String(20), default="active")  # pending, active, blocked
    inside_port_id: Mapped[int | None] = mapped_column(ForeignKey("ports.id"), nullable=True)
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    inside_port = relationship("Port")
    company = relationship("Company", back_populates="vehicles")


class Warehouse(Base):
    __tablename__ = "warehouses"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))
    port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"))
    kind: Mapped[str] = mapped_column(String(20), default="container_yard")  # container_yard, tank_farm
    capacity: Mapped[float] = mapped_column(Float, default=0)
    unit: Mapped[str] = mapped_column(String(10), default="boxes")
    bulk_level: Mapped[float] = mapped_column(Float, default=0)  # tank farms: current quantity
    live_inbound: Mapped[float] = mapped_column(Float, default=0)
    live_outbound: Mapped[float] = mapped_column(Float, default=0)
    live_level: Mapped[float] = mapped_column(Float, default=0)
    live_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    port = relationship("Port")


class SupplyOrder(Base):
    __tablename__ = "supply_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    order_no: Mapped[str] = mapped_column(String(20), unique=True)
    shipper_id: Mapped[int] = mapped_column(ForeignKey("industries.id"))
    consignee_id: Mapped[int] = mapped_column(ForeignKey("industries.id"))
    origin_port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"))
    destination_port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"))
    goods: Mapped[str] = mapped_column(String(120))
    hs_code: Mapped[str] = mapped_column(String(10))
    containers_planned: Mapped[int] = mapped_column(Integer, default=1)
    value_per_container: Mapped[float] = mapped_column(Float, default=25000)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    ship_id: Mapped[int | None] = mapped_column(ForeignKey("ships.id"), nullable=True)
    # the consolidated sea booking (transit request) this order travels on
    sea_request_id: Mapped[int | None] = mapped_column(ForeignKey("transit_requests.id", use_alter=True, name="fk_order_sea_request"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    shipper = relationship("Industry", foreign_keys=[shipper_id])
    consignee = relationship("Industry", foreign_keys=[consignee_id])
    origin_port = relationship("Port", foreign_keys=[origin_port_id])
    destination_port = relationship("Port", foreign_keys=[destination_port_id])
    ship = relationship("Ship")
    containers = relationship("Container", back_populates="order")
    sea_request = relationship("TransitRequest", foreign_keys=[sea_request_id])


class Container(Base):
    __tablename__ = "containers"
    id: Mapped[int] = mapped_column(primary_key=True)
    number: Mapped[str] = mapped_column(String(11), unique=True)  # ISO 6346, e.g. BWFU1234565
    size: Mapped[str] = mapped_column(String(6), default="40HC")
    order_id: Mapped[int | None] = mapped_column(ForeignKey("supply_orders.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="at_industry")
    # at_industry, in_yard_outbound, on_ship, in_yard_inbound, on_road, delivered
    customs_status: Mapped[str] = mapped_column(String(30), default="none")
    # none, export_pending, export_cleared, import_pending, import_cleared, held
    warehouse_id: Mapped[int | None] = mapped_column(ForeignKey("warehouses.id"), nullable=True)
    ship_id: Mapped[int | None] = mapped_column(ForeignKey("ships.id"), nullable=True)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    gross_weight_kg: Mapped[float] = mapped_column(Float, default=18000)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)
    order = relationship("SupplyOrder", back_populates="containers")
    warehouse = relationship("Warehouse")
    ship = relationship("Ship")
    vehicle = relationship("Vehicle")


class StockMovement(Base):
    __tablename__ = "stock_movements"
    id: Mapped[int] = mapped_column(primary_key=True)
    warehouse_id: Mapped[int] = mapped_column(ForeignKey("warehouses.id"))
    container_id: Mapped[int | None] = mapped_column(ForeignKey("containers.id"), nullable=True)
    direction: Mapped[str] = mapped_column(String(4))  # in, out
    quantity: Mapped[float] = mapped_column(Float, default=1)
    source: Mapped[str] = mapped_column(String(30))     # gate, vessel, manual, game
    reference: Mapped[str] = mapped_column(String(120), default="")
    created_by: Mapped[str] = mapped_column(String(60), default="system")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    warehouse = relationship("Warehouse")
    container = relationship("Container")


class VesselCall(Base):
    __tablename__ = "vessel_calls"
    id: Mapped[int] = mapped_column(primary_key=True)
    ship_id: Mapped[int] = mapped_column(ForeignKey("ships.id"))
    port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"))
    status: Mapped[str] = mapped_column(String(20), default="berthed")  # expected, berthed, departed
    operation: Mapped[str] = mapped_column(String(20), default="")      # load, discharge, both
    quantity: Mapped[float] = mapped_column(Float, default=0)
    arrived_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    departed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source: Mapped[str] = mapped_column(String(20), default="manual")
    ship = relationship("Ship")
    port = relationship("Port")


class GateTransaction(Base):
    __tablename__ = "gate_transactions"
    id: Mapped[int] = mapped_column(primary_key=True)
    port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"))
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicles.id"))
    direction: Mapped[str] = mapped_column(String(4))  # in, out
    containers: Mapped[str] = mapped_column(Text, default="")  # comma-separated numbers
    container_count: Mapped[int] = mapped_column(Integer, default=0)
    purpose: Mapped[str] = mapped_column(String(160), default="")
    clerk: Mapped[str] = mapped_column(String(60), default="system")
    source: Mapped[str] = mapped_column(String(20), default="manual")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    port = relationship("Port")
    vehicle = relationship("Vehicle")


class CustomsDeclaration(Base):
    __tablename__ = "customs_declarations"
    id: Mapped[int] = mapped_column(primary_key=True)
    decl_no: Mapped[str] = mapped_column(String(20), unique=True)
    decl_type: Mapped[str] = mapped_column(String(10))  # export, import
    order_id: Mapped[int] = mapped_column(ForeignKey("supply_orders.id"))
    port_id: Mapped[int] = mapped_column(ForeignKey("ports.id"))
    hs_code: Mapped[str] = mapped_column(String(10))
    declared_value: Mapped[float] = mapped_column(Float, default=0)
    duty_rate: Mapped[float] = mapped_column(Float, default=0)
    duty_amount: Mapped[float] = mapped_column(Float, default=0)
    status: Mapped[str] = mapped_column(String(20), default="submitted")
    # submitted, under_review, inspection, held, cleared, rejected
    officer: Mapped[str] = mapped_column(String(60), default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    submitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    order = relationship("SupplyOrder")
    port = relationship("Port")


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(60))
    action: Mapped[str] = mapped_column(String(80))
    entity: Mapped[str] = mapped_column(String(40))
    entity_ref: Mapped[str] = mapped_column(String(60), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GameEvent(Base):
    __tablename__ = "game_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class GameState(Base):
    __tablename__ = "game_state"
    __table_args__ = (UniqueConstraint("key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(40))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TransitRequest(Base):
    """A paid cargo job that registered companies can accept and carry with their own ship or vehicle."""
    __tablename__ = "transit_requests"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    mode: Mapped[str] = mapped_column(String(10))            # sea, road
    cargo_type: Mapped[str] = mapped_column(String(20))      # container, crude, lng
    description: Mapped[str] = mapped_column(String(200), default="")
    origin: Mapped[str] = mapped_column(String(120))
    destination: Mapped[str] = mapped_column(String(120))
    origin_port_id: Mapped[int | None] = mapped_column(ForeignKey("ports.id"), nullable=True)
    destination_port_id: Mapped[int | None] = mapped_column(ForeignKey("ports.id"), nullable=True)
    quantity: Mapped[float] = mapped_column(Float)
    unit: Mapped[str] = mapped_column(String(10))
    rate: Mapped[float] = mapped_column(Float)               # total payout in USD
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(20), default="open")  # open, accepted, in_transit, delivered, cancelled
    order_id: Mapped[int | None] = mapped_column(ForeignKey("supply_orders.id"), nullable=True)
    order_refs: Mapped[str] = mapped_column(Text, default="")   # all orders on a consolidated sea booking
    company_id: Mapped[int | None] = mapped_column(ForeignKey("companies.id"), nullable=True)
    ship_id: Mapped[int | None] = mapped_column(ForeignKey("ships.id"), nullable=True)
    vehicle_id: Mapped[int | None] = mapped_column(ForeignKey("vehicles.id"), nullable=True)
    posted_by: Mapped[str] = mapped_column(String(60), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    origin_port = relationship("Port", foreign_keys=[origin_port_id])
    destination_port = relationship("Port", foreign_keys=[destination_port_id])
    order = relationship("SupplyOrder", foreign_keys=[order_id])
    company = relationship("Company")
    ship = relationship("Ship")
    vehicle = relationship("Vehicle")


class JobApplication(Base):
    """A seafarer asking to join a company's crew."""
    __tablename__ = "job_applications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    position: Mapped[str] = mapped_column(String(30), default="captain")
    message: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default="pending")  # pending, accepted, rejected, withdrawn
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    user = relationship("User")
    company = relationship("Company")


class CrewMember(Base):
    """A seafarer working for a company, optionally assigned to command one ship."""
    __tablename__ = "crew_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    position: Mapped[str] = mapped_column(String(30), default="captain")
    ship_id: Mapped[int | None] = mapped_column(ForeignKey("ships.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, removed
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    user = relationship("User")
    company = relationship("Company")
    ship = relationship("Ship")


class PlayerVessel(Base):
    """A pirate skiff or navy frigate commanded by one player (roles pirate and marine)."""
    __tablename__ = "player_vessels"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    kind: Mapped[str] = mapped_column(String(10))                 # pirate, navy
    name: Mapped[str] = mapped_column(String(60))
    x: Mapped[float | None] = mapped_column(Float, nullable=True)
    z: Mapped[float | None] = mapped_column(Float, nullable=True)
    hd: Mapped[float | None] = mapped_column(Float, nullable=True)
    state: Mapped[str] = mapped_column(String(20), default="")
    seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    earnings: Mapped[float] = mapped_column(Float, default=0)     # plunder for pirates, pay and bounties for marines
    raids: Mapped[int] = mapped_column(Integer, default=0)        # pirates: raids · marines: escorts
    kills: Mapped[int] = mapped_column(Integer, default=0)         # pirates: ships sunk · marines: pirates destroyed
    saves: Mapped[int] = mapped_column(Integer, default=0)        # marines: attacks broken up
    losses: Mapped[int] = mapped_column(Integer, default=0)       # times this player's vessel was sunk
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    user = relationship("User")


class InsurancePolicy(Base):
    """Hull and cargo cover for one company ship, bought in 30-day terms."""
    __tablename__ = "insurance_policies"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    ship_id: Mapped[int] = mapped_column(ForeignKey("ships.id"))
    plan: Mapped[str] = mapped_column(String(20))                  # basic, standard, premium
    insured_value: Mapped[float] = mapped_column(Float)            # hull value
    premium: Mapped[float] = mapped_column(Float)                  # per 30-day term
    status: Mapped[str] = mapped_column(String(20), default="active")  # active, expired, cancelled, claimed
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    company = relationship("Company")
    ship = relationship("Ship")


class InsuranceClaim(Base):
    __tablename__ = "insurance_claims"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref: Mapped[str] = mapped_column(String(20), unique=True)
    policy_id: Mapped[int | None] = mapped_column(ForeignKey("insurance_policies.id"), nullable=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"))
    ship_id: Mapped[int] = mapped_column(ForeignKey("ships.id"))
    kind: Mapped[str] = mapped_column(String(10))                  # hull, cargo
    cause: Mapped[str] = mapped_column(String(30))                 # pirates, collision, other
    job_ref: Mapped[str] = mapped_column(String(20), default="")
    loss: Mapped[float] = mapped_column(Float, default=0)          # value lost
    amount: Mapped[float] = mapped_column(Float, default=0)        # payable under the policy
    status: Mapped[str] = mapped_column(String(20), default="under_review")  # under_review, paid, rejected
    note: Mapped[str] = mapped_column(String(300), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(60), default="")
    policy = relationship("InsurancePolicy")
    company = relationship("Company")
    ship = relationship("Ship")
