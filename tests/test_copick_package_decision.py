from app.services.copick_package_decision import evaluate_copick_package_decision


def _payload(*, saved_ft=192.0, reconciliation=0.0, blockers=None, hard=None, payback=32.8):
    return {
        "summary": {"shared_savings_reconciliation_difference_ft": reconciliation},
        "packages": [
            {
                "package_id": "COPICK-01",
                "product_codes": ["A", "B"],
                "completed_affected_pickings": 2,
                "package_modeled_route_saved_ft": saved_ft,
                "shared_joint_route_saved_ft": saved_ft,
                "hard_preconditions_before_pilot": hard or [],
                "execution_blockers": blockers or [],
                "setup_sensitivity": [
                    {
                        "hypothetical_total_package_setup_minutes": 15.0,
                        "payback_affected_pickings": payback,
                    }
                ],
            }
        ],
    }


def test_defer_when_operational_blockers_remain():
    result = evaluate_copick_package_decision(
        _payload(blockers=["geometry_and_capacity_not_field_verified"])
    )
    assert result["summary"] == {
        "REJECT": 0,
        "DEFER": 1,
        "READY_FOR_CONTROLLED_PILOT": 0,
    }
    decision = result["decisions"][0]
    assert decision["decision"] == "DEFER"
    assert "operational_blocker:geometry_and_capacity_not_field_verified" in decision["reasons"]
    assert decision["selected_decision_scenario"]["payback_affected_pickings"] == 32.8


def test_ready_when_gates_clear_and_payback_is_within_threshold():
    result = evaluate_copick_package_decision(_payload())
    decision = result["decisions"][0]
    assert decision["decision"] == "READY_FOR_CONTROLLED_PILOT"
    assert decision["human_approval_required"] is True
    assert decision["safe_to_execute"] is False


def test_defer_when_payback_exceeds_threshold():
    result = evaluate_copick_package_decision(_payload(payback=75.0))
    decision = result["decisions"][0]
    assert decision["decision"] == "DEFER"
    assert "package_payback_exceeds_configured_threshold" in decision["reasons"]


def test_reject_when_package_has_no_positive_route_benefit():
    result = evaluate_copick_package_decision(_payload(saved_ft=0.0))
    decision = result["decisions"][0]
    assert decision["decision"] == "REJECT"
    assert decision["reasons"] == ["no_positive_completed_package_route_benefit"]


def test_defer_when_shared_savings_do_not_reconcile():
    result = evaluate_copick_package_decision(_payload(reconciliation=0.01))
    decision = result["decisions"][0]
    assert decision["decision"] == "DEFER"
    assert decision["reasons"] == ["shared_savings_reconciliation_not_clean"]


def test_no_packages_returns_clean_zero_summary():
    result = evaluate_copick_package_decision(
        {
            "summary": {"shared_savings_reconciliation_difference_ft": 0.0},
            "packages": [],
        }
    )
    assert result["summary"] == {
        "REJECT": 0,
        "DEFER": 0,
        "READY_FOR_CONTROLLED_PILOT": 0,
    }
    assert result["decisions"] == []
    assert result["odoo_mutated"] is False
    assert result["safe_to_execute"] is False
