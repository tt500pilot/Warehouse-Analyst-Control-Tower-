"""Evaluate current vs candidate slotting for untouched AWIA holdout pickings.

This command is read-only with respect to Odoo and the instrumentation sidecar.
It reads current reservations for the requested open Pick Components transfers,
builds a candidate product-to-bin assignment, and runs the same deterministic
virtual-picker model against mock-v1 and the candidate assignment.
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

from app.services.slotting_optimizer import (
    load_slotting_product_metadata,
    optimize_slotting_layout,
)
from app.services.virtual_picker import (
    PickerAssumptions,
    enrich_reservation_lines,
    load_geometry,
    load_simulation_product_metadata,
)
from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report
from scripts.simulate_virtual_picker import _fetch_reservations

DEFAULT_DATA_DIR = ROOT_DIR / "data" / "simulation_sandbox"
DEFAULT_HOLDOUTS = (9, 10, 11, 12)


def _validate_holdouts(report: dict[str, Any], picking_ids: tuple[int, ...]) -> None:
    by_id: dict[int, dict[str, Any]] = {}
    for transaction in report.get("transactions", []):
        for picking in transaction.get("pick_component_transfers", []):
            picking_id = picking.get("picking_id")
            if isinstance(picking_id, int) and not isinstance(picking_id, bool):
                by_id[picking_id] = picking

    missing = [picking_id for picking_id in picking_ids if picking_id not in by_id]
    if missing:
        raise RuntimeError(f"Holdout Pick Components transfers not found: {missing}")

    invalid: list[dict[str, Any]] = []
    for picking_id in picking_ids:
        picking = by_id[picking_id]
        if picking.get("state") != "assigned" or not picking.get("execution_ready"):
            invalid.append(
                {
                    "picking_id": picking_id,
                    "state": picking.get("state"),
                    "execution_ready": picking.get("execution_ready"),
                }
            )
    if invalid:
        raise RuntimeError(
            "Holdout experiment requires all requested transfers to remain assigned and execution-ready: "
            + json.dumps(invalid, sort_keys=True)
        )


def build_holdout_reservations(
    client: OdooWarehouseClient,
    picking_ids: tuple[int, ...],
    data_dir: Path,
) -> tuple[dict[int, list[dict[str, Any]]], Any]:
    geometry = load_geometry(data_dir)
    simulation_product_by_code = load_simulation_product_metadata(data_dir)
    reservations_by_picking: dict[int, list[dict[str, Any]]] = {}

    for picking_id in picking_ids:
        lines, product_by_id = _fetch_reservations(client, picking_id=picking_id)
        reservations_by_picking[picking_id] = enrich_reservation_lines(
            lines,
            product_by_id,
            geometry,
            simulation_product_by_code=simulation_product_by_code,
        )
    return reservations_by_picking, geometry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Compare mock-v1 to a read-only optimized slotting candidate for untouched AWIA holdouts."
    )
    parser.add_argument(
        "--picking-ids",
        nargs="+",
        type=int,
        default=list(DEFAULT_HOLDOUTS),
        help="Holdout picking IDs; defaults to 9 10 11 12.",
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--walking-speed-ft-s", type=float, default=3.5)
    args = parser.parse_args()

    picking_ids = tuple(dict.fromkeys(args.picking_ids))
    if not picking_ids:
        raise ValueError("At least one picking ID is required.")

    client = OdooWarehouseClient.from_env()
    readiness = build_readiness_report(client)
    _validate_holdouts(readiness, picking_ids)
    data_dir = Path(args.data_dir)
    reservations_by_picking, geometry = build_holdout_reservations(
        client,
        picking_ids,
        data_dir,
    )
    assumptions = PickerAssumptions(walking_speed_ft_s=args.walking_speed_ft_s)
    product_metadata = load_slotting_product_metadata(data_dir)

    result = optimize_slotting_layout(
        reservations_by_picking,
        geometry,
        product_metadata,
        assumptions=assumptions,
        seed=args.seed,
    )
    result["database"] = client.database
    result["holdout_picking_ids"] = list(picking_ids)
    result["holdout_preflight"] = {
        "requested": len(picking_ids),
        "all_requested_assigned_and_execution_ready": True,
        "odoo_writes": False,
        "instrumentation_writes": False,
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
