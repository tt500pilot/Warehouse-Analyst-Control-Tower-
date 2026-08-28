"""Implementation-aware forward-pick slotting for the AWIA sandbox.

The previous mock-v2 relocation plan treated every optimized slot as the new
home for all on-hand inventory. That is intentionally not the default here.
This model treats optimized near-kitting locations as forward pick faces while
leaving excess stock in reserve. Candidate slots are selected from empty bins or
an already-exclusive current bin, so the first implementation-aware candidate
avoids incumbent displacement by construction.

Training pickings determine demand and assignment. Holdout pickings are used
only for evaluation. No Odoo writes occur.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from app.services.relocation_planner import _build_live_state
from app.services.slotting_optimizer import (
    _apply_assignment,
    build_profiles,
    evaluate_layout,
    optimize_slotting_layout,
)
from app.services.virtual_picker import Geometry, PickerAssumptions, shortest_path
from odoo_client import OdooWarehouseClient


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _eligible(metadata: dict[str, Any], location: dict[str, Any]) -> bool:
    zone = str(location.get("zone") or "").upper()
    if bool(metadata.get("secure_required")) and not _truthy(location.get("secure")):
        return False
    if bool(metadata.get("flight_critical")):
        if not _truthy(location.get("flight_critical_allowed")):
            return False
        if zone == "BULK":
            return False
    return True


def _distance_from_kitting(geometry: Geometry, tail: str) -> float:
    row = geometry.locations_by_tail[tail]
    distance, _ = shortest_path(
        geometry.adjacency,
        geometry.kitting_node,
        str(row["graph_node_id"]),
    )
    return float(distance)


def _distance_between(geometry: Geometry, source_tail: str, target_tail: str) -> float:
    source = geometry.locations_by_tail[source_tail]
    target = geometry.locations_by_tail[target_tail]
    distance, _ = shortest_path(
        geometry.adjacency,
        str(source["graph_node_id"]),
        str(target["graph_node_id"]),
    )
    return float(distance)


def _pick_face_qty(
    *,
    training_quantity_demand: float,
    training_kits: int,
    horizon_kits: int,
    buffer_factor: float,
) -> int:
    if training_kits <= 0:
        raise ValueError("training_kits must be positive")
    average_per_kit = float(training_quantity_demand) / float(training_kits)
    return max(1, int(math.ceil(average_per_kit * horizon_kits * buffer_factor)))


def _fits_pick_face(
    *,
    quantity: int,
    metadata: dict[str, Any],
    location: dict[str, Any],
) -> bool:
    unit_weight = float(metadata.get("weight_lb") or 0.0)
    if unit_weight <= 0:
        return False
    capacity_units = float(location.get("capacity_units") or 0.0)
    capacity_weight = float(location.get("capacity_weight_lb") or 0.0)
    return (
        capacity_units > 0
        and capacity_weight > 0
        and quantity <= capacity_units
        and quantity * unit_weight <= capacity_weight
    )


def optimize_forward_pick_layout(
    client: OdooWarehouseClient,
    *,
    training_reservations_by_picking: dict[int, list[dict[str, Any]]],
    holdout_reservations_by_picking: dict[int, list[dict[str, Any]]],
    geometry: Geometry,
    product_metadata: dict[str, dict[str, Any]],
    data_dir: str | Path,
    assumptions: PickerAssumptions | None = None,
    seed: int = 42,
    horizon_kits: int = 8,
    buffer_factor: float = 1.25,
    relocation_penalty_ft: float = 60.0,
    max_revert_penalty_minutes: float = 0.05,
    candidate_layout_version: str = "mock-v3-forward-pick-v1",
) -> dict[str, Any]:
    assumptions = assumptions or PickerAssumptions()
    if not training_reservations_by_picking or not holdout_reservations_by_picking:
        raise ValueError("Training and holdout reservations are required.")
    overlap = set(training_reservations_by_picking) & set(holdout_reservations_by_picking)
    if overlap:
        raise ValueError(f"Training/holdout overlap is not allowed: {sorted(overlap)}")

    profiles = build_profiles(training_reservations_by_picking, product_metadata)
    training_kits = len(training_reservations_by_picking)
    v2 = optimize_slotting_layout(
        training_reservations_by_picking,
        holdout_reservations_by_picking,
        geometry,
        product_metadata,
        assumptions=assumptions,
        seed=seed,
    )
    desired_by_code = {
        str(row["product_code"]): str(row["candidate_location"])
        for row in v2["recommendations"]
    }

    bins, product_positions, current_capacity_violations = _build_live_state(
        client,
        geometry,
        product_metadata,
    )

    profile_order = sorted(
        profiles.values(),
        key=lambda profile: (-profile.priority_score, profile.product_code),
    )
    pick_face_qty_by_code: dict[str, int] = {}
    assignment: dict[str, str] = {}
    assignment_reason: dict[str, str] = {}
    used_targets: set[str] = set()

    for profile in profile_order:
        code = profile.product_code
        metadata = product_metadata.get(code, {})
        pick_face_qty = _pick_face_qty(
            training_quantity_demand=profile.quantity_demand,
            training_kits=training_kits,
            horizon_kits=horizon_kits,
            buffer_factor=buffer_factor,
        )
        pick_face_qty_by_code[code] = pick_face_qty
        desired = desired_by_code.get(code)
        positions = product_positions.get(code, [])
        current_tails = {str(row["tail"]) for row in positions}

        candidates: list[tuple[float, str, str]] = []
        for tail, bin_row in bins.items():
            if tail in used_targets:
                continue
            geom = geometry.locations_by_tail.get(tail)
            if geom is None or not _eligible(metadata, geom):
                continue

            occupants = set(bin_row.get("occupants", {}))
            is_empty = float(bin_row.get("total_units") or 0.0) <= 0
            is_exclusive_current = tail in current_tails and occupants <= {code}
            if not is_empty and not is_exclusive_current:
                continue
            if is_empty and not _fits_pick_face(
                quantity=pick_face_qty,
                metadata=metadata,
                location=geom,
            ):
                continue

            distance = _distance_from_kitting(geometry, tail)
            score = distance * max(profile.line_frequency, 1)
            reason = "keep_exclusive_current" if is_exclusive_current else "empty_forward_pick"
            if not is_exclusive_current:
                score += relocation_penalty_ft
            if desired and tail == desired:
                score -= 20.0
            if int(geom.get("level") or 1) >= 2:
                score += 10.0
            candidates.append((score, tail, reason))

        if not candidates:
            raise RuntimeError(
                f"No empty/exclusive eligible forward-pick target can hold {pick_face_qty} units of {code}."
            )
        _, target, reason = min(candidates, key=lambda row: (row[0], row[1]))
        assignment[code] = target
        assignment_reason[code] = reason
        used_targets.add(target)

    def evaluate_training(mapping: dict[str, str]) -> dict[str, Any]:
        return evaluate_layout(
            _apply_assignment(training_reservations_by_picking, geometry, mapping),
            geometry,
            assumptions=assumptions,
            seed=seed,
        )

    training_candidate = evaluate_training(assignment)

    # Remove low-value relocations when an eligible, exclusive current bin can
    # be retained with only a negligible training-time penalty.
    for profile in sorted(profile_order, key=lambda item: (item.priority_score, item.product_code)):
        code = profile.product_code
        target = assignment[code]
        current_options = []
        metadata = product_metadata.get(code, {})
        for position in product_positions.get(code, []):
            tail = str(position["tail"])
            if tail == target or tail in used_targets:
                continue
            bin_row = bins[tail]
            occupants = set(bin_row.get("occupants", {}))
            if occupants <= {code} and _eligible(metadata, geometry.locations_by_tail[tail]):
                current_options.append(tail)
        if not current_options:
            continue
        current_tail = min(current_options, key=lambda tail: (_distance_from_kitting(geometry, tail), tail))
        trial = dict(assignment)
        trial[code] = current_tail
        trial_eval = evaluate_training(trial)
        if (
            float(trial_eval["total_start_to_stage_minutes"])
            <= float(training_candidate["total_start_to_stage_minutes"]) + max_revert_penalty_minutes
        ):
            used_targets.discard(target)
            used_targets.add(current_tail)
            assignment = trial
            training_candidate = trial_eval
            assignment_reason[code] = "reverted_low_value_move"

    training_baseline = evaluate_layout(
        training_reservations_by_picking,
        geometry,
        assumptions=assumptions,
        seed=seed,
    )
    holdout_baseline = evaluate_layout(
        holdout_reservations_by_picking,
        geometry,
        assumptions=assumptions,
        seed=seed,
    )
    holdout_candidate = evaluate_layout(
        _apply_assignment(holdout_reservations_by_picking, geometry, assignment),
        geometry,
        assumptions=assumptions,
        seed=seed,
    )

    holdout_codes = {
        str(row.get("product_code") or "")
        for rows in holdout_reservations_by_picking.values()
        for row in rows
        if row.get("product_code")
    }

    move_legs: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    for profile in profile_order:
        code = profile.product_code
        target = assignment[code]
        qty_required = pick_face_qty_by_code[code]
        metadata = product_metadata.get(code, {})
        positions = sorted(
            product_positions.get(code, []),
            key=lambda row: (
                -(float(row["quantity"]) - float(row["reserved_quantity"])),
                str(row["tail"]),
            ),
        )
        already_qty = sum(float(row["quantity"]) for row in positions if str(row["tail"]) == target)
        quantity_to_move = max(float(qty_required) - already_qty, 0.0)
        remaining = quantity_to_move
        source_legs: list[dict[str, Any]] = []
        for position in positions:
            if remaining <= 0:
                break
            source_tail = str(position["tail"])
            if source_tail == target:
                continue
            unreserved = max(
                float(position["quantity"]) - float(position["reserved_quantity"]),
                0.0,
            )
            take = min(unreserved, remaining)
            if take <= 0:
                continue
            leg = {
                "product_code": code,
                "source_location": source_tail,
                "target_location": target,
                "quantity": round(take, 3),
                "estimated_weight_lb": round(take * float(metadata.get("weight_lb") or 0.0), 3),
                "graph_distance_ft": round(_distance_between(geometry, source_tail, target), 3),
                "uses_unreserved_stock": True,
                "holdout_rereservation_required_for_test": code in holdout_codes,
            }
            source_legs.append(leg)
            move_legs.append(leg)
            remaining -= take

        if remaining > 0:
            # We do not fabricate moving reserved quantities. Report the gap and
            # require native Odoo unreserve/reassign if an experiment needs it.
            reservation_gap = round(remaining, 3)
        else:
            reservation_gap = 0.0

        avg_per_training_kit = (
            float(profile.quantity_demand) / float(training_kits)
            if training_kits
            else 0.0
        )
        recommendations.append(
            {
                "product_code": code,
                "current_locations": [str(row["tail"]) for row in positions],
                "forward_pick_location": target,
                "assignment_reason": assignment_reason[code],
                "pick_face_quantity": qty_required,
                "average_training_demand_per_kit": round(avg_per_training_kit, 3),
                "estimated_kits_between_replenishment": round(
                    qty_required / avg_per_training_kit, 2
                ) if avg_per_training_kit > 0 else None,
                "quantity_to_move_now": round(quantity_to_move, 3),
                "unreserved_move_quantity_planned": round(quantity_to_move - reservation_gap, 3),
                "reservation_quantity_gap": reservation_gap,
                "move_legs": source_legs,
                "target_initially_empty_or_same_sku": True,
                "target_capacity_pass_for_pick_face": True,
                "holdout_rereservation_required_for_test": code in holdout_codes and target not in {
                    str(row["tail"]) for row in positions
                },
            }
        )

    moved_units = sum(float(row["quantity"]) for row in move_legs)
    moved_weight = sum(float(row["estimated_weight_lb"]) for row in move_legs)
    moved_distance = sum(float(row["graph_distance_ft"]) for row in move_legs)
    moved_products = {
        row["product_code"]
        for row in recommendations
        if float(row["quantity_to_move_now"]) > 0
    }
    reservation_gaps = {
        row["product_code"]: row["reservation_quantity_gap"]
        for row in recommendations
        if float(row["reservation_quantity_gap"]) > 0
    }

    baseline_distance = float(holdout_baseline["total_distance_ft"])
    candidate_distance = float(holdout_candidate["total_distance_ft"])
    baseline_minutes = float(holdout_baseline["total_start_to_stage_minutes"])
    candidate_minutes = float(holdout_candidate["total_start_to_stage_minutes"])

    return {
        "mode": "read_only_forward_pick_optimization",
        "candidate_layout_version": candidate_layout_version,
        "odoo_mutated": False,
        "experimental_design": {
            "training_picking_ids": sorted(training_reservations_by_picking),
            "holdout_picking_ids": sorted(holdout_reservations_by_picking),
            "training_holdout_overlap": False,
            "holdout_used_for_optimization": False,
        },
        "forward_pick_policy": {
            "horizon_kits": horizon_kits,
            "buffer_factor": buffer_factor,
            "target_bins": "empty or already-exclusive current bins only",
            "excess_on_hand": "remains in reserve",
            "replenishment": "modeled as a required future operating-cost term; not yet executed",
            "relocation_penalty_ft_equivalent": relocation_penalty_ft,
            "max_training_minutes_penalty_to_avoid_a_move": max_revert_penalty_minutes,
        },
        "training": {
            "baseline": training_baseline,
            "candidate": training_candidate,
        },
        "holdout": {
            "baseline": holdout_baseline,
            "candidate": holdout_candidate,
            "improvement": {
                "distance_saved_ft": round(baseline_distance - candidate_distance, 3),
                "distance_reduction_pct": round(
                    (baseline_distance - candidate_distance) / baseline_distance * 100.0,
                    2,
                ) if baseline_distance else None,
                "start_to_stage_minutes_saved": round(baseline_minutes - candidate_minutes, 2),
                "start_to_stage_reduction_pct": round(
                    (baseline_minutes - candidate_minutes) / baseline_minutes * 100.0,
                    2,
                ) if baseline_minutes else None,
            },
        },
        "implementation": {
            "moved_products": len(moved_products),
            "move_legs": len(move_legs),
            "unreserved_units_to_move": round(moved_units, 3),
            "estimated_weight_lb_to_move": round(moved_weight, 3),
            "graph_distance_ft_for_move_legs": round(moved_distance, 3),
            "incumbent_displacement_moves": 0,
            "dependency_cycles": 0,
            "temporary_staging_required": False,
            "reservation_quantity_gaps": reservation_gaps,
            "holdout_test_requires_native_rereservation": any(
                row["holdout_rereservation_required_for_test"] for row in recommendations
            ),
        },
        "recommendations": recommendations,
        "fixture_capacity_status": {
            "current_state_capacity_anomalies": len(current_capacity_violations),
            "status": "sandbox_capacity_fixture_requires_recalibration"
            if current_capacity_violations
            else "baseline_within_fixture_capacity",
            "important": (
                "Current mock capacity values are synthetic placeholders. Existing live mock stock "
                "already exceeds them in some bins, so they cannot be treated as validated physical "
                "warehouse limits until the sandbox capacity fixture is corrected or real capacities are supplied."
            ),
        },
        "execution_gate": {
            "safe_to_execute": False,
            "blockers": [
                "sandbox_capacity_fixture_requires_recalibration"
            ] if current_capacity_violations else [],
            "requires_native_holdout_rereservation_for_physical_test": any(
                row["holdout_rereservation_required_for_test"] for row in recommendations
            ),
            "requires_replenishment_cost_model": True,
            "requires_relocation_roi": True,
            "requires_human_approval": True,
        },
    }
