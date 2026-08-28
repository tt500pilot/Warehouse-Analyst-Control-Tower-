"""Run the implementation-aware forward-pick slotting experiment.

Read-only: this command does not move stock, change reservations, or write
instrumentation. It trains on completed baseline pickings 5-8 and evaluates on
untouched holdouts 9-12.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.forward_pick_optimizer import optimize_forward_pick_layout
from app.services.slotting_optimizer import load_slotting_product_metadata
from app.services.virtual_picker import PickerAssumptions
from odoo_client import OdooWarehouseClient
from scripts.check_kitting_execution_readiness import build_readiness_report
from scripts.optimize_holdout_slotting import (
    DEFAULT_DATA_DIR,
    DEFAULT_HOLDOUTS,
    DEFAULT_TRAINING,
    _validate_experiment,
    build_reservations,
)


def main() -> None:
    client = OdooWarehouseClient.from_env()
    readiness = build_readiness_report(client)
    training_ids = tuple(DEFAULT_TRAINING)
    holdout_ids = tuple(DEFAULT_HOLDOUTS)
    _validate_experiment(readiness, training_ids, holdout_ids)

    data_dir = Path(DEFAULT_DATA_DIR)
    training_reservations, geometry = build_reservations(client, training_ids, data_dir)
    holdout_reservations, _ = build_reservations(client, holdout_ids, data_dir)
    product_metadata = load_slotting_product_metadata(data_dir)

    result = optimize_forward_pick_layout(
        client,
        training_reservations_by_picking=training_reservations,
        holdout_reservations_by_picking=holdout_reservations,
        geometry=geometry,
        product_metadata=product_metadata,
        data_dir=data_dir,
        assumptions=PickerAssumptions(),
        seed=42,
    )
    result["database"] = client.database
    result["preflight"] = {
        "training_all_done": True,
        "holdouts_all_assigned_and_execution_ready": True,
        "odoo_writes": False,
        "instrumentation_writes": False,
        "reservation_writes": False,
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
