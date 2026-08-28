"""Run a deterministic virtual human-like picker against one AWIA kitting session.

The simulator reads the live native Odoo reservation lines for one execution-ready
Pick Components transfer, plans a route through mock-v1 geometry, and records
synthetic observational events into the AWIA instrumentation sidecar. It does
not validate the Odoo transfer. Odoo execution remains a separate explicit step.

All generated observations are classified as simulated_human_like, not actual
human measurements.
"""

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

from app.services.kitting_instrumentation import KittingEventStore
from app.services.virtual_picker import (
    PickerAssumptions,
    build_virtual_picker_plan,
    enrich_reservation_lines,
    load_geometry,
    load_simulation_product_metadata,
)
from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report

DEFAULT_DATA_DIR = ROOT_DIR / "data" / "simulation_sandbox"


def _resolved_fields(client: OdooWarehouseClient, model: str, fields: tuple[str, ...]) -> list[str]:
    available = set(client.available_fields(model))
    return [field for field in fields if field in available]


def _candidate(report: dict[str, Any], picking_id: int) -> dict[str, Any]:
    for transaction in report.get("transactions", []):
        for picking in transaction.get("pick_component_transfers", []):
            if picking.get("picking_id") != picking_id:
                continue
            if picking.get("state") != "assigned" or not picking.get("execution_ready"):
                raise RuntimeError(
                    f"Picking {picking_id} is not assigned and execution-ready."
                )
            return {
                "picking_id": picking_id,
                "picking_name": picking.get("picking_name"),
                "manufacturing_order": transaction.get("manufacturing_order"),
                "awia_origin": transaction.get("awia_origin"),
            }
    raise RuntimeError(f"AWIA Pick Components picking {picking_id} was not found.")


def _parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _fetch_reservations(
    client: OdooWarehouseClient,
    *,
    picking_id: int,
) -> tuple[list[dict[str, Any]], dict[int, dict[str, Any]]]:
    line_fields = (
        "id",
        "picking_id",
        "move_id",
        "product_id",
        "quantity",
        "tracking",
        "lot_id",
        "lot_name",
        "location_id",
        "location_dest_id",
        "state",
    )
    lines = client.search_read(
        "stock.move.line",
        domain=[["picking_id", "=", picking_id], ["quantity", ">", 0]],
        fields=_resolved_fields(client, "stock.move.line", line_fields),
        limit=5000,
        order="id asc",
    )
    product_ids = sorted(
        {
            value[0] if isinstance(value, (list, tuple)) else value
            for line in lines
            for value in [line.get("product_id")]
            if isinstance(value, int)
            or (
                isinstance(value, (list, tuple))
                and value
                and isinstance(value[0], int)
            )
        }
    )
    product_fields = (
        "id",
        "default_code",
        "name",
        "tracking",
        "x_is_flight_critical",
    )
    products = client.search_read(
        "product.product",
        domain=[["id", "in", product_ids]],
        fields=_resolved_fields(client, "product.product", product_fields),
        limit=5000,
        order="id asc",
    )
    return lines, {
        int(row["id"]): row
        for row in products
        if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool)
    }


def simulate_picker(
    *,
    picking_id: int,
    session_id: str | None = None,
    apply_events: bool = False,
    data_dir: Path = DEFAULT_DATA_DIR,
    seed: int = 42,
    assumptions: PickerAssumptions | None = None,
) -> dict[str, Any]:
    client = OdooWarehouseClient.from_env()
    readiness = build_readiness_report(client)
    candidate = _candidate(readiness, picking_id)
    store = KittingEventStore()

    session = store.get_session(session_id) if session_id else store.active_session_for_picking(picking_id)
    if session is None:
        raise RuntimeError(
            f"Picking {picking_id} does not have an active/staged instrumentation session. Start one first."
        )
    if int(session["picking_id"]) != picking_id:
        raise RuntimeError(
            f"Session {session['session_id']} belongs to picking {session['picking_id']}, not {picking_id}."
        )
    if session["status"] != "active":
        raise RuntimeError(
            f"Session {session['session_id']} is {session['status']!r}; virtual picker requires an active session."
        )

    lines, product_by_id = _fetch_reservations(client, picking_id=picking_id)
    geometry = load_geometry(data_dir)
    simulation_product_by_code = load_simulation_product_metadata(data_dir)
    reservations = enrich_reservation_lines(
        lines,
        product_by_id,
        geometry,
        simulation_product_by_code=simulation_product_by_code,
    )
    plan = build_virtual_picker_plan(
        reservations,
        geometry,
        picking_id=picking_id,
        assumptions=assumptions,
        seed=seed,
    )

    result: dict[str, Any] = {
        "database": client.database,
        "mode": "apply_events" if apply_events else "dry_run",
        "classification": "simulated_human_like",
        "criticality_source": "simulation_fixture_by_default_code",
        "session_id": session["session_id"],
        "picking": candidate,
        "plan": plan,
        "events_written": 0,
        "stage_complete_written": False,
        "odoo_transfer_validated": False,
        "note": (
            "This command simulates a human-like picker and writes only AWIA sidecar observations. "
            "It does not validate or complete the native Odoo transfer."
        ),
    }
    if not apply_events:
        return result

    session = store.update_session_metadata(
        str(session["session_id"]),
        route_algorithm_version=str(plan["simulator_version"]),
    )
    started_at = _parse_iso(str(session["started_at"]))
    metadata_base = {
        "classification": "simulated_human_like",
        "simulator_version": plan["simulator_version"],
        "seed": seed,
        "preproduction_proxy": plan["routing"]["preproduction_proxy"],
    }

    for stop in plan["stops"]:
        arrival_at = started_at + timedelta(seconds=float(stop["arrival_elapsed_seconds"]))
        store.append_event(
            str(session["session_id"]),
            "location_arrival",
            occurred_at=arrival_at,
            move_line_id=stop.get("move_line_id"),
            product_id=stop.get("product_id"),
            product_code=stop.get("product_code"),
            location_code=stop.get("location_tail"),
            metadata={
                **metadata_base,
                "sequence": stop["sequence"],
                "distance_ft": stop["distance_ft"],
                "path_nodes": stop["path_nodes"],
                "travel_seconds": stop["travel_seconds"],
                "search_seconds": stop["search_seconds"],
                "handling_seconds": stop["handling_seconds"],
            },
        )
        result["events_written"] += 1

        scan_at = started_at + timedelta(seconds=float(stop["scan_elapsed_seconds"]))
        store.append_event(
            str(session["session_id"]),
            "item_scan",
            occurred_at=scan_at,
            move_line_id=stop.get("move_line_id"),
            product_id=stop.get("product_id"),
            product_code=stop.get("product_code"),
            location_code=stop.get("location_tail"),
            quantity=float(stop["quantity"]),
            metadata={
                **metadata_base,
                "sequence": stop["sequence"],
                "tracking": stop.get("tracking"),
                "lot": stop.get("lot"),
                "flight_critical": stop.get("flight_critical"),
                "criticality_source": stop.get("criticality_source"),
                "scan_seconds": stop["scan_seconds"],
            },
        )
        result["events_written"] += 1

    stage_at = started_at + timedelta(
        seconds=float(plan["return_to_stage"]["stage_complete_elapsed_seconds"])
    )
    store.append_event(
        str(session["session_id"]),
        "stage_complete",
        occurred_at=stage_at,
        location_code="WH/Pre-Production",
        metadata={
            **metadata_base,
            "total_distance_ft": plan["summary"]["total_distance_ft"],
            "simulated_start_to_stage_minutes": plan["summary"]["simulated_start_to_stage_minutes"],
            "return_path_nodes": plan["return_to_stage"]["path_nodes"],
        },
    )
    result["events_written"] += 1
    result["stage_complete_written"] = True
    result["session_report"] = store.session_report(str(session["session_id"]))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or record a deterministic human-like virtual picker run."
    )
    parser.add_argument("--picking-id", type=int, required=True)
    parser.add_argument("--session-id", default=None)
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--walking-speed-ft-s", type=float, default=3.5)
    parser.add_argument("--apply-events", action="store_true")
    args = parser.parse_args()

    assumptions = PickerAssumptions(walking_speed_ft_s=args.walking_speed_ft_s)
    payload = simulate_picker(
        picking_id=args.picking_id,
        session_id=args.session_id,
        apply_events=args.apply_events,
        data_dir=Path(args.data_dir),
        seed=args.seed,
        assumptions=assumptions,
    )
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
