from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.mapped_area_manifest import validate_mapped_area_manifest


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Run the existing AWIA mapped-area decision pipeline from one validated deployment manifest. "
            "This runner does not create geometry, store credentials, write Odoo, or authorize inventory movement."
        )
    )
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resolved = validate_mapped_area_manifest(manifest)

    geometry = Path(resolved["geometry_path"])
    if not geometry.exists():
        raise FileNotFoundError(geometry)

    product_metadata = resolved.get("product_metadata")
    if product_metadata and not Path(product_metadata).exists():
        raise FileNotFoundError(product_metadata)

    scripts = Path(__file__).resolve().parent
    command = [
        sys.executable,
        str(scripts / "run_mapped_area_decision_pipeline.py"),
        "--geometry",
        str(geometry),
        "--area-slug",
        resolved["area_slug"],
        "--output-dir",
        resolved["output_dir"],
        "--lookback-days",
        str(resolved["lookback_days"]),
        "--source-limit",
        str(resolved["source_limit"]),
        "--top",
        str(resolved["top"]),
        "--economics-setup-minutes",
        ",".join(str(value).rstrip("0").rstrip(".") for value in resolved["economics_setup_minutes"]),
        "--decision-setup-minutes",
        str(resolved["decision_setup_minutes"]),
        "--max-payback-pickings",
        str(resolved["max_payback_pickings"]),
    ]
    if product_metadata:
        command.extend(["--product-metadata", product_metadata])

    print("=== mapped-area manifest resolved ===")
    print(
        json.dumps(
            {
                "schema_version": manifest["schema_version"],
                "area_slug": resolved["area_slug"],
                "logical_area": resolved["logical_area"],
                "database_reference": resolved.get("database"),
                "geometry": str(geometry),
                "measurement_statuses": resolved.get("measurement_statuses") or [],
                "output_dir": resolved["output_dir"],
                "odoo_mutated": False,
                "inventory_execution_authorized": False,
            },
            indent=2,
        )
    )
    print("\n=== delegated decision pipeline ===")
    print(" ".join(command))

    completed = subprocess.run(command, check=False)
    if completed.returncode != 0:
        raise SystemExit(
            f"Manifest-driven decision pipeline failed with exit code {completed.returncode}."
        )


if __name__ == "__main__":
    main()
