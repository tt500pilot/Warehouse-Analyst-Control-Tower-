from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.forward_pick_optimizer import optimize_forward_pick_layout
from app.services.slotting_optimizer import _apply_assignment, evaluate_layout, load_slotting_product_metadata
from app.services.virtual_picker import PickerAssumptions, shortest_path
from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report
from scripts.optimize_holdout_slotting import (
    DEFAULT_DATA_DIR,
    DEFAULT_HOLDOUTS,
    DEFAULT_TRAINING,
    _validate_experiment,
    build_reservations,
)


def _distance_between(geometry, source_tail: str, target_tail: str) -> float:
    source_node = geometry.locations_by_tail[source_tail]["graph_node_id"]
    target_node = geometry.locations_by_tail[target_tail]["graph_node_id"]
    return float(shortest_path(geometry.adjacency, source_node, target_node)[0])


def _replenishment_minutes_per_kit_lower_bound(
    row: dict,
    geometry,
    assumptions: PickerAssumptions,
) -> float:
    current_locations = [str(value) for value in row.get("current_locations", []) if value]
    target = str(row.get("forward_pick_location") or "")
    avg_demand = float(row.get("average_training_demand_per_kit") or 0.0)
    pick_face_qty = float(row.get("pick_face_quantity") or 0.0)
    if not current_locations or not target or target in current_locations:
        return 0.0
    if avg_demand <= 0 or pick_face_qty <= 0:
        return 0.0

    source = min(
        current_locations,
        key=lambda tail: (_distance_between(geometry, tail, target), tail),
    )
    distance = _distance_between(geometry, source, target)
    service_seconds = (
        assumptions.base_search_seconds
        + assumptions.base_handling_seconds
        + assumptions.base_scan_seconds * 2.0
        + assumptions.handling_seconds_per_unit * min(pick_face_qty, 10.0)
    )
    event_minutes = distance / assumptions.walking_speed_ft_s / 60.0 + service_seconds / 60.0
    return event_minutes * (avg_demand / pick_face_qty)


def _training_original_locations(training: dict[int, list[dict]]) -> dict[str, str]:
    locations: dict[str, set[str]] = {}
    for rows in training.values():
        for row in rows:
            code = str(row.get("product_code") or "")
            tail = str(row.get("location_tail") or "")
            if code and tail:
                locations.setdefault(code, set()).add(tail)
    # Only SKUs with one deterministic training source are eligible for greedy
    # reversion. Multi-source SKUs remain untouched by this pruning pass.
    return {
        code: next(iter(tails))
        for code, tails in locations.items()
        if len(tails) == 1
    }


def _evaluate_assignment(reservations, geometry, assignment, assumptions):
    return evaluate_layout(
        _apply_assignment(reservations, geometry, assignment),
        geometry,
        assumptions=assumptions,
        seed=42,
    )


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

    full = optimize_forward_pick_layout(
        client,
        training_reservations_by_picking=training,
        holdout_reservations_by_picking=holdout,
        geometry=geometry,
        product_metadata=metadata,
        data_dir=data_dir,
        assumptions=assumptions,
        seed=42,
    )

    recommendations = {
        str(row["product_code"]): row
        for row in full["recommendations"]
    }
    current_location = _training_original_locations(training)
    assignment = {
        code: str(row["forward_pick_location"])
        for code, row in recommendations.items()
    }

    replenishment_by_code = {
        code: _replenishment_minutes_per_kit_lower_bound(row, geometry, assumptions)
        for code, row in recommendations.items()
    }

    def recurring_replenishment(mapping: dict[str, str]) -> float:
        total = 0.0
        for code, target in mapping.items():
            original = current_location.get(code)
            if original and target != original:
                total += replenishment_by_code.get(code, 0.0)
        return total

    training_eval = _evaluate_assignment(training, geometry, assignment, assumptions)
    training_kits = int(training_eval["pickings"])

    def objective(mapping: dict[str, str], evaluation: dict) -> float:
        average_kit_minutes = (
            float(evaluation["total_start_to_stage_minutes"]) / training_kits
            if training_kits
            else 0.0
        )
        return average_kit_minutes + recurring_replenishment(mapping)

    pruning_steps: list[dict] = []
    while True:
        current_objective = objective(assignment, training_eval)
        best = None
        for code in sorted(assignment):
            original = current_location.get(code)
            if not original or assignment[code] == original:
                continue
            trial = dict(assignment)
            trial[code] = original
            trial_eval = _evaluate_assignment(training, geometry, trial, assumptions)
            trial_objective = objective(trial, trial_eval)
            improvement = current_objective - trial_objective
            if improvement <= 1e-9:
                continue
            candidate = (
                improvement,
                code,
                trial,
                trial_eval,
                current_objective,
                trial_objective,
            )
            if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
                best = candidate
        if best is None:
            break
        improvement, code, assignment, training_eval, before, after = best
        pruning_steps.append(
            {
                "product_code": code,
                "action": "revert_forward_face_to_training_source",
                "training_source": current_location[code],
                "removed_forward_pick": recommendations[code]["forward_pick_location"],
                "recurring_minutes_per_kit_improved": round(improvement, 4),
                "objective_before": round(before, 4),
                "objective_after": round(after, 4),
            }
        )

    training_baseline = evaluate_layout(
        training,
        geometry,
        assumptions=assumptions,
        seed=42,
    )
    holdout_baseline = evaluate_layout(
        holdout,
        geometry,
        assumptions=assumptions,
        seed=42,
    )
    holdout_candidate = _evaluate_assignment(holdout, geometry, assignment, assumptions)

    retained_codes = sorted(
        code
        for code, target in assignment.items()
        if current_location.get(code) and target != current_location[code]
    )
    removed_codes = sorted(
        code
        for code in recommendations
        if code in current_location and code not in retained_codes
        and recommendations[code]["forward_pick_location"] != current_location[code]
    )
    recurring_lower = recurring_replenishment(assignment)

    train_baseline_avg = float(training_baseline["total_start_to_stage_minutes"]) / int(training_baseline["pickings"])
    train_candidate_avg = float(training_eval["total_start_to_stage_minutes"]) / int(training_eval["pickings"])
    train_gross_saved = train_baseline_avg - train_candidate_avg
    train_net_saved = train_gross_saved - recurring_lower

    holdout_baseline_avg = float(holdout_baseline["total_start_to_stage_minutes"]) / int(holdout_baseline["pickings"])
    holdout_candidate_avg = float(holdout_candidate["total_start_to_stage_minutes"]) / int(holdout_candidate["pickings"])
    holdout_gross_saved = holdout_baseline_avg - holdout_candidate_avg
    holdout_net_saved = holdout_gross_saved - recurring_lower

    retained_setup_legs = []
    for code in retained_codes:
        retained_setup_legs.extend(recommendations[code].get("move_legs", []))
    setup_minutes = 0.0
    for leg in retained_setup_legs:
        distance = float(leg.get("graph_distance_ft") or 0.0)
        qty = float(leg.get("quantity") or 0.0)
        service_seconds = (
            assumptions.base_search_seconds
            + assumptions.base_handling_seconds
            + assumptions.base_scan_seconds * 2.0
            + assumptions.handling_seconds_per_unit * min(qty, 10.0)
        )
        setup_minutes += distance / assumptions.walking_speed_ft_s / 60.0 + service_seconds / 60.0

    result = {
        "mode": "read_only_economics_pruned_forward_pick_subset",
        "odoo_mutated": False,
        "experimental_design": {
            "training_picking_ids": list(training_ids),
            "holdout_picking_ids": list(holdout_ids),
            "subset_selected_using_training_only": True,
            "holdout_used_for_pruning": False,
        },
        "selection_objective": (
            "minimize training average simulated kit minutes plus lower-bound recurring replenishment minutes per kit"
        ),
        "full_forward_pick_candidate": {
            "moved_products": full["implementation"]["moved_products"],
            "gross_holdout_minutes_saved_per_kit": round(
                float(full["holdout"]["improvement"]["start_to_stage_minutes_saved"])
                / int(full["holdout"]["baseline"]["pickings"]),
                4,
            ),
        },
        "pruned_subset": {
            "retained_forward_pick_products": retained_codes,
            "retained_count": len(retained_codes),
            "removed_forward_pick_products": removed_codes,
            "removed_count": len(removed_codes),
            "pruning_steps": pruning_steps,
        },
        "training": {
            "baseline_average_minutes_per_kit": round(train_baseline_avg, 4),
            "candidate_average_minutes_per_kit": round(train_candidate_avg, 4),
            "gross_minutes_saved_per_kit": round(train_gross_saved, 4),
            "recurring_replenishment_lower_bound_minutes_per_kit": round(recurring_lower, 4),
            "net_minutes_saved_per_kit": round(train_net_saved, 4),
        },
        "holdout": {
            "baseline_average_minutes_per_kit": round(holdout_baseline_avg, 4),
            "candidate_average_minutes_per_kit": round(holdout_candidate_avg, 4),
            "gross_minutes_saved_per_kit": round(holdout_gross_saved, 4),
            "recurring_replenishment_lower_bound_minutes_per_kit": round(recurring_lower, 4),
            "net_minutes_saved_per_kit": round(holdout_net_saved, 4),
            "distance_saved_ft": round(
                float(holdout_baseline["total_distance_ft"])
                - float(holdout_candidate["total_distance_ft"]),
                3,
            ),
        },
        "implementation": {
            "setup_move_legs": len(retained_setup_legs),
            "setup_units": round(sum(float(row.get("quantity") or 0.0) for row in retained_setup_legs), 3),
            "setup_weight_lb": round(sum(float(row.get("estimated_weight_lb") or 0.0) for row in retained_setup_legs), 3),
            "estimated_setup_minutes": round(setup_minutes, 2),
            "optimistic_payback_kits": round(setup_minutes / holdout_net_saved, 1) if holdout_net_saved > 0 else None,
        },
        "decision": (
            "retain_pruned_subset_for_next_gate"
            if holdout_net_saved > 0
            else "reject_forward_pick_subset_under_lower_bound_replenishment_cost"
        ),
        "remaining_gates": {
            "capacity_fixture_recalibration_required": True,
            "batched_replenishment_route_model_required_before_execution": True,
            "native_reservation_plan_required_for_holdout_physical_test": True,
            "human_approval_required": True,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
