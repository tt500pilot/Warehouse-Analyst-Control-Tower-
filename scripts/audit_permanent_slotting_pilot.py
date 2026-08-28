from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.relocation_planner import _build_live_state
from app.services.slotting_feasibility import (
    _live_quants_for_locations,
    _m2o_id,
    _product_index,
    build_live_location_index,
)
from app.services.slotting_optimizer import load_slotting_product_metadata, optimize_slotting_layout
from app.services.virtual_picker import PickerAssumptions
from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report
from scripts.optimize_holdout_slotting import (
    DEFAULT_DATA_DIR,
    DEFAULT_HOLDOUTS,
    DEFAULT_TRAINING,
    _validate_experiment,
    build_reservations,
)
from scripts.rank_permanent_slotting_candidates import _distance_between, _single_training_source


def _traceability_state(
    client: OdooWarehouseClient,
    *,
    product_code: str,
    source_tail: str,
) -> dict[str, Any]:
    locations = build_live_location_index(client)
    source_location = locations.get(source_tail)
    if not source_location or not isinstance(source_location.get("id"), int):
        raise RuntimeError(f"Source {source_tail} is not mapped to a live Odoo internal location.")
    source_location_id = int(source_location["id"])

    product_rows = client.search_read(
        "product.product",
        domain=[["default_code", "=", product_code]],
        fields=[
            field
            for field in ("id", "default_code", "name", "tracking")
            if field in set(client.available_fields("product.product"))
        ],
        limit=2,
        order="id asc",
    )
    if len(product_rows) != 1 or not isinstance(product_rows[0].get("id"), int):
        raise RuntimeError(
            f"Expected exactly one live Odoo product for {product_code}; found {len(product_rows)}."
        )
    product_id = int(product_rows[0]["id"])
    product = _product_index(client, {product_id}).get(product_id, product_rows[0])
    tracking = str(product.get("tracking") or "none")

    quants = [
        row
        for row in _live_quants_for_locations(client, [source_location_id])
        if _m2o_id(row.get("product_id")) == product_id
    ]
    traced_rows = [row for row in quants if _m2o_id(row.get("lot_id")) is not None]
    anonymous_rows = [row for row in quants if _m2o_id(row.get("lot_id")) is None]

    def qty(rows: list[dict[str, Any]], field: str) -> float:
        return round(sum(float(row.get(field) or 0.0) for row in rows), 3)

    lot_ids = sorted(
        {
            lot_id
            for row in traced_rows
            for lot_id in [_m2o_id(row.get("lot_id"))]
            if lot_id is not None
        }
    )
    lot_names: dict[int, str] = {}
    if lot_ids:
        lot_rows = client.search_read(
            "stock.lot",
            domain=[["id", "in", lot_ids]],
            fields=[
                field
                for field in ("id", "name", "product_id")
                if field in set(client.available_fields("stock.lot"))
            ],
            limit=10000,
            order="id asc",
        )
        lot_names = {
            int(row["id"]): str(row.get("name") or "")
            for row in lot_rows
            if isinstance(row.get("id"), int)
        }

    traced_details = []
    serial_quantity_violations = []
    for row in traced_rows:
        lot_id = _m2o_id(row.get("lot_id"))
        quantity = float(row.get("quantity") or 0.0)
        detail = {
            "quant_id": row.get("id"),
            "lot_or_serial_id": lot_id,
            "lot_or_serial_name": lot_names.get(lot_id or -1),
            "quantity": round(quantity, 3),
            "reserved_quantity": round(float(row.get("reserved_quantity") or 0.0), 3),
        }
        traced_details.append(detail)
        if tracking == "serial" and quantity > 1.000001:
            serial_quantity_violations.append(detail)

    anonymous_quantity = qty(anonymous_rows, "quantity")
    fully_traceable = (
        anonymous_quantity <= 1e-9
        and not serial_quantity_violations
        and (tracking != "serial" or qty(traced_rows, "quantity") <= len(lot_ids) + 1e-9)
    )

    return {
        "product_id": product_id,
        "tracking": tracking,
        "source_location_id": source_location_id,
        "source_location": source_tail,
        "total_quantity": qty(quants, "quantity"),
        "total_reserved_quantity": qty(quants, "reserved_quantity"),
        "traced_quantity": qty(traced_rows, "quantity"),
        "traced_reserved_quantity": qty(traced_rows, "reserved_quantity"),
        "anonymous_quantity": anonymous_quantity,
        "anonymous_reserved_quantity": qty(anonymous_rows, "reserved_quantity"),
        "lot_or_serial_ids": lot_ids,
        "lot_or_serial_count": len(lot_ids),
        "traced_quant_rows": traced_details,
        "anonymous_quant_rows": [
            {
                "quant_id": row.get("id"),
                "quantity": round(float(row.get("quantity") or 0.0), 3),
                "reserved_quantity": round(float(row.get("reserved_quantity") or 0.0), 3),
            }
            for row in anonymous_rows
        ],
        "serial_quantity_violations": serial_quantity_violations,
        "fully_traceable_for_full_stock_relocation": fully_traceable,
        "note": (
            "The existing AWIA tracking migration was demand-scoped. Anonymous remainder is expected "
            "until a full-stock relocation pilot explicitly serializes or lot-identifies the stock."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only readiness audit for a permanent slotting pilot.")
    parser.add_argument("--product-code", default="REGULATOR-552")
    args = parser.parse_args()

    client = OdooWarehouseClient.from_env()
    readiness = build_readiness_report(client)
    training_ids = tuple(DEFAULT_TRAINING)
    comparison_ids = tuple(DEFAULT_HOLDOUTS)
    _validate_experiment(readiness, training_ids, comparison_ids)

    data_dir = Path(DEFAULT_DATA_DIR)
    training, geometry = build_reservations(client, training_ids, data_dir)
    comparison, _ = build_reservations(client, comparison_ids, data_dir)
    metadata = load_slotting_product_metadata(data_dir)
    assumptions = PickerAssumptions()

    slotting = optimize_slotting_layout(
        training,
        comparison,
        geometry,
        metadata,
        assumptions=assumptions,
        seed=42,
    )
    recommendation = next(
        (row for row in slotting["recommendations"] if row.get("product_code") == args.product_code),
        None,
    )
    if recommendation is None:
        raise RuntimeError(f"No slotting recommendation found for {args.product_code}.")

    bins, product_positions, capacity_anomalies = _build_live_state(client, geometry, metadata)
    source_by_code = _single_training_source(training)
    source = source_by_code.get(args.product_code)
    target = str(recommendation["candidate_location"])
    if not source:
        raise RuntimeError(f"No single deterministic training source found for {args.product_code}.")

    positions = product_positions.get(args.product_code, [])
    live_quantity = sum(float(row["quantity"]) for row in positions)
    live_reserved = sum(float(row["reserved_quantity"]) for row in positions)
    live_weight = sum(float(row["weight_lb"]) for row in positions)
    traceability = _traceability_state(
        client,
        product_code=args.product_code,
        source_tail=source,
    )

    target_bin = bins.get(target)
    if target_bin is None:
        raise RuntimeError(f"Target {target} is not mapped to a live Odoo internal location.")

    other_occupants = sorted(
        occupant for occupant in target_bin.get("occupants", {}) if occupant != args.product_code
    )
    projected_units = float(target_bin["total_units"]) + live_quantity
    projected_weight = float(target_bin["total_weight_lb"]) + live_weight
    unit_capacity = float(target_bin["capacity_units"])
    weight_capacity = float(target_bin["capacity_weight_lb"])
    unit_capacity_pass = unit_capacity > 0 and projected_units <= unit_capacity
    weight_capacity_pass = weight_capacity > 0 and projected_weight <= weight_capacity

    geom = geometry.locations_by_tail[target]
    flight_critical = bool(metadata.get(args.product_code, {}).get("flight_critical"))
    secure_required = bool(metadata.get(args.product_code, {}).get("secure_required"))
    policy_pass = True
    policy_blockers: list[str] = []
    if flight_critical and not bool(geom.get("flight_critical_allowed")):
        policy_pass = False
        policy_blockers.append("flight_critical_not_allowed")
    if flight_critical and str(geom.get("zone") or "").upper() == "BULK":
        policy_pass = False
        policy_blockers.append("flight_critical_bulk_prohibited")
    if secure_required and not bool(geom.get("secure")):
        policy_pass = False
        policy_blockers.append("secure_storage_required")

    blockers: list[str] = []
    if live_reserved > 0:
        blockers.append("native_odoo_reservation_release_or_reassignment_required")
    if not traceability["fully_traceable_for_full_stock_relocation"]:
        blockers.append("tracked_full_stock_not_fully_lot_or_serial_identified")
    if other_occupants:
        blockers.append("target_occupied_requires_displacement")
    if capacity_anomalies:
        blockers.append("sandbox_capacity_fixture_not_trusted")
    if not policy_pass:
        blockers.extend(policy_blockers)
    blockers.append("material_handling_equipment_assumptions_not_defined")
    blockers.append("fresh_blind_validation_set_not_created")
    blockers.append("human_approval_required")

    required_before_move = [
        "replace or calibrate target physical capacity using real dimensions/load rating",
        "define actual material-handling method for this load",
        "create a fresh blind validation set before changing the layout",
        "plan native Odoo reservation release/reassignment without losing lot/serial traceability",
        "obtain human approval",
    ]
    if not traceability["fully_traceable_for_full_stock_relocation"]:
        required_before_move.insert(
            0,
            "fully lot/serial-identify the stock that will be physically relocated; do not treat anonymous tracked stock as move-ready",
        )

    result = {
        "mode": "read_only_permanent_slotting_pilot_readiness",
        "odoo_mutated": False,
        "product_code": args.product_code,
        "source_location": source,
        "target_location": target,
        "target_zone": geom.get("zone"),
        "relocation_graph_distance_ft": round(_distance_between(geometry, source, target), 3),
        "live_inventory": {
            "quantity": round(live_quantity, 3),
            "reserved_quantity": round(live_reserved, 3),
            "estimated_weight_lb": round(live_weight, 3),
            "positions": positions,
        },
        "traceability": traceability,
        "target_state": {
            "currently_empty": float(target_bin["total_units"]) <= 0,
            "other_occupants": other_occupants,
            "current_units": target_bin["total_units"],
            "current_weight_lb": target_bin["total_weight_lb"],
        },
        "placeholder_capacity_check": {
            "trusted_for_execution": False,
            "capacity_units": unit_capacity,
            "projected_units_after_move": round(projected_units, 3),
            "unit_capacity_pass_under_placeholder": unit_capacity_pass,
            "unit_margin": round(unit_capacity - projected_units, 3),
            "capacity_weight_lb": weight_capacity,
            "projected_weight_lb_after_move": round(projected_weight, 3),
            "weight_capacity_pass_under_placeholder": weight_capacity_pass,
            "weight_margin_lb": round(weight_capacity - projected_weight, 3),
            "warehouse_current_capacity_anomalies": len(capacity_anomalies),
            "note": (
                "The synthetic capacity fixture is not a validated physical limit. A local pass is informative only; "
                "real bin dimensions/load rating are required before execution."
            ),
        },
        "policy_check": {
            "flight_critical": flight_critical,
            "secure_required": secure_required,
            "target_flight_critical_allowed": bool(geom.get("flight_critical_allowed")),
            "target_secure": bool(geom.get("secure")),
            "pass": policy_pass,
            "blockers": policy_blockers,
        },
        "pilot_gate": {
            "ready_for_physical_move": False,
            "blockers": blockers,
            "required_before_move": required_before_move,
        },
        "recommended_experiment": {
            "scope": "single-SKU permanent relocation pilot",
            "candidate": args.product_code,
            "why": "best training-only payback with empty target and no incumbent displacement",
            "do_not_execute_yet": True,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
