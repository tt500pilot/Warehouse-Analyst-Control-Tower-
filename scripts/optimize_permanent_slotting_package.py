from __future__ import annotations

import itertools
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
from scripts.rank_permanent_slotting_candidates import (
    _distance_between,
    _relocation_minutes,
    _single_training_source,
)


def _average_minutes(evaluation: dict) -> float:
    pickings = int(evaluation["pickings"])
    return float(evaluation["total_start_to_stage_minutes"]) / pickings if pickings else 0.0


def _pareto_frontier(rows: list[dict]) -> list[dict]:
    frontier: list[dict] = []
    for row in rows:
        dominated = False
        for other in rows:
            if other is row:
                continue
            better_or_equal_savings = (
                float(other["training_minutes_saved_per_kit"])
                >= float(row["training_minutes_saved_per_kit"])
            )
            lower_or_equal_effort = (
                float(other["estimated_one_time_relocation_minutes"])
                <= float(row["estimated_one_time_relocation_minutes"])
            )
            strictly_better = (
                float(other["training_minutes_saved_per_kit"])
                > float(row["training_minutes_saved_per_kit"])
                or float(other["estimated_one_time_relocation_minutes"])
                < float(row["estimated_one_time_relocation_minutes"])
            )
            if better_or_equal_savings and lower_or_equal_effort and strictly_better:
                dominated = True
                break
        if not dominated:
            frontier.append(row)
    return sorted(
        frontier,
        key=lambda row: (
            row["training_payback_kits"] is None,
            row["training_payback_kits"] if row["training_payback_kits"] is not None else float("inf"),
            -float(row["training_minutes_saved_per_kit"]),
            row["products"],
        ),
    )


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

    slotting = optimize_slotting_layout(
        training,
        comparison,
        geometry,
        metadata,
        assumptions=assumptions,
        seed=42,
    )
    bins, product_positions, capacity_anomalies = _build_live_state(
        client,
        geometry,
        metadata,
    )
    source_by_code = _single_training_source(training)
    baseline = evaluate_layout(training, geometry, assumptions=assumptions, seed=42)
    baseline_avg = _average_minutes(baseline)

    candidates: list[dict] = []
    for recommendation in slotting["recommendations"]:
        code = str(recommendation["product_code"])
        source = source_by_code.get(code)
        target = str(recommendation["candidate_location"])
        if not source or not target or source == target:
            continue

        single_eval = evaluate_layout(
            _apply_assignment(training, geometry, {code: target}),
            geometry,
            assumptions=assumptions,
            seed=42,
        )
        marginal_saved = baseline_avg - _average_minutes(single_eval)
        if marginal_saved <= 0:
            continue

        positions = product_positions.get(code, [])
        live_quantity = sum(float(row["quantity"]) for row in positions)
        live_reserved = sum(float(row["reserved_quantity"]) for row in positions)
        unit_weight = float(metadata.get(code, {}).get("weight_lb") or 0.0)
        target_bin = bins.get(target, {})
        other_occupants = sorted(
            occupant
            for occupant in target_bin.get("occupants", {})
            if occupant != code
        )
        distance = _distance_between(geometry, source, target)
        relocation_minutes = _relocation_minutes(
            distance_ft=distance,
            quantity=live_quantity,
            assumptions=assumptions,
        )
        candidates.append(
            {
                "product_code": code,
                "source_location": source,
                "candidate_location": target,
                "marginal_training_minutes_saved_per_kit": round(marginal_saved, 4),
                "live_on_hand_quantity": round(live_quantity, 3),
                "live_reserved_quantity": round(live_reserved, 3),
                "estimated_weight_lb": round(live_quantity * unit_weight, 3),
                "estimated_one_time_relocation_minutes": round(relocation_minutes, 3),
                "target_empty": float(target_bin.get("total_units") or 0.0) <= 0,
                "target_other_occupants": other_occupants,
                "requires_incumbent_displacement": bool(other_occupants),
                "requires_native_reservation_handling": live_reserved > 0,
            }
        )

    by_code = {row["product_code"]: row for row in candidates}
    codes = sorted(by_code)
    package_rows: list[dict] = []

    for size in range(1, len(codes) + 1):
        for combo in itertools.combinations(codes, size):
            targets = [by_code[code]["candidate_location"] for code in combo]
            if len(set(targets)) != len(targets):
                continue
            assignment = {
                code: by_code[code]["candidate_location"]
                for code in combo
            }
            evaluation = evaluate_layout(
                _apply_assignment(training, geometry, assignment),
                geometry,
                assumptions=assumptions,
                seed=42,
            )
            saved_per_kit = baseline_avg - _average_minutes(evaluation)
            relocation_minutes = sum(
                float(by_code[code]["estimated_one_time_relocation_minutes"])
                for code in combo
            )
            payback = relocation_minutes / saved_per_kit if saved_per_kit > 0 else None
            occupied_targets = sorted(
                code for code in combo if by_code[code]["requires_incumbent_displacement"]
            )
            reserved_products = sorted(
                code for code in combo if by_code[code]["requires_native_reservation_handling"]
            )
            package_rows.append(
                {
                    "products": list(combo),
                    "product_count": len(combo),
                    "training_minutes_saved_per_kit": round(saved_per_kit, 4),
                    "training_distance_ft": evaluation["total_distance_ft"],
                    "estimated_one_time_relocation_minutes": round(relocation_minutes, 3),
                    "training_payback_kits": round(payback, 1) if payback is not None else None,
                    "payback_if_relocation_effort_2x_kits": round(payback * 2.0, 1) if payback is not None else None,
                    "payback_if_relocation_effort_3x_kits": round(payback * 3.0, 1) if payback is not None else None,
                    "estimated_weight_lb": round(
                        sum(float(by_code[code]["estimated_weight_lb"]) for code in combo),
                        3,
                    ),
                    "live_on_hand_units": round(
                        sum(float(by_code[code]["live_on_hand_quantity"]) for code in combo),
                        3,
                    ),
                    "occupied_target_products": occupied_targets,
                    "reserved_products": reserved_products,
                    "requires_incumbent_displacement": bool(occupied_targets),
                    "requires_native_reservation_handling": bool(reserved_products),
                    "all_targets_currently_empty": all(by_code[code]["target_empty"] for code in combo),
                }
            )

    positive_packages = [
        row for row in package_rows if float(row["training_minutes_saved_per_kit"]) > 0
    ]
    no_displacement = [
        row for row in positive_packages if not row["requires_incumbent_displacement"]
    ]

    def best_payback(rows: list[dict]) -> dict | None:
        eligible = [row for row in rows if row["training_payback_kits"] is not None]
        if not eligible:
            return None
        return min(
            eligible,
            key=lambda row: (
                float(row["training_payback_kits"]),
                -float(row["training_minutes_saved_per_kit"]),
                row["products"],
            ),
        )

    def max_savings(rows: list[dict]) -> dict | None:
        if not rows:
            return None
        return max(
            rows,
            key=lambda row: (
                float(row["training_minutes_saved_per_kit"]),
                -float(row["estimated_one_time_relocation_minutes"]),
                tuple(row["products"]),
            ),
        )

    result = {
        "mode": "read_only_permanent_slotting_package_optimization",
        "odoo_mutated": False,
        "experimental_design": {
            "training_picking_ids": list(training_ids),
            "reused_comparison_picking_ids": list(comparison_ids),
            "package_selection_uses_training_only": True,
            "comparison_set_used_for_package_selection": False,
            "warning": (
                "P9-P12 are not a final blind validation set because their simulated outcomes have already been inspected."
            ),
        },
        "candidate_pool": candidates,
        "summary": {
            "positive_single_candidates": len(candidates),
            "packages_evaluated": len(package_rows),
            "positive_packages": len(positive_packages),
            "current_mock_capacity_anomalies": len(capacity_anomalies),
        },
        "best_training_payback_any": best_payback(positive_packages),
        "best_training_payback_without_displacement": best_payback(no_displacement),
        "max_training_savings_without_displacement": max_savings(no_displacement),
        "pareto_frontier_without_displacement": _pareto_frontier(no_displacement),
        "all_positive_packages": sorted(
            positive_packages,
            key=lambda row: (
                row["training_payback_kits"] is None,
                row["training_payback_kits"] if row["training_payback_kits"] is not None else float("inf"),
                -float(row["training_minutes_saved_per_kit"]),
                row["products"],
            ),
        ),
        "interpretation_guardrails": {
            "relocation_time_is_model_based_not_observed": True,
            "weight_reported_but_no_equipment_specific_handling_model": True,
            "2x_and_3x_relocation_effort_sensitivity_reported": True,
            "capacity_fixture_trusted": False,
        },
        "next_gate": {
            "recalibrate_or_replace_synthetic_capacity_limits": True,
            "define_real_material_handling_equipment_assumptions": True,
            "release_or_reassign_odoo_reservations_only_after_approved_test_plan": True,
            "create_new_blind_validation_pickings": True,
            "human_approval_required": True,
        },
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
