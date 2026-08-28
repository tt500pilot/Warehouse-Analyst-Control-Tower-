from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _components(nodes: set[str], edges: list[tuple[str, str]]) -> list[list[str]]:
    adjacency: dict[str, set[str]] = defaultdict(set)
    for left, right in edges:
        adjacency[left].add(right)
        adjacency[right].add(left)
    remaining = set(nodes)
    result: list[list[str]] = []
    while remaining:
        start = min(remaining)
        queue = deque([start])
        seen = {start}
        remaining.remove(start)
        while queue:
            node = queue.popleft()
            for neighbor in adjacency.get(node, set()):
                if neighbor in seen:
                    continue
                seen.add(neighbor)
                remaining.discard(neighbor)
                queue.append(neighbor)
        result.append(sorted(seen))
    return sorted(result, key=lambda component: (-len(component), component[0]))


def validate(
    locations: list[dict[str, str]],
    nodes: list[dict[str, str]],
    edges: list[dict[str, str]],
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []

    location_names: set[str] = set()
    location_node_refs: set[str] = set()
    for index, row in enumerate(locations, start=2):
        name = str(row.get("complete_name") or "").strip()
        if not name:
            errors.append(f"locations.csv row {index}: complete_name is required")
            continue
        if name in location_names:
            errors.append(f"locations.csv row {index}: duplicate complete_name {name!r}")
        location_names.add(name)

        for axis in ("x", "y", "z"):
            if _float(row.get(axis)) is None:
                errors.append(f"locations.csv row {index} ({name}): numeric {axis} is required")
        node_id = str(row.get("graph_node_id") or "").strip()
        if not node_id:
            errors.append(f"locations.csv row {index} ({name}): graph_node_id is required")
        else:
            location_node_refs.add(node_id)

        if str(row.get("record_type") or "") == "storage_bin":
            for capacity_field in ("capacity_units", "capacity_weight_lb"):
                value = _float(row.get(capacity_field))
                if value is None:
                    warnings.append(
                        f"locations.csv row {index} ({name}): {capacity_field} is not populated"
                    )
                elif value <= 0:
                    errors.append(
                        f"locations.csv row {index} ({name}): {capacity_field} must be positive"
                    )

    node_ids: set[str] = set()
    duplicate_nodes: set[str] = set()
    for index, row in enumerate(nodes, start=2):
        node_id = str(row.get("graph_node_id") or "").strip()
        if not node_id:
            errors.append(f"graph-nodes.csv row {index}: graph_node_id is required")
            continue
        if node_id in node_ids:
            duplicate_nodes.add(node_id)
        node_ids.add(node_id)
        for axis in ("x", "y", "z"):
            if _float(row.get(axis)) is None:
                errors.append(f"graph-nodes.csv row {index} ({node_id}): numeric {axis} is required")
    for node_id in sorted(duplicate_nodes):
        errors.append(f"graph-nodes.csv: duplicate graph_node_id {node_id!r}")

    missing_location_nodes = sorted(location_node_refs - node_ids)
    for node_id in missing_location_nodes:
        errors.append(f"locations.csv references missing graph node {node_id!r}")

    graph_edges: list[tuple[str, str]] = []
    for index, row in enumerate(edges, start=2):
        left = str(row.get("from_graph_node_id") or "").strip()
        right = str(row.get("to_graph_node_id") or "").strip()
        if not left or not right:
            errors.append(f"path-edges.csv row {index}: from/to graph node IDs are required")
            continue
        if left not in node_ids:
            errors.append(f"path-edges.csv row {index}: unknown from node {left!r}")
        if right not in node_ids:
            errors.append(f"path-edges.csv row {index}: unknown to node {right!r}")
        if left == right:
            errors.append(f"path-edges.csv row {index}: self-edge {left!r} is not allowed")

        distance = _float(row.get("distance_ft"))
        if distance is None or distance <= 0:
            errors.append(f"path-edges.csv row {index}: distance_ft must be a positive number")

        bidirectional = _bool(row.get("bidirectional"))
        if bidirectional is None:
            warnings.append(
                f"path-edges.csv row {index} ({left}->{right}): bidirectional should be true/false"
            )
        if left in node_ids and right in node_ids and left != right:
            graph_edges.append((left, right))

    if node_ids and not graph_edges:
        errors.append("path graph has nodes but no valid edges")

    components = _components(node_ids, graph_edges) if node_ids else []
    if len(components) > 1:
        errors.append(
            f"path graph is disconnected: {len(components)} components; all mapped bins/endpoints must connect through legal paths"
        )

    location_types = defaultdict(int)
    for row in locations:
        location_types[str(row.get("record_type") or "unknown")] += 1

    return {
        "mode": "local_mapping_intake_validation",
        "ready_for_geometry_import": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "location_rows": len(locations),
            "storage_bins": location_types.get("storage_bin", 0),
            "flow_endpoints": location_types.get("flow_endpoint", 0),
            "graph_nodes": len(nodes),
            "path_edges": len(edges),
            "graph_components": len(components),
        },
        "graph_components": components,
        "guardrail": "Validation does not prove coordinates/capacities are physically correct; human field verification is still required.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate a completed AWIA XYZ/legal-path mapping intake package.")
    parser.add_argument("--locations", required=True)
    parser.add_argument("--nodes", required=True)
    parser.add_argument("--edges", required=True)
    args = parser.parse_args()

    result = validate(
        _read_csv(Path(args.locations)),
        _read_csv(Path(args.nodes)),
        _read_csv(Path(args.edges)),
    )
    print(json.dumps(result, indent=2))
    if not result["ready_for_geometry_import"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
