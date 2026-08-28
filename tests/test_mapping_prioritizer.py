from datetime import datetime, timedelta, timezone

from app.services.mapping_prioritizer import analyze_mapping_priorities, derive_logical_area

NOW = datetime(2026, 8, 28, 18, 0, tzinfo=timezone.utc)


def test_derive_logical_area_prefers_hierarchy_below_stock() -> None:
    assert derive_logical_area("WH/Stock/FAST/A-01-L1-BA") == (
        "WH/Stock/FAST",
        "hierarchy_below_stock",
    )


def test_derive_logical_area_uses_flat_aisle_fallback() -> None:
    assert derive_logical_area("WH/Stock/A-01") == (
        "WH/Stock/Aisle A",
        "flat_aisle_prefix",
    )


def test_derive_logical_area_looks_through_generic_mock_container() -> None:
    assert derive_logical_area("WH/Stock/AWIA Mock/H-04-L1-BA") == (
        "WH/Stock/AWIA Mock/Aisle H",
        "nested_flat_aisle_prefix",
    )


def test_mapping_prioritizer_ranks_high_activity_critical_area_first() -> None:
    products = [
        {
            "id": 1,
            "default_code": "CRIT-1",
            "name": "Critical Part",
            "standard_price": 1000.0,
            "tracking": "serial",
            "x_is_flight_critical": True,
        },
        {
            "id": 2,
            "default_code": "LOW-1",
            "name": "Low Part",
            "standard_price": 10.0,
            "tracking": "none",
            "x_is_flight_critical": False,
        },
    ]
    locations = [
        {"id": 101, "complete_name": "WH/Stock/BULK/H-01", "usage": "internal"},
        {"id": 102, "complete_name": "WH/Stock/FAST/A-01", "usage": "internal"},
        {"id": 201, "complete_name": "WH/Kitting", "usage": "internal"},
    ]
    quants = [
        {"id": 1, "product_id": [1, "Critical Part"], "location_id": [101, "WH/Stock/BULK/H-01"], "quantity": 20.0, "reserved_quantity": 5.0},
        {"id": 2, "product_id": [2, "Low Part"], "location_id": [102, "WH/Stock/FAST/A-01"], "quantity": 20.0, "reserved_quantity": 0.0},
    ]
    moves = []
    for index in range(10):
        moves.append(
            {
                "id": index + 1,
                "product_id": [1, "Critical Part"],
                "location_id": [101, "WH/Stock/BULK/H-01"],
                "location_dest_id": [201, "WH/Kitting"],
                "quantity": 1.0,
                "date": (NOW - timedelta(days=1, minutes=index)).strftime("%Y-%m-%d %H:%M:%S"),
            }
        )
    moves.append(
        {
            "id": 50,
            "product_id": [2, "Low Part"],
            "location_id": [102, "WH/Stock/FAST/A-01"],
            "location_dest_id": [201, "WH/Kitting"],
            "quantity": 1.0,
            "date": (NOW - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    bom_lines = [
        {"id": 1, "bom_id": [1, "BOM"], "product_id": [1, "Critical Part"], "product_qty": 1.0}
    ]

    result = analyze_mapping_priorities(
        products,
        quants,
        moves,
        locations,
        bom_lines=bom_lines,
        as_of=NOW,
        lookback_days=90,
    )

    assert result["odoo_mutated"] is False
    assert result["summary"]["recommended_first_area"] == "WH/Stock/BULK"
    assert result["recommended_mapping_scope"]["logical_area"] == "WH/Stock/BULK"
    assert result["areas"][0]["rank"] == 1
    assert result["areas"][0]["move_touches"] == 10
    assert result["areas"][0]["flight_critical_skus"] == 1
    assert result["methodology"]["physical_distance"].startswith("not used")


def test_non_stock_quant_location_is_flow_endpoint_not_ranked_storage() -> None:
    products = [
        {
            "id": 1,
            "default_code": "P-1",
            "name": "Part",
            "standard_price": 100.0,
            "tracking": "lot",
            "x_is_flight_critical": False,
        }
    ]
    locations = [
        {"id": 101, "complete_name": "WH/Stock/AWIA Mock/H-01-L1-BA", "usage": "internal"},
        {"id": 201, "complete_name": "WH/Pre-Production", "usage": "internal"},
        {"id": 301, "complete_name": "Inventory adjustment", "usage": "inventory"},
    ]
    quants = [
        {"id": 1, "product_id": [1, "Part"], "location_id": [101, "WH/Stock/AWIA Mock/H-01-L1-BA"], "quantity": 20.0, "reserved_quantity": 0.0},
        {"id": 2, "product_id": [1, "Part"], "location_id": [201, "WH/Pre-Production"], "quantity": 2.0, "reserved_quantity": 2.0},
        {"id": 3, "product_id": [1, "Part"], "location_id": [301, "Inventory adjustment"], "quantity": -22.0, "reserved_quantity": 0.0},
    ]
    moves = [
        {
            "id": 1,
            "product_id": [1, "Part"],
            "location_id": [101, "WH/Stock/AWIA Mock/H-01-L1-BA"],
            "location_dest_id": [201, "WH/Pre-Production"],
            "quantity": 2.0,
            "date": (NOW - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
        }
    ]

    result = analyze_mapping_priorities(
        products,
        quants,
        moves,
        locations,
        as_of=NOW,
        lookback_days=90,
    )

    assert [row["logical_area"] for row in result["areas"]] == [
        "WH/Stock/AWIA Mock/Aisle H"
    ]
    assert result["summary"]["quant_locations_excluded_from_storage_ranking"] == 2
    assert result["areas"][0]["top_flow_counterparts"] == [
        {"location": "WH/Pre-Production", "touches": 1}
    ]
