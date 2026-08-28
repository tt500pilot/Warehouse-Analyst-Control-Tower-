"""Execute remaining AWIA Pick Components transfers one at a time with audits.

The runner is intentionally fail-closed:
- dry-run by default
- requires the standard sandbox write guard for --apply
- plans only assigned + execution-ready Pick Components transfers
- executes each transfer through execute_kitting_transfer.execute_one_transfer
- requires completion_verified=True after every transfer
- refreshes readiness after every successful transfer
- stops immediately on an exception, failed audit, or degraded readiness state
- never interprets script runtime as human kitting cycle time
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

from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report
from scripts.execute_kitting_transfer import execute_one_transfer
from scripts.seed_odoo_sandbox import assert_write_guard

DEFAULT_ORIGIN_PREFIX = "AWIA-MOCK-MO-"


def collect_execution_queue(
    report: dict[str, Any],
    *,
    max_transfers: int | None = None,
) -> list[dict[str, Any]]:
    """Return assigned + execution-ready transfers in deterministic ID order."""
    if max_transfers is not None and max_transfers <= 0:
        raise ValueError("max_transfers must be greater than zero when provided.")

    queue: list[dict[str, Any]] = []
    for transaction in report.get("transactions", []):
        for picking in transaction.get("pick_component_transfers", []):
            picking_id = picking.get("picking_id")
            if not isinstance(picking_id, int) or isinstance(picking_id, bool):
                continue
            if picking.get("state") != "assigned" or not picking.get("execution_ready"):
                continue
            queue.append(
                {
                    "picking_id": picking_id,
                    "picking_name": picking.get("picking_name"),
                    "manufacturing_order": transaction.get("manufacturing_order"),
                    "awia_origin": transaction.get("awia_origin"),
                    "finished_product": transaction.get("product"),
                    "component_move_count": picking.get("component_move_count"),
                    "move_line_count": picking.get("move_line_count"),
                }
            )

    queue.sort(key=lambda row: int(row["picking_id"]))
    return queue[:max_transfers] if max_transfers is not None else queue


def _compact_transfer_result(result: dict[str, Any]) -> dict[str, Any]:
    execution = result.get("execution", {})
    selected = result.get("selected_transfer", {})
    audit = result.get("inventory_audit", {})
    return {
        "picking_id": selected.get("picking_id"),
        "picking_name": selected.get("picking_name"),
        "awia_origin": selected.get("awia_origin"),
        "manufacturing_order": selected.get("manufacturing_order"),
        "finished_product": selected.get("finished_product"),
        "post_picking_state": execution.get("post_picking_state"),
        "date_done": execution.get("date_done"),
        "all_stock_moves_done": execution.get("all_stock_moves_done"),
        "tracking_retained": execution.get("tracking_retained"),
        "inventory_audit_passed": execution.get("inventory_audit_passed"),
        "inventory_audit_rows": len(audit.get("rows", [])),
        "completion_verified": execution.get("completion_verified"),
    }


def execute_remaining_transfers(
    *,
    origin_prefix: str = DEFAULT_ORIGIN_PREFIX,
    max_transfers: int | None = None,
    apply: bool = False,
) -> dict[str, Any]:
    client = OdooWarehouseClient.from_env()
    assert_write_guard(client.database, apply=apply)

    initial = build_readiness_report(client, origin_prefix=origin_prefix)
    queue = collect_execution_queue(initial, max_transfers=max_transfers)
    initial_summary = initial["summary"]

    result: dict[str, Any] = {
        "database": client.database,
        "mode": "apply" if apply else "dry_run",
        "initial_state": {
            "pick_component_transfers": initial_summary.get("pick_component_transfers"),
            "open_transfers": initial_summary.get("open_transfers"),
            "completed_transfers": initial_summary.get("completed_transfers"),
            "ready_transfers": initial_summary.get("ready_transfers"),
            "execution_ready_transfers": initial_summary.get("execution_ready_transfers"),
            "all_open_transfers_execution_ready": initial_summary.get(
                "all_open_transfers_execution_ready"
            ),
            "tracking_missing_lines": initial_summary.get("tracking_missing_lines"),
            "reservation_incomplete_moves": initial_summary.get(
                "reservation_incomplete_moves"
            ),
        },
        "planned_transfer_count": len(queue),
        "planned_queue": queue,
        "executed_transfer_count": 0,
        "completed": [],
        "stopped_early": False,
        "stop_reason": None,
        "final_state": None,
        "timing_note": (
            "These are scripted workflow-proof transactions. Their execution timestamps and "
            "runtime are not human kitting cycle-time measurements."
        ),
    }

    if not queue:
        result["stop_reason"] = "No assigned, execution-ready Pick Components transfers remain."
        result["final_state"] = result["initial_state"]
        return result

    if initial_summary.get("all_open_transfers_execution_ready") is not True:
        result["stopped_early"] = True
        result["stop_reason"] = (
            "Initial readiness is degraded; refusing batch execution until all open transfers are ready."
        )
        result["final_state"] = result["initial_state"]
        return result

    if not apply:
        return result

    for planned in queue:
        target_id = int(planned["picking_id"])
        try:
            transfer_result = execute_one_transfer(
                origin_prefix=origin_prefix,
                picking_id=target_id,
                apply=True,
            )
        except Exception as exc:
            result["stopped_early"] = True
            result["stop_reason"] = (
                f"Transfer {target_id} raised {type(exc).__name__}: {exc}"
            )
            break

        compact = _compact_transfer_result(transfer_result)
        result["completed"].append(compact)
        result["executed_transfer_count"] += 1

        if compact.get("completion_verified") is not True:
            result["stopped_early"] = True
            result["stop_reason"] = (
                f"Transfer {target_id} failed its post-validation completion audit."
            )
            break

        refreshed = build_readiness_report(client, origin_prefix=origin_prefix)
        refreshed_summary = refreshed["summary"]
        if (
            refreshed_summary.get("open_transfers", 0) > 0
            and refreshed_summary.get("all_open_transfers_execution_ready") is not True
        ):
            result["stopped_early"] = True
            result["stop_reason"] = (
                f"Readiness degraded after transfer {target_id}; refusing to execute another transfer."
            )
            break

    final = build_readiness_report(client, origin_prefix=origin_prefix)
    final_summary = final["summary"]
    result["final_state"] = {
        "pick_component_transfers": final_summary.get("pick_component_transfers"),
        "open_transfers": final_summary.get("open_transfers"),
        "completed_transfers": final_summary.get("completed_transfers"),
        "ready_transfers": final_summary.get("ready_transfers"),
        "execution_ready_transfers": final_summary.get("execution_ready_transfers"),
        "all_open_transfers_execution_ready": final_summary.get(
            "all_open_transfers_execution_ready"
        ),
        "tracking_missing_lines": final_summary.get("tracking_missing_lines"),
        "reservation_incomplete_moves": final_summary.get("reservation_incomplete_moves"),
        "picking_states": final_summary.get("picking_states"),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Dry-run or execute remaining native AWIA Pick Components transfers one at a time."
    )
    parser.add_argument("--origin-prefix", default=DEFAULT_ORIGIN_PREFIX)
    parser.add_argument(
        "--max-transfers",
        type=int,
        default=None,
        help="Optional cap on how many currently ready transfers to execute.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Execute the planned queue. Default is read-only dry-run.",
    )
    args = parser.parse_args()
    result = execute_remaining_transfers(
        origin_prefix=args.origin_prefix,
        max_transfers=args.max_transfers,
        apply=args.apply,
    )
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
