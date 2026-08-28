"""Prepare native Odoo lot/serial stock for the active AWIA kitting workload.

The first AWIA sandbox seed intentionally proved basic inventory connectivity.
It created on-hand quantities even for tracked products without lot/serial IDs.
This migration upgrades only the quantity needed by currently open AWIA Pick
Components transfers.

On --apply it releases only missing-traceability reservations, temporarily
isolates the relevant anonymous tracked stock, creates deterministic lot/serial
stock equal to current demand, asks Odoo to reserve it, then restores the
anonymous remainder. Total managed on-hand quantity is preserved.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from odoo_client import OdooWarehouseClient
from scripts.seed_odoo_sandbox import assert_write_guard

DEFAULT_ORIGIN_PREFIX = "AWIA-MOCK-MO-"
LOT_PREFIX = "A"


def _m2o_id(value: Any) -> int | None:
    """Normalize an Odoo many2one value without treating False as integer 0."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if (
        isinstance(value, (list, tuple))
        and value
        and isinstance(value[0], int)
        and not isinstance(value[0], bool)
    ):
        return value[0]
    return None


def _m2o_label(value: Any) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    if isinstance(value, str):
        return value
    return None


def _number(value: Any) -> float:
    return float(value) if isinstance(value, (int, float)) else 0.0


def _safe_token(value: str, *, max_len: int = 18) -> str:
    token = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-") or "X"
    return token[:max_len]


def _integer_units(quantity: float, *, product_code: str) -> int:
    rounded = round(quantity)
    if not math.isclose(quantity, rounded, abs_tol=1e-9):
        raise RuntimeError(
            f"Serial-tracked product {product_code} has non-integer required quantity {quantity}."
        )
    if rounded < 0:
        raise RuntimeError(
            f"Serial-tracked product {product_code} has negative required quantity {quantity}."
        )
    return int(rounded)


def _lot_name(product_code: str, location_id: int) -> str:
    return f"{LOT_PREFIX}-{_safe_token(product_code)}-L{location_id}"


def _serial_name(product_code: str, location_id: int, sequence: int) -> str:
    return f"{LOT_PREFIX}-{_safe_token(product_code)}-{location_id}-S{sequence:04d}"


def build_tracking_targets(demands: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Expand product/location demand into deterministic lot/serial target rows."""
    targets: list[dict[str, Any]] = []
    ordered = sorted(
        demands,
        key=lambda row: (str(row["product_code"]), int(row["location_id"])),
    )
    for row in ordered:
        code = str(row["product_code"])
        tracking = str(row["tracking"])
        location_id = int(row["location_id"])
        quantity = float(row["quantity"])
        if quantity <= 0:
            continue
        if tracking == "lot":
            targets.append(
                {
                    "product_id": int(row["product_id"]),
                    "product_code": code,
                    "tracking": tracking,
                    "location_id": location_id,
                    "location": row.get("location"),
                    "lot_name": _lot_name(code, location_id),
                    "quantity": quantity,
                }
            )
            continue
        if tracking != "serial":
            continue
        units = _integer_units(quantity, product_code=code)
        for sequence in range(1, units + 1):
            targets.append(
                {
                    "product_id": int(row["product_id"]),
                    "product_code": code,
                    "tracking": tracking,
                    "location_id": location_id,
                    "location": row.get("location"),
                    "lot_name": _serial_name(code, location_id, sequence),
                    "quantity": 1.0,
                }
            )
    return targets


def merge_product_tracking(
    products: list[dict[str, Any]],
    moves: list[dict[str, Any]],
) -> dict[int, dict[str, Any]]:
    """Prefer stock.move.has_tracking, matching the readiness inspector."""
    tracking_by_product: dict[int, str] = {}
    for move in moves:
        product_id = _m2o_id(move.get("product_id"))
        tracking = str(move.get("has_tracking") or "none")
        if product_id is not None and tracking in {"lot", "serial"}:
            tracking_by_product[product_id] = tracking

    result: dict[int, dict[str, Any]] = {}
    for product in products:
        product_id = product.get("id")
        if not isinstance(product_id, int) or isinstance(product_id, bool):
            continue
        enriched = dict(product)
        if product_id in tracking_by_product:
            enriched["tracking"] = tracking_by_product[product_id]
        result[product_id] = enriched
    return result


def _search_one(
    client: OdooWarehouseClient,
    model: str,
    domain: list[Any],
    fields: list[str],
) -> dict[str, Any] | None:
    available = set(client.available_fields(model))
    selected = [field for field in fields if field in available]
    rows = client.search_read(model, domain=domain, fields=selected, limit=1)
    return rows[0] if rows else None


def _print_progress(label: str, current: int, total: int) -> None:
    if current == 1 or current == total or current % 25 == 0:
        print(f"{label}: {current}/{total}", flush=True)


def _resolve_awia_kitting_scope(
    client: OdooWarehouseClient,
    *,
    origin_prefix: str,
) -> dict[str, Any]:
    mo_fields = ["id", "name", "origin", "state", "picking_ids"]
    mos = client.search_read(
        "mrp.production",
        domain=[["origin", "=ilike", f"{origin_prefix}%"]],
        fields=[field for field in mo_fields if field in set(client.available_fields("mrp.production"))],
        limit=500,
        order="id asc",
    )
    picking_ids = sorted(
        {
            picking_id
            for mo in mos
            for picking_id in (mo.get("picking_ids") or [])
            if isinstance(picking_id, int) and not isinstance(picking_id, bool)
        }
    )
    if not picking_ids:
        raise RuntimeError("No AWIA manufacturing Pick Components transfers were found.")

    picking_fields = ["id", "name", "origin", "state", "picking_type_id"]
    pickings = client.search_read(
        "stock.picking",
        domain=[["id", "in", picking_ids]],
        fields=[field for field in picking_fields if field in set(client.available_fields("stock.picking"))],
        limit=500,
        order="id asc",
    )
    pbm_pickings = [
        row
        for row in pickings
        if "pick components" in (_m2o_label(row.get("picking_type_id")) or "").lower()
    ]
    if not pbm_pickings:
        raise RuntimeError("AWIA MOs exist but no native Pick Components transfers were resolved.")
    forbidden = [row for row in pbm_pickings if row.get("state") in {"done", "cancel"}]
    if forbidden:
        names = ", ".join(str(row.get("name")) for row in forbidden)
        raise RuntimeError(
            f"Refusing tracking migration because AWIA Pick Components transfers are already done/cancelled: {names}."
        )

    pbm_ids = [
        int(row["id"])
        for row in pbm_pickings
        if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
    ]
    move_fields = ["id", "picking_id", "product_id", "has_tracking", "state"]
    moves = client.search_read(
        "stock.move",
        domain=[["picking_id", "in", pbm_ids], ["state", "not in", ["done", "cancel"]]],
        fields=[field for field in move_fields if field in set(client.available_fields("stock.move"))],
        limit=5000,
        order="picking_id asc, id asc",
    )
    product_ids = sorted(
        {
            product_id
            for row in moves
            for product_id in [_m2o_id(row.get("product_id"))]
            if product_id is not None
        }
    )
    product_fields = [
        field
        for field in ("id", "default_code", "tracking")
        if field in set(client.available_fields("product.product"))
    ]
    products = client.search_read(
        "product.product",
        domain=[["id", "in", product_ids]],
        fields=product_fields,
        limit=5000,
        order="id asc",
    )
    product_by_id = merge_product_tracking(products, moves)

    line_fields = [
        "id",
        "move_id",
        "picking_id",
        "product_id",
        "quantity",
        "lot_id",
        "location_id",
        "state",
    ]
    reservation_lines = client.search_read(
        "stock.move.line",
        domain=[["picking_id", "in", pbm_ids], ["state", "not in", ["done", "cancel"]]],
        fields=[field for field in line_fields if field in set(client.available_fields("stock.move.line"))],
        limit=10000,
        order="picking_id asc, id asc",
    )

    missing_lines: list[dict[str, Any]] = []
    for line in reservation_lines:
        product_id = _m2o_id(line.get("product_id"))
        product = product_by_id.get(product_id or -1)
        tracking = str((product or {}).get("tracking") or "none")
        if tracking not in {"lot", "serial"}:
            continue
        if _number(line.get("quantity")) <= 0:
            continue
        if _m2o_id(line.get("lot_id")) is not None:
            continue
        missing_lines.append(line)

    return {
        "manufacturing_orders": mos,
        "pickings": pbm_pickings,
        "picking_ids": pbm_ids,
        "moves": moves,
        "products": products,
        "product_by_id": product_by_id,
        "reservation_lines": reservation_lines,
        "missing_tracking_lines": missing_lines,
        "tracking_source": "stock.move.has_tracking",
    }


def _build_demands(scope: dict[str, Any]) -> list[dict[str, Any]]:
    grouped: dict[tuple[int, int], float] = defaultdict(float)
    location_labels: dict[int, str | None] = {}
    for line in scope["missing_tracking_lines"]:
        product_id = _m2o_id(line.get("product_id"))
        location_id = _m2o_id(line.get("location_id"))
        if product_id is None or location_id is None:
            raise RuntimeError("A tracked reservation line is missing product/location identity.")
        grouped[(product_id, location_id)] += _number(line.get("quantity"))
        location_labels[location_id] = _m2o_label(line.get("location_id"))

    demands: list[dict[str, Any]] = []
    for (product_id, location_id), quantity in sorted(grouped.items()):
        product = scope["product_by_id"].get(product_id)
        if not product or not product.get("default_code"):
            raise RuntimeError(f"Tracked product {product_id} is missing a stable default_code.")
        demands.append(
            {
                "product_id": product_id,
                "product_code": str(product["default_code"]),
                "tracking": str(product.get("tracking") or "none"),
                "location_id": location_id,
                "location": location_labels.get(location_id),
                "quantity": round(quantity, 6),
            }
        )
    return demands


def _ensure_lot(
    client: OdooWarehouseClient,
    *,
    product_id: int,
    lot_name: str,
) -> tuple[int, str]:
    existing = _search_one(
        client,
        "stock.lot",
        [["product_id", "=", product_id], ["name", "=", lot_name]],
        ["id", "name", "product_id"],
    )
    if existing and isinstance(existing.get("id"), int) and not isinstance(existing.get("id"), bool):
        return int(existing["id"]), "existing"
    lot_id = client.execute_kw(
        "stock.lot",
        "create",
        args=[{"name": lot_name, "product_id": product_id}],
    )
    return int(lot_id), "created"


def _read_quant_total(
    client: OdooWarehouseClient,
    *,
    product_id: int,
    location_id: int,
    lot_name_prefix: str | None,
) -> float:
    domain: list[Any] = [
        ["product_id", "=", product_id],
        ["location_id", "=", location_id],
    ]
    if lot_name_prefix is None:
        domain.append(["lot_id", "=", False])
    else:
        domain.extend([["lot_id", "!=", False], ["lot_id.name", "=ilike", f"{lot_name_prefix}%"]])
    rows = client.search_read(
        "stock.quant",
        domain=domain,
        fields=["id", "quantity", "reserved_quantity", "lot_id"],
        limit=10000,
    )
    return round(sum(_number(row.get("quantity")) for row in rows), 6)


def _set_quant_target(
    client: OdooWarehouseClient,
    *,
    product_id: int,
    location_id: int,
    lot_id: int | None,
    quantity: float,
) -> str:
    domain: list[Any] = [
        ["product_id", "=", product_id],
        ["location_id", "=", location_id],
        ["lot_id", "=", lot_id if lot_id is not None else False],
    ]
    existing = _search_one(
        client,
        "stock.quant",
        domain,
        ["id", "quantity", "reserved_quantity", "lot_id"],
    )
    context = {"inventory_mode": True}
    if existing and isinstance(existing.get("id"), int) and not isinstance(existing.get("id"), bool):
        client.execute_kw(
            "stock.quant",
            "write",
            args=[[int(existing["id"])], {"inventory_quantity_auto_apply": quantity}],
            kwargs={"context": context},
        )
        return "updated"
    values: dict[str, Any] = {
        "product_id": product_id,
        "location_id": location_id,
        "inventory_quantity_auto_apply": quantity,
    }
    if lot_id is not None:
        values["lot_id"] = lot_id
    client.execute_kw(
        "stock.quant",
        "create",
        args=[values],
        kwargs={"context": context},
    )
    return "created"


def _remaining_missing_tracking_lines(
    client: OdooWarehouseClient,
    *,
    picking_ids: list[int],
    tracked_product_ids: list[int],
) -> int:
    if not tracked_product_ids:
        return 0
    rows = client.search_read(
        "stock.move.line",
        domain=[
            ["picking_id", "in", picking_ids],
            ["product_id", "in", tracked_product_ids],
            ["state", "not in", ["done", "cancel"]],
            ["quantity", ">", 0],
            ["lot_id", "=", False],
        ],
        fields=["id"],
        limit=10000,
    )
    return len(rows)


def migrate_tracking(
    *,
    origin_prefix: str = DEFAULT_ORIGIN_PREFIX,
    apply: bool = False,
) -> dict[str, Any]:
    client = OdooWarehouseClient.from_env()
    assert_write_guard(client.database, apply=apply)
    scope = _resolve_awia_kitting_scope(client, origin_prefix=origin_prefix)
    demands = _build_demands(scope)
    targets = build_tracking_targets(demands)

    lot_targets = [row for row in targets if row["tracking"] == "lot"]
    serial_targets = [row for row in targets if row["tracking"] == "serial"]
    tracked_product_ids = sorted({int(row["product_id"]) for row in demands})

    pair_snapshots: dict[tuple[int, int], dict[str, Any]] = {}
    for demand in demands:
        product_id = int(demand["product_id"])
        location_id = int(demand["location_id"])
        prefix = f"{LOT_PREFIX}-{_safe_token(str(demand['product_code']))}-"
        anonymous = _read_quant_total(
            client,
            product_id=product_id,
            location_id=location_id,
            lot_name_prefix=None,
        )
        deterministic_traced = _read_quant_total(
            client,
            product_id=product_id,
            location_id=location_id,
            lot_name_prefix=prefix,
        )
        total_managed = round(anonymous + deterministic_traced, 6)
        required = float(demand["quantity"])
        if total_managed + 1e-6 < required:
            raise RuntimeError(
                f"Insufficient managed stock for {demand['product_code']} at {demand.get('location') or location_id}: "
                f"required={required}, available={total_managed}."
            )
        pair_snapshots[(product_id, location_id)] = {
            **demand,
            "anonymous_before": anonymous,
            "deterministic_traced_before": deterministic_traced,
            "total_managed_before": total_managed,
            "anonymous_remainder": round(total_managed - required, 6),
        }

    summary: dict[str, Any] = {
        "database": client.database,
        "mode": "apply" if apply else "dry_run",
        "tracking_source": scope["tracking_source"],
        "awia_pick_component_transfers": len(scope["pickings"]),
        "reservation_lines_total": len(scope["reservation_lines"]),
        "missing_tracking_lines_to_rebuild": len(scope["missing_tracking_lines"]),
        "tracked_products_in_scope": len(tracked_product_ids),
        "product_location_demands": len(demands),
        "required_traced_quantity": round(sum(float(row["quantity"]) for row in demands), 6),
        "lot_tracked_products": len({row["product_id"] for row in lot_targets}),
        "serial_tracked_products": len({row["product_id"] for row in serial_targets}),
        "target_lot_records": len(lot_targets),
        "target_serial_records": len(serial_targets),
        "target_traced_quants": len(targets),
        "full_sandbox_serialization_avoided": True,
        "lots": {"created": 0, "existing": 0},
        "traced_quants": {"created": 0, "updated": 0},
        "anonymous_quants": {"created": 0, "updated": 0},
        "reassigned": False,
        "missing_tracking_lines_after_reassign": None,
    }
    if not demands:
        summary["note"] = "All open tracked AWIA reservation lines already have lot/serial evidence."
        return summary
    if not apply:
        return summary

    missing_line_ids = [
        int(row["id"])
        for row in scope["missing_tracking_lines"]
        if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
    ]
    client.execute_kw("stock.move.line", "unlink", args=[missing_line_ids])
    print(f"Anonymous tracked reservations released: {len(missing_line_ids)}", flush=True)

    for index, snapshot in enumerate(pair_snapshots.values(), start=1):
        action = _set_quant_target(
            client,
            product_id=int(snapshot["product_id"]),
            location_id=int(snapshot["location_id"]),
            lot_id=None,
            quantity=0.0,
        )
        summary["anonymous_quants"][action] += 1
        _print_progress("Anonymous inventory isolated", index, len(pair_snapshots))

    for index, target in enumerate(targets, start=1):
        lot_id, lot_action = _ensure_lot(
            client,
            product_id=int(target["product_id"]),
            lot_name=str(target["lot_name"]),
        )
        summary["lots"][lot_action] += 1
        quant_action = _set_quant_target(
            client,
            product_id=int(target["product_id"]),
            location_id=int(target["location_id"]),
            lot_id=lot_id,
            quantity=float(target["quantity"]),
        )
        summary["traced_quants"][quant_action] += 1
        _print_progress("Traced inventory", index, len(targets))

    affected_picking_ids = sorted(
        {
            picking_id
            for row in scope["missing_tracking_lines"]
            for picking_id in [_m2o_id(row.get("picking_id"))]
            if picking_id is not None
        }
    )
    client.execute_kw("stock.picking", "action_assign", args=[affected_picking_ids])
    summary["reassigned"] = True

    for index, snapshot in enumerate(pair_snapshots.values(), start=1):
        action = _set_quant_target(
            client,
            product_id=int(snapshot["product_id"]),
            location_id=int(snapshot["location_id"]),
            lot_id=None,
            quantity=float(snapshot["anonymous_remainder"]),
        )
        summary["anonymous_quants"][action] += 1
        _print_progress("Anonymous inventory restored", index, len(pair_snapshots))

    summary["missing_tracking_lines_after_reassign"] = _remaining_missing_tracking_lines(
        client,
        picking_ids=scope["picking_ids"],
        tracked_product_ids=tracked_product_ids,
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare demand-scoped lot/serial stock for AWIA Pick Components transfers."
    )
    parser.add_argument("--origin-prefix", default=DEFAULT_ORIGIN_PREFIX)
    parser.add_argument("--apply", action="store_true", help="Write to Odoo. Default is dry-run.")
    args = parser.parse_args()
    result = migrate_tracking(origin_prefix=args.origin_prefix, apply=args.apply)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
