"""Deterministic human-like picker simulation for AWIA sandbox kitting.

This module does not claim to measure a real person.  It converts the existing
mock warehouse path graph plus native Odoo reservation lines into a repeatable
virtual-picker route and time budget.  The output is suitable for synthetic
baseline experiments and algorithm comparisons while remaining explicitly
classified as simulated data.
"""

from __future__ import annotations

import csv
import heapq
import random
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BIN_TAIL = re.compile(r"([A-H]-\d{2}-L[12]-B[AB])$")


@dataclass(frozen=True)
class PickerAssumptions:
    walking_speed_ft_s: float = 3.5
    base_search_seconds: float = 6.0
    level_two_seconds: float = 4.0
    controlled_zone_seconds: float = 3.0
    base_handling_seconds: float = 4.0
    handling_seconds_per_unit: float = 0.8
    base_scan_seconds: float = 2.5
    lot_tracking_seconds: float = 1.5
    serial_tracking_seconds: float = 2.5
    flight_critical_seconds: float = 1.5
    stage_seconds: float = 12.0
    deterministic_jitter_seconds: float = 2.0

    def validate(self) -> None:
        numeric = self.__dict__
        if self.walking_speed_ft_s <= 0:
            raise ValueError("walking_speed_ft_s must be positive")
        if any(float(value) < 0 for key, value in numeric.items() if key != "walking_speed_ft_s"):
            raise ValueError("virtual picker timing assumptions cannot be negative")


@dataclass(frozen=True)
class Geometry:
    locations_by_tail: dict[str, dict[str, Any]]
    adjacency: dict[str, list[tuple[str, float]]]
    kitting_node: str = "NODE_KITTING"


def _float(value: Any) -> float:
    return float(value) if value not in (None, "") else 0.0


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int) and not isinstance(value[0], bool):
        return value[0]
    return None


def _label(value: Any) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    if isinstance(value, str):
        return value
    return None


def location_tail(value: str | None) -> str | None:
    match = BIN_TAIL.search(str(value or ""))
    return match.group(1) if match else None


def load_geometry(data_dir: Path) -> Geometry:
    warehouse_dir = data_dir / "mock_warehouse"
    locations_path = warehouse_dir / "locations.csv"
    edges_path = warehouse_dir / "edges.csv"
    if not locations_path.exists() or not edges_path.exists():
        raise FileNotFoundError(
            f"Missing generated AWIA geometry under {warehouse_dir}. Run scripts/generate_simulation_sandbox.py first."
        )

    with locations_path.open(newline="", encoding="utf-8") as handle:
        location_rows = list(csv.DictReader(handle))
    locations: dict[str, dict[str, Any]] = {}
    for row in location_rows:
        tail = location_tail(row.get("odoo_complete_name"))
        if tail:
            locations[tail] = row

    adjacency: dict[str, list[tuple[str, float]]] = {}
    with edges_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            a = str(row["from_node"])
            b = str(row["to_node"])
            distance = _float(row["distance_ft"])
            adjacency.setdefault(a, []).append((b, distance))
            if str(row.get("bidirectional") or "").strip().lower() in {"true", "1", "yes"}:
                adjacency.setdefault(b, []).append((a, distance))
    if "NODE_KITTING" not in adjacency:
        raise RuntimeError("Mock graph does not contain connected NODE_KITTING.")
    return Geometry(locations_by_tail=locations, adjacency=adjacency)


def shortest_path(
    adjacency: dict[str, list[tuple[str, float]]],
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
    raise RuntimeError(f"No legal warehouse path from {start} to {goal}.")


def enrich_reservation_lines(
    move_lines: list[dict[str, Any]],
    product_by_id: dict[int, dict[str, Any]],
    geometry: Geometry,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in move_lines:
        quantity = _float(line.get("quantity"))
        if quantity <= 0:
            continue
        product_id = _int(line.get("product_id"))
        location_name = _label(line.get("location_id"))
        tail = location_tail(location_name)
        if product_id is None or not tail:
            raise RuntimeError(f"Move line {line.get('id')} is missing product or mappable source location.")
        geom = geometry.locations_by_tail.get(tail)
        if geom is None:
            raise RuntimeError(f"Source location {location_name!r} does not exist in mock-v1 geometry.")
        product = product_by_id.get(product_id, {})
        rows.append(
            {
                "move_line_id": _int(line.get("id")),
                "product_id": product_id,
                "product": _label(line.get("product_id")),
                "product_code": product.get("default_code") or None,
                "tracking": str(line.get("tracking") or product.get("tracking") or "none"),
                "flight_critical": bool(product.get("x_is_flight_critical", False)),
                "quantity": quantity,
                "lot_id": _int(line.get("lot_id")),
                "lot": _label(line.get("lot_id")),
                "source_location": location_name,
                "location_tail": tail,
                "zone": str(geom.get("zone") or ""),
                "level": int(geom.get("level") or 1),
                "graph_node_id": str(geom["graph_node_id"]),
            }
        )
    return rows


def _stop_sort_key(row: dict[str, Any]) -> tuple[str, int]:
    return (str(row.get("location_tail") or ""), int(row.get("move_line_id") or 0))


def build_virtual_picker_plan(
    reservations: list[dict[str, Any]],
    geometry: Geometry,
    *,
    picking_id: int,
    assumptions: PickerAssumptions | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    assumptions = assumptions or PickerAssumptions()
    assumptions.validate()
    rng = random.Random(seed + picking_id)

    remaining = [dict(row) for row in reservations]
    remaining.sort(key=_stop_sort_key)
    current_node = geometry.kitting_node
    current_label = "WH/Pre-Production (proxy: ST_KITTING)"
    elapsed = 0.0
    total_distance = 0.0
    total_travel = 0.0
    total_search = 0.0
    total_handling = 0.0
    total_scan = 0.0
    stops: list[dict[str, Any]] = []

    while remaining:
        choices: list[tuple[float, tuple[str, int], int, list[str]]] = []
        for index, row in enumerate(remaining):
            distance, path = shortest_path(geometry.adjacency, current_node, str(row["graph_node_id"]))
            choices.append((distance, _stop_sort_key(row), index, path))
        distance, _, index, path = min(choices, key=lambda item: (item[0], item[1]))
        row = remaining.pop(index)

        travel_seconds = distance / assumptions.walking_speed_ft_s
        elapsed += travel_seconds
        total_distance += distance
        total_travel += travel_seconds
        arrival_elapsed = elapsed

        jitter = rng.uniform(0.0, assumptions.deterministic_jitter_seconds)
        search_seconds = assumptions.base_search_seconds + jitter
        if int(row.get("level") or 1) >= 2:
            search_seconds += assumptions.level_two_seconds
        if str(row.get("zone") or "").upper() == "CONTROLLED":
            search_seconds += assumptions.controlled_zone_seconds

        handling_seconds = (
            assumptions.base_handling_seconds
            + assumptions.handling_seconds_per_unit * min(float(row["quantity"]), 10.0)
            + rng.uniform(0.0, assumptions.deterministic_jitter_seconds)
        )
        scan_seconds = assumptions.base_scan_seconds + rng.uniform(
            0.0, assumptions.deterministic_jitter_seconds / 2.0
        )
        tracking = str(row.get("tracking") or "none").lower()
        if tracking == "lot":
            scan_seconds += assumptions.lot_tracking_seconds
        elif tracking == "serial":
            scan_seconds += assumptions.serial_tracking_seconds
        if bool(row.get("flight_critical")):
            scan_seconds += assumptions.flight_critical_seconds

        elapsed += search_seconds + handling_seconds + scan_seconds
        total_search += search_seconds
        total_handling += handling_seconds
        total_scan += scan_seconds

        stops.append(
            {
                **row,
                "sequence": len(stops) + 1,
                "from_location": current_label,
                "distance_ft": round(distance, 3),
                "path_nodes": path,
                "travel_seconds": round(travel_seconds, 3),
                "arrival_elapsed_seconds": round(arrival_elapsed, 3),
                "search_seconds": round(search_seconds, 3),
                "handling_seconds": round(handling_seconds, 3),
                "scan_seconds": round(scan_seconds, 3),
                "scan_elapsed_seconds": round(elapsed, 3),
            }
        )
        current_node = str(row["graph_node_id"])
        current_label = str(row["source_location"])

    return_distance, return_path = shortest_path(
        geometry.adjacency, current_node, geometry.kitting_node
    )
    return_travel = return_distance / assumptions.walking_speed_ft_s
    elapsed += return_travel
    total_distance += return_distance
    total_travel += return_travel
    stage_arrival_elapsed = elapsed
    elapsed += assumptions.stage_seconds

    return {
        "classification": "simulated_human_like",
        "simulator_version": "virtual-picker-nearest-neighbor-v1",
        "picking_id": picking_id,
        "assumptions": assumptions.__dict__,
        "routing": {
            "method": "nearest-neighbor over legal shortest-path graph distances",
            "start_node": geometry.kitting_node,
            "end_node": geometry.kitting_node,
            "preproduction_proxy": "ST_KITTING / NODE_KITTING",
        },
        "stops": stops,
        "return_to_stage": {
            "from_location": current_label,
            "distance_ft": round(return_distance, 3),
            "path_nodes": return_path,
            "travel_seconds": round(return_travel, 3),
            "arrival_elapsed_seconds": round(stage_arrival_elapsed, 3),
            "stage_seconds": assumptions.stage_seconds,
            "stage_complete_elapsed_seconds": round(elapsed, 3),
        },
        "summary": {
            "pick_lines": len(stops),
            "unique_source_locations": len({row["source_location"] for row in stops}),
            "total_distance_ft": round(total_distance, 3),
            "travel_minutes": round(total_travel / 60.0, 2),
            "search_minutes": round(total_search / 60.0, 2),
            "handling_minutes": round(total_handling / 60.0, 2),
            "scan_minutes": round(total_scan / 60.0, 2),
            "stage_minutes": round(assumptions.stage_seconds / 60.0, 2),
            "simulated_start_to_stage_minutes": round(elapsed / 60.0, 2),
        },
    }
