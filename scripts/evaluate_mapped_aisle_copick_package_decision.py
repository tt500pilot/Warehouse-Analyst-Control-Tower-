from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _number(value: Any) -> float | None:
    if value in (None, False, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scenario_for_setup(package: dict[str, Any], setup_minutes: float) -> dict[str, Any] | None:
    scenarios = list(package.get("setup_sensitivity") or [])
    if not scenarios:
        return None
    return min(
        scenarios,
        key=lambda row: abs(
            float(row.get("hypothetical_total_package_setup_minutes") or 0.0) - setup_minutes
        ),
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate REJECT/DEFER/READY_FOR_CONTROLLED_PILOT for mapped-aisle co-pick packages. "
            "Package readiness remains advisory and never authorizes inventory movement."
        )
    )
    parser.add_argument("--package-economics", required=True)
    parser.add_argument("--setup-minutes", type=float, default=15.0)
    parser.add_argument("--max-payback-pickings", type=float, default=50.0)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    if args.setup_minutes <= 0:
        raise ValueError("--setup-minutes must be greater than zero")
    if args.max_payback_pickings <= 0:
        raise ValueError("--max-payback-pickings must be greater than zero")
    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero")

    source_path = Path(args.package_economics)
    if not source_path.exists():
        raise FileNotFoundError(source_path)
    payload = json.loads(source_path.read_text(encoding="utf-8"))

    reconciliation_difference = abs(
        float((payload.get("summary") or {}).get("shared_savings_reconciliation_difference_ft") or 0.0)
    )

    decisions: list[dict[str, Any]] = []
    for package in payload.get("packages") or []:
        scenario = _scenario_for_setup(package, args.setup_minutes)
        payback = (
            _number(scenario.get("payback_affected_pickings")) if scenario is not None else None
        )
        observed = int(package.get("completed_affected_pickings") or 0)
        package_saved_ft = float(package.get("package_modeled_route_saved_ft") or 0.0)
        hard_preconditions = list(package.get("hard_preconditions_before_pilot") or [])
        blockers = list(package.get("execution_blockers") or [])

        reasons: list[str] = []
        if package_saved_ft <= 0:
            decision = "REJECT"
            reasons.append("no_positive_completed_package_route_benefit")
        elif reconciliation_difference > 0.001:
            decision = "DEFER"
            reasons.append("shared_savings_reconciliation_not_clean")
        elif hard_preconditions:
            decision = "DEFER"
            reasons.extend(f"hard_precondition:{item}" for item in hard_preconditions)
        else:
            operational_blockers = [
                item
                for item in blockers
                if item
                in {
                    "material_handling_method_not_defined",
                    "live_reservations_require_controlled_release_or_reassignment",
                    "geometry_and_capacity_not_field_verified",
                    "target_no_longer_empty",
                    "approved_product_physical_metadata_missing",
                    "lot_or_serial_relocation_workflow_required",
                }
            ]
            if operational_blockers:
                decision = "DEFER"
                reasons.extend(f"operational_blocker:{item}" for item in operational_blockers)
            elif payback is None:
                decision = "DEFER"
                reasons.append("package_payback_scenario_unavailable")
            elif payback > args.max_payback_pickings:
                decision = "DEFER"
                reasons.append("package_payback_exceeds_configured_threshold")
            else:
                decision = "READY_FOR_CONTROLLED_PILOT"
                reasons.append("package_operational_gates_cleared")
                reasons.append("package_payback_within_configured_threshold")

        payback_multiple = (
            payback / observed if payback is not None and observed > 0 else None
        )
        decisions.append(
            {
                "package_id": package.get("package_id"),
                "product_codes": package.get("product_codes") or [],
                "decision": decision,
                "reasons": reasons,
                "package_modeled_route_saved_ft": package_saved_ft,
                "shared_joint_route_saved_ft": package.get("shared_joint_route_saved_ft"),
                "selected_decision_scenario": {
                    "requested_total_package_setup_minutes": args.setup_minutes,
                    "matched_total_package_setup_minutes": (
                        _number(scenario.get("hypothetical_total_package_setup_minutes"))
                        if scenario is not None
                        else None
                    ),
                    "payback_affected_pickings": payback,
                    "maximum_allowed_payback_affected_pickings": args.max_payback_pickings,
                },
                "observed_completed_affected_pickings_in_lookback": observed,
                "lookback_days": args.lookback_days,
                "payback_multiple_of_observed_lookback_volume": (
                    round(payback_multiple, 2) if payback_multiple is not None else None
                ),
                "hard_preconditions_before_pilot": hard_preconditions,
                "execution_blockers": blockers,
                "human_approval_required": True,
                "safe_to_execute": False,
            }
        )

    summary = {
        "REJECT": sum(1 for row in decisions if row["decision"] == "REJECT"),
        "DEFER": sum(1 for row in decisions if row["decision"] == "DEFER"),
        "READY_FOR_CONTROLLED_PILOT": sum(
            1 for row in decisions if row["decision"] == "READY_FOR_CONTROLLED_PILOT"
        ),
    }
    result = {
        "mode": "read_only_mapped_aisle_copick_package_pilot_decision_gate",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "decision_policy": {
            "decision_total_package_setup_minutes": args.setup_minutes,
            "max_payback_affected_pickings": args.max_payback_pickings,
            "lookback_days": args.lookback_days,
            "reconciliation_tolerance_ft": 0.001,
            "approval": "human approval is always required after READY_FOR_CONTROLLED_PILOT",
        },
        "summary": summary,
        "decisions": decisions,
        "guardrails": [
            "Package decisions preserve shared co-pick savings at the package level and never allocate them arbitrarily to individual SKUs.",
            "READY_FOR_CONTROLLED_PILOT is not permission to relocate inventory.",
            "Payback is measured in modeled affected pickings, not calendar time or production ROI.",
            "Observed lookback volume is context only and is not annualized.",
            "Synthetic MOCK_FIXTURE results must not be represented as Firefly production performance.",
            "No Odoo writes are performed.",
        ],
        "package_economics_file": str(source_path),
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
