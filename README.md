# Warehouse Analyst Control Tower (AWIA)

AWIA is a warehouse analytics and decision-support API that connects to Odoo and provides the foundation for the Agentic Warehouse Inventory Analyst.

## Current capabilities

- Production-oriented Odoo XML-RPC client validated against local Odoo 19.
- FastAPI service with health checks and interactive OpenAPI docs.
- Read endpoints for products, inventory quants, stock move lines, and manufacturing orders.
- Module A inventory-health scoring and advisory cycle-count planning.

### Module A first slice

- ABC: top 20% inventory value = A/30-day interval; next 30% = B/90-day; bottom 50% = C/180-day.
- XYZ: four-week stock-move activity variability proxy.
- Seven-day product/location touch counts including the 40+ touches/week trigger.
- Unique-user touch signal from `write_uid`.
- Optional lot/serial tracking risk points.
- Optional `x_is_flight_critical` 1.75x multiplier when that custom field exists.
- Explainable product/location risk scores.
- Advisory daily count queue ordered by risk band and Odoo location code.

Operational writes remain disabled until an explicit human approval workflow is added.

## Local Windows setup

```powershell
cd C:\Users\jamil\Projects\Warehouse-Analyst-Control-Tower-
.\.venv\Scripts\Activate.ps1
git pull origin main
python -m pip install -r .\requirements.txt
```

```powershell
$env:ODOO_URL="http://localhost:8069"
$env:ODOO_DB="scm_os_demo"
$env:ODOO_USERNAME="admin"
$env:ODOO_PASSWORD="admin"
$env:ODOO_ALLOW_INSECURE_HTTP="true"
```

## Run tests

```powershell
python -m pytest -q
```

Current expected result: 13 passing tests.

## Run the API

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/docs`.

Raw endpoints:
- `GET /api/products?limit=100`
- `GET /api/inventory?limit=100`
- `GET /api/moves?limit=100`
- `GET /api/manufacturing-orders?limit=100`

Module A endpoints:
- `GET /api/inventory-health?limit=100&source_limit=5000`
- `GET /api/cycle-count-plan?limit=50&source_limit=5000`

The response includes `source_snapshot.truncated_possible` when the per-model source limit may have clipped the analysis snapshot.
