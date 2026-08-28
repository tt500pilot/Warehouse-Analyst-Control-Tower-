from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.mapped_aisle_relocation_readiness import analyze_relocation_readiness
from odoo_client import OdooWarehouseClient


def _fields(client: OdooWarehouseClient, model: str, wanted: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in wanted if field in available]


def _load_fixture_metadata(path: Path) -> dict[str, dict]:
    if not path.exists():
        raise FileNotFoundError(path)
    result: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            code = str(row.get("default_code") or "").strip()
            if not code:
                continue
            result[code] = {
                "weight_lb": float(row.get("weight_lb") or 0.0),
                "volume_ft3": float(row.get("volume_ft3") or 0.0),
                "velocity_profile": str(row.get("velocity_profile") or ""),
                "secure_required": str(row.get("secure_required") or "").strip().lower()
                in {"1", "true", "yes", "on"},
                "x_is_flight_critical": str(row.get("x_is_flight_critical") or "").strip().lower()
                in {"1", "true", "yes", "on"},
            }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only readiness gate for geometry-informed mapped-aisle relocations."
    )
    parser.add_argument(
        "--geometry",
        default="data/geometry/aisle-b-geometry.json",
    )
    parser.add_argument(
        "--slotting-result",
        default="data/analysis/aisle-b-slotting.json",
    )
    parser.add_argument(
        "--route-validation",
        default="data/analysis/aisle-b-route-validation.json",
    )
    parser.add_argument(
        "--product-metadata",
        default="data/simulation_sandbox/mock_odoo/products.csv",
        help="Sandbox physical-product metadata. Replace with an approved source for real deployments.",
    )
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument("--walking-speed-ft-s", type=float, default=3.5)
    parser.add_argument(
        "--output",
        default="data/analysis/aisle-b-relocation-readiness.json",
    )
    args = parser.parse_args()

    if args.source_limit <= 0:
        raise ValueError("--source-limit must be greater than zero")
    if args.walking_speed_ft_s <= 0:
        raise ValueError("--walking-speed-ft-s must be greater than zero")

    geometry_path = Path(args.geometry)
    slotting_path = Path(args.slotting_result)
    route_path = Path(args.route_validation)
    metadata_path = Path(args.product_metadata)
    for path in (geometry_path, slotting_path, route_path):
        if not path.exists():
            raise FileNotFoundError(path)

    geometry_payload = json.loads(geometry_path.read_text(encoding="utf-8"))
    geometry = geometry_payload.get("geometry") or geometry_payload
    slotting = json.loads(slotting_path.read_text(encoding="utf-8"))
    route_validation = json.loads(route_path.read_text(encoding="utf-8"))
    product_metadata = _load_fixture_metadata(metadata_path)

    client = OdooWarehouseClient.from_env()
    product_fields = _fields(
        client,
        "product.product",
        ("id", "default_code", "name", "tracking", "x_is_flight_critical"),
    )
    quant_fields = _fields(
        client,
        "stock.quant",
        ("id", "product_id", "location_id", "quantity", "reserved_quantity", "lot_id"),
    )

    products = client.search_read(
        "product.product",
        domain=[["active", "=", True]],
        fields=product_fields,
        limit=args.source_limit,
        order="id asc",
    )
    quants = client.search_read(
        "stock.quant",
        domain=[["quantity", "!=", 0]],
        fields=quant_fields,
        limit=args.source_limit,
        order="location_id asc, product_id asc, id asc",
    )

    result = analyze_relocation_readiness(
        geometry,
        slotting,
        route_validation,
        products,
        quants,
        product_metadata=product_metadata,
        walking_speed_ft_s=args.walking_speed_ft_s,
    )
    result["database"] = client.database
    result["geometry_file"] = str(geometry_path)
    result["slotting_result_file"] = str(slotting_path)
    result["route_validation_file"] = str(route_path)
    result["product_metadata_file"] = str(metadata_path)
    result["source_snapshot"] = {
        "products": len(products),
        "quants": len(quants),
        "source_limit": args.source_limit,
        "truncated_possible": len(products) >= args.source_limit or len(quants) >= args.source_limit,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
