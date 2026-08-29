from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return text or "mapping-scope"


def _analysis_slug(area: str) -> str:
    tail = str(area).strip().split("/")[-1]
    return _slug(tail)


def _run(label: str, command: list[str], *, capture_json: bool = False) -> dict[str, Any] | None:
    print(f"\n=== {label} ===")
    print(" ".join(command))
    if capture_json:
        completed = subprocess.run(command, check=False, text=True, capture_output=True)
        if completed.stderr:
            print(completed.stderr, file=sys.stderr, end="")
        if completed.returncode != 0:
            if completed.stdout:
                print(completed.stdout, end="")
            raise SystemExit(f"Stopped at {label!r} with exit code {completed.returncode}")
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            if completed.stdout:
                print(completed.stdout, end="")
            raise RuntimeError(f"{label} did not return valid JSON") from exc
        print(json.dumps(payload, indent=2))
        return payload

    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(f"Stopped at {label!r} with exit code {completed.returncode}")
    return None


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "SANDBOX ONLY: prove AWIA cross-area deployment using the deterministic AWIA Mock fixture. "
            "This runner ranks an area from read-only Odoo data, prepares the normal human-measurement handoff, "
            "then substitutes deterministic mock geometry only because the selected locations are under /AWIA Mock/. "
            "It validates/imports that synthetic geometry and runs the generic mapped-area decision pipeline."
        )
    )
    parser.add_argument("--area-rank", type=int, default=2)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--deployment-root", default="data/mapping_deployments")
    parser.add_argument("--geometry-dir", default="data/geometry")
    parser.add_argument("--analysis-dir", default="data/analysis")
    parser.add_argument("--economics-setup-minutes", default="5,15,30,60")
    parser.add_argument("--decision-setup-minutes", type=float, default=15.0)
    parser.add_argument("--max-payback-pickings", type=float, default=50.0)
    args = parser.parse_args()

    if args.area_rank <= 0:
        raise ValueError("--area-rank must be 1 or greater")
    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero")
    if args.source_limit <= 0:
        raise ValueError("--source-limit must be greater than zero")
    if args.top <= 0:
        raise ValueError("--top must be greater than zero")
    if args.area_rank > args.top:
        raise ValueError("--area-rank cannot exceed --top")

    python = sys.executable
    scripts = Path(__file__).resolve().parent

    scan = _run(
        "read-only Odoo warehouse opportunity scan",
        [
            python,
            str(scripts / "scan_mapping_opportunities.py"),
            "--lookback-days",
            str(args.lookback_days),
            "--source-limit",
            str(args.source_limit),
            "--top",
            str(args.top),
        ],
        capture_json=True,
    )
    assert scan is not None
    areas = list(scan.get("areas") or [])
    if args.area_rank > len(areas):
        raise ValueError(
            f"Requested rank {args.area_rank}, but only {len(areas)} mappable areas were returned"
        )
    selected = areas[args.area_rank - 1]
    selected_area = str(selected.get("logical_area") or "")
    if "/AWIA Mock/" not in selected_area:
        raise RuntimeError(
            "Refusing sandbox auto-geometry for a non-mock area. This runner may only operate on /AWIA Mock/ locations."
        )

    deployment_slug = _slug(selected_area)
    analysis_slug = _analysis_slug(selected_area)
    deployment_dir = Path(args.deployment_root) / deployment_slug
    intake_dir = deployment_dir / "mapping_intake"
    mock_completed_dir = deployment_dir / "mock_completed"

    _run(
        "normal mapping deployment preparation",
        [
            python,
            str(scripts / "prepare_mapping_deployment.py"),
            "--lookback-days",
            str(args.lookback_days),
            "--source-limit",
            str(args.source_limit),
            "--top",
            str(args.top),
            "--area",
            selected_area,
            "--output-root",
            str(args.deployment_root),
        ],
    )

    locations = intake_dir / f"{deployment_slug}-locations.csv"
    _run(
        "sandbox-only deterministic mock geometry completion",
        [
            python,
            str(scripts / "complete_mapping_intake_from_mock_fixture.py"),
            "--locations",
            str(locations),
            "--output-dir",
            str(mock_completed_dir),
        ],
    )

    completed_locations = mock_completed_dir / f"{deployment_slug}-locations.csv"
    completed_nodes = mock_completed_dir / f"{deployment_slug}-graph-nodes.csv"
    completed_edges = mock_completed_dir / f"{deployment_slug}-path-edges.csv"

    _run(
        "synthetic geometry validation",
        [
            python,
            str(scripts / "validate_mapping_intake.py"),
            "--locations",
            str(completed_locations),
            "--nodes",
            str(completed_nodes),
            "--edges",
            str(completed_edges),
        ],
    )

    top_counterparts = list(selected.get("top_flow_counterparts") or [])
    anchor_location = (
        str(top_counterparts[0].get("location") or "") if top_counterparts else ""
    )
    if not anchor_location:
        raise RuntimeError("Selected mock area has no operational flow counterpart to use as routing anchor")

    geometry_path = Path(args.geometry_dir) / f"{analysis_slug}-geometry.json"
    _run(
        "canonical geometry import",
        [
            python,
            str(scripts / "import_validated_geometry.py"),
            "--locations",
            str(completed_locations),
            "--nodes",
            str(completed_nodes),
            "--edges",
            str(completed_edges),
            "--anchor-location",
            anchor_location,
            "--output",
            str(geometry_path),
        ],
    )

    _run(
        "generic mapped-area decision pipeline",
        [
            python,
            str(scripts / "run_mapped_area_decision_pipeline.py"),
            "--geometry",
            str(geometry_path),
            "--area-slug",
            analysis_slug,
            "--output-dir",
            str(args.analysis_dir),
            "--lookback-days",
            str(args.lookback_days),
            "--source-limit",
            str(args.source_limit),
            "--economics-setup-minutes",
            args.economics_setup_minutes,
            "--decision-setup-minutes",
            str(args.decision_setup_minutes),
            "--max-payback-pickings",
            str(args.max_payback_pickings),
        ],
    )

    traceability_path = Path(args.analysis_dir) / f"{analysis_slug}-traceability-health.json"
    decision_path = Path(args.analysis_dir) / f"{analysis_slug}-pilot-decision.json"
    traceability = json.loads(traceability_path.read_text(encoding="utf-8"))
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    result = {
        "mode": "sandbox_only_cross_area_deployment_e2e",
        "odoo_mutated": False,
        "real_warehouse_geometry_inferred": False,
        "selected_rank": args.area_rank,
        "selected_area": selected_area,
        "opportunity_score": selected.get("opportunity_score"),
        "analysis_slug": analysis_slug,
        "anchor_location": anchor_location,
        "traceability_summary": traceability.get("summary") or {},
        "decision_summary": decision.get("summary") or {},
        "outputs": {
            "deployment_dir": str(deployment_dir),
            "mock_completed_geometry_intake": str(mock_completed_dir),
            "canonical_geometry": str(geometry_path),
            "traceability_health": str(traceability_path),
            "pilot_decision": str(decision_path),
            "manager_report_markdown": str(Path(args.analysis_dir) / f"{analysis_slug}-decision-report.md"),
            "manager_report_json": str(Path(args.analysis_dir) / f"{analysis_slug}-decision-report.json"),
        },
        "guardrails": [
            "This runner is sandbox-only and refuses selected areas outside /AWIA Mock/.",
            "The normal production front half still stops for human field measurement; this runner substitutes deterministic fixture geometry only for software validation.",
            "Tracked product/location positions with anonymous positive quantity are hard-blocked before target allocation in the generic decision pipeline.",
            "Synthetic geometry, capacities, product metadata, route savings, and decisions must never be represented as Firefly production performance.",
            "No Odoo writes are performed.",
        ],
    }
    print("\n=== sandbox cross-area deployment complete ===")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
