from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services.mapped_aisle_pilot_decision import evaluate_pilot_decision


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Evaluate REJECT/DEFER/READY_FOR_CONTROLLED_PILOT for mapped-aisle relocation candidates."
    )
    parser.add_argument(
        "--readiness",
        default="data/analysis/aisle-b-relocation-readiness.json",
    )
    parser.add_argument(
        "--economics",
        default="data/analysis/aisle-b-relocation-economics.json",
    )
    parser.add_argument("--setup-minutes", type=float, default=15.0)
    parser.add_argument("--max-payback-pickings", type=float, default=50.0)
    parser.add_argument("--lookback-days", type=int, default=90)
    parser.add_argument(
        "--output",
        default="data/analysis/aisle-b-pilot-decision.json",
    )
    args = parser.parse_args()

    readiness_path = Path(args.readiness)
    economics_path = Path(args.economics)
    if not readiness_path.exists():
        raise FileNotFoundError(readiness_path)
    if not economics_path.exists():
        raise FileNotFoundError(economics_path)

    readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
    economics = json.loads(economics_path.read_text(encoding="utf-8"))
    result = evaluate_pilot_decision(
        readiness,
        economics,
        decision_setup_minutes=args.setup_minutes,
        max_payback_affected_pickings=args.max_payback_pickings,
        lookback_days=args.lookback_days,
    )
    result["readiness_file"] = str(readiness_path)
    result["economics_file"] = str(economics_path)

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
