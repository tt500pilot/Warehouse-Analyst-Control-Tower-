from datetime import datetime, timedelta, timezone

from app.services.inventory_health import analyze_inventory_health, build_cycle_count_plan

AS_OF = datetime(2026, 8, 27, 17, 0, tzinfo=timezone.utc)


def _product(product_id: int, cost: float, *, critical: bool = False, tracking: str = "none"):
    return {"id": product_id, "default_code": f"P-{product_id:03d}", "name": f"Part {product_id}", "standard_price": cost, "tracking": tracking, "x_is_flight_critical": critical}


def _quant(product_id: int, location_id: int, qty: float):
    return {"product_id": [product_id, f"Part {product_id}"], "location_id": [location_id, f"WH/Stock/A-{location_id:02d}"], "quantity": qty, "reserved_quantity": 0}


def _move(product_id: int, location_id: int, days_ago: float, qty: float = 1.0, user_id: int = 2):
    stamp = AS_OF - timedelta(days=days_ago)
    return {"product_id": [product_id, f"Part {product_id}"], "location_id": [location_id, f"WH/Stock/A-{location_id:02d}"], "location_dest_id": [99, "WH/Production"], "quantity": qty, "date": stamp.strftime("%Y-%m-%d %H:%M:%S"), "write_uid": [user_id, f"User {user_id}"]}


def test_abc_distribution_matches_20_30_50_rank_split():
    products = [_product(i, cost=11 - i) for i in range(1, 11)]
    quants = [_quant(i, i, 10) for i in range(1, 11)]
    report = analyze_inventory_health(products, quants, [], as_of=AS_OF)
    by_product = {row["product_id"]: row for row in report["items"]}
    assert [by_product[i]["abc_class"] for i in range(1, 3)] == ["A", "A"]
    assert [by_product[i]["abc_class"] for i in range(3, 6)] == ["B", "B", "B"]
    assert [by_product[i]["abc_class"] for i in range(6, 11)] == ["C"] * 5


def test_high_touch_and_flight_critical_raise_risk_score():
    products = [_product(1, 100, critical=True, tracking="serial"), _product(2, 100, tracking="serial")]
    quants = [_quant(1, 1, 10), _quant(2, 2, 10)]
    moves = []
    for i in range(45):
        moves.append(_move(1, 1, days_ago=(i % 6) + 0.1, user_id=2 + (i % 5)))
        moves.append(_move(2, 2, days_ago=(i % 6) + 0.1, user_id=2 + (i % 5)))
    report = analyze_inventory_health(products, quants, moves, as_of=AS_OF)
    by_product = {row["product_id"]: row for row in report["items"]}
    assert by_product[1]["touches_7d"] == 45
    assert by_product[1]["risk_score"] > by_product[2]["risk_score"]


def test_xyz_uses_four_week_activity_variability_proxy():
    products = [_product(1, 10), _product(2, 10), _product(3, 10)]
    quants = [_quant(1, 1, 5), _quant(2, 2, 5), _quant(3, 3, 5)]
    moves = []
    for days_ago in (1, 8, 15, 22): moves.append(_move(1, 1, days_ago, qty=10))
    for days_ago, qty in ((1, 5), (8, 10), (15, 20), (22, 35)): moves.append(_move(2, 2, days_ago, qty=qty))
    moves.append(_move(3, 3, 1, qty=40))
    report = analyze_inventory_health(products, quants, moves, as_of=AS_OF)
    by_product = {row["product_id"]: row for row in report["items"]}
    assert by_product[1]["xyz_class"] == "X"
    assert by_product[2]["xyz_class"] == "Y"
    assert by_product[3]["xyz_class"] == "Z"


def test_cycle_count_plan_preserves_risk_band_then_routes_by_location():
    items = [{"risk_score": 85, "location_name": "WH/Stock/B-02", "default_code": "P2", "product_id": 2}, {"risk_score": 90, "location_name": "WH/Stock/A-01", "default_code": "P1", "product_id": 1}, {"risk_score": 65, "location_name": "WH/Stock/A-00", "default_code": "P3", "product_id": 3}]
    plan = build_cycle_count_plan(items, limit=3)
    assert [entry["product_id"] for entry in plan["entries"]] == [1, 2, 3]
    assert "advisory only" in plan["execution"]
