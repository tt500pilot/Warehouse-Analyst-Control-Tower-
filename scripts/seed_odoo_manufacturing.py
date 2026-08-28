"""Seed native Odoo manufacturing/kitting transactions for the AWIA sandbox.

This script builds on ``seed_odoo_sandbox.py``.  It intentionally creates
current native Odoo records through public ORM/RPC methods instead of
fabricating historical ``done`` records or audit timestamps.

Safety properties:
- dry-run by default
- same sandbox write guard as the master/inventory seeder
- finished goods, BOMs, BOM lines, and MOs are idempotent by stable keys
- Odoo's warehouse manufacturing workflow is inspected before writes
- a switch to two-step manufacturing requires explicit --configure-two-step
- MOs are created and confirmed, but component pickings are not auto-validated
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from odoo_client import OdooWarehouseClient
from scripts.generate_simulation_sandbox import generate
from scripts.seed_odoo_sandbox import assert_write_guard

DEFAULT_DATA_DIR = ROOT_DIR / "data" / "simulation_sandbox"
DEFAULT_MO_COUNT = 12

FINISHED_GOODS: dict[str, dict[str, str]] = {
    "BOM-OTS": {"default_code": "AWIA-FG-OTS", "name": "Orbital Transfer Stage"},
    "BOM-PROP": {"default_code": "AWIA-FG-PROP", "name": "Propulsion Subassembly"},
    "BOM-AV": {"default_code": "AWIA-FG-AV", "name": "Avionics Panel"},
    "BOM-GSE": {"default_code": "AWIA-FG-GSE", "name": "Ground Support Assembly"},
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _m2o_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], int):
        return value[0]
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
    if apply and (current == 1 or current == total or current % 10 == 0):
        print(f"{label}: {current}/{total}", flush=True)


def inspect_manufacturing_workflow(client: OdooWarehouseClient) -> dict[str, Any]:
    """Return the active warehouse manufacturing configuration."""
    fields = [
        "id",
        "name",
        "code",
        "manufacture_steps",
        "lot_stock_id",
        "pbm_loc_id",
        "pbm_type_id",
        "manu_type_id",
    ]
    available = set(client.available_fields("stock.warehouse"))
    selected = [field for field in fields if field in available]
    rows = client.search_read(
        "stock.warehouse",
        domain=[["active", "=", True]],
        fields=selected,
        limit=1,
        order="id asc",
    )
    if not rows:
        raise RuntimeError("No active Odoo warehouse was found in the sandbox database.")
    row = rows[0]
    steps = str(row.get("manufacture_steps") or "unknown")
    return {
        **row,
        "two_step_or_more": steps in {"pbm", "pbm_sam"},
        "separate_pick_components_expected": steps in {"pbm", "pbm_sam"},
    }


def configure_two_step_manufacturing(
    client: OdooWarehouseClient,
    workflow: dict[str, Any],
    *,
    apply: bool,
) -> tuple[dict[str, Any], str]:
    """Switch the sandbox warehouse to Odoo's native PBM two-step workflow."""
    if workflow.get("manufacture_steps") in {"pbm", "pbm_sam"}:
        return workflow, "already_two_step_or_more"
    if not apply:
        return workflow, "would_configure_two_step"
    warehouse_id = workflow.get("id")
    if not isinstance(warehouse_id, int):
        raise RuntimeError("Cannot configure manufacturing steps: warehouse ID is unavailable.")
    client.execute_kw(
        "stock.warehouse",
        "write",
        args=[[warehouse_id], {"manufacture_steps": "pbm"}],
    )
    refreshed = inspect_manufacturing_workflow(client)
    if not refreshed.get("two_step_or_more"):
        raise RuntimeError("Odoo did not activate the expected two-step manufacturing workflow.")
    return refreshed, "configured_two_step"


def _upsert_finished_good(
    client: OdooWarehouseClient,
    *,
    code: str,
    name: str,
    apply: bool,
) -> tuple[dict[str, Any] | None, str]:
    existing = _search_one(
        client,
        "product.product",
        [["default_code", "=", code]],
        ["id", "product_tmpl_id", "uom_id", "default_code", "name"],
    )
    template_fields = set(client.available_fields("product.template"))
    values: dict[str, Any] = {
        "name": name,
        "default_code": code,
        "tracking": "none",
    }
    if "is_storable" in template_fields:
        values["is_storable"] = True
    elif "type" in template_fields:
        values["type"] = "consu"
    values = {key: value for key, value in values.items() if key in template_fields}

    if existing:
        template_id = _m2o_id(existing.get("product_tmpl_id"))
        if apply and template_id:
            client.execute_kw("product.template", "write", args=[[template_id], values])
            existing = _search_one(
                client,
                "product.product",
                [["id", "=", existing["id"]]],
                ["id", "product_tmpl_id", "uom_id", "default_code", "name"],
            )
        return existing, "updated" if apply else "would_update"

    if not apply:
        return None, "would_create"

    template_id = int(client.execute_kw("product.template", "create", args=[values]))
    product = _search_one(
        client,
        "product.product",
        [["product_tmpl_id", "=", template_id]],
        ["id", "product_tmpl_id", "uom_id", "default_code", "name"],
    )
    if not product:
        raise RuntimeError(f"Created finished-good template {code} but no product variant was found.")
    return product, "created"


def _upsert_bom(
    client: OdooWarehouseClient,
    *,
    mock_bom_id: str,
    finished_product: dict[str, Any],
    product_qty: float,
    apply: bool,
) -> tuple[dict[str, Any] | None, str]:
    bom_code = f"AWIA-{mock_bom_id}"
    existing = _search_one(
        client,
        "mrp.bom",
        [["code", "=", bom_code]],
        ["id", "code", "product_tmpl_id", "product_id", "product_uom_id"],
    )
    template_id = _m2o_id(finished_product.get("product_tmpl_id"))
    product_id = finished_product.get("id") if isinstance(finished_product.get("id"), int) else None
    uom_id = _m2o_id(finished_product.get("uom_id"))
    if not template_id or not product_id:
        raise RuntimeError(f"Finished-good identity is incomplete for {mock_bom_id}.")

    available = set(client.available_fields("mrp.bom"))
    values: dict[str, Any] = {
        "code": bom_code,
        "product_tmpl_id": template_id,
        "product_id": product_id,
        "product_qty": product_qty,
        "type": "normal",
    }
    if uom_id:
        values["product_uom_id"] = uom_id
    values = {key: value for key, value in values.items() if key in available}

    if existing:
        if apply:
            client.execute_kw("mrp.bom", "write", args=[[existing["id"]], values])
            existing = _search_one(
                client,
                "mrp.bom",
                [["id", "=", existing["id"]]],
                ["id", "code", "product_tmpl_id", "product_id", "product_uom_id"],
            )
        return existing, "updated" if apply else "would_update"

    if not apply:
        return None, "would_create"
    bom_id = int(client.execute_kw("mrp.bom", "create", args=[values]))
    created = _search_one(
        client,
        "mrp.bom",
        [["id", "=", bom_id]],
        ["id", "code", "product_tmpl_id", "product_id", "product_uom_id"],
    )
    return created, "created"


def _upsert_bom_line(
    client: OdooWarehouseClient,
    *,
    bom_id: int,
    component: dict[str, Any],
    quantity: float,
    sequence: int,
    apply: bool,
) -> str:
    product_id = component.get("id") if isinstance(component.get("id"), int) else None
    uom_id = _m2o_id(component.get("uom_id"))
    if not product_id:
        raise RuntimeError("Component product ID is unavailable while creating a BOM line.")
    existing = _search_one(
        client,
        "mrp.bom.line",
        [["bom_id", "=", bom_id], ["product_id", "=", product_id]],
        ["id", "product_id", "product_qty", "product_uom_id", "sequence"],
    )
    available = set(client.available_fields("mrp.bom.line"))
    values: dict[str, Any] = {
        "bom_id": bom_id,
        "product_id": product_id,
        "product_qty": quantity,
        "sequence": sequence,
    }
    if uom_id:
        values["product_uom_id"] = uom_id
    values = {key: value for key, value in values.items() if key in available}

    if existing:
        if apply:
            client.execute_kw("mrp.bom.line", "write", args=[[existing["id"]], values])
        return "updated" if apply else "would_update"
    if not apply:
        return "would_create"
    client.execute_kw("mrp.bom.line", "create", args=[values])
    return "created"


def _build_mo_plan(rows: list[dict[str, str]], count: int) -> list[dict[str, str]]:
    if count <= 0:
        raise ValueError("MO count must be greater than zero.")
    if not rows:
        raise ValueError("No manufacturing-order fixtures are available.")
    planned: list[dict[str, str]] = []
    for index, row in enumerate(rows[:count], start=1):
        planned.append(
            {
                "sequence": str(index),
                "mock_bom_id": row["mock_bom_id"],
                "program": row["program"],
                "origin": f"AWIA-MOCK-MO-{index:03d}",
            }
        )
    return planned


def _upsert_and_confirm_mo(
    client: OdooWarehouseClient,
    *,
    origin: str,
    finished_product_id: int,
    bom_id: int,
    manufacture_picking_type_id: int | None,
    apply: bool,
) -> tuple[dict[str, Any] | None, str]:
    existing = _search_one(
        client,
        "mrp.production",
        [["origin", "=", origin]],
        ["id", "name", "origin", "state", "bom_id", "product_id", "picking_type_id", "picking_ids"],
    )
    if existing:
        if apply and existing.get("state") == "draft":
            client.execute_kw("mrp.production", "action_confirm", args=[[existing["id"]]])
            existing = _search_one(
                client,
                "mrp.production",
                [["id", "=", existing["id"]]],
                ["id", "name", "origin", "state", "bom_id", "product_id", "picking_type_id", "picking_ids"],
            )
            return existing, "confirmed_existing"
        return existing, "exists"

    if not apply:
        return None, "would_create_and_confirm"

    available = set(client.available_fields("mrp.production"))
    values: dict[str, Any] = {
        "origin": origin,
        "product_id": finished_product_id,
        "product_qty": 1.0,
        "bom_id": bom_id,
    }
    if manufacture_picking_type_id:
        values["picking_type_id"] = manufacture_picking_type_id
    values = {key: value for key, value in values.items() if key in available}
    mo_id = int(client.execute_kw("mrp.production", "create", args=[values]))
    client.execute_kw("mrp.production", "action_confirm", args=[[mo_id]])
    created = _search_one(
        client,
        "mrp.production",
        [["id", "=", mo_id]],
        ["id", "name", "origin", "state", "bom_id", "product_id", "picking_type_id", "picking_ids"],
    )
    return created, "created_and_confirmed"


def seed_manufacturing(
    *,
    data_dir: Path = DEFAULT_DATA_DIR,
    apply: bool = False,
    configure_two_step: bool = False,
    mo_count: int = DEFAULT_MO_COUNT,
) -> dict[str, Any]:
    if not (data_dir / "manifest.json").exists():
        generate(data_dir)

    client = OdooWarehouseClient.from_env()
    assert_write_guard(client.database, apply=apply)

    workflow_before = inspect_manufacturing_workflow(client)
    workflow = workflow_before
    workflow_action = "unchanged"
    if configure_two_step:
        workflow, workflow_action = configure_two_step_manufacturing(
            client, workflow_before, apply=apply
        )
    elif apply and not workflow_before.get("two_step_or_more"):
        raise RuntimeError(
            "The sandbox warehouse is still in one-step manufacturing. "
            "Rerun with --configure-two-step to enable Odoo's native Pick Components workflow."
        )

    bom_rows = _read_csv(data_dir / "mock_odoo" / "boms.csv")
    bom_line_rows = _read_csv(data_dir / "mock_odoo" / "bom_lines.csv")
    mo_fixture_rows = _read_csv(data_dir / "mock_odoo" / "manufacturing_orders.csv")
    lines_by_bom: dict[str, list[dict[str, str]]] = {}
    for row in bom_line_rows:
        lines_by_bom.setdefault(row["mock_bom_id"], []).append(row)

    summary: dict[str, Any] = {
        "database": client.database,
        "mode": "apply" if apply else "dry_run",
        "warehouse_before": {
            "id": workflow_before.get("id"),
            "name": workflow_before.get("name"),
            "code": workflow_before.get("code"),
            "manufacture_steps": workflow_before.get("manufacture_steps"),
            "separate_pick_components_expected": workflow_before.get("separate_pick_components_expected"),
        },
        "warehouse_after": {
            "manufacture_steps": workflow.get("manufacture_steps"),
            "separate_pick_components_expected": workflow.get("separate_pick_components_expected"),
        },
        "workflow_action": workflow_action,
        "finished_goods": {"created": 0, "updated": 0, "would_create": 0, "would_update": 0},
        "boms": {"created": 0, "updated": 0, "would_create": 0, "would_update": 0},
        "bom_lines": {"created": 0, "updated": 0, "would_create": 0, "would_update": 0},
        "manufacturing_orders": {
            "created_and_confirmed": 0,
            "confirmed_existing": 0,
            "exists": 0,
            "would_create_and_confirm": 0,
        },
        "pick_components": {"records_found": 0, "note": "Pick Components transfers are generated by Odoo's native two-step manufacturing workflow; AWIA does not fabricate them."},
    }

    finished_by_bom: dict[str, dict[str, Any]] = {}
    bom_ids_by_mock: dict[str, int] = {}

    for index, bom_row in enumerate(bom_rows, start=1):
        mock_bom_id = bom_row["mock_bom_id"]
        fg_spec = FINISHED_GOODS[mock_bom_id]
        finished, action = _upsert_finished_good(
            client,
            code=fg_spec["default_code"],
            name=fg_spec["name"],
            apply=apply,
        )
        summary["finished_goods"][action] += 1

        if not apply:
            summary["boms"]["would_create"] += 1
            for _ in lines_by_bom.get(mock_bom_id, []):
                summary["bom_lines"]["would_create"] += 1
            _print_progress("BOM families", index, len(bom_rows), apply=apply)
            continue

        if not finished:
            raise RuntimeError(f"Finished good {fg_spec['default_code']} was not resolved.")
        finished_by_bom[mock_bom_id] = finished
        bom, bom_action = _upsert_bom(
            client,
            mock_bom_id=mock_bom_id,
            finished_product=finished,
            product_qty=float(bom_row["product_qty"]),
            apply=True,
        )
        summary["boms"][bom_action] += 1
        if not bom or not isinstance(bom.get("id"), int):
            raise RuntimeError(f"BOM {mock_bom_id} was not resolved after apply.")
        bom_id = int(bom["id"])
        bom_ids_by_mock[mock_bom_id] = bom_id

        for line_sequence, line in enumerate(lines_by_bom.get(mock_bom_id, []), start=1):
            component = _search_one(
                client,
                "product.product",
                [["default_code", "=", line["component_code"]]],
                ["id", "default_code", "uom_id"],
            )
            if not component:
                raise RuntimeError(
                    f"Component {line['component_code']} is missing. Run seed_odoo_sandbox.py --apply first."
                )
            line_action = _upsert_bom_line(
                client,
                bom_id=bom_id,
                component=component,
                quantity=float(line["quantity"]),
                sequence=line_sequence,
                apply=True,
            )
            summary["bom_lines"][line_action] += 1
        _print_progress("BOM families", index, len(bom_rows), apply=apply)

    mo_plan = _build_mo_plan(mo_fixture_rows, mo_count)
    if not apply:
        summary["manufacturing_orders"]["would_create_and_confirm"] = len(mo_plan)
        return summary

    manufacture_type_id = _m2o_id(workflow.get("manu_type_id"))
    for index, planned in enumerate(mo_plan, start=1):
        mock_bom_id = planned["mock_bom_id"]
        finished = finished_by_bom[mock_bom_id]
        finished_id = int(finished["id"])
        bom_id = bom_ids_by_mock[mock_bom_id]
        _, action = _upsert_and_confirm_mo(
            client,
            origin=planned["origin"],
            finished_product_id=finished_id,
            bom_id=bom_id,
            manufacture_picking_type_id=manufacture_type_id,
            apply=True,
        )
        summary["manufacturing_orders"][action] += 1
        _print_progress("Manufacturing orders", index, len(mo_plan), apply=apply)

    pbm_type_id = _m2o_id(workflow.get("pbm_type_id"))
    if pbm_type_id:
        pickings = client.search_read(
            "stock.picking",
            domain=[
                ["picking_type_id", "=", pbm_type_id],
                ["state", "!=", "cancel"],
            ],
            fields=[
                field
                for field in ("id", "name", "origin", "state", "scheduled_date", "date_done", "picking_type_id")
                if field in set(client.available_fields("stock.picking"))
            ],
            limit=max(200, mo_count * 4),
            order="id desc",
        )
        awia_origins = {row["origin"] for row in mo_plan}
        # Native PBM pickings normally reference the generated MO name rather
        # than AWIA's stable origin.  Count all non-cancelled PBM records here;
        # a later inspection layer will link exact MO/picking identities.
        summary["pick_components"]["records_found"] = len(pickings)
        summary["pick_components"]["planned_awia_origins"] = len(awia_origins)

    return summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed native Odoo BOMs and confirmed manufacturing orders for AWIA kitting tests."
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--apply", action="store_true", help="Write to Odoo. Default is dry-run.")
    parser.add_argument(
        "--configure-two-step",
        action="store_true",
        help="Explicitly switch the sandbox warehouse to Odoo's 'Pick components then manufacture' workflow.",
    )
    parser.add_argument("--mo-count", type=int, default=DEFAULT_MO_COUNT)
    args = parser.parse_args()
    result = seed_manufacturing(
        data_dir=Path(args.data_dir),
        apply=args.apply,
        configure_two_step=args.configure_two_step,
        mo_count=args.mo_count,
    )
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
