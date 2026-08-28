# AWIA Simulation Sandbox Specification

## Purpose

The simulation sandbox gives AWIA a deterministic test environment before any real warehouse geometry or production Odoo data is used.

It contains two linked worlds:

1. **Mock Odoo operational data** — products, stock locations, quants, BOMs, manufacturing orders, component-pick transfers, and stock-move lines.
2. **Mock warehouse spatial data** — a permanent coordinate datum, physical bin coordinates, operational stations, and a connected walk graph.

The sandbox is synthetic and must never be treated as production data.

## Design principles

- **Deterministic:** generation seed is fixed at `42`.
- **Known answers:** the dataset intentionally contains problems AWIA should eventually discover.
- **Referential integrity:** mock product/location IDs used in quants and moves must resolve to generated master records.
- **Versioned geometry:** all coordinates belong to layout version `mock-v1`.
- **Permanent datum:** the mock warehouse uses the southwest inside floor corner as `(0, 0, 0)`.
- **Odoo remains the system of record:** mock Odoo IDs are the bridge between operational data and spatial metadata.
- **No autonomous execution:** the sandbox is for analysis and testing only.

## Mock warehouse

### Coordinate system

- Coordinate system: local Cartesian
- Units: feet
- Origin: southwest inside warehouse floor corner
- Datum: `(0, 0, 0)`
- Floor elevation: `z = 0`
- Layout version: `mock-v1`

Operational stations may move in future layout versions. The datum itself does not move.

### Footprint

The generated warehouse has:

- 8 aisles: `A` through `H`
- 6 bays per aisle
- 2 rack levels
- 2 bin sides per bay
- 192 physical bin locations
- 5 operational stations: Receiving, Kitting, Shipping, Quality, MRB
- South and north cross-aisles
- A connected walk graph

### Zones

| Aisles | Zone | Intended behavior |
| --- | --- | --- |
| A-B | FAST | Prime/fast-pick inventory |
| C-E | STANDARD | General storage |
| F-G | CONTROLLED | Secure/controlled material |
| H | BULK | Slow/bulk/reserve material |

### Spatial files

Generated under `data/simulation_sandbox/mock_warehouse/`:

- `warehouse_metadata.json` — datum, units, layout version, spacing assumptions
- `stations.csv` — receiving, kitting, shipping, quality, MRB coordinates
- `locations.csv` — Odoo-linked physical bin metadata and X/Y/Z
- `nodes.csv` — graph nodes
- `edges.csv` — legal walk connections and edge distance

Coordinates identify where an object is. The graph identifies how a worker may travel between locations. Future AWIA routing must use graph distance rather than straight-line distance when aisle geometry matters.

## Mock Odoo operational world

Generated under `data/simulation_sandbox/mock_odoo/`:

- `stock_locations.csv`
- `products.csv`
- `quants.csv`
- `boms.csv`
- `bom_lines.csv`
- `manufacturing_orders.csv`
- `stock_pickings.csv`
- `stock_move_lines.csv`

### Initial scale

Current deterministic generation creates:

- 201 Odoo-style stock locations
- 80 products
- 84 stock quants
- 4 BOMs
- 49 BOM component lines
- 120 manufacturing orders
- 120 Pick Components transfers
- 2,511 stock move lines

## Embedded benchmark scenarios

`expected_findings.json` documents intentionally planted conditions.

1. `BOLT-104` — high-velocity SKU intentionally stored in distant `WH/Stock/BULK/H-06-L2-BB`.
2. `BRACKET-77` — low-velocity SKU intentionally stored in prime `WH/Stock/FAST/A-01-L1-BA`.
3. `VALVE-441`, `BRACKET-221`, `HARNESS-310`, `FASTENER-900` — repeated Orbital Transfer Stage co-pick cluster.
4. `SERIAL-AVX-7` — serial-tracked, flight-critical avionics SKU with 55 recent synthetic touches.
5. `SEAL-218` — low available quantity and periodic shortage-affected kits.
6. `REGULATOR-552` — medium-velocity propulsion component intentionally stored in distant bulk storage.

These conditions become regression targets for future AWIA intelligence.

## Kitting measurement design

The mock environment supports a future Kitting Baseline Engine.

Each manufacturing order includes program, BOM, start/finish timestamps, component line count, shortage flag, and first-pass-complete flag. Each Pick Components transfer includes MO origin, scheduled/completion timestamps, and line count. Each kit move line includes product, source location, kitting destination, timestamp, user, quantity, MO, and picking reference.

This supports future calculations such as:

- gross kit cycle time
- line count per kit
- locations visited
- first-pass completion
- shortage impact
- actual route distance once geometry is applied
- optimal route distance
- travel distance per pick line
- before/after improvement

## Generated-data policy

Generated sandbox files should not be hand-edited. Change the generator, regenerate, and rerun tests.

```powershell
python .\scripts\generate_simulation_sandbox.py
```

Default output: `data/simulation_sandbox/`.

## Validation

```powershell
python -m pytest -q tests/test_simulation_sandbox.py
```

Validation checks expected counts, referential integrity, graph connectivity, physical-bin graph mappings, fixed datum definition, and the embedded benchmark conditions.

## Immediate follow-on work

1. Generate and inspect the sandbox locally.
2. Build an idempotent Odoo sandbox seeder targeting a separate local database (recommended: `awia_mock`).
3. Build the Kitting Baseline Engine using native Odoo objects first.
4. Add the AWIA geometry service for Odoo-location lookup, graph distance, route distance, and shortest paths.
5. Run the embedded benchmark scenarios as regression experiments.

The sandbox becomes the regression environment for every future warehouse optimization algorithm.
