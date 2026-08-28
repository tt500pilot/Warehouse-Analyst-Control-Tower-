"""Deterministic read-only slotting optimization for the AWIA sandbox.

The optimizer never mutates Odoo. It learns a candidate product-to-bin mapping
from a training set of enriched kitting lines, then evaluates that frozen
assignment on a separate holdout set with the same virtual-picker model.

The candidate layout changes SKU assignment only. The physical mock-v1 graph,
aisles, legal paths, and kitting station remain unchanged. Any relocation is a
recommendation requiring separate capacity validation and human approval before
an Odoo stock move is ever considered.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.virtual_picker import (
    Geometry,
    PickerAssumptions,
    build_virtual_picker_plan,
    shortest_path,
)


@dataclass(frozen=True)
class ProductSlottingProfile:
    product_code: str
    category: str | None
    velocity_profile: str
    flight_critical: bool
    secure_required: bool
    line_frequency: int
    quantity_demand: float

    @property
    def priority_score(self) -> float:
        velocity_bonus = {"HIGH": 30.0, "MEDIUM": 15.0, "LOW": 0.0}.get(
            self.velocity_profile.upper(), 0.0
        )
        return (
            self.line_frequency * 100.0
            + self.quantity_demand * 2.0
            + (35.0 if self.flight_critical else 0.0)
            + (25.0 if self.secure_required else 0.0)
            + velocity_bonus
        )


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def load_slotting_product_metadata(data_dir: Path) -> dict[str, dict[str, Any]]:
    path = data_dir / "mock_odoo" / "products.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"Missing simulation product fixture {path}. Run scripts/generate_simulation_sandbox.py first."
        )
    result: dict[str, dict[str, Any]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            code = str(row.get("default_code") or "").strip()
            if not code:
                continue
            result[code] = {
                "default_code": code,
                "category": str(row.get("category") or "") or None,
                "velocity_profile": str(row.get("velocity_profile") or "MEDIUM").upper(),
                "flight_critical": _truthy(row.get("x_is_flight_critical")),
                "secure_required": _truthy(row.get("secure_required")),
                "weight_lb": float(row.get("weight_lb") or 0.0),
                "volume_ft3": float(row.get("volume_ft3") or 0.0),
            }
    return result


def _eligible(profile: ProductSlottingProfile, location: dict[str, Any]) -> bool:
    zone = str(location.get("zone") or "").upper()
    if profile.secure_required and not _truthy(location.get("secure")):
        return False
    if profile.flight_critical and not _truthy(location.get("flight_critical_allowed")):
        return False
    if profile.flight_critical and zone == "BULK":
        return False
    return True


def _location_rank(geometry: Geometry, tail: str, row: dict[str, Any]) -> tuple[float, str]:
    distance, _ = shortest_path(
        geometry.adjacency,
        geometry.kitting_node,
        str(row["graph_node_id"]),
    )
    level_penalty_ft = 14.0 if int(row.get("level") or 1) >= 2 else 0.0
    controlled_penalty_ft = 10.5 if str(row.get("zone") or "").upper() == "CONTROLLED" else 0.0
    return distance + level_penalty_ft + controlled_penalty_ft, tail


def build_profiles(
    reservations_by_picking: dict[int, list[dict[str, Any]]],
    product_metadata: dict[str, dict[str, Any]],
) -> dict[str, ProductSlottingProfile]:
    counts: dict[str, dict[str, float]] = {}
    for reservations in reservations_by_picking.values():
        for row in reservations:
            code = str(row.get("product_code") or "").strip()
            if not code:
                continue
            bucket = counts.setdefault(code, {"line_frequency": 0.0, "quantity_demand": 0.0})
            bucket["line_frequency"] += 1.0
            bucket["quantity_demand"] += float(row.get("quantity") or 0.0)

    profiles: dict[str, ProductSlottingProfile] = {}
    for code, demand in counts.items():
        metadata = product_metadata.get(code, {})
        profiles[code] = ProductSlottingProfile(
            product_code=code,
            category=metadata.get("category"),
            velocity_profile=str(metadata.get("velocity_profile") or "MEDIUM"),
            flight_critical=bool(metadata.get("flight_critical", False)),
            secure_required=bool(metadata.get("secure_required", False)),
            line_frequency=int(demand["line_frequency"]),
            quantity_demand=float(demand["quantity_demand"]),
        )
    return profiles


def _apply_assignment(
    reservations_by_picking: dict[int, list[dict[str, Any]]],
    geometry: Geometry,
    assignment: dict[str, str],
) -> dict[int, list[dict[str, Any]]]:
    remapped: dict[int, list[dict[str, Any]]] = {}
    for picking_id, reservations in reservations_by_picking.items():
        rows: list[dict[str, Any]] = []
        for original in reservations:
            row = dict(original)
            code = str(row.get("product_code") or "")
            target_tail = assignment.get(code, str(row.get("location_tail") or ""))
            location = geometry.locations_by_tail[target_tail]
            row["location_tail"] = target_tail
            row["source_location"] = f"CANDIDATE/{target_tail}"
            row["zone"] = str(location.get("zone") or "")
            row["level"] = int(location.get("level") or 1)
            row["graph_node_id"] = str(location["graph_node_id"])
            rows.append(row)
        remapped[picking_id] = rows
    return remapped


def evaluate_layout(
    reservations_by_picking: dict[int, list[dict[str, Any]]],
    geometry: Geometry,
    *,
    assumptions: PickerAssumptions | None = None,
    seed: int = 42,
) -> dict[str, Any]:
    assumptions = assumptions or PickerAssumptions()
    per_picking: dict[int, dict[str, Any]] = {}
    total_distance = 0.0
    total_minutes = 0.0
    for picking_id in sorted(reservations_by_picking):
        plan = build_virtual_picker_plan(
            reservations_by_picking[picking_id],
            geometry,
            picking_id=picking_id,
            assumptions=assumptions,
            seed=seed,
        )
        summary = plan["summary"]
        per_picking[picking_id] = summary
        total_distance += float(summary["total_distance_ft"])
        total_minutes += float(summary["simulated_start_to_stage_minutes"])
    count = len(per_picking)
    return {
        "pickings": count,
        "total_distance_ft": round(total_distance, 3),
        "average_distance_ft": round(total_distance / count, 3) if count else None,
        "total_start_to_stage_minutes": round(total_minutes, 2),
        "average_start_to_stage_minutes": round(total_minutes / count, 2) if count else None,
        "per_picking": per_picking,
    }


def _objective(evaluation: dict[str, Any]) -> tuple[float, float]:
    return (
        float(evaluation["total_start_to_stage_minutes"]),
        float(evaluation["total_distance_ft"]),
    )


def optimize_slotting_layout(
    training_reservations_by_picking: dict[int, list[dict[str, Any]]],
    holdout_reservations_by_picking: dict[int, list[dict[str, Any]]],
    geometry: Geometry,
    product_metadata: dict[str, dict[str, Any]],
    *,
    assumptions: PickerAssumptions | None = None,
    seed: int = 42,
    candidate_layout_version: str = "mock-v2-candidate-slotting-v2",
) -> dict[str, Any]:
    if not training_reservations_by_picking:
        raise ValueError("At least one training picking is required.")
    if not holdout_reservations_by_picking:
        raise ValueError("At least one holdout picking is required.")
    overlap = set(training_reservations_by_picking) & set(holdout_reservations_by_picking)
    if overlap:
        raise ValueError(f"Training and holdout picking IDs must be disjoint; overlap={sorted(overlap)}")

    profiles = build_profiles(training_reservations_by_picking, product_metadata)
    if not profiles:
        raise ValueError("No product codes were available for slotting optimization.")

    location_items = list(geometry.locations_by_tail.items())
    ranked_locations = sorted(
        location_items,
        key=lambda item: _location_rank(geometry, item[0], item[1]),
    )
    profile_order = sorted(
        profiles.values(),
        key=lambda profile: (-profile.priority_score, profile.product_code),
    )

    assignment: dict[str, str] = {}
    used: set[str] = set()
    for profile in profile_order:
        eligible = [
            (tail, row)
            for tail, row in ranked_locations
            if tail not in used and _eligible(profile, row)
        ]
        if not eligible:
            raise RuntimeError(f"No eligible candidate bin for {profile.product_code}.")
        target_tail = eligible[0][0]
        assignment[profile.product_code] = target_tail
        used.add(target_tail)

    training_candidate_rows = _apply_assignment(
        training_reservations_by_picking,
        geometry,
        assignment,
    )
    training_candidate = evaluate_layout(
        training_candidate_rows,
        geometry,
        assumptions=assumptions,
        seed=seed,
    )

    # Pairwise hill-climb is TRAINING ONLY. Holdout data is never consulted
    # during assignment or swap selection.
    improved = True
    passes = 0
    while improved and passes < 20:
        improved = False
        passes += 1
        current_objective = _objective(training_candidate)
        best_assignment: dict[str, str] | None = None
        best_training_candidate: dict[str, Any] | None = None
        best_objective = current_objective
        codes = sorted(assignment)
        for left_index, left_code in enumerate(codes):
            for right_code in codes[left_index + 1 :]:
                left_tail = assignment[left_code]
                right_tail = assignment[right_code]
                if not _eligible(profiles[left_code], geometry.locations_by_tail[right_tail]):
                    continue
                if not _eligible(profiles[right_code], geometry.locations_by_tail[left_tail]):
                    continue
                trial = dict(assignment)
                trial[left_code], trial[right_code] = right_tail, left_tail
                trial_rows = _apply_assignment(training_reservations_by_picking, geometry, trial)
                trial_evaluation = evaluate_layout(
                    trial_rows,
                    geometry,
                    assumptions=assumptions,
                    seed=seed,
                )
                objective = _objective(trial_evaluation)
                if objective < best_objective:
                    best_objective = objective
                    best_assignment = trial
                    best_training_candidate = trial_evaluation
        if best_assignment is not None and best_training_candidate is not None:
            assignment = best_assignment
            training_candidate = best_training_candidate
            improved = True

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
    holdout_candidate_rows = _apply_assignment(
        holdout_reservations_by_picking,
        geometry,
        assignment,
    )
    holdout_candidate = evaluate_layout(
        holdout_candidate_rows,
        geometry,
        assumptions=assumptions,
        seed=seed,
    )

    training_codes = set(profiles)
    holdout_codes = {
        str(row.get("product_code") or "")
        for rows in holdout_reservations_by_picking.values()
        for row in rows
        if row.get("product_code")
    }
    unseen_holdout_codes = sorted(holdout_codes - training_codes)

    current_locations: dict[str, set[str]] = {}
    for reservations in training_reservations_by_picking.values():
        for row in reservations:
            code = str(row.get("product_code") or "")
            current_locations.setdefault(code, set()).add(str(row.get("location_tail") or ""))

    recommendations: list[dict[str, Any]] = []
    for profile in profile_order:
        target = assignment[profile.product_code]
        target_row = geometry.locations_by_tail[target]
        current = sorted(location for location in current_locations.get(profile.product_code, set()) if location)
        recommendations.append(
            {
                "product_code": profile.product_code,
                "category": profile.category,
                "velocity_profile": profile.velocity_profile,
                "flight_critical": profile.flight_critical,
                "secure_required": profile.secure_required,
                "training_line_frequency": profile.line_frequency,
                "training_quantity_demand": profile.quantity_demand,
                "priority_score": round(profile.priority_score, 2),
                "current_locations": current,
                "candidate_location": target,
                "candidate_zone": target_row.get("zone"),
                "candidate_level": int(target_row.get("level") or 1),
                "candidate_pick_tier": target_row.get("pick_tier"),
                "relocation_recommended": current != [target],
            }
        )

    distance_saved = float(holdout_baseline["total_distance_ft"]) - float(
        holdout_candidate["total_distance_ft"]
    )
    minutes_saved = float(holdout_baseline["total_start_to_stage_minutes"]) - float(
        holdout_candidate["total_start_to_stage_minutes"]
    )
    baseline_distance = float(holdout_baseline["total_distance_ft"])
    baseline_minutes = float(holdout_baseline["total_start_to_stage_minutes"])

    return {
        "mode": "read_only_simulation",
        "baseline_layout_version": "mock-v1",
        "candidate_layout_version": candidate_layout_version,
        "physical_graph_changed": False,
        "odoo_mutated": False,
        "experimental_design": {
            "training_picking_ids": sorted(training_reservations_by_picking),
            "holdout_picking_ids": sorted(holdout_reservations_by_picking),
            "training_holdout_overlap": False,
            "holdout_used_for_optimization": False,
            "unseen_holdout_product_codes": unseen_holdout_codes,
            "holdout_product_coverage_pct": round(
                ((len(holdout_codes) - len(unseen_holdout_codes)) / len(holdout_codes) * 100.0), 2
            ) if holdout_codes else None,
        },
        "method": {
            "initial_assignment": "training demand/criticality priority into nearest eligible unique bins",
            "improvement": "deterministic pairwise swaps scored only on training virtual-picker objective",
            "objective_order": ["total_start_to_stage_minutes", "total_distance_ft"],
            "hill_climb_passes": passes,
        },
        "training": {
            "baseline": training_baseline,
            "candidate": training_candidate,
        },
        "holdout": {
            "baseline": holdout_baseline,
            "candidate": holdout_candidate,
            "improvement": {
                "distance_saved_ft": round(distance_saved, 3),
                "distance_reduction_pct": round(distance_saved / baseline_distance * 100.0, 2)
                if baseline_distance
                else None,
                "start_to_stage_minutes_saved": round(minutes_saved, 2),
                "start_to_stage_reduction_pct": round(minutes_saved / baseline_minutes * 100.0, 2)
                if baseline_minutes
                else None,
            },
        },
        "recommendations": recommendations,
        "guardrails": {
            "flight_critical_excluded_from_bulk": True,
            "secure_products_require_secure_bins": True,
            "unique_candidate_bin_per_product": True,
            "capacity_validation_before_execution": True,
            "live_occupancy_validation_before_execution": True,
            "relocation_cost_not_yet_modeled": True,
            "human_approval_before_odoo_move": True,
        },
    }
