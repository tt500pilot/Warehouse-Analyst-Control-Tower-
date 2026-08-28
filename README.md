# Warehouse Analyst Control Tower (AWIA)

AWIA is a warehouse analytics and decision-support application that connects to Odoo and provides the foundation for the Agentic Warehouse Inventory Analyst.

## Current capabilities

- Production-oriented Odoo XML-RPC client validated against local Odoo 19.
- FastAPI service with health checks and interactive OpenAPI docs.
- Streamlit Control Tower UI that consumes the FastAPI service.
- Read endpoints for products, inventory quants, stock move lines, and manufacturing orders.
- Module A inventory-health scoring and advisory cycle-count planning.
- Deterministic AWIA Simulation Sandbox with linked mock Odoo transactions and warehouse geometry.

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

Configure Odoo in the PowerShell window that will run FastAPI:

```powershell
$env:ODOO_URL="http://localhost:8069"
$env:ODOO_DB="scm_os_demo"
$env:ODOO_USERNAME="admin"
$env:ODOO_PASSWORD="admin"
$env:ODOO_ALLOW_INSECURE_HTTP="true"
```

## AWIA Simulation Sandbox

Generate the deterministic mock warehouse + mock Odoo fixture set:

```powershell
python .\scripts\generate_simulation_sandbox.py
```

This creates `data\simulation_sandbox\` with:

- a fixed warehouse datum and layout version
- 192 physical bins with X/Y/Z coordinates
- a connected aisle/cross-aisle travel graph
- 80 synthetic products
- 120 manufacturing orders and Pick Components transfers
- 2,500+ stock move lines
- deliberately embedded benchmark problems for slotting, co-pick, shortage, and cycle-count testing

The full specification is in `docs/architecture/simulation-sandbox.md`.

Generated CSV/JSON files are intentionally not committed; they are reproducible from seed `42`.

## Run tests

```powershell
python -m pytest -q
```

Current expected result: 19 passing tests.

To validate only the sandbox:

```powershell
python -m pytest -q .\tests\test_simulation_sandbox.py
```

## Start FastAPI - Terminal 1

```powershell
cd C:\Users\jamil\Projects\Warehouse-Analyst-Control-Tower-
.\.venv\Scripts\Activate.ps1

$env:ODOO_URL="http://localhost:8069"
$env:ODOO_DB="scm_os_demo"
$env:ODOO_USERNAME="admin"
$env:ODOO_PASSWORD="admin"
$env:ODOO_ALLOW_INSECURE_HTTP="true"

python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

FastAPI docs: `http://127.0.0.1:8000/docs`

## Start Streamlit - Terminal 2

Open a second PowerShell window while FastAPI remains running:

```powershell
cd C:\Users\jamil\Projects\Warehouse-Analyst-Control-Tower-
.\.venv\Scripts\Activate.ps1
python -m streamlit run .\streamlit_app.py --server.port 8501
```

Open the Control Tower at `http://localhost:8501`.

The UI defaults to FastAPI at `http://127.0.0.1:8000`. To point it somewhere else:

```powershell
$env:AWIA_API_URL="http://127.0.0.1:8000"
python -m streamlit run .\streamlit_app.py --server.port 8501
```

### Streamlit views

- **Control Tower** - KPI cards, risk distribution, ABC distribution, and highest-risk locations.
- **Inventory Health** - searchable/filterable Module A risk table with scoring methodology.
- **Cycle Count Plan** - daily advisory count queue and route order.
- **Data Explorer** - read-only inspection of products, quants, move lines, and manufacturing orders.

The Streamlit UI talks only to FastAPI. Odoo credentials remain in the FastAPI process and are not exposed to the browser UI.

## API endpoints

Raw endpoints:
- `GET /api/products?limit=100`
- `GET /api/inventory?limit=100`
- `GET /api/moves?limit=100`
- `GET /api/manufacturing-orders?limit=100`

Module A endpoints:
- `GET /api/inventory-health?limit=100&source_limit=5000`
- `GET /api/cycle-count-plan?limit=50&source_limit=5000`

The response includes `source_snapshot.truncated_possible` when the per-model source limit may have clipped the analysis snapshot.
