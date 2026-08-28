from __future__ import annotations

from typing import Any, Mapping

Record = Mapping[str, Any]

REQUIRED_READ_MODELS = {
    "stock.location",
    "stock.quant",
    "stock.move.line",
}


def _bool(config: Record, key: str) -> bool:
    return bool(config.get(key))


def _measurement_statuses(geometry: Record) -> list[str]:
    payload = geometry.get("geometry") or geometry
    summary = payload.get("summary") or {}
    statuses = summary.get("measurement_statuses") or []
    if isinstance(statuses, str):
        statuses = [statuses]
    return sorted({str(value).strip() for value in statuses if str(value).strip()})


def evaluate_production_pilot_readiness(
    geometry: Record,
    config: Record,
) -> dict[str, Any]:
    """Evaluate whether a mapped area is ready for a controlled read-only production pilot.

    This gate never authorizes inventory movement or Odoo writes. A READY result means
    only that the prerequisite controls for a read-only pilot have been explicitly
    declared and that the geometry artifact does not contain unapproved provenance.
    """

    approved_models = {str(value) for value in config.get("approved_odoo_models") or []}
    approved_statuses = {
        str(value).strip()
        for value in config.get("approved_measurement_statuses") or ["FIELD_VERIFIED"]
        if str(value).strip()
    }
    geometry_statuses = _measurement_statuses(geometry)

    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: str) -> None:
        checks.append({"check_id": check_id, "passed": bool(passed), "detail": detail})

    add(
        "read_only_odoo_access_approved",
        _bool(config, "read_only_odoo_access_approved"),
        "Approved read-only access to the production Odoo subset must be documented.",
    )
    add(
        "odoo_write_access_disabled",
        _bool(config, "odoo_write_access_disabled"),
        "The pilot identity must not possess warehouse write permissions.",
    )
    missing_models = sorted(REQUIRED_READ_MODELS - approved_models)
    add(
        "required_odoo_models_approved",
        not missing_models,
        (
            "Approved read models include the minimum warehouse evidence set."
            if not missing_models
            else f"Missing approved models: {', '.join(missing_models)}"
        ),
    )
    add(
        "data_classification_review_complete",
        _bool(config, "data_classification_review_complete"),
        "Data classification / export-control / confidentiality review must be complete for the approved subset.",
    )

    unapproved_statuses = sorted(set(geometry_statuses) - approved_statuses)
    add(
        "geometry_field_verified",
        bool(geometry_statuses) and not unapproved_statuses,
        (
            f"Geometry measurement statuses approved: {', '.join(geometry_statuses)}"
            if geometry_statuses and not unapproved_statuses
            else (
                f"Unapproved geometry measurement statuses: {', '.join(unapproved_statuses)}"
                if unapproved_statuses
                else "Geometry artifact does not declare measurement provenance."
            )
        ),
    )
    add(
        "capacity_source_approved",
        _bool(config, "capacity_source_approved"),
        "Bin/unit/weight capacities must come from an approved field-verified or controlled engineering source.",
    )
    add(
        "product_physical_metadata_source_approved",
        _bool(config, "product_physical_metadata_source_approved"),
        "Weight/dimension metadata used for feasibility must come from an approved source.",
    )
    add(
        "material_handling_method_defined",
        _bool(config, "material_handling_method_defined"),
        "The physical handling method and applicable equipment constraints must be defined before evaluating relocation labor.",
    )
    add(
        "reservation_control_procedure_defined",
        _bool(config, "reservation_control_procedure_defined"),
        "A controlled process for reserved stock release/reassignment must be defined.",
    )
    add(
        "traceability_relocation_workflow_defined",
        _bool(config, "traceability_relocation_workflow_defined"),
        "Lot/serial traceability and scan/reconciliation requirements for any future relocation must be defined.",
    )
    add(
        "flight_critical_policy_available",
        _bool(config, "flight_critical_policy_available"),
        "The approved subset/policy must expose or otherwise resolve flight-critical eligibility before production recommendations are trusted.",
    )
    add(
        "human_approval_owner_defined",
        bool(str(config.get("human_approval_owner") or "").strip()),
        "A named role, not an automated agent, must own approval of any future controlled pilot action.",
    )
    add(
        "pilot_scope_owner_defined",
        bool(str(config.get("pilot_scope_owner") or "").strip()),
        "A warehouse/business owner must own the read-only pilot scope and success criteria.",
    )

    failed = [row for row in checks if not row["passed"]]
    status = "READY_FOR_READ_ONLY_PRODUCTION_PILOT" if not failed else "BLOCKED"

    return {
        "mode": "production_pilot_readiness_gate",
        "odoo_mutated": False,
        "safe_to_execute_inventory_moves": False,
        "status": status,
        "ready_for_read_only_production_pilot": status == "READY_FOR_READ_ONLY_PRODUCTION_PILOT",
        "geometry_measurement_statuses": geometry_statuses,
        "approved_measurement_statuses": sorted(approved_statuses),
        "approved_odoo_models": sorted(approved_models),
        "summary": {
            "checks": len(checks),
            "passed": len(checks) - len(failed),
            "failed": len(failed),
        },
        "checks": checks,
        "blocking_check_ids": [row["check_id"] for row in failed],
        "guardrails": [
            "READY_FOR_READ_ONLY_PRODUCTION_PILOT does not authorize inventory movement, Odoo writes, purchasing, scrapping, or vendor communication.",
            "MOCK_FIXTURE or other unapproved measurement provenance must block production-pilot readiness.",
            "Approval declarations in the config are evidence inputs and must correspond to real organizational approvals outside the software.",
            "The production pilot should use a least-privilege read-only identity and an explicitly approved data subset.",
        ],
    }
