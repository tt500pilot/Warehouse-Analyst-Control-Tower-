from app.ui.management_summary import build_optimization_management_summary


def test_blocked_summary_is_plain_english_and_action_oriented():
    result = build_optimization_management_summary(
        readiness={
            "status": "BLOCKED",
            "summary": {"checks": 13, "passed": 0, "failed": 13},
        },
        route={
            "primary_result": {
                "modeled_distance_saved_ft": 240.0,
                "modeled_distance_reduction_pct": 13.51,
            },
            "completed_historical_validation": {"modeled_pickings": 4},
        },
        individual_decision={"summary": {"DEFER": 5, "REJECT": 0, "READY_FOR_CONTROLLED_PILOT": 0}},
        package_decision={"summary": {"DEFER": 1, "REJECT": 0, "READY_FOR_CONTROLLED_PILOT": 0}},
        traceability={
            "summary": {
                "blocked_positions": 2,
                "blocked_products": 1,
                "anonymous_quantity": 17.0,
                "traceability_coverage_pct": 92.5,
            }
        },
        intake={"summary": {"organizational_actions_remaining": 13}},
    )

    assert result["action_status"] == "DO NOT MOVE INVENTORY YET"
    assert "13.51%" in result["opportunity"]
    assert "0 of 5 individual recommendations" in result["decision"]
    assert "0 of 1 co-pick packages" in result["decision"]
    assert "2 tracked inventory position" in result["traceability"]
    assert "0 of 13" in result["readiness"]
    assert "13 management/operational action" in result["readiness"]


def test_ready_summary_still_does_not_authorize_inventory_movement():
    result = build_optimization_management_summary(
        readiness={
            "status": "READY_FOR_READ_ONLY_PRODUCTION_PILOT",
            "ready_for_read_only_production_pilot": True,
            "summary": {"checks": 13, "passed": 13, "failed": 0},
        },
        route={
            "primary_result": {
                "modeled_distance_saved_ft": 100.0,
                "modeled_distance_reduction_pct": 8.0,
            },
            "completed_historical_validation": {"modeled_pickings": 10},
        },
        individual_decision={"summary": {"DEFER": 0, "REJECT": 0, "READY_FOR_CONTROLLED_PILOT": 1}},
        package_decision={"summary": {"DEFER": 0, "REJECT": 0, "READY_FOR_CONTROLLED_PILOT": 0}},
        traceability={
            "summary": {
                "blocked_positions": 0,
                "blocked_products": 0,
                "anonymous_quantity": 0,
                "traceability_coverage_pct": 100.0,
            }
        },
        intake={"summary": {"organizational_actions_remaining": 0}},
    )

    assert result["action_status"] == "READ-ONLY PILOT REVIEW CAN PROCEED"
    assert "100.0%" in result["traceability"]
    assert "13 of 13" in result["readiness"]
    assert "not proof" in result["footnote"].lower()
    assert "authorization to move inventory" in result["footnote"].lower()


def test_summary_handles_missing_traceability_without_guessing():
    result = build_optimization_management_summary(
        readiness={"status": "BLOCKED", "summary": {}},
        route={},
        individual_decision={},
        package_decision={},
        traceability=None,
        intake=None,
    )

    assert "not currently available" in result["traceability"]
    assert "not yet produced" in result["opportunity"]
