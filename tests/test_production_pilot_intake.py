from app.services.production_pilot_intake import build_production_pilot_intake_packet


def _check(check_id: str, passed: bool, evidence=None, detail="detail"):
    return {
        "check_id": check_id,
        "passed": passed,
        "detail": detail,
        "evidence_reference": evidence,
    }


def test_all_blocked_readiness_becomes_actionable_intake_packet():
    readiness = {
        "status": "BLOCKED",
        "checks": [
            _check("read_only_odoo_access_approved", False),
            _check("geometry_field_verified", False),
            _check("material_handling_method_defined", False),
            _check("human_approval_owner_defined", False),
        ],
    }

    result = build_production_pilot_intake_packet(readiness)

    assert result["source_readiness_status"] == "BLOCKED"
    assert result["ready_to_request_read_only_pilot_approval"] is False
    assert result["summary"] == {
        "checks": 4,
        "complete": 0,
        "blocked": 4,
        "organizational_actions_remaining": 4,
    }
    assert result["odoo_mutated"] is False
    assert result["safe_to_execute_inventory_moves"] is False
    assert all(row["status"] == "BLOCKED" for row in result["items"])
    assert all(row["suggested_owner"] for row in result["items"])
    assert all(row["required_evidence"] for row in result["items"])
    assert all(row["next_action"] for row in result["items"])


def test_phase_summary_groups_checks_into_expected_workstreams():
    readiness = {
        "status": "BLOCKED",
        "checks": [
            _check("read_only_odoo_access_approved", True, "ACCESS-001"),
            _check("required_odoo_models_approved", False),
            _check("geometry_field_verified", False),
            _check("capacity_source_approved", True, "CAP-001"),
            _check("reservation_control_procedure_defined", False),
            _check("pilot_scope_owner_defined", False),
        ],
    }

    result = build_production_pilot_intake_packet(readiness)
    summary = {row["phase"]: row for row in result["phase_summary"]}

    assert summary["Access & Data Governance"] == {
        "phase": "Access & Data Governance",
        "checks": 2,
        "complete": 1,
        "blocked": 1,
    }
    assert summary["Physical Mapping & Metadata"] == {
        "phase": "Physical Mapping & Metadata",
        "checks": 2,
        "complete": 1,
        "blocked": 1,
    }
    assert summary["Warehouse Operating Controls"]["blocked"] == 1
    assert summary["Governance & Ownership"]["blocked"] == 1


def test_complete_readiness_allows_requesting_read_only_pilot_review_only():
    readiness = {
        "status": "READY_FOR_READ_ONLY_PRODUCTION_PILOT",
        "checks": [
            _check("read_only_odoo_access_approved", True, "ACCESS-001"),
            _check("geometry_field_verified", True, "MAP-001"),
            _check("human_approval_owner_defined", True, "Warehouse Manager"),
        ],
    }

    result = build_production_pilot_intake_packet(readiness)

    assert result["ready_to_request_read_only_pilot_approval"] is True
    assert result["summary"]["blocked"] == 0
    assert result["summary"]["organizational_actions_remaining"] == 0
    assert result["safe_to_execute_inventory_moves"] is False
    assert all(row["status"] == "COMPLETE" for row in result["items"])


def test_unknown_check_uses_safe_fallback_guidance():
    readiness = {
        "status": "BLOCKED",
        "checks": [_check("future_control_not_yet_mapped", False)],
    }

    result = build_production_pilot_intake_packet(readiness)
    row = result["items"][0]

    assert row["phase"] == "Other"
    assert row["suggested_owner"] == "Pilot Sponsor / Process Owner"
    assert row["status"] == "BLOCKED"
    assert result["phase_summary"] == [
        {"phase": "Other", "checks": 1, "complete": 0, "blocked": 1}
    ]


def test_current_evidence_reference_is_preserved_for_manager_packet():
    readiness = {
        "status": "BLOCKED",
        "checks": [
            _check(
                "odoo_write_access_disabled",
                False,
                "sandbox only; replace with production access-control evidence",
            )
        ],
    }

    result = build_production_pilot_intake_packet(readiness)
    row = result["items"][0]

    assert row["current_evidence_reference"] == (
        "sandbox only; replace with production access-control evidence"
    )
    assert row["status"] == "BLOCKED"
