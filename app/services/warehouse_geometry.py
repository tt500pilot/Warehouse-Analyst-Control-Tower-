"""Canonical warehouse geometry model for AWIA.

Consumes a *validated* mapping intake and converts it into a reusable geometry
payload for routing, slotting, and later warehouse-delivery optimization.

The model keeps physical bin XYZ separate from legal path-graph geometry. Route
distance is computed to each location's graph access node. Horizontal rack-face
offset and vertical reach are exposed separately rather than silently folded
into travel distance.
"""

from __future__ import annotations

import heapq
import math
from collections import defaultdict
from typing import Any, Iterable, Mapping

Record = Mapping[str, Any]


def _float(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"Non-finite numeric value {value!r}")
    return number


def _bool(value: Any) -> bool | None:
    text = str(value or "").strip().lower()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def build_adjacency(
    nodes: Iterable[Record],
    edges: Iterable[Record],
) -> dict[str, list[tuple[str, float]]]:
    node_ids = {str(row.get("graph_node_id") or "").strip() for row in nodes}
    node_ids.discard("")
    adjacency: dict[str, list[tuple[str, float]]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        left = str(edge.get("from_graph_node_id") or "").strip()
        right = str(edge.get("to_graph_node_id") or "").strip()
        distance = _float(edge.get("distance_ft"))
        if left not in node_ids or right not in node_ids:
            raise ValueError(f"Edge references unknown node: {left!r} -> {right!r}")
        if distance <= 0:
            raise ValueError(f"Edge distance must be positive: {left!r} -> {right!r}")
        adjacency[left].append((right, distance))
        if _bool(edge.get("bidirectional")) is True:
            adjacency[right].append((left, distance))
    return adjacency


def shortest_path(
    adjacency: Mapping[str, list[tuple[str, float]]],
    start: str,
    goal: str,
) -> tuple[float, list[str]]:
    if start == goal:
        return 0.0, [start]
    queue: list[tuple[float, str]] = [(0.0, start)]
    distances = {start: 0.0}
    parents: dict[str, str] = {}
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances.get(node):
            continue
        if node == goal:
            path = [goal]
            while path[-1] != start:
                path.append(parents[path[-1]])
            path.reverse()
            return distance, path
        for neighbor, edge_distance in adjacency.get(node, []):
            candidate = distance + edge_distance
            if candidate < distances.get(neighbor, float("inf")):
                distances[neighbor] = candidate
                parents[neighbor] = node
                heapq.heappush(queue, (candidate, neighbor))
    raise RuntimeError(f"No legal warehouse path from {start!r} to {goal!r}")


def _location_payload(row: Record) -> dict[str, Any]:
    return {
        "record_type": str(row.get("record_type") or ""),
        "odoo_location_id": int(row["odoo_location_id"]) if str(row.get("odoo_location_id") or "").strip() else None,
        "complete_name": str(row.get("complete_name") or ""),
        "barcode": str(row.get("barcode") or ""),
        "logical_area": str(row.get("logical_area") or ""),
        "currently_holds_stock": _bool(row.get("currently_holds_stock")),
        "current_on_hand_units": _float(row.get("current_on_hand_units") or 0),
        "current_reserved_units": _float(row.get("current_reserved_units") or 0),
        "xyz": {
            "x": _float(row.get("x")),
            "y": _float(row.get("y")),
            "z": _float(row.get("z")),
        },
        "graph_node_id": str(row.get("graph_node_id") or "").strip(),
        "measurement_status": str(row.get("measurement_status") or ""),
        "accessible_by": str(row.get("accessible_by") or ""),
        "secure_zone": _bool(row.get("secure_zone")),
        "flight_critical_allowed": _bool(row.get("flight_critical_allowed")),
        "capacity_units": _float(row.get("capacity_units")) if str(row.get("capacity_units") or "").strip() else None,
        "capacity_weight_lb": _float(row.get("capacity_weight_lb")) if str(row.get("capacity_weight_lb") or "").strip() else None,
        "notes": str(row.get("notes") or ""),
    }


def build_canonical_geometry(
    locations: Iterable[Record],
    nodes: Iterable[Record],
    edges: Iterable[Record],
    *,
    anchor_location_name: str | None = None,
) -> dict[str, Any]:
    location_rows = list(locations)
    node_rows = list(nodes)
    edge_rows = list(edges)
    adjacency = build_adjacency(node_rows, edge_rows)

    nodes_by_id: dict[str, dict[str, Any]] = {}
    for row in node_rows:
        node_id = str(row.get("graph_node_id") or "").strip()
        nodes_by_id[node_id] = {
            "graph_node_id": node_id,
            "node_type": str(row.get("node_type") or ""),
            "serves_location": str(row.get("serves_location") or ""),
            "xyz": {
                "x": _float(row.get("x")),
                "y": _float(row.get("y")),
                "z": _float(row.get("z")),
            },
            "measurement_status": str(row.get("measurement_status") or ""),
            "accessible_by": str(row.get("accessible_by") or ""),
            "access_restriction": str(row.get("access_restriction") or ""),
            "notes": str(row.get("notes") or ""),
        }

    canonical_locations: list[dict[str, Any]] = []
    for raw in location_rows:
        row = _location_payload(raw)
        node = nodes_by_id.get(row["graph_node_id"])
        if node is None:
            raise ValueError(
                f"Location {row['complete_name']!r} references missing graph node {row['graph_node_id']!r}"
            )
        dx = row["xyz"]["x"] - node["xyz"]["x"]
        dy = row["xyz"]["y"] - node["xyz"]["y"]
        dz = row["xyz"]["z"] - node["xyz"]["z"]
        row["access_geometry"] = {
            "horizontal_offset_ft": round(math.hypot(dx, dy), 3),
            "vertical_reach_ft": round(abs(dz), 3),
            "three_dimensional_offset_ft": round(math.sqrt(dx * dx + dy * dy + dz * dz), 3),
            "note": "Offsets are physical access/ergonomic descriptors and are not automatically added to graph travel distance.",
        }
        canonical_locations.append(row)

    flow_endpoints = [row for row in canonical_locations if row["record_type"] == "flow_endpoint"]
    if not flow_endpoints:
        raise ValueError("Canonical geometry requires at least one flow_endpoint")

    if anchor_location_name:
        anchor = next(
            (row for row in flow_endpoints if row["complete_name"] == anchor_location_name),
            None,
        )
        if anchor is None:
            raise ValueError(f"Requested anchor flow endpoint {anchor_location_name!r} not found")
    else:
        anchor = sorted(flow_endpoints, key=lambda row: row["complete_name"])[0]

    distance_rows: list[dict[str, Any]] = []
    for row in canonical_locations:
        if row["record_type"] != "storage_bin":
            continue
        distance, path = shortest_path(adjacency, anchor["graph_node_id"], row["graph_node_id"])
        distance_rows.append(
            {
                "odoo_location_id": row["odoo_location_id"],
                "complete_name": row["complete_name"],
                "graph_node_id": row["graph_node_id"],
                "anchor_location": anchor["complete_name"],
                "anchor_graph_node_id": anchor["graph_node_id"],
                "graph_distance_to_access_ft": round(distance, 3),
                "horizontal_access_offset_ft": row["access_geometry"]["horizontal_offset_ft"],
                "vertical_reach_ft": row["access_geometry"]["vertical_reach_ft"],
                "path_nodes": path,
            }
        )
    distance_rows.sort(
        key=lambda row: (
            row["graph_distance_to_access_ft"],
            row["complete_name"],
        )
    )

    unique_location_nodes = {row["graph_node_id"] for row in canonical_locations}
    measurement_statuses = sorted(
        {str(row.get("measurement_status") or "") for row in canonical_locations}
        | {str(row.get("measurement_status") or "") for row in nodes_by_id.values()}
    )

    return {
        "schema_version": "awia-warehouse-geometry-v1",
        "classification": "validated_geometry_import",
        "units": "ft",
        "anchor": {
            "complete_name": anchor["complete_name"],
            "graph_node_id": anchor["graph_node_id"],
            "xyz": anchor["xyz"],
        },
        "summary": {
            "locations": len(canonical_locations),
            "storage_bins": sum(1 for row in canonical_locations if row["record_type"] == "storage_bin"),
            "flow_endpoints": len(flow_endpoints),
            "graph_nodes": len(nodes_by_id),
            "path_edges": len(edge_rows),
            "unique_location_access_nodes": len(unique_location_nodes),
            "measurement_statuses": measurement_statuses,
        },
        "locations": canonical_locations,
        "graph": {
            "nodes": list(nodes_by_id.values()),
            "edges": [
                {
                    "from_graph_node_id": str(row.get("from_graph_node_id") or ""),
                    "to_graph_node_id": str(row.get("to_graph_node_id") or ""),
                    "distance_ft": _float(row.get("distance_ft")),
                    "edge_type": str(row.get("edge_type") or ""),
                    "bidirectional": _bool(row.get("bidirectional")),
                    "access_restriction": str(row.get("access_restriction") or ""),
                    "notes": str(row.get("notes") or ""),
                }
                for row in edge_rows
            ],
        },
        "anchor_distances": distance_rows,
        "guardrails": {
            "graph_distance_is_to_access_node_not_bin_center": True,
            "vertical_reach_not_counted_as_walking_distance": True,
            "field_measurement_quality_not_proven_by_schema_validation": True,
        },
    }
