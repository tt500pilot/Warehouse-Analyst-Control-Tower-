# Odoo Sandbox Seeding Workflow

## Purpose

AWIA uses a separate Odoo database for integration testing. The deterministic CSV/JSON sandbox remains the source of synthetic truth; Odoo is used to validate native model/API behavior.

Recommended database name: `awia_mock`.

Never apply synthetic sandbox records to a production or shared demo database.

## Safety controls

`scripts/seed_odoo_sandbox.py` is dry-run by default. Actual writes require both:

1. `--apply`
2. `AWIA_ALLOW_SANDBOX_WRITES=true`

The database must also be in the allowlist. Defaults:

- `awia_mock`
- `awia_sandbox`
- `awia_test`

Override only when intentionally creating another dedicated sandbox:

```powershell
$env:AWIA_SANDBOX_DATABASE_ALLOWLIST="awia_mock,another_awia_test_db"
```

The current shared/demo database should not be added to this list.

## First validation

With the project virtual environment active:

```powershell
python -m pytest -q
```

Generate deterministic files:

```powershell
python .\scripts\generate_simulation_sandbox.py
```

## Dry run

A dry run does not write, even if pointed at another database:

```powershell
$env:ODOO_URL="http://localhost:8069"
$env:ODOO_DB="awia_mock"
$env:ODOO_USERNAME="admin"
$env:ODOO_PASSWORD="admin"
$env:ODOO_ALLOW_INSECURE_HTTP="true"

python .\scripts\seed_odoo_sandbox.py
```

## Apply

Only after `awia_mock` exists and the dry run is correct:

```powershell
$env:AWIA_ALLOW_SANDBOX_WRITES="true"
python .\scripts\seed_odoo_sandbox.py --apply
```

Rerunning is intended to be idempotent: locations are matched by barcode and products by default code.

## First-slice scope

The live-Odoo seeder currently creates/updates:

- AWIA mock root location
- synthetic operational stations
- synthetic bin locations
- synthetic products
- on-hand inventory through Odoo inventory quantities when the installed Odoo model supports `inventory_quantity`

It intentionally does **not** fake historical completed manufacturing/component-pick transactions or rewrite audit timestamps. Doing that would invalidate the kitting baseline.

Historical synthetic transactions remain in `data/simulation_sandbox/mock_odoo/` for deterministic algorithm tests.

## Kitting baseline endpoint

FastAPI exposes:

```text
GET /api/kitting-baseline
```

Default picking-type filter: `Pick Components`.

The first slice reports:

- kits/transfers analyzed
- average/median/P90 gross transfer cycle minutes
- move-event timestamp span proxy
- move lines per kit
- unique source locations per kit

Important: `create_date -> date_done` is explicitly labeled **gross transfer cycle time**. It includes queue/wait time and is not treated as picker labor time.

Fine-grained walking, search, and handling time will only be reported after AWIA has trustworthy geometry and/or scan/timer events.
