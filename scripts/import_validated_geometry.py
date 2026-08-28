from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.warehouse_geometry import build_canonical_geometry
from scripts.validate_mapping_intake import validate


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(path)
    with path.open("r", newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate and import an AWIA mapping intake into canonical warehouse geometry."
    )
    parser.add_argument("--locations", required=True)
    parser.add_argument("--nodes", required=True)
    parser.add_argument("--edges", required=True)
    parser.add_argument("--anchor-location", default="")
    parser.add_argument(
        "--output",
        default="data/geometry/warehouse_geometry.json",
        help="Canonical geometry JSON output path.",
    )
    args = parser.parse_args()

    locations = _read_csv(Path(args.locations))
    nodes = _read_csv(Path(args.nodes))
    edges = _read_csv(Path(args.edges))

    validation = validate(locations, nodes, edges)
    if not validation["ready_for_geometry_import"]:
        print(json.dumps({"validation": validation}, indent=2))
        raise SystemExit(2)

    geometry = build_canonical_geometry(
        locations,
        nodes,
        edges,
        anchor_location_name=args.anchor_location.strip() or None,
    )
    payload = {
        "validation": validation,
        "geometry": geometry,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    distances = geometry["anchor_distances"]
    summary = {
        "ready_for_geometry_import": True,
        "output": str(output),
        "schema_version": geometry["schema_version"],
        "anchor": geometry["anchor"],
        "summary": geometry["summary"],
        "nearest_storage_bin": distances[0] if distances else None,
        "farthest_storage_bin": distances[-1] if distances else None,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
