"""API contract tests that do not require a live Odoo instance."""

from fastapi.testclient import TestClient

from app.main import app, get_odoo_client


class FakeOdooClient:
    database = "test_db"
    uid = 2

    def check_read_access(self, model: str) -> bool:
        return model == "product.product"

    def fetch_products(self, *, limit: int):
        return [{"id": 1, "name": "Test Product"}][:limit]

    def fetch_stock_quants(self, *, limit: int):
        return [{"id": 10, "quantity": 42.0}][:limit]

    def fetch_stock_move_lines(self, *, limit: int):
        return [{"id": 20, "state": "done"}][:limit]

    def fetch_manufacturing_orders(self, *, limit: int):
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
    assert response.json() == {
        "status": "ok",
        "odoo": "connected",
        "database": "test_db",
        "uid": 2,
    }


def test_products_endpoint() -> None:
    response = client.get("/api/products?limit=5")
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
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


def test_limit_validation() -> None:
    response = client.get("/api/products?limit=0")
    assert response.status_code == 422
