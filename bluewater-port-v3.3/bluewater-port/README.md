# Bluewater Port Management

Port-authority back office for the Bluewater archipelago: ship, industry and vehicle registration, warehouse and tank-farm stock, supply orders, gate in/out and customs clearance. It has an admin panel, a REST API, and a live link to the 3D shipping game.

Stack: Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Jinja2 admin panel · Docker Compose.

## Run it

```bash
cp .env.example .env        # change SECRET_KEY and passwords for anything beyond local use
docker compose up --build
```

### Updating from an earlier download

Unzip the new version over the old folder, or into a fresh one, and run `docker compose up --build` again.

- **Image:** the app image is rebuilt on every `up` (`pull_policy: build`), so new code always lands.
- **Database:** on start the app adds any new tables and columns to your existing database, and fills in new ports, industries, ships and the demo company. You don't need to delete your data.
- **Check the version:** the admin sidebar, the start page and `http://localhost:6800/api/health` all show the version. This release is **3.3.0**.

If the start page still has no *Companies* card, the old container is still running. Run `docker compose down`, then `docker compose up --build`. `docker compose down -v` also works, but it deletes the database.

| What | Where |
|---|---|
| Start page | http://localhost:6800 |
| Company portal (register, buy fleet, take cargo) | http://localhost:6800/portal |
| Admin panel | http://localhost:6800/admin |
| 3D game (linked to the database) | http://localhost:6800/game |
| REST API docs (Swagger) | http://localhost:6800/docs |
| Adminer DB browser (optional) | `docker compose --profile tools up`, then http://localhost:6880 (server `db`) |

All host ports are in the 6xxx range, so this stack runs alongside an existing PostgreSQL or other containers:

| Service | Host port | Inside the stack |
|---|---|---|
| App (admin, API, game) | 6800 | app:8000 |
| PostgreSQL | 6543 | db:5432 |
| Adminer (optional) | 6880 | adminer:8080 |

The app always talks to its own database at `db:5432` inside the Compose network, so your other PostgreSQL is never touched. The data lives in its own volume (`bluewater-port_pgdata`). Change any host port in `.env`. To connect from your machine: `psql -h localhost -p 6543 -U bluewater bluewater`.

Demo accounts (created on first start):

| User | Password | Role |
|---|---|---|
| admin | admin123 | everything |
| customs | customs123 | customs decisions |
| gate | gate123 | gate in / out |
| warehouse | warehouse123 | orders, yards, vessels, transit requests |
| oceanic | oceanic123 | **company portal**: demo shipping company with a feeder ship and a road train |
| captain | captain123 | **seafarer**: commands Oceanic Dawn in the game |
| blackgull | pirate123 | **pirate**: skiff Black Gull |
| marine | marine123 | **marine**: frigate BNS Resolute |

On first start the database is seeded with the eight game ports, their warehouses and tank farms, six ships, thirteen industries and twenty-two vehicles. It also creates five orders at different stages, so every screen has data. Set `SEED_DEMO=false` to skip the orders.

## Company portal

Shipping lines and hauliers sign themselves up at `/portal/register`.

1. **Register**: create a company (shipping line, road haulier, or both) and its first user. Every company starts with **$60,000,000** of founding capital.
2. **Approval**: the port authority approves the company under *Registered companies*. Until then it can buy assets but can't take jobs.
3. **Marketplace**: buy ships (feeder or Panamax container ship, Aframax or Suezmax tanker, LNG carrier) and road vehicles (road train, container truck).
   - The price is deducted from the balance at once.
   - Each ship gets a valid IMO number and is registered in the company's name as *pending*.
   - The authority approves ships in the ship registry and vehicles in the vehicle registry. Approved vehicles pass the port gates.
4. **Transit board**: paid cargo jobs.
   - Jobs are posted automatically by the supply chain: a road pickup when containers are stuffed at a factory, a sea leg when export customs clears, and a road delivery when import customs clears.
   - The authority can also post jobs by hand, including crude and LNG charters.
   - The board can filter to jobs your fleet can carry.
5. **Carry and get paid**: accept a job with an idle, approved asset that's big enough, start it, then mark it delivered. The freight is credited, less 3 % port dues. A job that hasn't started can be handed back.
6. **Ledger**: every purchase, freight payment, port due and adjustment, with the running balance.

On the admin side, *Registered companies* shows each company's fleet, jobs and ledger. Admins can approve or suspend a company (suspending also disables its users) and post balance adjustments. *Transit requests* lists every job with its company and assigned asset.

Company accounts can only use the portal, and staff accounts can only use the admin panel.

## Seafarers and crew

Anyone can play a captain without owning a company.

1. **Register** at `/crew/register` as a seafarer with your licence and years at sea.
2. **Apply** to any approved company from *My career* (`/crew`). The company hires or declines you under *Portal → Crew*.
3. **Get a ship**: the company assigns you to one of its approved ships. One commanding officer per ship.
4. **Sail**: sign in at `/game`. Your ship spawns in the ocean marked **YOURS**. Pick a job from the *Company jobs* board, sail to the pickup port, load, cross to the delivery port and unload. The freight is paid to your company.

Companies can also create logins for their own staff under *Portal → Crew → Add staff* and assign them a ship straight away.

### Resigning and changing company

- **Resign:** on *My career*, tick the confirmation box and press *Resign*. You leave the company and give up your ship. A job your ship was carrying stays with the company.
- **Transfer:** while still employed, press *Apply to transfer* next to another company. The new company sees the application marked *Transfer · currently with …*. If it hires you, your old contract ends automatically, your old ship is handed back, and any other pending applications are withdrawn.
- Companies see people who left under *Former crew* ("Left the company" or "Moved to …").

### Company owners in the game

Signing in to `/game` as a company account shows every approved ship of the fleet. Select one and sail it, or let the others run on autopilot. Other players' ships appear in green.

Demo seafarer: **captain / captain123**, hired by Oceanic Carriers and assigned to *Oceanic Dawn*.

## Fleet insurance

Ships sink in the game to pirates or in collisions. When a company ship goes down, it is lost for good: it doesn't come back after 45 seconds the way the AI fleet does. Insurance is what pays for a new one. The underwriter is *Archipelago Mutual*, a fictional company.

**Plans** (*Portal → Insurance*, paid up front for 30 days, priced on the ship's purchase price):

| Plan | Hull paid | Cargo liability paid | Piracy | Deductible | Premium / 30 days |
|---|---|---|---|---|---|
| Basic hull | 60 % | none | excluded | $500,000 | 0.6 % of hull value |
| Standard hull & cargo | 80 % | 80 % | covered | $250,000 | 1.2 % |
| Premium full cover | 100 % | 100 % | covered | none | 2.0 % |

**When a ship sinks**, the game reports it to `POST /api/game/ship-lost`, and the port does the following:
- marks the ship *Lost at sea* and unassigns her crew;
- deals with her job:
  - cargo still waiting to be loaded goes back on the transit board;
  - cargo on board is lost. The company is charged the shipper's goods value as cargo liability, and the leg is re-posted so the supply chain carries on;
- opens a hull claim, plus a cargo claim if liability was charged, under any active policy. Claims under a plan that excludes the cause are rejected at once with the reason.

**Claims** wait for the port authority under *Admin → Insurance claims* (pay or reject, with a note). Any claim still undecided after 10 minutes is paid by the underwriter. Payouts and premiums appear in the company ledger.

**Replacing a ship:** *Portal → Fleet* offers **Replace** on every lost ship. It buys the same model again under a follow-on name (*Oceanic Dawn II*, *III* and so on), can insure it in the same step, and approves it straight away. It also appears in a running game without a reload. Uninsured companies can still buy a new ship from the marketplace if they can afford it.

The demo company's *Oceanic Dawn* starts with a Standard policy.

## Pirates and marines

Two more ways to play, with no company needed. Sign up from the start page.

**Pirates** (`/join/pirate`) get a fast skiff (44 knots) in open water.
- Hunt a cargo ship, match her speed alongside and press **E** to board. Plunder builds up every second while her hull goes down.
- Press **E** again to break off and bank the plunder. Sink her for a $500,000 bonus.
- Navy frigates, AI or marine players, fire missiles at you. Each hit costs 55 % of your hull. Above 30 knots, nearly half of them miss. A sunk skiff loses the raid in progress, and a new one slips out of the reefs after 45 seconds.

**Marines** (`/join/marine`) get a navy frigate in the Bluewater Navy.
- Close within 200 m of a cargo ship and press **E** to escort her. The autopilot keeps station on her quarter and fires at any pirate that closes in. Escorts pay $400 a second, banked every minute.
- Press **F** to fire a missile at the nearest pirate. Each pirate destroyed pays a $250,000 bounty.
- Pirates that see a frigate within 420 m break off their attack. Breaking up an attack in progress pays $40,000.

Each player's page at `/sea` shows earnings, raids or escorts, kills and the two leaderboards. The game reports events to `POST /api/game/report`. The server sets and caps every payout, so a modified game can't award itself a fortune. Pirate and marine vessels show up in other players' games as well.

Demo accounts: **blackgull / pirate123** (pirate, skiff *Black Gull*) and **marine / marine123** (marine, *BNS Resolute*).

## Ship collisions

Hulls are solid. Each ship is modelled as a capsule the length and beam of its hull.

- **Collisions:** ships that touch are pushed apart by mass, so a skiff bounces off a tanker. They lose way and are knocked sideways, and a blow off-centre swings the bow.
- **Damage:** above a walking pace, both hulls take damage that grows with closing speed. A hard enough ram can sink a ship, and repairs happen slowly while she is alongside.
- **AI avoidance:** autopilot ships predict the closest point of approach of every vessel nearby and give way to starboard. They slow down for a ship stopped ahead and wait for the canal in a holding area beside the fairway, so ships leaving the locks have a clear exit.

## What the admin panel does

- **Dashboard**: vessels alongside, customs queue, gate movements, yard and tank utilisation, orders by stage, the audit trail and game events.
- **Registry**
  - **Ships**: register by IMO number, approve or suspend, berth and depart. Only approved ships of the right type can berth at a terminal.
  - **Industries**: register shippers and consignees with an HS code. Only approved industries can book orders.
  - **Vehicles**: register road trains and trucks with a container limit. Blocked vehicles are refused at the gate.
- **Warehouses**
  - Each container yard has an inbound block and an outbound block, with capacity checks and a per-container movement ledger.
  - Tank farms record bulk receipts and releases in bbl or m³.
- **Containers**: ISO 6346 numbers with a verified check digit. Each container shows its full history across yards, gates and vessels.
- **Supply orders**: a guided workflow that shows the next action at every stage.
  `Draft → Confirmed → Stuffed at factory → Gate-in → Export customs → Loaded on vessel → Discharged → Import customs → Gate-out → Delivered`
- **Gate in / out**: per-port gate desk. Gate-in accepts only booked exports. Gate-out releases only customs-cleared imports.
- **Customs**
  - Export declarations are filed when all boxes reach the origin yard. Import declarations are raised automatically on discharge.
  - Duty is calculated from the HS chapter.
  - Officers can review, send to inspection, hold, clear or reject, with notes on the record.
- **Live from game**: ship cargo and status, yard and tank levels, industry status and the event stream from the running game. Refreshes every 10 s.
- **Users & roles**: an admin can add users and enable or disable them. A permission matrix shows what each role can change.
- **Audit log**: who did what, and when.

## Consolidated sea bookings

Industries ask the port to ship their goods by booking supply orders. When customs clears an order for export, the order joins its route, for example Saltmarsh → Tern Island.

- **Automatic booking:** once a route reaches 4 boxes, the port books one ship for all of them. This posts a single consolidated sea transit request on the company board that lists every order on it.
- **Book now:** on *Transit requests*, staff can see routes still under the threshold and book a ship for any of them straight away.
- **Order view:** each order shows the sea booking it travels on.

## Business rules enforced

- A container can't be loaded on a vessel until export customs is cleared.
- A container can't leave the port by road until import customs is cleared.
- Yard capacity is checked on every gate-in and every discharge.
- A vessel must be berthed at the right port to load or discharge.
- Vehicles can't carry more containers than registered, and blocked vehicles are refused.
- Every change is written to the audit log.
- Companies can't spend beyond their balance, and each asset can carry only one job at a time.
- A ship or vehicle must have enough capacity for the job and be approved before it can accept work.
- IMO numbers are validated against their check digit.

## The 3D game

`/game` is the full game. It's also published as a Claude artifact.

- **Supply chain:** 12 factories on four container islands raise shipment requests to factories on other islands. Each port groups the requests by route and books a ship at 30 boxes, or sooner if boxes have waited. Ships collect, sail and deliver, and get paid on time or at 70 % if late.
- **Canal:** a land bridge splits the map into two oceans. The Bluewater Canal's locks raise and lower ships one at a time, for a toll.
- **Pirates and navy:** pirate skiffs hunt cargo ships and can sink them, so the cargo is lost. Navy frigates, which you can also steer, launch missiles at them.
- **Rescue:** rescue boats go out from the nearest port to pick up survivors in life rafts.
- **Life and lighting:** dock workers, ship crews, trains, road trains, seagulls, drifting clouds and lighthouses. There's a day/night cycle that follows your local time or runs fast, with navigation lights, port floodlights and lit factory windows at night.

## The game link

`/game` serves the 3D game. When it's opened from this server, it detects the backend through `/api/health` and then:

- posts events to `POST /api/game/events`: `ship_berthed`, `ship_departed`, `cargo_loaded`, `cargo_discharged`, `gate_in` and `gate_out` become vessel calls, stock movements and gate transactions with source `game`. The others are logged in the event stream:
  - shipments and bookings: `shipment_requested`, `booking_created`, `booking_assigned`, `booking_delivered`;
  - the canal: `canal_transit`;
  - pirates and the navy: `pirates_sighted`, `pirate_attack`, `missile_launched`, `pirate_destroyed`, `ship_sunk`;
  - rescue: `rescue_launched`, `crew_rescued`.
- posts a snapshot every 5 s to `POST /api/game/snapshot`, which updates live yard, tank, industry and ship figures.

Game figures appear as **live** values next to the **booked** registry figures and never overwrite them. Opened anywhere else (for example as a Claude artifact), the game runs on its own.

## REST API

Every endpoint is listed at `/docs`. Calls need a signed-in admin-panel session or the header `X-API-Key: <API_KEY from .env>`. Example:

```bash
curl -H "X-API-Key: $API_KEY" http://localhost:6800/api/orders
curl -X POST -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
     -d '{"port_id":1,"plate":"RT-M-1","direction":"in","containers":["BWFU1234565"]}' \
     http://localhost:6800/api/gate
```

## Development

```bash
cd app
pip install -r requirements-dev.txt
pytest                                   # uses SQLite by default
DATABASE_URL=postgresql+psycopg://bluewater:bluewater@localhost:6543/bluewater pytest
DATABASE_URL=... uvicorn bluewater.main:app --reload
```

Tables are created on startup with `Base.metadata.create_all`. For a production schema history, add Alembic migrations.

Columns added by newer versions are applied automatically at startup by `sync_schema()` in `db.py`.

## Layout

```
docker-compose.yml       db (Postgres 16) + app (FastAPI) + optional adminer
app/bluewater/
  main.py                app, sessions, startup (create tables, seed)
  models.py              ships, industries, vehicles, warehouses, containers, orders,
                         stock movements, vessel calls, gate, customs, audit, game events
  services.py            business rules (lifecycle, gate, customs, duty, game ingest)
  api.py                 JSON REST API
  admin.py + templates/  admin panel
  static/game/index.html the 3D game
app/tests/               end-to-end tests of the full supply chain
```
