# Warehouse Analyst Control Tower (AWIA)

AWIA is a read-oriented warehouse analytics API that connects to Odoo and exposes the initial data foundation for the Agentic Warehouse Inventory Analyst.

## Current capabilities

- Production-oriented Odoo XML-RPC client.
- Local Odoo 19 connectivity.
- FastAPI service with interactive OpenAPI documentation.
- Health checks for the API process and Odoo connectivity.
- Read endpoints for products, inventory quants, stock move lines, and manufacturing orders.
- API tests that run without a live Odoo server.

Operational writes are intentionally not exposed yet. Future Odoo mutations must be protected by an explicit human-in-the-loop approval workflow.

## Local Windows setup

From PowerShell:

```powershell
cd C:\Users\jamil\Projects\Warehouse-Analyst-Control-Tower-
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\requirements.txt
```

Configure the local Odoo 19 connection for the current PowerShell session:

```powershell
$env:ODOO_URL="http://localhost:8069"
$env:ODOO_DB="scm_os_demo"
$env:ODOO_USERNAME="admin"
$env:ODOO_PASSWORD="admin"
$env:ODOO_ALLOW_INSECURE_HTTP="true"
```

Do not commit real passwords or API keys. `.env.example` is documentation only.

## Run the API

```powershell
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Open:

- `http://127.0.0.1:8000/`
- `http://127.0.0.1:8000/health`
- `http://127.0.0.1:8000/health/odoo`
- `http://127.0.0.1:8000/docs`

Warehouse endpoints:

- `GET /api/products?limit=100`
- `GET /api/inventory?limit=100`
- `GET /api/moves?limit=100`
- `GET /api/manufacturing-orders?limit=100`

## Run tests

```powershell
python -m pytest -q
```

The API tests replace the live Odoo dependency with a fake client, so they can run independently of Docker/Odoo availability.

## Direct Odoo smoke test

The original client smoke test remains available:

```powershell
python .\odoo_client.py
```
