from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.forward_pick_optimizer import optimize_forward_pick_layout
from app.services.slotting_optimizer import load_slotting_product_metadata
from app.services.virtual_picker import PickerAssumptions, shortest_path
from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report
from scripts.optimize_holdout_slotting import DEFAULT_DATA_DIR, DEFAULT_HOLDOUTS, DEFAULT_TRAINING, _validate_experiment, build_reservations


def _tail_distance(geometry, a: str, b: str) -> float:
    ga = geometry.locations_by_tail[a]["graph_node_id"]
    gb = geometry.locations_by_tail[b]["graph_node_id"]
    return float(shortest_path(geometry.adjacency, ga, gb)[0])


def main() -> None:
    client = OdooWarehouseClient.from_env()
    readiness = build_readiness_report(client)
    training_ids = tuple(DEFAULT_TRAINING)
    holdout_ids = tuple(DEFAULT_HOLDOUTS)
    _validate_experiment(readiness, training_ids, holdout_ids)

    data_dir = Path(DEFAULT_DATA_DIR)
    training, geometry = build_reservations(client, training_ids, data_dir)
    holdout, _ = build_reservations(client, holdout_ids, data_dir)
    metadata = load_slotting_product_metadata(data_dir)
    assumptions = PickerAssumptions()

    optimization = optimize_forward_pick_layout(
        client,
        training_reservations_by_picking=training,
        holdout_reservations_by_picking=holdout,
        geometry=geometry,
        product_metadata=metadata,
        data_dir=data_dir,
        assumptions=assumptions,
        seed=42,
    )

    gross_saved_total = float(optimization["holdout"]["improvement"]["start_to_stage_minutes_saved"])
    holdout_kits = int(optimization["holdout"]["baseline"]["pickings"])
    gross_saved_per_kit = gross_saved_total / holdout_kits if holdout_kits else 0.0

    setup_minutes = 0.0
    replenishment_lower = 0.0
    replenishment_upper = 0.0
    supply_shortfalls = {}

    for row in optimization["recommendations"]:
        code = row["product_code"]
        target = row["forward_pick_location"]
        pick_face_qty = float(row["pick_face_quantity"])
        avg_demand = float(row.get("average_training_demand_per_kit") or 0.0)

        source_tail = None
        for current in row.get("current_locations", []):
            source_tail = source_tail or current

        # If a face remains at the SKU's current bin and the desired face quantity
        # exceeds what can actually be staged from available stock, classify the
        # gap as a supply shortfall rather than a relocation requirement.
        if row.get("assignment_reason") in {"keep_exclusive_current", "reverted_low_value_move"}:
            moved = float(row.get("unreserved_move_quantity_planned") or 0.0)
            gap = float(row.get("reservation_quantity_gap") or 0.0)
            if moved == 0.0 and gap > 0.0:
                supply_shortfalls[code] = gap

        for leg in row.get("move_legs", []):
            distance = float(leg.get("graph_distance_ft") or 0.0)
            qty = float(leg.get("quantity") or 0.0)
            service_seconds = (
                assumptions.base_search_seconds
                + assumptions.base_handling_seconds
                + assumptions.base_scan_seconds * 2.0
                + assumptions.handling_seconds_per_unit * min(qty, 10.0)
            )
            setup_minutes += (
                distance / assumptions.walking_speed_ft_s + service_seconds
            ) / 60.0

        if source_tail and source_tail != target and avg_demand > 0 and pick_face_qty > 0:
            direct = _tail_distance(geometry, source_tail, target)
            source_node = geometry.locations_by_tail[source_tail]["graph_node_id"]
            target_node = geometry.locations_by_tail[target]["graph_node_id"]
            to_source = float(
                shortest_path(geometry.adjacency, geometry.kitting_node, source_node)[0]
            )
            to_kitting = float(
                shortest_path(geometry.adjacency, target_node, geometry.kitting_node)[0]
            )
            service_seconds = (
                assumptions.base_search_seconds
                + assumptions.base_handling_seconds
                + assumptions.base_scan_seconds * 2.0
                + assumptions.handling_seconds_per_unit * min(pick_face_qty, 10.0)
            )
            events_per_kit = avg_demand / pick_face_qty
            replenishment_lower += (
                (direct / assumptions.walking_speed_ft_s + service_seconds) / 60.0
            ) * events_per_kit
            replenishment_upper += (
                (
                    (to_source + direct + to_kitting) / assumptions.walking_speed_ft_s
                    + service_seconds
                )
                / 60.0
            ) * events_per_kit

    optimistic_net = gross_saved_per_kit - replenishment_lower
    pessimistic_net = gross_saved_per_kit - replenishment_upper
    result = {
        "mode": "read_only_forward_pick_economics",
        "gross_saved_minutes_per_kit": round(gross_saved_per_kit, 4),
        "estimated_setup_minutes": round(setup_minutes, 2),
        "replenishment_minutes_per_kit_lower_bound": round(replenishment_lower, 4),
        "replenishment_minutes_per_kit_upper_bound": round(replenishment_upper, 4),
        "optimistic_net_minutes_saved_per_kit": round(optimistic_net, 4),
        "pessimistic_net_minutes_saved_per_kit": round(pessimistic_net, 4),
        "optimistic_payback_kits": (
            round(setup_minutes / optimistic_net, 1) if optimistic_net > 0 else None
        ),
        "pessimistic_payback_kits": (
            round(setup_minutes / pessimistic_net, 1) if pessimistic_net > 0 else None
        ),
        "supply_shortfalls_not_relocation_gaps": supply_shortfalls,
        "capacity_fixture_recalibration_required": True,
        "odoo_writes": False,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
