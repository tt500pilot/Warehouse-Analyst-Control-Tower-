import pytest

from app.services.mapped_area_manifest import (
    SCHEMA_VERSION,
    build_mapped_area_manifest,
    validate_mapped_area_manifest,
)


def _geometry(status="FIELD_VERIFIED"):
    return {
        "schema_version": "awia-warehouse-geometry-v1",
        "anchor": {
            "complete_name": "WH/Pre-Production",
            "graph_node_id": "NODE_KITTING",
        },
        "summary": {"measurement_statuses": [status]},
    }


def test_build_manifest_captures_single_area_pipeline_contract():
    manifest = build_mapped_area_manifest(
        area_slug="aisle-h",
        logical_area="WH/Stock/AWIA Mock/Aisle H",
        geometry_path="data/geometry/aisle-h-geometry.json",
        geometry=_geometry(),
        database="awia_mock",
    )

    assert manifest["schema_version"] == SCHEMA_VERSION
    assert manifest["area"] == {
        "slug": "aisle-h",
        "logical_area": "WH/Stock/AWIA Mock/Aisle H",
        "database": "awia_mock",
    }
    assert manifest["geometry"]["measurement_statuses"] == ["FIELD_VERIFIED"]
    assert manifest["pipeline"]["lookback_days"] == 90
    assert manifest["pipeline"]["economics_setup_minutes"] == [5.0, 15.0, 30.0, 60.0]
    assert manifest["execution_boundary"] == {
        "odoo_write_access_required": False,
        "inventory_execution_authorized": False,
        "human_approval_required": True,
    }


def test_validate_manifest_returns_runner_settings():
    manifest = build_mapped_area_manifest(
        area_slug="aisle-b",
        logical_area="WH/Stock/AWIA Mock/Aisle B",
        geometry_path="data/geometry/aisle-b-geometry.json",
        geometry=_geometry("MOCK_FIXTURE"),
        output_dir="data/analysis",
        lookback_days=30,
        source_limit=5000,
        top=12,
        economics_setup_minutes="10,20",
        decision_setup_minutes=20,
        max_payback_pickings=40,
        product_metadata="data/mock/product-metadata.json",
    )

    resolved = validate_mapped_area_manifest(manifest)

    assert resolved["area_slug"] == "aisle-b"
    assert resolved["geometry_path"] == "data/geometry/aisle-b-geometry.json"
    assert resolved["measurement_statuses"] == ["MOCK_FIXTURE"]
    assert resolved["lookback_days"] == 30
    assert resolved["source_limit"] == 5000
    assert resolved["top"] == 12
    assert resolved["economics_setup_minutes"] == [10.0, 20.0]
    assert resolved["product_metadata"] == "data/mock/product-metadata.json"


def test_manifest_rejects_path_traversal_and_absolute_paths():
    with pytest.raises(ValueError, match="parent traversal"):
        build_mapped_area_manifest(
            area_slug="aisle-h",
            logical_area="WH/Stock/Aisle H",
            geometry_path="../secret.json",
            geometry=_geometry(),
        )

    with pytest.raises(ValueError, match="repository-relative"):
        build_mapped_area_manifest(
            area_slug="aisle-h",
            logical_area="WH/Stock/Aisle H",
            geometry_path="C:/warehouse/geometry.json",
            geometry=_geometry(),
        )


def test_manifest_rejects_execution_authority_escalation():
    manifest = build_mapped_area_manifest(
        area_slug="aisle-h",
        logical_area="WH/Stock/Aisle H",
        geometry_path="data/geometry/aisle-h-geometry.json",
        geometry=_geometry(),
    )
    manifest["execution_boundary"]["inventory_execution_authorized"] = True

    with pytest.raises(ValueError, match="inventory_execution_authorized=false"):
        validate_mapped_area_manifest(manifest)


def test_manifest_rejects_unknown_schema_and_bad_geometry_schema():
    manifest = build_mapped_area_manifest(
        area_slug="aisle-h",
        logical_area="WH/Stock/Aisle H",
        geometry_path="data/geometry/aisle-h-geometry.json",
        geometry=_geometry(),
    )
    manifest["schema_version"] = "future-version"
    with pytest.raises(ValueError, match="schema_version"):
        validate_mapped_area_manifest(manifest)

    with pytest.raises(ValueError, match="geometry"):
        build_mapped_area_manifest(
            area_slug="aisle-h",
            logical_area="WH/Stock/Aisle H",
            geometry_path="data/geometry/aisle-h-geometry.json",
            geometry={"schema_version": "wrong"},
        )
