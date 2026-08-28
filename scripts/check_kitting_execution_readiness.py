"""Read-only preflight for AWIA native Odoo Pick Components execution.

The preflight deliberately performs no mutations. It verifies that the AWIA
manufacturing orders are linked to native Odoo Pick Components transfers, that
all component demand is reserved, and that every lot/serial-tracked reservation
line has native traceability evidence before any execution simulator is allowed
to validate transfers.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.kitting_transactions import inspect_kitting_transactions
from odoo_client import OdooWarehouseClient

MO_FIELDS = (
    "id",
    "name",
    "origin",
    "state",
    "product_id",
    "bom_id",
    "picking_ids",
)

PICKING_FIELDS = (
    "id",
    "name",
    "origin",
    "state",
    "picking_type_id",
    "location_id",
    "location_dest_id",
    "create_date",
    "scheduled_date",
    "date_done",
)

MOVE_FIELDS = (
    "id",
    "picking_id",
    "product_id",
    "product_uom_qty",
    "quantity",
    "state",
    "picked",
    "has_tracking",
    "location_id",
    "location_dest_id",
)

MOVE_LINE_FIELDS = (
    "id",
    "move_id",
    "picking_id",
    "product_id",
    "quantity",
    "picked",
    "tracking",
    "lot_id",
    "lot_name",
    "location_id",
    "location_dest_id",
    "state",
    "date",
)


def _resolved_fields(client: OdooWarehouseClient, model: str, requested: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in requested if field in available]


def build_readiness_report(
    client: OdooWarehouseClient,
    *,
    origin_prefix: str = "AWIA-MOCK-MO-",
    source_limit: int = 5000,
) -> dict[str, Any]:
    mos = client.search_read(
        "mrp.production",
        domain=[["origin", "=ilike", f"{origin_prefix}%"]] if origin_prefix else [],
        fields=_resolved_fields(client, "mrp.production", MO_FIELDS),
        limit=source_limit,
        order="id asc",
    )

    picking_ids = sorted(
        {
            picking_id
            for mo in mos
            for picking_id in (mo.get("picking_ids") or [])
            if isinstance(picking_id, int)
        }
    )
    pickings: list[dict[str, Any]] = []
    moves: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    if picking_ids:
        pickings = client.search_read(
            "stock.picking",
            domain=[["id", "in", picking_ids]],
            fields=_resolved_fields(client, "stock.picking", PICKING_FIELDS),
            limit=source_limit,
            order="id asc",
        )
        resolved_picking_ids = [
            row["id"] for row in pickings if isinstance(row.get("id"), int)
        ]
        if resolved_picking_ids:
            moves = client.search_read(
                "stock.move",
                domain=[["picking_id", "in", resolved_picking_ids]],
                fields=_resolved_fields(client, "stock.move", MOVE_FIELDS),
                limit=source_limit,
                order="picking_id asc, id asc",
            )
            lines = client.search_read(
                "stock.move.line",
                domain=[["picking_id", "in", resolved_picking_ids]],
                fields=_resolved_fields(client, "stock.move.line", MOVE_LINE_FIELDS),
                limit=source_limit,
                order="picking_id asc, id asc",
            )

    report = inspect_kitting_transactions(
        mos,
        pickings,
        moves,
        lines,
        origin_prefix=origin_prefix,
        picking_type_contains="Pick Components",
    )
    report["source_snapshot"] = {
        "manufacturing_orders": len(mos),
        "pickings": len(pickings),
        "stock_moves": len(moves),
        "move_lines": len(lines),
        "source_limit_per_model": source_limit,
    }
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Check AWIA Odoo kitting execution readiness without making changes.")
    parser.add_argument("--origin-prefix", default="AWIA-MOCK-MO-")
    parser.add_argument("--source-limit", type=int, default=5000)
    parser.add_argument(
        "--details",
        action="store_true",
        help="Print the complete transaction report instead of only the summary.",
    )
    args = parser.parse_args()

    client = OdooWarehouseClient.from_env()
    report = build_readiness_report(
        client,
        origin_prefix=args.origin_prefix,
        source_limit=args.source_limit,
    )
    payload = report if args.details else report["summary"]
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
