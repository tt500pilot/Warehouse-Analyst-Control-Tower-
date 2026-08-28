"""Convert AWIA sandbox tracked inventory into native Odoo lot/serial stock.

The original master-data sandbox intentionally proved inventory connectivity
first.  This follow-on migration upgrades the tracked components needed by the
AWIA manufacturing/kitting scenario so Odoo can reserve real lot/serial stock.

Workflow on --apply:
1. Identify AWIA Pick Components transfers and the tracked products they use.
2. Unreserve those open transfers by unlinking their reservation move lines.
   Odoo's stock.move.line.unlink() natively frees the reserved quants.
3. Create deterministic stock.lot records and lot-specific stock.quant inventory
   using the fixture's target quantities and locations.
4. Zero the old anonymous (lot_id=False) quants for those same product/location
   targets so future reservation cannot silently fall back to untracked stock.
5. Call stock.picking.action_assign() so Odoo rebuilds reservations from the
   traced inventory.

Safety properties:
- dry-run by default
- same sandbox write guard as the other seeders
- refuses to touch done/cancelled AWIA Pick Components transfers
- deterministic lot/serial names and target quantities make reruns idempotent
- never bypasses Odoo validation or directly updates PostgreSQL
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from odoo_client import OdooWarehouseClient
from scripts.generate_simulation_sandbox import generate
from scripts.seed_odoo_sandbox import assert_write_guard

DEFAULT_DATA_DIR = ROOT_DIR / "data" / "simulation_sandbox"
DEFAULT_ORIGIN_PREFIX = "AWIA-MOCK-MO-"
LOT_PREFIX = "A"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
        return value[0]
    return None


def _m2o_label(value: Any) -> str | None:
    if isinstance(value, (list, tuple)) and len(value) > 1:
        return str(value[1])
    if isinstance(value, str):
        return value
    return None


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


def _print_progress(label: str, current: int, total: int, *, apply: bool) -> None:
    if apply and (current == 1 or current == total or current % 25 == 0):
        print(f"{label}: {current}/{total}", flush=True)


def _integer_units(quantity: float, *, product_code: str) -> int:
    rounded = round(quantity)
    if not math.isclose(quantity, rounded, abs_tol=1e-9):
        raise RuntimeError(
            f"Serial-tracked product {product_code} has non-integer sandbox quantity {quantity}."
        )
    if rounded < 0:
        raise RuntimeError(
            f"Serial-tracked product {product_code} has negative sandbox quantity {quantity}."
        )
    return int(rounded)


def _lot_name(product_code: str, location_sequence: int) -> str:
    return f"{LOT_PREFIX}-{product_code}-L{location_sequence:02d}"


def _serial_name(product_code: str, serial_sequence: int) -> str:
    return f"{LOT_PREFIX}-{product_code}-S{serial_sequence:04d}"


def build_tracking_targets(
    fixture_quants: list[dict[str, str]],
    *,
    relevant_tracking_by_code: dict[str, str],
) -> list[dict[str, Any]]:
    """Expand fixture quants into deterministic desired lot/serial stock rows."""
    rows_by_product: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in fixture_quants:
        code = row["product_code"]
        if relevant_tracking_by_code.get(code) in {"lot", "serial"}:
            rows_by_product[code].append(row)

    targets: list[dict[str, Any]] = []
    for code in sorted(rows_by_product):
        tracking = relevant_tracking_by_code[code]
        product_rows = sorted(rows_by_product[code], key=lambda row: row["location_code"])
        serial_sequence = 0
        for location_sequence, row in enumerate(product_rows, start=1):
            quantity = float(row["quantity"])
            if tracking == "lot":
                targets.append(
                    {
                        "product_code": code,
                        "tracking": tracking,
                        "location_code": row["location_code"],
                        "lot_name": _lot_name(code, location_sequence),
                        "quantity": quantity,
                    }
                )
                continue

            units = _integer_units(quantity, product_code=code)
            for _ in range(units):
                serial_sequence += 1
                targets.append(
                    {
                        "product_code": code,
                        "tracking": tracking,
                        "location_code": row["location_code"],
                        "lot_name": _serial_name(code, serial_sequence),
                        "quantity": 1.0,
                    }
                )
    return targets


def _load_location_barcodes(data_dir: Path) -> dict[str, str]:
    return {
        row["odoo_complete_name"]: row["barcode"]
        for row in _read_csv(data_dir / "mock_warehouse" / "locations.csv")
    }


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
            if isinstance(picking_id, int)
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

    pbm_ids = [int(row["id"]) for row in pbm_pickings if isinstance(row.get("id"), int)]
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
    product_fields = ["id", "default_code", "tracking"]
    products = client.search_read(
        "product.product",
        domain=[["id", "in", product_ids]],
        fields=[field for field in product_fields if field in set(client.available_fields("product.product"))],
        limit=5000,
        order="id asc",
    )
    tracking_by_code = {
        str(row.get("default_code")): str(row.get("tracking") or "none")
        for row in products
        if row.get("default_code") and row.get("tracking") in {"lot", "serial"}
    }

    line_fields = ["id", "picking_id", "product_id", "quantity", "lot_id"]
    reservation_lines = client.search_read(
        "stock.move.line",
        domain=[["picking_id", "in", pbm_ids], ["state", "not in", ["done", "cancel"]]],
        fields=[field for field in line_fields if field in set(client.available_fields("stock.move.line"))],
        limit=10000,
        order="picking_id asc, id asc",
    )

    return {
        "manufacturing_orders": mos,
        "pickings": pbm_pickings,
        "picking_ids": pbm_ids,
        "moves": moves,
        "products": products,
        "tracking_by_code": tracking_by_code,
        "reservation_lines": reservation_lines,
    }


def _resolve_product_ids(client: OdooWarehouseClient, codes: set[str]) -> dict[str, int]:
    rows = client.search_read(
        "product.product",
        domain=[["default_code", "in", sorted(codes)]],
        fields=["id", "default_code"],
        limit=max(100, len(codes) * 2),
    )
    return {
        str(row["default_code"]): int(row["id"])
        for row in rows
        if isinstance(row.get("id"), int) and row.get("default_code")
    }


def _resolve_location_ids(
    client: OdooWarehouseClient,
    location_codes: set[str],
    *,
    barcode_by_location_code: dict[str, str],
) -> dict[str, int]:
    result: dict[str, int] = {}
    for code in sorted(location_codes):
        barcode = barcode_by_location_code.get(code)
        if not barcode:
            raise RuntimeError(f"No deterministic warehouse barcode exists for fixture location {code}.")
        location = _search_one(
            client,
            "stock.location",
            [["barcode", "=", barcode]],
            ["id", "barcode", "complete_name"],
        )
        if not location or not isinstance(location.get("id"), int):
            raise RuntimeError(
                f"Odoo location for fixture {code} / barcode {barcode} was not found."
            )
        result[code] = int(location["id"])
    return result


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
    if existing and isinstance(existing.get("id"), int):
        return int(existing["id"]), "exists"
    lot_id = client.execute_kw(
        "stock.lot",
        "create",
        args=[{"name": lot_name, "product_id": product_id}],
    )
    return int(lot_id), "created"


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
    ]
    domain.append(["lot_id", "=", lot_id if lot_id is not None else False])
    existing = _search_one(
        client,
        "stock.quant",
        domain,
        ["id", "quantity", "reserved_quantity", "lot_id"],
    )
    context = {"inventory_mode": True}
    if existing and isinstance(existing.get("id"), int):
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


def _reservation_lines_remaining(client: OdooWarehouseClient, picking_ids: list[int]) -> int:
    rows = client.search_read(
        "stock.move.line",
        domain=[
            ["picking_id", "in", picking_ids],
            ["state", "not in", ["done", "cancel"]],
            ["quantity", ">", 0],
        ],
        fields=["id"],
        limit=10000,
    )
    return len(rows)


def migrate_tracking(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    origin_prefix: str = DEFAULT_ORIGIN_PREFIX,
    apply: bool = False,
) -> dict[str, Any]:
    if not (data_dir / "manifest.json").exists():
        generate(data_dir)

    client = OdooWarehouseClient.from_env()
    assert_write_guard(client.database, apply=apply)
    scope = _resolve_awia_kitting_scope(client, origin_prefix=origin_prefix)

    fixture_quants = _read_csv(data_dir / "mock_odoo" / "quants.csv")
    targets = build_tracking_targets(
        fixture_quants,
        relevant_tracking_by_code=scope["tracking_by_code"],
    )
    codes = {row["product_code"] for row in targets}
    location_codes = {row["location_code"] for row in targets}
    barcode_by_location_code = _load_location_barcodes(data_dir)

    product_ids = _resolve_product_ids(client, codes)
    location_ids = _resolve_location_ids(
        client,
        location_codes,
        barcode_by_location_code=barcode_by_location_code,
    )

    fixture_quant_pairs = sorted(
        {
            (row["product_code"], row["location_code"])
            for row in fixture_quants
            if row["product_code"] in codes
        }
    )
    lot_targets = [row for row in targets if row["tracking"] == "lot"]
    serial_targets = [row for row in targets if row["tracking"] == "serial"]
    summary: dict[str, Any] = {
        "database": client.database,
        "mode": "apply" if apply else "dry_run",
        "awia_pick_component_transfers": len(scope["pickings"]),
        "reservation_lines_to_rebuild": len(scope["reservation_lines"]),
        "tracked_products": len(codes),
        "lot_tracked_products": len({row["product_code"] for row in lot_targets}),
        "serial_tracked_products": len({row["product_code"] for row in serial_targets}),
        "target_lot_records": len(lot_targets),
        "target_serial_records": len(serial_targets),
        "target_traced_quants": len(targets),
        "anonymous_quant_targets_to_zero": len(fixture_quant_pairs),
        "lots": {"created": 0, "existing": 0},
        "traced_quants": {"created": 0, "updated": 0},
        "anonymous_quants": {"created": 0, "updated": 0},
        "reassigned": False,
        "reservation_lines_after_reassign": None,
    }
    if not apply:
        return summary

    reservation_ids = [
        int(row["id"])
        for row in scope["reservation_lines"]
        if isinstance(row.get("id"), int)
    ]
    if reservation_ids:
        client.execute_kw("stock.move.line", "unlink", args=[reservation_ids])
    remaining = _reservation_lines_remaining(client, scope["picking_ids"])
    if remaining:
        raise RuntimeError(
            f"Refusing inventory migration because {remaining} open reserved move line(s) remain after unreserve."
        )
    print(f"Reservations released: {len(reservation_ids)}", flush=True)

    lot_ids_by_key: dict[tuple[str, str], int] = {}
    for index, target in enumerate(targets, start=1):
        code = target["product_code"]
        product_id = product_ids.get(code)
        location_id = location_ids.get(target["location_code"])
        if not product_id or not location_id:
            raise RuntimeError(f"Could not resolve product/location for tracking target {target}.")
        lot_id, lot_action = _ensure_lot(
            client,
            product_id=product_id,
            lot_name=target["lot_name"],
        )
        summary["lots"][lot_action] += 1
        lot_ids_by_key[(code, target["lot_name"])] = lot_id
        quant_action = _set_quant_target(
            client,
            product_id=product_id,
            location_id=location_id,
            lot_id=lot_id,
            quantity=float(target["quantity"]),
        )
        summary["traced_quants"][quant_action] += 1
        _print_progress("Traced inventory", index, len(targets), apply=True)

    for index, (code, location_code) in enumerate(fixture_quant_pairs, start=1):
        product_id = product_ids.get(code)
        location_id = location_ids.get(location_code)
        if not product_id or not location_id:
            raise RuntimeError(
                f"Could not resolve anonymous quant target for {code} at {location_code}."
            )
        action = _set_quant_target(
            client,
            product_id=product_id,
            location_id=location_id,
            lot_id=None,
            quantity=0.0,
        )
        summary["anonymous_quants"][action] += 1
        _print_progress("Anonymous inventory cleanup", index, len(fixture_quant_pairs), apply=True)

    client.execute_kw("stock.picking", "action_assign", args=[scope["picking_ids"]])
    summary["reassigned"] = True
    summary["reservation_lines_after_reassign"] = _reservation_lines_remaining(
        client, scope["picking_ids"]
    )
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate AWIA tracked component inventory to native Odoo lots/serials."
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--origin-prefix", default=DEFAULT_ORIGIN_PREFIX)
    parser.add_argument("--apply", action="store_true", help="Write to Odoo. Default is dry-run.")
    args = parser.parse_args()
    result = migrate_tracking(
        data_dir=Path(args.data_dir),
        origin_prefix=args.origin_prefix,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
