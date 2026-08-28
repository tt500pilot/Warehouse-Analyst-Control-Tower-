from __future__ import annotations

from typing import Any, Mapping

Record = Mapping[str, Any]

REQUIRED_READ_MODELS = {
    "stock.location",
    "stock.quant",
    "stock.move.line",
}

PLACEHOLDER_EVIDENCE_MARKERS = (
    "sandbox",
    "example",
    "replace with",
    "placeholder",
    "todo",
    "tbd",
    "dummy",
    "test only",
)


def _bool(config: Record, key: str) -> bool:
    return bool(config.get(key))


def _text(config: Record, key: str) -> str:
    return str(config.get(key) or "").strip()


def _is_valid_evidence_reference(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    normalized = text.lower()
    return not any(marker in normalized for marker in PLACEHOLDER_EVIDENCE_MARKERS)


def _declared_with_evidence(config: Record, flag_key: str, evidence_key: str) -> bool:
    return _bool(config, flag_key) and _is_valid_evidence_reference(_text(config, evidence_key))


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
    only that prerequisite controls for a read-only pilot have been explicitly declared,
    non-placeholder evidence references are present, and geometry provenance is approved.
    """

    approved_models = {str(value) for value in config.get("approved_odoo_models") or []}
    approved_statuses = {
        str(value).strip()
        for value in config.get("approved_measurement_statuses") or ["FIELD_VERIFIED"]
        if str(value).strip()
    }
    geometry_statuses = _measurement_statuses(geometry)

    checks: list[dict[str, Any]] = []

    def add(check_id: str, passed: bool, detail: str, evidence_reference: str = "") -> None:
        checks.append(
            {
                "check_id": check_id,
                "passed": bool(passed),
                "detail": detail,
                "evidence_reference": evidence_reference or None,
            }
        )

    read_access_evidence = _text(config, "read_only_odoo_access_evidence")
    add(
        "read_only_odoo_access_approved",
        _declared_with_evidence(
            config, "read_only_odoo_access_approved", "read_only_odoo_access_evidence"
        ),
        "Approved read-only access to the production Odoo subset must be documented with a non-placeholder evidence reference.",
        read_access_evidence,
    )

    write_disabled_evidence = _text(config, "odoo_write_access_disabled_evidence")
    add(
        "odoo_write_access_disabled",
        _declared_with_evidence(
            config, "odoo_write_access_disabled", "odoo_write_access_disabled_evidence"
        ),
        "The pilot identity must not possess warehouse write permissions, and a non-placeholder access-control evidence reference must be recorded.",
        write_disabled_evidence,
    )

    missing_models = sorted(REQUIRED_READ_MODELS - approved_models)
    models_evidence = _text(config, "approved_odoo_models_evidence")
    models_pass = not missing_models and _is_valid_evidence_reference(models_evidence)
    add(
        "required_odoo_models_approved",
        models_pass,
        (
            "Approved read models include the minimum warehouse evidence set and a non-placeholder approval reference is recorded."
            if models_pass
            else (
                f"Missing approved models: {', '.join(missing_models)}"
                if missing_models
                else "Approved Odoo model list has no valid production approval evidence reference."
            )
        ),
        models_evidence,
    )

    classification_evidence = _text(config, "data_classification_review_evidence")
    add(
        "data_classification_review_complete",
        _declared_with_evidence(
            config,
            "data_classification_review_complete",
            "data_classification_review_evidence",
        ),
        "Data classification / export-control / confidentiality review must be complete and a non-placeholder approval reference recorded.",
        classification_evidence,
    )

    unapproved_statuses = sorted(set(geometry_statuses) - approved_statuses)
    geometry_evidence = _text(config, "geometry_verification_evidence")
    geometry_provenance_pass = bool(geometry_statuses) and not unapproved_statuses
    geometry_pass = geometry_provenance_pass and _is_valid_evidence_reference(
        geometry_evidence
    )
    if not geometry_statuses:
        geometry_detail = "Geometry artifact does not declare measurement provenance."
    elif unapproved_statuses:
        geometry_detail = f"Unapproved geometry measurement statuses: {', '.join(unapproved_statuses)}"
    elif not _is_valid_evidence_reference(geometry_evidence):
        geometry_detail = (
            f"Geometry statuses are approved ({', '.join(geometry_statuses)}), but no valid production field-verification evidence reference is recorded."
        )
    else:
        geometry_detail = f"Geometry measurement statuses approved: {', '.join(geometry_statuses)}"
    add(
        "geometry_field_verified",
        geometry_pass,
        geometry_detail,
        geometry_evidence,
    )

    evidence_backed_controls = [
        (
            "capacity_source_approved",
            "capacity_source_approved",
            "capacity_source_evidence",
            "Bin/unit/weight capacities must come from an approved field-verified or controlled engineering source with a non-placeholder evidence reference.",
        ),
        (
            "product_physical_metadata_source_approved",
            "product_physical_metadata_source_approved",
            "product_physical_metadata_source_evidence",
            "Weight/dimension metadata used for feasibility must come from an approved source with a non-placeholder evidence reference.",
        ),
        (
            "material_handling_method_defined",
            "material_handling_method_defined",
            "material_handling_method_reference",
            "The physical handling method and applicable equipment constraints must be defined and referenced before evaluating relocation labor.",
        ),
        (
            "reservation_control_procedure_defined",
            "reservation_control_procedure_defined",
            "reservation_control_procedure_reference",
            "A controlled process for reserved stock release/reassignment must be defined and referenced.",
        ),
        (
            "traceability_relocation_workflow_defined",
            "traceability_relocation_workflow_defined",
            "traceability_relocation_workflow_reference",
            "Lot/serial traceability and scan/reconciliation requirements for future relocation must be defined and referenced.",
        ),
        (
            "flight_critical_policy_available",
            "flight_critical_policy_available",
            "flight_critical_policy_reference",
            "The approved subset/policy must expose or otherwise resolve flight-critical eligibility, with the governing policy or field reference recorded.",
        ),
    ]
    for check_id, flag_key, evidence_key, detail in evidence_backed_controls:
        evidence = _text(config, evidence_key)
        add(
            check_id,
            _declared_with_evidence(config, flag_key, evidence_key),
            detail,
            evidence,
        )

    approval_owner = _text(config, "human_approval_owner")
    add(
        "human_approval_owner_defined",
        bool(approval_owner),
        "A named role, not an automated agent, must own approval of any future controlled pilot action.",
        approval_owner,
    )

    scope_owner = _text(config, "pilot_scope_owner")
    add(
        "pilot_scope_owner_defined",
        bool(scope_owner),
        "A warehouse/business owner must own the read-only pilot scope and success criteria.",
        scope_owner,
    )

    failed = [row for row in checks if not row["passed"]]
    status = "READY_FOR_READ_ONLY_PRODUCTION_PILOT" if not failed else "BLOCKED"

    return {
        "mode": "production_pilot_readiness_gate",
        "odoo_mutated": False,
        "safe_to_execute_inventory_moves": False,
        "status": status,
        "ready_for_read_only_production_pilot": status
        == "READY_FOR_READ_ONLY_PRODUCTION_PILOT",
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
            "Boolean declarations without evidence references are insufficient for evidence-backed production controls.",
            "Sandbox/example/TODO/TBD/placeholder evidence references are invalid for production readiness.",
            "Evidence references are audit pointers, not independent proof; they must correspond to real organizational approvals outside the software.",
            "The production pilot should use a least-privilege read-only identity and an explicitly approved data subset.",
        ],
    }
