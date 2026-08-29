from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

Record = Mapping[str, Any]


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, (list, tuple)) and value:
        first = value[0]
        if isinstance(first, int) and not isinstance(first, bool) and first > 0:
            return int(first)
    return None


def _m2o_name(value: Any) -> str:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1] or "")
    return ""


def _number(value: Any) -> float:
    if value in (None, False, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def build_traceability_block_index(report: Record) -> dict[tuple[int, int], dict[str, Any]]:
    """Index blocked product/location positions for upstream decision gates."""
    blocks: dict[tuple[int, int], dict[str, Any]] = {}
    for row in report.get("items") or []:
        if not row.get("blocked_from_relocation_analysis"):
            continue
        product_id = _m2o_id(row.get("product_id"))
        location_id = _m2o_id(row.get("location_id"))
        if product_id is None or location_id is None:
            continue
        blocks[(product_id, location_id)] = dict(row)
    return blocks


def analyze_traceability_health(
    products: Iterable[Record],
    quants: Iterable[Record],
    *,
    location_prefix: str = "",
) -> dict[str, Any]:
    """Assess lot/serial identification coverage for positive on-hand inventory.

    The analysis is read-only. It does not create lots/serials, change quantities,
    release reservations, or repair Odoo records. Any tracked product/location with
    positive quantity lacking lot/serial identity is blocked from relocation analysis.
    """

    prefix = str(location_prefix or "").strip()
    products_by_id = {
        product_id: row
        for row in products
        for product_id in [_m2o_id(row.get("id"))]
        if product_id is not None
    }

    positions: dict[tuple[int, int], dict[str, Any]] = defaultdict(
        lambda: {
            "quantity": 0.0,
            "reserved_quantity": 0.0,
            "identified_quantity": 0.0,
            "anonymous_quantity": 0.0,
            "lot_ids": set(),
            "quant_rows": 0,
            "location_name": "",
        }
    )

    for quant in quants:
        product_id = _m2o_id(quant.get("product_id"))
        location_id = _m2o_id(quant.get("location_id"))
        if product_id is None or location_id is None:
            continue

        product = products_by_id.get(product_id, {})
        tracking = str(product.get("tracking") or "none").strip().lower()
        if tracking not in {"lot", "serial"}:
            continue

        quantity = _number(quant.get("quantity"))
        if quantity <= 0:
            continue

        location_name = _m2o_name(quant.get("location_id"))
        if prefix and not location_name.startswith(prefix):
            continue

        reserved = max(_number(quant.get("reserved_quantity")), 0.0)
        lot_id = _m2o_id(quant.get("lot_id"))
        state = positions[(product_id, location_id)]
        state["quantity"] += quantity
        state["reserved_quantity"] += reserved
        state["quant_rows"] += 1
        state["location_name"] = location_name
        if lot_id is None:
            state["anonymous_quantity"] += quantity
        else:
            state["identified_quantity"] += quantity
            state["lot_ids"].add(lot_id)

    rows: list[dict[str, Any]] = []
    for (product_id, location_id), state in positions.items():
        product = products_by_id.get(product_id, {})
        quantity = float(state["quantity"])
        identified = float(state["identified_quantity"])
        anonymous = float(state["anonymous_quantity"])
        reserved = float(state["reserved_quantity"])
        coverage = identified / quantity * 100.0 if quantity > 0 else 100.0
        tracking = str(product.get("tracking") or "none").strip().lower()
        blocked = anonymous > 0.000001
        reasons: list[str] = []
        if blocked:
            reasons.append("positive_tracked_quantity_without_lot_or_serial_identity")
        if reserved > 0:
            reasons.append("live_reservation_present")

        rows.append(
            {
                "status": "BLOCKED_TRACEABILITY" if blocked else "TRACEABILITY_COMPLETE",
                "blocked_from_relocation_analysis": blocked,
                "product_id": product_id,
                "product_code": str(product.get("default_code") or ""),
                "product_name": str(product.get("name") or ""),
                "tracking": tracking,
                "location_id": location_id,
                "location_name": str(state["location_name"] or ""),
                "on_hand_quantity": round(quantity, 3),
                "reserved_quantity": round(reserved, 3),
                "identified_quantity": round(identified, 3),
                "anonymous_quantity": round(anonymous, 3),
                "traceability_coverage_pct": round(coverage, 2),
                "lot_or_serial_ids": sorted(state["lot_ids"]),
                "quant_rows": int(state["quant_rows"]),
                "reasons": reasons,
            }
        )

    rows.sort(
        key=lambda row: (
            0 if row["blocked_from_relocation_analysis"] else 1,
            -float(row["anonymous_quantity"]),
            -float(row["reserved_quantity"]),
            row["product_code"],
            row["location_name"],
        )
    )

    tracked_products = {row["product_id"] for row in rows}
    blocked_products = {
        row["product_id"] for row in rows if row["blocked_from_relocation_analysis"]
    }
    total_qty = sum(float(row["on_hand_quantity"]) for row in rows)
    identified_qty = sum(float(row["identified_quantity"]) for row in rows)
    anonymous_qty = sum(float(row["anonymous_quantity"]) for row in rows)
    coverage = identified_qty / total_qty * 100.0 if total_qty > 0 else 100.0

    return {
        "mode": "read_only_traceability_health",
        "odoo_mutated": False,
        "safe_to_execute_inventory_moves": False,
        "location_prefix": prefix or None,
        "summary": {
            "tracked_inventory_positions": len(rows),
            "tracked_products": len(tracked_products),
            "blocked_positions": sum(
                1 for row in rows if row["blocked_from_relocation_analysis"]
            ),
            "blocked_products": len(blocked_products),
            "positions_with_live_reservations": sum(
                1 for row in rows if float(row["reserved_quantity"]) > 0
            ),
            "total_tracked_on_hand_quantity": round(total_qty, 3),
            "identified_quantity": round(identified_qty, 3),
            "anonymous_quantity": round(anonymous_qty, 3),
            "traceability_coverage_pct": round(coverage, 2),
        },
        "items": rows,
        "guardrails": [
            "A blocked position is a data-quality/traceability gate, not an instruction to adjust inventory.",
            "Anonymous tracked quantity must be reconciled through approved inventory/quality procedures before relocation analysis can rely on it.",
            "Live reservations remain a separate operational control even when lot/serial coverage is complete.",
            "This analysis performs no Odoo writes.",
        ],
    }
