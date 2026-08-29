"""API contract tests that do not require a live Odoo instance."""

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app, get_odoo_client

NOW = datetime.now(timezone.utc)


class FakeOdooClient:
    database = "test_db"
    uid = 2

    def check_read_access(self, model: str) -> bool:
        return model == "product.product"

    def fetch_products(self, *, domain=None, fields=None, limit=100):
        records = [
            {"id": 1, "default_code": "P-001", "name": "Test Product", "standard_price": 100.0, "tracking": "serial", "x_is_flight_critical": True},
            {"id": 2, "default_code": "P-002", "name": "Second Product", "standard_price": 10.0, "tracking": "none", "x_is_flight_critical": False},
        ]
        return records[:limit]

    def fetch_stock_quants(self, *, domain=None, fields=None, limit=100):
        records = [
            {"id": 10, "product_id": [1, "Test Product"], "location_id": [101, "WH/Stock/A-01"], "lot_id": False, "quantity": 42.0, "reserved_quantity": 2.0},
            {"id": 11, "product_id": [2, "Second Product"], "location_id": [102, "WH/Stock/B-02"], "lot_id": False, "quantity": 12.0, "reserved_quantity": 0.0},
        ]
        return records[:limit]

    def fetch_stock_move_lines(self, *, domain=None, fields=None, limit=100):
        records = [
            {"id": 20, "product_id": [1, "Test Product"], "location_id": [101, "WH/Stock/A-01"], "location_dest_id": [200, "WH/Production"], "quantity": 3.0, "state": "done", "write_uid": [2, "Admin"], "date": (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")}
        ]
        return records[:limit]

    def fetch_manufacturing_orders(self, *, domain=None, fields=None, limit=100):
        return [{"id": 30, "name": "MO/TEST/0001"}][:limit]


def _fake_client() -> FakeOdooClient:
    return FakeOdooClient()


app.dependency_overrides[get_odoo_client] = _fake_client
client = TestClient(app)


def test_process_health() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_odoo_health() -> None:
    response = client.get("/health/odoo")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "odoo": "connected", "database": "test_db", "uid": 2}


def test_products_endpoint() -> None:
    response = client.get("/api/products?limit=5")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["records"][0]["name"] == "Test Product"


def test_inventory_endpoint() -> None:
    response = client.get("/api/inventory?limit=5")
    assert response.status_code == 200
    assert response.json()["records"][0]["quantity"] == 42.0


def test_moves_endpoint() -> None:
    response = client.get("/api/moves?limit=5")
    assert response.status_code == 200
    assert response.json()["records"][0]["state"] == "done"


def test_manufacturing_orders_endpoint() -> None:
    response = client.get("/api/manufacturing-orders?limit=5")
    assert response.status_code == 200
    assert response.json()["records"][0]["name"] == "MO/TEST/0001"


def test_inventory_health_endpoint() -> None:
    response = client.get("/api/inventory-health?limit=10&source_limit=100")
    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["locations_evaluated"] == 2
    assert body["returned_items"] == 2
    assert body["items"][0]["risk_score"] >= body["items"][1]["risk_score"]
    assert body["methodology"]["execution"].startswith("advisory only")


def test_cycle_count_plan_endpoint() -> None:
    response = client.get("/api/cycle-count-plan?limit=2&source_limit=100")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 2
    assert body["entries"][0]["sequence"] == 1
    assert "analyst approval" in body["execution"]


def test_traceability_health_endpoint() -> None:
    response = client.get(
        "/api/traceability-health?source_limit=100&location_prefix=WH%2FStock%2FA"
    )
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "read_only_traceability_health"
    assert body["odoo_mutated"] is False
    assert body["safe_to_execute_inventory_moves"] is False
    assert body["location_prefix"] == "WH/Stock/A"
    assert body["summary"]["tracked_inventory_positions"] == 1
    assert body["summary"]["blocked_positions"] == 1
    assert body["summary"]["anonymous_quantity"] == 42.0
    assert body["items"][0]["status"] == "BLOCKED_TRACEABILITY"


def test_limit_validation() -> None:
    response = client.get("/api/products?limit=0")
    assert response.status_code == 422
