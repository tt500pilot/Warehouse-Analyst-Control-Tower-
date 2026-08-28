from scripts.seed_odoo_manufacturing import _build_mo_plan, FINISHED_GOODS


def test_finished_goods_cover_all_mock_boms():
    assert set(FINISHED_GOODS) == {"BOM-OTS", "BOM-PROP", "BOM-AV", "BOM-GSE"}
    assert len({spec["default_code"] for spec in FINISHED_GOODS.values()}) == 4


def test_build_mo_plan_uses_stable_origins():
    rows = [
        {"mock_bom_id": "BOM-OTS", "program": "OTS"},
        {"mock_bom_id": "BOM-PROP", "program": "PROP"},
        {"mock_bom_id": "BOM-AV", "program": "AVIONICS"},
    ]
    plan = _build_mo_plan(rows, 3)
    assert [row["origin"] for row in plan] == [
        "AWIA-MOCK-MO-001",
        "AWIA-MOCK-MO-002",
        "AWIA-MOCK-MO-003",
    ]
    assert [row["mock_bom_id"] for row in plan] == ["BOM-OTS", "BOM-PROP", "BOM-AV"]


def test_build_mo_plan_rejects_nonpositive_count():
    rows = [{"mock_bom_id": "BOM-OTS", "program": "OTS"}]
    try:
        _build_mo_plan(rows, 0)
    except ValueError as exc:
        assert "greater than zero" in str(exc)
    else:
        raise AssertionError("Expected nonpositive MO count to be rejected")
