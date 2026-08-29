import json

import pytest

from app.ui.optimization_artifacts import (
    discover_analysis_areas,
    load_analysis_artifacts,
    validate_area_slug,
)


def test_validate_area_slug_accepts_normal_slug():
    assert validate_area_slug("aisle-h") == "aisle-h"


def test_validate_area_slug_rejects_path_traversal():
    with pytest.raises(ValueError):
        validate_area_slug("../secrets")
    with pytest.raises(ValueError):
        validate_area_slug("aisle/h")


def test_discover_analysis_areas_uses_readiness_artifact(tmp_path):
    (tmp_path / "aisle-h-production-pilot-readiness.json").write_text("{}", encoding="utf-8")
    (tmp_path / "aisle-b-production-pilot-readiness.json").write_text("{}", encoding="utf-8")
    (tmp_path / "not-an-area.txt").write_text("x", encoding="utf-8")

    assert discover_analysis_areas(tmp_path) == ["aisle-b", "aisle-h"]


def test_load_analysis_artifacts_only_reads_allowlisted_suffixes(tmp_path):
    readiness = {"status": "BLOCKED"}
    (tmp_path / "aisle-h-production-pilot-readiness.json").write_text(
        json.dumps(readiness), encoding="utf-8"
    )
    (tmp_path / "aisle-h-secret.json").write_text(
        json.dumps({"secret": True}), encoding="utf-8"
    )

    result = load_analysis_artifacts("aisle-h", tmp_path)

    assert result["artifacts"]["production_pilot_readiness"] == readiness
    assert "secret" not in result["artifacts"]
    assert result["complete"] is False


def test_load_analysis_artifacts_reports_invalid_json_without_crashing(tmp_path):
    (tmp_path / "aisle-h-production-pilot-readiness.json").write_text(
        "{not-json", encoding="utf-8"
    )

    result = load_analysis_artifacts("aisle-h", tmp_path)
    artifact = result["artifacts"]["production_pilot_readiness"]

    assert "artifact_error" in artifact
    assert artifact["artifact_path"].endswith("aisle-h-production-pilot-readiness.json")
