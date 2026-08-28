import pytest

from scripts.execute_kitting_transfer import (
    audit_inventory_deltas,
    build_expected_inventory_movements,
    select_execution_candidate,
)


def test_select_execution_candidate_chooses_first_ready_transfer():
    report = {
        "transactions": [
            {
                "manufacturing_order_id": 1,
                "manufacturing_order": "WH/MO/1",
                "awia_origin": "AWIA-MOCK-MO-001",
                "product": "FG-1",
                "bom": "BOM-1",
                "pick_component_transfers": [
                    {
                        "picking_id": 20,
                        "picking_name": "WH/PC/20",
                        "state": "waiting",
                        "execution_ready": False,
                    },
                    {
                        "picking_id": 10,
                        "picking_name": "WH/PC/10",
                        "state": "assigned",
                        "execution_ready": True,
                        "source_location": "WH/Stock",
                        "destination_location": "WH/Pre-Production",
                        "component_move_count": 3,
                        "move_line_count": 4,
                    },
                ],
            }
        ]
    }

    selected = select_execution_candidate(report)

    assert selected["picking_id"] == 10
    assert selected["awia_origin"] == "AWIA-MOCK-MO-001"


def test_select_execution_candidate_rejects_requested_nonready_transfer():
    report = {
        "transactions": [
            {
                "pick_component_transfers": [
                    {
                        "picking_id": 20,
                        "picking_name": "WH/PC/20",
                        "state": "waiting",
                        "execution_ready": False,
                    }
                ]
            }
        ]
    }

    with pytest.raises(RuntimeError, match="not currently assigned and execution-ready"):
        select_execution_candidate(report, picking_id=20)


def test_build_expected_inventory_movements_aggregates_same_lot_route():
    rows = [
        {
            "id": 1,
            "product_id": [101, "VALVE-441"],
            "quantity": 1.0,
            "location_id": [501, "WH/Stock/A"],
            "location_dest_id": [900, "WH/Pre-Production"],
            "lot_id": [7001, "A-VALVE-441-S0001"],
        },
        {
            "id": 2,
            "product_id": [101, "VALVE-441"],
            "quantity": 2.0,
            "location_id": [501, "WH/Stock/A"],
            "location_dest_id": [900, "WH/Pre-Production"],
            "lot_id": [7001, "A-VALVE-441-S0001"],
        },
    ]

    result = build_expected_inventory_movements(rows)

    assert len(result) == 1
    assert result[0]["expected_quantity"] == 3.0
    assert result[0]["move_line_ids"] == [1, 2]


def test_inventory_audit_requires_equal_and_opposite_stock_deltas():
    before = [
        {
            "product_id": 101,
            "product": "VALVE-441",
            "source_location_id": 501,
            "source_location": "WH/Stock/A",
            "destination_location_id": 900,
            "destination_location": "WH/Pre-Production",
            "lot_id": 7001,
            "lot": "A-VALVE-441-S0001",
            "expected_quantity": 1.0,
            "source_quantity": 1.0,
            "destination_quantity": 0.0,
        }
    ]
    after = [
        {
            **before[0],
            "source_quantity": 0.0,
            "destination_quantity": 1.0,
        }
    ]

    audit = audit_inventory_deltas(before, after)

    assert audit["passed"] is True
    assert audit["rows"][0]["source_delta"] == -1.0
    assert audit["rows"][0]["destination_delta"] == 1.0


def test_inventory_audit_fails_wrong_destination_delta():
    before = [
        {
            "product_id": 101,
            "product": "VALVE-441",
            "source_location_id": 501,
            "source_location": "WH/Stock/A",
            "destination_location_id": 900,
            "destination_location": "WH/Pre-Production",
            "lot_id": 7001,
            "lot": "A-VALVE-441-S0001",
            "expected_quantity": 1.0,
            "source_quantity": 1.0,
            "destination_quantity": 0.0,
        }
    ]
    after = [
        {
            **before[0],
            "source_quantity": 0.0,
            "destination_quantity": 0.5,
        }
    ]

    audit = audit_inventory_deltas(before, after)

    assert audit["passed"] is False
