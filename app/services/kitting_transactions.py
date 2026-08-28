"""Inspect native Odoo manufacturing-to-kitting transaction relationships.

This module is deliberately descriptive, not transactional.  It links AWIA
manufacturing orders to Odoo's native Pick Components transfers, stock moves,
and reserved move lines without validating or completing any warehouse work.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any


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


def _ids(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, int)]


def _number(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


def inspect_kitting_transactions(
    manufacturing_orders: list[dict[str, Any]],
    pickings: list[dict[str, Any]],
    stock_moves: list[dict[str, Any]],
    move_lines: list[dict[str, Any]],
    *,
    origin_prefix: str = "AWIA-MOCK-MO-",
    picking_type_contains: str = "Pick Components",
) -> dict[str, Any]:
    """Build a read-only MO -> PBM picking -> component reservation report."""
    if origin_prefix:
        mos = [
            row
            for row in manufacturing_orders
            if str(row.get("origin") or "").startswith(origin_prefix)
        ]
    else:
        mos = list(manufacturing_orders)

    picking_token = picking_type_contains.strip().lower()
    if picking_token:
        pbm_pickings = [
            row
            for row in pickings
            if picking_token in (_m2o_label(row.get("picking_type_id")) or "").lower()
        ]
    else:
        pbm_pickings = list(pickings)

    picking_by_id = {
        row["id"]: row for row in pbm_pickings if isinstance(row.get("id"), int)
    }
    picking_by_origin: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in pbm_pickings:
        origin = str(row.get("origin") or "")
        if origin:
            picking_by_origin[origin].append(row)

    moves_by_picking: dict[int, list[dict[str, Any]]] = defaultdict(list)
    move_by_id: dict[int, dict[str, Any]] = {}
    for row in stock_moves:
        move_id = row.get("id")
        if isinstance(move_id, int):
            move_by_id[move_id] = row
        picking_id = _m2o_id(row.get("picking_id"))
        if picking_id is not None:
            moves_by_picking[picking_id].append(row)

    lines_by_move: dict[int, list[dict[str, Any]]] = defaultdict(list)
    lines_by_picking: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in move_lines:
        move_id = _m2o_id(row.get("move_id"))
        picking_id = _m2o_id(row.get("picking_id"))
        if move_id is not None:
            lines_by_move[move_id].append(row)
        if picking_id is not None:
            lines_by_picking[picking_id].append(row)

    transactions: list[dict[str, Any]] = []
    linked_picking_ids: set[int] = set()
    mo_states: Counter[str] = Counter()
    picking_states: Counter[str] = Counter()
    total_component_moves = 0
    total_move_lines = 0

    for mo in sorted(mos, key=lambda row: int(row.get("id") or 0)):
        mo_state = str(mo.get("state") or "unknown")
        mo_states[mo_state] += 1
        direct_ids = _ids(mo.get("picking_ids"))
        linked = [picking_by_id[picking_id] for picking_id in direct_ids if picking_id in picking_by_id]
        link_method = "mrp.production.picking_ids" if linked else None

        if not linked:
            mo_name = str(mo.get("name") or "")
            linked = list(picking_by_origin.get(mo_name, []))
            if linked:
                link_method = "stock.picking.origin"

        picking_rows: list[dict[str, Any]] = []
        for picking in linked:
            picking_id = picking.get("id")
            if not isinstance(picking_id, int):
                continue
            linked_picking_ids.add(picking_id)
            picking_state = str(picking.get("state") or "unknown")
            picking_states[picking_state] += 1

            component_rows: list[dict[str, Any]] = []
            for move in sorted(moves_by_picking.get(picking_id, []), key=lambda row: int(row.get("id") or 0)):
                move_id = move.get("id")
                reserved_lines = lines_by_move.get(move_id, []) if isinstance(move_id, int) else []
                source_locations = sorted(
                    {
                        label
                        for label in (_m2o_label(line.get("location_id")) for line in reserved_lines)
                        if label
                    }
                )
                destination_locations = sorted(
                    {
                        label
                        for label in (_m2o_label(line.get("location_dest_id")) for line in reserved_lines)
                        if label
                    }
                )
                reserved_quantity = round(sum(_number(line.get("quantity")) for line in reserved_lines), 6)
                component_rows.append(
                    {
                        "stock_move_id": move_id,
                        "product_id": _m2o_id(move.get("product_id")),
                        "product": _m2o_label(move.get("product_id")),
                        "demand_quantity": _number(move.get("product_uom_qty")),
                        "reserved_line_quantity": reserved_quantity,
                        "move_state": move.get("state"),
                        "picked": bool(move.get("picked", False)),
                        "planned_source_location": _m2o_label(move.get("location_id")),
                        "planned_destination_location": _m2o_label(move.get("location_dest_id")),
                        "reserved_source_locations": source_locations,
                        "reserved_destination_locations": destination_locations,
                        "reservation_line_count": len(reserved_lines),
                    }
                )
            total_component_moves += len(component_rows)
            total_move_lines += len(lines_by_picking.get(picking_id, []))
            picking_rows.append(
                {
                    "picking_id": picking_id,
                    "picking_name": picking.get("name"),
                    "origin": picking.get("origin"),
                    "state": picking_state,
                    "picking_type": _m2o_label(picking.get("picking_type_id")),
                    "source_location": _m2o_label(picking.get("location_id")),
                    "destination_location": _m2o_label(picking.get("location_dest_id")),
                    "create_date": picking.get("create_date"),
                    "scheduled_date": picking.get("scheduled_date"),
                    "date_done": picking.get("date_done"),
                    "component_move_count": len(component_rows),
                    "move_line_count": len(lines_by_picking.get(picking_id, [])),
                    "components": component_rows,
                }
            )

        transactions.append(
            {
                "manufacturing_order_id": mo.get("id"),
                "manufacturing_order": mo.get("name"),
                "awia_origin": mo.get("origin"),
                "state": mo_state,
                "product": _m2o_label(mo.get("product_id")),
                "bom": _m2o_label(mo.get("bom_id")),
                "link_method": link_method,
                "pick_component_transfer_count": len(picking_rows),
                "pick_component_transfers": picking_rows,
            }
        )

    linked_mos = sum(1 for row in transactions if row["pick_component_transfer_count"] > 0)
    ready_transfers = picking_states.get("assigned", 0)
    partial_transfers = picking_states.get("partially_available", 0)
    waiting_transfers = sum(
        picking_states.get(state, 0) for state in ("waiting", "confirmed")
    )

    return {
        "summary": {
            "manufacturing_orders": len(transactions),
            "manufacturing_orders_with_pick_components": linked_mos,
            "pick_component_transfers": len(linked_picking_ids),
            "component_moves": total_component_moves,
            "reservation_move_lines": total_move_lines,
            "ready_transfers": ready_transfers,
            "partially_available_transfers": partial_transfers,
            "waiting_transfers": waiting_transfers,
            "all_manufacturing_orders_linked": bool(transactions) and linked_mos == len(transactions),
            "mo_states": dict(sorted(mo_states.items())),
            "picking_states": dict(sorted(picking_states.items())),
        },
        "methodology": {
            "linkage": "Prefer native mrp.production.picking_ids; fall back to stock.picking.origin matching the MO reference.",
            "reservation_quantity": "reserved_line_quantity is the sum of native stock.move.line.quantity on the open transfer. It is reservation/operation evidence, not completed picked quantity unless the move is actually picked/done.",
            "timing": "Open transfers are inspection data only. Cycle-time KPIs remain restricted to completed pickings in /api/kitting-baseline.",
            "mutations": "This inspector is read-only and never validates, picks, or completes Odoo transfers.",
        },
        "transactions": transactions,
    }
