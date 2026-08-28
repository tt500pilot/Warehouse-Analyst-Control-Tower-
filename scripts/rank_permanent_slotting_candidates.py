from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.relocation_planner import _build_live_state
from app.services.slotting_optimizer import (
    _apply_assignment,
    evaluate_layout,
    load_slotting_product_metadata,
    optimize_slotting_layout,
)
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
    source = geometry.locations_by_tail[source_tail]["graph_node_id"]
    target = geometry.locations_by_tail[target_tail]["graph_node_id"]
    return float(shortest_path(geometry.adjacency, source, target)[0])


def _single_training_source(training) -> dict[str, str]:
    locations: dict[str, set[str]] = {}
    for rows in training.values():
        for row in rows:
            code = str(row.get("product_code") or "")
            tail = str(row.get("location_tail") or "")
            if code and tail:
                locations.setdefault(code, set()).add(tail)
    return {
        code: next(iter(tails))
        for code, tails in locations.items()
        if len(tails) == 1
    }


def _relocation_minutes(
    *,
    distance_ft: float,
    quantity: float,
    assumptions: PickerAssumptions,
) -> float:
    # Permanent relocation is a one-time material-handling task. Unlike the
    # picker timing model, handling scales with the full quantity moved rather
    # than being capped at ten units.
    travel_seconds = distance_ft / assumptions.walking_speed_ft_s
    service_seconds = (
        assumptions.base_search_seconds
        + assumptions.base_handling_seconds
        + assumptions.base_scan_seconds * 2.0
        + assumptions.handling_seconds_per_unit * max(quantity, 0.0)
    )
    return (travel_seconds + service_seconds) / 60.0


def main() -> None:
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

    # The slotting assignment itself is learned from training only. The reused
    # P9-P12 set is passed because the existing optimizer API requires it, but
    # those results are not used to select or rank permanent moves here.
    slotting = optimize_slotting_layout(
        training,
        comparison,
        geometry,
        metadata,
        assumptions=assumptions,
        seed=42,
    )

    bins, product_positions, current_capacity_anomalies = _build_live_state(
        client,
        geometry,
        metadata,
    )
    source_by_code = _single_training_source(training)
    training_baseline = evaluate_layout(
        training,
        geometry,
        assumptions=assumptions,
        seed=42,
    )
    baseline_avg = float(training_baseline["total_start_to_stage_minutes"]) / int(training_baseline["pickings"])

    ranked = []
    for recommendation in slotting["recommendations"]:
        code = str(recommendation["product_code"])
        source = source_by_code.get(code)
        target = str(recommendation["candidate_location"])
        if not source or not target or source == target:
            continue

        trial = _apply_assignment(training, geometry, {code: target})
        trial_eval = evaluate_layout(
            trial,
            geometry,
            assumptions=assumptions,
            seed=42,
        )
        trial_avg = float(trial_eval["total_start_to_stage_minutes"]) / int(trial_eval["pickings"])
        saved_per_kit = baseline_avg - trial_avg

        positions = product_positions.get(code, [])
        live_quantity = sum(float(row["quantity"]) for row in positions)
        live_reserved = sum(float(row["reserved_quantity"]) for row in positions)
        unit_weight = float(metadata.get(code, {}).get("weight_lb") or 0.0)
        weight_lb = live_quantity * unit_weight
        distance = _distance_between(geometry, source, target)
        move_minutes = _relocation_minutes(
            distance_ft=distance,
            quantity=live_quantity,
            assumptions=assumptions,
        )

        target_bin = bins.get(target, {})
        other_occupants = sorted(
            occupant
            for occupant in target_bin.get("occupants", {})
            if occupant != code
        )
        simple_empty_target = float(target_bin.get("total_units") or 0.0) <= 0
        same_sku_only_target = bool(target_bin.get("occupants")) and not other_occupants
        simple_target = simple_empty_target or same_sku_only_target

        payback = move_minutes / saved_per_kit if saved_per_kit > 0 else None
        ranked.append(
            {
                "product_code": code,
                "source_location": source,
                "candidate_location": target,
                "candidate_zone": recommendation.get("candidate_zone"),
                "flight_critical": bool(recommendation.get("flight_critical")),
                "secure_required": bool(recommendation.get("secure_required")),
                "training_line_frequency": int(recommendation.get("training_line_frequency") or 0),
                "training_quantity_demand": float(recommendation.get("training_quantity_demand") or 0.0),
                "marginal_training_minutes_saved_per_kit": round(saved_per_kit, 4),
                "live_on_hand_quantity_to_relocate": round(live_quantity, 3),
                "live_reserved_quantity": round(live_reserved, 3),
                "estimated_weight_lb_to_relocate": round(weight_lb, 3),
                "relocation_graph_distance_ft": round(distance, 3),
                "estimated_one_time_relocation_minutes": round(move_minutes, 3),
                "training_payback_kits": round(payback, 1) if payback is not None else None,
                "target_empty": simple_empty_target,
                "target_same_sku_only": same_sku_only_target,
                "target_other_occupants": other_occupants,
                "simple_relocation_candidate": simple_target and live_reserved <= 0,
                "requires_incumbent_displacement": bool(other_occupants),
                "requires_native_reservation_handling": live_reserved > 0,
                "capacity_fixture_trusted": False,
            }
        )

    ranked.sort(
        key=lambda row: (
            row["training_payback_kits"] is None,
            row["training_payback_kits"] if row["training_payback_kits"] is not None else float("inf"),
            -row["marginal_training_minutes_saved_per_kit"],
            row["product_code"],
        )
    )

    positive = [row for row in ranked if row["marginal_training_minutes_saved_per_kit"] > 0]
    simple_positive = [row for row in positive if row["simple_relocation_candidate"]]

    result = {
        "mode": "read_only_permanent_slotting_candidate_ranking",
        "odoo_mutated": False,
        "experimental_design": {
            "training_picking_ids": list(training_ids),
            "reused_comparison_picking_ids": list(comparison_ids),
            "ranking_uses_training_only": True,
            "comparison_set_used_for_ranking": False,
            "warning": (
                "P9-P12 remain physically untouched but have been viewed across multiple model variants, "
                "so they should not be treated as the final blind validation set."
            ),
        },
        "decision_rule": (
            "Rank each candidate by one-time permanent relocation labor divided by its training-only marginal kitting savings. "
            "No arbitrary payback threshold is applied."
        ),
        "summary": {
            "ranked_relocation_candidates": len(ranked),
            "positive_training_savings_candidates": len(positive),
            "simple_positive_candidates_without_displacement_or_reservation": len(simple_positive),
            "current_mock_capacity_anomalies": len(current_capacity_anomalies),
        },
        "top_candidates": ranked[:15],
        "all_candidates": ranked,
        "next_gate": {
            "select_payback_policy_before_approval": True,
            "recalibrate_capacity_fixture_before_physical_execution": True,
            "run_dependency_planner_for_occupied_targets": True,
            "use_native_odoo_unreserve_reassign_for_reserved_stock": True,
            "create_new_blind_validation_pickings_for_final_test": True,
            "human_approval_required": True,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
