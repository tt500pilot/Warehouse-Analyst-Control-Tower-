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


def _run_json(label: str, command: list[str]) -> dict[str, Any]:
    print(f"\n=== {label} ===")
    print(" ".join(command))
    completed = subprocess.run(
        command,
        check=False,
        text=True,
        capture_output=True,
    )
    if completed.stderr:
        print(completed.stderr, file=sys.stderr, end="")
    if completed.returncode != 0:
        if completed.stdout:
            print(completed.stdout, end="")
        raise SystemExit(
            f"Deployment preparation stopped at {label!r} with exit code {completed.returncode}."
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        if completed.stdout:
            print(completed.stdout, end="")
        raise RuntimeError(f"{label} did not return valid JSON") from exc
    print(json.dumps(payload, indent=2))
    return payload


def _exact_area(scan: dict[str, Any], requested: str) -> dict[str, Any]:
    for row in scan.get("areas") or []:
        if str(row.get("logical_area") or "") == requested:
            return row
    available = ", ".join(str(row.get("logical_area") or "") for row in scan.get("areas") or [])
    raise ValueError(f"Unknown --area {requested!r}. Available returned areas: {available}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare the front half of an AWIA mapped-area deployment from read-only Odoo data: "
            "rank candidate areas, select one, generate the mapping intake package, and stop at the "
            "human field-measurement boundary. This command never infers real geometry and never writes Odoo."
        )
    )
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument("--top", type=int, default=10)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--area",
        default="",
        help="Exact logical area returned by the Odoo opportunity scan.",
    )
    selection.add_argument(
        "--area-rank",
        type=int,
        default=1,
        help="1-based ranked area to prepare. Defaults to 1 (highest priority).",
    )
    parser.add_argument(
        "--output-root",
        default="data/mapping_deployments",
        help="Root directory for the selected area's opportunity scan, mapping intake, and handoff files.",
    )
    args = parser.parse_args()

    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero")
    if args.source_limit <= 0:
        raise ValueError("--source-limit must be greater than zero")
    if args.top <= 0:
        raise ValueError("--top must be greater than zero")
    if args.area_rank <= 0:
        raise ValueError("--area-rank must be 1 or greater")
    if not args.area and args.area_rank > args.top:
        raise ValueError("--area-rank cannot exceed --top because only returned areas can be selected")

    python = sys.executable
    scripts = Path(__file__).resolve().parent

    scan = _run_json(
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
    )

    areas = list(scan.get("areas") or [])
    if not areas:
        raise RuntimeError("Opportunity scan returned no mappable warehouse areas")

    if args.area:
        selected = _exact_area(scan, args.area.strip())
        selection_source = "explicit_area"
    else:
        if args.area_rank > len(areas):
            raise ValueError(
                f"Requested --area-rank {args.area_rank}, but only {len(areas)} areas were returned"
            )
        selected = areas[args.area_rank - 1]
        selection_source = f"rank_{args.area_rank}"

    selected_area = str(selected.get("logical_area") or "").strip()
    if not selected_area:
        raise RuntimeError("Selected opportunity row has no logical_area")

    slug = _slug(selected_area)
    deployment_dir = Path(args.output_root) / slug
    intake_dir = deployment_dir / "mapping_intake"
    deployment_dir.mkdir(parents=True, exist_ok=True)

    scan_path = deployment_dir / f"{slug}-opportunity-scan.json"
    scan_path.write_text(json.dumps(scan, indent=2), encoding="utf-8")

    manifest = _run_json(
        "selected-area mapping intake generation",
        [
            python,
            str(scripts / "prepare_mapping_intake.py"),
            "--lookback-days",
            str(args.lookback_days),
            "--source-limit",
            str(args.source_limit),
            "--area",
            selected_area,
            "--output-dir",
            str(intake_dir),
        ],
    )

    manifest_path = intake_dir / f"{slug}-manifest.json"
    locations_path = intake_dir / f"{slug}-locations.csv"
    nodes_path = intake_dir / f"{slug}-graph-nodes.csv"
    edges_path = intake_dir / f"{slug}-path-edges.csv"

    flow_counterparts = list(manifest.get("operational_flow_counterparts") or [])
    suggested_anchor = None
    if flow_counterparts:
        suggested_anchor = flow_counterparts[0].get("location")

    handoff_path = deployment_dir / f"{slug}-field-mapping-handoff.md"
    deployment_path = deployment_dir / f"{slug}-deployment.json"

    handoff_lines = [
        "# AWIA Field Mapping Handoff",
        "",
        "> **Human measurement required.** AWIA selected this scope from approved read-only Odoo evidence, but it has not inferred warehouse coordinates or travel topology.",
        "",
        "## Selected scope",
        "",
        f"- Logical area: `{selected_area}`",
        f"- Selection source: `{selection_source}`",
        f"- Opportunity score: {selected.get('opportunity_score')}",
        f"- Why selected: {selected.get('why_map_this_area')}",
        f"- Database: `{scan.get('database')}`",
        f"- Lookback: {args.lookback_days} days",
        "",
        "## Mapping files",
        "",
        f"- Locations: `{locations_path}`",
        f"- Graph nodes: `{nodes_path}`",
        f"- Path edges: `{edges_path}`",
        f"- Intake manifest: `{manifest_path}`",
        f"- Opportunity scan: `{scan_path}`",
        "",
        "## Required field work",
        "",
    ]
    for instruction in manifest.get("measurement_instructions") or []:
        handoff_lines.append(f"- {instruction}")
    handoff_lines.extend(
        [
            "",
            "## Validation command after field measurement",
            "",
            "```powershell",
            "python .\\scripts\\validate_mapping_intake.py `",
            f"  --locations .\\{str(locations_path).replace('/', '\\')} `",
            f"  --nodes .\\{str(nodes_path).replace('/', '\\')} `",
            f"  --edges .\\{str(edges_path).replace('/', '\\')}",
            "```",
            "",
            "Do not proceed to geometry import until the validator returns `ready_for_geometry_import=true`.",
            "",
            "## Geometry import handoff",
            "",
            "After validation, import the geometry with the operational endpoint that represents the correct routing anchor for this scope.",
        ]
    )
    if suggested_anchor:
        handoff_lines.append(f"The top Odoo flow counterpart is `{suggested_anchor}`; treat this only as an anchor suggestion and confirm it operationally before import.")
    else:
        handoff_lines.append("No routing anchor is suggested automatically; choose and confirm the appropriate operational flow endpoint before import.")
    handoff_lines.extend(
        [
            "",
            "## Guardrails",
            "",
            "- No Odoo writes are performed.",
            "- Odoo evidence selects where to measure; it does not prove physical distance or optimal slotting.",
            "- Empty bins are intentionally included because they are candidate destinations.",
            "- Real coordinates, legal paths, capacities, restrictions, and secure/critical rules must be field-verified.",
            "- A validated geometry file is the boundary between this front-half deployment step and `run_mapped_area_decision_pipeline.py`.",
            "",
        ]
    )
    handoff_path.write_text("\n".join(handoff_lines), encoding="utf-8")

    deployment = {
        "mode": "read_only_mapping_deployment_preparation",
        "odoo_mutated": False,
        "geometry_inferred": False,
        "ready_for_decision_pipeline": False,
        "human_field_measurement_required": True,
        "database": scan.get("database"),
        "selection": {
            "source": selection_source,
            "selected_area": selected_area,
            "opportunity_score": selected.get("opportunity_score"),
            "why_selected": selected.get("why_map_this_area"),
            "ranked_areas_returned": len(areas),
        },
        "scope_counts": manifest.get("scope_counts") or {},
        "suggested_anchor_from_top_flow_counterpart": suggested_anchor,
        "outputs": {
            "opportunity_scan": str(scan_path),
            "mapping_manifest": str(manifest_path),
            "locations": str(locations_path),
            "graph_nodes": str(nodes_path),
            "path_edges": str(edges_path),
            "field_mapping_handoff": str(handoff_path),
        },
        "next_gate": {
            "action": "human_field_measurement_and_topology_capture",
            "validator": "scripts/validate_mapping_intake.py",
            "required_result": "ready_for_geometry_import=true",
        },
        "guardrails": [
            "This command uses read-only Odoo data to decide where to map; it never infers real XYZ coordinates or legal travel topology.",
            "The selected area is a mapping-investment priority, not proof that the area is physically inefficient.",
            "No optimization or relocation recommendation is produced before validated geometry exists.",
            "No Odoo writes are performed.",
        ],
    }
    deployment_path.write_text(json.dumps(deployment, indent=2), encoding="utf-8")

    print("\n=== mapping deployment prepared ===")
    print(json.dumps(deployment, indent=2))


if __name__ == "__main__":
    main()
