from app.services.production_pilot_readiness import evaluate_production_pilot_readiness


def _geometry(status: str = "FIELD_VERIFIED"):
    return {"geometry": {"summary": {"measurement_statuses": [status]}}}


def _ready_config():
    return {
        "read_only_odoo_access_approved": True,
        "odoo_write_access_disabled": True,
        "approved_odoo_models": ["stock.location", "stock.quant", "stock.move.line"],
        "data_classification_review_complete": True,
        "approved_measurement_statuses": ["FIELD_VERIFIED"],
        "capacity_source_approved": True,
        "product_physical_metadata_source_approved": True,
        "material_handling_method_defined": True,
        "reservation_control_procedure_defined": True,
        "traceability_relocation_workflow_defined": True,
        "flight_critical_policy_available": True,
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


def test_readiness_never_authorizes_inventory_execution():
    result = evaluate_production_pilot_readiness(_geometry(), _ready_config())
    assert result["odoo_mutated"] is False
    assert result["safe_to_execute_inventory_moves"] is False
