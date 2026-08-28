"""Decision gate for mapped-aisle relocation pilots.

Combines relocation readiness and economics sensitivity into an explicit
REJECT / DEFER / READY_FOR_CONTROLLED_PILOT classification. The service is
advisory only and never writes Odoo.

The decision is intentionally conservative:
- capacity failure => REJECT
- unresolved hard preconditions => DEFER
- undefined handling / live reservations / non-field-verified geometry => DEFER
- otherwise the selected setup/payback scenario must meet a configurable
  maximum payback threshold before a controlled pilot can be considered
- human approval is always required and is never auto-satisfied by this gate
"""

from __future__ import annotations

from typing import Any, Mapping

Record = Mapping[str, Any]


def _number(value: Any) -> float | None:
    if value in (None, False, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _scenario_for_setup(candidate: Record, setup_minutes: float) -> dict[str, Any] | None:
    scenarios = list(candidate.get("setup_sensitivity") or [])
    if not scenarios:
        return None
    return min(
        scenarios,
        key=lambda row: abs(float(row.get("hypothetical_total_setup_minutes") or 0.0) - setup_minutes),
    )


def evaluate_pilot_decision(
    readiness: Record,
    economics: Record,
    *,
    decision_setup_minutes: float = 15.0,
    max_payback_affected_pickings: float = 50.0,
    lookback_days: int = 90,
) -> dict[str, Any]:
    if decision_setup_minutes <= 0:
        raise ValueError("decision_setup_minutes must be positive")
    if max_payback_affected_pickings <= 0:
        raise ValueError("max_payback_affected_pickings must be positive")
    if lookback_days <= 0:
        raise ValueError("lookback_days must be positive")

    readiness_by_code = {
        str(row.get("product_code") or ""): row
        for row in readiness.get("recommendations") or []
        if row.get("product_code")
    }
    economics_by_code = {
        str(row.get("product_code") or ""): row
        for row in economics.get("candidates") or []
        if row.get("product_code")
    }
    rejected_codes = {
        str(row.get("product_code") or "")
        for row in economics.get("rejected_capacity_candidates") or []
        if row.get("product_code")
    }

    decisions: list[dict[str, Any]] = []
    all_codes = sorted(set(readiness_by_code) | set(economics_by_code) | rejected_codes)
    for code in all_codes:
        readiness_row = readiness_by_code.get(code, {})
        economics_row = economics_by_code.get(code, {})
        capacity_pass = bool(readiness_row.get("capacity_screen_pass"))
        blockers = list(readiness_row.get("execution_blockers") or [])
        hard_preconditions = list(economics_row.get("hard_preconditions_before_pilot") or [])

        selected_scenario = _scenario_for_setup(economics_row, decision_setup_minutes)
        payback_pickings = (
            _number(selected_scenario.get("payback_affected_pickings"))
            if selected_scenario is not None
            else None
        )
        observed_affected = int(economics_row.get("completed_pickings_with_product") or 0)
        payback_windows = (
            payback_pickings / observed_affected
            if payback_pickings is not None and observed_affected > 0
            else None
        )

        reasons: list[str] = []
        if not capacity_pass or code in rejected_codes:
            status = "REJECT"
            reasons.append("capacity_screen_failed")
        elif hard_preconditions:
            status = "DEFER"
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
                status = "DEFER"
                reasons.extend(f"operational_blocker:{item}" for item in operational_blockers)
            elif payback_pickings is None:
                status = "DEFER"
                reasons.append("payback_scenario_unavailable")
            elif payback_pickings > max_payback_affected_pickings:
                status = "DEFER"
                reasons.append("payback_exceeds_configured_threshold")
            else:
                status = "READY_FOR_CONTROLLED_PILOT"
                reasons.append("capacity_and_operational_gates_cleared")
                reasons.append("payback_within_configured_threshold")

        decisions.append(
            {
                "product_code": code,
                "product_name": readiness_row.get("product_name"),
                "decision": status,
                "reasons": reasons,
                "capacity_screen_pass": capacity_pass,
                "selected_decision_scenario": {
                    "requested_setup_minutes": decision_setup_minutes,
                    "matched_setup_minutes": (
                        _number(selected_scenario.get("hypothetical_total_setup_minutes"))
                        if selected_scenario is not None
                        else None
                    ),
                    "payback_affected_pickings": payback_pickings,
                    "maximum_allowed_payback_affected_pickings": max_payback_affected_pickings,
                },
                "observed_completed_affected_pickings_in_lookback": observed_affected,
                "lookback_days": lookback_days,
                "payback_multiple_of_observed_lookback_volume": (
                    round(payback_windows, 2) if payback_windows is not None else None
                ),
                "hard_preconditions_before_pilot": hard_preconditions,
                "execution_blockers": blockers,
                "human_approval_required": True,
                "safe_to_execute": False,
            }
        )

    counts = {
        "REJECT": sum(1 for row in decisions if row["decision"] == "REJECT"),
        "DEFER": sum(1 for row in decisions if row["decision"] == "DEFER"),
        "READY_FOR_CONTROLLED_PILOT": sum(
            1 for row in decisions if row["decision"] == "READY_FOR_CONTROLLED_PILOT"
        ),
    }

    return {
        "mode": "read_only_mapped_aisle_pilot_decision_gate",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "decision_policy": {
            "decision_setup_minutes": decision_setup_minutes,
            "max_payback_affected_pickings": max_payback_affected_pickings,
            "lookback_days": lookback_days,
            "capacity_failure": "REJECT",
            "hard_precondition_or_operational_blocker": "DEFER",
            "pilot_ready_rule": "all non-approval gates cleared and selected payback is within threshold",
            "approval": "human approval is always required after READY_FOR_CONTROLLED_PILOT",
        },
        "summary": counts,
        "decisions": decisions,
        "guardrails": [
            "READY_FOR_CONTROLLED_PILOT is not permission to move inventory.",
            "Payback is measured in modeled affected pickings, not calendar time or production ROI.",
            "Observed lookback volume is shown only as context and is not annualized.",
            "Synthetic MOCK_FIXTURE results must not be represented as Firefly production performance.",
            "No Odoo writes are performed.",
        ],
    }
