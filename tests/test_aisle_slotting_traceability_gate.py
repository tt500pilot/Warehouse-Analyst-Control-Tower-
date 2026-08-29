from app.services.aisle_slotting import analyze_aisle_slotting


def _geometry():
    return {
        "schema_version": "awia-warehouse-geometry-v1",
        "anchor": {"complete_name": "WH/Pre-Production"},
        "summary": {"measurement_statuses": ["MOCK_FIXTURE"]},
        "locations": [
            {
                "record_type": "storage_bin",
                "odoo_location_id": 10,
                "complete_name": "WH/Stock/AWIA Mock/H01-L1",
                "access_geometry": {"vertical_reach_ft": 2.5, "horizontal_offset_ft": 4.0},
            },
            {
                "record_type": "storage_bin",
                "odoo_location_id": 20,
                "complete_name": "WH/Stock/AWIA Mock/H02-L1",
                "access_geometry": {"vertical_reach_ft": 2.5, "horizontal_offset_ft": 4.0},
            },
            {
                "record_type": "storage_bin",
                "odoo_location_id": 30,
                "complete_name": "WH/Stock/AWIA Mock/H03-L1",
                "access_geometry": {"vertical_reach_ft": 2.5, "horizontal_offset_ft": 4.0},
            },
        ],
        "anchor_distances": [
            {"odoo_location_id": 10, "graph_distance_to_access_ft": 100.0},
            {"odoo_location_id": 20, "graph_distance_to_access_ft": 90.0},
            {"odoo_location_id": 30, "graph_distance_to_access_ft": 10.0},
        ],
    }


def _products():
    return [
        {"id": 1, "default_code": "LOT-HIGH", "name": "Blocked tracked part", "tracking": "lot"},
        {"id": 2, "default_code": "CLEAN-LOW", "name": "Clean part", "tracking": "none"},
    ]


def _quants():
    return [
        {
            "product_id": [1, "Blocked tracked part"],
            "location_id": [10, "WH/Stock/AWIA Mock/H01-L1"],
            "quantity": 10.0,
            "reserved_quantity": 0.0,
            "lot_id": False,
        },
        {
            "product_id": [2, "Clean part"],
            "location_id": [20, "WH/Stock/AWIA Mock/H02-L1"],
            "quantity": 10.0,
            "reserved_quantity": 0.0,
            "lot_id": False,
        },
    ]


def _moves():
    rows = []
    for index in range(10):
        rows.append(
            {
                "id": index + 1,
                "product_id": [1, "Blocked tracked part"],
                "location_id": [10, "WH/Stock/AWIA Mock/H01-L1"],
                "location_dest_id": [999, "WH/Pre-Production"],
                "quantity": 1.0,
            }
        )
    for index in range(5):
        rows.append(
            {
                "id": 100 + index,
                "product_id": [2, "Clean part"],
                "location_id": [20, "WH/Stock/AWIA Mock/H02-L1"],
                "location_dest_id": [999, "WH/Pre-Production"],
                "quantity": 1.0,
            }
        )
    return rows


def test_traceability_block_is_applied_before_target_allocation():
    traceability_blocks = {
        (1, 10): {
            "product_id": 1,
            "location_id": 10,
            "anonymous_quantity": 10.0,
            "traceability_coverage_pct": 0.0,
            "reasons": ["positive_tracked_quantity_without_lot_or_serial_identity"],
        }
    }

    result = analyze_aisle_slotting(
        _geometry(),
        _products(),
        _quants(),
        _moves(),
        traceability_blocks=traceability_blocks,
    )

    assert result["summary"]["traceability_candidates_suppressed"] == 1
    assert result["summary"]["recommendations"] == 1

    recommendation = result["recommendations"][0]
    assert recommendation["product_code"] == "CLEAN-LOW"
    assert recommendation["candidate"]["odoo_location_id"] == 30

    blocked = [
        row for row in result["not_recommended"] if row.get("reason") == "blocked_traceability"
    ]
    assert len(blocked) == 1
    assert blocked[0]["product_code"] == "LOT-HIGH"
    assert blocked[0]["hard_gate"] is True
    assert blocked[0]["anonymous_quantity"] == 10.0


def test_traceability_blocked_inventory_remains_occupied():
    traceability_blocks = {(1, 10): {"anonymous_quantity": 10.0, "reasons": []}}

    result = analyze_aisle_slotting(
        _geometry(),
        _products(),
        _quants(),
        _moves(),
        traceability_blocks=traceability_blocks,
    )

    assert result["summary"]["initially_occupied_bins"] == 2
    assert result["summary"]["initially_empty_bins"] == 1
    assert result["recommendations"][0]["candidate"]["odoo_location_id"] == 30


def test_without_traceability_block_higher_priority_sku_consumes_best_target():
    result = analyze_aisle_slotting(
        _geometry(),
        _products(),
        _quants(),
        _moves(),
    )

    assert result["summary"]["traceability_candidates_suppressed"] == 0
    assert result["recommendations"][0]["product_code"] == "LOT-HIGH"
    assert result["recommendations"][0]["candidate"]["odoo_location_id"] == 30
