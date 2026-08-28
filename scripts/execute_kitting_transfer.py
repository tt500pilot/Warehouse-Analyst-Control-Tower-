"""Execute one AWIA native Odoo Pick Components transfer with audit controls.

This is the first transactional kitting proof in the AWIA sandbox. It is
intentionally conservative:

- dry-run by default
- requires the standard sandbox write guard for --apply
- executes exactly one Pick Components transfer per invocation
- requires the transfer to pass the shared execution-readiness preflight
- marks native stock.move.picked=True before validation
- calls Odoo's native stock.picking.button_validate() without bypassing sanity
  checks or fabricating dates
- re-reads Odoo after validation and audits source/destination inventory deltas

The purpose is workflow proof, not a realistic labor-cycle benchmark. The
script performs the picker-complete and validate actions back-to-back, so its
elapsed time must not be interpreted as human kitting duration.
"""

from __future__ import annotations

import argparse
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
from scripts.check_kitting_execution_readiness import build_readiness_report
from scripts.seed_odoo_sandbox import assert_write_guard

DEFAULT_ORIGIN_PREFIX = "AWIA-MOCK-MO-"


def _m2o_id(value: Any) -> int | None:
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
    if isinstance(value, bool):
        return 0.0
    return float(value) if isinstance(value, (int, float)) else 0.0


def _resolved_fields(
    client: OdooWarehouseClient,
    model: str,
    requested: tuple[str, ...],
) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in requested if field in available]


def select_execution_candidate(
    report: dict[str, Any],
    *,
    picking_id: int | None = None,
) -> dict[str, Any]:
    """Return one assigned/execution-ready PBM transfer from a readiness report."""
    candidates: list[dict[str, Any]] = []
    for transaction in report.get("transactions", []):
        for picking in transaction.get("pick_component_transfers", []):
            current_id = picking.get("picking_id")
            if not isinstance(current_id, int) or isinstance(current_id, bool):
                continue
            if picking.get("state") != "assigned" or not picking.get("execution_ready"):
                continue
            candidates.append(
                {
                    "picking_id": current_id,
                    "picking_name": picking.get("picking_name"),
                    "picking_state": picking.get("state"),
                    "source_location": picking.get("source_location"),
                    "destination_location": picking.get("destination_location"),
                    "component_move_count": picking.get("component_move_count"),
                    "move_line_count": picking.get("move_line_count"),
                    "manufacturing_order_id": transaction.get("manufacturing_order_id"),
                    "manufacturing_order": transaction.get("manufacturing_order"),
                    "awia_origin": transaction.get("awia_origin"),
                    "finished_product": transaction.get("product"),
                    "bom": transaction.get("bom"),
                }
            )

    candidates.sort(key=lambda row: int(row["picking_id"]))
    if picking_id is not None:
        for candidate in candidates:
            if candidate["picking_id"] == picking_id:
                return candidate
        raise RuntimeError(
            f"Pick Components transfer {picking_id} is not currently assigned and execution-ready."
        )
    if not candidates:
        raise RuntimeError("No assigned, execution-ready AWIA Pick Components transfer is available.")
    return candidates[0]


def build_expected_inventory_movements(
    move_lines: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Aggregate expected stock deltas by product/source/destination/lot."""
    grouped: dict[tuple[int, int, int, int | None], dict[str, Any]] = {}
    for line in move_lines:
        quantity = _number(line.get("quantity"))
        if quantity <= 0:
            continue
        product_id = _m2o_id(line.get("product_id"))
        source_id = _m2o_id(line.get("location_id"))
        destination_id = _m2o_id(line.get("location_dest_id"))
        lot_id = _m2o_id(line.get("lot_id"))
        if product_id is None or source_id is None or destination_id is None:
            raise RuntimeError(
                f"Move line {line.get('id')} is missing product/source/destination identity."
            )
        key = (product_id, source_id, destination_id, lot_id)
        if key not in grouped:
            grouped[key] = {
                "product_id": product_id,
                "product": _m2o_label(line.get("product_id")),
                "source_location_id": source_id,
                "source_location": _m2o_label(line.get("location_id")),
                "destination_location_id": destination_id,
                "destination_location": _m2o_label(line.get("location_dest_id")),
                "lot_id": lot_id,
                "lot": _m2o_label(line.get("lot_id")),
                "expected_quantity": 0.0,
                "move_line_ids": [],
            }
        grouped[key]["expected_quantity"] += quantity
        if isinstance(line.get("id"), int) and not isinstance(line.get("id"), bool):
            grouped[key]["move_line_ids"].append(int(line["id"]))

    rows = list(grouped.values())
    for row in rows:
        row["expected_quantity"] = round(float(row["expected_quantity"]), 6)
        row["move_line_ids"].sort()
    rows.sort(
        key=lambda row: (
            str(row.get("product") or ""),
            int(row["source_location_id"]),
            int(row["destination_location_id"]),
            int(row["lot_id"] or 0),
        )
    )
    return rows


def _quant_quantity(
    client: OdooWarehouseClient,
    *,
    product_id: int,
    location_id: int,
    lot_id: int | None,
) -> float:
    rows = client.search_read(
        "stock.quant",
        domain=[
            ["product_id", "=", product_id],
            ["location_id", "=", location_id],
            ["lot_id", "=", lot_id if lot_id is not None else False],
        ],
        fields=_resolved_fields(
            client,
            "stock.quant",
            ("id", "quantity", "reserved_quantity", "lot_id"),
        ),
        limit=10000,
    )
    return round(sum(_number(row.get("quantity")) for row in rows), 6)


def snapshot_inventory(
    client: OdooWarehouseClient,
    movements: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    snapshot: list[dict[str, Any]] = []
    for movement in movements:
        row = dict(movement)
        row["source_quantity"] = _quant_quantity(
            client,
            product_id=int(movement["product_id"]),
            location_id=int(movement["source_location_id"]),
            lot_id=movement.get("lot_id"),
        )
        row["destination_quantity"] = _quant_quantity(
            client,
            product_id=int(movement["product_id"]),
            location_id=int(movement["destination_location_id"]),
            lot_id=movement.get("lot_id"),
        )
        snapshot.append(row)
    return snapshot


def audit_inventory_deltas(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    *,
    tolerance: float = 1e-6,
) -> dict[str, Any]:
    after_by_key = {
        (
            int(row["product_id"]),
            int(row["source_location_id"]),
            int(row["destination_location_id"]),
            row.get("lot_id"),
        ): row
        for row in after
    }
    audit_rows: list[dict[str, Any]] = []
    passed = True
    for prior in before:
        key = (
            int(prior["product_id"]),
            int(prior["source_location_id"]),
            int(prior["destination_location_id"]),
            prior.get("lot_id"),
        )
        current = after_by_key.get(key)
        if current is None:
            raise RuntimeError(f"Post-validation inventory snapshot is missing key {key}.")
        expected = float(prior["expected_quantity"])
        source_delta = round(
            float(current["source_quantity"]) - float(prior["source_quantity"]), 6
        )
        destination_delta = round(
            float(current["destination_quantity"])
            - float(prior["destination_quantity"]),
            6,
        )
        row_passed = math.isclose(
            source_delta, -expected, abs_tol=tolerance
        ) and math.isclose(destination_delta, expected, abs_tol=tolerance)
        passed = passed and row_passed
        audit_rows.append(
            {
                "product": prior.get("product"),
                "lot": prior.get("lot"),
                "source_location": prior.get("source_location"),
                "destination_location": prior.get("destination_location"),
                "expected_quantity": expected,
                "source_before": prior["source_quantity"],
                "source_after": current["source_quantity"],
                "source_delta": source_delta,
                "destination_before": prior["destination_quantity"],
                "destination_after": current["destination_quantity"],
                "destination_delta": destination_delta,
                "passed": row_passed,
            }
        )
    return {"passed": passed, "rows": audit_rows}


def _fetch_target_move_lines(
    client: OdooWarehouseClient,
    *,
    picking_id: int,
) -> list[dict[str, Any]]:
    fields = (
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
        "package_id",
        "result_package_id",
        "owner_id",
    )
    return client.search_read(
        "stock.move.line",
        domain=[["picking_id", "=", picking_id]],
        fields=_resolved_fields(client, "stock.move.line", fields),
        limit=5000,
        order="id asc",
    )


def _fetch_picking_state(
    client: OdooWarehouseClient,
    *,
    picking_id: int,
) -> dict[str, Any]:
    rows = client.search_read(
        "stock.picking",
        domain=[["id", "=", picking_id]],
        fields=_resolved_fields(
            client,
            "stock.picking",
            ("id", "name", "origin", "state", "date_done", "picking_type_id"),
        ),
        limit=1,
    )
    if not rows:
        raise RuntimeError(f"Pick Components transfer {picking_id} no longer exists.")
    return rows[0]


def execute_one_transfer(
    *,
    origin_prefix: str = DEFAULT_ORIGIN_PREFIX,
    picking_id: int | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    client = OdooWarehouseClient.from_env()
    assert_write_guard(client.database, apply=apply)

    readiness = build_readiness_report(client, origin_prefix=origin_prefix)
    candidate = select_execution_candidate(readiness, picking_id=picking_id)
    target_id = int(candidate["picking_id"])
    lines_before = _fetch_target_move_lines(client, picking_id=target_id)
    if not lines_before:
        raise RuntimeError(f"Pick Components transfer {target_id} has no detailed move lines.")

    expected_movements = build_expected_inventory_movements(lines_before)
    inventory_before = snapshot_inventory(client, expected_movements)
    move_ids = sorted(
        {
            move_id
            for line in lines_before
            for move_id in [_m2o_id(line.get("move_id"))]
            if move_id is not None
        }
    )
    tracked_lines = sum(
        1
        for line in lines_before
        if str(line.get("tracking") or "none") in {"lot", "serial"}
        and _number(line.get("quantity")) > 0
    )
    total_quantity = round(
        sum(_number(line.get("quantity")) for line in lines_before), 6
    )

    result: dict[str, Any] = {
        "database": client.database,
        "mode": "apply" if apply else "dry_run",
        "selected_transfer": candidate,
        "preflight": {
            "all_transfers_execution_ready": readiness["summary"].get(
                "all_transfers_execution_ready"
            ),
            "execution_ready_transfers": readiness["summary"].get(
                "execution_ready_transfers"
            ),
            "tracking_missing_lines": readiness["summary"].get(
                "tracking_missing_lines"
            ),
            "reservation_incomplete_moves": readiness["summary"].get(
                "reservation_incomplete_moves"
            ),
        },
        "target": {
            "stock_move_count": len(move_ids),
            "move_line_count": len(lines_before),
            "tracked_move_lines": tracked_lines,
            "total_reserved_quantity": total_quantity,
            "inventory_audit_keys": len(expected_movements),
        },
        "execution": {
            "would_mark_moves_picked": len(move_ids),
            "would_call_button_validate": True,
            "native_sanity_checks_bypassed": False,
            "completed": False,
        },
        "timing_note": (
            "This is a workflow proof only. Picked and validate are executed back-to-back; "
            "do not interpret the resulting duration as human kitting cycle time."
        ),
    }
    if not apply:
        return result

    if not move_ids:
        raise RuntimeError(f"Pick Components transfer {target_id} has no stock moves to mark picked.")

    client.execute_kw(
        "stock.move",
        "write",
        args=[move_ids, {"picked": True}],
    )
    lines_picked = _fetch_target_move_lines(client, picking_id=target_id)
    unpicked_positive_lines = [
        int(line["id"])
        for line in lines_picked
        if _number(line.get("quantity")) > 0
        and not bool(line.get("picked"))
        and isinstance(line.get("id"), int)
        and not isinstance(line.get("id"), bool)
    ]
    if unpicked_positive_lines:
        raise RuntimeError(
            "Odoo did not propagate picked=True to all detailed operations; "
            f"unpicked move lines: {unpicked_positive_lines[:20]}."
        )

    validation_result = client.execute_kw(
        "stock.picking",
        "button_validate",
        args=[[target_id]],
    )
    picking_after = _fetch_picking_state(client, picking_id=target_id)
    lines_after = _fetch_target_move_lines(client, picking_id=target_id)
    inventory_after = snapshot_inventory(client, expected_movements)
    inventory_audit = audit_inventory_deltas(inventory_before, inventory_after)

    post_move_ids = sorted(
        {
            move_id
            for line in lines_after
            for move_id in [_m2o_id(line.get("move_id"))]
            if move_id is not None
        }
    )
    post_moves: list[dict[str, Any]] = []
    if post_move_ids:
        post_moves = client.search_read(
            "stock.move",
            domain=[["id", "in", post_move_ids]],
            fields=_resolved_fields(
                client,
                "stock.move",
                ("id", "state", "picked", "product_id", "quantity", "product_uom_qty"),
            ),
            limit=5000,
            order="id asc",
        )

    picking_done = picking_after.get("state") == "done"
    moves_done = bool(post_moves) and all(move.get("state") == "done" for move in post_moves)
    tracking_retained = all(
        str(line.get("tracking") or "none") not in {"lot", "serial"}
        or _number(line.get("quantity")) <= 0
        or _m2o_id(line.get("lot_id")) is not None
        or bool(str(line.get("lot_name") or "").strip())
        for line in lines_after
    )
    completion_verified = (
        picking_done and moves_done and tracking_retained and inventory_audit["passed"]
    )

    result["execution"].update(
        {
            "moves_marked_picked": len(move_ids),
            "validation_rpc_result": validation_result,
            "post_picking_state": picking_after.get("state"),
            "date_done": picking_after.get("date_done"),
            "all_stock_moves_done": moves_done,
            "tracking_retained": tracking_retained,
            "inventory_audit_passed": inventory_audit["passed"],
            "completion_verified": completion_verified,
            "completed": picking_done,
        }
    )
    result["inventory_audit"] = inventory_audit
    if not completion_verified:
        result["warning"] = (
            "The transfer did not satisfy every post-validation audit check. "
            "Do not execute another transfer until this result is reviewed."
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run or execute one native AWIA Odoo Pick Components transfer."
    )
    parser.add_argument("--origin-prefix", default=DEFAULT_ORIGIN_PREFIX)
    parser.add_argument("--picking-id", type=int, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Mark picked and validate one transfer. Default is read-only dry-run.",
    )
    args = parser.parse_args()
    result = execute_one_transfer(
        origin_prefix=args.origin_prefix,
        picking_id=args.picking_id,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
