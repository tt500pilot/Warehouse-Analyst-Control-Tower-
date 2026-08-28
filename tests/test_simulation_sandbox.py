from __future__ import annotations

import csv
import json
from collections import defaultdict, deque

from scripts.generate_simulation_sandbox import generate


def _read_csv(path):
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_simulation_sandbox_manifest_and_referential_integrity(tmp_path):
    manifest = generate(tmp_path)
    assert manifest["seed"] == 42
    assert manifest["layout_version"] == "mock-v1"
    assert manifest["counts"]["bin_locations"] == 192
    assert manifest["counts"]["products"] == 80
    assert manifest["counts"]["manufacturing_orders"] == 120
    assert manifest["counts"]["stock_move_lines"] >= 2000

    products = _read_csv(tmp_path / "mock_odoo" / "products.csv")
    stock_locations = _read_csv(tmp_path / "mock_odoo" / "stock_locations.csv")
    quants = _read_csv(tmp_path / "mock_odoo" / "quants.csv")
    move_lines = _read_csv(tmp_path / "mock_odoo" / "stock_move_lines.csv")

    product_ids = {row["odoo_product_id"] for row in products}
    location_ids = {row["odoo_location_id"] for row in stock_locations}

    assert all(row["product_id"] in product_ids for row in quants)
    assert all(row["location_id"] in location_ids for row in quants)
    assert all(row["product_id"] in product_ids for row in move_lines)
    assert all(row["location_id"] in location_ids for row in move_lines)
    assert all(row["location_dest_id"] in location_ids for row in move_lines)


def test_warehouse_graph_is_connected_and_every_bin_maps_to_a_node(tmp_path):
    generate(tmp_path)
    nodes = _read_csv(tmp_path / "mock_warehouse" / "nodes.csv")
    edges = _read_csv(tmp_path / "mock_warehouse" / "edges.csv")
    locations = _read_csv(tmp_path / "mock_warehouse" / "locations.csv")

    node_ids = {row["node_id"] for row in nodes}
    adjacency = defaultdict(set)
    for edge in edges:
        adjacency[edge["from_node"]].add(edge["to_node"])
        adjacency[edge["to_node"]].add(edge["from_node"])

    start = next(iter(node_ids))
    visited = {start}
    queue = deque([start])
    while queue:
        node = queue.popleft()
        for neighbor in adjacency[node]:
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    assert visited == node_ids
    assert all(row["graph_node_id"] in node_ids for row in locations)


def test_benchmark_scenarios_are_intentionally_embedded(tmp_path):
    generate(tmp_path)
    quants = _read_csv(tmp_path / "mock_odoo" / "quants.csv")
    products = _read_csv(tmp_path / "mock_odoo" / "products.csv")
    moves = _read_csv(tmp_path / "mock_odoo" / "stock_move_lines.csv")
    bom_lines = _read_csv(tmp_path / "mock_odoo" / "bom_lines.csv")
    expected = json.loads((tmp_path / "expected_findings.json").read_text(encoding="utf-8"))

    quant_by_code = {row["product_code"]: row for row in quants}
    assert quant_by_code["BOLT-104"]["location_code"] == "WH/Stock/BULK/H-06-L2-BB"
    assert quant_by_code["BRACKET-77"]["location_code"] == "WH/Stock/FAST/A-01-L1-BA"
    assert quant_by_code["REGULATOR-552"]["location_code"] == "WH/Stock/BULK/H-04-L1-BA"

    product_by_code = {row["default_code"]: row for row in products}
    assert product_by_code["SERIAL-AVX-7"]["tracking"] == "serial"
    assert product_by_code["SERIAL-AVX-7"]["x_is_flight_critical"] == "True"

    recent_serial_moves = [row for row in moves if row["product_code"] == "SERIAL-AVX-7"]
    assert len(recent_serial_moves) >= 55

    ots_codes = {row["component_code"] for row in bom_lines if row["mock_bom_id"] == "BOM-OTS"}
    assert {"VALVE-441", "BRACKET-221", "HARNESS-310", "FASTENER-900"}.issubset(ots_codes)

    benchmark_ids = {item["id"] for item in expected["benchmarks"]}
    assert {
        "bad_slotting_high_velocity",
        "premium_slot_misuse",
        "co_pick_affinity",
        "cycle_count_risk",
        "shortage_prone_component",
        "putaway_inefficiency",
    }.issubset(benchmark_ids)


def test_mock_datum_is_fixed_and_documented(tmp_path):
    generate(tmp_path)
    metadata = json.loads((tmp_path / "mock_warehouse" / "warehouse_metadata.json").read_text(encoding="utf-8"))
    assert metadata["coordinate_system"] == "local_cartesian"
    assert metadata["units"] == "ft"
    assert metadata["datum"]["x"] == 0.0
    assert metadata["datum"]["y"] == 0.0
    assert metadata["datum"]["z"] == 0.0
    assert "Permanent" in metadata["datum"]["rule"]
