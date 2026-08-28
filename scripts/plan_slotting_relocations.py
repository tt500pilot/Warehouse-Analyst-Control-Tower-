"""Build a dependency-aware, read-only relocation plan for AWIA mock-v2 slotting.

The command rebuilds the validated train/holdout candidate, then converts its
logical slot recommendations into a live-Odoo-aware transformation plan. It does
not move stock, change reservations, or write instrumentation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.relocation_planner import build_relocation_plan
from app.services.slotting_optimizer import (
    load_slotting_product_metadata,
    optimize_slotting_layout,
)
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
    optimization = optimize_slotting_layout(
        training_reservations,
        holdout_reservations,
        geometry,
        product_metadata,
        assumptions=PickerAssumptions(),
        seed=42,
    )
    plan = build_relocation_plan(
        client,
        recommendations=optimization["recommendations"],
        geometry=geometry,
        data_dir=data_dir,
    )
    result = {
        "database": client.database,
        "candidate_layout_version": optimization["candidate_layout_version"],
        "experimental_design": optimization["experimental_design"],
        "holdout_improvement": optimization["holdout"]["improvement"],
        "relocation_plan": plan,
        "preflight": {
            "training_all_done": True,
            "holdouts_all_assigned_and_execution_ready": True,
            "odoo_writes": False,
            "instrumentation_writes": False,
            "reservation_writes": False,
        },
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
