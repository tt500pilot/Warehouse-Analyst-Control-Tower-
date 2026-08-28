from app.services.kitting_transactions import inspect_kitting_transactions


def test_inspector_links_mo_to_pbm_picking_and_reservation_lines():
    mos = [
        {
            "id": 10,
            "name": "WH/MO/00001",
            "origin": "AWIA-MOCK-MO-001",
            "state": "confirmed",
            "product_id": [500, "AWIA-FG-OTS"],
            "bom_id": [700, "AWIA-BOM-OTS"],
            "picking_ids": [20],
        }
    ]
    pickings = [
        {
            "id": 20,
            "name": "WH/PC/00001",
            "origin": "WH/MO/00001",
            "state": "assigned",
            "picking_type_id": [5, "Pick Components"],
            "location_id": [1, "WH/Stock"],
            "location_dest_id": [2, "WH/Pre-Production"],
            "create_date": "2026-08-28 12:00:00",
            "scheduled_date": "2026-08-28 12:00:00",
            "date_done": False,
        }
    ]
    moves = [
        {
            "id": 30,
            "picking_id": [20, "WH/PC/00001"],
            "product_id": [101, "VALVE-441"],
            "product_uom_qty": 1.0,
            "quantity": 1.0,
            "state": "assigned",
            "picked": False,
            "has_tracking": "none",
            "location_id": [1, "WH/Stock"],
            "location_dest_id": [2, "WH/Pre-Production"],
        }
    ]
    move_lines = [
        {
            "id": 40,
            "move_id": [30, "VALVE-441"],
            "picking_id": [20, "WH/PC/00001"],
            "product_id": [101, "VALVE-441"],
            "quantity": 1.0,
            "picked": False,
            "tracking": "none",
            "lot_id": False,
            "lot_name": False,
            "location_id": [11, "WH/Stock/CONTROLLED/F-05-L1-BA"],
            "location_dest_id": [2, "WH/Pre-Production"],
            "state": "assigned",
        }
    ]

    report = inspect_kitting_transactions(mos, pickings, moves, move_lines)

    assert report["summary"]["manufacturing_orders"] == 1
    assert report["summary"]["manufacturing_orders_with_pick_components"] == 1
    assert report["summary"]["pick_component_transfers"] == 1
    assert report["summary"]["ready_transfers"] == 1
    assert report["summary"]["execution_ready_transfers"] == 1
    assert report["summary"]["all_transfers_execution_ready"] is True
    assert report["summary"]["all_manufacturing_orders_linked"] is True

    transaction = report["transactions"][0]
    assert transaction["link_method"] == "mrp.production.picking_ids"
    component = transaction["pick_component_transfers"][0]["components"][0]
    assert component["demand_quantity"] == 1.0
    assert component["reserved_line_quantity"] == 1.0
    assert component["reserved_source_locations"] == ["WH/Stock/CONTROLLED/F-05-L1-BA"]
    assert component["picked"] is False
    assert component["execution_ready"] is True


def test_inspector_blocks_execution_when_tracked_line_has_no_lot_or_serial():
    report = inspect_kitting_transactions(
        [
            {
                "id": 10,
                "name": "WH/MO/00001",
                "origin": "AWIA-MOCK-MO-001",
                "state": "confirmed",
                "picking_ids": [20],
            }
        ],
        [
            {
                "id": 20,
                "name": "WH/PC/00001",
                "origin": "WH/MO/00001",
                "state": "assigned",
                "picking_type_id": [5, "Pick Components"],
            }
        ],
        [
            {
                "id": 30,
                "picking_id": [20, "WH/PC/00001"],
                "product_id": [101, "SERIAL-AVX-7"],
                "product_uom_qty": 1.0,
                "quantity": 1.0,
                "state": "assigned",
                "picked": False,
                "has_tracking": "serial",
            }
        ],
        [
            {
                "id": 40,
                "move_id": [30, "SERIAL-AVX-7"],
                "picking_id": [20, "WH/PC/00001"],
                "product_id": [101, "SERIAL-AVX-7"],
                "quantity": 1.0,
                "tracking": "serial",
                "lot_id": False,
                "lot_name": False,
                "state": "assigned",
            }
        ],
    )

    assert report["summary"]["tracked_component_moves"] == 1
    assert report["summary"]["tracked_reservation_lines"] == 1
    assert report["summary"]["tracking_missing_lines"] == 1
    assert report["summary"]["execution_ready_transfers"] == 0
    assert report["summary"]["all_transfers_execution_ready"] is False


def test_inspector_accepts_native_lot_assignment_for_tracked_line():
    report = inspect_kitting_transactions(
        [
            {
                "id": 10,
                "name": "WH/MO/00001",
                "origin": "AWIA-MOCK-MO-001",
                "state": "confirmed",
                "picking_ids": [20],
            }
        ],
        [
            {
                "id": 20,
                "name": "WH/PC/00001",
                "origin": "WH/MO/00001",
                "state": "assigned",
                "picking_type_id": [5, "Pick Components"],
            }
        ],
        [
            {
                "id": 30,
                "picking_id": [20, "WH/PC/00001"],
                "product_id": [101, "SEAL-218"],
                "product_uom_qty": 2.0,
                "quantity": 2.0,
                "state": "assigned",
                "picked": False,
                "has_tracking": "lot",
            }
        ],
        [
            {
                "id": 40,
                "move_id": [30, "SEAL-218"],
                "picking_id": [20, "WH/PC/00001"],
                "product_id": [101, "SEAL-218"],
                "quantity": 2.0,
                "tracking": "lot",
                "lot_id": [900, "LOT-SEAL-001"],
                "lot_name": False,
                "state": "assigned",
            }
        ],
    )

    assert report["summary"]["tracking_assigned_lines"] == 1
    assert report["summary"]["tracking_missing_lines"] == 0
    assert report["summary"]["execution_ready_transfers"] == 1
    assert report["summary"]["all_transfers_execution_ready"] is True


def test_inspector_keeps_open_transfers_out_of_cycle_time_claims():
    report = inspect_kitting_transactions(
        [
            {
                "id": 10,
                "name": "WH/MO/00001",
                "origin": "AWIA-MOCK-MO-001",
                "state": "confirmed",
                "picking_ids": [],
            }
        ],
        [],
        [],
        [],
    )

    assert report["summary"]["all_manufacturing_orders_linked"] is False
    assert "Cycle-time KPIs remain restricted to completed pickings" in report["methodology"]["timing"]
