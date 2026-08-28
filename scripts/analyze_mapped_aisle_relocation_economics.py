from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    return float(value)


def _parse_scenarios(value: str) -> list[float]:
    result: list[float] = []
    for item in value.split(","):
        text = item.strip()
        if not text:
            continue
        number = float(text)
        if number <= 0:
            raise ValueError("setup-minute scenarios must be positive")
        result.append(number)
    if not result:
        raise ValueError("at least one setup-minute scenario is required")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Sensitivity-based economics for capacity-screened mapped-aisle relocations. "
            "This is advisory modeling only and does not write Odoo."
        )
    )
    parser.add_argument(
        "--readiness",
        default="data/analysis/aisle-b-relocation-readiness.json",
    )
    parser.add_argument(
        "--route-validation",
        default="data/analysis/aisle-b-route-validation.json",
    )
    parser.add_argument(
        "--setup-minutes",
        default="5,15,30,60",
        help="Comma-separated hypothetical total relocation/setup times to test.",
    )
    parser.add_argument(
        "--output",
        default="data/analysis/aisle-b-relocation-economics.json",
    )
    args = parser.parse_args()

    readiness_path = Path(args.readiness)
    route_path = Path(args.route_validation)
    if not readiness_path.exists():
        raise FileNotFoundError(readiness_path)
    if not route_path.exists():
        raise FileNotFoundError(route_path)

    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    route = json.loads(route_path.read_text(encoding="utf-8"))
    scenarios = _parse_scenarios(args.setup_minutes)

    walking_speed = _number(readiness.get("walking_speed_ft_s_for_equivalent_only")) or 3.5
    summary = readiness.get("summary") or {}
    completed = route.get("completed_historical_validation") or {}
    completed_pickings = int(completed.get("modeled_pickings") or 0)
    completed_coverage = {
        int(row["product_id"]): int(row.get("pickings_with_product") or 0)
        for row in completed.get("recommendation_coverage") or []
        if row.get("product_id") is not None
    }

    screened_saved_ft = _number(summary.get("capacity_screened_subset_route_saved_ft"))
    avg_saved_ft_per_completed = screened_saved_ft / completed_pickings if completed_pickings else 0.0
    avg_saved_minutes_per_completed = (
        avg_saved_ft_per_completed / walking_speed / 60.0 if walking_speed > 0 else 0.0
    )

    scenario_rows = []
    for setup_minutes in scenarios:
        scenario_rows.append(
            {
                "hypothetical_total_setup_minutes": setup_minutes,
                "payback_completed_mapped_aisle_pickings": round(
                    setup_minutes / avg_saved_minutes_per_completed, 1
                )
                if avg_saved_minutes_per_completed > 0
                else None,
                "note": (
                    "Payback uses modeled walking-distance benefit averaged across all completed mapped-aisle pickings; "
                    "it is not calendar time, whole-kit labor, or a production ROI forecast."
                ),
            }
        )

    candidate_rows = []
    for row in readiness.get("recommendations") or []:
        if not row.get("capacity_screen_pass"):
            continue
        product_id = int(row["product_id"])
        benefit = row.get("completed_route_benefit") or {}
        attributable_saved_ft = _number(
            benefit.get("attributable_modeled_aisle_subroute_distance_saved_ft")
        )
        affected_pickings = completed_coverage.get(product_id, 0)
        saved_ft_per_affected = (
            attributable_saved_ft / affected_pickings if affected_pickings else 0.0
        )
        saved_minutes_per_affected = (
            saved_ft_per_affected / walking_speed / 60.0 if walking_speed > 0 else 0.0
        )
        relocation = row.get("relocation_geometry") or {}
        relocation_distance_ft = _number(relocation.get("legal_transfer_distance_ft"))
        walking_only_lower_bound_minutes = relocation_distance_ft / walking_speed / 60.0

        blockers = list(row.get("execution_blockers") or [])
        hard_preconditions = [
            blocker
            for blocker in blockers
            if blocker
            in {
                "tracked_stock_not_fully_lot_or_serial_identified",
                "target_unit_capacity_not_proven",
                "target_weight_capacity_not_proven",
                "target_no_longer_empty",
                "approved_product_physical_metadata_missing",
            }
        ]

        candidate_rows.append(
            {
                "product_id": product_id,
                "product_code": row.get("product_code"),
                "capacity_screen_pass": True,
                "completed_pickings_with_product": affected_pickings,
                "attributable_completed_aisle_subroute_saved_ft": round(
                    attributable_saved_ft, 3
                ),
                "modeled_saved_ft_per_affected_picking": round(saved_ft_per_affected, 3),
                "walking_only_saved_minutes_per_affected_picking": round(
                    saved_minutes_per_affected, 4
                ),
                "relocation_legal_transfer_distance_ft": round(relocation_distance_ft, 3),
                "relocation_walk_only_lower_bound_minutes": round(
                    walking_only_lower_bound_minutes, 4
                ),
                "execution_blockers": blockers,
                "hard_preconditions_before_pilot": hard_preconditions,
                "economics_status": (
                    "hypothetical_only_until_hard_preconditions_are_cleared"
                    if hard_preconditions
                    else "sensitivity_only_not_execution_ready"
                ),
                "setup_sensitivity": [
                    {
                        "hypothetical_total_setup_minutes": setup_minutes,
                        "payback_affected_pickings": round(
                            setup_minutes / saved_minutes_per_affected, 1
                        )
                        if saved_minutes_per_affected > 0
                        else None,
                    }
                    for setup_minutes in scenarios
                ],
            }
        )

    result = {
        "mode": "read_only_mapped_aisle_relocation_economics_sensitivity",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "classification": "hypothetical_sensitivity_not_production_roi",
        "basis": {
            "capacity_screened_recommendations_only": True,
            "completed_modeled_pickings": completed_pickings,
            "capacity_screened_subset_saved_ft": round(screened_saved_ft, 3),
            "average_saved_ft_per_completed_mapped_aisle_picking": round(
                avg_saved_ft_per_completed, 3
            ),
            "walking_only_saved_minutes_per_completed_mapped_aisle_picking": round(
                avg_saved_minutes_per_completed, 4
            ),
            "walking_speed_ft_s": walking_speed,
            "annualization_performed": False,
        },
        "portfolio_setup_sensitivity": scenario_rows,
        "candidates": candidate_rows,
        "rejected_capacity_candidates": [
            {
                "product_code": row.get("product_code"),
                "reason": "failed_capacity_screen",
                "execution_blockers": row.get("execution_blockers") or [],
            }
            for row in readiness.get("recommendations") or []
            if not row.get("capacity_screen_pass")
        ],
        "guardrails": [
            f"No calendar-time or annual ROI is calculated from the {completed_pickings}-picking completed modeled sample.",
            "Walking-only savings exclude search, handling, scanning, congestion, equipment, and whole-kit route effects.",
            "Setup time is intentionally a sensitivity input because the material-handling method is not defined.",
            "A tracked product with anonymous stock must clear traceability before any relocation pilot is considered.",
            "MOCK_FIXTURE geometry, capacities, and product physical metadata are not production sources of truth.",
            "No Odoo writes are performed.",
        ],
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
