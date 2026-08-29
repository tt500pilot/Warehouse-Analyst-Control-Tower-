from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.mapped_area_manifest import build_mapped_area_manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Build a versioned AWIA mapped-area deployment manifest from an existing canonical geometry artifact. "
            "The manifest stores references/settings only and never stores credentials or authorizes Odoo writes."
        )
    )
    parser.add_argument("--area-slug", required=True)
    parser.add_argument("--logical-area", required=True)
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--analysis-output-dir", default="data/analysis")
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--economics-setup-minutes", default="5,15,30,60")
    parser.add_argument("--decision-setup-minutes", type=float, default=15.0)
    parser.add_argument("--max-payback-pickings", type=float, default=50.0)
    parser.add_argument("--product-metadata", default=None)
    parser.add_argument("--database", default=None)
    args = parser.parse_args()

    geometry_path = Path(args.geometry)
    if not geometry_path.exists():
        raise FileNotFoundError(geometry_path)
    geometry_payload = json.loads(geometry_path.read_text(encoding="utf-8"))
    geometry = geometry_payload.get("geometry") or geometry_payload

    manifest = build_mapped_area_manifest(
        area_slug=args.area_slug,
        logical_area=args.logical_area,
        geometry_path=str(geometry_path),
        geometry=geometry,
        output_dir=args.analysis_output_dir,
        lookback_days=args.lookback_days,
        source_limit=args.source_limit,
        top=args.top,
        economics_setup_minutes=args.economics_setup_minutes,
        decision_setup_minutes=args.decision_setup_minutes,
        max_payback_pickings=args.max_payback_pickings,
        product_metadata=args.product_metadata,
        database=args.database,
    )

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "mode": "mapped_area_manifest_builder",
                "schema_version": manifest["schema_version"],
                "area_slug": manifest["area"]["slug"],
                "logical_area": manifest["area"]["logical_area"],
                "geometry": manifest["geometry"]["path"],
                "measurement_statuses": manifest["geometry"]["measurement_statuses"],
                "analysis_output_dir": manifest["pipeline"]["output_dir"],
                "manifest_output": str(output),
                "odoo_mutated": False,
                "inventory_execution_authorized": False,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
