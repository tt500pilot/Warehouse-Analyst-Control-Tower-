from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.generate_simulation_sandbox import build_warehouse


LOCATION_COLUMNS = [
    "record_type",
    "odoo_location_id",
    "complete_name",
    "barcode",
    "logical_area",
    "currently_holds_stock",
    "current_on_hand_units",
    "current_reserved_units",
    "x",
    "y",
    "z",
    "graph_node_id",
    "measurement_status",
    "accessible_by",
    "one_way_or_access_notes",
    "secure_zone",
    "flight_critical_allowed",
    "capacity_units",
    "capacity_weight_lb",
    "notes",
]

NODE_COLUMNS = [
    "graph_node_id",
    "node_type",
    "serves_location",
    "x",
    "y",
    "z",
    "measurement_status",
    "accessible_by",
    "access_restriction",
    "notes",
]

EDGE_COLUMNS = [
    "from_graph_node_id",
    "to_graph_node_id",
    "distance_ft",
    "edge_type",
    "bidirectional",
    "access_restriction",
    "notes",
]


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _tail(name: str) -> str:
    return str(name).strip().split("/")[-1]


def _shortest_path(
    adjacency: dict[str, list[str]],
    start: str,
    target: str,
) -> list[str]:
    if start == target:
        return [start]
    queue = deque([start])
    previous: dict[str, str | None] = {start: None}
    while queue:
        current = queue.popleft()
        for neighbor in sorted(adjacency.get(current, [])):
            if neighbor in previous:
                continue
            previous[neighbor] = current
            if neighbor == target:
                path = [target]
                cursor = current
                while cursor is not None:
                    path.append(cursor)
                    cursor = previous[cursor]
                return list(reversed(path))
            queue.append(neighbor)
    raise RuntimeError(f"No fixture path between {start} and {target}")


def _edge_key(left: str, right: str) -> tuple[str, str]:
    return tuple(sorted((left, right)))


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sandbox-only helper that completes a blank AWIA mapping intake from the "
            "deterministic mock warehouse geometry. It must never be used to infer real warehouse geometry."
        )
    )
    parser.add_argument(
        "--locations",
        default="data/mapping_intake/wh-stock-awia-mock-aisle-b-locations.csv",
    )
    parser.add_argument(
        "--output-dir",
        default="data/mapping_intake/mock_completed",
    )
    args = parser.parse_args()

    input_path = Path(args.locations)
    intake_rows = _read_csv(input_path)
    if not intake_rows:
        raise RuntimeError("Input mapping intake has no location rows.")

    storage_rows = [row for row in intake_rows if row.get("record_type") == "storage_bin"]
    if not storage_rows:
        raise RuntimeError("Input mapping intake has no storage_bin rows.")
    if any("/AWIA Mock/" not in str(row.get("complete_name") or "") for row in storage_rows):
        raise RuntimeError(
            "Refusing to auto-complete a non-mock mapping intake. This helper is sandbox-only and must not infer real geometry."
        )

    metadata, stations, fixture_locations, fixture_nodes, fixture_edges = build_warehouse()
    fixture_location_by_tail = {
        _tail(row["odoo_complete_name"]): row for row in fixture_locations
    }
    fixture_node_by_id = {str(row["node_id"]): row for row in fixture_nodes}
    fixture_station_by_id = {str(row["station_id"]): row for row in stations}

    completed_locations: list[dict[str, Any]] = []
    required_access_nodes: set[str] = set()
    served_locations: dict[str, list[str]] = defaultdict(list)

    for row in intake_rows:
        completed = dict(row)
        record_type = str(row.get("record_type") or "")
        name = str(row.get("complete_name") or "")
        if record_type == "storage_bin":
            fixture = fixture_location_by_tail.get(_tail(name))
            if fixture is None:
                raise RuntimeError(f"No deterministic fixture location for {name}")
            completed.update(
                {
                    "x": fixture["x"],
                    "y": fixture["y"],
                    "z": fixture["z"],
                    "graph_node_id": fixture["graph_node_id"],
                    "measurement_status": "MOCK_FIXTURE",
                    "secure_zone": fixture["secure"],
                    "flight_critical_allowed": fixture["flight_critical_allowed"],
                    "capacity_units": fixture["capacity_units"],
                    "capacity_weight_lb": fixture["capacity_weight_lb"],
                    "notes": "Synthetic deterministic geometry/capacity fixture; not a field measurement.",
                }
            )
        elif record_type == "flow_endpoint" and name == "WH/Pre-Production":
            # Sandbox manufacturing uses WH/Pre-Production as the Odoo-side proxy
            # for the physical Kitting station in the deterministic geometry.
            station = fixture_station_by_id["ST_KITTING"]
            completed.update(
                {
                    "x": station["x"],
                    "y": station["y"],
                    "z": station["z"],
                    "graph_node_id": station["graph_node_id"],
                    "measurement_status": "MOCK_FIXTURE",
                    "notes": "Synthetic mapping: WH/Pre-Production -> ST_KITTING/NODE_KITTING.",
                }
            )
        else:
            raise RuntimeError(
                f"Unsupported mock flow endpoint {name!r}; add an explicit deterministic station mapping first."
            )

        node_id = str(completed.get("graph_node_id") or "")
        if not node_id:
            raise RuntimeError(f"Completed row {name!r} has no graph_node_id")
        required_access_nodes.add(node_id)
        served_locations[node_id].append(name)
        completed_locations.append(completed)

    adjacency: dict[str, list[str]] = defaultdict(list)
    fixture_edge_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    for edge in fixture_edges:
        left = str(edge["from_node"])
        right = str(edge["to_node"])
        adjacency[left].append(right)
        adjacency[right].append(left)
        fixture_edge_by_key[_edge_key(left, right)] = edge

    anchor = "NODE_KITTING"
    if anchor not in required_access_nodes:
        raise RuntimeError("Completed mock scope must include WH/Pre-Production/NODE_KITTING as the routing anchor.")

    selected_nodes: set[str] = {anchor}
    selected_edge_keys: set[tuple[str, str]] = set()
    for target in sorted(required_access_nodes):
        path = _shortest_path(adjacency, anchor, target)
        selected_nodes.update(path)
        for left, right in zip(path, path[1:]):
            selected_edge_keys.add(_edge_key(left, right))

    node_rows: list[dict[str, Any]] = []
    for node_id in sorted(selected_nodes):
        fixture = fixture_node_by_id.get(node_id)
        if fixture is None:
            raise RuntimeError(f"Selected fixture node {node_id!r} is missing from node table")
        node_rows.append(
            {
                "graph_node_id": node_id,
                "node_type": fixture["node_type"],
                "serves_location": " | ".join(sorted(served_locations.get(node_id, []))),
                "x": fixture["x"],
                "y": fixture["y"],
                "z": fixture["z"],
                "measurement_status": "MOCK_FIXTURE",
                "accessible_by": "",
                "access_restriction": "",
                "notes": "Synthetic deterministic legal-path node; not field measured.",
            }
        )

    edge_rows: list[dict[str, Any]] = []
    for key in sorted(selected_edge_keys):
        fixture = fixture_edge_by_key[key]
        edge_rows.append(
            {
                "from_graph_node_id": fixture["from_node"],
                "to_graph_node_id": fixture["to_node"],
                "distance_ft": fixture["distance_ft"],
                "edge_type": fixture["edge_type"],
                "bidirectional": fixture["bidirectional"],
                "access_restriction": "",
                "notes": "Synthetic deterministic legal path; not field measured.",
            }
        )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = input_path.name
    if prefix.endswith("-locations.csv"):
        prefix = prefix[: -len("-locations.csv")]
    else:
        prefix = input_path.stem

    locations_path = output_dir / f"{prefix}-locations.csv"
    nodes_path = output_dir / f"{prefix}-graph-nodes.csv"
    edges_path = output_dir / f"{prefix}-path-edges.csv"
    manifest_path = output_dir / f"{prefix}-manifest.json"

    _write_csv(locations_path, LOCATION_COLUMNS, completed_locations)
    _write_csv(nodes_path, NODE_COLUMNS, node_rows)
    _write_csv(edges_path, EDGE_COLUMNS, edge_rows)

    manifest = {
        "mode": "sandbox_only_mock_mapping_completion",
        "real_warehouse_geometry_inferred": False,
        "layout_version": metadata["layout_version"],
        "datum": metadata["datum"],
        "input_locations": str(input_path),
        "outputs": {
            "locations": str(locations_path),
            "nodes": str(nodes_path),
            "edges": str(edges_path),
        },
        "counts": {
            "location_rows": len(completed_locations),
            "required_location_access_nodes": len(required_access_nodes),
            "selected_graph_nodes": len(node_rows),
            "selected_path_edges": len(edge_rows),
        },
        "routing_anchor": anchor,
        "notes": [
            "This file proves the mapping/validation software loop using deterministic synthetic geometry only.",
            "It must never be used to infer Firefly or any other real warehouse coordinates/topology.",
            "WH/Pre-Production is explicitly mapped to the sandbox ST_KITTING/NODE_KITTING approximation.",
            "Synthetic capacity values remain algorithm fixtures, not validated physical limits.",
        ],
    }
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
