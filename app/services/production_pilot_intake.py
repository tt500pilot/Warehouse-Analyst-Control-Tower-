from __future__ import annotations

from typing import Any, Mapping

Record = Mapping[str, Any]

CHECK_GUIDANCE: dict[str, dict[str, str]] = {
    "read_only_odoo_access_approved": {
        "phase": "Access & Data Governance",
        "suggested_owner": "Odoo / Business Systems + IT Security",
        "required_evidence": "Approved least-privilege production read-access request or access-control record",
        "next_action": "Define the AWIA service identity and approve read-only access to the agreed Odoo subset.",
    },
    "odoo_write_access_disabled": {
        "phase": "Access & Data Governance",
        "suggested_owner": "IT Security / Identity & Access Management",
        "required_evidence": "Role or permission evidence showing the pilot identity cannot perform warehouse writes",
        "next_action": "Verify the production pilot identity has no inventory, purchasing, scrap, or warehouse execution permissions.",
    },
    "required_odoo_models_approved": {
        "phase": "Access & Data Governance",
        "suggested_owner": "Warehouse Process Owner + Odoo / Business Systems",
        "required_evidence": "Approved production data-scope document naming the allowed Odoo models and fields",
        "next_action": "Approve the minimum warehouse data contract and explicitly list allowed models/fields.",
    },
    "data_classification_review_complete": {
        "phase": "Access & Data Governance",
        "suggested_owner": "IT Security / Export Control / Data Governance",
        "required_evidence": "Completed classification, confidentiality, and export-control review for the approved subset",
        "next_action": "Review whether the selected Odoo fields contain controlled, confidential, export-restricted, or otherwise sensitive information.",
    },
    "geometry_field_verified": {
        "phase": "Physical Mapping & Metadata",
        "suggested_owner": "Warehouse Operations / Industrial Engineering",
        "required_evidence": "Signed or approved field-measurement artifact for bins, stations, and legal travel paths",
        "next_action": "Measure the selected pilot area, validate coordinates/topology, and replace MOCK_FIXTURE provenance with FIELD_VERIFIED geometry.",
    },
    "capacity_source_approved": {
        "phase": "Physical Mapping & Metadata",
        "suggested_owner": "Warehouse Operations / Industrial Engineering / EHS as applicable",
        "required_evidence": "Approved bin unit/weight capacity source or controlled engineering reference",
        "next_action": "Document usable capacity limits for pilot bins and identify any equipment or safety restrictions.",
    },
    "product_physical_metadata_source_approved": {
        "phase": "Physical Mapping & Metadata",
        "suggested_owner": "Material Master / Engineering / Supply Chain Data Owner",
        "required_evidence": "Approved source for product weight and dimensions used in slotting feasibility",
        "next_action": "Define the authoritative weight/dimension source and resolve missing values for pilot SKUs.",
    },
    "material_handling_method_defined": {
        "phase": "Warehouse Operating Controls",
        "suggested_owner": "Warehouse Operations",
        "required_evidence": "Material-handling SOP or work instruction covering the pilot area and applicable equipment",
        "next_action": "Define how proposed relocations would be physically handled, including equipment and handling restrictions.",
    },
    "reservation_control_procedure_defined": {
        "phase": "Warehouse Operating Controls",
        "suggested_owner": "Inventory Control + Odoo / Business Systems",
        "required_evidence": "SOP or work instruction for reserved-stock release/reassignment during an approved relocation",
        "next_action": "Define how reservations are checked, released, reassigned, and restored without disrupting production demand.",
    },
    "traceability_relocation_workflow_defined": {
        "phase": "Warehouse Operating Controls",
        "suggested_owner": "Inventory Control + Quality",
        "required_evidence": "Approved lot/serial relocation, scan, verification, and reconciliation workflow",
        "next_action": "Define lot/serial controls, barcode scans, reconciliation, and exception handling for any future relocation.",
    },
    "flight_critical_policy_available": {
        "phase": "Warehouse Operating Controls",
        "suggested_owner": "Quality / Material Control / Engineering",
        "required_evidence": "Approved field, rule, or policy that resolves flight-critical storage/handling eligibility",
        "next_action": "Identify the authoritative flight-critical indicator or governing policy and make it available to the advisory analysis.",
    },
    "human_approval_owner_defined": {
        "phase": "Governance & Ownership",
        "suggested_owner": "Warehouse Leadership",
        "required_evidence": "Named role accountable for approving or rejecting future controlled pilot actions",
        "next_action": "Assign the role that owns final human approval; AWIA itself must never be the approver.",
    },
    "pilot_scope_owner_defined": {
        "phase": "Governance & Ownership",
        "suggested_owner": "Warehouse / Inventory Control Leadership",
        "required_evidence": "Named business owner plus documented pilot scope and success criteria",
        "next_action": "Name the pilot owner and define the contained area, duration, metrics, stop conditions, and review cadence.",
    },
}

PHASE_ORDER = [
    "Access & Data Governance",
    "Physical Mapping & Metadata",
    "Warehouse Operating Controls",
    "Governance & Ownership",
    "Other",
]


def build_production_pilot_intake_packet(readiness: Record) -> dict[str, Any]:
    checks = list(readiness.get("checks") or [])
    items: list[dict[str, Any]] = []

    for check in checks:
        check_id = str(check.get("check_id") or "unknown_check")
        guidance = CHECK_GUIDANCE.get(
            check_id,
            {
                "phase": "Other",
                "suggested_owner": "Pilot Sponsor / Process Owner",
                "required_evidence": "Documented approval or control evidence appropriate to this check",
                "next_action": "Assign an owner and document the evidence required to resolve this readiness check.",
            },
        )
        items.append(
            {
                "check_id": check_id,
                "phase": guidance["phase"],
                "status": "COMPLETE" if check.get("passed") else "BLOCKED",
                "passed": bool(check.get("passed")),
                "suggested_owner": guidance["suggested_owner"],
                "required_evidence": guidance["required_evidence"],
                "next_action": guidance["next_action"],
                "current_detail": check.get("detail"),
                "current_evidence_reference": check.get("evidence_reference"),
            }
        )

    phase_summary: list[dict[str, Any]] = []
    for phase in PHASE_ORDER:
        rows = [row for row in items if row["phase"] == phase]
        if not rows:
            continue
        phase_summary.append(
            {
                "phase": phase,
                "checks": len(rows),
                "complete": sum(1 for row in rows if row["passed"]),
                "blocked": sum(1 for row in rows if not row["passed"]),
            }
        )

    blocked = [row for row in items if not row["passed"]]
    return {
        "mode": "production_pilot_intake_packet",
        "source_readiness_status": readiness.get("status"),
        "odoo_mutated": False,
        "safe_to_execute_inventory_moves": False,
        "ready_to_request_read_only_pilot_approval": len(blocked) == 0,
        "summary": {
            "checks": len(items),
            "complete": len(items) - len(blocked),
            "blocked": len(blocked),
            "organizational_actions_remaining": len(blocked),
        },
        "phase_summary": phase_summary,
        "items": items,
        "guardrails": [
            "This packet organizes readiness work; it does not grant or verify organizational approval.",
            "Evidence references must point to real approved records and remain subject to the production readiness gate.",
            "A complete intake packet permits requesting a controlled read-only pilot review only; it never authorizes inventory movement.",
            "AWIA remains advisory and Odoo remains the system of record.",
        ],
    }
