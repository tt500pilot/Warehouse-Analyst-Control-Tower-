from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.relocation_planner import _build_live_state
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
    lot_ids = sorted({lot for row in positions for lot in row.get("lot_ids", [])})

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
    if other_occupants:
        blockers.append("target_occupied_requires_displacement")
    if capacity_anomalies:
        blockers.append("sandbox_capacity_fixture_not_trusted")
    if not policy_pass:
        blockers.extend(policy_blockers)
    blockers.append("material_handling_equipment_assumptions_not_defined")
    blockers.append("fresh_blind_validation_set_not_created")
    blockers.append("human_approval_required")

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
            "lot_or_serial_ids": lot_ids,
            "lot_or_serial_count": len(lot_ids),
            "positions": positions,
        },
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
            "required_before_move": [
                "replace or calibrate target physical capacity using real dimensions/load rating",
                "define actual material-handling method for this load",
                "create a fresh blind validation set before changing the layout",
                "plan native Odoo reservation release/reassignment without losing lot/serial traceability",
                "obtain human approval",
            ],
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
