from app.services.production_pilot_readiness import evaluate_production_pilot_readiness


def _geometry(status: str = "FIELD_VERIFIED"):
    return {"geometry": {"summary": {"measurement_statuses": [status]}}}


def _ready_config():
    return {
        "read_only_odoo_access_approved": True,
        "read_only_odoo_access_evidence": "ACCESS-APPROVAL-001",
        "odoo_write_access_disabled": True,
        "odoo_write_access_disabled_evidence": "IAM-READONLY-001",
        "approved_odoo_models": ["stock.location", "stock.quant", "stock.move.line"],
        "approved_odoo_models_evidence": "DATA-SCOPE-001",
        "data_classification_review_complete": True,
        "data_classification_review_evidence": "DATA-CLASS-001",
        "approved_measurement_statuses": ["FIELD_VERIFIED"],
        "geometry_verification_evidence": "WAREHOUSE-MAP-SIGNOFF-001",
        "capacity_source_approved": True,
        "capacity_source_evidence": "BIN-CAPACITY-SIGNOFF-001",
        "product_physical_metadata_source_approved": True,
        "product_physical_metadata_source_evidence": "PART-METADATA-SOURCE-001",
        "material_handling_method_defined": True,
        "material_handling_method_reference": "SOP-MATERIAL-HANDLING-001",
        "reservation_control_procedure_defined": True,
        "reservation_control_procedure_reference": "SOP-RESERVATION-CONTROL-001",
        "traceability_relocation_workflow_defined": True,
        "traceability_relocation_workflow_reference": "SOP-TRACEABILITY-RELOCATION-001",
        "flight_critical_policy_available": True,
        "flight_critical_policy_reference": "POLICY-FLIGHT-CRITICAL-001",
        "human_approval_owner": "Warehouse Manager",
        "pilot_scope_owner": "Inventory Control",
    }


def test_ready_when_all_read_only_pilot_controls_are_satisfied():
    result = evaluate_production_pilot_readiness(_geometry(), _ready_config())
    assert result["status"] == "READY_FOR_READ_ONLY_PRODUCTION_PILOT"
    assert result["ready_for_read_only_production_pilot"] is True
    assert result["safe_to_execute_inventory_moves"] is False
    assert result["summary"]["failed"] == 0


def test_mock_fixture_geometry_blocks_production_readiness():
    result = evaluate_production_pilot_readiness(_geometry("MOCK_FIXTURE"), _ready_config())
    assert result["status"] == "BLOCKED"
    assert result["ready_for_read_only_production_pilot"] is False
    assert "geometry_field_verified" in result["blocking_check_ids"]


def test_missing_required_odoo_model_blocks_readiness():
    config = _ready_config()
    config["approved_odoo_models"] = ["stock.location", "stock.quant"]
    result = evaluate_production_pilot_readiness(_geometry(), config)
    assert result["status"] == "BLOCKED"
    assert "required_odoo_models_approved" in result["blocking_check_ids"]


def test_missing_organizational_controls_block_readiness():
    config = _ready_config()
    config["data_classification_review_complete"] = False
    config["human_approval_owner"] = ""
    result = evaluate_production_pilot_readiness(_geometry(), config)
    assert result["status"] == "BLOCKED"
    assert "data_classification_review_complete" in result["blocking_check_ids"]
    assert "human_approval_owner_defined" in result["blocking_check_ids"]


def test_true_boolean_without_evidence_reference_still_blocks_readiness():
    config = _ready_config()
    config["capacity_source_evidence"] = ""
    result = evaluate_production_pilot_readiness(_geometry(), config)
    assert result["status"] == "BLOCKED"
    assert "capacity_source_approved" in result["blocking_check_ids"]
    capacity_check = next(
        row for row in result["checks"] if row["check_id"] == "capacity_source_approved"
    )
    assert capacity_check["passed"] is False
    assert capacity_check["evidence_reference"] is None


def test_geometry_status_requires_external_verification_evidence():
    config = _ready_config()
    config["geometry_verification_evidence"] = ""
    result = evaluate_production_pilot_readiness(_geometry(), config)
    assert result["status"] == "BLOCKED"
    assert "geometry_field_verified" in result["blocking_check_ids"]


def test_readiness_never_authorizes_inventory_execution():
    result = evaluate_production_pilot_readiness(_geometry(), _ready_config())
    assert result["odoo_mutated"] is False
    assert result["safe_to_execute_inventory_moves"] is False
