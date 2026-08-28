from app.services.virtual_picker import (
    Geometry,
    PickerAssumptions,
    build_virtual_picker_plan,
    shortest_path,
)


def _geometry():
    return Geometry(
        locations_by_tail={},
        adjacency={
            "NODE_KITTING": [("A", 10.0)],
            "A": [("NODE_KITTING", 10.0), ("B", 10.0), ("C", 30.0)],
            "B": [("A", 10.0), ("C", 10.0)],
            "C": [("B", 10.0), ("A", 30.0)],
        },
    )


def test_shortest_path_prefers_lower_legal_distance():
    distance, path = shortest_path(_geometry().adjacency, "NODE_KITTING", "C")
    assert distance == 30.0
    assert path == ["NODE_KITTING", "A", "B", "C"]


def test_virtual_picker_is_deterministic_and_nearest_neighbor():
    reservations = [
        {
            "move_line_id": 2,
            "product_id": 102,
            "product": "B",
            "product_code": "B",
            "tracking": "none",
            "flight_critical": False,
            "quantity": 1.0,
            "lot_id": None,
            "lot": None,
            "source_location": "WH/Stock/C",
            "location_tail": "C",
            "zone": "STANDARD",
            "level": 1,
            "graph_node_id": "C",
        },
        {
            "move_line_id": 1,
            "product_id": 101,
            "product": "A",
            "product_code": "A",
            "tracking": "lot",
            "flight_critical": True,
            "quantity": 2.0,
            "lot_id": 900,
            "lot": "LOT-A",
            "source_location": "WH/Stock/A",
            "location_tail": "A",
            "zone": "FAST",
            "level": 1,
            "graph_node_id": "A",
        },
    ]
    assumptions = PickerAssumptions(walking_speed_ft_s=2.0, deterministic_jitter_seconds=1.0)

    first = build_virtual_picker_plan(
        reservations,
        _geometry(),
        picking_id=5,
        assumptions=assumptions,
        seed=42,
    )
    second = build_virtual_picker_plan(
        reservations,
        _geometry(),
        picking_id=5,
        assumptions=assumptions,
        seed=42,
    )

    assert first == second
    assert [row["product_code"] for row in first["stops"]] == ["A", "B"]
    assert first["summary"]["total_distance_ft"] == 60.0
    assert first["classification"] == "simulated_human_like"


def test_virtual_picker_tracking_and_flight_critical_add_time():
    geometry = Geometry(
        locations_by_tail={},
        adjacency={"NODE_KITTING": [("A", 0.0)], "A": [("NODE_KITTING", 0.0)]},
    )
    base = {
        "move_line_id": 1,
        "product_id": 101,
        "product": "A",
        "product_code": "A",
        "quantity": 1.0,
        "lot_id": None,
        "lot": None,
        "source_location": "WH/Stock/A",
        "location_tail": "A",
        "zone": "FAST",
        "level": 1,
        "graph_node_id": "A",
    }
    assumptions = PickerAssumptions(deterministic_jitter_seconds=0.0)
    ordinary = build_virtual_picker_plan(
        [{**base, "tracking": "none", "flight_critical": False}],
        geometry,
        picking_id=1,
        assumptions=assumptions,
    )
    controlled = build_virtual_picker_plan(
        [{**base, "tracking": "serial", "flight_critical": True}],
        geometry,
        picking_id=1,
        assumptions=assumptions,
    )

    assert controlled["stops"][0]["scan_seconds"] > ordinary["stops"][0]["scan_seconds"]
