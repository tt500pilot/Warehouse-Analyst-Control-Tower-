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


def _count_decisions(summary: Record) -> int:
    return sum(
        int(summary.get(key, 0) or 0)
        for key in ("REJECT", "DEFER", "READY_FOR_CONTROLLED_PILOT")
    )


def build_optimization_management_summary(
    *,
    readiness: Record,
    route: Record,
    individual_decision: Record,
    package_decision: Record,
    traceability: Record | None = None,
    intake: Record | None = None,
) -> dict[str, Any]:
    """Translate technical optimization outputs into concise management language."""

    readiness_summary = readiness.get("summary") or {}
    route_primary = route.get("primary_result") or {}
    completed = route.get("completed_historical_validation") or {}
    individual_summary = individual_decision.get("summary") or {}
    package_summary = package_decision.get("summary") or {}
    trace_summary = (traceability or {}).get("summary") or {}
    intake_summary = (intake or {}).get("summary") or {}

    route_saved = _number(route_primary.get("modeled_distance_saved_ft"))
    route_reduction = _number(route_primary.get("modeled_distance_reduction_pct"))
    completed_pickings = int(completed.get("modeled_pickings", 0) or 0)

    individual_total = _count_decisions(individual_summary)
    package_total = _count_decisions(package_summary)
    individual_ready = int(individual_summary.get("READY_FOR_CONTROLLED_PILOT", 0) or 0)
    package_ready = int(package_summary.get("READY_FOR_CONTROLLED_PILOT", 0) or 0)

    checks = int(readiness_summary.get("checks", 0) or 0)
    passed = int(readiness_summary.get("passed", 0) or 0)
    actions_remaining = int(intake_summary.get("organizational_actions_remaining", 0) or 0)

    status = str(readiness.get("status") or "UNKNOWN")
    if status == "BLOCKED":
        headline = "Promising optimization opportunity, but not ready for production use."
        action_status = "DO NOT MOVE INVENTORY YET"
    elif readiness.get("ready_for_read_only_production_pilot"):
        headline = "Prerequisites are recorded for a controlled read-only production pilot."
        action_status = "READ-ONLY PILOT REVIEW CAN PROCEED"
    else:
        headline = "Optimization evidence is available, but production readiness is not yet confirmed."
        action_status = "HOLD FOR REVIEW"

    if route_saved is not None and route_reduction is not None:
        opportunity = (
            f"AWIA found a modeled opportunity to reduce travel in this mapped area by "
            f"{route_reduction:.2f}% ({route_saved:.0f} ft across {completed_pickings} completed modeled pickings)."
        )
    else:
        opportunity = "AWIA has not yet produced a complete modeled travel-savings result for this area."

    if individual_total or package_total:
        decision = (
            f"Nothing is automatically approved: {individual_ready} of {individual_total} individual recommendations "
            f"and {package_ready} of {package_total} co-pick packages are currently pilot-ready."
        )
    else:
        decision = "No pilot-ready relocation decisions are currently recorded for this area."

    if trace_summary:
        blocked_positions = int(trace_summary.get("blocked_positions", 0) or 0)
        blocked_products = int(trace_summary.get("blocked_products", 0) or 0)
        anonymous_qty = _number(trace_summary.get("anonymous_quantity")) or 0.0
        coverage = _number(trace_summary.get("traceability_coverage_pct"))
        if blocked_positions:
            traceability_message = (
                f"Traceability needs attention: {blocked_positions} tracked inventory position(s) across "
                f"{blocked_products} product(s) are blocked because lot/serial identity is incomplete "
                f"({anonymous_qty:.1f} anonymous tracked units)."
            )
        else:
            coverage_text = f"{coverage:.1f}%" if coverage is not None else "complete"
            traceability_message = (
                f"No anonymous tracked inventory is currently blocking this mapped area; traceability coverage is {coverage_text}."
            )
    else:
        traceability_message = "Area-scoped traceability status is not currently available."

    if checks:
        readiness_message = f"Production-readiness controls complete: {passed} of {checks}."
        if actions_remaining:
            readiness_message += f" {actions_remaining} management/operational action(s) remain."
    else:
        readiness_message = "Production-readiness controls have not yet been fully evaluated."

    if status == "BLOCKED":
        next_step = (
            "Management should focus on approvals and controls, not relocation execution: approve the read-only data scope, "
            "field-verify the mapped area and capacities, define handling/reservation/traceability procedures, and assign accountable owners."
        )
    else:
        next_step = (
            "Management should review the documented pilot scope, owners, safeguards, and success measures before authorizing any next phase."
        )

    return {
        "headline": headline,
        "action_status": action_status,
        "opportunity": opportunity,
        "decision": decision,
        "traceability": traceability_message,
        "readiness": readiness_message,
        "next_step": next_step,
        "footnote": "Modeled/sandbox results are decision-support evidence, not proof of production labor savings or authorization to move inventory.",
    }
