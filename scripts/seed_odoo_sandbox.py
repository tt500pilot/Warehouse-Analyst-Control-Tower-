"""Seed deterministic AWIA master/inventory data into a dedicated Odoo sandbox.

Safety properties:
- dry-run by default
- requires --apply plus AWIA_ALLOW_SANDBOX_WRITES=true
- refuses databases that are not explicitly sandbox-named
- upserts by stable business keys so reruns are idempotent
- does not fabricate historical completed transfers or audit timestamps
"""

from __future__ import annotations

import argparse
import csv
import os
from pathlib import Path
from typing import Any

from odoo_client import OdooWarehouseClient
from scripts.generate_simulation_sandbox import generate

DEFAULT_DATA_DIR = Path("data/simulation_sandbox")
DEFAULT_ALLOWED_DATABASES = {"awia_mock", "awia_sandbox", "awia_test"}


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def allowed_databases() -> set[str]:
    configured = {
        value.strip()
        for value in os.getenv("AWIA_SANDBOX_DATABASE_ALLOWLIST", "").split(",")
        if value.strip()
    }
    return configured or set(DEFAULT_ALLOWED_DATABASES)


def assert_write_guard(database: str, *, apply: bool) -> None:
    """Refuse accidental writes outside an explicitly designated sandbox."""
    if not apply:
        return
    allowed = allowed_databases()
    if database not in allowed:
        raise RuntimeError(
            f"Refusing AWIA sandbox writes to database {database!r}. "
            f"Allowed sandbox databases: {sorted(allowed)}."
        )
    if not _truthy(os.getenv("AWIA_ALLOW_SANDBOX_WRITES")):
        raise RuntimeError(
            "Refusing AWIA sandbox writes until AWIA_ALLOW_SANDBOX_WRITES=true is set."
        )


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _field_subset(client: OdooWarehouseClient, model: str, values: dict[str, Any]) -> dict[str, Any]:
    available = set(client.available_fields(model))
    return {key: value for key, value in values.items() if key in available}


def _search_one(client: OdooWarehouseClient, model: str, domain: list[Any], fields: list[str]) -> dict[str, Any] | None:
    available = set(client.available_fields(model))
    selected = [field for field in fields if field in available]
    records = client.search_read(model, domain=domain, fields=selected, limit=1)
    return records[0] if records else None


def _upsert_location(
    client: OdooWarehouseClient,
    *,
    parent_id: int,
    name: str,
    barcode: str,
    apply: bool,
) -> tuple[int | None, str]:
    existing = _search_one(client, "stock.location", [["barcode", "=", barcode]], ["id", "name", "barcode"])
    values = _field_subset(
        client,
        "stock.location",
        {"name": name, "barcode": barcode, "location_id": parent_id, "usage": "internal"},
    )
    if existing:
        if apply:
            client.execute_kw("stock.location", "write", args=[[existing["id"]], values])
        return existing["id"], "updated" if apply else "would_update"
    if not apply:
        return None, "would_create"
    new_id = client.execute_kw("stock.location", "create", args=[values])
    return int(new_id), "created"


def _ensure_mock_root(client: OdooWarehouseClient, *, apply: bool) -> tuple[int | None, str]:
    existing = _search_one(client, "stock.location", [["barcode", "=", "AWIA-MOCK-ROOT"]], ["id", "name"])
    if existing:
        return existing["id"], "exists"

    parents = client.search_read(
        "stock.location",
        domain=[["usage", "=", "internal"]],
        fields=[field for field in ("id", "name", "complete_name") if field in set(client.available_fields("stock.location"))],
        limit=100,
        order="id asc",
    )
    stock_parent = next(
        (
            row
            for row in parents
            if str(row.get("complete_name") or row.get("name") or "").endswith("Stock")
        ),
        parents[0] if parents else None,
    )
    if stock_parent is None:
        raise RuntimeError("No internal Odoo stock location exists; initialize Inventory/Warehouse before seeding AWIA.")

    if not apply:
        return None, "would_create"
    values = _field_subset(
        client,
        "stock.location",
        {"name": "AWIA Mock", "barcode": "AWIA-MOCK-ROOT", "location_id": stock_parent["id"], "usage": "internal"},
    )
    new_id = client.execute_kw("stock.location", "create", args=[values])
    return int(new_id), "created"


def _upsert_product(client: OdooWarehouseClient, row: dict[str, str], *, apply: bool) -> tuple[int | None, str]:
    code = row["default_code"]
    existing = _search_one(client, "product.product", [["default_code", "=", code]], ["id", "product_tmpl_id"])

    template_values: dict[str, Any] = {
        "name": row["name"],
        "default_code": code,
        "standard_price": float(row["standard_price"]),
        "tracking": row["tracking"],
        "weight": float(row["weight_lb"]),
    }
    template_fields = set(client.available_fields("product.template"))
    if "is_storable" in template_fields:
        template_values["is_storable"] = True
    elif "type" in template_fields:
        template_values["type"] = "consu"
    template_values = {key: value for key, value in template_values.items() if key in template_fields}

    if existing:
        template_id_value = existing.get("product_tmpl_id")
        template_id = template_id_value[0] if isinstance(template_id_value, (list, tuple)) else template_id_value
        if apply and isinstance(template_id, int):
            client.execute_kw("product.template", "write", args=[[template_id], template_values])
        return existing["id"], "updated" if apply else "would_update"

    if not apply:
        return None, "would_create"
    template_id = client.execute_kw("product.template", "create", args=[template_values])
    product = _search_one(client, "product.product", [["product_tmpl_id", "=", template_id]], ["id"])
    if not product:
        raise RuntimeError(f"Odoo created template {template_id} for {code} but no product variant was found.")
    return product["id"], "created"


def seed(*, data_dir: Path = DEFAULT_DATA_DIR, apply: bool = False) -> dict[str, Any]:
    if not (data_dir / "manifest.json").exists():
        generate(data_dir)

    client = OdooWarehouseClient.from_env()
    assert_write_guard(client.database, apply=apply)

    locations = _read_csv(data_dir / "mock_warehouse" / "locations.csv")
    stations = _read_csv(data_dir / "mock_warehouse" / "stations.csv")
    products = _read_csv(data_dir / "mock_odoo" / "products.csv")
    quants = _read_csv(data_dir / "mock_odoo" / "quants.csv")

    summary: dict[str, Any] = {
        "database": client.database,
        "mode": "apply" if apply else "dry_run",
        "locations": {"created": 0, "updated": 0, "would_create": 0, "would_update": 0},
        "products": {"created": 0, "updated": 0, "would_create": 0, "would_update": 0},
        "inventory": {"applied": 0, "would_apply": len(quants), "skipped": 0},
        "note": "Historical completed kitting transactions are intentionally not fabricated by this seeder.",
    }

    root_id, root_action = _ensure_mock_root(client, apply=apply)
    if apply and root_id is None:
        raise RuntimeError("Unable to resolve AWIA Mock root location.")

    location_ids_by_code: dict[str, int] = {}
    for row in stations:
        code = f"WH/{row['name']}"
        barcode = row["station_id"]
        if apply:
            location_id, action = _upsert_location(client, parent_id=int(root_id), name=row["name"], barcode=barcode, apply=True)
            if location_id is not None:
                location_ids_by_code[code] = location_id
        else:
            _, action = _upsert_location(client, parent_id=0, name=row["name"], barcode=barcode, apply=False)
        summary["locations"][action] += 1

    for row in locations:
        leaf_name = row["odoo_complete_name"].split("/")[-1]
        if apply:
            location_id, action = _upsert_location(client, parent_id=int(root_id), name=leaf_name, barcode=row["barcode"], apply=True)
            if location_id is not None:
                location_ids_by_code[row["odoo_complete_name"]] = location_id
        else:
            _, action = _upsert_location(client, parent_id=0, name=leaf_name, barcode=row["barcode"], apply=False)
        summary["locations"][action] += 1

    product_ids_by_code: dict[str, int] = {}
    for row in products:
        product_id, action = _upsert_product(client, row, apply=apply)
        summary["products"][action] += 1
        if product_id is not None:
            product_ids_by_code[row["default_code"]] = product_id

    if apply:
        quant_fields = set(client.available_fields("stock.quant"))
        if "inventory_quantity" not in quant_fields:
            summary["inventory"]["skipped"] = len(quants)
            summary["inventory"]["would_apply"] = 0
            summary["inventory"]["warning"] = "stock.quant.inventory_quantity is unavailable; inventory seed skipped."
        else:
            for row in quants:
                product_id = product_ids_by_code.get(row["product_code"])
                location_id = location_ids_by_code.get(row["location_code"])
                if not product_id or not location_id:
                    summary["inventory"]["skipped"] += 1
                    continue
                existing = _search_one(
                    client,
                    "stock.quant",
                    [["product_id", "=", product_id], ["location_id", "=", location_id]],
                    ["id"],
                )
                target_quantity = float(row["quantity"])
                if existing:
                    client.execute_kw("stock.quant", "write", args=[[existing["id"]], {"inventory_quantity": target_quantity}])
                    quant_ids = [existing["id"]]
                else:
                    quant_id = client.execute_kw(
                        "stock.quant",
                        "create",
                        args=[{"product_id": product_id, "location_id": location_id, "inventory_quantity": target_quantity}],
                    )
                    quant_ids = [int(quant_id)]
                client.execute_kw("stock.quant", "action_apply_inventory", args=[quant_ids])
                summary["inventory"]["applied"] += 1
            summary["inventory"]["would_apply"] = 0

    summary["root_location"] = root_action
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed AWIA synthetic master/inventory data into a dedicated Odoo sandbox database.")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--apply", action="store_true", help="Actually write to Odoo. Default is dry-run.")
    args = parser.parse_args()
    result = seed(data_dir=Path(args.data_dir), apply=args.apply)
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
