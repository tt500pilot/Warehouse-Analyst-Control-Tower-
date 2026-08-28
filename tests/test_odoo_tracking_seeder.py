import pytest

from scripts.seed_odoo_tracking import build_tracking_targets


def test_build_tracking_targets_creates_one_lot_per_demand_location():
    targets = build_tracking_targets(
        [
            {
                "product_id": 101,
                "product_code": "LOT-1",
                "tracking": "lot",
                "location_id": 501,
                "location": "WH/Stock/A",
                "quantity": 12.0,
            },
            {
                "product_id": 101,
                "product_code": "LOT-1",
                "tracking": "lot",
                "location_id": 502,
                "location": "WH/Stock/B",
                "quantity": 3.0,
            },
        ]
    )

    assert targets == [
        {
            "product_id": 101,
            "product_code": "LOT-1",
            "tracking": "lot",
            "location_id": 501,
            "location": "WH/Stock/A",
            "lot_name": "A-LOT-1-L501",
            "quantity": 12.0,
        },
        {
            "product_id": 101,
            "product_code": "LOT-1",
            "tracking": "lot",
            "location_id": 502,
            "location": "WH/Stock/B",
            "lot_name": "A-LOT-1-L502",
            "quantity": 3.0,
        },
    ]


def test_build_tracking_targets_expands_only_required_serial_quantity():
    targets = build_tracking_targets(
        [
            {
                "product_id": 201,
                "product_code": "SER-1",
                "tracking": "serial",
                "location_id": 601,
                "location": "WH/Stock/A",
                "quantity": 3.0,
            },
            {
                "product_id": 201,
                "product_code": "SER-1",
                "tracking": "serial",
                "location_id": 602,
                "location": "WH/Stock/B",
                "quantity": 2.0,
            },
        ]
    )

    assert [row["lot_name"] for row in targets] == [
        "A-SER-1-601-S0001",
        "A-SER-1-601-S0002",
        "A-SER-1-601-S0003",
        "A-SER-1-602-S0001",
        "A-SER-1-602-S0002",
    ]
    assert [row["location_id"] for row in targets] == [601, 601, 601, 602, 602]
    assert all(row["quantity"] == 1.0 for row in targets)


def test_build_tracking_targets_rejects_fractional_serial_demand():
    with pytest.raises(RuntimeError, match="non-integer required quantity"):
        build_tracking_targets(
            [
                {
                    "product_id": 201,
                    "product_code": "SER-1",
                    "tracking": "serial",
                    "location_id": 601,
                    "location": "WH/Stock/A",
                    "quantity": 1.5,
                }
            ]
        )


def test_build_tracking_targets_ignores_untracked_rows():
    assert (
        build_tracking_targets(
            [
                {
                    "product_id": 301,
                    "product_code": "NONE-1",
                    "tracking": "none",
                    "location_id": 701,
                    "location": "WH/Stock/C",
                    "quantity": 99.0,
                }
            ]
        )
        == []
    )
