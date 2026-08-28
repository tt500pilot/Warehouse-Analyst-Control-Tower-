from __future__ import annotations

import itertools
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
from scripts.optimize_forward_pick_subset import (
    _evaluate_assignment,
    _replenishment_minutes_per_kit_lower_bound,
    _training_original_locations,
)
from scripts.optimize_holdout_slotting import (
    DEFAULT_DATA_DIR,
    DEFAULT_HOLDOUTS,
    DEFAULT_TRAINING,
    _validate_experiment,
    build_reservations,
)


def _pruned_assignment(full, training, geometry, assumptions):
    recommendations = {str(row["product_code"]): row for row in full["recommendations"]}
    current_location = _training_original_locations(training)
    assignment = {
        code: str(row["forward_pick_location"])
        for code, row in recommendations.items()
    }
    replenishment_by_code = {
        code: _replenishment_minutes_per_kit_lower_bound(row, geometry, assumptions)
        for code, row in recommendations.items()
    }

    def recurring(mapping):
        return sum(
            replenishment_by_code.get(code, 0.0)
            for code, target in mapping.items()
            if current_location.get(code) and target != current_location[code]
        )

    evaluation = _evaluate_assignment(training, geometry, assignment, assumptions)
    training_kits = int(evaluation["pickings"])

    def objective(mapping, result):
        return float(result["total_start_to_stage_minutes"]) / training_kits + recurring(mapping)

    while True:
        before = objective(assignment, evaluation)
        best = None
        for code in sorted(assignment):
            original = current_location.get(code)
            if not original or assignment[code] == original:
                continue
            trial = dict(assignment)
            trial[code] = original
            trial_eval = _evaluate_assignment(training, geometry, trial, assumptions)
            after = objective(trial, trial_eval)
            gain = before - after
            if gain <= 1e-9:
                continue
            candidate = (gain, code, trial, trial_eval)
            if best is None or (candidate[0], candidate[1]) > (best[0], best[1]):
                best = candidate
        if best is None:
            break
        _, _, assignment, evaluation = best

    retained = sorted(
        code
        for code, target in assignment.items()
        if current_location.get(code) and target != current_location[code]
    )
    return assignment, retained, recommendations, current_location, recurring(assignment)


def _distance(geometry, start_node, end_node):
    return float(shortest_path(geometry.adjacency, start_node, end_node)[0])


def _route_distance(geometry, sequence):
    current = geometry.kitting_node
    total = 0.0
    for node in sequence:
        total += _distance(geometry, current, node)
        current = node
    total += _distance(geometry, current, geometry.kitting_node)
    return total


def _best_pickup_delivery_route(geometry, tasks):
    stops = []
    pickup_index = {}
    drop_index = {}
    for code, task in tasks.items():
        pickup = (code, "pickup", task["source_node"])
        drop = (code, "drop", task["target_node"])
        pickup_index[code] = pickup
        drop_index[code] = drop
        stops.extend([pickup, drop])

    best = None
    for permutation in itertools.permutations(stops):
        positions = {stop: idx for idx, stop in enumerate(permutation)}
        if any(positions[pickup_index[code]] > positions[drop_index[code]] for code in tasks):
            continue
        nodes = [stop[2] for stop in permutation]
        distance = _route_distance(geometry, nodes)
        lexical = tuple((stop[0], stop[1]) for stop in permutation)
        candidate = (distance, lexical, permutation)
        if best is None or candidate[:2] < best[:2]:
            best = candidate
    if best is None:
        raise RuntimeError("Unable to find a valid pickup-before-dropoff replenishment route.")
    return best[0], best[2]


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
    assignment, retained, recommendations, current_location, independent_lower = _pruned_assignment(
        full, training, geometry, assumptions
    )

    tasks = {}
    cadences = []
    for code in retained:
        row = recommendations[code]
        source_tail = current_location[code]
        target_tail = assignment[code]
        avg = float(row.get("average_training_demand_per_kit") or 0.0)
        face_qty = float(row.get("pick_face_quantity") or 0.0)
        cadence = face_qty / avg if avg > 0 else 0.0
        cadences.append(cadence)
        tasks[code] = {
            "product_code": code,
            "source_tail": source_tail,
            "target_tail": target_tail,
            "source_node": geometry.locations_by_tail[source_tail]["graph_node_id"],
            "target_node": geometry.locations_by_tail[target_tail]["graph_node_id"],
            "pick_face_quantity": face_qty,
            "average_demand_per_kit": avg,
            "natural_replenishment_cadence_kits": round(cadence, 2),
        }

    if not tasks:
        raise RuntimeError("Pruning retained no forward-pick faces to batch.")

    # A synchronized milk-run must run no less frequently than the tightest face.
    batch_interval_kits = min(cadences)
    route_distance_ft, sequence = _best_pickup_delivery_route(geometry, tasks)

    service_seconds = 0.0
    for code, action, _ in sequence:
        qty = tasks[code]["pick_face_quantity"]
        if action == "pickup":
            service_seconds += assumptions.base_search_seconds
            service_seconds += assumptions.base_handling_seconds
            service_seconds += assumptions.handling_seconds_per_unit * min(qty, 10.0)
            service_seconds += assumptions.base_scan_seconds
        else:
            service_seconds += assumptions.base_handling_seconds
            service_seconds += assumptions.base_scan_seconds

    route_minutes = route_distance_ft / assumptions.walking_speed_ft_s / 60.0 + service_seconds / 60.0
    batched_minutes_per_kit = route_minutes / batch_interval_kits

    training_baseline = evaluate_layout(training, geometry, assumptions=assumptions, seed=42)
    training_candidate = _evaluate_assignment(training, geometry, assignment, assumptions)
    holdout_baseline = evaluate_layout(holdout, geometry, assumptions=assumptions, seed=42)
    holdout_candidate = _evaluate_assignment(holdout, geometry, assignment, assumptions)

    train_gross = (
        float(training_baseline["total_start_to_stage_minutes"])
        - float(training_candidate["total_start_to_stage_minutes"])
    ) / int(training_baseline["pickings"])
    holdout_gross = (
        float(holdout_baseline["total_start_to_stage_minutes"])
        - float(holdout_candidate["total_start_to_stage_minutes"])
    ) / int(holdout_baseline["pickings"])

    setup_legs = [leg for code in retained for leg in recommendations[code].get("move_legs", [])]
    setup_minutes = 0.0
    for leg in setup_legs:
        distance = float(leg.get("graph_distance_ft") or 0.0)
        qty = float(leg.get("quantity") or 0.0)
        seconds = (
            distance / assumptions.walking_speed_ft_s
            + assumptions.base_search_seconds
            + assumptions.base_handling_seconds
            + assumptions.base_scan_seconds * 2.0
            + assumptions.handling_seconds_per_unit * min(qty, 10.0)
        )
        setup_minutes += seconds / 60.0

    train_net = train_gross - batched_minutes_per_kit
    holdout_net = holdout_gross - batched_minutes_per_kit

    result = {
        "mode": "read_only_batched_forward_replenishment",
        "odoo_mutated": False,
        "experimental_design": {
            "subset_selected_using_training_only": True,
            "holdout_used_for_subset_selection": False,
            "retained_products": retained,
        },
        "replenishment_tasks": tasks,
        "batch_policy": {
            "synchronized_interval_kits": round(batch_interval_kits, 2),
            "reason": "minimum natural replenishment cadence among retained forward faces",
            "all_retained_faces_topped_up_each_batch": True,
        },
        "optimized_batch_route": {
            "distance_ft": round(route_distance_ft, 3),
            "route_minutes": round(route_minutes, 3),
            "sequence": [
                {
                    "product_code": code,
                    "action": action,
                    "location": tasks[code]["source_tail"] if action == "pickup" else tasks[code]["target_tail"],
                }
                for code, action, _ in sequence
            ],
        },
        "economics": {
            "independent_replenishment_lower_bound_minutes_per_kit": round(independent_lower, 4),
            "batched_replenishment_minutes_per_kit": round(batched_minutes_per_kit, 4),
            "replenishment_minutes_saved_by_batching_per_kit": round(independent_lower - batched_minutes_per_kit, 4),
            "training_gross_kitting_minutes_saved_per_kit": round(train_gross, 4),
            "training_net_minutes_saved_per_kit": round(train_net, 4),
            "holdout_gross_kitting_minutes_saved_per_kit": round(holdout_gross, 4),
            "holdout_net_minutes_saved_per_kit": round(holdout_net, 4),
            "estimated_setup_minutes": round(setup_minutes, 2),
            "holdout_setup_payback_kits": round(setup_minutes / holdout_net, 1) if holdout_net > 0 else None,
        },
        "decision": (
            "retain_three_sku_batch_for_capacity_and_physical_test_gate"
            if holdout_net > 0
            else "reject_three_sku_forward_pick_batch"
        ),
        "remaining_gates": {
            "capacity_fixture_recalibration_required": True,
            "physical_replenishment_route_observation_required": True,
            "native_reservation_plan_required_for_holdout_test": True,
            "human_approval_required": True,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
