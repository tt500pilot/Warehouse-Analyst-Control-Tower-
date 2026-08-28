"""Audit mock-v2 slotting recommendations against live Odoo occupancy/capacity.

Read-only: this command does not move stock, unreserve transfers, reassign picks,
or write instrumentation. It rebuilds the same train/holdout slotting candidate,
then checks its recommended target bins against live Odoo quants and deterministic
sandbox capacity metadata.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.slotting_feasibility import audit_slotting_candidate
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
    audit = audit_slotting_candidate(
        client,
        recommendations=optimization["recommendations"],
        holdout_reservations_by_picking=holdout_reservations,
        geometry=geometry,
        data_dir=data_dir,
    )
    result = {
        "database": client.database,
        "candidate_layout_version": optimization["candidate_layout_version"],
        "experimental_design": optimization["experimental_design"],
        "holdout_improvement": optimization["holdout"]["improvement"],
        "feasibility_audit": audit,
        "preflight": {
            "training_all_done": True,
            "holdouts_all_assigned_and_execution_ready": True,
            "odoo_writes": False,
            "instrumentation_writes": False,
        },
    }
    print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
