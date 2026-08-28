from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(path)
    return json.loads(path.read_text(encoding="utf-8"))


def _fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a concise manager-readable Markdown/JSON report from the mapped-aisle "
            "slotting, route, readiness, economics, and pilot-decision artifacts."
        )
    )
    parser.add_argument("--slotting", default="data/analysis/aisle-b-slotting.json")
    parser.add_argument("--route-validation", default="data/analysis/aisle-b-route-validation.json")
    parser.add_argument("--readiness", default="data/analysis/aisle-b-relocation-readiness.json")
    parser.add_argument("--economics", default="data/analysis/aisle-b-relocation-economics.json")
    parser.add_argument("--decision", default="data/analysis/aisle-b-pilot-decision.json")
    parser.add_argument("--geometry", default="data/geometry/aisle-b-geometry.json")
    parser.add_argument("--output-markdown", default="data/analysis/aisle-b-decision-report.md")
    parser.add_argument("--output-json", default="data/analysis/aisle-b-decision-report.json")
    args = parser.parse_args()

    paths = {
        "slotting": Path(args.slotting),
        "route_validation": Path(args.route_validation),
        "readiness": Path(args.readiness),
        "economics": Path(args.economics),
        "decision": Path(args.decision),
        "geometry": Path(args.geometry),
    }
    data = {name: _load(path) for name, path in paths.items()}

    geometry = data["geometry"].get("geometry") or data["geometry"]
    route = data["route_validation"]
    readiness = data["readiness"]
    economics = data["economics"]
    decision = data["decision"]
    slotting = data["slotting"]

    anchor = geometry.get("anchor") or {}
    geo_summary = geometry.get("summary") or {}
    completed = route.get("completed_historical_validation") or {}
    primary = route.get("primary_result") or {}
    readiness_summary = readiness.get("summary") or {}
    decision_summary = decision.get("summary") or {}

    decision_rows = decision.get("decisions") or []
    decision_by_code = {str(row.get("product_code") or ""): row for row in decision_rows}
    readiness_by_code = {
        str(row.get("product_code") or ""): row
        for row in readiness.get("recommendations") or []
    }
    economics_by_code = {
        str(row.get("product_code") or ""): row
        for row in economics.get("candidates") or []
    }

    recommendations = []
    for slot in slotting.get("recommendations") or []:
        code = str(slot.get("product_code") or "")
        ready = readiness_by_code.get(code, {})
        econ = economics_by_code.get(code, {})
        dec = decision_by_code.get(code, {})
        recommendations.append(
            {
                "product_code": code,
                "product_name": slot.get("product_name"),
                "source": (slot.get("source") or {}).get("complete_name"),
                "candidate": (slot.get("candidate") or {}).get("complete_name"),
                "decision": dec.get("decision"),
                "capacity_screen_pass": ready.get("capacity_screen_pass"),
                "completed_route_saved_ft": (
                    ready.get("completed_route_benefit") or {}
                ).get("attributable_modeled_aisle_subroute_distance_saved_ft"),
                "payback_affected_pickings_at_selected_scenario": (
                    dec.get("selected_decision_scenario") or {}
                ).get("payback_affected_pickings"),
                "observed_completed_affected_pickings": dec.get(
                    "observed_completed_affected_pickings_in_lookback"
                ),
                "payback_multiple_of_observed_lookback_volume": dec.get(
                    "payback_multiple_of_observed_lookback_volume"
                ),
                "hard_preconditions": dec.get("hard_preconditions_before_pilot") or [],
                "execution_blockers": dec.get("execution_blockers") or [],
                "reasons": dec.get("reasons") or [],
                "economics_status": econ.get("economics_status"),
            }
        )

    report = {
        "mode": "mapped_aisle_decision_report",
        "classification": "synthetic_sandbox_decision_summary",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "scope": {
            "anchor": anchor,
            "storage_bins": geo_summary.get("storage_bins"),
            "graph_nodes": geo_summary.get("graph_nodes"),
            "path_edges": geo_summary.get("path_edges"),
            "measurement_statuses": geo_summary.get("measurement_statuses"),
        },
        "completed_route_validation": {
            "completed_modeled_pickings": completed.get("modeled_pickings"),
            "affected_pickings": completed.get("affected_pickings"),
            "baseline_distance_ft": primary.get("baseline_total_distance_ft"),
            "candidate_distance_ft": primary.get("candidate_total_distance_ft"),
            "all_recommendations_saved_ft": primary.get("modeled_distance_saved_ft"),
            "all_recommendations_reduction_pct": primary.get("modeled_distance_reduction_pct"),
        },
        "capacity_screened_subset": {
            "recommendations_evaluated": readiness_summary.get("recommendations_evaluated"),
            "capacity_screen_passed": readiness_summary.get("capacity_screen_passed"),
            "capacity_screen_failed": readiness_summary.get("capacity_screen_failed"),
            "route_saved_ft": readiness_summary.get("capacity_screened_subset_route_saved_ft"),
            "reduction_pct": readiness_summary.get("capacity_screened_subset_reduction_pct"),
        },
        "pilot_decision_summary": decision_summary,
        "recommendations": recommendations,
        "conclusion": (
            "No recommendation is authorized for execution. Sandbox evidence demonstrates that AWIA can identify, "
            "quantify, screen, and stop slotting proposals when feasibility or traceability gates fail."
        ),
        "guardrails": [
            "Synthetic MOCK_FIXTURE results are algorithm-development evidence only and must not be represented as Firefly production performance.",
            "Route savings are modeled mapped-aisle subroute distance, not observed picker labor or whole-warehouse travel.",
            "Payback is expressed in modeled affected pickings, not calendar time or annual ROI.",
            "READY_FOR_CONTROLLED_PILOT would still require human approval; this report never authorizes inventory movement.",
            "No Odoo writes are performed.",
        ],
        "source_files": {name: str(path) for name, path in paths.items()},
    }

    lines: list[str] = []
    lines.append("# AWIA Mapped-Aisle Pilot Decision Report")
    lines.append("")
    lines.append("> **Sandbox / modeled evidence only.** This report does not represent Firefly production performance and does not authorize inventory movement.")
    lines.append("")
    lines.append("## Executive decision")
    lines.append("")
    lines.append(
        f"- **READY_FOR_CONTROLLED_PILOT:** {decision_summary.get('READY_FOR_CONTROLLED_PILOT', 0)}"
    )
    lines.append(f"- **DEFER:** {decision_summary.get('DEFER', 0)}")
    lines.append(f"- **REJECT:** {decision_summary.get('REJECT', 0)}")
    lines.append("- **Odoo mutated:** No")
    lines.append("")
    lines.append(
        "**Decision:** No relocation should be executed from this sandbox result. The value of this run is that the decision pipeline found travel opportunity, tested it against real constraints in the model, and stopped proposals that were not operationally ready."
    )
    lines.append("")
    lines.append("## Mapped scope")
    lines.append("")
    lines.append(f"- Anchor: `{anchor.get('complete_name')}`")
    lines.append(f"- Storage bins: {geo_summary.get('storage_bins')}")
    lines.append(f"- Graph nodes: {geo_summary.get('graph_nodes')}")
    lines.append(f"- Legal path edges: {geo_summary.get('path_edges')}")
    lines.append(
        f"- Measurement status: {', '.join(geo_summary.get('measurement_statuses') or []) or 'unknown'}"
    )
    lines.append("")
    lines.append("## Completed-route validation")
    lines.append("")
    lines.append(f"- Completed modeled pickings: {completed.get('modeled_pickings')}")
    lines.append(f"- Affected completed pickings: {completed.get('affected_pickings')}")
    lines.append(
        f"- Baseline mapped-aisle subroute: {_fmt(primary.get('baseline_total_distance_ft'))} ft"
    )
    lines.append(
        f"- All slotting recommendations: {_fmt(primary.get('candidate_total_distance_ft'))} ft"
    )
    lines.append(
        f"- Gross modeled reduction before feasibility screening: {_fmt(primary.get('modeled_distance_saved_ft'))} ft ({_fmt(primary.get('modeled_distance_reduction_pct'))}%)"
    )
    lines.append(
        f"- Capacity-screened feasible subset: {_fmt(readiness_summary.get('capacity_screened_subset_route_saved_ft'))} ft modeled reduction ({_fmt(readiness_summary.get('capacity_screened_subset_reduction_pct'))}%)"
    )
    lines.append("")
    lines.append("## Candidate decisions")
    lines.append("")
    lines.append("| Product | Source -> Candidate | Decision | Capacity | Completed modeled savings | Selected payback | Observed affected pickings | Key reason |")
    lines.append("|---|---|---:|---:|---:|---:|---:|---|")
    for row in recommendations:
        reason = "; ".join(row["reasons"] or row["hard_preconditions"] or row["execution_blockers"])
        lines.append(
            "| {code} | {source} -> {candidate} | **{decision}** | {capacity} | {saved} ft | {payback} | {observed} | {reason} |".format(
                code=row["product_code"],
                source=row["source"],
                candidate=row["candidate"],
                decision=row["decision"] or "n/a",
                capacity="PASS" if row["capacity_screen_pass"] else "FAIL",
                saved=_fmt(row["completed_route_saved_ft"]),
                payback=(
                    f"{_fmt(row['payback_affected_pickings_at_selected_scenario'])} affected pickings"
                    if row["payback_affected_pickings_at_selected_scenario"] is not None
                    else "n/a"
                ),
                observed=row["observed_completed_affected_pickings"],
                reason=reason or "n/a",
            )
        )
    lines.append("")
    lines.append("## Interpretation")
    lines.append("")
    lines.append(
        "The sandbox demonstrates a full decision loop rather than a forced optimization outcome: Odoo activity identified where to map, measured geometry enabled legal-path routing, route modeling quantified opportunity, capacity screening rejected an overweight move, and traceability/operational gates deferred the remaining move."
    )
    lines.append("")
    lines.append(
        "The next production milestone should be **generalizing this pipeline to any selected warehouse area and replacing MOCK_FIXTURE geometry/capacity/product metadata with approved field-verified data**, not tuning the sandbox until a move becomes pilot-ready."
    )
    lines.append("")
    lines.append("## Guardrails")
    lines.append("")
    for item in report["guardrails"]:
        lines.append(f"- {item}")
    lines.append("")

    markdown_path = Path(args.output_markdown)
    json_path = Path(args.output_json)
    markdown_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.parent.mkdir(parents=True, exist_ok=True)
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "mode": report["mode"],
                "odoo_mutated": False,
                "safe_to_execute": False,
                "decision_summary": decision_summary,
                "completed_route_saved_ft_before_feasibility": primary.get("modeled_distance_saved_ft"),
                "capacity_screened_subset_saved_ft": readiness_summary.get("capacity_screened_subset_route_saved_ft"),
                "markdown_output": str(markdown_path),
                "json_output": str(json_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
