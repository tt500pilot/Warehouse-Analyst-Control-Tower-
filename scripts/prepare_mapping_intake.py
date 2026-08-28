from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.mapping_prioritizer import analyze_mapping_priorities, derive_logical_area
from odoo_client import OdooWarehouseClient


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


def _fields(client: OdooWarehouseClient, model: str, wanted: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in wanted if field in available]


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, int) and not isinstance(first, bool) and first > 0:
            return int(first)
    return None


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _location_name(row: dict[str, Any]) -> str:
    for key in ("complete_name", "display_name", "name"):
        if row.get(key):
            return str(row[key])
    return ""


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return text or "mapping-scope"


def _node_id(record_type: str, location: dict[str, Any]) -> str:
    location_id = _m2o_id(location.get("id"))
    if location_id is not None:
        return f"NODE_{record_type.upper()}_{location_id}"
    return f"NODE_{record_type.upper()}_{_slug(_location_name(location)).upper().replace('-', '_')}"


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _is_stock_descendant(location: dict[str, Any]) -> bool:
    if str(location.get("usage") or "").lower() != "internal":
        return False
    parts = [part.strip().lower() for part in _location_name(location).split("/") if part.strip()]
    return "stock" in parts


def _quant_totals(quants: list[dict[str, Any]]) -> dict[int, dict[str, float]]:
    result: dict[int, dict[str, float]] = defaultdict(lambda: {"quantity": 0.0, "reserved": 0.0})
    for quant in quants:
        location_id = _m2o_id(quant.get("location_id"))
        if location_id is None:
            continue
        result[location_id]["quantity"] += _number(quant.get("quantity"))
        result[location_id]["reserved"] += max(_number(quant.get("reserved_quantity")), 0.0)
    return result


def _location_row(
    location: dict[str, Any],
    *,
    record_type: str,
    logical_area: str,
    totals: dict[int, dict[str, float]],
) -> dict[str, Any]:
    location_id = _m2o_id(location.get("id"))
    current = totals.get(location_id or -1, {"quantity": 0.0, "reserved": 0.0})
    quantity = float(current["quantity"])
    return {
        "record_type": record_type,
        "odoo_location_id": location_id or "",
        "complete_name": _location_name(location),
        "barcode": location.get("barcode") or "",
        "logical_area": logical_area,
        "currently_holds_stock": quantity != 0,
        "current_on_hand_units": round(quantity, 3),
        "current_reserved_units": round(float(current["reserved"]), 3),
        "x": "",
        "y": "",
        "z": "",
        "graph_node_id": _node_id(record_type, location),
        "measurement_status": "NOT_MEASURED",
        "accessible_by": "",
        "one_way_or_access_notes": "",
        "secure_zone": "",
        "flight_critical_allowed": "",
        "capacity_units": "",
        "capacity_weight_lb": "",
        "notes": "",
    }


def _graph_node_row(location_row: dict[str, Any]) -> dict[str, Any]:
    record_type = str(location_row["record_type"])
    return {
        "graph_node_id": location_row["graph_node_id"],
        "node_type": "pick_access" if record_type == "storage_bin" else "flow_endpoint",
        "serves_location": location_row["complete_name"],
        "x": "",
        "y": "",
        "z": "",
        "measurement_status": "NOT_MEASURED",
        "accessible_by": "",
        "access_restriction": "",
        "notes": "Add cross-aisle/turn/gate nodes as new rows when required by the real travel path.",
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a local XYZ/path mapping intake package for the highest-priority "
            "warehouse area identified from read-only Odoo data."
        )
    )
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument(
        "--area",
        default="",
        help="Optional exact logical area from scan output; default uses the recommended first area.",
    )
    parser.add_argument(
        "--output-dir",
        default="data/mapping_intake",
        help="Local directory for generated CSV/JSON intake files.",
    )
    args = parser.parse_args()
    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero")
    if args.source_limit <= 0:
        raise ValueError("--source-limit must be greater than zero")

    client = OdooWarehouseClient.from_env()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d %H:%M:%S")

    product_fields = _fields(
        client,
        "product.product",
        ("id", "default_code", "name", "standard_price", "tracking", "x_is_flight_critical"),
    )
    quant_fields = _fields(
        client,
        "stock.quant",
        ("id", "product_id", "location_id", "quantity", "reserved_quantity"),
    )
    move_fields = _fields(
        client,
        "stock.move.line",
        (
            "id",
            "product_id",
            "location_id",
            "location_dest_id",
            "quantity",
            "qty_done",
            "date",
            "write_date",
            "state",
        ),
    )
    location_fields = _fields(
        client,
        "stock.location",
        ("id", "complete_name", "display_name", "name", "usage", "barcode"),
    )
    bom_line_fields = _fields(
        client,
        "mrp.bom.line",
        ("id", "bom_id", "product_id", "product_qty", "active"),
    )

    products = client.search_read(
        "product.product",
        domain=[["active", "=", True]],
        fields=product_fields,
        limit=args.source_limit,
        order="id asc",
    )
    quants = client.search_read(
        "stock.quant",
        domain=[["quantity", "!=", 0]],
        fields=quant_fields,
        limit=args.source_limit,
        order="location_id asc, product_id asc, id asc",
    )
    moves = client.search_read(
        "stock.move.line",
        domain=[["date", ">=", cutoff]],
        fields=move_fields,
        limit=args.source_limit,
        order="date asc, id asc",
    )
    locations = client.search_read(
        "stock.location",
        domain=[["usage", "=", "internal"]],
        fields=location_fields,
        limit=args.source_limit,
        order="id asc",
    )
    bom_lines = client.search_read(
        "mrp.bom.line",
        domain=[],
        fields=bom_line_fields,
        limit=args.source_limit,
        order="id asc",
    )

    scan = analyze_mapping_priorities(
        products,
        quants,
        moves,
        locations,
        bom_lines=bom_lines,
        as_of=now,
        lookback_days=args.lookback_days,
    )
    selected_area = args.area.strip() or str(scan["summary"].get("recommended_first_area") or "")
    if not selected_area:
        raise RuntimeError("Mapping priority scan did not produce a recommended area.")

    selected_summary = next(
        (row for row in scan["areas"] if row["logical_area"] == selected_area),
        None,
    )
    if selected_summary is None:
        available = ", ".join(row["logical_area"] for row in scan["areas"])
        raise ValueError(f"Unknown --area {selected_area!r}. Available areas: {available}")

    totals = _quant_totals(quants)
    bin_locations: list[dict[str, Any]] = []
    for location in locations:
        if not _is_stock_descendant(location):
            continue
        area, _strategy = derive_logical_area(_location_name(location))
        if area == selected_area:
            bin_locations.append(location)
    bin_locations.sort(key=lambda row: (_location_name(row), _m2o_id(row.get("id")) or 0))

    flow_names = [
        str(row["location"])
        for row in selected_summary.get("top_flow_counterparts", [])
        if row.get("location")
    ]
    flow_locations: list[dict[str, Any]] = []
    for name in flow_names:
        exact = [location for location in locations if _location_name(location) == name]
        if exact:
            flow_locations.extend(exact)
        else:
            flow_locations.append(
                {"id": "", "complete_name": name, "usage": "", "barcode": ""}
            )

    rows = [
        _location_row(location, record_type="storage_bin", logical_area=selected_area, totals=totals)
        for location in bin_locations
    ]
    rows.extend(
        _location_row(location, record_type="flow_endpoint", logical_area="FLOW_ENDPOINT", totals=totals)
        for location in flow_locations
    )
    node_rows = [_graph_node_row(row) for row in rows]

    slug = _slug(selected_area)
    output_dir = Path(args.output_dir)
    locations_path = output_dir / f"{slug}-locations.csv"
    nodes_path = output_dir / f"{slug}-graph-nodes.csv"
    edges_path = output_dir / f"{slug}-path-edges.csv"
    manifest_path = output_dir / f"{slug}-manifest.json"

    _write_csv(locations_path, LOCATION_COLUMNS, rows)
    _write_csv(nodes_path, NODE_COLUMNS, node_rows)
    _write_csv(edges_path, EDGE_COLUMNS, [])

    active_bins = sum(1 for row in rows if row["record_type"] == "storage_bin" and row["currently_holds_stock"])
    empty_bins = sum(1 for row in rows if row["record_type"] == "storage_bin" and not row["currently_holds_stock"])
    manifest = {
        "generated_at": now.isoformat(),
        "mode": "read_only_odoo_to_local_mapping_intake",
        "odoo_mutated": False,
        "database": client.database,
        "selected_area": selected_area,
        "selection_source": "explicit --area" if args.area.strip() else "mapping prioritizer recommendation",
        "opportunity_score": selected_summary["opportunity_score"],
        "why_selected": selected_summary["why_map_this_area"],
        "operational_flow_counterparts": selected_summary["top_flow_counterparts"],
        "scope_counts": {
            "all_storage_bins_in_selected_area": len(bin_locations),
            "currently_stocked_bins": active_bins,
            "currently_empty_bins": empty_bins,
            "flow_endpoints": len(flow_locations),
            "initial_graph_nodes": len(node_rows),
        },
        "files": {
            "locations_csv": str(locations_path),
            "graph_nodes_csv": str(nodes_path),
            "path_edges_csv": str(edges_path),
        },
        "measurement_instructions": [
            "Choose and document one permanent site datum at (0,0,0).",
            "In locations.csv, enter XYZ for every storage bin and operational endpoint, including empty bins.",
            "In graph-nodes.csv, measure the legal travel access point for every bin/endpoint; these coordinates may differ from the physical bin XYZ.",
            "Add graph-node rows for cross-aisles, turns, gates, barriers, elevators/lifts, or other path-control points required by the real layout.",
            "Populate path-edges.csv only with legal pedestrian/cart segments between graph nodes; do not use straight-line shortcuts through racks or barriers.",
            "Record access restrictions, one-way travel, secure-zone status, and real bin capacity/load rating before optimization.",
        ],
        "geometry_model": {
            "location_xyz": "physical bin/endpoint position",
            "graph_node_xyz": "legal travel access point used for route distance",
            "path_edges": "legal traversable segments joining graph nodes",
            "important": "bin XYZ and graph-node XYZ are intentionally separate concepts",
        },
        "guardrails": {
            "xyz_not_inferred_from_odoo": True,
            "empty_bins_included_for_candidate_slotting": True,
            "inventory_adjustment_moves_excluded_from_physical_priority_score": True,
            "travel_topology_not_inferred_from_location_names": True,
            "no_odoo_writes": True,
            "human_review_required_before_geometry_is_accepted": True,
        },
        "source_snapshot": {
            "products": len(products),
            "nonzero_quants": len(quants),
            "move_lines": len(moves),
            "internal_locations": len(locations),
            "bom_lines": len(bom_lines),
            "lookback_days": args.lookback_days,
            "source_limit_per_model": args.source_limit,
            "truncated_possible": any(
                len(items) >= args.source_limit
                for items in (products, quants, moves, locations, bom_lines)
            ),
        },
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
