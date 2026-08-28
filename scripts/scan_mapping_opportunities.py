from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.mapping_prioritizer import analyze_mapping_priorities
from odoo_client import OdooWarehouseClient


def _fields(client: OdooWarehouseClient, model: str, wanted: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in wanted if field in available]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only Odoo scan that ranks which warehouse area should be physically mapped first."
    )
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument("--top", type=int, default=10)
    args = parser.parse_args()
    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero")
    if args.source_limit <= 0:
        raise ValueError("--source-limit must be greater than zero")
    if args.top <= 0:
        raise ValueError("--top must be greater than zero")

    client = OdooWarehouseClient.from_env()
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=args.lookback_days)).strftime("%Y-%m-%d %H:%M:%S")

    product_fields = _fields(
        client,
        "product.product",
        ("id", "default_code", "name", "standard_price", "tracking", "x_is_flight_critical"),
    )
    quant_fields = _fields(
        client,
        "stock.quant",
        ("id", "product_id", "location_id", "quantity", "reserved_quantity"),
    )
    move_fields = _fields(
        client,
        "stock.move.line",
        (
            "id",
            "product_id",
            "location_id",
            "location_dest_id",
            "quantity",
            "qty_done",
            "date",
            "write_date",
            "state",
        ),
    )
    location_fields = _fields(
        client,
        "stock.location",
        ("id", "complete_name", "display_name", "name", "usage", "barcode"),
    )
    bom_line_fields = _fields(
        client,
        "mrp.bom.line",
        ("id", "bom_id", "product_id", "product_qty", "active"),
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
    moves = client.search_read(
        "stock.move.line",
        domain=[["date", ">=", cutoff]],
        fields=move_fields,
        limit=args.source_limit,
        order="date asc, id asc",
    )
    locations = client.search_read(
        "stock.location",
        domain=[["usage", "=", "internal"]],
        fields=location_fields,
        limit=args.source_limit,
        order="id asc",
    )
    bom_lines = client.search_read(
        "mrp.bom.line",
        domain=[],
        fields=bom_line_fields,
        limit=args.source_limit,
        order="id asc",
    )

    result = analyze_mapping_priorities(
        products,
        quants,
        moves,
        locations,
        bom_lines=bom_lines,
        as_of=now,
        lookback_days=args.lookback_days,
    )
    result["database"] = client.database
    result["source_snapshot"] = {
        "products": len(products),
        "quants": len(quants),
        "move_lines": len(moves),
        "locations": len(locations),
        "bom_lines": len(bom_lines),
        "source_limit_per_model": args.source_limit,
        "truncated_possible": any(
            len(rows) >= args.source_limit
            for rows in (products, quants, moves, locations, bom_lines)
        ),
    }
    result["areas"] = result["areas"][: args.top]
    result["returned_areas"] = len(result["areas"])
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
