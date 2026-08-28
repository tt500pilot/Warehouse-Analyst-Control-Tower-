from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.aisle_route_validation import evaluate_matched_route_impact
from odoo_client import OdooWarehouseClient


def _fields(client: OdooWarehouseClient, model: str, wanted: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in wanted if field in available]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Matched historical-picking route validation for mapped-aisle slotting recommendations."
    )
    parser.add_argument(
        "--geometry",
        default="data/geometry/aisle-b-geometry.json",
    )
    parser.add_argument(
        "--slotting-result",
        default="data/analysis/aisle-b-slotting.json",
        help="Optional saved aisle-slotting JSON. If omitted/not found, run analyze_mapped_aisle_slotting.py with --output first.",
    )
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument(
        "--output",
        default="data/analysis/aisle-b-route-validation.json",
    )
    args = parser.parse_args()

    geometry_path = Path(args.geometry)
    slotting_path = Path(args.slotting_result)
    if not geometry_path.exists():
        raise FileNotFoundError(geometry_path)
    if not slotting_path.exists():
        raise FileNotFoundError(
            f"{slotting_path}. Save the aisle slotting analysis to this path first."
        )

    geometry_payload = json.loads(geometry_path.read_text(encoding="utf-8"))
    geometry = geometry_payload.get("geometry") or geometry_payload
    slotting = json.loads(slotting_path.read_text(encoding="utf-8"))
    recommendations = slotting.get("recommendations") or []
    if not recommendations:
        raise RuntimeError("Slotting result has no recommendations to validate")

    client = OdooWarehouseClient.from_env()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.lookback_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    move_fields = _fields(
        client,
        "stock.move.line",
        (
            "id",
            "picking_id",
            "product_id",
            "location_id",
            "location_dest_id",
            "quantity",
            "qty_done",
            "date",
            "state",
        ),
    )
    moves = client.search_read(
        "stock.move.line",
        domain=[["date", ">=", cutoff]],
        fields=move_fields,
        limit=args.source_limit,
        order="date asc, id asc",
    )

    result = evaluate_matched_route_impact(geometry, moves, recommendations)
    result["database"] = client.database
    result["geometry_file"] = str(geometry_path)
    result["slotting_result_file"] = str(slotting_path)
    result["lookback_days"] = args.lookback_days
    result["source_snapshot"] = {
        "move_lines": len(moves),
        "source_limit": args.source_limit,
        "truncated_possible": len(moves) >= args.source_limit,
    }

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
