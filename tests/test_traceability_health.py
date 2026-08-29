from app.services.traceability_health import (
    analyze_traceability_health,
    build_traceability_block_index,
)


def _products():
    return [
        {"id": 1, "default_code": "LOT-1", "name": "Lot Part", "tracking": "lot"},
        {"id": 2, "default_code": "SER-1", "name": "Serial Part", "tracking": "serial"},
        {"id": 3, "default_code": "NONE-1", "name": "Untracked Part", "tracking": "none"},
    ]


def test_anonymous_lot_tracked_quantity_is_blocked():
    quants = [
        {
            "product_id": [1, "Lot Part"],
            "location_id": [10, "WH/Stock/A01"],
            "lot_id": False,
            "quantity": 7,
            "reserved_quantity": 1,
        },
        {
            "product_id": [1, "Lot Part"],
            "location_id": [10, "WH/Stock/A01"],
            "lot_id": [101, "LOT-101"],
            "quantity": 3,
            "reserved_quantity": 0,
        },
    ]
    result = analyze_traceability_health(_products(), quants)
    row = result["items"][0]

    assert result["summary"]["blocked_positions"] == 1
    assert row["status"] == "BLOCKED_TRACEABILITY"
    assert row["blocked_from_relocation_analysis"] is True
    assert row["on_hand_quantity"] == 10.0
    assert row["identified_quantity"] == 3.0
    assert row["anonymous_quantity"] == 7.0
    assert row["traceability_coverage_pct"] == 30.0
    assert "positive_tracked_quantity_without_lot_or_serial_identity" in row["reasons"]
    assert "live_reservation_present" in row["reasons"]


def test_fully_identified_serial_tracked_quantity_is_complete():
    quants = [
        {
            "product_id": [2, "Serial Part"],
            "location_id": [20, "WH/Stock/B01"],
            "lot_id": [201, "SER-201"],
            "quantity": 1,
            "reserved_quantity": 0,
        },
        {
            "product_id": [2, "Serial Part"],
            "location_id": [20, "WH/Stock/B01"],
            "lot_id": [202, "SER-202"],
            "quantity": 1,
            "reserved_quantity": 0,
        },
    ]
    result = analyze_traceability_health(_products(), quants)
    row = result["items"][0]

    assert row["status"] == "TRACEABILITY_COMPLETE"
    assert row["blocked_from_relocation_analysis"] is False
    assert row["traceability_coverage_pct"] == 100.0
    assert row["lot_or_serial_ids"] == [201, 202]


def test_untracked_products_are_not_in_traceability_scope():
    quants = [
        {
            "product_id": [3, "Untracked Part"],
            "location_id": [30, "WH/Stock/C01"],
            "lot_id": False,
            "quantity": 99,
            "reserved_quantity": 5,
        }
    ]
    result = analyze_traceability_health(_products(), quants)

    assert result["summary"]["tracked_inventory_positions"] == 0
    assert result["items"] == []


def test_location_prefix_limits_scope():
    quants = [
        {
            "product_id": [1, "Lot Part"],
            "location_id": [10, "WH/Stock/A01"],
            "lot_id": False,
            "quantity": 5,
            "reserved_quantity": 0,
        },
        {
            "product_id": [1, "Lot Part"],
            "location_id": [11, "WH/Stock/B01"],
            "lot_id": False,
            "quantity": 6,
            "reserved_quantity": 0,
        },
    ]
    result = analyze_traceability_health(
        _products(), quants, location_prefix="WH/Stock/A"
    )

    assert result["summary"]["tracked_inventory_positions"] == 1
    assert result["items"][0]["location_name"] == "WH/Stock/A01"


def test_block_index_contains_only_blocked_product_location_positions():
    report = {
        "items": [
            {
                "product_id": 1,
                "location_id": 10,
                "blocked_from_relocation_analysis": True,
                "anonymous_quantity": 7.0,
            },
            {
                "product_id": 2,
                "location_id": 20,
                "blocked_from_relocation_analysis": False,
                "anonymous_quantity": 0.0,
            },
        ]
    }

    index = build_traceability_block_index(report)

    assert set(index) == {(1, 10)}
    assert index[(1, 10)]["anonymous_quantity"] == 7.0


def test_traceability_analysis_never_authorizes_execution():
    result = analyze_traceability_health(_products(), [])
    assert result["odoo_mutated"] is False
    assert result["safe_to_execute_inventory_moves"] is False
