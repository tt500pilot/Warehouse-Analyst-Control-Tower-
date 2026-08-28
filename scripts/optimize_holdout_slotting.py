"""Train AWIA slotting on completed baseline kits and score untouched holdouts.

This command is read-only with respect to Odoo and the instrumentation sidecar.
The candidate layout is learned only from completed baseline Pick Components
transfers (default 5-8). Untouched open transfers (default 9-12) are then used
strictly for evaluation, preventing training/holdout leakage.
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
DEFAULT_TRAINING = (5, 6, 7, 8)
DEFAULT_HOLDOUTS = (9, 10, 11, 12)


def _picking_index(report: dict[str, Any]) -> dict[int, dict[str, Any]]:
    by_id: dict[int, dict[str, Any]] = {}
    for transaction in report.get("transactions", []):
        for picking in transaction.get("pick_component_transfers", []):
            picking_id = picking.get("picking_id")
            if isinstance(picking_id, int) and not isinstance(picking_id, bool):
                by_id[picking_id] = picking
    return by_id


def _validate_experiment(
    report: dict[str, Any],
    training_ids: tuple[int, ...],
    holdout_ids: tuple[int, ...],
) -> None:
    overlap = set(training_ids) & set(holdout_ids)
    if overlap:
        raise RuntimeError(f"Training and holdout IDs overlap: {sorted(overlap)}")

    by_id = _picking_index(report)
    missing = [picking_id for picking_id in (*training_ids, *holdout_ids) if picking_id not in by_id]
    if missing:
        raise RuntimeError(f"Pick Components transfers not found: {missing}")

    invalid_training = [
        {"picking_id": picking_id, "state": by_id[picking_id].get("state")}
        for picking_id in training_ids
        if by_id[picking_id].get("state") != "done"
    ]
    if invalid_training:
        raise RuntimeError(
            "Training transfers must already be completed so they cannot be influenced by the experiment: "
            + json.dumps(invalid_training, sort_keys=True)
        )

    invalid_holdouts: list[dict[str, Any]] = []
    for picking_id in holdout_ids:
        picking = by_id[picking_id]
        if picking.get("state") != "assigned" or not picking.get("execution_ready"):
            invalid_holdouts.append(
                {
                    "picking_id": picking_id,
                    "state": picking.get("state"),
                    "execution_ready": picking.get("execution_ready"),
                }
            )
    if invalid_holdouts:
        raise RuntimeError(
            "Holdout transfers must remain assigned and execution-ready: "
            + json.dumps(invalid_holdouts, sort_keys=True)
        )


def build_reservations(
    client: OdooWarehouseClient,
    picking_ids: tuple[int, ...],
    data_dir: Path,
) -> tuple[dict[int, list[dict[str, Any]]], Any]:
    geometry = load_geometry(data_dir)
    simulation_product_by_code = load_simulation_product_metadata(data_dir)
    reservations_by_picking: dict[int, list[dict[str, Any]]] = {}

    for picking_id in picking_ids:
        lines, product_by_id = _fetch_reservations(client, picking_id=picking_id)
        reservations = enrich_reservation_lines(
            lines,
            product_by_id,
            geometry,
            simulation_product_by_code=simulation_product_by_code,
        )
        if not reservations:
            raise RuntimeError(f"Picking {picking_id} returned no component move lines for simulation.")
        reservations_by_picking[picking_id] = reservations
    return reservations_by_picking, geometry


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train mock-v2 slotting on completed baseline kits and score untouched holdouts."
    )
    parser.add_argument(
        "--training-picking-ids",
        nargs="+",
        type=int,
        default=list(DEFAULT_TRAINING),
        help="Completed baseline picking IDs; defaults to 5 6 7 8.",
    )
    parser.add_argument(
        "--holdout-picking-ids",
        nargs="+",
        type=int,
        default=list(DEFAULT_HOLDOUTS),
        help="Untouched holdout picking IDs; defaults to 9 10 11 12.",
    )
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--walking-speed-ft-s", type=float, default=3.5)
    args = parser.parse_args()

    training_ids = tuple(dict.fromkeys(args.training_picking_ids))
    holdout_ids = tuple(dict.fromkeys(args.holdout_picking_ids))
    if not training_ids or not holdout_ids:
        raise ValueError("Both training and holdout picking IDs are required.")

    client = OdooWarehouseClient.from_env()
    readiness = build_readiness_report(client)
    _validate_experiment(readiness, training_ids, holdout_ids)

    data_dir = Path(args.data_dir)
    training_reservations, geometry = build_reservations(client, training_ids, data_dir)
    holdout_reservations, _ = build_reservations(client, holdout_ids, data_dir)
    assumptions = PickerAssumptions(walking_speed_ft_s=args.walking_speed_ft_s)
    product_metadata = load_slotting_product_metadata(data_dir)

    result = optimize_slotting_layout(
        training_reservations,
        holdout_reservations,
        geometry,
        product_metadata,
        assumptions=assumptions,
        seed=args.seed,
    )
    result["database"] = client.database
    result["preflight"] = {
        "training_picking_ids": list(training_ids),
        "training_all_done": True,
        "holdout_picking_ids": list(holdout_ids),
        "holdouts_all_assigned_and_execution_ready": True,
        "training_holdout_overlap": False,
        "odoo_writes": False,
        "instrumentation_writes": False,
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
