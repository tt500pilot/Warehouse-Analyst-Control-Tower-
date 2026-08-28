from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.aisle_slotting import analyze_aisle_slotting
from odoo_client import OdooWarehouseClient


def _fields(client: OdooWarehouseClient, model: str, wanted: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in wanted if field in available]


def _filter_travel_actionable_recommendations(result: dict[str, Any]) -> None:
    """Keep travel recommendations tied to observed anchor activity and positive travel benefit.

    The underlying geometry service can identify a Pareto-better empty bin even when a SKU
    had no observed source-to-anchor activity in the lookback window. That can be useful for
    a future ergonomics-only analysis, but it is not a valid recommendation for this CLI's
    travel-reduction objective and should not flow into route/economics/pilot gates.
    """

    kept: list[dict[str, Any]] = []
    filtered: list[dict[str, Any]] = []
    for row in result.get("recommendations") or []:
        touches = int(row.get("operational_touches_to_anchor") or 0)
        improvement = row.get("modeled_improvement") or {}
        saved_per_touch = float(improvement.get("graph_distance_saved_per_independent_touch_ft") or 0.0)
        gross = float(improvement.get("gross_independent_touch_distance_potential_ft") or 0.0)

        if touches <= 0:
            filtered.append(
                {
                    "product_code": row.get("product_code"),
                    "reason": "no_observed_anchor_activity_in_lookback",
                    "geometry_only_candidate": True,
                }
            )
            continue
        if saved_per_touch <= 0 or gross <= 0:
            filtered.append(
                {
                    "product_code": row.get("product_code"),
                    "reason": "no_modeled_travel_distance_benefit",
                    "geometry_only_candidate": True,
                }
            )
            continue
        kept.append(row)

    kept.sort(
        key=lambda row: (
            -float((row.get("modeled_improvement") or {}).get("gross_independent_touch_distance_potential_ft") or 0.0),
            -float(row.get("priority_score") or 0.0),
            str(row.get("product_code") or ""),
        )
    )
    for index, row in enumerate(kept, start=1):
        row["rank"] = index

    result["recommendations"] = kept
    result.setdefault("not_recommended", []).extend(filtered)
    summary = result.setdefault("summary", {})
    summary["recommendations"] = len(kept)
    summary["geometry_only_candidates_suppressed"] = len(filtered)
    summary["gross_independent_touch_distance_potential_ft"] = round(
        sum(
            float((row.get("modeled_improvement") or {}).get("gross_independent_touch_distance_potential_ft") or 0.0)
            for row in kept
        ),
        3,
    )
    result.setdefault("methodology", {})["travel_actionability_filter"] = (
        "recommendations require at least one observed source-to-anchor touch in the lookback window "
        "and positive modeled graph-distance benefit; geometry-only/ergonomic candidates are reported "
        "under not_recommended and do not flow into route, economics, or pilot-decision stages"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only mapped-aisle slotting advisor using validated AWIA geometry plus live Odoo data."
    )
    parser.add_argument(
        "--geometry",
        default="data/geometry/aisle-b-geometry.json",
        help="Canonical geometry JSON produced by import_validated_geometry.py",
    )
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument("--source-limit", type=int, default=20000)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument(
        "--output",
        default="",
        help="Optional path to save the full JSON result before top-N display truncation.",
    )
    args = parser.parse_args()
    if args.lookback_days <= 0:
        raise ValueError("--lookback-days must be greater than zero")
    if args.source_limit <= 0:
        raise ValueError("--source-limit must be greater than zero")
    if args.top <= 0:
        raise ValueError("--top must be greater than zero")

    geometry_path = Path(args.geometry)
    if not geometry_path.exists():
        raise FileNotFoundError(geometry_path)
    payload = json.loads(geometry_path.read_text(encoding="utf-8"))
    geometry = payload.get("geometry") or payload
    validation = payload.get("validation")
    if validation is not None and not validation.get("ready_for_geometry_import"):
        raise RuntimeError("Geometry artifact contains a failed validation result")

    client = OdooWarehouseClient.from_env()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=args.lookback_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )

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
            "state",
        ),
    )
    bom_fields = _fields(
        client,
        "mrp.bom.line",
        ("id", "bom_id", "product_id", "product_qty"),
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
    bom_lines = client.search_read(
        "mrp.bom.line",
        domain=[],
        fields=bom_fields,
        limit=args.source_limit,
        order="id asc",
    )

    result = analyze_aisle_slotting(
        geometry,
        products,
        quants,
        moves,
        bom_lines=bom_lines,
        lookback_days=args.lookback_days,
    )
    _filter_travel_actionable_recommendations(result)
    result["database"] = client.database
    result["geometry_file"] = str(geometry_path)
    result["source_snapshot"] = {
        "products": len(products),
        "quants": len(quants),
        "move_lines": len(moves),
        "bom_lines": len(bom_lines),
        "source_limit_per_model": args.source_limit,
        "truncated_possible": any(
            len(rows) >= args.source_limit for rows in (products, quants, moves, bom_lines)
        ),
    }

    if args.output.strip():
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2), encoding="utf-8")
        result["saved_output"] = str(output)

    display_result = dict(result)
    display_result["recommendations"] = result["recommendations"][: args.top]
    display_result["returned_recommendations"] = len(display_result["recommendations"])
    print(json.dumps(display_result, indent=2))


if __name__ == "__main__":
    main()
