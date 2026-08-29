from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def _slug_from_geometry(path: Path) -> str:
    stem = path.stem
    if stem.endswith("-geometry"):
        stem = stem[: -len("-geometry")]
    return stem or "mapped-area"


def _run(label: str, command: list[str]) -> None:
    print(f"\n=== {label} ===")
    print(" ".join(command))
    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            f"Pipeline stopped at {label!r} with exit code {completed.returncode}. "
            "No downstream stages were run."
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the full read-only AWIA decision pipeline for any already validated canonical mapped-area geometry. "
            "The runner applies traceability before slot allocation, then orchestrates slotting, matched route validation, "
            "relocation readiness, individual economics, co-pick package economics, individual and package pilot decisions, "
            "and manager report generation. It does not create geometry and does not write Odoo."
        )
    )
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--area-slug", default=None)
    parser.add_argument("--output-dir", default="data/analysis")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--economics-setup-minutes", default="5,15,30,60")
    parser.add_argument("--decision-setup-minutes", type=float, default=15.0)
    parser.add_argument("--max-payback-pickings", type=float, default=50.0)
    parser.add_argument("--product-metadata", default=None)
    args = parser.parse_args()

    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero")
    if args.source_limit <= 0:
        raise ValueError("--source-limit must be greater than zero")
    if args.top <= 0:
        raise ValueError("--top must be greater than zero")
    if args.decision_setup_minutes <= 0:
        raise ValueError("--decision-setup-minutes must be greater than zero")
    if args.max_payback_pickings <= 0:
        raise ValueError("--max-payback-pickings must be greater than zero")

    geometry = Path(args.geometry)
    if not geometry.exists():
        raise FileNotFoundError(geometry)

    area_slug = args.area_slug or _slug_from_geometry(geometry)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    traceability = output_dir / f"{area_slug}-traceability-health.json"
    slotting = output_dir / f"{area_slug}-slotting.json"
    route = output_dir / f"{area_slug}-route-validation.json"
    readiness = output_dir / f"{area_slug}-relocation-readiness.json"
    economics = output_dir / f"{area_slug}-relocation-economics.json"
    copick_packages = output_dir / f"{area_slug}-copick-package-economics.json"
    copick_package_decision = output_dir / f"{area_slug}-copick-package-pilot-decision.json"
    decision = output_dir / f"{area_slug}-pilot-decision.json"
    report_md = output_dir / f"{area_slug}-decision-report.md"
    report_json = output_dir / f"{area_slug}-decision-report.json"

    python = sys.executable
    scripts = Path(__file__).resolve().parent

    _run("traceability gate + mapped-area slotting analysis", [
        python, str(scripts / "analyze_mapped_aisle_slotting.py"),
        "--geometry", str(geometry),
        "--lookback-days", str(args.lookback_days),
        "--source-limit", str(args.source_limit),
        "--top", str(args.top),
        "--traceability-output", str(traceability),
        "--output", str(slotting),
    ])

    _run("matched completed/planned route validation", [
        python, str(scripts / "validate_mapped_aisle_route_impact.py"),
        "--geometry", str(geometry),
        "--slotting-result", str(slotting),
        "--lookback-days", str(args.lookback_days),
        "--source-limit", str(args.source_limit),
        "--output", str(route),
    ])

    readiness_command = [
        python, str(scripts / "analyze_mapped_aisle_relocation_readiness.py"),
        "--geometry", str(geometry),
        "--slotting-result", str(slotting),
        "--route-validation", str(route),
        "--output", str(readiness),
    ]
    if args.product_metadata:
        readiness_command.extend(["--product-metadata", args.product_metadata])
    _run("relocation readiness gate", readiness_command)

    _run("relocation economics sensitivity", [
        python, str(scripts / "analyze_mapped_aisle_relocation_economics.py"),
        "--readiness", str(readiness),
        "--route-validation", str(route),
        "--setup-minutes", args.economics_setup_minutes,
        "--output", str(economics),
    ])

    _run("co-pick package economics", [
        python, str(scripts / "analyze_mapped_aisle_copick_packages.py"),
        "--readiness", str(readiness),
        "--route-validation", str(route),
        "--setup-minutes", args.economics_setup_minutes,
        "--output", str(copick_packages),
    ])

    _run("co-pick package pilot decision gate", [
        python, str(scripts / "evaluate_mapped_aisle_copick_package_decision.py"),
        "--package-economics", str(copick_packages),
        "--setup-minutes", str(args.decision_setup_minutes),
        "--max-payback-pickings", str(args.max_payback_pickings),
        "--lookback-days", str(args.lookback_days),
        "--output", str(copick_package_decision),
    ])

    _run("individual pilot decision gate", [
        python, str(scripts / "evaluate_mapped_aisle_pilot_decision.py"),
        "--readiness", str(readiness),
        "--economics", str(economics),
        "--setup-minutes", str(args.decision_setup_minutes),
        "--max-payback-pickings", str(args.max_payback_pickings),
        "--lookback-days", str(args.lookback_days),
        "--output", str(decision),
    ])

    _run("manager decision report", [
        python, str(scripts / "generate_mapped_aisle_decision_report.py"),
        "--slotting", str(slotting),
        "--route-validation", str(route),
        "--readiness", str(readiness),
        "--economics", str(economics),
        "--decision", str(decision),
        "--copick-package-economics", str(copick_packages),
        "--copick-package-decision", str(copick_package_decision),
        "--geometry", str(geometry),
        "--output-markdown", str(report_md),
        "--output-json", str(report_json),
    ])

    traceability_payload = json.loads(traceability.read_text(encoding="utf-8"))
    slotting_payload = json.loads(slotting.read_text(encoding="utf-8"))
    decision_payload = json.loads(decision.read_text(encoding="utf-8"))
    package_payload = json.loads(copick_packages.read_text(encoding="utf-8"))
    package_decision_payload = json.loads(copick_package_decision.read_text(encoding="utf-8"))
    final = {
        "mode": "mapped_area_decision_pipeline",
        "odoo_mutated": False,
        "safe_to_execute": False,
        "area_slug": area_slug,
        "geometry": str(geometry),
        "traceability_summary": traceability_payload.get("summary") or {},
        "traceability_candidates_suppressed": int(
            (slotting_payload.get("summary") or {}).get("traceability_candidates_suppressed", 0) or 0
        ),
        "individual_decision_summary": decision_payload.get("summary") or {},
        "copick_package_summary": package_payload.get("summary") or {},
        "copick_package_decision_summary": package_decision_payload.get("summary") or {},
        "outputs": {
            "traceability_health": str(traceability),
            "slotting": str(slotting),
            "route_validation": str(route),
            "relocation_readiness": str(readiness),
            "relocation_economics": str(economics),
            "copick_package_economics": str(copick_packages),
            "copick_package_pilot_decision": str(copick_package_decision),
            "pilot_decision": str(decision),
            "manager_report_markdown": str(report_md),
            "manager_report_json": str(report_json),
        },
        "guardrails": [
            "This runner requires an already validated canonical geometry artifact; it does not infer real warehouse geometry.",
            "Tracked product/location positions with anonymous positive quantity are excluded before slot target allocation and cannot flow into route/economics/pilot decisions.",
            "Traceability-blocked inventory remains occupied inventory and is never treated as an available target bin.",
            "The pipeline is advisory/read-only and performs no Odoo writes.",
            "Shared co-pick route benefits are evaluated and decided as packages instead of being arbitrarily allocated to individual SKUs.",
            "READY_FOR_CONTROLLED_PILOT is not execution authorization; human approval remains required.",
            "Synthetic fixture inputs must never be represented as Firefly production performance.",
        ],
    }
    print("\n=== pipeline complete ===")
    print(json.dumps(final, indent=2))


if __name__ == "__main__":
    main()
