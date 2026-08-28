from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.production_pilot_readiness import evaluate_production_pilot_readiness


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Evaluate whether a validated mapped area is ready for a controlled read-only production pilot. "
            "This gate never authorizes inventory movement or Odoo writes."
        )
    )
    parser.add_argument("--geometry", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--output", default="data/analysis/production-pilot-readiness.json")
    args = parser.parse_args()

    geometry_path = Path(args.geometry)
    config_path = Path(args.config)
    if not geometry_path.exists():
        raise FileNotFoundError(geometry_path)
    if not config_path.exists():
        raise FileNotFoundError(config_path)

    geometry = json.loads(geometry_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    result = evaluate_production_pilot_readiness(geometry, config)
    result["geometry_file"] = str(geometry_path)
    result["config_file"] = str(config_path)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
