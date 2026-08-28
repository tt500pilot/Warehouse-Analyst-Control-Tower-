"""CLI for prospective AWIA kitting workflow instrumentation.

Examples:
  python scripts/kitting_instrumentation.py start --picking-id 5
  python scripts/kitting_instrumentation.py event --session-id <id> --type location_arrival --location-code A-01-L1-BA
  python scripts/kitting_instrumentation.py event --session-id <id> --type item_scan --product-code SEAL-218 --quantity 2
  python scripts/kitting_instrumentation.py stage --session-id <id>
  python scripts/kitting_instrumentation.py retag --session-id <id> --route-algorithm-version virtual-picker-nearest-neighbor-v1
  python scripts/kitting_instrumentation.py close --session-id <id>
  python scripts/kitting_instrumentation.py report --session-id <id>
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

from app.services.kitting_instrumentation import (
    KittingEventStore,
    SessionIdentity,
)
from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report


def _candidate_for_picking(client: OdooWarehouseClient, picking_id: int) -> dict[str, Any]:
    report = build_readiness_report(client)
    for transaction in report.get("transactions", []):
        for picking in transaction.get("pick_component_transfers", []):
            if picking.get("picking_id") != picking_id:
                continue
            if picking.get("state") != "assigned" or not picking.get("execution_ready"):
                raise RuntimeError(
                    f"Picking {picking_id} is not currently assigned and execution-ready."
                )
            return {
                "picking_id": picking_id,
                "picking_name": picking.get("picking_name"),
                "manufacturing_order": transaction.get("manufacturing_order"),
                "awia_origin": transaction.get("awia_origin"),
            }
    raise RuntimeError(f"AWIA Pick Components picking {picking_id} was not found.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Record prospective AWIA kitting observation events.")
    sub = parser.add_subparsers(dest="command", required=True)

    start = sub.add_parser("start")
    start.add_argument("--picking-id", type=int, required=True)
    start.add_argument("--operator", default=None)
    start.add_argument("--layout-version", default="mock-v1")
    start.add_argument("--route-algorithm-version", default="manual-observed-v1")
    start.add_argument("--notes", default=None)

    event = sub.add_parser("event")
    event.add_argument("--session-id", required=True)
    event.add_argument("--type", required=True)
    event.add_argument("--move-line-id", type=int, default=None)
    event.add_argument("--product-id", type=int, default=None)
    event.add_argument("--product-code", default=None)
    event.add_argument("--location-id", type=int, default=None)
    event.add_argument("--location-code", default=None)
    event.add_argument("--quantity", type=float, default=None)
    event.add_argument("--note", default=None)

    stage = sub.add_parser("stage")
    stage.add_argument("--session-id", required=True)

    retag = sub.add_parser("retag")
    retag.add_argument("--session-id", required=True)
    retag.add_argument("--operator", default=None)
    retag.add_argument("--layout-version", default=None)
    retag.add_argument("--route-algorithm-version", default=None)
    retag.add_argument("--notes", default=None)

    close = sub.add_parser("close")
    close.add_argument("--session-id", required=True)
    close.add_argument("--cancelled", action="store_true")

    report = sub.add_parser("report")
    report.add_argument("--session-id", required=True)

    active = sub.add_parser("active")
    active.add_argument("--picking-id", type=int, required=True)

    sub.add_parser("summary")

    args = parser.parse_args()
    store = KittingEventStore()

    if args.command == "start":
        client = OdooWarehouseClient.from_env()
        candidate = _candidate_for_picking(client, args.picking_id)
        payload = store.start_session(
            SessionIdentity(**candidate),
            operator=args.operator,
            layout_version=args.layout_version,
            route_algorithm_version=args.route_algorithm_version,
            notes=args.notes,
        )
    elif args.command == "event":
        metadata = {"note": args.note} if args.note else None
        payload = store.append_event(
            args.session_id,
            args.type,
            move_line_id=args.move_line_id,
            product_id=args.product_id,
            product_code=args.product_code,
            location_id=args.location_id,
            location_code=args.location_code,
            quantity=args.quantity,
            metadata=metadata,
        )
    elif args.command == "stage":
        payload = store.append_event(args.session_id, "stage_complete")
    elif args.command == "retag":
        payload = store.update_session_metadata(
            args.session_id,
            operator=args.operator,
            layout_version=args.layout_version,
            route_algorithm_version=args.route_algorithm_version,
            notes=args.notes,
        )
    elif args.command == "close":
        payload = store.close_session(args.session_id, cancelled=args.cancelled)
    elif args.command == "report":
        payload = store.session_report(args.session_id)
    elif args.command == "active":
        payload = store.active_session_for_picking(args.picking_id)
    elif args.command == "summary":
        payload = store.summarize_closed_sessions()
    else:
        raise RuntimeError(f"Unhandled command {args.command}")

    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
