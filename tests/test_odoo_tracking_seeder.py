import pytest

from scripts.seed_odoo_tracking import build_tracking_targets


def test_build_tracking_targets_creates_one_lot_per_fixture_location():
    rows = [
        {"product_code": "LOT-1", "location_code": "WH/Stock/A", "quantity": "12"},
        {"product_code": "LOT-1", "location_code": "WH/Stock/B", "quantity": "3"},
        {"product_code": "NONE-1", "location_code": "WH/Stock/C", "quantity": "99"},
    ]

    targets = build_tracking_targets(
        rows,
        relevant_tracking_by_code={"LOT-1": "lot"},
    )

    assert targets == [
        {
            "product_code": "LOT-1",
            "tracking": "lot",
            "location_code": "WH/Stock/A",
            "lot_name": "A-LOT-1-L01",
            "quantity": 12.0,
        },
        {
            "product_code": "LOT-1",
            "tracking": "lot",
            "location_code": "WH/Stock/B",
            "lot_name": "A-LOT-1-L02",
            "quantity": 3.0,
        },
    ]


def test_build_tracking_targets_expands_serial_quantity_to_one_each():
    rows = [
        {"product_code": "SER-1", "location_code": "WH/Stock/A", "quantity": "3"},
        {"product_code": "SER-1", "location_code": "WH/Stock/B", "quantity": "2"},
    ]

    targets = build_tracking_targets(
        rows,
        relevant_tracking_by_code={"SER-1": "serial"},
    )

    assert [row["lot_name"] for row in targets] == [
        "A-SER-1-S0001",
        "A-SER-1-S0002",
        "A-SER-1-S0003",
        "A-SER-1-S0004",
        "A-SER-1-S0005",
    ]
    assert [row["location_code"] for row in targets] == [
        "WH/Stock/A",
        "WH/Stock/A",
        "WH/Stock/A",
        "WH/Stock/B",
        "WH/Stock/B",
    ]
    assert all(row["quantity"] == 1.0 for row in targets)


def test_build_tracking_targets_rejects_fractional_serial_inventory():
    with pytest.raises(RuntimeError, match="non-integer sandbox quantity"):
        build_tracking_targets(
            [
                {
                    "product_code": "SER-1",
                    "location_code": "WH/Stock/A",
                    "quantity": "1.5",
                }
            ],
            relevant_tracking_by_code={"SER-1": "serial"},
        )
