from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

AREA_SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

ARTIFACT_SUFFIXES: dict[str, str] = {
    "slotting": "slotting.json",
    "route_validation": "route-validation.json",
    "relocation_readiness": "relocation-readiness.json",
    "relocation_economics": "relocation-economics.json",
    "copick_package_economics": "copick-package-economics.json",
    "copick_package_pilot_decision": "copick-package-pilot-decision.json",
    "pilot_decision": "pilot-decision.json",
    "decision_report": "decision-report.json",
    "production_pilot_readiness": "production-pilot-readiness.json",
    "production_pilot_intake": "production-pilot-intake.json",
}


def validate_area_slug(area_slug: str) -> str:
    slug = str(area_slug or "").strip().lower()
    if not AREA_SLUG_PATTERN.fullmatch(slug):
        raise ValueError("area_slug must contain only lowercase letters, numbers, and single hyphens")
    return slug


def discover_analysis_areas(analysis_dir: str | Path = "data/analysis") -> list[str]:
    directory = Path(analysis_dir)
    if not directory.exists():
        return []

    suffix = "-production-pilot-readiness.json"
    areas: set[str] = set()
    for path in directory.glob(f"*{suffix}"):
        name = path.name
        if not name.endswith(suffix):
            continue
        candidate = name[: -len(suffix)]
        try:
            areas.add(validate_area_slug(candidate))
        except ValueError:
            continue
    return sorted(areas)


def load_analysis_artifacts(
    area_slug: str,
    analysis_dir: str | Path = "data/analysis",
) -> dict[str, Any]:
    slug = validate_area_slug(area_slug)
    directory = Path(analysis_dir)
    artifacts: dict[str, Any] = {}
    missing: list[str] = []

    for key, suffix in ARTIFACT_SUFFIXES.items():
        path = directory / f"{slug}-{suffix}"
        if not path.exists():
            artifacts[key] = None
            missing.append(key)
            continue
        try:
            artifacts[key] = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            artifacts[key] = {
                "artifact_error": str(exc),
                "artifact_path": str(path),
            }

    return {
        "area_slug": slug,
        "analysis_dir": str(directory),
        "artifacts": artifacts,
        "missing_artifacts": missing,
        "complete": not missing,
        "guardrails": [
            "Only fixed AWIA analysis artifact suffixes are loaded.",
            "The area slug is validated and cannot contain path separators or traversal segments.",
            "This loader does not connect to Odoo and performs no writes.",
        ],
    }
