from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

Record = Mapping[str, Any]

SCHEMA_VERSION = "awia-mapped-area-deployment-v1"
AREA_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def _positive_int(value: Any, field: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return parsed


def _positive_float(value: Any, field: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be numeric") from exc
    if parsed <= 0:
        raise ValueError(f"{field} must be greater than zero")
    return parsed


def _validate_relative_path(value: Any, field: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field} is required")
    path = Path(text)
    if path.is_absolute():
        raise ValueError(f"{field} must be repository-relative")
    if ".." in path.parts:
        raise ValueError(f"{field} cannot contain parent traversal")
    return path.as_posix()


def build_mapped_area_manifest(
    *,
    area_slug: str,
    logical_area: str,
    geometry_path: str,
    geometry: Record,
    output_dir: str = "data/analysis",
    lookback_days: int = 90,
    source_limit: int = 20000,
    top: int = 20,
    economics_setup_minutes: str = "5,15,30,60",
    decision_setup_minutes: float = 15.0,
    max_payback_pickings: float = 50.0,
    product_metadata: str | None = None,
    database: str | None = None,
) -> dict[str, Any]:
    slug = str(area_slug or "").strip().lower()
    if not AREA_SLUG_RE.fullmatch(slug):
        raise ValueError("area_slug must contain lowercase letters, numbers, and single hyphens only")

    logical = str(logical_area or "").strip()
    if not logical:
        raise ValueError("logical_area is required")

    if geometry.get("schema_version") != "awia-warehouse-geometry-v1":
        raise ValueError("geometry must use awia-warehouse-geometry-v1")

    geometry_rel = _validate_relative_path(geometry_path, "geometry_path")
    output_rel = _validate_relative_path(output_dir, "output_dir")
    metadata_rel = (
        _validate_relative_path(product_metadata, "product_metadata")
        if product_metadata
        else None
    )

    lookback = _positive_int(lookback_days, "lookback_days")
    limit = _positive_int(source_limit, "source_limit")
    top_n = _positive_int(top, "top")
    decision_minutes = _positive_float(decision_setup_minutes, "decision_setup_minutes")
    max_payback = _positive_float(max_payback_pickings, "max_payback_pickings")

    setup_values: list[float] = []
    for raw in str(economics_setup_minutes or "").split(","):
        raw = raw.strip()
        if not raw:
            continue
        setup_values.append(_positive_float(raw, "economics_setup_minutes"))
    if not setup_values:
        raise ValueError("economics_setup_minutes must contain at least one positive value")

    measurement_statuses = sorted(
        {str(value) for value in (geometry.get("summary") or {}).get("measurement_statuses", []) if value}
    )
    anchor = geometry.get("anchor") or {}

    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "mapped_area_deployment_manifest",
        "area": {
            "slug": slug,
            "logical_area": logical,
            "database": str(database).strip() if database else None,
        },
        "geometry": {
            "path": geometry_rel,
            "schema_version": geometry.get("schema_version"),
            "measurement_statuses": measurement_statuses,
            "anchor_complete_name": anchor.get("complete_name"),
            "anchor_graph_node_id": anchor.get("graph_node_id"),
        },
        "pipeline": {
            "output_dir": output_rel,
            "lookback_days": lookback,
            "source_limit": limit,
            "top": top_n,
            "economics_setup_minutes": setup_values,
            "decision_setup_minutes": decision_minutes,
            "max_payback_pickings": max_payback,
            "product_metadata": metadata_rel,
        },
        "execution_boundary": {
            "odoo_write_access_required": False,
            "inventory_execution_authorized": False,
            "human_approval_required": True,
        },
        "guardrails": [
            "This manifest contains references and analysis settings only; it must never contain credentials or API keys.",
            "The referenced geometry must already exist and have passed the mapping-intake validation/import boundary.",
            "MOCK_FIXTURE geometry may be used for software validation only and must not be represented as production evidence.",
            "Running a manifest is read-only/advisory and never authorizes inventory movement or Odoo writes.",
        ],
    }


def validate_mapped_area_manifest(manifest: Record) -> dict[str, Any]:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"manifest schema_version must be {SCHEMA_VERSION}")

    area = manifest.get("area") or {}
    geometry = manifest.get("geometry") or {}
    pipeline = manifest.get("pipeline") or {}
    boundary = manifest.get("execution_boundary") or {}

    slug = str(area.get("slug") or "").strip().lower()
    if not AREA_SLUG_RE.fullmatch(slug):
        raise ValueError("manifest area.slug is invalid")
    if not str(area.get("logical_area") or "").strip():
        raise ValueError("manifest area.logical_area is required")

    geometry_path = _validate_relative_path(geometry.get("path"), "geometry.path")
    if geometry.get("schema_version") != "awia-warehouse-geometry-v1":
        raise ValueError("manifest geometry.schema_version is unsupported")
    output_dir = _validate_relative_path(pipeline.get("output_dir"), "pipeline.output_dir")

    lookback = _positive_int(pipeline.get("lookback_days"), "pipeline.lookback_days")
    source_limit = _positive_int(pipeline.get("source_limit"), "pipeline.source_limit")
    top = _positive_int(pipeline.get("top"), "pipeline.top")
    decision_minutes = _positive_float(
        pipeline.get("decision_setup_minutes"), "pipeline.decision_setup_minutes"
    )
    max_payback = _positive_float(
        pipeline.get("max_payback_pickings"), "pipeline.max_payback_pickings"
    )

    economics_values = pipeline.get("economics_setup_minutes") or []
    if not isinstance(economics_values, list) or not economics_values:
        raise ValueError("pipeline.economics_setup_minutes must be a non-empty list")
    economics = [
        _positive_float(value, "pipeline.economics_setup_minutes")
        for value in economics_values
    ]

    metadata = pipeline.get("product_metadata")
    metadata_path = _validate_relative_path(metadata, "pipeline.product_metadata") if metadata else None

    if boundary.get("odoo_write_access_required") is not False:
        raise ValueError("manifest must explicitly state odoo_write_access_required=false")
    if boundary.get("inventory_execution_authorized") is not False:
        raise ValueError("manifest must explicitly state inventory_execution_authorized=false")
    if boundary.get("human_approval_required") is not True:
        raise ValueError("manifest must explicitly state human_approval_required=true")

    return {
        "area_slug": slug,
        "logical_area": str(area.get("logical_area") or "").strip(),
        "database": area.get("database"),
        "geometry_path": geometry_path,
        "measurement_statuses": list(geometry.get("measurement_statuses") or []),
        "output_dir": output_dir,
        "lookback_days": lookback,
        "source_limit": source_limit,
        "top": top,
        "economics_setup_minutes": economics,
        "decision_setup_minutes": decision_minutes,
        "max_payback_pickings": max_payback,
        "product_metadata": metadata_path,
    }
